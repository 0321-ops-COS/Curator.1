"""Settings: REPO_DIR keeps its default and her labels come from the environment."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402


def settings_from(**values: str) -> "bot.Settings":
    env = {"DISCORD_TOKEN": "token", "ALLOWED_USER_IDS": "42", **values}
    with mock.patch.dict(os.environ, env, clear=True):
        return bot.Settings.from_env()


class SettingsTests(unittest.TestCase):
    def test_repo_dir_still_defaults_to_the_bot_checkout(self) -> None:
        # A required REPO_DIR would crash-loop the Mac LaunchAgent (KeepAlive is
        # 15s) until the .env on the Mac sets it. The startup preflight is what
        # refuses a target that cannot be built.
        self.assertEqual(settings_from().repo_dir, bot.REPO_ROOT.resolve())

    def test_labels_come_from_the_environment(self) -> None:
        settings = settings_from(
            PROJECT_NAME="Hustler", BUILDER_NAME="Freebuff", BOT_NAME="Melisa"
        )
        self.assertEqual(settings.project_name, "Hustler")
        self.assertEqual(settings.builder_name, "Freebuff")
        self.assertEqual(settings.bot_name, "Melisa")

    def test_labels_have_usable_defaults(self) -> None:
        settings = settings_from()
        self.assertEqual(settings.project_name, "")
        self.assertEqual(settings.builder_name, "Freebuff")
        self.assertEqual(settings.bot_name, "Melisa")


if __name__ == "__main__":
    unittest.main()
