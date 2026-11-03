"""The repositories the bot may build, and which checkout a job belongs to.

Phase 1 of multi-project support. ``hustler`` is always synthesized from the
existing settings (``REPO_DIR``, ``AUTHORIZED_BRANCH``, ``PROJECT_NAME``), so a
Mac with no ``projects.json`` behaves exactly as it did before this module
existed: one project, one checkout, one base branch.

Extra projects come from an optional ``projects.json`` beside this file::

    {"projects": [
      {"id": "notes", "name": "Notes", "path": "~/Desktop/Notes",
       "remote": "git@github.com:owner/Notes.git",
       "authorized_branch": "freebuff/base", "kind": "python",
       "checks": ["pytest"], "protected_paths": [".env"]}
    ]}

A bad extra entry is skipped with one log line and listed in ``/project``: a
typo in a file beside the bot is never a reason to refuse to start, and
``hustler`` itself is never skipped.

Where a job's worktree goes, which checkout it really belongs to, and whether
that checkout may build and push for the project all live here rather than in
the runner. The only git calls are two read-only probes of a worktree; nothing
here writes a ref or a file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import compare_link
import project_context
import rules

log = logging.getLogger(__name__)

# The project every job belongs to unless it names another one. It is the
# checkout in REPO_DIR, the base branch in AUTHORIZED_BRANCH.
DEFAULT_PROJECT = "hustler"
KINDS = ("ios", "python")
# A soft cap: a registry longer than this is a mistake, not a plan.
MAX_PROJECTS = 20
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# project_context.remote_url's wording when a checkout has no origin at all.
_NO_REMOTE = "(no origin remote)"


@dataclass(frozen=True)
class Project:
    """One checkout the bot may build, with the rules its jobs follow."""

    id: str
    name: str
    remote: str
    path: Path
    authorized_branch: str
    kind: str = "ios"
    checks: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT_PROJECT

    @property
    def uses_checks(self) -> bool:
        """True when this project's own checks replace the Mac's scripts.

        Only a non-default python project runs checks: ``hustler`` keeps
        scripts/check.sh and scripts/test.sh whatever its entry says, so a job
        on the Mac's own checkout behaves exactly as it did before projects.
        """
        return not self.is_default and self.kind == "python"

    def summary(self) -> str:
        """One line for /project."""
        tail = " (default)" if self.is_default else ""
        details = [self.kind, f"base {self.authorized_branch or 'not set'}"]
        if self.checks:
            details.append("checks: " + ", ".join(self.checks))
        if self.protected_paths:
            details.append("protected: " + ", ".join(self.protected_paths))
        return (f"{self.id} — {self.name} · {self.path} · "
                + " · ".join(details) + tail)


@dataclass(frozen=True)
class Rejected:
    """An extra entry that was skipped, and the one line saying why."""

    label: str
    reason: str

    def line(self) -> str:
        return f"{self.label}: {self.reason}"


class Registry:
    """The projects a job may name, the default first."""

    def __init__(
        self, projects: Sequence[Project], rejected: Iterable[Rejected] = ()
    ) -> None:
        if not projects:
            raise ValueError("a project registry needs its default project")
        if projects[0].id != DEFAULT_PROJECT:
            raise ValueError(f"the first project must be {DEFAULT_PROJECT!r}")
        self._projects = tuple(projects)
        self._by_id = {project.id: project for project in projects}
        if len(self._by_id) != len(self._projects):
            raise ValueError("two projects cannot share an id")
        self.rejected = tuple(rejected)

    def __iter__(self):
        return iter(self._projects)

    def __len__(self) -> int:
        return len(self._projects)

    def __contains__(self, project_id: object) -> bool:
        return str(project_id or "") in self._by_id

    @property
    def default(self) -> Project:
        return self._projects[0]

    def get(self, project_id: object) -> Project | None:
        return self._by_id.get(str(project_id or "").strip())

    def require(self, project_id: object) -> Project:
        """The project, or ValueError naming the ones that exist."""
        project = self.get(project_id)
        if project is None:
            raise ValueError(
                f"unknown project {str(project_id or '')!r}; "
                f"known: {', '.join(self.ids()) or 'none'}"
            )
        return project

    def ids(self) -> tuple[str, ...]:
        return tuple(project.id for project in self._projects)

    def choices(self) -> list[tuple[str, str]]:
        """(id, name) pairs, for an autocomplete list."""
        return [(project.id, project.name) for project in self._projects]

    def summary_lines(self) -> list[str]:
        """The project list for /project, skipped entries included."""
        lines = ["Projects:"]
        lines += [f"  {project.summary()}" for project in self._projects]
        if self.rejected:
            lines += [f"Skipped in projects.json (still on {DEFAULT_PROJECT} only for those):"]
            lines += [f"  {item.line()}" for item in self.rejected]
        return lines


def _clean_remote(value: object) -> str:
    text = str(value or "").strip()
    return "" if text == _NO_REMOTE else text


def _remote_from_checkout(path: Path) -> str:
    """The checkout's origin, read without changing it; "" when there is none."""
    try:
        return _clean_remote(project_context.remote_url(path))
    except (OSError, ValueError):  # pragma: no cover - remote_url already guards
        return ""


def remote_key(url: str) -> str:
    """A comparable identity for a remote URL: OWNER/REPO when GitHub.

    Anything else keeps host and path with credentials, scheme and a trailing
    ``.git`` dropped, so two spellings of the same remote compare equal.
    """
    value = _clean_remote(url)
    if not value:
        return ""
    slug = compare_link.owner_repo(value)
    if slug:
        return slug.lower()
    cleaned = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", value)
    cleaned = cleaned.rsplit("@", 1)[-1]
    if ":" in cleaned and "/" not in cleaned.split(":", 1)[0]:
        cleaned = cleaned.replace(":", "/", 1)  # scp-like git@host:OWNER/REPO
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned.strip("/").lower()


def path_is_protected(entries: Sequence[str], path: str) -> bool:
    """True when a repo-relative path matches one of these protected entries.

    An entry is an exact file, or a directory when it ends with "/" (the
    directory and everything below it), compared as the diff reports it. The
    gates share it so /merge reads a project's paths exactly as review does.
    """
    value = rules.normalize_path(path)
    if not value:
        return False
    for entry in entries:
        wanted = rules.normalize_path(entry)
        if wanted.endswith("/") and value.startswith(wanted):
            return True
        if value in (wanted, wanted.rstrip("/")):
            return True
    return False


def matches_protected(project: Project, path: str) -> bool:
    """True when a repo-relative path is one this project protects."""
    return path_is_protected(project.protected_paths, path)


def protected_violations(
    project: Project, changed_paths: Sequence[str]
) -> list[rules.RuleViolation]:
    """A project's own protected-path refusals, worded like the global ones.

    The caller adds these to ``rules.evaluate_change``'s list: a project may
    only protect more paths, so the global list still rejects for all.
    """
    bad = sorted(
        {
            rules.normalize_path(path)
            for path in changed_paths
            if matches_protected(project, path)
        }
    )
    violations = [
        rules.RuleViolation(
            code="protected_path",
            detail=f"Protected path changed in project '{project.id}': {path}",
            path=path,
        )
        for path in bad[: rules.MAX_PROTECTED_REPORTS]
    ]
    if len(bad) > rules.MAX_PROTECTED_REPORTS:
        rest = len(bad) - rules.MAX_PROTECTED_REPORTS
        violations.append(rules.RuleViolation(
            code="protected_path",
            detail=f"... and {rest} more protected paths in project '{project.id}'."))
    return violations


def _path_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:  # pragma: no cover - resolve only fails on a broken path
        return str(path.expanduser())


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if isinstance(item, str) and item.strip())


def synthesize(
    *,
    repo_dir: object,
    authorized_branch: object,
    project_name: object = "",
    remote: str | None = None,
) -> Project:
    """The ``hustler`` project, built from the settings the bot already has."""
    path = Path(repo_dir).expanduser()
    branch = str(authorized_branch or "").strip()
    if branch:
        try:
            branch = rules.assert_not_main(branch)
        except ValueError:
            # Settings.from_env refuses this at startup; the registry must not
            # crash a running bot over it, so it keeps the name as given.
            pass
    return Project(
        id=DEFAULT_PROJECT,
        name=str(project_name or "").strip() or path.name or "Hustler",
        remote=_remote_from_checkout(path) if remote is None else _clean_remote(remote),
        path=path,
        authorized_branch=branch,
        kind="ios",
    )


def _parse_entry(entry: object, index: int, folder: Path) -> tuple[Project | None, str]:
    """One projects.json entry as a Project, or (None, why it was skipped)."""
    fallback = f"entry {index + 1}"
    if not isinstance(entry, dict):
        return None, f"{fallback} is not an object"
    project_id = str(entry.get("id") or "").strip().lower()
    label = project_id or fallback
    if not _ID_RE.match(project_id):
        return None, "id must be 1-32 lowercase letters, digits, - or _"
    if project_id == DEFAULT_PROJECT:
        return None, f"id {DEFAULT_PROJECT!r} is reserved for the REPO_DIR checkout"

    raw_path = str(entry.get("path") or "").strip()
    if not raw_path:
        return None, "path is empty"
    project_path = Path(raw_path).expanduser()
    if not project_path.is_absolute():
        project_path = folder / project_path
    project_path = project_path.resolve()
    if not (project_path / ".git").exists():
        return None, f"path {project_path} is not a git checkout"

    remote = _clean_remote(entry.get("remote")) or _remote_from_checkout(project_path)
    if not remote:
        return None, "remote is empty and the checkout has no origin"

    try:
        branch = rules.assert_not_main(str(entry.get("authorized_branch") or ""))
    except ValueError as exc:
        return None, str(exc)

    kind = str(entry.get("kind") or "ios").strip().lower()
    if kind not in KINDS:
        return None, f"kind {kind!r} is not one of {', '.join(KINDS)}"
    if not _strings(entry.get("checks")) and entry.get("checks") not in (None, [], ()):
        return None, "checks must be a list of strings"
    if not _strings(entry.get("protected_paths")) and entry.get(
        "protected_paths"
    ) not in (None, [], ()):
        return None, "protected_paths must be a list of strings"

    return (
        Project(
            id=project_id,
            name=str(entry.get("name") or "").strip() or project_id,
            remote=remote,
            path=project_path,
            authorized_branch=branch,
            kind=kind,
            checks=_strings(entry.get("checks")),
            protected_paths=_strings(entry.get("protected_paths")),
        ),
        "",
    )


def load(
    *,
    repo_dir: object,
    authorized_branch: object,
    project_name: object = "",
    path: Path | str | None = None,
) -> Registry:
    """The registry: ``hustler`` plus whatever projects.json validly adds."""
    default = synthesize(
        repo_dir=repo_dir, authorized_branch=authorized_branch, project_name=project_name
    )
    projects: list[Project] = [default]
    rejected: list[Rejected] = []
    seen_ids = {default.id}
    seen_paths = {_path_key(default.path): default.id}
    seen_remotes: dict[str, str] = {}
    if remote_key(default.remote):
        seen_remotes[remote_key(default.remote)] = default.id

    file = Path(path) if path is not None else Path(__file__).with_name("projects.json")
    if not file.is_file():
        return Registry(projects)

    try:
        data = json.loads(file.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        rejected.append(Rejected(file.name, f"could not be read ({exc}); nothing extra was loaded"))
        log.warning("projects.json: %s", rejected[-1].line())
        return Registry(projects, rejected)

    entries = data.get("projects") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        rejected.append(Rejected(file.name, 'has no "projects" list; nothing extra was loaded'))
        log.warning("projects.json: %s", rejected[-1].line())
        return Registry(projects, rejected)

    for index, entry in enumerate(entries[:MAX_PROJECTS]):
        label = fallback_label(entry, index)
        project, problem = _parse_entry(entry, index, file.parent)
        if project is not None:
            known_path = seen_paths.get(_path_key(project.path))
            if known_path:
                problem, project = (
                    f"path {project.path} is already {known_path!r}",
                    None,
                )
        if project is not None:
            key = remote_key(project.remote)
            known_remote = seen_remotes.get(key) if key else None
            if known_remote:
                problem, project = f"remote is already {known_remote!r}", None
        if project is not None and project.id in seen_ids:
            problem, project = f"id {project.id!r} is already taken", None
        if project is None:
            rejected.append(Rejected(label, problem))
            log.warning("projects.json: skipping %s (%s)", label, problem)
            continue
        projects.append(project)
        seen_ids.add(project.id)
        seen_paths[_path_key(project.path)] = project.id
        if remote_key(project.remote):
            seen_remotes[remote_key(project.remote)] = project.id

    if len(entries) > MAX_PROJECTS:
        rejected.append(
            Rejected(file.name, f"has {len(entries)} entries; only the first {MAX_PROJECTS} were read")
        )
        log.warning("projects.json: %s", rejected[-1].line())
    return Registry(projects, rejected)


def fallback_label(entry: object, index: int) -> str:
    """How a skipped entry is named in the log and in /project."""
    if isinstance(entry, dict):
        named = str(entry.get("id") or "").strip()
        if named:
            return named
    return f"entry {index + 1}"


def from_settings(settings: object) -> Registry:
    """The registry in settings, or one built from it for a caller without one."""
    existing = getattr(settings, "projects", None)
    if isinstance(existing, Registry):
        return existing
    return load(
        repo_dir=getattr(settings, "repo_dir", Path.cwd()),
        authorized_branch=getattr(settings, "authorized_branch", ""),
        project_name=getattr(settings, "project_name", ""),
    )


def label_for(settings: object, project_id: object = "") -> str:
    """The project's name for a card, falling back to its id."""
    registry = getattr(settings, "projects", None)
    if isinstance(registry, Registry):
        project = registry.get(project_id)
        if project is not None:
            return project.name
    return str(project_id or DEFAULT_PROJECT)


def remote_for(settings: object, project_id: object = "", *, dan: bool = False) -> str:
    """The remote a job's branch lives on: her own for /dan, else its project's.

    A settings object without a registry (or a project id it does not know)
    keeps the single-checkout behaviour: the target remote of the project
    context, which is what every card showed before projects existed.
    """
    ctx = getattr(settings, "project", None)
    if dan:
        return str(getattr(ctx, "self_remote", "") or "")
    registry = getattr(settings, "projects", None)
    if isinstance(registry, Registry):
        project = registry.get(project_id)
        if project is not None and project.remote:
            return project.remote
    return str(getattr(ctx, "target_remote", "") or "")


def mixing_refusal(
    project: Project, *, worktree: Path, root: Path | None, origin: str
) -> str | None:
    """Why this worktree may not build or push for this project, or None.

    A job is bound to one project when it is queued, so its worktree has to be
    that project's checkout and its origin has to be that project's remote. Two
    equally useful failure modes are refused the same way: a worktree of another
    checkout, and a worktree whose origin was repointed. Nothing here pushes; the
    caller refuses before Freebuff launches and again before a push.
    """
    if root is None:
        return (
            f"Could not read which repository {worktree} belongs to, so it is not "
            f"known to be project '{project.id}'. Nothing was launched or pushed."
        )
    if _path_key(Path(root)) != _path_key(project.path):
        return (
            f"The worktree {worktree} belongs to {root}, not to project "
            f"'{project.id}' ({project.path}). Nothing was launched or pushed."
        )
    expected, actual = remote_key(project.remote), remote_key(origin)
    if not actual:
        return (
            f"{project.path} has no origin remote, so {worktree} cannot be checked "
            f"against project '{project.id}'. Nothing was launched or pushed."
        )
    if actual != expected:
        return (
            f"The worktree {worktree} pushes to "
            f"{project_context.redact_remote(origin) or '(no origin remote)'}, not to "
            f"project '{project.id}' "
            f"({project_context.redact_remote(project.remote)}). "
            "Nothing was launched or pushed."
        )
    return None


# How long one read-only git probe of a worktree may take.
GIT_TIMEOUT_SECONDS = 30.0


def worktree_path(root: Path, project_id: str = "", job_id: str = "") -> Path:
    """Where a job's worktree goes: ``<root>/<job>`` for the default project.

    The default keeps the path every existing worktree and log already uses, so
    nothing on the Mac moves; any other project nests under its id, so two jobs
    with the same id can never collide.
    """
    if project_id in ("", DEFAULT_PROJECT):
        return Path(root) / job_id
    return Path(root) / project_id / job_id


async def _git(cwd: Path, *args: str, git_bin: str = "git") -> tuple[int, str]:
    """One read-only git command in ``cwd``: (exit code, stripped stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            git_bin,
            "-C",
            str(cwd),
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return 1, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:  # pragma: no cover - a hung git is not a checkout
        proc.kill()
        await proc.wait()
        return 1, ""
    return proc.returncode, out.decode("utf-8", "replace").strip()


async def worktree_facts(
    worktree: Path, *, git_bin: str = "git"
) -> tuple[Path | None, str]:
    """The checkout a worktree belongs to and its origin URL, or (None, "").

    ``--git-common-dir`` is the main repository's ``.git`` for a linked
    worktree, so a job can be tied back to the project it was queued for rather
    than to wherever the worktree was created.
    """
    worktree = Path(worktree)
    code, common = await _git(worktree, "rev-parse", "--git-common-dir", git_bin=git_bin)
    if code != 0 or not common:
        return None, ""
    git_dir = Path(common)
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    git_dir = git_dir.resolve()
    root = git_dir.parent if git_dir.name == ".git" else git_dir
    code, origin = await _git(worktree, "config", "--get", "remote.origin.url", git_bin=git_bin)
    return root, origin if code == 0 else ""


async def check_worktree(
    project: Project, worktree: Path, *, git_bin: str = "git"
) -> str | None:
    """None when this worktree may build and push for the project, else why not."""
    root, origin = await worktree_facts(worktree, git_bin=git_bin)
    return mixing_refusal(
        project, worktree=Path(worktree), root=root, origin=origin
    )


def job_worktree(root: Path, registry: Registry, job_id: str) -> Path | None:
    """The first existing worktree for a job: the default path, then projects."""
    candidates = [worktree_path(root, DEFAULT_PROJECT, job_id)]
    candidates += [
        worktree_path(root, project_id, job_id)
        for project_id in registry.ids()
        if project_id != DEFAULT_PROJECT
    ]
    for path in candidates:
        if path.exists():
            return path
    return None
