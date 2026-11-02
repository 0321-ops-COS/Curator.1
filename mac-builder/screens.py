"""Which screen is Freebuff showing? A pure classifier over pane text.

Freebuff is closed source, so its screens cannot be listed in advance. This
module matches a small set of conservative patterns against a captured pane and
otherwise falls back to "working", because normal coding output is much more
common than any single prompt. The one thing it never does is guess a question
it cannot read: question boxes are left to ``questions.py``.

Every kind here is handled somewhere:

* ``question_box`` and ``survey`` — ``questions.py`` and the box driver.
* ``sign_in``, ``rate_limit``, ``update_prompt``, ``confirm_prompt``,
  ``error_or_crash`` — a person has to act. The runner does not paste a task
  into one, and the stall detector posts the pane with ``/screen``, ``/key``
  and ``/send`` until it changes or expires.
* ``ready_prompt`` and ``working`` — a normal screen.
* ``unknown`` — an empty pane, or a static screen nothing matched. It uses the
  stall path too, so an unrecognized screen is never silent.

Nothing here runs a command, touches tmux, or talks to Discord: it is a pure
function on text, so every pattern can be pinned by a fixture test.

Redaction lives here as well, so there is one function in front of every sink
(Discord, the job log, ``QUESTION_DEBUG``): ``redact_pane`` masks long
token-like strings, ``Bearer …`` values and API-key shapes, and deliberately
leaves short human codes alone — the owner has to be able to read those to sign
in.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import questions

# --------------------------------------------------------------- the kinds

QUESTION_BOX = "question_box"
SURVEY = "survey"
SIGN_IN = "sign_in"
RATE_LIMIT = "rate_limit"
UPDATE_PROMPT = "update_prompt"
CONFIRM_PROMPT = "confirm_prompt"
ERROR_OR_CRASH = "error_or_crash"
READY_PROMPT = "ready_prompt"
WORKING = "working"
UNKNOWN = "unknown"

KINDS = (
    QUESTION_BOX,
    SURVEY,
    SIGN_IN,
    RATE_LIMIT,
    UPDATE_PROMPT,
    CONFIRM_PROMPT,
    ERROR_OR_CRASH,
    READY_PROMPT,
    WORKING,
    UNKNOWN,
)

# Screens that want a person before anything else may be typed. The task text is
# never pasted into one, and the stall detector posts it instead of waiting for
# the hard timeout. ``confirm_prompt`` is included on purpose: a y/n prompt is
# not a place for a 4000-character task paste either.
WAITING_KINDS = frozenset(
    {
        QUESTION_BOX,
        SIGN_IN,
        RATE_LIMIT,
        UPDATE_PROMPT,
        CONFIRM_PROMPT,
        ERROR_OR_CRASH,
    }
)

TITLES = {
    QUESTION_BOX: "a question box",
    SURVEY: "Freebuff's own survey popup",
    SIGN_IN: "a sign-in screen",
    RATE_LIMIT: "a rate-limit screen",
    UPDATE_PROMPT: "an update prompt",
    CONFIRM_PROMPT: "a confirmation prompt",
    ERROR_OR_CRASH: "an error or crash screen",
    READY_PROMPT: "the ready prompt",
    WORKING: "a working screen",
    UNKNOWN: "an unrecognized screen",
}

# How much pane text one Discord post carries, and the longest /send text.
MAX_PANE_CHARS = 1800
MAX_SEND_CHARS = 500

# ------------------------------------------------------------- the patterns
#
# Conservative by design: a pattern that fires on ordinary coding output would
# turn every job into a false alarm, so each one needs a phrase a coding tool
# would not normally print. Patterns are matched against the cleaned pane lines.

SIGN_IN_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bpress\s+enter\s+to\s+open\s+(the\s+)?browser\b",
        r"\b(device|user)[- ]?code\b",
        r"\benter\s+(this|the)\s+(one[- ]time\s+)?code\b",
        r"\bverification\s+code\b",
        r"\b(sign|log)\s?in\s+(to|with|via)\b",
        r"^\s*(sign|log)\s?in\b",
        r"\b(authorization|authorisation)\s+(code|url|complete|required)\b",
        r"\balready\s+have\s+an\s+account\b",
        r"https?://\S*(?:login|signin|sign-in|device|oauth|authorize)",
    )
)

RATE_LIMIT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(rate|usage)\s+limit\b",
        r"\bquota\b",
        r"\bwaiting\s+room\b",
        r"\b(?:you've|you\s+have)\s+reached\s+(?:your|the)\s+limit\b",
        r"\blimited\s+mode\b",
        r"\btoo\s+many\s+requests\b",
        r"\btry\s+again\s+(?:in|at|tomorrow)\b",
        r"\brequests?\s+per\s+(?:minute|hour|day)\b",
        r"\bHTTP\s*429\b",
    )
)

UPDATE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bupdate\s+(?:is\s+)?available\b",
        r"\bnew\s+version\s+(?:of\s+freebuff\s+)?is\s+available\b",
        r"\bupdate\s+required\b",
        r"\bplease\s+update\s+(?:to|freebuff)\b",
        r"\bbrew\s+upgrade\b",
        r"\bnpm\s+install\s+-g\b.*\bfreebuff\b",
    )
)

CONFIRM_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bpress\s+enter\s+to\s+continue\b",
        r"\bcontinue\?\s*\[?[yn]",
        r"\[[yYnN]/\s?[yYnN]\]",
        r"\(y/n\)",
        r"\bare\s+you\s+sure\b",
        r"\bdo\s+you\s+(want\s+to|trust)\b",
        r"\bpermission\s+(to|request)\b",
        r"\ballow\s+(this|access|the\s+request)\b",
        r"\bproceed\?\b",
        r"\boverwrite\b.*\?",
    )
)

# Only a crash, never a diff that happens to mention an error string.
ERROR_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"Traceback \(most recent call last\)",
        r"\bSegmentation fault\b",
        r"\bcore dumped\b",
        r"^\s*(?:FATAL|PANIC|panic:|fatal:)\s*\S",
        r"\bUnhandled\s+(?:exception|error|promise\s+rejection)\b",
        r"\bsomething\s+went\s+wrong\b",
        r"\bcommand\s+not\s+found\b",
        r"\bModuleNotFoundError\b",
        r"\bCannot\s+find\s+module\b",
        r"\bFreebuff\s+(?:has\s+)?crashed\b",
        r"^\s*Error:\s*\S",
    )
)

READY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\btype\s+(?:a\s+)?(?:message|prompt|task)\b",
        r"\bask\s+(?:anything|a\s+question)\b",
        r"\bhow\s+can\s+i\s+help\b",
        r"\bwhat\s+would\s+you\s+like\s+(?:me\s+)?to\s+(?:build|do)\b",
        r"\bready\s+for\s+(?:your\s+)?(?:task|prompt|input)\b",
    )
)

WORKING_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bthinking\b",
        r"\bworking\b",
        r"\besc\s+to\s+(?:interrupt|stop|cancel)\b",
        r"\bctrl\+c\s+to\s+(?:stop|cancel|interrupt)\b",
        r"\bgenerating\b",
        r"\brunning\b.*\bcommand\b",
    )
)

# A bare prompt as the last visible line: ">", "❯", "$", "│ >".
BARE_PROMPT_RE = re.compile(r"^[│|]?\s*[>❯$]\s*$|^[│|]?\s*[>❯]\s+\S")
SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒"


@dataclass(frozen=True)
class Screen:
    """One classified pane: its kind, its identity, and the text it came from."""

    kind: str
    raw: str = ""

    @property
    def title(self) -> str:
        return TITLES.get(self.kind, TITLES[UNKNOWN])

    @property
    def key(self) -> str:
        """Stable identity of this screen's visible text.

        Used to post a stuck screen once instead of once per poll. The text is
        normalized first, so a repaint that only moves padding is the same
        screen.
        """
        body = "\n".join(line.rstrip() for line in _rows(self.raw))
        digest = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]
        return f"{self.kind}:{digest}"

    @property
    def needs_owner(self) -> bool:
        return self.kind in WAITING_KINDS

    @property
    def empty(self) -> bool:
        return not self.raw.strip()


def describe(kind: str) -> str:
    """A short phrase for a Discord message: "a sign-in screen"."""
    return TITLES.get(kind, TITLES[UNKNOWN])


def waits_for_owner(kind: str) -> bool:
    return kind in WAITING_KINDS


# --------------------------------------------------------------- the parser


def _clean_line(raw: str) -> str:
    """One pane line without escape codes, box borders or padding."""
    text = questions.strip_ansi(raw).replace("\r", "").strip()
    while text and (text[0] in questions.BOX_BORDERS or text[0] in questions.RULE_CHARS):
        text = text[1:].lstrip()
    while text and (text[-1] in questions.BOX_BORDERS or text[-1] in questions.RULE_CHARS):
        text = text[:-1].rstrip()
    return text


def _rows(pane: str) -> list[str]:
    return [_clean_line(line) for line in (pane or "").splitlines()]


def _visible(rows: list[str]) -> list[str]:
    return [row for row in rows if row and not questions.RULE_RE.match(row)]


def _any(patterns: tuple[re.Pattern[str], ...], lines: list[str]) -> bool:
    return any(pattern.search(line) for pattern in patterns for line in lines)


def classify(pane: str) -> Screen:
    """Classify one captured pane. Never raises, never guesses a question."""
    raw = pane or ""
    rows = _rows(raw)
    visible = _visible(rows)
    if not visible:
        return Screen(UNKNOWN, raw)

    box = questions.parse_pane(raw)
    if box is not None:
        return Screen(SURVEY if box.survey else QUESTION_BOX, raw)

    text = "\n".join(visible)
    if _any(SIGN_IN_PATTERNS, visible):
        return Screen(SIGN_IN, raw)
    if _any(RATE_LIMIT_PATTERNS, visible):
        return Screen(RATE_LIMIT, raw)
    if _any(UPDATE_PATTERNS, visible):
        return Screen(UPDATE_PROMPT, raw)
    if _any(ERROR_PATTERNS, visible):
        return Screen(ERROR_OR_CRASH, raw)
    if _any(CONFIRM_PATTERNS, visible):
        return Screen(CONFIRM_PROMPT, raw)
    if _any(READY_PATTERNS, visible):
        return Screen(READY_PROMPT, raw)
    last = visible[-1]
    if BARE_PROMPT_RE.match(last) or any(char in text for char in SPINNER_CHARS):
        # A spinner or a bare prompt is a live screen, not a stuck one.
        return Screen(READY_PROMPT if BARE_PROMPT_RE.match(last) else WORKING, raw)
    if _any(WORKING_PATTERNS, visible):
        return Screen(WORKING, raw)
    # Ordinary coding output is what most panes look like. It is not a prompt,
    # so it is reported as working rather than as something the owner must read.
    return Screen(WORKING, raw)


def classify_kind(pane: str) -> str:
    """The kind alone, for callers that only need the label."""
    return classify(pane).kind


# ------------------------------------------------------------- the redaction

# A long run of token characters that mixes letters and digits: an API key, a
# session token, a JWT fragment. 32+ characters, and a path separator anywhere
# disqualifies it, so file paths and stack traces stay readable. Short human
# codes ("ABCD-1234", "K7QP2") are far below the limit and are kept: the owner
# needs them to sign in.
_LONG_TOKEN_RE = re.compile(
    r"(?<![\w\-])"
    r"(?=[A-Za-z0-9_\-]*[0-9])"
    r"(?=[A-Za-z0-9_\-]*[A-Za-z])"
    r"[A-Za-z0-9_\-]{32,}"
    r"(?![\w\-])"
)


def redact_pane(text: str) -> str:
    """Everything read from a pane passes through here before it leaves.

    ``questions.redact`` already covers bearer tokens and the API-key shapes
    ``project_context`` knows; this adds the long token-like run, which is what
    a login screen prints when it echoes a session key.
    """
    return _LONG_TOKEN_RE.sub("[redacted]", questions.redact(text or ""))


def sanitize(pane: str, limit: int = MAX_PANE_CHARS) -> str:
    """Redacted, control-free pane text, newest end first when it is cut."""
    text = redact_pane(questions.strip_ansi(pane or ""))
    rows = [row.rstrip() for row in text.splitlines() if not questions.AD_LINE_RE.search(row)]
    text = "\n".join(rows).strip()
    text = "".join(char for char in text if char == "\n" or char >= " ")
    if len(text) > limit:
        text = "[...]\n" + text[-limit:]
    return text


# ----------------------------------------------------------------- the keys

# The only keys /key may send. A name, never a string, so nothing arbitrary can
# reach tmux: "rm -rf ~" is not a key.
KEY_NAMES = ("Enter", "Esc", "Up", "Down", "Left", "Right", "Tab", "y", "n")
KEY_SEQUENCES = {
    "enter": "Enter",
    "return": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "tab": "Tab",
    "y": "y",
    "yes": "y",
    "n": "n",
    "no": "n",
}


def key_sequence(name: str) -> str | None:
    """The tmux key for an allowlisted name, or None for anything else."""
    if not isinstance(name, str):
        return None
    return KEY_SEQUENCES.get(name.strip().lower())


def flatten_send(text: str, limit: int = MAX_SEND_CHARS) -> str:
    """One line, bounded: a newline would submit the prompt early."""
    value = questions.strip_ansi(text or "").replace("\r", " ").replace("\n", " ")
    return " ".join(value.split())[:limit]


# ------------------------------------------------------------- the farewell


def exit_summary(screen: Screen) -> str:
    """Why the session probably exited, in words the owner can act on."""
    if screen.kind == SIGN_IN:
        return "the session exited on a sign-in screen (signed out?)"
    if screen.kind == RATE_LIMIT:
        return "the session exited on a rate-limit screen"
    if screen.kind == ERROR_OR_CRASH:
        return "the session exited on an error or crash screen"
    if screen.kind == UPDATE_PROMPT:
        return "the session exited on an update prompt"
    if screen.kind == QUESTION_BOX:
        return "the session exited while a question box was open"
    if screen.kind == CONFIRM_PROMPT:
        return "the session exited on a confirmation prompt"
    if screen.empty:
        return "the session exited and its last screen was empty"
    return "the session exited normally"
