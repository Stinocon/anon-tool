#!/usr/bin/env python3
"""fp-sweep.py — measure the engine's false positives over a real, known-clean corpus.

Item #23 of docs/OPEN-ISSUES.md: the thresholds of the word-ish rules (KEY, HOST, URL, …) were
chosen by reasoning, not by measurement. This tool gives the number. It scans a corpus of files
that SHOULD be clean (this project's source and docs, the Pi extensions and the installed agent
package), runs the real engine's `detect()` over them, and aggregates every match
by rule type. On a clean corpus, each match is a candidate false positive.

    python3 scripts/fp-sweep.py                       # the default corpus (use --root off this machine)
    python3 scripts/fp-sweep.py --root DIR --json     # a specific corpus, machine-readable
    python3 scripts/fp-sweep.py --show KEY --show HOST  # print the matching lines (code, not secrets)
    python3 scripts/fp-sweep.py --entities catalogs/vendors.txt --show FORNITORE  # size up a list

Corpus hygiene: the default corpus deliberately EXCLUDES private configs (a router config, a
home-automation config, `.env`) — those hold legitimate real values, so a match there is a true
positive, not a false one. The catalog files are excluded only while measuring a dictionary with
`--entities`: they ARE lists of the very words a catalog matches, so a hit there says nothing — but
their own pattern matches are signal, and the default sweep keeps them in.
Pass `--root` to measure a different corpus and read the number accordingly.

Deterministic: a counter over regex/dictionary matches, never an LLM judgement. It reads the
engine that runs (`~/.anon/anon.py`, printed in the report), so the number describes the product.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ANON_HOME = Path(os.environ.get("ANON_HOME", Path.home() / ".anon")).expanduser()
sys.path.insert(0, str(ANON_HOME))
import anon  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PI_INSTALL = Path("/opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent")

# A corpus of files that must NOT be redacted: this project's authored code and docs, the Pi
# extensions, and the installed Pi package. Private configs (which hold real IPs/passwords) are NOT
# here on purpose — a match on real data is a true positive, not a false one.
DEFAULT_ROOTS = [
    REPO,
    Path.home() / ".pi" / "agent" / "extensions",
    PI_INSTALL / "dist",
    PI_INSTALL / "docs",
]
# tests/ are deliberate fixtures (they are SUPPOSED to match), so they would drown the signal.
DEFAULT_EXCLUDES = ["*/tests/*", "*/__pycache__/*", "*/node_modules/*", "*/.git/*", "*.min.js"]
# Excluded only while measuring a dictionary (below): the catalog files ARE lists of the words a
# catalog is meant to match, so a hit there says nothing — but their own pattern matches (a host in
# a comment, a phone number in the prose) are exactly what the default sweep exists to report.
CATALOG_EXCLUDE = "*/catalogs/*"

SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".yaml", ".yml", ".json",
    ".sh", ".bash", ".md", ".txt", ".toml", ".ini", ".conf", ".html", ".css",
    ".rsc", ".env", ".xml", ".properties", ".cfg",
}


def iter_files(roots: list[Path], excludes: list[str], max_bytes: int):
    from fnmatch import fnmatch

    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUFFIXES:
                continue
            if any(fnmatch(str(path), pattern) for pattern in excludes):
                continue
            try:
                if path.stat().st_size > max_bytes:
                    continue
            except OSError:
                continue
            yield path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", action="append", help="corpus root (repeatable; default: the built-in corpus)")
    parser.add_argument("--exclude", action="append", help="fnmatch pattern to skip (repeatable)")
    parser.add_argument("--max-bytes", type=int, default=1024 * 1024, help="skip files larger than this")
    parser.add_argument(
        "--entities",
        action="append",
        help=(
            "dictionary or catalog to scan WITH (repeatable; default: the operator's "
            "~/.anon/entities.txt). The catalogs themselves are left out of the corpus in this "
            "mode: a list of the words you are measuring is not evidence about them."
        ),
    )
    parser.add_argument("--show", action="append", default=[], metavar="TYPE", help="print matching lines for this rule type")
    parser.add_argument("--show-limit", type=int, default=40, help="how many lines --show prints per type")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    args = parser.parse_args()

    roots = [Path(r).expanduser() for r in (args.root or DEFAULT_ROOTS)]
    excludes = list(DEFAULT_EXCLUDES) + list(args.exclude or [])
    if args.entities:
        excludes.append(CATALOG_EXCLUDE)
        missing = [path for path in args.entities if not Path(path).expanduser().is_file()]
        if missing:
            # A typo used to be read as "zero matches": the report said the list was clean when the
            # list had never been loaded. Refuse instead.
            print(f"fp-sweep: no such dictionary: {', '.join(missing)}", file=sys.stderr)
            return 2
        entities = anon.load_entities_many(Path(path).expanduser() for path in args.entities)
    else:
        entities = anon.load_entities(anon.DEFAULT_ENTITIES)
    # Dictionary types are SUPPOSED to match real values; only the pattern rules can be "wrong" on
    # a clean corpus. `detect()` reports no provenance, so a dictionary entry typed like a pattern
    # (`HOST|db.intranet`) is indistinguishable from a HOST match: keep the type classified as a
    # pattern unless it is NOT a built-in rule type, so a pattern false positive is never hidden.
    pattern_types = {rule.type for rule in (*anon.RULES, *anon.HEURISTIC_RULES)}
    dictionary_types = {entity.type for entity in entities} - pattern_types

    per_type: Counter = Counter()
    files_per_type: dict[str, set] = defaultdict(set)
    samples: dict[str, list] = defaultdict(list)
    scanned = skipped = 0

    for path in iter_files(roots, excludes, args.max_bytes):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            skipped += 1
            continue
        scanned += 1
        found = anon.detect(text, entities)
        if not found:
            continue
        lines = text.splitlines()
        for start, end, ptype in found:
            per_type[ptype] += 1
            files_per_type[ptype].add(str(path))
            if ptype in args.show and len(samples[ptype]) < args.show_limit:
                line_no = text.count("\n", 0, start) + 1
                samples[ptype].append(f"{path}:{line_no}: {lines[line_no - 1].strip()[:160]}")

    report = {
        "engine": str(Path(anon.__file__).resolve()),
        "engine_version": anon.VERSION,
        "corpus_roots": [str(r) for r in roots],
        "files_scanned": scanned,
        "files_skipped": skipped,
        "false_positives_total": sum(
            count for ptype, count in per_type.items() if ptype not in dictionary_types
        ),
        "dictionary_matches_total": sum(
            count for ptype, count in per_type.items() if ptype in dictionary_types
        ),
        "by_type": {
            ptype: {
                "matches": count,
                "files": len(files_per_type[ptype]),
                "kind": "dictionary" if ptype in dictionary_types else "pattern",
            }
            for ptype, count in per_type.most_common()
        },
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"engine: {Path(anon.__file__).resolve()} v{anon.VERSION}")
        print(f"corpus: {scanned} files scanned, {skipped} skipped")
        print(f"pattern false positives on a clean corpus: {report['false_positives_total']}")
        for ptype, count in per_type.most_common():
            kind = "dict" if ptype in dictionary_types else "FP  "
            print(f"  [{kind}] {ptype:<14} {count:>5} matches in {len(files_per_type[ptype])} file(s)")
        if report["dictionary_matches_total"]:
            print(
                f"  (dictionary matches, expected — real values present: "
                f"{report['dictionary_matches_total']})"
            )
        for ptype in args.show:
            if not samples[ptype]:
                continue
            print(f"\n--- {ptype} (first {len(samples[ptype])}) ---")
            for line in samples[ptype]:
                print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
