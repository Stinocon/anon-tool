#!/usr/bin/env python3
"""Measure the suggestion seam against a loopback model: latency, tokens, and whether it answers.

    python3 scripts/bench-suggest.py --url http://127.0.0.1:8080/v1/chat/completions \
        --model qwen2.5-3b-instruct --repeat 3

The interesting number is not tokens/s but the WALL TIME of one usable answer: the seam is asked for
candidates a human then approves, so a model that needs two minutes (or that thinks until its budget
ends and returns nothing) is unusable however fast it is per token. Measured with a 9B reasoning
model: 138 s, 4096 tokens, empty answer — the seam reported it as an error, correctly.

Reads the same configuration as the server (--url/--model/--key/--header) and refuses a non-loopback
endpoint exactly like the seam does, so a benchmark can never be pointed somewhere else by accident.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import anon  # noqa: E402
import suggest  # noqa: E402

SAMPLE = """Verbale di sopralluogo — 12 marzo, sede di Ancona.

Presenti: il responsabile di stabilimento e il referente del cliente di Ancona. L'impianto di
rifiuti di via Roma 14 risulta non conforme al piano di manutenzione: la ditta che ha eseguito
l'intervento dichiara una taratura eseguita a gennaio, ma il registro non riporta la firma.
Riferimento: commessa C-2024-118 del cliente, contatto mario.rossi@example.com, tel. +39 02 1234567.
Server di raccolta dati: 10.20.30.40, host srvcrm.acme.local. IBAN IT60X0542811101000000123456.

Il consulente incaricato della verifica e' la societa' di Milano che segue anche le sedi di Prato
e di Bologna. Il legale rappresentante non era presente.
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bench-suggest.py", description="measure the suggest seam")
    parser.add_argument("--url", required=True, help="loopback chat-completions endpoint")
    parser.add_argument("--model", required=True, help="model name the endpoint answers to")
    parser.add_argument("--key", default="", help="api key, if the endpoint wants one")
    parser.add_argument("--header", action="append", default=[], help="extra header, repeatable")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--repeat", type=int, default=3, help="timed runs (default: 3)")
    parser.add_argument("--text", help="file to use instead of the built-in Italian sample")
    parser.add_argument(
        "--corpus", action="store_true",
        help="measure PROPOSAL QUALITY against the labelled corpus (tests/corpus.py): precision, "
        "recall and latency per document, instead of one sample repeated",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _load_module(name: str, path: pathlib.Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _doc_entities(doc: dict, tmp: pathlib.Path):
    """The document's OWN dictionary + repo catalogs, so the measurement is hermetic (never
    `~/.anon`), the same way `recall-sweep.py` does it."""
    path = tmp / "entities.txt"
    path.write_text(doc.get("entities", ""), encoding="utf-8")
    entities = anon.load_entities(path)
    for name in doc.get("catalogs", []):
        entities += anon.load_entities_many([pathlib.Path(__file__).resolve().parent.parent / "catalogs" / f"{name}.txt"])
    return entities


def _covers(proposal: str, value: str) -> bool:
    """The declared value is COVERED when a proposal contains it WHOLE (equal or superset):
    redacting that proposal is what removes it. A proposal that is a mere FRAGMENT of the value does
    NOT cover it — `Mario` does not remove `Mario Rossi`."""
    return proposal == value or value in proposal


def _named(proposal: str, value: str) -> bool:
    """A proposal NAMES part of a declared value on a word boundary: `Rossi` for `Mario Rossi` is a
    real fragment, not a hallucination, while `ario` is not. Used for precision and for the declared
    holes, where naming the sensitive token IS the useful signal."""
    if _covers(proposal, value):
        return True
    return re.search(rf"(?<!\w){re.escape(proposal)}(?!\w)", value) is not None


def run_corpus(backend, documents, tmp: pathlib.Path) -> dict:
    """Proposal precision/recall against the declared truth. A proposal counts as correct when it
    matches a `must_find` value on either side of the containment; a must_find value counts as
    found when some proposal covers it. Proposals on a document with no truth are all false
    positives — which is exactly what `prose-traps` is there to catch."""
    per_doc: list[dict[str, object]] = []
    declared = covered = proposed = correct = redundant = 0
    known_declared = known_closed = 0
    for doc in documents:
        text = doc["text"]
        must = [value for value, _type in doc.get("must_find", [])]
        known = [value for value, _why in doc.get("known_miss", [])]
        started = time.monotonic()
        try:
            report = suggest.suggest(text, backend, entities=_doc_entities(doc, tmp),
                                     families=None if doc.get("patterns") is None else set(doc["patterns"]))
        except suggest.BackendError as error:
            per_doc.append({"name": doc["name"], "chars": len(text),
                            "seconds": round(time.monotonic() - started, 2), "error": str(error)[:200]})
            declared += len(must)
            known_declared += len(known)
            continue
        elapsed = time.monotonic() - started
        proposals = report["candidates"]
        proposed += len(proposals)
        correct += sum(1 for p in proposals if any(_named(p["value"], value) for value in must))
        redundant += sum(1 for p in proposals if p.get("overlaps_detected"))
        doc_covered = sum(1 for value in must if any(_covers(p["value"], value) for p in proposals))
        doc_closed = sum(1 for value in known if any(_named(p["value"], value) for p in proposals))
        covered += doc_covered
        declared += len(must)
        known_closed += doc_closed
        known_declared += len(known)
        per_doc.append({
            "name": doc["name"], "chars": len(text), "seconds": round(elapsed, 2),
            "proposals": len(proposals), "must_find": len(must), "proposed": doc_covered,
            "known_miss": len(known), "closed": doc_closed,
        })
    return {
        "backend": backend.name,
        "documents": len(per_doc),
        "proposals": proposed,
        "correct": correct,
        "precision": round(correct / proposed, 3) if proposed else None,
        "must_find": declared,
        "covered": covered,
        "recall": round(covered / declared, 3) if declared else None,
        "already_found_by_engine": redundant,
        # The number that decides whether the model EARNS ITS PLACE: values the engine cannot find
        # (contextual references, names absent from the dictionary) that the model proposed anyway.
        "known_miss": known_declared,
        "known_miss_closed": known_closed,
        "per_document": per_doc,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    endpoint = suggest.check_loopback(args.url)  # refuses anything but loopback
    backend = suggest.LoopbackBackend(
        endpoint, args.model, api_key=args.key or None, headers=suggest.parse_headers(args.header),
        max_tokens=args.max_tokens, timeout=args.timeout,
    )
    if args.corpus:
        import tempfile
        corpus = _load_module("anon_corpus", pathlib.Path(__file__).resolve().parent.parent / "tests" / "corpus.py")
        with tempfile.TemporaryDirectory(prefix="anon-quality-") as tmpdir:
            summary = run_corpus(backend, corpus.DOCUMENTS, pathlib.Path(tmpdir))
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(f"backend   : {summary['backend']}")
            print(f"precision : {summary['correct']}/{summary['proposals']} = {summary['precision']}")
            print(f"recall    : {summary['covered']}/{summary['must_find']} = {summary['recall']}")
            print(f"redundant : {summary['already_found_by_engine']} proposal(s) the engine already found")
            print(f"known miss: {summary['known_miss_closed']}/{summary['known_miss']} closed by the model")
            for row in summary["per_document"]:
                if "error" in row:
                    print(f"  {row['name']:<20} ERROR {row['error']}")
                else:
                    print(f"  {row['name']:<20} {row['chars']:>6} chars  {row['seconds']:>6.2f}s  "
                          f"{row['proposed']}/{row['must_find']} found  "
                          f"{row['closed']}/{row['known_miss']} holes closed  {row['proposals']} proposals")
        return 0 if summary["recall"] is not None else 2
    text = pathlib.Path(args.text).read_text(encoding="utf-8") if args.text else SAMPLE
    entities = anon.load_entities_many([
        pathlib.Path.home() / ".anon" / name
        for name in ("entities.txt", "people.txt", "clients.txt")
    ])

    runs: list[dict[str, object]] = []
    for index in range(max(1, args.repeat)):
        started = time.monotonic()
        try:
            report = suggest.suggest(text, backend, entities=entities)
            elapsed = time.monotonic() - started
            candidates = report["candidates"]
            # An EMPTY answer is not a usable answer: a model that returns valid JSON with no
            # candidates looks like success to the seam (the format was obeyed) but is exactly the
            # failure this benchmark exists to expose — the 9B returned nothing usable, and so does
            # a mute model. Counted separately, and it does not count as answered.
            runs.append({
                "ok": bool(candidates),
                "empty": not candidates,
                "seconds": round(elapsed, 2),
                "candidates": len(candidates),
            })
        except suggest.BackendError as error:
            elapsed = time.monotonic() - started
            runs.append({"ok": False, "seconds": round(elapsed, 2), "error": str(error)[:200]})
        if not args.json:
            run = runs[-1]
            if run["ok"]:
                state = f"{run['candidates']} candidate(s)"
            elif run.get("empty"):
                state = "EMPTY ANSWER (valid JSON, no candidates)"
            else:
                state = f"ERROR {run['error']}"
            print(f"  run {index + 1}/{args.repeat}: {run['seconds']:>7.2f}s  {state}")

    ok = [run for run in runs if run["ok"]]
    summary = {
        "backend": backend.name,
        "text_chars": len(text),
        "max_tokens": args.max_tokens,
        "runs": runs,
        "median_seconds": round(statistics.median([run["seconds"] for run in runs]), 2) if runs else None,
        "answered": f"{len(ok)}/{len(runs)}",
        "empty_answers": sum(1 for run in runs if run.get("empty")),
        "median_candidates": round(statistics.median([run["candidates"] for run in ok]), 1) if ok else None,
    }
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print()
        print(f"backend      : {summary['backend']}")
        print(f"text         : {summary['text_chars']} characters")
        print(f"answered     : {summary['answered']}")
        print(f"median time  : {summary['median_seconds']}s for one usable answer")
        print(f"median props : {summary['median_candidates']}")
    return 0 if ok and len(ok) == len(runs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
