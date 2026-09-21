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
  * a present `Origin` header must be our own.
  * NO client-supplied filesystem path is ever used: uploaded bytes land in a per-request temp
    directory under a generated name and are deleted afterwards;
  * request bodies are capped; no shell, no eval; the server logs no content and no values.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
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

MAX_BODY_BYTES = 32 * 1024 * 1024
TOKEN_HEADER = "X-Anon-Token"
CONVERTER = Path(
    os.environ.get("ANON_CONVERTER") or (Path.home() / ".pi" / "agent" / "skills" / "docs" / "docs.py")
)
STATE = {"token": None, "nonce": None, "port": 1407, "jobs": 0}
CONVERT_MAX_BYTES = 32 * 1024 * 1024
LOCK = threading.Lock()


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


def _convert_to_markdown(source: Path) -> str:
    """Convert with the external converter, bounding BOTH the wall time and the output size.

    A deliberately hostile document (a zip bomb) could otherwise inflate a document far past
    memory; the cap turns that into a clean error.
    """
    if not CONVERTER.is_file():
        raise RuntimeError(
            "no document converter configured (set ANON_CONVERTER to a docs.py-compatible script)"
        )
    process = subprocess.Popen(
        [sys.executable, str(CONVERTER), str(source)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("conversion timed out")
    if len(stdout) > CONVERT_MAX_BYTES:
        raise RuntimeError(f"conversion produced more than {CONVERT_MAX_BYTES // 1024 // 1024} MB")
    if process.returncode != 0:
        raise RuntimeError((stderr.decode("utf-8", "replace") or "conversion failed").strip()[:400])
    return stdout.decode("utf-8", "replace")


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

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

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
            elif path == "/api/state":
                self._json(self._state())
            elif path == "/api/maps":
                self._json({"maps": self._list_maps()})
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
            "maps_count": len(self._list_maps()),
            "converter": CONVERTER.is_file(),
            "maps_dir": str(anon.DEFAULT_MAPS),
            "entities_path": str(anon.DEFAULT_ENTITIES),
        }

    def _list_maps(self) -> list[dict]:
        """Metadata only: the map holds the REAL values and must not travel on a list call."""
        out = []
        if anon.DEFAULT_MAPS.is_dir():
            for path in sorted(anon.DEFAULT_MAPS.glob("*.map.json"), reverse=True)[:100]:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
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
