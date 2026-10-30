"""Which local checkouts /status can see, and how a name maps onto one."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import repos  # noqa: E402


def make_repo(root: Path, name: str, *, branch: str = "main", commit: bool = True) -> Path:
    path = root / name
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True)
    if commit:
        subprocess.run(
            ["git", "-C", str(path), "commit", "-qm", "base"], check=True
        )
        subprocess.run(
            ["git", "-C", str(path), "branch", "-m", branch], check=True
        )
    return path


class DiscoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.first = make_repo(self.root, "Curator.1")
        self.second = make_repo(self.root, "Hustler")
        (self.root / "notes").mkdir()  # not a checkout
        (self.root / ".hidden").mkdir()  # skipped by name

    def test_only_git_checkouts_are_listed(self) -> None:
        found = repos.discover(self.root)
        self.assertEqual([item.name for item in found], ["Curator.1", "Hustler"])

    def test_a_checkout_is_described_with_branch_and_head(self) -> None:
        found = {item.name: item for item in repos.discover(self.root)}
        target = found["Hustler"]
        self.assertEqual(target.branch, "main")
        self.assertEqual(target.changed, 0)
        self.assertTrue(target.head.startswith("base") or "base" in target.head)
        self.assertEqual(target.path, self.second.resolve())

    def test_a_dirty_checkout_counts_its_changed_files(self) -> None:
        (self.first / "file.txt").write_text("changed\n", encoding="utf-8")
        (self.first / "new.txt").write_text("new\n", encoding="utf-8")
        found = {item.name: item for item in repos.discover(self.root)}
        self.assertEqual(found["Curator.1"].changed, 2)
        self.assertIn("2 changed", found["Curator.1"].line())

    def test_a_missing_root_is_empty_not_an_error(self) -> None:
        self.assertEqual(repos.discover(self.root / "nowhere"), ())

    def test_the_listing_is_capped(self) -> None:
        for index in range(3):
            make_repo(self.root, f"extra{index}")
        self.assertEqual(len(repos.discover(self.root, limit=2)), 2)


class FindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        make_repo(self.root, "Hustler")

    def test_a_name_is_matched_without_case(self) -> None:
        found = repos.find(self.root, "hustler")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Hustler")

    def test_an_owner_slash_repo_name_is_accepted(self) -> None:
        found = repos.find(self.root, "0321-ops-COS/Hustler")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Hustler")

    def test_a_unknown_name_is_none(self) -> None:
        self.assertIsNone(repos.find(self.root, "GEN2"))

    def test_paths_are_never_accepted(self) -> None:
        for value in ("../etc", "/etc", "..", ".", "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(repos.find(self.root, value))


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[str] = []
        self.followups: list[str] = []

    async def defer(self, **kwargs) -> None:
        self.deferred = True

    async def send_message(self, content, **kwargs) -> None:
        self.messages.append(content)


class _FakeFollowup:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def send(self, content, **kwargs) -> None:
        self.response.followups.append(content)


class _FakeInteraction:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=1)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup(self.response)


class StatusCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = make_repo(self.root, "Hustler")
        self.instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        self.instance.settings = SimpleNamespace(
            repo_dir=self.repo,
            repos_root=self.root,
            log_dir=self.root / "logs",
            authorized_branch="freebuff/base",
            project=None,
            status_timeout_seconds=30,
            hustlercore_dir="ios-app/HustlerCore",
            xcode_project="ios-app/Hustler.xcodeproj",
            xcode_scheme="Hustler",
            simulator_name="iPhone 16",
            swift_jobs=2,
            xcodebuild_jobs=2,
        )
        self.instance.queue = SimpleNamespace(
            snapshot=lambda: {"pending": 0, "running": None}
        )
        self.instance.manager = SimpleNamespace(enabled=False)

        async def allowed(_interaction) -> bool:
            return True

        self.instance.require_allowed = allowed

    async def test_without_a_repo_it_asks_which_one(self) -> None:
        interaction = _FakeInteraction()

        await self.instance.cmd_status(interaction)

        text = interaction.response.followups[0]
        self.assertIn("which repo?", text)
        self.assertIn("Hustler: main", text)
        self.assertIn("repo:<name>", text)

    async def test_a_named_repo_is_reported_on_its_own(self) -> None:
        interaction = _FakeInteraction()

        await self.instance.cmd_status(interaction, "Hustler")

        text = interaction.response.followups[0]
        self.assertIn("repo: Hustler", text)
        self.assertIn("branch: main", text)
        self.assertNotIn("which repo?", text)

    async def test_an_unknown_repo_lists_the_ones_it_knows(self) -> None:
        interaction = _FakeInteraction()

        await self.instance.cmd_status(interaction, "GEN2")

        text = interaction.response.followups[0]
        self.assertIn("No checkout named `GEN2`", text)
        self.assertIn("Known: Hustler", text)


if __name__ == "__main__":
    unittest.main()
