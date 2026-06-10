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
