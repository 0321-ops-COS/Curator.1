"""The commit subject keeps whole words; the body keeps the whole ask."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evidence  # noqa: E402

JOB_ID = "deadbeef"
PREFIX = f"Freebuff task {JOB_ID}: "
LONG = "the sign-in screen loses its data and stalls after a few seconds "


def job(title: str, text: str | None = None) -> SimpleNamespace:
    ask = title if text is None else text
    return SimpleNamespace(id=JOB_ID, title=title, text=ask, original_text=ask)


def outcome(ask: str) -> SimpleNamespace:
    return SimpleNamespace(
        job=job(ask[:100], ask), check_command="scripts/check.sh", check_state="passed",
        check_counts="", test_command="scripts/test.sh", test_state="passed",
        test_counts="", build_state="not run")


class SubjectTests(unittest.TestCase):
    def test_a_short_task_is_unchanged(self) -> None:
        self.assertEqual(evidence.commit_subject(job("fix the sign-in button")),
                         PREFIX + "fix the sign-in button")

    def test_a_long_task_is_cut_at_a_word_boundary_and_ends_with_an_ellipsis(self) -> None:
        title = (LONG * 3).strip()
        subject = evidence.commit_subject(job(title))
        self.assertLessEqual(len(subject), evidence.SUBJECT_LIMIT, "the line is bounded")
        self.assertTrue(subject.endswith("…"))
        tail = subject[len(PREFIX):-1]
        self.assertTrue(title.startswith(tail), "nothing was cut mid-word")
        self.assertEqual(title[len(tail)], " ", "the cut is at a space, not a word")

    def test_a_one_word_long_task_still_gets_a_bounded_subject(self) -> None:
        subject = evidence.commit_subject(job("x" * 200))
        self.assertLessEqual(len(subject), evidence.SUBJECT_LIMIT)
        self.assertTrue(subject.endswith("…"))


class BodyTests(unittest.TestCase):
    def test_the_full_ask_is_its_own_paragraph_above_the_verification_block(self) -> None:
        ask = (LONG * 4).strip()
        review = outcome(ask)
        body = evidence.commit_body(review)
        self.assertTrue(body.startswith(ask + "\n\nVerification\n"))
        self.assertIn("check: scripts/check.sh — passed", body)
        self.assertEqual(body, ask + "\n\n" + evidence.verification_block(review))
