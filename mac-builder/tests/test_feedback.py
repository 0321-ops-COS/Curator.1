"""An owner note becomes a draft sheet through the /plansheet path."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedback  # noqa: E402
import manager  # noqa: E402
import roadmap  # noqa: E402
import sheet_card  # noqa: E402

NOTE = "The list jumps when I scroll, and the second tab should be called Notes."
SHEET = "## Job 1\nFix the scroll jump in the list and rename the tab.\n"


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True

    async def send_message(self, content: str, **_kwargs: object) -> None:
        self.sent.append(content)


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.sent.append(content)
        attachment = kwargs.get("file")
        if hasattr(attachment, "close"):
            attachment.close()  # type: ignore[union-attr]


class FakeInteraction:
    def __init__(self, user_id: int = 42) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.channel_id = 1


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def send(self, content: str, **kwargs: object) -> SimpleNamespace:
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=55)

    async def fetch_message(self, message_id: int) -> object:
        raise RuntimeError("the old card is gone")


class FakeManager:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[object, object, str]] = []

    async def plan_sheet(self, inputs: object, limits: object, correction: str = "") -> manager.PlanResult:
        self.calls.append((inputs, limits, correction))
        return manager.PlanResult(SHEET)


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_note_is_prompt_material_and_the_source_is_named(self) -> None:
        interaction = FakeInteraction()
        with mock.patch("sheet_plan.command", new=mock.AsyncMock()) as command:
            await feedback.command(SimpleNamespace(), interaction, NOTE, "notes")

        _bot, _interaction, goal, project = command.await_args.args[:4]
        kwargs = command.await_args.kwargs
        self.assertEqual(goal, NOTE, "the owner's words are kept verbatim")
        self.assertEqual(project, "notes")
        self.assertIn(feedback.PREAMBLE, kwargs["prompt_goal"])
        self.assertIn("OWNER'S NOTE", kwargs["prompt_goal"])
        self.assertIn(NOTE, kwargs["prompt_goal"])
        self.assertEqual(kwargs["source"], "/feedback")


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()
        self.channel = FakeChannel()

    def make_bot(self):
        instance = type("Bot", (), {})()
        instance.settings = SimpleNamespace(
            log_dir=self.log_dir, repo_dir=self.root, project=None, projects=None,
            bot_name="Melisa", max_diff_lines=400, allowed_user_ids=frozenset({42}),
        )
        instance.manager = FakeManager()
        instance.queue = SimpleNamespace(submit=mock.AsyncMock())
        instance._sheet_draft = None
        instance._roadmap_steps = []
        instance._target_channel = lambda channel_id: self.channel

        async def require_allowed(interaction) -> bool:
            return interaction.user.id in instance.settings.allowed_user_ids

        instance.require_allowed = require_allowed
        instance._sheet_draft_path = lambda: self.log_dir / sheet_card.DRAFT_FILE
        return instance

    async def test_a_note_becomes_a_draft_and_nothing_is_queued(self) -> None:
        instance = self.make_bot()
        interaction = FakeInteraction()

        await feedback.command(instance, interaction, NOTE)

        inputs, _limits, _correction = instance.manager.calls[0]
        self.assertIn("OWNER'S NOTE", inputs.goal)
        self.assertIn(NOTE, inputs.goal)

        draft = instance._sheet_draft
        assert draft is not None
        self.assertEqual(len(draft.steps), 1)
        self.assertEqual(draft.steps[0].text, "Fix the scroll jump in the list and rename the tab.")
        instance.queue.submit.assert_not_awaited()
        self.assertFalse((self.log_dir / "roadmap.json").exists())

        self.assertEqual(len(self.channel.sent), 1)
        body = self.channel.sent[0][0]
        self.assertIn("Plan sheet draft", body)
        self.assertIn("From /feedback", body)
        self.assertIn("1 job(s)", body)
        self.assertIn("Planned", str(interaction.followup.sent[-1]))
        self.assertTrue(list(self.log_dir.glob("plansheet-*.md")))

    async def test_a_sheet_that_comes_back_empty_is_still_refused(self) -> None:
        instance = self.make_bot()

        async def prose(inputs, limits, correction=""):
            return manager.PlanResult("Sure! Here you go, no headings.")

        instance.manager = SimpleNamespace(enabled=True, plan_sheet=prose)
        interaction = FakeInteraction()

        await feedback.command(instance, interaction, NOTE)

        self.assertIsNone(instance._sheet_draft)
        self.assertIn("Nothing was drafted", str(interaction.followup.sent[-1]))
        self.assertEqual(self.channel.sent, [])


if __name__ == "__main__":
    unittest.main()
