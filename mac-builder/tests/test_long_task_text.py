"""Long text: the raised limits, the attachment path, and Discord's own caps."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import bot  # noqa: E402
import manager  # noqa: E402
import questions  # noqa: E402
import rules  # noqa: E402
import screens  # noqa: E402

LIMIT = 6000


class LimitTests(unittest.TestCase):
    def test_the_typed_task_limit_is_discords_option_maximum(self) -> None:
        self.assertEqual(rules.MAX_TASK_CHARS, LIMIT)
        self.assertEqual(rules.sanitize_task_text("x" * LIMIT), "x" * LIMIT)
        with self.assertRaises(ValueError) as raised:
            rules.sanitize_task_text("x" * (LIMIT + 1))
        self.assertIn(f"the limit is {LIMIT}", str(raised.exception))

    def test_answers_and_screen_sends_take_the_same_text(self) -> None:
        self.assertEqual(questions.MAX_ANSWER_CHARS, LIMIT)
        self.assertEqual(len(questions.flatten_answer("y" * (LIMIT + 500))), LIMIT)
        self.assertEqual(screens.MAX_SEND_CHARS, LIMIT)
        self.assertEqual(len(screens.flatten_send("z" * (LIMIT + 500))), LIMIT)
        # The other rules are untouched.
        self.assertEqual(rules.MAX_NOTES_CHARS, 4000)

    def test_the_manager_sees_a_whole_long_task(self) -> None:
        self.assertEqual(manager.MAX_ANSWER_TASK_CHARS, LIMIT)
        task = "t" * LIMIT
        user = manager.build_answer_messages(task=task, text="Which?")[1]["content"]
        self.assertIn(task, user)
        self.assertLessEqual(len(user), manager.MAX_ANSWER_CONTEXT_CHARS)


class AttachmentTests(unittest.TestCase):
    def test_the_attachment_is_exempt_from_the_character_limit(self) -> None:
        for name in ("plan.md", "TASK.TXT", "notes.Md"):
            self.assertEqual(rules.attachment_task_text(name, b"do it"), "do it")
        body = "a" * (LIMIT * 2)
        self.assertEqual(rules.attachment_task_text("big.md", body.encode()), body)
        self.assertEqual(
            rules.attachment_task_text("a.txt", b"hello\x00\n\n\n\nworld"),
            "hello\n\nworld",
        )

    def test_other_file_types_and_bad_bytes_are_refused(self) -> None:
        with self.assertRaises(ValueError) as raised:
            rules.attachment_task_text("plan.pdf", b"%PDF-1.4")
        self.assertIn(".txt", str(raised.exception))
        self.assertIn(".md", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            rules.attachment_task_text("a.txt", b"\xff\xfe\x00binary")
        self.assertIn("UTF-8", str(raised.exception))

    def test_an_oversized_or_empty_attachment_is_refused(self) -> None:
        with self.assertRaises(ValueError) as raised:
            rules.attachment_task_text("a.md", b"a" * (rules.MAX_ATTACHMENT_BYTES + 1))
        self.assertIn(str(rules.MAX_ATTACHMENT_BYTES), str(raised.exception))

    def test_typed_text_comes_first_then_the_attachment(self) -> None:
        self.assertEqual(rules.combine_task_text("typed", "attached"), "typed\n\nattached")
        self.assertEqual(rules.combine_task_text("typed", ""), "typed")
        self.assertEqual(rules.combine_task_text("  ", "attached"), "attached")
        self.assertEqual(rules.combine_task_text("", ""), "")


class FakeAttachment:
    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self.data = data

    async def read(self) -> bytes:
        return self.data


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.messages.append(content)


class TaskTextTests(unittest.IsolatedAsyncioTestCase):
    async def task(self, typed: str, attachment: object = None):
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        interaction = type("Interaction", (), {"response": FakeResponse()})()
        value = await instance._task_text(interaction, typed, attachment)  # type: ignore[arg-type]
        return value, interaction.response.messages

    async def test_text_and_an_attachment_make_one_task(self) -> None:
        self.assertEqual(await self.task("fix it"), ("fix it", []))
        attachment = FakeAttachment("task.md", b"# Rewrite\n\nThe whole plan.")
        self.assertEqual(
            (await self.task("", attachment))[0], "# Rewrite\n\nThe whole plan."
        )
        self.assertEqual(
            (await self.task("start here", FakeAttachment("more.txt", b"and the rest")))[0],
            "start here\n\nand the rest",
        )

    async def test_a_refused_attachment_explains_itself_and_queues_nothing(self) -> None:
        value, messages = await self.task("", FakeAttachment("task.pdf", b"%PDF"))
        self.assertIsNone(value)
        self.assertIn(".txt", messages[0])
        value, messages = await self.task("", FakeAttachment("task.md", b"\xff\xfe"))
        self.assertIsNone(value)
        self.assertIn("UTF-8", messages[0])

    async def test_no_text_and_no_attachment_asks_for_one(self) -> None:
        value, messages = await self.task("   ")
        self.assertIsNone(value)
        self.assertIn("attachment", messages[0])
        value, messages = await self.task("x" * (LIMIT + 1))
        self.assertIsNone(value)
        self.assertIn(str(LIMIT), messages[0])


class DeclaredOptionTests(unittest.TestCase):
    """Declared with Discord's own maxima, built the way setup_hook builds them."""

    def parameters(self, callback: object) -> dict[str, object]:
        command = discord.app_commands.Command(
            name="probe", description="probe", callback=callback  # type: ignore[arg-type]
        )
        return {parameter.name: parameter for parameter in command.parameters}

    def max_length(self, callback: object, option: str) -> int | None:
        parameter = self.parameters(callback)[option]
        self.assertEqual(parameter.type, discord.AppCommandOptionType.string)
        return parameter.max_value

    def test_every_text_option_and_the_attachment_are_declared(self) -> None:
        for callback in (bot.MacBuilderBot.cmd_answer, bot.MacBuilderBot.cmd_send):
            self.assertEqual(self.max_length(callback, "text"), LIMIT)
        self.assertEqual(self.max_length(bot.MacBuilderBot.cmd_key, "name"), LIMIT)
        for callback in (bot.MacBuilderBot.cmd_task, bot.MacBuilderBot.cmd_dan):
            self.assertEqual(self.max_length(callback, "text"), LIMIT)
            attachment = self.parameters(callback)["attachment"]
            self.assertEqual(attachment.type, discord.AppCommandOptionType.attachment)
            self.assertFalse(attachment.required)

