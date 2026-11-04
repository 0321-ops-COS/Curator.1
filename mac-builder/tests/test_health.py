"""/health is one read-only report, and it never invents a number."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import health  # noqa: E402


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True

    async def send_message(self, content: str, **_kwargs: object) -> None:
        self.sent.append(content)


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str, **_kwargs: object) -> None:
        self.sent.append(content)


class FakeInteraction:
    def __init__(self, user_id: int = 42) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def fake_bot(log_dir: Path, worktree_root: Path, *, allowed: tuple[int, ...] = (42,)):
    async def require_allowed(interaction) -> bool:
        # The real bot answers an off-list user and returns False; do the same.
        if interaction.user.id in allowed:
            return True
        await interaction.response.send_message(
            "You are not on this bot's allowlist.", ephemeral=True)
        return False

    return SimpleNamespace(
        settings=SimpleNamespace(
            log_dir=log_dir, worktree_root=worktree_root, bot_name="Melisa",
            allowed_user_ids=frozenset({42}),
        ),
        queue=SimpleNamespace(snapshot=lambda: {
            "running": {"id": "20261104-140735-435f989c", "kind": "task",
                        "title": "t", "started": 0.0, "elapsed": 252.0},
            "pending": 2,
        }),
        manager=SimpleNamespace(enabled=False),
        _started_at=1000.0,
        _sheet_draft=SimpleNamespace(steps=[1, 2, 3]),
        _roadmap_steps=[SimpleNamespace(job_id="a"), SimpleNamespace(job_id="")],
        _roadmap_paused=True,
        require_allowed=require_allowed,
    )


class FormatTests(unittest.TestCase):
    def test_uptime_is_compact(self) -> None:
        self.assertEqual(health.format_uptime(None), "unknown")
        self.assertEqual(health.format_uptime(42), "42s")
        self.assertEqual(health.format_uptime(552), "9m12s")
        self.assertEqual(health.format_uptime(11040), "3h04m")
        self.assertEqual(health.format_uptime(180000), "2d02h")

    def test_bytes_are_readable(self) -> None:
        self.assertEqual(health.format_bytes(None), "unknown")
        self.assertEqual(health.format_bytes(512), "512 B")
        self.assertEqual(health.format_bytes(1536), "1.5 KB")
        self.assertEqual(health.format_bytes(5 * 1024**3), "5.0 GB")


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log_dir, self.worktree_root = self.root / "logs", self.root / "wt"
        self.log_dir.mkdir()
        self.worktree_root.mkdir()

    def test_the_report_shows_the_live_state(self) -> None:
        instance = fake_bot(self.log_dir, self.worktree_root)
        state = health.snapshot(instance, now=1252.0)
        text = health.report(state)

        self.assertIn("up 4m12s", text)
        self.assertIn("running: 20261104-140735-435f989c (task) · 4m12s", text)
        self.assertIn("waiting: 2 job(s)", text)
        self.assertIn("NOT configured", text)
        self.assertIn("roadmap: 1 job(s) left, paused", text)
        self.assertIn("draft sheet: 3 job(s) not queued", text)
        self.assertIn("stray worktrees: none", text)

    def test_a_quiet_bot_and_an_unknown_disk_are_not_guessed(self) -> None:
        instance = fake_bot(self.log_dir, self.worktree_root)
        instance.queue = SimpleNamespace(snapshot=lambda: {"running": None, "pending": 0})
        instance._sheet_draft = None
        instance._roadmap_steps = []
        instance._roadmap_paused = False
        instance._started_at = None

        state = health.snapshot(instance, now=5.0)
        state.disk = None
        text = health.report(state)

        self.assertIn("up unknown", text)
        self.assertIn("running: nothing", text)
        self.assertIn("disk: unknown", text)
        self.assertIn("last log: none yet", text)
        self.assertNotIn("draft sheet", text)

    def test_a_worktree_no_job_log_explains_is_named(self) -> None:
        (self.worktree_root / "20261104-140735-435f989c").mkdir()
        (self.worktree_root / "leftover").mkdir()
        (self.log_dir / "20261104-140735-435f989c.log").write_text("x", encoding="utf-8")

        state = health.snapshot(fake_bot(self.log_dir, self.worktree_root), now=10.0)

        self.assertEqual(state.strays, ["leftover"])
        self.assertEqual(state.last_job[0], "20261104-140735-435f989c")

    def test_a_broken_queue_does_not_break_the_report(self) -> None:
        def boom():
            raise RuntimeError("queue exploded")

        instance = fake_bot(self.log_dir, self.worktree_root)
        instance.queue = SimpleNamespace(snapshot=boom)

        state = health.snapshot(instance, now=10.0)

        self.assertIsNone(state.running)
        self.assertEqual(state.waiting, 0)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log_dir, self.worktree_root = self.root / "logs", self.root / "wt"
        self.log_dir.mkdir()
        self.worktree_root.mkdir()

    async def test_health_is_ephemeral_and_read_only(self) -> None:
        instance = fake_bot(self.log_dir, self.worktree_root)
        interaction = FakeInteraction()

        await health.command(instance, interaction)

        self.assertTrue(interaction.response.deferred, "the reply is ephemeral")
        self.assertIn("Melisa health", interaction.followup.sent[0])
        self.assertEqual(list(self.log_dir.iterdir()), [], "nothing was written")

    async def test_a_user_off_the_allowlist_gets_no_report(self) -> None:
        instance = fake_bot(self.log_dir, self.worktree_root)
        interaction = FakeInteraction(user_id=9)

        await health.command(instance, interaction)

        self.assertIn("allowlist", interaction.response.sent[0])
        self.assertEqual(interaction.followup.sent, [])


if __name__ == "__main__":
    unittest.main()
