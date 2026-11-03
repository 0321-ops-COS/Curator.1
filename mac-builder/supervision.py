"""Supervised update: wait for idle, verify, pull, exit 0; never approves or merges."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENV = "MACBUILDER_SUPERVISED"
MANUAL = "launchctl kickstart -k gui/$(id -u)/com.hustler.macbuilder"
APPLIED_FILE = "update-applied.json"
SUITE_TIMEOUT = 1800.0


def is_supervised(environ: dict[str, str] | None = None) -> bool:
    return ((environ or os.environ).get(ENV) or "").strip() == "1"


def manual_restart() -> str:
    return f"I am not supervised: {ENV}=1 is not set, so launchd will not restart me. Restart by hand: `{MANUAL}`."


def _run(argv, cwd, timeout: float = 180.0) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout, done.stderr


class Supervisor:
    def __init__(self, root, log_dir, *, python: str | None = None,
                 run=_run, suite=None) -> None:
        self.root, self.log_dir = Path(root), Path(log_dir)
        self.python = python or sys.executable
        self._run, self._suite = run, suite

    def git(self, *args: str, timeout: float = 120.0) -> tuple[int, str, str]:
        return self._run(["git", "-C", str(self.root), *args], self.root, timeout)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")[1].strip()

    def dirty(self) -> int:
        return len([line for line in self.git("status", "--porcelain")[1].splitlines()
                    if line.strip()])

    def refusal(self, tip: str) -> str:
        """Why a pull would be unsafe, in plain words; '' when it is safe."""
        if self.dirty():
            return "the checkout has uncommitted changes; I never reset --hard"
        if self.git("merge-base", "--is-ancestor", "HEAD", tip)[0] != 0:
            return f"`{tip[:7]}` is not a fast-forward"
        return ""

    def subjects(self, old: str, new: str) -> str:
        lines = [line.strip() for line in self.git(
            "log", "--reverse", "--format=%s", f"{old}..{new}")[1].splitlines() if line.strip()]
        return "; ".join(lines[:6]) + (f"; and {len(lines) - 6} more" if len(lines) > 6 else "")

    def verify(self, tip: str) -> tuple[bool, str]:
        """The suite on the new commit in a detached worktree; the checkout stays put."""
        if self._suite is not None:
            return self._suite(tip)
        worktree = Path(tempfile.mkdtemp(prefix="macbuilder-update-"))
        code, _, err = self.git("worktree", "add", "--detach", str(worktree), tip)
        if code != 0:
            return False, f"could not create the update worktree: {err.strip()[:300]}"
        try:
            run = self._run([self.python, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                            worktree / "mac-builder", SUITE_TIMEOUT)
            return run[0] == 0, (run[1] + "\n" + run[2]).strip()[-1400:]
        finally:
            self.git("worktree", "remove", "--force", str(worktree))

    def pull(self) -> tuple[bool, str]:
        code, out, err = self.git("pull", "--ff-only")
        return code == 0, (err or out).strip()[:300]

    def note_applied(self, old: str, new: str, subjects: str, channel_id: int) -> None:
        try:
            (self.log_dir / APPLIED_FILE).write_text(json.dumps(
                {"old": old, "new": new, "subjects": subjects, "channel_id": channel_id}))
        except OSError:
            pass

    def take_applied(self) -> dict | None:
        """The update that just happened, once; the marker is deleted as it is read."""
        try:
            data = json.loads((self.log_dir / APPLIED_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        (self.log_dir / APPLIED_FILE).unlink(missing_ok=True)
        return data if isinstance(data, dict) and data.get("old") and data.get("new") else None
