"""Tests for repository path sandboxing."""

import os
import tempfile
import unittest

from commit_reviewer.util import (
    ToolError, safe_abs_path, check_pathspec)


class PathSandbox(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cr_root_")

    def test_empty_resolves_to_root(self):
        self.assertEqual(os.path.realpath(safe_abs_path(self.root, "")),
                         os.path.realpath(self.root))

    def test_allows_relative_inside(self):
        p = safe_abs_path(self.root, "sub/dir/file.c")
        self.assertTrue(p.startswith(os.path.realpath(self.root)))

    def test_rejects_parent_escape(self):
        with self.assertRaises(ToolError):
            safe_abs_path(self.root, "../outside")

    def test_rejects_absolute_escape(self):
        with self.assertRaises(ToolError):
            safe_abs_path(self.root, "/etc/passwd")

    def test_rejects_nul_byte(self):
        with self.assertRaises(ToolError):
            safe_abs_path(self.root, "a\x00b")

    def test_check_pathspec(self):
        self.assertEqual(check_pathspec("lib/foo.c"), "lib/foo.c")
        for bad in ("", "/abs/path", "../rel", "a/../../b", "x\x00y"):
            with self.assertRaises(ToolError, msg=repr(bad)):
                check_pathspec(bad)


if __name__ == "__main__":
    unittest.main()
