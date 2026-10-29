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
  rules.py            hard guardrails enforced in code
  freebuff_runner.py  branch + git worktree + tmux + diff collection + push
  scripts/            check.sh test.sh build.sh status.sh
  logs/               job logs (gitignored except .gitkeep)
  launchd/            com.hustler.macbuilder.plist (LaunchAgent)
  tests/              offline unit tests for the guardrails and parsing
  .env.example        configuration template (no secrets)
```

Only one job runs at a time. `/check`, `/test`, `/build` and `/task` are queued;
`/status` and `/logs` are read-only and answer immediately.

## Prerequisites

- macOS with Xcode 26.x installed and opened once as the bot user
- XcodeGen and tmux: `brew install xcodegen tmux`
- Python 3.11+
- The repository cloned at `~/Hustler` (or edit the plist), checked out on the
  authorized branch
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
git -C ~/Hustler fetch origin
git -C ~/Hustler switch freebuff/ce4b8907c250c30103d8993e
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
cd ~/Hustler
git switch freebuff/ce4b8907c250c30103d8993e
python3 -m venv mac-builder/.venv
mac-builder/.venv/bin/python -m pip install -r mac-builder/requirements.txt
cp mac-builder/.env.example mac-builder/.env
$EDITOR mac-builder/.env
mkdir -p mac-builder/logs
mac-builder/.venv/bin/python mac-builder/bot.py
```

`REPO_DIR` defaults to the checkout containing `mac-builder/`; leave it empty
for this layout. `WORKTREE_ROOT` defaults to `~/mac-builder-worktrees`, outside
the repository. Job worktrees stay there until you approve; rejected worktrees
are kept for inspection.

### Configuration

| Variable | Meaning |
|---|---|
| `DISCORD_TOKEN` | Bot token. Never commit it. |
| `ALLOWED_USER_IDS` | Comma/space-separated Discord user IDs; required. |
| `AUTHORIZED_BRANCH` | Base branch for tasks; never `main`/`master`. |
| `REPO_DIR` | Git checkout to build; default is this repository. |
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
| `MANAGER_API_BASE`, `MANAGER_API_KEY`, `MANAGER_MODEL` | Any OpenAI-compatible chat endpoint. |
| `MAX_DIFF_LINES` | Hard diff limit, default 400. |
| `MAX_REVISE_ROUNDS` | Automatic revision rounds, default 2. |
| `MAX_PENDING_JOBS` | Queue cap, default 10. |
| `CHECK/TEST/BUILD/STATUS_TIMEOUT_SECONDS` | Per-stage timeouts. |

## Run on boot (LaunchAgent)

A LaunchAgent, not a daemon: the Simulator needs a logged-in GUI session.

```bash
mkdir -p ~/Library/LaunchAgents
cp ~/Hustler/mac-builder/launchd/com.hustler.macbuilder.plist \
   ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hustler.macbuilder.plist
launchctl kickstart -k gui/$(id -u)/com.hustler.macbuilder
launchctl print gui/$(id -u)/com.hustler.macbuilder | head -n 40
tail -f ~/Hustler/mac-builder/logs/launchd.log
```

- The plist assumes the repo is at `~/Hustler`. Edit the `cd` path if not.
- `KeepAlive` is `true`: launchd restarts the bot if it exits.
- Bot stdout/stderr go to `mac-builder/logs/launchd.log`; job output goes to
  `mac-builder/logs/<job-id>.log`.
- `LimitLoadToSessionType` is `Aqua`, which is why auto-login matters.
- Stop with
  `launchctl bootout gui/$(id -u)/com.hustler.macbuilder`.
- To verify a reboot works: reboot, do not log in, and check that the bot is
  online (or `launchctl print` shows a PID). Auto-login must be enabled first.

## Slash commands

| Command | What it does |
|---|---|
| `/status` | Branch, git state, queue length, running job. Runs `status.sh` directly. |
| `/check` | `swift test` in HustlerCore. Queued. |
| `/test` | `xcodegen`, then `xcodebuild test` on one simulator, then shuts it down. Queued. |
| `/build` | `xcodegen`, then unsigned `xcodebuild build`. Queued. |
| `/logs [job-id]` | Tail of the latest or a specific job log. |
| `/task <text>` | Fresh `freebuff/<short-id>` branch + worktree, Freebuff in tmux, then review. |

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
`push` unless the current branch is `freebuff/*`, and refuses explicit
destinations outside `freebuff/*` plus `--force`, `--mirror`, `--all`, `--tags`
and `--delete`. This is defence in depth, **not a sandbox**: the macOS user
account is the real boundary, and an agent could call git by absolute path. The
bot itself also validates the branch before pushing, and never merges.

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
- `revise` sends concrete notes back to Freebuff, re-runs the checks and reviews
  again, up to `MAX_REVISE_ROUNDS` (2). After that the decision goes to you.
- `approve` and `ask_user` post an embed with **Approve** / **Reject** buttons.
  Approve commits the worktree, pushes only `freebuff/<id>` and reports the
  commit. Reject pushes nothing. There is no merge path anywhere in the bot.
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
| Any push outside `freebuff/*`, or to `main`/`master` | Refused in code. |
| Merge to main | No code path exists. |

Oversized and protected-path changes are reported in Discord with no Approve
button, because there is nothing safe to approve.

## Tests

The guardrails and parsing are testable without a Mac, Discord or a model:

```bash
cd mac-builder
python3 -m unittest discover -s tests -v
python3 -m py_compile bot.py queue.py jobs.py rules.py manager.py freebuff_runner.py
bash -n scripts/*.sh
```

`tests/test_rules.py` covers the deliberately bad diffs (oversized and
protected-path), branch validation and verdict forcing. `tests/test_jobs.py`
checks that the Discord token and manager key never reach a script environment.
`tests/test_manager.py` covers JSON extraction, proposal parsing and endpoint
configuration.

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
- **LaunchAgent paths**: the plist assumes `~/Hustler`; adjust if needed.

## Limitations worth knowing

- Pending Approve/Reject buttons live in bot memory. Restarting the bot (or
  `KeepAlive` restarting it) drops the buttons; the branch and worktree remain,
  so inspect or re-run the task. This is deliberately not persisted to disk.
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
