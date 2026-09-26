#!/usr/bin/env python3
"""mcp_anon.py — the engine behind a stdio MCP server: two tools, and nothing else.

A model or a harness can ask `check` ("is this sensitive?") and `anonymize` ("redact it"), on this
machine, with no network and no real value in the answer:

  * `check` answers with the verdict, the per-type counts and the LINE NUMBERS — never a value;
  * `anonymize` returns the redacted text, the counts and the map id, and writes the reversible map
    to the private store, exactly like the CLI. The real values stay in `~/.anon/maps/`; the caller
    gets what to deliver, not what to protect.

The transport is newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio). stdout carries protocol
frames only; diagnostics go to stderr. It imports the engine by name and reaches no network stack —
`OfflineContractTest` checks that, so a drive-by import cannot make "local" false.

    python3 mcp_anon.py         # speaks MCP on stdin/stdout
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import anon

SERVER_NAME = "anon-tool"
# Echoed back from the client's `initialize` when it sends one; this is the fallback.
PROTOCOL_VERSION = "2024-11-05"
# The transport reads a whole JSON line before parsing, so the bound is about what a caller may
# hand the ENGINE. A megabyte of text is already far past a page; a bigger payload is a mistake.
MAX_TEXT_CHARS = 2_000_000
# ...and this bounds MEMORY, before any tool bound is consulted: the frame is discarded, not parsed,
# when it is over the limit. Generous on purpose — the JSON overhead of the text bound stays under it.
MAX_FRAME_BYTES = 16 * 1024 * 1024

_TOOLS = (
    {
        "name": "check",
        "description": (
            "Is this text sensitive? Returns the verdict, the per-type counts and the line numbers "
            "of the findings — never the values themselves."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "the text to check"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "anonymize",
        "description": (
            "Redact the sensitive spans and write the reversible map to the private store. Returns "
            "the redacted text, the counts and the map id — never the real values."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "the text to redact"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
)


def _envelope(tool: str) -> dict[str, object]:
    return {"schema": anon.SCHEMA, "tool": f"mcp {tool}", "version": anon.VERSION}


def _engine_args() -> types.SimpleNamespace:
    """The dictionaries and catalogs the operator has, read the same way the CLI reads them.

    No flags: the MCP surface takes TEXT, and the dictionary is the operator's own store. `entities`
    and `catalogs` stay None, which is exactly "use the defaults that exist" — the same path
    `anon.py --check` takes when it is called with neither flag.
    """
    return types.SimpleNamespace(entities=None, catalogs=None, patterns=None)


def tool_check(text: str) -> dict[str, object]:
    """The verdict, the counts and the line numbers. Not one value: the caller learns THAT it is
    sensitive, not WHAT is."""
    entities = anon.resolve_entities(_engine_args())
    families = anon.resolve_families(_engine_args())
    found = anon.detect(text, entities, families=families)
    by_type: dict[str, int] = {}
    findings: list[dict[str, object]] = []
    for start, _end, ptype in found:
        by_type[ptype] = by_type.get(ptype, 0) + 1
        if len(findings) < 50:
            findings.append({"type": ptype, "line": text.count("\n", 0, start) + 1})
    result: dict[str, object] = {
        **_envelope("check"),
        "sensitive": bool(found),
        "total": len(found),
        "types": by_type,
        "findings": findings,
    }
    if len(found) > len(findings):
        result["findings_truncated"] = True
    return result


def tool_anonymize(text: str) -> dict[str, object]:
    """The redacted text and its map id. The map — the real values — goes to the private store, the
    only place that makes a redacted document reversible (`DEC-0033`)."""
    entities = anon.resolve_entities(_engine_args())
    families = anon.resolve_families(_engine_args())
    # The tag must be unique among the maps that EXIST, exactly like the CLI: a collision would let
    # a wrong map resolve these placeholders silently.
    tag = anon.allocate_tag([anon.DEFAULT_MAPS])
    redacted, entries, counts = anon.anonymize(text, entities, families=families, tag=tag)
    map_id: str | None = None
    if entries:
        map_path = anon.plan_map_path(types.SimpleNamespace(map=None))
        anon.write_map(map_path, Path("(mcp text)"), None, entries, counts)
        map_id = map_path.stem.removesuffix(".map")
    return {
        **_envelope("anonymize"),
        "map_id": map_id,
        "redacted": redacted,
        "counts": counts,
        # The placeholder list WITHOUT the originals: enough to check the delivery, not enough to
        # reconstruct it.
        "placeholders": [{"placeholder": token, "type": entry["type"]} for token, entry in entries.items()],
    }


def _send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id, result) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _content(payload: dict[str, object], *, is_error: bool = False) -> dict[str, object]:
    """A tool result: one text block, JSON-encoded. A tool failure is `isError`, not a protocol
    error — the protocol error would say the CALL was malformed, which is a different fact."""
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }


def _tools_call(request_id, params: dict[str, object]) -> None:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name not in ("check", "anonymize"):
        _result(request_id, _content({"error": f"unknown tool: {name}"}, is_error=True))
        return
    text = arguments.get("text") if isinstance(arguments, dict) else None
    if not isinstance(text, str) or not text.strip():
        _result(request_id, _content({"error": 'the "text" argument is required'}, is_error=True))
        return
    if len(text) > MAX_TEXT_CHARS:
        _result(
            request_id,
            _content(
                {"error": f"text is {len(text)} characters, over the {MAX_TEXT_CHARS} limit"},
                is_error=True,
            ),
        )
        return
    try:
        payload = tool_check(text) if name == "check" else tool_anonymize(text)
    except (ValueError, OSError) as exc:
        _result(request_id, _content({"error": str(exc)}, is_error=True))
        return
    _result(request_id, _content(payload))


def handle(request: dict[str, object]) -> None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}

    if method == "initialize":
        _result(request_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": anon.VERSION},
        })
    elif method == "notifications/initialized":
        return  # a notification: no reply, by the protocol
    elif method == "ping":
        _result(request_id, {})
    elif method == "tools/list":
        _result(request_id, {"tools": list(_TOOLS)})
    elif method == "tools/call":
        _tools_call(request_id, params)
    elif request_id is not None:
        _error(request_id, -32601, f"method not found: {method}")
    # a notification for an unknown method is ignored, as the protocol requires


def _read_frame(stream, limit: int = MAX_FRAME_BYTES) -> bytes | None:
    """One newline-terminated frame, `None` at end of input, `b""` for a frame over `limit`.

    An oversized frame is DISCARDED rather than parsed, so a single huge line cannot exhaust memory
    before any tool-level bound is consulted. `b""` cannot be a real frame (readline always returns
    at least the newline), so it is an unambiguous sentinel.
    """
    chunk = stream.readline(limit + 1)
    if chunk == b"":
        return None
    if len(chunk) > limit and not chunk.endswith(b"\n"):
        while chunk and not chunk.endswith(b"\n"):
            chunk = stream.readline(limit + 1)
        return b""
    return chunk


def main() -> int:
    stream = sys.stdin.buffer
    while True:
        frame = _read_frame(stream)
        if frame is None:
            return 0
        if frame == b"":
            _error(None, -32600, f"frame over the {MAX_FRAME_BYTES}-byte transport limit")
            continue
        try:
            line = frame.decode("utf-8")
        except UnicodeDecodeError:
            _error(None, -32700, "unparseable frame: not UTF-8")
            continue
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, RecursionError, MemoryError) as exc:
            # RecursionError (deeply nested JSON) and MemoryError are NOT JSONDecodeError: without
            # them, one malformed frame would terminate the transport instead of being reported.
            _error(None, -32700, f"unparseable frame: {type(exc).__name__}")
            continue
        if not isinstance(request, dict):
            _error(None, -32600, "invalid request: not a JSON object")
            continue
        try:
            handle(request)
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the transport
            print(f"mcp_anon: {type(exc).__name__}: {exc}", file=sys.stderr)
            if request.get("id") is not None:
                _error(request.get("id"), -32603, f"internal error: {type(exc).__name__}")


if __name__ == "__main__":
    sys.exit(main())
