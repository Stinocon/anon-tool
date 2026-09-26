#!/usr/bin/env python3
"""mutate.py — a deliberate defect per invariant, and the test that has to catch it.

A green suite says the tests PASS; it does not say they would FAIL if the code were wrong. Each
entry below breaks ONE stated invariant of a shipped module and names the test that must notice.
The harness copies the tree to a temporary directory (never editing the working tree), runs the
named test on the control and on the mutant, and requires the control to PASS and the mutant to
FAIL. A defect that survives is a hole in the suite, and the gate exits non-zero.

Deterministic: literal string anchors, named tests, no network, no randomness. An anchor that is
not present EXACTLY once is an error, so a mutation that silently stopped applying cannot pass.

    python3 scripts/mutate.py            # the whole corpus
    python3 scripts/mutate.py --only tag # a subset, by substring
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    find: str
    replace: str
    test: str  # argv after the interpreter: "tests/<file>.py Class.test_method"
    reason: str


# One mutation per invariant worth protecting. The test is the ONE that must fail on the mutant;
# keep it narrow, or a green control costs the whole suite for every entry.
MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        name="anon: a literal placeholder in the source may be reused",
        path="anon.py",
        find="            if candidate not in self.reserved:\n                break",
        replace="            break",
        test="tests/test_anon.py RoundTripTest.test_a_literal_tagged_placeholder_is_not_reused",
        reason="a `[EMAIL-1]` already in the text must not be given to a new value (round-trip breaks)",
    ),
    Mutation(
        name="anon: the tag allocator ignores the tags already taken",
        path="anon.py",
        find="            if candidate not in blocked:\n                return candidate",
        replace="            if True:\n                return candidate",
        test="tests/test_anon.py TagAllocatorTest.test_the_allocator_retries_a_taken_tag_and_stays_inside_the_placeholder_syntax",
        reason="a colliding tag makes a wrong map resolve the placeholders silently",
    ),
    Mutation(
        name="anon: an unterminated `<` swallows the rest of the part",
        path="anon.py",
        find=(
            "        if end == -1:\n"
            "            # The `<` is text: scan from just after it, so the value it was hiding is seen.\n"
            "            index = start + 1\n"
            "            continue"
        ),
        replace="        if end == -1:\n            yield (start, length)\n            return",
        test="tests/test_anon.py MarkupTokenTest.test_an_unterminated_tag_does_not_hide_the_value_after_it",
        reason="the tail must stay TEXT, or the redaction and the verification share the same blind spot",
    ),
    Mutation(
        name="anon: the locator fold drops casefolding",
        path="anon.py",
        find="    return value.translate(_LOCATOR_TRANSLATION).casefold()",
        replace="    return value.translate(_LOCATOR_TRANSLATION)",
        test="tests/test_anon.py RoundTripTest.test_the_locator_fold_covers_everything_ignorecase_matches",
        reason="the dictionary must match every spelling `re.IGNORECASE` matches, or a name stays in clear",
    ),
    Mutation(
        name="deanon: a run with nothing restored claims to be complete",
        path="deanon.py",
        find='        "complete": remaining == 0 and unknown == 0 and not unreadable_parts and bool(parts),',
        replace='        "complete": True,',
        test="tests/test_anon.py DeanonContainerTest.test_document_without_any_placeholder_is_reported",
        reason="a silent no-op must not be delivered as a complete restore",
    ),
    Mutation(
        name="pdfout: the xref offsets are shifted by one",
        path="pdfout.py",
        find='        out += f"{offsets[number]:010d} 00000 n \\n".encode()',
        replace='        out += f"{offsets[number] + 1:010d} 00000 n \\n".encode()',
        test="tests/test_anon.py PdfOutputTest.test_every_xref_offset_points_at_its_object",
        reason="the xref is the file's own index: a wrong offset is a PDF a reader refuses",
    ),
    Mutation(
        name="suggest: a hallucinated value is proposed anyway",
        path="suggest.py",
        find="        if not spans:  # not in the document: a hallucination, and it costs nothing\n            continue",
        replace="        if not spans:  # not in the document: a hallucination, and it costs nothing\n            spans = [(0, len(value))]",
        test="tests/test_suggest.py ParsingTest.test_a_value_is_located_by_us_not_trusted_from_the_model",
        reason="the model names candidates; locating them in the document is the engine's job",
    ),
    Mutation(
        name="backup: a refusal in the passphrase subshell does not stop the run",
        path="scripts/anon-home-backup.sh",
        find='  pass="$(passphrase confirm)" || exit $?',
        replace='  pass="$(passphrase confirm)"',
        test="tests/test_backup.py BackupTest.test_an_empty_passphrase_is_refused_and_writes_no_archive",
        reason="`die` inside `$(...)` exits only the subshell: without `|| exit` the run reaches openssl",
    ),
    Mutation(
        name="guard: the pre-commit filter check is disabled",
        path="git-hooks/pre-commit",
        find='        if value not in (b"unspecified", b"unset"):',
        replace="        if False:",
        test="tests/test_git_guard.py GitFilterGuardTest.test_a_clean_smudge_filter_is_refused",
        reason="a clean/smudge filter rewrites what is committed and what a clone gets",
    ),
    Mutation(
        name="guard: only the worktree attributes are checked, not the index",
        path="git-hooks/pre-commit",
        find='    for source, extra in (("worktree", ()), ("index", ("--cached",))):',
        replace='    for source, extra in (("worktree", ()),):',
        test="tests/test_git_guard.py GitFilterGuardTest.test_a_filter_staged_but_removed_from_the_worktree_is_still_refused",
        reason="the index is what the commit carries; a staged filter must not hide",
    ),
    Mutation(
        name="mcp: the text-size bound is removed",
        path="mcp_anon.py",
        find="    if len(text) > MAX_TEXT_CHARS:",
        replace="    if False:",
        test="tests/test_mcp.py McpTest.test_an_oversized_text_is_refused",
        reason="the transport must bound what it hands the engine, not trust the caller",
    ),
    Mutation(
        name="mcp: a deeply nested frame kills the transport",
        path="mcp_anon.py",
        find="        except (json.JSONDecodeError, RecursionError, MemoryError) as exc:",
        replace="        except json.JSONDecodeError as exc:",
        test="tests/test_mcp.py McpTest.test_a_deeply_nested_frame_does_not_kill_the_transport",
        reason="RecursionError is not JSONDecodeError: one bad frame must not end the server",
    ),
)

IGNORED = (".git", ".pi", "__pycache__", ".pytest_cache")


@dataclass(frozen=True)
class Run:
    """One test run: an exit code, its output, and whether it had to be killed for hanging."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def copy_tree(source: Path, destination: Path) -> None:
    """A private copy of the working tree, without history, private state or bytecode caches."""
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*IGNORED, "*.pyc", "*.redacted.*", "*.map.json"),
        symlinks=True,
    )


def run_test(tree: Path, test: str, timeout: float) -> "Run":
    home = tree / "_anon_home"
    home.mkdir(exist_ok=True)
    try:
        done = subprocess.run(
            [sys.executable, *test.split()],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "ANON_HOME": str(home)},
        )
        return Run(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr, timed_out=False)
    except subprocess.TimeoutExpired as exc:
        # A mutant that HANGS the test is not a pass: the defect is detected, if clumsily. Reported
        # as caught-and-slow rather than crashing the harness with an unhandled exception.
        return Run(returncode=None, stdout=exc.stdout or "", stderr=exc.stderr or "", timed_out=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mutate.py", description=__doc__)
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per test run")
    parser.add_argument("--only", help="run only mutations whose name contains this substring")
    args = parser.parse_args(argv)

    selected = [m for m in MUTATIONS if not args.only or args.only in m.name]
    if not selected:
        print(f"mutate: no mutation matches {args.only!r}")
        return 2

    failures: list[str] = []
    controls: dict[str, subprocess.CompletedProcess[str]] = {}
    with tempfile.TemporaryDirectory(prefix="anon-mutate-") as tmp:
        tree = Path(tmp) / "repo"
        copy_tree(ROOT, tree)

        for mutation in selected:
            target = tree / mutation.path
            if not target.is_file():
                failures.append(f"{mutation.name}: {mutation.path} is missing from the copy")
                print(f"FAIL  {mutation.name}\n      {mutation.path} not found")
                continue
            original = target.read_text(encoding="utf-8")
            occurrences = original.count(mutation.find)
            if occurrences != 1:
                failures.append(
                    f"{mutation.name}: the anchor occurs {occurrences} time(s), must be exactly 1"
                )
                print(f"FAIL  {mutation.name}\n      anchor occurs {occurrences} time(s)")
                continue

            # The control runs once per test selector: a selector that does not PASS unmutated
            # would make the mutant "fail" for the wrong reason, and every entry would look caught.
            if mutation.test not in controls:
                controls[mutation.test] = run_test(tree, mutation.test, args.timeout)
            control = controls[mutation.test]
            if not control.passed:
                detail = "timed out" if control.timed_out else f"exit {control.returncode}"
                failures.append(
                    f"{mutation.name}: the control test {mutation.test} does not pass on clean code ({detail})"
                )
                print(f"FAIL  {mutation.name}\n      control {mutation.test} {detail}")
                continue

            target.write_text(original.replace(mutation.find, mutation.replace), encoding="utf-8")
            try:
                mutant = run_test(tree, mutation.test, args.timeout)
            finally:
                target.write_text(original, encoding="utf-8")

            if mutant.passed:
                failures.append(f"{mutation.name}: the defect SURVIVED {mutation.test}")
                print(f"SURVIVED  {mutation.name}\n          {mutation.test} still passes — a hole")
            elif mutant.timed_out:
                print(f"caught    {mutation.name}\n          by {mutation.test} (timed out: the defect hangs it)")
            else:
                print(f"caught    {mutation.name}\n          by {mutation.test}")

    print()
    if failures:
        print(f"mutate: {len(failures)} of {len(selected)} mutation(s) not caught")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"mutate: all {len(selected)} mutation(s) caught — every defect made a test fail")
    return 0


if __name__ == "__main__":
    sys.exit(main())
