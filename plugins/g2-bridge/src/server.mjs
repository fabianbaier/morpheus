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
const DEFAULT_PROJECT_LIMIT = 25;
const DEFAULT_OUTPUT_POLL_INTERVAL_MS = 2000;
const DEFAULT_OUTPUT_POLL_ATTEMPTS = 45;
const DEFAULT_CODEX_APP_SERVER_PORT = 8765;
const DEFAULT_PROMPT_WAIT_FOR_RESULT_MS = 90_000;
const MAX_MESSAGES_PER_SESSION = 500;
const AGENT_BACKEND_MORPHEUS = "morpheus";
const AGENT_BACKEND_CODEX_APP_SERVER = "codex_app_server";
const EVEN_APP_ORIGINS = [
  "capacitor://localhost",
  "ionic://localhost",
  "http://localhost",
  "https://localhost",
  "null",
];
const REQUEST_ID_RE = /^[A-Za-z0-9._:-]{8,128}$/;
const PROJECT_SESSION_PREFIX = "project:";
const PROJECT_ACTIVE_SESSION_PREFIX = "project-session:";
const PROJECTS_NAV_PROJECT_ID = "__projects__";
const PROJECTS_NAV_SESSION_ID = `${PROJECT_SESSION_PREFIX}${PROJECTS_NAV_PROJECT_ID}`;
const LEGACY_PROJECTS_NAV_SESSION_ID = "nav:projects";
const SECRET_LIKE_RE =
  /\b(sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY)\b/;

function envInt(env, key, fallback, { min = 1, max = Number.MAX_SAFE_INTEGER } = {}) {
  const raw = env[key];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(Math.max(parsed, min), max);
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

function bridgeDebug(config, event, fields = {}) {
  if (!config?.debug) return;
  const payload = Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : "";
  config.logger.log(`[g2-debug] ${event}${payload}`);
}

function cleanMessageState(state, sessionId) {
  const key = sessionId || "morpheus";
  let buffer = state.sessions.get(key);
  if (!buffer) {
    buffer = { messages: [], clients: new Set(), nextId: 1 };
    state.sessions.set(key, buffer);
  }
  return buffer;
}

function pushMessage(state, sessionId, msg) {
  const key = sessionId || "morpheus";
  const buffer = cleanMessageState(state, sessionId);
  const id = buffer.nextId++;
  const entry = { id, ...msg };
  buffer.messages.push(entry);
  if (buffer.messages.length > MAX_MESSAGES_PER_SESSION) {
    buffer.messages.shift();
  }
  const payload = JSON.stringify(msg);
  for (const client of buffer.clients) {
    const res = client?.res || client;
    if (typeof client?.filter === "function" && !client.filter(msg)) continue;
    res.write(`id: ${id}\ndata: ${payload}\n\n`);
  }
  if (msg?.type === "result" || msg?.type === "error") {
    resolveResultWaiters(state, key, entry);
  }
  return id;
}

function latestMessageId(state, sessionId) {
  const buffer = state.sessions.get(sessionId || "morpheus");
  return buffer?.messages?.at(-1)?.id || 0;
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

function waitForResultMessage(state, sessionId, timeoutMs, after = 0) {
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

function replayMessagesToAlias(state, sessionId, alias) {
  const buffer = state.sessions.get(sessionId);
  if (!buffer?.messages?.length) return;
  const aliasBuffer = cleanMessageState(state, alias);
  const seen = new Set(aliasBuffer.messages.map((entry) => messageFingerprint(entry)));
  for (const entry of buffer.messages) {
    const { id: _id, ...msg } = entry;
    const fingerprint = messageFingerprint(msg);
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    pushMessage(state, alias, msg);
  }
}

function sessionMessageTargets(state, sessionId) {
  const key = String(sessionId || "").trim() || "morpheus";
  return [key, ...(state.sessionAliases.get(key) || [])];
}

function pushMessageForSession(state, sessionId, msg) {
  let primaryId = null;
  for (const target of sessionMessageTargets(state, sessionId)) {
    const id = pushMessage(state, target, msg);
    if (target === sessionId || primaryId === null) {
      primaryId = id;
    }
  }
  return primaryId;
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
  const status = statusFromBufferedMessages(state, activeSession.id, activeSession.status || "idle");
  return Boolean(selectedMatches || status === "busy");
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
    return Boolean(
      activeSession?.id &&
        state.selectedSession &&
        sessionMatches(state.selectedSession, activeSession.id) &&
        messageBelongsToSession(msg, activeSession),
    );
  }

  const projectId = projectIdFromSessionId(id);
  if (projectId) {
    const activeSession = activeSessionForProject(state, projectId);
    if (!activeSession?.id) return true;
    return Boolean(
      state.selectedSession &&
        sessionMatches(state.selectedSession, activeSession.id) &&
        messageBelongsToSession(msg, activeSession),
    );
  }

  if (state.selectedSession) {
    if (id === "morpheus") return messageBelongsToSession(msg, state.selectedSession);
    return sessionMatches(state.selectedSession, id) && messageBelongsToSession(msg, state.selectedSession);
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
      await new Promise((resolve) => this.queue.push(resolve));
    }
    this.active += 1;
    try {
      return await fn();
    } finally {
      this.active -= 1;
      const next = this.queue.shift();
      if (next) next();
    }
  }
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

    child.stdout.on("data", (chunk) => {
      stdoutBytes += chunk.length;
      if (stdoutBytes > outputLimitBytes) {
        tooLarge = true;
        child.kill("SIGTERM");
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
      if (timedOut) {
        reject(new Error(`${command} timed out after ${timeoutMs}ms`));
        return;
      }
      if (tooLarge) {
        reject(new Error(`${command} output exceeded ${outputLimitBytes} bytes`));
        return;
      }
      if (code !== 0) {
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
      if (prompt) {
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

function isProjectsNavProjectId(projectId) {
  return projectId === PROJECTS_NAV_PROJECT_ID;
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
  const messages = displayMessages.length ? displayMessages : activeMessages;
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

async function resolveProject(provider, ref, limit) {
  const { projects, current_project_id: currentProjectId } = await listProjects(provider, limit);
  const needle = String(ref || currentProjectId || "").trim();
  if (!needle) {
    return { ok: false, status: 404, error: "no project selected", projects };
  }
  const matches = projects.filter((project) => (
    project.id === needle ||
    project.tenant_id === needle ||
    project.name === needle ||
    project.root_path === needle
  ));
  if (matches.length === 0) {
    return { ok: false, status: 404, error: `no project matching '${needle}'`, projects };
  }
  if (matches.length > 1) {
    return { ok: false, status: 409, error: `ambiguous project reference '${needle}'`, projects };
  }
  return { ok: true, project: matches[0], projects };
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

function writeRequestId(req, state = null) {
  const explicit = normalizeRequestId(req);
  if (explicit) return explicit;
  if (typeof req.body?.text === "string") {
    const bodySessionId = typeof req.body?.sessionId === "string" ? req.body.sessionId : "";
    const selectedProjectId = projectKey(state?.selectedProject);
    const key = JSON.stringify({
      path: req.path,
      sessionId: bodySessionId || state?.selectedSession?.id || "",
      projectId: selectedProjectId,
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

function createRateLimitMiddleware(config, state, clock) {
  return (req, res, next) => {
    const key = req.ip || req.socket?.remoteAddress || "unknown";
    const current = clock();
    const bucket = state.rateLimits.get(key) || { windowStart: current, count: 0 };
    if (current - bucket.windowStart > config.rateLimitWindowMs) {
      bucket.windowStart = current;
      bucket.count = 0;
    }
    bucket.count += 1;
    state.rateLimits.set(key, bucket);
    if (bucket.count > config.rateLimitMax) {
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
  return (req, res, next) => {
    const isEventStream = req.path === "/events" || req.originalUrl?.startsWith("/api/events");
    const provided = normalizeTokenHeader(req, {
      acceptQueryToken: config.acceptQueryToken || isEventStream,
    });
    if (!secureEqual(provided, config.token)) {
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
      const text = String(result?.output?.text || "").trim();
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
    const command = codexResumeCommand({
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

  return {
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
      const result = await codex.prompt("", prompt || goal, cwd);
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
      const result = await codex.prompt(sessionId, text, cwd);
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

    async getHistory(sessionId, limit) {
      const buffered = historyFromBufferedMessages(getMessages(state, sessionId, 0), limit);
      if (hasAssistantHistory(buffered)) {
        return buffered;
      }
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
        const outputHistory = await morpheusOutputHistory(
          sessionId,
          limit,
          state.selectedProject?.id || state.selectedProject?.tenant_id || "",
        );
        if (hasAssistantHistory(outputHistory)) return outputHistory;
        return buffered.length ? buffered : persisted;
      }
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

    getStatus(sessionId) {
      const local = state.codexSessions.get(sessionId);
      const status = codex.getStatus?.(sessionId);
      if (!local && !status) return null;
      return {
        state: status?.state || local?.status || "idle",
        provider: "codex",
      };
    },

    interrupt(sessionId) {
      codex.interrupt?.(sessionId);
    },
  };
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
      ? await waitForResultMessage(state, spawned.id, config.promptWaitForResultMs)
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
    const activeMessages = getMessages(state, spawned.id, 0).map(sseMessagePayload);
    const displayMessages = requestSessionId
      ? getMessages(state, requestSessionId, 0).map(sseMessagePayload)
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
    res.status(202).json(body);
  } catch (err) {
    clearPendingProjectPrompt(state, project, requestId);
    const message = safeJsonError(err);
    const body = { error: message, code: "spawn_failed" };
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
    await refreshSessionOutput({
      provider,
      state,
      config,
      sessionId: outboundSession.id,
      publish: false,
    });
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
    rememberSessionAliases(state, activeSessionId, {
      project: promptProject,
      requestSessionId,
    });
    const shouldWaitForResult =
      config.waitForPromptResult && provider.agentBackend === AGENT_BACKEND_CODEX_APP_SERVER;
    const waitAfterId = waitBeforeIds.get(activeSessionId) || 0;
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
    const activeMessages = getMessages(state, activeSessionId, 0).map(sseMessagePayload);
    const displayMessages = requestSessionId
      ? getMessages(state, requestSessionId, 0).map(sseMessagePayload)
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
    scheduleOutputRefresh(context, activeSessionId);
    res.status(202).json(body);
  } catch (err) {
    const message = safeJsonError(err);
    const body = { error: message, code: "prompt_failed" };
    audit("remote_prompt_failed", {
      requestId,
      sessionId: session.id,
      reason: message,
    });
    rememberReplay(req, state, requestId, 500, body, config.clock, config.requestIdTtlMs);
    res.status(500).json(body);
  }
}

async function refreshSessionOutput({ provider, state, config, sessionId, publish = true }) {
  if (!sessionId || !provider.sessionOutput) return null;
  try {
    const result = await provider.sessionOutput({
      sessionId,
      projectId: state.selectedProject?.id || state.selectedProject?.tenant_id || "",
      lines: 10,
    });
    const text = String(result?.output?.text || "").trim();
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
      return result;
    }
    if (!publish) {
      state.outputHashes.set(sessionId, fingerprint);
      return result;
    }
    if (outputStatus === "busy" || outputStatus === "working") {
      state.outputHashes.set(sessionId, fingerprint);
      pushMessageForSession(state, sessionId, {
        type: "status",
        state: "busy",
        provider: "codex",
        sessionId,
        at: nowIso(config.clock),
      });
      return result;
    }
    publishAssistantResultIfNew(state, config, sessionId, text, "codex");
    return result;
  } catch (err) {
    bridgeDebug(config, "session-output-refresh-failed", {
      sessionId,
      reason: safeJsonError(err),
    });
    config.logger?.warn?.(`[g2-output] ${sessionId}: ${safeJsonError(err)}`);
    return null;
  }
}

async function refreshSessionHistory({ provider, state, config, sessionId }) {
  if (!sessionId || !provider.getHistory) return null;
  try {
    const history = await provider.getHistory(sessionId, 10);
    const text = latestHistoryText(history, "assistant");
    if (!text) return { history, text: "", published: false };
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

function stopOutputPoller(state, sessionId) {
  const timer = state.outputPollers.get(sessionId);
  if (timer) {
    clearInterval(timer);
    state.outputPollers.delete(sessionId);
  }
}

function scheduleOutputRefresh(context, sessionId) {
  const { config, provider, state } = context;
  if (!sessionId || !provider.sessionOutput || state.outputPollers.has(sessionId)) return;
  if (latestTerminalMessageAfterLatestPrompt(state, sessionId)) return;
  const pollAfterId = latestMessageId(state, sessionId);
  let attempts = 0;
  let running = false;
  const tick = async () => {
    if (running) return;
    running = true;
    try {
      if (latestTerminalMessage(state, sessionId, pollAfterId)) {
        stopOutputPoller(state, sessionId);
        return;
      }
      attempts += 1;
      const before = state.outputHashes.get(sessionId);
      const result = await refreshSessionOutput({ provider, state, config, sessionId });
      const historyResult = result?.output?.text
        ? null
        : await refreshSessionHistory({ provider, state, config, sessionId });
      const after = state.outputHashes.get(sessionId);
      const outputStatus = toEvenStatus(result?.session?.state) || result?.session?.state || "idle";
      const isBusy = outputStatus === "busy" || outputStatus === "working";
      const published =
        Boolean(historyResult?.published) ||
        Boolean(result?.output?.text && after && after !== before && !isBusy);
      if (published || attempts >= config.outputPollAttempts) {
        stopOutputPoller(state, sessionId);
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

  const bodySessionId = typeof req.body?.sessionId === "string" ? req.body.sessionId : "";
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
      const body = {
        error: "Select a Morpheus project or session before sending text from G2.",
        code: "project_not_selected",
      };
      audit("remote_text_rejected", {
        requestId,
        reason: "project_not_selected",
        lastProjectId: projectKey(state.lastProject) || "",
      });
      rememberReplay(req, state, requestId, 409, body, config.clock, config.requestIdTtlMs);
      res.status(409).json(body);
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
    codexAppServerPort: envInt(
      env,
      "CODEX_APP_SERVER_PORT",
      DEFAULT_CODEX_APP_SERVER_PORT,
      { min: 1, max: 65535 },
    ),
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
    sessionLimit: envInt(env, "MORPHEUS_G2_SESSION_LIMIT", 12, { min: 1, max: 50 }),
    projectLimit: envInt(env, "MORPHEUS_G2_PROJECT_LIMIT", DEFAULT_PROJECT_LIMIT, { min: 1, max: 50 }),
    spawnCommand: env.MORPHEUS_G2_SPAWN_COMMAND || "codex",
    agentBackend: env.MORPHEUS_G2_AGENT_BACKEND || AGENT_BACKEND_CODEX_APP_SERVER,
    mirrorCodexTui: env.MORPHEUS_G2_MIRROR_CODEX_TUI !== "0",
    includeCodexHistory: env.MORPHEUS_G2_INCLUDE_CODEX_HISTORY === "1",
    requestLog: env.MORPHEUS_G2_REQUEST_LOG !== "0",
    debug: env.MORPHEUS_G2_DEBUG === "1",
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
  config.clock = config.clock || Date.now;
  config.logger = config.logger || console;
  const state = {
    sessions: new Map(),
    codexSessions: new Map(),
    idempotency: new Map(),
    rateLimits: new Map(),
    outputHashes: new Map(),
    outputPollers: new Map(),
    mirroredCodexSessions: new Set(),
    sessionAliases: new Map(),
    resultWaiters: new Map(),
    projectActiveSessions: new Map(),
    pendingProjectPrompts: new Map(),
    projectPromptLocks: new Map(),
    projectSessionRowsCache: new Map(),
    projectListCache: null,
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
      selectedProjectId: state.selectedProject?.id || state.selectedProject?.tenant_id || null,
      selectedSessionId: state.selectedSession?.id || null,
    });
  });

  app.use("/api", createRateLimitMiddleware(config, state, config.clock));
  app.use("/api", createAuthMiddleware(config));
  app.use("/api", createRequestLogMiddleware(config, audit));

  app.get("/api/info", async (_req, res) => {
    const providerInfo = await provider.info();
    res.json({
      provider: provider.name,
      bridge: "g2",
      model: providerInfo.model,
      version: "0.1.0",
      publicUrl: config.publicUrl || null,
      selectedProject: state.selectedProject,
      selectedSession: state.selectedSession,
      allowedActions: provider.allowedActions,
      limits: {
        maxPromptChars: config.maxPromptChars,
        requestIdTtlMs: config.requestIdTtlMs,
        projectLimit: config.projectLimit,
      },
      policy: {
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
      },
    });
  });

  app.get("/api/sessions", async (req, res) => {
    const limit = Math.min(
      Math.max(Number.parseInt(req.query.limit || String(config.sessionLimit), 10), 1),
      config.sessionLimit,
    );
    const wantsProjects =
      req.query.view === "projects" ||
      req.query.scope === "projects" ||
      (!state.selectedProject && config.showProjectsFirst);
    try {
      if (wantsProjects) {
        const result = await listProjectsForResponse(provider, state, config, config.projectLimit);
        res.json(projectsResponseBody(result, state));
        return;
      }
      const { sessions, snapshot, stale, error } = await projectSessionMenuRows(
        provider,
        state,
        config,
        state.selectedProject,
        limit,
      );
      const selected = selectedSessionResponse(state);
      const responseText = selected.text;
      res.json({
        sessions,
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
        mode: "sessions",
        view: navigationView(state),
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
          res.json(projectsResponseBody({ ...cached, error: message }, state));
          return;
        }
      } else if (state.selectedProject) {
        const activeRow = activeProjectSessionRow(state, state.selectedProject);
        const pendingRow = pendingProjectPromptForProject(state, state.selectedProject);
        const selected = selectedSessionResponse(state);
        const responseText = selected.text;
        const rows = [
          ...(config.showBackToProjectsRow ? [projectMenuRow(state.selectedProject)] : []),
          ...(activeRow ? [activeRow] : []),
          ...(pendingRow ? [pendingRow] : []),
        ];
        if (rows.length) {
          res.json({
            sessions: rows,
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
            mode: "sessions",
            view: navigationView(state),
            stale: true,
            error: message,
          });
          return;
        }
      }
      res.status(500).json({ sessions: [], error: safeJsonError(err) });
    }
  });

  app.get("/api/projects", async (req, res) => {
    const limit = Math.min(
      Math.max(Number.parseInt(req.query.limit || String(config.projectLimit), 10), 1),
      config.projectLimit,
    );
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
        res.status(resolved.status).json({ error: resolved.error });
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
      res.status(500).json({ error: message });
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
    const activeProjectSelectId = projectIdFromActiveSessionId(sessionId);
    if (activeProjectSelectId) {
      const resolved = await resolveProject(provider, activeProjectSelectId, config.projectLimit);
      if (!resolved.ok) {
        res.status(resolved.status).json({ error: resolved.error });
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
      const resolved = await resolveProject(provider, projectId, config.projectLimit);
      if (!resolved.ok) {
        res.status(resolved.status).json({ error: resolved.error });
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
        res.status(resolved.status).json({ error: resolved.error });
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
      res.status(500).json({ error: message });
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
    const limit = Math.min(Math.max(Number.parseInt(req.query.limit || "10", 10), 1), 10);
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
        history: [],
        navigation: navigationPayload(state, { action: "navigate_projects" }),
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
        await refreshSessionOutput({ provider, state, config, sessionId: activeSession.id });
        let history = bufferedHistoryForRow(state, sessionId, limit, {
          preferredSessionIds: [activeSession.id],
        });
        if (!hasAssistantHistory(history)) {
          const activeHistory = await sessionHistory(provider, state, activeSession.id, limit);
          history = history.length ? [...history, ...activeHistory].slice(-limit) : activeHistory;
        }
        res.json({
          history,
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
          await refreshSessionOutput({ provider, state, config, sessionId: activeSession.id });
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
          history: [],
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
    const after = Number.parseInt(req.query.after || "0", 10);
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
      await refreshSessionOutput({ provider, state, config, sessionId: activeSession.id });
    } else if (!projectId) {
      await refreshSessionOutput({ provider, state, config, sessionId });
    }
    const projectActiveSession = projectId ? activeSessionForProject(state, projectId) : null;
    if (projectActiveSession?.id) {
      addSessionAlias(state, projectActiveSession.id, sessionId);
      await refreshSessionOutput({ provider, state, config, sessionId: projectActiveSession.id });
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
    const projectRowIsLiveRequest =
      projectId &&
      projectActiveSession &&
      state.selectedSession &&
      sessionMatches(state.selectedSession, projectActiveSession.id);
    if (projectId && !projectRowIsLiveRequest) {
      res.json({
        messages: [],
        state: directStatus?.state || bufferedStatus,
        sessionId,
        activeSessionId: projectActiveSession?.id || undefined,
        pendingSessionId: pendingProjectRow?.id || undefined,
        projectActiveSessionId: projectActiveSession ? `${PROJECT_ACTIVE_SESSION_PREFIX}${projectId}` : undefined,
        provider: directStatus?.provider || provider.name,
      });
      return;
    }
    res.json({
      messages: getMessages(state, sessionId, Number.isFinite(after) ? after : 0),
      state:
        directStatus?.state ||
        bufferedStatus,
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

  app.get("/api/events", (req, res) => {
    const requestedSessionId = typeof req.query.sessionId === "string" ? req.query.sessionId : "";
    const sessionId = String(requestedSessionId || state.selectedSession?.id || "morpheus");
    const lastEventId = Number.parseInt(req.headers["last-event-id"] || req.query.after || "0", 10);
    const activeProjectEventsId = projectIdFromActiveSessionId(sessionId);
    if (activeProjectEventsId) {
      const activeSession = activeSessionForProject(state, activeProjectEventsId);
      if (activeSession?.id) addSessionAlias(state, activeSession.id, sessionId);
    }
    const projectEventsId = projectIdFromSessionId(sessionId);
    if (projectEventsId) {
      const activeSession = activeSessionForProject(state, projectEventsId);
      if (activeSession?.id) addSessionAlias(state, activeSession.id, sessionId);
    }
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.setHeader("X-Accel-Buffering", "no");
    if (typeof res.flushHeaders === "function") res.flushHeaders();

    const transcriptAllowed = (msg) => transcriptStreamAllowed(state, sessionId, requestedSessionId, msg);
    const buffer = cleanMessageState(state, sessionId);
    if (transcriptAllowed()) {
      for (const entry of getMessages(state, sessionId, Number.isFinite(lastEventId) ? lastEventId : 0)) {
        if (!transcriptAllowed(entry)) continue;
        res.write(`id: ${entry.id}\ndata: ${JSON.stringify(sseMessagePayload(entry))}\n\n`);
      }
    }
    res.write(":ok\n\n");
    const client = { res, filter: transcriptAllowed };
    buffer.clients.add(client);
    bridgeDebug(config, "events-connect", {
      sessionId,
      requestedSessionId,
      selectedSessionId: state.selectedSession?.id || "",
      clients: buffer.clients.size,
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
    });
  });

  return { app, state, config, provider };
}

function startBridge(options = {}) {
  const config = { ...buildConfigFromEnv(), ...options };
  const { app } = createBridge(config);
  const server = app.listen(config.port, config.host, () => {
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
