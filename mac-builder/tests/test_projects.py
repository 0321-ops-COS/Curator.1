"""Projects phase 1: the registry, routing, the mixing guard and the sheet line.

No tmux, no Freebuff and no network: the routing tests use real throwaway
checkouts (the same way test_task_base does), and the launch is replaced with a
recorder so only the paths, refs and remotes are under test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
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
import review_record  # noqa: E402
import roadmap  # noqa: E402
from freebuff_runner import FreebuffError, FreebuffRunner, GitRepo, TaskEnvironment  # noqa: E402


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def make_checkout(parent: Path, name: str, branch: str = "freebuff/base") -> Path:
    """A checkout with its own bare origin and one branch pushed to it."""
    remote = parent / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    work = parent / name
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")
    (work / "README").write_text("r\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    git(work, "branch", "-m", "main")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", branch)
    git(work, "push", "-q", "origin", branch)
    return work


def remote_of(path: Path) -> str:
    return git(path, "config", "--get", "remote.origin.url").stdout.strip()


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.hustler = make_checkout(self.root, "Hustler")
        self.notes = make_checkout(self.root, "Notes", "freebuff/notes")

    def load(self, payload: object) -> projects.Registry:
        path = self.root / "projects.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return projects.load(
            repo_dir=self.hustler,
            authorized_branch="freebuff/base",
            project_name="Hustler",
            path=path,
        )

    def test_without_a_file_there_is_only_hustler_from_the_settings(self) -> None:
        registry = projects.load(
            repo_dir=self.hustler,
            authorized_branch="freebuff/base",
            project_name="Hustler",
            path=self.root / "absent.json",
        )
        self.assertEqual(registry.ids(), ("hustler",))
        self.assertEqual(registry.default.path, self.hustler)
        self.assertEqual(registry.default.name, "Hustler")
        self.assertEqual(registry.default.authorized_branch, "freebuff/base")
        self.assertEqual(registry.default.remote, remote_of(self.hustler))
        self.assertEqual(registry.default.kind, "ios")
        self.assertEqual(registry.rejected, ())

    def test_a_valid_extra_project_is_loaded_with_its_fields(self) -> None:
        registry = self.load({"projects": [{
            "id": "notes", "name": "Notes", "path": str(self.notes),
            "remote": "git@github.com:owner/Notes.git",
            "authorized_branch": "freebuff/notes", "kind": "python",
            "checks": ["pytest"], "protected_paths": [".env"],
        }]})
        notes = registry.require("notes")
        self.assertEqual(
            (notes.name, notes.kind, notes.authorized_branch, notes.checks,
             notes.protected_paths, notes.path),
            ("Notes", "python", "freebuff/notes", ("pytest",), (".env",),
             self.notes.resolve()),
        )
        self.assertEqual(registry.ids(), ("hustler", "notes"))

    def test_a_checkout_without_a_declared_remote_reads_its_origin(self) -> None:
        registry = self.load({"projects": [{
            "id": "notes", "path": str(self.notes), "authorized_branch": "freebuff/notes",
        }]})
        self.assertIn("notes", registry.ids())
        self.assertEqual(registry.require("notes").remote, remote_of(self.notes))

    def test_every_bad_entry_is_skipped_with_one_line_and_hustler_stays(self) -> None:
        entries = [
            "not an object",
            {"id": "Hustler", "path": str(self.root), "authorized_branch": "freebuff/x"},
            {"id": "Bad Id", "path": str(self.root), "authorized_branch": "freebuff/x"},
            {"id": "onemain", "path": str(self.notes), "remote": "git@github.com:o/a.git",
             "authorized_branch": "main"},
            {"id": "samemaster", "path": str(self.notes), "remote": "git@github.com:o/b.git",
             "authorized_branch": "master"},
            {"id": "missingbranch", "path": str(self.notes), "remote": "git@github.com:o/c.git"},
            {"id": "nosuchpath", "path": str(self.root / "nope"),
             "remote": "git@github.com:o/d.git", "authorized_branch": "freebuff/x"},
            {"id": "samepath", "path": str(self.hustler),
             "remote": "git@github.com:o/e.git", "authorized_branch": "freebuff/x"},
            {"id": "sameremote", "path": str(self.notes),
             "remote": remote_of(self.hustler), "authorized_branch": "freebuff/x"},
            {"id": "badkind", "path": str(self.notes),
             "remote": "git@github.com:o/f.git", "authorized_branch": "freebuff/x",
             "kind": "ruby"},
        ]
        with self.assertLogs("projects", level="WARNING") as logs:
            registry = self.load({"projects": [*entries, {
                "id": "notes", "path": str(self.notes),
                "remote": "git@github.com:owner/Notes.git",
                "authorized_branch": "freebuff/notes",
            }]})
        self.assertEqual(registry.ids(), ("hustler", "notes"), "only the good one loaded")
        self.assertEqual(len(registry.rejected), len(entries))
        self.assertEqual(len(logs.output), len(entries), "one log line per skipped entry")
        reasons = "\n".join(item.line() for item in registry.rejected)
        for expected in (
            "not an object", "reserved", "lowercase", "base branch", "branch name is empty",
            "is not a git checkout", "already 'hustler'", "remote is already", "kind",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, reasons)
        self.assertIn("Skipped in projects.json", "\n".join(registry.summary_lines()))

    def test_a_broken_file_still_leaves_hustler_usable(self) -> None:
        path = self.root / "projects.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("projects", level="WARNING"):
            registry = projects.load(
                repo_dir=self.hustler, authorized_branch="freebuff/base", path=path
            )
        self.assertEqual(registry.ids(), ("hustler",))
        self.assertEqual(len(registry.rejected), 1)
        self.assertIn("could not be read", registry.rejected[0].reason)

    def test_the_registry_lists_its_choices_and_refuses_an_unknown_id(self) -> None:
        registry = self.load({"projects": [{
            "id": "notes", "name": "Notes", "path": str(self.notes),
            "remote": "git@github.com:owner/Notes.git", "authorized_branch": "freebuff/x",
        }]})
        self.assertEqual(registry.choices(), [("hustler", "Hustler"), ("notes", "Notes")])
        with self.assertRaises(ValueError) as raised:
            registry.require("nope")
        self.assertIn("known: hustler, notes", str(raised.exception))


class RegistryFromSettingsTests(unittest.TestCase):
    def test_a_settings_object_without_a_registry_gets_a_synthesized_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(repo_dir=Path(tmp), authorized_branch="freebuff/base")
            registry = projects.from_settings(settings)
        self.assertEqual(registry.ids(), ("hustler",))
        self.assertEqual(registry.default.authorized_branch, "freebuff/base")

    def test_the_remote_falls_back_to_the_project_context(self) -> None:
        settings = SimpleNamespace(project=SimpleNamespace(
            target_remote="https://github.com/o/target.git",
            self_remote="https://github.com/o/self.git"))
        self.assertEqual(projects.remote_for(settings, ""), "https://github.com/o/target.git")
        self.assertEqual(
            projects.remote_for(settings, "", dan=True), "https://github.com/o/self.git"
        )

    def test_remote_identity_ignores_scheme_credentials_and_suffix(self) -> None:
        same = {
            "https://github.com/Owner/Repo.git",
            "git@github.com:Owner/Repo.git",
            "ssh://git@github.com/Owner/Repo",
            "https://token@github.com/Owner/Repo.git",
        }
        self.assertEqual({projects.remote_key(url) for url in same}, {"owner/repo"})


class ProjectRoutingTests(unittest.IsolatedAsyncioTestCase):
    """A Hustler job routes exactly as before; a second project goes elsewhere."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.hustler = make_checkout(self.root, "Hustler", "freebuff/base")
        self.notes = make_checkout(self.root, "Notes", "freebuff/notes")
        self.worktrees = self.root / "worktrees"

    def registry(self, *, github_remotes: bool = True) -> projects.Registry:
        hustler = projects.Project(
            id="hustler", name="Hustler",
            remote=("https://github.com/0321-ops-COS/Hustler.git"
                    if github_remotes else remote_of(self.hustler)),
            path=self.hustler, authorized_branch="freebuff/base", kind="ios",
        )
        notes = projects.Project(
            id="notes", name="Notes",
            remote=("https://github.com/0321-ops-COS/Notes.git"
                    if github_remotes else remote_of(self.notes)),
            path=self.notes, authorized_branch="freebuff/notes", kind="python",
        )
        return projects.Registry([hustler, notes])

    def runner(self, registry: projects.Registry) -> FreebuffRunner:
        runner = FreebuffRunner.__new__(FreebuffRunner)
        runner.settings = SimpleNamespace(
            repo_dir=self.hustler, authorized_branch="freebuff/base",
            worktree_root=self.worktrees, projects=registry,
            freebuff_bin=sys.executable, freebuff_args=(),
        )
        runner.repo = GitRepo(self.hustler)
        runner.worktree_root = self.worktrees
        runner.git_bin = "git"
        runner.tmux_bin = sys.executable
        runner._project_repos = {}
        runner._registry = None
        runner.launched = []

        async def launch(env, argv, task_text, log_path) -> None:
            runner.launched.append(env)

        runner._launch = launch
        runner._write_push_guard = lambda job_id, **kwargs: self.root / "guard"
        return runner

    async def test_a_hustler_job_keeps_its_path_base_prefix_and_link(self) -> None:
        registry = self.registry()
        runner = self.runner(registry)

        env = await runner.start_task(
            job_id="20260101-000000-deadbeef", task_text="do it",
            log_path=self.root / "job.log",
        )

        self.assertEqual(env.project, "hustler")
        self.assertEqual(env.worktree, self.worktrees / "20260101-000000-deadbeef")
        self.assertEqual(env.base_branch, "origin/freebuff/base")
        self.assertTrue(env.branch.startswith("freebuff/"))
        self.assertIs(runner.repo_for_project("hustler"), runner.repo)
        self.assertEqual(runner.launched, [env])

        settings = SimpleNamespace(
            projects=registry,
            project=SimpleNamespace(
                target_remote="https://github.com/0321-ops-COS/Hustler.git",
                self_remote="https://github.com/0321-ops-COS/Curator.1.git"),
        )
        outcome = jobs.ReviewOutcome(
            job=jobs.Job(id="20260101-000000-deadbeef", kind="task", title="t",
                         channel_id=1, requester_id=2, log_path=self.root / "job.log"),
            verdict="approve", notes="", environment=SimpleNamespace(
                branch=env.branch, base_branch=env.base_branch, kind="task"),
            check_state="passed", test_state="passed", diff_lines=1, changed_files=(),
        )
        # The same link the project context produced before projects existed.
        self.assertEqual(
            bot.review_compare_url(settings, outcome),
            "https://github.com/0321-ops-COS/Hustler/compare/"
            f"freebuff/base...{env.branch}?expand=1",
        )

    async def test_a_second_project_gets_its_own_worktree_base_and_repo(self) -> None:
        runner = self.runner(self.registry())

        env = await runner.start_task(
            job_id="20260101-000000-deadbeef", task_text="do it",
            log_path=self.root / "job.log", project="notes",
        )

        self.assertEqual(env.project, "notes")
        self.assertEqual(env.worktree, self.worktrees / "notes" / "20260101-000000-deadbeef")
        self.assertEqual(env.base_branch, "origin/freebuff/notes")
        self.assertTrue(env.branch.startswith("freebuff/"))
        self.assertNotEqual(runner.repo_for_project("notes"), runner.repo)
        created = git(self.notes, "rev-parse", "--verify", f"refs/heads/{env.branch}")
        self.assertEqual(created.returncode, 0, "the branch was cut in Notes")

    async def test_the_worktree_paths_are_distinct_per_project(self) -> None:
        runner = self.runner(self.registry())
        self.assertEqual(
            projects.worktree_path(self.worktrees, "hustler", "job-1"),
            self.worktrees / "job-1",
        )
        self.assertEqual(
            projects.worktree_path(self.worktrees, "notes", "job-1"),
            self.worktrees / "notes" / "job-1",
        )

    async def test_an_unregistered_project_is_refused_before_any_git(self) -> None:
        runner = self.runner(self.registry())
        with self.assertRaises(FreebuffError) as raised:
            await runner.start_task(
                job_id="20260101-000000-deadbeef", task_text="do it",
                log_path=self.root / "job.log", project="nope",
            )
        self.assertIn("known: hustler, notes", str(raised.exception))
        self.assertEqual(runner.launched, [])


class MixingGuardTests(unittest.IsolatedAsyncioTestCase):
    """A job may not build or push in a checkout that is not its project's."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.hustler = make_checkout(self.root, "Hustler", "freebuff/base")
        self.notes = make_checkout(self.root, "Notes", "freebuff/notes")

    def registry(self) -> projects.Registry:
        return projects.Registry([
            projects.Project(id="hustler", name="Hustler", remote=remote_of(self.hustler),
                             path=self.hustler, authorized_branch="freebuff/base"),
            projects.Project(id="notes", name="Notes", remote=remote_of(self.notes),
                             path=self.notes, authorized_branch="freebuff/notes", kind="python"),
        ])

    def runner(self) -> FreebuffRunner:
        runner = FreebuffRunner.__new__(FreebuffRunner)
        runner.settings = SimpleNamespace(
            repo_dir=self.hustler, authorized_branch="freebuff/base",
            worktree_root=self.root / "worktrees", projects=self.registry(),
        )
        runner.repo = GitRepo(self.hustler)
        runner.worktree_root = self.root / "worktrees"
        runner.git_bin = "git"
        runner._project_repos = {}
        runner._registry = None
        return runner

    def worktree(self, job_id: str = "job-1") -> Path:
        path = self.root / "worktrees" / job_id
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.hustler, "worktree", "add", "-q", "-b", f"freebuff/{job_id}",
            str(path), "origin/freebuff/base")
        return path

    async def test_a_worktree_of_the_right_checkout_passes(self) -> None:
        env = TaskEnvironment(branch="freebuff/job-1", worktree=self.worktree(),
                              session="s", base_branch="origin/freebuff/base")
        await self.runner()._check_worktree(env)  # no refusal

    async def test_a_worktree_of_another_checkout_is_refused(self) -> None:
        env = TaskEnvironment(branch="freebuff/job-1", worktree=self.worktree(),
                              session="s", base_branch="origin/freebuff/base",
                              project="notes")
        with self.assertRaises(FreebuffError) as raised:
            await self.runner()._check_worktree(env)
        self.assertIn("belongs to", str(raised.exception))
        self.assertIn("'notes'", str(raised.exception))

    async def test_a_repointed_origin_is_refused(self) -> None:
        worktree = self.worktree()
        runner = self.runner()  # registered while origin still is the project's
        git(worktree, "remote", "set-url", "origin", "https://github.com/evil/fork.git")
        env = TaskEnvironment(branch="freebuff/job-1", worktree=worktree,
                              session="s", base_branch="origin/freebuff/base")
        with self.assertRaises(FreebuffError) as raised:
            await runner._check_worktree(env)
        self.assertIn("pushes to https://github.com/evil/fork.git", str(raised.exception))

    async def test_the_guard_also_runs_before_a_push(self) -> None:
        runner = self.runner()
        env = TaskEnvironment(branch="freebuff/job-1", worktree=self.worktree(),
                              session="s", base_branch="origin/freebuff/base",
                              project="notes")
        runner.collect = mock.AsyncMock(side_effect=AssertionError("pushed past the guard"))
        with self.assertRaises(FreebuffError) as raised:
            await runner.approve_and_push(env, message="m", expected_snapshot="abc:def")
        self.assertIn("belongs to", str(raised.exception))

    def test_the_refusal_names_the_worktree_the_repo_and_the_project(self) -> None:
        hustler = self.registry().require("hustler")
        refusal = projects.mixing_refusal(
            hustler, worktree=self.root / "w", root=self.notes, origin=remote_of(self.notes)
        )
        self.assertIn(str(self.notes), refusal)
        self.assertIn(str(self.hustler), refusal)
        self.assertIn("Nothing was launched or pushed", refusal)


class JobProjectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        registry = projects.Registry([
            projects.Project(id="hustler", name="Hustler", remote="git@github.com:o/h.git",
                             path=self.root, authorized_branch="freebuff/base"),
            projects.Project(id="notes", name="Notes", remote="git@github.com:o/n.git",
                             path=self.root, authorized_branch="freebuff/notes"),
        ])
        self.settings = SimpleNamespace(
            log_dir=self.root / "logs", projects=registry,
            authorized_branch="freebuff/base",
            project=SimpleNamespace(target_remote="git@github.com:o/h.git",
                                    self_remote="git@github.com:o/self.git"),
        )

    def test_a_job_defaults_to_hustler_and_keeps_the_project_it_is_made_with(self) -> None:
        default = jobs.make_job(self.settings, kind="task", title="t",
                                channel_id=1, requester_id=2)
        self.assertEqual(default.project, projects.DEFAULT_PROJECT)
        notes = jobs.make_job(self.settings, kind="task", title="t",
                              channel_id=1, requester_id=2, project="notes")
        self.assertEqual(notes.project, "notes")

    def test_the_project_cannot_be_changed_after_the_job_is_made(self) -> None:
        job = jobs.make_job(self.settings, kind="task", title="t",
                            channel_id=1, requester_id=2, project="notes")
        with self.assertRaises(AttributeError) as raised:
            job.project = "hustler"
        self.assertIn("fixed when the job is made", str(raised.exception))
        self.assertEqual(job.project, "notes")

    def test_queueing_does_not_change_the_project(self) -> None:
        job = jobs.make_job(self.settings, kind="task", title="t",
                            channel_id=1, requester_id=2, project="notes")
        seen: list[str] = []

        async def handler(received: object) -> None:
            seen.append(received.project)  # type: ignore[attr-defined]
            with self.assertRaises(AttributeError):
                received.project = "hustler"  # type: ignore[attr-defined]

        queue = jobs.JobQueue(handler, max_pending=1)

        async def run() -> None:
            await queue.submit(job)
            queue.start()
            await asyncio.sleep(0.05)
            await queue.stop()

        asyncio.run(run())
        self.assertEqual(seen, ["notes"])
        self.assertEqual(job.project, "notes")

    def test_an_unknown_project_is_refused_when_the_job_is_made(self) -> None:
        with self.assertRaises(ValueError) as raised:
            jobs.make_job(self.settings, kind="task", title="t", channel_id=1,
                          requester_id=2, project="nope")
        self.assertIn("known: hustler, notes", str(raised.exception))

    def test_a_rebuilt_review_keeps_its_project(self) -> None:
        job = jobs.make_job(self.settings, kind="task", title="t", channel_id=1,
                            requester_id=2, text="do it", project="notes")
        job.id = "20260101-000000-deadbeef"
        outcome = jobs.ReviewOutcome(
            job=job, verdict="approve", notes="", environment=None,
            check_state="passed", test_state="passed", diff_lines=1, changed_files=(),
        )
        review_record.save(self.settings, outcome)  # env is None: nothing written
        own = review_record.record_path(self.settings, job.id)
        own.write_text(json.dumps({
            "status": review_record.AWAITING, "saved": 1.0,
            "job": {"id": job.id, "kind": "task", "title": "t", "channel_id": 1,
                    "requester_id": 2, "text": "do it", "branch": "freebuff/x",
                    "project": "notes", "repo_slug": "", "log": str(self.root / "j.log")},
            "env": {"branch": "freebuff/x", "worktree": str(self.root / "w"),
                    "base_branch": "origin/freebuff/notes", "kind": "task"},
            "review": {"verdict": "approve", "notes": "", "check_state": "passed",
                       "test_state": "passed", "diff_lines": 1, "changed_files": [],
                       "check_ok": True, "test_ok": True, "snapshot": "a:b"},
        }), encoding="utf-8")
        rebuilt = review_record.outcome(review_record.load_one(self.settings, job.id))
        self.assertEqual(rebuilt.job.project, "notes")


class RoadmapProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def test_a_step_without_a_project_is_hustler(self) -> None:
        steps = roadmap.parse("## Job\nFix the crash\n")
        self.assertEqual(steps[0].project, "hustler")
        self.assertEqual(steps[0].text, "Fix the crash")

    def test_a_project_line_picks_one_project_and_is_not_task_text(self) -> None:
        steps = roadmap.parse(
            "## Job\nproject: notes\nkind: task\nAdd a test\n\n"
            "## Job\nFix the crash\n"
        )
        self.assertEqual((steps[0].project, steps[1].project), ("notes", "hustler"))
        self.assertEqual(steps[0].text, "Add a test")
        self.assertEqual(steps[1].text, "Fix the crash")
        self.assertNotIn("project:", steps[0].text)

    def test_a_project_line_must_name_exactly_one_id(self) -> None:
        with self.assertRaises(roadmap.RoadmapError) as raised:
            roadmap.parse("## Job\nproject: notes extra\nDo it\n")
        self.assertIn("one id", str(raised.exception))

    def test_the_project_survives_a_save_and_a_load(self) -> None:
        steps = roadmap.parse("## Job\nproject: notes\nDo it\n")
        path = self.root / "roadmap.json"
        roadmap.save(path, steps, channel_id=1, requester_id=2)
        loaded, _, _, _ = roadmap.load(path)
        self.assertEqual(loaded[0].project, "notes")
        self.assertEqual(loaded[0].text, "Do it")

    def test_an_old_file_loads_as_hustler(self) -> None:
        path = self.root / "roadmap.json"
        path.write_text(json.dumps([{"text": "Do it", "kind": "task", "job_id": ""}]),
                        encoding="utf-8")
        loaded, _, _, _ = roadmap.load(path)
        self.assertEqual(loaded[0].project, "hustler")

    def test_the_preview_names_a_project_only_when_it_is_not_hustler(self) -> None:
        text = roadmap.preview(roadmap.parse(
            "## Job\nFix the crash\n\n## Job\nproject: notes\nAdd a test\n"))
        self.assertNotIn("[hustler]", text)
        self.assertIn("[notes]", text)


class _Response:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, content: str, **_kwargs: object) -> None:
        self.messages.append(content)

    async def defer(self, **_kwargs: object) -> None:
        return None

    def is_done(self) -> bool:
        return False


class _Followup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str, **_kwargs: object) -> None:
        self.messages.append(content)


class _Interaction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=42)
        self.channel_id = 1
        self.response = _Response()
        self.followup = _Followup()


class _Queue:
    def __init__(self) -> None:
        self.submitted: list[object] = []
        self.running = None

    async def submit(self, job: object) -> int:
        self.submitted.append(job)
        return 1

    def snapshot(self) -> dict[str, object]:
        return {"running": None, "pending": 0}


class CommandRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.registry = projects.Registry([
            projects.Project(id="hustler", name="Hustler", remote="git@github.com:o/h.git",
                             path=self.root, authorized_branch="freebuff/base"),
            projects.Project(id="notes", name="Notes", remote="git@github.com:o/n.git",
                             path=self.root, authorized_branch="freebuff/notes", kind="python"),
        ])

    def instance(self) -> "bot.MacBuilderBot":
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(
            log_dir=self.root / "logs", projects=self.registry,
            authorized_branch="freebuff/base",
            project=SimpleNamespace(project_name="Hustler", target_remote="git@github.com:o/h.git",
                                    self_remote="git@github.com:o/self.git",
                                    summary=lambda: "Hustler context"),
            question_wait_seconds=1800.0, question_auto_answer=False,
            question_auto_answer_max=3, question_suggest=False, question_debug=False,
        )
        instance.queue = _Queue()
        instance.manager = SimpleNamespace(enabled=True)
        instance._board = None
        instance._roadmap_steps = []
        instance._roadmap_jobs = {}
        instance._roadmap_job = None
        instance._roadmap_channel_id = 1
        instance._roadmap_requester_id = 42
        instance._roadmap_waiting = {}
        instance._roadmap_paused = False

        async def allowed(_interaction: object) -> bool:
            return True

        async def track(_job: object, _position: int) -> None:
            return None

        instance.require_allowed = allowed  # type: ignore[method-assign]
        instance._track_board = track  # type: ignore[method-assign]
        instance._update_watcher = SimpleNamespace(
            status_line=lambda _supervised=None: "checked"
        )
        return instance

    async def test_task_routes_to_the_named_project(self) -> None:
        instance = self.instance()
        interaction = _Interaction()
        await instance.cmd_task(interaction, "do it", None, "notes")
        self.assertEqual(len(instance.queue.submitted), 1)
        job = instance.queue.submitted[0]
        self.assertEqual(job.project, "notes")  # type: ignore[attr-defined]
        self.assertIn("on project `notes`", interaction.response.messages[0])

    async def test_task_defaults_to_hustler(self) -> None:
        instance = self.instance()
        interaction = _Interaction()
        await instance.cmd_task(interaction, "do it", None, None)
        self.assertEqual(instance.queue.submitted[0].project, "hustler")  # type: ignore[attr-defined]
        self.assertNotIn("project `", interaction.response.messages[0])

    async def test_task_refuses_an_unknown_project_and_queues_nothing(self) -> None:
        instance = self.instance()
        interaction = _Interaction()
        await instance.cmd_task(interaction, "do it", None, "nope")
        self.assertEqual(instance.queue.submitted, [])
        self.assertIn("Unknown project 'nope'", interaction.response.messages[0])
        self.assertIn("hustler, notes", interaction.response.messages[0])

    async def test_dan_takes_no_project(self) -> None:
        instance = self.instance()
        await instance.cmd_dan(_Interaction(), "do it")
        self.assertEqual(instance.queue.submitted[0].project, "hustler")  # type: ignore[attr-defined]

    async def test_the_task_autocomplete_lists_the_registry(self) -> None:
        instance = self.instance()
        choices = await instance.task_project_autocomplete(_Interaction(), "")
        self.assertEqual([(choice.name, choice.value) for choice in choices],
                         [("Hustler (hustler)", "hustler"), ("Notes (notes)", "notes")])
        filtered = await instance.task_project_autocomplete(_Interaction(), "not")
        self.assertEqual([choice.value for choice in filtered], ["notes"])

    async def test_status_names_the_running_jobs_project(self) -> None:
        instance = self.instance()
        instance.queue.running = SimpleNamespace(
            id="20260101-000000-deadbeef", kind="task", project="notes", duration=1.0
        )
        interaction = _Interaction()
        with mock.patch.object(bot.jobs, "probe_status",
                               mock.AsyncMock(return_value=(0, "status"))):
            await instance.cmd_status(interaction)
        text = interaction.followup.messages[-1]
        self.assertIn("projects: hustler, notes", text)
        self.assertIn("job project: Notes", text)

    async def test_a_review_card_names_the_project(self) -> None:
        instance = self.instance()
        outcome = jobs.ReviewOutcome(
            job=jobs.Job(id="20260101-000000-deadbeef", kind="task", title="t",
                         channel_id=1, requester_id=2,
                         log_path=self.root / "job.log", project="notes"),
            verdict="approve", notes="fine", environment=SimpleNamespace(
                branch="freebuff/x", base_branch="origin/freebuff/notes", kind="task"),
            check_state="passed", test_state="passed", diff_lines=2, changed_files=(),
        )
        embed = bot.review_embed(instance.settings, outcome)
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Project"], "Notes")

    async def test_a_dan_review_says_it_is_her_own_code(self) -> None:
        instance = self.instance()
        outcome = jobs.ReviewOutcome(
            job=jobs.Job(id="20260101-000000-deadbeef", kind="dan", title="t",
                         channel_id=1, requester_id=2, log_path=self.root / "job.log"),
            verdict="approve", notes="fine", environment=SimpleNamespace(
                branch="dan/x", base_branch="origin/main", kind="dan"),
            check_state="passed", test_state="passed", diff_lines=2, changed_files=(),
        )
        embed = bot.review_embed(instance.settings, outcome)
        self.assertEqual(
            {field.name: field.value for field in embed.fields}["Project"],
            "mac-builder (her own code)",
        )

    async def test_project_lists_the_projects_and_the_skipped_ones(self) -> None:
        instance = self.instance()
        instance.settings.projects = projects.Registry(
            list(self.registry), [projects.Rejected("ghost", "path /nope is not a git checkout")]
        )
        interaction = _Interaction()
        await instance.cmd_project(interaction)
        text = interaction.response.messages[0]
        self.assertIn("hustler — Hustler", text)
        self.assertIn("notes — Notes", text)
        self.assertIn("ghost: path /nope is not a git checkout", text)


class RoadmapCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.registry = projects.Registry([
            projects.Project(id="hustler", name="Hustler", remote="git@github.com:o/h.git",
                             path=self.root, authorized_branch="freebuff/base"),
            projects.Project(id="notes", name="Notes", remote="git@github.com:o/n.git",
                             path=self.root, authorized_branch="freebuff/notes"),
        ])

    def instance(self) -> "bot.MacBuilderBot":
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(
            log_dir=self.root / "logs", projects=self.registry,
            authorized_branch="freebuff/base",
            project=SimpleNamespace(target_remote="git@github.com:o/h.git",
                                    self_remote="git@github.com:o/self.git"),
        )
        instance.queue = _Queue()
        instance._roadmap_steps = []
        instance._roadmap_jobs = {}
        instance._roadmap_job = None
        instance._roadmap_channel_id = 1
        instance._roadmap_requester_id = 42
        instance._roadmap_waiting = {}
        instance._roadmap_paused = False
        instance._roadmap_merge = None

        async def track(_job: object, _position: int) -> None:
            return None

        instance._track_board = track  # type: ignore[method-assign]
        return instance

    async def apply(self, instance: "bot.MacBuilderBot", sheet: str) -> str:
        async def read() -> bytes:
            return sheet.encode("utf-8")

        attachment = SimpleNamespace(filename="sheet.md", read=read)
        return await instance._roadmap_apply(
            attachment, "preview", 1, 42
        )

    async def test_a_sheet_job_is_queued_for_its_project(self) -> None:
        instance = self.instance()
        await instance._roadmap_apply(
            SimpleNamespace(filename="sheet.md",
                            read=mock.AsyncMock(return_value=(
                                "## Job\nproject: notes\nAdd a test\n").encode("utf-8"))),
            "start", 1, 42)
        self.assertEqual(instance.queue.submitted[0].project, "notes")  # type: ignore[attr-defined]
        self.assertEqual(instance.queue.submitted[0].text, "Add a test")  # type: ignore[attr-defined]

    async def test_an_unknown_project_is_refused_before_anything_is_queued(self) -> None:
        instance = self.instance()
        text = await self.apply(instance, "## Job\nproject: ghost\nDo it\n")
        self.assertIn("unknown project 'ghost'", text)
        self.assertIn("Known: hustler, notes", text)
        self.assertEqual(instance.queue.submitted, [])

    async def test_a_preview_still_names_the_projects(self) -> None:
        instance = self.instance()
        text = await self.apply(instance, "## Job\nproject: notes\nDo it\n")
        self.assertIn("[notes]", text)
        self.assertEqual(instance.queue.submitted, [])

    async def test_the_roadmap_merge_check_uses_the_jobs_project(self) -> None:
        instance = self.instance()
        instance._roadmap_job = SimpleNamespace(project="notes", kind="task")
        notes_repo = SimpleNamespace()
        instance.freebuff = SimpleNamespace(
            repo=SimpleNamespace(), self_repo=SimpleNamespace(),
            repo_for_project=lambda project_id: notes_repo if project_id == "notes" else None,
        )
        self.assertIs(instance._roadmap_repo("task"), notes_repo)
        self.assertIs(instance._roadmap_repo("dan"), instance.freebuff.self_repo)

    async def test_a_project_step_merges_into_that_projects_branch(self) -> None:
        instance = self.instance()
        instance._roadmap_job = SimpleNamespace(project="notes", kind="task")
        self.assertEqual(instance._roadmap_base("task"), "freebuff/notes")
        instance._roadmap_job = None
        self.assertEqual(instance._roadmap_base("task"), "freebuff/base")


if __name__ == "__main__":
    unittest.main()
