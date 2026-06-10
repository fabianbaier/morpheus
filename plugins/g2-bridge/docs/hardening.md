# G2 Bridge Hardening Notes

This bridge deliberately starts narrower than Even Terminal. The first working
path is "G2/phone sends bounded final text into Morpheus as an operator note",
not raw terminal input and not Codex permission control.

## Decisions From Review

- Use Tailscale Serve for the default remote path. Keep the Node bridge bound to
  `127.0.0.1` and let Tailscale provide private HTTPS inside the tailnet.
- Prefer bearer tokens for ordinary API clients. Query-token auth remains enabled
  for stock Even saved-host probes and native `EventSource`; set
  `MORPHEUS_G2_ACCEPT_QUERY_TOKEN=0` to disable query tokens for ordinary API
  calls. The bridge does not print token material.
- Do not expose approvals, question responses, terminal keystrokes, interrupts,
  kill, push, merge, or arbitrary shell execution from the glasses path.
- Remote spawn is allowed only as a project-gated operation that starts the
  configured command (`codex` by default) in a Morpheus project. The transcript
  is staged as a note; it is not typed as arbitrary shell text.
- Require explicit project/session context before any voice or prompt text can
  reach Codex. Prompting a project row creates or reuses that project's active
  `project-session:<projectId>` conversation.
- Prefer explicit idempotency keys for write requests. Duplicate explicit keys
  replay the original response; omitted keys get in-flight prompt dedupe for
  stock client compatibility.
- Treat all voice text as untrusted transcript input. It can be staged as a
  note, but it is not proof of local operator approval.
- Keep audit logs metadata-only. Store text hashes and character counts there,
  not transcript text. The bridge-local message buffer keeps recent
  user/assistant text in memory so the glasses can render live history.
- Keep final transcript submission separate from live ASR partials. Parakeet
  partials are display/UI material; only a final push-to-talk release should
  call `/api/transcript/finalize`.

## Deferred Before Public Exposure

- Pairing and revocation: short-lived QR pairing, persistent per-device tokens,
  and a revoke/rotate command.
- Cookie or fetch-stream auth for events. Native browser `EventSource` cannot
  attach arbitrary `Authorization` headers.
- Tailscale ACL examples that restrict the served bridge to the phone device.
- Codex app-server provider gating for real prompt submission.
- Parakeet process manager in the Node bridge with chunk/session limits,
  backpressure, timeout, restart, and fake-ASR tests.

## First Hardware Test Contract

1. `GET /api/sessions` lists project rows until a project is selected.
2. `POST /api/select-project` pins the bridge to one project.
3. `POST /api/prompt` against a project row spawns or reuses that project's
   active G2 Codex conversation.
4. `GET /api/sessions` inside a selected project exposes that conversation as
   `project-session:<projectId>` plus a `Back to projects` compatibility row.
5. `POST /api/select-session` can open `project-session:<projectId>` without
   confusing it with the project navigation row.
6. `POST /api/transcript/finalize` follows the same bounded prompt path.
7. Morpheus sees the note/session; Codex does not treat the glasses as approval
   authority.
