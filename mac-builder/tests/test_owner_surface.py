"""The owner surface: plans on jobs, /handoff, digests, /jobs, /tidy, code state."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import digest  # noqa: E402
import handoff  # noqa: E402
import health  # noqa: E402
import job_index  # noqa: E402
import job_plan  # noqa: E402
import jobs  # noqa: E402
import manager  # noqa: E402
import questions  # noqa: E402
import tidy  # noqa: E402

SHEET_JOB = """Fix the scroll jump in the list.

Acceptance:
- the list does not jump when scrolling
- `python -m pytest tests -q` passes

Out of scope:
- the settings screen
"""


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
        self.sent: list[str] = []
        self.files: list[object] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.sent.append(content or "")
        if kwargs.get("file") is not None:
            self.files.append(kwargs["file"])


class FakeInteraction:
    def __init__(self, user_id: int = 42) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.channel_id = 1


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str, **_kwargs: object) -> None:
        self.sent.append(content)


def allowed_bot(**extra: object):
    instance = SimpleNamespace(
        settings=SimpleNamespace(log_dir=Path("."), bot_name="Melisa",
                                 allowed_user_ids=frozenset({42})),
        **extra,
    )

    async def require_allowed(interaction) -> bool:
        return interaction.user.id in instance.settings.allowed_user_ids

    instance.require_allowed = require_allowed
    return instance


# ------------------------------------------------------------------ job plans
class PlanTests(unittest.TestCase):
    def test_a_sheet_job_carries_its_acceptance_and_scope(self) -> None:
        brief = job_plan.parse(SHEET_JOB)

        self.assertEqual(brief.goal, "Fix the scroll jump in the list.")
        self.assertEqual(len(brief.acceptance), 2)
        self.assertIn("pytest", brief.acceptance[1])
        self.assertEqual(brief.out_of_scope, ("the settings screen",))
        self.assertFalse(brief.empty)

    def test_a_goal_alone_is_not_a_plan(self) -> None:
        self.assertEqual(job_plan.for_text("Fix the button colour."), "")
        self.assertEqual(job_plan.for_text(""), "")
        self.assertTrue(job_plan.parse("").empty)

    def test_a_one_line_acceptance_heading_keeps_its_item(self) -> None:
        brief = job_plan.parse("Rename the tab.\nAcceptance: the tab says Notes")

        self.assertEqual(brief.acceptance, ("the tab says Notes",))

    def test_the_rendered_plan_names_each_part(self) -> None:
        text = job_plan.for_text(SHEET_JOB)

        self.assertIn("goal: Fix the scroll jump", text)
        self.assertIn("acceptance: ", text)
        self.assertIn("out of scope: the settings screen", text)
        self.assertEqual(job_plan.describe(job_plan.parse(SHEET_JOB)),
                         "2 acceptance line(s), 1 out-of-scope line(s)")
        self.assertEqual(job_plan.describe(job_plan.parse("x")), "no plan")

    def test_the_plan_is_capped(self) -> None:
        long_job = "Goal.\nAcceptance:\n" + "\n".join("- " + "a" * 500 for _ in range(20))

        self.assertLessEqual(len(job_plan.for_text(long_job)), job_plan.MAX_RENDER)
        self.assertEqual(len(job_plan.parse(long_job).acceptance), job_plan.MAX_ITEMS)


class JobWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(log_dir=Path(self._tmp.name), projects=None)

    def test_make_job_stores_the_plan_it_derived(self) -> None:
        job = jobs.make_job(self.settings, kind="task", title="t", channel_id=1,
                            requester_id=2, text=SHEET_JOB)

        self.assertIn("out of scope: the settings screen", job.plan)

    def test_a_job_without_a_plan_says_nothing(self) -> None:
        job = jobs.make_job(self.settings, kind="task", title="t", channel_id=1,
                            requester_id=2, text="Fix the button colour.")

        self.assertEqual(job.plan, "")

    def test_the_review_prompt_reads_the_plan(self) -> None:
        brief = manager.ReviewBrief(
            task="t", diff="+x\n", diff_lines=1, status="", changed_files=["a.py"],
            check_state="passed", check_tail="", test_state="not run", test_tail="",
            plan="acceptance: pytest passes")

        messages = manager.build_review_messages(brief)

        self.assertIn("PLAN FOR THIS JOB", messages[1]["content"])
        self.assertIn("acceptance: pytest passes", messages[1]["content"])

    def test_the_review_prompt_says_so_when_there_is_no_plan(self) -> None:
        brief = manager.ReviewBrief(
            task="t", diff="", diff_lines=0, status="", changed_files=[],
            check_state="not run", check_tail="", test_state="not run", test_tail="")

        self.assertIn("(no plan recorded for this job)",
                      manager.build_review_messages(brief)[1]["content"])


# -------------------------------------------------------------- running code
class CodeStateTests(unittest.TestCase):
    def fake(self, answers: dict[str, str]):
        def git(_root, *args: str) -> str:
            return answers.get(" ".join(args), "")

        return git

    def test_behind_main_is_named_with_the_restart(self) -> None:
        state = health.code_state("/repo", git=self.fake({
            "rev-parse --short HEAD": "abc1234",
            "rev-parse --short origin/main": "def5678",
            "rev-list --count HEAD..origin/main": "3",
        }))

        self.assertTrue(state.known)
        self.assertTrue(state.stale)
        report = health.report(health.Snapshot(code=state))
        self.assertIn("code: running abc1234 · main def5678 (3 commit(s) behind", report)

    def test_up_to_date_says_nothing_about_a_restart(self) -> None:
        state = health.code_state("/repo", git=self.fake({
            "rev-parse --short HEAD": "abc1234",
            "rev-parse --short origin/main": "abc1234",
            "rev-list --count HEAD..origin/main": "0",
        }))

        self.assertFalse(state.stale)
        report = health.report(health.Snapshot(code=state))
        self.assertIn("code: running abc1234 · main abc1234", report)
        self.assertNotIn("restart", report)

    def test_a_missing_git_says_unknown_instead_of_up_to_date(self) -> None:
        state = health.code_state("/repo", git=self.fake({}))

        self.assertFalse(state.known)
        self.assertNotIn("code:", health.report(health.Snapshot(code=state)))
        self.assertFalse(health.code_state(None).known)


# -------------------------------------------------------------------- digest
def reviewed_job(**extra: object):
    payload = dict(
        id="20261104-140735-435f989c", kind="task", title="Fix the scroll jump",
        channel_id=1, requester_id=2, log_path=Path("/tmp/x.log"), project="hustler",
        branch="freebuff/deadbeef", status="awaiting_review",
    )
    payload.update(extra)
    return jobs.Job(**payload)


def outcome_for(job, **extra):
    payload = dict(
        job=job, verdict="approve", notes="The list no longer jumps.",
        environment=SimpleNamespace(branch="freebuff/deadbeef"),
        check_state="passed", test_state="not run", diff_lines=12,
        changed_files=("ios-app/List.swift", "ios-app/ListTests.swift"),
    )
    payload.update(extra)
    return SimpleNamespace(**payload)


class DigestTests(unittest.TestCase):
    def test_a_finished_job_says_what_changed_and_what_is_needed(self) -> None:
        job = reviewed_job()
        text = digest.build(job, outcome_for(job), roadmap_next="2 job(s) left — Rename")

        self.assertIn("`20261104-140735-435f989c`", text)
        self.assertIn("changed: 2 file(s), 12 line(s)", text)
        self.assertIn("check: passed · test: not run", text)
        self.assertIn("needs you: Approve or reject", text)
        self.assertIn("next: 2 job(s) left", text)
        self.assertIn("nothing was re-run", text)

    def test_a_blocked_change_needs_nothing_from_the_owner(self) -> None:
        job = reviewed_job()
        text = digest.build(job, outcome_for(job, verdict="blocked",
                                             notes="Touches project.yml."))

        self.assertIn("needs you: Nothing", text)
        self.assertIn("Touches project.yml", text)

    def test_a_self_build_says_it_needs_a_restart(self) -> None:
        job = reviewed_job(kind="dan")
        text = digest.build(job, outcome_for(job))

        self.assertIn("restart: this edits my own code", text)

    def test_a_script_job_has_no_review_to_quote(self) -> None:
        job = reviewed_job(kind="check", status="done")

        text = digest.build(job)

        self.assertIn("outcome: done", text)
        self.assertFalse(digest.is_builder(job))
        self.assertTrue(digest.is_builder(reviewed_job()))

    def test_the_digest_stays_under_discords_limit(self) -> None:
        job = reviewed_job()
        text = digest.build(job, outcome_for(job, notes="n" * 4000),
                            roadmap_next="x" * 400)

        self.assertLessEqual(len(text), digest.MAX_LINE)


# ---------------------------------------------------------------- job index
class JobIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(log_dir=Path(self._tmp.name))

    def test_a_finished_job_is_recorded_once_and_read_newest_first(self) -> None:
        first = reviewed_job(id="20261104-100000-aaaaaaaa")
        second = reviewed_job(id="20261104-110000-bbbbbbbb", status="approved")

        job_index.record(self.settings, first, now=100.0)
        job_index.record(self.settings, second, now=200.0)
        job_index.record(self.settings, second, outcome=outcome_for(second), now=201.0)
        rows = job_index.recent(self.settings)

        self.assertEqual([row["id"] for row in rows],
                         ["20261104-110000-bbbbbbbb", "20261104-100000-aaaaaaaa"])
        self.assertEqual(rows[0]["status"], "approve", "the newer line wins")
        self.assertEqual(len(rows), 2, "one job is one row")

    def test_a_broken_line_is_skipped_not_raised(self) -> None:
        path = job_index.path_for(self.settings.log_dir)
        path.write_text('{"id": "ok", "kind": "task", "status": "done", "finished": 1}\n'
                        'not json\n\n', encoding="utf-8")

        self.assertEqual(len(job_index.recent(self.settings)), 1)

    def test_the_listing_names_the_job_and_the_branch(self) -> None:
        job_index.record(self.settings, reviewed_job(), now=5.0)

        text = job_index.render(job_index.recent(self.settings))

        self.assertIn("Recent jobs", text)
        self.assertIn("freebuff/deadbeef", text)
        self.assertIn("Fix the scroll jump", text)
        self.assertIn("No finished job", job_index.render([]))

    def test_no_log_dir_records_nothing(self) -> None:
        self.assertIsNone(job_index.record(SimpleNamespace(), reviewed_job()))
        self.assertEqual(job_index.recent(SimpleNamespace()), [])


# --------------------------------------------------------------------- tidy
class TidyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log_dir, self.worktree_root = self.root / "logs", self.root / "wt"
        self.log_dir.mkdir()
        self.worktree_root.mkdir()
        self.settings = SimpleNamespace(log_dir=self.log_dir,
                                       worktree_root=self.worktree_root)

    def old(self, path: Path, days: float = 30.0) -> Path:
        path.write_text("x", encoding="utf-8")
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_only_old_logs_are_offered_for_deletion(self) -> None:
        stale = self.old(self.log_dir / "20260101-000000-aaaaaaaa.log")
        fresh = self.log_dir / "20261104-000000-bbbbbbbb.log"
        fresh.write_text("x", encoding="utf-8")
        busy = self.old(self.log_dir / "20260101-000000-cccccccc.log")
        (self.worktree_root / "leftover").mkdir()
        audit_dir = self.worktree_root / "audit-20260101"
        audit_dir.mkdir()
        self.old(audit_dir / "marker.txt")
        stamp = time.time() - 30 * 86400
        os.utime(audit_dir, (stamp, stamp))

        state = tidy.scan(self.settings, busy_id="20260101-000000-cccccccc")

        self.assertEqual(state.old_logs, [stale])
        self.assertEqual(state.stray_worktrees, ["audit-20260101", "leftover"])
        self.assertEqual(state.audit_dirs, ["audit-20260101"])
        text = tidy.report(state)
        self.assertIn("old logs (1", text)
        self.assertIn("only old logs are deleted", text)
        self.assertIn("git worktree remove", text)

    def test_the_clean_deletes_logs_inside_log_dir_and_nothing_else(self) -> None:
        inside = self.old(self.log_dir / "20260101-000000-aaaaaaaa.log")
        outside = self.old(self.root / "precious.log")
        record = self.log_dir / "20260101-000000-aaaaaaaa.review.json"
        record.write_text("{}", encoding="utf-8")

        removed, failed = tidy.clean_logs([inside, outside, record], self.log_dir)

        self.assertEqual(removed, [inside.name])
        self.assertEqual(sorted(failed), sorted([str(outside), str(record)]))
        self.assertFalse(inside.exists())
        self.assertTrue(outside.exists(), "a path outside LOG_DIR is never deleted")
        self.assertTrue(record.exists(), "a review record is not a log")

    def test_an_empty_scan_reports_nothing_to_do(self) -> None:
        state = tidy.scan(self.settings)

        self.assertTrue(state.nothing_to_do)
        self.assertIn("old logs: none", tidy.report(state))


# ----------------------------------------------------------------- handoff
class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log_dir, self.worktree_root = self.root / "logs", self.root / "wt"
        self.log_dir.mkdir()
        self.worktree_root.mkdir()

    def make_bot(self, *, running=None, paused=False):
        instance = SimpleNamespace(
            settings=SimpleNamespace(
                log_dir=self.log_dir, worktree_root=self.worktree_root,
                repo_dir=self.root, authorized_branch="freebuff/base",
                project=SimpleNamespace(project_id="hustler", project_name="Hustler",
                                        target_repo="git@github.com:o/Hustler.git"),
                bot_name="Melisa",
            ),
            queue=SimpleNamespace(snapshot=lambda: {
                "running": running, "pending": 1}),
            manager=SimpleNamespace(enabled=True),
            _started_at=time.time(),
            _sheet_draft=None,
            _roadmap_steps=[],
            _roadmap_paused=paused,
            _roadmap_job=None,
            code_root=None,
        )
        return instance

    def test_the_handoff_names_the_state_and_the_next_action(self) -> None:
        instance = self.make_bot()
        instance._roadmap_steps = [SimpleNamespace(job_id="", text="Rename the tab")]

        state = handoff.collect(instance)
        text = handoff.text(state)

        self.assertIn("# Session handoff", text)
        self.assertIn("## North star", text)
        self.assertIn("`hustler` (Hustler)", text)
        self.assertIn("roadmap: 1 job(s) left", text)
        self.assertIn("next job: Rename the tab", text)
        self.assertIn("## Not verified", text)
        self.assertIn("Not checked for this handoff", text)
        self.assertIn("next action", text.lower())
        self.assertIn("queues its next job", state.next_action)

    def test_a_running_job_is_what_the_handoff_waits_on(self) -> None:
        instance = self.make_bot(running={
            "id": "20261104-140735-435f989c", "kind": "task", "title": "t",
            "started": 0.0, "elapsed": 61.0})

        state = handoff.collect(instance)

        self.assertIn("`20261104-140735-435f989c`", state.running_job)
        self.assertIn("Wait for `20261104-140735-435f989c`", state.next_action)

    def test_a_paused_roadmap_is_the_next_action(self) -> None:
        instance = self.make_bot(paused=True)
        instance._roadmap_steps = [SimpleNamespace(job_id="", text="Next")]

        state = handoff.collect(instance)

        self.assertIn("resume the roadmap", state.next_action.lower())

    def test_writing_it_needs_no_discord_and_creates_one_file(self) -> None:
        state = handoff.collect(self.make_bot())

        path = handoff.write(self.log_dir, handoff.text(state))

        self.assertEqual(path.name, handoff.HANDOFF_FILE)
        self.assertEqual([item.name for item in self.log_dir.iterdir()],
                         [handoff.HANDOFF_FILE])
        self.assertIn("Session handoff", path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ commands
class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log_dir, self.worktree_root = self.root / "logs", self.root / "wt"
        self.log_dir.mkdir()
        self.worktree_root.mkdir()

    def test_the_three_commands_exist_on_the_bot(self) -> None:
        import bot

        for name in ("cmd_handoff", "cmd_jobs", "cmd_tidy", "cmd_health"):
            self.assertTrue(callable(getattr(bot.MacBuilderBot, name, None)), name)

    async def test_handoff_writes_the_file_and_answers_ephemerally(self) -> None:
        instance = allowed_bot()
        instance.settings.log_dir = self.log_dir
        instance.settings.worktree_root = self.worktree_root
        instance.settings.repo_dir = self.root
        instance.settings.authorized_branch = "freebuff/base"
        instance.settings.project = None
        instance.queue = SimpleNamespace(snapshot=lambda: {"running": None, "pending": 0})
        instance.manager = SimpleNamespace(enabled=False)
        instance._started_at = time.time()
        instance._sheet_draft = None
        instance._roadmap_steps = []
        instance._roadmap_paused = False
        instance._roadmap_job = None
        instance.code_root = None
        interaction = FakeInteraction()

        await handoff.command(instance, interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertTrue((self.log_dir / handoff.HANDOFF_FILE).is_file())
        self.assertIn("Handoff written", interaction.followup.sent[0])

    async def test_jobs_lists_what_the_index_holds(self) -> None:
        instance = allowed_bot()
        instance.settings.log_dir = self.log_dir
        job_index.record(instance.settings, reviewed_job(), now=1.0)
        interaction = FakeInteraction()

        await job_index.command(instance, interaction)

        self.assertIn("freebuff/deadbeef", interaction.followup.sent[0])

    async def test_tidy_reports_and_only_deletes_when_the_button_is_pressed(self) -> None:
        instance = allowed_bot()
        instance.settings.log_dir = self.log_dir
        instance.settings.worktree_root = self.worktree_root
        instance.queue = SimpleNamespace(running=None)
        stale = self.log_dir / "20260101-000000-aaaaaaaa.log"
        stale.write_text("x", encoding="utf-8")
        stamp = time.time() - 30 * 86400
        os.utime(stale, (stamp, stamp))
        interaction = FakeInteraction()

        await tidy.command(instance, interaction)

        self.assertTrue(stale.exists(), "the scan deletes nothing on its own")
        self.assertIn("old logs (1", interaction.followup.sent[0])


# ------------------------------------------------------- questions, roadmap
class EscalatedAdviceTests(unittest.TestCase):
    def test_advice_for_an_escalated_question_says_it_is_the_owners_call(self) -> None:
        runner = object.__new__(jobs.JobRunner)
        runner.settings = SimpleNamespace(bot_name="Melisa")
        question = questions.Question(
            number=1, total=1, text="Should I delete docs/old.md?",
            options=[questions.Option("Yes", ""), questions.Option("No", "")])
        result = manager.AnswerResult(answer="2", confidence="medium",
                                      reason="Nothing references it.")

        plain = runner._suggestion_line(question, result)
        escalated = runner._suggestion_line(question, result, escalated=True)

        self.assertIn("(suggestion only, you decide)", plain)
        self.assertNotIn("your call", plain)
        self.assertIn("This one is your call, not my answer.", escalated)


class RoadmapLineTests(unittest.TestCase):
    def test_the_next_sheet_job_is_named(self) -> None:
        import bot

        instance = object.__new__(bot.MacBuilderBot)
        instance._roadmap_steps = [
            SimpleNamespace(job_id="done-1", text="First"),
            SimpleNamespace(job_id="", text="Rename the tab to Notes"),
        ]

        self.assertEqual(instance._roadmap_next_line(),
                         "1 job(s) left — Rename the tab to Notes")
        instance._roadmap_steps = [SimpleNamespace(job_id="done-1", text="First")]
        self.assertEqual(instance._roadmap_next_line(), "")


class DigestPostTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_digest_is_posted_once_and_indexed(self) -> None:
        import bot

        with tempfile.TemporaryDirectory() as tmp:
            channel = FakeChannel()
            instance = object.__new__(bot.MacBuilderBot)
            instance.settings = SimpleNamespace(log_dir=Path(tmp), bot_name="Melisa",
                                                allowed_user_ids=frozenset({42}))
            instance._roadmap_steps = []
            instance._target_channel = lambda channel_id: channel
            job = reviewed_job()

            await instance._post_digest(job, outcome_for(job))

            self.assertEqual(len(channel.sent), 1)
            self.assertIn("digest", channel.sent[0])
            self.assertIn("needs you: Approve or reject", channel.sent[0])
            self.assertEqual(len(job_index.recent(instance.settings)), 1)

    async def test_a_failing_send_never_raises(self) -> None:
        import bot

        class Boom:
            async def send(self, *_a, **_k):
                raise RuntimeError("discord is down")

        instance = object.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(log_dir=Path("/tmp"), bot_name="Melisa")
        instance._roadmap_steps = []
        instance._target_channel = lambda channel_id: Boom()

        await instance._post_digest(reviewed_job())  # must not raise


if __name__ == "__main__":
    unittest.main()
