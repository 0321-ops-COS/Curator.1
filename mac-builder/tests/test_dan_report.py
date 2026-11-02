"""The dan report a review posts: recorded facts, and what nobody verified."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

SHA = "a" * 40
JOB_ID = "20261102-101500-deadbeef"
TASK = "Add a share button.\n\n## Acceptance\n1. The button is on the home screen.\n"


def outcome(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = dict(
        job=SimpleNamespace(id=JOB_ID, text=TASK, repo_slug="0321-ops-COS/Curator.1",
                            branch=f"dan/{JOB_ID}", channel_id=3),
        check_state="CHECK PASSED", test_state="not run", diff_lines=12,
        changed_files=("a.py", "b.py"),
        environment=SimpleNamespace(branch=f"dan/{JOB_ID}"), snapshot=SHA + ":c0ffee",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class ReportTests(unittest.TestCase):
    def test_every_field_is_the_recorded_value(self) -> None:
        text = bot.dan_report_text(outcome())
        for value in ("0321-ops-COS/Curator.1", f"dan/{JOB_ID}", SHA, "Files changed: 2",
                      "`a.py`, `b.py`", "Diff lines: 12", "Check: CHECK PASSED · test not run",
                      "Not verified: live Discord, live GitHub, simulator"):
            self.assertIn(value, text)

    def test_a_value_nobody_recorded_says_so(self) -> None:
        text = bot.dan_report_text(outcome(
            snapshot="", environment=None,
            job=SimpleNamespace(id=JOB_ID, text="", repo_slug="", branch="", channel_id=1)))
        self.assertEqual(text.count("not recorded"), 3)
        self.assertNotIn("None", text)

class PostTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_review_posts_the_report_and_then_the_card(self) -> None:
        sent: list[str] = []

        class Channel:
            async def send(self, text: str) -> None:
                sent.append(text)

        await bot.MacBuilderBot._post_report(
            bot.MacBuilderBot.__new__(bot.MacBuilderBot), Channel(), outcome())
        self.assertEqual(
            (len(sent), "Not verified:" in sent[0],
             sent[1].startswith("**For you to test:**")), (2, True, True))


if __name__ == "__main__":
    unittest.main()
