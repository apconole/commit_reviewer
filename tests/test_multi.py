"""Tests for backend spec parsing and multi-backend work planning."""

import unittest
from argparse import Namespace

from commit_reviewer.multi import (
    parse_backend_spec, sanitize_label, backend_label,
    make_backend_namespace, plan_backends)


class SpecParsing(unittest.TestCase):
    def test_parses_pairs(self):
        spec = parse_backend_spec(
            "provider=openai,model=foo,base-url=http://x/v1")
        self.assertEqual(spec["provider"], "openai")
        self.assertEqual(spec["model"], "foo")
        self.assertEqual(spec["base_url"], "http://x/v1")

    def test_trims_whitespace_and_empty(self):
        spec = parse_backend_spec(" model = foo ,, token=t0k ")
        self.assertEqual(spec, {"model": "foo", "token": "t0k"})

    def test_unknown_key_rejected(self):
        with self.assertRaises(SystemExit):
            parse_backend_spec("provider=openai,bogus=1")

    def test_missing_equals_rejected(self):
        with self.assertRaises(SystemExit):
            parse_backend_spec("openai")

    def test_empty_rejected(self):
        for bad in ("", "   ", ",,"):
            with self.assertRaises(SystemExit):
                parse_backend_spec(bad)


class Labels(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_label("gpt-4o/2024"), "gpt-4o-2024")
        self.assertEqual(sanitize_label("  "), "backend")

    def test_label_precedence(self):
        self.assertEqual(backend_label({"label": "a", "model": "m"}, 0), "a")
        self.assertEqual(backend_label({"model": "gpt-4o"}, 0), "gpt-4o")
        self.assertEqual(backend_label({}, 1), "backend2")


class NamespaceOverlay(unittest.TestCase):
    def test_overrides_and_preserves(self):
        base = Namespace(provider="auto", model="m0", max_tokens=8192,
                         timeout=300)
        spec = {"model": "m1", "provider": "openai"}
        ns = make_backend_namespace(base, spec)
        self.assertEqual(ns.model, "m1")
        self.assertEqual(ns.provider, "openai")
        self.assertEqual(ns.max_tokens, 8192)
        self.assertEqual(ns.timeout, 300)
        # The base namespace must not be mutated by the overlay.
        self.assertEqual(base.model, "m0")


class Planning(unittest.TestCase):
    def test_single_backend_gets_all(self):
        self.assertEqual(plan_backends(["a", "b", "c"], 1, "separate"),
                         [["a", "b", "c"]])

    def test_separate_every_backend_full_list(self):
        plan = plan_backends(["a", "b", "c"], 2, "separate")
        self.assertEqual(plan, [["a", "b", "c"], ["a", "b", "c"]])

    def test_combine_round_robin_partition(self):
        plan = plan_backends(["a", "b", "c", "d", "e"], 2, "combine")
        self.assertEqual(plan, [["a", "c", "e"], ["b", "d"]])

    def test_combine_fewer_commits_than_backends(self):
        plan = plan_backends(["a"], 3, "combine")
        self.assertEqual(plan, [["a"], [], []])


if __name__ == "__main__":
    unittest.main()
