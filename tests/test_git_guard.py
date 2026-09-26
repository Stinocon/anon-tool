#!/usr/bin/env python3
"""The pre-commit guard: a clean/smudge filter is refused, a harmless attribute is not.

The hook is driven the way git drives it — `python3 git-hooks/pre-commit` with the repository as the
working directory — against throwaway repositories, so the real one is never touched. The point of
the guard is that a `filter` attribute rewrites a file between the working tree and the object
database, and this tool's whole promise is "what you see is what is scanned".

  python3 tests/test_git_guard.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
HOME = HERE.parent
HOOK = HOME / "git-hooks" / "pre-commit"


class GitFilterGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="anon-guard-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self._git("init", "-q")
        self._git("config", "user.email", "guard@example.invalid")
        self._git("config", "user.name", "guard")
        (self.repo / "a.txt").write_text("hello\n", encoding="utf-8")
        self._git("add", "a.txt")

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)

    def _run_hook(self, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK)], cwd=cwd or self.repo, capture_output=True, text=True
        )

    def test_a_plain_tree_passes(self) -> None:
        done = self._run_hook()
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_clean_smudge_filter_is_refused(self) -> None:
        (self.repo / ".gitattributes").write_text("*.dat filter=lfs\n", encoding="utf-8")
        (self.repo / "b.dat").write_text("x\n", encoding="utf-8")
        self._git("add", ".gitattributes", "b.dat")
        done = self._run_hook()
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("filter", done.stderr)
        self.assertIn("b.dat", done.stderr)

    def test_a_filter_in_a_subdirectory_is_found(self) -> None:
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / ".gitattributes").write_text("*.dat filter=mine\n", encoding="utf-8")
        (sub / "b.dat").write_text("x\n", encoding="utf-8")
        self._git("add", "sub/.gitattributes", "sub/b.dat")
        self.assertEqual(self._run_hook().returncode, 1, "a subdirectory rule must not hide")

    def test_an_unset_filter_is_not_a_filter(self) -> None:
        """`-filter` removes the attribute, so no filter is applied: refusing it would be noise."""
        (self.repo / ".gitattributes").write_text("*.txt -filter\n", encoding="utf-8")
        self._git("add", ".gitattributes")
        self.assertEqual(self._run_hook().returncode, 0)

    def test_a_text_attribute_is_not_a_filter(self) -> None:
        (self.repo / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
        self._git("add", ".gitattributes")
        self.assertEqual(self._run_hook().returncode, 0)

    def test_a_filter_set_with_no_value_is_refused(self) -> None:
        """A bare `filter` (set, no value) is still an active filter attribute, and git refuses to
        apply it — an error at checkout, not a silent pass."""
        (self.repo / ".gitattributes").write_text("*.dat filter\n", encoding="utf-8")
        (self.repo / "b.dat").write_text("x\n", encoding="utf-8")
        self._git("add", ".gitattributes", "b.dat")
        self.assertEqual(self._run_hook().returncode, 1)

    def test_a_filter_staged_but_removed_from_the_worktree_is_still_refused(self) -> None:
        """The INDEX is what the commit carries: a staged `.gitattributes` whose worktree copy was
        emptied must not make the filter invisible to the guard."""
        (self.repo / ".gitattributes").write_text("*.dat filter=lfs\n", encoding="utf-8")
        (self.repo / "b.dat").write_text("x\n", encoding="utf-8")
        self._git("add", ".gitattributes", "b.dat")
        (self.repo / ".gitattributes").write_text("\n", encoding="utf-8")  # emptied on disk only
        done = self._run_hook()
        self.assertEqual(done.returncode, 1, "the STAGED filter must still be seen")
        self.assertIn("[index]", done.stderr)

    def test_the_installer_installs_the_hook(self) -> None:
        self._place_installer()
        done = subprocess.run(
            ["bash", str(self.repo / "scripts" / "install-git-hooks.sh")],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        installed = self.repo / ".git" / "hooks" / "pre-commit"
        self.assertTrue(installed.is_file())
        self.assertTrue(os.access(installed, os.X_OK), "a hook that is not executable never runs")

    def test_the_installer_refuses_when_a_hooks_path_would_disable_it(self) -> None:
        """An installed hook that git never runs is the worst outcome: it looks like protection."""
        self._place_installer()
        self._git("config", "core.hooksPath", str(self.repo / "elsewhere"))
        done = subprocess.run(
            ["bash", str(self.repo / "scripts" / "install-git-hooks.sh")],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.assertNotEqual(done.returncode, 0, "an install into a dead directory is not a success")
        self.assertIn("core.hooksPath", done.stderr)

    def _place_installer(self) -> None:
        (self.repo / "scripts").mkdir(exist_ok=True)
        (self.repo / "git-hooks").mkdir(exist_ok=True)
        shutil.copy(HOME / "scripts" / "install-git-hooks.sh", self.repo / "scripts" / "install-git-hooks.sh")
        shutil.copy(HOME / "git-hooks" / "pre-commit", self.repo / "git-hooks" / "pre-commit")

    def test_outside_a_repository_it_is_a_no_op(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="anon-guard-plain-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        done = self._run_hook(cwd=outside)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
