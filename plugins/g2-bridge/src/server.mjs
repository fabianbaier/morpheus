import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";
import express from "express";
import qrcode from "qrcode-terminal";
import { createCodexProvider } from "@evenrealities/even-terminal/dist/codex/provider.js";
import { CodexAppServerClient } from "@evenrealities/even-terminal/dist/codex/app-server.js";

const DEFAULT_PORT = 3456;
const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_MAX_PROMPT_CHARS = 240;
const DEFAULT_JSON_LIMIT = "256kb";
const DEFAULT_RUNNER_TIMEOUT_MS = 10_000;
const DEFAULT_OUTPUT_RUNNER_TIMEOUT_MS = 30_000;
const DEFAULT_RUNNER_OUTPUT_BYTES = 256 * 1024;
const DEFAULT_RUNNER_CONCURRENCY = 2;
const DEFAULT_REQUEST_ID_TTL_MS = 10 * 60 * 1000;
const DEFAULT_RATE_LIMIT_WINDOW_MS = 60 * 1000;
const DEFAULT_RATE_LIMIT_MAX = 120;
const DEFAULT_RATE_LIMIT_READ_MAX = 600;
const DEFAULT_CLIENT_POLL_OUTPUT_BUDGET_MS = 800;
const DEFAULT_PROJECT_LIMIT = 25;
const DEFAULT_OUTPUT_POLL_INTERVAL_MS = 2000;
const DEFAULT_OUTPUT_POLL_ATTEMPTS = 45;
const DEFAULT_CODEX_APP_SERVER_PORT = 8765;
const DEFAULT_CODEX_APP_SERVER_STARTUP_WAIT_MS = 30_000;
const CODEX_APP_SERVER_RETRY_DELAY_MS = 1_000;
const DEFAULT_PROMPT_WAIT_FOR_RESULT_MS = 90_000;
const DEFAULT_STALE_MIRROR_GRACE_MS = 4000;
const CODEX_LIVE_EVENTS_CHECK_MS = 60_000;
const LIVE_DELTA_HOLD_MS = 10_000;
const MAX_MESSAGES_PER_SESSION = 500;
const MAX_TRACKED_SESSIONS = 256;
const MAX_PROJECT_SESSION_ROW_CACHE_ENTRIES = 64;
const RATE_LIMIT_MAX_TRACKED_KEYS = 4096;
const DEFAULT_FEED_POLL_MS = 5000;
const DEFAULT_FEED_SUBSCRIBER_IDLE_MS = 30_000;
const DEFAULT_OMNI_STATUS_TTL_MS = 5000;
const DEFAULT_FEED_LIMIT = 20;
const MAX_FEED_LIMIT = 100;
const FEED_SESSION_ID = "feed:main";
const FEED_SESSION_TITLE = "Morpheus Feed";
// Synthetic session that carries guidance notices (e.g. "open a project
// first") for stock clients, which reject prompt responses without a session
// id and render errors as nothing.
const PROJECT_NOTICE_SESSION_ID = "notice:select-project";
const FEED_ACK_ACTIONS = new Set(["expanded", "dismissed"]);
const FEED_READ_ONLY_NOTICE =
  "Morpheus Feed is read-only. New pushes keep arriving here; open a project row to start a conversation.";
const AGENT_BACKEND_MORPHEUS = "morpheus";
const AGENT_BACKEND_CODEX_APP_SERVER = "codex_app_server";
const EVEN_APP_ORIGINS = [
  "capacitor://localhost",
  "ionic://localhost",
  "http://localhost",
  "https://localhost",
  "null",
];
const DEFAULT_ALLOWED_HOSTNAMES = ["localhost", "127.0.0.1", "::1"];
const REQUEST_ID_RE = /^[A-Za-z0-9._:-]{8,128}$/;
const PROJECT_SESSION_PREFIX = "project:";
const PROJECT_ACTIVE_SESSION_PREFIX = "project-session:";
const PROJECTS_NAV_PROJECT_ID = "__projects__";
const PROJECTS_NAV_SESSION_ID = `${PROJECT_SESSION_PREFIX}${PROJECTS_NAV_PROJECT_ID}`;
const LEGACY_PROJECTS_NAV_SESSION_ID = "nav:projects";
const SECRET_LIKE_RE =
  /\b(sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY)\b/;
const ANSI_ESCAPE_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;
const UUID_RE = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i;
const UUID_FRAGMENT_RE = /\b[0-9a-f]{4,12}(?:-[0-9a-f]{4,12}){2,5}\b/i;

function envInt(env, key, fallback, { min = 1, max = Number.MAX_SAFE_INTEGER } = {}) {
  const raw = env[key];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(Math.max(parsed, min), max);
}

// Clients may send garbage numeric params; NaN must fall back to the default
// instead of poisoning comparisons (`id > NaN` is always false) or producing
// `--limit NaN` CLI calls.
function parseIntParam(value, fallback) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function parseLimitParam(value, fallback, max) {
  return Math.min(Math.max(parseIntParam(value, fallback), 1), max);
}

function envList(value) {
  return String(value || "")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

function argValue(argv, name) {
  const idx = argv.indexOf(name);
  if (idx === -1 || idx + 1 >= argv.length) return "";
  return argv[idx + 1];
}

function trimTrailingSlash(value) {
  return String(value || "").replace(/\/+$/, "");
}

function publicUrlHint(publicUrl) {
  if (!publicUrl) return "";
  try {
    const url = new URL(publicUrl);
    if (/^tail[a-z0-9]+\.ts\.net$/i.test(url.hostname)) {
      return "MORPHEUS_G2_PUBLIC_URL looks like a tailnet suffix. Use the full machine hostname, e.g. https://your-mac.tailxxxx.ts.net";
    }
  } catch {
    return "MORPHEUS_G2_PUBLIC_URL is not a valid URL.";
  }
  return "";
}

function normalizeHostname(value) {
  return String(value || "").trim().toLowerCase();
}

function hostnameFromHostHeader(value) {
  const raw = String(value || "").trim();
  if (!raw || raw.includes("/") || raw.includes("@")) return "";
  if (raw.startsWith("[")) {
    const match = raw.match(/^\[([^\]]+)\](?::[0-9]+)?$/);
    return normalizeHostname(match?.[1] || "");
  }
  const colonCount = (raw.match(/:/g) || []).length;
  if (colonCount > 1) return normalizeHostname(raw);
  if (colonCount === 1) {
    const [host, port] = raw.split(":");
    if (!host || !/^[0-9]+$/.test(port)) return "";
    return normalizeHostname(host);
  }
  return normalizeHostname(raw);
}

function hostnameFromUrlOrHost(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (/^[A-Za-z][A-Za-z0-9+.-]*:\/\//.test(raw)) {
    try {
      return normalizeHostname(new URL(raw).hostname);
    } catch {
      return "";
    }
  }
  return hostnameFromHostHeader(raw);
}

function isLocalBindHost(host) {
  const hostname = normalizeHostname(host);
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "::1";
}

function allowUnsafeBind(config) {
  return config.allowUnsafeBind === true || config.allowUnsafeBind === "1";
}

function nowIso(clock) {
  return new Date(clock()).toISOString();
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function resultFingerprint(text) {
  return `result:${sha256(String(text || ""))}`;
}

function outputFingerprint(status, text) {
  return `output:${String(status || "idle")}:${sha256(String(text || ""))}`;
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, "'\\''")}'`;
}

function normalizeComparableText(value) {
  return String(value || "")
    .replace(/^G2:\s*/i, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

function terminalOutputLineText(line) {
  return String(line || "")
    .replace(ANSI_ESCAPE_RE, "")
    .replace(/[╭╮╰╯│─]+/g, " ")
    .replace(/^[\s>›•●○·*+-]+/u, "")
    .trim();
}

function isTerminalMirrorNoiseLine(rawLine, text, { sessionId = "", latestUserText = "" } = {}) {
  const raw = String(rawLine || "").trim();
  const clean = String(text || "").trim();
  if (!clean || clean === "'" || clean === "`") return true;
  if (/^[╭╮╰╯│─\s]+$/.test(raw)) return true;
  if (/^Last login:/i.test(clean)) return true;
  if (/^[^\s@]+@[^\s%]+%(\s|$)/.test(clean)) return true;
  if (/\bcodex\b.*\b--remote\b.*\bresume\b/i.test(clean)) return true;
  if (/\bresume\b\s+['"]?[0-9a-f-]{20,}/i.test(clean)) return true;
  if (sessionId && clean.includes(sessionId)) return true;
  if (UUID_RE.test(clean) && /\b(G2:|codex|resume|remote)\b/i.test(clean)) return true;
  if (UUID_FRAGMENT_RE.test(clean) && /\b(G2:|codex|resume|remote)\b|['"]/.test(clean)) return true;
  if (/^ERROR: remote app server .* transport failed/i.test(clean)) return true;
  if (/^morpheus timed out after \d+ms$/i.test(clean)) return true;
  if (/^G2:\s*/i.test(clean)) return true;
  if (/^[›>]\s*/u.test(raw) && !/^>_\s*/.test(raw)) return true;
  if (
    /^(>_\s*)?(OpenAI Codex|Update available|Run brew|See full release notes|Tip:|model:|directory:|permissions:)/i.test(
      clean,
    )
  ) {
    return true;
  }
  if (/^https:\/\/github\.com\/openai\/codex/i.test(clean)) return true;
  if (/^(Use \/skills|Implement \{feature\}|Summarize recent commits)$/i.test(clean)) return true;

  const latestComparable = normalizeComparableText(latestUserText);
  if (latestComparable && normalizeComparableText(clean) === latestComparable) return true;
  return false;
}

function collapseRepeatedMirrorCandidates(candidates) {
  const collapsed = [];
  for (const candidate of candidates) {
    const text = String(candidate || "").trim();
    if (!text) continue;
    const previous = collapsed.at(-1) || "";
    if (previous && text === previous) continue;
    if (previous && previous.length <= 32 && text === `${previous}${previous}`) continue;
    collapsed.push(text);
  }
  return collapsed;
}

function cleanTerminalMirrorOutput(rawText, { sessionId = "", latestUserText = "" } = {}) {
  const lines = String(rawText || "")
    .replace(/\r/g, "\n")
    .split("\n");
  const candidates = [];
  let block = [];

  function flush() {
    const text = collapseRepeatedMirrorCandidates(block).join("\n").trim();
    if (text) candidates.push(text);
    block = [];
  }

  for (const rawLine of lines) {
    const line = String(rawLine || "").replace(ANSI_ESCAPE_RE, "");
    if (!line.trim()) {
      flush();
      continue;
    }
    const text = terminalOutputLineText(line);
    if (isTerminalMirrorNoiseLine(line, text, { sessionId, latestUserText })) {
      flush();
      continue;
    }
    block.push(text);
  }
  flush();

  return collapseRepeatedMirrorCandidates(candidates).at(-1) || "";
}

function secureEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string") return false;
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  if (leftBuffer.length !== rightBuffer.length) return false;
  return crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

function normalizeTokenHeader(req, { acceptQueryToken = false } = {}) {
  const header = req.headers.authorization || "";
  if (!Array.isArray(header) && header.startsWith("Bearer ")) {
    return header.slice(7).trim();
  }
  if (acceptQueryToken && typeof req.query?.token === "string") {
    return req.query.token.trim();
  }
  return "";
}

function safeJsonError(err) {
  return err instanceof Error ? err.message : String(err || "unknown error");
}

const CODEX_APP_SERVER_STARTUP_ERROR_RE =
  /codex app-server (?:failed to start|not connected|ws (?:error|closed))|WebSocket connect timeout|ECONNREFUSED|ECONNRESET|socket hang up/i;

function isCodexAppServerStartupError(err) {
  return CODEX_APP_SERVER_STARTUP_ERROR_RE.test(safeJsonError(err));
}

// The upstream even-terminal client lazy-spawns `codex app-server` and only
// waits 5s for its ready line. A cold start regularly needs longer, which used
// to fail the first G2 prompt with `spawn_failed` moments before the
// app-server finished booting. The spawned child keeps running, so retrying
// inside the configured startup budget converges as soon as it is listening.
async function retryWhileCodexAppServerStarts(config, label, fn) {
  const budgetMs = Number(config.codexAppServerStartupWaitMs || 0);
  const deadline = config.clock() + budgetMs;
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await fn();
    } catch (err) {
      const remainingMs = deadline - config.clock();
      // A fatal bridge startup failure (e.g. port already in use) sets
      // abortStartup; keeping the codex warm-up retrying past that is noise.
      if (config.abortStartup || !isCodexAppServerStartupError(err) || remainingMs <= 0) throw err;
      bridgeFlow(config, "codex-app-server-startup-retry", {
        label,
        attempt,
        remainingMs,
        reason: safeJsonError(err),
      });
      await new Promise((resolve) => {
        const timer = setTimeout(
          resolve,
          Math.min(CODEX_APP_SERVER_RETRY_DELAY_MS, Math.max(1, remainingMs)),
        );
        if (typeof timer.unref === "function") timer.unref();
      });
    }
  }
}

function bridgeDebug(config, event, fields = {}) {
  if (!config?.debug) return;
  const payload = Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : "";
  config.logger.log(`[g2-debug] ${event}${payload}`);
}

function bridgeFlow(config, event, fields = {}) {
  if (config?.flowLog === false) return;
  const payload = Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : "";
  config.logger.log(`[g2-flow] ${event}${payload}`);
}

// Monotonic touch counter gives least-recently-touched eviction ordering
// without consulting the clock.
function touchSessionBuffer(state, buffer) {
  state.sessionTouchCounter = (state.sessionTouchCounter || 0) + 1;
  buffer.touchedAt = state.sessionTouchCounter;
}

function cleanMessageState(state, sessionId) {
  const key = sessionId || "morpheus";
  let buffer = state.sessions.get(key);
  const created = !buffer;
  if (created) {
    buffer = { messages: [], clients: new Set() };
    state.sessions.set(key, buffer);
  }
  touchSessionBuffer(state, buffer);
  // The key being created is exempt from eviction: at this point it has no
  // SSE clients yet, so protectedSessionKeys may not cover it, and evicting
  // it would hand the caller an orphaned buffer that never receives (or
  // delivers) another message.
  if (created) evictIdleSessionState(state, key);
  return buffer;
}

// Session ids that eviction must never drop: the default buffer, anything a
// connected SSE client, active poller, in-flight refresh, or pending result
// waiter still references, and the selected/active/pending sessions of every
// remembered project — including all aliases fanned out from those ids.
function protectedSessionKeys(state) {
  const keys = new Set(["morpheus"]);
  function protect(id) {
    const key = String(id || "").trim();
    if (!key || keys.has(key)) return;
    keys.add(key);
    for (const alias of state.sessionAliases.get(key) || []) keys.add(alias);
  }
  protect(state.selectedSession?.id);
  // The feed buffer is protected while the feed poller is running: the poller
  // only runs while something is subscribed to feed:main, and evicting the
  // buffer mid-subscription would drop pushes the client has not read yet.
  if (state.feedPoller) protect(FEED_SESSION_ID);
  for (const session of state.projectActiveSessions.values()) protect(session?.id);
  for (const pending of state.pendingProjectPrompts.values()) {
    protect(pending?.id);
    protect(pending?.projectSessionId);
  }
  for (const project of [state.selectedProject, state.lastProject]) {
    if (!projectKey(project)) continue;
    protect(projectSessionId(project));
    protect(activeProjectSessionId(project));
  }
  for (const [key, buffer] of state.sessions) {
    if (buffer?.clients?.size) protect(key);
  }
  for (const key of state.outputPollers.keys()) protect(key);
  for (const key of state.outputRefreshInflight.keys()) protect(key);
  for (const [key, waiters] of state.resultWaiters) {
    if (waiters?.length) protect(key);
  }
  // A protected alias keeps its real session (and that session's other
  // aliases) alive too, so live fan-out never loses its source buffer.
  for (const [realId, aliases] of state.sessionAliases) {
    if (keys.has(realId) || [...aliases].some((alias) => keys.has(alias))) {
      keys.add(realId);
      for (const alias of aliases) keys.add(alias);
    }
  }
  return keys;
}

// Shared bounded-map eviction: once `map` grows past `cap`, drop the entries
// with the oldest `tsOf(value)` down to a low-water mark below the cap. The
// low-water headroom matters: evicting to exactly the cap would re-run the
// full scan+sort on every subsequent insert while the map sits at the cap
// (per-request O(n log n) for pre-auth maps like the rate limiter).
function evictOldest(map, { cap, lowWater, tsOf = (value) => value, skip = () => false, onEvict } = {}) {
  if (map.size <= cap) return;
  const floor = Math.max(1, lowWater ?? Math.floor(cap * 0.875));
  const evictable = [...map.entries()]
    .filter(([key, value]) => !skip(key, value))
    .sort(([, left], [, right]) => (tsOf(left) || 0) - (tsOf(right) || 0));
  for (const [key, value] of evictable) {
    if (map.size <= floor) break;
    map.delete(key);
    onEvict?.(key, value);
  }
}

// Any client may name a brand-new session id (e.g. /api/events?sessionId=...),
// so per-session state must not grow forever. Once the tracked session count
// passes the cap, drop the least-recently-touched sessions nothing still
// references, together with the side-band state keyed by the same id. The
// key being created or touched right now (`activeKey`) is never evicted.
function evictIdleSessionState(state, activeKey = "") {
  const cap = state.maxTrackedSessions;
  if (state.sessions.size <= cap) return;
  const protectedKeys = protectedSessionKeys(state);
  evictOldest(state.sessions, {
    cap,
    tsOf: (buffer) => buffer?.touchedAt || 0,
    skip: (key) => key === activeKey || protectedKeys.has(key),
    onEvict: (key) => {
      state.outputHashes.delete(key);
      state.sessionAliases.delete(key);
      state.promptMirrorBaselines.delete(key);
      state.codexSessions.delete(key);
      state.codexLiveEventsCheckedAt.delete(key);
      state.outputPollStats.delete(key);
    },
  });
}

// Message ids are allocated from one bridge-wide counter so the same logical
// message keeps the same id in every buffer it lands in. Clients keep a single
// `after` cursor while hopping between project rows, project-session rows, and
// real thread ids; per-buffer counters made those cursors skip newer messages.
function allocateMessageId(state) {
  if (!state.nextMessageId) state.nextMessageId = 1;
  const id = state.nextMessageId;
  state.nextMessageId += 1;
  return id;
}

// Last allocated bridge-wide message id. Prompt waits use this as a floor when
// the provider resolves to a session id whose cursor was not captured before
// submission: anything already buffered for that session predates the prompt,
// so it must not be returned as this turn's result.
function currentMessageId(state) {
  return Number(state.nextMessageId || 1) - 1;
}

// Stock Even clients drop stream/poll messages whose sessionId does not match
// the row they are watching, so alias-delivered messages are presented under
// the session id the client asked for; the real thread id stays alongside.
function presentMessageForSession(msg, sessionId) {
  const target = String(sessionId || "");
  const original = String(msg?.sessionId || "");
  if (!target || !original || original === target) return msg;
  return { ...msg, sessionId: target, activeSessionId: original };
}

// `quiet` buffers the message for history/poll reads without broadcasting it
// to attached SSE clients: the feed baseline fetch uses it so pre-existing
// items hydrate the transcript without arriving as fresh push events.
function appendMessageEntry(state, sessionId, msg, id, { quiet = false } = {}) {
  const key = sessionId || "morpheus";
  const buffer = cleanMessageState(state, sessionId);
  const entry = { id, ...msg };
  buffer.messages.push(entry);
  if (buffer.messages.length > MAX_MESSAGES_PER_SESSION) {
    buffer.messages.shift();
  }
  if (!quiet) {
    const payload = JSON.stringify(presentMessageForSession(msg, key));
    for (const client of buffer.clients) {
      const res = client?.res || client;
      if (typeof client?.filter === "function" && !client.filter(msg)) continue;
      res.write(`id: ${id}\ndata: ${payload}\n\n`);
    }
  }
  if (msg?.type === "result" || msg?.type === "error") {
    resolveResultWaiters(state, key, entry);
  }
  return id;
}

function pushMessage(state, sessionId, msg) {
  return appendMessageEntry(state, sessionId, msg, allocateMessageId(state));
}

function latestMessageId(state, sessionId) {
  const buffer = state.sessions.get(sessionId || "morpheus");
  let latest = 0;
  for (const entry of buffer?.messages || []) {
    const id = Number(entry?.id || 0);
    if (id > latest) latest = id;
  }
  return latest;
}

function latestTerminalMessage(state, sessionId, after = 0) {
  const messages = getMessages(state, sessionId, 0);
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const msg = messages[idx];
    if (msg.id > after && (msg.type === "result" || msg.type === "error")) return msg;
  }
  return null;
}

function latestTerminalMessageAfterLatestPrompt(state, sessionId) {
  const messages = getMessages(state, sessionId, 0);
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const msg = messages[idx];
    if (msg.type === "result" || msg.type === "error") return msg;
    if (msg.type === "prompt_submitted" || msg.type === "user_prompt") return null;
  }
  return null;
}

function latestUserPromptText(state, sessionId) {
  const messages = getMessages(state, sessionId, 0);
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const msg = messages[idx];
    if (msg.type === "user_prompt" && msg.text) return String(msg.text);
    if (msg.type === "result" || msg.type === "error") return "";
  }
  return "";
}

function sessionAwaitingResultSinceLatestPrompt(state, sessionId) {
  const messages = getMessages(state, sessionId, 0);
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const msg = messages[idx];
    if (msg.type === "result" || msg.type === "error") return null;
    if (msg.type === "prompt_submitted" || msg.type === "user_prompt") return msg;
  }
  return null;
}

// True while the current turn is visibly streaming: a text_delta arrived after
// the latest prompt, recently enough that the live event stream is clearly
// alive. History/mirror fallbacks stay quiet then, so partial answers are not
// published as final results; if the stream dies mid-turn the hold ages out
// and the fallbacks take over.
function liveDeltaStreamActive(state, config, sessionId) {
  const messages = getMessages(state, sessionId, 0);
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const msg = messages[idx];
    if (msg.type === "result" || msg.type === "error") return false;
    if (msg.type === "prompt_submitted" || msg.type === "user_prompt") return false;
    if (msg.type === "text_delta") {
      const at = Date.parse(msg.at || "");
      if (!Number.isFinite(at)) return true;
      return config.clock() - at < LIVE_DELTA_HOLD_MS;
    }
  }
  return false;
}

function historyAnswerForPrompt(history, promptMsg) {
  const entries = Array.isArray(history) ? history : [];
  const promptHash = String(promptMsg?.textHash || "");
  const promptText = String(promptMsg?.text || "");
  if (!promptHash && !promptText) return "";
  for (let idx = entries.length - 1; idx >= 0; idx -= 1) {
    const entry = entries[idx];
    if (entry?.role !== "user" || !entry.text) continue;
    const entryText = String(entry.text);
    const matches =
      (promptText && entryText === promptText) ||
      (promptHash && sha256(entryText) === promptHash);
    if (!matches) continue;
    for (let after = entries.length - 1; after > idx; after -= 1) {
      const candidate = entries[after];
      if (candidate?.role === "assistant" && candidate.text) return String(candidate.text);
    }
    return "";
  }
  return "";
}

function rememberPromptMirrorBaseline(state, config, sessionId, outputResult) {
  if (!sessionId) return;
  const text = String(outputResult?.output?.text || "").trim();
  state.promptMirrorBaselines.set(sessionId, {
    textHash: sha256(text),
    at: config.clock(),
  });
}

function resolveResultWaiters(state, sessionId, msg) {
  const key = String(sessionId || "morpheus");
  const waiters = state.resultWaiters?.get(key);
  if (!waiters?.length) return;
  const remaining = [];
  for (const waiter of waiters) {
    if (msg.id <= waiter.after) {
      remaining.push(waiter);
      continue;
    }
    clearTimeout(waiter.timer);
    waiter.resolve(msg);
  }
  if (remaining.length) state.resultWaiters.set(key, remaining);
  else state.resultWaiters.delete(key);
}

// `after` is required on purpose: an implicit 0 made any previously buffered
// result on the session look like this turn's answer (the stale-answer bug).
// Callers must pass the message-id floor captured before submission.
function waitForResultMessage(state, sessionId, timeoutMs, after) {
  const existing = latestTerminalMessage(state, sessionId, after);
  if (existing) return Promise.resolve(existing);
  const key = String(sessionId || "morpheus");
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      const waiters = state.resultWaiters.get(key) || [];
      state.resultWaiters.set(
        key,
        waiters.filter((waiter) => waiter.resolve !== resolve),
      );
      resolve(null);
    }, Math.max(1, timeoutMs));
    if (typeof timer.unref === "function") timer.unref();
    const waiters = state.resultWaiters.get(key) || [];
    waiters.push({ resolve, timer, after });
    state.resultWaiters.set(key, waiters);
  });
}

function addSessionAlias(state, sessionId, alias) {
  const key = String(sessionId || "").trim();
  const value = String(alias || "").trim();
  if (!key || !value || key === value) return;
  let aliases = state.sessionAliases.get(key);
  if (!aliases) {
    aliases = new Set();
    state.sessionAliases.set(key, aliases);
  }
  if (!aliases.has(value)) aliases.add(value);
  replayMessagesToAlias(state, key, value);
}

function messageFingerprint(msg) {
  const { id: _id, ...payload } = msg || {};
  return JSON.stringify(payload);
}

function mergeBufferedMessages(...groups) {
  const seen = new Set();
  const merged = [];
  for (const group of groups) {
    for (const msg of Array.isArray(group) ? group : []) {
      const fingerprint = messageFingerprint(msg);
      if (seen.has(fingerprint)) continue;
      seen.add(fingerprint);
      merged.push(msg);
    }
  }
  return merged.sort((left, right) => {
    const leftTime = Date.parse(left?.at || "");
    const rightTime = Date.parse(right?.at || "");
    if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
      return leftTime - rightTime;
    }
    return Number(left?.id || 0) - Number(right?.id || 0);
  });
}

function replayMessagesToAlias(state, sessionId, alias) {
  const buffer = state.sessions.get(sessionId);
  if (!buffer?.messages?.length) return;
  const aliasBuffer = cleanMessageState(state, alias);
  const seen = new Set(aliasBuffer.messages.map((entry) => messageFingerprint(entry)));
  for (const entry of buffer.messages) {
    const { id, ...msg } = entry;
    const fingerprint = messageFingerprint(msg);
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    appendMessageEntry(state, alias, msg, id);
  }
}

function sessionMessageTargets(state, sessionId) {
  const key = String(sessionId || "").trim() || "morpheus";
  return [key, ...(state.sessionAliases.get(key) || [])];
}

function pushMessageForSession(state, sessionId, msg, options = {}) {
  const id = allocateMessageId(state);
  for (const target of sessionMessageTargets(state, sessionId)) {
    appendMessageEntry(state, target, msg, id, options);
  }
  return id;
}

function markSessionIdle(state, sessionId) {
  if (state.selectedSession && sessionMatches(state.selectedSession, sessionId)) {
    state.selectedSession = { ...state.selectedSession, status: "idle" };
  }
  for (const [projectId, projectSession] of state.projectActiveSessions) {
    if (sessionMatches(projectSession, sessionId)) {
      state.projectActiveSessions.set(projectId, { ...projectSession, status: "idle" });
    }
  }
}

// A turn that fails before its provider call completes must still terminate
// like a normal turn: publish the failure result and idle status into every
// buffer fanned out from sessionId, then return the selected/project session
// marks to idle — the same terminal sequence publishAssistantResultIfNew
// performs for successful turns. Without the markSessionIdle step the session
// row stays "busy" forever after a failure.
function publishTurnFailure(state, config, sessionId, providerName, text) {
  pushMessageForSession(state, sessionId, {
    type: "result",
    success: false,
    provider: providerName,
    sessionId,
    text,
    at: nowIso(config.clock),
  });
  pushMessageForSession(state, sessionId, {
    type: "status",
    state: "idle",
    provider: providerName,
    sessionId,
    at: nowIso(config.clock),
  });
  markSessionIdle(state, sessionId);
}

function resultAlreadyPublishedAfterLatestPrompt(state, sessionId, text) {
  const expected = String(text || "").trim();
  if (!expected) return false;
  const messages = getMessages(state, sessionId, 0);
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const message = messages[idx];
    if (message.type === "result" && String(message.text || "").trim() === expected) {
      return true;
    }
    if (
      message.type === "prompt_submitted" ||
      message.type === "user_prompt"
    ) {
      return false;
    }
  }
  return false;
}

function publishAssistantResultIfNew(state, config, sessionId, text, providerName = "codex") {
  const trimmed = String(text || "").trim();
  if (!sessionId || !trimmed) return false;
  const fingerprint = resultFingerprint(trimmed);
  if (resultAlreadyPublishedAfterLatestPrompt(state, sessionId, trimmed)) {
    state.outputHashes.set(sessionId, fingerprint);
    return false;
  }
  const outputHash = sha256(trimmed);
  state.outputHashes.set(sessionId, fingerprint);
  pushMessageForSession(state, sessionId, {
    type: "result",
    success: true,
    provider: providerName,
    sessionId,
    text: trimmed,
    outputHash,
    at: nowIso(config.clock),
  });
  pushMessageForSession(state, sessionId, {
    type: "status",
    state: "idle",
    provider: providerName,
    sessionId,
    at: nowIso(config.clock),
  });
  bridgeFlow(config, "terminal-result-published", {
    sessionId,
    provider: providerName,
    textChars: trimmed.length,
    targets: sessionMessageTargets(state, sessionId).map((target) => {
      const buffer = state.sessions.get(target);
      return {
        id: target,
        clients: buffer?.clients?.size || 0,
        latestId: buffer?.messages?.at(-1)?.id || 0,
      };
    }),
  });
  markSessionIdle(state, sessionId);
  return true;
}

function rememberSessionAliases(state, sessionId, { project, requestSessionId } = {}) {
  addSessionAlias(state, sessionId, "morpheus");
  if (requestSessionId) addSessionAlias(state, sessionId, requestSessionId);
  if (project) {
    addSessionAlias(state, sessionId, projectSessionId(project));
    addSessionAlias(state, sessionId, activeProjectSessionId(project));
  }
}

function rememberProjectContext(state, project) {
  if (!projectKey(project)) return null;
  state.lastProject = project;
  return project;
}

function projectContextForPrompt(state) {
  return state.selectedProject || state.lastProject || null;
}

function rememberActiveProjectSession(state, project, session) {
  const activeProjectId = projectKey(project);
  if (!activeProjectId || !session?.id) return;
  rememberProjectContext(state, project);
  state.projectActiveSessions.set(activeProjectId, session);
  addSessionAlias(state, session.id, projectSessionId(project));
  addSessionAlias(state, session.id, activeProjectSessionId(project));
}

function navigationEpoch(state) {
  return Number(state.navigationEpoch || 0);
}

function bumpNavigationEpoch(state) {
  state.navigationEpoch = navigationEpoch(state) + 1;
}

function selectedSessionStillMatches(state, project, sessionId) {
  return Boolean(
    projectKey(state.selectedProject) === projectKey(project) &&
      state.selectedSession &&
      sessionMatches(state.selectedSession, sessionId),
  );
}

function activeSessionForProject(state, projectOrId) {
  const projectId = typeof projectOrId === "string" ? projectOrId : projectKey(projectOrId);
  if (!projectId) return null;
  const remembered = state.projectActiveSessions.get(projectId) || null;
  if (
    remembered &&
    state.selectedSession &&
    sessionMatches(state.selectedSession, remembered.id)
  ) {
    return state.selectedSession;
  }
  return remembered;
}

function activeProjectHistoryShouldStayLive(state, projectId, activeSession) {
  if (!activeSession?.id) return false;
  const selectedMatches =
    state.selectedSession && sessionMatches(state.selectedSession, activeSession.id);
  return Boolean(selectedMatches);
}

function messageBelongsToSession(msg, session) {
  const messageSessionId = String(msg?.sessionId || "");
  return !messageSessionId || Boolean(session && sessionMatches(session, messageSessionId));
}

function transcriptStreamAllowed(state, sessionId, requestedSessionId = "", msg = null) {
  const id = String(sessionId || "");
  if (isProjectMenuSessionId(id)) return false;
  if (!requestedSessionId && !state.selectedSession) return false;
  if (id === "morpheus" && !state.selectedSession) return false;

  const activeProjectId = projectIdFromActiveSessionId(id);
  if (activeProjectId) {
    if (projectKey(state.selectedProject) !== activeProjectId) return false;
    const activeSession = activeSessionForProject(state, activeProjectId);
    const selectedProjectMatches = projectKey(state.selectedProject) === activeProjectId;
    const selectedSessionMatches =
      state.selectedSession && activeSession?.id && sessionMatches(state.selectedSession, activeSession.id);
    return Boolean(
      activeSession?.id &&
        (selectedSessionMatches || selectedProjectMatches) &&
        messageBelongsToSession(msg, activeSession),
    );
  }

  const projectId = projectIdFromSessionId(id);
  if (projectId) {
    const activeSession = activeSessionForProject(state, projectId);
    if (!activeSession?.id) return true;
    const selectedProjectMatches = projectKey(state.selectedProject) === projectId;
    const selectedSessionMatches =
      state.selectedSession && sessionMatches(state.selectedSession, activeSession.id);
    return Boolean(
      (selectedSessionMatches || selectedProjectMatches) &&
        messageBelongsToSession(msg, activeSession),
    );
  }

  if (state.selectedSession) {
    if (id === "morpheus") return messageBelongsToSession(msg, state.selectedSession);
    if (sessionMatches(state.selectedSession, id)) {
      return messageBelongsToSession(msg, state.selectedSession);
    }
  }
  if (requestedSessionId && id !== "morpheus") {
    // A client that explicitly addressed a concrete session keeps receiving
    // that session's own messages even after bridge-side selection moved on;
    // stock Even clients keep their EventSource open across back navigation.
    return messageBelongsToSession(msg, { id });
  }
  return false;
}

function statusFromBufferedMessages(state, sessionId, fallback = "idle") {
  const messages = getMessages(state, sessionId, 0);
  let sawBusyStatus = false;
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const message = messages[idx];
    if (message.type === "result" || message.type === "error") return "idle";
    if (
      sawBusyStatus &&
      (message.type === "prompt_submitted" ||
        message.type === "session_started" ||
        message.type === "tool_start" ||
        message.type === "text_delta" ||
        message.type === "running_stats")
    ) {
      return "busy";
    }
    if (message.type === "status") {
      if (message.state === "idle") return "idle";
      if (message.state === "awaiting") return "awaiting";
      if (message.state === "busy" || String(message.state || "").endsWith("_start")) {
        sawBusyStatus = true;
      }
    }
  }
  if (sawBusyStatus) return "busy";
  return fallback;
}

async function runProjectPromptLocked(state, projectId, fn) {
  const key = String(projectId || "");
  if (!key) return fn();
  const previous = state.projectPromptLocks.get(key) || Promise.resolve();
  let release;
  const current = new Promise((resolve) => {
    release = resolve;
  });
  const queued = previous.catch(() => {}).then(() => current);
  state.projectPromptLocks.set(key, queued);
  await previous.catch(() => {});
  try {
    return await fn();
  } finally {
    release();
    if (state.projectPromptLocks.get(key) === queued) {
      state.projectPromptLocks.delete(key);
    }
  }
}

function sseMessagePayload(entry) {
  const { id: _id, ...msg } = entry;
  return msg;
}

function getMessages(state, sessionId, after) {
  const buffer = state.sessions.get(sessionId || "morpheus");
  if (!buffer) return [];
  // Reading counts as use: a poll-only client (no SSE stream keeping the key
  // protected) must refresh the buffer's LRU stamp, or eviction wipes the
  // transcript of a session that is actively being read.
  touchSessionBuffer(state, buffer);
  return buffer.messages.filter((entry) => entry.id > after);
}

function historyFromBufferedMessages(messages, limit = 10) {
  const history = [];
  let currentAssistant = "";

  function flushAssistant() {
    const text = currentAssistant.trim();
    if (text) {
      history.push({ role: "assistant", text });
      currentAssistant = "";
    }
  }

  for (const message of messages) {
    if (message.type === "user_prompt" && message.text) {
      flushAssistant();
      history.push({ role: "user", text: String(message.text) });
    } else if (message.type === "text_delta" && message.text) {
      currentAssistant += String(message.text);
    } else if (message.type === "result" && message.text) {
      currentAssistant = String(message.text);
      flushAssistant();
    }
  }
  flushAssistant();
  return history.slice(-Math.max(1, limit));
}

function hasAssistantHistory(history) {
  return Array.isArray(history) && history.some((entry) => entry?.role === "assistant" && entry?.text);
}

async function sessionHistory(provider, state, sessionId, limit) {
  const buffered = historyFromBufferedMessages(getMessages(state, sessionId, 0), limit);
  if (hasAssistantHistory(buffered)) return buffered;
  if (!provider.getHistory) return buffered;
  const persisted = await provider.getHistory(sessionId, limit);
  if (hasAssistantHistory(persisted)) return persisted;
  return buffered.length ? buffered : persisted;
}

class Semaphore {
  constructor(max) {
    this.max = Math.max(1, max);
    this.active = 0;
    this.queue = [];
  }

  async run(fn) {
    if (this.active >= this.max) {
      // The waiter inherits the releasing task's slot; `active` stays put so a
      // fresh caller racing the handoff cannot slip in and over-admit.
      await new Promise((resolve) => this.queue.push(resolve));
    } else {
      this.active += 1;
    }
    try {
      return await fn();
    } finally {
      const next = this.queue.shift();
      if (next) next();
      else this.active -= 1;
    }
  }
}

// The real morpheus CLI reports failures as {"ok":false,"error":"..."} JSON on
// STDOUT and exits 1 with an empty stderr. Recognize that shape so callers see
// the CLI's own error message (a deliberate rejection of the request) instead
// of a generic exit-code failure.
function parseCliFailure(stdout) {
  const text = String(stdout || "").trim();
  if (!text.startsWith("{")) return null;
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const error = typeof parsed.error === "string" ? parsed.error.trim() : "";
  if (parsed.ok !== false && !error) return null;
  return {
    error: error || "morpheus CLI reported a failure",
    code: typeof parsed.code === "string" ? parsed.code : "",
  };
}

function runJsonCommand(command, args, options = {}) {
  const {
    timeoutMs = DEFAULT_RUNNER_TIMEOUT_MS,
    outputLimitBytes = DEFAULT_RUNNER_OUTPUT_BYTES,
    env = process.env,
  } = options;

  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let timedOut = false;
    let tooLarge = false;

    const child = spawn(command, args, {
      stdio: ["ignore", "pipe", "pipe"],
      env,
    });

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      setTimeout(() => child.kill("SIGKILL"), 500).unref();
    }, timeoutMs);
    timer.unref();

    child.stdout.on("data", (chunk) => {
      stdoutBytes += chunk.length;
      if (stdoutBytes > outputLimitBytes) {
        if (!tooLarge) {
          tooLarge = true;
          child.kill("SIGTERM");
          setTimeout(() => child.kill("SIGKILL"), 500).unref();
        }
        return;
      }
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      stderrBytes += chunk.length;
      if (stderrBytes <= outputLimitBytes) {
        stderr += chunk.toString("utf8");
      }
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      // The oversized-output kill can outlast the timeout; report the real
      // cause instead of misattributing it to a timeout.
      if (tooLarge) {
        reject(new Error(`${command} output exceeded ${outputLimitBytes} bytes`));
        return;
      }
      if (timedOut) {
        reject(new Error(`${command} timed out after ${timeoutMs}ms`));
        return;
      }
      if (code !== 0) {
        // Real CLI failures land as {"ok":false,"error":"..."} on stdout with
        // an empty stderr; surface that message (flagged as a CLI rejection so
        // routes can answer 4xx instead of 500) before falling back to
        // stderr-or-generic for infrastructure failures.
        const cliFailure = parseCliFailure(stdout);
        if (cliFailure) {
          const err = new Error(cliFailure.error);
          err.cliRejection = true;
          if (cliFailure.code) err.cliCode = cliFailure.code;
          reject(err);
          return;
        }
        reject(new Error(stderr.trim() || `${command} exited with code ${code}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch (err) {
        reject(new Error(`failed to parse JSON from ${command}: ${err.message}`));
      }
    });
  });
}

function createMorpheusProvider(options = {}) {
  const {
    morpheusBin = "morpheus",
    runner = runJsonCommand,
    runnerTimeoutMs = DEFAULT_RUNNER_TIMEOUT_MS,
    outputRunnerTimeoutMs = DEFAULT_OUTPUT_RUNNER_TIMEOUT_MS,
    runnerOutputBytes = DEFAULT_RUNNER_OUTPUT_BYTES,
    runnerConcurrency = DEFAULT_RUNNER_CONCURRENCY,
  } = options;
  const semaphore = new Semaphore(runnerConcurrency);

  async function morpheusJson(args, overrides = {}) {
    return semaphore.run(() =>
      runner(morpheusBin, args, {
        timeoutMs: overrides.timeoutMs || runnerTimeoutMs,
        outputLimitBytes: runnerOutputBytes,
      }),
    );
  }

  return {
    name: "morpheus",
    promptBehavior: "spawn_or_send_prompt",
    allowedActions: [
      "list_projects",
      "select_project",
      "list_sessions",
      "select_session",
      "spawn_session",
      "send_prompt",
      "read_output",
      "navigate_back",
    ],

    async info() {
      return {
        provider: "morpheus",
        model: "morpheus-remote",
        promptBehavior: "spawn_or_send_prompt",
      };
    },

    async listProjects(limit) {
      return morpheusJson([
        "remote",
        "projects",
        "--compact",
        "--limit",
        String(limit),
      ]);
    },

    async listSessions(limit, { projectId } = {}) {
      const args = [
        "remote",
        "snapshot",
        "--compact",
        "--limit",
        String(limit),
      ];
      if (projectId) {
        args.push("--project", projectId);
      }
      const snapshot = await morpheusJson(args);
      return {
        snapshot,
        sessions: (snapshot.sessions || []).slice(0, limit).map(sessionRowToEvenSession),
      };
    },

    async spawnSession({ projectId, goal, command, prompt }) {
      const args = [
        "remote",
        "spawn",
        "--compact",
        "--cmd",
        command,
      ];
      if (prompt !== undefined && prompt !== null) {
        args.push("--prompt", prompt);
      }
      if (projectId) {
        args.push("--project", projectId);
      }
      args.push(goal);
      return morpheusJson(args);
    },

    async stageOperatorNote({ sessionId, text, projectId }) {
      const args = [
        "remote",
        "note",
        "--target",
        sessionId,
        "--kind",
        "note",
      ];
      if (projectId) {
        args.push("--project", projectId);
      }
      args.push("--", text);
      return morpheusJson(args);
    },

    async sendPrompt({ sessionId, text, projectId }) {
      const args = [
        "remote",
        "prompt",
        "--compact",
        "--target",
        sessionId,
      ];
      if (projectId) {
        args.push("--project", projectId);
      }
      args.push("--", text);
      return morpheusJson(args);
    },

    async sessionOutput({ sessionId, projectId, lines = 10 }) {
      const args = [
        "remote",
        "output",
        "--compact",
        "--lines",
        String(lines),
      ];
      if (projectId) {
        args.push("--project", projectId);
      }
      args.push(sessionId);
      return morpheusJson(args, { timeoutMs: outputRunnerTimeoutMs });
    },

    async feedItems({ after = 0, limit = DEFAULT_FEED_LIMIT } = {}) {
      const args = ["remote", "feed", "--compact"];
      const afterId = Number(after || 0);
      if (Number.isFinite(afterId) && afterId > 0) {
        args.push("--after", String(afterId));
      }
      args.push("--limit", String(Math.max(1, Number(limit) || DEFAULT_FEED_LIMIT)));
      return morpheusJson(args);
    },

    async feedAck({ item, action }) {
      return morpheusJson([
        "remote",
        "feed-ack",
        "--compact",
        "--item",
        String(item),
        "--action",
        String(action),
      ]);
    },

    async contextAdd({ kind, data }) {
      return morpheusJson([
        "remote",
        "context-add",
        "--compact",
        "--kind",
        String(kind),
        "--data",
        String(data),
      ]);
    },

    async omniStatus() {
      return morpheusJson(["remote", "omni-status", "--compact"]);
    },
  };
}

function sessionRowToEvenSession(row) {
  const ageMs = Math.max(0, Number(row.age_secs || 0)) * 1000;
  return {
    id: row.tab_ref,
    title: row.goal || row.mission_ref || row.tab_ref,
    timestamp: new Date(Date.now() - ageMs).toISOString(),
    cwd: row.project_root || "",
    provider: "codex",
    status: toEvenStatus(row.state),
    allowedActions: ["select_session", "stage_operator_note"],
    promptBehavior: "stage_operator_note",
    morpheus: row,
  };
}

function codexThreadToEvenSession(row, fallback = {}) {
  const id = String(row?.id || row?.threadId || fallback.id || "");
  const timestamp = row?.timestamp || fallback.timestamp || nowIso(fallback.clock || Date.now);
  return {
    id,
    title: String(row?.title || row?.name || row?.preview || fallback.title || "Codex session").slice(0, 64),
    timestamp,
    cwd: String(row?.cwd || fallback.cwd || ""),
    provider: "codex",
    status: toEvenStatus(row?.status) || row?.status || fallback.status || "idle",
    allowedActions: ["select_session", "send_prompt", "interrupt"],
    promptBehavior: "send_prompt",
    codex: row || {},
  };
}

function projectRowToEvenSession(project) {
  const usage = project.usage || {};
  return {
    id: projectSessionId(project),
    title: String(project.name || project.id || "Morpheus project").slice(0, 64),
    timestamp: new Date(Math.max(0, Number(project.last_seen_at || 0)) * 1000 || Date.now()).toISOString(),
    cwd: String(project.root_path || ""),
    provider: "codex",
    status: "idle",
    allowedActions: ["select_project", "spawn_session", "list_sessions"],
    promptBehavior: "spawn_session_in_project",
    morpheusProject: {
      ...project,
      usage,
    },
  };
}

function projectSessionId(project) {
  return `${PROJECT_SESSION_PREFIX}${project?.id || project?.tenant_id || ""}`;
}

function activeProjectSessionId(project) {
  return `${PROJECT_ACTIVE_SESSION_PREFIX}${projectKey(project)}`;
}

function projectKey(project) {
  return String(project?.id || project?.tenant_id || "");
}

function rememberPendingProjectPrompt(state, project, pending) {
  const key = projectKey(project);
  if (!key) return null;
  const row = {
    id: activeProjectSessionId(project),
    title: pending.title || "Starting G2 session",
    timestamp: pending.timestamp || new Date().toISOString(),
    cwd: String(project?.root_path || ""),
    provider: "codex",
    status: "busy",
    allowedActions: ["select_session", "send_prompt", "interrupt"],
    promptBehavior: "send_prompt",
    pending: true,
    pendingRequestId: pending.requestId || "",
    activeSessionId: null,
    realSessionId: null,
    projectSessionId: projectSessionId(project),
    projectActiveSessionId: activeProjectSessionId(project),
    morpheusProject: project,
  };
  state.pendingProjectPrompts.set(key, row);
  return row;
}

function pendingProjectPromptForProject(state, projectOrId) {
  const key = typeof projectOrId === "string" ? projectOrId : projectKey(projectOrId);
  return key ? state.pendingProjectPrompts.get(key) || null : null;
}

function clearPendingProjectPrompt(state, projectOrId, requestId = "") {
  const key = typeof projectOrId === "string" ? projectOrId : projectKey(projectOrId);
  if (!key) return;
  const pending = state.pendingProjectPrompts.get(key);
  if (requestId && pending?.pendingRequestId && pending.pendingRequestId !== requestId) return;
  state.pendingProjectPrompts.delete(key);
}

function projectMenuRow(project) {
  return {
    id: PROJECTS_NAV_SESSION_ID,
    title: "Back to projects",
    timestamp: new Date().toISOString(),
    cwd: String(project?.root_path || ""),
    provider: "codex",
    status: "idle",
    allowedActions: ["select_project", "list_projects", "navigate_back"],
    promptBehavior: "select_project",
    preview: "Return to project list",
    lastMessage: "Return to project list",
    navigation: {
      action: "projects",
      projectId: project?.id || project?.tenant_id || "",
    },
  };
}

function isProjectMenuSessionId(sessionId) {
  return sessionId === PROJECTS_NAV_SESSION_ID || sessionId === LEGACY_PROJECTS_NAV_SESSION_ID;
}

// Stock Even clients open a row by fetching its history and render that
// history as the conversation. Project and projects-menu rows used to return
// empty history, which left the glasses on a blank "Waiting input" screen.
// These overviews are returned directly in the history response only; they are
// never pushed into message buffers, so they cannot be mistaken for a prompt
// result.
function projectSessionsOverviewHistory(project, rows) {
  const name = String(project?.name || project?.id || project?.tenant_id || "this project");
  const sessionRows = (Array.isArray(rows) ? rows : []).filter(
    (row) => row?.id && !isProjectMenuSessionId(row.id),
  );
  if (!sessionRows.length) {
    return [
      {
        role: "assistant",
        text: `Project ${name} has no sessions yet.\nSpeak now to start a new session here.`,
      },
    ];
  }
  const lines = [
    `Project ${name} — ${sessionRows.length} session${sessionRows.length === 1 ? "" : "s"}:`,
  ];
  sessionRows.slice(0, 8).forEach((row, index) => {
    const status = row.status ? ` [${row.status}]` : "";
    lines.push(`${index + 1}. ${shortText(row.title || row.id, 56)}${status}`);
  });
  if (sessionRows.length > 8) lines.push(`...and ${sessionRows.length - 8} more.`);
  lines.push("Go back and open a session row to resume it, or speak now to start a new session.");
  return [{ role: "assistant", text: lines.join("\n") }];
}

function projectsOverviewHistory(projects, lastProject) {
  const rows = Array.isArray(projects) ? projects : [];
  if (!rows.length) {
    return [{ role: "assistant", text: "No Morpheus projects available yet." }];
  }
  const lines = [`Morpheus projects — ${rows.length}:`];
  rows.slice(0, 8).forEach((project, index) => {
    const live = Number(project?.usage?.live_sessions || 0);
    const liveHint = live ? ` [${live} live]` : "";
    lines.push(`${index + 1}. ${shortText(project?.name || project?.id || "project", 56)}${liveHint}`);
  });
  if (rows.length > 8) lines.push(`...and ${rows.length - 8} more.`);
  const lastName = lastProject?.name || lastProject?.id || "";
  lines.push(
    lastName
      ? `Go back and open a project to see its sessions. Speaking here starts a new session in ${lastName}.`
      : "Go back and open a project to see its sessions.",
  );
  return [{ role: "assistant", text: lines.join("\n") }];
}

function isProjectsNavProjectId(projectId) {
  return projectId === PROJECTS_NAV_PROJECT_ID;
}

function isFeedSessionId(sessionId) {
  return sessionId === FEED_SESSION_ID;
}

// The omnipresence feed rides the stock Even session-row contract (PRD §3.1):
// one pseudo-session row that clients open like any conversation.
function feedSessionRow(state, config) {
  return hydrateSessionRowWithBufferedHistory(state, {
    id: FEED_SESSION_ID,
    title: FEED_SESSION_TITLE,
    timestamp: nowIso(config.clock),
    cwd: "",
    // Cosmetic only (our handlers key off the id): the stock Even app is
    // configured with one agent ("Agent setup: Codex") and drops session
    // rows whose provider differs — every other rendered row says "codex".
    provider: "codex",
    status: "idle",
    allowedActions: ["select_session"],
    promptBehavior: "feed_read_only",
    feed: { cursor: Number(state.feedCursor || 0) },
  });
}

// Cached `morpheus remote omni-status`. /api/sessions and /api/info consult it
// on every request, so it is fetched at most once per TTL; on provider failure
// the last known answer wins (default: disabled) so the session list never
// breaks just because omnipresence status is unavailable.
async function getOmniStatus({ provider, state, config }) {
  const cache = state.omniStatusCache || (state.omniStatusCache = { value: null, at: 0 });
  const now = config.clock();
  if (cache.value && now - cache.at < config.omniStatusTtlMs) return cache.value;
  if (typeof provider.omniStatus !== "function") {
    return cache.value || { enabled: false };
  }
  try {
    const status = await provider.omniStatus();
    state.omniStatusCache = {
      value: { ...status, enabled: status?.enabled === true },
      at: now,
    };
  } catch (err) {
    const reason = safeJsonError(err);
    bridgeDebug(config, "omni-status-failed", { reason });
    // Surface the first failure (and reason changes) at warn level — a
    // silently-hidden feed row is undebuggable from the glasses side.
    if (cache.warnedReason !== reason) {
      config.logger.warn(
        `[g2-bridge] omni-status check failed (${reason}); feed row hidden until it succeeds.`,
      );
    }
    state.omniStatusCache = {
      value: cache.value || { enabled: false },
      at: now,
      stale: true,
      warnedReason: reason,
    };
  }
  return state.omniStatusCache.value;
}

async function feedRowIfEnabled(context) {
  const omni = await getOmniStatus(context);
  if (!omni?.enabled) return null;
  return feedSessionRow(context.state, context.config);
}

function prependFeedRow(feedRow, rows) {
  const sessions = Array.isArray(rows) ? rows : [];
  if (!feedRow) return sessions;
  return [feedRow, ...sessions.filter((row) => row?.id !== FEED_SESSION_ID)];
}

// One feed item = one assistant line: the title (bounded to the PRD's ~220
// char one-page push budget), with the body on the next line when present.
function feedItemMessageText(item) {
  const title = shortText(item?.title, 220);
  const body = shortText(item?.body, 1000);
  if (!title) return body;
  return body ? `${title}\n${body}` : title;
}

// Publishes CLI feed items (ascending ids) into the feed:main message buffer
// as assistant-style result messages, so stock Even clients render each push
// as an assistant line and SSE fanout comes free via pushMessageForSession.
// The bridge-wide cursor advances past everything published; it only jumps to
// latest_id once the CLI page came back short (feeds.recent_after returns the
// OLDEST `limit` items above the cursor, so a full page means a burst is
// still paging and the remainder must arrive on the next poll). `quiet`
// buffers items without SSE fanout and `drainToLatest` forces the latest_id
// jump — the baseline fetch uses both, since its after=0 page is the newest
// items rather than a burst tail.
function publishFeedItems(state, config, result, options = {}) {
  const { limit = DEFAULT_FEED_LIMIT, quiet = false, drainToLatest = false } = options;
  const items = Array.isArray(result?.items) ? result.items : [];
  let published = 0;
  for (const item of items) {
    const id = Number(item?.id || 0);
    if (!Number.isFinite(id) || id <= Number(state.feedCursor || 0)) continue;
    state.feedCursor = id;
    const text = feedItemMessageText(item);
    if (!text) continue;
    pushMessageForSession(
      state,
      FEED_SESSION_ID,
      {
        type: "result",
        success: true,
        provider: "morpheus-feed",
        sessionId: FEED_SESSION_ID,
        text,
        feedItem: {
          id,
          ts: Number(item?.ts || 0),
          priority: Number(item?.priority || 0),
          sourceKind: String(item?.source_kind || ""),
          sourceRef: String(item?.source_ref || ""),
        },
        at: nowIso(config.clock),
      },
      { quiet },
    );
    published += 1;
  }
  const requested = Math.max(1, Number(limit) || DEFAULT_FEED_LIMIT);
  const pageDrained = drainToLatest || items.length < requested;
  const latestId = Number(result?.latest_id || 0);
  if (pageDrained && Number.isFinite(latestId) && latestId > Number(state.feedCursor || 0)) {
    state.feedCursor = latestId;
  }
  if (published) {
    bridgeFlow(config, "feed-items-published", {
      published,
      quiet,
      cursor: Number(state.feedCursor || 0),
    });
  }
  return published;
}

// One in-flight CLI feed read per bridge (same pattern as
// state.outputRefreshInflight): the history-open fetch and the background
// poller share a run instead of racing the cursor and double-publishing.
function fetchAndPublishFeedItems(context, { limit = DEFAULT_FEED_LIMIT } = {}) {
  const { provider, state, config } = context;
  if (typeof provider.feedItems !== "function") return Promise.resolve(0);
  if (state.feedFetchInflight) return state.feedFetchInflight;
  const run = (async () => {
    // The bridge cursor does not survive restarts, so the first fetch after
    // startup (cursor still 0) is a baseline: it hydrates the feed:main
    // buffer for history/display, jumps the cursor to latest_id, and never
    // re-pushes pre-existing items to SSE clients as fresh events. Only items
    // arriving after the baseline stream as new pushes.
    const baseline = !state.feedBaselined && Number(state.feedCursor || 0) === 0;
    const result = await provider.feedItems({
      after: Number(state.feedCursor || 0),
      limit,
    });
    const published = publishFeedItems(state, config, result, {
      limit,
      quiet: baseline,
      drainToLatest: baseline,
    });
    state.feedBaselined = true;
    return published;
  })();
  state.feedFetchInflight = run;
  run
    .catch(() => {})
    .finally(() => {
      if (state.feedFetchInflight === run) state.feedFetchInflight = null;
    });
  return run;
}

// A client counts as subscribed to the feed while an SSE client is attached
// to the feed buffer or a feed select/history/messages/status touch landed
// within the subscriber-idle window. Selection alone is NOT a subscriber:
// it is server-side state that survives client disconnects, so it may extend
// the window (selecting calls touchFeedSubscription) but must never keep the
// poller shelling out to the CLI forever after the glasses vanish.
function feedHasSubscribers(state, config) {
  if (state.sessions.get(FEED_SESSION_ID)?.clients?.size) return true;
  return config.clock() - Number(state.feedLastPolledAt || 0) < config.feedSubscriberIdleMs;
}

function stopFeedPoller(state, config, reason = "stop") {
  if (!state.feedPoller) return;
  clearInterval(state.feedPoller);
  state.feedPoller = null;
  bridgeFlow(config, "feed-poller-stop", { reason });
}

// Bridge feed poller: while anyone is subscribed to feed:main, poll the CLI
// feed with the shared cursor and publish new items into the feed buffer. The
// interval is unref'd and clears itself on the first tick without
// subscribers, so it never leaks timers or keeps the process alive.
function ensureFeedPoller(context) {
  const { provider, state, config } = context;
  if (state.feedPoller || typeof provider.feedItems !== "function") return;
  let running = false;
  const tick = async () => {
    if (!state.feedPoller || running) return;
    running = true;
    try {
      if (!feedHasSubscribers(state, config)) {
        stopFeedPoller(state, config, "no-subscribers");
        return;
      }
      await fetchAndPublishFeedItems(context);
    } catch (err) {
      bridgeDebug(config, "feed-poll-failed", { reason: safeJsonError(err) });
    } finally {
      running = false;
    }
  };
  const timer = setInterval(tick, config.feedPollMs);
  if (typeof timer.unref === "function") timer.unref();
  state.feedPoller = timer;
  bridgeFlow(config, "feed-poller-start", { intervalMs: config.feedPollMs });
  const kick = setTimeout(tick, Math.min(250, config.feedPollMs));
  if (typeof kick.unref === "function") kick.unref();
}

function touchFeedSubscription(context) {
  const { state, config } = context;
  state.feedLastPolledAt = config.clock();
  ensureFeedPoller(context);
}

function shortText(value, max = 240) {
  const text = String(value || "")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > max ? `${text.slice(0, Math.max(0, max - 1)).trim()}...` : text;
}

function latestHistoryText(history, role) {
  for (let idx = history.length - 1; idx >= 0; idx -= 1) {
    const item = history[idx];
    if (item?.role === role && item.text) return String(item.text);
  }
  return "";
}

function bufferedHistoryForRow(state, rowId, limit, options = {}) {
  const ids = [
    ...(options.preferredSessionIds || []),
    rowId,
    ...(options.fallbackSessionIds || []),
  ].filter(Boolean);
  const seen = new Set();
  let firstHistory = [];
  for (const id of ids) {
    if (seen.has(id)) continue;
    seen.add(id);
    const history = historyFromBufferedMessages(getMessages(state, id, 0), limit);
    if (!firstHistory.length && history.length) firstHistory = history;
    if (hasAssistantHistory(history)) return history;
  }
  return firstHistory;
}

function hydrateSessionRowWithBufferedHistory(state, row, options = {}) {
  const history = bufferedHistoryForRow(state, row.id, 10, options);
  const latestAssistant = latestHistoryText(history, "assistant");
  const latestUser = latestHistoryText(history, "user");
  const latestText = latestAssistant || latestUser || row.preview || row.title || "";
  const title =
    options.promoteAssistantToTitle && latestAssistant
      ? shortText(latestAssistant, 64)
      : String(row.title || "Codex session").slice(0, 64);
  return {
    ...row,
    title,
    preview: shortText(latestText, 500),
    lastMessage: shortText(latestText, 1000),
    latestOutput: latestAssistant ? shortText(latestAssistant, 1000) : "",
    history,
  };
}

function activeProjectSessionRow(state, project) {
  const projectId = projectKey(project);
  const activeSession = activeSessionForProject(state, projectId);
  if (!project || !activeSession) return null;
  const id = activeProjectSessionId(project);
  const status = statusFromBufferedMessages(state, activeSession.id, activeSession.status || "idle");
  return hydrateSessionRowWithBufferedHistory(
    state,
    {
      ...activeSession,
      id,
      title: activeSession.title || "G2 session",
      timestamp: activeSession.timestamp || new Date().toISOString(),
      cwd: String(project.root_path || activeSession.cwd || ""),
      provider: activeSession.provider || "codex",
      status,
      codex: activeSession.codex ? { ...activeSession.codex, status } : activeSession.codex,
      allowedActions: ["select_session", "send_prompt", "interrupt"],
      promptBehavior: "send_prompt",
      activeSessionId: activeSession.id,
      realSessionId: activeSession.id,
      projectSessionId: projectSessionId(project),
      projectActiveSessionId: id,
      morpheusProject: project,
    },
    { preferredSessionIds: [activeSession.id] },
  );
}

function selectedSessionResponse(state) {
  if (!state.selectedSession) {
    return {
      selectedSession: null,
      activeSessionId: null,
      displaySessionId: "",
      projectActiveSessionId: "",
      state: "idle",
      history: [],
      messages: [],
      text: "",
    };
  }

  const activeRow = state.selectedProject
    ? activeProjectSessionRow(state, state.selectedProject)
    : null;
  const selected = activeRow &&
      (sessionMatches(state.selectedSession, activeRow.activeSessionId) ||
        sessionMatches(state.selectedSession, activeRow.realSessionId))
    ? activeRow
    : hydrateSessionRowWithBufferedHistory(state, state.selectedSession);
  const activeSessionId =
    selected.activeSessionId ||
    selected.realSessionId ||
    state.selectedSession.id ||
    selected.id;
  const displaySessionId = selected.projectActiveSessionId || selected.id || activeSessionId;
  const displayMessages = getMessages(state, displaySessionId, 0);
  const activeMessages =
    activeSessionId && activeSessionId !== displaySessionId
      ? getMessages(state, activeSessionId, 0)
      : [];
  const messages = mergeBufferedMessages(displayMessages, activeMessages).map((entry) =>
    presentMessageForSession(entry, displaySessionId),
  );
  const history = selected.history || bufferedHistoryForRow(state, displaySessionId, 10, {
    preferredSessionIds: [activeSessionId],
  });
  const text = selected.latestOutput || latestHistoryText(history, "assistant") || "";
  return {
    selectedSession: selected,
    activeSessionId,
    displaySessionId,
    projectActiveSessionId: selected.projectActiveSessionId || "",
    state: selected.status || statusFromBufferedMessages(state, activeSessionId, "idle"),
    history,
    messages,
    text,
  };
}

async function projectSessionMenuRows(provider, state, config, project, limit) {
  const projectId = project?.id || project?.tenant_id || "";
  const cacheKey = projectId || "__global__";
  let result = null;
  let listError = "";
  try {
    result = await provider.listSessions(limit, { projectId });
    const providerSessions = Array.isArray(result) ? result : result.sessions || [];
    state.projectSessionRowsCache.set(cacheKey, {
      sessions: providerSessions,
      snapshot: Array.isArray(result) ? undefined : result.snapshot,
      at: config.clock(),
    });
    evictOldest(state.projectSessionRowsCache, {
      cap: MAX_PROJECT_SESSION_ROW_CACHE_ENTRIES,
      tsOf: (entry) => entry?.at || 0,
      skip: (key) => key === cacheKey,
    });
  } catch (err) {
    listError = safeJsonError(err);
    bridgeDebug(config, "project-session-menu-list-failed", {
      projectId,
      reason: listError,
    });
    config.logger?.warn?.(
      `[g2-sessions] using cached project session rows for ${projectId || "global"}: ${listError}`,
    );
    const cached = state.projectSessionRowsCache.get(cacheKey);
    result = {
      sessions: cached?.sessions || [],
      snapshot:
        cached?.snapshot || {
          generated_at: Math.floor(config.clock() / 1000),
          summary: listError ? `Project session list unavailable: ${listError}` : "Project session list unavailable.",
          counts: {},
          policy: {
            raw_terminal_buffers: false,
            source: "cache_fallback",
          },
        },
      stale: true,
      error: listError,
    };
  }
  const providerSessions = Array.isArray(result) ? result : result.sessions || [];
  const activeProjectRow = activeProjectSessionRow(state, project);
  const pendingProjectRow = activeProjectRow ? null : pendingProjectPromptForProject(state, project);
  const hydratedSessions = providerSessions.map((session) =>
    hydrateSessionRowWithBufferedHistory(state, session),
  );
  const sessions = activeProjectRow
    ? [
        ...(config.showBackToProjectsRow ? [projectMenuRow(project)] : []),
        activeProjectRow,
        ...hydratedSessions.filter((session) => session.id !== activeProjectRow.activeSessionId),
      ]
    : pendingProjectRow
      ? [
          ...(config.showBackToProjectsRow && project ? [projectMenuRow(project)] : []),
          pendingProjectRow,
          ...hydratedSessions,
        ]
    : [
        ...(config.showBackToProjectsRow && project ? [projectMenuRow(project)] : []),
        ...hydratedSessions,
      ];
  return {
    sessions,
    snapshot: Array.isArray(result) ? undefined : result.snapshot,
    stale: Boolean(result?.stale),
    error: listError || result?.error || "",
  };
}

function localProjectSessionMenuRows(state, config, project, limit) {
  const projectId = projectKey(project);
  const cacheKey = projectId || "__global__";
  const cached = state.projectSessionRowsCache.get(cacheKey);
  const activeProjectRow = activeProjectSessionRow(state, project);
  const pendingProjectRow = activeProjectRow ? null : pendingProjectPromptForProject(state, project);
  const seen = new Set();
  const sessions = [];

  function add(row) {
    if (!row?.id || seen.has(row.id)) return;
    seen.add(row.id);
    sessions.push(row);
  }

  if (config.showBackToProjectsRow && project) add(projectMenuRow(project));
  if (activeProjectRow) add(activeProjectRow);
  if (pendingProjectRow) add(pendingProjectRow);
  for (const row of cached?.sessions || []) {
    add(hydrateSessionRowWithBufferedHistory(state, row));
  }

  return {
    sessions: sessions.slice(0, limit),
    snapshot:
      cached?.snapshot || {
        generated_at: Math.floor(config.clock() / 1000),
        summary: "Using local live session state.",
        counts: sessions.reduce((acc, session) => {
          const status = session.status || "unknown";
          acc[status] = (acc[status] || 0) + 1;
          return acc;
        }, {}),
        policy: {
          raw_terminal_buffers: false,
          source: "local_live_session_state",
        },
      },
    stale: false,
    error: "",
  };
}

function pendingProjectPromptHistory(pendingRow) {
  const title = String(pendingRow?.title || "").replace(/^G2:\s*/i, "").trim();
  return title ? [{ role: "user", text: title }] : [];
}

function toEvenStatus(state) {
  switch (state) {
    case "blocked":
    case "crashed":
      return "awaiting";
    case "working":
      return "busy";
    case "idle":
    case "finished":
      return "idle";
    default:
      return null;
  }
}

function projectIdFromSessionId(sessionId) {
  if (isProjectMenuSessionId(sessionId)) return "";
  if (typeof sessionId !== "string" || !sessionId.startsWith(PROJECT_SESSION_PREFIX)) {
    return "";
  }
  return sessionId.slice(PROJECT_SESSION_PREFIX.length);
}

function projectIdFromActiveSessionId(sessionId) {
  if (typeof sessionId !== "string" || !sessionId.startsWith(PROJECT_ACTIVE_SESSION_PREFIX)) {
    return "";
  }
  return sessionId.slice(PROJECT_ACTIVE_SESSION_PREFIX.length);
}

function sessionMatches(session, ref) {
  return (
    session.id === ref ||
    session.morpheus?.tab_ref === ref ||
    session.morpheus?.mission_ref === ref
  );
}

async function resolveSession(provider, ref, limit, options = {}) {
  const { sessions, snapshot } = await provider.listSessions(limit, options);
  const matches = sessions.filter((session) => sessionMatches(session, ref));
  if (matches.length === 0) {
    return { ok: false, status: 404, error: `no session matching '${ref}'`, snapshot };
  }
  if (matches.length > 1) {
    return { ok: false, status: 409, error: `ambiguous session reference '${ref}'`, snapshot };
  }
  return { ok: true, session: matches[0], snapshot };
}

async function listProjects(provider, limit) {
  const result = await provider.listProjects(limit);
  return {
    ...result,
    projects: (result.projects || []).slice(0, limit),
  };
}

function cacheProjects(state, result, clock) {
  if (!Array.isArray(result?.projects)) return result;
  state.projectListCache = {
    ...result,
    projects: [...result.projects],
    at: clock(),
  };
  return result;
}

function cachedProjects(state, limit) {
  const cached = state.projectListCache;
  if (!cached?.projects?.length) return null;
  return {
    ...cached,
    projects: cached.projects.slice(0, limit),
    stale: true,
  };
}

async function listProjectsForResponse(provider, state, config, limit, options = {}) {
  const { preferCache = false } = options;
  if (preferCache) {
    const cached = cachedProjects(state, limit);
    if (cached) return cached;
  }
  try {
    return cacheProjects(state, await listProjects(provider, limit), config.clock);
  } catch (err) {
    const message = safeJsonError(err);
    bridgeDebug(config, "project-list-failed", { reason: message });
    const cached = cachedProjects(state, limit);
    if (cached) {
      config.logger?.warn?.(`[g2-projects] using cached project list: ${message}`);
      return { ...cached, error: message };
    }
    throw err;
  }
}

function projectsResponseBody(result, state) {
  return {
    sessions: (result.projects || []).map(projectRowToEvenSession),
    projects: result.projects || [],
    selectedProject: state.selectedProject,
    mode: "projects",
    stale: Boolean(result.stale),
    error: result.error || undefined,
  };
}

function projectMatches(project, ref) {
  return (
    project.id === ref ||
    project.tenant_id === ref ||
    project.name === ref ||
    project.root_path === ref
  );
}

async function resolveProject(provider, ref, limit) {
  const { projects, current_project_id: currentProjectId } = await listProjects(provider, limit);
  const needle = String(ref || currentProjectId || "").trim();
  if (!needle) {
    return { ok: false, status: 404, error: "no project selected", projects };
  }
  const matches = projects.filter((project) => projectMatches(project, needle));
  if (matches.length === 0) {
    return { ok: false, status: 404, error: `no project matching '${needle}'`, projects };
  }
  if (matches.length > 1) {
    return { ok: false, status: 409, error: `ambiguous project reference '${needle}'`, projects };
  }
  return { ok: true, project: matches[0], projects };
}

// Selecting project-shaped rows must not crash into Express's default HTML 500
// when the provider is down; resolve from the cached project list when it can,
// and report the failure as a JSON error result otherwise.
async function resolveProjectWithCachedFallback(provider, state, config, ref) {
  try {
    return await resolveProject(provider, ref, config.projectLimit);
  } catch (err) {
    const message = safeJsonError(err);
    bridgeDebug(config, "resolve-project-failed", { ref, reason: message });
    const cached = cachedProjects(state, config.projectLimit);
    const matches = (cached?.projects || []).filter((project) => projectMatches(project, ref));
    if (matches.length === 1) {
      config.logger?.warn?.(`[g2-projects] using cached project for '${ref}': ${message}`);
      return { ok: true, project: matches[0], projects: cached.projects, stale: true };
    }
    if (matches.length > 1) {
      // Ambiguity is a client-addressable error, exactly as on the live path;
      // it must not degrade into a 500 just because the resolve came from cache.
      return {
        ok: false,
        status: 409,
        error: `ambiguous project reference '${ref}'`,
        projects: cached.projects,
      };
    }
    return { ok: false, status: 500, error: message, projects: cached?.projects || [] };
  }
}

function createAuditLogger({ auditPath, clock, logger }) {
  if (!auditPath) return () => {};
  try {
    fs.mkdirSync(path.dirname(auditPath), { recursive: true, mode: 0o700 });
  } catch (err) {
    logger.warn(`Could not create audit log directory: ${safeJsonError(err)}`);
    return () => {};
  }

  return (event, details = {}) => {
    const record = {
      ts: nowIso(clock),
      event,
      ...details,
    };
    fs.promises
      .appendFile(auditPath, `${JSON.stringify(record)}\n`, { mode: 0o600 })
      .catch((err) => logger.warn(`Could not append G2 audit log: ${safeJsonError(err)}`));
  };
}

function normalizeRequestId(req) {
  const fromHeader = req.headers["x-request-id"];
  const headerId = Array.isArray(fromHeader) ? fromHeader[0] : fromHeader;
  const bodyId = req.body?.clientRequestId || req.body?.utteranceId || req.body?.requestId;
  const value = String(bodyId || headerId || "").trim();
  return REQUEST_ID_RE.test(value) ? value : "";
}

function requireRequestId(req, res) {
  const requestId = normalizeRequestId(req);
  if (!requestId) {
    res.status(400).json({
      error: "clientRequestId or X-Request-Id is required for write requests",
      code: "missing_request_id",
    });
    return "";
  }
  return requestId;
}

function writeRequestId(req, _state = null) {
  const explicit = normalizeRequestId(req);
  if (explicit) return explicit;
  if (typeof req.body?.text === "string") {
    // Only stable request fields go into the auto id. Hashing the bridge's
    // selected project/session broke in-flight dedupe for stock client
    // retries, because the first prompt itself mutates that selection.
    const bodySessionId = typeof req.body?.sessionId === "string" ? req.body.sessionId : "";
    const key = JSON.stringify({
      path: req.path,
      sessionId: bodySessionId,
      text: req.body.text,
    });
    return `auto-prompt-${sha256(key).slice(0, 32)}`;
  }
  return `auto-${crypto.randomUUID()}`;
}

function isAutoRequestId(requestId) {
  return String(requestId || "").startsWith("auto-");
}

function cleanReplayCache(state, ttlMs, now) {
  for (const [key, value] of state.idempotency.entries()) {
    if (now - value.ts > ttlMs) {
      value.resolve?.({
        status: 409,
        body: { error: "Request id expired before completion.", code: "request_expired" },
      });
      state.idempotency.delete(key);
    }
  }
}

function replayKey(req, requestId) {
  return `${req.method}:${req.path}:${requestId}`;
}

async function maybeReplay(req, res, state, requestId) {
  const remembered = state.idempotency.get(replayKey(req, requestId));
  if (!remembered) return false;
  if (remembered.pending) {
    const completed = await remembered.pending;
    res.status(completed.status).json({ ...completed.body, duplicate: true });
    return true;
  }
  res.status(remembered.status).json({ ...remembered.body, duplicate: true });
  return true;
}

function reserveReplay(req, state, requestId, clock, ttlMs) {
  cleanReplayCache(state, ttlMs, clock());
  const key = replayKey(req, requestId);
  if (state.idempotency.has(key)) return false;
  let resolve;
  const pending = new Promise((done) => {
    resolve = done;
  });
  state.idempotency.set(key, {
    ts: clock(),
    pending,
    resolve,
  });
  return true;
}

function rememberReplay(req, state, requestId, status, body, clock, ttlMs) {
  cleanReplayCache(state, ttlMs, clock());
  const existing = state.idempotency.get(replayKey(req, requestId));
  const completed = {
    status,
    body,
  };
  if (isAutoRequestId(requestId) && existing?.pending) {
    existing.resolve(completed);
    state.idempotency.delete(replayKey(req, requestId));
    return;
  }
  state.idempotency.set(replayKey(req, requestId), {
    ts: clock(),
    ...completed,
  });
  existing?.resolve?.(completed);
}

function parseAllowedOrigins(config) {
  const exact = new Set();
  for (const origin of config.allowedOrigins || []) {
    if (origin === "*" && !config.allowWildcardOrigin) continue;
    exact.add(origin);
  }
  if (config.evenAppCors) {
    for (const origin of EVEN_APP_ORIGINS) {
      exact.add(origin);
    }
  }
  return exact;
}

function configHostList(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") return envList(value);
  return [];
}

function parseAllowedHosts(config) {
  const exact = new Set(DEFAULT_ALLOWED_HOSTNAMES);
  for (const value of [config.localUrl, config.publicUrl, ...(configHostList(config.allowedHosts))]) {
    const hostname = hostnameFromUrlOrHost(value);
    if (hostname) exact.add(hostname);
  }
  return exact;
}

function createHostValidationMiddleware(config) {
  const allowed = parseAllowedHosts(config);
  return (req, res, next) => {
    if (config.allowAnyHost || req.path === "/healthz") {
      next();
      return;
    }
    const hostname = hostnameFromHostHeader(req.headers.host);
    if (!hostname || !allowed.has(hostname)) {
      res.status(403).json({ error: "Host is not allowed", code: "host_not_allowed" });
      return;
    }
    next();
  };
}

function createCorsMiddleware(config) {
  const allowed = parseAllowedOrigins(config);
  return (req, res, next) => {
    const origin = req.headers.origin;
    if (origin) {
      const ok = allowed.has(origin) || allowed.has("*");
      if (!ok) {
        res.status(403).json({ error: "Origin is not allowed", code: "origin_not_allowed" });
        return;
      }
      res.setHeader("Access-Control-Allow-Origin", origin);
      res.setHeader("Vary", "Origin");
      res.setHeader(
        "Access-Control-Allow-Headers",
        "Authorization, Content-Type, X-Request-Id",
      );
      res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    }
    if (req.method === "OPTIONS") {
      res.status(204).end();
      return;
    }
    next();
  };
}

// Rate-limit buckets are keyed by pre-auth client address, so an attacker (or
// a busy tailnet) can mint arbitrarily many keys. Expired buckets are swept at
// most once per window, and the map is hard-capped by dropping the oldest
// windows when it still overflows.
function sweepRateLimits(state, config, now) {
  if (
    state.rateLimits.size <= RATE_LIMIT_MAX_TRACKED_KEYS &&
    now - (state.rateLimitsSweptAt || 0) < config.rateLimitWindowMs
  ) {
    return;
  }
  state.rateLimitsSweptAt = now;
  for (const [key, bucket] of state.rateLimits) {
    if (now - bucket.windowStart > config.rateLimitWindowMs) {
      state.rateLimits.delete(key);
    }
  }
  evictOldest(state.rateLimits, {
    cap: RATE_LIMIT_MAX_TRACKED_KEYS,
    tsOf: (bucket) => bucket.windowStart,
  });
}

function createRateLimitMiddleware(config, state, clock) {
  return (req, res, next) => {
    // Glasses clients poll sessions/messages/status continuously, so read
    // requests get their own larger budget instead of starving (or being
    // starved by) the stricter write budget.
    const isRead = req.method === "GET" || req.method === "HEAD";
    const max = isRead ? config.rateLimitReadMax || config.rateLimitMax : config.rateLimitMax;
    const ip = req.ip || req.socket?.remoteAddress || "unknown";
    const key = `${ip}:${isRead ? "read" : "write"}`;
    const current = clock();
    sweepRateLimits(state, config, current);
    const bucket = state.rateLimits.get(key) || { windowStart: current, count: 0 };
    if (current - bucket.windowStart > config.rateLimitWindowMs) {
      bucket.windowStart = current;
      bucket.count = 0;
    }
    bucket.count += 1;
    state.rateLimits.set(key, bucket);
    if (bucket.count > max) {
      res.status(429).json({ error: "Rate limit exceeded", code: "rate_limited" });
      return;
    }
    next();
  };
}

function createRequestLogMiddleware(config, audit) {
  return (req, res, next) => {
    if (!config.requestLog) {
      next();
      return;
    }
    const startedAt = config.clock();
    const sessionId =
      typeof req.query?.sessionId === "string"
        ? req.query.sessionId
        : typeof req.params?.id === "string"
          ? req.params.id
          : typeof req.body?.sessionId === "string"
            ? req.body.sessionId
            : "";
    res.on("finish", () => {
      const durationMs = Math.max(0, config.clock() - startedAt);
      const record = {
        method: req.method,
        path: req.route?.path ? `${req.baseUrl || ""}${req.route.path}` : req.path,
        originalPath: req.path,
        status: res.statusCode,
        sessionId,
        view: typeof req.query?.view === "string" ? req.query.view : "",
        durationMs,
      };
      audit("api_request", record);
      config.logger.log(
        `[g2-api] ${record.status} ${record.method} ${record.originalPath}` +
          `${record.sessionId ? ` session=${record.sessionId}` : ""} ${durationMs}ms`,
      );
    });
    next();
  };
}

function createAuthMiddleware(config) {
  // timingSafeEqual over two zero-length buffers reports "equal", so an empty
  // configured token would silently disable bearer auth. createBridge refuses
  // to start with one; this guard keeps the middleware safe regardless.
  // createBridge normalizes (trims) the token, and this trim mirrors the one
  // normalizeTokenHeader applies to the client value, so a whitespace-padded
  // MORPHEUS_G2_TOKEN cannot lock every client out with 401s.
  const configuredToken = typeof config.token === "string" ? config.token.trim() : "";
  return (req, res, next) => {
    const isEventStream = req.path === "/events" || req.originalUrl?.startsWith("/api/events");
    const provided = normalizeTokenHeader(req, {
      acceptQueryToken: config.acceptQueryToken || isEventStream,
    });
    if (!configuredToken || !secureEqual(provided, configuredToken)) {
      res.status(401).json({ error: "Unauthorized" });
      return;
    }
    next();
  };
}

function validateText(text, maxPromptChars) {
  if (!text || typeof text !== "string" || !text.trim()) {
    return { ok: false, status: 400, error: "Missing text" };
  }
  const normalized = text.trim();
  if (normalized.length > maxPromptChars) {
    return {
      ok: false,
      status: 413,
      error: `Text exceeds ${maxPromptChars} characters`,
      code: "text_too_long",
    };
  }
  if (SECRET_LIKE_RE.test(normalized)) {
    return {
      ok: false,
      status: 400,
      error: "Text looks like a secret and was not accepted over the glasses bridge",
      code: "secret_like_text",
    };
  }
  return { ok: true, text: normalized };
}

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

// v1 location context payload: numeric lat/lon (strict — string coordinates
// are rejected, never coerced), optional accuracy/ts. Returns a compact
// allowlisted payload so nothing else from the request body reaches the CLI.
function validateLocationContext(body) {
  const lat = body?.lat;
  const lon = body?.lon;
  if (!finiteNumber(lat) || lat < -90 || lat > 90) {
    return {
      ok: false,
      code: "invalid_location",
      error: "lat must be a number between -90 and 90",
    };
  }
  if (!finiteNumber(lon) || lon < -180 || lon > 180) {
    return {
      ok: false,
      code: "invalid_location",
      error: "lon must be a number between -180 and 180",
    };
  }
  const payload = { lat, lon };
  if (body.accuracy !== undefined && body.accuracy !== null) {
    if (!finiteNumber(body.accuracy) || body.accuracy < 0) {
      return {
        ok: false,
        code: "invalid_location",
        error: "accuracy must be a non-negative number",
      };
    }
    payload.accuracy = body.accuracy;
  }
  if (body.ts !== undefined && body.ts !== null) {
    if (!finiteNumber(body.ts) || body.ts <= 0) {
      return {
        ok: false,
        code: "invalid_location",
        error: "ts must be a positive epoch number",
      };
    }
    payload.ts = body.ts;
  }
  return { ok: true, payload };
}

function selectedSessionTarget(state, bodySessionId) {
  if (!state.selectedSession) {
    return {
      ok: false,
      status: 409,
      error: "Select a Morpheus session before sending voice or prompt text.",
      code: "session_not_selected",
    };
  }
  const selected = state.selectedSession;
  if (bodySessionId && !sessionMatches(selected, bodySessionId)) {
    return {
      ok: false,
      status: 409,
      error: "Request sessionId does not match the selected session.",
      code: "selected_session_mismatch",
    };
  }
  return { ok: true, sessionId: selected.id, selected };
}

function goalFromRemoteText(text) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (!clean) return "G2 remote session";
  return `G2: ${clean.slice(0, 80)}`;
}

async function projectForProvider(provider, projectId, state, limit) {
  const ref = projectId || state.selectedProject?.id || state.selectedProject?.tenant_id || "";
  if (!ref) return state.selectedProject || null;
  const resolved = await resolveProject(provider, ref, limit);
  if (resolved.ok) return resolved.project;
  return state.selectedProject || null;
}

function codexResumeCommand({ sessionId, cwd, port }) {
  const wsUrl = `ws://127.0.0.1:${port}`;
  return [
    "codex",
    "--remote",
    shellQuote(wsUrl),
    "-C",
    shellQuote(cwd || process.cwd()),
    "resume",
    shellQuote(sessionId),
  ].join(" ");
}

function codexPassiveMirrorCommand(options) {
  // The mirror tab is display-only because Morpheus receives --prompt "".
  // Do not append a shell comment here: interactive zsh can pass "#" through
  // as a real Codex prompt when interactive comments are disabled.
  return codexResumeCommand(options);
}

function mergeSessionRows(...groups) {
  const seen = new Set();
  const merged = [];
  for (const group of groups) {
    for (const session of Array.isArray(group) ? group : []) {
      if (!session?.id || seen.has(session.id)) continue;
      seen.add(session.id);
      merged.push(session);
    }
  }
  return merged;
}

function statusFromCodexMessage(msg, current = "idle") {
  if (msg.type === "result") return "idle";
  if (msg.type === "permission_request" || msg.type === "user_question") return "awaiting";
  if (msg.type === "status") {
    if (msg.state === "idle") return "idle";
    if (msg.state === "awaiting") return "awaiting";
    if (msg.state === "busy" || String(msg.state || "").endsWith("_start")) return "busy";
    if (String(msg.state || "").endsWith("_end")) return current || "busy";
  }
  if (msg.type === "tool_start" || msg.type === "text_delta" || msg.type === "running_stats") return "busy";
  return current;
}

function createCodexAppServerBridgeProvider(options = {}) {
  const {
    morpheusProvider,
    state,
    config,
    audit = () => {},
    codexClient = null,
    createCodexAgentProvider = null,
  } = options;
  const client =
    codexClient ||
    new CodexAppServerClient(`ws://127.0.0.1:${config.codexAppServerPort}`);

  function rememberCodexSession(session) {
    if (!session?.id) return session;
    const existing = state.codexSessions.get(session.id) || {};
    const merged = {
      ...existing,
      ...session,
      provider: "codex",
      allowedActions: ["select_session", "send_prompt", "interrupt"],
      promptBehavior: "send_prompt",
    };
    state.codexSessions.set(session.id, merged);
    return merged;
  }

  function emit(sessionId, msg) {
    if (!sessionId) return;
    const entry = {
      provider: "codex",
      sessionId,
      at: nowIso(config.clock),
      ...msg,
    };
    bridgeDebug(config, "codex-event", {
      sessionId,
      type: entry.type,
      state: entry.state || "",
      textChars: String(entry.text || "").length,
      targets: sessionMessageTargets(state, sessionId).length,
    });
    pushMessageForSession(state, sessionId, entry);

    const existing =
      state.codexSessions.get(sessionId) ||
      (state.selectedSession && sessionMatches(state.selectedSession, sessionId)
        ? state.selectedSession
        : null);
    const status = statusFromCodexMessage(entry, existing?.status || "idle");
    if (existing) {
      rememberCodexSession({
        ...existing,
        id: sessionId,
        status,
        timestamp: nowIso(config.clock),
      });
    }
    if (state.selectedSession && sessionMatches(state.selectedSession, sessionId)) {
      state.selectedSession = {
        ...state.selectedSession,
        status,
        timestamp: nowIso(config.clock),
      };
    }
    for (const [projectId, projectSession] of state.projectActiveSessions) {
      if (sessionMatches(projectSession, sessionId)) {
        state.projectActiveSessions.set(projectId, {
          ...projectSession,
          status,
          timestamp: nowIso(config.clock),
        });
      }
    }
    if (entry.type === "result") {
      state.outputHashes.set(sessionId, resultFingerprint(entry.text));
      audit("codex_result_emitted", {
        sessionId,
        success: entry.success !== false,
        textHash: sha256(String(entry.text || "")),
        textChars: String(entry.text || "").length,
      });
    }
  }

  const codex =
    createCodexAgentProvider?.(emit, client) ||
    createCodexProvider(emit, () => client);
  const morpheusSnapshotSessions = new Map();
  const codexMirrorSessions = new Map();

  async function listMorpheusSnapshotSessions(limit, projectId = "") {
    if (!morpheusProvider?.listSessions) return [];
    try {
      const result = await morpheusProvider.listSessions(limit, { projectId });
      const sessions = Array.isArray(result) ? result : result.sessions || [];
      for (const session of sessions) {
        if (session?.id) morpheusSnapshotSessions.set(session.id, session);
        // Mirror tabs outlive bridge restarts, but the thread->tab map is in
        // memory only. Snapshot rows expose the exact codex thread id of
        // `codex ... resume <id>` tabs, so re-attach them here: terminal
        // output fallback keeps working for old threads and re-prompts do
        // not spawn duplicate mirror tabs.
        const resumeRef = String(session?.morpheus?.resume_ref || "").trim();
        if (resumeRef) {
          state.mirroredCodexSessions.add(resumeRef);
          if (!codexMirrorSessions.has(resumeRef)) {
            codexMirrorSessions.set(resumeRef, session);
            bridgeDebug(config, "codex-mirror-remapped", {
              sessionId: resumeRef,
              tabRef: session?.id || "",
            });
          }
        }
      }
      bridgeDebug(config, "morpheus-snapshot-sessions", {
        projectId,
        count: sessions.length,
      });
      return sessions;
    } catch (err) {
      audit("morpheus_snapshot_sessions_failed", {
        projectId,
        reason: safeJsonError(err),
      });
      bridgeDebug(config, "morpheus-snapshot-sessions-failed", {
        projectId,
        reason: safeJsonError(err),
      });
      return [];
    }
  }

  async function morpheusOutputHistory(sessionId, limit, projectId = "") {
    if (!morpheusProvider?.sessionOutput) return [];
    const outputSessionId = morpheusOutputSessionId(sessionId);
    if (!outputSessionId) return [];
    try {
      const result = await morpheusProvider.sessionOutput({
        sessionId: outputSessionId,
        projectId,
        lines: Math.max(1, limit || 10),
      });
      const rawText = String(result?.output?.text || "");
      const text = cleanTerminalMirrorOutput(rawText, {
        sessionId,
        latestUserText: latestUserPromptText(state, sessionId),
      }).trim();
      bridgeDebug(config, "morpheus-output-history-normalized", {
        sessionId,
        rawChars: rawText.length,
        textChars: text.length,
      });
      if (!text) return [];
      return [{ role: "assistant", text }];
    } catch (err) {
      audit("morpheus_session_output_failed", {
        sessionId,
        projectId,
        reason: safeJsonError(err),
      });
      bridgeDebug(config, "morpheus-session-output-failed", {
        sessionId,
        projectId,
        reason: safeJsonError(err),
      });
      return [];
    }
  }

  function isSelectedMorpheusSession(sessionId) {
    return Boolean(
      state.selectedSession?.morpheus && sessionMatches(state.selectedSession, sessionId),
    );
  }

  function isKnownMorpheusSession(sessionId) {
    if (isSelectedMorpheusSession(sessionId)) return true;
    if (codexMirrorSessions.has(sessionId)) return true;
    return [...morpheusSnapshotSessions.values()].some((session) =>
      session?.morpheus && sessionMatches(session, sessionId),
    );
  }

  function morpheusOutputSessionId(sessionId) {
    const mirrored = codexMirrorSessions.get(sessionId);
    if (mirrored?.id) return mirrored.id;
    if (isSelectedMorpheusSession(sessionId)) return state.selectedSession.id;
    const snapshot = [...morpheusSnapshotSessions.values()].find((session) =>
      session?.morpheus && sessionMatches(session, sessionId),
    );
    return snapshot?.id || "";
  }

  async function listCodexSessions(limit, projectId = "") {
    const project = await projectForProvider(morpheusProvider, projectId, state, config.projectLimit);
    const cwd = project?.root_path || state.selectedProject?.root_path || "";
    const resolvedProjectId = project?.id || project?.tenant_id || projectId || "";
    const rows = config.includeCodexHistory ? await codex.listSessions(limit, cwd) : [];
    const fromCodex = rows
      .filter((row) => {
        const rowCwd = String(row?.cwd || "");
        return !cwd || !rowCwd || rowCwd === cwd;
      })
      .map((row) =>
        codexThreadToEvenSession(row, {
          cwd,
          clock: config.clock,
        }),
      );
    const remembered = [...state.codexSessions.values()].filter((session) => {
      if (!cwd) return true;
      return !session.cwd || session.cwd === cwd;
    });
    const fromMorpheus = await listMorpheusSnapshotSessions(limit, resolvedProjectId);
    const merged = mergeSessionRows(remembered, fromCodex, fromMorpheus).slice(0, limit);
    bridgeDebug(config, "codex-list-sessions-merged", {
      projectId: resolvedProjectId,
      remembered: remembered.length,
      codex: fromCodex.length,
      morpheus: fromMorpheus.length,
      merged: merged.length,
    });
    return merged;
  }

  async function mirrorSessionToMorpheus({ sessionId, project, goal }) {
    if (!config.mirrorCodexTui || state.mirroredCodexSessions.has(sessionId)) return null;
    state.mirroredCodexSessions.add(sessionId);
    const cwd = project?.root_path || process.cwd();
    const command = codexPassiveMirrorCommand({
      sessionId,
      cwd,
      port: config.codexAppServerPort,
    });
    try {
      const result = await morpheusProvider.spawnSession({
        projectId: project?.id || project?.tenant_id || "",
        goal,
        command,
        prompt: "",
      });
      const mirroredSession = result?.session ? sessionRowToEvenSession(result.session) : null;
      if (mirroredSession?.id) {
        codexMirrorSessions.set(sessionId, mirroredSession);
        scheduleOutputRefresh({ config, provider: providerApi, state }, sessionId);
      }
      audit("codex_tui_mirrored", {
        sessionId,
        projectId: project?.id || project?.tenant_id || "",
        tabRef: result?.session?.tab_ref || "",
      });
      bridgeDebug(config, "codex-tui-mirror-ready", {
        sessionId,
        projectId: project?.id || project?.tenant_id || "",
        tabRef: mirroredSession?.id || "",
      });
      return result;
    } catch (err) {
      state.mirroredCodexSessions.delete(sessionId);
      audit("codex_tui_mirror_failed", {
        sessionId,
        projectId: project?.id || project?.tenant_id || "",
        reason: safeJsonError(err),
      });
      pushMessageForSession(state, sessionId, {
        type: "warning",
        provider: "morpheus",
        sessionId,
        message: `Could not mirror this Codex thread into a local terminal: ${safeJsonError(err)}`,
        at: nowIso(config.clock),
      });
      return null;
    }
  }

  const providerApi = {
    name: "morpheus-codex",
    agentBackend: AGENT_BACKEND_CODEX_APP_SERVER,
    promptBehavior: "codex_app_server",
    allowedActions: [
      "list_projects",
      "select_project",
      "list_sessions",
      "select_session",
      "spawn_session",
      "send_prompt",
      "navigate_back",
      "interrupt",
    ],

    async info() {
      const info = await codex.getInfo().catch((err) => ({
        provider: "codex",
        model: "Codex",
        version: "Unknown",
        error: safeJsonError(err),
      }));
      return {
        ...info,
        provider: "codex",
        promptBehavior: "codex_app_server",
        mirrorCodexTui: config.mirrorCodexTui,
      };
    },

    // even-terminal lazy-spawns the codex app-server on first use; warming it
    // at bridge startup means the first glasses prompt does not pay (or lose)
    // the cold-start race.
    async warmup() {
      if (typeof client.connect !== "function") return false;
      await retryWhileCodexAppServerStarts(config, "warmup", () => client.connect());
      return true;
    },

    // Releases everything the provider keeps alive outside request handling
    // (the codex app-server WebSocket and its pending calls) so a bridge that
    // failed to start can actually exit instead of hanging on a live socket.
    async shutdown() {
      if (typeof client.close === "function") await client.close();
    },

    listProjects(limit) {
      return morpheusProvider.listProjects(limit);
    },

    async listSessions(limit, { projectId } = {}) {
      const sessions = await listCodexSessions(limit, projectId);
      return {
        snapshot: {
          generated_at: Math.floor(config.clock() / 1000),
          summary: `${sessions.length} Codex app-server session${sessions.length === 1 ? "" : "s"}.`,
          counts: sessions.reduce((acc, session) => {
            const status = session.status || "unknown";
            acc[status] = (acc[status] || 0) + 1;
            return acc;
          }, {}),
          policy: {
            raw_terminal_buffers: false,
            source: "codex_app_server",
          },
        },
        sessions,
      };
    },

    async spawnSession({ projectId, goal, prompt }) {
      const project = await projectForProvider(morpheusProvider, projectId, state, config.projectLimit);
      if (!project) throw new Error("Select a Morpheus project before starting a Codex session.");
      const cwd = project.root_path || process.cwd();
      const result = await retryWhileCodexAppServerStarts(config, "spawn_session", () =>
        codex.prompt("", prompt || goal, cwd),
      );
      const sessionId = result.sessionId;
      if (!sessionId) throw new Error("Codex app-server did not return a session id");
      const evenSession = rememberCodexSession(
        codexThreadToEvenSession(
          {
            id: sessionId,
            title: goal,
            timestamp: nowIso(config.clock),
            cwd,
            status: "busy",
          },
          { cwd, clock: config.clock },
        ),
      );
      const mirrorPending = Boolean(config.mirrorCodexTui);
      mirrorSessionToMorpheus({ sessionId, project, goal }).catch((err) => {
        audit("codex_tui_mirror_failed", {
          sessionId,
          projectId: project?.id || project?.tenant_id || "",
          reason: safeJsonError(err),
        });
      });
      return {
        ok: true,
        provider: "codex",
        sessionId,
        evenSession,
        mirrorPending,
        mirrorResult: null,
        result,
      };
    },

    async stageOperatorNote() {
      return {
        ok: true,
        skipped: true,
        reason: "codex_app_server_sessions_use_structured_events",
      };
    },

    async sendPrompt({ sessionId, text, projectId }) {
      if (state.selectedSession?.morpheus && sessionMatches(state.selectedSession, sessionId)) {
        return morpheusProvider.sendPrompt({
          sessionId: state.selectedSession.id,
          text,
          projectId: projectId || state.selectedProject?.id || state.selectedProject?.tenant_id || "",
        });
      }
      const project = await projectForProvider(morpheusProvider, projectId, state, config.projectLimit);
      const cwd = project?.root_path || state.selectedSession?.cwd || process.cwd();
      const result = await retryWhileCodexAppServerStarts(config, "send_prompt", () =>
        codex.prompt(sessionId, text, cwd),
      );
      const resolvedSessionId = result.sessionId || sessionId;
      if (state.selectedSession && sessionMatches(state.selectedSession, sessionId)) {
        rememberCodexSession({
          ...state.selectedSession,
          id: resolvedSessionId,
          status: "busy",
          timestamp: nowIso(config.clock),
        });
      }
      return {
        ok: true,
        provider: "codex",
        sessionId: resolvedSessionId,
        text_chars: text.length,
        result,
      };
    },

    async getSessionStatus(sessionId) {
      return codex.getSessionStatus?.(sessionId) || codex.getStatus?.(sessionId)?.state || "idle";
    },

    async getHistory(sessionId, limit, options = {}) {
      const buffered = historyFromBufferedMessages(getMessages(state, sessionId, 0), limit);
      if (hasAssistantHistory(buffered)) {
        return buffered;
      }
      const allowOutputFallback = options.allowOutputFallback !== false;
      if (codex.getHistory) {
        let persisted = [];
        try {
          persisted = await codex.getHistory(sessionId, limit);
        } catch (err) {
          audit("codex_history_failed", {
            sessionId,
            reason: safeJsonError(err),
          });
          bridgeDebug(config, "codex-history-failed", {
            sessionId,
            reason: safeJsonError(err),
          });
        }
        if (hasAssistantHistory(persisted)) return persisted;
        if (!allowOutputFallback) return buffered.length ? buffered : persisted;
        const outputHistory = await morpheusOutputHistory(
          sessionId,
          limit,
          state.selectedProject?.id || state.selectedProject?.tenant_id || "",
        );
        if (hasAssistantHistory(outputHistory)) return outputHistory;
        return buffered.length ? buffered : persisted;
      }
      if (!allowOutputFallback) return buffered;
      const outputHistory = await morpheusOutputHistory(
        sessionId,
        limit,
        state.selectedProject?.id || state.selectedProject?.tenant_id || "",
      );
      return hasAssistantHistory(outputHistory) ? outputHistory : buffered;
    },

    async sessionOutput({ sessionId, projectId, lines = 10 }) {
      const outputSessionId = morpheusOutputSessionId(sessionId);
      if (!outputSessionId) {
        return { ok: true, skipped: true, output: { text: "", lines: [] } };
      }
      return morpheusProvider.sessionOutput({
        sessionId: outputSessionId,
        projectId: projectId || state.selectedProject?.id || state.selectedProject?.tenant_id || "",
        lines,
      });
    },

    // The omnipresence feed/context surface always lives on the Morpheus CLI,
    // regardless of which agent backend owns conversations.
    feedItems(options) {
      return morpheusProvider.feedItems(options);
    },

    feedAck(options) {
      return morpheusProvider.feedAck(options);
    },

    contextAdd(options) {
      return morpheusProvider.contextAdd(options);
    },

    omniStatus() {
      return morpheusProvider.omniStatus();
    },

    getStatus(sessionId) {
      const local = state.codexSessions.get(sessionId);
      const status = codex.getStatus?.(sessionId);
      if (!local && !status) return null;
      return {
        state: status?.state || local?.status || "idle",
        provider: "codex",
      };
    },

    // The upstream codex provider unsubscribes idle threads after a few
    // minutes (its SSE-client check never sees the bridge's own clients), so
    // turns typed into the laptop TUI stop reaching the bridge. Re-arm the
    // app-server subscription for threads the provider no longer tracks; its
    // auto-discovery rebuilds the session once notifications flow again.
    async ensureLiveEvents(sessionId) {
      const threadId = String(sessionId || "").trim();
      if (!threadId || threadId === "morpheus" || threadId.includes(":")) return false;
      if (typeof codex.getSubscribedSessions !== "function") return false;
      if (isKnownMorpheusSession(threadId)) return false;
      try {
        const tracked = codex.getSubscribedSessions() || [];
        if (tracked.some((entry) => entry?.threadId === threadId)) return true;
      } catch {
        return false;
      }
      const now = config.clock();
      const lastCheck = state.codexLiveEventsCheckedAt.get(threadId) || 0;
      if (now - lastCheck < CODEX_LIVE_EVENTS_CHECK_MS) return false;
      state.codexLiveEventsCheckedAt.set(threadId, now);
      // Client polls can name arbitrary thread ids; drop the oldest check
      // marks (they are re-checkable anyway) so the map stays bounded by the
      // configured cap even when a burst of distinct ids arrives within one
      // freshness window.
      evictOldest(state.codexLiveEventsCheckedAt, {
        cap: state.maxTrackedSessions,
        skip: (id) => id === threadId,
      });
      try {
        await client.threadResume({ threadId });
        bridgeFlow(config, "codex-live-events-resubscribed", { sessionId: threadId });
        return true;
      } catch (err) {
        bridgeDebug(config, "codex-live-events-resubscribe-failed", {
          sessionId: threadId,
          reason: safeJsonError(err),
        });
        return false;
      }
    },

    interrupt(sessionId) {
      codex.interrupt?.(sessionId);
    },
  };
  return providerApi;
}

async function spawnSessionFromText({ req, res, context, requestId, text, project }) {
  const { config, provider, state, audit } = context;
  if (!config.allowSpawn) {
    audit("remote_spawn_rejected", { requestId, reason: "spawn_disabled", projectId: project?.id || "" });
    const body = {
      error: "Remote session spawning is disabled.",
      code: "spawn_disabled",
    };
    rememberReplay(req, state, requestId, 403, body, config.clock, config.requestIdTtlMs);
    res.status(403).json(body);
    return;
  }
  if (!project) {
    audit("remote_spawn_rejected", { requestId, reason: "missing_project" });
    const body = {
      error: "Select a Morpheus project before starting a new G2 session.",
      code: "project_not_selected",
    };
    rememberReplay(req, state, requestId, 409, body, config.clock, config.requestIdTtlMs);
    res.status(409).json(body);
    return;
  }

  const textHash = sha256(text);
  const promptNavigationEpoch = navigationEpoch(state);
  const pendingProjectRow = rememberPendingProjectPrompt(state, project, {
    requestId,
    title: goalFromRemoteText(text),
    timestamp: nowIso(config.clock),
  });
  if (project) {
    rememberProjectContext(state, project);
    state.selectedProject = project;
  }
  if (pendingProjectRow) {
    pushMessageForSession(state, projectSessionId(project), {
      type: "status",
      state: "busy",
      provider: provider.name,
      sessionId: projectSessionId(project),
      at: nowIso(config.clock),
    });
  }
  try {
    const waitFloorId = currentMessageId(state);
    const spawnResult = await provider.spawnSession({
      projectId: project.id || project.tenant_id,
      goal: goalFromRemoteText(text),
      command: config.spawnCommand,
      prompt: text,
    });
    const spawned =
      spawnResult.evenSession ||
      (spawnResult.session ? sessionRowToEvenSession(spawnResult.session) : null);
    if (!spawned) {
      throw new Error("morpheus remote spawn returned no session");
    }
    clearPendingProjectPrompt(state, project, requestId);
    if (navigationEpoch(state) === promptNavigationEpoch) {
      state.selectedProject = project;
      state.selectedSession = spawned;
    }
    rememberActiveProjectSession(state, project, spawned);
    let noteResult = null;
    try {
      noteResult = await provider.stageOperatorNote({
        sessionId: spawned.id,
        text,
        projectId: project.id || project.tenant_id,
        source: "voice_final_transcript",
      });
    } catch (err) {
      noteResult = { ok: false, error: safeJsonError(err) };
    }

    const requestSessionId = typeof req.body?.sessionId === "string" ? req.body.sessionId : "";
    rememberSessionAliases(state, spawned.id, { project, requestSessionId });

    if (!latestTerminalMessage(state, spawned.id)) {
      pushMessageForSession(state, spawned.id, {
        type: "status",
        state: "busy",
        provider: spawned.provider || "codex",
        sessionId: spawned.id,
        at: nowIso(config.clock),
      });
    }
    pushMessageForSession(state, spawned.id, {
      type: "session_started",
      provider: provider.name,
      sessionId: spawned.id,
      textHash,
      textChars: text.length,
      at: nowIso(config.clock),
    });
    scheduleOutputRefresh(context, spawned.id);
    const shouldWaitForResult =
      config.waitForPromptResult && provider.agentBackend === AGENT_BACKEND_CODEX_APP_SERVER;
    const finalMessage = shouldWaitForResult
      ? await waitForResultMessage(state, spawned.id, config.promptWaitForResultMs, waitFloorId)
      : null;
    const responseHistory = bufferedHistoryForRow(
      state,
      requestSessionId || spawned.id,
      10,
      { preferredSessionIds: [spawned.id] },
    );
    const responseText = String(finalMessage?.text || latestHistoryText(responseHistory, "assistant") || "");
    const finalStatus = statusFromBufferedMessages(state, spawned.id, finalMessage ? "idle" : "busy");
    const finalSession = {
      ...spawned,
      status: finalStatus,
      timestamp: nowIso(config.clock),
    };
    rememberActiveProjectSession(state, project, finalSession);
    if (
      navigationEpoch(state) === promptNavigationEpoch &&
      selectedSessionStillMatches(state, project, spawned.id)
    ) {
      state.selectedSession = finalSession;
    }
    const activeMessages = getMessages(state, spawned.id, 0);
    const displayMessages = requestSessionId
      ? getMessages(state, requestSessionId, 0).map((entry) =>
          presentMessageForSession(entry, requestSessionId),
        )
      : activeMessages;

    const body = {
      ok: true,
      action: "spawn_session",
      provider: spawned.provider || spawnResult.provider || provider.name,
      sessionId: spawned.id,
      activeSessionId: spawned.id,
      displaySessionId: activeProjectSessionId(project),
      requestId,
      textHash,
      state: finalStatus,
      text: responseText,
      answer: responseText,
      message: responseText,
      output: responseText ? { text: responseText } : undefined,
      response: responseText,
      history: responseHistory,
      messages: displayMessages.length ? displayMessages : activeMessages,
      activeMessages,
      selectedProject: state.selectedProject || project,
      selectedSession: finalSession,
      result: {
        ...spawnResult,
        text: responseText,
        output: responseText ? { text: responseText } : undefined,
        finalMessage,
      },
      noteResult,
    };
    rememberReplay(req, state, requestId, 202, body, config.clock, config.requestIdTtlMs);
    audit("remote_spawn_session", {
      requestId,
      sessionId: spawned.id,
      projectId: project.id || project.tenant_id,
      textHash,
      textChars: text.length,
    });
    bridgeFlow(config, "prompt-response", {
      action: "spawn_session",
      requestId,
      requestSessionId,
      activeSessionId: spawned.id,
      displaySessionId: activeProjectSessionId(project),
      state: finalStatus,
      responseTextChars: responseText.length,
      activeMessages: activeMessages.length,
      displayMessages: displayMessages.length,
      selectedProjectId: projectKey(state.selectedProject || project) || "",
      selectedSessionId: state.selectedSession?.id || "",
    });
    res.status(202).json(body);
  } catch (err) {
    clearPendingProjectPrompt(state, project, requestId);
    const message = safeJsonError(err);
    const body = { error: message, code: "spawn_failed" };
    // The project row was marked busy before the spawn attempt; publish the
    // failure as that turn's terminal message so polls drop back to idle and
    // the glasses see why nothing started.
    if (pendingProjectRow) {
      publishTurnFailure(
        state,
        config,
        projectSessionId(project),
        provider.name,
        `Could not start a G2 session: ${message}`,
      );
    }
    audit("remote_spawn_failed", {
      requestId,
      projectId: project.id || project.tenant_id,
      reason: message,
    });
    rememberReplay(req, state, requestId, 500, body, config.clock, config.requestIdTtlMs);
    res.status(500).json(body);
  }
}

async function sendPromptToSession({ req, res, context, requestId, text, session }) {
  const { config, provider, state, audit } = context;
  if (!config.allowTerminalPrompts) {
    audit("remote_prompt_rejected", { requestId, reason: "terminal_prompts_disabled", sessionId: session.id });
    const body = {
      error: "Remote prompt submission to terminal sessions is disabled.",
      code: "terminal_prompts_disabled",
    };
    rememberReplay(req, state, requestId, 403, body, config.clock, config.requestIdTtlMs);
    res.status(403).json(body);
    return;
  }

  const textHash = sha256(text);
  const promptNavigationEpoch = navigationEpoch(state);
  // Remembers which session buffer received the prompt_submitted/busy markers
  // so the failure path can publish a terminal message into the same buffers.
  let promptMarkedSessionId = "";
  try {
    const requestSessionId = typeof req.body?.sessionId === "string" ? req.body.sessionId : "";
    const promptProject = state.selectedProject;
    const sessionProjectId = projectIdFromSessionId(session.id);
    const outboundSession =
      (sessionProjectId && state.projectActiveSessions.get(sessionProjectId)) || session;
    const activeProjectId = projectKey(state.selectedProject);
    const rememberedProjectSession = activeProjectId
      ? state.projectActiveSessions.get(activeProjectId)
      : null;
    const waitBeforeIds = new Map(
      [...new Set([outboundSession.id, session.id, rememberedProjectSession?.id, requestSessionId].filter(Boolean))]
        .map((id) => [id, latestMessageId(state, id)]),
    );
    const baselineOutput = await refreshSessionOutputWithBudget({
      provider,
      state,
      config,
      sessionId: outboundSession.id,
      publish: false,
      scheduleOnBudget: false,
    });
    if (baselineOutput.budgetExceeded) {
      // The slow read still captures the pre-prompt screen in the background;
      // record the baseline whenever it lands so stale-mirror holds keep
      // working without blocking the prompt on the terminal read.
      baselineOutput.pending
        .then((result) => rememberPromptMirrorBaseline(state, config, outboundSession.id, result))
        .catch(() => {});
    } else {
      rememberPromptMirrorBaseline(state, config, outboundSession.id, baselineOutput.result);
    }
    rememberSessionAliases(state, outboundSession.id, {
      project: promptProject,
      requestSessionId,
    });
    pushMessageForSession(state, outboundSession.id, {
      type: "prompt_submitted",
      provider: provider.name,
      sessionId: outboundSession.id,
      textHash,
      textChars: text.length,
      source: req.path === "/transcript/finalize" ? "voice_final_transcript" : "typed_prompt",
      at: nowIso(config.clock),
    });
    pushMessageForSession(state, outboundSession.id, {
      type: "status",
      state: "busy",
      provider: outboundSession.provider || provider.name,
      sessionId: outboundSession.id,
      at: nowIso(config.clock),
    });
    promptMarkedSessionId = outboundSession.id;
    const waitFloorId = currentMessageId(state);
    const result = await provider.sendPrompt({
      sessionId: outboundSession.id,
      text,
      projectId: promptProject?.id || promptProject?.tenant_id || "",
      source: req.path === "/transcript/finalize" ? "voice_final_transcript" : "typed_prompt",
    });
    const activeSessionId = result.sessionId || outboundSession.id;
    let activeSession = {
      ...outboundSession,
      id: activeSessionId,
      provider: outboundSession.provider || result.provider || provider.name,
      status: "busy",
      timestamp: nowIso(config.clock),
    };
    if (session.id !== activeSessionId) addSessionAlias(state, activeSessionId, session.id);
    if (activeSessionId !== outboundSession.id) {
      const baseline = state.promptMirrorBaselines.get(outboundSession.id);
      if (baseline) state.promptMirrorBaselines.set(activeSessionId, baseline);
    }
    rememberSessionAliases(state, activeSessionId, {
      project: promptProject,
      requestSessionId,
    });
    const shouldWaitForResult =
      config.waitForPromptResult && provider.agentBackend === AGENT_BACKEND_CODEX_APP_SERVER;
    // A session id first seen at resolution time may already hold a previous
    // turn's result; falling back to the pre-submission floor (instead of 0)
    // keeps waitForResultMessage from short-circuiting on that stale answer.
    const waitAfterId = waitBeforeIds.get(activeSessionId) ?? waitFloorId;
    const finalMessage = shouldWaitForResult
      ? await waitForResultMessage(state, activeSessionId, config.promptWaitForResultMs, waitAfterId)
      : null;
    const responseHistory = bufferedHistoryForRow(
      state,
      requestSessionId || activeSessionId,
      10,
      { preferredSessionIds: [activeSessionId], fallbackSessionIds: [session.id] },
    );
    const responseText = String(finalMessage?.text || latestHistoryText(responseHistory, "assistant") || "");
    const finalStatus = statusFromBufferedMessages(state, activeSessionId, finalMessage ? "idle" : "busy");
    activeSession = {
      ...activeSession,
      status: finalStatus,
      timestamp: nowIso(config.clock),
    };
    rememberActiveProjectSession(state, promptProject, activeSession);
    if (
      navigationEpoch(state) === promptNavigationEpoch &&
      selectedSessionStillMatches(state, promptProject, session.id)
    ) {
      state.selectedSession = activeSession;
    }
    const activeMessages = getMessages(state, activeSessionId, 0);
    const displayMessages = requestSessionId
      ? getMessages(state, requestSessionId, 0).map((entry) =>
          presentMessageForSession(entry, requestSessionId),
        )
      : activeMessages;
    const body = {
      ok: true,
      action: "send_prompt",
      provider: activeSession.provider || result.provider || provider.name,
      sessionId: activeSessionId,
      activeSessionId,
      displaySessionId: promptProject
        ? activeProjectSessionId(promptProject)
        : requestSessionId || session.id,
      requestId,
      textHash,
      state: finalStatus,
      text: responseText,
      answer: responseText,
      message: responseText,
      output: responseText ? { text: responseText } : undefined,
      response: responseText,
      history: responseHistory,
      messages: displayMessages.length ? displayMessages : activeMessages,
      activeMessages,
      result: {
        ...result,
        text: responseText,
        output: responseText ? { text: responseText } : undefined,
        finalMessage,
      },
      selectedProject: state.selectedProject || promptProject,
      selectedSession: activeSession,
    };
    rememberReplay(req, state, requestId, 202, body, config.clock, config.requestIdTtlMs);
    audit("remote_prompt_sent", {
      requestId,
      sessionId: activeSessionId,
      requestedSessionId: session.id,
      projectId: promptProject?.id || promptProject?.tenant_id || "",
      textHash,
      textChars: text.length,
    });
    bridgeFlow(config, "prompt-response", {
      action: "send_prompt",
      requestId,
      requestSessionId,
      activeSessionId,
      displaySessionId: promptProject
        ? activeProjectSessionId(promptProject)
        : requestSessionId || session.id,
      state: finalStatus,
      responseTextChars: responseText.length,
      activeMessages: activeMessages.length,
      displayMessages: displayMessages.length,
      selectedProjectId: projectKey(state.selectedProject || promptProject) || "",
      selectedSessionId: state.selectedSession?.id || "",
    });
    scheduleOutputRefresh(context, activeSessionId);
    res.status(202).json(body);
  } catch (err) {
    const message = safeJsonError(err);
    const body = { error: message, code: "prompt_failed" };
    // The prompt_submitted/busy markers already landed in the session buffers;
    // publish the failure as this turn's terminal message so status polls
    // return to idle, stale-mirror holds disarm, and the glasses see why.
    if (promptMarkedSessionId) {
      publishTurnFailure(state, config, promptMarkedSessionId, provider.name, `Prompt failed: ${message}`);
    }
    audit("remote_prompt_failed", {
      requestId,
      sessionId: session.id,
      reason: message,
    });
    rememberReplay(req, state, requestId, 500, body, config.clock, config.requestIdTtlMs);
    res.status(500).json(body);
  }
}

function refreshSessionOutput({ provider, state, config, sessionId, publish = true }) {
  if (!sessionId || !provider.sessionOutput) return Promise.resolve(null);
  // Reading the terminal mirror shells out to the morpheus CLI, so concurrent
  // client polls and the background poller share one in-flight read per
  // session instead of queueing duplicate runs behind the runner semaphore.
  if (publish) {
    const inflight = state.outputRefreshInflight?.get(sessionId);
    if (inflight) return inflight;
  }
  const run = refreshSessionOutputUncached({ provider, state, config, sessionId, publish });
  if (publish && state.outputRefreshInflight) {
    state.outputRefreshInflight.set(sessionId, run);
    run
      .catch(() => {})
      .finally(() => {
        if (state.outputRefreshInflight.get(sessionId) === run) {
          state.outputRefreshInflight.delete(sessionId);
        }
      });
  }
  return run;
}

async function refreshSessionOutputUncached({ provider, state, config, sessionId, publish }) {
  try {
    const result = await provider.sessionOutput({
      sessionId,
      projectId: state.selectedProject?.id || state.selectedProject?.tenant_id || "",
      lines: 10,
    });
    if (result?.skipped) return result;
    const rawText = String(result?.output?.text || "");
    const text = provider.agentBackend === AGENT_BACKEND_CODEX_APP_SERVER
      ? cleanTerminalMirrorOutput(rawText, {
          sessionId,
          latestUserText: latestUserPromptText(state, sessionId),
        }).trim()
      : rawText.trim();
    const normalizedResult = {
      ...result,
      output: {
        ...(result?.output || {}),
        text,
        lines: text ? text.split("\n") : [],
        line_count: text ? text.split("\n").length : 0,
        char_count: text.length,
      },
    };
    if (provider.agentBackend === AGENT_BACKEND_CODEX_APP_SERVER) {
      bridgeDebug(config, "session-output-normalized", {
        sessionId,
        rawChars: rawText.length,
        textChars: text.length,
      });
    }
    if (!text) return null;
    const outputStatus = toEvenStatus(result?.session?.state) || result?.session?.state || "idle";
    const fingerprint = outputStatus === "busy" || outputStatus === "working"
      ? outputFingerprint(outputStatus, text)
      : resultFingerprint(text);
    if (
      state.outputHashes.get(sessionId) === fingerprint &&
      (outputStatus === "busy" ||
        outputStatus === "working" ||
        resultAlreadyPublishedAfterLatestPrompt(state, sessionId, text))
    ) {
      return normalizedResult;
    }
    if (!publish) {
      state.outputHashes.set(sessionId, fingerprint);
      return normalizedResult;
    }
    // While text_deltas are actively streaming, the codex result event owns
    // turn completion; the mirror shows a half-rendered answer that must not
    // be published as final.
    if (liveDeltaStreamActive(state, config, sessionId)) {
      bridgeDebug(config, "session-output-held-for-live-deltas", { sessionId });
      return normalizedResult;
    }
    // A follow-up prompt leaves the previous answer on the terminal until the
    // TUI redraws for the new turn. Publishing that unchanged screen would
    // resolve the new prompt with the old answer, so hold identical mirror text
    // until it changes, or until the grace window passes with a settled tab.
    const outstandingPrompt = sessionAwaitingResultSinceLatestPrompt(state, sessionId);
    if (outstandingPrompt) {
      const baseline = state.promptMirrorBaselines.get(sessionId);
      if (baseline && baseline.textHash === sha256(text)) {
        const ageMs = config.clock() - baseline.at;
        const settled = outputStatus === "idle";
        if (ageMs < config.staleMirrorGraceMs || !settled) {
          bridgeDebug(config, "session-output-stale-mirror-held", {
            sessionId,
            ageMs,
            outputStatus,
            graceMs: config.staleMirrorGraceMs,
          });
          return normalizedResult;
        }
      }
    }
    // Morpheus marks an open interactive Codex tab as "working" even after the
    // answer is visible and the TUI is waiting for the next prompt. The terminal
    // text itself is the stronger live signal for G2, so publish it instead of
    // hiding it behind the coarse tab state.
    publishAssistantResultIfNew(state, config, sessionId, text, "codex");
    return normalizedResult;
  } catch (err) {
    bridgeDebug(config, "session-output-refresh-failed", {
      sessionId,
      reason: safeJsonError(err),
    });
    config.logger?.warn?.(`[g2-output] ${sessionId}: ${safeJsonError(err)}`);
    return null;
  }
}

const OUTPUT_BUDGET_EXCEEDED = Symbol("output-budget-exceeded");

// Client polls (sessions/messages/events/history) must stay fast even when
// reading the terminal mirror is slow. The refresh keeps running past the
// budget and publishes whenever it completes; the background output poller
// owns retries from then on.
async function refreshSessionOutputWithBudget({
  provider,
  state,
  config,
  sessionId,
  publish = true,
  scheduleOnBudget = true,
}) {
  const pending = refreshSessionOutput({ provider, state, config, sessionId, publish });
  const budgetMs = Number(config.clientPollOutputBudgetMs || 0);
  if (!budgetMs) {
    return { result: await pending, budgetExceeded: false, pending };
  }
  let timer;
  const budget = new Promise((resolve) => {
    timer = setTimeout(() => resolve(OUTPUT_BUDGET_EXCEEDED), Math.max(1, budgetMs));
    if (typeof timer.unref === "function") timer.unref();
  });
  const raced = await Promise.race([pending, budget]);
  clearTimeout(timer);
  if (raced !== OUTPUT_BUDGET_EXCEEDED) {
    return { result: raced, budgetExceeded: false, pending };
  }
  pending.catch(() => {});
  bridgeDebug(config, "session-output-budget-handoff", { sessionId, budgetMs });
  bridgeFlow(config, "session-output-budget-handoff", { sessionId, budgetMs });
  if (scheduleOnBudget) {
    scheduleOutputRefresh({ config, provider, state }, sessionId);
  }
  return { result: null, budgetExceeded: true, pending };
}

async function refreshSessionHistory({
  provider,
  state,
  config,
  sessionId,
  allowOutputFallback = true,
}) {
  if (!sessionId || !provider.getHistory) return null;
  try {
    // Codex streams the answer as text_delta events while persisting partial
    // assistant text into thread history. Publishing that mid-turn history
    // would resolve the prompt wait with a truncated answer, so while deltas
    // are actively arriving the real `result` event owns turn completion.
    if (liveDeltaStreamActive(state, config, sessionId)) {
      bridgeDebug(config, "session-history-held-for-live-deltas", { sessionId });
      return { history: [], text: "", published: false };
    }
    const history = await provider.getHistory(sessionId, 10, { allowOutputFallback });
    // While a prompt is outstanding, the latest assistant entry in history is
    // usually the answer to the PREVIOUS turn. Publishing it would feed the old
    // answer back as the new result, so only accept history text that follows
    // the outstanding prompt itself; otherwise report stale so callers can use
    // the live terminal mirror instead.
    const outstandingPrompt = sessionAwaitingResultSinceLatestPrompt(state, sessionId);
    const text = outstandingPrompt
      ? historyAnswerForPrompt(history, outstandingPrompt)
      : latestHistoryText(history, "assistant");
    if (!text) {
      if (outstandingPrompt && hasAssistantHistory(history)) {
        bridgeDebug(config, "session-history-stale-while-awaiting", {
          sessionId,
          entries: history.length,
        });
      }
      return { history, text: "", published: false };
    }
    const published = publishAssistantResultIfNew(state, config, sessionId, text, provider.name || "codex");
    return { history, text, published };
  } catch (err) {
    bridgeDebug(config, "session-history-refresh-failed", {
      sessionId,
      reason: safeJsonError(err),
    });
    return null;
  }
}

function shouldPreferThreadHistoryForClientPoll(provider, state, sessionId) {
  if (provider.agentBackend !== AGENT_BACKEND_CODEX_APP_SERVER) return false;
  const id = String(sessionId || "");
  const selectedIsMorpheus =
    state.selectedSession?.morpheus && sessionMatches(state.selectedSession, id);
  if (selectedIsMorpheus) return false;
  for (const session of state.projectActiveSessions.values()) {
    if (session?.morpheus && sessionMatches(session, id)) return false;
  }
  return true;
}

async function refreshSessionForSessionsPoll({
  provider,
  state,
  config,
  sessionId,
  reason = "sessions_poll",
  outputFallback = true,
}) {
  if (!sessionId) return { refreshed: false, published: false };
  // The feed pseudo-session has no terminal mirror or thread history behind
  // it; the feed poller owns its refreshes.
  if (isFeedSessionId(sessionId)) return { refreshed: false, published: false, status: "idle" };
  const beforeLatestId = latestMessageId(state, sessionId);
  const beforeHash = state.outputHashes.get(sessionId) || "";
  const preferThreadHistory = shouldPreferThreadHistoryForClientPoll(provider, state, sessionId);
  if (preferThreadHistory) void provider.ensureLiveEvents?.(sessionId);
  const outputPollActive = outputPollIsActive(state, sessionId);
  const firstHistoryResult = preferThreadHistory
    ? await refreshSessionHistory({
        provider,
        state,
        config,
        sessionId,
        allowOutputFallback: false,
      })
    : null;
  const firstHistoryText = String(firstHistoryResult?.text || "").trim();
  const shouldUseOutputFallback =
    outputFallback && !firstHistoryText && (!preferThreadHistory || !outputPollActive);
  const budgetedOutput = shouldUseOutputFallback
    ? await refreshSessionOutputWithBudget({ provider, state, config, sessionId })
    : null;
  const outputResult = budgetedOutput?.result || null;
  const outputBudgetExceeded = Boolean(budgetedOutput?.budgetExceeded);
  const outputText = String(outputResult?.output?.text || "").trim();
  let historyResult = null;
  if (firstHistoryText) {
    historyResult = firstHistoryResult;
  } else if (!outputText && !preferThreadHistory) {
    historyResult = await refreshSessionHistory({
      provider,
      state,
      config,
      sessionId,
      allowOutputFallback: outputFallback && !outputBudgetExceeded,
    });
  } else if (preferThreadHistory) {
    historyResult = firstHistoryResult;
  }
  const directStatus = provider.getStatus?.(sessionId);
  const bufferedStatus = statusFromBufferedMessages(state, sessionId, "idle");
  const outputStatus =
    toEvenStatus(outputResult?.session?.state) ||
    outputResult?.session?.state ||
    "";
  const status = String(historyResult?.text || "").trim()
    ? bufferedStatus
    : outputStatus || directStatus?.state || bufferedStatus;
  const afterLatestId = latestMessageId(state, sessionId);
  const afterHash = state.outputHashes.get(sessionId) || "";
  const historyText = String(historyResult?.text || "");
  const published =
    afterLatestId !== beforeLatestId ||
    afterHash !== beforeHash ||
    Boolean(historyResult?.published);

  bridgeDebug(config, "sessions-poll-refresh", {
    reason,
    sessionId,
    status,
    preferThreadHistory,
    outputPollActive,
    outputPollStopReason: state.outputPollStats?.get(sessionId)?.stopReason || "",
    outputChars: outputText.length,
    historyChars: historyText.length,
    published,
  });
  if (published || outputText.length || historyText.length) {
    bridgeFlow(config, "sessions-poll-refresh", {
      reason,
      sessionId,
      status,
      preferThreadHistory,
      outputPollActive,
      outputPollStopReason: state.outputPollStats?.get(sessionId)?.stopReason || "",
      outputChars: outputText.length,
      historyChars: historyText.length,
      published,
      latestId: afterLatestId,
    });
  }
  return {
    refreshed: Boolean(outputResult || historyResult),
    published,
    status,
    outputResult,
    historyResult,
  };
}

async function refreshSelectedSessionForSessionsPoll({
  provider,
  state,
  config,
  reason,
  outputFallback = true,
}) {
  const ids = [];
  const activeProjectSession = state.selectedProject
    ? activeSessionForProject(state, state.selectedProject)
    : null;
  if (activeProjectSession?.id) ids.push(activeProjectSession.id);
  if (state.selectedSession?.id) ids.push(state.selectedSession.id);

  let published = false;
  // Only the first session may fall back to the (budgeted) terminal read, so
  // one client poll never stacks multiple output budgets.
  let allowOutput = outputFallback;
  for (const sessionId of [...new Set(ids)]) {
    const result = await refreshSessionForSessionsPoll({
      provider,
      state,
      config,
      sessionId,
      reason,
      outputFallback: allowOutput,
    });
    allowOutput = false;
    published = published || Boolean(result.published);
  }
  return { published };
}

function rememberOutputPollState(state, sessionId, patch) {
  if (!sessionId) return;
  const previous = state.outputPollStats?.get(sessionId) || {};
  state.outputPollStats?.set(sessionId, {
    ...previous,
    sessionId,
    ...patch,
  });
}

function outputPollIsActive(state, sessionId) {
  return Boolean(sessionId && state.outputPollers.has(sessionId));
}

function stopOutputPoller(state, sessionId, reason = "stop") {
  const timer = state.outputPollers.get(sessionId);
  if (timer) {
    clearInterval(timer);
    state.outputPollers.delete(sessionId);
  }
  rememberOutputPollState(state, sessionId, {
    active: false,
    stoppedAt: Date.now(),
    stopReason: reason,
  });
}

function scheduleOutputRefresh(context, sessionId) {
  const { config, provider, state } = context;
  if (!sessionId || !provider.sessionOutput || state.outputPollers.has(sessionId)) return;
  if (config.outputPollAttempts <= 0) return;
  if (latestTerminalMessageAfterLatestPrompt(state, sessionId)) return;
  const pollAfterId = latestMessageId(state, sessionId);
  let attempts = 0;
  let skippedAttempts = 0;
  let running = false;
  rememberOutputPollState(state, sessionId, {
    active: true,
    startedAt: config.clock(),
    pollAfterId,
    attempts,
    skippedAttempts,
    published: false,
    stopReason: "",
  });
  bridgeDebug(config, "output-poller-start", {
    sessionId,
    pollAfterId,
    intervalMs: config.outputPollIntervalMs,
    maxAttempts: config.outputPollAttempts,
  });
  bridgeFlow(config, "output-poller-start", {
    sessionId,
    pollAfterId,
    intervalMs: config.outputPollIntervalMs,
    maxAttempts: config.outputPollAttempts,
  });
  const tick = async () => {
    if (!state.outputPollers.has(sessionId)) return;
    if (running) return;
    running = true;
    try {
      if (latestTerminalMessage(state, sessionId, pollAfterId)) {
        bridgeDebug(config, "output-poller-stop", {
          sessionId,
          reason: "result-message-already-present",
          attempts,
          skippedAttempts,
        });
        stopOutputPoller(state, sessionId, "result-message-already-present");
        return;
      }
      const before = state.outputHashes.get(sessionId);
      const preferThreadHistory = shouldPreferThreadHistoryForClientPoll(provider, state, sessionId);
      const firstHistoryResult = preferThreadHistory
        ? await refreshSessionHistory({
            provider,
            state,
            config,
            sessionId,
            allowOutputFallback: false,
          })
        : null;
      const firstHistoryText = String(firstHistoryResult?.text || "").trim();
      const result = firstHistoryText
        ? null
        : await refreshSessionOutput({ provider, state, config, sessionId });
      const skipped = Boolean(result?.skipped);
      if (skipped) {
        skippedAttempts += 1;
      } else {
        attempts += 1;
      }
      let historyResult = null;
      if (firstHistoryText) {
        historyResult = firstHistoryResult;
      } else if (!result?.output?.text && !preferThreadHistory) {
        historyResult = await refreshSessionHistory({ provider, state, config, sessionId });
      }
      const after = state.outputHashes.get(sessionId);
      const outputStatus = toEvenStatus(result?.session?.state) || result?.session?.state || "idle";
      const isBusy = outputStatus === "busy" || outputStatus === "working";
      const published =
        Boolean(firstHistoryResult?.published) ||
        Boolean(historyResult?.published) ||
        Boolean(result?.output?.text && after && after !== before);
      rememberOutputPollState(state, sessionId, {
        active: true,
        lastAttemptAt: config.clock(),
        attempts,
        skippedAttempts,
        preferThreadHistory,
        firstHistoryChars: firstHistoryText.length,
        outputChars: String(result?.output?.text || "").length,
        outputStatus,
        isBusy,
        published,
      });
      bridgeDebug(config, "output-poller-tick", {
        sessionId,
        attempts,
        skippedAttempts,
        preferThreadHistory,
        firstHistoryChars: firstHistoryText.length,
        outputChars: String(result?.output?.text || "").length,
        outputStatus,
        isBusy,
        published,
      });
      if (published || attempts >= config.outputPollAttempts) {
        bridgeDebug(config, "output-poller-stop", {
          sessionId,
          reason: published ? "published" : "attempts-exhausted",
          attempts,
          skippedAttempts,
        });
        bridgeFlow(config, "output-poller-stop", {
          sessionId,
          reason: published ? "published" : "attempts-exhausted",
          attempts,
          skippedAttempts,
          outputChars: String(result?.output?.text || "").length,
          historyChars: String(historyResult?.text || firstHistoryText || "").length,
        });
        stopOutputPoller(state, sessionId, published ? "published" : "attempts-exhausted");
      } else if (skipped && skippedAttempts < Math.max(3, config.outputPollAttempts)) {
        const retry = setTimeout(tick, Math.min(750, config.outputPollIntervalMs));
        if (typeof retry.unref === "function") retry.unref();
      } else if (skipped) {
        bridgeDebug(config, "output-poller-stop", {
          sessionId,
          reason: "skipped-exhausted",
          attempts,
          skippedAttempts,
        });
        stopOutputPoller(state, sessionId, "skipped-exhausted");
      }
    } finally {
      running = false;
    }
  };
  const timer = setInterval(tick, config.outputPollIntervalMs);
  if (typeof timer.unref === "function") timer.unref();
  state.outputPollers.set(sessionId, timer);
  setTimeout(tick, Math.min(750, config.outputPollIntervalMs));
}

function navigationView(state) {
  if (state.selectedSession) return "session";
  if (state.selectedProject) return "sessions";
  return "projects";
}

function navigationPayload(state, extra = {}) {
  const view = navigationView(state);
  return {
    ok: true,
    view,
    mode: view,
    selectedProject: state.selectedProject,
    selectedSession: state.selectedSession,
    ...extra,
  };
}

function bridgeLimits(config) {
  return {
    maxPromptChars: config.maxPromptChars,
    requestIdTtlMs: config.requestIdTtlMs,
    rateLimitWindowMs: config.rateLimitWindowMs,
    rateLimitMax: config.rateLimitMax,
    sessionLimit: config.sessionLimit,
    projectLimit: config.projectLimit,
  };
}

function bridgePolicy(config, provider) {
  return {
    rawTerminalKeystrokes: false,
    remoteApprovals: false,
    destructiveActions: false,
    promptBehavior: provider.promptBehavior,
    agentBackend: config.agentBackend,
    codexAppServerPort: config.codexAppServerPort,
    mirrorCodexTui: config.mirrorCodexTui,
    includeCodexHistory: config.includeCodexHistory,
    waitForPromptResult: config.waitForPromptResult,
    promptWaitForResultMs: config.promptWaitForResultMs,
    spawnCommand: config.spawnCommand,
    remoteSpawn: config.allowSpawn,
    terminalPromptSubmit: config.allowTerminalPrompts,
    interruptNavigatesBack: config.interruptNavigatesBack,
    directBackToProjects: config.directBackToProjects,
    showBackToProjectsRow: config.showBackToProjectsRow,
    voiceInputTrust: "untrusted_final_transcript_only",
  };
}

function deviceStateSnapshot({ state, config, provider }) {
  const view = navigationView(state);
  const selected = selectedSessionResponse(state);
  const selectedProjectId = projectKey(state.selectedProject) || null;
  const activeProjectSession = state.selectedProject
    ? activeSessionForProject(state, state.selectedProject)
    : null;
  const pendingProjectSession = state.selectedProject
    ? pendingProjectPromptForProject(state, state.selectedProject)
    : null;
  const selectedSession = selected.selectedSession || state.selectedSession || null;
  const selectedSessionId = selectedSession?.id || state.selectedSession?.id || null;
  const projectActiveSessionIdValue =
    selected.projectActiveSessionId ||
    (activeProjectSession?.id && state.selectedProject
      ? activeProjectSessionId(state.selectedProject)
      : pendingProjectSession?.projectActiveSessionId || pendingProjectSession?.id || null);
  const status = selected.state || selectedSession?.status || pendingProjectSession?.status || "idle";
  return {
    ok: true,
    bridge: "g2",
    provider: provider.name,
    view,
    mode: view,
    selectedProject: state.selectedProject,
    selectedSession,
    selectedProjectId,
    selectedSessionId,
    activeSessionId: selected.activeSessionId || activeProjectSession?.id || null,
    projectActiveSessionId: projectActiveSessionIdValue,
    state: status,
    status,
    stale: false,
    allowedActions: provider.allowedActions || [],
    limits: bridgeLimits(config),
    policy: bridgePolicy(config, provider),
  };
}

function navigateBack(state, options = {}) {
  const { directBackToProjects = true } = options;
  const from = navigationView(state);
  const priorSession = state.selectedSession;
  const priorProject = state.selectedProject;
  if (state.selectedSession) {
    state.selectedSession = null;
    if (directBackToProjects) {
      state.selectedProject = null;
    }
  } else if (state.selectedProject) {
    state.selectedProject = null;
  }
  const to = navigationView(state);
  bumpNavigationEpoch(state);
  return {
    from,
    to,
    changed: from !== to,
    priorProject,
    priorSession,
  };
}

async function handleNavigateBack(req, res, { config, state, audit }) {
  const requestId = writeRequestId(req);
  if (await maybeReplay(req, res, state, requestId)) return;
  if (!reserveReplay(req, state, requestId, config.clock, config.requestIdTtlMs)) {
    if (await maybeReplay(req, res, state, requestId)) return;
  }
  const result = navigateBack(state, { directBackToProjects: config.directBackToProjects });
  const body = navigationPayload(state, {
    action: "navigate_back",
    requestId,
    from: result.from,
    to: result.to,
    changed: result.changed,
  });
  rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
  audit("navigate_back", {
    requestId,
    from: result.from,
    to: result.to,
    changed: result.changed,
    priorProjectId: result.priorProject?.id || result.priorProject?.tenant_id || "",
    priorSessionId: result.priorSession?.id || "",
  });
  res.json(body);
}

// Stock Even clients treat every row as a conversation and will happily speak
// at the feed. The feed is read-only in v1: instead of spawning a Codex
// session, answer with an assistant-style notice (also buffered into feed:main
// so message/history pollers render the same reply) and keep the feed
// selected. This is the least surprising stock-client behavior: the glasses
// show a normal assistant answer, and the feed keeps streaming.
function respondFeedPromptReadOnly({ req, res, context, requestId, text }) {
  const { config, state, audit } = context;
  touchFeedSubscription(context);
  const textHash = sha256(text);
  pushMessageForSession(state, FEED_SESSION_ID, {
    type: "result",
    success: true,
    provider: "morpheus-feed",
    sessionId: FEED_SESSION_ID,
    text: FEED_READ_ONLY_NOTICE,
    feedNotice: true,
    at: nowIso(config.clock),
  });
  const messages = getMessages(state, FEED_SESSION_ID, 0).map((entry) =>
    presentMessageForSession(entry, FEED_SESSION_ID),
  );
  const body = {
    ok: true,
    action: "feed_read_only",
    provider: "morpheus-feed",
    sessionId: FEED_SESSION_ID,
    requestId,
    textHash,
    state: "idle",
    text: FEED_READ_ONLY_NOTICE,
    answer: FEED_READ_ONLY_NOTICE,
    message: FEED_READ_ONLY_NOTICE,
    response: FEED_READ_ONLY_NOTICE,
    output: { text: FEED_READ_ONLY_NOTICE },
    history: historyFromBufferedMessages(getMessages(state, FEED_SESSION_ID, 0), 10),
    messages,
    selectedSession: state.selectedSession,
  };
  rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
  audit("remote_prompt_feed_read_only", {
    requestId,
    textHash,
    textChars: text.length,
  });
  res.status(200).json(body);
}

async function submitTextToMorpheus(req, res, context) {
  const { config, provider, state, audit } = context;
  const requestId = writeRequestId(req, state);
  if (await maybeReplay(req, res, state, requestId)) return;

  const textResult = validateText(req.body?.text, config.maxPromptChars);
  if (!textResult.ok) {
    audit("remote_text_rejected", {
      requestId,
      reason: textResult.code || "invalid_text",
      textChars: typeof req.body?.text === "string" ? req.body.text.length : 0,
    });
    res.status(textResult.status).json({
      error: textResult.error,
      code: textResult.code || "invalid_text",
    });
    return;
  }

  if (!reserveReplay(req, state, requestId, config.clock, config.requestIdTtlMs)) {
    if (await maybeReplay(req, res, state, requestId)) return;
  }

  const rawBodySessionId = typeof req.body?.sessionId === "string" ? req.body.sessionId : "";
  // Prompts sent while a projects-menu row is open ("Back to projects") used
  // to die with selected_session_stale. The menu row is navigation, not a
  // session, so route the text like a sessionless prompt: project context
  // (selected or last project) decides between spawn and follow-up.
  const bodySessionId = isProjectMenuSessionId(rawBodySessionId) ? "" : rawBodySessionId;
  if (rawBodySessionId && !bodySessionId) {
    bridgeFlow(config, "prompt-menu-row-redirect", {
      requestId,
      requestSessionId: rawBodySessionId,
      selectedProjectId: projectKey(state.selectedProject) || "",
      lastProjectId: projectKey(state.lastProject) || "",
    });
  }
  // Prompts aimed at the read-only omnipresence feed must never spawn Codex.
  if (
    isFeedSessionId(bodySessionId) ||
    (!bodySessionId && isFeedSessionId(state.selectedSession?.id))
  ) {
    respondFeedPromptReadOnly({ req, res, context, requestId, text: textResult.text });
    return;
  }
  const activeProjectSessionProjectId = projectIdFromActiveSessionId(bodySessionId);
  if (activeProjectSessionProjectId) {
    const project = await resolveProject(provider, activeProjectSessionProjectId, config.projectLimit);
    if (!project.ok) {
      const body = { error: project.error, code: "project_not_found" };
      audit("remote_prompt_rejected", {
        requestId,
        reason: "project_not_found",
        projectId: activeProjectSessionProjectId,
      });
      rememberReplay(req, state, requestId, project.status, body, config.clock, config.requestIdTtlMs);
      res.status(project.status).json(body);
      return;
    }
    const resolvedProjectId = project.project.id || project.project.tenant_id || activeProjectSessionProjectId;
    await runProjectPromptLocked(state, resolvedProjectId, async () => {
      const activeSession = state.projectActiveSessions.get(resolvedProjectId);
      if (!activeSession) {
        rememberProjectContext(state, project.project);
        state.selectedProject = project.project;
        state.selectedSession = null;
        await spawnSessionFromText({ req, res, context, requestId, text: textResult.text, project: project.project });
        return;
      }
      rememberProjectContext(state, project.project);
      state.selectedProject = project.project;
      state.selectedSession = activeSession;
      await sendPromptToSession({
        req,
        res,
        context,
        requestId,
        text: textResult.text,
        session: activeSession,
      });
    });
    return;
  }

  const projectId = projectIdFromSessionId(bodySessionId);
  if (projectId) {
    const project = await resolveProject(provider, projectId, config.projectLimit);
    if (!project.ok) {
      const body = { error: project.error, code: "project_not_found" };
      audit("remote_spawn_rejected", { requestId, reason: "project_not_found", projectId });
      rememberReplay(req, state, requestId, project.status, body, config.clock, config.requestIdTtlMs);
      res.status(project.status).json(body);
      return;
    }
    const resolvedProjectId = project.project.id || project.project.tenant_id || "";
    await runProjectPromptLocked(state, resolvedProjectId, async () => {
      const currentSelectedProjectId = state.selectedProject?.id || state.selectedProject?.tenant_id || "";
      if (state.selectedSession && currentSelectedProjectId === resolvedProjectId) {
        await sendPromptToSession({
          req,
          res,
          context,
          requestId,
          text: textResult.text,
          session: state.selectedSession,
        });
        return;
      }
      const rememberedSession = state.projectActiveSessions.get(resolvedProjectId);
      if (rememberedSession) {
        rememberProjectContext(state, project.project);
        state.selectedProject = project.project;
        state.selectedSession = rememberedSession;
        await sendPromptToSession({
          req,
          res,
          context,
          requestId,
          text: textResult.text,
          session: rememberedSession,
        });
        return;
      }
      rememberProjectContext(state, project.project);
      state.selectedProject = project.project;
      await spawnSessionFromText({ req, res, context, requestId, text: textResult.text, project: project.project });
    });
    return;
  }

  let targetSessionId = "";
  if (bodySessionId) {
    targetSessionId = bodySessionId;
  } else if (state.selectedSession) {
    targetSessionId = state.selectedSession.id;
  }

  if (!targetSessionId) {
    const promptProject = projectContextForPrompt(state);
    if (!promptProject) {
      // Stock Even clients render nothing for error-shaped responses, so a
      // bare 409 leaves the glasses stuck on an empty "Waiting input" view
      // (e.g. after prompting from the client's own "+ Add session" row).
      // Answer with a rendered assistant notice instead, mirroring the
      // feed:main read-only response shape.
      const projectNames = (cachedProjects(state, 3)?.projects || [])
        .map((project) => project?.name || project?.id || "")
        .filter(Boolean);
      const notice = projectNames.length
        ? `No project is open yet. Go back and open a project first (${projectNames.join(", ")}), then speak again.`
        : "No project is open yet. Go back and open a project row first, then speak again.";
      const textHash = sha256(textResult.text);
      // Stock clients reject prompt responses without a session id ("empty
      // session id"), so the notice rides a synthetic session whose buffer
      // holds the notice for follow-up message/history polls.
      const noticeSessionId = bodySessionId || PROJECT_NOTICE_SESSION_ID;
      pushMessageForSession(state, noticeSessionId, {
        type: "result",
        success: true,
        provider: provider.name,
        sessionId: noticeSessionId,
        text: notice,
        promptNotice: true,
        at: nowIso(config.clock),
      });
      const body = {
        ok: true,
        action: "project_not_selected_notice",
        code: "project_not_selected",
        provider: provider.name,
        sessionId: noticeSessionId,
        activeSessionId: noticeSessionId,
        requestId,
        textHash,
        state: "idle",
        text: notice,
        answer: notice,
        message: notice,
        response: notice,
        output: { text: notice },
        history: [{ role: "assistant", text: notice }],
        messages: getMessages(state, noticeSessionId, 0).map((entry) =>
          presentMessageForSession(entry, noticeSessionId),
        ),
        selectedSession: state.selectedSession,
      };
      audit("remote_text_rejected", {
        requestId,
        reason: "project_not_selected",
        lastProjectId: projectKey(state.lastProject) || "",
      });
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      res.status(200).json(body);
      return;
    }
    if (!state.selectedProject && state.lastProject) {
      bridgeDebug(config, "prompt-using-remembered-project", {
        requestId,
        projectId: projectKey(promptProject),
      });
      audit("remote_prompt_remembered_project", {
        requestId,
        projectId: projectKey(promptProject),
      });
    }
    rememberProjectContext(state, promptProject);
    state.selectedProject = promptProject;
    await spawnSessionFromText({ req, res, context, requestId, text: textResult.text, project: promptProject });
    return;
  }

  const latest = await resolveSession(provider, targetSessionId, config.sessionLimit, {
    projectId: state.selectedProject?.id || state.selectedProject?.tenant_id || "",
  });
  if (!latest.ok && bodySessionId && !state.selectedProject) {
    const globalLatest = await resolveSession(provider, targetSessionId, config.sessionLimit);
    if (globalLatest.ok) {
      latest.ok = true;
      latest.session = globalLatest.session;
      latest.snapshot = globalLatest.snapshot;
    }
  }
  if (!latest.ok) {
    state.selectedSession = null;
    const body = {
      error: "Selected session is no longer available; select a session again.",
      code: "selected_session_stale",
    };
    audit("remote_text_rejected", { requestId, reason: "selected_session_stale" });
    rememberReplay(req, state, requestId, 409, body, config.clock, config.requestIdTtlMs);
    res.status(409).json(body);
    return;
  }
  state.selectedSession = latest.session;

  await sendPromptToSession({ req, res, context, requestId, text: textResult.text, session: latest.session });
}

function buildConfigFromEnv(env = process.env, argv = process.argv.slice(2)) {
  const tokenFromEnv = env.MORPHEUS_G2_TOKEN || "";
  const token = tokenFromEnv || crypto.randomBytes(24).toString("hex");
  const host = argValue(argv, "--host") || env.HOST || DEFAULT_HOST;
  const port = envInt({ PORT: argValue(argv, "--port") || env.PORT }, "PORT", DEFAULT_PORT, {
    min: 1,
    max: 65535,
  });
  const publicUrl = trimTrailingSlash(
    argValue(argv, "--public-url") || env.MORPHEUS_G2_PUBLIC_URL || "",
  );

  return {
    host,
    port,
    publicUrl,
    localUrl: `http://${host}:${port}`,
    token,
    tokenSource: tokenFromEnv ? "env" : "ephemeral",
    morpheusBin: env.MORPHEUS_BIN || "morpheus",
    allowedOrigins: envList(env.MORPHEUS_G2_ALLOWED_ORIGINS),
    allowedHosts: envList(env.MORPHEUS_G2_ALLOWED_HOSTS),
    allowAnyHost: env.MORPHEUS_G2_ALLOW_ANY_HOST === "1",
    allowUnsafeBind: env.MORPHEUS_G2_ALLOW_UNSAFE_BIND === "1",
    allowWildcardOrigin: env.MORPHEUS_G2_ALLOW_WILDCARD_ORIGIN === "1",
    evenAppCors: env.MORPHEUS_G2_EVEN_APP_CORS !== "0",
    acceptQueryToken: env.MORPHEUS_G2_ACCEPT_QUERY_TOKEN !== "0",
    maxPromptChars: envInt(env, "MORPHEUS_G2_MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS, {
      min: 1,
      max: 2000,
    }),
    jsonLimit: env.MORPHEUS_G2_JSON_LIMIT || DEFAULT_JSON_LIMIT,
    runnerTimeoutMs: envInt(env, "MORPHEUS_G2_RUNNER_TIMEOUT_MS", DEFAULT_RUNNER_TIMEOUT_MS, {
      min: 100,
      max: 60_000,
    }),
    outputRunnerTimeoutMs: envInt(
      env,
      "MORPHEUS_G2_OUTPUT_RUNNER_TIMEOUT_MS",
      DEFAULT_OUTPUT_RUNNER_TIMEOUT_MS,
      { min: 100, max: 5 * 60_000 },
    ),
    runnerOutputBytes: envInt(
      env,
      "MORPHEUS_G2_RUNNER_OUTPUT_BYTES",
      DEFAULT_RUNNER_OUTPUT_BYTES,
      { min: 1024, max: 4 * 1024 * 1024 },
    ),
    runnerConcurrency: envInt(
      env,
      "MORPHEUS_G2_RUNNER_CONCURRENCY",
      DEFAULT_RUNNER_CONCURRENCY,
      { min: 1, max: 8 },
    ),
    promptWaitForResultMs: envInt(
      env,
      "MORPHEUS_G2_PROMPT_WAIT_FOR_RESULT_MS",
      DEFAULT_PROMPT_WAIT_FOR_RESULT_MS,
      { min: 1, max: 5 * 60_000 },
    ),
    outputPollIntervalMs: envInt(
      env,
      "MORPHEUS_G2_OUTPUT_POLL_INTERVAL_MS",
      DEFAULT_OUTPUT_POLL_INTERVAL_MS,
      { min: 250, max: 30_000 },
    ),
    outputPollAttempts: envInt(
      env,
      "MORPHEUS_G2_OUTPUT_POLL_ATTEMPTS",
      DEFAULT_OUTPUT_POLL_ATTEMPTS,
      { min: 1, max: 300 },
    ),
    staleMirrorGraceMs: envInt(
      env,
      "MORPHEUS_G2_STALE_MIRROR_GRACE_MS",
      DEFAULT_STALE_MIRROR_GRACE_MS,
      { min: 1, max: 5 * 60_000 },
    ),
    feedPollMs: envInt(env, "MORPHEUS_G2_FEED_POLL_MS", DEFAULT_FEED_POLL_MS, {
      min: 250,
      max: 5 * 60_000,
    }),
    feedSubscriberIdleMs: envInt(
      env,
      "MORPHEUS_G2_FEED_SUBSCRIBER_IDLE_MS",
      DEFAULT_FEED_SUBSCRIBER_IDLE_MS,
      { min: 1000, max: 60 * 60_000 },
    ),
    omniStatusTtlMs: envInt(env, "MORPHEUS_G2_OMNI_STATUS_TTL_MS", DEFAULT_OMNI_STATUS_TTL_MS, {
      min: 250,
      max: 60 * 60_000,
    }),
    codexAppServerPort: envInt(
      env,
      "CODEX_APP_SERVER_PORT",
      DEFAULT_CODEX_APP_SERVER_PORT,
      { min: 1, max: 65535 },
    ),
    codexAppServerStartupWaitMs: envInt(
      env,
      "MORPHEUS_G2_CODEX_STARTUP_WAIT_MS",
      DEFAULT_CODEX_APP_SERVER_STARTUP_WAIT_MS,
      { min: 0, max: 5 * 60_000 },
    ),
    warmCodexAppServer: env.MORPHEUS_G2_WARM_CODEX_APP_SERVER !== "0",
    requestIdTtlMs: envInt(env, "MORPHEUS_G2_REQUEST_ID_TTL_MS", DEFAULT_REQUEST_ID_TTL_MS, {
      min: 1000,
      max: 60 * 60 * 1000,
    }),
    rateLimitWindowMs: envInt(env, "MORPHEUS_G2_RATE_LIMIT_WINDOW_MS", DEFAULT_RATE_LIMIT_WINDOW_MS, {
      min: 1000,
      max: 60 * 60 * 1000,
    }),
    rateLimitMax: envInt(env, "MORPHEUS_G2_RATE_LIMIT_MAX", DEFAULT_RATE_LIMIT_MAX, {
      min: 1,
      max: 10_000,
    }),
    rateLimitReadMax: envInt(env, "MORPHEUS_G2_RATE_LIMIT_READ_MAX", DEFAULT_RATE_LIMIT_READ_MAX, {
      min: 1,
      max: 100_000,
    }),
    clientPollOutputBudgetMs: envInt(
      env,
      "MORPHEUS_G2_CLIENT_POLL_OUTPUT_BUDGET_MS",
      DEFAULT_CLIENT_POLL_OUTPUT_BUDGET_MS,
      { min: 100, max: 30_000 },
    ),
    sessionLimit: envInt(env, "MORPHEUS_G2_SESSION_LIMIT", 12, { min: 1, max: 50 }),
    projectLimit: envInt(env, "MORPHEUS_G2_PROJECT_LIMIT", DEFAULT_PROJECT_LIMIT, { min: 1, max: 50 }),
    maxTrackedSessions: envInt(env, "MORPHEUS_G2_MAX_TRACKED_SESSIONS", MAX_TRACKED_SESSIONS, {
      min: 8,
      max: 10_000,
    }),
    spawnCommand: env.MORPHEUS_G2_SPAWN_COMMAND || "codex",
    agentBackend: env.MORPHEUS_G2_AGENT_BACKEND || AGENT_BACKEND_CODEX_APP_SERVER,
    mirrorCodexTui: env.MORPHEUS_G2_MIRROR_CODEX_TUI !== "0",
    includeCodexHistory: env.MORPHEUS_G2_INCLUDE_CODEX_HISTORY !== "0",
    requestLog: env.MORPHEUS_G2_REQUEST_LOG !== "0",
    debug: env.MORPHEUS_G2_DEBUG === "1",
    flowLog: env.MORPHEUS_G2_FLOW_LOG !== "0",
    waitForPromptResult: !["0", "false", "no", "off"].includes(
      String(env.MORPHEUS_G2_WAIT_FOR_RESULT || "1").toLowerCase(),
    ),
    allowSpawn: env.MORPHEUS_G2_ALLOW_SPAWN !== "0",
    allowTerminalPrompts: env.MORPHEUS_G2_ALLOW_TERMINAL_PROMPTS !== "0",
    interruptNavigatesBack: env.MORPHEUS_G2_INTERRUPT_NAVIGATES_BACK !== "0",
    directBackToProjects: env.MORPHEUS_G2_DIRECT_BACK_TO_PROJECTS !== "0",
    showBackToProjectsRow: env.MORPHEUS_G2_SHOW_BACK_ROW !== "0",
    showProjectsFirst: env.MORPHEUS_G2_SHOW_PROJECTS_FIRST !== "0",
    auditPath:
      env.MORPHEUS_G2_AUDIT_LOG === "off"
        ? ""
        : env.MORPHEUS_G2_AUDIT_LOG ||
          path.join(os.homedir(), ".morpheus", "g2-bridge-audit.jsonl"),
    clock: Date.now,
    logger: console,
  };
}

function createBridge(options = {}) {
  const config = {
    ...buildConfigFromEnv({}),
    ...options,
  };
  config.publicUrl = trimTrailingSlash(config.publicUrl || "");
  config.localUrl = trimTrailingSlash(config.localUrl || `http://${config.host}:${config.port}`);
  config.allowAnyHost = config.allowAnyHost === true || config.allowAnyHost === "1";
  config.clock = config.clock || Date.now;
  config.logger = config.logger || console;
  if (typeof config.token !== "string" || !config.token.trim()) {
    // An empty token would make every bearer comparison succeed (see
    // createAuthMiddleware); refusing to start is the only safe default.
    throw new Error(
      "Refusing to start the Morpheus G2 bridge with an empty bearer token. " +
        "Set MORPHEUS_G2_TOKEN to a non-empty secret.",
    );
  }
  // The trimmed token is the canonical secret: clients' values are trimmed by
  // normalizeTokenHeader, so a whitespace-padded MORPHEUS_G2_TOKEN must mean
  // the same secret everywhere (QR payloads, auth comparison, startup guard).
  config.token = config.token.trim();
  const state = {
    sessions: new Map(),
    nextMessageId: 1,
    sessionTouchCounter: 0,
    // Single source of truth for the tracked-session cap: envInt already
    // clamped the env value, and this fallback covers direct options.
    maxTrackedSessions: Number(config.maxTrackedSessions) || MAX_TRACKED_SESSIONS,
    codexSessions: new Map(),
    idempotency: new Map(),
    rateLimits: new Map(),
    rateLimitsSweptAt: 0,
    outputHashes: new Map(),
    outputPollers: new Map(),
    outputPollStats: new Map(),
    outputRefreshInflight: new Map(),
    promptMirrorBaselines: new Map(),
    codexLiveEventsCheckedAt: new Map(),
    mirroredCodexSessions: new Set(),
    sessionAliases: new Map(),
    resultWaiters: new Map(),
    projectActiveSessions: new Map(),
    pendingProjectPrompts: new Map(),
    projectPromptLocks: new Map(),
    projectSessionRowsCache: new Map(),
    projectListCache: null,
    omniStatusCache: { value: null, at: 0 },
    feedCursor: 0,
    feedBaselined: false,
    feedPoller: null,
    feedFetchInflight: null,
    feedLastPolledAt: 0,
    navigationEpoch: 0,
    lastProject: null,
    selectedProject: null,
    selectedSession: null,
  };
  const audit = createAuditLogger(config);
  const morpheusProvider = createMorpheusProvider({
    morpheusBin: config.morpheusBin,
    runner: config.runner,
    runnerTimeoutMs: config.runnerTimeoutMs,
    outputRunnerTimeoutMs: config.outputRunnerTimeoutMs,
    runnerOutputBytes: config.runnerOutputBytes,
    runnerConcurrency: config.runnerConcurrency,
  });
  const provider =
    config.provider ||
    (config.agentBackend === AGENT_BACKEND_MORPHEUS
      ? morpheusProvider
      : createCodexAppServerBridgeProvider({
          morpheusProvider,
          state,
          config,
          audit,
          codexClient: config.codexClient,
          createCodexAgentProvider: config.createCodexAgentProvider,
        }));
  const app = express();

  app.disable("x-powered-by");
  app.use(createHostValidationMiddleware(config));
  app.use(createCorsMiddleware(config));
  app.use(express.json({ limit: config.jsonLimit }));
  app.use((err, _req, res, next) => {
    if (!err) {
      next();
      return;
    }
    const status = err.type === "entity.too.large" ? 413 : 400;
    res.status(status).json({ error: status === 413 ? "JSON body too large" : "Invalid JSON" });
  });

  app.get("/healthz", (_req, res) => {
    res.json({
      ok: true,
      provider: provider.name,
      bridge: "g2",
    });
  });

  app.use("/api", createRateLimitMiddleware(config, state, config.clock));
  app.use("/api", createAuthMiddleware(config));
  app.use("/api", createRequestLogMiddleware(config, audit));

  app.get("/api/info", async (_req, res) => {
    const providerInfo = await provider.info();
    const omni = await getOmniStatus({ provider, state, config });
    res.json({
      provider: provider.name,
      bridge: "g2",
      model: providerInfo.model,
      version: "0.1.0",
      publicUrl: config.publicUrl || null,
      omnipresence: { enabled: Boolean(omni?.enabled) },
      selectedProject: state.selectedProject,
      selectedSession: state.selectedSession,
      allowedActions: provider.allowedActions,
      limits: bridgeLimits(config),
      policy: bridgePolicy(config, provider),
    });
  });

  app.get("/api/device/state", (_req, res) => {
    res.json(deviceStateSnapshot({ state, config, provider }));
  });

  app.get("/api/sessions", async (req, res) => {
    const limit = parseLimitParam(req.query.limit, config.sessionLimit, config.sessionLimit);
    const wantsProjects =
      req.query.view === "projects" ||
      req.query.scope === "projects" ||
      (!state.selectedProject && config.showProjectsFirst);
    // The Morpheus Feed pseudo-session leads every session list (projects
    // view, project sessions, open session) while omnipresence is enabled;
    // getOmniStatus is cached and never throws.
    const feedRow = await feedRowIfEnabled({ provider, state, config });
    try {
      if (wantsProjects) {
        const result = await listProjectsForResponse(provider, state, config, config.projectLimit);
        const body = projectsResponseBody(result, state);
        body.sessions = prependFeedRow(feedRow, body.sessions);
        res.json(body);
        return;
      }
      await refreshSelectedSessionForSessionsPoll({
        provider,
        state,
        config,
        reason: "sessions_poll_before_rows",
      });
      if (navigationView(state) === "session") {
        const { sessions, snapshot, stale, error } = localProjectSessionMenuRows(
          state,
          config,
          state.selectedProject,
          limit,
        );
        const selected = selectedSessionResponse(state);
        const responseText = selected.text;
        bridgeDebug(config, "sessions-response", {
          view: "session",
          state: selected.state,
          selectedSessionId: selected.selectedSession?.id || "",
          activeSessionId: selected.activeSessionId || "",
          displaySessionId: selected.displaySessionId || "",
          messages: selected.messages.length,
          textChars: String(responseText || "").length,
          outputPollActive: outputPollIsActive(state, selected.activeSessionId),
          outputPollStopReason: state.outputPollStats?.get(selected.activeSessionId)?.stopReason || "",
        });
        bridgeFlow(config, "sessions-response", {
          view: "session",
          state: selected.state,
          selectedSessionId: selected.selectedSession?.id || "",
          activeSessionId: selected.activeSessionId || "",
          displaySessionId: selected.displaySessionId || "",
          rowCount: sessions.length,
          messages: selected.messages.length,
          textChars: String(responseText || "").length,
          outputPollActive: outputPollIsActive(state, selected.activeSessionId),
          outputPollStopReason: state.outputPollStats?.get(selected.activeSessionId)?.stopReason || "",
        });
        res.json({
          sessions: prependFeedRow(feedRow, sessions),
          snapshot,
          selectedProject: state.selectedProject,
          selectedSession: selected.selectedSession || state.selectedSession,
          activeSessionId: selected.activeSessionId || undefined,
          displaySessionId: selected.displaySessionId || undefined,
          projectActiveSessionId: selected.projectActiveSessionId || undefined,
          state: selected.state,
          history: selected.history,
          messages: selected.messages,
          text: responseText,
          answer: responseText,
          message: responseText,
          response: responseText,
          output: responseText ? { text: responseText } : undefined,
          mode: "session",
          view: "session",
          stale,
          error: error || undefined,
        });
        return;
      }
      let { sessions, snapshot, stale, error } = await projectSessionMenuRows(
        provider,
        state,
        config,
        state.selectedProject,
        limit,
      );
      // The before-rows refresh already owned this poll's output budget;
      // the post-rows pass is history-only so one poll stays bounded.
      const postRefresh = await refreshSelectedSessionForSessionsPoll({
        provider,
        state,
        config,
        reason: "sessions_poll_after_rows",
        outputFallback: false,
      });
      if (postRefresh.published) {
        ({ sessions, snapshot, stale, error } = await projectSessionMenuRows(
          provider,
          state,
          config,
          state.selectedProject,
          limit,
        ));
      }
      const selected = selectedSessionResponse(state);
      const responseText = selected.text;
      const view = navigationView(state);
      bridgeDebug(config, "sessions-response", {
        view,
        state: selected.state,
        selectedSessionId: selected.selectedSession?.id || "",
        activeSessionId: selected.activeSessionId || "",
        displaySessionId: selected.displaySessionId || "",
        rowCount: sessions.length,
        messages: selected.messages.length,
        textChars: String(responseText || "").length,
        outputPollActive: outputPollIsActive(state, selected.activeSessionId),
        outputPollStopReason: state.outputPollStats?.get(selected.activeSessionId)?.stopReason || "",
      });
      bridgeFlow(config, "sessions-response", {
        view,
        state: selected.state,
        selectedSessionId: selected.selectedSession?.id || "",
        activeSessionId: selected.activeSessionId || "",
        displaySessionId: selected.displaySessionId || "",
        rowCount: sessions.length,
        messages: selected.messages.length,
        textChars: String(responseText || "").length,
        outputPollActive: outputPollIsActive(state, selected.activeSessionId),
        outputPollStopReason: state.outputPollStats?.get(selected.activeSessionId)?.stopReason || "",
      });
      res.json({
        sessions: prependFeedRow(feedRow, sessions),
        snapshot,
        selectedProject: state.selectedProject,
        selectedSession: selected.selectedSession || state.selectedSession,
        activeSessionId: selected.activeSessionId || undefined,
        displaySessionId: selected.displaySessionId || undefined,
        projectActiveSessionId: selected.projectActiveSessionId || undefined,
        state: selected.state,
        history: selected.history,
        messages: selected.messages,
        text: responseText,
        answer: responseText,
        message: responseText,
        response: responseText,
        output: responseText ? { text: responseText } : undefined,
        mode: view,
        view,
        stale,
        error: error || undefined,
      });
    } catch (err) {
      const message = safeJsonError(err);
      bridgeDebug(config, "sessions-endpoint-failed", {
        mode: wantsProjects ? "projects" : "sessions",
        reason: message,
      });
      if (wantsProjects) {
        const cached = cachedProjects(state, config.projectLimit);
        if (cached) {
          const body = projectsResponseBody({ ...cached, error: message }, state);
          body.sessions = prependFeedRow(feedRow, body.sessions);
          res.json(body);
          return;
        }
      } else if (state.selectedProject) {
        const activeRow = activeProjectSessionRow(state, state.selectedProject);
        const pendingRow = pendingProjectPromptForProject(state, state.selectedProject);
        const selected = selectedSessionResponse(state);
        const responseText = selected.text;
        const view = navigationView(state);
        const rows = [
          ...(config.showBackToProjectsRow ? [projectMenuRow(state.selectedProject)] : []),
          ...(activeRow ? [activeRow] : []),
          ...(pendingRow ? [pendingRow] : []),
        ];
        if (rows.length) {
          res.json({
            sessions: prependFeedRow(feedRow, rows),
            snapshot: {
              generated_at: Math.floor(config.clock() / 1000),
              summary: `Using local G2 session state after list failure: ${message}`,
              counts: rows.reduce((acc, session) => {
                const status = session.status || "unknown";
                acc[status] = (acc[status] || 0) + 1;
                return acc;
              }, {}),
              policy: {
                raw_terminal_buffers: false,
                source: "local_state_fallback",
              },
            },
            selectedProject: state.selectedProject,
            selectedSession: selected.selectedSession || state.selectedSession,
            activeSessionId: selected.activeSessionId || undefined,
            displaySessionId: selected.displaySessionId || undefined,
            projectActiveSessionId: selected.projectActiveSessionId || undefined,
            state: selected.state,
            history: selected.history,
            messages: selected.messages,
            text: responseText,
            answer: responseText,
            message: responseText,
            response: responseText,
            output: responseText ? { text: responseText } : undefined,
            mode: view,
            view,
            stale: true,
            error: message,
          });
          return;
        }
      }
      res.status(500).json({ sessions: prependFeedRow(feedRow, []), error: safeJsonError(err) });
    }
  });

  app.get("/api/projects", async (req, res) => {
    const limit = parseLimitParam(req.query.limit, config.projectLimit, config.projectLimit);
    try {
      const result = await listProjectsForResponse(provider, state, config, limit);
      res.json({
        ...result,
        selectedProject: state.selectedProject,
      });
    } catch (err) {
      res.status(500).json({ projects: [], error: safeJsonError(err) });
    }
  });

  app.get("/api/selected-session", (_req, res) => {
    res.json({ selectedProject: state.selectedProject, selectedSession: state.selectedSession });
  });

  app.get("/api/navigation", (_req, res) => {
    res.json(navigationPayload(state));
  });

  app.get("/api/status", async (req, res) => {
    const requestedSessionId = typeof req.query.sessionId === "string" ? req.query.sessionId : "";
    const sessionId = requestedSessionId || state.selectedSession?.id || "";
    if (!sessionId) {
      res.json({
        state: "idle",
        sessionId: null,
        provider: provider.name,
        selectedProject: state.selectedProject,
        selectedSession: state.selectedSession,
      });
      return;
    }
    if (isProjectMenuSessionId(sessionId)) {
      res.json({
        state: "idle",
        sessionId,
        provider: "codex",
        selectedProject: state.selectedProject,
        selectedSession: null,
        navigation: "projects",
      });
      return;
    }
    if (isFeedSessionId(sessionId)) {
      touchFeedSubscription({ provider, state, config });
      res.json({
        state: "idle",
        sessionId,
        provider: "morpheus",
        selectedProject: state.selectedProject,
        selectedSession: state.selectedSession,
      });
      return;
    }
    const activeProjectStatusId = projectIdFromActiveSessionId(sessionId);
    if (activeProjectStatusId) {
      const activeSession = activeSessionForProject(state, activeProjectStatusId);
      const directStatus = activeSession ? provider.getStatus?.(activeSession.id) : null;
      res.json({
        state:
          directStatus?.state ||
          (activeSession ? statusFromBufferedMessages(state, activeSession.id, activeSession.status || "idle") : "idle"),
        sessionId,
        activeSessionId: activeSession?.id || null,
        provider: directStatus?.provider || activeSession?.provider || "codex",
        selectedProject: state.selectedProject,
        selectedSession: activeSession
          ? {
              ...activeSession,
              id: sessionId,
              activeSessionId: activeSession.id,
              realSessionId: activeSession.id,
            }
          : null,
      });
      return;
    }
    if (projectIdFromSessionId(sessionId)) {
      const projectId = projectIdFromSessionId(sessionId);
      const selectedProjectId = state.selectedProject?.id || state.selectedProject?.tenant_id || "";
      const activeSession =
        selectedProjectId === projectId
          ? activeSessionForProject(state, projectId)
          : state.projectActiveSessions.get(projectId);
      const directStatus = activeSession ? provider.getStatus?.(activeSession.id) : null;
      res.json({
        state:
          directStatus?.state ||
          (activeSession ? statusFromBufferedMessages(state, activeSession.id, activeSession.status || "idle") : "idle"),
        sessionId,
        activeSessionId: activeSession?.id || null,
        projectActiveSessionId: activeSession ? `${PROJECT_ACTIVE_SESSION_PREFIX}${projectId}` : null,
        provider: directStatus?.provider || activeSession?.provider || "codex",
        selectedProject: state.selectedProject,
        selectedSession: null,
      });
      return;
    }
    try {
      const directStatus = provider.getStatus?.(sessionId);
      if (directStatus) {
        const remembered = state.codexSessions?.get(sessionId);
        const shouldUpdateSelection =
          !requestedSessionId ||
          (state.selectedSession && sessionMatches(state.selectedSession, sessionId));
        if (remembered && shouldUpdateSelection) {
          state.selectedSession = remembered;
        }
        res.json({
          state: directStatus.state || remembered?.status || state.selectedSession?.status || "idle",
          sessionId,
          provider: directStatus.provider || remembered?.provider || provider.name,
          selectedProject: state.selectedProject,
          selectedSession: state.selectedSession,
        });
        return;
      }
      const resolved = await resolveSession(provider, sessionId, config.sessionLimit, {
        projectId: state.selectedProject?.id || state.selectedProject?.tenant_id || "",
      });
      if (!resolved.ok) {
        if (requestedSessionId) {
          res.status(resolved.status).json({ error: resolved.error });
          return;
        }
        res.json({
          state: state.selectedSession?.status || "idle",
          sessionId: state.selectedSession?.id || null,
          provider: state.selectedSession?.provider || provider.name,
          selectedProject: state.selectedProject,
          selectedSession: state.selectedSession,
        });
        return;
      }
      const shouldUpdateSelection =
        !requestedSessionId ||
        (state.selectedSession && sessionMatches(state.selectedSession, sessionId));
      if (shouldUpdateSelection) {
        state.selectedSession = resolved.session;
      }
      res.json({
        state: resolved.session.status || "idle",
        sessionId: resolved.session.id,
        provider: resolved.session.provider || provider.name,
        selectedProject: state.selectedProject,
        selectedSession: state.selectedSession,
      });
    } catch (err) {
      res.status(500).json({ error: safeJsonError(err) });
    }
  });

  app.post("/api/select-project", async (req, res) => {
    const requestId = writeRequestId(req);
    if (await maybeReplay(req, res, state, requestId)) return;

    const projectId = req.body?.projectId || projectIdFromSessionId(req.body?.sessionId);
    if (!projectId || typeof projectId !== "string") {
      res.status(400).json({ error: "Missing projectId", code: "missing_project_id" });
      return;
    }
    if (!reserveReplay(req, state, requestId, config.clock, config.requestIdTtlMs)) {
      if (await maybeReplay(req, res, state, requestId)) return;
    }
    if (isProjectsNavProjectId(projectId) || isProjectMenuSessionId(req.body?.sessionId)) {
      const priorProject = state.selectedProject;
      state.selectedProject = null;
      state.selectedSession = null;
      bumpNavigationEpoch(state);
      let result;
      try {
        result = await listProjectsForResponse(
          provider,
          state,
          config,
          config.projectLimit,
          { preferCache: true },
        );
      } catch (err) {
        result = {
          projects: [],
          stale: true,
          error: safeJsonError(err),
        };
      }
      const body = {
        ...projectsResponseBody(result, state),
        navigation: navigationPayload(state, {
          action: "navigate_projects",
          requestId,
        }),
        requestId,
      };
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      audit("select_project_menu", {
        requestId,
        priorProjectId: priorProject?.id || priorProject?.tenant_id || "",
      });
      res.json(body);
      return;
    }
    try {
      const resolved = await resolveProject(provider, projectId, config.projectLimit);
      if (!resolved.ok) {
        audit("select_project_failed", { requestId, projectId, reason: resolved.error });
        const body = { error: resolved.error };
        rememberReplay(req, state, requestId, resolved.status, body, config.clock, config.requestIdTtlMs);
        res.status(resolved.status).json(body);
        return;
      }
      rememberProjectContext(state, resolved.project);
      state.selectedProject = resolved.project;
      state.selectedSession = null;
      bumpNavigationEpoch(state);
      const body = {
        ok: true,
        provider: provider.name,
        selectedProject: state.selectedProject,
        requestId,
      };
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      audit("select_project", { requestId, projectId: resolved.project.id || resolved.project.tenant_id });
      pushMessage(state, `${PROJECT_SESSION_PREFIX}${resolved.project.id || resolved.project.tenant_id}`, {
        type: "selected_project",
        provider: provider.name,
        projectId: resolved.project.id || resolved.project.tenant_id,
        at: nowIso(config.clock),
      });
      res.json(body);
    } catch (err) {
      const message = safeJsonError(err);
      audit("select_project_failed", { requestId, projectId, reason: message });
      const body = { error: message };
      rememberReplay(req, state, requestId, 500, body, config.clock, config.requestIdTtlMs);
      res.status(500).json(body);
    }
  });

  app.post("/api/select-session", async (req, res) => {
    const requestId = writeRequestId(req);
    if (await maybeReplay(req, res, state, requestId)) return;

    const sessionId = req.body?.sessionId;
    if (!sessionId || typeof sessionId !== "string" || sessionId.length > 128) {
      res.status(400).json({ error: "Missing sessionId", code: "missing_session_id" });
      return;
    }
    if (!reserveReplay(req, state, requestId, config.clock, config.requestIdTtlMs)) {
      if (await maybeReplay(req, res, state, requestId)) return;
    }
    if (isProjectMenuSessionId(sessionId)) {
      const priorProject = state.selectedProject;
      state.selectedProject = null;
      state.selectedSession = null;
      bumpNavigationEpoch(state);
      const body = navigationPayload(state, {
        action: "navigate_projects",
        requestId,
      });
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      audit("select_project_menu", {
        requestId,
        priorProjectId: priorProject?.id || priorProject?.tenant_id || "",
      });
      res.json(body);
      return;
    }
    if (isFeedSessionId(sessionId)) {
      state.selectedSession = feedSessionRow(state, config);
      bumpNavigationEpoch(state);
      touchFeedSubscription({ provider, state, config });
      void fetchAndPublishFeedItems({ provider, state, config }).catch(() => {});
      const body = {
        ok: true,
        provider: provider.name,
        selectedSession: state.selectedSession,
        requestId,
      };
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      audit("select_feed_session", { requestId });
      res.json(body);
      return;
    }
    const activeProjectSelectId = projectIdFromActiveSessionId(sessionId);
    if (activeProjectSelectId) {
      const resolved = await resolveProjectWithCachedFallback(
        provider,
        state,
        config,
        activeProjectSelectId,
      );
      if (!resolved.ok) {
        audit("select_session_failed", { requestId, sessionId, reason: resolved.error });
        const body = { error: resolved.error };
        rememberReplay(req, state, requestId, resolved.status, body, config.clock, config.requestIdTtlMs);
        res.status(resolved.status).json(body);
        return;
      }
      rememberProjectContext(state, resolved.project);
      state.selectedProject = resolved.project;
      const activeSession = state.projectActiveSessions.get(activeProjectSelectId) || null;
      state.selectedSession = activeSession;
      bumpNavigationEpoch(state);
      if (activeSession?.id) addSessionAlias(state, activeSession.id, sessionId);
      const body = {
        ok: true,
        provider: provider.name,
        selectedProject: state.selectedProject,
        selectedSession: activeSession
          ? activeProjectSessionRow(state, state.selectedProject) || activeSession
          : null,
        activeSessionId: activeSession?.id || null,
        requestId,
      };
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      audit("select_project_active_session", {
        requestId,
        projectId: activeProjectSelectId,
        activeSessionId: activeSession?.id || "",
      });
      res.json(body);
      return;
    }
    const projectId = projectIdFromSessionId(sessionId);
    if (projectId) {
      const resolved = await resolveProjectWithCachedFallback(provider, state, config, projectId);
      if (!resolved.ok) {
        audit("select_session_failed", { requestId, sessionId, reason: resolved.error });
        const body = { error: resolved.error };
        rememberReplay(req, state, requestId, resolved.status, body, config.clock, config.requestIdTtlMs);
        res.status(resolved.status).json(body);
        return;
      }
      rememberProjectContext(state, resolved.project);
      state.selectedProject = resolved.project;
      const activeSession = state.projectActiveSessions.get(projectId) || null;
      state.selectedSession = null;
      bumpNavigationEpoch(state);
      const body = {
        ok: true,
        provider: provider.name,
        selectedProject: state.selectedProject,
        selectedSession: null,
        activeSessionId: activeSession?.id || null,
        projectActiveSessionId: activeSession ? `${PROJECT_ACTIVE_SESSION_PREFIX}${projectId}` : null,
        navigation: navigationPayload(state, { action: "select_project" }),
        requestId,
      };
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      audit("select_project", {
        requestId,
        projectId,
        activeSessionId: activeSession?.id || "",
      });
      res.json(body);
      return;
    }
    try {
      const resolved = await resolveSession(provider, sessionId, config.sessionLimit, {
        projectId: state.selectedProject?.id || state.selectedProject?.tenant_id || "",
      });
      if (!resolved.ok) {
        audit("select_session_failed", { requestId, sessionId, reason: resolved.error });
        const body = { error: resolved.error };
        rememberReplay(req, state, requestId, resolved.status, body, config.clock, config.requestIdTtlMs);
        res.status(resolved.status).json(body);
        return;
      }
      state.selectedSession = resolved.session;
      bumpNavigationEpoch(state);
      const body = {
        ok: true,
        provider: provider.name,
        selectedSession: state.selectedSession,
        requestId,
      };
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      audit("select_session", { requestId, sessionId: resolved.session.id });
      pushMessage(state, resolved.session.id, {
        type: "selected_session",
        provider: provider.name,
        sessionId: resolved.session.id,
        at: nowIso(config.clock),
      });
      res.json(body);
    } catch (err) {
      const message = safeJsonError(err);
      audit("select_session_failed", { requestId, sessionId, reason: message });
      const body = { error: message };
      rememberReplay(req, state, requestId, 500, body, config.clock, config.requestIdTtlMs);
      res.status(500).json(body);
    }
  });

  app.post("/api/prompt", (req, res) => {
    submitTextToMorpheus(req, res, { config, provider, state, audit }).catch((err) => {
      const body = { error: safeJsonError(err), code: "prompt_failed" };
      const requestId = writeRequestId(req, state);
      rememberReplay(req, state, requestId, 500, body, config.clock, config.requestIdTtlMs);
      if (!res.headersSent) res.status(500).json(body);
    });
  });
  app.post("/api/transcript/finalize", (req, res) => {
    submitTextToMorpheus(req, res, { config, provider, state, audit }).catch((err) => {
      const body = { error: safeJsonError(err), code: "prompt_failed" };
      const requestId = writeRequestId(req, state);
      rememberReplay(req, state, requestId, 500, body, config.clock, config.requestIdTtlMs);
      if (!res.headersSent) res.status(500).json(body);
    });
  });

  app.post("/api/back", (req, res) => {
    handleNavigateBack(req, res, { config, state, audit }).catch((err) => {
      res.status(500).json({ error: safeJsonError(err) });
    });
  });

  app.post("/api/navigation/back", (req, res) => {
    handleNavigateBack(req, res, { config, state, audit }).catch((err) => {
      res.status(500).json({ error: safeJsonError(err) });
    });
  });

  app.post("/api/interrupt", (req, res) => {
    if (config.interruptNavigatesBack) {
      handleNavigateBack(req, res, { config, state, audit }).catch((err) => {
        res.status(500).json({ error: safeJsonError(err) });
      });
      return;
    }
    audit("blocked_action", { action: "interrupt", requestId: normalizeRequestId(req) || null });
    res.status(403).json({
      error: "G2 interrupt is blocked until selected-session provider gating is proven.",
      code: "action_blocked",
    });
  });

  app.post("/api/permission-response", (req, res) => {
    audit("blocked_action", {
      action: "permission-response",
      requestId: normalizeRequestId(req) || null,
    });
    res.status(403).json({
      error: "Glasses cannot answer permission prompts.",
      code: "action_blocked",
    });
  });

  app.post("/api/question-response", (req, res) => {
    audit("blocked_action", {
      action: "question-response",
      requestId: normalizeRequestId(req) || null,
    });
    res.status(403).json({
      error: "Glasses question responses are not enabled yet.",
      code: "action_blocked",
    });
  });

  app.post("/api/audio/start", (_req, res) => {
    res.status(501).json({
      error:
        "Audio streaming is not wired yet. Send final transcripts to /api/transcript/finalize.",
      code: "audio_not_wired",
    });
  });

  app.post("/api/audio/chunk", (_req, res) => {
    res.status(501).json({
      error:
        "Audio streaming is not wired yet. Send final transcripts to /api/transcript/finalize.",
      code: "audio_not_wired",
    });
  });

  app.post("/api/audio/finish", (_req, res) => {
    res.status(501).json({
      error:
        "Audio streaming is not wired yet. Send final transcripts to /api/transcript/finalize.",
      code: "audio_not_wired",
    });
  });

  app.get("/api/sessions/:id/history", async (req, res) => {
    const sessionId = String(req.params.id || "");
    const limit = parseLimitParam(req.query.limit, 10, 10);
    if (isProjectMenuSessionId(sessionId)) {
      const priorProject = state.selectedProject;
      state.selectedProject = null;
      state.selectedSession = null;
      bumpNavigationEpoch(state);
      audit("history_project_menu", {
        priorProjectId: priorProject?.id || priorProject?.tenant_id || "",
      });
      let result;
      try {
        result = await listProjectsForResponse(
          provider,
          state,
          config,
          config.projectLimit,
          { preferCache: true },
        );
      } catch (err) {
        result = {
          projects: [],
          stale: true,
          error: safeJsonError(err),
        };
      }
      res.json({
        ...projectsResponseBody(result, state),
        history: projectsOverviewHistory(result.projects, state.lastProject),
        navigation: navigationPayload(state, { action: "navigate_projects" }),
      });
      return;
    }
    if (isFeedSessionId(sessionId)) {
      // Stock Even clients open rows by fetching history; opening the feed
      // row pulls fresh feed items into the buffer, selects the feed (like
      // concrete session rows), and returns the items as assistant-only
      // history lines.
      try {
        await fetchAndPublishFeedItems(
          { provider, state, config },
          { limit: Math.max(limit, DEFAULT_FEED_LIMIT) },
        );
      } catch (err) {
        bridgeDebug(config, "feed-history-fetch-failed", { reason: safeJsonError(err) });
      }
      state.selectedSession = feedSessionRow(state, config);
      bumpNavigationEpoch(state);
      touchFeedSubscription({ provider, state, config });
      audit("select_feed_history_open", {});
      const feedHistory = historyFromBufferedMessages(
        getMessages(state, FEED_SESSION_ID, 0),
        limit,
      );
      res.json({
        history: feedHistory.length
          ? feedHistory
          : [{ role: "assistant", text: "Morpheus Feed is quiet. New pushes will appear here." }],
        mode: "session",
        view: "session",
        selectedProject: state.selectedProject,
        selectedSession: state.selectedSession,
        navigation: navigationPayload(state, { action: "select_feed_session" }),
      });
      return;
    }
    try {
      const activeProjectHistoryId = projectIdFromActiveSessionId(sessionId);
      if (activeProjectHistoryId) {
        const selectedProjectId = state.selectedProject?.id || state.selectedProject?.tenant_id || "";
        const activeSession = activeSessionForProject(state, activeProjectHistoryId);
        const selectedSessionMatchesActive =
          !state.selectedSession ||
          (activeSession && sessionMatches(state.selectedSession, activeSession.id));
        if (selectedProjectId !== activeProjectHistoryId || !selectedSessionMatchesActive) {
          res.json({
            history: [],
            selectedProject: state.selectedProject,
            selectedSession: state.selectedSession,
            activeSessionId: activeSession?.id || null,
            navigation: navigationPayload(state, { action: "stale_history_ignored" }),
          });
          return;
        }
        const resolved = await resolveProject(provider, activeProjectHistoryId, config.projectLimit);
        if (resolved.ok) {
          rememberProjectContext(state, resolved.project);
          state.selectedProject = resolved.project;
        }
        if (!activeSession) {
          state.selectedSession = null;
          bumpNavigationEpoch(state);
          res.json({
            history: [],
            selectedProject: state.selectedProject,
            selectedSession: null,
            navigation: navigationPayload(state, { action: "select_project_active_session" }),
          });
          return;
        }
        state.selectedSession = activeSession;
        bumpNavigationEpoch(state);
        addSessionAlias(state, activeSession.id, sessionId);
        await refreshSessionForSessionsPoll({
          provider,
          state,
          config,
          sessionId: activeSession.id,
          reason: "history_active_session",
        });
        let history = bufferedHistoryForRow(state, sessionId, limit, {
          preferredSessionIds: [activeSession.id],
        });
        if (!hasAssistantHistory(history)) {
          const activeHistory = await sessionHistory(provider, state, activeSession.id, limit);
          history = history.length ? [...history, ...activeHistory].slice(-limit) : activeHistory;
        }
        res.json({
          history,
          mode: "session",
          view: "session",
          selectedProject: state.selectedProject,
          selectedSession: activeProjectSessionRow(state, state.selectedProject) || activeSession,
          activeSessionId: activeSession.id,
          navigation: navigationPayload(state, { action: "select_project_active_session" }),
        });
        return;
      }
      const projectId = projectIdFromSessionId(sessionId);
      if (projectId) {
        const selectedProjectId = state.selectedProject?.id || state.selectedProject?.tenant_id || "";
        if (selectedProjectId !== projectId) {
          const resolved = await resolveProject(provider, projectId, config.projectLimit);
          if (resolved.ok) {
            rememberProjectContext(state, resolved.project);
            state.selectedProject = resolved.project;
          }
        }
        const activeSession = state.projectActiveSessions.get(projectId) || null;
        const pendingProjectRow = !activeSession ? pendingProjectPromptForProject(state, projectId) : null;
        if (pendingProjectRow) {
          const rows = [
            ...(config.showBackToProjectsRow && state.selectedProject ? [projectMenuRow(state.selectedProject)] : []),
            pendingProjectRow,
          ];
          bridgeDebug(config, "project-history-pending", {
            projectId,
            sessionId,
            pendingRequestId: pendingProjectRow.pendingRequestId || "",
          });
          res.json({
            history: pendingProjectPromptHistory(pendingProjectRow),
            sessions: rows,
            snapshot: {
              generated_at: Math.floor(config.clock() / 1000),
              summary: "1 pending Codex app-server session.",
              counts: { busy: 1 },
              policy: {
                raw_terminal_buffers: false,
                source: "codex_app_server",
              },
            },
            mode: "session",
            view: "session",
            selectedProject: state.selectedProject,
            selectedSession: pendingProjectRow,
            activeSessionId: null,
            projectActiveSessionId: pendingProjectRow.id,
            navigation: {
              ...navigationPayload(state, {
                action: "select_project_pending_session",
                mode: "session",
              }),
              view: "session",
              mode: "session",
              selectedSession: pendingProjectRow,
            },
          });
          return;
        }
        if (activeProjectHistoryShouldStayLive(state, projectId, activeSession)) {
          const status = statusFromBufferedMessages(state, activeSession.id, activeSession.status || "idle");
          state.selectedSession = {
            ...activeSession,
            status,
            timestamp: activeSession.timestamp || nowIso(config.clock),
          };
          addSessionAlias(state, activeSession.id, sessionId);
          await refreshSessionForSessionsPoll({
            provider,
            state,
            config,
            sessionId: activeSession.id,
            reason: "history_project_live_session",
          });
          let history = bufferedHistoryForRow(state, sessionId, limit, {
            preferredSessionIds: [activeSession.id],
          });
          if (!hasAssistantHistory(history)) {
            const activeHistory = await sessionHistory(provider, state, activeSession.id, limit);
            history = history.length ? [...history, ...activeHistory].slice(-limit) : activeHistory;
          }
          bridgeDebug(config, "project-history-live", {
            projectId,
            sessionId,
            activeSessionId: activeSession.id,
            status,
            selectedSessionId: state.selectedSession?.id || "",
          });
          audit("history_project_live_session", {
            projectId,
            activeSessionId: activeSession.id,
            status,
          });
          res.json({
            history,
            mode: "session",
            view: "session",
            selectedProject: state.selectedProject,
            selectedSession: activeProjectSessionRow(state, state.selectedProject) || state.selectedSession,
            activeSessionId: activeSession.id,
            projectActiveSessionId: `${PROJECT_ACTIVE_SESSION_PREFIX}${projectId}`,
            navigation: navigationPayload(state, { action: "select_project_active_session" }),
          });
          return;
        }
        state.selectedSession = null;
        bumpNavigationEpoch(state);
        const { sessions, snapshot, stale, error } = await projectSessionMenuRows(
          provider,
          state,
          config,
          state.selectedProject,
          limit,
        );
        bridgeDebug(config, "project-history-menu", {
          projectId,
          sessionId,
          activeSessionId: activeSession?.id || "",
          selectedSessionId: state.selectedSession?.id || "",
          rows: sessions.length,
        });
        res.json({
          history: projectSessionsOverviewHistory(state.selectedProject, sessions),
          sessions,
          snapshot,
          mode: "sessions",
          selectedProject: state.selectedProject,
          selectedSession: null,
          activeSessionId: activeSession?.id || null,
          projectActiveSessionId: activeSession ? `${PROJECT_ACTIVE_SESSION_PREFIX}${projectId}` : null,
          navigation: navigationPayload(state, { action: "select_project", mode: "sessions" }),
          stale,
          error: error || undefined,
        });
        return;
      }
      // Stock Even clients open a session row by fetching its history; they
      // never call /api/select-session. Treat the row open as selection so
      // follow-up prompts, live polling, and the project's active-session row
      // resume this session instead of spawning a new one.
      if (!state.selectedSession || !sessionMatches(state.selectedSession, sessionId)) {
        try {
          const resolved = await resolveSession(provider, sessionId, config.sessionLimit, {
            projectId: state.selectedProject?.id || state.selectedProject?.tenant_id || "",
          });
          if (resolved.ok) {
            state.selectedSession = resolved.session;
            bumpNavigationEpoch(state);
            if (state.selectedProject) {
              rememberActiveProjectSession(state, state.selectedProject, resolved.session);
            }
            audit("select_session_history_open", { sessionId: resolved.session.id });
            bridgeFlow(config, "history-open-selected-session", {
              sessionId,
              selectedSessionId: resolved.session.id,
              selectedProjectId: projectKey(state.selectedProject) || "",
            });
          }
        } catch (err) {
          bridgeDebug(config, "history-open-select-failed", {
            sessionId,
            reason: safeJsonError(err),
          });
        }
      }
      const history = await sessionHistory(provider, state, sessionId, limit);
      res.json({ history });
    } catch (err) {
      const fallback = historyFromBufferedMessages(getMessages(state, sessionId, 0), limit);
      res.json({
        history: fallback,
        error: fallback.length ? undefined : safeJsonError(err),
      });
    }
  });

  app.get("/api/messages", async (req, res) => {
    const sessionId = String(req.query.sessionId || state.selectedSession?.id || "morpheus");
    const after = parseIntParam(req.query.after, 0);
    if (isProjectMenuSessionId(sessionId)) {
      state.selectedProject = null;
      state.selectedSession = null;
      res.json({
        messages: [],
        state: "idle",
        sessionId,
        provider: provider.name,
        navigation: navigationPayload(state, { action: "navigate_projects" }),
      });
      return;
    }
    if (isFeedSessionId(sessionId)) {
      // Message polls count as feed subscription; the feed poller (not this
      // request) fetches new items, so the poll stays fast.
      touchFeedSubscription({ provider, state, config });
    }
    const activeProjectMessagesId = projectIdFromActiveSessionId(sessionId);
    const projectId = projectIdFromSessionId(sessionId);
    const selected =
      state.selectedSession && sessionMatches(state.selectedSession, sessionId)
        ? state.selectedSession
        : null;
    const activeSession = activeProjectMessagesId
      ? activeSessionForProject(state, activeProjectMessagesId)
      : null;
    if (activeSession?.id) {
      addSessionAlias(state, activeSession.id, sessionId);
      await refreshSessionForSessionsPoll({
        provider,
        state,
        config,
        sessionId: activeSession.id,
        reason: "messages_active_session",
      });
    } else if (!projectId) {
      await refreshSessionForSessionsPoll({
        provider,
        state,
        config,
        sessionId,
        reason: "messages_session",
      });
    }
    const projectActiveSession = projectId ? activeSessionForProject(state, projectId) : null;
    if (projectActiveSession?.id) {
      addSessionAlias(state, projectActiveSession.id, sessionId);
      await refreshSessionForSessionsPoll({
        provider,
        state,
        config,
        sessionId: projectActiveSession.id,
        reason: "messages_project_session",
      });
    }
    const pendingProjectRow = projectId && !projectActiveSession
      ? pendingProjectPromptForProject(state, projectId)
      : null;
    const directStatus = provider.getStatus?.(activeSession?.id || projectActiveSession?.id || sessionId);
    const bufferedStatusTarget = activeSession?.id || projectActiveSession?.id || sessionId;
    const bufferedStatus = statusFromBufferedMessages(
      state,
      bufferedStatusTarget,
      activeSession?.status || projectActiveSession?.status || pendingProjectRow?.status || selected?.status || "idle",
    );
    const hasBufferedResult = getMessages(state, bufferedStatusTarget, 0).some(
      (message) => message.type === "result",
    );
    const responseStatus = hasBufferedResult
      ? bufferedStatus
      : directStatus?.state || bufferedStatus;
    const selectedProjectMatches = projectId && projectKey(state.selectedProject) === projectId;
    const projectRowIsLiveRequest =
      projectId &&
      projectActiveSession &&
      ((state.selectedSession && sessionMatches(state.selectedSession, projectActiveSession.id)) ||
        selectedProjectMatches);
    if (projectId && !projectRowIsLiveRequest) {
      res.json({
        messages: [],
        state: responseStatus,
        sessionId,
        activeSessionId: projectActiveSession?.id || undefined,
        pendingSessionId: pendingProjectRow?.id || undefined,
        projectActiveSessionId: projectActiveSession ? `${PROJECT_ACTIVE_SESSION_PREFIX}${projectId}` : undefined,
        provider: directStatus?.provider || provider.name,
      });
      return;
    }
    bridgeFlow(config, "messages-response", {
      sessionId,
      after,
      activeSessionId: activeSession?.id || projectActiveSession?.id || "",
      projectRowIsLiveRequest: Boolean(projectRowIsLiveRequest),
      state: responseStatus,
      messages: getMessages(state, sessionId, after).length,
      selectedProjectId: projectKey(state.selectedProject) || "",
      selectedSessionId: state.selectedSession?.id || "",
    });
    res.json({
      messages: getMessages(state, sessionId, after).map((entry) =>
        presentMessageForSession(entry, sessionId),
      ),
      state: responseStatus,
      sessionId,
      activeSessionId: activeSession?.id || projectActiveSession?.id || undefined,
      projectActiveSessionId: projectActiveSession ? `${PROJECT_ACTIVE_SESSION_PREFIX}${projectId}` : undefined,
      provider:
        directStatus?.provider ||
        activeSession?.provider ||
        projectActiveSession?.provider ||
        selected?.provider ||
        provider.name,
    });
  });

  // Omnipresence feed (PRD §3.1): poll-style feed for clients that want raw
  // items instead of the feed:main pseudo-session. Cursor semantics mirror
  // the CLI: items ascending by id and strictly greater than `after`;
  // after=0 (or omitted) returns the newest `limit` items, still ascending.
  app.get("/api/feed", async (req, res) => {
    const after = Math.max(0, parseIntParam(req.query.after, 0));
    const limit = parseLimitParam(req.query.limit, DEFAULT_FEED_LIMIT, MAX_FEED_LIMIT);
    const omni = await getOmniStatus({ provider, state, config });
    if (typeof provider.feedItems !== "function") {
      res.json({ items: [], latest_id: 0, omnipresence: { enabled: Boolean(omni?.enabled) } });
      return;
    }
    try {
      const result = await provider.feedItems({ after, limit });
      res.json({
        items: Array.isArray(result?.items) ? result.items : [],
        latest_id: Number(result?.latest_id || 0),
        omnipresence: { enabled: Boolean(omni?.enabled) },
      });
    } catch (err) {
      res.status(500).json({ items: [], error: safeJsonError(err), code: "feed_unavailable" });
    }
  });

  // Dismiss/expand acks feed the relevance memory (PRD §3.6). Metadata-only
  // audit: item id and action, never item text.
  app.post("/api/feed/ack", async (req, res) => {
    const requestId = writeRequestId(req);
    if (await maybeReplay(req, res, state, requestId)) return;
    const itemId = parseIntParam(req.body?.itemId ?? req.body?.item, 0);
    const action = typeof req.body?.action === "string" ? req.body.action : "";
    if (!Number.isInteger(itemId) || itemId <= 0) {
      res.status(400).json({ error: "Missing itemId", code: "missing_feed_item_id" });
      return;
    }
    if (!FEED_ACK_ACTIONS.has(action)) {
      res.status(400).json({
        error: "Feed ack action must be 'expanded' or 'dismissed'.",
        code: "invalid_feed_action",
      });
      return;
    }
    if (!reserveReplay(req, state, requestId, config.clock, config.requestIdTtlMs)) {
      if (await maybeReplay(req, res, state, requestId)) return;
    }
    try {
      const result = await provider.feedAck({ item: itemId, action });
      const body = { ok: true, item: itemId, action, requestId, result };
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      audit("feed_ack", { requestId, item: itemId, action });
      res.json(body);
    } catch (err) {
      const message = safeJsonError(err);
      // A structured {"ok":false,...} stdout failure is the CLI rejecting
      // this ack (bad item/action), not bridge infrastructure breaking:
      // relay it as a 400 so clients see the validation error, not a 500.
      const status = err?.cliRejection === true ? 400 : 500;
      const body = { error: message, code: "feed_ack_failed" };
      rememberReplay(req, state, requestId, status, body, config.clock, config.requestIdTtlMs);
      audit("feed_ack_failed", { requestId, item: itemId, action, reason: message });
      res.status(status).json(body);
    }
  });

  // Context ingestion (PRD §3.2). v1 accepts only `location` with strictly
  // numeric coordinates. Coordinates are forwarded to the CLI and never
  // logged: the audit record carries the kind plus a payload hash only.
  app.post("/api/context", async (req, res) => {
    const requestId = writeRequestId(req);
    if (await maybeReplay(req, res, state, requestId)) return;
    const kind = typeof req.body?.kind === "string" ? req.body.kind : "";
    if (kind !== "location") {
      audit("context_rejected", { requestId, kind, reason: "unsupported_kind" });
      res.status(400).json({
        error: "Unsupported context kind. v1 accepts kind 'location' only.",
        code: "unsupported_context_kind",
      });
      return;
    }
    const validated = validateLocationContext(req.body);
    if (!validated.ok) {
      audit("context_rejected", { requestId, kind, reason: validated.code });
      res.status(400).json({ error: validated.error, code: validated.code });
      return;
    }
    if (!reserveReplay(req, state, requestId, config.clock, config.requestIdTtlMs)) {
      if (await maybeReplay(req, res, state, requestId)) return;
    }
    const data = JSON.stringify(validated.payload);
    const payloadHash = sha256(data);
    try {
      const result = await provider.contextAdd({ kind, data });
      const body = { ok: true, kind, id: result?.id, requestId };
      rememberReplay(req, state, requestId, 200, body, config.clock, config.requestIdTtlMs);
      audit("context_add", { requestId, kind, payloadHash });
      res.json(body);
    } catch (err) {
      const message = safeJsonError(err);
      // Structured CLI rejections (stdout {"ok":false,...}) mean the CLI
      // refused this signal; answer 400 like the bridge's own validation.
      const status = err?.cliRejection === true ? 400 : 500;
      const body = { error: message, code: "context_add_failed" };
      rememberReplay(req, state, requestId, status, body, config.clock, config.requestIdTtlMs);
      // The failure reason may echo CLI arguments (which include coordinates),
      // so the audit record stays hash-only; the reason goes to debug logging.
      audit("context_add_failed", { requestId, kind, payloadHash });
      bridgeDebug(config, "context-add-failed", { requestId, reason: message });
      res.status(status).json(body);
    }
  });

  app.get("/api/events", async (req, res) => {
    const requestedSessionId = typeof req.query.sessionId === "string" ? req.query.sessionId : "";
    const sessionId = String(requestedSessionId || state.selectedSession?.id || "morpheus");
    if (isFeedSessionId(sessionId)) {
      // A connected SSE client counts as a feed subscriber for as long as the
      // stream stays open (the buffer's client set keeps the poller alive).
      touchFeedSubscription({ provider, state, config });
    }
    const safeLastEventId = parseIntParam(req.headers["last-event-id"] || req.query.after, 0);
    const replayRequested =
      req.query.needReplay === "true" ||
      req.query.needReplay === "1" ||
      req.query.replay === "true" ||
      req.query.replay === "1";
    let activeEventSession = null;
    const activeProjectEventsId = projectIdFromActiveSessionId(sessionId);
    if (activeProjectEventsId) {
      activeEventSession = activeSessionForProject(state, activeProjectEventsId);
      if (activeEventSession?.id) addSessionAlias(state, activeEventSession.id, sessionId);
    }
    const projectEventsId = projectIdFromSessionId(sessionId);
    if (projectEventsId) {
      activeEventSession = activeSessionForProject(state, projectEventsId);
      if (activeEventSession?.id) addSessionAlias(state, activeEventSession.id, sessionId);
    }
    try {
      if (activeEventSession?.id) {
        await refreshSessionForSessionsPoll({
          provider,
          state,
          config,
          sessionId: activeEventSession.id,
          reason: "events_connect",
        });
      }
    } catch (err) {
      bridgeFlow(config, "events-connect-refresh-failed", {
        sessionId,
        activeSessionId: activeEventSession?.id || "",
        reason: safeJsonError(err),
      });
    }
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.setHeader("X-Accel-Buffering", "no");
    if (typeof res.flushHeaders === "function") res.flushHeaders();

    const transcriptAllowed = (msg) => transcriptStreamAllowed(state, sessionId, requestedSessionId, msg);
    const buffer = cleanMessageState(state, sessionId);
    let replayAfter = replayRequested ? 0 : safeLastEventId;
    let replayEntries = transcriptAllowed() ? getMessages(state, sessionId, replayAfter) : [];
    if (!replayEntries.length && replayAfter > 0 && transcriptAllowed()) {
      replayAfter = 0;
      replayEntries = getMessages(state, sessionId, replayAfter);
    }
    let replayed = 0;
    let dropped = 0;
    for (const entry of replayEntries) {
      if (!transcriptAllowed(entry)) {
        dropped += 1;
        continue;
      }
      res.write(
        `id: ${entry.id}\ndata: ${JSON.stringify(presentMessageForSession(sseMessagePayload(entry), sessionId))}\n\n`,
      );
      replayed += 1;
    }
    res.write(":ok\n\n");
    const client = { res, filter: transcriptAllowed };
    buffer.clients.add(client);
    bridgeDebug(config, "events-connect", {
      sessionId,
      requestedSessionId,
      selectedSessionId: state.selectedSession?.id || "",
      activeSessionId: activeEventSession?.id || "",
      clients: buffer.clients.size,
      replayed,
      dropped,
    });
    bridgeFlow(config, "events-connect", {
      sessionId,
      requestedSessionId,
      selectedProjectId: projectKey(state.selectedProject) || "",
      selectedSessionId: state.selectedSession?.id || "",
      activeSessionId: activeEventSession?.id || "",
      clients: buffer.clients.size,
      replayRequested,
      lastEventId: safeLastEventId,
      replayAfter,
      replayed,
      dropped,
      bufferedMessages: buffer.messages.length,
      latestId: latestMessageId(state, sessionId),
      allowed: transcriptAllowed(),
    });
    const heartbeat = setInterval(() => {
      res.write(":heartbeat\n\n");
    }, 15_000);
    req.on("close", () => {
      clearInterval(heartbeat);
      buffer.clients.delete(client);
      bridgeDebug(config, "events-close", {
        sessionId,
        requestedSessionId,
        clients: buffer.clients.size,
      });
      bridgeFlow(config, "events-close", {
        sessionId,
        requestedSessionId,
        clients: buffer.clients.size,
      });
    });
  });

  return { app, state, config, provider };
}

function startBridge(options = {}) {
  const config = { ...buildConfigFromEnv(), ...options };
  if (!isLocalBindHost(config.host) && !allowUnsafeBind(config)) {
    throw new Error(
      `Refusing to bind Morpheus G2 bridge to non-local host '${config.host}'. ` +
        "Keep the bridge on loopback and publish it with Tailscale Serve, or set " +
        "MORPHEUS_G2_ALLOW_UNSAFE_BIND=1 only behind trusted ACLs.",
    );
  }
  const { app, state, provider } = createBridge(config);
  if (
    config.warmCodexAppServer &&
    provider.agentBackend === AGENT_BACKEND_CODEX_APP_SERVER &&
    typeof provider.warmup === "function"
  ) {
    provider
      .warmup()
      .then(() => config.logger.log("[g2-bridge] codex app-server is ready"))
      .catch((err) =>
        config.logger.warn(`[g2-bridge] codex app-server warm-up failed: ${safeJsonError(err)}`),
      );
  }
  const server = app.listen(config.port, config.host, (err) => {
    // Express 5 also invokes this callback with the listen error; the "error"
    // handler below owns reporting, so skip the startup banner then.
    if (err) return;
    const localUrl = trimTrailingSlash(config.localUrl || `http://${config.host}:${config.port}`);
    const publicUrl = trimTrailingSlash(config.publicUrl || "");
    const qrUrl = publicUrl || localUrl;
    config.logger.log(`Morpheus G2 bridge listening on ${localUrl}`);
    if (publicUrl) {
      config.logger.log(`Phone/G2 URL: ${publicUrl}`);
      config.logger.log(`QR encodes: ${publicUrl}`);
      const hint = publicUrlHint(publicUrl);
      if (hint) config.logger.warn(hint);
    } else {
      config.logger.log(
        "Phone/G2 URL not set. Use MORPHEUS_G2_PUBLIC_URL after `tailscale serve --bg 3456`.",
      );
      config.logger.log(`QR encodes: ${localUrl}`);
    }
    if (config.tokenSource === "ephemeral") {
      config.logger.warn(
        "No MORPHEUS_G2_TOKEN was set; generated an ephemeral token that is not printed.",
      );
    } else {
      config.logger.log("Bearer token loaded from MORPHEUS_G2_TOKEN and not printed.");
    }
    config.logger.log("Use Tailscale Serve for private phone access: tailscale serve --bg " + config.port);
    if (config.host !== "127.0.0.1" && config.host !== "localhost") {
      config.logger.warn("Non-local bind host. Keep this behind Tailscale ACLs or a trusted tunnel.");
    }
    qrcode.generate(qrUrl, { small: true });
    // Resolve omnipresence once at startup and say so — whether the feed row
    // shows is otherwise invisible until a client connects.
    void getOmniStatus({ provider, state, config }).then((omni) => {
      config.logger.log(
        `[g2-bridge] omnipresence ${omni?.enabled ? "enabled — feed row shown" : "disabled — feed row hidden (run: morpheus omni on)"}`,
      );
    });
  });
  server.on("error", (err) => {
    if (err?.code === "EADDRINUSE") {
      config.logger.error(
        `Morpheus G2 bridge could not listen on ${config.host}:${config.port}: the port is already in use. ` +
          "Stop the other process or pick a different port (--port flag or PORT env).",
      );
    } else {
      config.logger.error(
        `Morpheus G2 bridge could not listen on ${config.host}:${config.port}: ${safeJsonError(err)}`,
      );
    }
    process.exitCode = 1;
    // startBridge already kicked off the provider warm-up (codex app-server
    // WebSocket). Tear down what it started so the failed process can drain
    // its event loop and exit, instead of hanging unreachable; process.exit()
    // is deliberately avoided so in-process embedders (and tests) survive.
    config.abortStartup = true;
    try {
      Promise.resolve(provider.shutdown?.()).catch(() => {});
    } catch {
      // best-effort teardown only
    }
    server.close(() => {});
  });
  return server;
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  startBridge();
}

export {
  buildConfigFromEnv,
  createBridge,
  createCodexAppServerBridgeProvider,
  createMorpheusProvider,
  runJsonCommand,
  sessionRowToEvenSession,
  startBridge,
};
