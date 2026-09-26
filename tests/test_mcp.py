#!/usr/bin/env python3
"""The MCP seam: two tools over stdio, on this machine, with no real value in the answer.

The server is a real subprocess speaking newline-delimited JSON-RPC, driven the way a client drives
it, so the transport and the tools are exercised together. The invariant under test is not "it
answers" but "it answers without handing back what it protects": `check` returns a verdict and line
numbers, `anonymize` returns the redacted text and the map id, and the real values stay in the map.

  python3 tests/test_mcp.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
HOME = HERE.parent
SERVER = HOME / "mcp_anon.py"

TEXT = "Il cliente Contoso, referente mario.rossi@contoso.it, host db01.contoso.local.\n"
SECRETS = ("mario.rossi@contoso.it", "Contoso", "db01.contoso.local")


class McpServer:
    """A running `mcp_anon.py` on stdin/stdout, one JSON-RPC message per line."""

    def __init__(self, home: Path) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "ANON_HOME": str(home)},
        )

    def _write(self, message: dict) -> None:
        assert self.process.stdin
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method: str, params: dict | None = None, request_id: int = 1) -> dict:
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        assert self.process.stdout
        line = self.process.stdout.readline()
        assert line, self.process.stderr.read() if self.process.stderr else "no response"
        return json.loads(line)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def send_raw(self, line: str) -> dict:
        assert self.process.stdin
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        assert self.process.stdout
        return json.loads(self.process.stdout.readline())

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        self.process.wait(timeout=10)
        for stream in (self.process.stdout, self.process.stderr):
            if stream and not stream.closed:
                stream.close()


class McpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="anon-mcp-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        # A dictionary in the private home: the MCP tools read the operator's store the way the CLI
        # does, and without it `Contoso` is invisible (only the pattern rules would fire).
        (self.home / "entities.txt").write_text("AZIENDA|Contoso\n", encoding="utf-8")
        self.server = McpServer(self.home)
        self.addCleanup(self.server.close)
        self.server.call("initialize", {"protocolVersion": "2024-11-05"})
        self.server.notify("notifications/initialized")

    def tool(self, name: str, **arguments) -> dict:
        response = self.server.call("tools/call", {"name": name, "arguments": arguments}, request_id=7)
        result = response["result"]
        return {"isError": result.get("isError", False), "payload": json.loads(result["content"][0]["text"])}

    def test_initialize_names_the_server(self) -> None:
        server = McpServer(self.home)
        self.addCleanup(server.close)
        result = server.call("initialize", {})["result"]
        self.assertEqual(result["serverInfo"]["name"], "anon-tool")
        self.assertIn("tools", result["capabilities"])
        self.assertTrue(result["protocolVersion"])

    def test_tools_list_exposes_check_and_anonymize(self) -> None:
        tools = self.server.call("tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"check", "anonymize"})
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["required"], ["text"])

    def test_check_reports_the_verdict_without_a_value(self) -> None:
        result = self.tool("check", text=TEXT)
        self.assertFalse(result["isError"])
        payload = result["payload"]
        self.assertTrue(payload["sensitive"])
        self.assertGreaterEqual(payload["total"], 3)
        self.assertIn("EMAIL", payload["types"])
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(payload), f"the value {secret!r} came back")

    def test_check_says_clean_for_clean_text(self) -> None:
        result = self.tool("check", text="Nessun dato personale in questa riga.\n")
        self.assertFalse(result["payload"]["sensitive"])
        self.assertEqual(result["payload"]["total"], 0)

    def test_anonymize_returns_the_redacted_text_and_writes_the_map(self) -> None:
        result = self.tool("anonymize", text=TEXT)
        self.assertFalse(result["isError"])
        payload = result["payload"]
        self.assertIn("[EMAIL-", payload["redacted"])
        self.assertIn("[AZIENDA-", payload["redacted"])
        self.assertTrue(payload["map_id"])
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(payload), f"the value {secret!r} came back")
        maps = list((self.home / "maps").glob("*.map.json"))
        self.assertEqual(len(maps), 1, "exactly one map, in the private store")
        stored = json.loads(maps[0].read_text(encoding="utf-8"))
        self.assertIn("mario.rossi@contoso.it", json.dumps(stored), "the map is what makes it reversible")
        self.assertEqual(maps[0].stat().st_mode & 0o777, 0o600, "the map is private")

    def test_anonymize_with_nothing_to_redact_writes_no_map(self) -> None:
        result = self.tool("anonymize", text="Nessun dato personale qui.\n")
        payload = result["payload"]
        self.assertEqual(payload["redacted"], "Nessun dato personale qui.\n")
        self.assertIsNone(payload["map_id"])
        self.assertEqual(list((self.home / "maps").glob("*.map.json")) if (self.home / "maps").exists() else [], [])

    def test_a_missing_text_is_a_tool_error(self) -> None:
        result = self.tool("check")
        self.assertTrue(result["isError"])
        self.assertIn("text", result["payload"]["error"])

    def test_an_unknown_tool_is_a_tool_error(self) -> None:
        self.assertTrue(self.tool("no-such-tool", text="x")["isError"])

    def test_an_oversized_text_is_refused(self) -> None:
        result = self.tool("check", text="x" * 2_000_001)
        self.assertTrue(result["isError"])
        self.assertIn("limit", result["payload"]["error"])

    def test_an_unknown_method_is_a_protocol_error(self) -> None:
        response = self.server.call("no/such/method")
        self.assertEqual(response["error"]["code"], -32601)

    def test_a_parse_error_does_not_kill_the_transport(self) -> None:
        broken = self.server.send_raw("{not json")
        self.assertEqual(broken["error"]["code"], -32700)
        # the transport is still alive: a valid call right after answers normally
        self.assertFalse(self.tool("check", text="ok\n")["isError"])

    def test_a_deeply_nested_frame_does_not_kill_the_transport(self) -> None:
        """`json.loads` raises RecursionError (not JSONDecodeError) on a deeply nested frame; one
        malformed frame must be reported, not terminate the server. The nesting must be VALID: the
        C scanner rejects an unbalanced one early, before it ever recurses."""
        broken = self.server.send_raw("[" * 200_000 + "]" * 200_000)
        self.assertEqual(broken["error"]["code"], -32700)
        self.assertFalse(self.tool("check", text="ok\n")["isError"])

    def test_an_oversized_frame_is_discarded_before_parsing(self) -> None:
        """The transport bound is about MEMORY: the frame is dropped before `json.loads`, and the
        rest of the oversized line is drained so the NEXT frame still reads cleanly."""
        import io

        sys.path.insert(0, str(HOME))
        import mcp_anon

        stream = io.BytesIO(b"x" * 50 + b"\n" + b'{"jsonrpc":"2.0","id":1}\n')
        self.assertEqual(mcp_anon._read_frame(stream, limit=40), b"", "an oversized frame is dropped")
        self.assertEqual(
            mcp_anon._read_frame(stream, limit=40),
            b'{"jsonrpc":"2.0","id":1}\n',
            "the oversized line was drained",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
