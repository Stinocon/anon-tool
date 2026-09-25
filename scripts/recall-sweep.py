#!/usr/bin/env python3
"""recall-sweep.py — measure the engine's false NEGATIVES on a labelled corpus.

`fp-sweep.py` measures the direction everybody measures: how much a known-clean corpus gets
redacted by mistake. This tool measures the direction that decides whether an anonymizer is safe:
of the values a document DECLARES sensitive (`tests/corpus.py`), how many does `detect()` actually
cover? A value with no covered occurrence is a LEAK.

    python3 scripts/recall-sweep.py              # the human report
    python3 scripts/recall-sweep.py --json       # the same numbers, machine-readable

Exit 1 if any declared value is left uncovered or any `must_not` string is redacted, 0 otherwise —
so it doubles as a gate. The corpus is synthetic and self-contained: it declares its own dictionary
and reads the catalogs from THIS repository (`catalogs/`), never from `~/.anon`, so the number is
the same on any machine and no private data is involved.

Deterministic: regexes and dictionaries, no model, no network.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("anon_engine", ROOT / "anon.py")
assert _spec and _spec.loader
anon = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = anon
_spec.loader.exec_module(anon)

_cspec = importlib.util.spec_from_file_location("anon_corpus", ROOT / "tests" / "corpus.py")
assert _cspec and _cspec.loader
corpus = importlib.util.module_from_spec(_cspec)
sys.modules[_cspec.name] = corpus
_cspec.loader.exec_module(corpus)


def _entities(text: str, tmp: Path):
    """Load a document's declared dictionary through the REAL parser."""
    path = tmp / "entities.txt"
    path.write_text(text, encoding="utf-8")
    return anon.load_entities(path)


def _catalogs(names: list[str]):
    paths = [ROOT / "catalogs" / f"{name}.txt" for name in names]
    return anon.load_entities_many(paths)


def _occurrences(text: str, value: str) -> list[int]:
    return [m.start() for m in re.finditer(re.escape(value), text)]


def _covering(found, text: str, value: str) -> list[tuple[int, int, str]]:
    """Detected spans that FULLY contain at least one occurrence of `value`."""
    out = []
    for start, end, ptype in found:
        span = text[start:end]
        if value in span:
            out.append((start, end, ptype))
    return out


def evaluate(documents) -> dict:
    """Run every document and return {per_type, leaks, violations, ...}."""
    per_type: dict[str, dict[str, int]] = {}
    leaks: list[dict] = []
    type_mismatch: list[dict] = []
    violations: list[dict] = []
    undeclared: list[dict] = []
    declared_fp: list[dict] = []
    known_miss: list[dict] = []
    total_declared = 0

    with tempfile.TemporaryDirectory(prefix="anon-recall-") as tmpdir:
        tmp = Path(tmpdir)
        for doc in documents:
            text = doc["text"]
            entities = _entities(doc.get("entities", ""), tmp) + _catalogs(doc.get("catalogs", []))
            families = None if doc.get("patterns") is None else set(doc["patterns"])
            found = anon.detect(text, entities, families=families)

            expected_spans: list[tuple[int, int]] = []
            for value, want in doc.get("must_find", []):
                total_declared += 1
                stats = per_type.setdefault(want, {"declared": 0, "covered": 0, "typed": 0})
                stats["declared"] += 1
                cover = _covering(found, text, value)
                if cover:
                    stats["covered"] += 1
                    if any(ptype == want for _s, _e, ptype in cover):
                        stats["typed"] += 1
                    else:
                        type_mismatch.append({
                            "document": doc["name"], "value": value,
                            "want": want, "got": cover[0][2],
                        })
                    for m in _occurrences(text, value):
                        if any(s <= m and m + len(value) <= e for s, e, _t in found):
                            expected_spans.append((m, m + len(value)))
                else:
                    leaks.append({"document": doc["name"], "value": value, "type": want})

            for value in doc.get("must_not", []):
                cover = _covering(found, text, value)
                if cover:
                    violations.append({"document": doc["name"], "value": value,
                                       "covered_by": [{"type": t, "text": text[s:e]}
                                                      for s, e, t in cover]})

            for value, ptype, why in doc.get("declared_fp", []):
                declared_fp.append({"document": doc["name"], "value": value,
                                    "type": ptype, "why": why})

            for value, why in doc.get("known_miss", []):
                known_miss.append({"document": doc["name"], "value": value, "why": why})

            for start, end, ptype in found:
                if not any(start < e and s < end for s, e in expected_spans):
                    undeclared.append({"document": doc["name"], "type": ptype,
                                       "text": text[start:end]})

    covered_total = sum(s["covered"] for s in per_type.values())
    return {
        "documents": len(documents),
        "declared": total_declared,
        "covered": covered_total,
        "recall": round(covered_total / total_declared, 4) if total_declared else 1.0,
        "per_type": per_type,
        "leaks": leaks,
        "type_mismatch": type_mismatch,
        "violations": violations,
        "undeclared": undeclared,
        "declared_fp": declared_fp,
        "known_miss": known_miss,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args()

    report = evaluate(corpus.DOCUMENTS)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"recall-sweep: {report['documents']} documents, {report['declared']} declared values, "
              f"recall {report['recall'] * 100:.1f}%")
        print(f"{'type':<16}{'declared':>9}{'covered':>9}{'typed':>7}")
        for ptype in sorted(report["per_type"]):
            s = report["per_type"][ptype]
            print(f"{ptype:<16}{s['declared']:>9}{s['covered']:>9}{s['typed']:>7}")
        for label, key in (("LEAK (declared, not covered)", "leaks"),
                           ("FALSE POSITIVE (must_not redacted)", "violations"),
                           ("type mismatch", "type_mismatch"),
                           ("undeclared span (informational)", "undeclared"),
                           ("declared known miss", "known_miss")):
            rows = report[key]
            if not rows:
                continue
            print(f"\n{label}: {len(rows)}")
            for row in rows[:20]:
                detail = row.get("value") or row.get("text", "")
                extra = f" [{row.get('type', row.get('got', ''))}]" if row.get("type") else ""
                print(f"  - {row['document']}: {detail}{extra}")

    return 1 if (report["leaks"] or report["violations"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
