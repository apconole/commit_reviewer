"""Tests for the grep tool (limits, binary and .git skipping)."""

import os
import tempfile
import unittest

from commit_reviewer.tools import run_tool


class GrepTool(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cr_grep_")
        with open(os.path.join(self.root, "a.c"), "w") as f:
            f.write("needle one\nkeep\nneedle two\n")
        with open(os.path.join(self.root, "b.txt"), "w") as f:
            f.write("needle in text\n")
        os.mkdir(os.path.join(self.root, ".git"))
        with open(os.path.join(self.root, ".git", "hook.c"), "w") as f:
            f.write("needle under git\n")
        with open(os.path.join(self.root, "bin.dat"), "wb") as f:
            f.write(b"needle\x00binary\n")
        self.state = {"root": self.root, "verbose": False}

    def _files(self, out):
        return sorted({ln.split(":", 1)[0] for ln in out.splitlines()
                       if ":" in ln})

    def test_finds_matches(self):
        out = run_tool("grep", {"pattern": "needle"}, self.state)
        self.assertIn("a.c", out)

    def test_skips_git_and_binary(self):
        out = run_tool("grep", {"pattern": "needle"}, self.state)
        files = self._files(out)
        self.assertNotIn(".git/hook.c", files)
        self.assertNotIn("bin.dat", files)

    def test_include_glob(self):
        out = run_tool("grep", {"pattern": "needle", "include": "*.c"},
                       self.state)
        self.assertIn("a.c", out)
        self.assertNotIn("b.txt", out)

    def test_max_results_limit(self):
        out = run_tool("grep", {"pattern": "needle", "max_results": 1},
                       self.state)
        n = len([ln for ln in out.splitlines() if "needle" in ln])
        self.assertEqual(n, 1)
        self.assertIn("limit reached", out)

    def test_no_match_message(self):
        out = run_tool("grep", {"pattern": "zzz_absent"}, self.state)
        self.assertTrue(out.startswith("no matches"))


if __name__ == "__main__":
    unittest.main()
