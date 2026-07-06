# Omnipresence Mode — Quickstart

Omnipresence mode turns your Even Realities G2 into an ambient Morpheus
surface: your laptop runs the loops, judges relevance against your memory
file, and pushes one-line updates to the glasses. Design and decisions live in
[omnipresence-prd.md](omnipresence-prd.md).

## 1. Laptop setup

One command does everything (enable omni, create template loops, install the
loop runner, `tailscale serve`, and start the G2 bridge with a persisted
token):

```bash
cd ~/github/fabianbaier/morpheus
make start-omni
# override the tailnet URL if needed:
#   make start-omni G2_PUBLIC_URL=https://your-mac.your-tailnet.ts.net
```

Related targets: `make g2-bridge` (bridge only), `make omni-status`,
`make omni-off`. The bearer token is generated once into
`~/.morpheus/g2-token` and reused across restarts.

Or step by step:

```bash
pip install -e .          # or: make start (installs venv + daemon)

morpheus omni on          # enable omnipresence
morpheus omni init        # create the template loops (omni-location, omni-memory)
morpheus omni status      # verify: enabled, threshold, loops, feed rules
```

`omni init` creates two ordinary loops you can see in `morpheus loops list`:

- **omni-location** (every 5m, routed to the feed with an `on_threshold`
  rule): checks your latest location signal, reads your memory file, does one
  bounded search nearby, prints one headline or `NOTHING`.
- **omni-memory** (hourly, no feed rule): mines recent feed items and your
  expand/dismiss reactions, appends up to 3 dated facts to your memory file.

Make sure the loop runner is installed so due loops actually run:

```bash
make loop-runner          # launchd runner for due prompt loops
```

## 2. Your memory file

```bash
morpheus memory show      # sectioned markdown: People / Interests / Current / Never push
morpheus memory add --section Current "out of espresso beans; usual brand noted"
morpheus memory add --section "Never push" "no sports news"
morpheus memory log       # audit trail of every change
```

The file lives at `~/.morpheus/memory.md` — edit it directly any time. The
`Never push` section is always included in the judge prompt, no matter how
large the file grows.

## 3. Route your own loops to the glasses

Only loops **you** route push to the glasses — no rule, no push:

```bash
morpheus loops add hn-g2 "watch HN for Even Realities G2 news; print one headline or NOTHING" --every 30m
morpheus feeds route <loop-id>                    # on_threshold with your omni threshold
morpheus feeds route <loop-id> --policy on_change # or a non-judged policy
morpheus feeds rules                              # see all routing
morpheus feeds unroute <loop-id>                  # stop pushing it
```

## 4. Thresholds, caps, quiet hours

`~/.morpheus/config.toml`:

```toml
[omni]
enabled = true
threshold = 0.7        # judge score needed to push (0..1)
push_per_hour = 6      # 0 = no pushes at all
quiet_hours = ""       # off by default; e.g. "22:00-08:00"
judge_command = ""     # default: codex exec; e.g. "claude -p"
ntfy_topic = ""        # off by default; set to wake the glasses from sleep (see below)
ntfy_server = "https://ntfy.sh"
escalate_score = 0.85  # judge score at/above which a push also fires a phone notification
```

## 4b. Wake the glasses from sleep (phone push)

Feed pushes render on the glasses only while the display is on — mini apps are
foreground-only, so nothing can wake a sleeping screen. The exception is the
Even app's built-in **notification mirroring**: phone notifications pop up on
the glasses even from sleep. Omnipresence uses this as an *escalation* channel —
only the most relevant pushes (judge score ≥ `escalate_score`, or a priority>0
item) also fire a phone notification, so urgent items break through while the
rest wait quietly in the feed for your next glance.

The transport is [ntfy](https://ntfy.sh) (free, no account):

1. Install the **ntfy** app on your iPhone.
2. Subscribe to a **hard-to-guess topic** (it acts as a capability URL — anyone
   who knows it can push to you), e.g. `morpheus-fabi-9f3k2x`.
3. Put it in `~/.morpheus/config.toml`:

   ```toml
   [omni]
   ntfy_topic = "morpheus-fabi-9f3k2x"
   escalate_score = 0.85
   ```

4. Test it end to end:

   ```bash
   morpheus omni test-push     # a "Morpheus test push" should hit your phone
   ```

5. In the **Even app → Notifications**, enable notifications and make sure
   **ntfy** is whitelisted (non-default apps appear in the list after their
   first notification, then you toggle them on). Now an escalated push lights
   up the glasses; double-tap dismisses it.

Two honest caveats: escalated headlines leave your tailnet (via the ntfy server
and Apple's push infrastructure), unlike everything else here — keep the
escalation bar high, or self-host ntfy. And a mirrored notification's dismissal
isn't observable, so the memory updater can't learn from escalated pushes the
way it learns from feed taps.

## 5. Bridge + real G2 glasses

```bash
cd plugins/g2-bridge && npm install
export MORPHEUS_G2_TOKEN="$(openssl rand -hex 24)"
export MORPHEUS_G2_PUBLIC_URL="https://your-mac.your-tailnet.ts.net"
export MORPHEUS_G2_ALLOWED_ORIGINS="$MORPHEUS_G2_PUBLIC_URL"
npm start
# in another shell:
tailscale serve --bg 3456
```

With omnipresence enabled, the stock Even client's session list shows
**"Morpheus Feed"** as the first row. Open it: recent pushes render as
messages, and new ones stream in while the row is open. In the simulator (and
future mini app): tap expands a push (with the judge's "why"), double-tap
dismisses it, and both reactions feed back into the memory updater.

Location: the mini app streams phone GPS to the bridge via the EvenHub SDK
≥ 0.0.11 (`location` permission; fixes are deduped under 25 m). Install it with
`make g2-app-pack` — see **[omnipresence-mini-app.md](omnipresence-mini-app.md)**
for the full install path (the packed `.ehpk` is the only path Even grants
location). Browser dev mode has manual lat/lon controls and a walk simulator.
Anything that can POST JSON can also feed
`POST /api/context {"kind":"location","lat":..,"lon":..}` over the tailnet
(iOS Shortcuts, Overland, OwnTracks).

## 6. Smoke-test without glasses

```bash
# post something to the feed and read it back the way the glasses would
python3 -c "from morpheus import feeds; feeds.post('hello from the feed')"
curl -s "$MORPHEUS_G2_PUBLIC_URL/api/feed" -H "Authorization: Bearer $MORPHEUS_G2_TOKEN"
curl -s "$MORPHEUS_G2_PUBLIC_URL/api/sessions/feed:main/history" \
  -H "Authorization: Bearer $MORPHEUS_G2_TOKEN"
```

## Hardware smoke checklist (per the PRD's fidelity rule)

1. Stock Even app connects over Tailscale Serve; "Morpheus Feed" is the first row.
2. Opening the row shows recent pushes; a `feeds.post(...)` from the laptop
   appears on the glasses while the row is open.
3. A location fix posted from the phone shows up in `morpheus context latest`.
4. `omni-location` run (`morpheus loops run <id>`) with a real judge produces
   either a push or a clean `NOTHING`.
5. Dismissed pushes stay dismissed after bridge restarts.
