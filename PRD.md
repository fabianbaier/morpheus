# Morpheus — Product Requirements Document

| Field | Value |
|---|---|
| **Status** | v0.2.0 (interactive Textual TUI: keybindings, stock-ticker row flash, in-dashboard spawn/kill/prune/note) |
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

Remaining for v0.1:
- launchd integration so the daemon runs unattended; dashboard becomes a
  pure renderer
- macOS native notifications via `terminal-notifier` for true emergencies
  (parity with the 🐇 alerts panel when the dashboard isn't focused)
- Worktree collision warnings (`workflow_use_worktrees` memory)
- Configurable detection patterns in `~/.morpheus/config.toml`
- `morpheus status` shell-prompt one-liner for non-Morpheus tabs

### v0.2 — Briefings + composition

- `morpheus brief` — morning + evening digest via `claude -p` (overnight GH
  activity, calendar, stale sessions, decisions needed)
- `morpheus ask "<query>"` — conversational query over current state
- Cron-style task scheduler composing the existing `loop` / `schedule` /
  `scheduled-tasks` MCP — not a re-implementation
- PRD watch (configurable repo + glob, polled via `gh`)
- Web-search topic watchers — `claude -p` with web search on a schedule

### v0.3 — Triggers + safeguards

- Spawn-from-trigger: new GH review-requested PR → daemon creates a draft
  codex session in a worktree, diff pre-loaded, `/adversarial-review` prompt
  pre-filled, paused at first action
- Token budget guard — per-session estimate; warn at 70%, auto-suggest
  snapshot at 90%
- Soft-autonomy ladder configuration (per-action authorization)
- Daemon health beacon with self-recovery + an alert when the daemon itself
  is unhealthy

### v0.4 — Ledger + economy

- Cost ledger — every autonomous action logged with $ estimate
- Daily / monthly cap; auto-disable autonomy at cap; alert at 80%
- Action ledger viewable via `/retro` weekly
- Topic threads — group sessions about the same PR/PRD into a thread
- MCP server — expose mission state and notes as MCP tools so Claude Code
  /Codex can call them natively without shelling out

### v1.0 — Cross-platform + extensibility

- Linux + tmux support
- Plugin system for custom detection / formatters / notifiers
- Multi-user / shared mode (team mission control)

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

## 14. References & Memory

- `feedback_morpheus_no_inbox.md` — inbox rejected, augment existing surfaces
- `workflow_parallel_sessions.md` — ~20 tabs, tab-bar pain
- `workflow_use_worktrees.md` — multi-agent collisions, the original sin
- Claude Code Insights (2026-05-19) — multi-clauding stats, friction surfaces
- Prior conversation: 2026-05-19 design session covering architecture, scope,
  adversarial pass, and the codex challenge (running at time of v0 writing)

---

*"I can only show you the door. You're the one that has to walk through it."*
