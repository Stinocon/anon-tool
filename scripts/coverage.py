#!/usr/bin/env python3
"""coverage.py — how much of the shipped engine the tests actually run, with a floor that fails.

The engine is stdlib-only, so the measurement is too: `sys.monitoring` records the lines a run
executes (Python 3.12+), and `code.co_lines()` is the denominator — the lines the compiler emitted.

The records are propagated to CHILD processes: the CLI tests spawn `anon.py`, the web tests spawn
the server, and without that propagation every line executed in a child would count as uncovered —
which is how a coverage number ends up describing the harness instead of the product. A generated
`sitecustomize.py` on PYTHONPATH installs the monitor in every process Python starts and appends one
`file:line` list per process; the parent aggregates them.

The floor is a TRIPWIRE, not a target: it sits a few points BELOW the measured value, so an optional
skip (no converter, no local model) cannot fail the gate, while a real loss of coverage does. It says
"the suite still runs the engine", not "the engine is 90% correct" — the mutation corpus is the sharp
gate, this is the coarse one.

    python3 scripts/coverage.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What the gate measures: the shipped code that makes the privacy decision. `web/` is driven through
# subprocesses and a UI, so its line coverage is not comparable to the engine's.
SOURCES = ("anon.py", "deanon.py", "suggest.py", "pdfout.py")
SUITES = ("tests/test_anon.py", "tests/test_suggest.py")
FLOORS = {"anon.py": 85.0, "deanon.py": 78.0, "suggest.py": 88.0, "pdfout.py": 72.0}

# Installed into every child through PYTHONPATH. It must print NOTHING (the suites' output is the
# signal) and must never raise: a failure to install the monitor degrades to "no record", not to a
# broken test run. Each (code, line) returns DISABLE after it has been recorded once — coverage only
# needs to know a line EXECUTED, so a line never fires twice and the overhead collapses to one call
# per distinct line instead of one per execution. Files are opened in APPEND mode, so a reused PID
# cannot overwrite an earlier one.
SITECUSTOMIZE = '''\
import atexit, os, sys

_covered = set()
_targets = set(filter(None, os.environ.get("ANON_COV_TARGETS", "").split(os.pathsep)))


def _line(code, line):
    name = code.co_filename
    if name in _targets:
        _covered.add((name, line))
    return sys.monitoring.DISABLE


def _start():
    if not hasattr(sys, "monitoring"):
        return
    try:
        tool = sys.monitoring.COVERAGE_ID
        sys.monitoring.use_tool_id(tool, "anon-coverage")
        sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, _line)
        sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
    except Exception:  # noqa: BLE001 - a monitor that cannot install must not break the run
        pass


@atexit.register
def _dump():
    prefix = os.environ.get("ANON_COV_OUT")
    if not prefix or not _covered:
        return
    try:
        with open("%s." % prefix + str(os.getpid()), "a") as handle:
            for name, line in sorted(_covered):
                handle.write("%s:%d\\n" % (name, line))
    except OSError:
        pass


_start()
'''


def executable_lines(path: Path) -> set[int]:
    """The line numbers the compiler emitted code for — the denominator of the percentage."""
    code = compile(path.read_bytes(), str(path), "exec")
    lines: set[int] = set()
    stack = [code]
    while stack:
        current = stack.pop()
        for _start, _end, line in current.co_lines():
            if line:
                lines.add(line)
        for const in current.co_consts:
            if isinstance(const, types.CodeType):
                stack.append(const)
    return lines


def run_suites(work: Path, timeout: float) -> list[str]:
    """Run each suite under the monitor; return the ones that did not exit clean."""
    (work / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    path = os.pathsep.join(part for part in (str(work), os.environ.get("PYTHONPATH", "")) if part)
    # Every absolute spelling of the target files: a module imported as `anon` and one launched as
    # `python3 /path/anon.py` carry different `co_filename` strings, and both must be recognised.
    targets = os.pathsep.join(
        spelling
        for name in SOURCES
        for spelling in (str(ROOT / name), str((ROOT / name).resolve()))
    )
    red: list[str] = []
    # ANON_HOME is redirected, exactly as `mutate.py` does: the gate measures the code, it must not
    # read or write the operator's real store. Today every CLI-spawning test overrides it, but one
    # new test that does not would otherwise reach `~/.anon` from inside a gate.
    home = work / "anon-home"
    home.mkdir(exist_ok=True)
    for index, suite in enumerate(SUITES):
        print(f"coverage: {suite} under the line monitor…", flush=True)
        done = subprocess.run(
            [sys.executable, suite],
            cwd=ROOT,
            timeout=timeout,
            env={
                **os.environ,
                "PYTHONPATH": path,
                "ANON_HOME": str(home),
                "ANON_COV_OUT": str(work / f"cov{index}"),
                "ANON_COV_TARGETS": targets,
            },
        )
        if done.returncode != 0:
            red.append(suite)
    return red


def covered_lines(work: Path) -> set[tuple[str, int]]:
    covered: set[tuple[str, int]] = set()
    for blob in work.glob("cov*.*"):
        for row in blob.read_text(encoding="utf-8").splitlines():
            name, _, line = row.rpartition(":")
            if not name:
                continue
            try:
                covered.add((str(Path(name).resolve()), int(line)))
            except ValueError:
                continue
    return covered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coverage.py", description=__doc__)
    parser.add_argument("--timeout", type=float, default=240.0, help="seconds per suite")
    args = parser.parse_args(argv)

    if not hasattr(sys, "monitoring"):
        print(f"coverage: needs Python 3.12+ (sys.monitoring); this is {sys.version.split()[0]}")
        return 2

    with tempfile.TemporaryDirectory(prefix="anon-coverage-") as tmp:
        work = Path(tmp)
        red = run_suites(work, args.timeout)
        covered = covered_lines(work)

    failures: list[str] = []
    print()
    print(f"{'module':<14}{'covered':>9}{'lines':>8}{'coverage':>10}{'floor':>9}")
    for name in SOURCES:
        total = executable_lines(ROOT / name)
        key = str((ROOT / name).resolve())
        hit = {line for (filename, line) in covered if filename == key}
        percent = 100.0 * len(hit & total) / len(total) if total else 0.0
        floor = FLOORS[name]
        mark = "" if percent >= floor else "  <-- BELOW"
        print(f"{name:<14}{len(hit & total):>9}{len(total):>8}{percent:>9.1f}%{floor:>8.0f}%{mark}")
        if not hit:
            failures.append(f"{name}: no line was recorded — the suite never imported it")
        elif percent < floor:
            failures.append(f"{name}: {percent:.1f}% is below the {floor:.0f}% floor")

    for suite in red:
        failures.append(f"{suite}: the suite did not exit clean, so its coverage is partial")

    print()
    if failures:
        print(f"coverage: {len(failures)} problem(s)")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("coverage: every shipped module is exercised above its floor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
