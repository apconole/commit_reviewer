"""Tests for output capping and git_show line-range paging."""

import os
import shutil
import subprocess
import tempfile
import unittest

from commit_reviewer.constants import TOOL_PAGE_LIMIT, MAX_FILE_LINES
from commit_reviewer.util import cap_text
from commit_reviewer.tools.fs import read_file
from commit_reviewer.tools.git import git_show
from commit_reviewer.tools.sedawk import sed_read


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t"] + list(args),
        cwd=repo, capture_output=True, text=True, check=True).stdout


class CapText(unittest.TestCase):
    def test_under_limit_unchanged(self):
        self.assertEqual(cap_text("hello\nworld", 1000), "hello\nworld")

    def test_cuts_on_line_boundary_and_notes(self):
        text = "\n".join("line %d" % i for i in range(5000))
        out = cap_text(text, 200, hint="use offsets")
        self.assertIn("[... output capped:", out)
        self.assertIn("use offsets", out)
        # Never ends with a partial line: last emitted line is complete.
        import re
        self.assertRegex(out.split("\n[...")[0].splitlines()[-1],
                         r"^line \d+$")

    def test_no_partial_line(self):
        text = "a" * 50 + "\n" + "b" * 50
        out = cap_text(text, 60)
        body = out.split("\n[...")[0]
        self.assertEqual(body, "a" * 50)


class ReadFileCap(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cr_cap_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_long_lines_are_capped(self):
        with open(os.path.join(self.root, "big.c"), "w") as f:
            for i in range(400):
                f.write("%s %d\n" % ("x" * 200, i))
        out = read_file({"root": self.root}, {"path": "big.c"})
        self.assertLessEqual(len(out), TOOL_PAGE_LIMIT + 1024)
        self.assertIn("output capped", out)


class SedCap(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cr_sed_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_large_output_capped(self):
        with open(os.path.join(self.root, "f.txt"), "w") as f:
            for i in range(20000):
                f.write("row %d data\n" % i)
        out = sed_read({"root": self.root},
                       {"path": "f.txt", "expression": "p"})
        self.assertIn("output capped", out)


class GitShowPaging(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self.repo = tempfile.mkdtemp(prefix="cr_gs_")
        _git(self.repo, "init", "-q")
        with open(os.path.join(self.repo, "many.c"), "w") as f:
            for i in range(1, MAX_FILE_LINES + 501):
                f.write("line %d\n" % i)
        _git(self.repo, "add", "many.c")
        _git(self.repo, "commit", "-qm", "add many")
        self.sha = _git(self.repo, "rev-parse", "HEAD").strip()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_default_window_paged_with_total(self):
        out = git_show({}, {"rev": self.sha, "file": "many.c"})
        total = MAX_FILE_LINES + 500
        self.assertIn("%d line(s) total" % total, out)
        self.assertIn("showing 1-%d" % MAX_FILE_LINES, out)
        self.assertIn("     1  line 1", out)

    def test_start_end_range(self):
        out = git_show({}, {"rev": self.sha, "file": "many.c",
                            "start_line": 10, "end_line": 12})
        self.assertIn("showing 10-12", out)
        self.assertIn("line 10", out)
        self.assertIn("line 12", out)
        self.assertNotIn("line 9\n", out)

    def test_invalid_range(self):
        with self.assertRaises(Exception):
            git_show({}, {"rev": self.sha, "file": "many.c",
                          "start_line": 5, "end_line": 1})


if __name__ == "__main__":
    unittest.main()
