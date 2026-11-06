"""Routing: notifications get every job, #ai gets her replies, all repos named."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import projects  # noqa: E402

JOB_ID = "20260101-000000-deadbeef"
OWNER = 42


class FakeChannel:
    """Enough of a text channel for send(), and a name for the lookup."""

    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.sent: list[SimpleNamespace] = []

    async def send(self, content: str | None = None, **kwargs: object) -> SimpleNamespace:
        self.sent.append(SimpleNamespace(content=content, **kwargs))
        return SimpleNamespace(id=1000 + len(self.sent))


class FakeThread:
    """Patched in as discord.Thread, so threads need no live gateway state."""

    def __init__(self, channel_id: int, name: str, parent: FakeChannel) -> None:
        self.id = channel_id
        self.name = name
        self.parent = parent


class FakePublisher:
    def __init__(self) -> None:
        self.tracked: list[dict] = []
        self.reported: list[tuple[str, str]] = []

    def track(self, job_id: str, **kwargs: object) -> None:
        self.tracked.append({"job_id": job_id, **kwargs})

    async def report(
        self, job_id: str, stage: str, detail: str = "", **kwargs: object
    ) -> None:
        self.reported.append((stage, detail))


def job_ref(**overrides: object) -> bot.Job:
    values: dict[str, object] = dict(
        id=JOB_ID,
        kind="task",
        title="make the list scroll",
        channel_id=99,
        requester_id=OWNER,
        log_path=Path("/tmp/job.log"),
    )
    values.update(overrides)
    return bot.Job(**values)  # type: ignore[arg-type]


class NotificationRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def instance(
        self,
        *,
        channels: list[FakeChannel],
        notify_id: int | None = None,
        notify_name: str = "notifications",
    ) -> bot.MacBuilderBot:
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(
            log_dir=Path(self._tmp.name),
            notify_channel_id=notify_id,
            notify_channel_name=notify_name,
            ai_channel_id=3,
            ai_channel_name="ai",
            project=SimpleNamespace(project_name="Hustler"),
            projects=None,
        )
        instance.get_channel = lambda channel_id: next(
            (channel for channel in channels if channel.id == channel_id), None
        )
        instance.get_all_channels = lambda: list(channels)
        return instance

    async def test_the_notifications_chat_is_found_by_name(self) -> None:
        general = FakeChannel(5, "general")
        notifications = FakeChannel(6, "notifications")
        instance = self.instance(
            channels=[general, notifications], notify_name="#notifications"
        )
        with mock.patch.object(bot.discord, "TextChannel", FakeChannel):
            self.assertIs(instance._notify_channel(), notifications)

    async def test_an_explicit_id_wins_over_the_name(self) -> None:
        configured = FakeChannel(7, "builds")
        notifications = FakeChannel(6, "notifications")
        instance = self.instance(
            channels=[configured, notifications], notify_id=7
        )
        with mock.patch.object(bot.discord, "TextChannel", FakeChannel):
            self.assertIs(instance._notify_channel(), configured)

    async def test_a_job_reports_to_notifications_not_where_it_started(self) -> None:
        started = FakeChannel(99, "builds")
        notifications = FakeChannel(6, "notifications")
        instance = self.instance(channels=[started, notifications])
        with mock.patch.object(bot.discord, "TextChannel", FakeChannel):
            with mock.patch.object(bot.job_index, "record", lambda *a, **k: None):
                await instance._post_job_result(job_ref(), True, "done")
        self.assertEqual(len(notifications.sent), 1)
        self.assertEqual(started.sent, [])

    async def test_without_notifications_a_job_falls_back_to_its_channel(self) -> None:
        started = FakeChannel(99, "builds")
        instance = self.instance(channels=[started])
        with mock.patch.object(bot.discord, "TextChannel", FakeChannel):
            self.assertIs(instance._notification_channel(99), started)

    async def test_the_board_goes_to_notifications_for_every_repo(self) -> None:
        started = FakeChannel(99, "notes-work")
        notifications = FakeChannel(6, "notifications")
        instance = self.instance(channels=[started, notifications])
        instance.settings.projects = projects.Registry([
            projects.Project(
                id="hustler", name="Hustler", remote="",
                path=Path("/tmp/hustler"), authorized_branch="freebuff/base",
            ),
            projects.Project(
                id="notes", name="Notes", remote="",
                path=Path("/tmp/notes"), authorized_branch="freebuff/base",
            ),
        ])
        instance._board = FakePublisher()

        with mock.patch.object(bot.discord, "TextChannel", FakeChannel):
            await instance._track_board(job_ref(project="notes"), 1)
            await instance._track_board(
                job_ref(id="20260101-000000-feedface", project="hustler"), 2
            )

        self.assertEqual([row["channel_id"] for row in instance._board.tracked], [6, 6])
        self.assertEqual(
            [row["project"] for row in instance._board.tracked], ["Notes", "Hustler"]
        )

    async def test_without_notifications_a_board_stays_where_the_job_started(self) -> None:
        started = FakeChannel(99, "builds")
        instance = self.instance(channels=[started])
        with mock.patch.object(bot.discord, "TextChannel", FakeChannel):
            self.assertEqual(instance._board_channel_id(job_ref()), 99)


class AiChatTests(unittest.TestCase):
    def instance(self, *, ai_id: int | None = 3, ai_name: str = "ai"):
        instance = bot.MacBuilderBot.__new__(bot.MacBuilderBot)
        instance.settings = SimpleNamespace(
            notify_channel_id=None,
            notify_channel_name="notifications",
            ai_channel_id=ai_id,
            ai_channel_name=ai_name,
        )
        return instance

    def test_ai_threads_are_the_ai_chat(self) -> None:
        ai = FakeChannel(3, "ai")
        other = FakeChannel(4, "builds")
        topic = FakeThread(9, "a-topic", ai)
        elsewhere = FakeThread(10, "b-topic", other)
        instance = self.instance()
        with mock.patch.object(bot.discord, "Thread", FakeThread):
            self.assertTrue(instance._is_ai_channel(ai))
            self.assertTrue(instance._is_ai_channel(topic))
            self.assertFalse(instance._is_ai_channel(elsewhere))

    def test_ai_threads_work_when_the_chat_is_found_by_name(self) -> None:
        ai = FakeChannel(3, "ai")
        other = FakeChannel(4, "builds")
        topic = FakeThread(9, "a-topic", ai)
        instance = self.instance(ai_id=None)
        with mock.patch.object(bot.discord, "Thread", FakeThread):
            self.assertTrue(instance._is_ai_channel(topic))
            self.assertFalse(instance._is_ai_channel(FakeThread(10, "t", other)))


class NotificationSettingsTests(unittest.TestCase):
    def settings_from(self, **values: str) -> "bot.Settings":
        env = {"DISCORD_TOKEN": "token", "ALLOWED_USER_IDS": "42", **values}
        with mock.patch.dict(os.environ, env, clear=True):
            return bot.Settings.from_env()

    def test_the_notifications_name_defaults_and_strips_a_hash(self) -> None:
        self.assertEqual(self.settings_from().notify_channel_name, "notifications")
        self.assertEqual(
            self.settings_from(NOTIFY_CHANNEL_NAME="#notifications").notify_channel_name,
            "notifications",
        )

    def test_an_empty_name_falls_back_to_notifications(self) -> None:
        self.assertEqual(
            self.settings_from(NOTIFY_CHANNEL_NAME="#").notify_channel_name,
            "notifications",
        )
