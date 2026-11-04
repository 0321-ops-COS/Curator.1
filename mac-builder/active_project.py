"""Which registered project the bot works on, chosen from Discord.

The bot's target checkout, base branch and project context are fixed when it
starts: the runner, the queue and the push guard are all built from them, so
re-pointing a running bot at another repository mid-flight would be a guardrail
bug, not a feature.

So /project use records the choice and the next start applies it, before the
context is built and before anything is constructed from it. Until then /project
says plainly which project is active now and which one is chosen for the next
start — a phone command that quietly claimed to have switched repositories would
be worse than one that waits.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import projects

log = logging.getLogger("macbuilder")

CHOICE_FILE = "active-project.json"


def path_for(log_dir: Path | str) -> Path:
    """Where the choice is saved: beside roadmap.json, in LOG_DIR."""
    return Path(log_dir) / CHOICE_FILE


def load(log_dir: Path | str) -> str:
    """The saved project id, or "" when nothing valid is saved."""
    try:
        data = json.loads(path_for(log_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("project") or "").strip().lower()


def save(log_dir: Path | str, project_id: str) -> None:
    """Record the project to use at the next start; never raise into a command."""
    path = path_for(log_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"project": project_id}), "utf-8")
    except OSError:
        log.warning("could not save the active project in %s", path)


def clear(log_dir: Path | str) -> None:
    try:
        path_for(log_dir).unlink(missing_ok=True)
    except OSError:
        log.warning("could not clear the active project in %s", log_dir)


def retarget(settings, project_id: str):
    """Settings aimed at a registry entry: (settings, project) or (settings, None).

    Only the target fields change — ``repo_dir``, ``authorized_branch`` and the
    display name. The registry, the allowlist and every guardrail stay exactly
    as they were, and an id nobody registered changes nothing.
    """
    registry = getattr(settings, "projects", None)
    if not project_id or not isinstance(registry, projects.Registry):
        return settings, None
    project = registry.get(project_id)
    if project is None:
        log.warning(
            "%s names unknown project %r; keeping the default", CHOICE_FILE, project_id
        )
        return settings, None
    if project.is_default:
        log.info("active project from %s: %s", CHOICE_FILE, project.id)
        return settings, project
    settings = dataclasses.replace(
        settings,
        repo_dir=Path(project.path),
        authorized_branch=project.authorized_branch,
        project_name=project.name,
    )
    log.info("active project from %s: %s (%s)", CHOICE_FILE, project.id, project.path)
    return settings, project


def active_id(settings) -> str:
    """The project id the loaded settings point at, default when unknown."""
    registry = getattr(settings, "projects", None)
    repo = getattr(settings, "repo_dir", None)
    if isinstance(registry, projects.Registry) and repo is not None:
        for project in registry:
            try:
                if Path(project.path).resolve() == Path(repo).resolve():
                    return project.id
            except OSError:  # pragma: no cover - an odd path is not a crash
                continue
    return projects.DEFAULT_PROJECT


def refusal(registry, project_id: str, current: str) -> str:
    """Why a chosen id cannot be used, or "" when it can."""
    if not project_id:
        known = ", ".join(registry.ids()) if isinstance(registry, projects.Registry) else current
        return f"Not switched: pick a project id, one of {known}."
    if not isinstance(registry, projects.Registry) or registry.get(project_id) is None:
        known = ", ".join(registry.ids()) if isinstance(registry, projects.Registry) else current
        return f"Not switched: unknown project {project_id!r}. Known: {known}."
    return ""
