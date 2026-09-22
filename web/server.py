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
import json
import os
import signal
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WEB_DIR.parent))
import anon  # noqa: E402
import deanon as deanon_engine  # noqa: E402

MAX_BODY_BYTES = 160 * 1024 * 1024
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


def _anonymize_text(text: str, catalogs, patterns, save_map: bool) -> dict:
    entities, families = _resolve(catalogs, patterns)
    tag = anon.new_tag()
    redacted, entries, counts = anon.anonymize(text, entities, families=families, tag=tag)
    map_id = None
    if save_map and entries:
        map_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"
        payload = {
            "tag": tag,
            "version": anon.VERSION,
            "schema": anon.SCHEMA,
            "id": map_id,
            "source": "(web UI)",
            "output": "(web UI)",
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "counts": counts,
            "entries": entries,
        }
        anon._write_private(anon.DEFAULT_MAPS / f"{map_id}.map.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {
        "redacted": redacted,
        "tag": tag,
        "counts": counts,
        "entries": [{"placeholder": key, "type": value["type"]} for key, value in entries.items()],
        "map_id": map_id,
        "rules_applied": anon.entity_count(entities),
    }


# --------------------------------------------------------------------------------------
# request handler
# --------------------------------------------------------------------------------------
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

    def _read_json(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

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
            elif path == "/favicon.svg":
                self._static("favicon.svg", "image/svg+xml")
            elif path == "/favicon.ico":
                # Answered explicitly: a 404 here shows up as a console error on every load, and
                # "no console errors" is part of the definition of a working UI. The real icon is
                # the SVG declared in <link rel="icon">.
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
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
                text = anon.DEFAULT_ENTITIES.read_text(encoding="utf-8") if anon.DEFAULT_ENTITIES.is_file() else ""
                self._json({"text": text, "path": str(anon.DEFAULT_ENTITIES)})
            else:
                self._error(404, "not found")
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
            else:
                self._error(404, "not found")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback body
            self._error(500, f"{type(exc).__name__}: {str(exc)[:200]}")

    # --- endpoints --------------------------------------------------------------------
    def _state(self) -> dict:
        return {
            "schema": anon.SCHEMA,
            "version": anon.VERSION,
            "catalogs": anon.list_catalogs(),
            "patterns": list(anon.PATTERN_FAMILIES),
            "maps_count": len(self._map_files()),
            "converter": CONVERTER.is_file(),
            "maps_dir": str(anon.DEFAULT_MAPS),
            "entities_path": str(anon.DEFAULT_ENTITIES),
        }

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
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                out.append({"id": path.stem.replace(".map", ""), "unreadable": True, "entries": 0})
                continue
            if not isinstance(data, dict):
                # Valid JSON, wrong shape (`[1,2,3]`): treated like any other unreadable map, so a
                # hand-edited file cannot turn a read-only listing into a 500.
                out.append({"id": path.stem.replace(".map", ""), "unreadable": True, "entries": 0})
                continue
            out.append(
                {
                    "id": data.get("id", path.stem.replace(".map", "")),
                    "created": data.get("created"),
                    "source": Path(str(data.get("source", ""))).name,
                    "counts": data.get("counts"),
                    "entries": len(data.get("entries") or {}),
                }
            )
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
                text = source.read_text(encoding="utf-8", errors="replace")
                origin = "text"
            else:
                if kind == "image":
                    raise ValueError("an image cannot be anonymized: its pixels are not scannable")
                text = _convert_to_markdown(source)
                origin = "converted"
            result = _anonymize_text(text, catalogs, patterns, True)
            result["origin"] = origin
            with LOCK:
                STATE["jobs"] += 1
            self._json(result)
        finally:
            shutil.rmtree(work, ignore_errors=True)

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
        payload = self._read_json()
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("no text")
        work = Path(tempfile.mkdtemp(prefix="anon-web-"))
        try:
            probe = work / "entities.txt"
            probe.write_text(text, encoding="utf-8")
            try:
                entries = anon.load_entities(probe)  # validates BEFORE touching the real file
            except ValueError as exc:
                raise ValueError(f"dictionary rejected: {exc}") from exc
            anon._write_private(anon.DEFAULT_ENTITIES, text if text.endswith("\n") else text + "\n")
            self._json({"saved": True, "entries": anon.entity_count(entries), "path": str(anon.DEFAULT_ENTITIES)})
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
        "--allow-lan",
        action="store_true",
        help="permit a non-loopback bind — the UI has NO authentication, so this exposes your "
        "documents and the entity dictionary to the network",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
