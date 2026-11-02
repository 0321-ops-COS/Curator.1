"""A review rebuilt from what was saved, its refusals, and a refreshed Approve."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import freebuff_runner  # noqa: E402
import jobs  # noqa: E402
import review_record  # noqa: E402
import roadmap  # noqa: E402

JOB_ID = "20260101-000000-deadbeef"
BRANCH = "freebuff/a1b2c3d4"
FILES = ("ios-app/RootView.swift", "ios-app/Theme.swift")
SNAPSHOT = "a" * 40 + ":b2c3"


def config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(log_dir=root, max_diff_lines=400, project=None)


def outcome(worktree: Path, snapshot: str = SNAPSHOT) -> jobs.ReviewOutcome:
    return jobs.ReviewOutcome(
        job=jobs.Job(id=JOB_ID, kind="task", title="add a button", channel_id=1,
                     requester_id=42, log_path=worktree.parent / f"{JOB_ID}.log",
                     text="Add a button."),
        verdict="ask_user", notes="looks right",
        environment=freebuff_runner.TaskEnvironment(
            branch=BRANCH, worktree=worktree, session="macbuilder-deadbeef",
            base_branch="freebuff/base"),
        check_state="passed", test_state="passed", diff_lines=12,
        changed_files=FILES, snapshot=snapshot)


class FakeFreebuff:
    """collect(), approve_and_push() and the one read-only git call a refresh makes."""

    def __init__(self, snapshot: str = SNAPSHOT, files: tuple[str, ...] = FILES,
                 pushed: str = "") -> None:
        self.snapshot, self.files, self.pushed = snapshot, list(files), pushed
        self.collects = self.pushes = 0

    def _repo_for(self, env: object) -> SimpleNamespace:
        async def run(_args: list[str], check: bool = False) -> SimpleNamespace:
            return SimpleNamespace(code=0, stdout=self.pushed)
        return SimpleNamespace(_run=run)

    async def collect(self, env: object, max_diff_lines: int) -> SimpleNamespace:
        self.collects += 1
        return SimpleNamespace(snapshot=self.snapshot, changed_paths=list(self.files))

    async def approve_and_push(self, env: object, *, message: str,
                               expected_snapshot: str) -> str:
        self.pushes += 1
        return "abcdef1234567"


class RebuiltCardTests(unittest.TestCase):
    def test_a_rebuilt_review_yields_the_same_card_and_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            settings = config(root)
            original = outcome(worktree)
            review_record.save(settings, original)
            records = review_record.load(settings)
            self.assertEqual(len(records), 1, "a saved review is still awaiting")
            rebuilt = review_record.outcome(records[0])
            self.assertEqual(bot.review_embed(settings, rebuilt).to_dict(),
                             bot.review_embed(settings, original).to_dict())
            self.assertEqual(
                (rebuilt.job.id, rebuilt.environment.branch, rebuilt.snapshot,
                 rebuilt.changed_files, rebuilt.check_ok, rebuilt.diff_lines,
                 rebuilt.environment.session, rebuilt.environment.worktree),
                (JOB_ID, BRANCH, SNAPSHOT, FILES, True, 12,
                 "macbuilder-deadbeef", worktree))


class RefusalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.settings = config(self.root)
        self.outcome = outcome(self.worktree)
        review_record.save(self.settings, self.outcome)
        self.bot = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        self.bot.settings = self.settings

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_unchanged_is_allowed_and_every_stale_case_says_why(self) -> None:
        self.bot.freebuff = FakeFreebuff()
        self.assertIsNone(await self.bot.review_refusal(self.outcome))
        for freebuff, expected in (
            (FakeFreebuff("f" * 40 + ":new", ("ios-app/RootView.swift",)),
             "worktree changed since this card was shown"),
            (FakeFreebuff(files=("ios-app/Other.swift",)), "changed files differ"),
            (FakeFreebuff(pushed=f"abc\trefs/heads/{BRANCH}\n"), "already on origin"),
        ):
            self.bot.freebuff = freebuff
            why = await self.bot.review_refusal(self.outcome)
            self.assertIn(expected, why)
            self.assertIn("nothing", why.lower())

    async def test_a_decided_job_is_refused_without_reading_the_worktree(self) -> None:
        review_record.mark(self.settings, JOB_ID, "approved")
        self.bot.freebuff = FakeFreebuff()
        why = await self.bot.review_refusal(self.outcome)
        self.assertIn("was already approved", why)
        self.assertEqual(self.bot.freebuff.collects, 0)
        self.assertEqual(review_record.load(self.settings), [])


class RefreshedApproveTests(unittest.IsolatedAsyncioTestCase):
    """Approve on a refreshed card is the same single strict push."""

    def card(self, freebuff: FakeFreebuff) -> tuple[bot.ReviewView, SimpleNamespace]:
        self.decisions: list[tuple[str, str]] = []
        self.refusal: str | None = None
        sent: list[str] = []

        async def allowed(_interaction: object) -> bool:
            return True

        async def refusal(_outcome: object) -> str | None:
            return self.refusal

        async def send(content: str = "", **_kwargs: object) -> None:
            sent.append(content)

        self.bot = SimpleNamespace(
            settings=config(Path("/nonexistent/logs")), freebuff=freebuff,
            require_allowed=allowed, review_refusal=refusal,
            review_decided=lambda item, status: self.decisions.append((item.id, status)))
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42), message=None, sent=sent,
            response=SimpleNamespace(defer=self._defer, is_done=lambda: True),
            followup=SimpleNamespace(send=send))
        view = bot.ReviewView(self.bot, outcome(Path("/nonexistent/worktree")),
                              refreshed=True)
        return view, interaction

    async def _defer(self, **_kwargs: object) -> None:
        return None

    async def test_approve_pushes_once_but_a_refusal_pushes_nothing(self) -> None:
        freebuff = FakeFreebuff()
        view, interaction = self.card(freebuff)
        await view.approve.callback(interaction)
        self.assertEqual((freebuff.pushes, self.decisions),
                         (1, [(JOB_ID, "approved")]))
        self.assertEqual(view.outcome.job.status, "approved")
        self.assertIn("Pushed", interaction.sent[-1])

        freebuff = FakeFreebuff()
        view, interaction = self.card(freebuff)
        self.refusal = "Stale. Nothing was pushed."
        await view.approve.callback(interaction)
        self.assertEqual((freebuff.pushes, self.decisions), (0, []))
        self.assertIn("Nothing was pushed", interaction.sent[-1])


class RoadmapAdvanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_refreshed_approve_advances_the_roadmap_but_a_pause_holds_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
            instance.settings = config(root)
            instance._roadmap_steps = [roadmap.Step("Add a button.", "task", JOB_ID),
                                       roadmap.Step("Refactor the parser.", "task")]
            instance._roadmap_jobs, instance._roadmap_job = {}, None
            instance._roadmap_paused = True
            instance._roadmap_merge = None
            instance._roadmap_channel_id, instance._roadmap_requester_id = 1, 42
            instance._target_channel = lambda channel_id: None

            async def merged(branch: str, base: str, kind: str) -> bool:
                return True

            instance._roadmap_merged = merged
            review_record.save(instance.settings, outcome(worktree))
            approved = jobs.Job(id=JOB_ID, kind="task", title="t", channel_id=1,
                                requester_id=42, log_path=root / "j.log")

            instance.review_decided(approved, "approved")
            self.assertIs(instance._roadmap_job, approved)
            self.assertIs(instance._roadmap_jobs[JOB_ID], approved)
            self.assertEqual(review_record.load_one(instance.settings, JOB_ID)["status"],
                             "approved")

            queued: list[str] = []

            async def queue_step(step: object) -> None:
                queued.append(step.text)

            instance._queue_roadmap_step = queue_step
            if hasattr(bot.MacBuilderBot, "_roadmap_set_paused"):
                # The pause branch adds the early return this pins; without it the
                # next step queues at once and there is nothing to hold.
                await instance._roadmap_tick()
                self.assertEqual(queued, [], "a paused roadmap holds the next job")
                instance._roadmap_paused = False
            await instance._roadmap_tick()
            self.assertEqual(queued, ["Refactor the parser."])


if __name__ == "__main__":
    unittest.main()
