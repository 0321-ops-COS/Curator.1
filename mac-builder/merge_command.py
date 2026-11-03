"""The gated /merge: a job's recorded facts, the gates, and one merge call.

Facts come only from what the job recorded and GitHub returned; ``merge_gates``
decides. The merge endpoint is called only when every gate passed and the run is
not a dry run. A dry run changes nothing and returns the head, so a moved head is
refused.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import compare_link
import github_client
import merge_gates
import projects
from merge_gates import MergeFacts


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def repo_slug_for(settings: object, kind: str, project: str = "") -> str:
    """``OWNER/REPO`` behind a job: her own checkout for /dan, its project else."""
    remote = projects.remote_for(settings, project, dan=_text(kind) == "dan")
    return compare_link.owner_repo(remote) or ""


@dataclass(frozen=True)
class MergeOutcome:
    text: str
    merged: bool = False
    dry_run_sha: str = ""


async def run_merge(job: object, settings: object, *, dry_run: bool = True, client=None
                    ) -> MergeOutcome:
    """Check every gate, then merge or not. A dry run never touches the PR."""
    branch = _text(getattr(job, "branch", ""))
    base = str(getattr(job, "base", "") or "").strip()
    base = base[7:] if base.startswith("origin/") else base
    label = f"`{_text(getattr(job, 'id', ''))}` {branch or '(no branch)'} → {base or '(no base)'}"
    owner, _, name = _text(getattr(job, "repo_slug", "")).partition("/")
    if not (owner and name):
        return MergeOutcome(f"{label}\nNo GitHub origin remote for this checkout, so there "
                            "is nothing to gate. Nothing was merged.")
    client = client or github_client.GitHubClient.from_env(owner, name)
    pr, number, mergeable, conflicts, head, ci = None, 0, None, False, "", ()
    if branch and base:
        pr = await asyncio.to_thread(client.find_pr, branch, base)
    if pr is not None:
        number = int(pr.get("number") or 0)
        detail = await asyncio.to_thread(client.get_pr, number)
        mergeable, conflicts = detail.get("mergeable"), detail.get("mergeable_state") == "dirty"
        head = _text(detail.get("head_sha"))
        ci = await asyncio.to_thread(client.ci_states, head) if head else ()
    project_id, project_protected = "", ()
    registry = getattr(settings, "projects", None)
    if isinstance(registry, projects.Registry):
        entry = registry.get(_text(getattr(job, "project", "")))
        if entry is not None and not entry.is_default:  # Hustler: as before
            project_id, project_protected = entry.id, tuple(entry.protected_paths)
    results = merge_gates.evaluate_gates(MergeFacts(
        job_status=_text(getattr(job, "status", "")), branch=branch, base=base, repo=name,
        authorized_branch=_text(getattr(settings, "authorized_branch", "")),
        check_state=_text(getattr(job, "check_state", "")),
        diff_lines=max(0, int(getattr(job, "diff_lines", 0) or 0)),
        changed_paths=tuple(str(path) for path in getattr(job, "changed_paths", ()) or ()),
        pr_exists=pr is not None, pr_mergeable=mergeable, pr_conflicts=conflicts,
        ci_states=tuple(ci), head_sha=head, approved_sha=None,
        dry_run_sha=_text(getattr(job, "merge_dry_run_sha", "")),
        project_id=project_id, project_protected=project_protected,
    ), max_diff_lines=int(getattr(settings, "max_diff_lines", 400) or 400))
    body = merge_gates.format_gates(results)
    if dry_run:
        return MergeOutcome(f"{label}\n{body}\ndry run, nothing merged.", dry_run_sha=head)
    if not merge_gates.all_passed(results):
        failed = ", ".join(result.name for result in results if not result.passed)
        return MergeOutcome(f"{label}\n{body}\nNothing was merged; failed: {failed}.")
    try:
        await asyncio.to_thread(client.merge_pr, number, head)
    except github_client.GitHubError as exc:
        return MergeOutcome(f"{label}\n{body}\nThe merge was not confirmed: {exc}")
    hint = github_client.revert_hint(owner, name, number)
    return MergeOutcome(f"{label}\n{body}\nMerged PR #{number} at `{head[:7]}`. {hint}",
                        merged=True)
