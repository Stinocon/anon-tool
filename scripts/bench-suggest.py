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
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    endpoint = suggest.check_loopback(args.url)  # refuses anything but loopback
    text = pathlib.Path(args.text).read_text(encoding="utf-8") if args.text else SAMPLE
    backend = suggest.LoopbackBackend(
        endpoint, args.model, api_key=args.key or None, headers=suggest.parse_headers(args.header),
        max_tokens=args.max_tokens, timeout=args.timeout,
    )
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
            runs.append({"ok": True, "seconds": round(elapsed, 2), "candidates": len(candidates)})
        except suggest.BackendError as error:
            elapsed = time.monotonic() - started
            runs.append({"ok": False, "seconds": round(elapsed, 2), "error": str(error)[:200]})
        if not args.json:
            run = runs[-1]
            state = f"{run['candidates']} candidate(s)" if run["ok"] else f"ERROR {run['error']}"
            print(f"  run {index + 1}/{args.repeat}: {run['seconds']:>7.2f}s  {state}")

    ok = [run for run in runs if run["ok"]]
    summary = {
        "backend": backend.name,
        "text_chars": len(text),
        "max_tokens": args.max_tokens,
        "runs": runs,
        "median_seconds": round(statistics.median([run["seconds"] for run in runs]), 2) if runs else None,
        "answered": f"{len(ok)}/{len(runs)}",
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
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
