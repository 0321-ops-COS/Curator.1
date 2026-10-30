from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project_context as pc  # noqa: E402
from manager import ReviewBrief, build_review_messages  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def make_repo(path: Path, branch: str = "main", remote: str = "", files: tuple[str, ...] = ()) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    for name in files:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    (path / "README").write_text("r\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path


class RemoteTests(unittest.TestCase):
    def test_credentials_are_stripped(self) -> None:
        self.assertEqual(
            pc.redact_remote("https://user:tok3n@github.com/o/r.git"),
            "https://github.com/o/r.git",
        )
        self.assertEqual(pc.redact_remote("git@github.com:o/r.git"), "git@github.com:o/r.git")


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.bot = make_repo(
            self.root / "curator", remote="https://tok@github.com/o/Curator.1.git",
            files=("mac-builder/bot.py",),
        )
        self.app = make_repo(
            self.root / "hustler", branch="freebuff/abc", remote="https://github.com/o/Hustler.git",
            files=("ios-app/project.yml",),
        )

    def ctx(self, target: Path, branch: str = "freebuff/abc") -> pc.ProjectContext:
        return pc.build_context(target_dir=target, authorized_branch=branch, self_dir=self.bot)

    def test_knows_target_and_herself(self) -> None:
        ctx = self.ctx(self.app)
        self.assertEqual(ctx.project_name, "hustler")
        self.assertEqual(ctx.target_remote, "https://github.com/o/Hustler.git")
        self.assertEqual(ctx.self_remote, "https://github.com/o/Curator.1.git")
        self.assertFalse(ctx.target_is_self)
        text = ctx.summary()
        self.assertIn("Hustler.git", text)
        self.assertIn("Curator.1.git", text)
        self.assertIn("mac-builder", text)
        self.assertNotIn("tok@", text + ctx.prompt_block())
        self.assertIn("Freebuff", ctx.prompt_block())

    def test_preflight_passes_for_real_target(self) -> None:
        pc.preflight(self.ctx(self.app))

    def test_preflight_refuses_own_checkout_in_one_line(self) -> None:
        ctx = self.ctx(self.bot, branch="main")
        self.assertTrue(ctx.target_is_self)
        with self.assertRaises(pc.PreflightError) as cm:
            pc.preflight(ctx)
        message = str(cm.exception)
        self.assertNotIn("\n", message)
        self.assertIn("ios-app/project.yml", message)
        self.assertIn("own repo", message)
        self.assertIn("WARNING", ctx.summary())

    def test_preflight_refuses_missing_branch(self) -> None:
        with self.assertRaises(pc.PreflightError) as cm:
            pc.preflight(self.ctx(self.app, branch="freebuff/nope"))
        self.assertIn("not a local branch", str(cm.exception))
        self.assertNotIn("\n", str(cm.exception))

    def test_subdirectory_resolves_to_repo_root(self) -> None:
        self.assertEqual(self.ctx(self.app / "ios-app").target_dir, self.app)


class ReviewPromptTests(unittest.TestCase):
    def brief(self, **kw: str) -> ReviewBrief:
        return ReviewBrief(
            task="t", diff="d", diff_lines=1, status="", changed_files=("a",),
            check_state="ok", check_tail="", test_state="ok", test_tail="", **kw,
        )

    def test_project_context_reaches_the_reviewer(self) -> None:
        system, user = [
            m["content"] for m in build_review_messages(self.brief(project_context="Target repo: X"))
        ]
        self.assertIn("PROJECT\nTarget repo: X", system)
        self.assertTrue(user.startswith("TASK GIVEN TO THE BUILDER"))

    def test_no_project_section_when_absent(self) -> None:
        system = build_review_messages(self.brief())[0]["content"]
        self.assertNotIn("\nPROJECT\n", system)

    def test_chat_prompt_makes_her_the_repo_manager(self) -> None:
        import manager
        self.assertIn("repo manager", manager.CHAT_SYSTEM_PROMPT)
        self.assertIn("repo manager", manager.REVIEW_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
