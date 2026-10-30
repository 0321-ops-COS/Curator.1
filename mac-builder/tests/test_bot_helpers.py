"""The /logs id lookup and message splitting, called without Discord."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

JOB_ID = "20260101-000000-deadbeef"


class SplitMessageTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self) -> None:
        self.assertEqual(bot.split_message("hello"), ["hello"])

    def test_empty_text_is_dropped(self) -> None:
        self.assertEqual(bot.split_message("   \n  "), [])

    def test_long_text_splits_on_newlines_within_the_limit(self) -> None:
        text = "\n".join(f"line {i}" for i in range(400))
        chunks = bot.split_message(text, limit=200)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 200)
        self.assertEqual(
            "\n".join(chunks).replace("\n", ""), text.replace("\n", "")
        )

    def test_a_line_longer_than_the_limit_is_cut(self) -> None:
        chunks = bot.split_message("x" * 450, limit=200)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertEqual("".join(chunks), "x" * 450)


class ResolveLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_dir = Path(self._tmp.name)
        self.instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        self.instance.settings = SimpleNamespace(log_dir=self.log_dir)

    def write(self, name: str) -> Path:
        path = self.log_dir / name
        path.write_text("log\n", encoding="utf-8")
        return path

    def test_a_valid_id_resolves_inside_the_log_dir(self) -> None:
        path = self.write(f"{JOB_ID}.log")
        self.assertEqual(self.instance._resolve_log(JOB_ID), path)

    def test_the_dot_log_suffix_is_optional(self) -> None:
        path = self.write(f"{JOB_ID}.log")
        self.assertEqual(self.instance._resolve_log(f"{JOB_ID}.log"), path)

    def test_a_path_or_a_bad_id_is_refused(self) -> None:
        self.write(f"{JOB_ID}.log")
        for value in ("../../etc/passwd", "nope", "20260101-000000-zzzzzzzz", "/etc/passwd"):
            self.assertIsNone(self.instance._resolve_log(value), value)

    def test_a_missing_log_is_none(self) -> None:
        self.assertIsNone(self.instance._resolve_log(JOB_ID))

    def test_the_newest_log_wins_when_no_id_is_given(self) -> None:
        older = self.write("20260101-000000-aaaaaaaa.log")
        newer = self.write("20260102-000000-bbbbbbbb.log")
        now = time.time()
        os.utime(older, (now - 100, now - 100))
        os.utime(newer, (now, now))
        self.assertEqual(self.instance._resolve_log(None), newer)


if __name__ == "__main__":
    unittest.main()
