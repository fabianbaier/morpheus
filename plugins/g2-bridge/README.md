# Even Realities G2 Bridge

This plugin is the Morpheus-side home for Even Realities G2 work. It is designed
to stay as close as possible to Even Terminal while giving Morpheus a stricter
policy boundary.

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
  projects` row plus the active G2 conversation as `project-session:<projectId>`.
  The original `project:<projectId>` row is navigation-only after the project is
  open. Old Codex app-server history is hidden by default so deleted/archived
  laptop sessions do not reappear on the glasses.
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
  thread in that project. Follow-up prompts target the selected Codex thread.
  For the Codex app-server backend, the bridge waits for the final Codex
  `result` event before returning, then includes the answer redundantly as
  `text`, `answer`, `message`, `response`, `output.text`, `history`,
  `messages`, and `activeMessages`. This keeps stock Even clients from dropping
  back to `Waiting input` before the glasses have a completed answer to render.
  Set `MORPHEUS_G2_WAIT_FOR_RESULT=0` to restore immediate `202` responses, or
  tune the wait with `MORPHEUS_G2_PROMPT_WAIT_FOR_RESULT_MS` (default 90000).
- By default, new app-server threads are also mirrored into a local Morpheus/iTerm
  tab with `codex --remote ws://127.0.0.1:$CODEX_APP_SERVER_PORT resume <thread>`
  so the laptop can watch the same session. Set
  `MORPHEUS_G2_MIRROR_CODEX_TUI=0` to disable that.
- `GET /api/messages` returns structured bridge messages from Codex app-server:
  `text_delta`, tool/status events, final `result`, then `status: idle`. This
  matches Even Terminal's Codex provider and does not depend on terminal output
  polling.
- `GET /api/sessions/:id/history` returns the recent user/assistant text for
  Even Terminal-compatible clients that refresh completed sessions through
  history instead of the live message stream. When the bridge has a buffered
  assistant answer, that buffered answer wins over stale/user-only Codex
  persisted history. `project:<projectId>/history` opens the project session
  list; `project-session:<projectId>/history` opens the active G2 conversation.
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

Set `MORPHEUS_G2_INCLUDE_CODEX_HISTORY=1` only when you intentionally want the
project session list to include older Codex app-server threads.

The bridge prints `[g2-api]` request lines by default so hardware runs can show
whether the phone is reading `/api/messages`, `/api/events`, or
`/api/sessions/:id/history`. Set `MORPHEUS_G2_REQUEST_LOG=0` to quiet those logs.

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

With the stock Even app, the first stream shows projects as session rows. Select
a project row and send a prompt to create a Codex app-server session in that
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
