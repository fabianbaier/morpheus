# Morpheus Plugins

This directory holds optional integrations that can sit next to Morpheus without
turning the core cockpit into a bundle of device- or model-specific code.

Plugins are intentionally repo-local first. A plugin can be a Node app, Python
package, bridge process, device adapter, model adapter, or a thin wrapper around
an upstream tool. Each plugin should include:

- `morpheus-plugin.json` - stable metadata and capability hints.
- `README.md` - install, run, safety, and transport notes.
- Runtime code in the plugin's own language and dependency manager.

Core Morpheus should expose small, transport-neutral surfaces such as
`morpheus remote snapshot`, `morpheus remote brief`, and future safe prompt
submission APIs. Plugins consume those surfaces and bring their own device or
model runtime.

## Current Plugin Shape

```text
plugins/
  g2-bridge/       Even Realities G2 bridge, Even Terminal-compatible API
  parakeet-mlx/    Local Apple Silicon ASR adapter for Parakeet MLX
```

## Capability Rules

Manifests describe what a plugin wants to do. The bridge or user still owns the
final policy decision.

- `read_status` can read compact Morpheus snapshots and attention cards.
- `stage_note` can write bounded Morpheus notes.
- `submit_prompt` can submit text to an agent session only through a provider
  that has an explicit safe prompt API.
- `interrupt` can interrupt only the explicitly selected voice-bound session.
- `approval_response`, `terminal_keystrokes`, `spawn`, `kill`, `push`, `merge`,
  and `external_send` must default to disabled for remote/device plugins.

Device plugins should treat microphones, glasses, phone WebViews, and public
tunnels as untrusted input surfaces. Voice can request or draft actions; it must
not become approval authority by default.
