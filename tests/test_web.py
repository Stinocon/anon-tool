#!/usr/bin/env python3
"""Integration tests for the local web UI (`web/server.py`).

Starts the real server in a subprocess against a hermetic `ANON_HOME` and drives it over HTTP:
the engine endpoints, plus the security guards (token, Host header, Origin) that make a
loopback-only, unauthenticated UI an acceptable trade-off.

  python3 ~/.anon/tests/test_web.py
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = HERE.parent
SERVER = HOME / "web" / "server.py"
TOKEN_HEADER = "X-Anon-Token"

sys.dont_write_bytecode = True

DOCX_AVAILABLE = (Path.home() / ".pi" / "agent" / "skills" / "docs" / "docs.py").is_file()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class StaticUiTest(unittest.TestCase):
    """Checks on the front-end that need no browser.

    A `$(missing-id)` throws at load time and silently kills every initialisation after it
    (`refresh()`, `refreshMaps()`, `loadEntities()`), which left the UI half-dead while every
    HTTP test stayed green. One real browser run is worth more than a thousand assertions, but a
    static consistency check catches this whole class for free.
    """

    JS = (HOME / "web" / "app.js").read_text(encoding="utf-8")
    HTML = (HOME / "web" / "index.html").read_text(encoding="utf-8")

    def test_every_referenced_element_id_exists(self) -> None:
        declared = set(re.findall(r'id="([^"]+)"', self.HTML))
        referenced = set(re.findall(r'\$\("([^"]+)"\)', self.JS))
        missing = sorted(referenced - declared)
        self.assertEqual(missing, [], f"app.js references ids missing from index.html: {missing}")

    def test_tab_panels_match_their_aria_controls(self) -> None:
        controls = re.findall(r'aria-controls="([^"]+)"', self.HTML)
        declared = set(re.findall(r'id="([^"]+)"', self.HTML))
        self.assertEqual(sorted(set(controls) - declared), [], "every tab must control a real panel")
        self.assertEqual(len(controls), len(set(controls)), "duplicated aria-controls")

    def test_no_inline_script_without_a_nonce(self) -> None:
        for match in re.finditer(r"<script([^>]*)>", self.HTML):
            attributes = match.group(1)
            if "src=" in attributes:
                continue
            self.assertIn("nonce=", attributes, f"inline script without a nonce: {match.group(0)}")

    def test_reveal_view_can_be_relocked(self) -> None:
        """The real values must be removable from the page without a reload, and they expire."""
        self.assertIn('id="hide-map"', self.HTML)
        self.assertIn("REVEAL_TTL_MS", self.JS)
        self.assertIn('$("hide-map").addEventListener', self.JS)

    def test_the_model_output_reaches_the_page_as_text_only(self) -> None:
        """A model answer is untrusted input: it must never be assigned as HTML.

        The DOM harness is deliberately minimal (it has no query-by-class), so this is checked at
        the source: the panel writes every model value with `textContent`, and a regression to
        `innerHTML` fails HERE. Verified by mutation — switching that one line to innerHTML leaves
        every other check green.
        """
        self.assertIn("value.textContent = proposal.value", self.JS)
        self.assertNotIn("innerHTML = proposal", self.JS)
        self.assertNotIn("innerHTML = value", self.JS)

    def test_a_value_that_cannot_be_a_dictionary_line_is_not_appended(self) -> None:
        """A dictionary line is `TIPO|valore`: a value with a pipe or a newline, written as is,
        would be parsed as several entries — or as a `@type` directive. It is refused and named."""
        self.assertIn("const dictionaryLine = (type, value) =>", self.JS)
        self.assertIn(r"/[\n\r|]/.test(value)", self.JS)
        # The CALL, not just the helper: asserting that the filter exists while nothing uses it is
        # an assertion that cannot fail. (Measured: removing the call left the suite green.)
        self.assertIn("dictionaryLine(type, proposal.value)", self.JS)
        self.assertIn("non aggiunte", self.JS)

    def test_a_capped_candidate_scan_is_stated_out_loud(self) -> None:
        """`candidates_capped` means the list is partial: silence would read as 'nothing found'."""
        self.assertIn("candidates_capped", self.JS)


class SuggestTest(unittest.TestCase):
    """The local-model seam from the UI: proposals, and the failures that must NOT look empty.

    The model is a real loopback HTTP server answering a canned OpenAI body, and the app server is
    the real one started with `--suggest-url`: the path exercised here is the path that runs.
    """

    ANSWER = json.dumps({"candidates": [
        {"value": "Contoso", "type": "AZIENDA", "reason": "cliente"},
        {"value": "ACME Holdings", "type": "AZIENDA", "reason": "non e' nel testo"},
    ]})

    @staticmethod
    def _model_server(answer: str):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                body = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @staticmethod
    def _spawn(tmp: Path, *extra: str):
        port = free_port()
        process = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(port), *extra],
            env={**os.environ, "ANON_HOME": str(tmp)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 15
        page = None
        while time.time() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"server died: {process.stderr.read()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    page = response.read().decode()
                break
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.2)
        if page is None:
            raise AssertionError("server did not start")
        match = re.search(r'window\.ANON_TOKEN = "([^"]+)"', page)
        assert match, "token not injected into the page"
        return process, port, match.group(1)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-web-suggest-"))
        (self.tmp / "maps").mkdir()
        (self.tmp / "catalogs").mkdir()
        self.model = self._model_server(self.ANSWER)
        self.process, self.port, self.token = self._spawn(
            self.tmp,
            "--suggest-url", f"http://127.0.0.1:{self.model.server_address[1]}/v1/chat/completions",
            "--suggest-model", "fake",
        )

    def tearDown(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.model.shutdown()
        self.model.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path: str, payload: dict | None = None, server: tuple | None = None) -> tuple[int, dict]:
        process, port, token = server or (self.process, self.port, self.token)
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=data,
            headers={TOKEN_HEADER: token, "Content-Type": "application/json"}, method="POST" if data else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_the_state_names_the_local_model(self) -> None:
        status, body = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(body["suggest"])
        self.assertIn("fake", body["suggest_backend"])

    def test_proposals_are_located_and_a_hallucination_is_dropped(self) -> None:
        status, body = self.call("/api/suggest", {"text": "Il cliente Contoso ha rinnovato."})
        self.assertEqual(status, 200, body)
        self.assertEqual([item["value"] for item in body["candidates"]], ["Contoso"])
        self.assertNotIn("ACME Holdings", json.dumps(body), "a value that is not in the text is dropped")
        start, end = body["candidates"][0]["spans"][0]["start"], body["candidates"][0]["spans"][0]["end"]
        self.assertEqual("Il cliente Contoso ha rinnovato."[start:end], "Contoso")

    def test_the_seam_leaves_nothing_behind(self) -> None:
        self.call("/api/suggest", {"text": "Il cliente Contoso ha rinnovato."})
        self.assertEqual(list((self.tmp / "maps").glob("*.map.json")), [], "the seam writes no map")
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), ["catalogs", "maps"])

    def test_a_backend_that_does_not_answer_is_an_error_not_an_empty_list(self) -> None:
        # Nothing listens on this loopback port: the failure must be an ERROR, because an empty
        # 200 would read as "nothing sensitive found".
        process, port, token = self._spawn(
            self.tmp, "--suggest-url", "http://127.0.0.1:9/v1/chat/completions", "--suggest-model", "dead"
        )
        try:
            status, body = self.call("/api/suggest", {"text": "Contoso"}, server=(process, port, token))
            self.assertEqual(status, 502, body)
            self.assertIn("did not answer", body["error"])
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_without_configuration_the_endpoint_says_so(self) -> None:
        process, port, token = self._spawn(self.tmp)
        try:
            status, body = self.call("/api/state", server=(process, port, token))
            self.assertFalse(body["suggest"], "no endpoint means the seam is off")
            status, body = self.call("/api/suggest", {"text": "Contoso"}, server=(process, port, token))
            self.assertEqual(status, 400, body)
            self.assertIn("not configured", body["error"])
        finally:
            process.terminate()
            process.wait(timeout=5)


class RateLimitTest(unittest.TestCase):
    """The API token bucket: a runaway client gets a 429, not a pinned worker thread."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="anon-web-rate-"))
        (cls.tmp / "maps").mkdir()
        (cls.tmp / "catalogs").mkdir()
        cls.port = free_port()
        cls.env = {**os.environ, "ANON_HOME": str(cls.tmp)}
        cls.process = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(cls.port), "--rate-limit", "3"],
            env=cls.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 15
        page = None
        while time.time() < deadline:
            if cls.process.poll() is not None:
                raise AssertionError(f"server died: {cls.process.stderr.read()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/", timeout=1) as response:
                    page = response.read().decode()
                break
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.2)
        if page is None:
            raise AssertionError("server did not start")
        match = re.search(r'window\.ANON_TOKEN = "([^"]+)"', page)
        assert match, "token not injected into the page"
        cls.token = match.group(1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _call(self) -> tuple[int, dict, str]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/state", headers={TOKEN_HEADER: self.token}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read().decode()

    def test_the_bucket_refuses_after_the_burst(self) -> None:
        results = [self._call() for _ in range(8)]
        statuses = [status for status, _headers, _body in results]
        self.assertIn(200, statuses, f"nothing succeeded: {statuses}")
        self.assertIn(429, statuses, f"the limit never fired: {statuses}")
        first_refusal = statuses.index(429)
        self.assertTrue(all(code == 200 for code in statuses[:first_refusal]),
                        f"the burst must come first: {statuses}")
        self.assertTrue(all(code == 429 for code in statuses[first_refusal:]),
                        f"the refusal must stay sticky: {statuses}")
        headers, body = next((headers, body) for status, headers, body in results if status == 429)
        self.assertIn("Retry-After", headers, "a 429 must say when to come back")
        # With --rate-limit 3 the bucket refills in ~20s: a constant `1` would send the client
        # back into the wall again and again.
        self.assertGreater(int(headers["Retry-After"]), 1, "Retry-After must come from the bucket")
        self.assertIn("error", body, "the refusal is JSON, not a half-processed job")
        self.assertIn("too many requests", body)


class ConverterCapTest(unittest.TestCase):
    """The converter's output is capped WHILE it is produced, not after buffering it.

    `communicate()` used to read the whole output into memory first, so a zip bomb inflated the
    server before the cap was ever compared. The fake converter here writes far more than the
    cap and never stops on its own: the answer must arrive anyway, and quickly.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="anon-web-cap-"))
        (cls.tmp / "maps").mkdir()
        (cls.tmp / "catalogs").mkdir()
        cls.converter = cls.tmp / "fake_converter.py"
        cls.converter.write_text(
            "import sys\n"
            "chunk = 'A' * 4096\n"
            "for _ in range(20000):\n"  # ~80 MB: the cap must stop it long before this
            "    sys.stdout.write(chunk)\n"
            "    sys.stdout.flush()\n",
            encoding="utf-8",
        )
        cls.port = free_port()
        cls.env = {
            **os.environ,
            "ANON_HOME": str(cls.tmp),
            "ANON_CONVERTER": str(cls.converter),
            "ANON_CONVERT_MAX_BYTES": "1024",
        }
        cls.process = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(cls.port)],
            env=cls.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 15
        page = None
        while time.time() < deadline:
            if cls.process.poll() is not None:
                raise AssertionError(f"server died: {cls.process.stderr.read()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/", timeout=1) as response:
                    page = response.read().decode()
                break
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.2)
        if page is None:
            raise AssertionError("server did not start")
        match = re.search(r'window\.ANON_TOKEN = "([^"]+)"', page)
        assert match, "token not injected into the page"
        cls.token = match.group(1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_runaway_converter_is_cut_off(self) -> None:
        import zipfile

        docx = self.tmp / "bomb.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", "<w:t>x</w:t>")

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/anonymize-document",
            data=docx.read_bytes(),
            headers={
                TOKEN_HEADER: self.token,
                "X-Filename": "bomb.docx",
                "Content-Type": "application/octet-stream",
            },
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, body = response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read().decode()
        elapsed = time.time() - started
        self.assertEqual(status, 500, body)
        self.assertIn("more than 1 KB", body)
        self.assertLess(elapsed, 20, "the cap must abort the child, not wait for it")


class UploadCapTest(unittest.TestCase):
    """The upload cap is a DEFAULT, not a constant: an operator with a genuinely huge document
    raises it deliberately (ANON_MAX_UPLOAD_BYTES) instead of turning the tool off. Loading the
    module under a patched environment is enough — the cap is a module-level constant."""

    def _cap(self) -> int:
        spec = importlib.util.spec_from_file_location("anon_web_cap", SERVER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return int(module.MAX_BODY_BYTES)

    def test_the_cap_defaults_to_160_mb_and_follows_the_environment(self) -> None:
        original = os.environ.get("ANON_MAX_UPLOAD_BYTES")
        try:
            os.environ.pop("ANON_MAX_UPLOAD_BYTES", None)
            self.assertEqual(self._cap(), 160 * 1024 * 1024)
            os.environ["ANON_MAX_UPLOAD_BYTES"] = str(500 * 1024 * 1024)
            self.assertEqual(self._cap(), 500 * 1024 * 1024)
        finally:
            if original is None:
                os.environ.pop("ANON_MAX_UPLOAD_BYTES", None)
            else:
                os.environ["ANON_MAX_UPLOAD_BYTES"] = original


class WebUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="anon-web-test-"))
        (cls.tmp / "maps").mkdir()
        (cls.tmp / "catalogs").mkdir()
        (cls.tmp / "entities.txt").write_text("AZIENDA|Contoso\n", encoding="utf-8")
        (cls.tmp / "catalogs" / "cities.txt").write_text(
            "@type CITTÀ\n@match case-sensitive\n@context (?:sede di)\\s+\nAncona\n", encoding="utf-8"
        )
        cls.port = free_port()
        cls.env = {**os.environ, "ANON_HOME": str(cls.tmp)}
        cls.process = subprocess.Popen(
            # --rate-limit 0: the functional tests must not depend on the token bucket (RateLimitTest
            # owns that); without it, adding one more request tipped the suite over 120/min.
            [sys.executable, str(SERVER), "--port", str(cls.port), "--rate-limit", "0"],
            env=cls.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 15
        page = None
        while time.time() < deadline:
            # Check for death FIRST: a bare `except Exception` around the probe would swallow the
            # AssertionError that reports why the server died.
            if cls.process.poll() is not None:
                raise AssertionError(f"server died: {cls.process.stderr.read()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/", timeout=1) as response:
                    page = response.read().decode()
                break
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.2)
        if page is None:
            raise AssertionError(
                f"server did not start (stderr: {cls.process.stderr.read() if cls.process.poll() is not None else 'still running'})"
            )
        match = re.search(r'window\.ANON_TOKEN = "([^"]+)"', page)
        assert match, "token not injected into the page"
        cls.token = match.group(1)
        cls.page = page

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
        for stream in (cls.process.stdout, cls.process.stderr):
            if stream:
                stream.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- helpers -----------------------------------------------------------------
    def call_bytes(self, path: str) -> tuple[int, bytes]:
        """GET a binary response (the streamed document) without trying to read it as JSON."""
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        request.add_header(TOKEN_HEADER, self.token)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def call(self, path: str, payload: dict | None = None, headers: dict | None = None,
             method: str | None = None, raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                         method=method or ("POST" if data else "GET"))
        request.add_header(TOKEN_HEADER, self.token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as error:
            body = error.read().decode()
            try:
                return error.code, json.loads(body)
            except json.JSONDecodeError:
                return error.code, {"raw": body}

    # --- tests -------------------------------------------------------------------
    def test_page_and_state(self) -> None:
        self.assertIn("anon-tool", self.page)
        self.assertNotIn("__ANON_TOKEN__", self.page, "the token placeholder must be replaced")
        self.assertNotIn("__ANON_NONCE__", self.page, "the nonce placeholder must be replaced")
        status, info = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(info["schema"], "anon/1")
        self.assertEqual([c["name"] for c in info["catalogs"]], ["cities"])
        self.assertEqual(info["patterns"], ["identity", "network", "legal"])

    def test_csp_nonce_matches_the_inline_token_script(self) -> None:
        """`script-src 'self'` alone BLOCKS the inline token script: the UI would be dead in a
        browser (every API call 403) while HTTP-only tests stayed green. The nonce must match."""
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as response:
            csp = response.headers.get("Content-Security-Policy", "")
            page = response.read().decode()
        match = re.search(r"'nonce-([^']+)'", csp)
        self.assertIsNotNone(match, f"no nonce in the CSP: {csp!r}")
        nonce = match.group(1)
        self.assertIn(f'nonce="{nonce}"', page, "the script tag must carry the same nonce")
        self.assertIn(f'window.ANON_TOKEN = "{self.token}"', page)

    def test_security_guards(self) -> None:
        without_token = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/state")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(without_token, timeout=5)
        self.assertEqual(caught.exception.code, 403, "the API must reject a request without the token")

        status, _payload = self.call("/api/state", headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403, "a foreign Host header (DNS rebinding) must be refused")

        status, _payload = self.call("/api/state", headers={"Origin": "http://evil.example.com"})
        self.assertEqual(status, 403, "a foreign Origin must be refused")

    def test_anonymize_and_reveal_and_deanonymize(self) -> None:
        status, result = self.call("/api/anonymize", {
            "text": "Cliente Contoso S.r.l. e sede di Ancona, referente mario@contoso.it\n",
            "catalogs": ["cities"],
            "patterns": ["identity", "network", "legal"],
        })
        self.assertEqual(status, 200)
        tag = result["tag"]
        self.assertIn(f"[AZIENDA-1-{tag}]", result["redacted"])
        self.assertIn(f"sede di [CITTÀ-1-{tag}]", result["redacted"])
        self.assertIn(f"[EMAIL-1-{tag}]", result["redacted"])
        self.assertTrue(result["map_id"])
        self.assertEqual({item["type"] for item in result["entries"]}, {"AZIENDA", "CITTÀ", "EMAIL"})

        status, revealed = self.call("/api/maps/reveal", {"id": result["map_id"], "confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(revealed["entries"][f"[EMAIL-1-{tag}]"]["original"], "mario@contoso.it")

        status, refused = self.call("/api/maps/reveal", {"id": result["map_id"]})
        self.assertEqual(status, 400, "revealing the real values needs an explicit confirmation")

        restored = f"Cliente [AZIENDA-1-{tag}] e sede di [CITTÀ-1-{tag}], referente [EMAIL-1-{tag}]\n"
        status, back = self.call("/api/deanonymize", None,
                                 headers={"X-Filename": "finale.txt", "X-Map-Id": result["map_id"],
                                          "Content-Type": "application/octet-stream"},
                                 raw=restored.encode())
        self.assertEqual(status, 200)
        self.assertTrue(back["report"]["complete"])
        content = base64.b64decode(back["content_b64"]).decode()
        self.assertIn("Contoso S.r.l.", content)
        self.assertIn("mario@contoso.it", content)

    def test_audit_never_returns_the_values(self) -> None:
        status, result = self.call("/api/audit", {"text": "Il cliente Con Toso e mario@contoso.it\n"})
        self.assertEqual(status, 200)
        self.assertEqual(result["verdict"], "sensitive")
        raw = json.dumps(result)
        self.assertNotIn("contoso.it", raw)
        self.assertNotIn("Con Toso", raw)
        self.assertNotIn("Contoso", raw)

        status, revealed = self.call("/api/audit", {"text": "Il cliente Con Toso e mario@contoso.it\n", "reveal": True})
        self.assertEqual(status, 200)
        self.assertEqual(revealed["near_miss"][0]["token"], "Con Toso")

    def test_entities_can_be_read_and_saved(self) -> None:
        status, data = self.call("/api/entities")
        self.assertEqual(status, 200)
        self.assertIn("Contoso", data["text"])

        status, saved = self.call("/api/entities", {"text": "AZIENDA|Contoso\nPERSONA|Mario Rossi\n"}, method="PUT")
        self.assertEqual(status, 200)
        self.assertEqual(saved["entries"], 2)

        bad = self.call("/api/entities", {"text": "@nope x\nAZIENDA|Y\n"}, method="PUT")
        self.assertEqual(bad[0], 400, "a broken dictionary must be rejected, not written")
        status, after = self.call("/api/entities")
        self.assertIn("Mario Rossi", after["text"], "the previous dictionary must still be intact")

    def test_named_dictionaries_are_addressable_and_unknown_ones_are_refused(self) -> None:
        # Each dictionary has its own file; `file` selects it, and an unknown name is a 400 (never a
        # silent fallback that would write into the wrong file).
        status, people = self.call("/api/entities?file=people")
        self.assertEqual(status, 200)
        self.assertEqual(people["name"], "people")
        self.assertIn("people.txt", people["path"])
        self.assertEqual(sorted(people["files"]), ["clients", "entities", "people"])

        status, saved = self.call(
            "/api/entities?file=clients", {"text": "@type AZIENDA\nContoso S.p.A.\n"}, method="PUT"
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["name"], "clients")
        self.assertIn("clients.txt", saved["path"])
        status, again = self.call("/api/entities?file=clients")
        self.assertIn("Contoso S.p.A.", again["text"])
        # The generic file must be untouched by a save into clients.
        status, generic = self.call("/api/entities")
        self.assertNotIn("Contoso S.p.A.", generic["text"])

        for bad in ("/api/entities?file=nope", "/api/entities?file=../../etc/passwd"):
            status, _ = self.call(bad)
            self.assertEqual(status, 400, bad)

    def test_map_list_exposes_metadata_only(self) -> None:
        status, data = self.call("/api/maps")
        self.assertEqual(status, 200)
        for entry in data["maps"]:
            self.assertNotIn("original", json.dumps(entry))
            self.assertIn("entries", entry)
        # The listing is capped; the total is not, and the truncation is declared.
        self.assertEqual(data["total"], len(data["maps"]))
        self.assertFalse(data["truncated"])

    def test_a_corrupt_map_is_listed_not_skipped(self) -> None:
        """Skipping an unreadable map made the listing disagree with `total` and hid the file."""
        broken = self.tmp / "maps" / "20200101-000000-deadbe.map.json"
        broken.write_text("{ questo non e' json", encoding="utf-8")
        wrong_shape = self.tmp / "maps" / "20200102-000000-feeded.map.json"
        wrong_shape.write_text("[1, 2, 3]", encoding="utf-8")
        wrong_types = self.tmp / "maps" / "20200103-000000-badbad.map.json"
        wrong_types.write_text('{"entries": 5, "counts": "no", "created": 7}', encoding="utf-8")
        null_entry = self.tmp / "maps" / "20200105-000000-nullva.map.json"
        null_entry.write_text('{"entries": {"[EMAIL-1]": null}}', encoding="utf-8")
        nul_name = self.tmp / "maps" / "20200106-000000-nulnam.map.json"
        nul_name.write_text('{"entries": {}, "source": "a\u0000b"}', encoding="utf-8")
        try:
            status, data = self.call("/api/maps")
            self.assertEqual(status, 200)
            entry = next((item for item in data["maps"] if item["id"].startswith("20200101")), None)
            self.assertIsNotNone(entry, "the corrupt map must still appear")
            self.assertTrue(entry["unreadable"])
            odd = next((item for item in data["maps"] if item["id"].startswith("20200102")), None)
            self.assertIsNotNone(odd, "valid JSON of the wrong shape must not 500 the endpoint")
            self.assertTrue(odd["unreadable"])
            typed = next((item for item in data["maps"] if item["id"].startswith("20200103")), None)
            self.assertIsNotNone(typed, "a dict with wrong VALUE types must not 500 the endpoint")
            # `entries` is not an object, so this is not a map at all: it is reported as unreadable
            # rather than shown as "0 entries" (which would be a silent lie about its content).
            self.assertTrue(typed["unreadable"])
            self.assertEqual(data["total"], len(data["maps"]), "total must match what is listed")
            for prefix, why in (("20200105", "a null entry value"), ("20200106", "a NUL in source")):
                item = next((entry for entry in data["maps"] if entry["id"].startswith(prefix)), None)
                self.assertIsNotNone(item, f"{why} must not 500 the listing")
            self.assertEqual(data["total"], len(data["maps"]))
        finally:
            for path in (broken, wrong_shape, wrong_types, null_entry, nul_name):
                path.unlink()

    def test_a_wrong_shaped_request_body_is_a_400_not_a_500(self) -> None:
        """The body is client input: a JSON array must be refused, not crash the handler."""
        for body in (b"[1, 2, 3]", b'"solo una stringa"', b"5"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/audit", data=body, method="POST"
            )
            request.add_header(TOKEN_HEADER, self.token)
            request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    status, payload = response.status, response.read().decode()
            except urllib.error.HTTPError as error:
                status, payload = error.code, error.read().decode()
            self.assertEqual(status, 400, f"{body!r} -> {status}: {payload}")
            self.assertNotIn("AttributeError", payload)

    def test_a_deeply_nested_body_is_a_400_not_a_500(self) -> None:
        """`json.loads` raises RecursionError (not ValueError) on a deeply nested body."""
        body = b"[" * 200_000 + b"]" * 200_000  # the depth that raises RecursionError (measured)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/anonymize", data=body, method="POST"
        )
        request.add_header(TOKEN_HEADER, self.token)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, payload = response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read().decode()
        self.assertEqual(status, 400, f"{status}: {payload[:200]}")
        self.assertIn("malformed JSON", payload)
        self.assertNotIn("Traceback", payload)

    def test_a_wrong_typed_filter_field_is_a_400(self) -> None:
        """`{"catalogs": 5}` reached an iteration over an int: 500 before, 400 now."""
        for payload in (
            {"text": "ciao", "catalogs": 5},
            {"text": "ciao", "patterns": True},
        ):
            status, body = self.call("/api/anonymize", payload=payload)
            self.assertEqual(status, 400, f"{payload} -> {status}: {body}")
            self.assertNotIn("TypeError", str(body))

    def test_a_deeply_nested_map_is_refused_not_crashed(self) -> None:
        nested = self.tmp / "maps" / "20200108-000000-nest00.map.json"
        nested.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
        try:
            status, listing = self.call("/api/maps")
            self.assertEqual(status, 200, listing)
            item = next((entry for entry in listing["maps"] if entry["id"].startswith("20200108")), None)
            self.assertIsNotNone(item, "a nested map must appear as unreadable, not 500 the listing")
            status, body = self.call(
                "/api/maps/reveal", payload={"id": "20200108-000000-nest00", "confirm": True}
            )
            self.assertEqual(status, 400, body)
            self.assertIn("malformed JSON", str(body))
            self.assertNotIn("Traceback", str(body))
        finally:
            nested.unlink()

    def test_a_malformed_entry_value_is_refused_on_reveal(self) -> None:
        """`entries` can be a dict while its VALUES are still wrong: validated one level deeper."""
        broken = self.tmp / "maps" / "20200107-000000-nullva.map.json"
        broken.write_text('{"entries": {"[EMAIL-1]": null}}', encoding="utf-8")
        try:
            status, payload = self.call(
                "/api/maps/reveal", payload={"id": "20200107-000000-nullva", "confirm": True}
            )
            self.assertEqual(status, 400, payload)
            self.assertNotIn("AttributeError", str(payload))
        finally:
            broken.unlink()

    def test_a_wrong_shaped_map_is_refused_on_reveal(self) -> None:
        """`load_map` is reachable from the API too: a non-object map must be a 400, not a 500."""
        broken = self.tmp / "maps" / "20200104-000000-cafeba.map.json"
        broken.write_text("[1, 2, 3]", encoding="utf-8")
        try:
            status, payload = self.call(
                "/api/maps/reveal", payload={"id": "20200104-000000-cafeba", "confirm": True}
            )
            self.assertEqual(status, 400, payload)
            self.assertNotIn("AttributeError", str(payload))
        finally:
            broken.unlink()

    def test_audit_declares_a_truncated_findings_list(self) -> None:
        """/api/audit capped `findings` at 50 in silence, exactly like the CLI used to."""
        text = "\n".join(f"utente{index}@cliente{index}.it" for index in range(1, 61))
        status, data = self.call(
            "/api/audit", payload={"text": text}, headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["total"], 60)
        self.assertLess(len(data["findings"]), 60)
        self.assertTrue(data["findings_truncated"])

        status, small = self.call(
            "/api/audit", payload={"text": "solo utente9@cliente9.it\n"}, headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 200, small)
        self.assertFalse(small["findings_truncated"], "the flag must be false, not merely absent")

    def test_state_reports_the_upload_cap(self) -> None:
        """The UI refuses an oversized file before spending the transfer, so it must learn the cap
        from the server instead of keeping its own copy (the hard-coded hint drifted silently)."""
        status, info = self.call("/api/state")
        self.assertEqual(status, 200, info)
        self.assertEqual(info["max_upload_bytes"], 160 * 1024 * 1024)

    def test_the_map_count_is_not_capped_by_the_listing(self) -> None:
        """`/api/state` counted through the capped list: 300 maps would have reported 100."""
        before = len(list((self.tmp / "maps").glob("*.map.json")))
        created = []
        for index in range(105):
            path = self.tmp / "maps" / f"20200101-000000-{index:06d}.map.json"
            path.write_text('{"entries": {}}', encoding="utf-8")
            created.append(path)
        try:
            status, maps = self.call("/api/maps")
            self.assertEqual(status, 200)
            self.assertEqual(len(maps["maps"]), 100, "the listing is capped")
            self.assertEqual(maps["total"], before + 105, "the total must count every map")
            self.assertTrue(maps["truncated"])
            status, state = self.call("/api/state")
            self.assertEqual(state["maps_count"], before + 105)
        finally:
            for path in created:
                path.unlink()

    @unittest.skipUnless(DOCX_AVAILABLE, "document converter not installed")
    def test_document_upload_offers_the_redacted_document_and_the_text(self) -> None:
        """One redaction, two artifacts: the .docx handed back and the .md the model reads.

        They must belong to the SAME allocation. If the tab redacted the Markdown while the
        container pass redacted the file separately, the two would share a tag while meaning
        different things — `[EMAIL-1-<tag>]` in the .docx and in the .md would be two different
        addresses — and a restore holding the wrong map would resolve in silence.
        """
        work = Path(tempfile.mkdtemp(prefix="anon-web-doc-"))
        try:
            markdown = work / "doc.md"
            markdown.write_text("Cliente Contoso con referente mario@contoso.it\n", encoding="utf-8")
            docx = work / "doc.docx"
            subprocess.run(["pandoc", str(markdown), "-o", str(docx)], check=True)
            status, result = self.call("/api/anonymize-document", None,
                                       headers={"X-Filename": "doc.docx", "X-Catalogs": "",
                                                "X-Patterns": "identity", "Content-Type": "application/octet-stream"},
                                       raw=docx.read_bytes())
            self.assertEqual(status, 200, result)
            self.assertEqual(result["origin"], "container", "a docx is rewritten, not converted away")
            self.assertNotIn("container_error", result)
            tag = result["tag"]
            self.assertEqual(result["container_name"], "doc.redacted.docx")
            # Fetched, not carried: the response names a URL and the document streams from it.
            self.assertNotIn("container_b64", result, "the document must not travel inside the JSON")
            status_doc, docx_bytes = self.call_bytes(result["container_url"])
            self.assertEqual(status_doc, 200)

            # The document that comes back is a real container, and the value is gone from it.
            with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
                inside = "\n".join(archive.read(name).decode("utf-8", "replace")
                                   for name in archive.namelist())
            self.assertIn(f"[EMAIL-1-{tag}]", inside)
            self.assertNotIn("mario@contoso.it", inside)
            self.assertNotIn("Contoso", inside)

            # Same tag, same placeholders, both artifacts: the two views of one redaction.
            self.assertIn(f"[EMAIL-1-{tag}]", result["redacted"])
            self.assertIn(f"[AZIENDA-1-{tag}]", result["redacted"])
            self.assertNotIn("mario@contoso.it", result["redacted"])

            # And the map on disk is the one both of them refer to.
            map_path = self.tmp / "maps" / f"{result['map_id']}.map.json"
            saved = json.loads(map_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["tag"], tag)
            self.assertEqual(saved["entries"][f"[EMAIL-1-{tag}]"]["original"], "mario@contoso.it")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @unittest.skipUnless(DOCX_AVAILABLE, "document converter not installed")
    def test_a_filename_with_a_space_or_an_accent_can_be_downloaded(self) -> None:
        """The download URL is used by a BROWSER, which percent-encodes it, while the server hands
        the path over still encoded. Without the quote/unquote pair a document called
        "mia relazione.docx" — an ordinary name — answered 400 and could not be downloaded at all."""
        work = Path(tempfile.mkdtemp(prefix="anon-web-name-"))
        try:
            for name in ("mia relazione.docx", "relazione-perché.docx", "100%.docx"):
                markdown = work / "body.md"
                markdown.write_text("Cliente Contoso\n", encoding="utf-8")
                docx = work / "body.docx"
                subprocess.run(["pandoc", str(markdown), "-o", str(docx)], check=True)
                status, result = self.call("/api/anonymize-document", None,
                                           headers={"X-Filename": name, "X-Catalogs": "",
                                                    "X-Patterns": "identity",
                                                    "Content-Type": "application/octet-stream"},
                                           raw=docx.read_bytes())
                self.assertEqual(status, 200, (name, result))
                # Fetched exactly as returned: the URL already carries the encoding a browser sends.
                status_doc, blob = self.call_bytes(result["container_url"])
                self.assertEqual(status_doc, 200, (name, result["container_url"]))
                self.assertTrue(zipfile.is_zipfile(io.BytesIO(blob)), f"{name}: not a container")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_the_download_refuses_a_missing_token_and_a_traversal(self) -> None:
        """Two ways of asking for a document that is not yours to ask for."""
        import urllib.request as urlrequest

        plain = urlrequest.Request(f"http://127.0.0.1:{self.port}/api/download/deadbeefdeadbeef/x.docx")
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urlrequest.urlopen(plain, timeout=10)  # no token header
        self.assertEqual(denied.exception.code, 403)
        for path in ("/api/download/../../../etc/passwd", "/api/download/nope",
                     "/api/download/deadbeefdeadbeef/../../maps", "/api/download/zzzz/x.docx"):
            status, _body = self.call_bytes(path)
            self.assertEqual(status, 400, path)

    def test_an_unreadable_container_is_refused_and_writes_no_map(self) -> None:
        """A file that claims to be a container but cannot be opened must not be guessed at.

        It is handed to the converter (the PDF path), which cannot make sense of it either: the
        answer is an error and NOTHING is written — no map, no half-redacted artifact.
        """
        before = {path.name for path in (self.tmp / "maps").iterdir()}
        status, result = self.call("/api/anonymize-document", None,
                                   headers={"X-Filename": "finto.docx", "X-Catalogs": "",
                                            "X-Patterns": "identity", "Content-Type": "application/octet-stream"},
                                   raw=b"PK\x03\x04" + bytes(range(256)) * 20)
        self.assertGreaterEqual(status, 400, result)
        self.assertEqual({path.name for path in (self.tmp / "maps").iterdir()}, before,
                         "a refused document must not leave a map behind")


class DocumentFallbackTest(unittest.TestCase):
    """The PDF path: a container the engine cannot open falls back to the Markdown conversion.

    Regression test for a deadlock found in adversarial review. `_anonymize_document_file` took
    TAG_LOCK and then called `_anonymize_text`, which takes it again; `threading.Lock` is not
    reentrant, so the request hung forever and left the lock held — one PDF froze every later
    request too. The watchdog below is the assertion: a deadlocked call never finishes.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-web-fallback-"))
        (self.tmp / "maps").mkdir()
        self.env = {**os.environ, "ANON_HOME": str(self.tmp)}
        spec = importlib.util.spec_from_file_location("anon_web_fallback", SERVER)
        assert spec and spec.loader
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_unopenable_container_falls_back_instead_of_deadlocking(self) -> None:
        source = self.tmp / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n" + b"x" * 64)
        outcome: dict = {}

        def run() -> None:
            with mock.patch.object(self.module.anon, "anonymize_container",
                                   side_effect=self.module.anon.UnreadableContainer("REFUSED — not a ZIP")), \
                 mock.patch.object(self.module, "_convert_to_markdown",
                                   return_value="Cliente Contoso, mario@contoso.it"):
                outcome["result"] = self.module._anonymize_document_file(source, "doc.pdf", None, None)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(),
                         "the fallback deadlocked: it re-took TAG_LOCK inside the block that holds it")
        self.assertEqual(outcome["result"]["origin"], "converted")
        self.assertIn("container_error", outcome["result"])
        self.assertNotIn("container_b64", outcome["result"], "nothing is handed back for a PDF")
        self.assertIn("CONTOSO", outcome["result"]["redacted"].upper())
        self.assertNotIn("mario@contoso.it", outcome["result"]["redacted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
