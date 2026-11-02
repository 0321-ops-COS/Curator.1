"""The gated /merge, offline: a mocked transport, no token, no network."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import github_client  # noqa: E402
import merge_command  # noqa: E402

TOKEN = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0"  # split: a fixture, not a credential
SHA = "a" * 40
SETTINGS = dict(max_diff_lines=400, authorized_branch="freebuff/base",
                allowed_user_ids=frozenset({42}),
                project=SimpleNamespace(self_remote="git@github.com:0321-ops-COS/Curator.1.git",
                                        target_remote="https://github.com/0321-ops-COS/Hustler"))
FACTS = dict(id="20260101-000000-deadbeef", kind="dan", status="approved", branch="dan/x",
             base="origin/main", check_state="passed", diff_lines=12, merge_dry_run_sha=SHA,
             repo_slug="0321-ops-COS/Curator.1", changed_paths=("ios-app/RootView.swift",))


def client(calls: list, ci: str = "success", head: str = SHA) -> github_client.GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "PUT":
            return httpx.Response(200, json={"merged": True})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [
                {"status": "completed", "conclusion": ci}]})
        if path.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "statuses": []})
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json={"mergeable": True, "mergeable_state": "clean",
                                             "head": {"sha": head}})
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[{"number": 7}])
        return httpx.Response(404, json={})

    return github_client.GitHubClient("0321-ops-COS", "Curator.1", TOKEN,
                                      transport=httpx.MockTransport(handler))


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def gates(self, *, dry_run: bool = False, ci: str = "success", head: str = SHA,
                    **over: object):
        calls: list = []
        outcome = await merge_command.run_merge(SimpleNamespace(**{**FACTS, **over}),
                                                SimpleNamespace(**SETTINGS), dry_run=dry_run,
                                                client=client(calls, ci, head))
        return outcome, [call for call in calls if call[0] == "PUT"]

    async def test_a_failed_gate_names_itself_and_merges_nothing(self) -> None:
        outcome, merged = await self.gates(ci="failure")
        self.assertEqual((outcome.merged, "❌ CI:" in outcome.text, merged), (False, True, []))

    async def test_a_dry_run_returns_the_head_and_merges_nothing(self) -> None:
        outcome, merged = await self.gates(dry_run=True, merge_dry_run_sha="")
        self.assertEqual((outcome.merged, outcome.dry_run_sha, merged,
                          "dry run, nothing merged" in outcome.text), (False, SHA, [], True))

    async def test_a_head_that_moved_after_the_dry_run_merges_nothing(self) -> None:
        outcome, merged = await self.gates(head="b" * 40)
        self.assertEqual((merged, "❌ head unchanged:" in outcome.text), ([], True))

    async def test_every_gate_passing_merges_the_expected_head_once(self) -> None:
        outcome, merged = await self.gates()
        self.assertEqual((outcome.merged, merged),
                         (True, [("PUT", "/repos/0321-ops-COS/Curator.1/pulls/7/merge")]))
        self.assertIn("Merged PR #7", outcome.text)
        self.assertIn("press Revert on that page", outcome.text)
        self.assertNotIn(TOKEN, outcome.text)

    async def test_hustler_main_is_never_merged_whatever_else_passes(self) -> None:
        outcome, merged = await self.gates(kind="task", branch="freebuff/a",
                                           repo_slug="0321-ops-COS/Hustler")
        self.assertEqual((merged, "main is never merged" in outcome.text), ([], True))


def interaction(user_id: int = 42):
    sent: list = []

    async def send(content: str, **_kwargs: object) -> None:
        sent.append(content)

    return SimpleNamespace(user=SimpleNamespace(id=user_id), followup=SimpleNamespace(send=send),
                           response=SimpleNamespace(is_done=lambda: False,
                                                    defer=mock.AsyncMock(), send_message=send)), sent


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def instance(self):
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings, instance._merge_jobs = SimpleNamespace(**SETTINGS), {}
        return instance

    async def test_a_missing_token_is_one_plain_line_and_nothing_else(self) -> None:
        fake, sent = interaction()
        with mock.patch.dict(os.environ, {}, clear=True):
            await self.instance().cmd_merge(fake, None, True)
        self.assertEqual((len(sent), "MANAGER_GITHUB_TOKEN is not set" in sent[0]), (1, True))

    async def test_a_denied_user_is_told_nothing_and_no_call_is_made(self) -> None:
        fake, sent = interaction(user_id=7)
        with mock.patch.dict(os.environ, {github_client.TOKEN_ENV: TOKEN}), mock.patch.object(
                merge_command, "run_merge", mock.AsyncMock()) as run_merge:
            await self.instance().cmd_merge(fake, None, False)
        self.assertEqual((sent[0].count("allowlist") > 0, run_merge.await_count), (True, 0))

    async def test_a_dry_run_head_is_remembered_on_the_job(self) -> None:
        fake, sent, instance = *interaction(), self.instance()
        reviewed = SimpleNamespace(**{**FACTS, "merge_dry_run_sha": ""})
        instance._remember_merge_job(reviewed)
        outcome = merge_command.MergeOutcome("dry run, nothing merged.", dry_run_sha=SHA)
        with mock.patch.dict(os.environ, {github_client.TOKEN_ENV: TOKEN}), mock.patch.object(
                merge_command, "run_merge", mock.AsyncMock(return_value=outcome)):
            await instance.cmd_merge(fake, None, True)
        self.assertEqual((reviewed.merge_dry_run_sha, "dry run" in sent[-1]), (SHA, True))


if __name__ == "__main__":
    unittest.main()
