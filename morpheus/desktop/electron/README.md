# Morpheus Desktop (Electron shell)

This is the thin native-macOS wrapper around the Morpheus desktop cockpit. It
does three things and nothing more:

1. spawns the Python bridge (`morpheus desktop serve --handshake`),
2. reads the JSON handshake line (`{host, port, token, url}`) from its stdout,
3. opens a native window at that URL and manages the child process lifecycle.

All the actual UI and data live in `../web` (served by the Python bridge) and
`../server.py` / `../bridge.py`. The bridge reads the same `~/.morpheus/morpheus.db`
as the CLI, so the desktop app and CLI always share state.

## Run in development (macOS)

```bash
# 1. make sure the CLI is importable on PATH
cd ../../..            # repo root
pip install -e .

# 2. run the Electron shell against the local bridge
cd morpheus/desktop/electron
npm install
npm start
```

If `morpheus` isn't on PATH (e.g. it lives in a venv), point the shell at it:

```bash
MORPHEUS_BIN=/path/to/.venv/bin/morpheus npm start
```

## Build a signed .app / .dmg (macOS only)

`.app`/`.dmg` packaging requires macOS, Xcode command-line tools, and (for
distribution) an Apple Developer signing identity — it cannot be produced on a
Linux CI box. On a Mac:

```bash
npm install
npm run dist        # → dist/Morpheus-<version>.dmg
```

To codesign + notarize, set `CSC_LINK` / `CSC_KEY_PASSWORD` and the
`APPLE_ID` / `APPLE_APP_SPECIFIC_PASSWORD` / `APPLE_TEAM_ID` env vars that
electron-builder reads, then run `npm run dist` again. The bundled app expects a
Python interpreter with `morpheus` installed to be reachable; for a fully
self-contained app, bundle the venv (e.g. with `pyinstaller` or `briefcase`) and
set `MORPHEUS_BIN` accordingly in `main.js`.

## Process lifecycle

`main.js` reaps the Python bridge on quit, window-close, and SIGTERM/SIGINT/
SIGHUP. For the paths where no handler can run (force-quit, SIGKILL, a crash),
the bridge is started with `--parent-watchdog`: it polls its parent pid and
exits on its own when the shell dies, so no orphaned server ever squats on the
port.

## Headless smoke test (Linux CI)

The shell runs under Xvfb for CI smoke tests; containers lack Chromium's setuid
sandbox, so pass `--no-sandbox` there (never needed on macOS):

```bash
xvfb-run -a ./node_modules/.bin/electron --no-sandbox --disable-gpu .
```

Verified: handshake → window renders the cockpit → SIGTERM reaps the bridge →
SIGKILL triggers the bridge's parent watchdog.

## Security

The bridge binds `127.0.0.1` only, requires a per-launch bearer token on every
request, and validates the `Host` header. The renderer runs with
`contextIsolation: true` and `nodeIntegration: false`; `preload.js` exposes only
a tiny read-only surface.
