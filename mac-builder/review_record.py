"""The review a card showed, saved so the card can be rebuilt after a restart.

A ``ReviewOutcome`` lives only in memory, so its buttons die with the process
even though the branch, the worktree and the review are still there. Every fact
the card shows is written beside the job's log when the review is posted, and
``/review`` rebuilds the card from it. The worktree stays the truth: a refreshed
card compares it with the saved snapshot and refuses anything else.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import jobs
import projects
from freebuff_runner import TaskEnvironment

log = logging.getLogger(__name__)

AWAITING = "awaiting_review"
DECIDED = ("approved", "rejected")
JOB_FIELDS = ("id", "kind", "title", "channel_id", "requester_id", "text",
              "branch", "project", "repo_slug")
REVIEW_FIELDS = ("verdict", "notes", "check_state", "test_state", "diff_lines",
                 "changed_files", "check_ok", "test_ok", "snapshot")


def record_path(settings: object, job_id: str) -> Path:
    """One small file per review, next to the job's log."""
    return Path(getattr(settings, "log_dir")) / f"{job_id}.review.json"


def save(settings: object, outcome: jobs.ReviewOutcome) -> Path | None:
    """Write what the card says, before it is posted, so /review can rebuild it."""
    env, job = outcome.environment, outcome.job
    if env is None or not getattr(settings, "log_dir", None):
        return None  # nothing a card could be rebuilt from, or nowhere to write
    payload = {
        "status": AWAITING,
        "saved": time.time(),
        "job": {**{name: getattr(job, name) for name in JOB_FIELDS},
                "log": str(job.log_path)},
        "env": {"branch": getattr(env, "branch", ""),
                "worktree": str(getattr(env, "worktree", "")),
                "base_branch": getattr(env, "base_branch", ""),
                "kind": getattr(env, "kind", "task"),
                "base_commit": getattr(env, "base_commit", "")},
        "review": {name: list(getattr(outcome, name))
                   if name == "changed_files" else getattr(outcome, name)
                   for name in REVIEW_FIELDS},
    }
    path = record_path(settings, job.id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), "utf-8")
    except OSError:  # a record that cannot be written must not fail a review
        log.exception("could not save the review record for job %s", job.id)
        return None
    return path


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), dict) for key in ("job", "env", "review")
    ):
        return None
    data.setdefault("status", AWAITING)
    return data


def load(settings: object) -> list[dict]:
    """Every saved review still waiting on a decision, oldest file first."""
    try:
        paths = sorted(Path(getattr(settings, "log_dir")).glob("*.review.json"))
    except OSError:  # pragma: no cover - an unreadable log dir is not a review
        return []
    records = [record for record in map(_read, paths)
               if record is not None and record["status"] == AWAITING]
    return records


def load_one(settings: object, job_id: str) -> dict | None:
    """The saved record for one job, decided or not."""
    return _read(record_path(settings, job_id))


def mark(settings: object, job_id: str, status: str) -> None:
    """Record the decision, so a dead card cannot act and /review stops listing it."""
    if status not in DECIDED:
        raise ValueError(f"not a decided review status: {status}")
    record = _read(record_path(settings, job_id))
    if record is None:
        return
    record["status"] = status
    try:
        record_path(settings, job_id).write_text(json.dumps(record, indent=2), "utf-8")
    except OSError:
        log.exception("could not update the review record for job %s", job_id)


def outcome(record: dict) -> jobs.ReviewOutcome:
    """The outcome the card was built from, rebuilt from a saved record."""
    data, env_data, review = record["job"], record["env"], record["review"]
    job = jobs.Job(
        log_path=Path(str(data.get("log", ""))),
        channel_id=int(data.get("channel_id") or 0),
        requester_id=int(data.get("requester_id") or 0),
        # A record written before projects existed has no project, and belonged
        # to the one checkout there was.
        project=str(data.get("project") or projects.DEFAULT_PROJECT),
        **{name: str(data.get(name, "")) for name in
           ("id", "kind", "title", "text", "branch", "repo_slug")},
    )
    job.status = str(record.get("status", AWAITING))
    worktree = Path(str(env_data.get("worktree", "")))
    # The session and push guard are named after the job, so both are rebuilt
    # rather than guessed: Approve stops the same session and removes the same
    # guard it would have removed before the restart.
    env = TaskEnvironment(
        branch=str(env_data.get("branch", "")), worktree=worktree,
        session=f"macbuilder-{job.id[-8:]}",
        base_branch=str(env_data.get("base_branch", "")),
        guard_dir=worktree.parent / f".guard-{job.id}" / "bin",
        kind=str(env_data.get("kind", "task")),
        base_commit=str(env_data.get("base_commit", "")),
    )
    fields = {name: review.get(name) for name in REVIEW_FIELDS}
    fields["changed_files"] = tuple(str(name) for name in fields["changed_files"] or ())
    fields["diff_lines"] = int(fields["diff_lines"] or 0)
    fields["check_ok"] = bool(fields["check_ok"])
    return jobs.ReviewOutcome(job=job, environment=env, **fields)
