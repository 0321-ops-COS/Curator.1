"""Every screen Freebuff can show, and the promise that none is silent.

The classifier is a pure function on captured text, so the fixtures here are
sanitized panes. No tmux, no Discord, no network and no real secret: the tokens
below are invented shapes, and the sign-in code is deliberately the sort of
short human code that must survive redaction.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import freebuff_runner  # noqa: E402
import jobs  # noqa: E402
import screens  # noqa: E402
from freebuff_runner import (  # noqa: E402
    CommandResult,
    FreebuffError,
    FreebuffRunner,
    TaskEnvironment,
)

# Token shapes only: nothing here is a real credential.
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"
API_KEY = "sk-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2"
LONG_TOKEN = "sess_" + "a1B2" * 9
HUMAN_CODE = "ABCD-1234"

SIGN_IN_PANE = """\
╭──────────────────────────────────────╮
│ Sign in to Freebuff                  │
│ Press Enter to open the browser      │
│ then enter the code {code}     │
╰──────────────────────────────────────╯
""".format(code=HUMAN_CODE)

RATE_LIMIT_PANE = """\
You have reached your usage limit for today.
Try again in 3 hours, or upgrade for a higher quota.
"""

UPDATE_PANE = """\
A new version of Freebuff is available.
Run brew upgrade freebuff to continue.
"""

CONFIRM_PANE = """\
Overwrite src/App.tsx?
[y/N]
"""

CRASH_PANE = """\
Traceback (most recent call last):
  File "app.py", line 3, in <module>
RuntimeError: freebuff-exploded
"""

READY_PANE = """\
Freebuff • high · ~/worktrees/job
> 
"""

CODING_PANE = """\
src/App.tsx        | 12 ++++++++++--
tests/app.test.ts  |  4 ++--
Saved 2 files. Running swift test...
[check] 309 tests passed
"""

SURVEY_PANE = """\
╭──────────────────────────────────────────────────╮
│  quick question 1/11                             │
│  How did you hear about Freebuff?                │
│  ✕ esc not now                                   │
╰──────────────────────────────────────────────────╯
"""


# ------------------------------------------------------------------ fixtures


class ClassifierTests(unittest.TestCase):
    def test_every_kind_is_reachable(self) -> None:
        self.assertEqual(len(screens.KINDS), 10)

    def test_a_question_box_is_left_to_the_question_parser(self) -> None:
        pane = (
            "Some questions for you\n"
            "│ ▼ 1. Which tone?\n"
            "│   ○ Formal\n"
            "│   ○ Casual\n"
        )
        self.assertEqual(screens.classify(pane).kind, screens.QUESTION_BOX)

    def test_the_survey_popup_is_its_own_kind(self) -> None:
        self.assertEqual(screens.classify(SURVEY_PANE).kind, screens.SURVEY)

    def test_a_sign_in_screen(self) -> None:
        screen = screens.classify(SIGN_IN_PANE)
        self.assertEqual(screen.kind, screens.SIGN_IN)
        self.assertTrue(screen.needs_owner)
        self.assertIn("sign-in", screen.title)

    def test_a_rate_limit_screen(self) -> None:
        self.assertEqual(screens.classify(RATE_LIMIT_PANE).kind, screens.RATE_LIMIT)

    def test_an_update_prompt(self) -> None:
        self.assertEqual(screens.classify(UPDATE_PANE).kind, screens.UPDATE_PROMPT)

    def test_a_confirmation_prompt(self) -> None:
        self.assertEqual(
            screens.classify(CONFIRM_PANE).kind, screens.CONFIRM_PROMPT
        )

    def test_a_crash_screen(self) -> None:
        self.assertEqual(
            screens.classify(CRASH_PANE).kind, screens.ERROR_OR_CRASH
        )

    def test_the_ready_prompt(self) -> None:
        self.assertEqual(screens.classify(READY_PANE).kind, screens.READY_PROMPT)

    def test_normal_coding_output_is_working_not_an_error(self) -> None:
        # The false positive that would ruin every job: a diff, a test run and a
        # stack-trace-free failure message are not a crash screen.
        screen = screens.classify(CODING_PANE)
        self.assertEqual(screen.kind, screens.WORKING)
        self.assertFalse(screen.needs_owner)

    def test_a_failed_test_line_is_not_a_crash(self) -> None:
        pane = "FAILED tests/test_app.py::test_x - AssertionError: 1 != 2\n2 failed, 307 passed\n"
        self.assertEqual(screens.classify(pane).kind, screens.WORKING)

    def test_an_empty_pane_is_unknown(self) -> None:
        self.assertEqual(screens.classify("").kind, screens.UNKNOWN)
        self.assertEqual(screens.classify("   \n\n").kind, screens.UNKNOWN)
        self.assertTrue(screens.classify("").empty)

    def test_only_a_person_screen_needs_the_owner(self) -> None:
        for kind in screens.KINDS:
            expected = kind in (
                screens.QUESTION_BOX,
                screens.SIGN_IN,
                screens.RATE_LIMIT,
                screens.UPDATE_PROMPT,
                screens.CONFIRM_PROMPT,
                screens.ERROR_OR_CRASH,
            )
            self.assertEqual(screens.waits_for_owner(kind), expected, kind)

    def test_the_screen_key_ignores_trailing_whitespace(self) -> None:
        first = screens.classify(SIGN_IN_PANE)
        second = screens.classify(SIGN_IN_PANE.replace("\n", "   \n"))
        self.assertEqual(first.key, second.key)
        self.assertNotEqual(first.key, screens.classify(RATE_LIMIT_PANE).key)

    def test_exit_summaries_name_what_was_on_screen(self) -> None:
        self.assertIn(
            "rate-limit", screens.exit_summary(screens.classify(RATE_LIMIT_PANE))
        )
        self.assertIn("sign-in", screens.exit_summary(screens.classify(SIGN_IN_PANE)))
        self.assertIn(
            "error", screens.exit_summary(screens.classify(CRASH_PANE))
        )
        self.assertIn("normally", screens.exit_summary(screens.classify(READY_PANE)))


# ----------------------------------------------------------------- redaction


class RedactionTests(unittest.TestCase):
    def test_a_long_token_is_masked(self) -> None:
        text = screens.redact_pane(f"session key {LONG_TOKEN} saved")
        self.assertNotIn(LONG_TOKEN, text)
        self.assertIn("[redacted]", text)

    def test_a_bearer_value_is_masked(self) -> None:
        text = screens.redact_pane("Authorization: Bearer abcdef123456ghijkl")
        self.assertNotIn("abcdef123456ghijkl", text)

    def test_an_api_key_shape_is_masked(self) -> None:
        for value in (API_KEY, TOKEN):
            text = screens.redact_pane(f"using {value} now")
            self.assertNotIn(value, text, value)

    def test_a_short_human_code_survives_because_the_owner_needs_it(self) -> None:
        text = screens.redact_pane(SIGN_IN_PANE)
        self.assertIn(HUMAN_CODE, text)

    def test_paths_stay_readable(self) -> None:
        text = screens.redact_pane("src/features/screen-handling/components/Panel.tsx")
        self.assertIn("src/features/screen-handling/components/Panel.tsx", text)

    def test_sanitize_strips_escape_codes_and_keeps_the_newest_text(self) -> None:
        pane = "\x1b[31mred\x1b[0m\n" + "x" * 3000
        text = screens.sanitize(pane, limit=100)
        self.assertNotIn("\x1b", text)
        self.assertTrue(text.startswith("[...]"))
        self.assertLessEqual(len(text), 120)


# --------------------------------------------------------------------- keys


class KeyTests(unittest.TestCase):
    def test_the_allowlist_has_a_sequence_for_every_name(self) -> None:
        for name in screens.KEY_NAMES:
            self.assertIsNotNone(screens.key_sequence(name), name)
        self.assertEqual(screens.key_sequence("Esc"), "Escape")
        self.assertEqual(screens.key_sequence("escape"), "Escape")
        self.assertEqual(screens.key_sequence(" y "), "y")

    def test_anything_else_is_not_a_key(self) -> None:
        for value in ("q", "C-c", "rm -rf ~", "Enter; rm -rf ~", "", "  ", "\x1b"):
            self.assertIsNone(screens.key_sequence(value), value)
        self.assertIsNone(screens.key_sequence(None))  # type: ignore[arg-type]

    def test_send_text_is_one_line_and_capped(self) -> None:
        self.assertEqual(screens.flatten_send("a\nb\r\nc"), "a b c")
        self.assertEqual(
            len(screens.flatten_send("x" * 5000)), screens.MAX_SEND_CHARS
        )
        self.assertEqual(screens.flatten_send("\x1b[31mhi\x1b[0m"), "hi")


# ------------------------------------------------------------------- runner


def runner_settings(**overrides: object) -> SimpleNamespace:
    values = dict(
        repo_dir=Path("/nonexistent/repo"),
        worktree_root=Path("/nonexistent/worktrees"),
        freebuff_timeout_seconds=30.0,
        freebuff_idle_seconds=5.0,
        freebuff_poll_seconds=0.02,
        freebuff_ready_seconds=1.0,
        question_wait_seconds=10.0,
        question_auto_answer=False,
        question_debug=False,
        stall_seconds=90.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def task_env() -> TaskEnvironment:
    return TaskEnvironment(
        branch="freebuff/deadbeef",
        worktree=Path("/nonexistent/worktree"),
        session="macbuilder-test",
        base_branch="freebuff/base",
        signature_at_start="a",
    )


class _Repo:
    """A repo whose signature does what the test says, one call at a time."""

    def __init__(self, *values: str) -> None:
        self.values = list(values) or [""]
        self.calls = 0

    async def signature(self, cwd: Path) -> str:
        self.calls += 1
        return self.values[min(self.calls - 1, len(self.values) - 1)]


class FakeTerminal(FreebuffRunner):
    """A runner whose pane is a script, with no tmux anywhere."""

    def __init__(
        self, settings: object, panes: list[str], *, alive: bool = True
    ) -> None:
        super().__init__(settings)
        self.panes = list(panes)
        self.reads = 0
        self.alive = alive
        self.keys: list[str] = []
        self.pasted: list[str] = []

    async def _tmux(
        self, *args: str, input_bytes: bytes | None = None
    ) -> CommandResult:
        if args and args[0] == "send-keys":
            self.keys.append(args[-1])
        return CommandResult(0, "", "")

    async def _session_alive(self, session: str) -> bool:
        return self.alive

    async def _pane(self, session: str) -> str:
        pane = self.panes[min(self.reads, len(self.panes) - 1)]
        self.reads += 1
        return pane

    async def _send_text(self, session: str, text: str) -> None:
        self.pasted.append(text)


class ScreenRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log_path = Path(tmp.name) / "job.log"
        patcher = mock.patch.object(freebuff_runner, "READY_POLL_SECONDS", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)
        delay = mock.patch.object(freebuff_runner, "KEY_DELAY_SECONDS", 0)
        delay.start()
        self.addCleanup(delay.stop)

    def reporter(self) -> tuple[list[tuple[str, str]], object]:
        posted: list[tuple[str, str]] = []

        async def report(screen: screens.Screen, tail: str) -> None:
            posted.append((screen.kind, tail))

        return posted, report

    async def test_a_stuck_sign_in_screen_is_posted_once_and_expires(self) -> None:
        runner = FakeTerminal(
            runner_settings(stall_seconds=0.1, question_wait_seconds=0.3),
            [SIGN_IN_PANE],
        )
        runner.repo = _Repo("a")
        posted, report = self.reporter()
        runner.screen_reporter = report

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path), timeout=15
        )

        self.assertEqual(reason, "stalled")
        self.assertEqual([kind for kind, _tail in posted], [screens.SIGN_IN])
        self.assertIn(HUMAN_CODE, posted[0][1], "the owner needs the code")
        self.assertEqual(runner.keys[-1], "Escape", "an expired screen is dismissed")
        self.assertEqual(runner.last_stall_kind, screens.SIGN_IN)
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("screen sign_in instead of progress", log)
        self.assertIn("did not change within", log)
        self.assertIn("sending Esc", log)

    async def test_the_clocks_pause_while_a_screen_waits(self) -> None:
        # The hard timeout is 0.3s and the wait on the screen is 0.5s: if the
        # deadline were not pushed out by the wait, this would be a timeout.
        runner = FakeTerminal(
            runner_settings(
                stall_seconds=0.1,
                question_wait_seconds=0.5,
                freebuff_timeout_seconds=0.3,
            ),
            [SIGN_IN_PANE],
        )
        runner.repo = _Repo("a")
        posted, report = self.reporter()
        runner.screen_reporter = report

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path), timeout=20
        )

        self.assertEqual(reason, "stalled")
        self.assertEqual(len(posted), 1)
        log = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("timed out after", log)

    async def test_a_screen_that_changes_after_the_post_is_not_stuck(self) -> None:
        runner = FakeTerminal(
            runner_settings(stall_seconds=0.1, freebuff_idle_seconds=0.2),
            [SIGN_IN_PANE] * 8 + [READY_PANE] * 40,
        )
        runner.repo = _Repo("a", "b")
        posted, report = self.reporter()
        runner.screen_reporter = report

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path), timeout=20
        )

        self.assertEqual(reason, "idle")
        self.assertEqual(len(posted), 1)
        self.assertNotIn("Escape", runner.keys)

    async def test_a_session_exit_names_the_screen_it_left(self) -> None:
        runner = FakeTerminal(
            runner_settings(stall_seconds=5), [RATE_LIMIT_PANE], alive=False
        )
        runner.repo = _Repo("a")

        reason = await asyncio.wait_for(
            runner.wait_for_changes(task_env(), self.log_path), timeout=15
        )

        self.assertEqual(reason, "exited")
        self.assertIn("rate-limit", runner.exit_note)
        log = self.log_path.read_text(encoding="utf-8")
        self.assertIn("tmux session exited", log)

    async def test_the_task_is_not_pasted_into_a_sign_in_screen(self) -> None:
        runner = FakeTerminal(
            runner_settings(question_wait_seconds=5),
            [SIGN_IN_PANE, SIGN_IN_PANE, READY_PANE],
        )
        posted, report = self.reporter()
        runner.screen_reporter = report

        await asyncio.wait_for(
            runner._wait_ready(task_env(), self.log_path), timeout=15
        )

        self.assertEqual([kind for kind, _tail in posted], [screens.SIGN_IN])
        self.assertEqual(runner.pasted, [], "nothing may be typed into a sign-in")
        self.assertIsNone(runner.pending_screen, "ready clears the waiting screen")

    async def test_the_launch_pastes_the_task_only_after_the_screen_clears(self) -> None:
        runner = FakeTerminal(
            runner_settings(question_wait_seconds=5), [SIGN_IN_PANE, READY_PANE]
        )
        runner.repo = _Repo("a")
        posted, report = self.reporter()
        runner.screen_reporter = report
        env = task_env()

        await asyncio.wait_for(
            runner._launch(env, ["freebuff"], "do the thing", self.log_path),
            timeout=15,
        )

        self.assertEqual(runner.pasted, ["do the thing"])
        self.assertEqual([kind for kind, _tail in posted], [screens.SIGN_IN])

    async def test_a_sign_in_screen_that_never_clears_fails_before_typing(self) -> None:
        runner = FakeTerminal(
            runner_settings(ready_seconds=0.2, question_wait_seconds=0.3),
            [SIGN_IN_PANE],
        )
        posted, report = self.reporter()
        runner.screen_reporter = report

        with self.assertRaises(FreebuffError) as caught:
            await asyncio.wait_for(
                runner._wait_ready(task_env(), self.log_path), timeout=15
            )

        self.assertIn("sign-in screen", str(caught.exception))
        self.assertEqual(len(posted), 1)
        self.assertEqual(runner.pasted, [])

    async def test_a_key_is_only_sent_while_a_screen_is_waiting(self) -> None:
        runner = FakeTerminal(runner_settings(), [SIGN_IN_PANE])
        self.assertFalse(await runner.send_key("Enter"), "nothing is waiting yet")

        runner._screen = screens.classify(SIGN_IN_PANE)
        runner._screen_session = "macbuilder-test"
        runner._screen_log = self.log_path
        self.assertTrue(await runner.send_key("Enter"))
        self.assertEqual(runner.keys, ["Enter"])
        self.assertFalse(await runner.send_key("q"), "q is not in the allowlist")
        self.assertIn("[/key Enter", self.log_path.read_text(encoding="utf-8"))

    async def test_send_goes_through_the_paste_path_and_is_capped(self) -> None:
        runner = FakeTerminal(runner_settings(), [SIGN_IN_PANE])
        self.assertFalse(await runner.send_screen_text("hello"), "nothing waiting")

        runner._screen = screens.classify(RATE_LIMIT_PANE)
        runner._screen_session = "macbuilder-test"
        runner._screen_log = self.log_path
        self.assertTrue(await runner.send_screen_text("hello\nthere"))
        self.assertEqual(runner.pasted, ["hello there"])

        await runner.send_screen_text("x" * 2000)
        self.assertEqual(len(runner.pasted[-1]), screens.MAX_SEND_CHARS)

    async def test_screen_shows_the_pane_only_while_a_session_runs(self) -> None:
        runner = FakeTerminal(runner_settings(), [SIGN_IN_PANE])
        runner._screen_session = "macbuilder-test"
        pane = await runner.current_pane()
        assert pane is not None
        self.assertIn(HUMAN_CODE, pane)

        runner.alive = False
        self.assertIsNone(await runner.current_pane())


# ---------------------------------------------------------------------- bot


class _FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.deferred = 0

    async def send_message(self, content: str, **_kwargs: object) -> None:
        self.messages.append(content)

    async def defer(self, **_kwargs: object) -> None:
        self.deferred += 1


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str, **_kwargs: object) -> None:
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self, user_id: int = 7) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.channel_id = 99
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class _ScreenFreebuff:
    def __init__(
        self,
        pane: str | None = None,
        pending: screens.Screen | None = None,
        *,
        ok: bool = True,
    ) -> None:
        self.pane = pane
        self.pending_screen = pending
        self.ok = ok
        self.keys: list[str] = []
        self.sent: list[str] = []

    async def current_pane(self, limit: int | None = None) -> str | None:
        return self.pane

    async def send_key(self, name: str) -> bool:
        self.keys.append(name)
        return self.ok

    async def send_screen_text(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


class ScreenCommandTests(unittest.IsolatedAsyncioTestCase):
    def instance(self, freebuff: object, *, allowed: bool = True) -> bot.MacBuilderBot:
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(log_dir=Path("/nonexistent/logs"))
        instance.freebuff = freebuff

        async def check(_interaction: object) -> bool:
            return allowed

        instance.require_allowed = check
        return instance

    async def test_screen_shows_the_pane_and_the_waiting_kind(self) -> None:
        pending = screens.classify(SIGN_IN_PANE)
        instance = self.instance(_ScreenFreebuff(pane=SIGN_IN_PANE, pending=pending))
        interaction = _FakeInteraction()

        await instance.cmd_screen(interaction)

        sent = interaction.followup.messages[0]
        self.assertIn("sign_in", sent)
        self.assertIn(HUMAN_CODE, sent)

    async def test_screen_says_so_when_no_session_runs(self) -> None:
        instance = self.instance(_ScreenFreebuff(pane=None))
        interaction = _FakeInteraction()

        await instance.cmd_screen(interaction)

        self.assertIn("No Freebuff session", interaction.followup.messages[0])

    async def test_key_refuses_anything_outside_the_allowlist(self) -> None:
        freebuff = _ScreenFreebuff()
        instance = self.instance(freebuff)
        interaction = _FakeInteraction()

        await instance.cmd_key(interaction, "q")
        await instance.cmd_key(interaction, "rm -rf ~")

        self.assertEqual(freebuff.keys, [], "nothing outside the list is a key")
        self.assertIn("Enter", interaction.response.messages[0])

    async def test_key_sends_an_allowlisted_one(self) -> None:
        freebuff = _ScreenFreebuff()
        instance = self.instance(freebuff)
        interaction = _FakeInteraction()

        await instance.cmd_key(interaction, "Tab")

        self.assertEqual(freebuff.keys, ["Tab"])
        self.assertIn("Sent", interaction.response.messages[0])

    async def test_key_with_nothing_waiting_says_so(self) -> None:
        instance = self.instance(_ScreenFreebuff(ok=False))
        interaction = _FakeInteraction()

        await instance.cmd_key(interaction, "Enter")

        self.assertIn("No Freebuff session is waiting", interaction.response.messages[0])

    async def test_send_refuses_text_over_the_cap(self) -> None:
        freebuff = _ScreenFreebuff()
        instance = self.instance(freebuff)
        interaction = _FakeInteraction()

        await instance.cmd_send(interaction, "x" * (screens.MAX_SEND_CHARS + 1))

        self.assertEqual(freebuff.sent, [])
        self.assertIn(str(screens.MAX_SEND_CHARS), interaction.response.messages[0])

    async def test_send_pastes_one_line(self) -> None:
        freebuff = _ScreenFreebuff()
        instance = self.instance(freebuff)
        interaction = _FakeInteraction()

        await instance.cmd_send(interaction, "continue\nplease")

        self.assertEqual(freebuff.sent, ["continue please"])

    async def test_every_screen_command_checks_the_allowlist(self) -> None:
        for call in ("screen", "key", "send"):
            freebuff = _ScreenFreebuff(pane=SIGN_IN_PANE)
            instance = self.instance(freebuff, allowed=False)
            interaction = _FakeInteraction()
            if call == "screen":
                await instance.cmd_screen(interaction)
            elif call == "key":
                await instance.cmd_key(interaction, "Enter")
            else:
                await instance.cmd_send(interaction, "hello")
            self.assertEqual(freebuff.keys, [], call)
            self.assertEqual(freebuff.sent, [], call)
            self.assertEqual(interaction.response.messages, [], call)
            self.assertEqual(interaction.followup.messages, [], call)


# --------------------------------------------------------------------- jobs


class _StuckFreebuff:
    """A runner stand-in that stops on a screen, with no session at all."""

    def __init__(self, reason: str, *, stall_kind: str = "", exit_note: str = "") -> None:
        self.reason = reason
        self.last_stall_kind = stall_kind
        self.exit_note = exit_note
        self.aborted = False
        self.screen_reporter = None

    async def start_task(
        self, *, job_id: str, task_text: str, log_path: Path, kind: str = "task"
    ) -> TaskEnvironment:
        return task_env()

    async def wait_for_changes(
        self, env: TaskEnvironment, log_path: Path, **kwargs: object
    ) -> str:
        self.kwargs = kwargs
        return self.reason

    async def collect(self, env: TaskEnvironment, max_diff_lines: int) -> object:
        return SimpleNamespace(empty=True, diff="", diff_lines=0)

    async def abort(self, env: TaskEnvironment | None = None) -> None:
        self.aborted = True


class _NoManager:
    enabled = False


def make_job(tmp: str) -> jobs.Job:
    return jobs.Job(
        id="20261102-000000-deadbeef",
        kind="task",
        title="make the card bigger",
        channel_id=1,
        requester_id=2,
        log_path=Path(tmp) / "job.log",
        text="make the card bigger",
    )


class StalledJobTests(unittest.IsolatedAsyncioTestCase):
    async def run_job(self, freebuff: _StuckFreebuff) -> tuple[jobs.Job, list[str]]:
        posted: list[str] = []

        async def post_job(job: jobs.Job, ok: bool, summary: str) -> None:
            posted.append(summary)

        async def post_review(outcome: jobs.ReviewOutcome) -> None:
            raise AssertionError("a job stopped on a screen has no review")

        runner = jobs.JobRunner(
            SimpleNamespace(
                max_diff_lines=400,
                max_revise_rounds=0,
                question_wait_seconds=1800,
                question_auto_answer=False,
            ),
            _NoManager(),
            freebuff,
            post_job=post_job,
            post_review=post_review,
        )
        with tempfile.TemporaryDirectory() as tmp:
            job = make_job(tmp)
            await runner.run(job)
        return job, posted

    async def test_a_stalled_job_fails_and_names_the_screen(self) -> None:
        freebuff = _StuckFreebuff("stalled", stall_kind=screens.RATE_LIMIT)
        job, posted = await self.run_job(freebuff)

        self.assertEqual(job.status, "failed")
        self.assertTrue(freebuff.aborted)
        self.assertIn("stuck on a rate-limit screen", posted[0])
        self.assertIn("Esc", posted[0])
        self.assertIn("QUESTION_WAIT_SECONDS", posted[0])

    async def test_an_exit_says_what_the_last_screen_was(self) -> None:
        freebuff = _StuckFreebuff(
            "exited", exit_note="the session exited on a rate-limit screen"
        )
        job, posted = await self.run_job(freebuff)

        self.assertEqual(job.status, "failed")
        self.assertIn("exited; the session exited on a rate-limit screen", posted[0])

    async def test_the_reporter_belongs_to_the_job_that_installed_it(self) -> None:
        freebuff = _StuckFreebuff("stalled", stall_kind=screens.SIGN_IN)
        await self.run_job(freebuff)

        self.assertIn("on_question", freebuff.kwargs)
        self.assertIsNone(
            freebuff.screen_reporter, "a finished job leaves no reporter behind"
        )


if __name__ == "__main__":
    unittest.main()
