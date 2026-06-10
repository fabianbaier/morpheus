# Parakeet MLX ASR Plugin

This plugin keeps local speech recognition separate from the G2 bridge. It wraps
`senstella/parakeet-mlx` behind a tiny JSONL protocol so bridges can feed it
G2-compatible PCM chunks and receive transcript updates.

Even Hub microphone capture produces PCM 16 kHz, signed 16-bit little-endian,
mono audio. That matches the adapter's expected input.

## Install

```bash
cd plugins/parakeet-mlx
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

The first run downloads the Parakeet model, so expect a warmup delay.

By default the adapter only allows the model named below. To test another model,
set an explicit allowlist:

```bash
export PARAKEET_ALLOWED_MODELS="mlx-community/parakeet-tdt-0.6b-v3"
```

## JSONL Protocol

Start:

```json
{"type":"start","model":"mlx-community/parakeet-tdt-0.6b-v3"}
```

Audio chunk:

```json
{"type":"audio","pcm16le_b64":"<base64 bytes>"}
```

Finish:

```json
{"type":"finish"}
```

The process writes JSONL responses:

```json
{"type":"ready","model":"mlx-community/parakeet-tdt-0.6b-v3"}
{"type":"partial","text":"run the tests"}
{"type":"final","text":"run the tests"}
```

Partial text is for display only. Bridges should submit only finalized text
after push-to-talk release, VAD end-of-speech, or another explicit send action.

## Limits

The adapter validates base64 and rejects odd-length PCM16 payloads. Defaults:

- `PARAKEET_MAX_CHUNK_BYTES=131072`
- `PARAKEET_MAX_SESSION_BYTES=16777216`
- `PARAKEET_ALLOWED_MODELS=mlx-community/parakeet-tdt-0.6b-v3`

The G2 bridge does not stream audio into this process yet. Until that backend is
wired, send final transcript text to `POST /api/transcript/finalize`.
