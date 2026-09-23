#!/usr/bin/env python3
"""bench-scan.py — measure the dictionary scan, and what the locator change bought.

The scan locates a dictionary entry by its first token. That locator used to be ONE case-insensitive
alternation over every source, which cost `positions × sources` inside the regex engine: with the
shipped catalogs (290 sources, mostly common Italian words) the engine retried all of them at every
offset of a 5 MB document. It is now a walk over the word runs of the text with dict lookups, plus an
alternation only for the sources holding a non-word character (`D-Link`, `Hyper-V`).

    python3 scripts/bench-scan.py                 # both locators, 5 MB, the repository's catalogs
    python3 scripts/bench-scan.py --mb 20         # a bigger document
    python3 scripts/bench-scan.py --now           # only the current locator
    python3 scripts/bench-scan.py --crossover     # where the two cross over, per number of sources

The corpus is synthetic and built from a fixed word list, so every run is the same bytes; the sources
are the catalogs committed to this repository (`catalogs/*.txt`), never the operator's own dictionary,
which must not enter a repository. Only timings, sizes and counts are printed.

`legacy_hits` below is a deliberate copy of the OLD locator. It exists to be measured against, never
to be called by the engine; it reuses the current candidate resolution because that part was already
factored out and is not what changed.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "anon.py"
CATALOGS = HERE.parent / "catalogs"

# Fixed prose: common Italian words, which is what makes the old alternation expensive — those words
# are also catalog sources, so the engine had to retry them at every position of the document.
WORDS = (
    "il cliente ha chiesto una verifica della configurazione di rete e dei servizi esposti "
    "prima della consegna del rapporto finale al referente tecnico con particolare attenzione "
    "alle regole di filtraggio e alla separazione delle VLAN interne dell'ufficio principale "
    "in sintesi l attività procede secondo il piano concordato senza scostamenti rilevanti e "
    "senza interruzioni del servizio durante le finestre di manutenzione programmata"
).split()


def load_engine():
    spec = importlib.util.spec_from_file_location("anon_engine", ENGINE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["anon_engine"] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


def entities(anon):
    found = []
    for name in ("it-cities", "vendors", "products"):
        path = CATALOGS / f"{name}.txt"
        found += anon.load_entities(path)
    return found


def corpus(megabytes: float, anon, entities_) -> str:
    """A document of the requested size, with a few real catalog names sprinkled through it."""
    prose = " ".join(WORDS)
    names = sorted({e.surface for e in entities_ if e.form == "NFC"})[:12]
    chunk = (prose + "\n") * 200
    if names:
        chunk += " ".join(names) + "\n"
    repeats = max(1, int(megabytes * 1024 * 1024 / len(chunk)))
    return (chunk * repeats)[: int(megabytes * 1024 * 1024)]


def legacy_hits(anon, text: str, entities_):
    """A deliberate copy of the OLD locator: one NON-overlapping alternation over every source.

    It reuses the current candidate resolution (`_literal_candidates`), which was already factored
    out and is not what changed; the loop around it is the old one, non-overlapping `finditer`
    included — that is the whole difference the bench measures.
    """
    subset = [e for e in entities_ if e.first_token and e.inner and not e.context]
    index = anon._build_scan_index(subset, word_min=len(subset) + 1)
    sources = sorted(
        {e.first_token for e in subset if e.first_token and not e.context},
        key=len,
        reverse=True,
    )
    alternation = re.compile("|".join(re.escape(s) for s in sources), re.IGNORECASE)
    for match in alternation.finditer(text):
        matched = match.group(0)
        start = match.start()
        for end in range(1, len(matched) + 1):
            key = anon._locator_key(matched[:end])
            if key not in index.literals:
                continue
            for entity in anon._literal_candidates(index, text, start, end, key):
                anchored = entity.regex.match(text, start)
                if anchored is not None:
                    yield entity, *anchored.span(anon.ENTITY_GROUP)


def crossover(anon, entities_, text: str, megabytes: float) -> None:
    """Where the two locators cross over, so `SCAN_WORD_SOURCES_MIN` is measured, not guessed.

    Each is measured DIRECTLY, with the threshold that would pick it disabled (`word_min=0` builds an
    index for the word walk, `word_min=huge` builds one for the alternation): measuring `entity_hits`
    instead would only measure the choice it makes, which is the thing under test. The alternation's
    cost is `positions × sources` and jumps as soon as the sources include common words; the word
    walk's cost is a per-token floor whatever the count.
    """
    pool = sorted(
        (e for e in entities_ if e.first_token and e.inner and not e.context),
        key=lambda e: e.first_token,
    )
    print(f"{'sources':>9}{'alternation s':>16}{'word walk s':>14}{'faster':>14}")
    for count in (1, 2, 6, 12, 25, 50, 64, 80, 120, 200, 290):
        subset = pool[:count]
        forced_word = anon._build_scan_index(subset, word_min=0)
        forced_alternation = anon._build_scan_index(subset, word_min=len(pool) + 1)
        started = time.perf_counter()
        sum(1 for _ in anon._alternation_hits(forced_alternation, text))
        alternation = time.perf_counter() - started
        started = time.perf_counter()
        sum(1 for _ in anon._word_run_hits(forced_word, text))
        walk = time.perf_counter() - started
        label = "alternation" if alternation < walk else "word walk"
        print(f"{count:>9}{alternation:>16.3f}{walk:>14.3f}{label:>16}")


def timed(run, text: str) -> tuple[float, int]:
    started = time.perf_counter()
    hits = sum(1 for _ in run())
    return time.perf_counter() - started, hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mb", type=float, default=5.0, help="document size in MB (default 5)")
    parser.add_argument("--now", action="store_true", help="measure only the current locator")
    parser.add_argument("--legacy", action="store_true", help="measure only the old locator")
    parser.add_argument(
        "--crossover", action="store_true",
        help="where the two locators cross over, against the number of sources",
    )
    args = parser.parse_args()

    anon = load_engine()
    entities_ = entities(anon)
    text = corpus(args.mb, anon, entities_)
    megabytes = len(text) / 1024 / 1024
    sources = {e.first_token for e in entities_ if e.first_token and e.inner and not e.context}
    word = {s for s in sources if re.fullmatch(r"\w+", s, re.UNICODE)}
    print(f"entità {len(entities_)}  sorgenti {len(sources)}  ({len(word)} di sole lettere, "
          f"{len(sources) - len(word)} con un carattere non di parola)")
    print(f"documento {megabytes:.2f} MB  ({len(text):,} caratteri)\n")
    if args.crossover:
        # A smaller document: the alternation rows grow quadratically with the source count, and the
        # crossover does not move with size — only the two costs' ratio does, and that is flat.
        small = corpus(min(args.mb, 2.0), anon, entities_)
        print(f"documento {len(small) / 1024 / 1024:.2f} MB\n")
        crossover(anon, entities_, small, len(small) / 1024 / 1024)
        return 0
    print(f"{'locator':<34}{'secondi':>9}{'MB/s':>9}{'hit':>8}")

    index = anon._scan_index(entities_)

    def current_locator():
        yield from anon._word_run_hits(index, text)
        yield from anon._alternation_hits(index, text)

    if not args.now:
        seconds, hits = timed(lambda: legacy_hits(anon, text, entities_), text)
        print(f"{'vecchio: alternazione su tutto':<34}{seconds:>9.2f}{megabytes / seconds:>9.2f}"
              f"{hits:>8}")
    if not args.legacy:
        # The SAME work as the row above: the locator only. The context groups and the hand-built
        # entries are common to both and would flatter the newer implementation.
        seconds, hits = timed(current_locator, text)
        print(f"{'nuovo: parole + alternazione':<34}{seconds:>9.2f}{megabytes / seconds:>9.2f}"
              f"{hits:>8}")
        seconds, hits = timed(lambda: anon.entity_hits(text, entities_), text)
        print(f"{'  (di cui entity_hits completo)':<34}{seconds:>9.2f}{megabytes / seconds:>9.2f}"
              f"{hits:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
