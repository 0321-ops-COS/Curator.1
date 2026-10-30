"""A failed slash-command sync is logged, and never kills the bot at login."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import bot  # noqa: E402

GUILD_ID = 123456789


def forbidden(code: int = 50001, message: str = "Missing Access") -> discord.Forbidden:
    return discord.Forbidden(mock.Mock(status=403), {"code": code, "message": message})


class FakeTree:
    """The bits of app_commands.CommandTree that sync_commands touches."""

    def __init__(self, error: BaseException | None = None, count: int = 2) -> None:
        self.error = error
        self.count = count
        self.copies: list[int] = []
        self.syncs: list[int | None] = []

    def copy_global_to(self, *, guild: discord.Object) -> None:
        self.copies.append(guild.id)

    async def sync(self, guild: discord.Object | None = None) -> list[str]:
        self.syncs.append(guild.id if guild is not None else None)
        if self.error is not None:
            raise self.error
        return ["command"] * self.count


def make_bot(*, guild_id: int | None = GUILD_ID, tree: FakeTree | None = None):
    instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
    instance.settings = SimpleNamespace(guild_id=guild_id)
    instance.tree = tree if tree is not None else FakeTree()
    return instance


class SyncFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_forbidden_guild_sync_is_a_warning_not_a_crash(self) -> None:
        tree = FakeTree(error=forbidden())
        instance = make_bot(tree=tree)

        with self.assertLogs("macbuilder", level="WARNING") as logs:
            await instance.sync_commands()

        self.assertEqual(tree.syncs, [GUILD_ID])
        joined = "\n".join(logs.output)
        self.assertIn("50001", joined)
        self.assertIn("applications.commands", joined)
        self.assertIn("Missing Access", joined)

    async def test_the_global_sync_failing_is_also_only_a_warning(self) -> None:
        tree = FakeTree(error=discord.HTTPException(mock.Mock(status=503), {"message": "upstream"}))
        instance = make_bot(guild_id=None, tree=tree)

        with self.assertLogs("macbuilder", level="WARNING") as logs:
            await instance.sync_commands()

        self.assertEqual(tree.syncs, [None])
        self.assertIn("HTTP 503", "\n".join(logs.output))

    async def test_a_working_sync_still_uses_the_configured_guild_and_logs_the_count(self) -> None:
        tree = FakeTree(count=7)
        instance = make_bot(tree=tree)

        with self.assertLogs("macbuilder", level="INFO") as logs:
            await instance.sync_commands()

        self.assertEqual(tree.copies, [GUILD_ID])
        self.assertEqual(tree.syncs, [GUILD_ID])
        self.assertIn("synced 7 application commands", "\n".join(logs.output))

    async def test_without_a_guild_it_syncs_globally(self) -> None:
        tree = FakeTree()
        instance = make_bot(guild_id=None, tree=tree)

        with self.assertLogs("macbuilder", level="INFO"):
            await instance.sync_commands()

        self.assertEqual(tree.copies, [])
        self.assertEqual(tree.syncs, [None])


if __name__ == "__main__":
    unittest.main()
