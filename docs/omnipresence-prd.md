# Omnipresence Mode PRD

| Field | Value |
| --- | --- |
| Status | Draft for architecture review — no implementation yet |
| Owner | Morpheus |
| Surface | Core (feeds, loops, memory) + `plugins/g2-bridge` + simulator |
| Last updated | 2026-07-03 |
| Depends on | Bug-fix pass on bridge/simulator/loops/feeds (in flight) |

## 1. Summary

Omnipresence mode makes the G2 glasses an **ambient Morpheus surface**: when
the glasses are connected to the laptop (the compute engine, over Tailscale),
the default experience is not a conversation picker — it is a quiet stream of
short, sharp, relevant pushes. Morpheus runs background loops (web searches,
standing goals, watchers) on the laptop, filters their output through a
user-relevance judge backed by a durable memory file, and pushes only the items
that clear a threshold. Each push is one or two coherent sentences sized for
the G2 display.

Canonical example: Morpheus remembered from an earlier conversation that you
ran out of espresso beans. Your phone reports you are walking down a street. A
location-context loop finds a supermarket 50 m to the left carrying your usual
brand on promotion. The relevance judge scores the item against your memory
file (0.86 ≥ 0.7 threshold) and the glasses show:
`Supermarket 50m left: your espresso beans are on promo.`

## 2. Design principles

- **Laptop is the engine.** All loops, searches, judging, and memory live on
  the laptop. Glasses/phone are display + sensors. Nothing omnipresence adds
  requires a cloud service.
- **Omnipresence is the default mode of a connected G2.** Connecting the
  glasses to the bridge lands on the ambient feed. Conversations (today's
  project/session flow) remain one tap away; they are the drill-down, not the
  default.
- **Reuse the existing spine.** Loops are the background jobs. Feeds are the
  push stream (its docstring already names AR glasses as a client). The bridge
  is the transport. Omnipresence adds: context ingestion, a user memory file,
  a relevance judge, and feed delivery over the bridge.
- **Memory is a file the user owns.** Plain markdown, human-editable, updated
  by Morpheus with visible diffs — like Claude/ChatGPT memory but inspectable.
- **Push budget, not push firehose.** Thresholds, per-hour caps, and quiet
  hours keep the glasses calm. A dismissed push is a signal, not just a swipe.

## 3. Architecture

```text
 phone / G2 ──POST /api/context (location, ts)──▶ g2-bridge ──▶ context store (sqlite)
                                                                     │
 loops (launchd, every minute)                                       ▼
   ├─ standing loops: news, PRs, calendar, goals        candidate items (loop output)
   ├─ location loop: fires on material location change               │
   └─ memory-updater loop: mines recent interactions                 ▼
                                                        relevance judge (LLM, cheap)
 ~/.morpheus/memory.md ◀── visible updates ──┐            score vs memory + context
                                             │                       │ score ≥ threshold
                                             │                       ▼
                     interactions (Morpheus chat, G2 prompts)   feeds (on_threshold rule)
                                                                     │
                                                       g2-bridge GET /api/feed + SSE
                                                                     │
                                                        G2 idle screen: 1–2 sentences
```

### 3.1 Delivery: feed on the glasses (no new intelligence)

- Bridge gains `GET /api/feed` (poll, `after` cursor) and `feed` SSE events on
  the existing `/api/events` stream, backed by `morpheus feeds` (same pattern
  as the desktop server's `/api/feed` + SSE `feed` frames).
- **Decided: the stock Even client is the v1 target.** The feed is exposed as
  a pseudo-session row `feed:main` ("Morpheus Feed"): stock Even clients open
  every row as a conversation, so feed items stream like assistant messages
  with zero client changes. The simulator renders the same data, but only as
  the test harness — not as the product surface.
- **Reality check from the SDK research:** glasses mini apps are
  foreground-only ("Plugins are foreground-only on the glasses" — official
  FAQ). While the Morpheus feed row is the open app/conversation, the bridge
  can push new content at any moment with zero user action via the existing
  SSE/message contract (that is Even Terminal's own streaming model). There
  is no background wake for mini apps.
- **Any-time pushes with the app closed — notification mirroring:** the stock
  Even app mirrors phone notifications onto the glasses as foreground pop-ups
  (double-tap dismisses), with a per-app whitelist. The laptop can therefore
  reach the glasses at any time by sending itself a phone push (e.g. ntfy /
  Pushover) — no mini app open, no companion app. Caveats: no formatting
  control, the sender app must be whitelisted once, and dismissal is not
  observable by the bridge (no ack). Omnipresence uses this as the escalation
  channel for high-priority pushes and the open feed row as the rich channel.
- Item shape: title (≤200 chars, the push), body (detail on tap), priority,
  source ref, judge rationale in metadata. Display budget (G2 canvas is
  576×288, 4-bit green, ~400–500 chars per full screen, no font control):
  keep a push to **≤ ~220 chars** so it renders on one page without
  scrolling; use flicker-free `textContainerUpgrade`-style updates.

#### Simulator fidelity requirement

Past experience: tests passed on the simulator while the real G2 failed — the
simulator did not behave like the stock Even client. That must not repeat for
omnipresence. Before Phase 1 ships:

- Capture **real-device traffic fixtures** (exact request sequences, headers,
  polling cadence, and event handling from a stock Even client session) and
  replay them in the test suite; simulator-only green is not acceptance.
- Keep the simulator's request/polling behavior byte-aligned with the stock
  client wherever the stock behavior is known (session-row opening via
  history, single global message cursor, SSE vs poll fallback, message-id
  handling), and document every known divergence in one place.
- Respect the officially documented simulator gaps (evenhub-simulator 0.7.x
  README is explicit that it is "a supplement to, not a replacement for,
  hardware testing"): no status events, `eventSource` hardcoded to 1 (ring
  input untestable), IMU always null, list rendering re-implemented rather
  than shared firmware code, and looser image/size enforcement. Anything
  touching those must be hardware-verified. Firmware limits to encode in
  tests: ~999 bytes per text container, 63 bytes × 20 items per list,
  ~400–500 chars per full screen.
- Use the official testing ladder (simulator → local BLE hot-reload to real
  glasses → private `.ehpk` → beta) and the simulator's `--automation-port`
  HTTP API (screenshot/input/console) for scripted checks — already wired in
  `npm run sim:even`.
- Every omnipresence bridge feature lands with (a) simulator tests and (b) a
  scripted hardware smoke checklist against the stock client; a feature is
  "done" only after the hardware smoke passes.

### 3.2 Context ingestion

- `POST /api/context` on the bridge: `{kind: "location", lat, lon, accuracy,
  ts}` (later: `activity`, `battery`, `calendar_window`). Auth same as other
  writes; rate-limited; stored via a new `morpheus context` CLI/db surface
  (sqlite table `context_signals`, latest-per-kind view).
- **Decided: no companion app — the EvenHub mini app is the location
  courier.** EvenHub SDK **0.0.11** (published 2026-06-22) added first-class
  phone-location APIs for mini apps: `getAppLocation(options)` one-shot and
  `startAppLocationUpdates` / `onAppLocationChanged` continuous updates, with
  accuracy tiers and a `distanceFilter` in meters. Requirements: bump the
  app's SDK from 0.0.10 to 0.0.11, declare the `location` permission in
  `app.json`, and whitelist the bridge URL under the `network` permission so
  the mini app can `fetch()`-POST fixes to `/api/context`.
- Known caveats (from the SDK docs and community verification): location
  worked only for Hub-installed apps historically — QR-sideloaded dev builds
  got `PERMISSION_DENIED`, so validate the installed path; backgrounding is
  iOS-favorable (WKWebView usually keeps running) but Android may suspend the
  WebView — re-arm `startAppLocationUpdates` on foreground and treat
  continuous background location as best-effort.
- Optional gap-filler for app-closed periods (not required for v1): any
  generic GPS logger that can POST to a URL (iOS Shortcuts automations,
  Overland, OwnTracks) can feed the same `/api/context` endpoint at coarse
  cadence over Tailscale.
- Privacy: signals stay in the local sqlite; never included in web-search
  queries verbatim beyond coarse place names the location loop resolves.

### 3.3 User relevance memory

- `~/.morpheus/memory.md`: sectioned markdown (`## People`, `## Interests`,
  `## Current`, `## Never push`). User-editable at any time.
- A **memory-updater loop** (default: hourly) mines recent interactions —
  Morpheus desktop/CLI chats, G2 prompts and dismissals — and appends/edits
  entries with dated one-line facts (`2026-07-03: out of espresso beans;
  usual brand noted`). Every change is a visible diff (file is
  git-friendly); a `morpheus memory log` shows what changed and why.
- **Decided:** memory is a single user-level file, not per-project overlays.
  Missions keep their existing per-mission memory; omnipresence reads both
  (user memory + active mission cards) when judging.

### 3.4 Background work: user-defined loops only

**Decided:** loops are the control surface. Omnipresence never invents
background jobs — it consumes only loops the **user created and explicitly
routed** into the omni feed (a feed rule per loop is the opt-in). That is how
the user controls exactly what they get updates about: no rule, no push.

All background work rides the existing loop runner (fixed in the bug pass to
claim atomically, not drift, and not double-run):

- **Standing loops** — user-defined goals ("watch HN for X", "PRs needing my
  review", "calendar conflicts today"), plain `codex exec`/`claude` prompt
  loops as today, each opted into the feed with its own rule/threshold.
- **Location loop** — a *template* the user instantiates and configures
  (radius, place sensitivity, what kinds of finds are welcome). Event-ish:
  runs each minute but exits fast unless location moved materially (> N
  meters or new place) since last evaluation; then runs a bounded web/local
  search around the place, seeded with the top relevant memory entries.
- **Memory-updater loop** — also a template the user instantiates (see 3.3);
  it feeds the memory file, not the glasses, so it needs no feed rule.

`morpheus omni init` offers the templates interactively, but everything it
creates is an ordinary loop: visible in `morpheus loops list`, editable,
pausable, and deletable like any other.

### 3.5 Relevance judge and threshold

- New feed rule policy `on_threshold` (alongside always/on_change/on_match/
  on_failure): candidate summaries are scored 0–1 by a cheap LLM call with
  memory.md + latest context signals + recent-push history in the prompt;
  configurable threshold (default 0.7) and per-source overrides.
- **Decided:** the judge runs the same way every other Morpheus job runs —
  through the provider CLIs (`codex exec` by default, `claude -p` as the
  alternative), pluggable via config. No direct API calls; zero new
  dependencies or credentials.
- Judge writes `{score, rationale}` into item metadata so every push can
  answer "why did you show me this".
- Guards: max pushes/hour (default 6), optional quiet hours (off by default),
  dedupe against recent pushes, and a daily judge-cost cap in the ledger.
  Judge failure = no push (fail closed), logged to the feed's own health
  source.

### 3.6 Mode & controls

- `morpheus omni on|off|status` and `[omni]` section in `~/.morpheus/config.toml`
  (threshold, caps, quiet hours, judge command, location sensitivity).
- **Decided:** all knobs — thresholds, push caps, quiet hours — are
  configurable through the Morpheus app (desktop UI + config file + CLI).
  Quiet hours are supported but **off by default**; defaults are threshold
  0.7 and 6 pushes/hour.
- Bridge `/api/info` advertises `omnipresence: true` so clients land on the
  feed view by default when connected; conversations remain reachable via the
  existing project rows.
- Dismiss/expand on a push is reported back (`POST /api/feed/ack`) and feeds
  the memory-updater as a negative/positive relevance signal.
- **Decided gestures** (from the SDK research; the G2 exposes exactly four
  touch events to apps — click, double-click, scroll-top, scroll-bottom; no
  long-press): **single tap = expand** (cheap, reversible — an accidental
  temple brush at worst expands a push) and **double-tap = dismiss** (matches
  the OS-wide double-tap-to-dismiss convention users already know from
  notification pop-ups; two-stage, so brushes can't eat a push). Scroll pages
  within an expanded push. Never map dismiss to single tap. If the R1 ring is
  present, its events are distinguishable (`eventSource=2`) and are treated
  as deliberate: ring tap may dismiss directly. Acks only exist on the
  in-app channel — mirrored notifications have no observable dismissal.

## 4. Phases

| Phase | Deliverable | New code touches |
| --- | --- | --- |
| 0 | Bug-fix pass (bridge, simulator, loops, feeds) | done in parallel branch work |
| 1 | Feed on glasses: bridge feed endpoints, `feed:main` pseudo-session, simulator feed view | bridge + simulator only |
| 2 | Context ingestion: `/api/context`, context store, mini-app location via EvenHub SDK 0.0.11 (`location` + `network` permissions), simulator location control | bridge + mini app + small core |
| 3 | Memory: `memory.md` format, `morpheus memory` CLI, memory-updater loop template | core |
| 4 | Judge: `on_threshold` policy, omnipresence loop pack (location + standing), push budget | core + config |
| 5 | Default-on polish: omni mode flag in `/api/info`, dismiss feedback, quiet hours, notification-mirroring escalation channel (phone push), optional launchd unit for the bridge, pairing/per-device state before all-day exposure | bridge + core |

Each phase is independently shippable and reviewable; Phase 1 alone already
gives "loops push short lines to my glasses" with today's rules (always/
on_change/on_match/on_failure).

## 5. Decisions (review round 1)

1. **Judge runner**: through the provider CLIs like every other Morpheus job —
   `codex exec` default, `claude -p` alternative, pluggable via config. No
   direct API calls.
2. **Location source**: prefer the official G2/Even Hub SDK path so no
   separate companion app is needed — see §3.2 for what the SDK research
   found and the resulting v1 mechanism.
3. **Client**: stock Even client is the v1 target; the simulator exists to run
   tests and must be held to device-fidelity requirements (see §3.1) because
   simulator-green/device-broken has been a real pain point.
4. **Memory scope**: single user-level `~/.morpheus/memory.md`.
5. **Push acks**: use the least accident-prone G2 gesture available for
   dismiss vs expand — see §3.6 for the recommendation from the SDK research.
6. **Controls**: thresholds, push caps, and quiet hours all configurable via
   the Morpheus app; quiet hours off by default; starting defaults 0.7
   threshold, 6 pushes/hour.
