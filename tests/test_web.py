#!/usr/bin/env python3
"""Integration tests for the local web UI (`web/server.py`).

Starts the real server in a subprocess against a hermetic `ANON_HOME` and drives it over HTTP:
the engine endpoints, plus the security guards (token, Host header, Origin) that make a
loopback-only, unauthenticated UI an acceptable trade-off.

  python3 ~/.anon/tests/test_web.py
"""

from __future__ import annotations

import base64
import concurrent.futures
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

# The stdlib PDF writer, loaded by path: the server imports it, and a test that builds its own PDF
# fixture must use the same code, not a second implementation.
_pdf_spec = importlib.util.spec_from_file_location("pdfout_for_web", HOME / "pdfout.py")
assert _pdf_spec and _pdf_spec.loader
pdfout = importlib.util.module_from_spec(_pdf_spec)
sys.modules["pdfout_for_web"] = pdfout
_pdf_spec.loader.exec_module(pdfout)


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
    I18N = (HOME / "web" / "i18n.js").read_text(encoding="utf-8")

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

    def test_the_highlight_view_never_writes_document_text_as_html(self) -> None:
        """The redacted text comes from the operator's document, so the view is built with
        `textContent` only — the rule this file already states for anything we did not write — and a
        pill carries the TYPE, never a real value (which the redacted text does not contain)."""
        self.assertIn('id="highlight-view"', self.HTML)
        self.assertIn("renderHighlight(result.redacted)", self.JS)
        self.assertIn("span.textContent = chunk", self.JS)
        self.assertIn("pill.textContent = match[1]", self.JS)
        self.assertIn("pill.dataset.type = match[1]", self.JS)
        self.assertNotIn('$("highlight-view").innerHTML', self.JS)

    def test_the_redaction_report_carries_no_real_value(self) -> None:
        """The report is an attestation, not a reveal: it reads only the counts, the placeholder
        list and the map id — never the mapping the reveal action populates."""
        self.assertIn('id="download-report"', self.HTML)
        self.assertIn("state.lastReport = redactionReport(", self.JS)
        body = self.JS.split("function redactionReport", 1)[1].split("\nfunction ", 1)[0]
        for field in ("result.counts", "result.entries", "result.map_id"):
            self.assertIn(field, body, f"the report no longer states {field}")
        self.assertNotIn("mapping", body, "the report must not touch the revealed mapping")

    def _element_html(self) -> dict:
        """id -> inner HTML for every element, via a parser (nested tags break a regex)."""
        from html.parser import HTMLParser

        source = self.HTML

        class Collector(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=False)
                self.stack: list[dict] = []
                self.found: dict[str, str] = {}

            def handle_starttag(self, tag, attrs):
                if tag in ("br", "meta", "link", "input", "img", "hr"):
                    return
                self.stack.append({"tag": tag, "id": dict(attrs).get("id"), "start": self.getpos()})

            def handle_endtag(self, tag):
                for index in range(len(self.stack) - 1, -1, -1):
                    if self.stack[index]["tag"] == tag:
                        node = self.stack.pop(index)
                        break
                else:
                    return
                if node["id"]:
                    # inner HTML is re-read from the source between this tag and its close: the
                    # parser does not expose offsets, so the collector is only used for the ids.
                    self.found[node["id"]] = ""

        collector = Collector()
        collector.feed(source)
        # inner HTML by regex on the id, good enough because the assertion below compares
        # STRUCTURE (ids and interactive tags), not text
        out = {}
        for key in set(re.findall(r'id="([^"]+)"', source)):
            match = re.search(rf'<[a-z0-9]+[^>]*\bid="{re.escape(key)}"[^>]*>', source)
            if not match:
                continue
            start = match.end()
            end = source.find("</", start)
            out[key] = source[start:end] if end > 0 else ""
        return out

    def test_every_string_app_js_composes_has_both_languages(self) -> None:
        """A `i18n.t("key")` with no entry in the table shows the KEY to the user ("suggest.paste").

        Worse, a key present in Italian and missing in English shows Italian in an English session,
        which is the defect this pass exists to remove. Both directions are asserted, so adding a
        string to app.js without translating it fails the gate instead of shipping half-translated.
        """
        used = set(re.findall(r'i18n\.t\("([a-z][a-zA-Z0-9._-]+)"', self.JS))
        self.assertGreater(len(used), 30, "app.js must compose its strings through the table")
        body = self.I18N.split("const DYNAMIC = {", 1)[1] if "const DYNAMIC = {" in self.I18N else ""
        self.assertNotEqual(body, "", "the dynamic table must exist")
        italian = body.split("  en: {", 1)[0]
        english = body.split("  en: {", 1)[1]
        it_keys = set(re.findall(r'^\s{4}"?([a-z][\w.-]*)"?:', italian, re.M))
        en_keys = set(re.findall(r'^\s{4}"?([a-z][\w.-]*)"?:', english, re.M))
        self.assertEqual(sorted(used - it_keys), [], "keys used in app.js and missing from the table")
        self.assertEqual(sorted(used - en_keys), [], "keys used in app.js without an English string")

    def test_the_labels_app_js_composes_come_from_the_table_not_the_code(self) -> None:
        """A string written straight into app.js is invisible to the dictionary, so it stays
        Italian in an English session: `Opzioni — tutti i pattern · nessun catalogo` shipped that
        way, because a hard-coded literal passes the key test above. These words may live only in
        the i18n table."""
        banned = [
            "tutti i pattern",
            "nessun pattern",
            "nessun catalogo",
            "nessuna sostituzione",
            "Nessuna mappa: anonimizza",
            "non ancora mostrata",
            "valori reali rimossi dalla pagina",
            "fatto: file scaricato",
            "INCOMPLETO: vedi il dettaglio",
            "aggiunte: premi Salva",
            "Elaborazione…",
            "Salvo…",
            "richiesta fallita",
            "caricamento interrotto",
            "} voci",
            "riga ${",
            "verdetto:",
        ]
        for phrase in banned:
            self.assertNotIn(
                phrase, self.JS, f"{phrase!r} is hard-coded in app.js, so it can never translate"
            )

    def test_a_translation_never_drops_an_interactive_child_or_a_live_value(self) -> None:
        """The English value must keep everything the Italian element carries.

        The first version replaced a `<label>`'s inner HTML and deleted the pattern checkboxes
        inside it: in English the summary reported "no patterns" while every pattern still ran, and
        switching back re-checked what the user had unticked. A translated element must not own an
        interactive child, and must not own a LIVE child either (`#upload-limit`, `#entities-path`
        are written by app.js, so a re-inserted copy would freeze at its load-time value).
        """
        inside = self._element_html()
        pairs = re.findall(r'"#([a-z0-9-]+)":\s*\{\s*inner:\s*("(?:[^"\\]|\\.)*")', self.I18N)
        self.assertGreater(len(pairs), 30, "the dictionary must not shrink to nothing")
        for key, literal in pairs:
            italian = inside.get(key, "")
            english = json.loads(literal)
            self.assertNotEqual(italian, "", f"{key}: the element must exist in index.html")
            for live_id in re.findall(r'id="([^"]+)"', italian):
                self.assertIn(
                    live_id, english,
                    f"{key}: the translation drops the child element id={live_id!r}, which would "
                    "detach a live value or a control",
                )
            for tag in ("input", "button", "select", "textarea"):
                self.assertEqual(
                    len(re.findall(rf"<{tag}\b", italian)), len(re.findall(rf"<{tag}\b", english)),
                    f"{key}: the translation changes the number of <{tag}> elements",
                )

    def test_every_translation_key_still_exists_in_the_html(self) -> None:
        """A translation key that no longer matches an element is a DEFECT, not clutter.

        The dictionary is keyed by selector, so renaming an id in the markup silently orphans its
        English text and the interface shows Italian in an English session — a drift nobody would
        notice by reading either file alone.
        """
        keys = set(re.findall(r'"(#[a-z0-9-]+)":', self.I18N))
        ids = set(re.findall(r'id="([^"]+)"', self.HTML))
        orphans = sorted(key for key in keys if key.lstrip("#") not in ids)
        self.assertGreater(len(keys), 30, "the dictionary must not shrink to nothing")
        self.assertEqual(orphans, [], f"translation keys without an element: {orphans}")

    def test_the_language_selector_offers_italian_and_english(self) -> None:
        """Default Italian: the markup holds the Italian text, and the English lives in i18n.js."""
        select = re.search(r'<select id="lang"[^>]*>(.*?)</select>', self.HTML, re.S)
        self.assertIsNotNone(select, "the language selector must be in the header")
        self.assertIn('<option value="it">Italiano</option>', select.group(1))
        self.assertIn('<option value="en">English</option>', select.group(1))
        self.assertIn('src="/i18n.js"', self.HTML)
        self.assertLess(
            self.HTML.index('src="/i18n.js"'), self.HTML.index('src="/app.js"'),
            "i18n.js must load before app.js, which uses it",
        )
        # La pagina servita è italiana: nessuna stringa inglese del dizionario finisce nell'HTML.
        for english in ("Custom dictionary", "Drop the document here", "Put the real values back"):
            self.assertNotIn(english, self.HTML)

    def test_the_local_model_panel_is_discoverable(self) -> None:
        """Unconfigured is a STATE, not a reason to hide the panel: a feature nobody can find is a
        feature that does not exist. It stays visible, inert, and says how to turn it on."""
        card = re.search(r'<div class="card" id="suggest-card"[^>]*>', self.HTML)
        self.assertIsNotNone(card, "the panel must be in the Dizionario tab")
        self.assertNotIn("hidden", card.group(0), "the panel is hidden again: it becomes undiscoverable")
        # "inert" is the half that matters: the hint shows and the fields are closed in the HTML
        # itself, before any script runs, so a JS failure cannot leave them armed.
        self.assertIn('<div id="suggest-fields" hidden>', self.HTML)
        hint = re.search(r'<p class="muted small" id="suggest-off"[^>]*>', self.HTML)
        self.assertIsNotNone(hint, "the hint that says how to enable the seam must exist")
        self.assertNotIn("hidden", hint.group(0))
        # and the wiring that keeps it that way, asserted at the call site (a test that only
        # asserted the ids passed while the bug it was written for was live).
        self.assertIn('$("suggest-fields").hidden = !state.suggest', self.JS)
        self.assertIn('$("suggest-off").hidden = state.suggest', self.JS)

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
        # The message now lives in the i18n table: the code must name the key, not the words.
        self.assertIn('i18n.t("entities.addedSkipped"', self.JS)

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
    def _model_server(answer: str, broken_stream: bool = False):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if payload.get("stream"):
                    self._stream(answer, broken_stream)
                    return
                body = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, content: str, broken: bool) -> None:
                """Three deltas and a terminator; no Content-Length, so the body ends at the close."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in (content[:7], content[7:21], content[21:]):
                    if not chunk:
                        continue
                    frame = {"choices": [{"delta": {"content": chunk}}]}
                    self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                    self.wfile.flush()
                if broken:
                    self.wfile.write(b"data: {oops not json\n\n")
                else:
                    self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

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

    def test_the_state_states_the_model_window_and_timeout(self) -> None:
        """The page learns the model's bounds from the server — the character window that reaches
        it, and the timeout after which the call gives up — instead of keeping a copy that drifts
        (the same rule as the upload cap). A long text with no declared window is what made the
        panel look frozen before it timed out."""
        status, body = self.call("/api/state")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["suggest_max_chars"], 20000, "the module's declared window")
        self.assertEqual(body["suggest_timeout"], 60.0)

        process, port, token = self._spawn(
            self.tmp,
            "--suggest-url", "http://127.0.0.1:9/v1/chat/completions",
            "--suggest-model", "dead", "--suggest-timeout", "12",
        )
        try:
            status, configured = self.call("/api/state", server=(process, port, token))
            self.assertEqual(status, 200, configured)
            self.assertEqual(configured["suggest_timeout"], 12.0, "the timeout is the backend's own")
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_the_state_states_the_chunking_configuration(self) -> None:
        """Chunking is OPT-IN: with no flag the seam still truncates, and the state says so."""
        status, body = self.call("/api/state")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["suggest_chunk_chars"], 0)
        self.assertEqual(body["suggest_chunk_overlap"], 500)

    def test_chunking_covers_a_long_document_from_the_ui(self) -> None:
        process, port, token = self._spawn(
            self.tmp,
            "--suggest-url", f"http://127.0.0.1:{self.model.server_address[1]}/v1/chat/completions",
            "--suggest-model", "fake",
            "--suggest-chunk-chars", "1000", "--suggest-chunk-overlap", "200",
        )
        try:
            status, state = self.call("/api/state", server=(process, port, token))
            self.assertEqual(status, 200, state)
            self.assertEqual(state["suggest_chunk_chars"], 1000)
            self.assertEqual(state["suggest_chunk_overlap"], 200)
            status, report = self.call(
                "/api/suggest", {"text": "Contoso " + "x" * 3000}, server=(process, port, token)
            )
            self.assertEqual(status, 200, report)
            self.assertGreater(report["chunks"], 1)
            self.assertFalse(report["truncated"], "the tail is covered, not dropped")
            self.assertEqual([item["value"] for item in report["candidates"]], ["Contoso"])
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_a_chunk_pair_that_cannot_advance_is_refused_at_startup(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SERVER), "--port", str(free_port()),
             "--suggest-url", "http://127.0.0.1:9/v1/chat/completions", "--suggest-model", "m",
             "--suggest-chunk-chars", "100", "--suggest-chunk-overlap", "100"],
            env={**os.environ, "ANON_HOME": str(self.tmp)}, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("chunk-overlap", result.stderr)

    def call_text(self, path: str, payload: dict, server: tuple | None = None) -> tuple[int, str]:
        """A raw body, for a response that is not JSON (the event stream)."""
        process, port, token = server or (self.process, self.port, self.token)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
            headers={TOKEN_HEADER: token, "Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    @staticmethod
    def events(raw: str) -> list[dict]:
        """The events in an SSE body, in order."""
        found = []
        for frame in raw.split("\n\n"):
            line = frame.strip()
            if line.startswith("data:"):
                found.append(json.loads(line[5:].strip()))
        return found

    def test_the_streamed_endpoint_reports_the_same_proposals(self) -> None:
        """The streamed call must produce the report the blocking one produces, from one code path,
        and a value that is not in the text must stay dropped when it arrives in pieces."""
        text = "Il cliente Contoso ha rinnovato."
        status, raw = self.call_text("/api/suggest-stream", {"text": text})
        self.assertEqual(status, 200, raw)
        found = self.events(raw)
        self.assertGreaterEqual(len(found), 3, raw)
        self.assertEqual(found[0]["event"], "start")
        self.assertEqual(found[-1]["event"], "done")
        self.assertTrue(all(event["event"] == "delta" for event in found[1:-1]), raw)
        deltas = "".join(event["text"] for event in found if event["event"] == "delta")
        self.assertIn("Contoso", deltas, "the model's own answer is what is streamed")
        _status, blocking = self.call("/api/suggest", {"text": text})
        self.assertEqual(found[-1]["report"]["candidates"], blocking["candidates"])
        self.assertEqual([item["value"] for item in found[-1]["report"]["candidates"]], ["Contoso"],
                         "ACME Holdings is not in the text: the stream must not relax the location check")

    def test_the_streamed_call_writes_nothing_either(self) -> None:
        self.call_text("/api/suggest-stream", {"text": "Il cliente Contoso ha rinnovato."})
        self.assertEqual(list((self.tmp / "maps").glob("*.map.json")), [], "the seam writes no map")

    def test_a_stream_that_breaks_is_an_error_event_not_a_short_answer(self) -> None:
        """A failure after the stream began cannot change the status: it must be an EVENT, so the
        client is told instead of being handed a partial answer that looks complete."""
        broken = self._model_server(self.ANSWER, broken_stream=True)
        process, port, token = self._spawn(
            self.tmp,
            "--suggest-url", f"http://127.0.0.1:{broken.server_address[1]}/v1/chat/completions",
            "--suggest-model", "fake",
        )
        try:
            status, raw = self.call_text("/api/suggest-stream", {"text": "Cliente Contoso."},
                                         server=(process, port, token))
        finally:
            process.terminate()
            process.wait(timeout=5)
            broken.shutdown()
            broken.server_close()
        self.assertEqual(status, 200, raw)
        found = self.events(raw)
        self.assertEqual(found[-1]["event"], "error", raw)
        self.assertIn("did not answer", found[-1]["message"])

    def test_without_a_model_the_stream_is_refused_before_it_opens(self) -> None:
        """No model configured: a normal JSON 400, not an empty event stream (which would read as
        "the model found nothing")."""
        process, port, token = self._spawn(self.tmp)
        try:
            status, raw = self.call_text("/api/suggest-stream", {"text": "Contoso"},
                                         server=(process, port, token))
        finally:
            process.terminate()
            process.wait(timeout=5)
        self.assertEqual(status, 400, raw)
        self.assertIn("not configured", json.loads(raw)["error"])

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


class BurstTest(unittest.TestCase):
    """Simultaneous connections must not be dropped by the listen backlog.

    `socketserver.TCPServer.request_queue_size` is 5: it is the number of connections the kernel
    holds for a server that has not accepted them yet. Under that default, 48 simultaneous
    connections left about half of them reset in EVERY run (19-28 of 48, over four measurement
    sessions) — the client sees a socket error (`URLError: [Errno 54] Connection reset by peer`
    most often) instead of a status, and the server logs nothing. The fix is the number the kernel
    is told to hold; this asserts the BEHAVIOUR, because the attribute is what a later edit would
    silently drop.
    """

    BURST = 48

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="anon-web-burst-"))
        (cls.tmp / "maps").mkdir()
        (cls.tmp / "catalogs").mkdir()
        (cls.tmp / "entities.txt").write_text("AZIENDA|Contoso\n", encoding="utf-8")
        # The rate limit is off on purpose: with it on, the tail of the burst is a 429 and the
        # test would measure the bucket instead of the backlog.
        cls.process, cls.port, cls.token = SuggestTest._spawn(cls.tmp, "--rate-limit", "0")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_burst_of_simultaneous_connections_answers_every_one(self) -> None:
        # 48 is above the default backlog (5) by enough that the failure is not a coin flip: the
        # measurement above left about half of them reset, in every run of four sessions.
        body = json.dumps({"text": "Rif. Contoso in data odierna.", "catalogs": [],
                           "patterns": []}).encode()

        def one(_index: int) -> object:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/anonymize", data=body,
                headers={TOKEN_HEADER: self.token, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code
            except OSError as error:  # ConnectionResetError is an OSError
                return f"{type(error).__name__}: {error}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.BURST) as pool:
            results = list(pool.map(one, range(self.BURST)))
        failures = [result for result in results if result != 200]
        self.assertEqual(failures, [], f"{len(failures)}/{self.BURST} connections failed: {failures[:4]}")


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

    def test_state_reports_the_build_fingerprint(self) -> None:
        """The page shows the build and `check-container-fresh.py` compares it: the version alone
        moves only on a release, so it cannot tell a stale container from a current one."""
        status, info = self.call("/api/state")
        self.assertEqual(status, 200, info)
        build = info["build"]
        self.assertIsInstance(build, str)
        self.assertEqual(len(build), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in build), build)

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

    @unittest.skipUnless(DOCX_AVAILABLE, "document converter not installed")
    def test_a_pdf_is_rebuilt_redacted_from_the_markdown(self) -> None:
        """A PDF is never rewritten in place: the answer is a NEW text-only PDF built from the
        redacted Markdown, same tag and map, and the response says the layout is lost."""
        work = Path(tempfile.mkdtemp(prefix="anon-web-pdf-"))
        try:
            pdf = work / "verbale.pdf"
            pdf.write_bytes(pdfout.build_pdf("Cliente Contoso, referente mario@contoso.it\n"))
            status, result = self.call("/api/anonymize-document", None,
                                       headers={"X-Filename": "verbale.pdf", "X-Catalogs": "",
                                                "X-Patterns": "identity",
                                                "Content-Type": "application/octet-stream"},
                                       raw=pdf.read_bytes())
            self.assertEqual(status, 200, result)
            self.assertEqual(result["origin"], "converted")
            self.assertEqual(result["container_name"], "verbale.redacted.pdf")
            self.assertIn("layout", result["container_error"])
            status_doc, blob = self.call_bytes(result["container_url"])
            self.assertEqual(status_doc, 200)
            self.assertTrue(blob.startswith(b"%PDF-"), blob[:8])
            # The rebuilt PDF is readable, carries the placeholder and not the real value.
            rebuilt = work / "rebuilt.pdf"
            rebuilt.write_bytes(blob)
            markdown = subprocess.run([sys.executable, str(HOME / "convert.py"), str(rebuilt)],
                                      capture_output=True, text=True)
            if markdown.returncode != 0 or not markdown.stdout.strip():
                self.skipTest(f"anydoc could not read the rebuilt PDF: {markdown.stderr[:120]}")
            self.assertIn(f"[EMAIL-1-{result['tag']}]", markdown.stdout)
            self.assertNotIn("mario@contoso.it", markdown.stdout)
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
