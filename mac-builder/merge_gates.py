"""Merge gates: pure logic for deciding whether a finished job may merge."""

from __future__ import annotations

from dataclasses import dataclass

import rules



@dataclass(frozen=True)
class MergeFacts:
    job_status: str
    branch: str
    base: str
    repo: str
    authorized_branch: str
    check_state: str
    diff_lines: int
    changed_paths: tuple[str, ...]
    pr_exists: bool
    pr_mergeable: bool | None
    pr_conflicts: bool
    ci_states: tuple[str, ...]
    head_sha: str
    approved_sha: str | None
    dry_run_sha: str | None


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    reason: str

def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""

def _allowed(facts: MergeFacts) -> tuple[bool, str]:
    repo, branch = _text(facts.repo), _text(facts.branch)
    base, authorized = _text(facts.base), _text(facts.authorized_branch)
    status = _text(facts.job_status)
    if status != "approved":
        return False, f"job {status or '(empty)'} is not approved"
    if repo == "Hustler" and base == "main":
        return False, "main is never merged; Hustler merges into its own branch"
    if repo == "Curator.1" and branch.startswith("dan/") and base == "main":
        return True, f"{branch} may merge into main"
    if repo == "Hustler" and (not base or not authorized):
        return False, "Hustler needs a base and an authorized branch; one is empty"
    if repo == "Hustler" and base == authorized and branch.startswith("freebuff/"):
        return True, f"{branch} may merge into {authorized}"
    if repo not in ("Curator.1", "Hustler"):
        return False, f"unknown repo {repo or '(empty)'}"
    return False, f"refused {branch or '(no branch)'} into {base or '(no base)'}"

def _check(facts: MergeFacts) -> tuple[bool, str]:
    state = _text(facts.check_state)
    ok = state.lower() == "passed"  # what jobs._run_stage records for exit 0
    return ok, "check passed" if ok else f"check {state or '(empty)'} is not passed"

def _diff_size(facts: MergeFacts, max_diff_lines: int) -> tuple[bool, str]:
    ok = facts.diff_lines <= max_diff_lines
    return ok, f"{facts.diff_lines} changed lines, limit {max_diff_lines}"

def _forbidden(path: str) -> bool:
    value = rules.normalize_path(path)
    name = value.rsplit("/", 1)[-1]
    return bool(value) and (
        rules.is_protected_path(value)
        or name in ("rules.py", "freebuff_runner.py")
        or value.endswith("ios-app/project.yml")
        or name.endswith((".entitlements", ".mobileprovision", ".p12"))
    )

def _protected(facts: MergeFacts) -> tuple[bool, str]:
    paths = [str(path) for path in facts.changed_paths or ()]
    bad = [path for path in paths if _forbidden(path)]
    if bad:
        return False, "protected: " + ", ".join(bad[:3])
    return True, f"{len(paths)} paths, none protected"

def _pull_request(facts: MergeFacts) -> tuple[bool, str]:
    if not facts.pr_exists:
        return False, "no pull request for this branch"
    if facts.pr_mergeable is not True or facts.pr_conflicts:
        return False, f"mergeable={facts.pr_mergeable!r}, conflicts={facts.pr_conflicts}"
    return True, "PR open, mergeable and conflict-free"

def _ci(facts: MergeFacts) -> tuple[bool, str]:
    states = tuple(_text(state).lower() for state in facts.ci_states or ())
    if not states:
        return True, "no CI checks"
    bad = [state or "(empty)" for state in states if state != "success"]
    if bad:
        return False, "not success: " + ", ".join(bad[:4])
    return True, f"{len(states)} CI checks succeeded"

def _head_unchanged(facts: MergeFacts) -> tuple[bool, str]:
    head = _text(facts.head_sha)
    if facts.approved_sha is not None:
        source, expected = "approved_sha", _text(facts.approved_sha)
    elif facts.dry_run_sha is not None:
        source, expected = "dry_run_sha", _text(facts.dry_run_sha)
    else:
        return False, "run /merge with dry_run first"
    if not expected:
        return False, f"{source} is empty; run /merge with dry_run first"
    ok = bool(head) and head == expected
    return ok, f"head {head[:10] or '(none)'} vs {source} {expected[:10]}"

def evaluate_gates(facts: MergeFacts, *, max_diff_lines: int) -> list[GateResult]:
    def safe(name: str, gate, *args) -> GateResult:
        try:
            passed, reason = gate(facts, *args)
        except Exception as exc:  # a gate must never take the caller down
            return GateResult(name, False, f"could not be evaluated: {exc}")
        return GateResult(name, bool(passed), reason)

    return [
        safe("allowed", _allowed),
        safe("check", _check),
        safe("diff size", _diff_size, max_diff_lines),
        safe("protected paths", _protected),
        safe("pull request", _pull_request),
        safe("CI", _ci),
        safe("head unchanged", _head_unchanged),
    ]

def all_passed(results: list[GateResult]) -> bool:
    return bool(results) and all(result.passed for result in results)

def format_gates(results: list[GateResult]) -> str:
    text = "\n".join(
        f"{'✅' if result.passed else '❌'} {result.name}: {result.reason}"
        for result in results
    )
    if len(text) <= 1500:
        return text
    return text[:1499] + "…"
