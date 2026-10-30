"""Project context: which repo she builds, and that mac-builder/ is protected.

The tests use real throwaway git repositories so the git plumbing is exercised
rather than mocked.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import manager  # noqa: E402
import project_context as pc  # noqa: E402
from manager import ReviewBrief, build_review_messages  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def make_repo(
    parent: Path,
    name: str,
    *,
    branch: str = "main",
    remote: str = "",
    files: tuple[str, ...] = (),
) -> Path:
    path = parent / name
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    for entry in files:
        target = path / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    (path / "README").write_text("r\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path


class RedactionTests(unittest.TestCase):
    def test_a_token_in_the_url_is_stripped(self) -> None:
        self.assertEqual(
            pc.redact_remote("https://user:tok3n@github.com/o/r.git"),
            "https://github.com/o/r.git",
        )

    def test_remotes_without_a_secret_are_unchanged(self) -> None:
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git", ""):
            self.assertEqual(pc.redact_remote(url), url)


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.bot = make_repo(
            self.root,
            "curator",
            remote="https://tok@github.com/o/Curator.1.git",
            files=("mac-builder/bot.py",),
        )
        self.app = make_repo(
            self.root,
            "Hustler",
            branch="freebuff/abc",
            remote="https://tok:secr3t@github.com/o/Hustler.git",
            files=("ios-app/project.yml",),
        )

    def ctx(self, target: Path, *, self_dir: Path | None = None) -> pc.ProjectContext:
        return pc.build_context(
            target_dir=target,
            authorized_branch="freebuff/abc",
            self_dir=self_dir or self.bot,
        )

    def test_knows_the_target_and_herself(self) -> None:
        ctx = self.ctx(self.app)
        self.assertEqual(ctx.project_name, "Hustler")
        self.assertFalse(ctx.target_is_self)
        self.assertIn("Hustler.git", ctx.target_remote)
        self.assertIn("Curator.1.git", ctx.self_remote)
        text = ctx.summary()
        self.assertIn("freebuff/abc", text)
        self.assertIn("Freebuff", text)
        self.assertIn("mac-builder", text)

    def test_credentials_never_reach_the_text(self) -> None:
        ctx = self.ctx(self.app)
        for text in (ctx.target_remote, ctx.self_remote, ctx.summary(), ctx.prompt_block()):
            self.assertNotIn("tok@", text)
            self.assertNotIn("secr3t", text)

    def test_a_subdirectory_resolves_to_the_repo_root(self) -> None:
        self.assertEqual(self.ctx(self.app / "ios-app").target_dir, self.app)

    def test_same_checkout_is_reported_as_the_supported_layout(self) -> None:
        # On the Mac mac-builder/ lives inside the Hustler checkout, so the
        # target and her own folder are the same repository.
        ctx = self.ctx(self.app, self_dir=self.app)
        self.assertTrue(ctx.target_is_self)
        text = ctx.summary()
        self.assertIn("supported layout", text)
        for phrase in ("nothing to build", "no project to build", "WARNING"):
            self.assertNotIn(phrase, text)
            self.assertNotIn(phrase, ctx.prompt_block())

    def test_the_prompt_says_who_she_is_and_what_she_builds(self) -> None:
        ctx = pc.build_context(
            target_dir=self.app,
            authorized_branch="freebuff/abc",
            self_dir=self.bot,
            project_name="Hustler",
            builder_name="Freebuff",
            bot_name="Melisa",
        )
        block = ctx.prompt_block()
        self.assertIn("Melisa, the repo manager", block)
        self.assertIn("'Hustler'", block)
        self.assertIn("freebuff/abc", block)
        self.assertIn("Freebuff", block)
        self.assertIn("protected", block)


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.bot = make_repo(self.root, "curator", files=("mac-builder/bot.py",))
        self.app = make_repo(
            self.root, "Hustler", branch="freebuff/abc", files=("ios-app/project.yml",)
        )

    def ctx(self, target: Path, *, self_dir: Path | None = None) -> pc.ProjectContext:
        return pc.build_context(
            target_dir=target,
            authorized_branch="freebuff/abc",
            self_dir=self_dir or self.bot,
        )

    def test_a_target_with_the_project_and_the_branch_passes(self) -> None:
        pc.preflight(self.ctx(self.app))

    def test_her_own_checkout_passes_when_it_is_the_project(self) -> None:
        # The Mac layout: mac-builder/ inside the Hustler checkout. Sharing the
        # checkout is normal and must not be refused.
        repo = make_repo(
            self.root,
            "Hustler-mac",
            branch="freebuff/abc",
            files=("ios-app/project.yml", "mac-builder/bot.py"),
        )
        ctx = self.ctx(repo, self_dir=repo)
        self.assertTrue(ctx.target_is_self)
        pc.preflight(ctx)
        self.assertNotIn("nothing to build", ctx.prompt_block())
        self.assertNotIn("nothing to build", ctx.summary())

    def test_refuses_a_target_without_the_project(self) -> None:
        with self.assertRaises(pc.PreflightError) as caught:
            pc.preflight(self.ctx(self.bot))
        message = str(caught.exception)
        self.assertNotIn("\n", message)
        self.assertIn("ios-app/project.yml", message)
        self.assertIn("point REPO_DIR", message)

    def test_refuses_an_authorized_branch_that_is_not_local(self) -> None:
        ctx = pc.build_context(
            target_dir=self.app,
            authorized_branch="freebuff/not-there",
            self_dir=self.bot,
        )
        with self.assertRaises(pc.PreflightError) as caught:
            pc.preflight(ctx)
        message = str(caught.exception)
        self.assertNotIn("\n", message)
        self.assertIn("not a local branch", message)


class PromptTests(unittest.TestCase):
    def brief(self, **overrides: str) -> ReviewBrief:
        values = dict(
            task="Make the scoreboard show net pay per hour.",
            diff="diff --git a/a.swift b/a.swift\n+let x = 1\n",
            diff_lines=1,
            status=" M a.swift",
            changed_files=("a.swift",),
            check_state="passed",
            check_tail="Executed 131 tests",
            test_state="unavailable (scheme has no test action)",
            test_tail="scheme has no test action",
            round_number=0,
        )
        values.update(overrides)
        return ReviewBrief(**values)  # type: ignore[arg-type]

    def test_review_system_prompt_carries_the_project_block(self) -> None:
        messages = build_review_messages(
            self.brief(project_context="Target repo: https://github.com/o/Hustler.git")
        )
        system = messages[0]["content"]
        self.assertIn("repo manager", system)
        self.assertIn("PROJECT\nTarget repo: https://github.com/o/Hustler.git", system)

    def test_review_system_prompt_has_no_project_section_without_a_context(self) -> None:
        system = build_review_messages(self.brief())[0]["content"]
        self.assertNotIn("\nPROJECT\n", system)

    def test_chat_prompt_makes_her_the_repo_manager(self) -> None:
        self.assertIn("repo manager", manager.CHAT_SYSTEM_PROMPT)
        self.assertIn("mac-builder", manager.CHAT_SYSTEM_PROMPT)
        self.assertIn("protected", manager.CHAT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
