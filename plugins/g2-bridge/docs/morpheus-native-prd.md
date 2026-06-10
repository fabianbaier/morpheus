# Morpheus-Native G2 Cockpit PRD

| Field | Value |
| --- | --- |
| Status | Draft for implementation planning |
| Owner | Morpheus |
| Surface | Even Realities G2 bridge and simulator |
| Last updated | 2026-06-10 |
| Target branch | `codex/g2-morpheus-native-prd` |

## 1. Summary

The G2 bridge should become a compact Morpheus cockpit, not merely a Codex
remote control surface. A user wearing Even Realities G2 glasses should be able
to understand and steer the same Morpheus state they see on the laptop:
projects, live sessions, mission goals, active autonomous goal runs, prompt
loops, resume/join affordances, and recent attention cards. When the user enters
a session, the experience should feel like native Codex streaming, but the
session must remain orchestrated by Morpheus and attached to Morpheus mission
graph state.

The current bridge proves the transport and safety boundary: Even Terminal-style
HTTP routes, project selection, project-scoped Codex app-server threads,
bounded final transcript submission, message/history polling, event streams,
and a local simulator. The next step is to promote Morpheus from metadata helper
to the source of truth for the G2 information architecture.

## 2. Problem

The current bridge is useful for creating or continuing one G2 Codex thread, but
it does not yet expose the minimum Morpheus operating model:

- The user cannot browse Morpheus goals from G2.
- The user cannot browse, run, pause, resume, or join Morpheus prompt loops.
- The user cannot see the same attention-card summary that `morpheus remote
  snapshot` already provides.
- The user cannot resume or join old mission/loop context from glasses.
- The project/session rows do not distinguish "native Codex conversation" from
  "Morpheus mission with memory, graph edges, artifacts, and loop/goal links."
- The simulator validates the bridge transport, but not the richer Morpheus
  cockpit model.

This makes the bridge feel like "Codex on glasses with Morpheus nearby" instead
of "Morpheus on glasses with Codex as the inner agent runtime."

## 3. Current State

The current G2 bridge:

- Lists Morpheus project tenants through `morpheus remote projects`.
- Shows projects as Even-compatible session rows before a project is selected.
- Uses the Codex app-server backend by default for G2 conversations.
- Starts or reuses one project-active G2 conversation as
  `project-session:<projectId>`.
- Mirrors new Codex app-server threads into Morpheus/iTerm for laptop
  visibility when configured.
- Supports safe prompt and final transcript submission through `/api/prompt` and
  `/api/transcript/finalize`.
- Streams Codex structured events through `/api/events`, `/api/messages`, and
  `/api/sessions/:id/history`.
- Blocks permission responses, question responses, raw terminal keystrokes,
  destructive actions, and public tunnel defaults.

Morpheus already has related primitives outside the bridge:

- `morpheus remote snapshot`: compact device-friendly fleet state with sessions,
  active goal rows, attention cards, and policy metadata.
- `morpheus remote brief`: raw-buffer-free mission brief.
- `morpheus remote output`: cleaned latest output for one mission.
- `morpheus goal list/status/continue/pause/resume/done/clear`.
- `morpheus loops list/show/history/run/pause/resume/join/unjoin`.
- Dashboard actions for fresh resume, closed resume, loop run join, loop target
  join, and goal controller/worker workflows.

## 4. Product Thesis

G2 should be the fastest "where is my agent fleet and what needs me?" surface.
It should not try to replicate the full desktop dashboard on a tiny display.
Instead, it should expose a thin, glanceable Morpheus cockpit with a deep path
into one selected session.

The successful mental model is:

```text
G2
  -> Morpheus project
    -> Sessions
    -> Goals
    -> Loops
    -> Attention
      -> Selected item detail
        -> Native Codex-like stream when the item is a session
```

## 5. Goals

1. A user can recover the current Morpheus fleet state from G2 in under two
   seconds: blocked sessions, working sessions, active goals, due loops, and top
   attention card.
2. A user can browse Morpheus projects and, inside a project, switch between
   sessions, goals, loops, and attention cards.
3. A user can open a Morpheus session and see a native Codex-like stream:
   user prompt, assistant deltas/result, tool/status events when available, and
   final answer fallback through polling/history.
4. A user can submit bounded final transcript text as a prompt or operator note
   only after explicit project/session context is selected.
5. A user can resume or join Morpheus-managed work from G2 when Morpheus already
   has exact resume metadata or a safe join action.
6. A user can inspect goals and loops without granting glasses authority to
   approve commands, answer Codex permission prompts, push, merge, delete, kill,
   or type arbitrary terminal text.
7. The G2 simulator can exercise the same navigation and polling model without
   physical glasses.

## 6. Non-Goals

- Do not turn glasses voice into raw shell input.
- Do not expose Codex permission approval from glasses.
- Do not expose push, merge, delete, kill, force-push, external messaging, or
  arbitrary command execution.
- Do not build a full dashboard UI on G2. The screen is a glance and steering
  surface.
- Do not require public internet tunnels for first-class operation.
- Do not make live ASR streaming part of this PRD. Final transcript submission
  remains the write path until the Parakeet backend is explicitly wired.
- Do not duplicate all Morpheus dashboard features. Promote only the compact
  remote primitives that fit G2.

## 7. Users And Jobs

### Primary User

The primary user runs many Morpheus-managed Codex/Claude/shell sessions and
wants to check or steer them while away from the laptop screen.

### Jobs

- "What needs my attention right now?"
- "Which project am I in?"
- "What sessions are working, blocked, idle, or done?"
- "What was this session trying to do?"
- "Can I send this thought to the right session without touching the laptop?"
- "Can I continue the native Codex conversation for this mission?"
- "Can I see whether a goal run is active, paused, budget-tight, or failed?"
- "Can I see and join the latest loop run if it produced something worth
  pursuing?"

## 8. Information Architecture

### Project Row

Project rows remain the first-level entry point. Each row should include compact
usage hints:

- live session count
- active/paused goal count
- active/due loop count
- top urgent attention count

Selecting a project opens the project cockpit rather than immediately implying a
Codex conversation.

### Project Cockpit Rows

Inside a project, the bridge should expose stable navigation rows:

- `nav:sessions:<projectId>`
- `nav:goals:<projectId>`
- `nav:loops:<projectId>`
- `nav:attention:<projectId>`
- `project-session:<projectId>` when an active G2 conversation exists
- `nav:projects` / `project:__projects__` for stock-client back navigation

The existing project row id, `project:<projectId>`, should remain compatible
with stock Even clients, but its semantic meaning should be "open project
cockpit" rather than "this is the conversation."

### Session Rows

Session rows represent Morpheus missions. They should include:

- tab reference and mission reference
- state: blocked, crashed, working, idle, finished, unknown
- mission goal/title
- phase, next step, blocker, linked PR/worktree when available
- prompt behavior: `send_prompt`, `stage_operator_note`, `resume_available`, or
  `read_only`
- native stream availability: `codex_app_server`, `terminal_output`, or
  `history_only`

Selecting a session opens the best available session experience:

1. Native Codex app-server stream if the bridge knows an active Codex thread.
2. Morpheus terminal output polling if it is a Morpheus/iTerm session.
3. Raw-buffer-free mission brief and recent notes/events if no safe live stream
   is available.

### Goal Rows

Goal rows represent Morpheus `GoalRun` records. They should include:

- goal id prefix
- status
- objective
- turns used/max turns
- workers active/max workers
- autonomy level
- last judge reason or budget warning

Supported actions:

- inspect status
- queue one continuation only if the controller session is live and Morpheus
  policy allows it
- pause
- resume
- mark done or clear only if the user confirms through a non-voice action path
  that is explicit enough for a tiny device

MVP should make goal rows read-mostly: list, inspect, continue, pause, resume.
Done/clear can follow after interaction confidence is proven.

### Loop Rows

Loop rows represent Morpheus prompt loops. They should include:

- loop id
- status
- name
- interval and next due time
- target mission or ticker/context
- run count
- last summary

Supported actions:

- inspect loop detail
- run now
- pause/resume
- join target when a session is selected
- inspect recent runs
- join/resume a selected loop run when resume metadata is exact

MVP should support list, inspect, run now, pause/resume, and read recent runs.
Join/resume loop run is gated behind exact resume metadata and simulator tests.

### Attention Rows

Attention rows are derived from `morpheus remote snapshot` cards:

- blocked/crashed sessions
- recently idle sessions
- failed or paused goals
- tight goal budgets
- broadcast/goal notes

Selecting an attention row opens the underlying session/goal/loop detail when
there is a stable source reference.

## 9. API Shape

The bridge should preserve Even Terminal-compatible endpoints while adding
Morpheus-native rows and detail routes.

### Existing Routes To Preserve

- `GET /api/info`
- `GET /api/projects`
- `GET /api/sessions`
- `POST /api/select-project`
- `POST /api/select-session`
- `GET /api/navigation`
- `POST /api/back`
- `POST /api/navigation/back`
- `POST /api/prompt`
- `POST /api/transcript/finalize`
- `GET /api/messages`
- `GET /api/events`
- `GET /api/sessions/:id/history`

### New Or Extended Routes

Use additive routes so stock clients keep working:

- `GET /api/morpheus/snapshot?projectId=...`
- `GET /api/morpheus/goals?projectId=...`
- `GET /api/morpheus/goals/:goalRef`
- `POST /api/morpheus/goals/:goalRef/continue`
- `POST /api/morpheus/goals/:goalRef/pause`
- `POST /api/morpheus/goals/:goalRef/resume`
- `GET /api/morpheus/loops?projectId=...`
- `GET /api/morpheus/loops/:loopId`
- `GET /api/morpheus/loops/:loopId/runs`
- `POST /api/morpheus/loops/:loopId/run`
- `POST /api/morpheus/loops/:loopId/pause`
- `POST /api/morpheus/loops/:loopId/resume`
- `POST /api/morpheus/loops/:loopId/join`
- `POST /api/morpheus/loop-runs/:runId/join`
- `GET /api/morpheus/attention?projectId=...`
- `GET /api/morpheus/items/:id`

The route names are intentionally explicit. Internally they may call Python
remote helpers directly, shell through `morpheus`, or share a small JSON helper
module, but the public API should not leak CLI table formatting.

### Row Id Prefixes

Use stable typed ids so a tiny client can route selection without guessing:

- `project:<projectId>`
- `project-session:<projectId>`
- `nav:projects`
- `nav:sessions:<projectId>`
- `nav:goals:<projectId>`
- `nav:loops:<projectId>`
- `nav:attention:<projectId>`
- `session:<tabRefOrMissionRef>`
- `goal:<goalRef>`
- `loop:<loopId>`
- `loop-run:<runId>`
- `attention:<cardId>`

## 10. Interaction Model

### Read Path

1. User opens G2 bridge.
2. Bridge shows projects.
3. User selects a project.
4. Bridge shows project cockpit rows plus active conversation if one exists.
5. User selects sessions/goals/loops/attention.
6. Bridge shows rows in that category.
7. User selects an item.
8. Bridge shows item detail, stream, or history depending on item type.

### Write Path

All text writes must pass through one of these explicit modes:

- `send_prompt`: selected live Codex-capable session.
- `spawn_session`: selected project with no active session, if remote spawn is
  enabled.
- `stage_operator_note`: selected mission or broadcast note.
- `goal_continue`: selected goal controller, bounded Morpheus continuation text.
- `loop_run_now`: selected loop, no free-form text.
- `loop_join`: selected loop and selected mission target, no free-form text.

Final voice transcripts remain untrusted. A voice transcript can provide text,
but it cannot prove approval for destructive or privileged actions.

### Back/Interrupt

Back remains navigation-only by default. G2 double-tap/interrupt should never
kill or interrupt laptop work unless the user explicitly opts into that policy
and provider gating is proven.

## 11. Safety Requirements

- Token auth remains mandatory for all `/api/*` routes.
- Query-token auth remains allowed for SSE/EventSource compatibility, but can be
  disabled for ordinary routes.
- CORS remains allow-list based.
- Rate limiting and request id replay remain enabled for writes.
- Audit logs remain metadata-only: no transcript text.
- Write responses include text hash and character count, not raw transcript in
  audit.
- All write routes require a selected project, session, goal, or loop context.
- Goal and loop actions must call Morpheus helpers, not raw shell commands
  assembled from untrusted G2 text.
- Resume/join actions require exact Morpheus resume metadata or a documented
  safe fallback.
- Permission/question response endpoints stay blocked.
- Audio streaming endpoints stay 501 until a separate ASR PRD lands.

## 12. Implementation Plan

### Phase 0: Document And Fixture Current Contract

- Land this PRD.
- Add a small architecture note to the bridge README linking to this PRD.
- Preserve existing tests while adding fixtures for Morpheus snapshot rows with
  sessions, goals, loops, and attention cards.

### Phase 1: Morpheus Data Provider

- Extend `createMorpheusProvider` with typed methods:
  - `fleetSnapshot(projectId)`
  - `goalRows(projectId)`
  - `goalDetail(goalRef)`
  - `loopRows(projectId)`
  - `loopDetail(loopId)`
  - `loopRuns(loopId)`
  - `attentionRows(projectId)`
- Prefer compact JSON helpers over parsing rich CLI table output.
- Cache the last successful rows per project/category for offline-ish G2
  polling fallback.
- Add provider-level tests with fake Morpheus JSON runners.

### Phase 2: Navigation Model

- Add category navigation rows inside selected projects.
- Keep stock Even behavior for `/api/sessions` and
  `/api/sessions/:id/history`.
- Extend `select-session` and history routing to understand typed row prefixes.
- Ensure stale polling from an old category cannot re-enter a different active
  view after the user navigates back.

### Phase 3: Session Detail And Native Codex Experience

- Normalize Morpheus sessions into Even-compatible rows without losing mission
  metadata.
- For known Codex app-server threads, keep current structured SSE behavior.
- For Morpheus/iTerm sessions, poll cleaned output through `morpheus remote
  output` and expose history/messages safely.
- Use `morpheus remote brief` for sessions without safe live output.
- Preserve prompt result fallback in `/api/prompt` response for runtimes that
  miss SSE.

### Phase 4: Goals

- Add goal list/detail routes and rows.
- Add goal continue/pause/resume write routes with idempotency and audit.
- Make continue use Morpheus continuation primitives only.
- Defer done/clear until hardware interaction confidence is high enough.
- Add tests for ambiguous goal refs, inactive controllers, budget exhaustion,
  duplicate request ids, and stale selection.

### Phase 5: Loops

- Add loop list/detail/run/pause/resume routes and rows.
- Add loop run history rows.
- Add join selected loop to selected mission.
- Add join/resume selected loop run only when exact resume metadata exists.
- Add tests for paused loops, due loops, missing targets, missing resume
  metadata, duplicate request ids, and run failures.

### Phase 6: Simulator UX

- Add top-level category rendering to the simulator.
- Add rows for sessions/goals/loops/attention.
- Add gesture-safe action affordances:
  - click opens detail
  - back returns one level
  - transcript submit prompts or notes only in selected context
- Add simulator smoke tests for:
  - project -> sessions -> session stream
  - project -> goals -> continue
  - project -> loops -> run now
  - project -> attention -> underlying detail
  - stale polling after back navigation

## 13. Success Metrics

- G2 project overview renders in under 2 seconds on a local Tailscale path.
- A selected project shows sessions, goals, loops, and attention categories
  without needing the laptop UI.
- A blocked session can be found and opened from G2 in no more than four
  gestures from the project list.
- A final transcript sent to an active Codex session returns an answer through
  prompt response fallback even if SSE is unavailable.
- Duplicate G2 writes with the same request id never execute twice.
- Goal continue and loop run routes are covered by unit tests and simulator
  client tests.
- The bridge never exposes raw terminal buffers or approval endpoints.

## 14. Test Plan

- Node syntax check for `plugins/g2-bridge/src/server.mjs`.
- Existing `plugins/g2-bridge/test/server.test.mjs` suite.
- New route tests for Morpheus snapshot, category rows, goal actions, loop
  actions, and attention rows.
- Fake Morpheus runner fixtures for projects, remote snapshot, goal status,
  loops, loop runs, remote brief, and remote output.
- Simulator client tests for category navigation and polling fallbacks.
- Manual hardware smoke:
  - start bridge behind Tailscale Serve
  - pair stock or simulator client
  - open project
  - inspect sessions/goals/loops/attention
  - send bounded prompt to selected session
  - trigger goal continue
  - run/pause/resume one harmless loop

## 15. Open Questions

- Should G2 category rows be exposed through `/api/sessions` only, or should
  Even-compatible rows be a compatibility layer over new typed routes?
- Should goal continue be available from voice, or require a non-voice gesture
  plus a confirmation row?
- Should loop run-now be allowed from glasses before pairing/revocation exists?
- What exact G2 gesture maps to "confirm" without being too easy to trigger by
  accident?
- Should the bridge call Morpheus through CLI JSON commands, import Python
  helpers through a subprocess shim, or eventually share an HTTP bridge with the
  desktop server?
- Should native Codex app-server sessions be registered as Morpheus missions at
  creation time instead of only mirrored into iTerm best-effort?

## 16. Adversarial Review Log

### Round 1: Initial Product/Architecture Attack

Pending. This round should attack whether the PRD over-expands the bridge,
whether goals/loops belong on glasses, whether safety boundaries are sufficient,
and whether the navigation model can work on the G2 display.

### Round 2: Post-Revision Implementation Attack

Pending. This round should attack API ambiguity, idempotency, stale polling,
resume/join safety, test coverage, and rollout sequencing after the first round
has been incorporated.

