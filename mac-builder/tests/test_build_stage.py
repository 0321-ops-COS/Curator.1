"""The build stage: only when the scheme has no test action, and never a free pass.

The runner is driven through a fake executor, so each case is one exit code:
a passing build, a failing one, a scheme whose test action already built the
app, and a machine without the tools.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import evidence  # noqa: E402
import jobs  # noqa: E402
from freebuff_runner import TaskEnvironment  # noqa: E402


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def settings(tmp: Path) -> SimpleNamespace:
    return SimpleNamespace(
        build_timeout_seconds=5.0, check_timeout_seconds=5.0,
        test_timeout_seconds=5.0, status_timeout_seconds=5.0,
        log_dir=tmp / "logs", repo_dir=tmp, worktree_root=tmp / "wt",
        bot_name="Melisa", authorized_branch="freebuff/ce4b8907c250c30103d8993e",
        xcode_project="ios-app/Hustler.xcodeproj", xcode_scheme="Hustler",
        simulator_name="", swift_jobs=2, xcodebuild_jobs=2, projects=None,
    )


def make_job(tmp: Path) -> jobs.Job:
    return jobs.Job(id="20260101-000000-deadbeef", kind="task", title="fix login",
                    channel_id=1, requester_id=2, log_path=tmp / "j.log")


def make_env(tmp: Path) -> TaskEnvironment:
    return TaskEnvironment(branch="freebuff/deadbeef", worktree=tmp,
                           session="s", base_branch="origin/main")


def result(code: int, *, timed_out: bool = False, lines: list[str] | None = None):
    tail = list(lines or [])
    return jobs.ExecOutcome(code, timed_out, tail, tail)


class BuildStageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.runner = jobs.JobRunner(
            settings(self.tmp), manager=SimpleNamespace(), freebuff=SimpleNamespace(),
            post_job=_noop, post_review=_noop)
        self.job = make_job(self.tmp)
        self.env = make_env(self.tmp)
        self.assertTrue((jobs.SCRIPT_DIR / "build.sh").is_file())

    async def stage(self, outcome, test_ok=None):
        calls: list[list[str]] = []

        async def fake_exec(argv, **_kwargs):
            calls.append(list(argv))
            return outcome

        with mock.patch.object(jobs, "_exec", fake_exec):
            value = await self.runner._run_build_stage(self.job, self.env, test_ok)
        return value, calls

    async def test_a_passing_build_is_the_build_stage(self) -> None:
        (state, tail, ok), calls = await self.stage(result(0, lines=["BUILD SUCCEEDED"]))
        self.assertEqual((state, ok), ("passed", True))
        self.assertEqual(tail, "")
        self.assertEqual(calls[0][:1], ["/bin/bash"])
        self.assertTrue(calls[0][1].endswith("scripts/build.sh"))

    async def test_a_failing_build_keeps_its_last_twenty_lines(self) -> None:
        lines = [f"error: Login.swift:{number}: no such module" for number in range(25)]
        (state, tail, ok), _calls = await self.stage(result(65, lines=lines))
        self.assertEqual(state, "failed (exit 65)")
        self.assertFalse(ok)
        self.assertEqual(len(tail.splitlines()), 20)
        self.assertIn("Login.swift:24", tail)

    async def test_a_scheme_with_a_test_action_skips_the_build(self) -> None:
        (state, tail, ok), calls = await self.stage(result(0), test_ok=True)
        self.assertEqual((state, tail, ok), ("not run", "", None))
        self.assertEqual(calls, [])  # nothing was run, so nothing is claimed

    async def test_a_missing_tool_is_unavailable_not_a_failure(self) -> None:
        (state, tail, ok), _calls = await self.stage(
            result(127, lines=["[build] xcodegen not found."]))
        self.assertIsNone(ok)
        self.assertTrue(state.startswith("unavailable"))
        self.assertIn("xcodegen", tail)

    async def test_no_simulator_is_unavailable(self) -> None:
        (state, _tail, ok), _calls = await self.stage(
            result(2, lines=["[build] no available iPhone simulator."]))
        self.assertIsNone(ok)
        self.assertIn("simulator", state)


class BuildEvidenceTests(unittest.TestCase):
    def outcome(self, **facts: object) -> jobs.ReviewOutcome:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        values = dict(
            job=make_job(root), verdict="ask_user", notes="",
            environment=make_env(root),
            check_state="passed", test_state="unavailable (scheme has no test action)",
            diff_lines=7, changed_files=("ios-app/Hustler/Login.swift",),
            check_command="scripts/check.sh", check_counts="12 passed, 0 failed",
            test_command="scripts/test.sh", build_command="scripts/build.sh")
        values.update(facts)
        return jobs.ReviewOutcome(**values)

    def test_the_verification_block_names_the_build(self) -> None:
        block = evidence.verification_block(
            self.outcome(build_state="passed", build_ok=True))
        self.assertIn("build: scripts/build.sh — passed", block)

    def test_a_skipped_build_is_not_named_in_the_block(self) -> None:
        block = evidence.verification_block(self.outcome(build_state="not run"))
        self.assertNotIn("build:", block)

    def test_a_failed_build_removes_approval_and_shows_its_tail(self) -> None:
        outcome = self.outcome(build_state="failed (exit 65)", build_ok=False,
                               build_tail="error: Concurrency.swift:12:1: missing")
        self.assertFalse(bot.is_approvable(outcome))
        card = {field.name: str(field.value)
                for field in bot.review_embed(SimpleNamespace(), outcome).fields}
        self.assertIn("failed (exit 65)", card["Build"])
        self.assertIn("Concurrency.swift", card["Build"])

    def test_an_unavailable_build_still_allows_approval(self) -> None:
        outcome = self.outcome(build_state="unavailable (xcodegen or xcodebuild missing)",
                               build_ok=None)
        self.assertTrue(bot.is_approvable(outcome))


if __name__ == "__main__":
    unittest.main()
