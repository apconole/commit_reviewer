"""Tests for findings parsing, severity ranking, and compaction."""

import unittest

from commit_reviewer.agent import (
    parse_findings, worst_severity, compact_messages,
    is_context_length_error, COMPACTED_TOOL_NOTICE)


class Findings(unittest.TestCase):
    def test_extracts_last_block(self):
        text = ("noise\n```json\n{\"findings\": []}\n```\n"
                "final:\n```json\n{\"findings\": [{\"severity\": \"low\"}]}"
                "\n```\n")
        self.assertEqual(parse_findings(text), [{"severity": "low"}])

    def test_ignores_invalid_json(self):
        text = "```json\n{not json}\n```\n"
        self.assertIsNone(parse_findings(text))

    def test_absent_returns_none(self):
        self.assertIsNone(parse_findings("no block here"))


class Severity(unittest.TestCase):
    def test_worst_rank(self):
        f = [{"severity": "low"}, {"severity": "high"}, {"severity": "med"}]
        self.assertEqual(worst_severity(f), "high")

    def test_empty_is_none(self):
        self.assertEqual(worst_severity([]), "none")


class Compaction(unittest.TestCase):
    def _msgs(self):
        return [{"role": "tool", "content": str(i)} for i in range(5)]

    def test_keeps_recent(self):
        msgs = self._msgs()
        self.assertTrue(compact_messages(msgs, 2))
        self.assertEqual([m["content"] for m in msgs][-2:], ["3", "4"])
        self.assertTrue(all(m["content"] == COMPACTED_TOOL_NOTICE
                            for m in msgs[:3]))

    def test_no_change_when_all_compacted(self):
        msgs = self._msgs()
        compact_messages(msgs, 0)
        self.assertFalse(compact_messages(msgs, 0))

    def test_context_error_detection(self):
        self.assertTrue(is_context_length_error("HTTP 400: prompt is too long"))
        self.assertFalse(is_context_length_error("rate limited"))


if __name__ == "__main__":
    unittest.main()
