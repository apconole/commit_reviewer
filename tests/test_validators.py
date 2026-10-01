"""Tests for the read-only sed/awk validators."""

import unittest

from commit_reviewer.tools.sedawk import (
    validate_sed, validate_awk, split_sed_commands)
from commit_reviewer.util import ToolError


class SedValidator(unittest.TestCase):
    def test_allows_read_only(self):
        for script in ("10,40p", "/^foo/,/^}/p", "5n", "$=", "1,3d",
                       "s/a/b/g", "s|a|b|p"):
            self.assertTrue(validate_sed(script), script)

    def test_rejects_write_and_exec(self):
        for script in ("w /tmp/evil", "1,2w out.txt", "q", "$d\nw x",
                       "s/a/b/e", "1p;w out.txt"):
            with self.assertRaises(ToolError, msg=script):
                validate_sed(script)

    def test_split_honors_quotes_and_regex(self):
        self.assertEqual(split_sed_commands("1p;2p"), ["1p", "2p"])
        # A ';' inside a regex or quotes must not split the command.
        self.assertEqual(len(split_sed_commands("/a;b/p")), 1)


class AwkValidator(unittest.TestCase):
    def test_allows_safe(self):
        for prog in ("length > 120 {print NR}",
                     "$1 ~ /foo/ && $2 ~ /bar/ {print}"):
            self.assertTrue(validate_awk(prog), prog)

    def test_rejects_dangerous(self):
        for prog in ('BEGIN{system("rm -rf /")}',
                     '{getline x < "/etc/passwd"}',
                     '{print > "out.txt"}',
                     'BEGIN{while(("ls|")|getline l)}',
                     '{print $1 | "sort"}',
                     'BEGIN{print ENVIRON["HOME"]}'):
            with self.assertRaises(ToolError, msg=prog):
                validate_awk(prog)


if __name__ == "__main__":
    unittest.main()
