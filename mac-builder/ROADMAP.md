# Melisa roadmap

Melisa is the repo manager: she supervises the builder (Freebuff, "Dan" in the
docs), every repository the owner registers, and herself, and she carries the
conversation between the owner — on a phone, through Discord — and the build.
The owner is the editor and the tester. No laptop is needed for a normal
working day.

Hustler (the web app, then the native iOS app) is the first project. More
repositories follow, so nothing below may assume there is exactly one.

Each phase is separately shippable and separately tested. Phase 1 and Phase 1.5
are built; Phase 1.5 is what is current.

## Phase 1 — every screen Freebuff can show (done)

**Problem.** Today a job can sit silently: only the "Some questions for you"
box and the "quick question n/11" survey are recognized, readiness is "any
output at all" (so a sign-in screen is treated as ready and gets the task text
pasted into it), and the idle timer only starts once the worktree changes. A
login, a rate limit, an update prompt, a confirmation or a crash screen
therefore waits out the whole hard timeout with no message in Discord. A
session exit is noticed but never explained, and "done" can fire while Freebuff
is still thinking.

**Build.**
1. `screens.py`: a pure classifier over captured pane text — `question_box`,
   `survey`, `sign_in`, `rate_limit`, `update_prompt`, `confirm_prompt`,
   `error_or_crash`, `ready_prompt`, `working`, `unknown` — built from
   conservative patterns plus a generic fallback, because the real Freebuff
   binary is closed source and its screens cannot be enumerated in advance.
2. A stall detector in `freebuff_runner.py`: pane text and worktree both
   unchanged for `STALL_SECONDS` while the job is not done ⇒ post the
   sanitized pane to Discord once per distinct screen, pause the idle and
   hard-timeout clocks, and cap the wait at `QUESTION_WAIT_SECONDS` before Esc
   and an "expired" report. Unknown screens use the same path, so nothing is
   ever silent.
3. Readiness is a recognized ready prompt, or output that is not one of the
   screens that must be handled by a person first. A sign-in screen is posted
   instead of pasted into; after the owner finishes, the task is sent.
4. `/screen` (read-only pane), `/key <name>` (fixed allowlist only), `/send
   <text>` (the same paste path as `/task`, with a length cap). Allowlist
   checked exactly like every other command.
5. Redaction before anything leaves the process: long token-like strings,
   `Bearer …` values and API-key shapes are masked; short human sign-in codes
   are kept, because the owner needs them.
6. Exit reasons: the last screen is classified and named in Discord instead of
   a bare "exited".
7. `scripts/screencap.sh` saves a real pane locally, so screens that are still
   only known from a photograph become fixtures instead of guesses.

**Not in this phase.** No new screens invented, no change to the review or
Approve path, no change to the allowlist, and no multi-repo assumptions.

## Phase 1.5 — the live status board (current)

**Problem.** The owner is on a phone and never at a laptop, but a job announces
itself in separate posts and the only way to know where it is was `/status`
plus a log tail. Freebuff reports no progress, so anything that looks like a
percentage would be invented.

**Build.**
1. `status_board.py`: the stage model (`queued`, `branch_ready`,
   `freebuff_working`, `waiting_on_you`, `checks`, `tests`, `manager_review`,
   `awaiting_approve`, `pushed`, `rejected`, `failed`, `cancelled`,
   `timed_out`), a seven-slot bar whose fill is the stage number, real elapsed
   time, the queue position, and a "looks like: <kind>" line carrying the exact
   command for the screen `screens.py` reported. Pure functions with an injected
   clock.
2. One status hook (`stage, detail`) from `jobs.py` and `freebuff_runner.py`,
   called where Phase 1 already sees branches, questions, stalls, sign-in gates,
   stage runs, reviews and exits. A status failure never fails or delays a job.
3. One Discord message per job, posted in `NOTIFY_CHANNEL_ID` (else the job's
   channel) and edited in place, throttled by `STATUS_EDIT_MIN_SECONDS`; a
   deleted message costs one new post. Queue positions refresh on a timer.
4. Pings, as separate messages, only for: waiting on you, review ready, and a
   job finished, failed or timed out; plus one ping per stall after
   `STATUS_STALL_PING_SECONDS`. Deduplicated by job and screen kind, never by
   pane text. `STATUS_PINGS=0` turns them off.
5. `/status` renders the same board for the running job plus one line per
   waiting job; `/board` re-posts a lost board. Both allowlist-checked.

**Not in this phase.** No web server, no network endpoint and no iPhone widget:
that needs its own approval, and the board is deliberately a message the owner
already reads.

## Phase 2 — a registry of projects

- A projects registry (a small JSON file next to the bot, gitignored if it holds
  paths) replacing the single `REPO_DIR`: id, path, remote, authorized branch,
  push rules, builder binary, and per-project protected paths.
- `/project list` and `/project use <id>`; every job records the project it
  belongs to, and `/status` names it.
- The same guardrails per project, evaluated from that project's entry: never
  `main`/`master`, only the job's own `freebuff/*` branch, protected paths and
  diff caps unchanged.
- `/dan` still edits only her own folder, in her own checkout.
- Tests: a second project cannot borrow the first one's branch, branch prefix
  or protected paths; a project with a missing checkout fails in one line.

## Phase 3 — the editor's side of the conversation

- `/feedback <plain language>`: notes from the owner become a plan and then a
  task, through the manager, with the plan posted before anything runs.
- Job-finished and needs-you digests: one message per event that says what
  happened, what changed, what is blocked and what the owner must decide.
- Phone-first status: `/status` as a short, scannable summary, and no wall of
  log text unless asked.
- Tests: a digest is produced from a fake job set with no network, and a
  feedback note never becomes a shell argument.

## Phase 4 — Melisa managing herself

- A self-update flow: `/dan` result approved, then a controlled restart that
  reports the old and new commit, with the queue drained first.
- A health report: process age, queue depth, last job, manager reachability,
  disk space for logs and worktrees, and stray worktrees/sessions.
- Crash recovery: an unfinished job is reported at startup, with what it left
  behind, instead of disappearing.
- Tests: a restart refuses while a job is running; a health report is rendered
  from fake state; recovery names the interrupted job.

## Standing rules for every phase

- Discord input is never a command line: argv lists only, never a shell.
- The allowlist, the push guard, `rules.py`, `ReviewView`/`approve_and_push`
  and the Approve re-check stay off limits, including to `/dan`.
- Nothing is pushed without a human Approve, and never to `main`/`master`.
- Everything above is testable offline: no Discord, no tmux, no network, no
  real secrets.
