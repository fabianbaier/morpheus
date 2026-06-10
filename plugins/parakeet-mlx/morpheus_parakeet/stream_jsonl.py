"""JSONL streaming adapter for parakeet-mlx.

Input lines:
  {"type":"start","model":"mlx-community/parakeet-tdt-0.6b-v3"}
  {"type":"audio","pcm16le_b64":"..."}
  {"type":"finish"}

Output lines:
  {"type":"ready","model":"..."}
  {"type":"partial","text":"..."}
  {"type":"final","text":"..."}
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any


DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
DEFAULT_MAX_CHUNK_BYTES = 128 * 1024
DEFAULT_MAX_SESSION_BYTES = 16 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _allowed_models() -> set[str]:
    raw = os.environ.get("PARAKEET_ALLOWED_MODELS", DEFAULT_MODEL)
    return {item.strip() for item in raw.split(",") if item.strip()}


def _model_allowed(model_name: str, allowed: set[str]) -> bool:
    return "*" in allowed or model_name in allowed


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _pcm16le_to_mlx_audio(payload: str, *, max_chunk_bytes: int) -> tuple[Any, int]:
    try:
        import mlx.core as mx
        import numpy as np
    except Exception as exc:  # pragma: no cover - depends on optional runtime
        raise RuntimeError("Install this plugin with `pip install -e plugins/parakeet-mlx`.") from exc

    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise RuntimeError("invalid base64 audio payload") from exc
    if not raw:
        raise RuntimeError("empty audio payload")
    if len(raw) > max_chunk_bytes:
        raise RuntimeError(f"audio chunk exceeds {max_chunk_bytes} bytes")
    if len(raw) % 2:
        raise RuntimeError("pcm16le audio payload must have an even byte length")
    samples = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    return mx.array(samples), len(raw)


def main() -> None:
    try:
        from parakeet_mlx import from_pretrained
    except Exception as exc:
        _write({"type": "error", "error": "parakeet-mlx is not installed", "detail": str(exc)})
        raise SystemExit(1) from exc

    allowed_models = _allowed_models()
    model_name = os.environ.get("PARAKEET_MODEL", DEFAULT_MODEL)
    max_chunk_bytes = _env_int("PARAKEET_MAX_CHUNK_BYTES", DEFAULT_MAX_CHUNK_BYTES)
    max_session_bytes = _env_int("PARAKEET_MAX_SESSION_BYTES", DEFAULT_MAX_SESSION_BYTES)
    total_audio_bytes = 0
    model = None
    transcriber = None

    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write({"type": "error", "error": "invalid JSON", "detail": str(exc)})
                continue

            kind = message.get("type")
            if kind == "start":
                requested_model = str(message.get("model") or model_name).strip()
                if not _model_allowed(requested_model, allowed_models):
                    _write({"type": "error", "error": "model is not allowed", "model": requested_model})
                    continue
                model_name = requested_model
                model = from_pretrained(model_name)
                transcriber = model.transcribe_stream(context_size=(256, 256))
                transcriber.__enter__()
                _write({"type": "ready", "model": model_name})
                continue

            if kind == "audio":
                if transcriber is None:
                    model = from_pretrained(model_name)
                    transcriber = model.transcribe_stream(context_size=(256, 256))
                    transcriber.__enter__()
                    _write({"type": "ready", "model": model_name})
                payload = message.get("pcm16le_b64")
                if not isinstance(payload, str) or not payload:
                    _write({"type": "error", "error": "audio message requires pcm16le_b64"})
                    continue
                try:
                    audio, byte_count = _pcm16le_to_mlx_audio(
                        payload,
                        max_chunk_bytes=max_chunk_bytes,
                    )
                except RuntimeError as exc:
                    _write({"type": "error", "error": str(exc)})
                    continue
                total_audio_bytes += byte_count
                if total_audio_bytes > max_session_bytes:
                    _write({"type": "error", "error": f"audio session exceeds {max_session_bytes} bytes"})
                    break
                transcriber.add_audio(audio)
                _write({"type": "partial", "text": transcriber.result.text})
                continue

            if kind == "finish":
                text = transcriber.result.text if transcriber is not None else ""
                _write({"type": "final", "text": text})
                break

            _write({"type": "error", "error": f"unknown message type: {kind}"})
    finally:
        if transcriber is not None:
            transcriber.__exit__(None, None, None)


if __name__ == "__main__":
    main()
