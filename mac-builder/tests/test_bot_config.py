"""Settings: REPO_DIR keeps its default and her labels come from the environment."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
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

    def test_question_defaults_keep_auto_answer_off(self) -> None:
        settings = settings_from()
        self.assertFalse(settings.question_auto_answer)
        self.assertEqual(settings.question_auto_answer_max, 3)
        self.assertEqual(settings.question_manager_cooldown_seconds, 30.0)
        self.assertFalse(settings.question_suggest)

    def test_a_negative_auto_answer_cap_is_refused(self) -> None:
        with self.assertRaises(bot.ConfigError):
            settings_from(QUESTION_AUTO_ANSWER_MAX="-1")

    def test_a_negative_manager_cooldown_is_refused(self) -> None:
        with self.assertRaises(bot.ConfigError):
            settings_from(QUESTION_MANAGER_COOLDOWN_SECONDS="-5")

    def test_manager_fallback_models_default_to_empty(self) -> None:
        self.assertEqual(settings_from().manager_fallback_models, ())

    def test_manager_fallback_models_are_trimmed_and_blanks_dropped(self) -> None:
        settings = settings_from(MANAGER_FALLBACK_MODELS=" backup-a , ,backup-b ")
        self.assertEqual(settings.manager_fallback_models, ("backup-a", "backup-b"))


class ManagerFallbackWiringTests(unittest.TestCase):
    def test_the_bot_hands_the_fallbacks_to_the_manager(self) -> None:
        settings = settings_from(MANAGER_FALLBACK_MODELS="backup-a,backup-b")
        instance = bot.MacBuilderBot(settings)
        self.assertEqual(instance.manager.fallback_models, ("backup-a", "backup-b"))


class LaunchAgentTests(unittest.TestCase):
    """The plist must run the folder that holds this code, and say which it is."""

    def setUp(self) -> None:
        self.path = bot.HERE / "launchd" / "com.hustler.macbuilder.plist"
        self.text = self.path.read_text(encoding="utf-8")

    def test_the_plist_is_valid_and_runs_the_bot_from_one_path(self) -> None:
        data = plistlib.loads(self.path.read_bytes())
        self.assertEqual(data["Label"], "com.hustler.macbuilder")
        command = data["ProgramArguments"][-1]
        self.assertIn("MACBUILDER_DIR", command)
        self.assertIn('"$MB/bot.py"', command)
        self.assertIn('"$MB/logs/launchd.log"', command)

    def test_the_old_hustler_mac_builder_path_is_gone(self) -> None:
        # mac-builder moved out of Hustler; a stale path here silently runs old
        # code from a folder nobody edits any more.
        self.assertNotIn("Hustler/mac-builder", self.text)
        self.assertIn("$HOME/Curator.1/mac-builder", self.text)


class CodeLocationTests(unittest.TestCase):
    """The startup line has to name the code that is actually running."""

    def test_a_folder_that_is_not_a_checkout_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bot.code_location(Path(tmp)), "not a git checkout")

    def test_a_checkout_reports_its_branch_and_short_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", "-C", str(root), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init", "-q", "-b", "freebuff/test")
            git(
                "-c",
                "user.email=bot@example.test",
                "-c",
                "user.name=bot",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "one",
            )
            location = bot.code_location(root)
            self.assertTrue(location.startswith("freebuff/test@"), location)
            short = location.split("@", 1)[1]
            self.assertEqual(len(short), 7, short)
            self.assertRegex(short, r"^[0-9a-f]+$")


if __name__ == "__main__":
    unittest.main()
