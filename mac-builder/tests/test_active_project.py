"""A project chosen with /project use is applied at the next start, not mid-run."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_project  # noqa: E402
import projects  # noqa: E402


@dataclass(frozen=True)
class FakeSettings:
    repo_dir: Path
    authorized_branch: str
    project_name: str
    allowed_user_ids: frozenset[int]
    log_dir: Path
    projects: projects.Registry
    project: object = None


class ChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()
        self.registry = projects.Registry([
            projects.Project("hustler", "Hustler", "git@github.com:o/Hustler.git",
                             self.root, "freebuff/base"),
            projects.Project("notes", "Notes", "git@github.com:o/Notes.git",
                             self.root / "notes", "freebuff/base", kind="python",
                             checks=("pytest",)),
        ])
        self.settings = FakeSettings(
            repo_dir=self.root, authorized_branch="freebuff/base", project_name="Hustler",
            allowed_user_ids=frozenset({42}), log_dir=self.log_dir,
            projects=self.registry,
        )

    def test_a_choice_round_trips_and_a_broken_file_is_no_choice(self) -> None:
        self.assertEqual(active_project.load(self.log_dir), "")
        active_project.save(self.log_dir, "notes")
        self.assertEqual(active_project.load(self.log_dir), "notes")
        active_project.path_for(self.log_dir).write_text("{nope", encoding="utf-8")
        self.assertEqual(active_project.load(self.log_dir), "")
        active_project.clear(self.log_dir)
        self.assertFalse(active_project.path_for(self.log_dir).exists())

    def test_use_retargets_only_the_project_fields(self) -> None:
        settings, project = active_project.retarget(self.settings, "notes")

        assert project is not None
        self.assertEqual(project.id, "notes")
        self.assertEqual(settings.repo_dir, Path(self.registry.get("notes").path))
        self.assertEqual(settings.authorized_branch, "freebuff/base")
        self.assertEqual(settings.project_name, "Notes")
        self.assertEqual(settings.allowed_user_ids, frozenset({42}), "the allowlist never moves")
        self.assertIs(settings.projects, self.registry, "the registry never moves")

    def test_an_unknown_id_or_no_choice_changes_nothing(self) -> None:
        for wanted in ("ghost", ""):
            settings, project = active_project.retarget(self.settings, wanted)
            self.assertIsNone(project)
            self.assertIs(settings, self.settings)

    def test_the_default_project_needs_no_change(self) -> None:
        settings, project = active_project.retarget(self.settings, "hustler")
        assert project is not None
        self.assertEqual(project.id, "hustler")
        self.assertIs(settings, self.settings)

    def test_the_active_id_is_read_from_the_target_path(self) -> None:
        self.assertEqual(active_project.active_id(self.settings), "hustler")
        switched, _project = active_project.retarget(self.settings, "notes")
        self.assertEqual(active_project.active_id(switched), "notes")

    def test_the_refusal_names_what_exists(self) -> None:
        self.assertEqual(active_project.refusal(self.registry, "notes", "hustler"), "")
        self.assertIn("unknown project 'ghost'", active_project.refusal(self.registry, "ghost", "hustler"))
        self.assertIn("hustler, notes", active_project.refusal(self.registry, "ghost", "hustler"))
        self.assertIn("pick a project id", active_project.refusal(self.registry, "", "hustler"))


if __name__ == "__main__":
    unittest.main()
