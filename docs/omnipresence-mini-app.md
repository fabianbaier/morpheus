# Morpheus G2 mini app — install for native phone GPS

The stock Even Terminal client (Host setup screen) talks to the Morpheus bridge
for the feed and conversations, but it **cannot send your phone's location**.
The Morpheus mini app — the code in `plugins/g2-bridge/simulator/` — runs
*inside* the Even app (no separate companion app) and streams phone GPS to the
bridge using the EvenHub SDK 0.0.11 location APIs. This is the native path for
the omnipresence location loop.

There are two ways to get it onto the glasses. **Only the packed `.ehpk` +
Private-testing path grants the location permission** — Even denies location to
QR-sideloaded dev apps. Use the dev-QR path to iterate on the feed/UI fast, and
the `.ehpk` path when you want GPS.

| Path | Command | Location (GPS)? | Review? | Use for |
| --- | --- | --- | --- | --- |
| Dev QR (hot-reload) | `make g2-app-dev` + `make g2-app-qr` | ❌ denied | none | fast feed/UI iteration on the glasses |
| Packed `.ehpk` (Private testing) | `make g2-app-pack` → upload | ✅ granted | none (private) | the real install, with GPS |

## Prerequisites (once)

- Even Realities app on your phone, signed into your Even account.
- **Developer Mode** enabled in the Even app → this unlocks **Developer Center**
  (the QR scanner and, on the portal, `.ehpk` uploads).
- The Morpheus bridge running and reachable over Tailscale
  (`make start-omni` or `make g2-bridge`), with omni on (`morpheus omni on`).

## Path A — dev QR (feed/UI, no GPS)

Fast iteration: edits hot-reload on the glasses. Phone and laptop must be on the
same network (same WiFi, or the phone on your tailnet).

```bash
# terminal 1: the mini app dev server (Vite on :5173)
make g2-app-dev

# terminal 2: print the QR to scan (encodes the LAN URL + your bridge + token)
make g2-app-qr
```

In the Even app: **Developer Center → scan** the QR. The mini app loads with the
Morpheus Feed as its landing view. This path is perfect for checking the feed,
gestures, and layout — but any location call returns `PERMISSION_DENIED`, so the
omni-location loop still sees "no location signals" until you use Path B.

If the phone can't reach `http://<LAN-IP>:5173`, expose the dev server over
Tailscale (`tailscale serve --bg 5173`) and pass that URL:
`npx evenhub qr --url "https://<your-tailnet-host>/?bridge=<bridge>&token=<token>"`.

## Path B — packed `.ehpk` (this is the one that enables GPS)

```bash
make g2-app-pack
# builds plugins/g2-bridge/simulator/morpheus-g2.ehpk with your tailnet
# bridge host baked into the network whitelist.
```

Then install it privately (no store review):

1. `cd plugins/g2-bridge/simulator && npx evenhub login` (your Even account).
2. Upload `morpheus-g2.ehpk` at **hub.evenrealities.com** (Even developer portal).
3. Assign it to **Private testing** (or Beta) and install it to your paired
   glasses from the Even app.
4. On first launch the app requests **location** — allow it. Because it's a
   Hub-installed app, the grant sticks (unlike the dev-QR path).

The app streams GPS to `POST /api/context` on the bridge (deduped to ~25 m
moves, re-armed when the app foregrounds). Verify on the laptop:

```bash
morpheus context latest --kind location   # your fix should appear
morpheus loops run <omni-location-id>      # now searches around you
```

## The network whitelist (already handled)

`make g2-app-pack` runs `scripts/build-manifest.mjs`, which injects the bridge
host into the manifest's `network` permission whitelist (from
`MORPHEUS_G2_PUBLIC_URL`, default your tailnet host). Even blocks any request to
a host not on that list. Override for a different bridge:

```bash
make g2-app-pack G2_PUBLIC_URL=https://your-mac.your-tailnet.ts.net
```

Two things to know: the whitelist is enforced only inside a **packed/installed**
app (not raw dev-QR), and it is **not** a CORS bypass — the bridge must also
allow the app's origin. If installed-app requests are blocked, start the bridge
with the app's origin in `MORPHEUS_G2_ALLOWED_ORIGINS` (check the bridge's
`[g2-api]` logs for the rejected origin) alongside the public URL.

## Manifest reference (`plugins/g2-bridge/simulator/app.json`)

Schema-valid for EvenHub today; edit before a real submission if you like
(rename `name` — ≤20 chars, must not contain "Even"; `package_id` is lowercase
reverse-domain, ≥2 segments, no hyphens/underscores). The build injects the
`network` whitelist; everything else is committed:

- `package_id`: `com.morpheus.g2simulator`
- `name`: `Morpheus G2 Dev`
- `permissions`: `network` (bridge host) + `location`
- `min_sdk_version`: `0.0.10` — installs broadly; the app's location code guards
  for the 0.0.11 APIs and no-ops on older hosts, so GPS needs an Even app new
  enough to expose SDK 0.0.11.

## Honest caveats

- The Even developer portal's exact upload UI isn't publicly documented; the
  CLI (`login`, `pack`) and the Private/Beta testing ladder are confirmed. If
  the portal asks for anything unexpected on first login, that's Even's flow,
  not Morpheus.
- Background location isn't guaranteed by the platform: iOS keeps the WebView
  alive better than Android, and the app re-arms location on foreground. Treat
  continuous background GPS as best-effort — the loop already treats stale
  fixes as "don't search."
