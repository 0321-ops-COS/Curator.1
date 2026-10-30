"""AI manager: reviews finished work and answers planning chat.

The manager is a pure chat client. It has no tools, cannot edit files and
cannot run commands. Its only outputs are a verdict and notes, or chat text.
Any provider that speaks the OpenAI-compatible ``/chat/completions`` shape works;
the base URL, key and model all come from the environment.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import httpx

import rules
from planner import PlanInputs

log = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 80_000
MAX_PLAN_CHARS = 60_000
MAX_CHAT_CHARS = 24_000
MAX_CHAT_MESSAGES = 12
MAX_PROPOSED_TASKS = 3

REVIEW_SYSTEM_PROMPT = """You are the repo manager for a private iOS build bot. Your project, repo and
builder are given under PROJECT below. You only READ and JUDGE.
You never edit code, never run commands and never push or merge anything.

The bot has already enforced these hard rules in code, before you see the work:
- diffs over MAX_DIFF_LINES are rejected without asking you,
- project.yml, *.xcconfig, .github/, .env*, the mac-builder folder and
  signing/provisioning files are protected and rejected without asking you,
- a failing check or test forces "revise" whatever you say,
- the bot can never merge or push to main.

Judge only the material given to you:
1. Does the diff actually do what the task asked, completely?
2. Are there bugs, crashes, or obvious logic errors?
3. Is there scope creep: unrelated edits, new dependencies, refactors?
4. Were tests weakened, deleted or skipped?
5. Is anything risky for a private, single-user app?

Verdicts:
- "approve": the change does the task, is in scope, and looks sound.
- "revise": concrete, fixable problems. Notes must be direct instructions the
  builder can act on. Do not ask for optional polish.
- "ask_user": a judgement call the owner must make, or the material is too
  incomplete or ambiguous to judge.

Answer with ONLY a JSON object, no markdown fence, no commentary:
{"verdict": "approve" | "revise" | "ask_user", "notes": "..."}
Keep notes under 1500 characters. Empty notes are only acceptable for approve.
"""

CHAT_SYSTEM_PROMPT = """You are the repo manager and planning assistant for a private iOS build
bot running on a Mac mini. Always know, and state when asked, which repo, branch and
project you work on, who the builder is, and that your own code (the mac-builder folder)
is a separate protected target.
You can answer questions about the repo, the queue and the latest job from the context
you are given. You cannot run commands, edit code, approve work or push anything.

Be concise and concrete. If you do not have the information, say so instead of guessing.
Never claim that something was run or verified unless the context says so.

If you want to propose concrete build tasks, end your reply with exactly one fenced
json block like:
```json
{"proposed_tasks": ["Short task one", "Short task two"]}
```
Propose at most three. The owner must press a confirmation button before any task runs.
"""


PLAN_SYSTEM_PROMPT = """You are the repo manager for a private iOS build bot. Your project, repo and
builder are given under PROJECT below. You write plans for the builder to execute.
You never edit code, run commands, push or merge anything, and a plan request never
builds anything.

Write ONE markdown document with exactly these sections, in this order:
# Task for Dan — <the goal in one line>
## Evidence
## Items (priority order)
## Acceptance per item
## Out of scope
## Open decisions for the owner

Rules:
- In Evidence, label every claim `verified` (the material below shows it) or
  `assumed` (you inferred it). Never state a guess as a fact and never say
  something was run when the material does not show it.
- Items are concrete, independent and most valuable first; one branch each. Name
  the file or module it touches and the test that proves it.
- Acceptance must be a check the owner can run, not a feeling: a command, an
  observable refusal, a file that exists.
- Use only paths that appear in the file map. If a path is not there, say the path
  must be confirmed instead of inventing one.
- Open decisions come last and are questions only the owner can answer.
- No code block longer than 10 lines. Keep the whole document under 200 lines.
"""


class ManagerUnavailable(RuntimeError):
    """The manager endpoint is not configured."""


@dataclass
class ReviewBrief:
    task: str
    diff: str
    diff_lines: int
    status: str
    changed_files: Sequence[str]
    check_state: str
    check_tail: str
    test_state: str
    test_tail: str
    round_number: int = 0
    project_context: str = ""


@dataclass
class ReviewResult:
    verdict: str
    notes: str
    raw: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class ChatReply:
    text: str
    proposed_tasks: list[str] = field(default_factory=list)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    return head + f"\n[truncated {len(text) - limit} characters]"


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "[earlier output omitted]\n" + text[-limit:]


def _status_text(response: httpx.Response) -> str:
    """'HTTP 503 Service Unavailable', without a trailing space when there is no reason."""
    reason = (response.reason_phrase or "").strip()
    return f"HTTP {response.status_code}" + (f" {reason}" if reason else "")


def describe_error(exc: BaseException | None) -> str:
    """Name a failure so a message is never a bare 'request failed:'.

    A timeout with an empty message, or a provider that rejects the key, must
    still say what happened; otherwise the owner sees nothing to act on.
    """
    if exc is None:
        return "unknown error"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__}: {_status_text(exc.response)}"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def build_review_messages(brief: ReviewBrief) -> list[dict[str, str]]:
    changed = ", ".join(brief.changed_files[:120]) or "(none)"
    if len(brief.changed_files) > 120:
        changed += f", ... (+{len(brief.changed_files) - 120} more)"
    diff_text = brief.diff.strip() or "(no diff text; see diff line count and status)"
    user = f"""TASK GIVEN TO THE BUILDER
{brief.task}

REVIEW ROUND
{brief.round_number} revision round(s) already sent

CHANGED FILES ({len(brief.changed_files)})
{changed}

DIFF SIZE
{brief.diff_lines} changed lines

GIT STATUS
{_truncate(brief.status.strip() or "(clean)", 4000)}

CHECK (/check, swift test in HustlerCore)
state: {brief.check_state}
{_tail(brief.check_tail.strip() or "(no output)", 6000)}

TEST (/test, xcodebuild test on one simulator)
state: {brief.test_state}
{_tail(brief.test_tail.strip() or "(no output)", 6000)}

DIFF
{diff_text}
"""
    system = REVIEW_SYSTEM_PROMPT
    if brief.project_context.strip():
        system += "\nPROJECT\n" + brief.project_context.strip() + "\n"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _truncate(user, MAX_CONTEXT_CHARS)},
    ]


def build_plan_messages(inputs: PlanInputs) -> list[dict[str, str]]:
    recent = "\n".join(f"- {line}" for line in inputs.recent) or "- (no job logs yet)"
    files = "\n".join(f"- {entry}" for entry in inputs.file_map) or "- (not listed)"
    gaps = "\n".join(f"- {line}" for line in inputs.known_gaps) or "- (none recorded)"
    user = f"""GOAL
{inputs.goal}

RECENT JOBS AND ERROR LINES (newest first, from LOG_DIR)
{recent}

FILE MAP (top level of the target checkout)
{files}

KNOWN GAPS (from the README)
{gaps}
"""
    system = PLAN_SYSTEM_PROMPT
    if inputs.project_block.strip():
        system += "\nPROJECT\n" + inputs.project_block.strip() + "\n"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _truncate(user, MAX_PLAN_CHARS)},
    ]


def parse_review(text: str) -> ReviewResult:
    """Parse the manager reply defensively; never trust its shape."""
    raw = text or ""
    payload = _extract_json_object(raw)
    if payload is None:
        return ReviewResult(
            verdict="ask_user",
            notes="The manager did not return a JSON verdict. Check the manager endpoint and model.",
            raw=raw[:4000],
            error="invalid_json",
        )
    verdict = rules.coerce_verdict(payload.get("verdict"))
    notes = rules.clamp_notes(payload.get("notes"))
    if verdict == "revise" and not notes:
        notes = "The manager asked for a revision but did not explain why. Review the diff manually."
    return ReviewResult(verdict=verdict, notes=notes, raw=raw[:4000])


def _extract_json_object(text: str) -> dict[str, object] | None:
    candidates = []
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            value = json.loads(candidate[start : end + 1])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_chat_reply(text: str) -> ChatReply:
    """Split chat prose from an optional proposed_tasks JSON block."""
    raw = text or ""
    proposals: list[str] = []
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            value = json.loads(fence.group(1).strip())
        except (ValueError, TypeError):
            value = None
        if isinstance(value, dict) and isinstance(value.get("proposed_tasks"), list):
            cleaned = raw[: fence.start()] + raw[fence.end() :]
            for item in value["proposed_tasks"]:
                if len(proposals) >= MAX_PROPOSED_TASKS:
                    break
                if not isinstance(item, str):
                    continue
                try:
                    proposals.append(rules.sanitize_task_text(item))
                except ValueError:
                    continue
            raw = cleaned
    return ChatReply(text=raw.strip(), proposed_tasks=proposals)


class Manager:
    def __init__(
        self,
        *,
        api_base: str = "",
        api_key: str = "",
        model: str = "",
        timeout_seconds: float = 120.0,
    ) -> None:
        self.api_base = (api_base or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_base and self.api_key and self.model)

    @property
    def endpoint(self) -> str:
        if self.api_base.endswith("/chat/completions"):
            return self.api_base
        return f"{self.api_base}/chat/completions"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _complete(self, messages: Iterable[dict[str, str]], *, max_tokens: int) -> str:
        if not self.enabled:
            raise ManagerUnavailable(
                "manager is not configured (MANAGER_API_BASE, MANAGER_API_KEY, MANAGER_MODEL)"
            )
        payload = {
            "model": self.model,
            "messages": list(messages),
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        last_error: BaseException | None = None
        for attempt in range(2):
            try:
                response = await self._http().post(self.endpoint, json=payload)
                if response.status_code >= 500 and attempt == 0:
                    last_error = ManagerUnavailable(_status_text(response))
                    continue
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ManagerUnavailable("manager returned no text content")
                return content
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    continue
        raise ManagerUnavailable(f"manager request failed: {describe_error(last_error)}")

    async def review(self, brief: ReviewBrief) -> ReviewResult:
        try:
            text = await self._complete(build_review_messages(brief), max_tokens=900)
        except ManagerUnavailable as exc:
            log.warning("manager unavailable: %s", exc)
            return ReviewResult(
                verdict="ask_user",
                notes=f"Manager unavailable: {exc}. Review the diff and test output manually.",
                error="unavailable",
            )
        return parse_review(text)

    async def plan(self, inputs: PlanInputs) -> str:
        """Write a plan document. Text only: no tools, no jobs, no edits."""
        text = await self._complete(build_plan_messages(inputs), max_tokens=1800)
        if not text.strip():
            raise ManagerUnavailable("manager returned an empty plan")
        return text.strip()

    async def chat(self, messages: Sequence[dict[str, str]], context: str) -> ChatReply:
        trimmed: list[dict[str, str]] = []
        for message in list(messages)[-MAX_CHAT_MESSAGES:]:
            role = message.get("role")
            content = message.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            trimmed.append({"role": role, "content": _truncate(content, 4000)})
        system = {
            "role": "system",
            "content": CHAT_SYSTEM_PROMPT
            + "\n\nCURRENT BOT CONTEXT\n"
            + _truncate(context.strip() or "(no jobs yet)", MAX_CHAT_CHARS),
        }
        try:
            text = await self._complete([system, *trimmed], max_tokens=900)
        except ManagerUnavailable as exc:
            return ChatReply(text=f"The AI manager is not available: {exc}")
        reply = parse_chat_reply(text)
        if not reply.text:
            reply.text = "(the manager returned an empty reply)"
        return reply
