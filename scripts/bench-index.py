#!/usr/bin/env python3
"""What does indexing a container's visible text cost, and what did it cost before?

The container pass maps every character of the visible text back to its source offset, so that a
value a word processor split across runs is still rewritten in one place. That map used to be a
Python list of ints plus a list of single characters — tens of bytes per character — which is how a
64 MB part could ask for gigabytes. It is now a chunked join plus an `array('i')`.

The numbers this prints are the ones quoted in `docs/DESIGN.md`, `CHANGELOG.md` and
`docs/OPEN-ISSUES.md`; this script is what makes them re-runnable instead of remembered:

    python3 scripts/bench-index.py --mb 16            # both implementations, in two processes
    python3 scripts/bench-index.py --mb 16 --impl now

Each implementation runs in its OWN process: peak RSS is per process and never comes down, so
measuring both in one interpreter would charge the first one's peak to the second.

The legacy variant below is a deliberate copy of the old code. It exists to be measured against,
never to be called by the engine.
"""
from __future__ import annotations

import argparse
import importlib.util
import resource
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "anon.py"


def load_engine():
    spec = importlib.util.spec_from_file_location("anon_engine", ENGINE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["anon_engine"] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


def fixture(megabytes: float) -> str:
    """A document-shaped XML: a run every ~50 characters, which is what Word actually emits."""
    per_run = 200_000
    runs = max(1, int(megabytes * 1_000_000 / 65))
    return "".join(
        f"<w:r><w:t>Cliente {index:06d}, referente Mario Rossi lunga riga di testo. </w:t></w:r>"
        for index in range(min(runs, per_run))
    )


def legacy_masked_index(xml: str) -> tuple[str, list[int], set[int]]:
    """The pre-rewrite implementation: kept here only as the thing being measured against."""
    import re

    markup = re.compile(r"<[^>]*>")
    run_level = re.compile(r"^</?(?:w:r|w:t|w:rPr|w:proofErr|w:noProof|w:lastRenderedPageBreak|"
                           r"a:r|a:t|a:rPr|a:endParaRPr|text:span|text:s)\b")
    visible: list[str] = []
    offsets: list[int] = []
    structural: set[int] = set()
    position = 0
    for match in markup.finditer(xml):
        visible.extend(xml[position:match.start()])
        offsets.extend(range(position, match.start()))
        if not all(run_level.match(tag) for tag in markup.findall(match.group(0))):
            structural.add(len(visible))
        visible.append(" ")
        offsets.append(-1)
        position = match.end()
    visible.extend(xml[position:])
    offsets.extend(range(position, len(xml)))
    return "".join(visible), offsets, structural


def measure(impl: str, megabytes: float) -> None:
    xml = fixture(megabytes)
    if impl == "now":
        index = load_engine().masked_index
    else:
        index = legacy_masked_index
    started = time.time()
    visible, offsets, _structural = index(xml)
    elapsed = time.time() - started
    # `ru_maxrss` is KiB on Linux and BYTES on macOS — the two platforms disagree, and getting it
    # backwards is how a benchmark reports half a terabyte of peak RSS without anyone noticing.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1024 if sys.platform.startswith("linux") else 1
    peak_mb = peak * scale / 1e6
    print(
        f"{impl:<4} xml {len(xml) / 1e6:6.1f} MB -> visible {len(visible) / 1e6:6.1f} MB | "
        f"{elapsed:5.2f}s | peak RSS {peak_mb:6.0f} MB | "
        f"{peak_mb * 1e6 / max(len(visible), 1):5.1f} bytes per visible character | "
        f"offsets: {type(offsets).__name__}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mb", type=float, default=16.0, help="size of the synthetic XML part")
    parser.add_argument("--impl", choices=("now", "legacy", "both"), default="both")
    args = parser.parse_args()
    if args.impl != "both":
        measure(args.impl, args.mb)
        return 0
    for impl in ("legacy", "now"):
        subprocess.run([sys.executable, str(Path(__file__)), "--mb", str(args.mb), "--impl", impl],
                       check=True)
    print("\nNote: 'bytes per visible character' is whole-process peak RSS divided by the visible\n"
          "characters, not the cost of the index alone — it counts the XML, the joined text and the\n"
          "interpreter too. It is the ratio that matters, and it is the same experiment on both sides.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
