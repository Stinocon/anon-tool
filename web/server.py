#!/usr/bin/env python3
"""server.py — local web UI for anon-tool. Loopback only, stdlib only, no framework.

The engine is imported (never reimplemented): the CLI, the Pi guard, the skill and this UI all
run the same code, so a fix or a test lands everywhere at once.

    python3 ~/.anon/web/server.py            # -> http://127.0.0.1:1407
    python3 ~/.anon/web/server.py --port 1407

Threat model (see docs/DESIGN.md): the only network surface of the whole tool. It is safe *only*
because it is loopback-only and unauthenticated, so:

  * binds 127.0.0.1; a non-loopback `--host` is refused unless `--allow-lan` is passed explicitly;
  * every request must carry `Host: 127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding);
  * every `/api/*` request must carry the per-run token, injected into the page at load time —
    a page from another origin cannot read it, and custom headers require a CORS preflight we
    never grant (CSRF);
  * a present `Origin` header must be our own;
  * `/api/*` is rate limited (token bucket), so a runaway script cannot pin every worker thread;
  * NO client-supplied filesystem path is ever used: uploaded bytes land in a per-request temp
    directory under a generated name and are deleted afterwards;
  * request bodies are capped; the converter's output is capped WHILE it is produced;
  * no shell, no eval; the server logs no content and no values.
"""

from __future__ import annotations

import argparse
import base64
import zipfile
import json
import os
import signal
import re
import shutil
from urllib.parse import quote, unquote
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WEB_DIR.parent))
import anon  # noqa: E402
import deanon as deanon_engine  # noqa: E402
import suggest as suggest_engine  # noqa: E402


def _dictionary_path(raw: object) -> tuple[str, Path]:
    """Resolve the `file` query parameter to a named dictionary (default: entities).

    An unknown name is a 400, never a fallback: silently saving to the wrong dictionary would
    corrupt an operator's curated data.
    """
    name = (str(raw) if raw is not None else "entities").strip().lower() or "entities"
    path = anon.DICTIONARIES.get(name)
    if path is None:
        raise ValueError(f"unknown dictionary '{name}' — expected one of {', '.join(anon.DICTIONARIES)}")
    return name, path

MAX_BODY_BYTES = int(os.environ.get("ANON_MAX_UPLOAD_BYTES") or 160 * 1024 * 1024)
# 160 MB is a default, not a law of nature: the operator can raise it (ANON_MAX_UPLOAD_BYTES) when a
# document legitimately is that big, knowing what it costs — the conversion is bounded in TIME by
# CONVERT_TIMEOUT_SECONDS and in output by CONVERT_MAX_BYTES, and the redacted Markdown a Pi session
# can read is capped at 12 MB by the guard. A cap the operator cannot move is a cap they will work
# around by turning the tool off.
TOKEN_HEADER = "X-Anon-Token"
# The converter runs as a child process, so both bounds are enforced from the parent: the output
# is read in blocks and the child is killed the moment the cap is passed.
# Overridable so the bound can be exercised in tests, and lowered by an operator who wants a
# tighter ceiling than memory alone would enforce.
CONVERT_MAX_BYTES = int(os.environ.get("ANON_CONVERT_MAX_BYTES") or 160 * 1024 * 1024)
CONVERT_TIMEOUT_SECONDS = int(os.environ.get("ANON_CONVERT_TIMEOUT") or 300)
STDERR_KEEP_BYTES = 8192
DEFAULT_RATE_LIMIT = 120  # requests per minute on /api/*, per process; 0 disables the bucket
MAP_LIST_LIMIT = 100  # `/api/maps` is capped; the COUNT is not (see `_list_maps`)
# The tool ships its own converter; the Pi `docs` skill is used only as a fallback when the
# tool's own convert.py is missing (older installs).
def _default_converter() -> Path:
    own = Path(__file__).resolve().parent.parent / "convert.py"
    return own if own.is_file() else Path.home() / ".pi" / "agent" / "skills" / "docs" / "docs.py"


CONVERTER = Path(os.environ.get("ANON_CONVERTER") or _default_converter())
STATE = {"token": None, "nonce": None, "port": 1407, "jobs": 0, "limiter": None}
LOCK = threading.Lock()
# The placeholder tag is allocated against the tags on disk, so the scan and the map write must not
# interleave with another request: this is a THREADED server, and two requests would otherwise read
# the same set and draw the same tag — the collision the allocation exists to prevent.
TAG_LOCK = threading.Lock()


class RateLimiter:
    """Token bucket for the local API.

    The UI is loopback-only and unauthenticated, which is acceptable *because* nothing else can
    reach it — but a runaway script (or a tab stuck in a loop) can still pin one worker thread
    per request until the process is unusable. The bucket is deliberately generous: it exists to
    stop a runaway, not to throttle an operator clicking through the tabs.
    """

    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        self.rate = max(0.0, float(per_minute)) / 60.0
        self.burst = float(burst if burst is not None else max(3, per_minute // 4))
        self.tokens = self.burst
        self.stamp = time.monotonic()
        self.lock = threading.Lock()

    def retry_after(self) -> int:
        """Seconds until one token is available again — what `Retry-After` must advertise."""
        if self.rate <= 0:
            return 0
        return max(1, int(1.0 / self.rate + 0.5))

    def allow(self) -> bool:
        if self.rate <= 0:
            return True
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now
            if self.tokens < 1.0:
                return False
            self.tokens -= 1.0
            return True


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def _resolve(catalogs: str | None, patterns: str | None):
    """Turn UI selections into (entities, families) using the engine's own resolvers."""
    request = argparse.Namespace(entities=None, catalogs=catalogs or None, patterns=patterns or None)
    return anon.resolve_entities(request), anon.resolve_families(request)


# The local-model seam: OFF unless the operator configures it. There is deliberately NO default
# endpoint — a default pointing at a server that is not running is an error that looks like a
# configuration. `suggest.py` refuses anything that is not loopback at construction time, so the
# address cannot leave this machine even if a remote URL is passed to the flag.
SUGGEST_BACKEND: suggest_engine.Backend | None = None


def build_suggest_backend(args: argparse.Namespace) -> suggest_engine.Backend | None:
    """The backend from the flags, or None when the seam is not configured.

    Built once at STARTUP, not per request: a bad endpoint (a remote host, a typo in the scheme)
    must fail at launch with a message, not at the first click.
    """
    if not args.suggest_url and not args.suggest_model:
        return None
    if not args.suggest_url or not args.suggest_model:
        raise ValueError("--suggest-url and --suggest-model go together")
    return suggest_engine.LoopbackBackend(
        args.suggest_url,
        args.suggest_model,
        api_key=(args.suggest_key or "").strip() or None,
        headers=suggest_engine.parse_headers(args.suggest_header),
        max_tokens=args.suggest_max_tokens,
        timeout=args.suggest_timeout,
        constrained=getattr(args, "suggest_constrained", False),
    )


def _map_path(map_id: str) -> Path:
    """Map ids come from us and are validated: no path can escape the maps directory."""
    if not map_id or not all(char.isalnum() or char in "-_" for char in map_id):
        raise ValueError("invalid map id")
    candidate = (anon.DEFAULT_MAPS / f"{map_id}.map.json").resolve()
    if candidate.parent != anon.DEFAULT_MAPS.resolve():
        raise ValueError("invalid map id")
    return candidate


def _human_bytes(size: int) -> str:
    """A cap reads better as `1 KB` than as `0 MB` when a test lowers it."""
    if size >= 1024 * 1024:
        return f"{size // 1024 // 1024} MB"
    if size >= 1024:
        return f"{size // 1024} KB"
    return f"{size} bytes"


def _convert_to_markdown(source: Path) -> str:
    """Convert with the external converter, bounding BOTH the wall time and the output size.

    The cap has to apply WHILE the converter runs: `communicate()` buffers the whole output first,
    so a deliberately hostile document (a zip bomb) inflates memory well before any size check.
    stdout is read in blocks and the child is killed the moment the cap is passed; stderr is
    drained by a thread, because a full stderr pipe blocks the child and would look like a
    timeout.
    """
    if not CONVERTER.is_file():
        raise RuntimeError(
            "no document converter configured (set ANON_CONVERTER to a docs.py-compatible script)"
        )
    process = subprocess.Popen(
        [sys.executable, str(CONVERTER), str(source)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # Its own session: the converter spawns the engine as a GRANDCHILD that inherits stdout,
        # and killing only the direct child would leave the reader blocked on a pipe the
        # grandchild still holds. The whole group is killed instead, and the child is always
        # reaped (a killed process nobody waits for stays a zombie on a long-running server).
        start_new_session=True,
    )

    def terminate() -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
    stderr_chunks: list[bytes] = []

    def drain_stderr() -> None:
        kept = 0
        while True:
            try:
                chunk = process.stderr.read(4096)
            except (OSError, ValueError):  # closed under us
                return
            if not chunk:
                return
            if kept < STDERR_KEEP_BYTES:
                stderr_chunks.append(chunk)
                kept += len(chunk)

    reader = threading.Thread(target=drain_stderr, daemon=True)
    reader.start()
    timer = threading.Timer(CONVERT_TIMEOUT_SECONDS, terminate)
    timer.start()
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > CONVERT_MAX_BYTES:
                terminate()
                raise RuntimeError(
                    f"conversion produced more than {_human_bytes(CONVERT_MAX_BYTES)}"
                )
            chunks.append(chunk)
        process.wait()
    finally:
        timer.cancel()
        reader.join(timeout=2)
        for pipe in (process.stdout, process.stderr):
            try:
                pipe.close()
            except OSError:
                pass
    if process.returncode != 0:
        if process.returncode < 0:
            raise RuntimeError("conversion timed out")
        detail = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
        raise RuntimeError((detail or "conversion failed")[:400])
    return b"".join(chunks).decode("utf-8", "replace")


def _save_map(tag: str, source: str, output: str, counts: dict, entries: dict) -> str:
    """Write one map and return its id. The payload shape lives here, once, for both artifacts."""
    map_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"
    payload = {
        "tag": tag,
        "version": anon.VERSION,
        "schema": anon.SCHEMA,
        "id": map_id,
        "source": source,
        "output": output,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "counts": counts,
        "entries": entries,
    }
    anon._write_private(anon.DEFAULT_MAPS / f"{map_id}.map.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return map_id


def _anonymize_text(text: str, catalogs, patterns, save_map: bool) -> dict:
    entities, families = _resolve(catalogs, patterns)
    with TAG_LOCK:
        tag = anon.allocate_tag([anon.DEFAULT_MAPS])
        redacted, entries, counts = anon.anonymize(text, entities, families=families, tag=tag)
        map_id = None
        if save_map and entries:
            map_id = _save_map(tag, "(web UI)", "(web UI)", counts, entries)
    return {
        "redacted": redacted,
        "tag": tag,
        "counts": counts,
        "entries": [{"placeholder": key, "type": value["type"]} for key, value in entries.items()],
        "map_id": map_id,
        "rules_applied": anon.entity_count(entities),
    }


DOWNLOAD_TTL_SECONDS = 3600


def _downloads_dir() -> Path:
    """Where a redacted document waits to be fetched, inside the private store (0600).

    It holds a REDACTED document, so a leftover is not a leak; it is still pruned, because a
    download directory that only grows is a disk leak.
    """
    directory = anon.DEFAULT_MAPS.parent / "downloads"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _publish_download(redacted: Path, name: str) -> str:
    """Copy the redacted document where the client can fetch it, and return its id."""
    token = os.urandom(8).hex()
    target = _downloads_dir() / token
    target.mkdir(mode=0o700, exist_ok=True)
    published = target / Path(name).name
    shutil.copyfile(redacted, published)
    os.chmod(published, 0o600)
    now = time.time()
    for stale in _downloads_dir().glob("*"):
        try:
            if now - stale.stat().st_mtime <= DOWNLOAD_TTL_SECONDS:
                continue
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
            else:
                # rmtree on a file raises NotADirectoryError, which the except below would swallow:
                # the entry would stay for ever while the code looked like it pruned.
                stale.unlink(missing_ok=True)
        except OSError:
            continue
    return f"{token}/{published.name}"


def _anonymize_document_file(source: Path, filename: str, catalogs, patterns) -> dict:
    """Redact a DOCUMENT: one redaction, two artifacts.

    The container is redacted first — one allocation, one map, one tag — and the Markdown the model
    reads is then derived from the ALREADY redacted file. Redacting the Markdown and the container
    as two independent passes would produce two maps sharing a tag, i.e. `[EMAIL-1-<tag>]` meaning a
    different value in each artifact, and a restore holding the wrong map would resolve in silence.

    A PDF, or a package we cannot open, cannot be rewritten in place: there the Markdown conversion
    is the only path, and the response says so instead of pretending.

    TAG_LOCK is a plain Lock, NOT reentrant: the fallback calls `_anonymize_text`, which takes it
    again. It must therefore run OUTSIDE the block below — taking it twice in one thread deadlocks
    that request and then every later one, and a PDF is enough to trigger it.
    """
    entities, families = _resolve(catalogs, patterns)
    redacted_path = source.with_name(f"redacted-{filename}")
    with TAG_LOCK:
        tag = anon.allocate_tag([anon.DEFAULT_MAPS])
        try:
            entries, counts, parts = anon.anonymize_container(
                source, redacted_path, entities, families=families, tag=tag
            )
        except (anon.UnreadableContainer, OSError, zipfile.BadZipFile) as exc:
            unreadable: Exception | None = exc
        else:
            unreadable = None
            map_id = _save_map(tag, filename, f"redacted-{filename}", counts, entries) if entries else None

    if unreadable is not None:
        result = _anonymize_text(_convert_to_markdown(source), catalogs, patterns, True)
        result["origin"] = "converted"
        result["container_error"] = str(unreadable)
        return result

    try:
        markdown = _convert_to_markdown(redacted_path)
    except Exception:
        # The map holds the REAL values: if there is no artifact to go with it, it must not stay.
        if map_id:
            (anon.DEFAULT_MAPS / f"{map_id}.map.json").unlink(missing_ok=True)
        raise

    result = {
        "redacted": markdown,
        "tag": tag,
        "counts": counts,
        "entries": [{"placeholder": key, "type": value["type"]} for key, value in entries.items()],
        "map_id": map_id,
        "rules_applied": anon.entity_count(entities),
        "origin": "container",
        "parts": parts,
    }
    if entries:
        # Not base64 inside the JSON: that builds ~1.33x the file in one string, on top of the file,
        # the Markdown and the decoded copies — a 160 MB upload becomes several hundred MB of
        # strings in a single response. The document is published under the private store and
        # STREAMED from there, so this process holds one block at a time.
        result["container_name"] = f"{Path(filename).stem}.redacted{Path(filename).suffix}"
        try:
            published = _publish_download(redacted_path, result["container_name"])
        except OSError:
            # Same rule as a failed conversion: no artifact to fetch means the map must not stay.
            if map_id:
                (anon.DEFAULT_MAPS / f"{map_id}.map.json").unlink(missing_ok=True)
            raise
        # Quoted here, unquoted in `_download`: a browser percent-encodes the path, and
        # `BaseHTTPRequestHandler` hands the raw (still encoded) path over. Without this pair a
        # filename with a space, an accent, a `%` or a `#` could not be downloaded at all.
        result["container_url"] = f"/api/download/{quote(published, safe='/')}"
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "anon-tool"
    protocol_version = "HTTP/1.1"
    # A client that announces a Content-Length and then stalls would otherwise pin a worker
    # thread forever; this bounds every socket read.
    timeout = 30

    # --- plumbing ---------------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - silence the default logger
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, retry_after: int | None = None) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str, content_type: str) -> None:
        path = WEB_DIR / name
        if not path.is_file():
            self._error(404, "not found")
            return
        body = path.read_bytes()
        policy = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
        if name == "index.html":
            # The script that carries the token is inline, so it needs a nonce: `script-src 'self'`
            # alone would BLOCK it and leave `window.ANON_TOKEN` undefined — every API call would
            # then fail with a 403 in a real browser.
            body = body.replace(b"__ANON_TOKEN__", STATE["token"].encode())
            body = body.replace(b"__ANON_NONCE__", STATE["nonce"].encode())
            policy = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                f"script-src 'self' 'nonce-{STATE['nonce']}'"
            )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if length > MAX_BODY_BYTES:
            raise ValueError(f"body too large (limit {MAX_BODY_BYTES // 1024 // 1024} MB)")
        return self.rfile.read(length) if length else b""

    def _query(self) -> dict:
        return {key: values[0] for key, values in parse_qs(urlparse(self.path).query).items()}

    def _read_json(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        # A body is client input, and it is parsed by the SAME helper the map reader uses: a JSON
        # array, a scalar, a deeply nested payload and invalid UTF-8 are all a clean 400.
        return anon.read_json_object(raw.decode("utf-8", "replace"), "request body")

    def _guard(self, api: bool) -> bool:
        """Host check on everything, token + Origin on the API. False = already answered."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            self._error(403, "host not allowed")
            return False
        if not api:
            return True
        if self.headers.get(TOKEN_HEADER) != STATE["token"]:
            self._error(403, "missing or invalid token (reload the page)")
            return False
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") not in (
            f"http://127.0.0.1:{STATE['port']}",
            f"http://localhost:{STATE['port']}",
        ):
            self._error(403, "origin not allowed")
            return False
        # Rate limit AFTER authentication: an unauthenticated flood is refused by the token check
        # above (cheap), while a runaway of our own page is what can actually do work.
        limiter = STATE.get("limiter")
        if limiter is not None and not limiter.allow():
            self._error(
                429,
                "too many requests — slow down and try again",
                retry_after=limiter.retry_after(),
            )
            return False
        return True

    # --- routing ----------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if not self._guard(api=path.startswith("/api/")):
            return
        try:
            if path in ("/", "/index.html"):
                self._static("index.html", "text/html; charset=utf-8")
            elif path == "/app.css":
                self._static("app.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._static("app.js", "text/javascript; charset=utf-8")
            elif path == "/i18n.js":
                self._static("i18n.js", "text/javascript; charset=utf-8")
            elif path == "/favicon.svg":
                self._static("favicon.svg", "image/svg+xml")
            elif path == "/favicon.ico":
                # Answered explicitly: a 404 here shows up as a console error on every load, and
                # "no console errors" is part of the definition of a working UI. The real icon is
                # the SVG declared in <link rel="icon">.
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path.startswith("/api/download/"):
                self._download(path[len("/api/download/"):])
            elif path == "/api/state":
                self._json(self._state())
            elif path == "/api/maps":
                paths = self._map_files()  # ONE listing: `total` and the page describe the same set
                self._json(
                    {
                        "maps": self._list_maps(paths),
                        "total": len(paths),
                        "truncated": len(paths) > MAP_LIST_LIMIT,
                    }
                )
            elif path == "/api/entities":
                name, target = _dictionary_path(self._query().get("file"))
                text = target.read_text(encoding="utf-8") if target.is_file() else ""
                self._json(
                    {"text": text, "path": str(target), "name": name, "files": list(anon.DICTIONARIES)}
                )
            else:
                self._error(404, "not found")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - a 500 must never leak a traceback body
            self._error(500, f"{type(exc).__name__}: {exc}")

    def do_PUT(self) -> None:  # noqa: N802
        self._write()

    def do_POST(self) -> None:  # noqa: N802
        self._write()

    def _write(self) -> None:
        path = self.path.split("?")[0]
        if not self._guard(api=True):
            return
        try:
            if path == "/api/anonymize":
                self._anonymize()
            elif path == "/api/anonymize-document":
                self._anonymize_document()
            elif path == "/api/deanonymize":
                self._deanonymize()
            elif path == "/api/audit":
                self._audit()
            elif path == "/api/entities":
                self._save_entities()
            elif path == "/api/maps/reveal":
                self._reveal_map()
            elif path == "/api/suggest":
                self._suggest()
            else:
                self._error(404, "not found")
        except suggest_engine.BackendError as exc:
            # A local model that did not answer is an UPSTREAM failure, never an empty list: an
            # empty list would read as "nothing sensitive found".
            self._error(502, f"the local model did not answer: {exc}")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback body
            self._error(500, f"{type(exc).__name__}: {str(exc)[:200]}")

    # --- endpoints --------------------------------------------------------------------
    def _state(self) -> dict:
        return {
            "schema": anon.SCHEMA,
            "version": anon.VERSION,
            # The fingerprint of the code this build serves: the page shows it, and
            # `scripts/check-container-fresh.py` compares it with the repository's to catch a
            # container that was never rebuilt after a fix (the version alone never moves between
            # commits, so it cannot tell a stale container from a current one).
            "build": anon.code_fingerprint(),
            "catalogs": anon.list_catalogs(),
            "patterns": list(anon.PATTERN_FAMILIES),
            "maps_count": len(self._map_files()),
            "converter": CONVERTER.is_file(),
            "suggest": SUGGEST_BACKEND is not None,
            "suggest_backend": SUGGEST_BACKEND.name if SUGGEST_BACKEND else None,
            # The model's own bounds, stated by the server rather than copied into the page: the
            # window is the module constant, the timeout belongs to the configured backend.
            "suggest_max_chars": suggest_engine.DEFAULT_MAX_CHARS,
            "suggest_timeout": getattr(SUGGEST_BACKEND, "timeout", suggest_engine.DEFAULT_TIMEOUT),
            "suggest_constrained": bool(getattr(SUGGEST_BACKEND, "constrained", False)),
            "maps_dir": str(anon.DEFAULT_MAPS),
            "entities_path": str(anon.DEFAULT_ENTITIES),
            "entities_paths": {name: str(path) for name, path in anon.DICTIONARIES.items()},
            # The upload cap belongs to the server and the UI states it (it refuses a bigger file
            # before spending the transfer): a hard-coded copy on the page would drift from this.
            "max_upload_bytes": MAX_BODY_BYTES,
        }

    def _suggest(self) -> None:
        """Proposals from the local model. Writes NOTHING: the operator approves them one by one."""
        if SUGGEST_BACKEND is None:
            raise ValueError(
                "the local model is not configured: start the server with --suggest-url and "
                "--suggest-model (a loopback address only)"
            )
        payload = self._read_json()
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError('"text" is required')
        entities, families = _resolve(payload.get("catalogs"), payload.get("patterns"))
        report = suggest_engine.suggest(text, SUGGEST_BACKEND, entities=entities, families=families)
        self._json({"schema": anon.SCHEMA, "mode": "suggest", **report})

    def _map_files(self) -> list[Path]:
        if not anon.DEFAULT_MAPS.is_dir():
            return []
        return sorted(anon.DEFAULT_MAPS.glob("*.map.json"), reverse=True)

    def _list_maps(self, paths: list[Path], limit: int | None = MAP_LIST_LIMIT) -> list[dict]:
        """Metadata only: the map holds the REAL values and must not travel on a list call.

        The caller passes the file list it counted, so `total` and the listing always describe the
        same set (two independent globs could disagree — the server is threaded and the CLI writes
        maps concurrently). The listing is capped (`MAP_LIST_LIMIT`); a truncated list is declared,
        and an unreadable map is REPORTED rather than skipped, for the same reason.
        """
        out = []
        for path in paths[:limit] if limit is not None else paths:
            try:
                # The metadata reader VALIDATES the map with the same rules the restore path uses
                # (one boundary, see deanon.load_map_metadata). A LISTING must still never fail:
                # one unreadable file is reported as unreadable and the rest is served.
                out.append(deanon_engine.load_map_metadata(path))
            except Exception:  # noqa: BLE001 - the point: no file can turn a listing into a 500
                out.append({"id": path.stem.replace(".map", ""), "unreadable": True, "entries": 0})
        return out

    def _anonymize(self) -> None:
        payload = self._read_json()
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("no text")
        result = _anonymize_text(
            text, payload.get("catalogs"), payload.get("patterns"), payload.get("save_map", True)
        )
        with LOCK:
            STATE["jobs"] += 1
        self._json(result)

    def _anonymize_document(self) -> None:
        raw = self._read_body()
        if not raw:
            raise ValueError("empty upload")
        filename = Path(self.headers.get("X-Filename") or "upload.bin").name
        catalogs = self.headers.get("X-Catalogs")
        patterns = self.headers.get("X-Patterns")
        work = Path(tempfile.mkdtemp(prefix="anon-web-"))
        try:
            source = work / filename
            source.write_bytes(raw)
            kind = anon.sniff(source)
            if kind is None:
                result = _anonymize_text(source.read_text(encoding="utf-8", errors="replace"), catalogs, patterns, True)
                result["origin"] = "text"
            elif kind == "image":
                raise ValueError("an image cannot be anonymized: its pixels are not scannable")
            else:
                result = _anonymize_document_file(source, filename, catalogs, patterns)
            with LOCK:
                STATE["jobs"] += 1
            self._json(result)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _download(self, name: str) -> None:
        """Stream a published document, in blocks, with its length declared up front."""
        parts = unquote(name).split("/")
        directory = (anon.DEFAULT_MAPS.parent / "downloads").resolve()
        if len(parts) != 2 or not all(char in "0123456789abcdef" for char in parts[0]):
            raise ValueError("invalid download id")
        candidate = (directory / parts[0] / Path(parts[1]).name)
        resolved = candidate.resolve()
        if directory not in resolved.parents or not resolved.is_file():
            raise ValueError("unknown download")
        size = resolved.stat().st_size
        # `send_header` does NOT validate its value: a newline in a filename would split the
        # response. Today the name comes from an upload header, which cannot carry one — but that
        # is an assumption about a different component, and this is one line.
        served = re.sub(r"[^A-Za-z0-9._-]", "_", Path(parts[1]).name)[:80] or "documento.bin"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f'attachment; filename="{served}"')
        self.end_headers()
        with resolved.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, 65536)

    def _deanonymize(self) -> None:
        raw = self._read_body()
        if not raw:
            raise ValueError("empty upload")
        filename = Path(self.headers.get("X-Filename") or "document.bin").name
        map_id = self.headers.get("X-Map-Id") or ""
        map_path = _map_path(map_id)
        if not map_path.is_file():
            raise ValueError("unknown map id")
        entries = deanon_engine.load_map(map_path)
        work = Path(tempfile.mkdtemp(prefix="anon-web-"))
        try:
            source = work / filename
            source.write_bytes(raw)
            output = work / f"restored-{filename}"
            if anon.sniff(source) == "container":
                report = deanon_engine.deanon_container(source, output, entries)
            else:
                report = deanon_engine.deanon_text(source, output, entries)
            report["schema"] = anon.SCHEMA
            self._json(
                {
                    "report": report,
                    "filename": filename.replace(".deanon", ""),
                    "content_b64": base64.b64encode(output.read_bytes()).decode("ascii"),
                }
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _audit(self) -> None:
        payload = self._read_json()
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("no text")
        entities, families = _resolve(payload.get("catalogs"), payload.get("patterns"))
        found = anon.detect(text, entities, families=families)
        candidates, capped = anon.near_misses(text, entities, found)
        reveal = bool(payload.get("reveal"))
        by_type: dict[str, int] = {}
        findings = []
        for start, _end, ptype in found:
            by_type[ptype] = by_type.get(ptype, 0) + 1
            if len(findings) < 50:
                findings.append({"type": ptype, "line": text.count("\n", 0, start) + 1})
        candidates = candidates if reveal else [
            {k: v for k, v in item.items() if k not in ("token", "entity")} for item in candidates
        ]
        verdict = "sensitive" if found else ("suspicious" if candidates else "clean")
        self._json(
            {
                "schema": anon.SCHEMA,
                "verdict": verdict,
                "total": len(found),
                "types": by_type,
                "findings": findings,
                "near_miss": candidates,
                "revealed": reveal,
                "candidates_capped": capped,
                "placeholders_present": sum(1 for _ in anon.PLACEHOLDER_RE.finditer(text)),
                "findings_truncated": len(found) > len(findings),
            }
        )

    def _save_entities(self) -> None:
        name, target = _dictionary_path(self._query().get("file"))
        payload = self._read_json()
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("no text")
        work = Path(tempfile.mkdtemp(prefix="anon-web-"))
        try:
            probe = work / f"{name}.txt"
            probe.write_text(text, encoding="utf-8")
            try:
                entries = anon.load_entities(probe)  # validates BEFORE touching the real file
            except ValueError as exc:
                raise ValueError(f"dictionary rejected: {exc}") from exc
            anon._write_private(target, text if text.endswith("\n") else text + "\n")
            self._json(
                {"saved": True, "entries": anon.entity_count(entries), "path": str(target), "name": name}
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _reveal_map(self) -> None:
        payload = self._read_json()
        if payload.get("confirm") is not True:
            raise ValueError("revealing the mapping exposes the REAL values: pass confirm=true")
        map_path = _map_path(str(payload.get("id") or ""))
        if not map_path.is_file():
            raise ValueError("unknown map id")
        entries = deanon_engine.load_map(map_path)
        self._json({"id": map_path.stem.replace(".map", ""), "entries": entries})


class LocalServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the FQDN lookup at bind time.

    `HTTPServer.server_bind()` calls `socket.getfqdn()`, a reverse-DNS lookup that can stall for
    seconds (or until the resolver times out) on a machine whose DNS is slow — the server would
    appear to hang at startup, with no output and nothing listening. We serve exactly one local
    host, so the name is not worth a network round trip.
    """

    daemon_threads = True
    # `socketserver.TCPServer.request_queue_size` is 5, the listen backlog the kernel applies to
    # connections nobody has accepted yet. The accept loop spawns a thread per connection, so a
    # burst is refused at the kernel before anything can answer it. Measured on this server: with
    # the default, 48 simultaneous connections leave about half of them (19-28 of 48, over four
    # measurement sessions) reset in EVERY run — a client-side socket error, most often
    # `URLError: [Errno 54] Connection reset by peer`, with no status code and no line in the log.
    # With 128, no run lost one. The UI makes a handful of requests at a time, so it never bit an
    # operator; the burst is still an ordinary shape, and the fix is the number the kernel is told
    # to hold.
    request_queue_size = 128

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server.py", description="local web UI for anon-tool")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1407)
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=DEFAULT_RATE_LIMIT,
        metavar="PER_MINUTE",
        help=f"requests per minute allowed on /api/* (default: {DEFAULT_RATE_LIMIT}; 0 disables the limit)",
    )
    parser.add_argument(
        "--suggest-url",
        help="loopback endpoint of a local model, OpenAI-compatible (e.g. "
        "http://127.0.0.1:8000/v1/chat/completions); without it the suggestion panel stays off",
    )
    parser.add_argument("--suggest-model", help="model name, as the local server knows it")
    parser.add_argument("--suggest-key", help="a bearer key for the local model, when it wants one")
    parser.add_argument(
        "--suggest-header",
        action="append",
        metavar="'Nome: valore'",
        help="extra header for the local model (repeatable); a reasoning model may need none of this, "
        "but some servers ask for a client tag",
    )
    parser.add_argument(
        "--suggest-max-tokens",
        type=int,
        default=suggest_engine.DEFAULT_MAX_TOKENS,
        help=f"generation budget for the local model (default {suggest_engine.DEFAULT_MAX_TOKENS}); a "
        "reasoning model spends it thinking and may need far more",
    )
    parser.add_argument(
        "--suggest-timeout",
        type=float,
        default=suggest_engine.DEFAULT_TIMEOUT,
        help=f"seconds to wait for the local model (default {suggest_engine.DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--suggest-constrained",
        action="store_true",
        help="pin the model's answer to the JSON schema (llama.cpp/vLLM); off by default so an "
        "endpoint that rejects the field is not turned into a 400 on every call",
    )
    parser.add_argument(
        "--allow-lan",
        action="store_true",
        help="permit a non-loopback bind — the UI has NO authentication, so this exposes your "
        "documents and the entity dictionary to the network",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    global SUGGEST_BACKEND
    args = build_parser().parse_args(argv)
    try:
        SUGGEST_BACKEND = build_suggest_backend(args)
    except ValueError as exc:
        print(f"server: {exc}", file=sys.stderr)
        return 2
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not args.allow_lan:
        print(
            f"server: refusing to bind {args.host}: the UI has no authentication and handles "
            "documents with real client data. Use --allow-lan only on a trusted network.",
            file=sys.stderr,
        )
        return 2
    if not loopback:
        print(f"server: WARNING binding {args.host} — the UI is reachable from the network", file=sys.stderr)
    import secrets

    STATE["token"] = secrets.token_urlsafe(24)
    STATE["nonce"] = secrets.token_urlsafe(16)
    STATE["port"] = args.port
    STATE["limiter"] = RateLimiter(args.rate_limit)
    server = LocalServer((args.host, args.port), Handler)
    host_display = "127.0.0.1" if loopback else args.host
    print(f"anon-tool UI on http://{host_display}:{args.port}  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
