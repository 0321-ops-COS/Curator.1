"""A task must branch from the remote branch, never from a stale local copy.

Real git repositories: a bare origin, a clone that falls behind it, and the
runner told to branch from origin.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import freebuff_runner as fr  # noqa: E402
from freebuff_runner import FreebuffError, GitRepo  # noqa: E402


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(path), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def commit(path: Path, name: str, text: str) -> str:
    (path / name).write_text(text + "\n", encoding="utf-8")
    git(path, "add", "-A")
    ident = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    git(path, *ident, "-c", "commit.gpgsign=false", "commit", "-q", "-m", text)
    return git(path, "rev-parse", "HEAD")


class RemoteBranch:
    """A bare origin whose freebuff/base moves on after the local clone."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.local = root / "local"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "freebuff/base", str(self.origin)],
            check=True,
        )
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.seed)], check=True)
        self.first = commit(self.seed, "one.txt", "one")
        git(self.seed, "push", "-q", "origin", "HEAD:freebuff/base")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.local)], check=True)
        self.second = commit(self.seed, "two.txt", "two")
        git(self.seed, "push", "-q", "origin", "HEAD:freebuff/base")


class FreshBaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.remote = RemoteBranch(self.root)

    async def test_the_worktree_starts_from_the_remote_tip(self) -> None:
        repo = GitRepo(self.remote.local)
        # The local branch really is behind, which is the bug being pinned.
        self.assertEqual(
            git(self.remote.local, "rev-parse", "freebuff/base"), self.remote.first
        )
        await repo.fetch_branch("freebuff/base")
        await repo.verify_base("freebuff/base")
        worktree = self.root / "worktrees" / "job-1"
        await repo.create_worktree("freebuff/new", worktree, "origin/freebuff/base")
        self.assertEqual(await repo.head(worktree), self.remote.second)
        self.assertNotEqual(self.remote.first, self.remote.second)

    async def test_a_failed_fetch_raises_the_git_error(self) -> None:
        repo = GitRepo(self.remote.local)
        git(self.remote.local, "remote", "set-url", "origin", str(self.root / "missing.git"))
        with self.assertRaises(FreebuffError) as raised:
            await repo.fetch_branch("freebuff/base")
        self.assertIn("could not fetch origin freebuff/base", str(raised.exception))

    async def test_verify_base_needs_the_remote_ref(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        git(plain, "init", "-q", "-b", "freebuff/base")
        commit(plain, "a.txt", "a")
        repo = GitRepo(plain)
        with self.assertRaises(FreebuffError) as raised:
            await repo.verify_base("freebuff/base")
        self.assertIn("origin/freebuff/base does not exist", str(raised.exception))

    async def test_main_is_still_refused(self) -> None:
        with self.assertRaises(ValueError):
            await GitRepo(self.remote.local).verify_base("main")


class FakeGit:
    def __init__(self, *, fail_fetch: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_fetch = fail_fetch

    async def fetch_branch(self, branch: str) -> None:
        self.calls.append(f"fetch:{branch}")
        if self.fail_fetch:
            raise FreebuffError("could not fetch origin freebuff/base: boom")

    async def verify_base(self, base: str) -> None:
        self.calls.append(f"verify:{base}")

    async def branch_exists(self, branch: str) -> bool:
        return False

    async def create_worktree(self, branch: str, path: Path, base: str) -> None:
        self.calls.append(f"worktree:{base}")
        path.mkdir(parents=True)

    async def head(self, cwd: Path) -> str:
        return "0123456789abcdef"


class StartTaskBaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def runner(self, repo: FakeGit) -> "fr.FreebuffRunner":
        runner = fr.FreebuffRunner.__new__(fr.FreebuffRunner)
        runner.settings = SimpleNamespace(authorized_branch="freebuff/base")
        runner.repo = repo
        runner.worktree_root = self.root / "worktrees"
        tmux = self.root / "tmux"
        tmux.write_text("", encoding="utf-8")
        runner.tmux_bin = str(tmux)
        runner._freebuff_argv = lambda: ["true"]
        runner._write_push_guard = lambda *args, **kwargs: self.root / "guard"

        async def launch(env, argv, task_text, log_path) -> None:
            repo.calls.append("launch")

        runner._launch = launch
        return runner

    async def test_a_task_fetches_then_cuts_from_origin(self) -> None:
        repo = FakeGit()
        log = self.root / "job.log"
        env = await self.runner(repo).start_task(
            job_id="20260101-000000-deadbeef", task_text="do it", log_path=log
        )
        calls = repo.calls
        self.assertEqual(calls[0], "fetch:freebuff/base")
        self.assertIn("verify:freebuff/base", calls)
        self.assertLess(
            calls.index("fetch:freebuff/base"), calls.index("worktree:origin/freebuff/base")
        )
        self.assertEqual(calls[-1], "launch")
        self.assertEqual(env.base_branch, "origin/freebuff/base")
        self.assertEqual(env.base_commit, "0123456")
        self.assertIn(
            "base: origin/freebuff/base @ 0123456 (just fetched)",
            log.read_text(encoding="utf-8"),
        )

    async def test_a_failed_fetch_stops_the_job_before_any_worktree(self) -> None:
        repo = FakeGit(fail_fetch=True)
        with self.assertRaises(FreebuffError):
            await self.runner(repo).start_task(
                job_id="20260101-000000-deadbeef",
                task_text="do it",
                log_path=self.root / "job.log",
            )
        self.assertEqual(repo.calls, ["fetch:freebuff/base"])
        self.assertEqual(list((self.root / "worktrees").glob("*")), [])

    async def test_a_task_configured_on_main_is_still_refused(self) -> None:
        repo = FakeGit()
        runner = self.runner(repo)
        runner.settings = SimpleNamespace(authorized_branch="main")
        with self.assertRaises(ValueError):
            await runner.start_task(
                job_id="20260101-000000-deadbeef",
                task_text="do it",
                log_path=self.root / "job.log",
            )
        self.assertEqual(repo.calls, [])
