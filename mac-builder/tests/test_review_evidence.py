"""The review card and Approve show the evidence: original ask, block and CI."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import jobs  # noqa: E402
import review_record  # noqa: E402


def outcome(ci_state: str = "", **facts: object) -> jobs.ReviewOutcome:
    values = dict(
        job=jobs.Job(
            id="20260101-000000-deadbeef", kind="fix", title="fix login",
            channel_id=1, requester_id=2, log_path=Path("/tmp/j.log"),
            text="Rewrite the login flow.",
            original_text="Login is broken on the iPad; the button does nothing."),
        verdict="ask_user", notes="needs a decision",
        environment=SimpleNamespace(branch="freebuff/deadbeef"),
        check_state="passed", test_state="not run", diff_lines=9,
        changed_files=("ios-app/Hustler/Login.swift",),
        check_command="scripts/check.sh", check_counts="12 passed, 0 failed",
        test_command="scripts/test.sh", ci_state=ci_state)
    values.update(facts)
    return jobs.ReviewOutcome(**values)


def shown(embed: object) -> dict[str, str]:
    return {field.name: str(field.value) for field in embed.fields}


class CardTests(unittest.TestCase):
    def test_the_card_shows_the_original_request_verification_and_ci(self) -> None:
        card = shown(bot.review_embed(SimpleNamespace(), outcome(ci_state="failed")))
        self.assertIn("button does nothing", card["Original request"])
        self.assertIn("check: scripts/check.sh — passed (12 passed, 0 failed)",
                      card["Verification"])
        self.assertIn("test: scripts/test.sh — not run", card["Verification"])
        self.assertEqual(card["CI"], "failed")

    def test_a_test_only_change_is_named_on_the_card(self) -> None:
        card = shown(bot.review_embed(
            SimpleNamespace(),
            outcome(test_only="Test-only change: no app code was changed")))
        self.assertEqual(card["Test-only"], "Test-only change: no app code was changed")
        self.assertEqual(card["CI"], "none")


class ApproveTests(unittest.IsolatedAsyncioTestCase):
    async def press(self, reviewed: jobs.ReviewOutcome, ci: str):
        pushes: list[dict[str, str]] = []
        sent: list[str] = []

        async def allowed(_interaction: object) -> bool:
            return True

        async def ci_state(_outcome: object) -> str:
            return ci

        async def approve_and_push(env: object, *, message: str, expected_snapshot: str,
                                   verification: str = "") -> str:
            pushes.append({"message": message, "verification": verification})
            return "abcdef1234567"

        async def defer(**_kwargs: object) -> None:
            return None

        async def send(content: str = "", **_kwargs: object) -> None:
            sent.append(content)

        instance = SimpleNamespace(
            settings=SimpleNamespace(),
            freebuff=SimpleNamespace(approve_and_push=approve_and_push),
            require_allowed=allowed, ci_state=ci_state,
            review_decided=lambda job, status: None)
        view = bot.ReviewView(instance, reviewed)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=2), message=None,
            response=SimpleNamespace(defer=defer),
            followup=SimpleNamespace(send=send))
        await view.approve.callback(interaction)
        return view, pushes, sent

    async def test_a_failed_ci_state_blocks_approve(self) -> None:
        view, pushes, sent = await self.press(outcome(), "failed")
        self.assertEqual(pushes, [], "nothing may be pushed while CI is red")
        self.assertIn("CI failed", sent[-1])
        self.assertFalse(view.resolved, "the card must stay usable")

    async def test_an_approve_pushes_the_verification_block(self) -> None:
        _, pushes, sent = await self.press(outcome(), "pending")
        self.assertEqual(len(pushes), 1)
        self.assertIn("Verification", pushes[0]["verification"])
        self.assertIn("check: scripts/check.sh", pushes[0]["verification"])
        self.assertIn("Pushed", sent[-1])


class RecordTests(unittest.TestCase):
    def test_a_rebuilt_review_keeps_the_new_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(log_dir=Path(tmp), max_diff_lines=400, project=None)
            reviewed = outcome(
                ci_state="pending",
                test_only="Test-only change: no app code was changed")
            review_record.save(settings, reviewed)
            rebuilt = review_record.outcome(review_record.load(settings)[0])
            self.assertEqual(shown(bot.review_embed(settings, rebuilt)),
                             shown(bot.review_embed(settings, reviewed)))
            self.assertEqual(rebuilt.job.original_text, reviewed.job.original_text)


if __name__ == "__main__":
    unittest.main()
