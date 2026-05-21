# Morpheus — Product Requirements Document

| Field | Value |
|---|---|
| **Status** | v0.8.0a26 implemented (cwd project tenancy + cleanup + cached activity snapshot); autonomous PRD goal runs implemented locally |
| **Author** | Fabian Baier |
| **Last updated** | 2026-05-21 |
| **Target platform** | macOS + iTerm2 |
| **Repo** | `~/github/fabianbaier/morpheus` |

> *"You take the blue pill — the story ends, you wake up in your bed and believe
> whatever you want to believe. You take the red pill — you stay in Wonderland,
> and I show you how deep the rabbit hole goes."*

---

## 1. Executive Summary

Morpheus is a **terminal-native mission graph cockpit for parallel AI agents**. It
turns ~20 open codex / claude / shell sessions into a coherent operational
picture by owning the mission layer: what each session exists to do, what it is
waiting on, what it decided, what it should do next, and whether it is safe to
close, resume, or spawn a replacement.

**Core thesis**: the tab bar is not the product. The tab bar is a signal strip.
The source of truth is the Morpheus cockpit: one keyboard-driven terminal UI
with Matrix-style live streams, durable mission cards, a compounding mission
graph, direct jump/attach actions, and sharp alerts for sessions that need the
user's eyes.

The v0.6 implementation already proves the base layer: iTerm observation,
smart titles, Textual dashboard, launchd daemon, notifications, briefings,
worktree warnings, token guard, cost/action ledger, spawn-from-trigger, and MCP
state exposure. v0.7 must now solve the deeper pain: **intent recovery across
days**. If Morpheus cannot tell the user "why this session exists, what changed,
what proof exists, and what to do next" after two days away, it has failed.

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
- **Smart tab titles alone** → better labels, same forgotten intent
- **An inbox / triage queue** → just another tab to forget. (Explicitly
  rejected — see `feedback_morpheus_no_inbox.md`.)
- **Slack / Notion task list** → context-switching out of the terminal
- **A pretty Matrix animation alone** → vibe without operational leverage

### 2.4 Competitive and pattern research

Adversarial review changed the v0.7 wedge. The nearby market already has many
ways to run multiple agents in one terminal, board, or dashboard. Morpheus must
therefore not compete as "another multi-agent TUI." It must compete as the
durable mission-graph layer that makes old sessions instantly understandable.

Research inputs and takeaways:

- **CCPM** (`automazeio/ccpm`) — spec-driven workflow: ideas → PRDs → epics →
  tasks → GitHub issues → parallel worktrees → commits. Takeaway: Morpheus needs
  traceability fields (`source_doc`, `epic_ref`, `issue_ref`,
  `acceptance_criteria`, `proof_artifacts`) so every mission can explain where
  its work came from and how "done" is judged.
- **Claude Code** — great because it is terminal-native, repo-aware, resumable,
  worktree-friendly, memory-aware, hookable, permissioned, and built around a
  visible agent loop. Takeaway: Morpheus should not become a coding agent; it
  should be the cross-session layer that tracks loop phase, proof, permissions,
  cost, and resumability across Claude, Codex, OpenCode, and shell sessions.
- **2026 goal-mode research** — Codex CLI exposes experimental
  [`/goal`](https://developers.openai.com/codex/cli/slash-commands) to attach a
  persistent target to a larger task; Codex
  [subagents](https://developers.openai.com/codex/concepts/subagents) keep noisy
  parallel exploration out of the main thread. Hermes
  [`/goal`](https://hermes-agent.nousresearch.com/docs/user-guide/features/goals)
  makes the Ralph-loop shape explicit: a standing objective, cheap judge after
  every turn, continuation prompt, pause/resume/clear controls, mid-run
  subgoals, persistence, and a turn budget. Claude Code
  [`/goal`](https://code.claude.com/docs/en/goal) uses a separate evaluator
  model after each turn, while
  [Agent View](https://code.claude.com/docs/en/agent-view) adds the missing
  operator surface: background sessions grouped by state, peek/reply, attach,
  per-session worktree isolation, and durable supervisor state. Takeaway:
  Morpheus should implement goal mode above provider CLIs: the goal belongs to
  the mission graph, the controller is visible, workers are first-class mission
  rows, and proof/status rollups reach the cockpit, white-rabbit ticker, MCP,
  and `morpheus ask`.
- **Karpathy LLM Wiki pattern** — raw sources stay immutable, the LLM maintains
  an interlinked markdown wiki, and an index/log/lint loop makes knowledge
  compound. Takeaway: transcripts are raw sources, mission cards are maintained
  wiki pages, and the mission graph links sessions, topics, files, PRs,
  decisions, blockers, proof, and archived snapshots.
- **Open-source session managers** — Claude Squad, Agent Session Manager, agtx,
  Agent Deck, lazyagent, Kolu, Cline, OpenCode, Roo Code, and Aider show the
  convergent primitives: tmux/worktrees, live previews, task boards, hooks,
  prompt sending, cost/token views, subagent trees, and plan/act modes.
  Takeaway: Morpheus can borrow those primitives, but its unique value must be
  "48-hour recall": select an old mission and know what it was for without
  reading a transcript.

---

## 3. Goals & Non-Goals

### Goals (current + next)

- G1: At a glance, the user can tell which sessions need attention now.
- G2: For any session, the user can recover the mission in under 2 seconds:
  goal, why it exists, current plan, last decision, blocker, and next step.
- G3: Spawning a new agent session is one keyboard action from the cockpit and
  always creates a durable mission card.
- G4: Stale sessions are visible, explainable, and prunable in seconds.
- G5: Long-running sessions can be snapshotted and resumed without losing
  mission continuity.
- G6: Worktree/path collisions are surfaced before they corrupt commits.
- G7: Morpheus runs unattended via launchd; the user does not have to remember
  to start it.
- G8: Morning/evening briefings show overnight work, stale sessions, PR queue,
  decisions needed, and yesterday's unfinished intent.
- G9: New PR review requests can spawn pre-loaded draft sessions automatically,
  inside daily cost and autonomy caps.
- G10: Every agent session can see what every other session is doing without
  leaving the terminal, via context files and MCP tools.
- G11: Every durable mission can show lineage and proof: source PRD/issue,
  acceptance criteria, branch/worktree, claimed paths, checks run, artifacts,
  and confidence.
- G12: Mission knowledge compounds across sessions via a local graph of
  missions, topics, decisions, blockers, files, PRs, snapshots, and evidence.
- G13: A durable PRD or mission can be promoted into an autonomous goal run:
  one visible controller session keeps the end goal alive, fans out bounded
  worker sessions in parallel, collects their status/proof, and stops, pauses,
  or escalates based on explicit completion criteria and budgets.

### Non-Goals (forever)

- N1: Replace tmux / iTerm. Morpheus is a layer on top, not a replacement.
- N2: Build an inbox. Morpheus is an operating cockpit, not another queue.
- N3: Auto-merge / auto-push / auto-approve PRs. Soft-autonomy ladder caps
  destructive actions at "ask first" forever.
- N4: Build new tooling for things the user already has: `loop`, `schedule`,
  `scheduled-tasks` MCP, `gh`, `codex`, `claude`. Compose; do not rebuild.
- N5: Cross-platform on day one. macOS + iTerm2 only. Linux/tmux is v1.0.
- N6: Treat the tab bar as sufficient. Tab titles are alerts; mission cards are
  memory.
- N7: Replace CCPM, agtx, Agent Deck, or any task board. Morpheus can link to
  their artifacts, but its core is mission recall and agent attention routing.

---

## 4. Design Principles

1. **The cockpit is the source of truth.** The tab bar, native notifications,
   shell prompt, and MCP tools are surfaces over one durable mission model.
2. **The tab bar is a signal strip.** It should say "look here now," not carry
   the whole product. If a detail cannot fit in a tab title, it belongs in the
   cockpit mission card.
3. **The mission graph is the kernel.** Every session attaches to a durable
   mission node with a goal, why, state, phase, current plan, last decision,
   blocker, next step, claimed paths, repo, branch, worktree, command, links,
   source docs, and proof artifacts. The graph is what survives context switches
   and reboots.
4. **Keyboard-first, terminal-native.** The happy path is `morpheus`, then
   `j/k`, `Enter`, `n`, `s`, `/`, `p`, `d`. No mouse, no browser, no ceremony.
5. **Matrix is structure, not decoration.** Streams encode live session state;
   the aesthetic earns its keep only when it makes attention routing faster.
6. **Loop state beats transcript length.** The cockpit should show whether a
   mission is planning, editing, testing, reviewing, blocked, or done-needs-human
   before asking the user to read raw chat.
7. **Facts need provenance.** User-authored truth, transcript-derived evidence,
   and LLM-inferred summaries must be stored separately. A summary without a
   source is a hint, not truth.
8. **Silent by default, loud when it matters.** Notifications fire only on
   true emergencies (blocked > 30s on critical-tagged sessions, prod alerts).
9. **Soft-autonomy ladder.** Per-action class authorization:
   - Always allowed: polling, summarizing, web search, draft session creation
   - Ask first: spawning a live session that runs commands, killing a session,
     deleting files
   - Never: merging, pushing, approving PRs, sending external messages
10. **Compose existing primitives.** `loop`, `schedule`, `scheduled-tasks` MCP,
   `codex exec`, `claude -p`, `gh` — Morpheus orchestrates these, doesn't
   replace them.
11. **State is durable.** SQLite + JSON files in `~/.morpheus/` survive iTerm
   restarts and reboots. If a session disappears, the mission remains.
12. **Coordination must be active.** Passive "please check context first"
    conventions are useful but not enough; Morpheus should surface collisions
    and claims directly in the cockpit.
13. **Autonomy needs a controller and a kill switch.** A goal run must always
    have an inspectable controller session, explicit done criteria, turn/time/
    spend/worker budgets, pause/resume/clear controls, and a rule that real user
    input preempts queued continuation work.

### 4.1 Hard product stance

Strong claim: **Morpheus wins only if it becomes the place the user lives while
running agents.** A tab-title enhancer is useful but not important enough.
A Matrix animation is delightful but not sufficient. The valuable product is
the thing that lets the user run 20 agents for three days and still know, in
seconds, what each agent is for, what it touched, what it decided, what it
needs, and whether to continue, snapshot, archive, or kill it.

Build fewer clever automations until the cockpit can do that. The next unit of
value is not another watcher; it is a durable mission graph plus fast keyboard
control.

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

## 6. Product Surface

### 6.1 Primary surface: the mission cockpit

A single dedicated terminal tab running `morpheus` is the home base. It is not
an inbox. It is an operating cockpit for active sessions.

Required cockpit layout:

```
┌────────────────────────────────────────────────────────────────────┐
│ MORPHEUS        🔴 2 blocked   💀 1 crashed   🟢 9 working          │
├───────────────────────┬──────────────────────────────┬─────────────┤
│ Matrix session streams │ Selected live transcript      │ Mission card │
│ + sortable mission list│ tail + alert history          │ why/next/etc │
├───────────────────────┴──────────────────────────────┴─────────────┤
│ 🐇 ticker: blocked prompts, session summaries, collisions, spawns    │
└────────────────────────────────────────────────────────────────────┘
```

The cockpit answers three questions faster than tab switching can:

- **What needs me now?** Blocked, crashed, done-needs-review, collision, and
  token-risk sessions float to the top.
- **What was this about?** The mission card shows goal, why, plan, last
  decision, loop phase, current blocker, next step, repo, branch, worktree,
  claimed paths, source doc/issue, command, linked PR, checks, proof artifacts,
  age, and snapshot location.
- **What can I do next?** Every common action is a keybinding, not a ceremony.

### 6.1.1 Live cockpit stream requirements

Morpheus must behave like the place the user stays while many agents run, not a
static mission registry. The Matrix visual language is only successful when it
carries operational signal.

Required live-stream behavior:

- The Matrix rain surface shows **real terminal output shards** from active
  sessions embedded in the falling rain, not a static terminal log next to or
  below decorative rain.
- The stream mix is relevance-ranked: selected session first, then blocked /
  crashed / working / recently active sessions.
- Matrix texture appears as the base layer of the stream field. Live session
  output falls through it as bright readable shards, so the panel feels like one
  combined Matrix rain made from all active sessions.
- Web searches, tool progress, intermediate summaries, final responses, build
  output, prompts, and errors must appear in Morpheus quickly enough that the
  user can understand what is happening without switching tabs.
- The mission card and selected live stream must stay coupled: moving selection
  updates the card and the visible transcript tail for that same session.
- The right-side mission card may summarize or overlay durable intent, but it
  must never replace the raw recent terminal tail. If the card is stale or
  unset, the transcript tail still gives the user immediate situational
  awareness.
- The expected flow is: stay in the Morpheus tab, watch live streams, move with
  `j`/`k` or arrows, press `Enter` only when direct interaction with the real
  iTerm tab is needed, then return to Morpheus.
- Future owned-PTY support must preserve the same contract: Morpheus is the
  live observation and control surface; per-session shells remain instantly
  attachable.
- The bottom white-rabbit strip is a ticker, not just an error log. When a
  session finishes or closes, it should show a short headline from the latest
  substantive terminal output so completed work arrives as skimmable ticker
  items.
- Agent sessions that finish a response and return to an idle prompt should also
  produce a ticker headline. A hard process exit is not required; `working →
  idle` with new substantive output counts as "ready for review." The cockpit
  should also emit that ready headline when it observes a recently idle session
  whose transition was already written by another watcher, such as the launchd
  daemon.
- Ready/completed headlines must summarize the latest assistant answer block,
  not merely the last visible terminal line. They should ignore Codex prompt
  chrome, model/status lines, web-search trace lines, source URLs, and
  separator rules, then select a one-sentence response headline. A future
  background LLM summarizer may improve this, but the cockpit must never block
  on a synchronous summarization call.
- The ticker display order is newest-first. A new ready/completed/headline item
  should appear at the top of the rabbit strip so the freshest thing demanding
  attention is visible without scrolling.
- Passive terminal discovery and successful project cleanup/admin bookkeeping
  should not appear in the white-rabbit ticker. Generic shell/app tabs such as
  `Python"`, `zsh`, or `Terminal` may still appear in the mission table when
  observed, but the ticker should stay reserved for meaningful work signals.

Acceptance test: start a Codex session that performs a web search, keep focus
in Morpheus, and verify the cockpit shows the search/tool progress and latest
response tail in the live stream or selected card before pressing `Enter` to
attach.

### 6.2 Keyboard and function map

| Key | Function | Required behavior |
|---|---|---|
| `j` / `k` or arrows | Move selection | Moves through sessions without changing focus in iTerm |
| `Enter` | Attach / focus / inspect | Jumps to the selected iTerm tab or owned PTY session; on `LOOP` rows, opens the loop manager preselected to that loop |
| `n` | New mission | Opens goal + command form; creates mission card before launch |
| `b` | Brief selected | Shows a short "why / status / next step" card for selected session |
| `e` | Edit mission | Edits goal, why, plan, next step, tags, linked PR, worktree |
| `Space` | Toggle mission details | Expands/collapses metadata under the selected mission's latest output |
| `a` | Answer prompt | Drafts a response for the selected blocked session; sending is ask-first with preview |
| `s` | Snapshot | Writes transcript + mission card to `~/.morpheus/snapshots/` |
| `r` | Resume fresh | Spawns a new session seeded with the snapshot + mission card |
| `/` | Note / claim | Posts a note or path/worktree claim |
| `l` | Loop control | Creates a recurring prompt loop routed to ticker/context or the selected mission |
| `Shift+L` / `L` | Manage loops | Opens the current project's loop manager with edit and run-history controls |
| `w` | Worker | Spawns a manual child worker under the selected PRD run/coordinator/worker |
| `Shift+G` / `G` | Goal run | Starts or inspects an autonomous goal run for the selected PRD/mission; status/pause/resume/clear stay in-cockpit |
| `p` | Prune | Archives stale/finished sessions after confirmation |
| `d` | Dismiss / close | Closes selected live tab or archives an already-dead mission |
| `g` | Go to alert | Cycles through current 🐇 alerts |
| `?` | Help | Shows the keymap in-place |
| `q` | Quit dashboard | Leaves daemon and tab-title updates running |

Strong requirement: if a function changes session lifecycle or sends text into
another session, it must write an action ledger entry. If it spends money, it
must write a cost ledger entry.

### 6.3 Secondary surface: tab titles

Every iTerm tab still gets a smart title, refreshed every ~5 seconds:

| Prefix | Meaning |
|---|---|
| 🟢 | Actively emitting output |
| 🟡 | Idle (process alive, no recent output) |
| 🔴 BLOCKED: | Waiting for user input (known prompt pattern matched) |
| ⚫ | Finished (no activity for > 30 min) |
| 💀 | Crashed (matched a crash pattern) |
| `36h •` prefix | Stale (idle/finished and aged past threshold) |
| `▶ MORPHEUS` | The Morpheus cockpit itself (self-excluded from monitoring) |

Tab titles are intentionally lossy. They are allowed to encode state, age, and
short goal. They are not allowed to be the only place mission intent lives.

### 6.4 CLI commands

The CLI remains the scriptable surface for the same mission model:

| Command | Purpose |
|---|---|
| `morpheus` | Launch the cockpit in the current tab |
| `morpheus watch` | Run the tick loop in the foreground |
| `morpheus spawn "<goal>" "<cmd>"` | Open a new iTerm tab, run the command, register mission |
| `morpheus list` | Print every registered mission with state, age, last event |
| `morpheus prune [--older-than 4h]` | Interactively close stale tabs |
| `morpheus snapshot <tab_prefix>` | Dump a tab's mission + buffer to markdown |
| `morpheus context [--format md/json/short]` | Print the shared cross-session snapshot |
| `morpheus activity [--format table/json/short] [--refresh]` | Print cached live per-session headlines and transcript tails; `--refresh` forces an iTerm poll |
| `morpheus projects list/prune/delete/nuke` | Inspect known project tenants, remove empty tenants, or force-close and purge a project |
| `morpheus note "<text>"` | Post a cross-session note attached to the current tab |
| `morpheus notes [--limit 15]` | List recent cross-session notes |
| `morpheus brief` | Produce a morning/evening operational digest |
| `morpheus ask "<query>"` | Ask questions over current Morpheus state |
| `morpheus poll-prs` | One-shot PR review queue poll and optional draft spawn |
| `morpheus ledger costs/actions` | Inspect cost and action ledgers |
| `morpheus run find-prds [root]` | List Markdown source candidates in a worktree |
| `morpheus run start <prd> [--cmd codex]` | Create a PRD parent mission, spawn one coordinator tab, and link it |
| `morpheus goal start <prd-or-mission> [--cmd codex] [--workers auto\|N]` | Promote a PRD or durable mission into an autonomous goal run with a controller and bounded worker fan-out |
| `morpheus goal list/status/continue/run-due/pause/resume/done/clear` | Inspect, nudge, budget-pause, complete, or clear a goal run without attaching to the controller session |
| `morpheus goal task-add/tasks/task-spawn/task-status` | Create bounded goal tasks, spawn visible workers, and roll up worker heartbeat/block/done status |
| `morpheus loops add/list/run/run-due/pause/resume` | Configure recurring prompt loops and execute due loops |
| `morpheus install-loop-runner / uninstall-loop-runner / loop-runner-status` | Install, remove, and inspect the launchd runner that wakes to execute due loops |
| `morpheus mcp serve` | Expose Morpheus state to agent tools |
| `morpheus doctor` | Diagnose iTerm2 + Python API connectivity |

### 6.5 PRD Runs

PRD Runs are the v0.8 product wedge: a durable parent mission created from a PRD
or spec file, with coordinator and worker sessions linked underneath it.

Conservative v1 behavior:

- The new-session flow shows Markdown source files from the selected worktree or
  current working directory, with PRD/spec-looking files sorted first.
- Markdown source discovery must be bounded and must not recursively scan broad roots
  such as `$HOME`, `/Users`, or `/`. If a selected tab reports a broad cwd,
  Morpheus falls back to the dashboard/project cwd before opening the modal.
- Choosing a PRD creates a parent `mission_memory` row with `source_kind=prd`,
  a source artifact, and a run status file under `~/.morpheus/runs/<mission>/`.
- Morpheus spawns exactly one coordinator tab and links it to the parent mission
  with a `coordinator` edge.
- The coordinator prompt tells the agent to read the PRD, propose worker slices,
  write status to Morpheus events/artifacts, and avoid automatic fan-out.
- Child workers are manual in v0.8. Automatic decomposition belongs in v0.9
  after the tree model and ownership boundaries feel correct.

Remaining v0.8 work:

- Add collapse/expand affordance and persisted tree state for PRD run rows.
- Improve per-child ownership, file paths, proof requirements, and blockers.
- Add a run status updater that writes mission events and keeps the status file
  aligned with the graph.

Implemented behavior: PRD parent rows render as virtual rows in the mission
table, coordinator/worker sessions render underneath them, and `w` spawns a
manually scoped worker linked to the same parent mission with a `worker` edge
plus assignment events. In v0.8.0a9, `d` on a virtual parent row archives the
run and closes live child tabs; `p` archives orphan parent rows that no longer
have live children.

### 6.5.1 Autonomous Goal Runs

Autonomous Goal Runs are the v0.9 evolution of PRD Runs. A user can select a
PRD, spec, or durable mission and say "make this the goal." By default the end
goal is completion of the PRD's acceptance criteria, with passing verification
and captured proof artifacts. The user can override the done definition, worker
count, command/provider, model, and budgets before the run starts.

Morpheus's goal mode is deliberately not just a provider CLI slash command. It
is a mission-graph primitive that may use Codex `/goal`, Claude `/goal`, Hermes
`/goal`, provider subagents, or plain `codex`/`claude` sessions underneath, but
the source of truth remains Morpheus.

Required behavior:

- Starting a goal run creates a durable `goal_runs` row, a parent mission edge,
  and exactly one visible controller session marked as `role=goal_controller`.
- The controller prompt reads the PRD/source mission, current graph state,
  acceptance criteria, claimed paths, existing workers, budget limits, and
  allowed autonomy level before doing any work.
- The controller decomposes the goal into bounded worker tasks, creates workers
  with explicit ownership scopes and verification requirements, and records
  edges from the goal run to each worker mission.
- Worker sessions are first-class Morpheus rows, not hidden implementation
  details. Each worker may use provider-native subagents internally for
  exploration, tests, or local decomposition, but its externally visible
  contract is still a Morpheus mission with owned paths, status, proof, and a
  summary.
- Worker write scopes must be disjoint by default. If the controller wants two
  workers to touch the same path, it must surface the conflict in the cockpit
  and wait for approval or serialize the work.
- The controller runs an explicit observe/evaluate/continue loop:
  1. observe current worker statuses, events, artifacts, and transcript
     headlines;
  2. reconcile results into the parent mission card and status file;
  3. run deterministic checks when specified by the PRD or worker contract;
  4. ask a cheap goal judge whether the done definition is satisfied by the
     surfaced evidence;
  5. continue, pause, escalate, or mark done.
- The goal judge can only judge evidence the controller surfaced in the graph
  and transcript. It should not silently inspect files or run tools itself.
  Deterministic checks beat model judgment whenever both exist.
- User messages and in-cockpit controls preempt queued continuation work.
  `status`, `pause`, and `clear` are control-plane operations that must be safe
  while the controller or workers are mid-turn.
- Goal runs have hard budgets: max controller turns, max wall-clock age,
  max active workers, max total workers, max spend, and optional max changed
  files. Hitting a budget auto-pauses and tells the user exactly how to resume,
  narrow, or clear the run.
- The controller may ask before spawning the first live worker. After the user
  approves the goal run's autonomy envelope, bounded worker fan-out inside that
  envelope is allowed without repeated prompts. Pushing, merging, external
  messaging, payments, and account actions remain outside the envelope forever.
- Completion requires a final controller summary, worker closeout summaries,
  proof artifacts, updated acceptance-criteria state, and a final judge/check
  event linked to the parent goal.

Cockpit and context requirements:

- The selected goal card shows objective, status, budgets, controller state,
  active workers, completed workers, blockers, latest judge reason, last proof,
  changed paths, next controller action, and "why not done yet."
- The PRD tree renders the goal controller directly under the PRD parent, with
  workers nested under the controller unless the user switches to a flat
  status-grouped view.
- The white-rabbit ticker receives concise headlines for goal start, worker
  spawn, worker heartbeat, worker block, worker completion, judge continue,
  budget pause, and goal completion.
- `morpheus ask "what is the status of the PRD goal?"` answers from the graph,
  status file, activity cache, events, and artifacts without requiring the user
  to attach to transcripts.
- Context files and MCP expose goal run state so any live agent can ask what the
  controller and sibling workers are doing before touching files.

Failure handling:

- False-negative judge decisions are bounded by the turn/spend budgets and can
  be cleared manually once the user trusts the evidence.
- False-positive judge decisions are mitigated by requiring explicit proof
  artifacts and controller-reported checks before `done`.
- A crashed worker returns to `blocked` or `ready_for_retry` with its last
  heartbeat, changed paths, output path, and retry notes.
- A blocked worker posts a durable blocker event and appears under the goal card
  before the controller spawns replacement work.
- If the controller exits or its tab closes while the goal is active, Morpheus
  keeps the goal row visible and offers `resume` using the provider-native
  resume path or a fresh controller seeded from the goal graph.

Acceptance tests:

- Selecting a PRD and pressing `Shift+G` creates a goal run, controller mission,
  status file, graph edges, and a visible tree row without modifying the PRD
  itself.
- With a PRD that has three independent acceptance criteria, the controller
  spawns bounded workers, each worker owns a disjoint scope, and their heartbeat
  and completion events roll up to the goal card and white-rabbit ticker.
- While workers are running, `morpheus goal status <goal_id>` and `morpheus ask`
  can explain progress, blockers, budgets, and next action without reading raw
  transcripts.
- `morpheus goal pause <goal_id>` stops future continuation turns without
  killing already-running worker commands; `resume` restarts the controller
  loop with a fresh budget window; `clear` removes the active loop while leaving
  historical mission graph records intact.
- A run is not marked done until the PRD done definition, acceptance checklist,
  worker summaries, and verification artifacts all agree.

Non-goals:

- No unbounded recursive worker fan-out. Nested provider subagents may happen
  inside a worker, but Morpheus caps visible workers and treats the controller
  as the only entity allowed to spawn new Morpheus workers.
- No auto-merge, auto-push, auto-approve, or external side effects.
- No hidden background swarm. If work is happening, it has a row, role, budget,
  and inspectable status.

Implementation status:

- 2026-05-21: Autonomous goal runs implemented locally. Morpheus now stores
  `goal_runs` and `goal_tasks`, creates goal controller prompts/status files,
  renders goal state into the cockpit mission card, context files, MCP tools,
  and `morpheus goal status`, and links visible controller/worker sessions into
  the PRD tree with `goal_controller` and `goal_worker` edges.
- 2026-05-21: The controller loop is graph-backed and bounded. The watcher and
  cockpit can nudge idle controllers with a continuation prompt, `morpheus goal
  continue` can queue one turn manually, and `morpheus goal run-due` can be used
  as a one-shot runner. Turn budgets auto-pause exhausted goals; pause/resume/
  done/clear remain explicit control-plane operations.
- 2026-05-21: Worker fan-out is first-class. Controllers can create disjoint
  tasks with `task-add`, spawn visible worker tabs with `task-spawn`, and roll
  up heartbeat/block/done state with `task-status`. Overlapping claimed paths
  are rejected by default and recorded as goal path conflicts.
- 2026-05-21: Verification passed locally with focused goal-run tests,
  full unittest discovery, py_compile, CLI help smoke checks, and
  `git diff --check`.

### 6.6 Project Tenancy

Morpheus must support multiple project cockpits on the same machine without
splitting the mission graph into disconnected state stores. The default tenant
is the project implied by the directory where the user runs `morpheus`.

Required behavior:

- `cd ~/project-a && morpheus` opens a Project A cockpit.
- `cd ~/project-b && morpheus` opens a Project B cockpit.
- Each cockpit defaults to showing and controlling only missions whose terminal
  cwd belongs to that project tenant.
- The cockpit header must show the active project name and root path, plus a
  hidden-session count when other projects have live sessions outside the current
  scope.
- The cockpit must provide an in-place project switcher (`t`) with a global
  fleet option and every known project tenant so the user can recover if they
  launched Morpheus from the wrong cwd.
- The global mission graph remains one SQLite-backed store under `~/.morpheus/`
  so cross-project recall, proof, daemon observation, and collision detection
  keep working.
- Project identity is resolved from the nearest Git worktree root first, then
  known project markers (`pyproject.toml`, `package.json`, `Cargo.toml`,
  `go.mod`, `.git`, etc.), then canonical cwd.
- Existing live tabs are assigned to tenants by their iTerm-reported cwd on each
  tick. When cwd is unavailable, Morpheus may use a window-level cockpit binding
  as a low-confidence fallback, but the durable tenant assignment should be
  repaired once cwd is known.
- iTerm windows are useful visual boundaries and spawn targets, but they are
  not the source of truth. Tabs can move between windows; tenant identity lives
  in mission graph metadata.
- Spawned tabs from a project cockpit should start in that project's root and
  inherit the project tenant.
- CLI commands default to the current project tenant when run from a project
  directory: `morpheus`, `morpheus list`, `morpheus context`, and graph views.
- CLI commands need explicit global escape hatches: `--all` for fleet-wide views
  and, later, `--project <name-or-path>` for selecting another tenant.
- Context files should keep the existing global `~/.morpheus/context.md` while
  also allowing tenant-scoped context output for project-local agents.
- Project cleanup must be first-class. `morpheus projects list` shows every
  known tenant with live/session graph counts, `morpheus projects prune`
  removes only empty tenant rows, and `morpheus projects delete <project>`
  deliberately removes that tenant plus all related Morpheus-owned DB graph
  rows so no mission, memory, event, artifact, edge, note, loop, loop-run, or
  old action-ledger row is left orphaned.
- The cockpit project switcher must allow pruning empty tenants and deleting
  non-live tenant graph state with confirmation. Live sessions remain
  ask-first: close or prune live tabs before deleting the project tenant, or
  use an explicit CLI flag that closes live tabs first.
- The cockpit project switcher must show an explicit legend for project actions:
  `Enter` switches, `p` prunes empty tenants, `d` deletes non-live graph state,
  and `n` nukes an active project by closing its live tabs before purging all
  related Morpheus DB rows. Nuke requires a second confirmation inside the
  palette.

Non-goals for the first tenant implementation:

- Do not create one SQLite database per project.
- Do not store durable Morpheus state inside project repositories.
- Do not make iTerm window identity the only tenant boundary.
- Do not hide cross-project collisions from the global daemon/cockpit.

Implementation status:

- 2026-05-20: Design accepted. Planned implementation is one shared DB with
  `project_tenants`, `tenant_id`, and `project_root` metadata plus tenant-scoped
  default filters in the dashboard, list, context, and graph surfaces.
- 2026-05-20: Implementation ~60% complete. Tenant resolver, shared DB schema,
  tick-time cwd assignment, CLI/list/context graph scoping, dashboard row/buffer
  scoping, and focused tenant tests are in progress locally.
- 2026-05-20: Implementation 100% complete locally. `make test` passed with
  compileall, 86 unit tests, and `git diff --check`.
- 2026-05-20: Follow-up UX fix implemented after dogfood. The cockpit header
  shows the active project root and hidden-session count, and `t` opens a
  project switcher with global fleet plus known tenants.
- 2026-05-20: Project cleanup follow-up 100% complete locally. DB-level tenant
  usage/purge primitives, `morpheus projects list/prune/delete`, project
  switcher prune/delete controls, and focused cleanup tests passed in
  `make test`. Dogfood pruned two empty `/private/tmp` tenants from the real
  Morpheus DB.
- 2026-05-20: Follow-up UX fix 100% complete locally. The project switcher now
  shows a visible action legend and supports `n` nuke for force-closing live
  project tabs before tenant cleanup; CLI also exposes `morpheus projects nuke`.

### 6.7 Prompt Loops

Prompt loops are recurring prompts that behave like small launchd-fed sensors or
workers. They are not always-on agents. Morpheus stores their schedule, target,
run history, output artifacts, and ticker summaries; the loop runner LaunchAgent
or another cron/launchd entry should call `morpheus loops run-due` to execute
due loops.

Required behavior:

- `l` opens a cockpit loop form. The selected mission becomes the default target
  when one exists; otherwise the loop reports to ticker/context only.
- New loops are due immediately and the cockpit starts the first run in a
  background task after creation. The UI must remain responsive while the
  command runs.
- Each loop stores: name, prompt, interval, command, owning project tenant/root,
  target mission/tab, active/paused state, next run time, last run status, and
  last summary.
- Project-scoped loop rows appear in the cockpit as `LOOP` rows so active,
  paused, due, and last-run state are visible even when a loop is not attached
  to a live mission.
- Selecting a `LOOP` row updates the mission card into a loop card with prompt,
  schedule, target, and recent run history. Pressing `Enter` on a `LOOP` row
  opens the loop manager preselected to that loop; pressing `e` edits that loop
  directly.
- Due loop execution captures stdout/stderr into
  `~/.morpheus/loops/<loop-id>/<timestamp>.txt`.
- Every run emits a one-line ticker note (`kind=loop`). If targeted, it also
  writes a `loop_output` mission event and attaches a `loop-output` artifact so
  other sessions can consume it through context, graph inspection, or MCP.
- Loop output is a pipe-like input to other sessions: it must be visible in
  `context.md`/`context.json`, linked to the target mission, and summarized in
  the rabbit ticker newest-first.
- The dashboard must not synchronously execute due loops. Long-running Codex or
  Claude calls belong in `morpheus loops run-due` invoked by the loop runner,
  launchd/cron, or run manually, or in the cockpit's nonblocking one-shot
  runner.
- `morpheus install-loop-runner` writes a separate LaunchAgent from the watcher:
  `com.morpheus.loop-runner` runs `morpheus loops run-due` on a fixed interval,
  logs to `~/.morpheus/loop-runner.log`, and records a wake beacon used by
  `morpheus loop-runner-status`.
- The built-in Codex loop command includes `--skip-git-repo-check` because
  launchd runs without an interactive trust prompt; legacy loops stored as plain
  `codex exec` are normalized at execution time.
- Minimum interval is 60 seconds to avoid accidental runaway spend.

Implemented cockpit controls:

- `Shift+L` / `L` opens the loop manager for the current project, with recent
  run history plus edit/join/output/run/pause/target/delete controls. If a `LOOP`
  row is already selected, the manager opens on that loop. Editing can change
  name, prompt, interval, and command without leaving the cockpit.
- The loop manager has separate loop and run tables. Pressing `Tab` switches
  between them; pressing `Enter` or `J` on a run joins/resumes that specific
  run in an iTerm tab. Active captured runs open a companion session seeded with
  the live output file; completed Codex runs resume the exact `codex exec`
  session id captured from the run output when available without sending an
  extra prompt, then fall back to a fresh companion session seeded with the
  captured output and stable loop-run mission id.
- Loop-run sessions and completed runs should remain visible only in the
  selected loop's run table and loop card; they must not render as separate
  rows, rain entries, or worktree-collision participants in the main mission
  table.
- Pressing `o` in the loop manager opens the selected captured run output and
  refreshes while that run is still marked running. Runs insert a `running`
  history row, stable mission id, and output path as soon as they start, so
  background loop work is inspectable before completion.
- Pressing `d` while the run table has focus deletes the selected completed run
  from history after confirmation. Output files remain on disk. Pressing `d`
  while the loop table has focus still deletes the whole loop after confirmation.
- Pressing `t` targets a loop to the mission selected before opening the loop
  manager. If no candidate mission was selected and the loop already has a
  target, `t` focuses that existing target mission instead of silently doing
  nothing.
- Pressing `r` in the loop manager runs the selected loop once immediately.

Future work:

- Optional session-backed loop runner where a loop can reuse a visible worker
  session instead of executing stateless command runs. The current implemented
  model stores captured command executions as loop-run artifacts, not reusable
  live tabs.
- Optional fan-out where a loop result can draft an instruction for a target
  session, with user approval before sending text.
- Background LLM summarization for long loop outputs, recorded with provenance
  and cost ledger entries.

### 6.7 Cross-session context

Two files maintained by the tick loop:

- `~/.morpheus/context.md` — human-readable markdown snapshot
- `~/.morpheus/context.json` — parseable JSON snapshot
- `~/.morpheus/activity.json` — cached live headline/tail snapshot for fast "what is everyone doing?" reads

Agents inside other tabs can read these to know what every other session is
doing. They can post notes back via `morpheus note "text"` and those notes
appear in everyone's next context refresh.

The markdown includes a `**[YOU]**` marker so a session can tell which row
is itself vs others, and a usage block explaining how to interact with the
shared state.

---

## 7. Architecture

### 7.1 Process model (current v0.6)

```
                              ┌─────────────────────┐
                              │  morpheus cockpit   │ ← one iTerm tab
                              │  Textual dashboard  │
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

In v0.6, launchd can own the background tick loop while the cockpit renders
the same SQLite-backed state. The cockpit may still co-run ticks when the daemon
is not installed, but that is a fallback, not the primary model.

### 7.1.1 Target process model (v0.7)

v0.7 keeps the iTerm bridge, but promotes the mission model above the tab
model. A tab can disappear; the mission should not.

```
                   ┌──────────────────────────────┐
                   │ launchd morpheus daemon      │
                   │ poll / detect / trigger / log│
                   └──────────────┬───────────────┘
                                  │
        ┌─────────────────────────▼─────────────────────────┐
        │ ~/.morpheus/morpheus.db                           │
        │ missions + graph + notes + events + artifacts     │
        │ edges + ledgers                                   │
        └──────────────┬────────────────────┬───────────────┘
                       │                    │
        ┌──────────────▼────────────┐ ┌─────▼────────────────┐
        │ terminal cockpit          │ │ iTerm bridge          │
        │ Matrix streams + cards    │ │ tab titles / focus    │
        └──────────────┬────────────┘ └─────┬────────────────┘
                       │                    │
        ┌──────────────▼────────────────────▼───────────────┐
        │ agent sessions: managed new sessions + imported tabs│
        └────────────────────────────────────────────────────┘
```

The v0.7 architectural rule: all new sessions created by Morpheus must create
or update the mission graph first, then launch the command. Imported iTerm tabs
can remain best-effort observed sessions, but managed sessions get durable
mission graph memory, provenance, loop phase, and proof tracking.

### 7.2 Module layout

| Module | Responsibility |
|---|---|
| `morpheus/cli.py` | Typer entry points |
| `morpheus/core.py` | The tick loop and `_tick()` |
| `morpheus/dashboard.py` | Textual cockpit with live session streams, Matrix texture, mission table, mission card, alerts, keybindings |
| `morpheus/db.py` | SQLite schema, `Mission`, `MissionMemory`, `MissionEvent`, `MissionArtifact`, `MissionEdge`, `Note`, CRUD |
| `morpheus/loops.py` | Prompt loop interval parsing, due-run execution, output capture, ticker/graph publication |
| `morpheus/detect.py` | State classifier from pane buffer |
| `morpheus/iterm_client.py` | Thin async wrapper over iterm2 Python API |
| `morpheus/naming.py` | Tab-title formatting, goal inference |
| `morpheus/context.py` | Cross-session snapshot builders |
| `morpheus/daemon.py` | launchd install/uninstall/status and beacon integration |
| `morpheus/brief.py` | Morning/evening state digest |
| `morpheus/ask.py` | Conversational query over Morpheus state |
| `morpheus/trigger.py` | GitHub PR polling and draft session spawn |
| `morpheus/ledger.py` | Cost and action ledger tables |
| `morpheus/config.py` | `~/.morpheus/config.toml` defaults and loader |
| `morpheus/mcp_server.py` | MCP tools exposing sessions, mission graph read/update, notes/claims, spend/actions |
| `morpheus/mission_graph.py` | v0.7 graph helpers: provenance, edges, stale/lint checks |
| `morpheus/prd_runs.py` | v0.8 PRD run helpers: PRD discovery, parent mission creation, coordinator prompt/status files |
| `morpheus/goals.py` | v0.9 goal-run helpers: controller creation, worker fan-out planning, judge/check reconciliation, budgets, pause/resume/clear |
| `morpheus/proof.py` | v0.7 proof artifact capture and last-check summaries |

### 7.3 State schema

**`missions` (live session attachments)**

| Column | Type | Notes |
|---|---|---|
| `tab_id` | TEXT PK | iTerm-assigned tab ID |
| `mission_id` | TEXT | Stable mission ID, nullable for imported unknown tabs until claimed |
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

**`mission_memory` (v0.7 durable mission node)**

| Column | Type | Notes |
|---|---|---|
| `mission_id` | TEXT PK | Stable ID that survives tab replacement |
| `title` | TEXT | Human-readable short mission name |
| `why` | TEXT | Why this session exists |
| `done_definition` | TEXT | What "done" means for this mission |
| `acceptance_criteria` | TEXT | Bulleted or JSON checklist |
| `current_plan` | TEXT | Short plan or checklist |
| `next_step` | TEXT | The next human or agent action |
| `last_decision` | TEXT | Most recent meaningful user/agent decision |
| `last_summary` | TEXT | LLM or user summary of transcript state |
| `blocked_on` | TEXT | Prompt, missing info, external dependency, or review |
| `phase` | TEXT | planning / editing / testing / reviewing / blocked / done_needs_human / archived |
| `confidence` | REAL | 0.0-1.0 summary confidence; low confidence means "read transcript" |
| `source_kind` | TEXT | user / transcript / inferred / imported |
| `source_ref` | TEXT | PRD path, issue URL, transcript span, snapshot path, or null |
| `epic_ref` | TEXT | Optional CCPM/agtx/spec epic reference |
| `issue_ref` | TEXT | GitHub issue/PR/task reference |
| `last_verified_at` | REAL | Last time checks/proof were captured |
| `claimed_paths` | TEXT | JSON list of paths this mission is touching |
| `topic` | TEXT | Optional topic/thread grouping |
| `created_at`, `updated_at` | REAL | Lifecycle |
| `archived_at` | REAL | Set when live tab is gone but mission remains |

**`mission_events` (append-only timeline)**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTO | |
| `mission_id` | TEXT FK | |
| `ts` | REAL | Unix timestamp |
| `kind` | TEXT | state_change / decision / blocker / prompt / answer / check / summary / archive / resume / loop_output / goal_start / goal_judge / goal_pause / goal_done / worker_heartbeat |
| `actor` | TEXT | user / morpheus / codex / claude / shell / imported |
| `summary` | TEXT | Short event summary |
| `source_ref` | TEXT | Transcript span, snapshot path, command, or URL |
| `metadata_json` | TEXT | Structured extra data |

**`mission_artifacts` (proof and outputs)**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTO | |
| `mission_id` | TEXT FK | |
| `kind` | TEXT | snapshot / diff / test / build / pr / issue / doc / screenshot / log |
| `path_or_url` | TEXT | Local path or external URL |
| `status` | TEXT | pending / pass / fail / unknown |
| `summary` | TEXT | Human-readable artifact summary |
| `created_at` | REAL | Unix timestamp |

**`mission_edges` (local knowledge graph)**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTO | |
| `from_id` | TEXT | Mission/topic/artifact/entity ID |
| `to_id` | TEXT | Mission/topic/artifact/entity ID |
| `relation` | TEXT | relates_to / blocks / supersedes / duplicates / touches / proves / spawned_from / goal_run / goal_controller / goal_worker |
| `reason` | TEXT | Why the edge exists |
| `created_at` | REAL | Unix timestamp |

**`goal_runs` (v0.9 autonomous controller state)**

| Column | Type | Notes |
|---|---|---|
| `goal_id` | TEXT PK | Stable goal-run ID |
| `parent_mission_id` | TEXT | PRD/source mission the goal belongs to |
| `controller_mission_id` | TEXT | Visible controller session mission |
| `tenant_id` | TEXT | Owning project tenant |
| `project_root` | TEXT | Worktree/root used for the controller session |
| `source_kind` | TEXT | prd / mission / issue / user |
| `source_ref` | TEXT | PRD path, mission ID, issue URL, or user text |
| `objective` | TEXT | Standing end goal |
| `done_definition` | TEXT | Explicit completion condition |
| `status` | TEXT | active / paused / blocked / done / failed / cleared |
| `autonomy_level` | TEXT | observe_only / ask_to_spawn / bounded_fanout |
| `max_turns`, `turns_used` | INTEGER | Controller continuation budget |
| `max_workers`, `active_workers` | INTEGER | Fan-out budget and live count |
| `max_spend_usd`, `spent_usd` | REAL | Optional spend budget |
| `judge_model` | TEXT | Cheap evaluator model/provider label |
| `last_judge_reason` | TEXT | Most recent continue/done/pause rationale |
| `last_continued_at` | REAL | Unix timestamp |
| `created_at`, `updated_at`, `finished_at` | REAL | Lifecycle |

**`goal_tasks` (v0.9 worker contracts under a goal)**

| Column | Type | Notes |
|---|---|---|
| `task_id` | TEXT PK | Stable worker task ID |
| `goal_id` | TEXT FK | Owning goal run |
| `worker_mission_id` | TEXT | Linked worker mission, nullable until spawned |
| `title` | TEXT | Human-readable worker assignment |
| `scope` | TEXT | Owned files, modules, or responsibility boundary |
| `status` | TEXT | planned / ready / running / blocked / done / failed |
| `claimed_paths` | TEXT | JSON list of worker-owned paths |
| `verification` | TEXT | Required checks/proof for this worker |
| `last_heartbeat_at` | REAL | Unix timestamp |
| `result_summary` | TEXT | Final handoff summary |
| `metadata_json` | TEXT | Changed files, tests, blockers, retry notes |
| `created_at`, `updated_at` | REAL | Lifecycle |

Strong requirement: `mission_memory` and the graph tables are not nice-to-have.
Without them, Morpheus still tells the user "something is happening" but cannot
answer "what was I trying to accomplish, why, with what proof, and what is
connected to it?"

Provenance rule: user-authored fields beat transcript-derived fields, which
beat LLM-inferred fields. The UI must show low-confidence inferred summaries as
untrusted hints, not as durable truth.

**`notes`**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTO | |
| `tab_id` | TEXT | Source tab, nullable |
| `session_id` | TEXT | Source iTerm session, nullable |
| `text` | TEXT | The note body |
| `kind` | TEXT | note / claim / broadcast / loop |
| `created_at` | REAL | Unix timestamp |

**`prompt_loops`**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTO | |
| `name` | TEXT | Human-readable loop label |
| `prompt` | TEXT | Prompt passed to command |
| `interval_seconds` | REAL | Stored interval; minimum 60s |
| `command` | TEXT | Command prefix/template, e.g. `codex exec --skip-git-repo-check` or `claude -p {prompt}` |
| `tenant_id` | TEXT | Owning project tenant for dashboard visibility and cleanup |
| `project_root` | TEXT | Owning project root path |
| `target_mission_id` | TEXT | Optional mission to receive events/artifacts |
| `target_tab_id` | TEXT | Optional live tab to attach ticker context |
| `status` | TEXT | active / paused |
| `last_run_at`, `next_run_at` | REAL | Scheduler timestamps |
| `last_run_status`, `last_summary` | TEXT | Last run outcome |
| `created_at`, `updated_at` | REAL | Lifecycle |

**`prompt_loop_runs`**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTO | |
| `loop_id` | INTEGER | Parent prompt loop |
| `started_at`, `finished_at` | REAL | Run timestamps |
| `status` | TEXT | success / failed / timeout |
| `exit_code` | INTEGER | Process exit code when available |
| `output_path` | TEXT | Captured stdout/stderr artifact |
| `summary` | TEXT | Ticker headline |
| `mission_id`, `tab_id`, `session_id` | TEXT | Stable run mission id plus optional attached iTerm session |
| `agent_kind`, `resume_ref`, `resume_command`, `resume_confidence` | TEXT | Provider resume metadata, exact for Codex when `session id:` is captured |
| `target_mission_id`, `target_tab_id` | TEXT | Routing snapshot |

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

### 8.1 Current progress tracker

This table is the source of truth for where the product stands right now.

| Area | Status | Evidence / next step |
|---|---|---|
| v0.6 runtime foundation | Shipped | iTerm observation, Textual cockpit, launchd, notifications, briefings, trigger spawn, ledgers, MCP |
| PRD strategic pivot | Done in v0.6.2 | Product stance now says Mission Graph Cockpit, not tab-title manager |
| Competitive research | Done in v0.6.2 | CCPM, Claude Code, Karpathy LLM Wiki, and open-source session managers folded into requirements |
| Local dev launch flow | Implemented in v0.7.0 foundation | `Makefile` creates `.venv`, installs editable checkout, reloads daemon, opens cockpit |
| Quickstart/architecture README | Implemented in v0.7.0 foundation | README now documents `make start`, architecture, mission graph, state files |
| User PATH CLI install | Implemented in v0.8.0a11 | `make install-cli` links `~/.local/bin/morpheus` to this repo's editable venv command so `morpheus` can launch from any worktree without activating `.venv` |
| Stable mission ID design | Implemented in v0.7.0 foundation | `missions.mission_id` added; live tabs attach to durable mission IDs |
| Mission graph schema | Implemented in v0.7.0 foundation | `mission_memory`, `mission_events`, `mission_artifacts`, `mission_edges` added |
| Provenance model | Foundation implemented | Graph fields store source kind/ref and confidence; UI trust treatment still pending |
| Loop phase / proof tracking | Foundation implemented | `phase`, `last_verified_at`, events, artifacts exist; selected cockpit card now displays phase/events/artifacts |
| Mission card panel | Implemented in v0.8.0a10 | Right-side Textual card defaults to mission title/status plus a prominent latest-output block; `Space` expands graph fields, events, and artifacts underneath |
| Live session streams | Implemented in v0.7.0a3 | Dashboard captures real terminal tails from selected/relevant sessions; v0.7.0a5 changes the visual treatment from static tails to Matrix rain shards |
| Session-end rabbit ticker | Implemented in v0.7.0a4 | Finished sessions now emit bottom-strip completion headlines from the latest substantive terminal output and store a mission summary event when possible |
| Matrix rain output shards | Implemented in v0.7.0a5 | Left panel is rain-first again: real terminal output is embedded as falling bright shards inside the Matrix rain instead of rendered as a static terminal tail |
| Dense Matrix rain baseline | Implemented in v0.8.0a15 | Decorative columns seed into the visible viewport, refill on resize, move faster, and render dim ambient glyphs outside active tails so the left panel keeps an operator-screen rain texture even when output shards are sparse |
| Rain performance path | Implemented in v0.8.0a16 | Animation uses cached mission rows instead of hitting SQLite every 120ms, updates live buffers without forcing a second render, and skips rain-column reconciliation when the mission signature has not changed |
| Adaptive low-load rain | Implemented in v0.8.0a17 | Default rain cadence drops to 2 FPS, shard text parsing runs only when live buffers change, headline/tail scans are capped to recent terminal output, and slow render frames trigger short animation backoff so the cockpit remains usable |
| Low-FPS rain repaint guard | Implemented in v0.8.0a18 | Rain now renders at 0.5 FPS by default and table refreshes update mission/card state without forcing rain repaint or shard parsing, removing the hidden duplicate render loop that made the cockpit unresponsive |
| Zoom-safe compact layout | Implemented in v0.8.0a19 | Command-plus/iTerm font zoom now triggers a compact header/alert layout, dashboard panes fill the available body height instead of collapsing, and newly spawned sessions refresh the mission list immediately |
| Closed-session provider resume | Implemented in v0.8.0a22 | Archived missions keep provider resume metadata; closed rows remain selectable and `r` opens a new iTerm tab with exact Codex session IDs when the terminal exposed one, then types the Morpheus recovery prompt into the resumed CLI. Pressing `d` or `p` on a closed row now dismisses that resumable attachment from the dashboard without deleting graph history |
| Robust self-tab exclusion | Implemented in v0.7.0a6 | Dashboard passes its own tab/session IDs into the watcher; core also recognizes the Morpheus screen by buffer if iTerm leaves the title as `Python"` |
| Ready-response rabbit ticker | Implemented in v0.8.0a2 | `working → idle` now emits a `ready [...]` headline by extracting the latest assistant answer block, skipping Codex chrome/separators/source URLs, and compressing it to one sentence |
| Idle ticker reconciliation | Implemented in v0.8.0a23 | The dashboard reconciles recently idle observed sessions into the white-rabbit ticker even if another watcher updated SQLite before the cockpit saw the state transition |
| White-rabbit ticker noise reduction | Implemented in v0.8.0a24, expanded in v0.8.0a34 | Successful focus actions, closed-row dismiss/prune bookkeeping, project cleanup confirmations, and passively observed generic shell tabs stay silent so the ticker remains reserved for work signals, failures, collisions, Morpheus-launched spawns, loops, and ready/completed summaries |
| Cached activity snapshot | Implemented in v0.8.0a25 | The watch loop writes `~/.morpheus/activity.json` with per-session headline and tail lines so agents can answer what live sessions are doing from a cached file; `morpheus activity` reads that cache instantly and `--refresh` forces a live iTerm poll only when needed |
| Newest-first rabbit ticker | Implemented in v0.8.0a3 | Bottom alert strip redraws from the newest-first alert deque so fresh session headlines stay at the top instead of appending chronologically |
| Prompt loops foundation | Implemented in v0.8.0a4 | `l` creates recurring prompt loops; `morpheus loops run-due` runs due prompts, captures output, publishes ticker notes, and routes graph events/artifacts to target missions |
| Prompt loop cockpit visibility | Implemented in v0.8.0a27 | Loops now store owning project tenant/root, render as `LOOP` rows in the mission table, and `Shift+L`/`L` opens the current project's loop manager so active/due/last-run state is visible without running loop commands inside the dashboard |
| Prompt loop cockpit editing/history | Implemented in v0.8.0a28 | The loop manager shows run counts and recent run history, explains when no runner has executed yet, edits name/prompt/interval/command in-cockpit, and backfills legacy targeted loops into their project tenant |
| Loop row inspect/edit UX | Implemented in v0.8.0a29 | Selecting a `LOOP` row renders a loop card with prompt/config/history; `Enter` opens the loop manager preselected to that loop, and `e` edits the loop instead of falling into mission-edit errors |
| Immediate loop first run | Implemented in v0.8.0a30 | New loops are due at creation and the cockpit starts the first run in a background task; the loop manager also exposes `r`/run-now for existing loops while recurring execution remains cron/launchd-friendly |
| Loop runner LaunchAgent | Implemented in v0.8.0a31 | `morpheus install-loop-runner` installs `com.morpheus.loop-runner`, a separate launchd job that wakes every interval to run due loops without blocking the watcher; status/log/beacon commands mirror the watcher daemon |
| Loop run output inspection | Implemented in v0.8.0a32 | Loop runs now record a `running` history row and output file at start, stream command output to that file, and expose `o` in the loop manager to inspect/follow selected run output; `t` now targets/focuses missions instead of reading like a run join |
| Loop run join/resume | Implemented in v0.8.0a33, exact Codex resume in v0.8.0a35, cleanup UX in v0.8.0a36, dashboard leak cleanup in v0.8.0a37 | The loop manager now has selectable run rows; `Enter`/`J` joins or resumes the selected run in an iTerm tab, run rows keep stable `looprun_<loop>_<run>` mission ids plus attached tab/session ids, completed `codex exec` runs store exact session ids from captured output and resume without an injected prompt, loop-run sessions/memories stay out of main dashboard rows, rain, and collision alerts, `d` deletes selected run history, and `o` follows selected run output |
| PRD Runs foundation | Implemented in v0.8.0a1 | PRD finder, new-session PRD selector, parent mission creation, coordinator prompt/status files, `morpheus run start`, and coordinator graph edge shipped |
| PRD run tree UI | Partially implemented in v0.8.0a5 | Shows virtual PRD parent rows with coordinator/worker sessions rendered underneath them; collapse/expand remains future polish |
| PRD child worker spawn | Implemented in v0.8.0a5 | `w` spawns a manual child worker under the selected PRD parent/coordinator/worker with scope and verification prompts |
| Autonomous PRD goal runs | Foundation implemented | `goal_runs`/`goal_tasks`, `morpheus goal start/status/pause/resume/clear`, rendered controller prompt/status files, `Shift+G` dashboard start flow, controller graph edges, budgets, and status/proof rollups. Automatic continuation and worker fan-out execution remain v0.9 follow-up work |
| Nonblocking PRD picker | Implemented in v0.8.0a6 | `n` uses a bounded PRD scan and refuses broad roots like `$HOME`, preventing the dashboard from freezing before the new-session modal opens |
| Markdown source picker | Implemented in v0.8.0a7 | The `n` picker shows all discovered `.md`/`.markdown` files rather than only PRD-named files, while sorting PRD/spec candidates first |
| Edit mission flow | Implemented in v0.8.0a8 | `e` opens a dashboard editor for goal/title/why/done/criteria/plan/next/phase/blocker/source/issue/PR/worktree/claimed paths/topic, saves graph memory + live fields, and records a `mission_edit` event |
| Brief selected | Implemented in v0.8.0a8 | `b` opens a cited local brief for the selected mission using graph memory, recent events, artifacts, and transcript tail |
| PRD parent cleanup | Implemented in v0.8.0a9 | `d` on a virtual PRD parent archives the run and closes live coordinator/worker tabs; `p` archives orphan PRD parent rows with no live child tabs |
| Output-first mission card | Implemented in v0.8.0a10 | The selected card shows much more latest terminal output by default and moves mission/graph metadata behind the `Space` details toggle |
| User PATH CLI install | Implemented in v0.8.0a11 | `make install-cli` installs a safe user shim and prints a PATH hint when `~/.local/bin` is not visible to the shell |
| Resume fresh | Implemented in v0.8.0a12 | `r` snapshots the selected live tab, spawns a seeded replacement, links new -> old with `spawned_from`, and closes/archives the old tab after spawn |
| MCP mission tools | Implemented in v0.8.0a13 | MCP exposes durable graph list/show/update, event/artifact, and mission-link tools; spawn/kill remain outside MCP |
| Direct terminal broadcast | Implemented in v0.8.0a14 | `morpheus note --kind broadcast` records shared context and uses the iTerm API to type the message into selected live sessions |
| 48-hour recall eval | Not implemented | Add fixture or dogfood checklist: stale mission → press `b` → know next action in <10s |

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

### v0.7 — Mission Graph Cockpit (NEXT)

This is the strategic pivot from "better tab manager" to "AI-agent mission
control." v0.6 watches sessions well; v0.7 makes sessions explain themselves
and link themselves into a durable local knowledge graph.

Must ship:
- **Stable mission IDs** — missions survive tab closure, restart, snapshot,
  and resume. `tab_id` becomes an attachment, not the identity.
- **Mission graph schema** — `mission_memory`, `mission_events`,
  `mission_artifacts`, and `mission_edges` with migration from current
  `missions` rows.
- **Loop phase tracking** — each mission exposes `phase`, `blocked_on`,
  `last_verified_at`, checks run, and proof status so the cockpit shows where
  the agent is in the plan-act-observe loop.
- **Provenance-aware memory** — user-authored fields, transcript-derived
  evidence, and LLM-inferred summaries are stored separately; low-confidence
  inferred summaries are visibly marked.
- **Traceability links** — optional `source_doc`, `epic_ref`, `issue_ref`,
  `acceptance_criteria`, linked PR, branch, worktree, claimed paths, and proof
  artifacts.
- **Mission card panel** — selected session shows a compact identity line plus
  the latest terminal output first; `Space` expands the full graph card.
- **Edit mission flow** — `e` opens an inline form to correct goal, why,
  done definition, acceptance criteria, phase, next step, source links, linked
  PR, worktree, and claimed paths.
- **Brief selected** — `b` produces a terse cited "what this is / why it
  matters / what happened / what proof exists / what to do next" card from the
  mission graph and recent transcript.
- **Resume fresh** — `r` snapshots the selected session and spawns a new
  session seeded with the snapshot + mission graph card, then links old and new
  attachments with a `spawned_from` edge.
- **Closed-session provider resume** — when a tab disappears, persist
  `agent_kind`, `resume_ref`, `resume_command`, `resume_confidence`,
  `last_tab_id`, and `closed_at` on `mission_memory`. Closed resumable missions
  stay visible as dashboard rows. Pressing `r` opens a new iTerm tab and runs
  the provider-native resume path: Codex `codex resume`, Claude
  `claude --resume` / `--continue`, and Gemini `gemini` plus `/chat resume`
  when a checkpoint tag is known. Codex must capture the exact session ID from
  the terminal's `To continue this session, run codex resume <id>` line while
  the tab is still visible, and the Morpheus recovery prompt must be typed into
  the resumed CLI after launch rather than appended as an extra resume argument.
- **Archive instead of forget** — closing a tab archives the mission; it does
  not delete the historical record unless explicitly purged.
- **Topic threads** — group sessions by PR, feature, incident, or research
  topic; show per-topic status, stale work, and graph links.
- **MCP mission tools** — expose mission graph read/update tools; keep
  spawn/kill as ask-first actions.
- **Session-manager integrations** — link to CCPM/agtx/Agent Deck artifacts
  when present, but keep Morpheus's own state local and terminal-native.
- **48-hour recall eval** — dogfood and document the stale-session test: select
  an untouched mission after 48 hours, press `b`, and recover next action in
  under 10 seconds.

Success bar: after leaving a session alone for 48 hours, the user should be
able to select it, press `b`, and know exactly why it exists, what happened,
what proof exists, what it is connected to, and what to do next without reading
the transcript.

Not in v0.7:
- Web dashboard
- Multi-user/shared mode
- Full custom terminal multiplexer
- Automatic destructive actions
- Web-search topic watcher execution unless mission graph recall is already solid

### v0.9 — Autonomous Goal Runs

This is the move from "Morpheus can organize a PRD run" to "Morpheus can keep a
PRD implementation moving without the user typing keep-going prompts."

Must ship:

- **Goal run schema and CLI** — `goal_runs`, `goal_tasks`, `morpheus goal
  start/status/pause/resume/clear`, and graph/event/artifact integration.
- **In-cockpit start flow** — `Shift+G` on a PRD/mission opens an autonomy
  envelope form: objective, done definition, command/provider, max workers,
  turn/time/spend budgets, and whether bounded fan-out is allowed.
- **Visible controller session** — one controller tab owns decomposition,
  worker launch, reconciliation, judge/check decisions, and final synthesis.
- **Worker fan-out** — controller-created worker sessions get explicit scopes,
  isolated worktrees where possible, verification requirements, graph edges, and
  white-rabbit ticker updates.
- **Goal evaluation loop** — after each controller turn, Morpheus records
  surfaced evidence, runs deterministic checks when configured, asks a cheap
  evaluator for continue/done/block, and either queues the next controller turn
  or pauses/completes the run.
- **Status rollups** — selected goal card, PRD tree, context files,
  `morpheus ask`, MCP tools, and activity cache can explain progress without
  attaching to raw transcripts.
- **Budget and safety controls** — hard caps for turns, wall time, spend,
  workers, and changed paths; user preemption; no auto-merge/push/approve or
  external side effects.
- **Recovery** — if the controller closes, Morpheus keeps the goal row visible
  and resumes through provider-native resume metadata or a fresh controller
  seeded from the goal graph.

Success bar: select a PRD, approve a bounded goal run, leave it alone, and come
back to a cockpit that can answer what shipped, which workers are still active,
what proof exists, why the goal is or is not done, and what human decision is
needed next.

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

### 10.4 MCP integration

The Morpheus MCP server exposes `list_sessions()`, `get_session(id)`,
`list_missions()`, `get_mission(ref)`, `update_mission(ref, ...)`,
`add_mission_event(ref, ...)`, `add_mission_artifact(ref, ...)`,
`link_missions(from_ref, to_ref, ...)`, `get_context()`,
`get_context_short()`, `post_note(text)`, `claim_path(path)`, `daily_spend()`,
and `recent_actions()` as first-class tools. Claude Code and Codex can see and
update cross-session mission graph state without shelling out. Spawn/kill stay
out of MCP and remain ask-first actions.

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

**Mitigations**: re-claim the Morpheus title every tick; later, also detect by
checking if the tab is running the morpheus process (PID lookup).

### 11.10 Forgetting the daemon is running

If the daemon is not installed, the user may be relying on the cockpit's
co-running tick loop. If they close the cockpit, title updates stop silently.

**Mitigations**: `morpheus install-daemon` makes the watcher always-on, and
`morpheus daemon-status` reports the health beacon age.

### 11.11 False memory and summary drift

Mission summaries can become worse than useless if they hallucinate, compress
away the real blocker, or overwrite user intent with model guesses.

**Mitigations**: provenance columns, confidence score, append-only
`mission_events`, cited transcript/snapshot references, and UI labels that
distinguish user-authored truth from inferred summaries.

### 11.12 Commodity session manager trap

The space already has tmux/worktree dashboards, task boards, cost views, and
prompt-sending tools. Building only those primitives would make Morpheus a
late clone.

**Mitigations**: v0.7 prioritizes 48-hour mission recall, mission graph,
provenance, proof artifacts, and intent recovery before broader multiplexer
features.

---

## 12. Decisions & Open Questions

Resolved decisions from the adversarial review:

- **D1**: v0.7 uses a new durable graph layer rather than migrating all live
  tab state directly into `missions`. `missions` remains the live attachment
  table; `mission_memory`, `mission_events`, `mission_artifacts`, and
  `mission_edges` own durable recall.
- **D2**: The product name for v0.7 is **Mission Graph Cockpit**, not the old
  mission-memory cockpit label. Memory is necessary; graph/provenance/proof is the
  stronger wedge.
- **D3**: `a` never blindly answers a blocked prompt. It drafts a response,
  previews it, sends only after confirmation, and logs the action.
- **D4**: `b` starts manual-only. Automatic summaries come later, after
  provenance and confidence marking exist.
- **D5**: Archives store card fields + snapshot paths by default. Full
  transcript is captured by explicit snapshot or token-risk trigger.
- **D6**: Morpheus does not rebuild CCPM/agtx/Agent Deck. It links to their
  artifacts when present and owns recall across them.

Open questions:

- **Q1**: Should v0.7 export the mission graph as markdown wiki files in
  addition to SQLite? Recommendation: SQLite first, markdown export second.
  The graph needs reliable queries; markdown is excellent for review and git
  history but slower as the primary store.
- **Q2**: Should `morpheus spawn` require `why`, `done_definition`, and
  `acceptance_criteria`, or allow blank fields and prompt later?
  Recommendation: require them in the cockpit form, allow blanks only from CLI
  for speed.
- **Q3**: Should Morpheus eventually own PTY sessions rather than observing
  iTerm tabs? Recommendation: yes for managed sessions, but v0.7 should keep
  the iTerm bridge and first fix mission graph recall.
- **Q4**: Should graph linting be a command (`morpheus graph lint`) or part of
  `morpheus brief`? Recommendation: start as a command, then include top issues
  in the brief once noise is low.

---

## 13. Out of Scope (explicitly)

- iTerm replacement / re-implementation
- A custom terminal multiplexer
- Tab-title-only product direction
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

**08:30 — start the day in the cockpit**

```
morpheus    # ← in a dedicated iTerm tab
```

Cockpit opens. Streams show all 10 sessions; blocked/crashed/token-risk
sessions are grouped at the top. The selected mission card shows why the
overnight session exists, what it was trying to finish, and the exact prompt
blocking it. You hit `Enter`, iTerm jumps to that tab, you resolve it, then
come back to morpheus with `⌘+1`.

**09:15 — new PR review request lands**

(With v0.5 spawn-from-trigger:) Daemon detects the new PR, creates a
draft session in a worktree, 🐇 alert: *"draft session ready for PR
#225 — attach or dismiss"*. You navigate down with `j`, hit `Enter`,
review and approve. Three keystrokes total.

**14:00 — context lost on a long-running codex**

Tab 7 has been chewing on x402 testing for 90 minutes; you have no recall.
Move to it with `j`, press `b`, and Morpheus shows: goal, why it matters,
current plan, last decision, blocker, claimed paths, and suggested next step.
Full recall in 2 seconds.

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
| `morpheus activity [-f table/json/short] [--refresh]` | Print cached live activity headlines and tails |
| `morpheus note "<text>" [--tab ID] [--kind note/claim/broadcast]` | Post a cross-session note; broadcasts also type into live iTerm sessions |
| `morpheus notes [--limit 15] [--tab ID]` | List recent cross-session notes |
| `morpheus brief [--out FILE] [--notify] [--no-llm] [--no-gh]` | Generate digest of current state via claude-p |
| `morpheus ask "<query>" [--no-llm]` | Ask morpheus about its own state (claude-p answer) |
| `morpheus poll-prs` | One-shot GH PR poll → 🐇 alerts (and draft sessions if config enables) |
| `morpheus ledger costs [-n 50]` | Recent LLM cost ledger entries + today's total |
| `morpheus ledger actions [-n 50]` | Recent action ledger (spawns/kills/notes/etc) |
| `morpheus install-daemon [--poll 5]` | Install + start the launchd background watcher |
| `morpheus uninstall-daemon` | Stop and remove the launchd agent |
| `morpheus daemon-status` | Report daemon health (running? last beacon? log size?) |
| `morpheus install-loop-runner [--interval 60]` | Install + start the launchd prompt-loop runner |
| `morpheus uninstall-loop-runner` | Stop and remove the loop runner LaunchAgent |
| `morpheus loop-runner-status` | Report loop runner health (loaded? last wake? log size?) |
| `morpheus mcp serve` | Start MCP stdio server for Claude Code / Codex |
| `morpheus doctor` | Diagnose iTerm2 + Python API connectivity |
| `morpheus version` | Print morpheus version |

## 15. Config schema (current + v0.7 target)

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

[mission_graph]              # v0.7 target
enabled = true
summary_confidence_floor = 0.65
markdown_export = false
auto_lint = false
require_spawn_why = true
require_spawn_done_definition = true
require_spawn_acceptance_criteria = true
proof_commands = ["test", "lint", "build"]

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
- CCPM — https://github.com/automazeio/ccpm
- Claude Code docs — https://code.claude.com/docs/en/overview,
  https://code.claude.com/docs/en/agent-sdk/agent-loop,
  https://code.claude.com/docs/en/memory,
  https://code.claude.com/docs/en/hooks,
  https://code.claude.com/docs/en/worktrees
- Karpathy LLM Wiki — https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- Open-source adjacent tools: https://github.com/smtg-ai/claude-squad,
  https://github.com/izll/agent-session-manager,
  https://github.com/fynnfluegge/agtx,
  https://github.com/asheshgoplani/agent-deck,
  https://github.com/chojs23/lazyagent,
  https://kolu.dev/,
  https://github.com/anomalyco/opencode,
  https://github.com/cline/cline,
  https://github.com/Aider-AI/aider

---

*"I can only show you the door. You're the one that has to walk through it."*
