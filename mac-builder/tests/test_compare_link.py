"""The compare link is built from text and never carries a credential."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import compare_link  # noqa: E402
import jobs  # noqa: E402

BRANCH = "freebuff/9a1c2b3d"
BASE = "freebuff/ce4b8907c250c30103d8993e"
EXPECTED = (
    "https://github.com/0321-ops-COS/Hustler/compare/"
    f"{BASE}...{BRANCH}?expand=1"
)


class CompareUrlTests(unittest.TestCase):
    def test_https_remote(self) -> None:
        self.assertEqual(
            compare_link.compare_url(
                "https://github.com/0321-ops-COS/Hustler.git", BASE, BRANCH
            ),
            EXPECTED,
        )

    def test_https_remote_without_dot_git(self) -> None:
        self.assertEqual(
            compare_link.compare_url(
                "https://github.com/0321-ops-COS/Hustler", BASE, BRANCH
            ),
            EXPECTED,
        )

    def test_scp_remote(self) -> None:
        self.assertEqual(
            compare_link.compare_url(
                "git@github.com:0321-ops-COS/Hustler.git", BASE, BRANCH
            ),
            EXPECTED,
        )

    def test_ssh_remote(self) -> None:
        self.assertEqual(
            compare_link.compare_url(
                "ssh://git@github.com/0321-ops-COS/Hustler.git", BASE, BRANCH
            ),
            EXPECTED,
        )

    def test_https_credentials_are_never_in_the_output(self) -> None:
        link = compare_link.compare_url(
            "https://user:ghp_secret@github.com/0321-ops-COS/Hustler.git", BASE, BRANCH
        )
        self.assertEqual(link, EXPECTED)
        self.assertNotIn("ghp_secret", link or "")
        self.assertNotIn("user", link or "")

    def test_ssh_credentials_are_never_in_the_output(self) -> None:
        link = compare_link.compare_url(
            "ssh://user:token@github.com/0321-ops-COS/Hustler.git", BASE, BRANCH
        )
        self.assertEqual(link, EXPECTED)
        self.assertNotIn("token", link or "")

    def test_non_github_hosts_return_none(self) -> None:
        for remote in (
            "https://gitlab.com/0321-ops-COS/Hustler.git",
            "git@bitbucket.org:0321-ops-COS/Hustler.git",
            "ssh://git@github.example.com/0321-ops-COS/Hustler.git",
        ):
            with self.subTest(remote=remote):
                self.assertIsNone(compare_link.compare_url(remote, BASE, BRANCH))

    def test_unparseable_remotes_return_none(self) -> None:
        for remote in (
            "",
            "   ",
            "(no origin remote)",
            "not a url",
            "https://github.com/only-owner",
            "https://github.com/owner/repo/extra",
            "https://github.com/owner/repo?x=1",
        ):
            with self.subTest(remote=remote):
                self.assertIsNone(compare_link.compare_url(remote, BASE, BRANCH))

    def test_empty_base_or_branch_returns_none(self) -> None:
        remote = "https://github.com/0321-ops-COS/Hustler.git"
        self.assertIsNone(compare_link.compare_url(remote, "", BRANCH))
        self.assertIsNone(compare_link.compare_url(remote, BASE, ""))
        self.assertIsNone(compare_link.compare_url(remote, "  ", BRANCH))


def outcome(*, kind: str, base_branch: str) -> jobs.ReviewOutcome:
    return jobs.ReviewOutcome(
        job=jobs.Job(
            id="20260101-000000-deadbeef",
            kind=kind,
            title="add a button",
            channel_id=1,
            requester_id=2,
            log_path=Path("/tmp/20260101-000000-deadbeef.log"),
        ),
        verdict="ask_user",
        notes="",
        environment=SimpleNamespace(
            branch=BRANCH, base_branch=base_branch, kind=kind
        ),
        check_state="passed",
        test_state="passed",
        diff_lines=4,
        changed_files=("ios-app/RootView.swift",),
    )


class ReviewCompareLinkTests(unittest.TestCase):
    settings = SimpleNamespace(
        project=SimpleNamespace(
            target_remote="https://github.com/0321-ops-COS/Hustler.git",
            self_remote="git@github.com:0321-ops-COS/Curator.1.git",
        )
    )

    def test_a_task_compares_the_authorized_branch_to_the_job_branch(self) -> None:
        link = bot.review_compare_url(
            self.settings, outcome(kind="task", base_branch=f"origin/{BASE}")
        )
        self.assertEqual(link, EXPECTED)

    def test_a_dan_job_compares_main_to_its_own_branch(self) -> None:
        link = bot.review_compare_url(
            self.settings, outcome(kind="dan", base_branch="origin/main")
        )
        self.assertEqual(
            link,
            "https://github.com/0321-ops-COS/Curator.1/compare/"
            f"main...{BRANCH}?expand=1",
        )

    def test_no_project_context_means_no_link(self) -> None:
        self.assertIsNone(
            bot.review_compare_url(
                SimpleNamespace(), outcome(kind="task", base_branch=f"origin/{BASE}")
            )
        )


if __name__ == "__main__":
    unittest.main()
