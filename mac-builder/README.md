# mac-builder — private Discord build bot

A single-user Discord bot for a headless Mac mini (M1, 8 GB). It queues
`xcodegen` / `xcodebuild` / `swift test` jobs, runs Freebuff as a supervised
builder on an isolated `freebuff/*` branch, and has an AI manager review the
diff and test output. Nothing is pushed until you press **Approve**, and the bot
never merges.

## Baseline findings (read before installing)

These were checked before any code was written:

- The `main` branch of this repository is the static web app (`index.html`,
  `sw.js`, icons). It has no Xcode project.
- The authorized branch `freebuff/ce4b8907c250c30103d8993e` adds `ios-app/`:
  `project.yml` (XcodeGen), the `Hustler` app target, and the dependency-free
  `HustlerCore` / `HustlerArchive` Swift package.
- The review environment was Linux, so `xcodegen`, `xcodebuild` and `swift` were
  not available. **The Xcode baseline was not executed here.** On the Mac,
  confirm this before trusting the bot:

  ```bash
  xcode-select -p
  xcodebuild -version
  brew install xcodegen tmux
  cd ios-app && xcodegen generate --spec project.yml
  swift test --package-path HustlerCore
  xcodebuild build -project Hustler.xcodeproj -scheme Hustler \
    -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO
  ```

- **The `Hustler` scheme has no test action.** `project.yml` declares only
  `build`, `run` and `archive`, and there is no test target in the Xcode
  project. `xcodebuild test -scheme Hustler` therefore fails with
  `Scheme Hustler is not currently configured for the test action`.
  `scripts/test.sh` detects that exact condition, shuts the simulator down and
  exits **78**, which the bot reports as “unavailable”. It does **not** force a
  revision, because that failure is a property of the repository, not of the
  diff under review. Real test failures still force `revise`.
  Adding an app test target is an app change and is out of scope for this task.
- `ios-app/README.md` states the app build is proven only by CI, and that the
  core suite is 131 tests.

## What runs where

```
mac-builder/
  bot.py              Discord client, allowlist, slash commands, buttons, #ai chat
  queue.py            single-worker asyncio FIFO queue
  jobs.py             fixed-script execution + the Freebuff review pipeline
  manager.py          OpenAI-compatible review and planning chat (no tools)
  project_context.py  which repo/branch/project she builds, plus herself
  rules.py            hard guardrails enforced in code
  freebuff_runner.py  branch + git worktree + tmux + diff collection + push
  scripts/            check.sh test.sh build.sh status.sh qcapture.sh
  logs/               job logs (gitignored except .gitkeep)
  launchd/            com.hustler.macbuilder.plist (LaunchAgent)
  tests/              offline unit tests for the guardrails and parsing
  .env.example        configuration template (no secrets)
```

Only one job runs at a time. `/check`, `/test`, `/build` and `/task` are queued;
`/status` and `/logs` are read-only and answer immediately. `/cancel` stops the
running job or a waiting one, and the queue carries on with the next job.

## Prerequisites

- macOS with Xcode 26.x installed and opened once as the bot user
- XcodeGen and tmux: `brew install xcodegen tmux`
- Python 3.11+
- The bot's own checkout at `~/Curator.1` (it holds `mac-builder/`; set
  `MACBUILDER_DIR` in the plist if it lives elsewhere)
- The project she builds at `~/Desktop/Hustler`, set as `REPO_DIR` in `.env`,
  checked out on the authorized branch
- A Discord application and bot token
- An OpenAI-compatible chat endpoint, key and model
- The Freebuff CLI installed as `freebuff` in `PATH`, or `FREEBUFF_BIN` set
- Automatic login enabled (the Simulator needs the logged-in Aqua session)
- FileVault **off** if you want login-free reboots; macOS disables auto-login
  when FileVault is on. Weigh that trade-off yourself.

One-time Mac preparation as the same user that will own the bot:

```bash
sudo xcodebuild -license accept
xcodebuild -runFirstLaunch
xcrun simctl list devices available      # there must be an available iPhone
git -C ~/Curator.1 fetch origin
git -C ~/Desktop/Hustler fetch origin
git -C ~/Desktop/Hustler switch freebuff/ce4b8907c250c30103d8993e
```

## Discord setup

1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → **Reset Token** → copy it into `.env` as `DISCORD_TOKEN`.
3. **Bot → Privileged Gateway Intents** → enable **Message Content Intent**
   (required for `#ai` chat).
4. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`;
   permissions View Channels, Send Messages, Read Message History, Use Slash
   Commands. Invite the bot to your private server.
5. Enable Discord **Developer Mode** (Settings → Advanced), then right-click
   your name → **Copy User ID**. Put it in `ALLOWED_USER_IDS`. Multiple IDs are
   comma-separated.
6. Create `#ai` and, optionally, `#builds`. Right-click → **Copy Channel ID**
   for `AI_CHANNEL_ID` / `NOTIFY_CHANNEL_ID`, or leave `AI_CHANNEL_ID` empty and
   let `AI_CHANNEL_NAME=ai` find the channel by name.
7. Set `GUILD_ID` to your server ID so slash commands appear instantly instead
   of waiting for a global sync.

Every command, button and AI-channel message re-checks the allowlist. If
`ALLOWED_USER_IDS` is empty the bot refuses to start. Discord input never
reaches a shell: commands only select fixed scripts, and `/task` text is pasted
into Freebuff’s terminal via `tmux load-buffer`/`paste-buffer`.

## Install and run manually

```bash
cd ~/Curator.1
/opt/homebrew/bin/python3.12 -m venv mac-builder/.venv
mac-builder/.venv/bin/python -m pip install -r mac-builder/requirements.txt
cp mac-builder/.env.example mac-builder/.env
$EDITOR mac-builder/.env
mkdir -p mac-builder/logs
mac-builder/.venv/bin/python mac-builder/bot.py
```

`REPO_DIR` defaults to the checkout containing `mac-builder/`. Since
`mac-builder/` was moved out of Hustler into the Curator.1 repository the two
are separate checkouts on the Mac, so set `REPO_DIR=~/Desktop/Hustler` (the
project she builds) in `.env`. Otherwise startup refuses the Curator.1 checkout,
in one line, for having no `ios-app/project.yml`. At startup she refuses a
target without `ios-app/project.yml` or without `AUTHORIZED_BRANCH` as a local
branch there.
`/project` shows which repo, branch and project she is on, and herself.
`WORKTREE_ROOT` defaults to `~/mac-builder-worktrees`, outside
the repository. Job worktrees stay there until you approve; rejected worktrees
are kept for inspection.

### Configuration

| Variable | Meaning |
|---|---|
| `DISCORD_TOKEN` | Bot token. Never commit it. |
| `ALLOWED_USER_IDS` | Comma/space-separated Discord user IDs; required. |
| `AUTHORIZED_BRANCH` | Base branch for tasks; never `main`/`master`. |
| `REPO_DIR` | The project checkout to build; default is the one containing `mac-builder/`. On the Mac that is `~/Desktop/Hustler`, because `mac-builder/` now lives in the Curator.1 checkout. |
| `PROJECT_NAME`, `BUILDER_NAME`, `BOT_NAME` | Labels for `/project` and the manager; default to the target folder name, `Freebuff`, `Melisa`. |
| `LOG_DIR` | Job logs; default `mac-builder/logs`. |
| `WORKTREE_ROOT` | Per-task worktrees; default `~/mac-builder-worktrees`. |
| `HUSTLERCORE_DIR` | Swift package path; default `ios-app/HustlerCore`. |
| `XCODE_PROJECT`, `XCODE_SCHEME` | `ios-app/Hustler.xcodeproj`, `Hustler`. |
| `SIMULATOR_NAME` | Preferred simulator; falls back to first available iPhone. |
| `SWIFT_JOBS`, `XCODEBUILD_JOBS` | Parallel build jobs; default `2` for 8 GB. |
| `FREEBUFF_BIN`, `FREEBUFF_ARGS` | Interactive Freebuff CLI and fixed arguments. |
| `FREEBUFF_READY_SECONDS` | Wait for first terminal output before sending the task. |
| `FREEBUFF_IDLE_SECONDS` | Worktree must stop changing this long to count as done. |
| `FREEBUFF_TIMEOUT_SECONDS` | Hard stop per Freebuff round. |
| `QUESTION_WAIT_SECONDS` | How long a Freebuff question box may hold a job waiting for `/answer`; after that it is dismissed with `Esc` and the job fails. Default 1800. |
| `QUESTION_AUTO_ANSWER` | `1` lets the manager answer a question, only when it replies with high confidence. Default `0`: the manager is rate limited and off by default. |
| `QUESTION_AUTO_ANSWER_MAX` | At most this many auto-answers per job; after that every question is yours. Default 3. |
| `QUESTION_MANAGER_COOLDOWN_SECONDS` | After a 429 or a timeout from the manager, skip manager calls for this long and ask you instead. Default 30. |
| `QUESTION_SUGGEST` | `1` posts the manager's advice as a follow-up under the question ("suggestion only, you decide"). Never an answer, never typed. Default `0`. |
| `QUESTION_DEBUG` | `1` writes the whole pane to the job log after every key sent to a question box. Default `0`; turn it on once to learn the real box. |
| `MANAGER_API_BASE`, `MANAGER_API_KEY`, `MANAGER_MODEL` | Any OpenAI-compatible chat endpoint. |
| `MANAGER_FALLBACK_MODELS` | Optional comma-separated fallback model ids. When set, requests also send `"models": [MANAGER_MODEL, ...]` so OpenRouter falls through on a 429 or provider error; empty leaves the payload unchanged. |
| `MAX_DIFF_LINES` | Hard diff limit, default 400. |
| `MAX_REVISE_ROUNDS` | Automatic revision rounds, default 2. |
| `MAX_PENDING_JOBS` | Queue cap, default 10. |
| `CHECK/TEST/BUILD/STATUS_TIMEOUT_SECONDS` | Per-stage timeouts. |

## Run on boot (LaunchAgent)

A LaunchAgent, not a daemon: the Simulator needs a logged-in GUI session.

```bash
mkdir -p ~/Library/LaunchAgents
cp ~/Curator.1/mac-builder/launchd/com.hustler.macbuilder.plist \
   ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hustler.macbuilder.plist
launchctl kickstart -k gui/$(id -u)/com.hustler.macbuilder
launchctl print gui/$(id -u)/com.hustler.macbuilder | head -n 40
tail -f ~/Curator.1/mac-builder/logs/launchd.log
```

- The plist holds one path: `MB` defaults to `$HOME/Curator.1/mac-builder`.
  Set `MACBUILDER_DIR` in its `EnvironmentVariables` (or edit that one line) if
  the checkout lives elsewhere. The `com.hustler.` label is only an identifier
  and was left alone so an existing install keeps working.
- `KeepAlive` is `true`: launchd restarts the bot if it exits.
- Bot stdout/stderr go to `mac-builder/logs/launchd.log`; job output goes to
  `mac-builder/logs/<job-id>.log`.
- The first startup line is
  `code: <folder> [<branch>@<commit>] · env: <folder>/.env · logs: <folder>`, so
  the folder and commit that actually ran are never a guess.
- `LimitLoadToSessionType` is `Aqua`, which is why auto-login matters.
- Stop with
  `launchctl bootout gui/$(id -u)/com.hustler.macbuilder`.
- To verify a reboot works: reboot, do not log in, and check that the bot is
  online (or `launchctl print` shows a PID). Auto-login must be enabled first.

## Slash commands

| Command | What it does |
|---|---|
| `/project` | Which repo, branch and project Melisa is working on, plus her own repo. |
| `/status` | Project, branch, git state, queue length, running job and how long it has been going, and whether question detection is on. Runs `status.sh` directly. |
| `/cancel [job-id]` | Stops the running job, or one still waiting. The branch is kept; nothing is pushed. |
| `/check` | `swift test` in HustlerCore. Queued. |
| `/test` | `xcodegen`, then `xcodebuild test` on one simulator, then shuts it down. Queued. |
| `/build` | `xcodegen`, then unsigned `xcodebuild build`. Queued. |
| `/logs [job-id]` | Tail of the latest or a specific job log. |
| `/plan <goal>` | Read-only plan as a markdown file. Creates no job and builds nothing. |
| `/task <text>` | Fresh `freebuff/<short-id>` branch + worktree, Freebuff in tmux, then review. |
| `/dan <text>` | Self-build: `dan/<job-id>` from a freshly fetched `origin/main` in her own checkout, Freebuff in tmux, Python checks instead of Xcode, then review. Approve pushes `dan/*` only. |
| `/answer <number or text>` | Answers the question Freebuff is waiting on: an option number picks that option, anything else is typed as free text. |

Job results are posted where the job was started, or to `NOTIFY_CHANNEL_ID`.

## Freebuff runner

1. `git worktree add -b freebuff/<8 hex> <worktree> <authorized-branch>`.
2. tmux session `macbuilder-<job tail>`, working directory = the worktree.
3. The task text is loaded through a tmux buffer and pasted, so quotes, `;`,
   `$` and newlines are never interpreted by a shell.
4. Pane output is captured with `tmux pipe-pane` into `logs/<job-id>.log`.
5. Completion is detected when the worktree stops changing for
   `FREEBUFF_IDLE_SECONDS`, the session exits, or the hard timeout hits. This is
   a heuristic: there is no documented machine-readable “done” signal for an
   interactive Freebuff CLI. The timeout kills the session.
6. The branch and worktree are staged, the diff is counted with
   `git diff --numstat`, and only an under-limit diff is loaded as text.
7. `check.sh` and `test.sh` run inside the worktree, so they test the candidate
   change, not the base branch.

A generated `git` shim is placed first on the session `PATH`. It refuses
`push` unless the current branch is the job's own prefix — `freebuff/*` for a
`/task`, `dan/*` for a `/dan` — and refuses explicit destinations outside that
prefix plus `--force`, `--mirror`, `--all`, `--tags` and `--delete`. This is
defence in depth, **not a sandbox**: the macOS user account is the real
boundary, and an agent could call git by absolute path. The bot itself also
validates the branch before pushing, and never merges.

### Questions from Freebuff (`/answer`)

Freebuff sometimes stops on a question box instead of finishing:

```
╭──── Some questions for you ────╮
│                        Close ✕ │
│ ▼ 1. Which tone should the project description use?     │
│     ○ Formal                                            │
│       Polished, professional prose suitable for docs    │
│     ○ Casual                                            │
│     ○ Custom                                            │
│ ▶ 2. What name should go in the title of the new file?  │
│     ↳ (click to answer)                                 │
│ │ Submit │   ↑↓ navigate • Enter select                 │
╰────────────────────────────────╯
```

1. While it waits, the pane is read every `FREEBUFF_POLL_SECONDS` with
   `capture-pane`. `questions.py` parses the box: the title **Some questions for
   you**, every numbered question with its wrapped lines rejoined, the `○`
   options with the description line under an option (which is not an option of
   its own), the `Custom` row, and whether a question is expanded (`▼`) or
   collapsed (`▶`). A question with no options and `(click to answer)` is free
   text. The ad banner above the box and `Close ✕` are ignored. No model and no
   shell are involved.
2. The box is driven **one question at a time**. Question `n` of `m` is posted in
   the channel the job was started in, with its options numbered. `/answer 2`
   picks option 2, `/answer <text>` types free text, and free text for a box with
   a `Custom` row selects that row first. Free text goes through
   `tmux load-buffer`/`paste-buffer`/`send-keys`, exactly like `/task` text, so
   Discord text never reaches a shell.
3. No key sequence is counted blindly. After every key the pane is captured
   again and the state is checked: which row is focused, which row is marked
   `◉` as chosen, which question is expanded, whether the box is still there. The
   next key is computed from what the pane actually says, and a selection is
   confirmed by the row that `Enter` marks. When the last question is answered,
   **Submit** is reached before `Enter` is sent: the focus is first walked onto
   Submit with `Down` (which can never select an option) and that move is
   confirmed on the pane. If the pane never shows Submit with the focus, no
   `Enter` is sent at all. After it is pressed the box must disappear; if it does
   not, the job fails rather than pretending it answered. Any state that is not
   the expected one sends `Esc`, ends the job as **failed** with the reason, and
   leaves the pane in the log. Cancelling the job, or shutting the bot down,
   dismisses the box and forgets the question, so a late `/answer` says nothing
   is pending.
4. While a question is open the idle timer is paused and the hard timeout is
   pushed out by however long the box stayed open, so a job that is waiting for
   you is never mistaken for a finished one and never killed. `/answer` always
   answers the question that is open. If it is still unanswered after
   `QUESTION_WAIT_SECONDS` (1800, 30 minutes), the box is dismissed with `Esc`
   and the job ends as **failed** with that reason.
5. If the title is on screen but no question could be parsed, nothing is guessed:
   the sanitized pane tail goes to the job log and to Discord once per box, and
   the job keeps waiting (never idle) until the box parses or
   `QUESTION_WAIT_SECONDS` expires. `QUESTION_DEBUG=1` writes the pane to the job
   log after every key, which is how the real key behaviour is learned. `/status`
   and a startup log line say whether this detection is enabled.
6. Freebuff's own survey (**quick question 1/11**, `esc not now`) is never a task
   question: it is dismissed with `Esc`, never posted and never forwarded.
7. `/answer` goes through the same tree-wide allowlist check as every other
   command, and there is no second path to it. A number that names no option is
   refused with the real option count. With nothing pending it says so and does
   nothing.
8. `QUESTION_AUTO_ANSWER=1` lets the manager answer instead of you, but only when
   it replies with `{"answer": "...", "confidence": "high", "reason": "..."}`
   and the answer names a real option or plain text. The reply must be exactly
   that one object: prose around it, a fence, a missing reason or an unknown
   confidence is a failed parse and goes to you. A 429, a timeout, anything
   below high confidence, or an answer that does not fit the box falls back to
   you. Off by default.
9. The escalation gate is **code, not prompt**. Before any auto-answer is
   accepted, the question text, every option label and description, and the
   chosen answer are checked against one list (`manager.ESCALATION_TERMS`):
   deleting, removing, renaming, moving or restructuring files, dependencies and
   packages, installing, upgrading, force, push, merge, and any protected path
   (`project.yml`, `*.xcconfig`, `.github/`, `.env*`, `mac-builder/`, signing or
   provisioning material), plus text that reads like a prompt injection. Any
   match sends the question to you whatever the confidence, and the question
   side of the gate runs before the manager is even called. The answer prompt
   also tells the manager to reply `low` when it is unsure.
10. The question and its options are **data, never instructions**. The answer
    prompt says so, and the manager may only pick one of the numbered options or
    answer the question as asked. Its context is the job's task text, the plan
    if the job has one, the question number and "n of m", the numbered options
    with descriptions, and a short tail of the running job's `git diff --stat` —
    every part capped by a `MAX_ANSWER_*` constant and redacted before it leaves
    the process.
11. `QUESTION_SUGGEST=1` adds advice without giving up control. The question is
    posted immediately, exactly as before; the manager's line follows as its own
    message, `"<BOT_NAME> suggests option N: <reason> (suggestion only, you
    decide)"`, and only if it arrives inside a short window. It never delays the
    question, never blocks `/answer`, is never returned to the runner and is
    never typed into the session. If the manager is unavailable or throttled,
    nothing is shown.
12. Every auto-answer is auditable: `"<BOT_NAME> answered option N (confidence
    high): <reason>"` is posted to the job's channel and written to the job log.
    At most `QUESTION_AUTO_ANSWER_MAX` (3) auto-answers are taken per job, and
    after a 429 or a timeout no manager call is made for
    `QUESTION_MANAGER_COOLDOWN_SECONDS` (30): those questions are yours.

### Live test on the Mac (four `/task` runs)

Each run is a fresh `/task`. Keep `QUESTION_DEBUG=1` so every key is in the job
log, and snapshot the real box with `scripts/qcapture.sh <label>`: it finds the
live `macbuilder-*` session on the `macbuilder` socket and writes the plain and
coloured pane to `~/Desktop/qbox/` as `NN-<label>.txt` and
`NN-<label>-color.txt` (it only reads; it types nothing). `/status` must show
the `questions: detection on · wait 1800s · ...` line — if it does not, the bot
is running another folder, and the startup `code:` line says which one.

1. **Two questions, tone and name, no manager.** `QUESTION_AUTO_ANSWER=0`,
   `QUESTION_SUGGEST=0`, `QUESTION_DEBUG=1`:
   `/task Add a SHORT project description to the README, and ask me which tone
   and which name to use before you write it.`
   Expected: two questions arrive in order, `1 of 2` then `2 of 2`, with
   numbered options and their descriptions; nothing is answered for you;
   `/answer 1` and `/answer <text>` both reach Submit and the job reviews.
2. **The same with advice on.** `QUESTION_SUGGEST=1`, auto-answer still `0`:
   `/task Add a SHORT project description to the README, and ask me which tone
   and which name to use before you write it.`
   Expected: each question is posted immediately, and a second message follows —
   `<BOT_NAME> suggests option N: <reason> (suggestion only, you decide)` — or
   no second message at all when the manager is slow or throttled. The advice
   is never typed, never answers the question, and `/answer` works while it is
   still being fetched.
3. **A harmless auto-answer.** `QUESTION_AUTO_ANSWER=1`:
   `/task Add a SHORT project description to the README; if you need a tone, ask
   me.`
   Expected: no question reaches you; `<BOT_NAME> answered option N (confidence
   high): <reason>` is posted and written to the job log, and the job carries on.
   `/status` reads `auto-answer on (max 3/job)`.
4. **An escalating question.** `QUESTION_AUTO_ANSWER=1`:
   `/task Look through docs/ for stale files, then ask me whether to delete
   docs/old.md.`
   Expected: the question comes to you, the job log has
   `[question N is the owner's call: it mentions 'delete']`, the manager is not
   called at all (no suggestion even with `QUESTION_SUGGEST=1`), and nothing is
   typed until you `/answer`.

### `/dan` (self-build)

`/dan` is the only path that may change `mac-builder/` itself:

1. `git fetch origin main` in her own checkout (`PROJECT` self repo, i.e. the one
   holding `mac-builder/`). A failed fetch fails the job; the branch is never
   created from a stale base.
2. `git worktree add -b dan/<job-id> <worktree> origin/main`, with a push guard
   that allows `dan/*` only.
3. Same tmux supervision, idle detection and review as `/task`, with the same
   `MAX_DIFF_LINES` and the same snapshot re-check on Approve.
4. `/check` and `/test` become Python stages run in the worktree with the bot's
   own interpreter (`sys.executable`, i.e. `mac-builder/.venv/bin/python` under
   the LaunchAgent): `.venv/bin/python -m unittest discover -s tests -t .`, then
   `py_compile` over every module and `bash -n scripts/*.sh`. Never the system
   `/usr/bin/python3`, which has none of the dependencies. Any failure forces
   `revise`, exactly as a failing `swift test` does for `/task`.
5. Approve pushes `dan/<job-id>` and nothing else. Reject pushes nothing.
6. Restarting the bot is still manual, also for a change that edits her.

What `/dan` may never change: `mac-builder/rules.py`,
`mac-builder/freebuff_runner.py` (the push path and guard), the allowlist and
Approve code in `mac-builder/bot.py` (checked line by line in the diff), and the
tests that pin them (`test_rules.py`, `test_push_guard.py`, `test_dan.py`,
`test_approve_recheck.py`). Each refusal names the file and the reason, so the
model can move the change somewhere it is allowed.

Freebuff runs in a tmux server on its own socket (`-L macbuilder`) whose
environment is a copy of the bot environment with `DISCORD_TOKEN` and every
`MANAGER_*` variable removed. Other variables, including `SSH_AUTH_SOCK` and
any Freebuff configuration, are kept so git push and the CLI still work. The
builder still runs as the same macOS user and can read files that user can
read; use a dedicated macOS account if that matters to you.

**Assumption:** `freebuff` is an interactive CLI that accepts a prompt typed
into its terminal. If your CLI differs, set `FREEBUFF_BIN` to a small wrapper
script that starts the interactive session; `FREEBUFF_ARGS` supplies fixed
arguments (never Discord input).

## AI manager

- Reviews only: original task, diff, `git status`, changed paths, `/check` and
  `/test` state and tails. It has no tools, cannot run commands and cannot edit
  files.
- Must answer with
  `{"verdict": "approve" | "revise" | "ask_user", "notes": "..."}`.
  Unparseable replies become `ask_user`; the raw reply is kept for inspection.
- `MANAGER_MAX_TOKENS` (default 4000) is the reply budget. Reasoning models
  such as Gemini flash spend tokens thinking, so a small budget comes back as an
  empty or truncated reply. A 5xx, a 429 or a timeout is retried once; a 401, a
  404 or a malformed body is not.
- Log tails in the review prompt are capped at 2500 characters each, so a
  runaway build log cannot push the diff out of the context budget.
- `revise` sends concrete notes back to Freebuff, re-runs the checks and reviews
  again, up to `MAX_REVISE_ROUNDS` (2). After that the decision goes to you.
- `approve` and `ask_user` post an embed with **Approve** / **Reject** buttons.
  Approve commits the worktree, pushes only `freebuff/<id>` and reports the
  commit. Reject pushes nothing. There is no merge path anywhere in the bot.
- `/plan <goal>` asks the manager for a plan in the owner's task-document shape:
  evidence, items in priority order, acceptance per item, out of scope, open
  decisions. It is given the project block, the tails and error lines of the
  newest job logs, a shallow file map of the target checkout and the two gap
  sections of this README, and every claim it writes must be labelled `verified`
  or `assumed`. The result is wrapped with the goal and a timestamp and saved to
  `LOG_DIR/plans/<timestamp>.md` (gitignored), then posted as a file. A plan
  request never creates a job, starts tmux or touches `mac-builder/`; it can only
  produce text. If the manager is not configured, `/plan` says which variables to
  set instead of posting an empty file.
  Job-log excerpts, the README gaps and the goal are credential-redacted before
  they are sent, so a token in a log never reaches the model or the saved file.
  The file name is the timestamp only (`plans/20261030-101500.md`), never the
  goal. A prompt that had to be clipped says so to the model, and a plan the
  model cut off at its token limit is labelled truncated at the top and the
  bottom instead of reading like a finished document.
- In `#ai`, the manager answers questions using the queue, the latest job log
  summary and a diff excerpt. It may end a reply with
  `{"proposed_tasks": [...]}`; each proposal gets a **Queue this task** button
  and runs only after you press it.

## Hard rules (`rules.py`)

These run before the manager and cannot be overridden by it:

| Rule | Result |
|---|---|
| Diff over `MAX_DIFF_LINES` (added + removed lines) | Blocked immediately; no model call. |
| `project.yml`, `*.xcconfig`, `.github/`, `.env*`, `mac-builder/`, signing/provisioning files | Blocked immediately; no model call. |
| Failing `/check` or a real `/test` failure | Verdict forced to `revise`. |
| `/test` reports “no test action” | Recorded as unavailable; does not fail the diff. |
| `/dan`: `rules.py`, `freebuff_runner.py`, the allowlist or Approve lines in `bot.py`, or the four tests that pin them | Blocked immediately; the detail says why. |
| `/dan`: `.env*`, `.github/`, `project.yml`, signing material, `main`/`master` | Blocked exactly as for `/task`. |
| Any push outside `freebuff/*` (task) or `dan/*` (dan), or to `main`/`master` | Refused in code. |
| Merge to main | No code path exists. |

Oversized and protected-path changes are reported in Discord with no Approve
button, because there is nothing safe to approve.

## Tests

The guardrails and parsing are testable without a Mac, Discord or a model:

```bash
cd mac-builder
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile bot.py queue.py jobs.py rules.py manager.py project_context.py freebuff_runner.py questions.py
bash -n scripts/*.sh
```

Discovery is automatic: every `tests/test_*.py` runs, so a new file needs no
registration. All three commands must exit 0 before a push. `discover -s tests -t .`
is the form that also works from the repository root.

`tests/test_rules.py` covers the deliberately bad diffs (oversized and
protected-path), branch validation and verdict forcing. `tests/test_jobs.py`
checks that the Discord token and manager key never reach a script environment.
`tests/test_bot_helpers.py` covers the `/logs` id lookup (including a path that
is not an id) and message splitting.
`tests/test_cancel.py` covers `/cancel`, cancelling a running or a waiting job,
elapsed formatting, and that a cancelled job still posts a result without
overwriting a review's `awaiting_review` or `blocked` status.
`tests/test_manager.py` covers JSON extraction, proposal parsing, endpoint
configuration and that a failed request names what failed. `tests/test_project_context.py`
builds throwaway git repos to check the project context, credential redaction and
every startup refusal, and that the project block reaches the review prompt.
`tests/test_bot_config.py` keeps `REPO_DIR` defaulted and covers the labels;
`tests/test_project_commands.py` calls `/project` and `/status` without Discord.
`tests/test_self_planning.py` calls `/plan` without Discord: the allowlist is
enforced, no job is created, the file is saved under `logs/plans/`, the prompt
carries the project block and the error lines from the newest log, and an
unconfigured manager names the variables to set.

`tests/test_questions.py` parses the two-question box (wrapped text, an option
description, the `Custom` row, the collapsed free-text question and the ad
banner) and the survey popup, renders a model of the box so a two-question run is
driven question by question through `Submit`, and checks that `Enter` is only
sent once Submit has the focus (and never when it cannot be shown), that a wrong
probe is corrected and verified, that a box which ignores its keys fails the
job, that an unreadable box is posted as a pane and never idles, that a token in
the pane reaches neither the log nor Discord, that free text only ever travels
through `load-buffer`/`paste-buffer` as an argument list, that one question has
one owner and one job runs at a time, that cancelling drops the pending
question, that `/answer` refuses a number outside the options, says so when
nothing is pending and tells a denied user nothing, and that the manager's
auto-answer is off by default and needs high confidence.

`tests/test_question_manager.py` covers the manager without a network: the
strict reply parser, the escalation list and its term in the log, the capped
context and its redaction, 429/timeout versus 500, the fixed order of an
auto-answer (gate, manager, answer gate, audit, then the answer), a failed audit
post still returning the answer, a cooldown shared by two jobs, the per-job cap,
and the advice line including a late one that is dropped. `tests/test_bot_config.py`
also checks that the LaunchAgent plist is valid, runs `bot.py` from one path and
no longer names the old Hustler folder, and that the startup line reports the
branch and short commit of the code that is running.

## What is not verified here

- **XcodeGen/Xcode builds and `swift test`**: this workspace was Linux with no
  Apple toolchain. Run the baseline commands above on the Mac.
- **A live Discord login, slash-command sync, buttons and `#ai` chat**: needs
  your token and server.
- **A real Freebuff CLI session**: needs the CLI installed and authenticated on
  the Mac. The idle-completion heuristic and `FREEBUFF_BIN` assumption are the
  main things to watch on the first `/task`.
- **A real model endpoint**: the manager was tested only against mocked JSON
  parsing, never a live provider.
- **A reboot**: auto-login and FileVault state are machine settings this
  repository cannot prove. Test it once on the mini.
- **LaunchAgent paths**: the plist defaults to `~/Curator.1/mac-builder`; set
  `MACBUILDER_DIR` if the checkout lives elsewhere.

## Limitations worth knowing

- Pending Approve/Reject buttons live in bot memory. Restarting the bot (or
  `KeepAlive` restarting it) drops the buttons; the branch and worktree remain,
  so inspect or re-run the task. This is deliberately not persisted to disk.
- A pending question is held in memory the same way: restarting the bot drops
  it, and Freebuff stays on its box until the session is stopped or
  `QUESTION_WAIT_SECONDS` expires. `/answer` answers the question of the running
  job only; there is no queue of old questions.
- Recognizing a question box is a text heuristic on `capture-pane`, like the idle
  detector. If Freebuff changes the box design the parse returns nothing, the
  pane is posted and the job waits instead of idling; it then expires with `Esc`
  after `QUESTION_WAIT_SECONDS`. The pane is read every
  `FREEBUFF_POLL_SECONDS`, so a question is noticed within one poll.
- The exact key behaviour of the box (which arrow moves the focus, what `Enter`
  marks, how **Submit** is reached after the last answer) is learned from the
  pane, not assumed: the loop probes, reads the result and corrects, and fails
  with the pane in the log when the box does not follow. `QUESTION_DEBUG=1` is
  the switch that records every step.
- Freebuff is supervised, not sandboxed: it runs as the bot's macOS user. The
  rules, the diff limits and the push shim constrain what it can do to the
  repository, not what it can read on the machine.
- The queue is in memory. A restart loses queued jobs; a running job is stopped
  on shutdown.
- Rejected worktrees are intentionally kept for inspection. Clean them with
  `git worktree list` and `git worktree remove <path>`.
- Approve pushes the branch with your existing git credentials (`gh auth`,
  SSH agent or a credential helper). The bot stores no token and never force
  pushes.
- Raw terminal escape codes are kept in the job log; Discord tails strip ANSI.
- `/status` runs a read-only git command outside the queue, so it answers even
  while a long build runs.
- `/cancel` keeps the branch that Freebuff worked on, which is the record of what
  it did, and removes only the worktree and the push guard. Nothing is pushed,
  and a timed-out session is stopped before its changes are counted.
- `/dan` may edit `mac-builder/`, which means it can change how she behaves next
  start; the guardrail files and the allowlist/Approve lines are refused in code,
  and nothing is pushed until you press Approve. A dan branch that changes her
  still needs a bot restart to take effect.
- `/plan` is bounded: at most `MAX_PLAN_CHARS` (60 000 characters) of material
  goes in, and at most `MANAGER_MAX_TOKENS` (default 4000) comes back. A plan that
  hits the output limit is labelled incomplete, not presented as whole.
- The manager is told `(no plan)` for every question: `Job.plan` exists and the
  answer prompt reads it, but nothing sets it yet — a `/plan` creates a document,
  not a job — so plan wiring has deliberately not been built.
- A slash-command sync that fails (403 / code 50001 `Missing Access`, usually a bot
  invited without the `applications.commands` scope, or a `GUILD_ID` it cannot see)
  is a warning at startup, not a crash: the bot keeps running with the commands from
  its last successful sync. A guild sync refused with 50001 is retried once globally,
  so the commands are still published somewhere; global commands can take up to an
  hour to appear, so re-invite the bot with that scope for an immediate guild sync.
- Startup logs one line naming `GUILD_ID` and whether the bot can see it (ids and
  names only, no member counts). It uses a REST listing rather than the guild cache,
  which is still empty in `setup_hook` before READY, and a failure in that diagnostic
  is logged and never stops startup.
