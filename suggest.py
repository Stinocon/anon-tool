#!/usr/bin/env python3
"""suggest.py — ask a LOCAL model for candidates, while the engine stays the only writer.

This is the seam of `docs/DESIGN.md` §7 / `docs/OPEN-ISSUES.md` #17: it PROPOSES spans and prints
them. It writes nothing — no redacted file, no map, no placeholder. Approving a candidate is a human
act; applying one is `anon.py`'s job, through the dictionary or an explicit `--entities` entry.

Three properties are not negotiable here, and each is enforced rather than intended:

  * LOOPBACK ONLY. The endpoint must be `localhost`, `127.0.0.0/8` or `::1`. Anything else is
    refused before a single byte is sent. The contract of this tool is that a document does not
    leave the machine; a "helpful" LAN endpoint would break it silently, which is worse than a
    refusal.
  * FAIL-CLOSED. A timeout, a non-2xx, unparseable output: ZERO candidates and exit 2, with the
    reason on stderr. "The model proposed nothing" and "the model never answered" are different
    facts and must not be presented with the same face.
  * THE MODEL DOES NOT DECIDE WHAT IS REDACTED. Its answer is located in the text BY US — a value it
    returns that is not in the document is dropped — reported as a proposal, and the deterministic
    engine remains the only writer. A hallucinating model costs a review, not a wrong redaction.

This module is the ONE place in the project that imports a network stack. `anon.py`, `deanon.py` and
`convert.py` are checked against that by `tests/test_anon.py::OfflineContractTest`, deliberately.

Modes
  python3 suggest.py FILE [--entities PATH ...] [--catalogs NAMES]
  python3 suggest.py FILE --url URL --model NAME [--json]

Exit codes
  0  a backend answered and proposed nothing (or no backend was configured: nothing to suggest)
  2  error (bad arguments, a non-loopback URL, a failed backend)
  4  candidates proposed — not clean, not proof: a review list (same meaning as `--audit`)
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol

import anon

VERSION = anon.VERSION
DEFAULT_TIMEOUT = 60.0
# What a local model is asked to SEND is bounded separately from what it may read: the engine's caps
# (160 MB upload, 12 MB scan) are sized for a scanner, not for a 32k-token context window.
DEFAULT_MAX_CHARS = 20_000
# A reasoning model needs room to think before it answers; too small a value spends the budget on
# `reasoning_content` and returns an empty `content`.
DEFAULT_MAX_TOKENS = 1024
# Chunking (opt-in): a document longer than the window can be covered by OVERLAPPING windows
# instead of being cut, so its tail is not silently lost. The overlap has to exceed the longest
# value that may straddle a cut: anything shorter appears WHOLE in one of the two neighbouring
# windows, and a half token — a prefix of a real value — is never shown to the model.
DEFAULT_CHUNK_OVERLAP = 500
MIN_VALUE_LENGTH = 3
MAX_OCCURRENCES = 20

# The shape the model is asked for, as a JSON Schema, so the BACKEND constrains the decoding
# instead of the seam defending against a free-form answer afterwards. A reasoning model that
# narrates its "Thinking Process" instead of answering cannot produce it at all when the grammar is
# pinned: the failure class disappears rather than being reported. The seam's fail-closed handling
# stays — a constrained backend can still time out, 500 or truncate.
CANDIDATES_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "type": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


class BackendError(RuntimeError):
    """The backend did not answer usably. Always fatal: never an empty result set."""


def check_loopback(url: str) -> str:
    """Refuse anything that is not this machine. Returns the URL unchanged when it is.

    A refusal here is the whole security property of the feature: without it, "local model" would be
    a hostname away from "somebody else's model".
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"the endpoint must be http(s), not {parsed.scheme or '(none)'!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("the endpoint has no host")
    if host == "localhost":
        return url
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            f"{host!r} is not a loopback address: the data must not leave this machine. "
            "Use http://127.0.0.1:PORT/... or http://localhost:PORT/..."
        ) from None
    if not address.is_loopback:
        raise ValueError(f"{address} is not a loopback address: the data must not leave this machine")
    return url


@dataclass
class Proposal:
    """One candidate, located in the document. `spans` are where WE found it, not where it said."""

    value: str
    type: str
    reason: str
    spans: list[tuple[int, int]] = field(default_factory=list)
    overlaps_detected: bool = False

    @property
    def count(self) -> int:
        return len(self.spans)

    def to_json(self) -> dict[str, object]:
        return {
            "value": self.value,
            "type": self.type,
            "reason": self.reason,
            "count": self.count,
            "spans": [{"start": start, "end": end} for start, end in self.spans],
            "overlaps_detected": self.overlaps_detected,
        }


SYSTEM_PROMPT = (
    "You are a detector for a LOCAL document anonymizer. Read the text and list the strings that "
    "identify a person, an organisation, a place, a device or a commercial relationship: names, "
    "company names, addresses, project codenames, contract or ticket numbers, hostnames, account "
    "identifiers, licence keys. Do NOT list ordinary words, technology names or generic roles. "
    'Answer with JSON only: {"candidates": [{"value": "<exact substring>", "type": "AZIENDA", '
    '"reason": "<short reason>"}]}. The "value" MUST be copied verbatim from the text. If there is '
    "nothing to report, answer {\"candidates\": []}."
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is how a VALIDATED loopback URL becomes a request to somebody else.

    `check_loopback` runs once, at construction. `urllib`'s default redirect handler would follow a
    301/302 to any host, so a "local model server" answering `Location: http://<public>/` would make
    THIS module issue a request off the machine — the one thing the seam promises not to do. A
    loopback endpoint has no legitimate reason to redirect, so the redirect is refused and becomes a
    transport failure (a `BackendError`: a failed backend, never a silent second request).

    Found by an adversarial review, not by the tests: the docstring said "no redirects" and nothing
    enforced it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect refused (it would leave the loopback): {newurl}", headers, fp
        )


def parse_headers(pairs: list[str] | None) -> dict[str, str]:
    """`--header "Name: value"` (repeatable) into a dict, refusing anything malformed.

    A local server often wants its own header (MTPLX tags the client, proxies want a key): an
    unparsable header is a mistake that would otherwise be sent as a literal name.
    """
    headers: dict[str, str] = {}
    for pair in pairs or []:
        name, sep, value = pair.partition(":")
        if not sep or not name.strip():
            raise ValueError(f'--header wants "Name: value", found {pair!r}')
        headers[name.strip()] = value.strip()
    return headers


def _http_post(payload: dict[str, object], url: str, timeout: float, headers: dict[str, str]) -> str:
    """The default transport: one POST, no retries, no redirects anywhere, no proxies.

    Two environment-driven escapes are closed here rather than assumed closed:

      * a redirect — refused by `_NoRedirect` above;
      * a proxy — `urllib` honours HTTP_PROXY/HTTPS_PROXY/ALL_PROXY, which would send a "loopback"
        request to whatever the environment names. `ProxyHandler({})` disables that: with a loopback
        endpoint there is nothing to proxy, and honouring the environment would quietly undo the
        loopback guarantee. Both are covered by tests.
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


Transport = Callable[[dict[str, object], str, float, dict[str, str]], str]
"""A transport takes (payload, url, timeout, headers) and returns the RAW HTTP BODY as text."""


def _http_post_lines(
    payload: dict[str, object], url: str, timeout: float, headers: dict[str, str]
):
    """The streaming twin of `_http_post`: same opener, same two closed escapes, line by line.

    Reading the response as a stream IS the point: `response.read()` would wait for the model to
    finish and turn this back into the blocking call it exists to avoid. The two escapes matter twice
    over here — once the body has started there is no status code left to check.
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        yield from response


StreamTransport = Callable[[dict[str, object], str, float, dict[str, str]], Iterator[bytes]]
"""A stream transport takes (payload, url, timeout, headers) and yields the RAW RESPONSE LINES."""


class Backend(Protocol):
    """What a detector backend must provide.

    `complete(prompt)` returns the ASSISTANT MESSAGE — the text the model produced — not the wire
    body: each backend unwraps its own envelope, so `parse_candidates` reads one thing regardless of
    whether the source was an OpenAI-compatible endpoint, a hand-written stub or a person.
    It must raise `BackendError` on any failure: returning an empty string on a timeout would be
    indistinguishable from "the model looked and found nothing".
    """

    name: str

    def complete(self, prompt: str) -> str: ...


class NullBackend:
    """No model configured. The scaffold's honest default: it suggests nothing and says so."""

    name = "null"

    def complete(self, prompt: str) -> str:  # noqa: ARG002 - the interface, not a use
        return json.dumps({"candidates": []})


class LoopbackBackend:
    """An OpenAI-compatible chat endpoint on this machine (Ollama, LM Studio, llama.cpp server)."""

    def __init__(
        self,
        url: str,
        model: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        constrained: bool = False,
        stream_transport: "StreamTransport | None" = None,
    ) -> None:
        self.url = check_loopback(url)
        self.model = model
        self.timeout = timeout
        self.transport = transport or _http_post
        self.stream_transport = stream_transport or _http_post_lines
        self.max_tokens = max_tokens
        self.constrained = constrained
        self.headers = dict(headers or {})
        # An explicit `--header Authorization:` wins over `--api-key`: the operator wrote it by hand.
        if api_key and not any(name.lower() == "authorization" for name in self.headers):
            self.headers["Authorization"] = f"Bearer {api_key}"
        # host:port only: a URL may carry userinfo (`http://user:secret@127.0.0.1:8000/`), and this
        # name ends up in `/api/state` and in every response. Credentials stay out of the report.
        parts = urllib.parse.urlsplit(self.url)
        self.name = f"{parts.hostname}:{parts.port or ''}/{model}"

    def _payload(self, prompt: str, stream: bool) -> dict[str, object]:
        """One payload builder for both transports: the streamed request must ask for exactly what
        the blocking one asks for, and two copies would drift apart one flag at a time."""
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            # A REASONING model spends this budget thinking and may return an empty `content`: too
            # small a value turns a good answer into "no JSON". See `_content_of` for the fallback.
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if self.constrained:
            # Supported by llama.cpp, vLLM and the OpenAI-compatible servers that implement the
            # `json_schema` response format. Off by default: an endpoint that rejects the field
            # would turn every call into a 400, and a local Ollama may not accept it.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "candidates", "schema": CANDIDATES_SCHEMA},
            }
        return payload

    def complete(self, prompt: str) -> str:
        try:
            raw = self.transport(self._payload(prompt, False), self.url, self.timeout, self.headers)
        except Exception as exc:  # noqa: BLE001 - every transport failure is the same failure here
            raise BackendError(f"the backend did not answer: {type(exc).__name__}: {exc}") from exc
        return _content_of(raw)

    def complete_stream(self, prompt: str) -> Iterator[str]:
        """The same answer, in pieces: server-sent events from an OpenAI-compatible endpoint.

        The pieces are a PROGRESS view, never the verdict — the caller joins them and runs the same
        `parse_candidates` and location check on the result. A stream that fails is a `BackendError`
        like any other failure: a partial answer quietly treated as the whole answer would look
        exactly like "the model found nothing".
        """
        try:
            for line in self.stream_transport(
                self._payload(prompt, True), self.url, self.timeout, self.headers
            ):
                piece = _delta_of(line)
                if piece:
                    yield piece
        except BackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - every transport failure is the same failure here
            raise BackendError(f"the backend did not answer: {type(exc).__name__}: {exc}") from exc


def _content_of(raw: str) -> str:
    """The assistant message out of an OpenAI-compatible response body."""
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise BackendError(f"the backend answered non-JSON: {raw[:200]!r}") from exc
    if not isinstance(body, dict):
        raise BackendError(f"the backend answered a {type(body).__name__}, not an object")
    error = body.get("error")
    if error:
        raise BackendError(f"the backend reported an error: {str(error)[:200]}")
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BackendError("the backend answered without choices[0].message") from exc
    if not isinstance(message, dict):
        raise BackendError("choices[0].message is not an object")
    content = message.get("content")
    if not isinstance(content, str):
        raise BackendError("the assistant content is not a string")
    if not content.strip():
        # A reasoning model (Qwen/MTPLX, and the same shape from others) can return an EMPTY
        # `content` with the answer in `reasoning_content`. Reporting "the answer holds no JSON"
        # for a model that did answer is a lie about the failure, so the thinking text is used as
        # a fallback — the engine still only reads JSON out of it.
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            content = reasoning
    return content


def _delta_of(line: bytes) -> str:
    """The assistant text carried by one SSE line, or "" for a line that carries none.

    OpenAI-compatible streams send `data: {"choices":[{"delta":{"content":"…"}}]}` and end with
    `data: [DONE]`; a reasoning model puts its thinking in `delta.reasoning_content`, the streamed
    twin of the fallback `_content_of` applies. Blank lines, `event:` lines and `:` comments are
    ignored. A `data:` line that is not JSON is an ERROR, not an empty piece: swallowing it would
    drop part of the answer and the truncation would only surface later as "the answer holds no
    JSON", which is a lie about what went wrong.
    """
    text = line.decode("utf-8", "replace").strip()
    if not text.startswith("data:"):
        return ""
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return ""
    try:
        body = json.loads(data)
    except ValueError as exc:
        raise BackendError(f"the stream carried a non-JSON chunk: {data[:120]!r}") from exc
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if error:
        raise BackendError(f"the backend reported an error: {str(error)[:200]}")
    try:
        delta = body["choices"][0]["delta"]
    except (KeyError, IndexError, TypeError):
        return ""
    if not isinstance(delta, dict):
        return ""
    for key in ("content", "reasoning_content"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_json(content: str) -> object:
    """The first JSON value in the answer: models like to wrap it in prose or a code fence."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    raise BackendError(f"the backend answer holds no JSON: {content[:200]!r}")


def parse_candidates(content: str, text: str, detected: Iterable[tuple[int, int, str]] = ()) -> list[Proposal]:
    """Turn the model's answer into proposals LOCATED in `text`.

    Two rules do the real work: a value that does not occur in the document is dropped (the engine
    locates, the model only names), and a value that is already a placeholder is dropped (a model
    that proposes `[EMAIL-1-9f2c1a]` is re-proposing the redaction we just did).
    """
    parsed = _extract_json(content)
    if isinstance(parsed, dict):
        raw_items = parsed.get("candidates", [])
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        raise BackendError(f"expected an object or a list, found {type(parsed).__name__}")
    if not isinstance(raw_items, list):
        raise BackendError('"candidates" is not a list')

    detected_spans = list(detected)
    proposals: dict[str, Proposal] = {}
    for item in raw_items:
        if isinstance(item, str):
            value, ptype, reason = item, "ALTRO", ""
        elif isinstance(item, dict):
            value = item.get("value")
            ptype = item.get("type") or "ALTRO"
            reason = item.get("reason") or ""
            if not isinstance(value, str) or not isinstance(ptype, str) or not isinstance(reason, str):
                continue
        else:
            continue
        value = value.strip()
        if len(value) < MIN_VALUE_LENGTH or anon.PLACEHOLDER_RE.search(value):
            continue
        if value in proposals:
            continue
        spans: list[tuple[int, int]] = []
        cursor = 0
        while len(spans) < MAX_OCCURRENCES:
            found = text.find(value, cursor)
            if found == -1:
                break
            spans.append((found, found + len(value)))
            cursor = found + len(value)
        if not spans:  # not in the document: a hallucination, and it costs nothing
            continue
        overlaps = any(
            start < hit_end and hit_start < end
            for start, end in spans
            for hit_start, hit_end, _type in detected_spans
        )
        proposals[value] = Proposal(
            value=value, type=ptype.upper(), reason=reason, spans=spans, overlaps_detected=overlaps
        )
    return sorted(proposals.values(), key=lambda proposal: proposal.spans[0][0])


def _frame(
    text: str, windows: list[tuple[int, int]], detected: list[tuple[int, int, str]], backend,
    chunk_overlap: int,
) -> dict[str, object]:
    """What the model was given and what the engine already found — the report's own header.

    One function so the blocking, streamed and chunked run cannot describe the same call
    differently. `analyzed_chars` is COVERAGE — how far into the document the model was taken — not
    the sum of the windows: with overlap the same characters are sent more than once, and summing
    them would report a document longer than the one that exists.
    """
    covered = windows[-1][1] if windows else 0
    return {
        "backend": backend.name,
        "chars": len(text),
        "analyzed_chars": covered,
        "truncated": covered < len(text),
        "chunks": len(windows),
        "chunk_overlap": chunk_overlap,
        "detected": [
            {"start": start, "end": end, "type": ptype} for start, end, ptype in detected
        ],
    }


def _merge(proposal_lists: list[list[Proposal]], limit: int) -> list[Proposal]:
    """One proposal per VALUE, across the windows.

    The same value seen in two overlapping windows is one candidate, not two: the spans are unioned
    so its `count` is the real number of occurrences, and `overlaps_detected` is the OR. Without
    this, the overlap that keeps a value whole at a cut would also duplicate every value near it.
    """
    merged: dict[str, Proposal] = {}
    for proposals in proposal_lists:
        for proposal in proposals:
            current = merged.get(proposal.value)
            if current is None:
                merged[proposal.value] = proposal
                continue
            current.spans = sorted({*current.spans, *proposal.spans})
            current.overlaps_detected = current.overlaps_detected or proposal.overlaps_detected
    return sorted(merged.values(), key=lambda proposal: proposal.spans[0][0])[:limit]


def validate_chunking(chunk_chars: int, chunk_overlap: int) -> None:
    """A window that does not ADVANCE is not a window: a bad pair is refused, never looped on."""
    if chunk_chars <= 0:
        return
    if chunk_overlap < 0:
        raise ValueError("--chunk-overlap cannot be negative")
    if chunk_overlap >= chunk_chars:
        raise ValueError(
            f"--chunk-overlap ({chunk_overlap}) must be smaller than --chunk-chars ({chunk_chars})"
        )


def _windows(
    text: str, *, max_chars: int, chunk_chars: int, chunk_overlap: int
) -> list[tuple[int, int]]:
    """The character ranges the model is asked about, in order.

    With `chunk_chars <= 0` there is ONE window — the first `max_chars` characters — and the tail is
    DECLARED truncated (the historical behaviour). With chunking on, consecutive windows OVERLAP by
    at least `chunk_overlap`: every position sits in at least one window, and any value no longer
    than the overlap is contained WHOLE in one of them, so a value across a cut is still seen and
    located. That guarantee is what the arithmetic has to protect: a window never STARTS after
    `previous_end - overlap` (that would eat the overlap and lose a straddling value), and moving the
    start BACK to a word boundary only adds overlap. The end is nudged forward to the next
    whitespace (within the overlap's reach) so a token is not cut in half — half a token is a
    prefix of a real value, and the engine would propose it as if it were the value.
    """
    length = len(text)
    if chunk_chars <= 0:
        return [(0, min(max_chars, length))]
    validate_chunking(chunk_chars, chunk_overlap)
    if length <= chunk_chars:
        return [(0, length)]
    spans: list[tuple[int, int]] = []
    start = 0
    while start < length:
        end = min(start + chunk_chars, length)
        reach = min(end + chunk_overlap, length)
        while end < reach and not text[end].isspace():
            end += 1
        spans.append((start, end))
        if end >= length:
            break
        nxt = end - chunk_overlap  # the EXACT overlap: never take more than this back off
        while nxt > start and not text[nxt - 1].isspace():
            nxt -= 1  # a word boundary, reached by moving BACK: it only ADDS overlap
        # The whole window was one unbroken token, so no boundary was reachable: keep the exact
        # overlap, which still advances (`chunk_overlap < chunk_chars`) and still covers.
        start = nxt if nxt > start else end - chunk_overlap
    return spans


def _report(
    *, text: str, detected: list[tuple[int, int, str]], windows: list[tuple[int, int]],
    proposals: list[Proposal], backend, chunk_overlap: int,
) -> dict[str, object]:
    """The one place the report is built: the blocking, streamed and chunked faces of one feature
    must produce exactly the same report for the same document.
    """
    return {
        **_frame(text, windows, detected, backend, chunk_overlap),
        "candidates": [proposal.to_json() for proposal in proposals],
    }


def suggest(
    text: str,
    backend: NullBackend | LoopbackBackend,
    *,
    entities: list[anon.Entity] | None = None,
    families: set[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    chunk_chars: int = 0,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    limit: int = 100,
) -> dict[str, object]:
    """What the model proposes for this text, plus what the ENGINE already found for comparison.

    The deterministic pass is included on purpose: a proposal that overlaps it is noise, and showing
    both is what makes the division of labour visible — the engine detects, the model suggests, the
    human approves. With `chunk_chars` the document is covered by overlapping windows and the
    answers are merged by value; without it only the first `max_chars` are sent, and the tail is
    declared in the report as `truncated`.
    """
    entities = entities or []
    # The same pattern families the operator selected: a deselected rule must not keep marking a
    # proposal as "already detected", or the report would describe a run that did not happen.
    detected = anon.detect(text, entities, families=families)
    windows = _windows(text, max_chars=max_chars, chunk_chars=chunk_chars, chunk_overlap=chunk_overlap)
    proposal_lists = [
        parse_candidates(backend.complete(text[start:end]), text, detected) for start, end in windows
    ]
    return _report(
        text=text, detected=detected, windows=windows,
        proposals=_merge(proposal_lists, limit), backend=backend,
        chunk_overlap=chunk_overlap if len(windows) > 1 else 0,
    )


def suggest_events(
    text: str,
    backend: NullBackend | LoopbackBackend,
    *,
    entities: list[anon.Entity] | None = None,
    families: set[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    chunk_chars: int = 0,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    limit: int = 100,
) -> Iterator[dict[str, object]]:
    """The same work as `suggest()`, reported while it happens: `start`, then `delta`*, then `done`.

    The deltas are PROGRESS, not a verdict: nothing is proposed until the joined answer has been
    parsed and every value located in the document, so a stream cannot turn half a value into a
    redaction. With chunking there is one answer per window and the deltas keep arriving across the
    whole document; the `done` report is the merged one, exactly what `suggest()` returns. A backend
    without a streaming transport still emits one delta per window — the client reads one event
    sequence either way.
    """
    entities = entities or []
    detected = anon.detect(text, entities, families=families)
    windows = _windows(text, max_chars=max_chars, chunk_chars=chunk_chars, chunk_overlap=chunk_overlap)
    overlap = chunk_overlap if len(windows) > 1 else 0
    yield {"event": "start", **_frame(text, windows, detected, backend, overlap)}
    stream = getattr(backend, "complete_stream", None)
    proposal_lists: list[list[Proposal]] = []
    for start, end in windows:
        window_text = text[start:end]
        if stream is None:
            answer = backend.complete(window_text)
            if answer:
                yield {"event": "delta", "text": answer}
        else:
            pieces: list[str] = []
            for piece in stream(window_text):
                pieces.append(piece)
                yield {"event": "delta", "text": piece}
            answer = "".join(pieces)
        proposal_lists.append(parse_candidates(answer, text, detected))
    yield {
        "event": "done",
        "report": _report(
            text=text, detected=detected, windows=windows,
            proposals=_merge(proposal_lists, limit), backend=backend, chunk_overlap=overlap,
        ),
    }


def build_backend(args: argparse.Namespace) -> NullBackend | LoopbackBackend:
    """No flags at all -> the null backend; a flag that is EMPTY is an error, not a silent no-op.

    `--url "$MODEL_URL"` with the variable unset used to degrade to "no backend configured" and
    exit 0, which the operator would read as "the model found nothing". An empty value is a mistake
    and says so.
    """
    if args.url is None and args.model is None:
        return NullBackend()
    url = (args.url or "").strip()
    model = (args.model or "").strip()
    if not url or not model:
        raise ValueError("--url and --model go together, and neither may be empty")
    return LoopbackBackend(
        url,
        model,
        timeout=args.timeout,
        api_key=(args.api_key or "").strip() or None,
        headers=parse_headers(args.header),
        max_tokens=args.max_tokens,
        constrained=getattr(args, "constrained", False),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="suggest.py",
        description="Ask a LOCAL model for candidates. Writes nothing: the engine remains the writer.",
    )
    parser.add_argument("file", help="text file to read (never modified)")
    parser.add_argument("--entities", action="append", metavar="PATH", help="dictionary (repeatable)")
    parser.add_argument("--catalogs", help="comma-separated catalog names from ~/.anon/catalogs/")
    parser.add_argument("--patterns", help="pattern groups to apply (same names as anon.py)")
    parser.add_argument(
        "--url",
        help="loopback endpoint, OpenAI-compatible: Ollama http://127.0.0.1:11434/v1/chat/completions, "
        "LM Studio :1234, llama.cpp :8080. A non-loopback host is refused.",
    )
    parser.add_argument("--model", help="model name, as the local server knows it")
    parser.add_argument("--api-key", help="sent as `Authorization: Bearer <key>` (a local server may want one)")
    parser.add_argument(
        "--header",
        action="append",
        metavar="'Name: value'",
        help="extra request header (repeatable), e.g. --header 'x-mtplx-client: pi'",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"generation budget; a reasoning model needs room to think (default {DEFAULT_MAX_TOKENS})",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"seconds (default {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                        help=f"how much of the document to send (default {DEFAULT_MAX_CHARS}); always declared")
    parser.add_argument(
        "--chunk-chars", type=int, default=0,
        help="cover a document longer than --max-chars with OVERLAPPING windows of this size instead "
             "of truncating it (0 = off: the historical single window); one model call per window, "
             "merged and deduplicated by value",
    )
    parser.add_argument(
        "--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP,
        help=f"characters of overlap between windows (default {DEFAULT_CHUNK_OVERLAP}); must be "
             "smaller than --chunk-chars, and longer than any value that may straddle a cut",
    )
    parser.add_argument("--limit", type=int, default=100, help="how many candidates to report (default 100)")
    parser.add_argument(
        "--constrained", action="store_true",
        help="pin the answer to the JSON schema (llama.cpp/vLLM): a backend that would narrate instead "
        "of answering cannot produce a non-schema reply. Off by default, for endpoints that reject it.",
    )
    parser.add_argument(
        "--stream", action="store_true",
        help="read the answer as server-sent events and show it on stderr while it arrives; the report "
        "is the same one, and nothing is proposed until the whole answer has been parsed and located",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        backend = build_backend(args)
    except ValueError as exc:
        print(f"suggest: {exc}", file=sys.stderr)
        return 2

    source = Path(args.file).expanduser()
    if not source.is_file():
        print(f"suggest: not a file: {source}", file=sys.stderr)
        return 2
    try:
        # The document as text: a container is a separate problem, and a model reading raw XML parts
        # is not this seam's job (the engine's container path is).
        text = source.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        print(f"suggest: cannot read {source.name} as text: {exc}", file=sys.stderr)
        return 2

    try:
        entities = anon.resolve_entities(args)
        families = anon.resolve_families(args)
        validate_chunking(args.chunk_chars, args.chunk_overlap)
    except (ValueError, OSError) as exc:
        print(f"suggest: {exc}", file=sys.stderr)
        return 2

    try:
        report: dict[str, object] | None = None
        if args.stream:
            # The delta text goes to STDERR: stdout stays the report, so `--json` keeps its shape and
            # a pipeline can read it while a human watches the model answer.
            for event in suggest_events(
                text, backend, entities=entities, families=families,
                max_chars=args.max_chars, chunk_chars=args.chunk_chars,
                chunk_overlap=args.chunk_overlap, limit=args.limit,
            ):
                if event["event"] == "delta":
                    print(event["text"], end="", file=sys.stderr, flush=True)
                elif event["event"] == "done":
                    report = event["report"]  # type: ignore[assignment]
            if not args.json:
                print(file=sys.stderr)
        else:
            report = suggest(
                text, backend, entities=entities, families=families,
                max_chars=args.max_chars, chunk_chars=args.chunk_chars,
                chunk_overlap=args.chunk_overlap, limit=args.limit,
            )
    except BackendError as exc:
        print(f"suggest: {exc}", file=sys.stderr)
        return 2  # a failed backend is an ERROR, never "nothing found"
    assert report is not None, "the done event is always emitted"

    if args.json:
        print(json.dumps({"schema": "anon/1", "mode": "suggest", "file": str(source), **report}, indent=2))
    else:
        chunks = int(report.get("chunks", 1) or 1)
        in_chunks = f" in {chunks} chunks" if chunks > 1 else ""
        print(f"suggest: backend {report['backend']}, {report['analyzed_chars']} of {report['chars']} chars{in_chunks}"
              + (" (TRUNCATED — the tail was not sent)" if report["truncated"] else ""))
        print(f"suggest: the engine already finds {len(report['detected'])} span(s) here")
        proposals = report["candidates"]
        if not proposals:
            print("suggest: nothing proposed. This is a review list, not a verdict.")
            return 0
        for proposal in proposals:
            overlap = " (already detected)" if proposal["overlaps_detected"] else ""
            print(f"  [{proposal['type']}] {proposal['value']!r} x{proposal['count']}{overlap}"
                  f"{' — ' + proposal['reason'] if proposal['reason'] else ''}")
        print("suggest: PROPOSALS — approve them by adding them to the dictionary, then run anon.py")
    return 4 if report["candidates"] else 0


if __name__ == "__main__":
    sys.exit(main())
