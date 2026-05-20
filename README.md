```
        ███╗   ███╗ ██████╗ ██████╗ ██████╗ ██╗  ██╗███████╗██╗   ██╗███████╗
        ████╗ ████║██╔═══██╗██╔══██╗██╔══██╗██║  ██║██╔════╝██║   ██║██╔════╝
        ██╔████╔██║██║   ██║██████╔╝██████╔╝███████║█████╗  ██║   ██║███████╗
        ██║╚██╔╝██║██║   ██║██╔══██╗██╔═══╝ ██╔══██║██╔══╝  ██║   ██║╚════██║
        ██║ ╚═╝ ██║╚██████╔╝██║  ██║██║     ██║  ██║███████╗╚██████╔╝███████║
        ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚══════╝

                          mission control for your terminal
```

> *"I'm trying to free your mind, Neo. But I can only show you the door.
> You're the one that has to walk through it."*

You have 20 tabs open in iTerm. Each is running an agent — `codex`, `claude`, a build,
an SSH session. They alert. You forget what they were doing. They go stale. Over days,
the tab bar becomes archaeology.

**Morpheus is the air-traffic-controller for your tabs.** It watches every iTerm tab,
detects what each one is doing, rewrites the tab title with state and mission, and
gives you one-keystroke spawn / prune / snapshot.

The tab bar IS the dashboard. Morpheus just makes it smarter.

## What you get

- 🟢 `PR #224 conflict` — actively working
- 🟡 `setup-CIBA agent` — idle
- 🔴 `x402 review` — BLOCKED, needs your input
- ⚫ `0.3.0 release` — finished
- 💀 `kms recovery` — crashed
- `36h • bundle ID PRD` — stale, prune candidate

Plus:
- `morpheus spawn "<goal>" "<cmd>"` — opens a new iTerm tab with the right command
- `morpheus list` — see everything at once
- `morpheus prune` — close stale tabs interactively
- `morpheus snapshot <tab>` — dump a tab to markdown before it dies (token-blowup escape)
- `morpheus` (no args) — Matrix-rain live dashboard inside one dedicated tab, with 🐇 alerts panel for every state change / new note / spawn
- `morpheus context` — shared cross-session snapshot every running agent can read
- `morpheus note "<text>"` — post a note other sessions will see in their context

## Install

Requires macOS + iTerm2 + Python 3.10+.

```bash
cd ~/github/fabianbaier/morpheus
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Then **enable the iTerm2 Python API**: `iTerm2 → Settings → General → Magic → Enable Python API`. Set the auth to "Require Authentication" if you want, but for first-run, "Allow all apps to connect" is the path of least resistance.

## Use

In one tab, start the watcher:

```bash
morpheus watch
```

Leave that running. Every 5s it polls all tabs, detects state, rewrites titles.

From any other tab:

```bash
morpheus spawn "PR #224 review" -- codex
morpheus list
morpheus prune
morpheus snapshot 3
```

Or for the full TUI:

```bash
morpheus dashboard
```

## Cross-session awareness

Every agent session can see what every other session is doing, live, without
leaving its tab. The watch loop writes a fresh snapshot every ~5s to:

- `~/.morpheus/context.md` — markdown, human/agent readable
- `~/.morpheus/context.json` — parseable JSON

**From inside any codex/claude session:**

```bash
cat ~/.morpheus/context.md             # what is everyone doing?
morpheus context                        # same, rendered
morpheus context -f short               # one-line summary for prompts
morpheus context -f json                # parseable
morpheus note "touching src/auth/*, hold off"   # post for siblings
morpheus note --kind claim "PR #224 worktree zealous-bose"
morpheus notes -n 20                    # recent cross-session traffic
```

Agents inside a session pick up `$ITERM_SESSION_ID` automatically so notes
are attributed to the right tab. The shared markdown marks each session's
own row with `**[YOU]**` so an agent can tell which row is itself.

**Suggested AGENTS.md / CLAUDE.md snippet** for repos you work on in parallel:

```markdown
## Other sessions
Before editing files, run `morpheus context -f short` to see what other
agents are doing. If you'd collide, post a `morpheus note --kind claim`
first or reschedule.
```

## State

Mission cards + notes persist to `~/.morpheus/morpheus.db` (SQLite). Logs
go to `~/.morpheus/morpheus.log`. Snapshots from `morpheus snapshot` go to
`~/.morpheus/snapshots/`. All survive iTerm restarts.

## Roadmap

- **v0.0** — watch, spawn, list, prune, snapshot, basic dashboard
- **v0.1** (this) — Matrix-rain dashboard with 🐇 alerts panel, cross-session context, hardened doctor
- **v0.1.1** — launchd daemon, macOS notifier, worktree collision warnings, configurable patterns
- **v0.2** — `morpheus ask` conversational front, briefing mode, cron tasks composing `loop` / `schedule`
- **v0.3** — spawn-from-trigger (new GH review-requested PR → draft session), token budget guard
- **v0.4** — soft-autonomy ladder, cost ledger, web-search topic watchers

> *"Welcome to the desert of the real."*
