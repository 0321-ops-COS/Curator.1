"""The /selftest: every stage on fakes, never the live system.

Each stage runs one real module against a fake input — a built-in sheet, a fake
review, a fake clock and channel, a mocked GitHub transport — and reports pass or
fail with one line of evidence. Nothing here starts Freebuff, calls GitHub, writes
a repository, pushes, merges or queues anything, so the report says what it proved
and names what it cannot.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Sequence

import httpx

import github_client
import merge_command
import roadmap
import status_board
import test_card

SHA = "a" * 40
JOB_ID = "20261102-101500-selftest"
# A placeholder, not a credential: the transport below never opens a socket.
TOKEN = "selftest-placeholder-not-a-token"
SAMPLE_SHEET = """# Selftest sheet
## Job
Add a share button to the home screen.
kind: task
## Job 2: the parser
Refactor the parser and keep its tests green.
kind: dan
"""


@dataclass(frozen=True)
class Stage:
    """One stage's verdict: its name, whether it passed, and the evidence."""

    name: str
    ok: bool
    evidence: str


def report(stages: Sequence[Stage]) -> str:
    """One line per stage, then what the run proves and what it cannot."""
    lines = [f"{'✅' if s.ok else '❌'} {s.name}: {'pass' if s.ok else 'FAIL'} — {s.evidence}"
             for s in stages]
    failed = [s.name for s in stages if not s.ok]
    lines.append(f"Proven: nothing — failed: {', '.join(failed)}." if failed else
                 "Proven: the sheet parser, the report and test card, the board and its pings, "
                 "the merge gates with a mocked transport and the allowlist all ran on fakes.")
    lines.append("Not proven: live Freebuff, live Discord delivery, live GitHub, "
                 "simulator, restart.")
    return "\n".join(lines)[:1900]


def fake_outcome() -> SimpleNamespace:
    """A review as the report and the test card read it; nothing was run."""
    job = SimpleNamespace(id=JOB_ID, kind="dan", branch="dan/selftest",
                          repo_slug="0321-ops-COS/Curator.1",
                          text="Add a share button.\n\n## Acceptance\n1. On the home screen.\n")
    return SimpleNamespace(job=job, check_state="CHECK PASSED", test_state="not run",
                           diff_lines=12, changed_files=("bot.py",), snapshot=SHA + ":c0ffee",
                           environment=SimpleNamespace(branch="dan/selftest"))


async def stage_intake() -> Stage:
    """1. Intake: the built-in sheet parses into two jobs."""
    steps = roadmap.parse(SAMPLE_SHEET)
    kinds = ", ".join(step.kind for step in steps)
    return Stage("Intake", len(steps) == 2, f"roadmap.parse read {len(steps)} jobs: {kinds}")


async def stage_report(report_text: object) -> Stage:
    """2. The dan report and the test card, from a fake review outcome."""
    outcome = fake_outcome()
    text, card = report_text(outcome), test_card.card(outcome.job.text)  # type: ignore[operator]
    ok = "Not verified:" in text and f"`{JOB_ID}`" in text and card.startswith("**For you")
    return Stage("Report and test card", ok,
                 f"{len(text)} char report on `{JOB_ID}`, card with "
                 f"{card.count(chr(10))} step(s)")


class FakeBoardTransport(status_board.BoardTransport):
    """A channel that records everything the board would post or edit."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    async def send(self, channel_id: int, text: str) -> str:
        self.posted.append(text)
        return str(len(self.posted))

    async def edit(self, channel_id: int, message_id: str, text: str) -> bool:
        self.posted.append(text)
        return True


async def stage_board() -> Stage:
    """3. Board and pings: every stage drawn, then one digest on a fake clock."""
    clock, transport = [1000.0], FakeBoardTransport()
    publisher = status_board.BoardPublisher(
        transport, edit_min_seconds=0.0, stall_ping_seconds=0.0, pings=True,
        digest_seconds=30.0, now=lambda: clock[0], wall=lambda: clock[0])
    publisher.track(JOB_ID, channel_id=1, requester_id=42, kind="dan")
    count, drawn = len(status_board.PROGRESS_STAGES), 0
    for stage in status_board.PROGRESS_STAGES:
        clock[0] += 20.0
        await publisher.report(JOB_ID, stage, detail=f"at {stage}")
        text = transport.posted[-1]
        drawn += (status_board.bar(status_board.step(stage)) in text
                  and status_board.stage_title(stage) in text)
    clock[0] += 20.0
    await publisher.report(JOB_ID, status_board.FREEBUFF_WORKING, detail="still working")
    before = len(transport.posted)
    clock[0] += 60.0
    await publisher.tick()
    digest = [text for text in transport.posted[before:] if "⏱" in text]
    return Stage("Board and pings", drawn == count and bool(digest),
                 f"{drawn}/{count} stage bars drawn, digest ping: "
                 f"{digest[0] if digest else 'none'}")


def _client(calls: list, ci: str = "success") -> github_client.GitHubClient:
    """A GitHub transport that answers from memory and records every call."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "PUT":
            return httpx.Response(200, json={"merged": True})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={
                "check_runs": [{"status": "completed", "conclusion": ci}]})
        if path.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "statuses": []})
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json={"mergeable": True, "mergeable_state": "clean",
                                             "head": {"sha": SHA}})
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[{"number": 7}])
        return httpx.Response(404, json={})

    return github_client.GitHubClient(
        "0321-ops-COS", "Curator.1", TOKEN, transport=httpx.MockTransport(handler))


JOB_FACTS = dict(id=JOB_ID, kind="dan", status="approved", branch="dan/selftest", base="origin/main",
                 check_state="passed", diff_lines=12, merge_dry_run_sha=SHA,
                 changed_paths=("bot.py",), repo_slug="0321-ops-COS/Curator.1")


async def stage_gates() -> Stage:
    """4. Gates: a passing dry run merges nothing; a failing CI is refused."""
    settings, dry_calls, fail_calls = (
        SimpleNamespace(max_diff_lines=400, authorized_branch="freebuff/base"), [], [])
    dry = await merge_command.run_merge(SimpleNamespace(**JOB_FACTS), settings, dry_run=True,
                                        client=_client(dry_calls))
    failed = await merge_command.run_merge(SimpleNamespace(**JOB_FACTS), settings, dry_run=False,
                                           client=_client(fail_calls, ci="failure"))
    puts = [call for call in dry_calls + fail_calls if call[0] == "PUT"]
    ok = bool(dry.dry_run_sha == SHA and not failed.merged and not puts and "❌ CI:" in failed.text)
    return Stage("Gates", ok, f"dry run returned `{dry.dry_run_sha[:7]}` and failing CI was "
                              f"refused, with {len(puts)} merge call(s)")


class FakeDeniedInteraction:
    """A Discord interaction from a user who is not on the allowlist."""

    def __init__(self, user_id: int) -> None:
        self.sent: list[str] = []
        self.user = SimpleNamespace(id=user_id)
        self.response = SimpleNamespace(is_done=lambda: False, send_message=self._send)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, text: str, **_kwargs: object) -> None:
        self.sent.append(text)


async def stage_guards(bot_instance: object) -> Stage:
    """5. Guards: /merge, /roadmap and /selftest all refuse a non-allowed user."""
    allowed = getattr(getattr(bot_instance, "settings", None), "allowed_user_ids", frozenset())
    denied = next(user for user in itertools.count(1) if user not in allowed)
    commands = [bot_instance.cmd_merge, bot_instance.cmd_roadmap,  # type: ignore[attr-defined]
                bot_instance.cmd_selftest]  # type: ignore[attr-defined]
    refused = 0
    for command in commands:
        fake = FakeDeniedInteraction(denied)
        await command(fake)
        refused += len(fake.sent) == 1 and "allowlist" in fake.sent[0]
    return Stage("Guards", refused == len(commands),
                 f"{refused}/{len(commands)} commands refused user {denied} "
                 "with the allowlist line")


async def run(bot_instance: object, report_text: object) -> list[Stage]:
    """Every stage once, in order; a stage that raises is a failure, not a crash."""
    stages: list[Stage] = []
    for name, runner in (("Intake", stage_intake),
                         ("Report and test card", lambda: stage_report(report_text)),
                         ("Board and pings", stage_board),
                         ("Gates", stage_gates),
                         ("Guards", lambda: stage_guards(bot_instance))):
        try:
            stages.append(await runner())
        except Exception as exc:  # a broken stage is a finding, not a crash
            stages.append(Stage(name, False, f"raised {type(exc).__name__}: {exc}"))
    return stages
