# Even Realities G2 Bridge

This plugin is the Morpheus-side home for Even Realities G2 work. It is designed
to stay as close as possible to Even Terminal while giving Morpheus a stricter
policy boundary.

For the planned evolution from "Codex on glasses" to a compact Morpheus cockpit,
see [docs/morpheus-native-prd.md](docs/morpheus-native-prd.md).

## Reference Path

First validate the stock upstream flow:

```bash
npm install -g @evenrealities/even-terminal@0.8.1
even-terminal --provider codex --tailscale
```

Even Terminal currently gives us the useful shape:

- local HTTP bridge
- QR/token pairing
- `GET /api/sessions`
- `POST /api/prompt`
- `GET /api/messages`
- `GET /api/events`
- provider abstraction
- Codex app-server-backed sessions

This plugin mirrors that API shape so the glasses-side client can be kept close
to Even Terminal. The main Morpheus difference is policy: G2 voice must not be
raw terminal input or approval authority.

## Local Development

```bash
cd plugins/g2-bridge
npm install
export MORPHEUS_G2_TOKEN="$(openssl rand -hex 24)"
npm start
```

By default the bridge binds to `127.0.0.1:3456`.

### Local Simulator Development

The `simulator/` app is a local Even Hub-style G2 client for developing without
physical glasses. It can run as an ordinary browser preview or inside the
official `evenhub-simulator`.

Fast mock loop:

```bash
cd plugins/g2-bridge
npm install
npm --prefix simulator install
npm run sim:mock
```

In another shell:

```bash
cd plugins/g2-bridge
npm run sim:dev
```

Open:

```text
http://127.0.0.1:5173/?bridge=http://127.0.0.1:3456&token=dev-token
```

Open the stock Even-style browser skin with:

```text
http://127.0.0.1:5173/?skin=even&bridge=http://127.0.0.1:3456&token=dev-token
```

The mock bridge exposes Morpheus project rows and a fake Codex provider, so the
simulator can select a project, submit a final transcript, create a session, and
watch streamed answer events locally.

Real bridge loop:

```bash
cd plugins/g2-bridge
export MORPHEUS_G2_TOKEN="$(openssl rand -hex 24)"
export MORPHEUS_G2_ALLOWED_ORIGINS="http://127.0.0.1:5173,http://localhost:5173"
MORPHEUS_G2_PUBLIC_URL="http://127.0.0.1:3456" npm start
```

Then run the simulator dev server and open the same URL with your real token.

Official Even simulator:

```bash
cd plugins/g2-bridge
npm run sim:dev
npm run sim:even
```

`sim:even` launches `evenhub-simulator "http://127.0.0.1:5173/?even=1"
--automation-port 9898`. The browser preview is still useful for transcript
entry and debug logs; the official simulator validates the G2 framebuffer and
input path.

Automated smoke coverage lives in `test/server.test.mjs` and drives the same
simulator client module against the bridge: project selection, transcript
submission, session creation, and streamed result polling.

Expose it privately to a phone on your tailnet:

```bash
tailscale serve --bg 3456
```

That gives the Even app / phone WebView a stable private HTTPS URL such as:

```text
https://your-mac.your-tailnet.ts.net
```

Start the bridge with that public URL so the QR points at the laptop, not the
phone's loopback:

```bash
MORPHEUS_G2_PUBLIC_URL="https://your-mac.your-tailnet.ts.net" \
MORPHEUS_G2_ALLOWED_ORIGINS="https://your-mac.your-tailnet.ts.net" \
npm start
```

`MORPHEUS_G2_PUBLIC_URL` is used exactly as the QR URL. Make sure it includes
the machine name. For example, use `https://fabians-macbook-pro.tail3387a8.ts.net`,
not `https://tail3387a8.ts.net`.

Do not use Tailscale Funnel, ngrok, or Cloudflare Tunnel for the first hardware
bring-up. Those are public-internet paths and need a stronger pairing/revocation
layer than this plugin exposes today.

## Current Server Behavior

The bridge uses Morpheus for project discovery and, by default, uses the Codex
app-server path from Even Terminal for G2 conversations:

- `GET /api/info` returns bridge and policy metadata.
- `GET /api/projects` lists Morpheus project tenants.
- `GET /api/sessions` returns projects as Even-compatible session rows before a
  project is selected. Once a project is selected, it returns a `Back to
  projects` row, the active G2 conversation as `project-session:<projectId>`,
  and the project's recent sessions: Morpheus-known terminal tabs plus recent
  Codex app-server threads in the project directory, so glasses can resume or
  join them. Set `MORPHEUS_G2_INCLUDE_CODEX_HISTORY=0` to hide old Codex
  threads again. The original `project:<projectId>` row is navigation-only
  after the project is open.
- `POST /api/select-project` pins the bridge to one project.
- `POST /api/select-session` pins the G2 bridge to one Codex session.
- `GET /api/navigation` returns the current bridge view: `projects`, `sessions`,
  or `session`.
- `POST /api/back` and `POST /api/navigation/back` return from a G2 session
  directly to the project overview by default. Set
  `MORPHEUS_G2_DIRECT_BACK_TO_PROJECTS=0` for a custom client that wants the
  older two-step flow: session -> project sessions -> project overview.
- Selecting the `Back to projects` row also returns to the project overview,
  including stock Even clients that open rows by fetching
  `/api/sessions/nav:projects/history`. Set `MORPHEUS_G2_SHOW_BACK_ROW=0` to
  hide this compatibility row.
- `POST /api/interrupt` is treated as the same non-destructive back action by
  default, for G2 gestures that map double-tap/back to interrupt. Set
  `MORPHEUS_G2_INTERRUPT_NAVIGATES_BACK=0` to restore the blocked interrupt
  response.
- `GET /api/events` streams Server-Sent Events. Native `EventSource` cannot set
  an `Authorization` header, so event-stream clients should pass
  `?token=$MORPHEUS_G2_TOKEN`; this endpoint accepts query-token auth even if
  query-token auth is disabled for ordinary API calls.
- `POST /api/prompt` submits bounded text to the selected session. If no session
  is selected, or the prompt targets a project row, it starts a Codex app-server
  thread in that project. Prompts that target the `Back to projects` menu row
  are routed the same way as sessionless prompts (the selected or last project
  decides between spawn and follow-up) instead of failing with `409
  selected_session_stale`. Follow-up prompts target the selected Codex thread.
  By default the bridge waits for the final Codex `result` event before
  returning, so G2 runtimes that miss Server-Sent Events still receive the
  answer in the prompt response body. The answer is included redundantly as
  `text`, `answer`, `message`, `response`, `output.text`, `history`,
  `messages`, and `activeMessages`. Tune that wait with
  `MORPHEUS_G2_PROMPT_WAIT_FOR_RESULT_MS` (default 90000), or set
  `MORPHEUS_G2_WAIT_FOR_RESULT=0` to return as soon as the Codex turn is
  launched and rely on `/api/events`, `/api/messages`, or history polling.
- By default, new app-server threads are also mirrored into a local Morpheus/iTerm
  tab with `codex --remote ws://127.0.0.1:$CODEX_APP_SERVER_PORT resume <thread>`
  so the laptop can watch the same session. Set
  `MORPHEUS_G2_MIRROR_CODEX_TUI=0` to disable that.
- `GET /api/messages` returns structured bridge messages from Codex app-server:
  `text_delta`, tool/status events, final `result`, then `status: idle`. This
  matches Even Terminal's Codex provider and does not depend on terminal output
  polling. Message ids come from one bridge-wide counter, so a client can keep
  a single `after` cursor while it moves between `project:<id>`,
  `project-session:<id>`, and real thread ids without skipping newer messages.
  Stream and poll messages are presented under the session id the client
  subscribed with (stock Even clients drop messages tagged with a foreign
  session id); the real codex thread id rides along as `activeSessionId`.
  While a turn is actively streaming `text_delta` events, history and
  terminal-mirror fallbacks hold so partial answers are never published as
  final results.
- Read requests (`GET`) have their own rate budget
  (`MORPHEUS_G2_RATE_LIMIT_READ_MAX`, default 600/min per client) so steady
  glasses polling of sessions/messages/status cannot exhaust the stricter
  write budget (`MORPHEUS_G2_RATE_LIMIT_MAX`, default 120/min).
- Client polls never block on slow terminal-mirror reads for more than
  `MORPHEUS_G2_CLIENT_POLL_OUTPUT_BUDGET_MS` (default 800). Past that budget
  the read keeps running in the background and publishes the mirrored text as
  soon as it lands, while the poll returns the buffered state immediately.
  Concurrent polls share one in-flight terminal read per session, and at most
  one budgeted read runs per poll request.
- Mirror tabs are re-attached after a bridge restart: Morpheus snapshot rows
  expose the exact `codex ... resume <id>` thread id, so existing mirror tabs
  keep feeding terminal output fallback and re-prompts do not spawn duplicate
  mirror tabs.
- `GET /api/sessions/:id/history` returns the recent user/assistant text for
  Even Terminal-compatible clients that refresh completed sessions through
  history instead of the live message stream. When the bridge has a buffered
  assistant answer, that buffered answer wins over stale/user-only Codex
  persisted history. `project:<projectId>/history` opens the project session
  list; `project-session:<projectId>/history` opens the active G2 conversation.
  Stock Even clients open every row as a conversation view, so project and
  `Back to projects` rows return a compact overview as history (the project's
  sessions, or the project list) instead of a blank screen; the overview is
  response-only and never enters the message buffers. Opening a concrete
  session row by history also selects that session on the bridge, so follow-up
  prompts resume it instead of spawning a new thread.
- `POST /api/transcript/finalize` accepts final voice transcripts through the
  same safe prompt path.
- `POST /api/permission-response` and `/api/question-response` are intentionally
  blocked for now.

Write clients should send `clientRequestId` or `X-Request-Id`. Duplicate explicit
request IDs return the original response instead of staging a second note. For
stock clients that omit an id, identical in-flight prompt posts are still
deduped for the duration of the running request, then the temporary id is
discarded so a later repeated utterance can run normally.

Set `MORPHEUS_G2_AGENT_BACKEND=morpheus` to use the older Morpheus/iTerm polling
backend. In that mode, prompts are sent to Morpheus-known Codex terminal tabs
and output completion uses bounded polling (`MORPHEUS_G2_OUTPUT_POLL_INTERVAL_MS`,
`MORPHEUS_G2_OUTPUT_POLL_ATTEMPTS`).

After a follow-up prompt, the previous answer is still on the terminal until
the TUI redraws, so the bridge holds mirror text that is byte-identical to the
pre-prompt screen instead of republishing it as the new answer. Identical text
is released once it survives `MORPHEUS_G2_STALE_MIRROR_GRACE_MS` (default
4000ms) with an idle tab, which keeps genuine repeat answers working. The
bridge also re-arms the Codex app-server thread subscription during client
polling, so turns typed directly into the laptop TUI keep streaming to the
glasses even after the upstream provider's idle sweep unsubscribes the thread.

The bridge starts (and connects to) the Codex app-server in the background at
startup, matching Even Terminal's own boot behavior, so the first glasses
prompt does not race a cold app-server start. Prompt submission additionally
retries while the app-server is still starting, for up to
`MORPHEUS_G2_CODEX_STARTUP_WAIT_MS` (default 30000). Set
`MORPHEUS_G2_WARM_CODEX_APP_SERVER=0` to skip the startup warm-up.

Set `MORPHEUS_G2_INCLUDE_CODEX_HISTORY=0` if you do not want the project
session list to include older Codex app-server threads (for example when
deleted/archived laptop sessions keep reappearing on the glasses).

The bridge prints `[g2-api]` request lines by default so hardware runs can show
whether the phone is reading `/api/messages`, `/api/events`, or
`/api/sessions/:id/history`. Set `MORPHEUS_G2_DEBUG=1` to add stream/session
routing logs such as event-stream connects and project-history live/menu
decisions. Set `MORPHEUS_G2_REQUEST_LOG=0` to quiet the ordinary request logs.

The bridge does not print bearer tokens. Set `MORPHEUS_G2_TOKEN` yourself and
store it somewhere private while pairing your phone-side client.

## Hardware Bring-Up

1. Validate stock Even Terminal on the G2 first:

   ```bash
   npm install -g @evenrealities/even-terminal@0.8.1
   even-terminal --provider codex --tailscale
   ```

2. Install Tailscale on the laptop and phone, sign both into the same tailnet,
   and keep the laptop awake.

3. Start the Morpheus bridge:

   ```bash
   cd plugins/g2-bridge
   npm install
   export MORPHEUS_G2_TOKEN="$(openssl rand -hex 24)"
   export MORPHEUS_G2_PUBLIC_URL="https://your-mac.your-tailnet.ts.net"
   export MORPHEUS_G2_ALLOWED_ORIGINS="$MORPHEUS_G2_PUBLIC_URL"
   npm start
   ```

4. In another shell:

   ```bash
   tailscale serve --bg 3456
   ```

5. From the phone/G2 client, call `GET /api/sessions`, choose a project row
   (`project:<id>`), then send final text to `/api/prompt` or
   `/api/transcript/finalize`. The active conversation then appears as
   `project-session:<id>`.

With the stock Even app, the first stream shows projects as session rows.
Opening a project row renders a session overview (the project's recent
sessions plus a resume/new-session hint), and the session list behind it shows
those sessions as rows. Open a session row to resume that session, or send a
prompt from the project view to create a Codex app-server session in that
project. The active conversation appears as `project-session:<projectId>` while
`project:<projectId>` remains the project/session-list row. Going back to
projects and re-opening the same project reuses the remembered active
conversation for follow-up prompts. The glasses receive the final answer from
structured Codex events, history/messages, and the completed `/api/prompt`
response; the local Morpheus/iTerm mirror is best-effort laptop visibility.

Example smoke test from the laptop:

```bash
curl -sS "$MORPHEUS_G2_PUBLIC_URL/api/sessions" \
  -H "Authorization: Bearer $MORPHEUS_G2_TOKEN"

curl -sS "$MORPHEUS_G2_PUBLIC_URL/api/projects" \
  -H "Authorization: Bearer $MORPHEUS_G2_TOKEN"

curl -sS "$MORPHEUS_G2_PUBLIC_URL/api/select-project" \
  -H "Authorization: Bearer $MORPHEUS_G2_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"projectId":"p_...","clientRequestId":"project-demo-0001"}'

curl -sS "$MORPHEUS_G2_PUBLIC_URL/api/select-session" \
  -H "Authorization: Bearer $MORPHEUS_G2_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sessionId":"abc123","clientRequestId":"select-demo-0001"}'

curl -sS "$MORPHEUS_G2_PUBLIC_URL/api/transcript/finalize" \
  -H "Authorization: Bearer $MORPHEUS_G2_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"please summarize where this task is stuck","clientRequestId":"utterance-demo-0001"}'
```

## Provider Backends

The route contract is shared by multiple backends:

- `codex_app_server` is the default. It reuses Even Terminal's Codex provider
  and app-server client for event-driven turn completion.
- `morpheus` is the fallback terminal backend for local Morpheus/iTerm sessions.
- `parakeet_mlx` is reserved for future ASR/audio chunks.

## Safety Defaults

- Even app compatibility accepts `?token=...` by default because the stock app
  uses that pattern for saved host probes and native `EventSource` cannot attach
  bearer headers. Set `MORPHEUS_G2_ACCEPT_QUERY_TOKEN=0` to require
  `Authorization: Bearer ...` for ordinary API calls; `/api/events` still accepts
  query-token auth for browser EventSource compatibility.
- No arbitrary terminal keystrokes.
- No glasses-driven Codex permission approvals.
- Project-gated spawn only starts Codex app-server threads in a selected
  Morpheus project. The optional laptop mirror runs a fixed `codex --remote
  ... resume <thread>` command; it does not approve commands or type arbitrary
  shell text.
- Follow-up prompts are sent only to the selected Codex thread. In fallback
  `morpheus` backend mode, set `MORPHEUS_G2_ALLOW_TERMINAL_PROMPTS=0` to disable
  terminal prompt submission.
- Back navigation is state-only. It never kills, interrupts, or hides a laptop
  session.
- No kill, push, merge, or external send.
- New prompt writes either target the selected G2 Codex session or spawn a
  bounded Codex prompt in the selected project.
- Duplicate explicit request ids replay the original response; omitted ids get
  in-flight prompt dedupe for stock client retries.
- No transcript text in the audit log. The in-memory message buffer keeps recent
  user/assistant text only so the glasses can render live history.
- Unknown voice intents become drafts or notes, not actions.

See [docs/hardening.md](docs/hardening.md) for the review decisions.
