"""Tests for the per-commit review worktree used to pin file tools."""

import os
import shutil
import subprocess
import tempfile
import unittest

from commit_reviewer.util import (
    create_review_worktree, remove_review_worktree)
from commit_reviewer.tools.fs import read_file


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t"] + list(args),
        cwd=repo, capture_output=True, text=True, check=True).stdout


class WorktreePin(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self.repo = tempfile.mkdtemp(prefix="cr_repo_")
        self.base = tempfile.mkdtemp(prefix="cr_base_")
        _git(self.repo, "init", "-q")
        with open(os.path.join(self.repo, "f.c"), "w") as f:
            f.write("old line\n")
        _git(self.repo, "add", "f.c")
        _git(self.repo, "commit", "-qm", "first")
        self.old_sha = _git(self.repo, "rev-parse", "HEAD").strip()
        with open(os.path.join(self.repo, "f.c"), "w") as f:
            f.write("new line\nsecond\n")
        _git(self.repo, "commit", "-qam", "second")
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.base, ignore_errors=True)

    def test_reads_pinned_revision_not_tip(self):
        wt = create_review_worktree(self.old_sha, self.base)
        try:
            out = read_file({"root": wt}, {"path": "f.c"})
            self.assertIn("old line", out)
            self.assertNotIn("new line", out)
            # Working tree / tip still has the newer content.
            head_out = read_file({"root": self.repo}, {"path": "f.c"})
            self.assertIn("new line", head_out)
        finally:
            remove_review_worktree(wt)

    def test_created_under_base_dir_and_base_preserved(self):
        wt = create_review_worktree(self.old_sha, self.base)
        try:
            self.assertTrue(wt.startswith(os.path.abspath(self.base)))
            self.assertTrue(os.path.isdir(wt))
        finally:
            remove_review_worktree(wt)
        self.assertFalse(os.path.exists(wt))
        self.assertTrue(os.path.isdir(self.base))

    def test_remove_is_idempotent_and_safe_on_empty(self):
        wt = create_review_worktree(self.old_sha, self.base)
        remove_review_worktree(wt)
        remove_review_worktree(wt)  # second call must not raise
        remove_review_worktree("")   # no-op
        remove_review_worktree(None)  # no-op


if __name__ == "__main__":
    unittest.main()
