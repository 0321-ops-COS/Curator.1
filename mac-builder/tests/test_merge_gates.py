import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from merge_gates import MergeFacts, all_passed, evaluate_gates, format_gates  # noqa: E402

MAX, SHA = 400, "a" * 40
NAMES = ["allowed", "check", "diff size", "protected paths", "pull request", "CI", "head unchanged"]  # noqa: E501


def facts(**overrides: object) -> MergeFacts:
    values: dict[str, object] = {
        "job_status": "approved", "branch": "dan/gates", "base": "main",
        "repo": "Curator.1", "authorized_branch": "", "check_state": "passed",
        "diff_lines": 12, "changed_paths": ("ios-app/RootView.swift",),
        "pr_exists": True, "pr_mergeable": True, "pr_conflicts": False,
        "ci_states": ("success",), "head_sha": SHA, "approved_sha": SHA,
        "dry_run_sha": None,
    }
    values.update(overrides)
    return MergeFacts(**values)  # type: ignore[arg-type]

def gates(**overrides: object) -> dict:
    return {g.name: g for g in evaluate_gates(facts(**overrides), max_diff_lines=MAX)}


class GateTests(unittest.TestCase):
    def test_all_seven_pass_and_empty_ci_passes(self) -> None:
        got = evaluate_gates(facts(), max_diff_lines=MAX)
        self.assertEqual([g.name for g in got], NAMES)
        self.assertTrue(all_passed(got), format_gates(got))
        quiet = gates(ci_states=())
        self.assertEqual(quiet["CI"].reason, "no CI checks")
        self.assertTrue(all_passed(list(quiet.values())))

    def test_each_failure_fails_only_its_own_gate(self) -> None:
        cases = {"allowed": dict(job_status="blocked"), "check": dict(check_state=""),
                 "diff size": dict(diff_lines=MAX + 1),
                 "protected paths": dict(changed_paths=(".github/ci.yml",)),
                 "pull request": dict(pr_mergeable=None), "CI": dict(ci_states=("failure",)),
                 "head unchanged": dict(head_sha="b" * 40)}
        for name, overrides in cases.items():
            got = gates(**overrides)
            self.assertEqual([n for n, g in got.items() if not g.passed], [name], name)
            self.assertIn(f"❌ {name}:", format_gates(list(got.values())))

    def test_branch_rules(self) -> None:
        hustler = dict(repo="Hustler", branch="freebuff/a")
        self.assertFalse(gates(**hustler, base="main", authorized_branch="freebuff/a")["allowed"].passed)
        merged = gates(**hustler, base="freebuff/b", authorized_branch="freebuff/b")
        self.assertTrue(merged["allowed"].passed)
        self.assertFalse(gates(**hustler, base="", authorized_branch="")["allowed"].passed)
        self.assertFalse(gates(**hustler, base="freebuff/b", authorized_branch="")["allowed"].passed)
        self.assertFalse(gates(branch="freebuff/a")["allowed"].passed)

    def test_protected_paths_are_refused(self) -> None:
        changed = ("ios-app/project.yml", ".github/ci.yml", "mac-builder/rules.py",
                   "mac-builder/freebuff_runner.py", "ios-app/Sign/A.entitlements",
                   "B.mobileprovision", "C.p12", ".env.local", "mac-builder/bot.py")
        gate = gates(changed_paths=changed)["protected paths"]
        self.assertFalse(gate.passed)
        self.assertIn("ios-app/project.yml", gate.reason)

    def test_missing_shas_ask_for_a_dry_run(self) -> None:
        got = evaluate_gates(facts(approved_sha=None, dry_run_sha=None), max_diff_lines=MAX)
        self.assertEqual(next(g.reason for g in got if g.name == "head unchanged"),
                         "run /merge with dry_run first")
        self.assertIn("approved_sha", gates(approved_sha="b" * 40)["head unchanged"].reason)

    def test_odd_input_never_raises(self) -> None:
        odd = gates(job_status="", branch="", base="", repo="", check_state="",
                    changed_paths=("",), ci_states=("",), head_sha="", approved_sha="",
                    dry_run_sha="")
        self.assertFalse(odd["allowed"].passed)

    def test_the_report_stays_under_1500_characters(self) -> None:
        changed = tuple(f"ios-app/F{index}.swift" for index in range(300))
        text = format_gates(evaluate_gates(facts(changed_paths=changed), max_diff_lines=MAX))
        self.assertLess(len(text), 1500)
