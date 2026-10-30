"""What /dan may change, what it may push, and the checks it runs on itself."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import jobs  # noqa: E402
import project_context  # noqa: E402
import rules  # noqa: E402
from freebuff_runner import (  # noqa: E402
    FreebuffError,
    FreebuffRunner,
    GitRepo,
    TaskEnvironment,
)

DAN_GUARDRAIL_TESTS = (
    "mac-builder/tests/test_rules.py",
    "mac-builder/tests/test_push_guard.py",
    "mac-builder/tests/test_dan.py",
    "mac-builder/tests/test_approve_recheck.py",
)


class DanPathTests(unittest.TestCase):
    def test_a_dan_job_may_edit_the_bot_code(self) -> None:
        for path in (
            "mac-builder/bot.py",
            "mac-builder/jobs.py",
            "mac-builder/project_context.py",
            "mac-builder/README.md",
            "mac-builder/tests/test_command_sync.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(rules.is_protected_path(path, dan=True))
        self.assertEqual(
            rules.evaluate_change(
                diff_lines=10,
                changed_paths=["mac-builder/bot.py"],
                max_diff_lines=100,
                dan=True,
            ),
            [],
        )

    def test_a_dan_job_cannot_edit_the_guardrail_modules(self) -> None:
        for path in ("mac-builder/rules.py", "mac-builder/freebuff_runner.py"):
            with self.subTest(path=path):
                self.assertTrue(rules.is_protected_path(path, dan=True))
                violations = rules.evaluate_change(
                    diff_lines=4,
                    changed_paths=[path],
                    max_diff_lines=100,
                    dan=True,
                )
                self.assertEqual(len(violations), 1)
                self.assertEqual(violations[0].code, "protected_path")
                self.assertIn("guardrail", violations[0].detail.lower())

    def test_a_dan_job_cannot_edit_the_guardrail_tests(self) -> None:
        for path in DAN_GUARDRAIL_TESTS:
            with self.subTest(path=path):
                violations = rules.evaluate_change(
                    diff_lines=2,
                    changed_paths=[path],
                    max_diff_lines=100,
                    dan=True,
                )
                self.assertEqual(len(violations), 1)
                self.assertIn("this test pins", violations[0].detail)

    def test_a_dan_job_still_cannot_touch_env_github_or_signing(self) -> None:
        for path in (
            "mac-builder/.env",
            ".env.local",
            ".github/workflows/ci.yml",
            "ios-app/project.yml",
            "ios-app/Foo.xcconfig",
            "signing/dev.p12",
        ):
            with self.subTest(path=path):
                self.assertTrue(rules.is_protected_path(path, dan=True))

    def test_a_task_job_still_cannot_edit_mac_builder(self) -> None:
        self.assertTrue(rules.is_protected_path("mac-builder/bot.py"))
        violations = rules.evaluate_change(
            diff_lines=3,
            changed_paths=["mac-builder/bot.py"],
            max_diff_lines=100,
        )
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].code, "protected_path")

    def test_a_dan_job_cannot_change_the_allowlist_or_approve_code(self) -> None:
        diff = (
            "diff --git a/mac-builder/bot.py b/mac-builder/bot.py\n"
            "--- a/mac-builder/bot.py\n"
            "+++ b/mac-builder/bot.py\n"
            "@@ -1,2 +1,3 @@\n"
            "     async def on_message(self, message):\n"
            "+        if message.author.id in self.settings.allowed_user_ids:\n"
            "+            await self.approve_and_push()\n"
        )
        violations = rules.evaluate_change(
            diff_lines=2,
            changed_paths=["mac-builder/bot.py"],
            max_diff_lines=100,
            dan=True,
            diff=diff,
        )
        self.assertEqual(len(violations), 2)
        codes = {violation.code for violation in violations}
        self.assertEqual(codes, {"dan_guardrail"})
        for violation in violations:
            self.assertIn("allowlist or the Approve code", violation.detail)
            self.assertIn("normal PR", violation.detail)

    def test_a_dan_diff_that_leaves_the_guardrail_lines_alone_passes(self) -> None:
        diff = (
            "diff --git a/mac-builder/bot.py b/mac-builder/bot.py\n"
            "--- a/mac-builder/bot.py\n"
            "+++ b/mac-builder/bot.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def split_message(text):\n"
            "+    \"\"\"Split a long reply into Discord-sized chunks.\"\"\"\n"
        )
        self.assertEqual(
            rules.evaluate_change(
                diff_lines=1,
                changed_paths=["mac-builder/bot.py"],
                max_diff_lines=100,
                dan=True,
                diff=diff,
            ),
            [],
        )


class DanBranchTests(unittest.TestCase):
    def test_only_dan_branches_are_pushed_from_a_dan_job(self) -> None:
        branch = "dan/20261030-120000-deadbeef"
        self.assertTrue(rules.is_dan_branch(branch))
        self.assertEqual(rules.validate_push_branch(branch, kind="dan"), branch)
        with self.assertRaises(ValueError):
            rules.validate_push_branch(branch)

    def test_a_dan_job_cannot_push_a_freebuff_branch(self) -> None:
        with self.assertRaises(ValueError):
            rules.validate_push_branch("freebuff/deadbeef", kind="dan")
        with self.assertRaises(ValueError):
            rules.validate_push_branch("refs/heads/freebuff/deadbeef", kind="dan")

    def test_main_is_blocked_for_both_kinds(self) -> None:
        for kind in ("task", "dan"):
            for branch in ("main", "master", "refs/heads/main", "HEAD"):
                with self.subTest(kind=kind, branch=branch):
                    with self.assertRaises(ValueError):
                        rules.validate_push_branch(branch, kind=kind)

    def test_a_lookalike_prefix_is_not_a_dan_branch(self) -> None:
        for branch in ("dan/", "dan/../x", "dan/a/b", "dans/x", "freebuff/x"):
            with self.subTest(branch=branch):
                self.assertFalse(rules.is_dan_branch(branch))


class DanFetchTests(unittest.TestCase):
    """A dan branch must start from a freshly fetched origin/main."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        self.other = root / "other"
        self.repo = root / "repo"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)

        subprocess.run(["git", "init", "-q", str(self.other)], check=True)
        self._git(self.other, "config", "user.email", "test@example.com")
        self._git(self.other, "config", "user.name", "Test")
        (self.other / "file.txt").write_text("base\n", encoding="utf-8")
        self._git(self.other, "add", "file.txt")
        self._git(self.other, "commit", "-qm", "base")
        self._git(self.other, "branch", "-m", "main")
        self._git(self.other, "remote", "add", "origin", str(self.remote))
        self._git(self.other, "push", "-q", "origin", "main")

        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.repo)], check=True)
        self._git(self.repo, "config", "user.email", "test@example.com")
        self._git(self.repo, "config", "user.name", "Test")

    def _git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True
        )

    def test_fetch_origin_main_moves_the_base_forward(self) -> None:
        (self.other / "file.txt").write_text("newer\n", encoding="utf-8")
        self._git(self.other, "commit", "-qam", "newer")
        self._git(self.other, "push", "-q", "origin", "main")
        newer = self._git(self.other, "rev-parse", "HEAD").stdout.strip()

        stale = self._git(self.repo, "rev-parse", "origin/main").stdout.strip()
        self.assertNotEqual(stale, newer)

        asyncio.run(GitRepo(self.repo).fetch_origin_main())
        fetched = self._git(self.repo, "rev-parse", "origin/main").stdout.strip()
        self.assertEqual(fetched, newer)

    def test_a_fetch_without_a_remote_fails_loudly(self) -> None:
        lonely = Path(self.tmp.name) / "lonely"
        subprocess.run(["git", "init", "-q", str(lonely)], check=True)

        with self.assertRaises(FreebuffError) as caught:
            asyncio.run(GitRepo(lonely).fetch_origin_main())
        self.assertIn("could not fetch origin main", str(caught.exception))


class DanPushGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.repo / "file.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "file.txt")
        self._git("commit", "-qm", "base")
        self._git("branch", "-m", "dan/self")
        self._git("remote", "add", "origin", str(self.remote))
        self._git("push", "-q", "origin", "dan/self")

        runner = FreebuffRunner(
            SimpleNamespace(repo_dir=self.repo, worktree_root=root / "worktrees")
        )
        guard = runner._write_push_guard("job-1", prefix=rules.DAN_BRANCH_PREFIX) / "bin"
        self.env = dict(os.environ)
        self.env["PATH"] = f"{guard}:{self.env['PATH']}"

    def _git(self, *args: str, guard: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env if guard else None,
            capture_output=True,
            text=True,
        )

    def test_a_dan_worktree_may_push_its_dan_branch(self) -> None:
        (self.repo / "file.txt").write_text("changed\n", encoding="utf-8")
        self._git("commit", "-qam", "change")
        result = self._git(
            "push", "origin", "refs/heads/dan/self:refs/heads/dan/self", guard=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_dan_worktree_cannot_push_a_freebuff_branch(self) -> None:
        self._git("branch", "freebuff/other")
        result = self._git("push", "origin", "freebuff/other", guard=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only dan/* may be pushed", result.stderr)

    def test_a_dan_worktree_cannot_push_main(self) -> None:
        self._git("branch", "main")
        result = self._git("push", "origin", "main", guard=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blocked", result.stderr)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


class DanWorktreeRemovalTests(unittest.IsolatedAsyncioTestCase):
    """A dan worktree belongs to her checkout, not to the target checkout."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)

        seed = root / "seed"
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        _git(seed, "config", "user.email", "test@example.com")
        _git(seed, "config", "user.name", "Test")
        (seed / "file.txt").write_text("base\n", encoding="utf-8")
        _git(seed, "add", "file.txt")
        _git(seed, "commit", "-qm", "base")
        _git(seed, "branch", "-m", "main")
        _git(seed, "remote", "add", "origin", str(self.remote))
        _git(seed, "push", "-q", "origin", "main")

        self.her = root / "curator"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.her)], check=True)
        _git(self.her, "config", "user.email", "test@example.com")
        _git(self.her, "config", "user.name", "Test")

        # A second, unrelated checkout: the target repo the bot usually builds.
        self.target = root / "hustler"
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        _git(self.target, "config", "user.email", "test@example.com")
        _git(self.target, "config", "user.name", "Test")
        (self.target / "ios-app").mkdir()
        (self.target / "ios-app" / "project.yml").write_text("name: Hustler\n", encoding="utf-8")
        _git(self.target, "add", "ios-app/project.yml")
        _git(self.target, "commit", "-qm", "target")

        self.runner = FreebuffRunner(
            SimpleNamespace(
                repo_dir=self.target,
                worktree_root=root / "worktrees",
                project=SimpleNamespace(self_dir=self.her),
                max_diff_lines=400,
            )
        )

        async def no_tmux(_env=None) -> None:
            return None

        self.runner.abort = no_tmux  # the unit test has no tmux session

    async def _dan_worktree(self, job_id: str) -> TaskEnvironment:
        branch = f"dan/{job_id}"
        worktree = Path(self.tmp.name) / "worktrees" / job_id
        await self.runner.self_repo.create_worktree(branch, worktree, "origin/main")
        return TaskEnvironment(
            branch=branch,
            worktree=worktree,
            session="test",
            base_branch="origin/main",
            kind="dan",
        )

    async def test_the_target_checkout_does_not_own_a_dan_worktree(self) -> None:
        env = await self._dan_worktree("job-0")
        with self.assertLogs("freebuff_runner", level="WARNING") as logs:
            self.assertFalse(await self.runner.repo.remove_worktree(env.worktree))
        self.assertTrue(env.worktree.exists())
        self.assertIn("could not remove worktree", "\n".join(logs.output))

    async def test_approve_removes_the_dan_worktree_from_her_checkout(self) -> None:
        env = await self._dan_worktree("job-a")
        (env.worktree / "file.txt").write_text("changed\n", encoding="utf-8")
        changes = await self.runner.collect(env, 400)

        sha = await self.runner.approve_and_push(
            env, message="dan: a change", expected_snapshot=changes.snapshot
        )

        self.assertTrue(sha)
        self.assertFalse(env.worktree.exists())
        pushed = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "dan/job-a"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(pushed.stdout.strip(), sha)

    async def test_cancel_removes_the_dan_worktree_from_her_checkout(self) -> None:
        env = await self._dan_worktree("job-b")
        self.runner._active = env

        removed = await self.runner.cancel_job("job-b")

        self.assertIn("worktree", removed)
        self.assertFalse(env.worktree.exists())

    async def test_a_failed_removal_is_reported_in_the_log(self) -> None:
        not_a_worktree = Path(self.tmp.name) / "plain-directory"
        not_a_worktree.mkdir()

        with self.assertLogs("freebuff_runner", level="WARNING") as logs:
            self.assertFalse(await self.runner.self_repo.remove_worktree(not_a_worktree))

        self.assertIn("could not remove worktree", "\n".join(logs.output))


class DanStageTests(unittest.IsolatedAsyncioTestCase):
    """The self-build stages: unittest, then py_compile and bash -n."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.package = self.worktree / "mac-builder"
        (self.package / "tests").mkdir(parents=True)
        (self.package / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.package / "scripts").mkdir()
        (self.package / "scripts" / "check.sh").write_text("echo ok\n", encoding="utf-8")
        self.job = SimpleNamespace(kind="dan", log_path=self.root / "job.log", id="job-1")
        self.runner = jobs.JobRunner.__new__(jobs.JobRunner)
        self.runner.settings = SimpleNamespace()
        self.env = SimpleNamespace(worktree=self.worktree)

    async def test_a_failing_unittest_stage_forces_a_revise(self) -> None:
        (self.package / "tests" / "test_self.py").write_text(
            "import unittest\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_no(self):\n"
            "        self.fail('broken')\n",
            encoding="utf-8",
        )
        state, _tail, ok = await self.runner._run_stage(self.job, self.env, "check")

        self.assertFalse(ok)
        self.assertIn("failed (exit", state)
        forced = rules.force_revise_reason(check_ok=bool(ok), test_ok=None, dan=True)
        self.assertIsNotNone(forced)
        self.assertIn("unittest discover", forced)
        self.assertNotIn("swift", forced)

    async def test_a_passing_unittest_stage_does_not_force_a_revise(self) -> None:
        (self.package / "tests" / "test_self.py").write_text(
            "import unittest\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_yes(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        state, _tail, ok = await self.runner._run_stage(self.job, self.env, "check")

        self.assertTrue(ok, state)
        self.assertEqual(state, "passed")
        self.assertIsNone(rules.force_revise_reason(check_ok=True, test_ok=None))

    async def test_the_test_stage_compiles_modules_and_checks_scripts(self) -> None:
        (self.package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        state, _tail, ok = await self.runner._run_stage(self.job, self.env, "test")

        self.assertTrue(ok, state)
        self.assertIn("py_compile passed", state)
        self.assertIn("bash -n passed", state)

    async def test_a_broken_module_fails_the_test_stage(self) -> None:
        (self.package / "module.py").write_text("def broken(:\n", encoding="utf-8")
        state, _tail, ok = await self.runner._run_stage(self.job, self.env, "test")

        self.assertFalse(ok)
        self.assertIn("py_compile failed", state)
        self.assertIsNotNone(rules.force_revise_reason(check_ok=True, test_ok=False))


class _FakeQueue:
    def __init__(self) -> None:
        self.submitted = []

    async def submit(self, job):
        self.submitted.append(job)
        return 1


class _FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, content, **kwargs) -> None:
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=7)
        self.channel_id = 99
        self.response = _FakeResponse()


class DanCommandTests(unittest.IsolatedAsyncioTestCase):
    def _instance(self) -> bot.MacBuilderBot:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(log_dir=Path(self._tmp.name))
        instance.queue = _FakeQueue()

        async def allowed(_interaction) -> bool:
            return True

        instance.require_allowed = allowed
        return instance

    async def test_the_command_queues_a_dan_job_on_a_dan_branch(self) -> None:
        instance = self._instance()
        interaction = _FakeInteraction()

        await instance.cmd_dan(interaction, "  make the queue visible  ")

        self.assertEqual(len(instance.queue.submitted), 1)
        job = instance.queue.submitted[0]
        self.assertEqual(job.kind, "dan")
        self.assertEqual(job.text, "make the queue visible")
        self.assertIn(f"dan/{job.id}", interaction.response.messages[0])
        self.assertIn("nothing is pushed until you press Approve", interaction.response.messages[0])

    async def test_empty_text_queues_nothing(self) -> None:
        instance = self._instance()
        interaction = _FakeInteraction()

        await instance.cmd_dan(interaction, "   ")

        self.assertEqual(instance.queue.submitted, [])
        self.assertTrue(interaction.response.messages)

    def test_project_summary_names_both_commands_and_the_guardrails(self) -> None:
        ctx = project_context.ProjectContext(
            bot_name="Melisa",
            project_name="Hustler",
            builder_name="Freebuff",
            target_dir=Path("/target"),
            target_remote="git@example.com:target.git",
            authorized_branch="freebuff/base",
            self_dir=Path("/self"),
            self_remote="git@example.com:bot.git",
        )
        text = ctx.summary()
        self.assertIn("/dan:", text)
        self.assertIn("/task:", text)
        self.assertIn("guardrails", text)
        self.assertIn("dan/*", text)
        self.assertIn("freebuff/*", text)


if __name__ == "__main__":
    unittest.main()
