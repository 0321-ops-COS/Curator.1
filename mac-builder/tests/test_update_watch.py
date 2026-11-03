"""The update watcher: a throwaway remote, real git, no network and no Discord."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import update_watch  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.test",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.test",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=True, env={**os.environ, **GIT_ENV})
    return done.stdout.strip()


class Fixture:
    """A bare origin, a checkout of it, and a clone that pushes new commits."""

    def __init__(self, case: unittest.TestCase) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.remote, self.work, self.other = root / "origin.git", root / "work", root / "other"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(root, "clone", "-q", str(self.remote), str(self.work))
        self.commit(self.work, "mac-builder/bot.py", "print('v1')\n", "First bot commit")
        git(self.work, "push", "-q", "-u", "origin", "main")
        git(root, "clone", "-q", str(self.remote), str(self.other))

    def commit(self, where: Path, name: str, text: str, message: str) -> None:
        (where / name).parent.mkdir(parents=True, exist_ok=True)
        (where / name).write_text(text, encoding="utf-8")
        git(where, "add", "-A")
        git(where, "commit", "-q", "-m", message)

    def push(self, name: str, text: str, message: str) -> None:
        self.commit(self.other, name, text, message)
        git(self.other, "push", "-q", "origin", "HEAD:main")


class OwnCodeTests(unittest.TestCase):
    def watcher(self, fixture: Fixture, **kw: float) -> "update_watch.UpdateWatcher":
        return update_watch.UpdateWatcher(
            root=fixture.work, repo_dir=fixture.work, branch="main", **kw
        )

    def test_new_origin_commits_are_reported_once_with_groups_and_status(self) -> None:
        fixture = Fixture(self)
        watcher = self.watcher(fixture)
        fixture.push("mac-builder/jobs.py", "x = 2\n", "Tidy the job runner")
        fixture.push("mac-builder/tests/test_jobs.py", "x = 1\n", "Pin the runner")
        fixture.push("README.md", "docs\n", "Explain the runner")
        notices = watcher.check()
        self.assertEqual(len(notices), 1)
        for expected in ("Tidy the job runner", "Pin the runner", "Explain the runner",
                         "bot code (1)", "tests (1)", "docs only (1)", "changed lines:",
                         "working tree: clean", "fast-forward: yes", "git pull --ff-only"):
            self.assertIn(expected, notices[0])
        self.assertEqual(watcher.check(), (), "one notice per new origin/main tip")
        self.assertIn("3 behind", watcher.status_line())
        self.assertIn("supervised unknown", watcher.status_line())

    def test_a_running_commit_at_or_ahead_of_origin_is_not_a_notice(self) -> None:
        fixture = Fixture(self)
        fixture.commit(fixture.work, "mac-builder/bot.py", "print('local')\n", "Local change")
        watcher = self.watcher(fixture)
        self.assertEqual(watcher.check(), ())
        self.assertIn("0 behind", watcher.status_line())

    def test_diverged_commits_say_no_fast_forward_and_offer_nothing(self) -> None:
        fixture = Fixture(self)
        watcher = self.watcher(fixture)  # running commit is the first one
        fixture.push("mac-builder/jobs.py", "remote\n", "Remote runner change")
        fixture.commit(fixture.work, "mac-builder/bot.py", "print('local')\n", "Local change")
        notices = watcher.check()
        self.assertEqual(len(notices), 1)
        self.assertIn("Remote runner change", notices[0])
        self.assertIn("fast-forward: no", notices[0])
        self.assertNotIn("git pull --ff-only", notices[0])

    def test_a_dirty_tree_is_said_plainly_and_nothing_is_offered(self) -> None:
        fixture = Fixture(self)
        watcher = self.watcher(fixture)
        fixture.push("mac-builder/bot.py", "x = 2\n", "New bot code")
        (fixture.work / "mac-builder/bot.py").write_text("print('uncommitted')\n", encoding="utf-8")
        text = watcher.check()[0]
        self.assertIn("working tree: dirty (1 uncommitted path(s))", text)
        self.assertIn("nothing is offered", text)
        self.assertNotIn("git pull --ff-only", text)

    def test_checks_can_be_disabled(self) -> None:
        fixture = Fixture(self)
        watcher = self.watcher(fixture, interval_seconds=0)
        self.assertFalse(watcher.enabled)
        self.assertEqual(watcher.check(), ())
        self.assertIn("checks disabled", watcher.status_line())


class AuthorizedBranchTests(unittest.TestCase):
    def test_the_hustler_branch_moving_past_the_base_is_reported(self) -> None:
        fixture = Fixture(self)
        git(fixture.work, "branch", "freebuff/base")
        git(fixture.work, "push", "-q", "origin", "freebuff/base")
        git(fixture.other, "fetch", "-q", "origin", "freebuff/base")
        watcher = update_watch.UpdateWatcher(
            root=fixture.work, repo_dir=fixture.other, branch="freebuff/base"
        )
        self.assertEqual(watcher.check(), (), "nothing moved yet")
        git(fixture.work, "checkout", "-q", "freebuff/base")
        fixture.commit(fixture.work, "mac-builder/scripts/status.sh", "#!/bin/bash\n",
                       "Refresh the status script")
        git(fixture.work, "push", "-q", "origin", "freebuff/base")
        git(fixture.work, "checkout", "-q", "main")
        notices = watcher.check()
        self.assertEqual(len(notices), 1)
        for expected in ("freebuff/base", "Refresh the status script", "bot code (1)", "base"):
            self.assertIn(expected, notices[0])
        self.assertEqual(watcher.check(), (), "one notice per new tip")


if __name__ == "__main__":
    unittest.main()
