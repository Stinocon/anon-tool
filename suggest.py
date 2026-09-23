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
from typing import Callable, Iterable, Protocol

import anon

VERSION = anon.VERSION
DEFAULT_TIMEOUT = 60.0
# What a local model is asked to SEND is bounded separately from what it may read: the engine's caps
# (160 MB upload, 12 MB scan) are sized for a scanner, not for a 32k-token context window.
DEFAULT_MAX_CHARS = 20_000
MIN_VALUE_LENGTH = 3
MAX_OCCURRENCES = 20


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


def _http_post(payload: dict[str, object], url: str, timeout: float) -> str:
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
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


Transport = Callable[[dict[str, object], str, float], str]
"""A transport takes (payload, url, timeout) and returns the RAW HTTP BODY as text."""


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
    ) -> None:
        self.url = check_loopback(url)
        self.model = model
        self.timeout = timeout
        self.transport = transport or _http_post
        self.name = f"{urllib.parse.urlsplit(self.url).netloc}/{model}"

    def complete(self, prompt: str) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "stream": False,
        }
        try:
            raw = self.transport(payload, self.url, self.timeout)
        except Exception as exc:  # noqa: BLE001 - every transport failure is the same failure here
            raise BackendError(f"the backend did not answer: {type(exc).__name__}: {exc}") from exc
        return _content_of(raw)


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
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BackendError("the backend answered without choices[0].message.content") from exc
    if not isinstance(content, str):
        raise BackendError("the assistant content is not a string")
    return content


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


def suggest(
    text: str,
    backend: NullBackend | LoopbackBackend,
    *,
    entities: list[anon.Entity] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    limit: int = 100,
) -> dict[str, object]:
    """What the model proposes for this text, plus what the ENGINE already found for comparison.

    The deterministic pass is included on purpose: a proposal that overlaps it is noise, and showing
    both is what makes the division of labour visible — the engine detects, the model suggests, the
    human approves.
    """
    entities = entities or []
    detected = anon.detect(text, entities)
    analyzed = text[:max_chars]
    answer = backend.complete(analyzed)
    proposals = parse_candidates(answer, text, detected)[:limit]
    return {
        "backend": backend.name,
        "chars": len(text),
        "analyzed_chars": len(analyzed),
        "truncated": len(analyzed) < len(text),
        "detected": [
            {"start": start, "end": end, "type": ptype}
            for start, end, ptype in detected
        ],
        "candidates": [proposal.to_json() for proposal in proposals],
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
    return LoopbackBackend(url, model, timeout=args.timeout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="suggest.py",
        description="Ask a LOCAL model for candidates. Writes nothing: the engine remains the writer.",
    )
    parser.add_argument("file", help="text file to read (never modified)")
    parser.add_argument("--entities", action="append", metavar="PATH", help="dictionary (repeatable)")
    parser.add_argument("--catalogs", help="comma-separated catalog names from ~/.anon/catalogs/")
    parser.add_argument(
        "--url",
        help="loopback endpoint, OpenAI-compatible: Ollama http://127.0.0.1:11434/v1/chat/completions, "
        "LM Studio :1234, llama.cpp :8080. A non-loopback host is refused.",
    )
    parser.add_argument("--model", help="model name, as the local server knows it")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"seconds (default {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                        help=f"how much of the document to send (default {DEFAULT_MAX_CHARS}); always declared")
    parser.add_argument("--limit", type=int, default=100, help="how many candidates to report (default 100)")
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
    except (ValueError, OSError) as exc:
        print(f"suggest: {exc}", file=sys.stderr)
        return 2

    try:
        report = suggest(text, backend, entities=entities, max_chars=args.max_chars, limit=args.limit)
    except BackendError as exc:
        print(f"suggest: {exc}", file=sys.stderr)
        return 2  # a failed backend is an ERROR, never "nothing found"

    if args.json:
        print(json.dumps({"schema": "anon/1", "mode": "suggest", "file": str(source), **report}, indent=2))
    else:
        print(f"suggest: backend {report['backend']}, {report['analyzed_chars']} of {report['chars']} chars"
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
