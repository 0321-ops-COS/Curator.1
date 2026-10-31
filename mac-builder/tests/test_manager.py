from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Callable

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manager import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    MAX_CONTEXT_CHARS,
    MAX_LOG_TAIL_CHARS,
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

    def test_long_log_tails_are_cut_before_the_diff(self) -> None:
        runaway = "\n".join(f"log line {i}" for i in range(20000))
        messages = build_review_messages(
            brief(
                check_tail=runaway,
                test_tail=runaway,
                diff="diff --git a/a b/a\n+let last = 1\n",
            )
        )
        user = messages[1]["content"]
        # The diff survives whole and the tails are the part that shrinks.
        self.assertTrue(user.rstrip().endswith("+let last = 1"))
        self.assertNotIn("log line 0", user)
        self.assertLess(len(user), MAX_CONTEXT_CHARS // 2)
        check_section = user.split("CHECK", 1)[1].split("TEST", 1)[0]
        self.assertLessEqual(len(check_section), MAX_LOG_TAIL_CHARS + 300)


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


class ManagerRetryTests(unittest.IsolatedAsyncioTestCase):
    """Retry only what the same request can survive: 5xx, 429 and timeouts."""

    def manager(
        self, handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
    ) -> tuple[Manager, list[int]]:
        calls: list[int] = []

        def counting(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return handler(request)

        instance = Manager(
            api_base="https://example.test/v1",
            api_key="k",
            model="m",
            retry_delay_seconds=0.0,
            **kwargs,  # type: ignore[arg-type]
        )
        instance._client = httpx.AsyncClient(transport=httpx.MockTransport(counting))
        return instance, calls

    async def review_with(
        self, handler: Callable[[httpx.Request], httpx.Response]
    ) -> tuple[object, list[int]]:
        instance, calls = self.manager(handler)
        try:
            return await instance.review(brief()), calls
        finally:
            await instance.close()

    async def test_a_5xx_is_retried_once_then_given_up(self) -> None:
        result, calls = await self.review_with(
            lambda request: httpx.Response(503, request=request)
        )
        self.assertEqual(len(calls), 2)
        self.assertIn("HTTP 503", result.notes)  # type: ignore[attr-defined]

    async def test_a_5xx_can_succeed_on_the_second_attempt(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if len(calls_seen) == 0:
                calls_seen.append(1)
                return httpx.Response(500, request=request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"verdict": "approve", "notes": "ok"}'}}]},
                request=request,
            )

        calls_seen: list[int] = []
        result, _ = await self.review_with(handler)
        self.assertEqual(result.verdict, "approve")  # type: ignore[attr-defined]

    async def test_a_429_is_retried(self) -> None:
        result, calls = await self.review_with(
            lambda request: httpx.Response(429, request=request)
        )
        self.assertEqual(len(calls), 2)
        self.assertIn("HTTP 429", result.notes)  # type: ignore[attr-defined]

    async def test_a_timeout_is_retried(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("", request=request)

        result, calls = await self.review_with(handler)
        self.assertEqual(len(calls), 2)
        self.assertIn("ReadTimeout", result.notes)  # type: ignore[attr-defined]

    async def test_a_401_is_not_retried(self) -> None:
        result, calls = await self.review_with(
            lambda request: httpx.Response(401, request=request)
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("HTTP 401", result.notes)  # type: ignore[attr-defined]

    async def test_a_malformed_payload_is_not_retried(self) -> None:
        result, calls = await self.review_with(
            lambda request: httpx.Response(200, json={"choices": []}, request=request)
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("IndexError", result.notes)  # type: ignore[attr-defined]


class ManagerTokenBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def payload_for(self, **kwargs: object) -> dict[str, object]:
        seen: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "hello"}}]},
                request=request,
            )

        instance = Manager(
            api_base="https://example.test/v1",
            api_key="k",
            model="m",
            retry_delay_seconds=0.0,
            **kwargs,  # type: ignore[arg-type]
        )
        instance._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await instance.chat([{"role": "user", "content": "hi"}], "")
        finally:
            await instance.close()
        return seen[0]

    async def test_the_default_budget_leaves_room_for_reasoning(self) -> None:
        self.assertGreaterEqual(DEFAULT_MAX_TOKENS, 4000)
        payload = await self.payload_for()
        self.assertEqual(payload["max_tokens"], DEFAULT_MAX_TOKENS)

    async def test_an_explicit_budget_reaches_the_payload(self) -> None:
        payload = await self.payload_for(max_tokens=6000)
        self.assertEqual(payload["max_tokens"], 6000)


class ManagerFallbackModelTests(unittest.IsolatedAsyncioTestCase):
    """Fallbacks are an extra "models" list; without them the payload is unchanged."""

    async def payload_for(self, **kwargs: object) -> dict[str, object]:
        seen: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "hello"}}]},
                request=request,
            )

        instance = Manager(
            api_base="https://example.test/v1",
            api_key="k",
            model="primary",
            retry_delay_seconds=0.0,
            **kwargs,  # type: ignore[arg-type]
        )
        instance._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await instance.chat([{"role": "user", "content": "hi"}], "")
        finally:
            await instance.close()
        return seen[0]

    async def test_no_fallbacks_keeps_the_payload_exactly_as_before(self) -> None:
        payload = await self.payload_for()
        self.assertNotIn("models", payload)
        self.assertEqual(
            set(payload), {"model", "messages", "temperature", "max_tokens"}
        )
        self.assertEqual(payload["model"], "primary")

    async def test_fallbacks_follow_the_primary_in_order(self) -> None:
        payload = await self.payload_for(fallback_models=("backup-a", "backup-b"))
        self.assertEqual(payload["model"], "primary")
        self.assertEqual(payload["models"], ["primary", "backup-a", "backup-b"])

    async def test_entries_are_trimmed_and_blanks_dropped(self) -> None:
        payload = await self.payload_for(
            fallback_models=(" backup-a ", "", "   ", "backup-b")
        )
        self.assertEqual(payload["models"], ["primary", "backup-a", "backup-b"])

    async def test_only_blank_entries_leave_no_list(self) -> None:
        payload = await self.payload_for(fallback_models=("", "  "))
        self.assertNotIn("models", payload)


if __name__ == "__main__":
    unittest.main()
