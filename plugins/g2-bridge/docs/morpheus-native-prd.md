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

The current bridge proves the transport shape: Even Terminal-style HTTP routes,
project selection, project-scoped Codex app-server threads, bounded final
transcript submission, message/history polling, event streams, and a local
simulator. It does not yet prove the full safety boundary. The next step is to
promote Morpheus from metadata helper to the source of truth for both the G2
information architecture and the G2 write policy.

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
- The bridge keeps selected project/session state in process-global memory, so
  a second phone, simulator, retry, or future paired device can mutate another
  client's target.
- The current default write path can send final transcript text to Codex before
  Morpheus pairing, per-device routing, cost/autonomy policy, and provider
  gating are proven.
- The fallback Morpheus terminal prompt path can type text into iTerm; that is
  not acceptable as a default glasses path.

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
- Stores navigation, active sessions, aliases, caches, and idempotency in one
  in-memory process state rather than per paired device.

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
4. A user can submit bounded final transcript text as an operator note in the
   read/status MVP, and later as a prompt only after pairing, per-device
   context, Morpheus policy gates, and provider gating are proven.
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

## 6.1 MVP Safety Posture

The MVP must be read/status/note-first:

- Read: projects, attention cards, sessions, goals, loops, briefs, and cleaned
  latest output.
- Write: bounded operator notes to a selected mission or project.
- Blocked by default: prompt submission, session spawn, terminal prompt
  fallback, goal continuation, loop run-now, loop join, and loop-run resume.

Prompt/spawn/goal/loop writes can graduate only after these prerequisites are
implemented and covered by tests:

- per-device pairing state and revocation
- per-device selected project/session/category state
- Morpheus config, ledger, autonomy, and cost policy checks
- provider gating for Codex app-server health, cwd, session id, and version
- Host-header validation and loopback/public-host allow-listing
- outbound redaction for history, assistant output, SSE, and terminal output
- idempotency tests for every mutating endpoint
- two-client tests proving one device cannot mutate another device's target

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

MVP should make goal rows read-only: list and inspect. Continue, pause, and
resume can graduate after pairing, policy, controller-liveness, cooldown, and
idempotency tests pass. Done/clear can follow only after interaction confidence
is proven.

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

MVP should support list, inspect, and read recent runs. Run-now, pause/resume,
join, and loop-run resume can graduate after pairing, loop policy, target
selection, exact resume metadata, and simulator tests pass.

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

During the read/status/note MVP, preserved write routes have compatibility
semantics:

- `/api/transcript/finalize` accepts only `mode: "stage_operator_note"` unless a
  graduated write mode is explicitly enabled.
- `/api/prompt` returns `403 action_disabled` for `send_prompt` and
  `spawn_session` modes until pairing, policy, provider gating, and tests have
  graduated those modes.
- Legacy clients that omit `mode` are treated as `stage_operator_note` in the
  MVP when a valid note target is selected; otherwise they receive `409
  target_required`.
- Every mutating request must include `clientRequestId`, `mode`, `targetId`, and
  the latest `viewToken`.

### New Or Extended Routes

Use additive routes so stock clients keep working:

- `POST /api/pairing/start`
- `POST /api/pairing/complete`
- `POST /api/pairing/revoke`
- `GET /api/device/state`
- `GET /api/operations/:operationId`
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

### Device State

Every mutating endpoint must resolve the caller to a paired device identity
before reading or writing navigation state. The bridge may keep an in-memory
cache for speed, but the semantic model is:

- selected project is per device
- selected category is per device
- selected session/goal/loop is per device
- idempotency keys are scoped by device plus method plus route
- event streams are scoped by device plus explicit target plus view token

The global bridge process may expose shared read caches for project/session
rows, but it must not use process-global selected state as a write target.
Stateful reads also require paired device identity:

- `/api/messages`
- `/api/events`
- `/api/sessions/:id/history`
- selected-item detail routes

These routes must accept an explicit `targetId` or `viewToken` and must not fall
back to process-global selection. Revoking a device token terminates its event
streams and causes future stateful reads to return `401`.

### Pairing And Tokens

Pairing separates bootstrap bridge auth from device identity:

- The bridge owner starts pairing locally and receives a short-lived pairing
  code/QR.
- A device completes pairing and receives an opaque paired-device token plus a
  device id.
- Ordinary API calls use the paired-device token. The original bridge bearer
  token remains an owner/admin secret, not the long-lived device credential.
- Device tokens can be rotated or revoked. Revocation invalidates pending
  operations and closes SSE streams.
- The simulator must default to session-only storage for device tokens. Durable
  localStorage persistence is opt-in for development only.
- Unpaired clients are denied for all `/api/*` routes except pairing bootstrap
  and health/info routes explicitly marked public.

### Idempotency

Every mutating route requires a `clientRequestId` or `X-Request-Id`. The replay
record stores:

- method
- route
- device id
- mode
- source target ids
- destination target ids
- body hash
- operation id
- terminal status code
- terminal response body

The same id with the same payload replays the original response. The same id
with a different mode, target, or body hash returns `409 idempotency_conflict`
and performs no work. In-flight duplicates wait on the first operation instead
of launching a second operation.

### Operations

Longer writes return an operation resource:

- `operationId`
- `status`: `pending`, `running`, `succeeded`, `failed`, `cancelled`,
  `expired`
- `mode`
- `targetId`
- `pollUrl`
- `eventName`
- `createdAt`
- `expiresAt`
- `result`
- `error`

`202 Accepted` responses include the operation id, poll URL, and SSE event name.
Operations expire after a bounded TTL and keep enough terminal result metadata
for idempotent replay.
Target latency budgets:

- synchronous write acknowledgment: p95 under 2 seconds
- default prompt/finalize HTTP timeout budget: under 10 seconds
- polling interval: 1 second initially, backing off to 5 seconds
- SSE heartbeat: 15 seconds
- SSE reconnect token TTL: no more than 5 minutes
- operation retention for replay: at least 10 minutes

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

These are display examples, not parser contracts. Implementation ids should be
opaque, URL-safe typed ids containing canonical project/category scope. Clients
must not rely on prefix matching. Malformed ids return `400`; ambiguous or
cross-project refs return `409`.

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

All text writes must pass through explicit modes, and the mode set is phased.

MVP allowed mode:

- `stage_operator_note`: selected mission or selected project. Broadcast notes
  are disabled by default and require explicit non-voice confirmation plus
  Morpheus policy checks before graduation.

Later gated modes:

- `send_prompt`: selected live Codex-capable session, after provider and policy
  gates pass.
- `spawn_session`: selected project with no active session, after local policy
  allows remote spawn and cost/autonomy caps pass.
- `goal_continue`: selected live goal controller, bounded Morpheus continuation
  text, after budget and cooldown checks pass.
- `loop_run_now`: selected loop, no free-form text, after loop policy allows
  manual device runs.
- `loop_join`: selected loop and selected mission target, no free-form text.

Final voice transcripts remain untrusted. A voice transcript can provide text,
but it cannot prove approval for destructive or privileged actions.

Graduated write requests use this minimum schema:

```json
{
  "mode": "stage_operator_note",
  "targetId": "opaque-target-id",
  "viewToken": "opaque-view-token",
  "clientRequestId": "device-generated-id",
  "text": "bounded final transcript"
}
```

Privileged mode names such as `send_prompt`, `spawn_session`, `goal_continue`,
`loop_run_now`, and `loop_join` are feature-gated. If the mode is disabled, the
server returns `403 action_disabled` and records no operation.
Future join/run/continue requests include explicit source ids, target ids, and
the current `viewToken`; the server validates those ids against the paired
device state before executing.

`/api/prompt` should default to fast `202 Accepted` with pending state,
selected item ids, and polling/event-stream instructions. Long HTTP waits for a
final answer are allowed only as a compatibility mode for clients that cannot
poll or stream, and must still replay by request id without executing twice.

### Back/Interrupt

Back remains navigation-only by default. G2 double-tap/interrupt should never
kill or interrupt laptop work unless the user explicitly opts into that policy
and provider gating is proven.

## 11. Safety Requirements

- Token auth remains mandatory for all `/api/*` routes.
- Per-device pairing, revocation, and token rotation must land before any
  glasses-triggered prompt/spawn/goal/loop write is enabled by default.
- Query-token auth is disabled for ordinary routes by default; short-lived query
  tokens remain allowed for SSE/EventSource compatibility.
- CORS remains allow-list based.
- Host headers are validated against loopback hosts and the configured public
  URL. Binding to `0.0.0.0` requires an explicit unsafe flag.
- Rate limiting and request id replay remain enabled for writes.
- Audit logs remain metadata-only: no transcript text.
- Write responses include text hash and character count, not raw transcript in
  audit.
- All write routes require paired-device identity plus selected project,
  session, goal, or loop context.
- Navigation and write target state are scoped per paired device, not process
  global.
- Prompt and spawn routes must consult Morpheus config, ledger, autonomy, daily
  caps, project allow-lists, and denied action policy.
- Prompt routes require persisted session-to-project mapping. They must not fall
  back to `process.cwd()` or a stale last project.
- Codex app-server prompt and Morpheus terminal prompt are separate policy
  gates. Terminal prompt fallback is disabled for G2 by default. If it is ever
  enabled, it must prove the target is a managed Codex input prompt and require
  local confirmation.
- Goal and loop actions must call Morpheus helpers, not raw shell commands
  assembled from untrusted G2 text.
- Resume/join actions require opaque Morpheus-issued capabilities. A capability
  contains project, mission/session/run, provider, cwd/worktree, version,
  expiry, and the policy decision that authorized it. Stale, missing, expired,
  or ambiguous capabilities return `409` or `410`; glasses writes do not get a
  free-form fallback.
- Outbound redaction is centralized for transcript text, assistant output,
  history, SSE payloads, and terminal mirror output.
- Manifest capabilities, `/api/info` policy, row `allowedActions`, README
  documentation, and endpoint behavior are generated from or checked against one
  policy table.
- Canonical action names are defined once. Initial vocabulary:
  `read_projects`, `read_sessions`, `read_goals`, `read_loops`,
  `read_attention`, `read_detail`, `stage_operator_note`, `navigate_back`,
  `send_prompt`, `spawn_session`, `goal_continue`, `goal_pause`,
  `goal_resume`, `loop_run_now`, `loop_pause`, `loop_resume`, `loop_join`,
  `loop_run_join`, and `provider_interrupt`.
- Permission/question response endpoints stay blocked.
- Audio streaming endpoints stay 501 until a separate ASR PRD lands.
- Unauthenticated `/healthz` returns only readiness metadata, not selected
  project/session ids. Device-specific state lives behind authenticated
  `/api/device/state`.

## 12. Implementation Plan

### Phase 0: Document, Reconcile, And Freeze The Current Contract

- Land this PRD.
- Add a small architecture note to the bridge README linking to this PRD.
- Reconcile the current hardening note with reality: the bridge can currently
  submit final transcript text to Codex, so the docs must distinguish current
  compatibility behavior from the intended read/status/note MVP.
- Preserve existing tests while adding fixtures for current Morpheus snapshot
  rows with sessions, goals, loops, and attention cards.
- Record the current unsafe/default-on knobs that must change before hardware
  rollout: prompt/spawn defaults, terminal fallback, query tokens, global
  selected state, long HTTP waits, and missing Host validation.

### Phase 1: Safety And Pairing Prerequisites

- Add Host-header validation and reject unsafe bind hosts unless explicitly
  configured.
- Add per-device pairing identity, revocation, token rotation, and device-scoped
  navigation state.
- Scope idempotency keys by device plus route.
- Default ordinary query-token auth off; provide short-lived SSE tokens.
- Add Morpheus policy checks for all future writes: config, ledger, autonomy,
  daily caps, project allow-lists, and denied actions.
- Disable terminal prompt fallback for G2 by default.
- Centralize outbound redaction for history, assistant text, SSE, and terminal
  output.
- Add two-client tests proving isolated project/session selections and prompt
  targets.

### Phase 2: Morpheus Device Cockpit API

- Create or extend a compact JSON device schema that returns projects,
  attention cards, sessions, goals, loops, allowed actions, and policy metadata
  from Morpheus as first-class concepts.
- Treat the Even Terminal routes as an adapter over this schema, not as the
  product model.
- Add Python-side remote helpers before adding Node route glue when a helper
  would otherwise parse table output.
- Include policy metadata with every payload: raw buffers, destructive actions,
  approval authority, allowed write modes, stale/fallback status, and
  redaction status.

### Phase 3: G2 Morpheus Data Provider

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
- Cache entries include `snapshotVersion`, `viewToken`, `staleAt`, and
  `isStale`. Stale cached rows are read-only, and writes from stale views are
  rejected.
- Add provider-level tests with fake Morpheus JSON runners.

### Phase 4: Navigation Model

- Add category navigation rows inside selected projects.
- Keep stock Even behavior for `/api/sessions` and
  `/api/sessions/:id/history`.
- Extend `select-session` and history routing to understand typed row prefixes.
- Ensure stale polling from an old category cannot re-enter a different active
  view after the user navigates back.
- Store navigation view, selected item, and stream target per device.

### Phase 5: Session Detail And Native Codex Experience

- Normalize Morpheus sessions into Even-compatible rows without losing mission
  metadata.
- For known Codex app-server threads, keep current structured SSE behavior.
- For Morpheus/iTerm sessions, poll cleaned output through `morpheus remote
  output` and expose history/messages safely.
- Use `morpheus remote brief` for sessions without safe live output.
- Preserve prompt result fallback in `/api/prompt` response for runtimes that
  miss SSE.
- Change the default prompt response model to fast `202 Accepted`; keep long
  waits only behind compatibility config.
- Persist session-to-project mapping and remove `process.cwd()` prompt fallback.

### Phase 6: Goals

- Add goal list/detail routes and rows.
- Add goal pause/resume routes after pairing and policy checks.
- Add goal continue only after controller liveness, cooldown, budget, and
  local policy checks pass.
- Make continue use Morpheus continuation primitives only.
- Defer done/clear until hardware interaction confidence is high enough.
- Add tests for ambiguous goal refs, inactive controllers, budget exhaustion,
  duplicate request ids, and stale selection.

### Phase 7: Loops

- Add loop list/detail/run/pause/resume routes and rows.
- Add loop run history rows.
- Add join selected loop to selected mission.
- Add join/resume selected loop run only when exact resume metadata exists.
- Add tests for paused loops, due loops, missing targets, missing resume
  metadata, duplicate request ids, and run failures.

### Phase 8: Simulator And Hardware UX

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
- Add hardware acceptance criteria: real Even Hub traffic fixtures, exact event
  enum mapping, push-to-talk final transcript contract, display paging limits,
  reconnect/background behavior, and latency budgets.

## 13. Success Metrics

- G2 project overview renders in under 2 seconds on a local Tailscale path.
- A selected project shows sessions, goals, loops, and attention categories
  without needing the laptop UI.
- A blocked session can be found and opened from G2 in no more than four
  gestures from the project list.
- In the read/status MVP, a final transcript can be staged as a note and never
  reaches Codex as a prompt.
- After prompt mode graduates, a final transcript sent to an active Codex
  session returns a pending response within mobile-safe timeout budgets and an
  answer through event stream, messages, or history polling.
- Duplicate G2 writes with the same request id never execute twice.
- Two paired devices can select different projects and submit notes without
  cross-routing.
- Prompt/spawn/goal/loop writes are denied when Morpheus policy, caps, or
  project allow-lists deny them.
- Goal continue and loop run routes are covered by unit tests and simulator
  client tests.
- The bridge never exposes raw terminal buffers or approval endpoints.

## 14. Test Plan

- Node syntax check for `plugins/g2-bridge/src/server.mjs`.
- Existing `plugins/g2-bridge/test/server.test.mjs` suite.
- New route tests for Morpheus snapshot, category rows, goal actions, loop
  actions, attention rows, pairing, revocation, and device-scoped state.
- Security tests for bad Host headers, unsafe bind rejection, query-token
  defaults, rate limits, denied policy/caps, and stale project/session targets.
- Redaction tests for common token families in history, assistant output, SSE,
  terminal mirror output, and prompt response payloads.
- Prompt tests for fast `202`, no-result polling, client timeout retry, and
  duplicate request id replay.
- Idempotency conflict tests for same request id with different body, mode, or
  target.
- SSE tests for revoked device tokens, short-lived event tokens, reconnect, and
  heartbeat behavior.
- Stale-cache tests proving stale rows are read-only and stale allowed actions
  cannot execute.
- Stateful GET tests proving `/api/messages`, `/api/events`, and history do not
  leak between devices.
- Two-client tests where each client selects a different project/session and
  concurrent writes cannot cross-route.
- Fake Morpheus runner fixtures for projects, remote snapshot, goal status,
  loops, loop runs, remote brief, and remote output.
- Simulator client tests for category navigation and polling fallbacks.
- MVP manual hardware smoke:
  - start bridge behind Tailscale Serve
  - pair stock or simulator client
  - open project
  - inspect sessions/goals/loops/attention
  - stage a bounded operator note to a selected mission or project
  - verify prompt, spawn, goal continue, and loop writes return disabled
  - revoke device and verify reads/writes/SSE stop
- Privileged-write graduation smoke, after all gates and tests land:
  - send bounded prompt to selected session
  - trigger goal continue after budget/cooldown policy passes
  - run/pause/resume one harmless loop after loop policy passes

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
- Should prompt/spawn mode require local laptop confirmation even after pairing,
  or is paired-device plus Morpheus policy sufficient?
- Should bearer tokens ever be stored by the simulator, or should simulator auth
  be session-only to match the hardware pairing direction?
- Which action vocabulary should be canonical across manifest, `/api/info`,
  row `allowedActions`, and README docs?

## 16. Adversarial Review Log

### Round 1: Initial Product/Architecture Attack

Completed by a hostile review pass against the current bridge and this PRD.
Key findings incorporated:

- The hardening note said "operator note only" while current bridge behavior can
  send transcript text to Codex. The PRD now makes the MVP read/status/note-first
  and requires pairing, policy, and provider gates before prompt/spawn writes
  graduate.
- Process-global selected project/session state can cross-route multiple
  clients. The PRD now requires per-device pairing state, device-scoped
  selection, and two-client tests.
- G2 writes can bypass Morpheus autonomy/cost policy. The PRD now requires
  Morpheus config, ledger, caps, allow-list, and denied-action checks.
- Terminal prompt fallback is raw terminal input in disguise. The PRD now
  disables it by default for G2 and requires proof plus local confirmation if it
  is ever enabled.
- Pairing/revocation and Host-header hardening cannot remain deferred while
  writes are live. The PRD now moves both into prerequisites.
- Prompt routing cannot fall back to stale project state or `process.cwd()`. The
  PRD now requires persisted session-to-project mapping.
- Long HTTP waits are mobile-hostile. The PRD now prefers fast `202` pending
  responses with stream/poll follow-up.
- Outbound data needs centralized redaction, not scattered secret regexes. The
  PRD now requires redaction for all display/output channels.

### Round 2: Post-Revision Implementation Attack

Completed by two adversarial passes: one focused on API/rollout/idempotency and
one focused on security/device state. Key findings incorporated:

- Preserving `/api/prompt` and `/api/transcript/finalize` was ambiguous. The PRD
  now requires explicit `mode`, `targetId`, `viewToken`, and `clientRequestId`,
  and disabled privileged modes return `403 action_disabled`.
- Device state also affects reads. The PRD now requires paired identity and
  explicit target/view tokens for messages, events, history, and selected-item
  detail routes.
- Idempotency needed payload conflict semantics. The PRD now stores mode,
  targets, body hash, operation id, status, and response, and returns `409` for
  same-key/different-payload conflicts.
- `202 Accepted` needed an operation model. The PRD now defines operation
  fields, status enum, polling, SSE events, expiry, and latency budgets.
- Resume/join fallback was too loose. The PRD now requires opaque
  Morpheus-issued capabilities and returns `409` or `410` for stale, missing,
  expired, or ambiguous capabilities.
- Cached rows needed stale semantics. The PRD now requires snapshot versions,
  view tokens, stale timestamps, read-only stale rows, and write rejection from
  stale views.
- Row ids needed to be opaque. The PRD now treats prefix ids as examples only
  and requires URL-safe typed ids with canonical scope and no prefix matching.
- Hardware smoke still tested privileged writes too early. The PRD now splits
  MVP read/note hardware smoke from privileged-write graduation smoke.
- Security review added concrete details for short-lived SSE tokens, simulator
  session-only token storage, healthz non-leakage, separate Codex-app-server vs
  terminal prompt gates, and centralized outbound redaction.
