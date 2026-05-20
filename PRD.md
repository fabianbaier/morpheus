# Morpheus — Product Requirements Document

| Field | Value |
|---|---|
| **Status** | v0.6.0 (config + worktree + token guard + ledger + ask + spawn-from-trigger + MCP server — full roadmap shipped 2026-05-19) |
| **Author** | Fabian Baier |
| **Last updated** | 2026-05-19 |
| **Target platform** | macOS + iTerm2 |
| **Repo** | `~/github/fabianbaier/morpheus` |

> *"You take the blue pill — the story ends, you wake up in your bed and believe
> whatever you want to believe. You take the red pill — you stay in Wonderland,
> and I show you how deep the rabbit hole goes."*

---

## 1. Executive Summary

Morpheus is **mission control for your iTerm tabs**. It is the supervisor process
that turns ~20 open codex / claude / shell sessions into a coherent operational
picture — by augmenting the surfaces you already have (the tab bar, native
notifications, shell prompts) rather than building a new triage UI you'd have to
remember to check.

**Core thesis**: the tab bar is already a dashboard. It's just dumb. Tab titles
don't reflect state. There's no triage at a glance. Morpheus makes the tab bar
smart, and nothing else changes about how you work.

---

## 2. Problem Statement

### 2.1 The pain

The user runs ~20 tabs in iTerm. Each tab is one task: a `codex -yolo`, a `claude`,
a build, an SSH session, a `gh` poll. Over hours and days:

- Sessions **alert** (typically with a yes/no prompt) and the user has no recall
  of what that session was even *doing*. Context-switching cost is enormous.
- **Spawning** a new tab is friction — you'd rather hand off to an existing one,
  which dilutes its purpose.
- **Stale tabs accumulate**. By day three the tab bar is archaeology.
- **Multi-agent collisions** on shared worktrees corrupt commits. (See
  `workflow_use_worktrees.md` memory: "unrelated files snuck into commits.")
- **Token blowups** silently kill sessions. Two unanalyzable sessions in one
  month per Claude Insights.
- **PR reviews pile up** because the user has no top-of-mind queue of what
  needs his eyes.

### 2.2 User research

From Claude Code Insights (1,382 messages across 149 sessions, 2026-04-14 →
2026-05-19):

- 49% of messages happen during overlapping ("multi-clauding") sessions
- Heavy adversarial-review workflow: Claude + Codex in request-changes →
  verify → approve rounds, up to 16 PRs in parallel
- Primary friction surfaces: "wrong approach" (24), "buggy code" (20),
  "misunderstood request" (9)
- Two sessions exceeded output token limits and became unanalyzable
- Recurring corrections: stale context, wrong worktree, branch targeting,
  over-scoped solutions

### 2.3 Why prior workarounds fail

- **More tabs** → more archaeology
- **Tmux** alone → same problem, different multiplexer
- **An inbox / triage queue** → just another tab to forget. (Explicitly
  rejected — see `feedback_morpheus_no_inbox.md`.)
- **Slack / Notion task list** → context-switching out of the terminal

---

## 3. Goals & Non-Goals

### Goals (v0)

- G1: At a glance, the user can tell which tab needs his attention.
- G2: Spawning a new session is one keystroke from anywhere.
- G3: Stale sessions are visible and prunable in seconds.
- G4: When a long-running session is about to blow its token budget, the user
  can snapshot it to markdown and resume in a fresh session.
- G5: Every agent session can see what every other session is doing, in real
  time, without leaving the terminal.

### Goals (v0.1+)

- G6: Morpheus runs unattended via launchd; the user doesn't have to remember
  to start it.
- G7: A morning brief assembles overnight changes (GH activity, calendar,
  stale sessions, decisions needed) into a single readable digest.
- G8: New PR review requests spawn pre-loaded draft sessions automatically.
- G9: Web-search topic watchers push curated info on the user's schedule.

### Non-Goals (forever)

- N1: Replace tmux / iTerm. Morpheus is a layer on top, not a replacement.
- N2: An inbox or triage UI separate from the tab bar. (See feedback memory.)
- N3: Auto-merge / auto-push / auto-approve PRs. Soft-autonomy ladder caps
  destructive actions at "ask first" forever.
- N4: Build new tooling for things the user already has: `loop`, `schedule`,
  `scheduled-tasks` MCP, `gh`, `codex`, `claude`. Compose; do not rebuild.
- N5: Cross-platform on day one. macOS + iTerm2 only. Linux/tmux is v1.0.

---

## 4. Design Principles

1. **The tab bar IS the dashboard.** Augment it; don't build alongside it.
2. **No new triage surfaces.** Inbox, queue, sidebar — all rejected. If
   information needs to find the user, it goes through surfaces he already
   uses (tab titles, native notifications, shell prompt).
3. **Mission cards are the kernel.** Every session has a goal, a state, a
   last event, a next-step. The card is what survives across context switches.
4. **Silent by default, loud when it matters.** Notifications fire only on
   true emergencies (blocked > 30s on critical-tagged sessions, prod alerts).
5. **Soft-autonomy ladder.** Per-action class authorization:
   - Always allowed: polling, summarizing, web search, draft session creation
   - Ask first: spawning a live session that runs commands, killing a session,
     deleting files
   - Never: merging, pushing, approving PRs, sending external messages
6. **Compose existing primitives.** `loop`, `schedule`, `scheduled-tasks` MCP,
   `codex exec`, `claude -p`, `gh` — Morpheus orchestrates these, doesn't
   replace them.
7. **State is durable.** SQLite + JSON files in `~/.morpheus/` survive iTerm
   restarts and reboots.
8. **Cross-session awareness is a first-class feature.** Agents can see what
   other agents are doing, in real time, via a shared context file.

---

## 5. Personas

### Fabian — Solo founder, fleet operator

- Runs 5–10 active terminal tabs at a time, each pointed at one task
- 49% of messages happen during overlapping sessions
- Workflow signature: adversarial PR review (Claude + Codex in rounds), up
  to 16 stacked PRs in parallel
- Visual preference: Matrix-themed (black/green), retro hacker aesthetic
- Communication style: silent corrective nudges, not upfront specs
- Pain hierarchy: intent loss > stale tabs > token blowups >
  multi-agent collisions > forgotten PR reviews
- Will reject any tool that demands he learn a new mental model

---

## 6. Product Surface (v0)

### 6.1 The tab bar

Every iTerm tab gets a smart title, refreshed every ~5 seconds:

| Prefix | Meaning |
|---|---|
| 🟢 | Actively emitting output |
| 🟡 | Idle (process alive, no recent output) |
| 🔴 BLOCKED: | Waiting for user input (known prompt pattern matched) |
| ⚫ | Finished (no activity for > 30 min) |
| 💀 | Crashed (matched a crash pattern) |
| `36h •` prefix | Stale (idle/finished and aged past threshold) |
| `▶ MORPHEUS` | The Morpheus tab itself (self-excluded from monitoring) |

### 6.2 CLI commands

| Command | Purpose |
|---|---|
| `morpheus` | Launch the dashboard in the current tab |
| `morpheus watch` | Run the tick loop in the foreground (titles only, no dashboard) |
| `morpheus spawn "<goal>" "<cmd>"` | Open a new iTerm tab, run the command, register mission |
| `morpheus list` | Print every registered mission with state, age, last event |
| `morpheus prune [--older-than 4h]` | Interactively close stale tabs |
| `morpheus snapshot <tab_prefix>` | Dump a tab's mission + buffer to markdown |
| `morpheus context [--format md/json/short]` | Print the shared cross-session snapshot |
| `morpheus note "<text>"` | Post a cross-session note attached to the current tab |
| `morpheus notes [--limit 15]` | List recent cross-session notes |
| `morpheus doctor` | Diagnose iTerm2 + Python API connectivity |
| `morpheus version` | Print version |

### 6.3 The dashboard tab

A single dedicated tab running `morpheus` (or `morpheus dashboard`):

- Banner: MORPHEUS ASCII title in green
- Summary line: total sessions, counts by state
- Table: every mission sorted with **blocked first**, then crashed, working,
  idle, finished. Columns: ID, state emoji, goal, age, last event, live?
- Self-marked with `▶ MORPHEUS` prefix so the watcher skips this tab
- Drives the same tick loop as `watch` — running the dashboard also keeps
  every other tab's title updated

### 6.4 Cross-session context

Two files maintained by the tick loop:

- `~/.morpheus/context.md` — human-readable markdown snapshot
- `~/.morpheus/context.json` — parseable JSON snapshot

Agents inside other tabs can read these to know what every other session is
doing. They can post notes back via `morpheus note "text"` and those notes
appear in everyone's next context refresh.

The markdown includes a `**[YOU]**` marker so a session can tell which row
is itself vs others, and a usage block explaining how to interact with the
shared state.

---

## 7. Architecture

### 7.1 Process model (v0)

```
                              ┌─────────────────────┐
                              │  morpheus dashboard │ ← one iTerm tab
                              │  (or `morpheus watch`)│
                              └──────────┬──────────┘
                                         │
                       iTerm2 Python API │  every ~5s:
                                         ▼
                  ┌──────────────────────────────────────┐
                  │  TICK LOOP                           │
                  │  1. enumerate_tabs()                 │
                  │  2. for each tab: detect.detect()    │
                  │  3. db.upsert(mission)               │
                  │  4. iterm.set_tab_name(new_title)    │
                  │  5. db.reconcile_missing()           │
                  │  6. context.write_context_file()     │
                  └──────────────────────────────────────┘
                                         │
                                         ▼
                  ┌──────────────────────────────────────┐
                  │  ~/.morpheus/                        │
                  │    morpheus.db    (SQLite)           │
                  │    morpheus.log                      │
                  │    context.md     (live snapshot)    │
                  │    context.json   (live snapshot)    │
                  │    snapshots/                        │
                  └──────────────────────────────────────┘
                                         ▲
              one-shot CLI invocations   │
                  spawn / list / prune / │
                  snapshot / context /   │
                  note / notes / doctor  │
              ─────────────────────────  │
                                         ▼
                              every other iTerm tab
                              reads `~/.morpheus/context.md`
                              and runs `morpheus note "..."`
                              for cross-session messaging
```

In v0 the dashboard process IS the daemon. v0.1 splits them: a launchd-managed
background daemon owns the tick loop, the dashboard becomes a pure renderer.

### 7.2 Module layout

| Module | Responsibility |
|---|---|
| `morpheus/cli.py` | Typer entry points |
| `morpheus/core.py` | The tick loop and `_tick()` |
| `morpheus/dashboard.py` | Rich Live dashboard (runs the tick loop too) |
| `morpheus/db.py` | SQLite schema, `Mission`, `Note`, CRUD |
| `morpheus/detect.py` | State classifier from pane buffer |
| `morpheus/iterm_client.py` | Thin async wrapper over iterm2 Python API |
| `morpheus/naming.py` | Tab-title formatting, goal inference |
| `morpheus/context.py` | Cross-session snapshot builders |

### 7.3 State schema

**`missions`**

| Column | Type | Notes |
|---|---|---|
| `tab_id` | TEXT PK | iTerm-assigned tab ID |
| `session_id` | TEXT | iTerm session ID (matches `$ITERM_SESSION_ID`) |
| `goal` | TEXT | Auto-inferred or user-provided |
| `state` | TEXT | working / idle / blocked / finished / crashed / unknown |
| `last_event` | TEXT | Short human-readable detection label |
| `last_event_at` | REAL | Unix timestamp |
| `buffer_hash` | TEXT | sha1 prefix for change detection |
| `buffer_changed_at` | REAL | Unix timestamp of last buffer change |
| `cmd` | TEXT | Original command (when spawned via Morpheus) |
| `linked_pr` | INTEGER | PR number, nullable |
| `linked_worktree` | TEXT | Absolute path, nullable |
| `created_at`, `updated_at` | REAL | Lifecycle |

**`notes`**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTO | |
| `tab_id` | TEXT | Source tab, nullable |
| `session_id` | TEXT | Source iTerm session, nullable |
| `text` | TEXT | The note body |
| `kind` | TEXT | note / claim / broadcast |
| `created_at` | REAL | Unix timestamp |

### 7.4 Detection

Pattern-based and conservative (false positives on "blocked" cause alert
fatigue). Priority: `crashed` > `blocked` > `finished` > `working`/`idle`.

Bundled patterns: codex edit prompts, claude permission menus, generic
`[y/N]`, sudo password, segfault, panic, python traceback.

Patterns are user-extensible in `detect.py`. v0.2 will move them to a config
file.

### 7.5 iTerm2 Python API surface used

- `iterm2.run_until_complete(coro)` — entry point
- `iterm2.async_get_app(connection)` → `App`
- `App.windows` → `[Window]`
- `Window.tabs` → `[Tab]`
- `Tab.current_session` → `Session`
- `Session.async_get_screen_contents()` → screen buffer
- `Session.async_set_name(name)` → tab title
- `Window.async_create_tab()` → new tab
- `Session.async_send_text(text)` → run a command
- `Tab.async_close(force=True)` → kill a tab

Requires user to enable iTerm2 → Settings → General → Magic → Enable Python
API and (for first-run convenience) set Authentication to "Allow all apps to
connect."

---

## 8. v0 — Shipped

| Feature | Status |
|---|---|
| Auto tab-title encoding (🟢🟡🔴⚫💀 + goal + stale age) | ✓ |
| Mission cards in SQLite, auto-inferred from tab name | ✓ |
| Pattern-based state detection (codex, claude, shell, sudo) | ✓ |
| `morpheus spawn` — open new tab and register | ✓ |
| `morpheus list` — table of all missions | ✓ |
| `morpheus prune` — interactive close of stale tabs | ✓ |
| `morpheus snapshot` — dump tab to markdown (token-blowup escape) | ✓ |
| `morpheus dashboard` — live Rich table inside one tab | ✓ |
| `morpheus context` / `note` / `notes` — cross-session awareness | ✓ |
| `morpheus doctor` — connectivity diagnostic | ✓ |
| `~/.morpheus/context.md` + `.json` auto-written every tick | ✓ |
| Self-excludes Morpheus's own tab | ✓ |
| Reconciles missions for closed tabs | ✓ |
| Logging to `~/.morpheus/morpheus.log` | ✓ |

---

## 9. Roadmap

### v0.3 — Always-on + briefings (SHIPPED 2026-05-19)

The daemon now runs unattended, the dashboard becomes a pure renderer
(both share state via SQLite), and `morpheus brief` produces a
human-readable digest of your day on demand.

Shipped:
- ✅ **launchd LaunchAgent** — `morpheus install-daemon` writes
  `~/Library/LaunchAgents/com.morpheus.watch.plist` and loads it.
  Auto-starts at login, auto-restarts on crash. Logs to
  `~/.morpheus/daemon.log`. Uninstall with `morpheus uninstall-daemon`,
  status with `morpheus daemon-status`. The dashboard works whether or
  not the daemon is running (it co-runs the tick); but with the daemon
  on, tab titles stay current even when the dashboard isn't open.
- ✅ **`morpheus brief`** — gathers state (open sessions by state,
  stale candidates, recent notes, GH review queue via `gh`), pipes
  into `claude -p` (or `codex exec` if claude isn't available) with a
  digest prompt, prints a markdown brief. `--out FILE` to save,
  `--notify` to push to macOS notification, `--no-llm` for a
  template-only brief without LLM.
- ✅ **macOS notifications** via `terminal-notifier`. The dashboard
  fires a system notification on every 🐇 alert when the dashboard
  tab isn't focused (or when running in `morpheus watch` headless
  mode). Per-kind silencing in config. Falls back to silent if
  `terminal-notifier` isn't installed (with a one-time message).
- ✅ Health beacon at `~/.morpheus/daemon.beacon` written every tick;
  `morpheus daemon-status` reports the last-beacon age so you know if
  the daemon hung.

### v0.2 — Interactive TUI (SHIPPED 2026-05-19)

Morpheus is now the user's home base. Live in the morpheus tab; navigate,
spawn, focus, prune, snapshot, post notes — all without leaving.

Stack: switched the dashboard from `rich.Live` to **Textual** (canonical
Python TUI framework, by the Rich author). Rich.Text + rain.Rain still
own the rendering of cells/animation.

Shipped:
- ✅ DataTable of missions, **sorted newest-active first** (by
  `buffer_changed_at` desc).
- ✅ **Stock-ticker row flash**: on every state change, the entire row
  paints a state-colored background for 3s, then settles. Green for
  →working, yellow for →blocked, red for →crashed, magenta for
  →finished. Mirrors a Bloomberg green/red ticker.
- ✅ Keybinding-driven UX:
  - `j/k` (or arrows) — navigate the missions table
  - `Enter` — focus the selected session's iTerm tab (jumps you there
    via `tab.async_select`)
  - `n` — modal: spawn new session (goal + command form)
  - `d` — close the selected session's iTerm tab
  - `p` — prune all stale (idle/finished, age > 4h)
  - `s` — snapshot selected to `~/.morpheus/snapshots/`
  - `/` — modal: post a cross-session note attached to the selected tab
  - `r` — force-refresh the heavy tick now
  - `q` (or Ctrl+C) — quit
- ✅ Two modal screens (`NewSessionScreen`, `NoteScreen`) — input fields
  with green/yellow Matrix borders, Enter/Esc to commit/cancel.
- ✅ Rain widget still animates inside its panel, sized to container.
- ✅ Footer auto-renders the active keybindings.

### v0.1 — Rain + alerts (SHIPPED 2026-05-19)

Shipped:
- ✅ Matrix rain animation in the dashboard. One vertical stream per session;
  speed / brightness / color encode state (fast bright = working, yellow
  flicker = blocked, red glitch = crashed, slow dim = idle/finished).
  Decorative streams fill remaining columns at slow speed.
- ✅ **🐇 white rabbit alerts panel** at the bottom of the dashboard. Fires on:
  state changes (blocked/crashed/finished transitions), new spawned sessions,
  new closed sessions, new cross-session notes. White-rabbit emoji marks
  every "follow this" event (Matrix reference; see [[feedback-white-rabbit-new]]).
- ✅ Three-pane Rich Layout (header / rain+missions / alerts).
- ✅ Hardened `morpheus doctor` — surfaces clear iTerm2 setup steps when
  the Python API isn't enabled (iterm2 lib doesn't always raise; we use a
  success flag + BaseException catch).

Remaining for v0.1.x:
- Configurable detection patterns in `~/.morpheus/config.toml` (currently
  hardcoded in `detect.py` — easy to extend in code; v0.4 makes it config)
- `morpheus status` shell-prompt one-liner for non-Morpheus tabs

### v0.4 — Safeguards + worktree awareness (SHIPPED 2026-05-19)

- ✅ **Worktree collision warnings** — iTerm shell-integration `path`
  variable read per tab; tabs sharing a cwd trigger a `worktree_collision`
  alert (dedup'd per group).
- ✅ **Token budget guard** — heuristic on continuous "working" time.
  `warn_minutes` (default 60) fires a 🐇 alert, `snapshot_minutes`
  (default 120) fires the louder "SNAPSHOT NOW" alert with the exact
  command to run.
- ✅ **`~/.morpheus/config.toml`** — exhaustive schema, defaults written
  on first read. Sections: general / detection / notifications / brief /
  autonomy / worktree / token_guard / trigger / topic_watchers / colors.
- ✅ **Soft-autonomy ladder** declared in config (allowed_actions /
  ask_first_actions / denied_actions). Enforced in v0.5+ for autonomous
  spawn paths via `ledger.is_within_daily_cap()`.
- ✅ **`morpheus daemon-status`** — already detailed in v0.3.

### v0.5 — Triggers + autonomous draft sessions (SHIPPED 2026-05-19)

- ✅ **Spawn-from-trigger** — `morpheus/trigger.py` polls
  `gh search prs review-requested:@me` every `gh_poll_secs` from inside
  the watch loop. New PRs land as 🐇 alerts. If
  `config.trigger.spawn_from_gh_pr = true` AND we're inside the daily
  $ cap, a draft codex tab spawns in a worktree (`gh pr checkout` into
  `.claude/worktrees/pr-N` + `codex`). `seen_prs` SQLite table prevents
  re-spawn loops. Available on-demand via `morpheus poll-prs`.
- ✅ **`morpheus ask "<query>"`** — gather_state → claude-p with a
  tight system prompt; falls back to codex exec, then to raw snapshot
  if no LLM available. Logs each ask (~$0.03) to the cost ledger.
- 🔜 **Web-search topic watchers** — schema in config
  (`[topic_watchers]`) but executor deferred to v0.7. (Would call
  `claude -p` with web search on a schedule.)

### v0.6 — Ledger + economy + MCP (SHIPPED 2026-05-19)

- ✅ **Cost ledger** — `cost_ledger` SQLite table; every paid LLM
  invocation logged with tokens + dollars. `morpheus ledger costs`.
  `ledger.daily_dollar_total()` is what the autonomy gate consults.
- ✅ **Action ledger** — `action_ledger` table records every
  spawn/kill/note/snapshot/trigger_spawn. `morpheus ledger actions`.
  Feeds `/retro` for weekly review.
- ✅ **MCP server** — `morpheus mcp serve` exposes the read-only state
  + post_note / claim_path as MCP tools. Wires into Claude Code via
  `~/.claude.json` mcpServers entry. Tools: list_sessions, get_session,
  get_context, get_context_short, post_note, claim_path, daily_spend,
  recent_actions.
- 🔜 **Topic threads** — deferred to v0.7 (column `topic` on missions
  table, `--topic` flag on spawn/list); minor scope.
- 🔜 **MCP spawn/kill** — deferred to v0.7 (FastMCP + iTerm async
  context lifecycle needs more design).

### v1.0 — Cross-platform + extensibility

- Linux + tmux support (parity of features via tmux control mode)
- Terminal.app support via AppleScript (lower-feature fallback)
- Plugin system for custom detection / formatters / notifiers
- Multi-user / shared mode (team mission control over LAN)
- Web dashboard (read-only) for the truly browser-shaped among us

---

## 10. Cross-Session Awareness Pattern

This is a v0 capability worth documenting explicitly because it's the most
powerful new primitive Morpheus introduces.

### 10.1 Why

Multiple agents running in parallel on the same machine often need to know
about each other:

- "Am I about to touch a file another agent is editing?" (collision)
- "What's the state of the other PR reviews in flight?" (coordination)
- "Has someone else already discovered this bug?" (deduplication)

Today, agents have zero awareness of siblings. Morpheus changes that.

### 10.2 How (from inside any agent session)

The tick loop writes a fresh snapshot every ~5s to `~/.morpheus/context.md`.
Any agent can read it:

```
cat ~/.morpheus/context.md
```

Or query specific aspects:

```
morpheus context              # full markdown
morpheus context -f short     # one-line summary
morpheus context -f json      # parseable JSON
morpheus context --refresh    # force live re-poll (slower)
```

To post a note that other sessions will see:

```
morpheus note "I'm working on src/auth/middleware.ts — hold off"
morpheus note --kind claim "claiming worktree zealous-bose for PR #224"
morpheus note --kind broadcast "found a critical bug in jwks rotation, see PR #220"
```

### 10.3 How (from an agent prompt)

Agents can be instructed to:

> Before making any changes, run `morpheus context -f short` to see what
> other sessions are doing. If your work overlaps theirs, post a
> `morpheus note --kind claim` first and consider rescheduling.

For Claude Code / Codex specifically, this can be encoded in a project
`AGENTS.md` or `CLAUDE.md` so agents check it automatically.

### 10.4 Future: MCP integration (v0.4)

A Morpheus MCP server will expose `list_sessions()`, `get_session(id)`,
`post_note(text)`, `claim_path(path)` as first-class tools. Claude Code with
the MCP enabled will see cross-session state without needing shell calls.

---

## 11. Risks & Adversarial Considerations

### 11.1 Autonomy creep

User's own history shows over-scoping is already a problem (v1→v3.2
architecture docs, audit-reconcile scaffold). A proactive daemon
compounds this risk.

**Mitigations**: soft-autonomy ladder (never auto-merge/push/approve),
action ledger, kill switch, daily cap.

### 11.2 Alert fatigue v2

Cron + web search + GH polling could ping constantly.

**Mitigations**: silent by default, escalate only on (a) blocked > 30s on
critical-tagged session, (b) finished with action items, (c) crash. Per-tab
quiet hours.

### 11.3 Daemon failure modes

Who watches the watcher? Cron silently breaks; goroutines deadlock; SQLite
locks.

**Mitigations**: health beacon written every tick; if the file is stale,
launchd restarts; the dashboard shows "DAEMON STALE: X min ago — investigate?"
banner.

### 11.4 State sync races

Daemon writing while dashboard reading. SQLite handles concurrent reads (WAL
mode); single writer is fine.

### 11.5 Cost runaway

`claude -p` / `codex exec` on a loop = real money.

**Mitigations**: per-day $ cap; auto-disable autonomy at cap; alert at 80%;
log every invocation with token + $ estimate.

### 11.6 Headless agent loops spiraling

A daemon-spawned agent can produce v1→v3.2 docs silently.

**Mitigations**: hard caps (max iterations, max wall-time, must-checkpoint
patterns); spawn-from-trigger creates *draft* sessions paused at first
action — the user attaches and runs.

### 11.7 Cross-CLI maintenance trap

Each CLI has different prompt patterns, exit codes, output formats.

**Mitigations**: don't try to support arbitrary tools. v0.2 whitelist:
codex, claude, gh, fly, docker. User-extensible patterns in config.

### 11.8 Pane-buffer signal is noisy

Generic regexes will false-positive.

**Mitigations**: per-tool patterns with explicit labels; prefer conservative
(false-negative > false-positive); user can add patterns easily.

### 11.9 The Morpheus tab is itself a tab

Self-detection prefix (`▶ MORPHEUS`) used by the watcher to skip itself.
Could fail if the user renames the tab.

**Mitigations**: also detect by checking if the tab is running the morpheus
process (PID lookup) — defer to v0.1.

### 11.10 Forgetting the daemon is running

In v0 the user runs `morpheus dashboard` in a tab; if they close it, title
updates stop silently.

**Mitigations**: v0.1 launchd integration eliminates this entirely.

---

## 12. Open Questions

- **Q1**: Should the Matrix-rain dashboard be the default view in v0.1, or
  remain optional via `morpheus dashboard --rain`?
- **Q2**: How aggressively should Morpheus auto-infer the `goal` from
  command context? More inference = better default titles, but more risk of
  wrong titles that the user has to correct.
- **Q3**: What's the right format for the "morning brief" — markdown
  rendered in the terminal, a Slack DM, a macOS notification, or a file the
  user opens in their preferred reader?
- **Q4**: Should `morpheus note --kind claim "<path>"` actively prevent
  other agents from editing the claimed path, or just warn? Active
  prevention requires fs-event watching.
- **Q5**: For spawn-from-trigger draft sessions (v0.3): should they be
  paused before running the first command, or before the very first prompt
  in the codex session? The former is safer, the latter is more useful.
- **Q6**: Does the user prefer the Morpheus tab to be pinned to the leftmost
  position in iTerm (always-known location) or just exist wherever opened?

---

## 13. Out of Scope (explicitly)

- iTerm replacement / re-implementation
- A custom terminal multiplexer
- An inbox UI of any kind
- Auto-merge, auto-push, auto-approve PRs, auto-send messages
- Cloud hosting / multi-user / SaaS
- Cross-platform v0 (Linux/tmux is v1.0)
- Browser-based dashboard
- Re-implementing cron, schedule, or anything `claude`/`codex` already does
- Replacing `gh`, `git`, `fly`, etc. — Morpheus orchestrates, doesn't replace

---

## 13.5 Daily life with Morpheus (user journey)

A walked-through day for our solo founder running 10+ agent sessions:

**08:00 — first coffee**

```
morpheus brief
```

Reads a 10-line markdown digest:
- 3 sessions still working from overnight (1 blocked, 2 finished)
- 4 new PRs landed in your review queue
- 1 stale session from yesterday's experiment — suggest pruning
- Today's stated focus from last brief: "ship PR #220, start #224 conflict"

Brief either prints to stdout or fires as a macOS notification (`--notify`).
Optional: scheduled by launchd at 08:00 + 18:00 (see v0.6 schedule
integration).

**08:30 — start the day in MORPHEUS**

```
morpheus    # ← in a dedicated iTerm tab
```

Dashboard opens. Ticker shows all 10 sessions sorted by recency. The 1
blocked-from-overnight is at the top with a fresh yellow flash.
You hit `Enter`, iTerm jumps to that tab, you resolve it, come back to
morpheus with `⌘+1`.

**09:15 — new PR review request lands**

(With v0.5 spawn-from-trigger:) Daemon detects the new PR, creates a
draft session in a worktree, 🐇 alert: *"draft session ready for PR
#225 — attach or dismiss"*. You navigate down with `j`, hit `Enter`,
review and approve. Three keystrokes total.

**14:00 — context lost on a long-running codex**

Tab 7 has been chewing on x402 testing for 90 minutes; you have no
recall. Hover over it with `j`, mission card shows: goal,
last-meaningful-event (LLM-summarized from buffer), suggested
next-step. Full recall in 2 seconds.

**16:00 — token-blowup risk**

🐇 alert: *"tab 7 at 87% of estimated token budget — snapshot suggested."*
You press `s`. Morpheus dumps the session to
`~/.morpheus/snapshots/2026-05-19T16-04-22-tab7.md`. You open a fresh
codex tab and feed it the snapshot. Continuity preserved.

**18:00 — evening brief**

`morpheus brief` again — what shipped today, what's still open, what
needs your eyes tomorrow morning.

**Throughout — cross-session coordination**

When you start working on `src/auth/middleware.ts` in tab 3, you press
`/` and post a note: *"touching src/auth/* — hold off."* Tab 8 (another
codex session) reads `~/.morpheus/context.md` between actions, sees the
claim, defers.

---

## 14. CLI command reference (complete as of v0.6.0)

| Command | Purpose |
|---|---|
| `morpheus` | Default — launch the interactive Textual dashboard |
| `morpheus dashboard` | Same as `morpheus` (explicit subcommand) |
| `morpheus watch [--poll 5] [--no-notify]` | Headless tick loop; updates titles + context.md + macOS notifications |
| `morpheus spawn "<goal>" "<cmd>"` | Open new iTerm tab, run cmd, register mission |
| `morpheus list [--stale 4]` | Print every mission with state, age, last event |
| `morpheus prune [--older-than 4] [--yes]` | Close stale tabs (idle/finished, age >threshold) |
| `morpheus snapshot <tab_prefix> [--out FILE]` | Dump tab buffer + mission to markdown |
| `morpheus context [-f md/json/short] [--refresh]` | Print the shared cross-session snapshot |
| `morpheus note "<text>" [--tab ID] [--kind note/claim/broadcast]` | Post a cross-session note |
| `morpheus notes [--limit 15] [--tab ID]` | List recent cross-session notes |
| `morpheus brief [--out FILE] [--notify] [--no-llm] [--no-gh]` | Generate digest of current state via claude-p |
| `morpheus ask "<query>" [--no-llm]` | Ask morpheus about its own state (claude-p answer) |
| `morpheus poll-prs` | One-shot GH PR poll → 🐇 alerts (and draft sessions if config enables) |
| `morpheus ledger costs [-n 50]` | Recent LLM cost ledger entries + today's total |
| `morpheus ledger actions [-n 50]` | Recent action ledger (spawns/kills/notes/etc) |
| `morpheus install-daemon [--poll 5]` | Install + start the launchd background watcher |
| `morpheus uninstall-daemon` | Stop and remove the launchd agent |
| `morpheus daemon-status` | Report daemon health (running? last beacon? log size?) |
| `morpheus mcp serve` | Start MCP stdio server for Claude Code / Codex |
| `morpheus doctor` | Diagnose iTerm2 + Python API connectivity |
| `morpheus version` | Print morpheus version |

## 15. Config schema (v0.4 — proposed)

`~/.morpheus/config.toml`:

```toml
[general]
poll_interval = 5.0          # seconds between ticks
stale_after_hours = 4.0      # what counts as a stale tab
log_level = "info"

[detection]
# Add user-defined patterns
extra_blocked_patterns = [
  "ENTER your access token:",
  "MFA code:",
]

[notifications]
enabled = true
silence_kinds = []           # any of: state, note, spawn, close, error
quiet_hours = []             # e.g., ["22:00-07:00"]

[brief]
schedule = ["08:00", "18:00"]
include_gh_queue = true
gh_repos = ["bkeyID/bkey-id-backend", "bkeyID/bkey-id-mobile"]  # optional filter
include_calendar = false     # v0.5

[autonomy]
daily_dollar_cap = 5.00
permissions = "soft"         # off | soft | full
allowed_actions = ["poll", "summarize", "research", "draft"]
ask_first_actions = ["spawn", "kill", "delete"]
denied_actions = ["merge", "push", "approve", "external-message"]

[colors]
# Override the palette (Rich color names or color(N))
state_working = "bright_green"
state_blocked = "bold bright_red"
flash_duration_secs = 3.0
```

## 16. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `morpheus doctor` reports connection failure | iTerm2 Python API not enabled or iTerm2 not running | Enable in iTerm2 Settings → General → Magic → Python API; restart iTerm2 |
| Tab titles don't update | Watch loop not running anywhere | Run `morpheus install-daemon` or open `morpheus` dashboard |
| Dashboard says "iTerm2 connect failed" | Cookie auth blocked | Set authentication to "Allow all apps to connect" in iTerm2 magic settings |
| No notifications appear | terminal-notifier not installed | `brew install terminal-notifier` |
| Brief says "claude not found" | Anthropic CLI missing | `npm install -g @anthropic-ai/claude-code` or use `--no-llm` |
| State stays "unknown" forever | No detection pattern matched | Add custom patterns to `detect.py` (or v0.4 config); confirm `morpheus context -f short` is updating |
| Self tab (MORPHEUS) gets classified | Self-marker prefix was overridden | Title gets re-claimed every tick; if persistent, run `morpheus dashboard` again |
| Worktree collision (v0.4+) | Two sessions in same dir | Use a worktree per task — see `workflow_use_worktrees` memory |

## 18. References & Memory

- `feedback_morpheus_no_inbox.md` — inbox rejected, augment existing surfaces
- `workflow_parallel_sessions.md` — ~20 tabs, tab-bar pain
- `workflow_use_worktrees.md` — multi-agent collisions, the original sin
- Claude Code Insights (2026-05-19) — multi-clauding stats, friction surfaces
- Prior conversation: 2026-05-19 design session covering architecture, scope,
  adversarial pass, and the codex challenge (running at time of v0 writing)

---

*"I can only show you the door. You're the one that has to walk through it."*
