# Melisa roadmap

Melisa is the repo manager: she supervises the builder (Freebuff, "Dan" in the
docs), every repository the owner registers, and herself, and she carries the
conversation between the owner — on a phone, through Discord — and the build.
The owner is the editor and the tester. No laptop is needed for a normal
working day.

## The goal

**Curator is the home base for the building team. Melisa is the owner's
companion and the communication, planning and coordination layer between the
owner and the builders.**

The owner decides: they describe what they want, set priorities, approve
consequential changes and test the result in the real world. Dan builds. Melisa
turns the owner's words into clear, sized instructions, keeps the work
organized, tracks progress, coordinates more than one repository, and says what
needs the owner's attention — from a phone, with no laptop in the loop.

The end state this roadmap builds towards:

1. **The owner's words become planned work.** A note, a complaint, an idea or a
   goal becomes a reviewed plan, then sized jobs, then a build — with the plan
   shown before anything runs.
2. **One bot, many projects.** Hustler is the first project, not the definition
   of the system. Every job names the repository and project it belongs to, and
   no rule assumes there is only one checkout.
3. **Nothing consequential happens without the owner.** Nothing is pushed
   without a human Approve, nothing merges except through the gated `/merge`,
   and no command can be talked around the allowlist.
4. **Every claim is checkable.** Exact repository, branch, base/head commit,
   changed files, checks and outcomes; anything unverified is labelled so.
5. **Continuity survives the context window.** Decisions, blockers and the next
   action live in the repository and in GitHub, not in a chat log.
6. **It is reusable.** A second project, a second kind of app (iOS or Python),
   and a future owner can pick it up without rewriting the bot.

## Where it stands (2026-11-04)

| Phase | State | What is left |
|---|---|---|
| 1 — every screen Freebuff can show | shipped | — |
| 1.5 — the live status board | shipped | — |
| 2 — a registry of projects | shipped | switching without a restart |
| 3 — the editor's side of the conversation | mostly shipped | one digest per finished job |
| 4 — Melisa managing herself | mostly shipped | self-restart from Discord |

## Phase 1 — every screen Freebuff can show (shipped)

**Problem.** A job could sit silently: a login, a rate limit, an update prompt, a
confirmation or a crash screen waited out the whole hard timeout with no message
in Discord, and "done" could fire while Freebuff was still thinking.

**Built.** `screens.py` classifies captured pane text (question box, survey,
sign-in, rate limit, update prompt, confirm prompt, error or crash, ready,
working, unknown) with conservative patterns plus a generic fallback; a stall
detector in `freebuff_runner.py` posts a sanitized pane once per distinct screen
and pauses the clocks; readiness is a recognized ready prompt; `/screen`,
`/key <name>` and `/send <text>` cover the human side; credentials are redacted
before anything leaves the process; exit reasons are classified and named; and
`scripts/screencap.sh` turns a real pane into a fixture instead of a guess.

**Not in this phase, and still not.** No invented screens, no change to the
review or Approve path, no multi-repo assumptions.

## Phase 1.5 — the live status board (shipped)

**Problem.** The owner is on a phone and never at a laptop, but a job announced
itself in separate posts and the only way to know where it was was `/status`
plus a log tail. Freebuff reports no progress, so a percentage would be invented.

**Built.** `status_board.py` has the stage model, a seven-slot bar filled from
the stage number, real elapsed time, queue position, and a "looks like: <kind>"
line carrying the exact command for the screen `screens.py` reported; one status
hook from `jobs.py` and `freebuff_runner.py`; one Discord message per job edited
in place; pings only for waiting-on-you, review-ready, finished and stalled; a
pane-heartbeat digest; and `/status` and `/board`.

## Phase 2 — a registry of projects (shipped)

**Problem.** One `REPO_DIR`, one branch, one set of protected paths: every rule
assumed Hustler.

**Built.** `projects.py` holds a registry with `hustler` synthesized from the
existing settings plus optional `projects.json` entries (id, path, remote,
authorized branch, kind, checks, protected paths); a bad entry is skipped with
one line instead of refusing to start. Every job records the project it was made
for and cannot be re-routed; worktrees nest per project; the review, the push
guard and `/merge` read that project's protected paths; `/project` shows the
context and the registry, and `/project action:use project_id:<id>` saves the
project to use at the next start.

**Still open.** Switching without a restart. The runner, queue and push guard
are built from the target at startup, so a live swap is a restart. That is
deliberate: a mid-flight re-point is a guardrail bug, not a feature.

## Phase 3 — the editor's side of the conversation (mostly shipped)

**Problem.** The owner has feedback, not specifications, and it had to be
hand-written into a sheet.

**Built.** `/plansheet` drafts a sheet from a goal; `/feedback` does the same from
a note, verbatim, with the note framed as data rather than instructions;
`/audit` posts a fix sheet from failing branches or stages, with the same draft
card (Preview, Edit job, Remove job, Replace sheet, Approve and queue, Discard);
`/review` re-posts a review whose buttons died; `/roadmap` queues one job at a
time, waits for each merge, and pauses and resumes; `/status` is phone-first.

**Still open.** One digest per finished job saying what happened, what changed,
what is blocked and what the owner must decide — today that is a board plus a
result line.

## Phase 4 — Melisa managing herself (mostly shipped)

**Built.** A supervised update (`supervision.py`: wait for idle, verify, pull,
exit, launchd restarts) with the old and new commit announced; `update_watch.py`
looking for newer code; `/selftest` running every stage on fakes; the roadmap
restored after a restart; `/health` reporting uptime, queue, last job, manager,
roadmap, draft, disk and stray worktrees; and an interrupted job named at
startup from the marker the queue writes while it runs.

**Still open.** A self-restart initiated from Discord that refuses while a job
is running, and a cleanup command for old logs and leftover worktrees.

## Standing rules for every phase

- Discord input is never a command line: argv lists only, never a shell.
- The allowlist, the push guard, `rules.py`, `ReviewView`/`approve_and_push`
  and the Approve re-check stay off limits, including to `/dan`.
- Nothing is pushed without a human Approve, and never to `main`/`master`.
- Every claim in a report is verified or labelled unverified.
- Everything above is testable offline: no Discord, no tmux, no network, no
  real secrets.
