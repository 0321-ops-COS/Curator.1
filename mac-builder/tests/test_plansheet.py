"""`/plansheet` drafts a sheet on the card: read-only, one retry, a paged picker."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import manager  # noqa: E402
import projects  # noqa: E402
import roadmap  # noqa: E402
import sheet_card  # noqa: E402
import sheet_plan  # noqa: E402

SHEET = (
    "## Job 1\nFix the parser and keep its tests green.\n\n"
    "## Job 2\nkind: dan\nproject: notes\nRefresh the docs in mac-builder/README.md.\n"
)
THIRTY = "\n\n".join(f"## Job {n}\nDo step {n}." for n in range(1, 31))
PROTECTED_SHEET = "## Job 1\nAdd the flag in project.yml and read it back.\n"


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deferred = False
        self.view: object | None = None
        self.modal: object | None = None

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True

    async def send_message(self, content: str, **_kwargs: object) -> None:
        self.sent.append(content)

    async def edit_message(self, **kwargs: object) -> None:
        self.view = kwargs.get("view")

    async def send_modal(self, modal: object) -> None:
        self.modal = modal


class FakeFollowup:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.calls.append({"content": content, **kwargs})
        # Discord closes an attachment after sending; do the same here.
        attachment = kwargs.get("file")
        if hasattr(attachment, "close"):
            attachment.close()  # type: ignore[union-attr]

    @property
    def last(self) -> str:
        return str(self.calls[-1]["content"])


class FakeInteraction:
    def __init__(self, user_id: int = 42, values: list[str] | None = None) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.channel_id = 1
        self.data = {"values": values or ["0"]}


class FakeChannel:
    """A channel that records what the card would post and has no old card."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def send(self, content: str, **kwargs: object) -> SimpleNamespace:
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=101)

    async def fetch_message(self, message_id: int) -> object:
        raise RuntimeError("the old card is gone")


class FakeManager:
    enabled = True

    def __init__(self, replies: list[manager.PlanResult]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[object, object, str]] = []

    async def plan_sheet(self, inputs: object, limits: object, correction: str = "") -> manager.PlanResult:
        self.calls.append((inputs, limits, correction))
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return reply


def make_registry(root: Path) -> projects.Registry:
    return projects.Registry([
        projects.Project("hustler", "Hustler", "git@github.com:o/Hustler.git", root, "freebuff/base"),
        projects.Project("notes", "Notes", "git@github.com:o/Notes.git", root / "notes",
                         "freebuff/base", kind="python", checks=("pytest",)),
    ])


class SheetCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()
        self.channel = FakeChannel()

    def make_bot(self, *, allowed: tuple[int, ...] = (42,), replies: list | None = None,
                 registry: object = None, manager_object: object = None):
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(
            log_dir=self.log_dir, allowed_user_ids=frozenset(allowed), max_diff_lines=400,
            repo_dir=self.root, project=None, projects=registry, bot_name="Melisa",
            authorized_branch="freebuff/base", notify_channel_id=None)
        instance.manager = manager_object if manager_object is not None else FakeManager(
            replies or [manager.PlanResult(SHEET)])
        instance.queue = SimpleNamespace(
            submit=mock.AsyncMock(side_effect=AssertionError("a sheet drafted a job")))
        instance._sheet_draft = None
        instance._roadmap_steps = []
        instance._target_channel = lambda channel_id: self.channel
        return instance

    def sheets(self, suffix: str = "") -> list[Path]:
        return sorted(self.log_dir.glob(f"plansheet-*{suffix}.md"))

    async def test_a_valid_reply_becomes_a_draft_and_creates_no_job(self) -> None:
        registry = make_registry(self.root)
        fake = FakeManager([manager.PlanResult(SHEET)])
        instance = self.make_bot(replies=[manager.PlanResult(SHEET)], registry=registry)
        instance.manager = fake
        interaction = FakeInteraction()

        with mock.patch.object(bot.jobs, "make_job") as make_job:
            await instance.cmd_plansheet(interaction, "tidy the parser")

        make_job.assert_not_called()
        instance.queue.submit.assert_not_awaited()
        self.assertEqual(instance._roadmap_steps, [], "a sheet never starts a roadmap")
        self.assertFalse((self.log_dir / "roadmap.json").exists(), "nothing was queued")
        draft = instance._sheet_draft
        assert draft is not None
        self.assertEqual(len(draft.steps), 2)
        self.assertEqual([step.kind for step in draft.steps], ["task", "dan"])
        self.assertEqual([step.project for step in draft.steps], ["hustler", "notes"])
        self.assertTrue((self.log_dir / sheet_card.DRAFT_FILE).exists())

        limits = fake.calls[0][1]
        self.assertEqual((limits.max_diff_lines, limits.max_task_chars),
                         (400, roadmap.MAX_TASK_CHARS), "the live limits reach the prompt")

        cards = self.channel.sent
        self.assertEqual(len(cards), 1)
        self.assertIn("Plan sheet draft", cards[0][0])
        self.assertIn("2 job(s)", cards[0][0])
        self.assertIn("nothing is queued", interaction.followup.last)
        self.assertIn("2 job(s)", interaction.followup.last)
        self.assertIsNotNone(interaction.followup.calls[-1].get("file"))

        saved = self.sheets()
        self.assertEqual(len(saved), 1)
        text = saved[0].read_text(encoding="utf-8")
        self.assertIn("tidy the parser", text)
        self.assertIn("project: notes", text)
        self.assertIn("Refresh the docs", text)

    async def test_a_bad_reply_is_retried_once_then_nothing_is_drafted(self) -> None:
        previous = sheet_card.Draft([roadmap.Step("Existing job.", "task")], 1, 42)
        fake = FakeManager([manager.PlanResult("Here is my plan! No jobs at all."),
                            manager.PlanResult("Still prose, still no headings.")])
        instance = self.make_bot()
        instance.manager = fake
        instance._sheet_draft = previous
        interaction = FakeInteraction()

        await instance.cmd_plansheet(interaction, "tidy the parser")

        self.assertEqual(len(fake.calls), 2, "one retry, no more")
        self.assertIn("## Job", fake.calls[1][2], "the refusal is appended to the retry")
        self.assertIs(instance._sheet_draft, previous, "an existing draft is untouched")
        self.assertEqual([step.text for step in previous.steps], ["Existing job."])
        self.assertIn("Nothing was drafted", interaction.followup.last)
        self.assertIsNotNone(interaction.followup.calls[-1].get("file"))
        self.assertEqual(len(self.sheets("-refused")), 1)
        self.assertEqual(self.sheets().count(self.sheets("-refused")[0]), 1)

    async def test_unknown_project_truncated_prose_and_unconfigured_manager_are_refused(self) -> None:
        registry = make_registry(self.root)
        instance = self.make_bot(registry=registry)
        fake = FakeManager([manager.PlanResult(SHEET)])
        instance.manager = fake

        unknown = FakeInteraction()
        await instance.cmd_plansheet(unknown, "tidy the parser", "ghost")
        self.assertIn("unknown project 'ghost'", unknown.followup.last)
        self.assertIn("hustler, notes", unknown.followup.last)
        self.assertEqual(fake.calls, [], "an unknown project never reaches the model")
        self.assertIsNone(instance._sheet_draft)

        truncated = FakeManager([manager.PlanResult("## Job 1\nhalf a job", truncated=True)])
        instance.manager = truncated
        cut = FakeInteraction()
        await instance.cmd_plansheet(cut, "tidy the parser")
        self.assertIn("token limit", cut.followup.last)
        self.assertIn("Nothing was drafted", cut.followup.last)
        self.assertEqual(len(truncated.calls), 1, "a truncated reply is not retried")
        self.assertIsNone(instance._sheet_draft)

        prose = FakeManager([manager.PlanResult("Sure! Here is the sheet:\n## Job 1\nDo it.")] * 2)
        instance.manager = prose
        around = FakeInteraction()
        await instance.cmd_plansheet(around, "tidy the parser")
        self.assertIn("prose", around.followup.last)
        self.assertIsNone(instance._sheet_draft)

        unconfigured = self.make_bot(manager_object=SimpleNamespace(enabled=False))
        off = FakeInteraction()
        await unconfigured.cmd_plansheet(off, "tidy the parser")
        self.assertIn("not configured", off.followup.last)
        self.assertIn("MANAGER_API_KEY", off.followup.last)
        self.assertIsNone(unconfigured._sheet_draft)

    async def test_a_sheet_job_naming_an_unknown_project_is_refused(self) -> None:
        sheet = "## Job 1\nFix it.\n## Job 2\nproject: ghost\nShip it.\n"
        instance = self.make_bot(replies=[manager.PlanResult(sheet)], registry=make_registry(self.root))
        interaction = FakeInteraction()

        await instance.cmd_plansheet(interaction, "tidy the parser")

        self.assertIn("job 2 names an unknown project 'ghost'", interaction.followup.last)
        self.assertIsNone(instance._sheet_draft)
        self.assertEqual(self.sheets(), [], "a refused sheet is never saved")

    async def test_a_protected_path_is_a_warning_not_a_refusal(self) -> None:
        instance = self.make_bot(replies=[manager.PlanResult(PROTECTED_SHEET)])
        interaction = FakeInteraction()

        await instance.cmd_plansheet(interaction, "add the flag")

        body = self.channel.sent[0][0]
        self.assertIn("protected path", body)
        self.assertIn("project.yml", body)
        draft = instance._sheet_draft
        assert draft is not None
        self.assertEqual(len(draft.steps), 1, "the sheet is still drafted")

    async def test_an_unallowlisted_user_is_refused_before_any_manager_call(self) -> None:
        fake = FakeManager([manager.PlanResult(SHEET)])
        instance = self.make_bot(allowed=(42,))
        instance.manager = fake
        interaction = FakeInteraction(user_id=7)

        await instance.cmd_plansheet(interaction, "tidy the parser")

        self.assertIn("allowlist", interaction.response.sent[0])
        self.assertEqual(fake.calls, [])
        self.assertIsNone(instance._sheet_draft)
        self.assertEqual(self.sheets(), [])

    async def test_thirty_jobs_draft_and_the_picker_reaches_the_last_five(self) -> None:
        instance = self.make_bot(replies=[manager.PlanResult(THIRTY)])
        await instance.cmd_plansheet(FakeInteraction(), "thirty steps")

        draft = instance._sheet_draft
        assert draft is not None
        self.assertEqual(len(draft.steps), 30)
        self.assertIn("30 job(s)", self.channel.sent[0][0], "the card keeps the true count")

        first = sheet_card.JobPicker(instance, "edit")
        values = [option.value for option in first.children[0].options]  # type: ignore[union-attr]
        self.assertEqual((values[0], values[-1], len(values)), ("0", "24", 25))
        earlier = next(item for item in first.children if getattr(item, "label", "") == "Previous")
        self.assertTrue(earlier.disabled)  # type: ignore[attr-defined]
        later = next(item for item in first.children if getattr(item, "label", "") == "Next")
        self.assertFalse(later.disabled)  # type: ignore[attr-defined]

        paging = FakeInteraction()
        await later.callback(paging)  # type: ignore[attr-defined]
        second = paging.response.view
        assert isinstance(second, sheet_card.JobPicker)
        values = [option.value for option in second.children[0].options]  # type: ignore[union-attr]
        self.assertEqual(values, ["25", "26", "27", "28", "29"])
        editing = FakeInteraction(values=["25"])
        await second.children[0].callback(editing)  # type: ignore[union-attr]
        modal = editing.response.modal
        assert modal is not None
        self.assertEqual(modal.title, "Edit job 26")  # type: ignore[attr-defined]

        removing = sheet_card.JobPicker(instance, "remove", 1)
        removal = FakeInteraction(values=["25"])
        await removing.children[0].callback(removal)  # type: ignore[union-attr]
        self.assertEqual(removal.response.view, None)
        self.assertEqual(len(instance._sheet_draft.steps), 29)  # type: ignore[union-attr]
        self.assertEqual(instance._sheet_draft.steps[25].text, "Do step 27.")  # type: ignore[union-attr]
        self.assertIsNotNone(instance._sheet_draft)  # type: ignore[union-attr]


class ProjectSurvivalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / sheet_card.DRAFT_FILE

    def test_a_project_survives_edit_save_and_load(self) -> None:
        draft = sheet_card.Draft([roadmap.Step("one", "task", "", "notes"),
                                  roadmap.Step("two", "task", "", "hustler")], 1, 42)
        draft.edit(0, "one, edited")
        self.assertEqual([step.text for step in draft.steps], ["one, edited", "two"])
        self.assertEqual([step.project for step in draft.steps], ["notes", "hustler"])

        sheet_card.save(self.path, draft)
        self.assertIn("project: notes", self.path.read_text(encoding="utf-8"))
        loaded = sheet_card.load(self.path)
        assert loaded is not None
        self.assertEqual([step.project for step in loaded.steps], ["notes", "hustler"])
        self.assertEqual([step.kind for step in loaded.steps], ["task", "task"])

    def test_a_sheet_without_projects_reads_as_it_did_before(self) -> None:
        draft = sheet_card.Draft([roadmap.Step("only job", "task")], 1, 42)
        self.assertEqual(sheet_card.sheet_text(draft.steps), "## Job 1\nkind: task\nonly job")


class ParseTests(unittest.TestCase):
    def test_prose_before_the_first_heading_is_refused(self) -> None:
        with self.assertRaises(sheet_plan.SheetError):
            sheet_plan.parse_sheet("# Build sheet\n\n## Job 1\nDo it.")
        self.assertEqual(len(sheet_plan.parse_sheet(SHEET)), 2)

    def test_a_protected_path_is_found_in_prose_and_punctuation(self) -> None:
        steps = [roadmap.Step("Read `.env.local` before starting.", "task"),
                 roadmap.Step("Change mac-builder/bot.py.", "dan"),
                 roadmap.Step("Open ios-app/RootView.swift.", "task")]
        self.assertEqual(sheet_plan.named_protected_paths(steps[0]), [".env.local"])
        self.assertEqual(sheet_plan.named_protected_paths(steps[1]), [])
        self.assertEqual(sheet_plan.named_protected_paths(steps[2]), [])
        self.assertEqual(len(sheet_plan.protected_warnings(steps)), 1)


if __name__ == "__main__":
    unittest.main()
