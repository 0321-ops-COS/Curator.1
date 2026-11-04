"""The review card shows the evidence: the verification block and the test-only line."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import jobs  # noqa: E402


def outcome(**facts: object) -> jobs.ReviewOutcome:
    values = dict(
        job=jobs.Job(
            id="20260101-000000-deadbeef", kind="fix", title="fix login",
            channel_id=1, requester_id=2, log_path=Path("/tmp/j.log"),
            text="Rewrite the login flow."),
        verdict="ask_user", notes="needs a decision",
        environment=SimpleNamespace(branch="freebuff/deadbeef"),
        check_state="passed", test_state="not run", diff_lines=9,
        changed_files=("ios-app/Hustler/Login.swift",),
        check_command="scripts/check.sh", check_counts="12 passed, 0 failed",
        test_command="scripts/test.sh")
    values.update(facts)
    return jobs.ReviewOutcome(**values)


def shown(embed: object) -> dict[str, str]:
    return {field.name: str(field.value) for field in embed.fields}


class CardTests(unittest.TestCase):
    def test_a_test_only_change_is_named_on_the_card(self) -> None:
        card = shown(bot.review_embed(
            SimpleNamespace(),
            outcome(test_only="Test-only change: no app code was changed")))
        self.assertEqual(card["Test-only"], "Test-only change: no app code was changed")


if __name__ == "__main__":
    unittest.main()
