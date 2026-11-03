"""P2: a project's own checks and protected paths are now enforced.

No tmux, no Freebuff and no network: the checks are real shell commands in a
throwaway worktree, and Freebuff and the manager are replaced with recorders.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import jobs  # noqa: E402
import projects  # noqa: E402
import rules  # noqa: E402
from freebuff_runner import ChangeSet, TaskEnvironment  # noqa: E402


def registry(*, checks=(), protected=(), kind="python",
             default_protected=()) -> projects.Registry:
    """hustler as it is today, plus a non-default project with its own rules."""
    return projects.Registry([
        projects.Project(
            id="hustler", name="Hustler", remote="git@github.com:o/h.git",
            path=Path("/hustler"), authorized_branch="freebuff/base",
            protected_paths=tuple(default_protected)),
        projects.Project(
            id="notes", name="Notes", remote="git@github.com:o/n.git",
            path=Path("/notes"), authorized_branch="freebuff/notes",
            kind=kind, checks=tuple(checks), protected_paths=tuple(protected)),
    ])


class FakeFreebuff:
    def __init__(self, changes: ChangeSet, worktree: Path) -> None:
        self.changes, self.worktree = changes, worktree

    async def start_task(self, *, job_id: str, task_text: str, log_path: Path,
                         kind: str = "task", project: str = "") -> TaskEnvironment:
        return TaskEnvironment(
            branch="freebuff/deadbeef", worktree=self.worktree, session="fake",
            base_branch="freebuff/notes", project=project)

    async def wait_for_changes(self, env: object, log_path: Path, **_kwargs) -> str:
        return "idle"
    async def collect(self, env: object, max_diff_lines: int) -> ChangeSet:
        return self.changes
    async def abort(self, env: object = None) -> None:
        return None


class ApprovingManager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def review(self, brief: object) -> object:
        self.calls.append("review")
        return SimpleNamespace(verdict="approve", notes="Fine.")


class ProjectChecksTests(unittest.IsolatedAsyncioTestCase):
    """A project's checks run in its job worktree; its paths are refused first."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()

    def runner(self, **project_kwargs: object) -> jobs.JobRunner:
        runner = jobs.JobRunner.__new__(jobs.JobRunner)
        runner.settings = SimpleNamespace(
            projects=registry(**project_kwargs), log_dir=self.root)
        return runner

    def job(self, project: str = "notes") -> jobs.Job:
        return jobs.Job(
            id="20260101-000000-deadbeef", kind="task", title="task", channel_id=1,
            requester_id=2, log_path=self.root / "job.log", text="do the thing",
            project=project)

    async def stage(self, **project_kwargs: object):
        return await self.runner(**project_kwargs)._stage_outcome(
            self.job(), SimpleNamespace(worktree=self.worktree), "check")

    async def review(self, changes: ChangeSet, *, project: str = "notes",
                     **project_kwargs: object):
        """Run the whole pipeline for one change set and record what it did."""
        manager, posted = ApprovingManager(), []

        async def post_review(outcome: jobs.ReviewOutcome) -> None:
            posted.append(outcome)

        async def post_job(job: jobs.Job, ok: bool, summary: str) -> None:
            return None

        settings = SimpleNamespace(
            max_diff_lines=400, max_revise_rounds=0, authorized_branch="freebuff/base",
            projects=registry(**project_kwargs), log_dir=self.root,
            project=SimpleNamespace(prompt_block=lambda: "ctx"))
        runner = jobs.JobRunner(settings, manager, FakeFreebuff(changes, self.worktree),
                                post_job=post_job, post_review=post_review)
        ok, _summary = await runner._run_task(self.job(project))
        return ok, posted, manager, settings

    def change(self, path: str = "src/notes.py", **overrides: object) -> ChangeSet:
        return ChangeSet(diff="+x\n", diff_lines=1, changed_paths=[path],
                         status=" M x", **overrides)

    async def test_a_passing_check_is_shown_as_passed(self) -> None:
        state, _tail, ok, lines = await self.stage(
            checks=["pwd > first.txt", "ls first.txt"])
        self.assertTrue(ok, state)
        self.assertEqual(state, "passed")  # exactly what /merge gates on
        self.assertEqual(lines, ("pwd > first.txt: passed", "ls first.txt: passed"))
        # In order, in the job worktree: the second check saw the first's file.
        # `pwd` prints the physical path (the shell resolves the cwd) and on
        # macOS /var is a symlink to /private/var, so compare resolved paths.
        printed = (self.worktree / "first.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(os.path.realpath(printed), os.path.realpath(self.worktree))
        self.assertIsNone(rules.force_revise_reason(check_ok=True, test_ok=None))

    async def test_a_timed_out_check_is_reported_as_timed_out(self) -> None:
        with mock.patch.object(jobs, "PROJECT_CHECK_TIMEOUT_SECONDS", 1.0):
            state, _tail, ok, _lines = await self.stage(checks=["sleep 30"])
        self.assertFalse(ok)
        self.assertIn("timed out after 1s", state)

    async def test_the_hustler_path_is_unchanged(self) -> None:
        # Even a hustler entry with checks keeps the iOS scripts (non-default only).
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "check.sh").write_text('touch "$PWD/ios-ran"\n', encoding="utf-8")
        runner = jobs.JobRunner.__new__(jobs.JobRunner)
        runner.settings = SimpleNamespace(log_dir=self.root, projects=projects.Registry([
            projects.Project(
                id="hustler", name="Hustler", remote="git@github.com:o/h.git",
                path=self.root, authorized_branch="freebuff/base", kind="python",
                checks=("exit 1",)),
        ]))
        with mock.patch.object(jobs, "SCRIPT_DIR", scripts):
            state, _tail, ok = await runner._run_stage(
                self.job("hustler"), SimpleNamespace(worktree=self.worktree), "check")
        self.assertTrue(ok, state)
        self.assertEqual(state, "passed")
        self.assertTrue((self.worktree / "ios-ran").exists())

    async def test_a_failing_check_forces_a_revise(self) -> None:
        ok, posted, manager, _settings = await self.review(
            self.change(), checks=["printf boom; exit 7"])
        self.assertEqual(manager.calls, ["review"])  # the manager said approve
        self.assertFalse(ok)
        self.assertFalse(posted[0].check_ok)
        self.assertNotEqual(posted[0].verdict, "approve")
        self.assertIn("failed (exit 7)", posted[0].check_state)
        self.assertIn("Fix the failure", posted[0].notes)
        self.assertIn("/check (printf boom; exit 7) failed", posted[0].notes)
        self.assertNotIn("swift test", posted[0].notes)  # not a project check
        self.assertIn("- printf boom; exit 7: failed (exit 7)", posted[0].notes)

    async def test_a_protected_path_in_a_non_default_project_is_rejected(self) -> None:
        ok, posted, manager, _settings = await self.review(
            self.change("secrets/key.txt"), protected=["secrets/", "ops/runbook.md"])
        self.assertFalse(ok)
        self.assertEqual(manager.calls, [])  # the manager was not called
        self.assertEqual(posted[0].verdict, "blocked")
        self.assertEqual(posted[0].violations[0].code, "protected_path")
        self.assertIn("secrets/key.txt", posted[0].notes)

    async def test_a_renamed_protected_path_is_rejected(self) -> None:
        # Status names where it came from; the diff header names the protected
        # destination, which is what must not slip through.
        ok, posted, manager, _settings = await self.review(
            ChangeSet(
                diff=("diff --git a/public/key.txt b/secrets/key.txt\n"
                      "--- a/public/key.txt\n+++ b/secrets/key.txt\n+key\n"),
                diff_lines=1, changed_paths=["public/key.txt"],
                status=" R public/key.txt -> secrets/key.txt"), protected=["secrets/"])
        self.assertFalse(ok)
        self.assertEqual(manager.calls, [])
        self.assertEqual(posted[0].verdict, "blocked")
        self.assertIn("secrets/key.txt", posted[0].notes)

    async def test_global_protected_paths_still_reject_for_every_project(self) -> None:
        for path in (".github/workflows/ci.yml", ".env"):  # notes and hustler
            with self.subTest(path=path):
                ok, posted, manager, _settings = await self.review(
                    self.change(path), checks=["true"], protected=["secrets/"])
                self.assertFalse(ok)
                self.assertEqual(manager.calls, [])
                self.assertEqual(posted[0].verdict, "blocked")
                self.assertIn(path, posted[0].notes)

    async def test_each_check_is_on_the_job_card(self) -> None:
        ok, posted, _manager, settings = await self.review(
            self.change(), checks=["echo ok", "true"], protected=["secrets/"])
        self.assertTrue(ok)
        self.assertEqual(posted[0].violations, ())  # an unprotected change reviews
        self.assertEqual(posted[0].check_lines, ("echo ok: passed", "true: passed"))
        self.assertIn("project checks", posted[0].test_state)  # no iOS scripts
        fields = {field.name: field.value
                  for field in bot.review_embed(settings, posted[0]).fields}
        self.assertEqual(fields["Checks"], "echo ok: passed\ntrue: passed")
        self.assertEqual(fields["Check"], "passed")

    async def test_an_empty_check_list_cannot_pass(self) -> None:
        ok, posted, _manager, _settings = await self.review(self.change(), checks=[])
        self.assertFalse(ok)
        self.assertIs(posted[0].check_ok, False)
        self.assertEqual(posted[0].check_state, "no checks configured")
        self.assertEqual(posted[0].check_lines, ("no checks configured",))
        self.assertNotEqual(posted[0].verdict, "approve")
        self.assertIn("/check failed.", posted[0].notes)  # nothing single to name
        self.assertNotIn("swift test", posted[0].notes)
        self.assertIn("- no checks configured", posted[0].notes)

    async def test_a_default_projects_protected_paths_are_ignored(self) -> None:
        # hustler behaves exactly as it did before projects existed.
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("check.sh", "test.sh"):
            (scripts / name).write_text("exit 0\n", encoding="utf-8")
        with mock.patch.object(jobs, "SCRIPT_DIR", scripts):
            ok, posted, manager, _settings = await self.review(
                self.change("lib/x.py"), project="hustler",
                default_protected=["lib/"])
        self.assertTrue(ok, posted[0].check_state)
        self.assertEqual(manager.calls, ["review"])
        self.assertEqual(posted[0].violations, ())


if __name__ == "__main__":
    unittest.main()
