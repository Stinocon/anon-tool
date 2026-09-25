#!/usr/bin/env python3
"""Tests for the local-model seam (`suggest.py`): loopback-only, fail-closed, writes nothing.

The seam is the one place in this project that talks to anything, so the tests are about the
boundary rather than about model quality: a non-loopback endpoint must be refused BEFORE a request
is made, a backend that fails must be an ERROR and never an empty "nothing found", and the module
must leave the filesystem exactly as it found it — no redacted copy, no map.

  python3 tests/test_suggest.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
HOME = HERE.parent
SUGGEST_PY = HOME / "suggest.py"

# `suggest.py` imports the engine by name, so the engine's directory has to be importable.
sys.path.insert(0, str(HOME))
_spec = importlib.util.spec_from_file_location("anon_suggest", SUGGEST_PY)
assert _spec and _spec.loader
suggest = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = suggest
_spec.loader.exec_module(suggest)


def completion(content: str) -> str:
    """An OpenAI-compatible chat body carrying `content` as the assistant message."""
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})


def sse(content: str) -> bytes:
    """One server-sent-event line carrying `content` as a delta."""
    return ("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n").encode()


class SilentBackend:
    """The unit-test backend: no transport at all, a fixed answer."""

    name = "silent"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


class LoopbackTest(unittest.TestCase):
    """The security property: the data must not leave the machine."""

    def test_loopback_endpoints_are_accepted(self) -> None:
        for url in (
            "http://127.0.0.1:11434/v1/chat/completions",
            "http://127.9.9.9:1234/v1/chat/completions",
            "http://localhost:8080/v1/chat/completions",
            "http://[::1]:11434/v1/chat/completions",
        ):
            self.assertEqual(suggest.check_loopback(url), url)

    def test_a_remote_host_is_refused_as_not_loopback(self) -> None:
        for url in (
            "http://192.168.1.10:11434/v1/chat/completions",  # the LAN, one typo away
            "http://10.0.0.2:8080/v1/chat/completions",
            "https://api.openai.com/v1/chat/completions",
            "http://example.com/v1/chat/completions",
            "http://my-nas.local:11434/v1/chat/completions",  # a NAME: it does not resolve here
            "http://[2001:db8::1]:11434/v1/chat/completions",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                suggest.check_loopback(url)
            self.assertIn("loopback", str(caught.exception).lower())

    def test_a_malformed_endpoint_is_its_own_refusal(self) -> None:
        for url, expected in (("ftp://127.0.0.1/v1", "http(s)"), ("http:///v1/chat/completions", "no host")):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                suggest.check_loopback(url)
            self.assertIn(expected, str(caught.exception))

    def test_the_backend_refuses_at_construction_not_at_request_time(self) -> None:
        with self.assertRaises(ValueError):
            suggest.LoopbackBackend("http://192.168.1.10:11434/v1/chat/completions", "any")


class BackendTest(unittest.TestCase):
    def test_the_request_is_openai_shaped_and_deterministic(self) -> None:
        seen: dict[str, object] = {}

        def transport(payload, url, timeout, headers):  # noqa: ANN001 - the transport signature
            seen.update(payload=payload, url=url, timeout=timeout, headers=headers)
            return completion('{"candidates": []}')

        backend = suggest.LoopbackBackend(
            "http://127.0.0.1:11434/v1/chat/completions", "qwen", transport=transport,
            api_key="local-key", headers={"x-mtplx-client": "pi"},
        )
        self.assertEqual(backend.complete("testo"), '{"candidates": []}')
        payload = seen["payload"]
        self.assertEqual(payload["model"], "qwen")  # type: ignore[index]
        self.assertEqual(payload["temperature"], 0)  # type: ignore[index]
        self.assertFalse(payload["stream"])  # type: ignore[index]
        self.assertEqual(payload["messages"][1]["content"], "testo")  # type: ignore[index]
        self.assertIn("JSON only", payload["messages"][0]["content"])  # type: ignore[index]
        # A reasoning model spends the budget thinking: the default must leave room for an answer.
        self.assertGreaterEqual(payload["max_tokens"], 256)  # type: ignore[operator]
        # The headers a local server may require (MTPLX tags the client; many want a bearer key).
        self.assertEqual(seen["headers"]["x-mtplx-client"], "pi")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer local-key")

    def test_constrained_decoding_pins_the_answer_to_the_schema(self) -> None:
        """The shape is enforced by the BACKEND's decoding, not defended against afterwards."""
        seen: dict[str, object] = {}

        def transport(payload, url, timeout, headers):  # noqa: ANN001, ARG001
            seen.update(payload=payload)
            return completion('{"candidates": []}')

        free = suggest.LoopbackBackend("http://127.0.0.1:8080/v1/chat/completions", "qwen", transport=transport)
        free.complete("testo")
        # Default OFF: an endpoint that rejects the field must not become a 400 on every call.
        self.assertNotIn("response_format", seen["payload"])

        pinned = suggest.LoopbackBackend("http://127.0.0.1:8080/v1/chat/completions", "qwen",
                                         transport=transport, constrained=True)
        pinned.complete("testo")
        response_format = seen["payload"]["response_format"]  # type: ignore[index]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["schema"]["required"], ["candidates"])
        self.assertEqual(response_format["json_schema"]["schema"]["additionalProperties"], False)

    def test_a_failing_transport_is_an_error_never_an_empty_answer(self) -> None:
        def transport(payload, url, timeout, headers):  # noqa: ANN001, ARG001
            raise TimeoutError("no answer in 60s")

        backend = suggest.LoopbackBackend("http://127.0.0.1:11434/v1/chat/completions", "qwen", transport=transport)
        with self.assertRaises(suggest.BackendError) as caught:
            backend.complete("testo")
        self.assertIn("TimeoutError", str(caught.exception))

    def test_a_backend_error_or_a_weird_body_is_an_error(self) -> None:
        for raw in (
            "not json at all",
            json.dumps({"error": "model not found"}),
            json.dumps({"choices": []}),
            json.dumps([1, 2, 3]),
            json.dumps({"choices": [{"message": {"content": 5}}]}),
        ):
            backend = suggest.LoopbackBackend(
                "http://127.0.0.1:11434/v1/chat/completions", "qwen", transport=lambda *a, r=raw: r
            )
            with self.subTest(raw=raw), self.assertRaises(suggest.BackendError):
                backend.complete("testo")


class StreamTest(unittest.TestCase):
    """`complete_stream` reads server-sent events; `suggest_events` reports the same work in pieces.

    The deltas are PROGRESS, never a verdict. The tests pin that the joined pieces go through the
    SAME parse-and-locate pass as the blocking call, that the two faces produce one report, and that
    a broken stream is an ERROR rather than a short answer.
    """

    URL = "http://127.0.0.1:11434/v1/chat/completions"

    def _streaming(self, lines: list[bytes], **kwargs):
        seen: list[dict] = []

        def stream_transport(payload, url, timeout, headers):  # noqa: ANN001, ARG001
            seen.append(payload)
            yield from lines

        return suggest.LoopbackBackend(self.URL, "qwen", stream_transport=stream_transport, **kwargs), seen

    def test_the_streamed_request_asks_for_a_stream_and_the_pieces_join(self) -> None:
        backend, seen = self._streaming([sse('{"cand'), sse('idates": []}'), b"data: [DONE]\n"])
        self.assertEqual("".join(backend.complete_stream("testo")), '{"candidates": []}')
        self.assertTrue(seen[0]["stream"], "the blocking payload must not be sent")
        self.assertEqual(seen[0]["messages"][1]["content"], "testo")

    def test_noise_lines_are_ignored_and_a_reasoning_delta_is_used(self) -> None:
        lines = [
            b"\n",
            b": keep-alive\n",
            b"event: message\n",
            b'data: {"choices": [{"delta": {"reasoning_content": "pen"}}]}\n',
            b'data: {"choices": [{"delta": {}}]}\n',
            b"data: [DONE]\n",
        ]
        backend, _ = self._streaming(lines)
        self.assertEqual("".join(backend.complete_stream("x")), "pen")

    def test_a_non_json_chunk_is_an_error_not_an_empty_piece(self) -> None:
        """Swallowing it would drop part of the answer and report "the answer holds no JSON"."""
        backend, _ = self._streaming([b"data: not json\n"])
        with self.assertRaises(suggest.BackendError):
            list(backend.complete_stream("x"))

    def test_a_stream_that_breaks_mid_answer_is_an_error(self) -> None:
        def stream_transport(payload, url, timeout, headers):  # noqa: ANN001, ARG001
            yield sse('{"cand')
            raise TimeoutError("the model stopped answering")

        backend = suggest.LoopbackBackend(self.URL, "qwen", stream_transport=stream_transport)
        with self.assertRaises(suggest.BackendError) as caught:
            list(backend.complete_stream("x"))
        self.assertIn("did not answer", str(caught.exception))

    def test_an_error_object_inside_the_stream_is_an_error(self) -> None:
        backend, _ = self._streaming([b'data: {"error": {"message": "no model loaded"}}\n'])
        with self.assertRaises(suggest.BackendError) as caught:
            list(backend.complete_stream("x"))
        self.assertIn("no model loaded", str(caught.exception))

    def test_a_backend_without_a_stream_still_emits_one_delta(self) -> None:
        events = list(suggest.suggest_events("testo", SilentBackend('{"candidates": []}')))
        self.assertEqual([event["event"] for event in events], ["start", "delta", "done"])
        self.assertEqual(events[2]["report"]["candidates"], [])

    def test_the_streamed_report_equals_the_blocking_report(self) -> None:
        """One source of truth: the two faces of the feature must agree on the same document."""
        text = "Il cliente Contoso, referente mario.rossi@contoso.it\n"
        answer = '{"candidates": [{"value": "Contoso", "type": "AZIENDA", "reason": "cliente"}]}'
        backend, _ = self._streaming([sse(answer[:20]), sse(answer[20:]), b"data: [DONE]\n"])
        blocking = suggest.suggest(
            text, suggest.LoopbackBackend(self.URL, "qwen", transport=lambda *a: completion(answer))
        )
        events = list(suggest.suggest_events(text, backend))
        self.assertEqual(events[-1]["report"], blocking)
        self.assertEqual(events[0]["detected"], blocking["detected"])
        deltas = [event["text"] for event in events if event["event"] == "delta"]
        self.assertEqual("".join(deltas), answer, "the deltas are the whole answer, in pieces")
        self.assertEqual(len(deltas), 2, "in TWO pieces: a one-piece stream would pass by accident")

    def test_a_hallucinated_value_is_still_dropped_when_it_arrives_in_pieces(self) -> None:
        """A value the model invents is not in the document: the stream must not relax that."""
        answer = '{"candidates": [{"value": "Nome Inventato", "type": "PERSONA"}]}'
        backend, _ = self._streaming([sse(answer[:30]), sse(answer[30:])])
        events = list(suggest.suggest_events("Il cliente Contoso.\n", backend))
        self.assertEqual(events[-1]["report"]["candidates"], [])


class HeadersTest(unittest.TestCase):
    """`--header` and `--api-key`: what a local server may require, refused when malformed."""

    def test_headers_parse_and_a_malformed_one_is_refused(self) -> None:
        self.assertEqual(suggest.parse_headers(["x-a: 1", "X-B:due:tre"]), {"x-a": "1", "X-B": "due:tre"})
        self.assertEqual(suggest.parse_headers(None), {})
        for bad in ("niente-due-punti", ": valore"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                suggest.parse_headers([bad])

    def test_an_explicit_authorization_header_wins_over_the_api_key(self) -> None:
        backend = suggest.LoopbackBackend(
            "http://127.0.0.1:8000/v1/chat/completions", "m", api_key="ignored",
            headers={"authorization": "Custom xyz"}, transport=lambda *a: "{}",
        )
        self.assertEqual(backend.headers["authorization"], "Custom xyz")
        self.assertNotIn("Authorization", backend.headers)

    def test_a_reasoning_model_that_answers_in_reasoning_content(self) -> None:
        """Qwen/MTPLX with a small budget: `content` empty, the answer in `reasoning_content`."""
        body = json.dumps({"choices": [{"message": {
            "role": "assistant", "content": "",
            "reasoning_content": 'Thinking...\\n{"candidates": [{"value": "Contoso"}]}',
        }}]})
        proposals = suggest.parse_candidates(suggest._content_of(body), "Il cliente Contoso.")
        self.assertEqual([p.value for p in proposals], ["Contoso"])

    def test_both_content_and_reasoning_empty_is_still_a_failure(self) -> None:
        body = json.dumps({"choices": [{"message": {"content": "   ", "reasoning_content": ""}}]})
        with self.assertRaises(suggest.BackendError):
            suggest.parse_candidates(suggest._content_of(body), "testo")


class ParsingTest(unittest.TestCase):
    TEXT = "Il cliente Contoso S.r.l. ha come referente Mario Rossi, ticket 2026-4711.\n"

    def test_a_value_is_located_by_us_not_trusted_from_the_model(self) -> None:
        answer = completion(json.dumps({"candidates": [
            {"value": "Contoso S.r.l.", "type": "AZIENDA", "reason": "cliente"},
            {"value": "Mario Rossi", "type": "PERSONA", "reason": "referente"},
            {"value": "ACME Holdings", "type": "AZIENDA", "reason": "inventato"},   # not in the text
            {"value": "2026-4711", "type": "ALTRO", "reason": "ticket"},
        ]}))
        proposals = suggest.parse_candidates(suggest._content_of(answer), self.TEXT)
        values = [proposal.value for proposal in proposals]
        self.assertEqual(values, ["Contoso S.r.l.", "Mario Rossi", "2026-4711"])
        self.assertNotIn("ACME Holdings", values, "a value that is not in the document is dropped")
        for proposal in proposals:
            for start, end in proposal.spans:
                self.assertEqual(self.TEXT[start:end], proposal.value, "the span is where WE found it")

    def test_a_placeholder_is_not_re_proposed(self) -> None:
        answer = completion(json.dumps({"candidates": [{"value": "[EMAIL-1-9f2c1a]"}]}))
        self.assertEqual(suggest.parse_candidates(suggest._content_of(answer), "resta [EMAIL-1-9f2c1a]"), [])

    def test_a_fenced_or_prose_wrapped_answer_still_parses(self) -> None:
        for content in (
            '```json\n{"candidates": [{"value": "Contoso"}]}\n```',
            'Sure! Here it is: {"candidates": [{"value": "Contoso"}]} Hope that helps.',
            '["Contoso"]',
        ):
            proposals = suggest.parse_candidates(content, self.TEXT)
            self.assertEqual([proposal.value for proposal in proposals], ["Contoso"], content)

    def test_a_bare_list_of_strings_yields_altro(self) -> None:
        proposals = suggest.parse_candidates('{"candidates": ["Contoso", 5, {"value": ""}]}', self.TEXT)
        self.assertEqual([(p.value, p.type) for p in proposals], [("Contoso", "ALTRO")])

    def test_a_proposal_that_the_engine_already_detected_is_marked(self) -> None:
        engine = SilentBackend(json.dumps({"candidates": [
            {"value": "Contoso S.r.l."}, {"value": "Mario Rossi"},
        ]}))
        with tempfile.TemporaryDirectory() as tmp:
            dictionary = Path(tmp) / "entities.txt"
            dictionary.write_text("AZIENDA|Contoso\n", encoding="utf-8")
            entities = suggest.anon.load_entities(dictionary)
            report = suggest.suggest(self.TEXT, engine, entities=entities)
        marked = {item["value"]: item["overlaps_detected"] for item in report["candidates"]}
        self.assertTrue(marked["Contoso S.r.l."], "the dictionary already finds it")
        self.assertFalse(marked["Mario Rossi"], "nothing else does")

    def test_the_tail_of_a_long_document_is_declared_not_silently_dropped(self) -> None:
        backend = SilentBackend('{"candidates": []}')
        report = suggest.suggest("x" * 5000, backend, max_chars=1000)
        self.assertEqual(report["analyzed_chars"], 1000)
        self.assertTrue(report["truncated"])
        self.assertEqual(len(backend.prompts[0]), 1000)


class CliTest(unittest.TestCase):
    """End to end, against a real HTTP server on 127.0.0.1 — the path that would actually run."""

    ANSWER_CONTENT = json.dumps({"candidates": [
        {"value": "Contoso", "type": "AZIENDA", "reason": "cliente"},
        {"value": "Mario Rossi", "type": "PERSONA", "reason": "referente"},
    ]})
    ANSWERS = [completion(ANSWER_CONTENT)]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-suggest-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.source = self.tmp / "nota.txt"
        self.source.write_text("Cliente Contoso, referente Mario Rossi.\n", encoding="utf-8")
        self.env = {**os.environ, "ANON_HOME": str(self.home)}
        self.requests: list[dict] = []

        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                requests.append(payload)
                if payload.get("stream"):
                    self._stream(CliTest.ANSWER_CONTENT)
                    return
                body = CliTest.ANSWERS[0].encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, content: str) -> None:
                """Three deltas and a terminator. No Content-Length: the body ends at the close."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in (content[:15], content[15:40], content[40:]):
                    if chunk:
                        self.wfile.write(sse(chunk))
                        self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n")
                self.wfile.flush()

            def log_message(self, *args) -> None:  # keep the test output clean
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_suggest(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SUGGEST_PY), *args], capture_output=True, text=True, env=self.env, check=False
        )

    def test_a_local_model_is_asked_and_the_answer_becomes_a_review_list(self) -> None:
        before = sorted(path.name for path in self.home.rglob("*"))
        result = self.run_suggest(str(self.source), "--url", self.url, "--model", "fake", "--json")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["mode"], "suggest")
        self.assertEqual([item["value"] for item in report["candidates"]], ["Contoso", "Mario Rossi"])
        self.assertEqual(self.source.read_text(encoding="utf-8"),
                         "Cliente Contoso, referente Mario Rossi.\n", "the source is never modified")
        self.assertEqual(sorted(path.name for path in self.home.rglob("*")), before,
                         "the seam writes nothing: no map, no redacted copy")
        self.assertEqual(self.requests[0]["model"], "fake")

    def test_a_non_loopback_endpoint_is_refused_before_any_request(self) -> None:
        result = self.run_suggest(str(self.source), "--url", "http://192.168.1.10:11434/v1/chat/completions",
                                  "--model", "fake")
        self.assertEqual(result.returncode, 2)
        self.assertIn("loopback", result.stderr.lower())
        self.assertEqual(self.requests, [], "nothing was sent")

    def test_a_dead_backend_is_an_error_not_an_empty_result(self) -> None:
        # Nothing listens on this loopback port: the failure must be LOUD, because a silent empty
        # answer would read as "the model found nothing" and the document would be trusted.
        result = self.run_suggest(str(self.source), "--url", "http://127.0.0.1:9/v1/chat/completions",
                                  "--model", "fake")
        self.assertEqual(result.returncode, 2)
        self.assertIn("did not answer", result.stderr)

    def test_a_truncated_document_says_so_in_the_human_output(self) -> None:
        result = self.run_suggest(str(self.source), "--url", self.url, "--model", "fake",
                                  "--max-chars", "5")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("TRUNCATED", result.stdout, "the operator must see that the tail was not sent")

    def test_streaming_shows_the_answer_while_it_arrives_and_the_report_is_unchanged(self) -> None:
        """`--stream` moves the PROGRESS to stderr; stdout stays the report, and it is the same one."""
        result = self.run_suggest(str(self.source), "--url", self.url, "--model", "fake",
                                  "--stream", "--json")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([item["value"] for item in report["candidates"]], ["Contoso", "Mario Rossi"])
        self.assertIn("Contoso", result.stderr, "the model's text is shown while it arrives")
        self.assertTrue(self.requests[0]["stream"], "the request asked for server-sent events")

    def test_an_empty_flag_is_an_error_not_a_silent_null_backend(self) -> None:
        for args in (("--url", "", "--model", "fake"), ("--url", self.url, "--model", "")):
            with self.subTest(args=args):
                result = self.run_suggest(str(self.source), *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("may be empty", result.stderr)

    def test_without_a_backend_nothing_is_asked_and_nothing_is_claimed(self) -> None:
        result = self.run_suggest(str(self.source))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing proposed", result.stdout)
        self.assertEqual(self.requests, [])


class RedirectTest(unittest.TestCase):
    """A redirect is the way a validated loopback URL becomes an off-machine request."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-suggest-redirect-"))
        self.source = self.tmp / "nota.txt"
        self.source.write_text("Cliente Contoso.\n", encoding="utf-8")
        self.env = {**os.environ, "ANON_HOME": str(self.tmp / "home")}
        (self.tmp / "home").mkdir()
        self.followed: list[str] = []

        followed = self.followed

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                followed.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args) -> None:
                return

        self.sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        threading.Thread(target=self.sink.serve_forever, daemon=True).start()
        sink_url = f"http://127.0.0.1:{self.sink.server_address[1]}/leak"

        class Redirector(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(302)
                self.send_header("Location", sink_url)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args) -> None:
                return

        self.redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=self.redirector.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.redirector.server_address[1]}/v1/chat/completions"

    def tearDown(self) -> None:
        for server in (self.redirector, self.sink):
            server.shutdown()
            server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_redirect_is_refused_rather_than_followed(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SUGGEST_PY), str(self.source), "--url", self.url, "--model", "fake"],
            capture_output=True, text=True, env=self.env, check=False, timeout=30,
        )
        self.assertEqual(self.followed, [], "the redirect target was never contacted")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("redirect refused", result.stderr)


class LocalModelTest(unittest.TestCase):
    """The seam against a REAL loopback model — skipped unless one is configured.

    `make test` must stay green on a machine with no model: the end-to-end path is exercised by
    pointing ANON_MODEL_URL at a running server (see `make model`), and by nothing else. Two
    contracts are checked, and both matter: the seam must return localized candidates when the
    model obeys the format, and it must FAIL (never return an empty list) when the model does not.
    """

    URL = os.environ.get("ANON_MODEL_URL", "")
    MODEL = os.environ.get("ANON_MODEL_NAME", "")

    def setUp(self) -> None:
        if not self.URL or not self.MODEL:
            self.skipTest("no local model configured (set ANON_MODEL_URL and ANON_MODEL_NAME)")

    def test_a_real_model_answers_with_a_report_or_fails_loudly(self) -> None:
        backend = suggest.LoopbackBackend(self.URL, self.MODEL, timeout=float(os.environ.get("ANON_MODEL_TIMEOUT", "200")))
        text = "Verbale per il cliente di Ancona: referente il dott. Rossi, tel. 02 1234567."
        started = time.monotonic()
        try:
            report = suggest.suggest(text, backend, entities=[])
        except suggest.BackendError as error:
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 260, "the seam must fail on the timeout, not hang past it")
            message = str(error)
            # A model that obeys the transport but not the FORMAT is a legitimate outcome: measured
            # with a 9B reasoning model, which narrated a "Thinking Process" for 43 s instead of
            # answering JSON, and the seam refused it as an error — the whole point. A transport
            # failure is NOT legitimate here: it means no model was exercised at all, which is what
            # this test exists to prevent (pointing it at a dead port would otherwise pass).
            contract_failure = "no JSON" in message or "non-JSON" in message
            self.assertTrue(
                contract_failure,
                f"the endpoint answered nothing usable at the transport level: {message[:200]}",
            )
            return
        elapsed = time.monotonic() - started
        self.assertIn("candidates", report)
        self.assertLess(elapsed, 260)
        for candidate in report["candidates"]:
            # Whatever the model proposed, it must have been LOCALIZED in the text by the engine:
            # a value that is not in the document cannot be a candidate.
            self.assertIn(candidate["value"], text)

    def test_a_constrained_backend_obeys_the_schema_on_a_real_model(self) -> None:
        """With the schema pinned, a reply that is not the schema cannot exist.

        Opt-in (`ANON_MODEL_CONSTRAINED=1`): an endpoint that REJECTS the `response_format` field
        would fail here, correctly, and that is not what the default run is asserting.
        """
        if not os.environ.get("ANON_MODEL_CONSTRAINED"):
            self.skipTest("set ANON_MODEL_CONSTRAINED=1 to exercise constrained decoding")
        backend = suggest.LoopbackBackend(
            self.URL, self.MODEL, timeout=float(os.environ.get("ANON_MODEL_TIMEOUT", "200")), constrained=True
        )
        text = "Verbale per il cliente di Ancona: referente il dott. Rossi, tel. 02 1234567."
        report = suggest.suggest(text, backend, entities=[])
        self.assertIn("candidates", report)


class BenchMetricTest(unittest.TestCase):
    """The `--corpus` quality metric must not be gameable: a FRAGMENT does not cover a value, or
    the headline "the model earns its place" number could be maxed by proposing one token each."""

    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).resolve().parent.parent / "scripts" / "bench-suggest.py"
        spec = importlib.util.spec_from_file_location("bench_suggest_metric", path)
        assert spec and spec.loader
        cls.bench = importlib.util.module_from_spec(spec)
        sys.modules["bench_suggest_metric"] = cls.bench
        spec.loader.exec_module(cls.bench)

    def test_a_fragment_does_not_cover_a_value(self) -> None:
        self.assertFalse(self.bench._covers("Mario", "Mario Rossi"))
        self.assertFalse(self.bench._covers("ario", "Mario Rossi"))
        self.assertTrue(self.bench._covers("Mario Rossi", "Mario Rossi"))
        self.assertTrue(self.bench._covers("Cliente Mario Rossi", "Mario Rossi"))

    def test_a_bounded_fragment_names_the_value(self) -> None:
        self.assertTrue(self.bench._named("Rossi", "Mario Rossi"))
        self.assertFalse(self.bench._named("ario", "Mario Rossi"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
