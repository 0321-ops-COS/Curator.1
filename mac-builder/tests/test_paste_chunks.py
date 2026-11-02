"""Paste: long text in chunks, the pane checked, and nothing cut is submitted."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import freebuff_runner  # noqa: E402
from freebuff_runner import (  # noqa: E402
    CommandResult,
    FreebuffError,
    FreebuffRunner,
)

TAIL = '6 "CI": every ci_state is "'


def settings(**overrides: object) -> SimpleNamespace:
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
        freebuff_bin="freebuff",
        freebuff_args=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeBox(FreebuffRunner):
    """A runner whose tmux is an input box with the same cap as Freebuff's.

    ``keep`` is how many characters of one paste the box takes (None is all);
    the real box stops at about 2048. The pane draws the text in a bordered
    box, one line per line, so the tail check has to see through the layout.
    """

    def __init__(self, *, keep: int | None = None, blind: int = 0) -> None:
        super().__init__(settings())
        self.keep = keep
        self.blind = blind
        self.buffer = b""
        self.box = ""
        self.pasted: list[str] = []
        self.keys: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    async def _tmux(
        self, *args: str, input_bytes: bytes | None = None
    ) -> CommandResult:
        self.calls.append(args)
        if args[0] == "load-buffer":
            self.buffer = input_bytes or b""
        elif args[0] == "paste-buffer":
            value = self.buffer.decode("utf-8")
            self.pasted.append(value)
            self.box += value if self.keep is None else value[: self.keep]
        elif args[0] == "capture-pane":
            if self.blind:
                self.blind -= 1
                return CommandResult(0, "", "")
            return CommandResult(0, self.render(), "")
        elif args[0] == "send-keys":
            self.keys.append(args[-1])
        return CommandResult(0, "", "")

    def render(self) -> str:
        lines = self.box.splitlines() or [""]
        return "\n".join(f"│ {line} │" for line in lines) + "\n"


class PasteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for name, value in (
            ("PASTE_CHUNK_PAUSE_SECONDS", 0),
            ("KEY_DELAY_SECONDS", 0),
        ):
            patcher = mock.patch.object(freebuff_runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_a_6000_character_task_arrives_in_full_in_chunks(self) -> None:
        runner = FakeBox(keep=2048)
        text = "".join(f"{index}: {TAIL}\n" for index in range(300))[:6000]

        await runner._send_text("macbuilder-test", text)

        self.assertEqual("".join(runner.pasted), text, "every character, in order")
        self.assertEqual(len(runner.pasted), 6)
        chunk = freebuff_runner.PASTE_CHUNK_CHARS
        self.assertTrue(all(len(piece) <= chunk for piece in runner.pasted))
        self.assertIn("-p", [args for args in runner.calls if args[0] == "paste-buffer"][0])
        self.assertEqual(runner.keys, ["Enter"])

    async def test_a_cut_paste_is_cleared_and_never_submitted(self) -> None:
        runner = FakeBox(keep=200)
        patcher = mock.patch.object(freebuff_runner, "PASTE_CONFIRM_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Every line is distinct, so the tail can only be at the end.
        text = "".join(f"line {index}: {TAIL}\n" for index in range(60))
        with self.assertRaises(FreebuffError) as raised:
            await runner._send_text("macbuilder-test", text)

        self.assertEqual(
            str(raised.exception), "the paste was cut off; nothing was submitted"
        )
        self.assertEqual(runner.keys, ["Escape", "C-u"], "the box is cleared, no Enter")

    async def test_a_short_text_is_one_paste_as_before(self) -> None:
        runner = FakeBox()

        await runner._send_text("macbuilder-test", TAIL)

        self.assertEqual(runner.pasted, [TAIL])
        self.assertEqual(runner.keys, ["Enter"])

    async def test_the_check_looks_again_before_giving_up(self) -> None:
        runner = FakeBox(blind=1)

        await runner._send_text("macbuilder-test", TAIL)

        self.assertEqual(runner.keys, ["Enter"], "a slow repaint still submits")
