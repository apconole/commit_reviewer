"""Tests for commit-spec resolution (uses the real repository history)."""

import os
import subprocess
import unittest

from commit_reviewer.gitcommits import parse_commit_specs


def _repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    out = subprocess.run(["git", "-C", here, "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    return out.stdout.strip()


class CommitSpecs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _repo_root()
        if not cls.root:
            raise unittest.SkipTest("not running inside a git repository")
        cls._cwd = os.getcwd()
        os.chdir(cls.root)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._cwd)

    def _head(self):
        out = subprocess.run(["git", "rev-parse", "HEAD"],
                             capture_output=True, text=True)
        return out.stdout.strip()

    def test_single_head(self):
        self.assertEqual(parse_commit_specs(["HEAD"]), [self._head()])

    def test_negative_count(self):
        self.assertEqual(len(parse_commit_specs(["-3"])), 3)

    def test_range_oldest_first(self):
        shas = parse_commit_specs(["HEAD~3..HEAD"])
        self.assertEqual(len(shas), 3)
        self.assertEqual(shas[-1], self._head())

    def test_reversed_range_recovers(self):
        # HEAD..HEAD~3 is empty in rev-list order; the resolver swaps it.
        shas = parse_commit_specs(["HEAD..HEAD~3"])
        self.assertEqual(len(shas), 3)

    def test_dedup_across_specs(self):
        shas = parse_commit_specs(["HEAD", "HEAD"])
        self.assertEqual(len(shas), 1)

    def test_invalid_spec_exits(self):
        with self.assertRaises(SystemExit):
            parse_commit_specs(["--help;rm -rf /"])


if __name__ == "__main__":
    unittest.main()
