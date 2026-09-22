#!/usr/bin/env python3
"""bench-check.py — measure `anon.py --check` throughput, the number the guard's cap rests on.

The guard blocks a file it cannot scan inside its timeout, so `MAX_CHECK_BYTES` in
`~/.pi/agent/extensions/anon-guard.ts` must be derived from a measurement, not from an estimate.
This script produces that measurement, deterministically and offline.

  python3 scripts/bench-check.py                       # live engine (~/.anon/anon.py), 5 MB
  python3 scripts/bench-check.py --engine ./anon.py    # the copy in this repository
  python3 scripts/bench-check.py --mb 10 --entities 400

It builds a synthetic corpus (a fixed word list, so every run is the same bytes) and a synthetic
dictionary of N entries — never the operator's own `entities.txt`, which must not enter a
repository. Only the timings and the entry count are printed.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORDS = (
    "il cliente ha chiesto una verifica della configurazione di rete e dei servizi esposti "
    "prima della consegna del rapporto finale al referente tecnico, con particolare attenzione "
    "alle regole di filtraggio e alla separazione delle VLAN interne dell'ufficio principale. "
    "In sintesi l'attivita procede secondo il piano concordato senza scostamenti rilevanti e "
    "senza interruzioni del servizio durante le finestre di manutenzione programmata."
).split()

NAMES = (
    "Ferraris", "Bonatti", "Trevisan", "Lombardi", "Sanna", "Peruzzi", "Marchetti", "Greco",
    "Costa", "Rinaldi", "Villa", "Fabbri", "Serra", "Monti", "Barone", "Lodi", "Neri",
    "Orlandi", "Bellini", "Rossetti", "Gatti", "Mancuso", "Ferretti", "Palumbo", "Draghi",
    "Sartori", "Bruno", "Caputo", "Vitali", "Ferrero", "Russo", "Damiani", "Pellegrini",
)
TYPES = ("AZIENDA", "CLIENTE", "REFERENTE", "SEDE", "PROGETTO", "DOMINIO")


def synthetic_dictionary(entries: int) -> str:
    """A realistic-shaped dictionary: multi-word names, some with a legal form.

    The first token is a distinctive NAME, not a word from the prose above: that is what a real
    `entities.txt` looks like, and it is what makes the fast scan able to skip an entry.
    """
    lines = ["# synthetic benchmark dictionary — generated, no real data"]
    for index in range(entries):
        ptype = TYPES[index % len(TYPES)]
        first = NAMES[(index * 3) % len(NAMES)]
        second = NAMES[(index * 11 + 5) % len(NAMES)]
        suffix = " Srl" if index % 4 == 0 else ""
        lines.append(f"{ptype}|{first} {second} {index}{suffix}")
    return "\n".join(lines) + "\n"


def synthetic_text(megabytes: float, dictionary_lines: list[str], seed: int = 20260101) -> str:
    random.seed(seed)
    target = int(megabytes * 1024 * 1024)
    out: list[str] = []
    size = 0
    while size < target:
        chunk = " ".join(random.choice(WORDS) for _ in range(120))
        # The dictionary is HIT regularly: a corpus with no matches would measure only the regex
        # scans, which is not what a real document does. `--hits` decides how many DISTINCT
        # entries occur — the worst case for the fast scan is all of them.
        out.append(chunk + " " + random.choice(dictionary_lines) + ".\n")
        size += len(out[-1])
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench-check.py", description="measure anon.py --check throughput")
    parser.add_argument("--engine", default=str(Path.home() / ".anon" / "anon.py"))
    parser.add_argument("--mb", type=float, default=5.0, help="corpus size in MB (default: 5)")
    parser.add_argument("--entities", type=int, default=200, help="dictionary entries (default: 200)")
    parser.add_argument(
        "--hits",
        type=int,
        default=10,
        help="how many DISTINCT entries occur in the corpus: 10 is a document that mentions a few "
        "entities, `--entities` is the worst case where every entry occurs (default: 10)",
    )
    parser.add_argument("--repeat", type=int, default=3, help="timed runs per size (default: 3)")
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    engine = Path(args.engine).expanduser()
    if not engine.is_file():
        print(f"bench: engine not found: {engine}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="anon-bench-") as work:
        workdir = Path(work)
        dictionary = workdir / "entities.txt"
        dictionary_text = synthetic_dictionary(args.entities)
        dictionary.write_text(dictionary_text, encoding="utf-8")
        all_lines = [line.split("|", 1)[1] for line in dictionary_text.splitlines() if "|" in line]
        hits = max(1, min(args.hits, len(all_lines)))
        # Every entry of the corpus is one of the first `hits`: deterministic and reproducible.
        dictionary_lines = all_lines[:hits]

        sizes = [round(args.mb * factor, 2) for factor in (0.25, 1.0)] if args.mb >= 1 else [args.mb]
        if args.mb >= 8:
            sizes = [2.0, args.mb]
        rows = []
        for size in sizes:
            corpus = workdir / f"corpus-{size}.md"
            corpus.write_text(synthetic_text(size, dictionary_lines), encoding="utf-8")
            measured = round(corpus.stat().st_size / 1024 / 1024, 2)
            times = []
            for _ in range(args.repeat):
                started = time.monotonic()
                result = subprocess.run(
                    [sys.executable, str(engine), str(corpus), "--check", "--json", "--entities", str(dictionary)],
                    capture_output=True, text=True,
                )
                times.append(time.monotonic() - started)
                if result.returncode not in (0, 1) or not result.stdout.startswith("{"):
                    print(f"bench: engine failed on {corpus.name}: {result.stderr.strip()[:300]}", file=sys.stderr)
                    return 2
            best = min(times)
            found = json.loads(result.stdout).get("total", 0)
            rows.append(
                {
                    "mb": measured,
                    "best_seconds": round(best, 2),
                    "median_seconds": round(sorted(times)[len(times) // 2], 2),
                    "mb_per_second": round(measured / best, 2) if best else 0.0,
                    "spans_found": found,
                }
            )

    if args.json:
        print(json.dumps({"engine": str(engine), "entries": args.entities, "hits": args.hits, "runs": rows}, ensure_ascii=False))
        return 0

    print(f"engine : {engine}")
    print(f"dictionary: {args.entities} synthetic entries, {args.hits} of them occurring in the corpus")
    print(f"{'MB':>6} {'best s':>8} {'median s':>9} {'MB/s':>7} {'spans':>7}")
    for row in rows:
        print(
            f"{row['mb']:>6} {row['best_seconds']:>8} {row['median_seconds']:>9} "
            f"{row['mb_per_second']:>7} {row['spans_found']:>7}"
        )
    print("\nDerive a cap from the slowest row: cap_bytes = mb_per_second * budget_seconds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
