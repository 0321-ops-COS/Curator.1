"""Approve must push only the change that was reviewed.

Each test builds a real git repository with a bare remote and a worktree, takes
the snapshot the way the pipeline does, then edits the worktree and presses
Approve in code.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freebuff_runner import (  # noqa: E402
    CommandResult,
    FreebuffError,
    FreebuffRunner,
    TaskEnvironment,
)

SOURCE = Path("ios-app") / "RootView.swift"


class RecordingRunner(FreebuffRunner):
    """A runner that records the order of the kill and the final collect."""

    def __init__(self, settings: object, events: list[str]) -> None:
        super().__init__(settings)
        self.events = events

    async def _tmux(self, *args: str, input_bytes: bytes | None = None) -> CommandResult:
        self.events.append("tmux:" + args[0])
        return CommandResult(0, "", "")

    async def collect(self, env: TaskEnvironment, max_diff_lines: int):
        events = getattr(self, "events", None)
        if events is not None:
            events.append("collect")
        return await super().collect(env, max_diff_lines)


class ApproveRecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.worktree = self.root / "worktrees" / "job-1"

        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.repo / "ios-app").mkdir()
        (self.repo / SOURCE).write_text("let a = 1\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        self._git("branch", "-m", "freebuff/base")
        self._git("remote", "add", "origin", str(self.remote))
        self._git("worktree", "add", "-q", "-b", "freebuff/job1", str(self.worktree), "freebuff/base")
        self._git("push", "-q", "origin", "freebuff/job1")
        self.pushed_sha = self._remote_sha("freebuff/job1")

        self.events: list[str] = []
        self.runner = RecordingRunner(
            SimpleNamespace(
                repo_dir=self.repo,
                worktree_root=self.root / "worktrees",
                max_diff_lines=400,
            ),
            self.events,
        )
        self.env = TaskEnvironment(
            branch="freebuff/job1",
            worktree=self.worktree,
            session="fake-session",
            base_branch="freebuff/base",
            guard_dir=self.root / "guard",
        )

    # ------------------------------------------------------------- helpers

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(self.repo), check=True, capture_output=True, text=True
        )

    def _remote_sha(self, branch: str) -> str:
        result = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _edit(self, text: str) -> None:
        (self.worktree / SOURCE).write_text(text, encoding="utf-8")

    def _review(self):
        """Collect the change set the way the job pipeline does before review."""
        changes = asyncio.run(self.runner.collect(self.env, 400))
        self.events.clear()
        return changes

    def _approve(self, expected_snapshot: str) -> str:
        return asyncio.run(
            self.runner.approve_and_push(
                self.env, message="approve", expected_snapshot=expected_snapshot
            )
        )

    # --------------------------------------------------------------- tests

    def test_unchanged_worktree_still_pushes(self) -> None:
        self._edit("let a = 2\n")
        reviewed = self._review()
        sha = self._approve(reviewed.snapshot)

        self.assertTrue(sha)
        self.assertEqual(self._remote_sha("freebuff/job1"), sha)
        self.assertFalse(self.worktree.exists())
        self.assertLess(
            self.events.index("tmux:kill-session"),
            self.events.index("collect"),
            "the Freebuff session must be stopped before the final collect",
        )

    def test_protected_file_added_after_review_blocks_approve(self) -> None:
        self._edit("let a = 2\n")
        reviewed = self._review()
        (self.worktree / "ios-app" / "project.yml").write_text("name: Hustler\n", encoding="utf-8")

        with self.assertRaises(FreebuffError) as caught:
            self._approve(reviewed.snapshot)

        self.assertIn("changed after the review", str(caught.exception))
        self.assertEqual(self._remote_sha("freebuff/job1"), self.pushed_sha)
        self.assertTrue(self.worktree.exists(), "the worktree is kept for inspection")
        self.assertLess(
            self.events.index("tmux:kill-session"),
            self.events.index("collect"),
        )

    def test_edit_after_review_with_the_same_line_count_blocks_approve(self) -> None:
        self._edit("let a = 2\n")
        reviewed = self._review()
        self._edit("let b = 9\n")

        with self.assertRaises(FreebuffError) as caught:
            self._approve(reviewed.snapshot)

        self.assertIn("nothing was pushed", str(caught.exception))
        self.assertEqual(self._remote_sha("freebuff/job1"), self.pushed_sha)

    def test_rules_run_again_even_when_the_snapshot_matches(self) -> None:
        # Defence in depth: a reviewed snapshot that rules should never have
        # accepted must still be refused at Approve.
        (self.worktree / "ios-app" / "project.yml").write_text("name: Hustler\n", encoding="utf-8")
        reviewed = self._review()

        with self.assertRaises(FreebuffError) as caught:
            self._approve(reviewed.snapshot)

        self.assertIn("hard rules", str(caught.exception))
        self.assertIn("ios-app/project.yml", str(caught.exception))
        self.assertEqual(self._remote_sha("freebuff/job1"), self.pushed_sha)

    def test_untracked_file_added_after_review_blocks_approve(self) -> None:
        self._edit("let a = 2\n")
        reviewed = self._review()
        (self.worktree / "ios-app" / "Extra.swift").write_text("let c = 3\n", encoding="utf-8")

        with self.assertRaises(FreebuffError) as caught:
            self._approve(reviewed.snapshot)

        self.assertIn("changed after the review", str(caught.exception))
        self.assertEqual(self._remote_sha("freebuff/job1"), self.pushed_sha)

    def test_missing_snapshot_is_refused(self) -> None:
        self._edit("let a = 2\n")
        self._review()

        with self.assertRaises(FreebuffError) as caught:
            self._approve("")

        self.assertIn("no reviewed snapshot", str(caught.exception))
        self.assertEqual(self._remote_sha("freebuff/job1"), self.pushed_sha)
        self.assertNotIn("tmux:kill-session", self.events)


if __name__ == "__main__":
    unittest.main()
