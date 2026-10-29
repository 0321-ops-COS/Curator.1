from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freebuff_runner import FreebuffRunner  # noqa: E402


class PushGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
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
        self._git("branch", "-m", "freebuff/test")
        self._git("branch", "main")
        self._git("remote", "add", "origin", str(self.remote))
        self._git("push", "-q", "origin", "freebuff/test")

        runner = FreebuffRunner(
            SimpleNamespace(repo_dir=self.repo, worktree_root=root / "worktrees")
        )
        guard = runner._write_push_guard("job-1") / "bin"
        self.env = dict(os.environ)
        self.env["PATH"] = f"{guard}:{self.env['PATH']}"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str, guard: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env if guard else None,
            capture_output=True,
            text=True,
        )

    def test_push_to_main_is_blocked(self) -> None:
        result = self._git("push", "origin", "main", guard=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blocked push to main", result.stderr)

    def test_push_to_renamed_main_is_blocked(self) -> None:
        result = self._git("push", "origin", "freebuff/test:refs/heads/main", guard=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blocked push to refs/heads/main", result.stderr)

    def test_force_push_is_blocked(self) -> None:
        result = self._git("push", "--force", "origin", "freebuff/test", guard=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blocked git flag --force", result.stderr)

    def test_non_push_flags_are_not_blocked(self) -> None:
        result = self._git("log", "--all", "--oneline", guard=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_freebuff_push_is_allowed(self) -> None:
        (self.repo / "file.txt").write_text("changed\n", encoding="utf-8")
        self._git("add", "file.txt")
        self._git("commit", "-qm", "change")
        result = self._git(
            "push", "origin", "refs/heads/freebuff/test:refs/heads/freebuff/test", guard=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        remote_branch = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", "freebuff/test"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(remote_branch.returncode, 0)


if __name__ == "__main__":
    unittest.main()
