"""`/plan` is read-only: allowlist, no Job, one saved file, and real inputs."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import planner  # noqa: E402
import project_context as pc  # noqa: E402
from manager import PlanResult, build_plan_messages  # noqa: E402

JOB_ID = "20261030-101500-abcd1234"
PLAN_BODY = "# Task for Dan — make the queue visible\n\n## Evidence\n- `verified`: queue.py has no listing command.\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def make_repo(parent: Path) -> Path:
    path = parent / "Hustler"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "freebuff/abc", str(path)], check=True)
    for entry in ("ios-app/project.yml", "mac-builder/bot.py", "ios-app/RootView.swift"):
        target = path / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    (path / "README").write_text("r\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True

    async def send_message(self, content: str, **_kwargs: object) -> None:
        self.sent.append(content)


class FakeFollowup:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.calls.append({"content": content, **kwargs})
        # Discord closes the attachment after the upload; do the same here so a
        # saved plan does not leave an open handle in the test process.
        attachment = kwargs.get("file")
        if hasattr(attachment, "close"):
            attachment.close()  # type: ignore[union-attr]

    @property
    def last(self) -> str:
        return str(self.calls[-1]["content"])


class FakeInteraction:
    def __init__(self, user_id: int = 42) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.channel_id = 1
        self.message = None


class FakeManager:
    enabled = True

    def __init__(self, body: str = PLAN_BODY) -> None:
        self.body = body
        self.inputs: planner.PlanInputs | None = None

    async def plan(self, inputs: planner.PlanInputs) -> PlanResult:
        self.inputs = inputs
        return PlanResult(text=self.body)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.repo = make_repo(root)
        self.log_dir = root / "logs"
        self.log_dir.mkdir()
        self.ctx = pc.build_context(
            target_dir=self.repo,
            authorized_branch="freebuff/abc",
            self_dir=self.repo,
            project_name="Hustler",
        )

    def settings(self) -> "bot.Settings":
        env = {
            "DISCORD_TOKEN": "token",
            "ALLOWED_USER_IDS": "42",
            "PROJECT_NAME": "Hustler",
            "AUTHORIZED_BRANCH": "freebuff/abc",
            "REPO_DIR": str(self.repo),
            "LOG_DIR": str(self.log_dir),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            return replace(bot.Settings.from_env(), project=self.ctx)

    def fake_bot(self, *, allowed: tuple[int, ...] = (42,), manager: object = None):
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = replace(
            self.settings(), allowed_user_ids=frozenset(allowed)
        )
        instance.manager = manager if manager is not None else FakeManager()
        # A plan must never reach the queue; make any submit an immediate failure.
        submit = mock.AsyncMock(side_effect=AssertionError("a plan created a job"))
        instance.queue = SimpleNamespace(submit=submit, pending_count=0)
        return instance

    def write_log(self, text: str, job_id: str = JOB_ID) -> Path:
        path = self.log_dir / f"{job_id}.log"
        path.write_text(text, encoding="utf-8")
        return path

    def saved_plans(self) -> list[Path]:
        return sorted((self.log_dir / planner.PLAN_DIR_NAME).glob("*.md"))

    async def test_a_plan_is_saved_posted_as_a_file_and_creates_no_job(self) -> None:
        self.write_log("=== Freebuff task ===\nerror: RootView.swift:12 fails to compile\n")
        manager = FakeManager()
        interaction = FakeInteraction()
        instance = self.fake_bot(manager=manager)

        with mock.patch.object(bot.jobs, "make_job") as make_job:
            await instance.cmd_plan(interaction, "make the queue visible")

        make_job.assert_not_called()
        instance.queue.submit.assert_not_awaited()
        self.assertEqual(instance.queue.pending_count, 0)

        plans = self.saved_plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].suffix, ".md")
        saved = plans[0].read_text(encoding="utf-8")
        self.assertIn("make the queue visible", saved)
        self.assertIn("queue.py has no listing command", saved)
        self.assertIn("verified", saved)

        self.assertIsNotNone(interaction.followup.calls[-1].get("file"))
        self.assertIn("no job was created", interaction.followup.last)

    async def test_the_prompt_carries_the_project_block_and_recent_failures(self) -> None:
        older = self.write_log("all good\n", "20261029-090000-aaaaaaaa")
        time.sleep(0.02)
        self.write_log(
            "=== Freebuff task ===\nerror: RootView.swift:12 fails to compile\n",
            JOB_ID,
        )
        self.assertLess(older.stat().st_mtime, (self.log_dir / f"{JOB_ID}.log").stat().st_mtime)
        manager = FakeManager()

        await self.fake_bot(manager=manager).cmd_plan(FakeInteraction(), "fix the build")

        self.assertIsNotNone(manager.inputs)
        inputs = manager.inputs
        assert inputs is not None
        self.assertTrue(any(JOB_ID in line for line in inputs.recent))
        self.assertTrue(any("fails to compile" in line for line in inputs.recent))
        self.assertTrue(any("ios-app/" in entry for entry in inputs.file_map))

        messages = build_plan_messages(inputs)
        system, user = messages[0]["content"], messages[1]["content"]
        self.assertIn("Hustler", system)
        self.assertIn("freebuff/abc", system)
        self.assertIn("fails to compile", user)
        self.assertIn("fix the build", user)

    async def test_an_unallowlisted_user_gets_no_plan(self) -> None:
        manager = FakeManager()
        interaction = FakeInteraction(user_id=7)

        await self.fake_bot(allowed=(42,), manager=manager).cmd_plan(
            interaction, "make the queue visible"
        )

        self.assertIn("allowlist", interaction.response.sent[0])
        self.assertIsNone(manager.inputs)
        self.assertFalse(self.saved_plans())

    async def test_an_unconfigured_manager_says_what_to_set(self) -> None:
        interaction = FakeInteraction()
        instance = self.fake_bot(manager=SimpleNamespace(enabled=False))

        await instance.cmd_plan(interaction, "make the queue visible")

        text = interaction.followup.last
        self.assertTrue(text.strip())
        self.assertIn("MANAGER_API_KEY", text)
        self.assertIn("not configured", text)
        self.assertFalse(self.saved_plans())

    async def test_the_manager_failing_names_the_failure_and_saves_nothing(self) -> None:
        class Broken:
            enabled = True

            async def plan(self, inputs: planner.PlanInputs) -> str:
                raise bot.ManagerUnavailable("manager request failed: ReadTimeout")

        interaction = FakeInteraction()

        await self.fake_bot(manager=Broken()).cmd_plan(interaction, "fix the build")

        self.assertIn("ReadTimeout", interaction.followup.last)
        self.assertFalse(self.saved_plans())


    async def test_a_token_in_a_log_never_reaches_the_prompt_or_the_plan(self) -> None:
        token = "ghp_" + "0123456789abcdefghij"
        api_key = "sk-" + "abcdefghijklmnopqrstu"
        self.write_log(
            f"DISCORD_TOKEN={token}\n"
            f"Authorization: Bearer {api_key}\n"
            f"error: token {token} was rejected\n"
        )

        class Echoing(FakeManager):
            async def plan(self, inputs: planner.PlanInputs) -> PlanResult:
                self.inputs = inputs
                # A model may quote its material; the saved file must stay clean.
                return PlanResult(text=PLAN_BODY + "\n" + "\n".join(inputs.recent))

        manager = Echoing()
        await self.fake_bot(manager=manager).cmd_plan(FakeInteraction(), "fix the build")

        assert manager.inputs is not None
        for message in build_plan_messages(manager.inputs):
            self.assertNotIn(token, message["content"])
            self.assertNotIn(api_key, message["content"])
        saved = self.saved_plans()[0].read_text(encoding="utf-8")
        self.assertNotIn(token, saved)
        self.assertNotIn(api_key, saved)
        self.assertIn("[redacted]", saved)


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_known_gaps_reads_only_the_two_gap_sections(self) -> None:
        readme = self.root / "README.md"
        readme.write_text(
            "\n".join(
                [
                    "# mac-builder",
                    "- ignored before any section",
                    "## What is not verified here",
                    "- **Xcode**: no toolchain here.",
                    "- **A live Discord login**: needs your token.",
                    "## Limitations worth knowing",
                    "- Buttons live in memory.",
                    "## Tests",
                    "- not a gap",
                ]
            )
        )
        gaps = planner.known_gaps(readme)
        self.assertEqual(len(gaps), 3)
        self.assertIn("Xcode", gaps[0])
        self.assertIn("Buttons live in memory", gaps[2])
        self.assertNotIn("not a gap", gaps)

    def test_known_gaps_is_empty_without_a_readme(self) -> None:
        self.assertEqual(planner.known_gaps(self.root / "missing.md"), ())

    def test_recent_failures_is_newest_first_and_ignores_other_files(self) -> None:
        log_dir = self.root / "logs"
        log_dir.mkdir()
        old = log_dir / "20261029-090000-aaaaaaaa.log"
        old.write_text("job one\n")
        new = log_dir / "20261030-101500-bbbbbbbb.log"
        new.write_text("error: xcodebuild failed\n")
        (log_dir / "notes.log").write_text("error: not a job log\n")
        now = time.time()
        os.utime(old, (now - 100, now - 100))
        os.utime(new, (now, now))

        entries = planner.recent_failures(log_dir)
        self.assertEqual(len(entries), 2)
        self.assertIn("bbbbbbbb", entries[0])
        self.assertIn("xcodebuild failed", entries[0])
        self.assertIn("job one", entries[1])
        self.assertFalse(any("not a job log" in entry for entry in entries))

    def test_a_log_without_errors_still_reports_its_last_line(self) -> None:
        log_dir = self.root / "logs"
        log_dir.mkdir()
        (log_dir / f"{JOB_ID}.log").write_text("started\nall checks passed\n")
        entries = planner.recent_failures(log_dir)
        self.assertEqual(len(entries), 1)
        self.assertIn("all checks passed", entries[0])

    def test_write_plan_creates_the_folder_and_never_overwrites(self) -> None:
        from datetime import datetime

        when = datetime(2026, 10, 30, 10, 15, 0)
        first = planner.write_plan(self.root / "logs", "one", created=when)
        second = planner.write_plan(self.root / "logs", "two", created=when)
        self.assertEqual(first.parent.name, planner.PLAN_DIR_NAME)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(encoding="utf-8"), "one")
        self.assertEqual(second.read_text(encoding="utf-8"), "two")

    def test_plan_document_names_the_goal_once_and_keeps_the_body(self) -> None:
        from datetime import datetime

        body = "# Task for Dan — make the queue visible\n\n## Evidence\n- `assumed`: x\n"
        text = planner.plan_document(
            "make the queue visible",
            body,
            created=datetime(2026, 10, 30, 10, 15, 0),
        )
        self.assertEqual(text.count("# Task for Dan"), 1)
        self.assertIn("2026-10-30 10:15", text)
        self.assertIn("`assumed`: x", text)

    def test_file_map_marks_her_own_folder_as_protected(self) -> None:
        repo = self.root / "repo"
        for entry in ("ios-app/RootView.swift", "mac-builder/bot.py"):
            target = repo / entry
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n")
        entries = planner.file_map(repo)
        self.assertTrue(any("mac-builder/" in entry and "protected" in entry for entry in entries))
        self.assertTrue(any("ios-app/" in entry for entry in entries))


    def test_a_plan_cut_off_at_the_token_limit_says_so(self) -> None:
        from datetime import datetime

        when = datetime(2026, 10, 30, 10, 15, 0)
        body = "# Task for Dan — x\n\n## Items (priority order)\n1. do the first thing"
        cut = planner.plan_document("x", body, created=when, truncated=True)
        self.assertIn("cut off at the model's token limit", cut)
        self.assertIn("Plan truncated", cut)
        self.assertLess(cut.index("cut off"), cut.index("## Items"))
        self.assertTrue(cut.rstrip().endswith("before it finished."))
        whole = planner.plan_document("x", body, created=when)
        self.assertNotIn("truncated", whole.lower())

    def test_finish_reason_length_is_the_truncation_signal(self) -> None:
        from manager import _reply_and_truncation

        text, truncated = _reply_and_truncation(
            {"choices": [{"message": {"content": "half"}, "finish_reason": "length"}]}
        )
        self.assertEqual(text, "half")
        self.assertTrue(truncated)
        _, stopped = _reply_and_truncation(
            {"choices": [{"message": {"content": "whole"}, "finish_reason": "stop"}]}
        )
        self.assertFalse(stopped)

    def test_a_clipped_prompt_tells_the_model_material_is_missing(self) -> None:
        from manager import MAX_PLAN_CHARS

        inputs = planner.PlanInputs(
            goal="x",
            project_block="",
            recent=tuple("error: " + "y" * 200 for _ in range(400)),
            file_map=(),
            known_gaps=(),
        )
        user = build_plan_messages(inputs)[1]["content"]
        self.assertIn("truncated to fit this request", user)
        self.assertLessEqual(len(user), MAX_PLAN_CHARS + 400)

    def test_a_goal_cannot_choose_the_plan_filename(self) -> None:
        from datetime import datetime

        when = datetime(2026, 10, 30, 10, 15, 0)
        goal = "../../etc/passwd\nrm -rf /"
        text = planner.plan_document(goal, "## Evidence\n- `assumed`: x", created=when)
        path = planner.write_plan(self.root / "logs", text, created=when)
        self.assertEqual(
            path.parent.resolve(),
            (self.root / "logs" / planner.PLAN_DIR_NAME).resolve(),
        )
        self.assertRegex(path.name, r"^\d{8}-\d{6}(-\d+)?\.md$")
        self.assertIn("rm -rf /", path.read_text(encoding="utf-8"))


if __name__ == "__main__":

    unittest.main()
