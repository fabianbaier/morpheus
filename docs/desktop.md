# Morpheus Desktop

A desktop **chat-agent cockpit** for Morpheus — the same feel as Claude Code /
Codex / Cowork, tailored to Morpheus's mission graph, live sessions, autonomous
goals, and prompt loops. It is a *front-end over the same SQLite database the CLI
and daemon use*, so the desktop app and CLI always share state.

```
┌──────────────────────────────────────────────────────────────────┐
│  ▶ MORPHEUS   🟢 3 working · 🔴 1 blocked        $0.42   ⌘K       │
├────────────┬───────────────────────────────────┬─────────────────┤
│ Conversation│                                   │  Mission Card   │
│  ✦ Chat     │     Chat with Morpheus            │  why / done     │
│ Sessions    │     (streaming markdown answers,  │  plan / next    │
│  🔴 fix auth│      action chips, citations)     │  blocked-on     │
│  🟢 build UI│                                   │  timeline       │
│ Goals       │  ── or, session selected ──       │  proof          │
│ Loops       │     live terminal tail            │                 │
├────────────┴───────────────────────────────────┴─────────────────┤
│ 🐇  spawned codex PR #224 · loop "blockers" ran · tab-3 blocked    │
└──────────────────────────────────────────────────────────────────┘
```

## Architecture

```
morpheus/desktop/
  bridge.py     OS-agnostic domain layer over morpheus.db → JSON dicts.
                Reads + notes/chat work everywhere; iTerm2 control ops are
                macOS-only and degrade gracefully elsewhere.
  server.py     stdlib HTTP + Server-Sent-Events bridge. Token auth, Host-header
                validation, loopback-only bind. Serves the web/ SPA.
  web/          Vanilla HTML/CSS/JS single-page chat app (no build step).
  electron/     Thin Electron shell → native macOS .app/.dmg.
```

Why this shape (it came out of two design-review rounds):

* **Compatible by construction** — one database (`~/.morpheus/morpheus.db`), one
  `config.toml`. The desktop app is just another view, like the TUI dashboard.
* **No new runtime dependency** — the server is pure standard library, so it runs
  on the exact interpreter the CLI uses and is fully testable offline.
* **Safe DB coexistence** — `db.py` now opens the database in WAL mode with a
  busy timeout, so the long-running server, the CLI, and the launchd daemon can
  read/write concurrently without "database is locked".

## Run it

```bash
pip install -e .

# Open the cockpit in your browser (starts the bridge, picks a free port):
morpheus desktop

# …or just run the bridge (no browser) — useful for the Electron shell or remote:
morpheus desktop serve
```

`morpheus desktop` prints the URL (with a one-time token) and opens it. The
server binds `127.0.0.1` only and requires the token on every request.

### Native macOS app (Electron)

```bash
cd morpheus/desktop/electron
npm install
npm start            # dev: opens a native window against the local bridge
npm run dist         # build a .dmg (macOS + Xcode CLT required)
```

See [`electron/README.md`](../morpheus/desktop/electron/README.md) for signing
and notarization.

## The chat agent

The composer has an **agent picker**. Two modes:

### 1. Ask Morpheus (the oracle)

A GUI over `morpheus ask`. It answers from the live fleet snapshot + mission
graph, and routes slash-commands to actions:

| You type | What happens |
| --- | --- |
| `what is blocked right now?` | conversational answer over fleet state |
| `summarize the auth-refactor mission` | reads that mission's graph card |
| `/spawn review PR #224 -- codex` | opens an iTerm tab + registers a mission (macOS) |
| `/broadcast hold off on src/auth/*` | records a cross-session broadcast note |
| `/note remember to rebase` | posts a shared note |

When no `claude`/`codex` CLI is available, chat falls back to the raw state
snapshot, so it always returns something useful.

### 2. Live agent (Claude / Codex / Gemini)

Pick **Claude Code**, **Codex**, or **Gemini** from the picker and you're chatting
with the *real CLI* — but it feels native, like Claude Code's own desktop app.
Under the hood `morpheus/desktop/agents.py` spawns the CLI in its streaming mode
(`claude -p --output-format stream-json`, `codex exec --json`, …), normalises the
output into one event schema, and streams it to the UI over SSE. You see, live:

* **streamed prose** as the model writes,
* **tool-use cards** (Read / Edit / Bash / Grep / Task …) with their inputs and
  collapsible results,
* **web search / web fetch** chips when the agent goes to the web,
* a **thinking** indicator, and a final **cost** + web-search count.

Conversations are multi-turn: the agent's `session_id` is captured and replayed
(`claude --resume`) so context carries across turns. A working-directory chip and
a permission-mode selector (default / plan / acceptEdits / bypass) let you control
what the agent may do. Each turn's cost is logged to the same ledger as the rest
of Morpheus.

Only installed CLIs are selectable (the picker greys out the rest). `claude` is
the most fully supported; the `codex`/`gemini` adapters map their known event
shapes and degrade any unrecognised line to streamed text.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/agents` | which agent CLIs are installed + the current working dir |
| `POST /api/agent/turn` | `{agent, message, cwd?, session_ref?, permission_mode?}` → an SSE stream of `session`/`thinking`/`text`/`tool_use`/`tool_result`/`web_search`/`result` events |

## Feeds — the condensed aggregator

A **feed** is one terminal stream of high-level updates you (or any device — AR
glasses, a watch, a phone widget) can subscribe to, instead of watching every
session. Sources push condensed items into it; **rules with thresholds** decide
what gets through:

| Policy | Pushes when |
| --- | --- |
| `always` | every result |
| `on_change` | the summary differs from the last pushed item |
| `on_match` | the result matches a regex (e.g. `breaking\|error\|>\s*100`) |
| `on_failure` | the source run failed (failures always push, with priority) |

Today the source is **loops**: create a loop ("scan HN every 30m"), set its feed
subscription in the loop detail view (or at creation), and matching results land
in the feed — in the desktop Feed view, the Mission Cockpit card, and any
external subscriber. The schema is source-generic (`source_kind`/`source_ref`),
so later sources — email, sensors, agent awareness — plug in without migration.

**Subscribing from minimal clients** (the glasses path):

```bash
# one line per item, newest first — no JSON parsing needed
curl "http://127.0.0.1:$PORT/api/feed/text?token=$TOKEN"
# 18:08 ! [loop] BREAKING: agents everywhere
# or live: the /api/stream SSE emits `feed` events as items arrive
```

## TUI parity in the desktop

Loops and goals are fully manageable from the UI: click a loop for its detail
view (prompt, command, run history, feed subscription) with pause / resume /
run-now / delete; click a goal for budgets + tasks with pause / resume / done /
clear; the `+` buttons (and ⌘K "New loop… / New goal…") open creation forms. The
**Mission Cockpit** view is the everything-overview: fleet health, sessions,
goals, loops, the feed tail, and recent notes in one grid.

## HTTP API

All `/api/*` routes require `Authorization: Bearer <token>` (or `?token=` for the
SSE stream). `/healthz` is unauthenticated.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/healthz` | readiness probe (used by the Electron parent) |
| GET | `/api/fleet` | counts, sessions, goals, notes, spend |
| GET | `/api/sessions` | live sessions |
| GET | `/api/sessions/{ref}` | full mission card (memory + events + artifacts + edges) |
| GET | `/api/goals` `/api/loops` `/api/notes` `/api/activity` `/api/spend` `/api/projects` | cockpit data |
| GET | `/api/loops/{id}` | loop detail: config, run history, feed rule |
| POST | `/api/loops` | create loop `{name, prompt, every, command?, feed_policy?, feed_pattern?}` |
| POST | `/api/loops/{id}/action` | `{action: pause\|resume\|delete\|run_now}` |
| POST | `/api/loops/{id}/feed-rule` | `{policy, pattern?}` ('' clears) |
| POST | `/api/goals` | create goal `{objective, done_definition?, source?, autonomy_level?, max_turns?, max_workers?}` |
| POST | `/api/goals/{ref}/action` | `{action: pause\|resume\|done\|clear}` |
| GET | `/api/feed` | feed items `?limit=&since_id=` |
| GET | `/api/feed/text` | plain-text condensed feed (glasses/watch-friendly) |
| GET | `/api/feed/rules` | all push rules |
| POST | `/api/feed` | manual post `{title, body?, priority?}` |
| GET | `/api/stream?token=` | Server-Sent-Events; pushes a `fleet` event on change |
| POST | `/api/chat` | `{message, use_llm?, include_gh?}` → answer |
| POST | `/api/notes` | `{text, kind?, tab_id?}` |
| POST | `/api/broadcast` | `{text, submit?}` |
| POST | `/api/spawn` | `{goal, command}` (macOS/iTerm only) |
| POST | `/api/send` | `{tab_id, text, submit?}` (macOS/iTerm only) |

## Tests

```bash
python -m unittest tests.test_desktop_bridge tests.test_desktop_server \
                   tests.test_desktop_agents tests.test_desktop_web
```

`test_desktop_agents` checks the agent runner against a real captured
`claude --output-format stream-json` fixture (`tests/fixtures/claude_stream.jsonl`)
and exercises `run_turn` end-to-end against a fake agent that replays it — so it
needs no network, credits, or installed CLI.

`test_desktop_web` runs the front-end JS unit tests via Node and is skipped
automatically when Node isn't installed.
