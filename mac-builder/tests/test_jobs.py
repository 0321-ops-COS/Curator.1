from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobs  # noqa: E402


def settings(log_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        log_dir=log_dir,
        hustlercore_dir="ios-app/HustlerCore",
        xcode_project="ios-app/Hustler.xcodeproj",
        xcode_scheme="Hustler",
        simulator_name="iPhone 16",
        swift_jobs=2,
        xcodebuild_jobs=2,
    )


class ScriptEnvTests(unittest.TestCase):
    def test_secrets_are_not_forwarded(self) -> None:
        old = dict(os.environ)
        os.environ["DISCORD_TOKEN"] = "super-secret"
        os.environ["MANAGER_API_KEY"] = "also-secret"
        try:
            env = jobs.build_script_env(settings(Path("/tmp")), Path("/repo"))
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertNotIn("DISCORD_TOKEN", env)
        self.assertNotIn("MANAGER_API_KEY", env)
        self.assertEqual(env["REPO_DIR"], "/repo")
        self.assertEqual(env["XCODE_SCHEME"], "Hustler")
        self.assertIn("/opt/homebrew/bin", env["PATH"])


class JobNamingTests(unittest.TestCase):
    def test_job_id_matches_log_and_regex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = jobs.make_job(
                settings(Path(tmp)),
                kind="check",
                title="swift test",
                channel_id=1,
                requester_id=2,
            )
            self.assertTrue(jobs.JOB_ID_RE.match(job.id))
            self.assertEqual(job.log_path.name, f"{job.id}.log")
            self.assertEqual(job.log_path.parent, Path(tmp))
            self.assertTrue(job.log_path.parent.is_dir())


class TextTests(unittest.TestCase):
    def test_tail_text_strips_ansi_and_limits(self) -> None:
        raw = "\x1b[31mred\x1b[0m\n" + "\n".join(f"line {i}" for i in range(100))
        text = jobs.tail_text(raw, max_lines=10, max_chars=500)
        self.assertNotIn("\x1b[", text)
        self.assertIn("line 99", text)
        self.assertNotIn("line 0", text)
        self.assertLessEqual(len(text), 500 + len("[earlier output omitted]\n"))

    def test_followup_keeps_task_and_notes(self) -> None:
        text = jobs.build_followup_text("Do the thing", "Fix the crash")
        self.assertIn("Do the thing", text)
        self.assertIn("Fix the crash", text)
        self.assertIn("Do not touch protected files", text)

    def test_queue_module_is_not_stdlib(self) -> None:
        self.assertEqual(jobs.Job.__module__, "macbuilder_queue")
        self.assertTrue(hasattr(jobs, "JobQueue"))
        self.assertTrue(hasattr(jobs, "QueueFull"))


if __name__ == "__main__":
    unittest.main()
