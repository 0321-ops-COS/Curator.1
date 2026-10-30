from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Callable

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manager import (  # noqa: E402
    Manager,
    ReviewBrief,
    build_review_messages,
    describe_error,
    parse_chat_reply,
    parse_review,
)


def brief(**overrides: object) -> ReviewBrief:
    values = dict(
        task="Make the scoreboard show net pay per hour.",
        diff="diff --git a/a.swift b/a.swift\n+let x = 1\n",
        diff_lines=1,
        status=" M a.swift",
        changed_files=["a.swift"],
        check_state="passed",
        check_tail="Executed 131 tests",
        test_state="unavailable (scheme has no test action)",
        test_tail="scheme has no test action",
        round_number=0,
    )
    values.update(overrides)
    return ReviewBrief(**values)  # type: ignore[arg-type]


class ParseReviewTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        result = parse_review('{"verdict": "approve", "notes": "Looks good."}')
        self.assertEqual(result.verdict, "approve")
        self.assertEqual(result.notes, "Looks good.")
        self.assertTrue(result.ok)

    def test_fenced_json(self) -> None:
        result = parse_review(
            'Here is my review:\n```json\n{"verdict": "revise", "notes": "Fix the typo."}\n```'
        )
        self.assertEqual(result.verdict, "revise")
        self.assertEqual(result.notes, "Fix the typo.")

    def test_uppercase_verdict_is_normalised(self) -> None:
        result = parse_review('{"verdict": "APPROVE", "notes": ""}')
        self.assertEqual(result.verdict, "approve")

    def test_unknown_verdict_becomes_ask_user(self) -> None:
        result = parse_review('{"verdict": "merge", "notes": "ship it"}')
        self.assertEqual(result.verdict, "ask_user")

    def test_invalid_json_asks_the_user(self) -> None:
        result = parse_review("I think this is fine, just merge it.")
        self.assertEqual(result.verdict, "ask_user")
        self.assertEqual(result.error, "invalid_json")
        self.assertIn("JSON verdict", result.notes)

    def test_revise_without_notes_gets_a_fallback(self) -> None:
        result = parse_review('{"verdict": "revise", "notes": ""}')
        self.assertEqual(result.verdict, "revise")
        self.assertTrue(result.notes)

    def test_non_string_notes_are_ignored(self) -> None:
        result = parse_review('{"verdict": "approve", "notes": 42}')
        self.assertEqual(result.notes, "")


class ChatReplyTests(unittest.TestCase):
    def test_proposal_is_split_out(self) -> None:
        reply = parse_chat_reply(
            "Here is a plan.\n```json\n"
            '{"proposed_tasks": ["Add a scoreboard test", "Fix the float label"]}\n'
            "```\n"
        )
        self.assertEqual(reply.text, "Here is a plan.")
        self.assertEqual(
            reply.proposed_tasks, ["Add a scoreboard test", "Fix the float label"]
        )

    def test_proposals_are_capped_and_validated(self) -> None:
        reply = parse_chat_reply(
            "```json\n"
            '{"proposed_tasks": ["one", "", 7, "two", "three", "four"]}\n'
            "```"
        )
        self.assertEqual(reply.proposed_tasks, ["one", "two", "three"])

    def test_plain_text_stays_plain(self) -> None:
        reply = parse_chat_reply("The queue is empty.")
        self.assertEqual(reply.text, "The queue is empty.")
        self.assertEqual(reply.proposed_tasks, [])


class PromptTests(unittest.TestCase):
    def test_review_prompt_carries_task_diff_and_failures(self) -> None:
        messages = build_review_messages(
            brief(check_state="failed (exit 65)", check_tail="error: boom")
        )
        self.assertEqual(messages[0]["role"], "system")
        user = messages[1]["content"]
        self.assertIn("Make the scoreboard show net pay per hour.", user)
        self.assertIn("+let x = 1", user)
        self.assertIn("failed (exit 65)", user)
        self.assertIn("error: boom", user)
        self.assertIn("no test action", user)

    def test_review_prompt_says_manager_cannot_act(self) -> None:
        system = build_review_messages(brief())[0]["content"]
        self.assertIn("never edit code", system)
        self.assertIn("approve", system)
        self.assertIn("revise", system)
        self.assertIn("ask_user", system)


class ManagerConfigTests(unittest.TestCase):
    def test_disabled_without_all_settings(self) -> None:
        self.assertFalse(Manager().enabled)
        self.assertFalse(Manager(api_base="https://x/v1", model="m").enabled)
        self.assertFalse(Manager(api_base="https://x/v1", api_key="k").enabled)

    def test_endpoint_is_appended_once(self) -> None:
        manager = Manager(
            api_base="https://example.test/v1/", api_key="k", model="m"
        )
        self.assertTrue(manager.enabled)
        self.assertEqual(manager.endpoint, "https://example.test/v1/chat/completions")
        manager = Manager(
            api_base="https://example.test/v1/chat/completions",
            api_key="k",
            model="m",
        )
        self.assertEqual(manager.endpoint, "https://example.test/v1/chat/completions")


class ManagerFailureTests(unittest.IsolatedAsyncioTestCase):
    """A failed request must name what failed, even when the message is empty."""

    def manager(self, handler: Callable[[httpx.Request], httpx.Response]) -> Manager:
        instance = Manager(api_base="https://example.test/v1", api_key="k", model="m")
        instance._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return instance

    async def notes_for(self, handler: Callable[[httpx.Request], httpx.Response]) -> str:
        instance = self.manager(handler)
        try:
            return (await instance.review(brief())).notes
        finally:
            await instance.close()

    async def test_the_http_status_and_the_class_are_reported(self) -> None:
        notes = await self.notes_for(lambda request: httpx.Response(401, request=request))
        self.assertIn("HTTP 401", notes)
        self.assertIn("HTTPStatusError", notes)

    async def test_an_empty_exception_message_still_names_the_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("", request=request)

        notes = await self.notes_for(handler)
        self.assertIn("ReadTimeout", notes)
        self.assertNotIn("failed: .", notes)

    async def test_a_malformed_payload_names_the_error(self) -> None:
        notes = await self.notes_for(
            lambda request: httpx.Response(200, json={"choices": []}, request=request)
        )
        self.assertIn("IndexError", notes)

    def test_describe_error_covers_status_and_blank_messages(self) -> None:
        self.assertEqual(describe_error(None), "unknown error")
        self.assertEqual(describe_error(ValueError("")), "ValueError")
        request = httpx.Request("POST", "https://example.test/v1/chat/completions")
        response = httpx.Response(503, request=request)
        error = httpx.HTTPStatusError("boom", request=request, response=response)
        self.assertIn("HTTP 503", describe_error(error))


if __name__ == "__main__":
    unittest.main()
