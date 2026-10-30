"""Slash-command sync: a failure is logged, a 50001 falls back globally, never a crash."""

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
OTHER_GUILD_ID = 987654321


def forbidden(code: int = 50001, message: str = "Missing Access") -> discord.Forbidden:
    return discord.Forbidden(mock.Mock(status=403), {"code": code, "message": message})


def server_error() -> discord.HTTPException:
    return discord.HTTPException(mock.Mock(status=503), {"message": "upstream"})


class FakeTree:
    """The bits of app_commands.CommandTree that sync_commands touches."""

    def __init__(
        self,
        error: BaseException | None = None,
        count: int = 2,
        global_error: BaseException | None = None,
    ) -> None:
        self.error = error
        self.global_error = global_error
        self.count = count
        self.copies: list[int] = []
        self.syncs: list[int | None] = []

    def copy_global_to(self, *, guild: discord.Object) -> None:
        self.copies.append(guild.id)

    async def sync(self, guild: discord.Object | None = None) -> list[str]:
        self.syncs.append(guild.id if guild is not None else None)
        error = self.error if guild is not None else self.global_error
        if error is not None:
            raise error
        return ["command"] * self.count


class FakeHTTP:
    """The one REST call the diagnostics makes; it works before READY."""

    def __init__(
        self,
        guilds: list[dict[str, object]] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.guilds = guilds if guilds is not None else []
        self.error = error
        self.calls: list[tuple[int, bool]] = []

    async def get_guilds(
        self, limit: int = 200, *args: object, with_counts: bool = True
    ) -> list[dict[str, object]]:
        self.calls.append((limit, with_counts))
        if self.error is not None:
            raise self.error
        return self.guilds


def make_bot(
    *,
    guild_id: int | None = GUILD_ID,
    tree: FakeTree | None = None,
    http: FakeHTTP | None = None,
):
    instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
    instance.settings = SimpleNamespace(guild_id=guild_id)
    instance.tree = tree if tree is not None else FakeTree()
    instance.http = http if http is not None else FakeHTTP()
    # Deliberately no gateway connection: setup_hook runs before READY, so the
    # guild cache (a read-only property over self._connection) is unusable here.
    # A diagnostic that touched self.guilds would raise AttributeError.
    return instance


class SyncFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_50001_guild_sync_falls_back_to_a_global_sync(self) -> None:
        tree = FakeTree(error=forbidden())
        instance = make_bot(tree=tree)

        with self.assertLogs("macbuilder", level="WARNING") as logs:
            await instance.sync_commands()

        self.assertEqual(tree.syncs, [GUILD_ID, None])
        joined = "\n".join(logs.output)
        self.assertIn("50001", joined)
        self.assertIn("Missing Access", joined)
        self.assertIn("applications.commands", joined)
        self.assertIn("Retrying once globally", joined)

    async def test_a_forbidden_guild_sync_with_another_code_does_not_fall_back(self) -> None:
        tree = FakeTree(error=forbidden(50013, "Missing Permissions"))
        instance = make_bot(tree=tree)

        with self.assertLogs("macbuilder", level="WARNING") as logs:
            await instance.sync_commands()

        self.assertEqual(tree.syncs, [GUILD_ID])
        joined = "\n".join(logs.output)
        self.assertIn("50013", joined)
        self.assertNotIn("Retrying once globally", joined)

    async def test_the_global_fallback_failing_is_also_only_a_warning(self) -> None:
        tree = FakeTree(error=forbidden(), global_error=server_error())
        instance = make_bot(tree=tree)

        with self.assertLogs("macbuilder", level="WARNING") as logs:
            await instance.sync_commands()

        self.assertEqual(tree.syncs, [GUILD_ID, None])
        joined = "\n".join(logs.output)
        self.assertIn("globally", joined)
        self.assertIn("HTTP 503", joined)

    async def test_the_global_sync_failing_is_also_only_a_warning(self) -> None:
        tree = FakeTree(global_error=server_error())
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

        with self.assertLogs("macbuilder", level="INFO") as logs:
            await instance.sync_commands()

        self.assertEqual(tree.copies, [])
        self.assertEqual(tree.syncs, [None])
        self.assertIn(
            "published 2 application commands globally", "\n".join(logs.output)
        )


class GuildDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_diagnostics_name_the_configured_guild(self) -> None:
        http = FakeHTTP([{"id": GUILD_ID, "name": "Curator"}])
        instance = make_bot(http=http)

        with self.assertLogs("macbuilder", level="INFO") as logs:
            await instance.sync_commands()

        joined = "\n".join(logs.output)
        self.assertIn("Curator", joined)
        self.assertIn(str(GUILD_ID), joined)
        self.assertIn("visible to the bot", joined)
        # with_counts=False: no member counts in the log.
        self.assertEqual(http.calls, [(200, False)])

    async def test_the_diagnostics_do_not_need_the_guild_cache(self) -> None:
        http = FakeHTTP(
            [
                {"id": OTHER_GUILD_ID, "name": "Elsewhere"},
                {"id": 4242, "name": "Second"},
            ]
        )
        instance = make_bot(http=http)

        with self.assertLogs("macbuilder", level="INFO") as logs:
            await instance.sync_commands()

        joined = "\n".join(logs.output)
        self.assertIn(
            f"GUILD_ID={GUILD_ID} is not one of the 2 guild(s) this bot is in", joined
        )
        self.assertIn("Elsewhere", joined)
        self.assertIn("Second", joined)

    async def test_a_failing_diagnostic_still_lets_the_sync_run(self) -> None:
        http = FakeHTTP(error=server_error())
        tree = FakeTree(count=4)
        instance = make_bot(tree=tree, http=http)

        with self.assertLogs("macbuilder", level="INFO") as logs:
            await instance.sync_commands()

        joined = "\n".join(logs.output)
        self.assertIn("could not list the bot's guilds", joined)
        self.assertIn("HTTPException", joined)
        self.assertEqual(tree.syncs, [GUILD_ID])
        self.assertIn("synced 4 application commands", joined)


if __name__ == "__main__":
    unittest.main()
