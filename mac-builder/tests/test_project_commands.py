"""The /project and /status text, called on the bot object without Discord."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import project_context as pc  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def make_repo(
    parent: Path, name: str, *, branch: str = "main", files: tuple[str, ...] = ()
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
    return path


def settings() -> "bot.Settings":
    env = {
        "DISCORD_TOKEN": "token",
        "ALLOWED_USER_IDS": "42",
        "PROJECT_NAME": "Hustler",
        "AUTHORIZED_BRANCH": "freebuff/abc",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        return bot.Settings.from_env()


class FakeResponse:
    def __init__(self) -> None:
        self.sent = ""
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True

    async def send_message(self, content: str, **_kwargs: object) -> None:
        self.sent = content


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str, **_kwargs: object) -> None:
        self.sent.append(content)


class FakeInteraction:
    def __init__(self, user_id: int = 42) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.channel_id = 1
        self.message = None


class FakeQueue:
    def snapshot(self) -> dict[str, object]:
        return {"running": None, "pending": 2}


class ProjectCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        repo = make_repo(
            root,
            "Hustler",
            branch="freebuff/abc",
            files=("ios-app/project.yml", "mac-builder/bot.py"),
        )
        self.ctx = pc.build_context(
            target_dir=repo,
            authorized_branch="freebuff/abc",
            self_dir=repo,
            project_name="Hustler",
        )

    def fake_bot(self, *, allowed: tuple[int, ...] = (42,)) -> "bot.MacBuilderBot":
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = replace(
            settings(), allowed_user_ids=frozenset(allowed), project=self.ctx
        )
        instance.queue = FakeQueue()
        instance.manager = SimpleNamespace(enabled=False)
        return instance

    async def test_project_command_shows_the_target_branch_and_herself(self) -> None:
        interaction = FakeInteraction()
        await self.fake_bot().cmd_project(interaction)
        text = interaction.response.sent
        for expected in (
            "Hustler",
            "freebuff/abc",
            "Freebuff",
            "mac-builder",
            "protected",
            "supported layout",
        ):
            self.assertIn(expected, text)

    async def test_project_command_refuses_a_user_who_is_not_allowlisted(self) -> None:
        interaction = FakeInteraction(user_id=7)
        await self.fake_bot(allowed=(42,)).cmd_project(interaction)
        self.assertIn("allowlist", interaction.response.sent)
        self.assertNotIn("freebuff/abc", interaction.response.sent)

    async def test_status_shows_the_project_and_the_target_repo(self) -> None:
        interaction = FakeInteraction()
        probe = mock.AsyncMock(return_value=(0, "status output"))
        with mock.patch.object(bot.jobs, "probe_status", probe):
            await self.fake_bot().cmd_status(interaction)
        text = interaction.followup.sent[-1]
        self.assertIn("project: Hustler", text)
        self.assertIn("target repo:", text)
        self.assertIn("authorized branch: freebuff/abc", text)
        self.assertIn("status.sh exit: 0", text)


if __name__ == "__main__":
    unittest.main()
