# Morpheus ChatGPT App Draft

Status: draft local surface implemented. It is ready to back a real streamable HTTP MCP Apps server, but it is not yet exposing `/mcp` over HTTPS.

## Goal

Use ChatGPT, including voice conversations, as the operator interface for Morpheus:

- Ask for a crisp fleet status.
- Get short attention cards that Morpheus can push to a phone, web page, or glasses surface.
- Drill into one session without exposing raw terminal buffers.
- Send bounded operator notes back into Morpheus.
- Keep spawn, kill, push, merge, approve, and external-send actions behind a later explicit confirmation gateway.

## Implemented Local Surface

The Python module is `morpheus.remote`.

CLI helpers:

```bash
morpheus remote snapshot
morpheus remote cards
morpheus remote brief <tab_ref-or-mission_ref>
morpheus remote note "short instruction" --target <tab_ref>
morpheus remote manifest
morpheus remote widget --preview --out /tmp/morpheus-live-card.html
```

The generated snapshot intentionally includes small, model/device-friendly fields:

- `summary`: voice-sized status.
- `cards`: urgent/normal/low cards with short titles and bodies.
- `sessions`: compact session rows using `tab_ref` and `mission_ref`, not raw terminal buffers.
- `goals`: compact goal-run rows.
- `policy`: explicit remote-control limits.

## Apps SDK Shape

The draft manifest follows current ChatGPT Apps/MCP guidance:

- Read tools use `annotations.readOnlyHint=true`.
- Write tools use `readOnlyHint=false`.
- Bounded internal writes use `openWorldHint=false`.
- Destructive actions are not exposed.
- The render tool advertises the widget through `_meta.ui.resourceUri` and `_meta["openai/outputTemplate"]`.

Tools:

- `get_fleet_snapshot`: data tool for voice/mobile status.
- `get_attention_cards`: data tool for push candidates.
- `get_session_brief`: focused drill-down without raw terminal buffers.
- `stage_operator_note`: bounded internal write to Morpheus notes.
- `render_morpheus_live_card`: render tool for the ChatGPT iframe widget.

Reference docs:

- ChatGPT Developer Mode: https://developers.openai.com/api/docs/guides/developer-mode
- MCP server build guide: https://developers.openai.com/apps-sdk/build/mcp-server
- ChatGPT UI bridge: https://developers.openai.com/apps-sdk/build/chatgpt-ui
- Apps auth: https://developers.openai.com/apps-sdk/build/auth

## Production Path

1. Add a small streamable HTTP MCP server around `morpheus.remote`.
2. Register the widget HTML as resource `ui://morpheus/live-card.html`.
3. Put the server behind HTTPS for ChatGPT Developer Mode.
4. Add OAuth before using the app outside a local/tunneled personal dev setup.
5. Add a confirmation gateway for higher-risk actions:
   - spawn worker
   - stop/kill session
   - send iTerm text
   - push to GitHub
   - merge/approve
   - send external message
6. Add a device push worker that polls `attention_cards()` and fans out to APNs/Web Push/G2 SDK.

## Device Rule

Morpheus should decide what to push, not the device. Devices should receive only:

- one-line summary,
- top card title/body,
- target ref,
- allowed actions.

The device should never receive raw buffers, secrets, full logs, or broad action powers.
