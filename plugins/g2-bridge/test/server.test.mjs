import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createBridge, startBridge } from "../src/server.mjs";
import { G2BridgeClient } from "../simulator/src/bridge-client.js";

const TOKEN = "test-token-123456";
const FIXTURE = fileURLToPath(new URL("./fixtures/mock-morpheus.mjs", import.meta.url));

function silentLogger() {
  return {
    log() {},
    warn() {},
    error() {},
  };
}

function fixtureAuditPath(name) {
  return path.join(tmpdir(), `morpheus-g2-${process.pid}-${name}.jsonl`);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function withBridge(t, options = {}) {
  fs.chmodSync(FIXTURE, 0o755);
  const auditPath = fixtureAuditPath(t.name.replace(/[^A-Za-z0-9_-]/g, "_"));
  fs.rmSync(auditPath, { force: true });
  const bridge = createBridge({
    token: TOKEN,
    morpheusBin: FIXTURE,
    allowedOrigins: ["https://phone.example"],
    publicUrl: "https://mac.tailnet.ts.net",
    agentBackend: "morpheus",
    showProjectsFirst: false,
    auditPath,
    logger: silentLogger(),
    ...options,
  });
  const server = await new Promise((resolve) => {
    const started = bridge.app.listen(0, "127.0.0.1", () => resolve(started));
  });
  t.after(() => {
    server.close();
    fs.rmSync(auditPath, { force: true });
  });
  const port = server.address().port;
  return { ...bridge, baseUrl: `http://127.0.0.1:${port}`, auditPath };
}

function fakeCodexAgentProvider({
  seedSessions = [],
  history = null,
  asyncResultMs = 0,
  emitFinalResult = true,
  promptReturnDelayMs = 0,
  throwOnProjectHistory = false,
  throwHistoryFor = [],
} = {}) {
  const sessions = [...seedSessions];
  let nextId = 1;

  return (emit) => ({
    async getInfo() {
      return { provider: "codex", model: "Codex", version: "test" };
    },

    async listSessions(_limit, cwd) {
      return sessions.filter((session) => !cwd || session.cwd === cwd);
    },

    getStatus(sessionId) {
      const session = sessions.find((item) => item.id === sessionId);
      return session ? { state: session.status, provider: "codex" } : null;
    },

    async getSessionStatus(sessionId) {
      return sessions.find((item) => item.id === sessionId)?.status || "idle";
    },

    async getHistory(sessionId, limit) {
      if (
        (throwOnProjectHistory && String(sessionId).startsWith("project:")) ||
        throwHistoryFor.includes(String(sessionId))
      ) {
        throw new Error("thread not found");
      }
      const entries = history?.[sessionId] || [];
      return entries.slice(-Math.max(1, limit || 10));
    },

    async prompt(sessionId, text, cwd) {
      const id = sessionId || `codex-thread-${nextId++}`;
      let session = sessions.find((item) => item.id === id);
      if (!session) {
        session = {
          id,
          title: `G2: ${text}`,
          timestamp: new Date(1_779_999_999_000).toISOString(),
          cwd,
          status: "busy",
        };
        sessions.unshift(session);
      }
      session.status = "busy";
      emit(id, { type: "status", state: "busy", provider: "codex", sessionId: id });
      const finish = () => {
        if (!emitFinalResult) return;
        emit(id, {
          type: "result",
          success: true,
          text: `answer for: ${text}`,
          provider: "codex",
          sessionId: id,
        });
        session.status = "idle";
        emit(id, { type: "status", state: "idle", provider: "codex", sessionId: id });
      };
      const resultDelayMs = typeof asyncResultMs === "function" ? asyncResultMs(text, id) : asyncResultMs;
      if (resultDelayMs > 0) {
        const timer = setTimeout(finish, resultDelayMs);
        if (typeof timer.unref === "function") timer.unref();
      } else {
        finish();
      }
      if (promptReturnDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, promptReturnDelayMs));
      }
      return { sessionId: id, provider: "codex" };
    },
  });
}

function fakeMorpheusRunner({
  mirrorDelayMs = 0,
  mirrorOutputText = "",
  outputState = "",
  snapshotDelayMs = 0,
  outputDelayMs = 0,
  outputFailuresBeforeSuccess = 0,
  outputErrorMessage = "morpheus timed out after 10000ms",
  onMirrorStart = () => {},
  onMirrorDone = () => {},
  onSpawnCommand = () => {},
  onOutputStart = () => {},
} = {}) {
  let mirroredTabRef = "";
  let outputCalls = 0;
  return async (_command, args) => {
    if (args[0] !== "remote") throw new Error(`unexpected command: ${args.join(" ")}`);

    if (args[1] === "projects") {
      return {
        current_project_id: "p_alpha",
        projects: [
          {
            id: "p_alpha",
            tenant_id: "p_alpha",
            name: "alpha",
            root_path: "/tmp/morpheus-alpha",
            root_kind: "git",
            created_at: 1_779_999_900,
            last_seen_at: 1_779_999_999,
            archived: false,
            usage: { live_sessions: 0, graph_rows: 0 },
          },
        ],
      };
    }

    if (args[1] === "snapshot") {
      if (snapshotDelayMs > 0) await sleep(snapshotDelayMs);
      return {
        generated_at: 1_779_999_999,
        summary: "slow Morpheus snapshot",
        counts: {},
        sessions: [],
      };
    }

    if (args[1] === "spawn") {
      const commandIndex = args.indexOf("--cmd");
      onSpawnCommand(commandIndex === -1 ? "" : args[commandIndex + 1] || "", args);
      onMirrorStart();
      if (mirrorDelayMs > 0) await sleep(mirrorDelayMs);
      onMirrorDone();
      mirroredTabRef = "mirror-tab";
      return {
        ok: true,
        session: {
          tab_ref: "mirror-tab",
          mission_ref: "mirror-mission",
          state: "working",
          goal: args[args.length - 1],
          project: {
            id: "p_alpha",
            tenant_id: "p_alpha",
            name: "alpha",
            root_path: "/tmp/morpheus-alpha",
          },
        },
      };
    }

    if (args[1] === "output") {
      const ref = args[args.length - 1];
      if (ref !== mirroredTabRef) throw new Error(`unexpected output target: ${ref}`);
      onOutputStart(ref);
      if (outputDelayMs > 0) await sleep(outputDelayMs);
      outputCalls += 1;
      if (outputCalls <= outputFailuresBeforeSuccess) {
        throw new Error(outputErrorMessage);
      }
      return {
        ok: true,
        session: {
          tab_ref: ref,
          mission_ref: "mirror-mission",
          state: outputState || (mirrorOutputText ? "idle" : "working"),
          goal: "G2: mirrored Codex session",
        },
        output: {
          text: mirrorOutputText,
          lines: mirrorOutputText ? [mirrorOutputText] : [],
          line_count: mirrorOutputText ? 1 : 0,
          char_count: mirrorOutputText.length,
        },
      };
    }

    throw new Error(`unexpected remote command: ${args.slice(1).join(" ")}`);
  };
}

async function request(baseUrl, pathname, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.token !== null) {
    headers.Authorization = `Bearer ${options.token || TOKEN}`;
  }
  if (options.origin) {
    headers.Origin = options.origin;
  }
  let body;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  return fetch(`${baseUrl}${pathname}`, {
    method: options.method || (body ? "POST" : "GET"),
    headers,
    body,
  });
}

async function requestWithHost(baseUrl, pathname, host, options = {}) {
  const url = new URL(pathname, baseUrl);
  const headers = { ...(options.headers || {}), Host: host };
  if (options.token !== null) {
    headers.Authorization = `Bearer ${options.token || TOKEN}`;
  }
  let body = "";
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: url.hostname,
        port: url.port,
        path: `${url.pathname}${url.search}`,
        method: options.method || (body ? "POST" : "GET"),
        headers,
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          resolve({
            status: res.statusCode,
            headers: {
              get(name) {
                return res.headers[String(name).toLowerCase()] || null;
              },
            },
            async json() {
              return text ? JSON.parse(text) : {};
            },
            async text() {
              return text;
            },
          });
        });
      },
    );
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

async function readStreamUntil(body, needle, timeoutMs = 1000) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  try {
    while (!text.includes(needle)) {
      let timer;
      const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`Timed out waiting for ${needle}`)), Math.max(1, timeoutMs));
        if (typeof timer.unref === "function") timer.unref();
      });
      const chunk = await Promise.race([reader.read(), timeout]).finally(() => clearTimeout(timer));
      if (chunk.done) break;
      text += decoder.decode(chunk.value, { stream: true });
    }
    return text;
  } finally {
    await reader.cancel().catch(() => {});
  }
}

async function readStreamFor(body, durationMs = 100) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  const until = Date.now() + Math.max(1, durationMs);
  try {
    while (Date.now() < until) {
      let timer;
      const timeout = new Promise((resolve) => {
        timer = setTimeout(() => resolve({ done: true, value: null }), Math.max(1, until - Date.now()));
        if (typeof timer.unref === "function") timer.unref();
      });
      const chunk = await Promise.race([reader.read(), timeout]).finally(() => clearTimeout(timer));
      if (chunk.done) break;
      text += decoder.decode(chunk.value, { stream: true });
    }
    return text;
  } finally {
    await reader.cancel().catch(() => {});
  }
}

async function readMessagesUntil(baseUrl, sessionId, predicate, timeoutMs = 1000) {
  const until = Date.now() + Math.max(1, timeoutMs);
  let body = null;
  while (Date.now() < until) {
    const res = await request(baseUrl, `/api/messages?sessionId=${encodeURIComponent(sessionId)}`);
    body = await res.json();
    if (predicate(body.messages || [])) return body;
    await sleep(20);
  }
  throw new Error(`Timed out waiting for messages in ${sessionId}: ${JSON.stringify(body)}`);
}

test("rejects unauthenticated API reads", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await request(baseUrl, "/api/sessions", { token: null });
  assert.equal(res.status, 401);
});

test("accepts Even app query-token authentication", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.sessions[0].id, "abc123");
});

test("exposes Even Terminal compatible status endpoint", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await request(baseUrl, `/api/status?token=${TOKEN}`, { token: null });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.state, "idle");
  assert.equal(body.provider, "morpheus");
});

test("refreshes status for a requested session id without selecting it", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await request(baseUrl, `/api/status?sessionId=abc123&token=${TOKEN}`, {
    token: null,
  });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.sessionId, "abc123");
  assert.equal(body.state, "idle");
  assert.equal(body.provider, "codex");
  assert.equal(body.selectedSession, null);
});

test("can expose projects as G2 session rows first", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  const res = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.mode, "projects");
  assert.equal(body.sessions[0].id, "project:p_alpha");
  assert.equal(body.sessions[0].title, "alpha");
  assert.equal(body.sessions[0].provider, "codex");
  assert.equal(body.sessions[0].status, "idle");
  assert.equal(body.sessions[0].cwd, "/tmp/morpheus-alpha");
});

test("selects a project and then lists project sessions", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  const select = await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha" },
  });
  assert.equal(select.status, 200);
  assert.equal((await select.json()).selectedProject.id, "p_alpha");

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  assert.equal(sessions.status, 200);
  const body = await sessions.json();
  assert.equal(body.mode, "sessions");
  assert.equal(body.sessions[0].id, "project:__projects__");
  assert.equal(body.sessions[0].title, "Back to projects");
  assert.equal(body.sessions[0].promptBehavior, "select_project");
  assert.equal(body.sessions[0].allowedActions.includes("select_project"), true);
  assert.equal(body.sessions[1].id, "abc123");
  assert.equal(body.sessions[1].provider, "codex");
  assert.equal(body.selectedProject.id, "p_alpha");
});

test("opening a project row history returns project session rows without menu transcript", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });

  const projectHistory = await request(baseUrl, "/api/sessions/project:p_alpha/history");
  assert.equal(projectHistory.status, 200);
  const body = await projectHistory.json();
  assert.equal(body.mode, "sessions");
  assert.equal(body.navigation.view, "sessions");
  assert.equal(body.navigation.action, "select_project");
  assert.equal(body.selectedProject.id, "p_alpha");
  assert.equal(body.selectedSession, null);
  assert.equal(body.sessions[0].id, "project:__projects__");
  assert.equal(body.sessions[1].id, "abc123");
  assert.deepEqual(body.history, []);
});

test("codex app-server project menu includes Morpheus snapshot sessions after bridge restart", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const projectHistory = await request(baseUrl, "/api/sessions/project:p_alpha/history");
  assert.equal(projectHistory.status, 200);
  const historyBody = await projectHistory.json();
  assert.equal(historyBody.mode, "sessions");
  assert.equal(historyBody.navigation.view, "sessions");
  assert.equal(historyBody.sessions[0].id, "project:__projects__");
  assert.equal(historyBody.sessions[1].id, "abc123");
  assert.equal(historyBody.selectedSession, null);
  assert.deepEqual(historyBody.history, []);

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  assert.equal(sessions.status, 200);
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.mode, "sessions");
  assert.equal(sessionsBody.sessions[1].id, "abc123");

  const directHistory = await request(baseUrl, "/api/sessions/abc123/history");
  assert.equal(directHistory.status, 200);
  const directHistoryBody = await directHistory.json();
  assert.match(directHistoryBody.history.at(-1).text, /current directory tree/);
});

test("codex app-server can read output for a Morpheus snapshot session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      throwHistoryFor: ["abc123"],
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const selectProject = await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-morpheus-output-project" },
  });
  assert.equal(selectProject.status, 200);

  const selectSession = await request(baseUrl, "/api/select-session", {
    body: { sessionId: "abc123", clientRequestId: "codex-morpheus-output-session" },
  });
  assert.equal(selectSession.status, 200);
  const selectBody = await selectSession.json();
  assert.equal(selectBody.selectedSession.id, "abc123");
  assert.equal(selectBody.selectedSession.promptBehavior, "stage_operator_note");

  const history = await request(baseUrl, "/api/sessions/abc123/history");
  assert.equal(history.status, 200);
  const historyBody = await history.json();
  assert.match(historyBody.history.at(-1).text, /README\.md, morpheus\/, plugins\/, tests\/\./);
});

test("rejects unlisted browser origins before processing API requests", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await request(baseUrl, "/api/info", { origin: "https://evil.example" });
  assert.equal(res.status, 403);
  assert.equal((await res.json()).code, "origin_not_allowed");
});

test("rejects unlisted Host headers before processing API requests", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await requestWithHost(baseUrl, "/api/info", "evil.example:443");
  assert.equal(res.status, 403);
  assert.equal((await res.json()).code, "host_not_allowed");

  const unauthenticated = await requestWithHost(baseUrl, "/api/info", "evil.example:443", {
    token: null,
  });
  assert.equal(unauthenticated.status, 403);
  assert.equal((await unauthenticated.json()).code, "host_not_allowed");
});

test("allows configured public and local Host headers", async (t) => {
  const { baseUrl } = await withBridge(t);

  const local = await request(baseUrl, "/api/info");
  assert.equal(local.status, 200);

  const publicHost = await requestWithHost(baseUrl, "/api/info", "mac.tailnet.ts.net:443");
  assert.equal(publicHost.status, 200);
  const body = await publicHost.json();
  assert.equal(body.publicUrl, "https://mac.tailnet.ts.net");
});

test("keeps healthz public for unlisted Host headers", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await requestWithHost(baseUrl, "/healthz", "evil.example", { token: null });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.equal(body.selectedProjectId, undefined);
  assert.equal(body.selectedSessionId, undefined);
});

test("rejects unsafe bind hosts unless explicitly allowed", () => {
  assert.throws(
    () =>
      startBridge({
        host: "0.0.0.0",
        port: 0,
        token: TOKEN,
        tokenSource: "env",
        logger: silentLogger(),
      }),
    /Refusing to bind Morpheus G2 bridge to non-local host/,
  );
});

test("allows listed origins and returns policy metadata", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await request(baseUrl, "/api/info", { origin: "https://phone.example" });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("access-control-allow-origin"), "https://phone.example");
  const body = await res.json();
  assert.equal(body.publicUrl, "https://mac.tailnet.ts.net");
  assert.equal(body.policy.rawTerminalKeystrokes, false);
  assert.equal(body.policy.remoteApprovals, false);
  assert.equal(body.policy.promptBehavior, "spawn_or_send_prompt");
  assert.equal(body.policy.interruptNavigatesBack, true);
});

test("device state requires authentication", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await request(baseUrl, "/api/device/state", { token: null });
  assert.equal(res.status, 401);
});

test("device state returns selected project and session ids", async (t) => {
  const { baseUrl } = await withBridge(t);
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "device-state-project" },
  });
  await request(baseUrl, "/api/select-session", {
    body: { sessionId: "abc123", clientRequestId: "device-state-session" },
  });

  const res = await request(baseUrl, "/api/device/state");
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.equal(body.bridge, "g2");
  assert.equal(body.view, "session");
  assert.equal(body.mode, "session");
  assert.equal(body.selectedProjectId, "p_alpha");
  assert.equal(body.selectedSessionId, "abc123");
  assert.equal(body.selectedProject.id, "p_alpha");
  assert.equal(body.selectedSession.id, "abc123");
  assert.equal(body.stale, false);
  assert.equal(body.policy.rawTerminalKeystrokes, false);
  assert.equal(body.policy.remoteApprovals, false);
});

test("device state separates project row id from active session id", async (t) => {
  const { baseUrl } = await withBridge(t);
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_beta", clientRequestId: "device-active-project" },
  });
  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      text: "start active device state session",
      clientRequestId: "device-active-prompt",
    },
  });
  assert.equal(prompt.status, 202);

  const res = await request(baseUrl, "/api/device/state");
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.selectedProjectId, "p_beta");
  assert.equal(body.selectedSessionId, "project-session:p_beta");
  assert.equal(body.selectedSession.id, "project-session:p_beta");
  assert.equal(body.activeSessionId, "g2spawn");
  assert.equal(body.projectActiveSessionId, "project-session:p_beta");
});

test("spawns a session for prompt submission without an existing selected session", async (t) => {
  const { baseUrl } = await withBridge(t);
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_beta", clientRequestId: "select-empty-project" },
  });
  const res = await request(baseUrl, "/api/prompt", {
    body: { text: "hello", clientRequestId: "prompt-0001" },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.action, "spawn_session");
  assert.equal(body.sessionId, "g2spawn");
  assert.equal(body.result.session.prompt, "hello");

  const messages = await request(baseUrl, "/api/messages?sessionId=g2spawn");
  const messageBody = await messages.json();
  const resultMessage = messageBody.messages.find((message) => message.type === "result");
  assert.match(resultMessage.text, /current directory tree/);
});

test("rejects prompt without selected project or session context", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await request(baseUrl, "/api/prompt", {
    body: { text: "follow up please", clientRequestId: "prompt-followup-0001" },
  });
  assert.equal(res.status, 409);
  const body = await res.json();
  assert.equal(body.code, "project_not_selected");
});

test("spawns in remembered project when Add session prompt omits sessionId", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_beta", clientRequestId: "remembered-project-select" },
  });
  await request(baseUrl, "/api/sessions/project:__projects__/history");

  const res = await request(baseUrl, "/api/prompt", {
    body: {
      text: "start a fresh remembered project session",
      clientRequestId: "remembered-project-add-session",
    },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.action, "spawn_session");
  assert.equal(body.sessionId, "g2spawn");
  assert.equal(body.selectedProject.id, "p_beta");
  assert.equal(body.result.session.project.root_path, "/tmp/morpheus-beta");
  assert.equal(body.result.session.prompt, "start a fresh remembered project session");
});

test("continues selected session when app posts the project row again", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "project-followup-select-project" },
  });
  await request(baseUrl, "/api/select-session", {
    body: { sessionId: "abc123", clientRequestId: "project-followup-select-session" },
  });

  const res = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "same session please",
      clientRequestId: "project-followup-prompt",
    },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.action, "send_prompt");
  assert.equal(body.sessionId, "abc123");
});

test("selects a session and sends bounded prompt text idempotently", async (t) => {
  const { baseUrl, auditPath } = await withBridge(t);
  const select = await request(baseUrl, "/api/select-session", {
    body: { sessionId: "abc123", clientRequestId: "select-0001" },
  });
  assert.equal(select.status, 200);
  assert.equal((await select.json()).selectedSession.id, "abc123");

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "abc123",
      text: "put this in the Morpheus operator notes",
      clientRequestId: "prompt-0002",
    },
  });
  assert.equal(prompt.status, 202);
  const promptBody = await prompt.json();
  assert.equal(promptBody.action, "send_prompt");
  assert.equal(promptBody.result.ok, true);
  assert.equal(promptBody.result.text_chars, 39);

  const replay = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "abc123",
      text: "put this in the Morpheus operator notes",
      clientRequestId: "prompt-0002",
    },
  });
  assert.equal(replay.status, 202);
  assert.equal((await replay.json()).duplicate, true);

  const messages = await request(baseUrl, "/api/messages?sessionId=abc123");
  const messageBody = await messages.json();
  const promptMessage = messageBody.messages.find((message) => message.type === "prompt_submitted");
  assert.equal(promptMessage.text, undefined);
  assert.equal(promptMessage.textChars, 39);
  assert.match(promptMessage.textHash, /^[a-f0-9]{64}$/);

  const audit = fs.readFileSync(auditPath, "utf8");
  assert.match(audit, /remote_prompt_sent/);
  assert.doesNotMatch(audit, /put this in the Morpheus operator notes/);
});

test("accepts finalized transcript through the same safe prompt path", async (t) => {
  const { baseUrl } = await withBridge(t);
  await request(baseUrl, "/api/select-session", {
    body: { sessionId: "missionalpha", clientRequestId: "select-0002" },
  });
  const res = await request(baseUrl, "/api/transcript/finalize", {
    body: {
      text: "voice final transcript only",
      clientRequestId: "utterance-0001",
    },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.action, "send_prompt");
  assert.equal(body.sessionId, "abc123");
});

test("navigates back from a G2 session directly to projects by default", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "nav-select-project" },
  });
  await request(baseUrl, "/api/select-session", {
    body: { sessionId: "abc123", clientRequestId: "nav-select-session" },
  });

  const selected = await request(baseUrl, "/api/navigation");
  assert.equal((await selected.json()).view, "session");

  const backOne = await request(baseUrl, "/api/back", {
    body: { clientRequestId: "nav-back-1" },
  });
  assert.equal(backOne.status, 200);
  const backOneBody = await backOne.json();
  assert.equal(backOneBody.from, "session");
  assert.equal(backOneBody.to, "projects");
  assert.equal(backOneBody.selectedProject, null);
  assert.equal(backOneBody.selectedSession, null);

  const projects = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const projectsBody = await projects.json();
  assert.equal(projectsBody.mode, "projects");
  assert.equal(projectsBody.sessions[0].id, "project:p_alpha");
});

test("two-step back navigation remains available when direct mode is disabled", async (t) => {
  const { baseUrl } = await withBridge(t, {
    showProjectsFirst: true,
    directBackToProjects: false,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "nav-two-step-select-project-2" },
  });
  await request(baseUrl, "/api/select-session", {
    body: { sessionId: "abc123", clientRequestId: "nav-two-step-select-session-2" },
  });

  const backOne = await request(baseUrl, "/api/back", {
    body: { clientRequestId: "nav-two-step-back-one" },
  });
  const backOneBody = await backOne.json();
  assert.equal(backOneBody.from, "session");
  assert.equal(backOneBody.to, "sessions");
  assert.equal(backOneBody.selectedProject.id, "p_alpha");
  assert.equal(backOneBody.selectedSession, null);

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.mode, "sessions");
  assert.equal(sessionsBody.sessions[0].id, "project:__projects__");
  assert.equal(sessionsBody.sessions[1].id, "abc123");

  const backTwo = await request(baseUrl, "/api/interrupt", {
    body: { clientRequestId: "nav-two-step-back-two" },
  });
  const backTwoBody = await backTwo.json();
  assert.equal(backTwoBody.from, "sessions");
  assert.equal(backTwoBody.to, "projects");
});

test("duplicate in-flight back gesture replays instead of navigating twice", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    directBackToProjects: false,
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "nav-duplicate-back-select-project" },
  });
  await request(baseUrl, "/api/prompt", {
    body: { text: "session for duplicate back", clientRequestId: "nav-duplicate-back-prompt" },
  });

  const [one, two] = await Promise.all([
    request(baseUrl, "/api/back", {
      body: { clientRequestId: "nav-duplicate-back-same-id" },
    }),
    request(baseUrl, "/api/back", {
      body: { clientRequestId: "nav-duplicate-back-same-id" },
    }),
  ]);
  assert.equal(one.status, 200);
  assert.equal(two.status, 200);
  const bodies = [await one.json(), await two.json()];
  assert.equal(bodies.every((body) => body.to === "sessions"), true);
  assert.equal(bodies.some((body) => body.duplicate === true), true);

  const navigation = await request(baseUrl, "/api/navigation");
  assert.equal((await navigation.json()).view, "sessions");
});

test("project menu row returns from sessions to projects", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "nav-row-select-project" },
  });

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.sessions[0].id, "project:__projects__");

  const selectNav = await request(baseUrl, "/api/select-session", {
    body: { sessionId: "project:__projects__", clientRequestId: "nav-row-select" },
  });
  assert.equal(selectNav.status, 200);
  const selectNavBody = await selectNav.json();
  assert.equal(selectNavBody.view, "projects");
  assert.equal(selectNavBody.selectedProject, null);

  const projects = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const projectsBody = await projects.json();
  assert.equal(projectsBody.mode, "projects");
  assert.equal(projectsBody.sessions[0].id, "project:p_alpha");
});

test("project-shaped back row returns projects through select-project", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "nav-project-shaped-select-project" },
  });

  const nav = await request(baseUrl, "/api/select-project", {
    body: { projectId: "__projects__", clientRequestId: "nav-project-shaped-back" },
  });
  assert.equal(nav.status, 200);
  const navBody = await nav.json();
  assert.equal(navBody.mode, "projects");
  assert.equal(navBody.navigation.view, "projects");
  assert.equal(navBody.selectedProject, null);
  assert.equal(navBody.sessions[0].id, "project:p_alpha");
});

test("project menu history request returns to projects for stock Even row opens", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "nav-history-select-project" },
  });

  const history = await request(baseUrl, "/api/sessions/project:__projects__/history");
  assert.equal(history.status, 200);
  const historyBody = await history.json();
  assert.equal(historyBody.mode, "projects");
  assert.equal(historyBody.navigation.view, "projects");
  assert.deepEqual(historyBody.history, []);
  assert.equal(historyBody.sessions[0].id, "project:p_alpha");
  assert.equal(historyBody.sessions[0].title, "alpha");

  const projects = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const projectsBody = await projects.json();
  assert.equal(projectsBody.mode, "projects");
  assert.equal(projectsBody.sessions[0].id, "project:p_alpha");
});

test("legacy nav project history still returns projects", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "legacy-nav-history-select-project" },
  });

  const history = await request(baseUrl, "/api/sessions/nav:projects/history");
  assert.equal(history.status, 200);
  const historyBody = await history.json();
  assert.equal(historyBody.mode, "projects");
  assert.deepEqual(historyBody.history, []);
  assert.equal(historyBody.sessions[0].id, "project:p_alpha");
});

test("project menu history uses cached project rows if live project listing fails", async (t) => {
  const baseRunner = fakeMorpheusRunner();
  let failProjects = false;
  const runner = async (command, args, options) => {
    if (args[1] === "projects" && failProjects) {
      throw new Error("project list down");
    }
    return baseRunner(command, args, options);
  };
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    runner,
    showProjectsFirst: true,
  });

  const firstProjects = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  assert.equal(firstProjects.status, 200);
  assert.equal((await firstProjects.json()).sessions[0].id, "project:p_alpha");

  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "nav-history-cached-select-project" },
  });
  failProjects = true;

  const history = await request(baseUrl, "/api/sessions/project:__projects__/history");
  assert.equal(history.status, 200);
  const historyBody = await history.json();
  assert.equal(historyBody.mode, "projects");
  assert.equal(historyBody.stale, true);
  assert.deepEqual(historyBody.history, []);
  assert.equal(historyBody.sessions[0].id, "project:p_alpha");
});

test("session menu polling uses cached rows if live provider listing fails", async (t) => {
  let failListSessions = false;
  const codexSession = {
    id: "codex-cached",
    title: "Cached Codex session",
    timestamp: new Date(1_779_999_999_000).toISOString(),
    cwd: "/tmp/morpheus-alpha",
    status: "idle",
  };
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    includeCodexHistory: true,
    createCodexAgentProvider: () => ({
      async getInfo() {
        return { provider: "codex", model: "Codex", version: "test" };
      },
      async listSessions(_limit, cwd) {
        if (failListSessions) throw new Error("codex list sessions down");
        return !cwd || codexSession.cwd === cwd ? [codexSession] : [];
      },
      getStatus(sessionId) {
        return sessionId === codexSession.id ? { state: "idle", provider: "codex" } : null;
      },
      async getSessionStatus(sessionId) {
        return sessionId === codexSession.id ? "idle" : "unknown";
      },
      async getHistory() {
        return [];
      },
      async prompt() {
        throw new Error("prompt not expected");
      },
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "session-cache-select-project" },
  });

  const firstSessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  assert.equal(firstSessions.status, 200);
  const firstBody = await firstSessions.json();
  assert.equal(firstBody.mode, "sessions");
  assert.equal(firstBody.sessions.some((session) => session.id === "codex-cached"), true);
  failListSessions = true;

  const secondSessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  assert.equal(secondSessions.status, 200);
  const secondBody = await secondSessions.json();
  assert.equal(secondBody.mode, "sessions");
  assert.equal(secondBody.stale, true);
  assert.match(secondBody.error, /codex list sessions down/);
  assert.equal(secondBody.sessions.some((session) => session.id === "codex-cached"), true);
  assert.equal(secondBody.sessions[0].id, "project:__projects__");
});

test("spawns a local Morpheus session when prompting a project row", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  const res = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_beta",
      text: "hey can you hear me",
    },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.action, "spawn_session");
  assert.equal(body.sessionId, "g2spawn");
  assert.equal(body.selectedProject.id, "p_beta");
  assert.equal(body.result.session.cmd, "codex");
  assert.equal(body.result.session.prompt, "hey can you hear me");
});

test("codex app-server backend emits final results without terminal polling", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_beta", clientRequestId: "codex-select-project" },
  });

  const res = await request(baseUrl, "/api/prompt", {
    body: { text: "list the directory", clientRequestId: "codex-prompt-0001" },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.action, "spawn_session");
  assert.equal(body.provider, "codex");
  assert.equal(body.sessionId, "codex-thread-1");
  assert.equal(body.text, "answer for: list the directory");
  assert.equal(body.output.text, "answer for: list the directory");
  assert.equal(body.state, "idle");
  assert.equal(body.selectedSession.status, "idle");
  assert.equal(body.selectedSession.promptBehavior, "send_prompt");

  const messages = await request(baseUrl, "/api/messages?sessionId=codex-thread-1");
  const messageBody = await messages.json();
  const resultMessage = messageBody.messages.find((message) => message.type === "result");
  assert.equal(resultMessage.text, "answer for: list the directory");
  assert.equal(messageBody.state, "idle");

  const history = await request(baseUrl, "/api/sessions/codex-thread-1/history");
  const historyBody = await history.json();
  assert.deepEqual(historyBody.history.at(-1), {
    role: "assistant",
    text: "answer for: list the directory",
  });
});

test("codex app-server prompt response waits for delayed final result", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 25 }),
    mirrorCodexTui: false,
    waitForPromptResult: true,
    promptWaitForResultMs: 1000,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_beta", clientRequestId: "codex-delayed-select-project" },
  });

  const res = await request(baseUrl, "/api/prompt", {
    body: { text: "delayed answer", clientRequestId: "codex-delayed-prompt" },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.sessionId, "codex-thread-1");
  assert.equal(body.state, "idle");
  assert.equal(body.text, "answer for: delayed answer");
  assert.equal(body.history.at(-1).text, "answer for: delayed answer");
});

test("codex app-server backend mirrors results to project row and default message targets", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_beta", clientRequestId: "codex-alias-select-project" },
  });

  const res = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_beta",
      text: "alias target please",
      clientRequestId: "codex-alias-prompt",
    },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.sessionId, "codex-thread-1");
  assert.equal(body.activeSessionId, "codex-thread-1");
  assert.equal(body.displaySessionId, "project-session:p_beta");

  const actual = await request(baseUrl, "/api/messages?sessionId=codex-thread-1");
  const actualBody = await actual.json();
  assert.equal(actualBody.messages.find((message) => message.type === "result").text, "answer for: alias target please");

  const projectAlias = await request(baseUrl, "/api/messages?sessionId=project:p_beta");
  const projectAliasBody = await projectAlias.json();
  assert.equal(
    projectAliasBody.messages.find((message) => message.type === "result").text,
    "answer for: alias target please",
  );

  const projectStatus = await request(baseUrl, "/api/status?sessionId=project:p_beta");
  const projectStatusBody = await projectStatus.json();
  assert.equal(projectStatusBody.sessionId, "project:p_beta");
  assert.equal(projectStatusBody.activeSessionId, "codex-thread-1");
  assert.equal(projectStatusBody.state, "idle");
  assert.equal(projectStatusBody.selectedSession, null);

  const projectHistory = await request(baseUrl, "/api/sessions/project:p_beta/history");
  const projectHistoryBody = await projectHistory.json();
  assert.equal(projectHistoryBody.navigation.view, "session");
  assert.equal(projectHistoryBody.activeSessionId, "codex-thread-1");
  assert.equal(projectHistoryBody.selectedSession.activeSessionId, "codex-thread-1");
  assert.equal(projectHistoryBody.sessions, undefined);
  assert.equal(projectHistoryBody.history.at(-1).role, "assistant");
  assert.equal(projectHistoryBody.history.at(-1).text, "answer for: alias target please");

  const activeHistory = await request(baseUrl, "/api/sessions/project-session:p_beta/history");
  const activeHistoryBody = await activeHistory.json();
  assert.equal(activeHistoryBody.history.at(-1).role, "assistant");
  assert.equal(activeHistoryBody.history.at(-1).text, "answer for: alias target please");

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.sessions[0].id, "project:__projects__");
  assert.equal(sessionsBody.sessions[1].id, "project-session:p_beta");
  assert.equal(sessionsBody.sessions[1].activeSessionId, "codex-thread-1");
  assert.equal(sessionsBody.sessions[1].latestOutput, "answer for: alias target please");
  assert.equal(sessionsBody.sessions[1].title, "G2: alias target please");
  assert.equal(sessionsBody.sessions.some((session) => session.id === "codex-thread-1"), false);

  const defaultMessages = await request(baseUrl, "/api/messages");
  const defaultBody = await defaultMessages.json();
  assert.equal(
    defaultBody.messages.find((message) => message.type === "result").text,
    "answer for: alias target please",
  );
});

test("project row messages stop exposing transcript output after leaving the session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "visible only while live",
      clientRequestId: "codex-project-live-only",
    },
  });

  const liveProjectMessages = await request(baseUrl, "/api/messages?sessionId=project:p_alpha");
  const liveProjectBody = await liveProjectMessages.json();
  assert.equal(
    liveProjectBody.messages.find((message) => message.type === "result").text,
    "answer for: visible only while live",
  );

  await request(baseUrl, "/api/sessions/project:__projects__/history");

  const staleProjectMessages = await request(baseUrl, "/api/messages?sessionId=project:p_alpha");
  const staleProjectBody = await staleProjectMessages.json();
  assert.deepEqual(staleProjectBody.messages, []);

  const activeMessages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const activeBody = await activeMessages.json();
  assert.equal(
    activeBody.messages.find((message) => message.type === "result").text,
    "answer for: visible only while live",
  );
});

test("concurrent project row prompts do not spawn duplicate codex sessions", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ promptReturnDelayMs: 25 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const [first, second] = await Promise.all([
    request(baseUrl, "/api/prompt", {
      body: {
        sessionId: "project:p_alpha",
        text: "first concurrent turn",
        clientRequestId: "codex-concurrent-project-1",
      },
    }),
    request(baseUrl, "/api/prompt", {
      body: {
        sessionId: "project:p_alpha",
        text: "second concurrent turn",
        clientRequestId: "codex-concurrent-project-2",
      },
    }),
  ]);

  assert.equal(first.status, 202);
  assert.equal(second.status, 202);
  const firstBody = await first.json();
  const secondBody = await second.json();
  assert.equal(firstBody.sessionId, "codex-thread-1");
  assert.equal(secondBody.sessionId, "codex-thread-1");

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.sessions.some((session) => session.activeSessionId === "codex-thread-2"), false);
  assert.equal(sessionsBody.sessions[1].activeSessionId, "codex-thread-1");
});

test("duplicate in-flight prompt request id replays original response", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ promptReturnDelayMs: 25 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const body = {
    sessionId: "project:p_alpha",
    text: "same request should only spawn once",
    clientRequestId: "codex-duplicate-in-flight",
  };
  const [first, second] = await Promise.all([
    request(baseUrl, "/api/prompt", { body }),
    request(baseUrl, "/api/prompt", { body }),
  ]);

  assert.equal(first.status, 202);
  assert.equal(second.status, 202);
  const firstBody = await first.json();
  const secondBody = await second.json();
  assert.equal(firstBody.sessionId, "codex-thread-1");
  assert.equal(secondBody.sessionId, "codex-thread-1");
  assert.equal(secondBody.duplicate, true);

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.sessions.some((session) => session.activeSessionId === "codex-thread-2"), false);
});

test("missing request id still dedupes identical in-flight prompt for stock clients", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ promptReturnDelayMs: 25 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const body = {
    sessionId: "project:p_alpha",
    text: "stock client omitted request id",
  };
  const [first, second] = await Promise.all([
    request(baseUrl, "/api/prompt", { body }),
    request(baseUrl, "/api/prompt", { body }),
  ]);

  assert.equal(first.status, 202);
  assert.equal(second.status, 202);
  const firstBody = await first.json();
  const secondBody = await second.json();
  assert.equal(firstBody.sessionId, "codex-thread-1");
  assert.equal(secondBody.sessionId, "codex-thread-1");
  assert.equal(secondBody.duplicate, true);

  const repeat = await request(baseUrl, "/api/prompt", { body });
  assert.equal(repeat.status, 202);
  const repeatBody = await repeat.json();
  assert.equal(repeatBody.sessionId, "codex-thread-1");
  assert.equal(repeatBody.duplicate, undefined);
});

test("prompt failure after idempotency reservation resolves duplicate waiters", async (t) => {
  const { baseUrl } = await withBridge(t, {
    provider: {
      name: "codex",
      agentBackend: "codex_app_server",
      async getInfo() {
        return { provider: "codex" };
      },
      async listSessions() {
        return { sessions: [] };
      },
      async listProjects() {
        throw new Error("project list down");
      },
    },
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const body = {
    sessionId: "project:p_alpha",
    text: "trigger project list failure",
    clientRequestId: "prompt-fails-after-reserve",
  };
  const [first, second] = await Promise.all([
    request(baseUrl, "/api/prompt", { body }),
    request(baseUrl, "/api/prompt", { body }),
  ]);
  assert.equal(first.status, 500);
  assert.equal(second.status, 500);
  const bodies = [await first.json(), await second.json()];
  assert.equal(bodies.every((item) => item.code === "prompt_failed"), true);
  assert.equal(bodies.some((item) => item.duplicate === true), true);
});

test("fast follow-up results are not followed by a stale busy status", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-fast-order-select" },
  });
  await request(baseUrl, "/api/prompt", {
    body: { text: "initial fast order", clientRequestId: "codex-fast-order-first" },
  });
  await request(baseUrl, "/api/prompt", {
    body: { text: "fast follow-up order", clientRequestId: "codex-fast-order-second" },
  });

  const messages = await request(baseUrl, "/api/messages?sessionId=codex-thread-1");
  const messageBody = await messages.json();
  const resultIndex = messageBody.messages.findIndex(
    (message) => message.type === "result" && message.text === "answer for: fast follow-up order",
  );
  const busyIndex = messageBody.messages.findIndex(
    (message, index) => index < resultIndex && message.type === "status" && message.state === "busy",
  );
  assert.ok(busyIndex >= 0);
  assert.ok(busyIndex < resultIndex);
  const lastStatus = messageBody.messages.filter((message) => message.type === "status").at(-1);
  assert.equal(lastStatus.state, "idle");

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.selectedSession.status, "idle");
  assert.equal(
    sessionsBody.sessions.find((session) => session.id === "project-session:p_alpha").status,
    "idle",
  );
});

test("history prefers buffered assistant result over user-only persisted history", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      history: {
        "codex-thread-1": [{ role: "user", text: "alias target please" }],
      },
    }),
    mirrorCodexTui: false,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_beta", clientRequestId: "codex-history-buffer-select" },
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      text: "alias target please",
      clientRequestId: "codex-history-buffer-prompt",
    },
  });

  const historyRes = await request(baseUrl, "/api/sessions/codex-thread-1/history");
  const historyBody = await historyRes.json();
  assert.equal(historyBody.history.at(-1).role, "assistant");
  assert.equal(historyBody.history.at(-1).text, "answer for: alias target please");
});

test("codex app-server backend keeps follow-ups in the selected session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-follow-select-project" },
  });
  await request(baseUrl, "/api/prompt", {
    body: { text: "first turn", clientRequestId: "codex-follow-first" },
  });

  const follow = await request(baseUrl, "/api/prompt", {
    body: { text: "same session follow up", clientRequestId: "codex-follow-second" },
  });
  assert.equal(follow.status, 202);
  const followBody = await follow.json();
  assert.equal(followBody.action, "send_prompt");
  assert.equal(followBody.sessionId, "codex-thread-1");

  const sessions = await request(baseUrl, "/api/sessions?token=test-token-123456", { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.sessions.filter((session) => session.id === "codex-thread-1").length, 0);
  assert.equal(sessionsBody.sessions[0].id, "project:__projects__");
  assert.equal(sessionsBody.sessions[1].id, "project-session:p_alpha");
  assert.equal(sessionsBody.sessions[1].activeSessionId, "codex-thread-1");
  assert.equal(sessionsBody.sessions[1].latestOutput, "answer for: same session follow up");

  const messages = await request(baseUrl, "/api/messages?sessionId=codex-thread-1");
  const messageBody = await messages.json();
  const results = messageBody.messages.filter((message) => message.type === "result");
  assert.equal(results.at(-1).text, "answer for: same session follow up");

  const projectMessages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const projectMessageBody = await projectMessages.json();
  const projectResults = projectMessageBody.messages.filter((message) => message.type === "result");
  assert.equal(projectResults.at(-1).text, "answer for: same session follow up");
});

test("project row follow-up reuses remembered active session after back and reopen", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-reopen-select-project" },
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "first reopened turn",
      clientRequestId: "codex-reopen-first",
    },
  });

  await request(baseUrl, "/api/sessions/project:__projects__/history");
  const projectHistory = await request(baseUrl, "/api/sessions/project:p_alpha/history");
  const projectHistoryBody = await projectHistory.json();
  assert.equal(projectHistoryBody.navigation.view, "sessions");
  assert.equal(projectHistoryBody.selectedSession, null);
  assert.equal(projectHistoryBody.mode, "sessions");
  assert.equal(projectHistoryBody.sessions[0].id, "project:__projects__");
  assert.equal(projectHistoryBody.sessions[1].id, "project-session:p_alpha");

  const activeHistory = await request(baseUrl, "/api/sessions/project-session:p_alpha/history");
  const activeHistoryBody = await activeHistory.json();
  assert.equal(activeHistoryBody.history.at(-1).text, "answer for: first reopened turn");

  const follow = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "same reopened session",
      clientRequestId: "codex-reopen-follow",
    },
  });
  assert.equal(follow.status, 202);
  const followBody = await follow.json();
  assert.equal(followBody.action, "send_prompt");
  assert.equal(followBody.sessionId, "codex-thread-1");
  assert.equal(followBody.text, "answer for: same reopened session");

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.sessions.some((session) => session.id === "codex-thread-2"), false);
  assert.equal(sessionsBody.sessions[1].id, "project-session:p_alpha");
  assert.equal(sessionsBody.sessions[1].activeSessionId, "codex-thread-1");
  assert.equal(sessionsBody.sessions[1].latestOutput, "answer for: same reopened session");

  const projectMessages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const projectMessageBody = await projectMessages.json();
  const projectResults = projectMessageBody.messages.filter((message) => message.type === "result");
  assert.equal(projectResults.at(-1).text, "answer for: same reopened session");
});

test("concurrent active project-session follow-ups serialize per project", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 40 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: true,
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "first active session turn",
      clientRequestId: "codex-active-concurrent-first",
    },
  });

  const [first, second] = await Promise.all([
    request(baseUrl, "/api/prompt", {
      body: {
        sessionId: "project-session:p_alpha",
        text: "active followup A",
        clientRequestId: "codex-active-concurrent-a",
      },
    }),
    request(baseUrl, "/api/prompt", {
      body: {
        sessionId: "project-session:p_alpha",
        text: "active followup B",
        clientRequestId: "codex-active-concurrent-b",
      },
    }),
  ]);
  assert.equal(first.status, 202);
  assert.equal(second.status, 202);
  const firstBody = await first.json();
  const secondBody = await second.json();
  assert.equal(firstBody.sessionId, "codex-thread-1");
  assert.equal(secondBody.sessionId, "codex-thread-1");
  assert.equal(firstBody.text, "answer for: active followup A");
  assert.equal(secondBody.text, "answer for: active followup B");
});

test("project row history returns the session menu after leaving a live session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ throwOnProjectHistory: true }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-project-history-select" },
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "answer visible through project history",
      clientRequestId: "codex-project-history-prompt",
    },
  });
  await request(baseUrl, "/api/back", {
    body: { clientRequestId: "codex-project-history-back" },
  });

  const projectHistory = await request(baseUrl, "/api/sessions/project:p_alpha/history");
  assert.equal(projectHistory.status, 200);
  const projectHistoryBody = await projectHistory.json();
  assert.equal(projectHistoryBody.navigation.view, "sessions");
  assert.equal(projectHistoryBody.mode, "sessions");
  assert.equal(projectHistoryBody.sessions[0].id, "project:__projects__");
  assert.equal(projectHistoryBody.sessions[1].id, "project-session:p_alpha");

  const history = await request(baseUrl, "/api/sessions/project-session:p_alpha/history");
  assert.equal(history.status, 200);
  const historyBody = await history.json();
  assert.equal(historyBody.history.at(-1).role, "assistant");
  assert.equal(historyBody.history.at(-1).text, "answer for: answer visible through project history");
});

test("project row history returns the session menu after leaving a busy active session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 500 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: false,
  });
  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "busy project should not auto-open",
      clientRequestId: "codex-project-history-busy-menu-prompt",
    },
  });
  assert.equal(prompt.status, 202);
  assert.equal((await prompt.json()).state, "busy");

  await request(baseUrl, "/api/sessions/project:__projects__/history");

  const projectHistory = await request(baseUrl, "/api/sessions/project:p_alpha/history");
  assert.equal(projectHistory.status, 200);
  const projectHistoryBody = await projectHistory.json();
  assert.equal(projectHistoryBody.mode, "sessions");
  assert.equal(projectHistoryBody.navigation.view, "sessions");
  assert.equal(projectHistoryBody.navigation.action, "select_project");
  assert.equal(projectHistoryBody.selectedProject.id, "p_alpha");
  assert.equal(projectHistoryBody.selectedSession, null);
  assert.equal(projectHistoryBody.sessions[0].id, "project:__projects__");
  assert.equal(projectHistoryBody.sessions[1].id, "project-session:p_alpha");
  assert.equal(projectHistoryBody.sessions[1].status, "busy");
  assert.deepEqual(projectHistoryBody.history, []);
});

test("project row event stream receives codex final result via query-token auth", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 25 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    acceptQueryToken: false,
  });

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);
  assert.match(events.headers.get("content-type") || "", /text\/event-stream/);

  const streamed = readStreamUntil(events.body, "answer for: stream this answer", 1000);
  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "stream this answer",
      clientRequestId: "codex-stream-project-row-prompt",
    },
  });
  assert.equal(prompt.status, 202);
  assert.match(await streamed, /"type":"result"/);
});

test("codex prompt can return before final answer when result waiting is disabled", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 80 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: false,
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "live result after submit",
      clientRequestId: "codex-live-after-submit",
    },
  });
  assert.equal(prompt.status, 202);
  const promptBody = await prompt.json();
  assert.equal(promptBody.state, "busy");
  assert.equal(promptBody.text, "");

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const streamed = await readStreamUntil(events.body, "answer for: live result after submit", 1000);
  assert.match(streamed, /"type":"result"/);

  const messages = await request(baseUrl, "/api/messages?sessionId=project:p_alpha");
  const messageBody = await messages.json();
  assert.equal(
    messageBody.messages.find((message) => message.type === "result").text,
    "answer for: live result after submit",
  );
});

test("project history polling during new session keeps glasses stream live", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 120 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: false,
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "stay live despite project history poll",
      clientRequestId: "codex-live-project-history-race",
    },
  });
  assert.equal(prompt.status, 202);
  assert.equal((await prompt.json()).state, "busy");

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);

  const projectHistory = await request(baseUrl, "/api/sessions/project:p_alpha/history");
  assert.equal(projectHistory.status, 200);
  const projectHistoryBody = await projectHistory.json();
  assert.equal(projectHistoryBody.mode, "session");
  assert.equal(projectHistoryBody.view, "session");
  assert.equal(projectHistoryBody.navigation.view, "session");
  assert.equal(projectHistoryBody.activeSessionId, "codex-thread-1");
  assert.equal(projectHistoryBody.selectedSession.activeSessionId, "codex-thread-1");
  assert.equal(projectHistoryBody.sessions, undefined);

  const streamed = await readStreamUntil(events.body, "answer for: stay live despite project history poll", 1000);
  assert.match(streamed, /"type":"result"/);

  const navigation = await request(baseUrl, "/api/navigation");
  const navigationBody = await navigation.json();
  assert.equal(navigationBody.view, "session");
  assert.equal(navigationBody.selectedSession.id, "codex-thread-1");
});

test("project history polling while codex thread id is pending keeps glasses stream live", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      asyncResultMs: 120,
      promptReturnDelayMs: 200,
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: false,
  });

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const streamed = readStreamUntil(events.body, "answer for: slow thread id still streams", 1000);

  const prompt = request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "slow thread id still streams",
      clientRequestId: "codex-pending-thread-stream",
    },
  });
  await sleep(40);

  const projectHistory = await request(baseUrl, "/api/sessions/project:p_alpha/history");
  assert.equal(projectHistory.status, 200);
  const projectHistoryBody = await projectHistory.json();
  assert.equal(projectHistoryBody.navigation.view, "session");
  assert.equal(projectHistoryBody.selectedSession.pending, true);
  assert.equal(projectHistoryBody.history[0].role, "user");
  assert.equal(projectHistoryBody.history[0].text, "slow thread id still streams");

  const promptRes = await prompt;
  assert.equal(promptRes.status, 202);
  assert.match(await streamed, /"type":"result"/);
});

test("client polling prefers codex thread history over slow terminal mirror output", async (t) => {
  let outputCalls = 0;
  let resolveMirrorDone;
  const mirrorDone = new Promise((resolve) => {
    resolveMirrorDone = resolve;
  });
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 20,
      history: {
        "codex-thread-1": [
          { role: "user", text: "history first, mirror slow" },
          { role: "assistant", text: "answer from real codex thread history" },
        ],
      },
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 30000,
    outputPollAttempts: 0,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "terminal mirror should not block client polling",
      outputDelayMs: 1200,
      onMirrorDone: () => resolveMirrorDone(),
      onOutputStart: () => {
        outputCalls += 1;
      },
    }),
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "history first, mirror slow",
      clientRequestId: "codex-thread-history-before-slow-mirror",
    },
  });
  assert.equal(prompt.status, 202);
  await mirrorDone;

  const sessionsStarted = Date.now();
  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsElapsedMs = Date.now() - sessionsStarted;
  assert.equal(sessions.status, 200);
  assert.ok(
    sessionsElapsedMs < 700,
    `/api/sessions waited ${sessionsElapsedMs}ms, which means it likely blocked on terminal output`,
  );
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.mode, "session");
  assert.equal(sessionsBody.state, "idle");
  assert.equal(sessionsBody.text, "answer from real codex thread history");
  assert.equal(sessionsBody.output.text, "answer from real codex thread history");
  assert.equal(sessionsBody.history.at(-1).text, "answer from real codex thread history");
  assert.equal(
    sessionsBody.messages.find((message) => message.type === "result")?.text,
    "answer from real codex thread history",
  );
  assert.equal(outputCalls, 0);

  const messagesStarted = Date.now();
  const messages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const messagesElapsedMs = Date.now() - messagesStarted;
  assert.equal(messages.status, 200);
  assert.ok(
    messagesElapsedMs < 700,
    `/api/messages waited ${messagesElapsedMs}ms, which means it likely blocked on terminal output`,
  );
  const messagesBody = await messages.json();
  assert.equal(messagesBody.state, "idle");
  assert.equal(
    messagesBody.messages.find((message) => message.type === "result")?.text,
    "answer from real codex thread history",
  );
  assert.equal(outputCalls, 0);
});

test("client polling skips terminal fallback while codex thread history is not ready", async (t) => {
  let outputCalls = 0;
  let resolveMirrorDone;
  const mirrorDone = new Promise((resolve) => {
    resolveMirrorDone = resolve;
  });
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 20,
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 30000,
    outputPollAttempts: 0,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "terminal mirror answer is only for the background poller",
      outputDelayMs: 1200,
      onMirrorDone: () => resolveMirrorDone(),
      onOutputStart: () => {
        outputCalls += 1;
      },
    }),
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "history not ready, mirror slow",
      clientRequestId: "codex-thread-history-not-ready-slow-mirror",
    },
  });
  assert.equal(prompt.status, 202);
  await mirrorDone;

  const started = Date.now();
  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const elapsedMs = Date.now() - started;
  assert.equal(sessions.status, 200);
  assert.ok(
    elapsedMs < 700,
    `/api/sessions waited ${elapsedMs}ms, which means it likely used terminal fallback`,
  );
  const body = await sessions.json();
  assert.equal(body.mode, "session");
  assert.equal(body.text || "", "");
  assert.equal(
    body.messages.some((message) => String(message.text || "").includes("terminal mirror answer")),
    false,
  );
  assert.equal(outputCalls, 0);
});

test("mirrored terminal output is streamed when codex app-server misses final result", async (t) => {
  let mirrorCommand = "";
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 20,
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 20,
    outputPollAttempts: 20,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "terminal mirror answer: 2",
      onSpawnCommand: (command) => {
        mirrorCommand = command;
      },
    }),
  });

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const streamed = readStreamUntil(events.body, "terminal mirror answer: 2", 1000);

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "one plus one through mirror",
      clientRequestId: "codex-mirror-output-stream",
    },
  });
  assert.equal(prompt.status, 202);
  assert.match(await streamed, /"type":"result"/);

  const messages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const messagesBody = await messages.json();
  assert.equal(
    messagesBody.messages.find((message) => message.type === "result")?.text,
    "terminal mirror answer: 2",
  );
  assert.match(mirrorCommand, /resume 'codex-thread-1' #$/);
});

test("mirrored terminal output strips command echoes and duplicate resumed prompts", async (t) => {
  const dirtyTranscript = [
    "cd /tmp/morpheus-alpha && codex --remote 'ws://127.0.0.1:8765' -C '/tmp/morpheus-alpha' resume 'codex-thread-1' 'G2: Hey, what is 27 minus 5?'",
    ">_ OpenAI Codex (v0.139.0)",
    "model: gpt-5.5 xhigh",
    "directory: ~/github/fabianbaier/morpheus",
    "permissions: YOLO mode",
    "› Hey, what is 27 minus 5?",
    "• 22",
    "› G2: Hey, what is 27 minus 5?",
    "• 22",
    "2222",
    "ERROR: remote app server at `ws://127.0.0.1:8765/` transport failed: WebSocket protocol error: Connection reset without closing handshake",
  ].join("\n");
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 20,
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 20,
    outputPollAttempts: 20,
    runner: fakeMorpheusRunner({
      mirrorOutputText: dirtyTranscript,
    }),
  });

  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    eventSourceFactory: () => null,
  });

  await client.connect();
  await client.activateSelected();
  const submitted = await client.submitTranscriptViaSessionPolling("Hey, what is 27 minus 5?", {
    waitFor: /\b22\b/,
    timeoutMs: 2000,
    intervalMs: 20,
  });

  assert.equal(submitted.mode, "session");
  assert.match(submitted.glassesText, /\b22\b/);
  assert.doesNotMatch(submitted.glassesText, /codex --remote|codex-thread-1|Connection reset|2222/);

  const messages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const messagesBody = await messages.json();
  const result = messagesBody.messages.find((message) => message.type === "result");
  assert.equal(result?.text, "22");
});

test("mirrored terminal output timeout is not streamed as the session answer", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 20,
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 20,
    outputPollAttempts: 20,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "terminal mirror answer after timeout",
      outputFailuresBeforeSuccess: 1,
      outputErrorMessage: "morpheus timed out after 10000ms",
    }),
  });

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const streamed = readStreamUntil(events.body, "terminal mirror answer after timeout", 1500);

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "one plus one after output timeout",
      clientRequestId: "codex-mirror-output-timeout-stream",
    },
  });
  assert.equal(prompt.status, 202);
  const eventText = await streamed;
  assert.match(eventText, /"type":"result"/);
  assert.doesNotMatch(eventText, /morpheus timed out after 10000ms/);

  const messages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const messagesBody = await messages.json();
  assert.equal(
    messagesBody.messages.find((message) => message.type === "result")?.text,
    "terminal mirror answer after timeout",
  );
  assert.equal(
    messagesBody.messages.some((message) => String(message.message || message.text || "").includes("timed out")),
    false,
  );
});

test("codex history polling streams final answer when live notification is missed", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      history: {
        "codex-thread-1": [
          { role: "user", text: "history fallback answer" },
          { role: "assistant", text: "answer from persisted codex history" },
        ],
      },
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    outputPollIntervalMs: 20,
    outputPollAttempts: 20,
  });

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const streamed = readStreamUntil(events.body, "answer from persisted codex history", 1500);

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "history fallback answer",
      clientRequestId: "codex-history-poll-stream",
    },
  });
  assert.equal(prompt.status, 202);
  assert.match(await streamed, /"type":"result"/);

  const messages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const messagesBody = await messages.json();
  assert.equal(
    messagesBody.messages.find((message) => message.type === "result")?.text,
    "answer from persisted codex history",
  );
});

test("same mirrored answer can stream again after a follow-up prompt", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 20,
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 20,
    outputPollAttempts: 20,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "2",
    }),
  });

  const first = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "what is one plus one",
      clientRequestId: "codex-repeat-answer-first",
    },
  });
  assert.equal(first.status, 202);
  await readMessagesUntil(
    baseUrl,
    "project-session:p_alpha",
    (messages) => messages.filter((message) => message.type === "result" && message.text === "2").length === 1,
  );

  const second = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "what is one plus one again",
      clientRequestId: "codex-repeat-answer-second",
    },
  });
  assert.equal(second.status, 202);
  const messagesBody = await readMessagesUntil(
    baseUrl,
    "project-session:p_alpha",
    (messages) => messages.filter((message) => message.type === "result" && message.text === "2").length === 2,
  );
  assert.equal(
    messagesBody.messages.filter((message) => message.type === "result" && message.text === "2").length,
    2,
  );
});

test("codex app-server streams results before slow terminal mirror finishes", async (t) => {
  let mirrorStarted = false;
  let mirrorDone = false;
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 25 }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: true,
    promptWaitForResultMs: 1000,
    runner: fakeMorpheusRunner({
      mirrorDelayMs: 250,
      onMirrorStart: () => {
        mirrorStarted = true;
      },
      onMirrorDone: () => {
        mirrorDone = true;
      },
    }),
  });

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);

  const streamed = readStreamUntil(events.body, "answer for: quick answer slow mirror", 1000);
  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "quick answer slow mirror",
      clientRequestId: "codex-slow-mirror-live-stream",
    },
  });
  assert.equal(prompt.status, 202);
  const body = await prompt.json();
  assert.equal(body.text, "answer for: quick answer slow mirror");
  assert.equal(body.result.mirrorPending, true);
  assert.equal(mirrorStarted, true);
  assert.equal(mirrorDone, false);
  assert.match(await streamed, /"type":"result"/);

  await sleep(300);
  assert.equal(mirrorDone, true);
});

test("stale project row event stream does not replay transcript after leaving session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "do not replay this after back",
      clientRequestId: "codex-stale-event-prompt",
    },
  });
  await request(baseUrl, "/api/sessions/project:__projects__/history");

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const initial = await readStreamFor(events.body, 100);
  assert.doesNotMatch(initial, /do not replay this after back/);
  assert.doesNotMatch(initial, /"type":"result"/);
});

test("project row event stream opened before prompt stops after back navigation", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 80 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: true,
  });

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const prompt = request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "late result must not paint stale view",
      clientRequestId: "codex-project-stream-late-back",
    },
  });

  let enteredSession = false;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const navigation = await request(baseUrl, "/api/navigation");
    if ((await navigation.json()).view === "session") {
      enteredSession = true;
      break;
    }
    await sleep(5);
  }
  assert.equal(enteredSession, true);

  const back = await request(baseUrl, "/api/back", {
    body: { clientRequestId: "codex-project-stream-late-back-nav" },
  });
  assert.equal((await back.json()).to, "projects");

  const promptRes = await prompt;
  assert.equal(promptRes.status, 202);
  assert.equal((await promptRes.json()).text, "answer for: late result must not paint stale view");

  const streamed = await readStreamFor(events.body, 120);
  assert.doesNotMatch(streamed, /late result must not paint stale view/);
  assert.doesNotMatch(streamed, /"type":"result"/);

  await request(baseUrl, "/api/sessions/project:p_alpha/history");
  const history = await request(baseUrl, "/api/sessions/project-session:p_alpha/history");
  const historyBody = await history.json();
  assert.equal(historyBody.history.at(-1).text, "answer for: late result must not paint stale view");
});

test("default event stream opened before prompt stops after back navigation", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 80 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: true,
  });

  const events = await fetch(`${baseUrl}/api/events?token=${TOKEN}`);
  assert.equal(events.status, 200);
  const prompt = request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "default stream must not paint stale view",
      clientRequestId: "codex-default-stream-late-back",
    },
  });

  let enteredSession = false;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const navigation = await request(baseUrl, "/api/navigation");
    if ((await navigation.json()).view === "session") {
      enteredSession = true;
      break;
    }
    await sleep(5);
  }
  assert.equal(enteredSession, true);

  const back = await request(baseUrl, "/api/back", {
    body: { clientRequestId: "codex-default-stream-late-back-nav" },
  });
  assert.equal((await back.json()).to, "projects");

  const promptRes = await prompt;
  assert.equal(promptRes.status, 202);

  const streamed = await readStreamFor(events.body, 120);
  assert.doesNotMatch(streamed, /default stream must not paint stale view/);
  assert.doesNotMatch(streamed, /"type":"result"/);
});

test("default event stream suppresses late results from a previously selected session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      asyncResultMs: (text) => (text.includes("alpha slow") ? 350 : 20),
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const events = await fetch(`${baseUrl}/api/events?token=${TOKEN}`);
  assert.equal(events.status, 200);
  const alphaPrompt = request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "alpha slow result",
      clientRequestId: "codex-default-stream-alpha-slow",
    },
  });

  let enteredAlpha = false;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const navigation = await request(baseUrl, "/api/navigation");
    const body = await navigation.json();
    if (body.view === "session" && body.selectedProject?.id === "p_alpha") {
      enteredAlpha = true;
      break;
    }
    await sleep(5);
  }
  assert.equal(enteredAlpha, true);

  const betaPrompt = request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_beta",
      text: "beta fast result",
      clientRequestId: "codex-default-stream-beta-fast",
    },
  });
  assert.equal((await betaPrompt).status, 202);
  assert.equal((await alphaPrompt).status, 202);

  const streamed = await readStreamFor(events.body, 220);
  assert.match(streamed, /beta fast result/);
  assert.doesNotMatch(streamed, /answer for: alpha slow result/);
});

test("default event stream does not replay transcript after leaving session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "do not replay through default stream",
      clientRequestId: "codex-default-event-prompt",
    },
  });
  await request(baseUrl, "/api/back", {
    body: { clientRequestId: "codex-default-event-back" },
  });

  const events = await fetch(`${baseUrl}/api/events?token=${TOKEN}`);
  assert.equal(events.status, 200);
  const initial = await readStreamFor(events.body, 100);
  assert.doesNotMatch(initial, /do not replay through default stream/);
  assert.doesNotMatch(initial, /"type":"result"/);
});

test("selecting an active project-session row reopens its real codex session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "first selected project row turn",
      clientRequestId: "codex-select-project-row-first",
    },
  });
  await request(baseUrl, "/api/sessions/project:__projects__/history");

  const select = await request(baseUrl, "/api/select-session", {
    body: {
      sessionId: "project-session:p_alpha",
      clientRequestId: "codex-select-project-row-reopen",
    },
  });
  assert.equal(select.status, 200);
  const selectBody = await select.json();
  assert.equal(selectBody.activeSessionId, "codex-thread-1");
  assert.equal(selectBody.selectedSession.activeSessionId, "codex-thread-1");

  const follow = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project-session:p_alpha",
      text: "same selected project row session",
      clientRequestId: "codex-select-project-row-follow",
    },
  });
  const followBody = await follow.json();
  assert.equal(followBody.action, "send_prompt");
  assert.equal(followBody.sessionId, "codex-thread-1");
  assert.equal(followBody.text, "answer for: same selected project row session");
});

test("selecting a project row after an active session enters project sessions without opening transcript", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "first project navigation turn",
      clientRequestId: "codex-select-project-row-nav-first",
    },
  });
  await request(baseUrl, "/api/sessions/project:__projects__/history");

  const select = await request(baseUrl, "/api/select-session", {
    body: {
      sessionId: "project:p_alpha",
      clientRequestId: "codex-select-project-row-nav",
    },
  });
  assert.equal(select.status, 200);
  const selectBody = await select.json();
  assert.equal(selectBody.selectedProject.id, "p_alpha");
  assert.equal(selectBody.selectedSession, null);
  assert.equal(selectBody.navigation.view, "sessions");
  assert.equal(selectBody.activeSessionId, "codex-thread-1");

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.sessions[0].id, "project:__projects__");
  assert.equal(sessionsBody.sessions[1].id, "project-session:p_alpha");
  assert.equal(sessionsBody.sessions[1].latestOutput, "answer for: first project navigation turn");
});

test("codex app-server backend hides old codex history from project session list", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      seedSessions: [
        {
          id: "old-cached-thread",
          title: "Old cached Codex thread",
          timestamp: new Date(1_779_999_000_000).toISOString(),
          cwd: "/tmp/morpheus-alpha",
          status: "idle",
        },
      ],
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-history-select-project" },
  });

  const before = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const beforeBody = await before.json();
  assert.deepEqual(beforeBody.sessions.map((session) => session.id), ["project:__projects__", "abc123"]);
  assert.equal(beforeBody.sessions.some((session) => session.id === "old-cached-thread"), false);

  await request(baseUrl, "/api/prompt", {
    body: { text: "fresh g2 turn", clientRequestId: "codex-history-fresh" },
  });
  const after = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const afterBody = await after.json();
  assert.equal(afterBody.sessions.some((session) => session.id === "codex-thread-1"), false);
  assert.equal(afterBody.sessions[0].id, "project:__projects__");
  assert.equal(afterBody.sessions[1].id, "project-session:p_alpha");
  assert.equal(afterBody.sessions[1].activeSessionId, "codex-thread-1");
  assert.equal(afterBody.sessions.some((session) => session.id === "old-cached-thread"), false);
});

test("status polling does not undo back navigation from a codex session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-nav-select-project" },
  });
  await request(baseUrl, "/api/prompt", {
    body: { text: "first turn", clientRequestId: "codex-nav-first" },
  });

  const backOne = await request(baseUrl, "/api/back", {
    body: { clientRequestId: "codex-nav-back-one" },
  });
  assert.equal((await backOne.json()).to, "projects");

  const status = await request(baseUrl, "/api/status?sessionId=codex-thread-1");
  assert.equal(status.status, 200);
  assert.equal((await status.json()).selectedSession, null);

  const staleHistory = await request(baseUrl, "/api/sessions/project-session:p_alpha/history");
  assert.equal(staleHistory.status, 200);
  const staleHistoryBody = await staleHistory.json();
  assert.deepEqual(staleHistoryBody.history, []);
  assert.equal(staleHistoryBody.navigation.view, "projects");
  assert.equal(staleHistoryBody.selectedProject, null);
  assert.equal(staleHistoryBody.selectedSession, null);

  const navigation = await request(baseUrl, "/api/navigation");
  assert.equal((await navigation.json()).view, "projects");
});

test("delayed prompt completion after back does not reselect the session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 80 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: true,
  });

  const prompt = request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "finish after I leave",
      clientRequestId: "codex-finish-after-back",
    },
  });
  let enteredSession = false;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const navigation = await request(baseUrl, "/api/navigation");
    if ((await navigation.json()).view === "session") {
      enteredSession = true;
      break;
    }
    await sleep(5);
  }
  assert.equal(enteredSession, true);

  const back = await request(baseUrl, "/api/back", {
    body: { clientRequestId: "codex-finish-after-back-nav" },
  });
  assert.equal((await back.json()).to, "projects");

  const promptRes = await prompt;
  assert.equal(promptRes.status, 202);
  assert.equal((await promptRes.json()).text, "answer for: finish after I leave");

  const navigation = await request(baseUrl, "/api/navigation");
  const navigationBody = await navigation.json();
  assert.equal(navigationBody.view, "projects");
  assert.equal(navigationBody.selectedSession, null);

  const activeHistory = await request(baseUrl, "/api/sessions/project:p_alpha/history");
  const activeHistoryBody = await activeHistory.json();
  assert.equal(activeHistoryBody.navigation.view, "sessions");
  assert.equal(activeHistoryBody.selectedSession, null);
  assert.equal(activeHistoryBody.projectActiveSessionId, "project-session:p_alpha");
});

test("stale active history from another project does not replace current session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "alpha answer",
      clientRequestId: "codex-stale-alpha",
    },
  });
  await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_beta",
      text: "beta answer",
      clientRequestId: "codex-stale-beta",
    },
  });

  const stale = await request(baseUrl, "/api/sessions/project-session:p_alpha/history");
  assert.equal(stale.status, 200);
  const staleBody = await stale.json();
  assert.deepEqual(staleBody.history, []);
  assert.equal(staleBody.navigation.action, "stale_history_ignored");
  assert.equal(staleBody.selectedProject.id, "p_beta");
  assert.equal(staleBody.selectedSession.id, "codex-thread-2");

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(sessionsBody.selectedProject.id, "p_beta");
  assert.equal(sessionsBody.selectedSession.id, "project-session:p_beta");
  assert.equal(sessionsBody.selectedSession.activeSessionId, "codex-thread-2");
  assert.equal(sessionsBody.sessions[1].id, "project-session:p_beta");
});

test("rejects over-limit prompt text", async (t) => {
  const { baseUrl } = await withBridge(t, { maxPromptChars: 8 });
  await request(baseUrl, "/api/select-session", {
    body: { sessionId: "abc123", clientRequestId: "select-0003" },
  });
  const res = await request(baseUrl, "/api/prompt", {
    body: { text: "this is too long", clientRequestId: "prompt-0003" },
  });
  assert.equal(res.status, 413);
  assert.equal((await res.json()).code, "text_too_long");
});

test("keeps approvals blocked", async (t) => {
  const { baseUrl } = await withBridge(t);
  const res = await request(baseUrl, "/api/permission-response", {
    body: { decision: "approve", clientRequestId: "approve-0001" },
  });
  assert.equal(res.status, 403);
  assert.equal((await res.json()).code, "action_blocked");
});

test("simulator client sees delayed result from prompt response without EventSource", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 80 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    eventSourceFactory: () => null,
  });

  await client.connect();
  await client.activateSelected();
  const submitted = await client.submitTranscript("what is 2 plus 2 plus 2");

  assert.equal(submitted.mode, "session");
  assert.equal(submitted.status, "idle");
  assert.match(submitted.glassesText, /answer for: what is 2 plus 2 plus 2/);
});

test("simulator stock polling catches glasses updates through sessions endpoint", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      asyncResultMs: 20,
      promptReturnDelayMs: 60,
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  const calls = [];
  const fetchImpl = async (url, options) => {
    const parsed = new URL(url);
    calls.push(`${parsed.pathname}${parsed.search}`);
    return fetch(url, options);
  };
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    fetchImpl,
    eventSourceFactory: () => null,
  });

  await client.connect();
  await client.activateSelected();
  const submitted = await client.submitTranscriptViaSessionPolling("what is 4 plus 5", {
    waitFor: /answer for: what is 4 plus 5/,
    timeoutMs: 1000,
    intervalMs: 20,
  });

  assert.equal(submitted.mode, "session");
  assert.equal(submitted.status, "idle");
  assert.equal(submitted.selectedSession.id, "project-session:p_alpha");
  assert.equal(submitted.activeSessionId, "codex-thread-1");
  assert.match(submitted.glassesText, /answer for: what is 4 plus 5/);
  assert.equal(calls.some((path) => path.startsWith("/api/messages")), false);
  assert.ok(calls.filter((path) => path.startsWith("/api/sessions")).length >= 3);
});

test("simulator polling hydrates terminal mirror output when codex final event is missed", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 20,
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 5000,
    outputPollAttempts: 1,
    runner: fakeMorpheusRunner({
      mirrorDelayMs: 1200,
      mirrorOutputText: "terminal-only answer from resumed Codex",
    }),
  });
  const calls = [];
  const fetchImpl = async (url, options) => {
    const parsed = new URL(url);
    calls.push(`${parsed.pathname}${parsed.search}`);
    return fetch(url, options);
  };
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    fetchImpl,
    eventSourceFactory: () => null,
  });

  await client.connect();
  await client.activateSelected();
  const submitted = await client.submitTranscriptViaSessionPolling("answer from visible terminal only", {
    waitFor: /terminal-only answer from resumed Codex/,
    timeoutMs: 3000,
    intervalMs: 50,
  });

  assert.equal(submitted.mode, "session");
  assert.equal(submitted.status, "idle");
  assert.equal(submitted.selectedSession.id, "project-session:p_alpha");
  assert.equal(submitted.activeSessionId, "codex-thread-1");
  assert.match(submitted.glassesText, /terminal-only answer from resumed Codex/);
  assert.equal(calls.some((path) => path.startsWith("/api/messages")), false);
  assert.equal(calls.some((path) => path.startsWith("/api/events")), false);
});

test("simulator polling shows mirror output even while morpheus tab remains working", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 20,
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 5000,
    outputPollAttempts: 8,
    runner: fakeMorpheusRunner({
      mirrorDelayMs: 1200,
      mirrorOutputText: "terminal answer while the tab is still marked working",
      outputState: "working",
      snapshotDelayMs: 1200,
    }),
  });
  const sessionsDurations = [];
  const fetchImpl = async (url, options) => {
    const parsed = new URL(url);
    const started = Date.now();
    const response = await fetch(url, options);
    if (parsed.pathname === "/api/sessions" && !parsed.searchParams.has("view")) {
      sessionsDurations.push(Date.now() - started);
    }
    return response;
  };
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    fetchImpl,
    eventSourceFactory: () => null,
  });

  await client.connect();
  await client.activateSelected();
  sessionsDurations.length = 0;
  const submitted = await client.submitTranscriptViaSessionPolling("working terminal output must show", {
    waitFor: /terminal answer while the tab is still marked working/,
    timeoutMs: 4000,
    intervalMs: 50,
  });

  assert.equal(submitted.mode, "session");
  assert.match(submitted.glassesText, /terminal answer while the tab is still marked working/);
  assert.deepEqual(
    sessionsDurations.filter((duration) => duration > 700),
    [],
    `/api/sessions should not wait on full Morpheus snapshots in session view: ${sessionsDurations.join(", ")}`,
  );
});

test("sessions poll includes active session result for glasses session view", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 40 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  await request(baseUrl, "/api/sessions/project:p_alpha/history");
  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "what does 3 + 2",
      clientRequestId: "codex-sessions-poll-result",
    },
  });
  assert.equal(prompt.status, 202);

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const body = await sessions.json();

  assert.equal(body.view, "session");
  assert.equal(body.mode, "session");
  assert.equal(body.state, "idle");
  assert.equal(body.selectedSession.id, "project-session:p_alpha");
  assert.equal(body.selectedSession.activeSessionId, "codex-thread-1");
  assert.equal(body.selectedSession.status, "idle");
  assert.equal(body.selectedSession.codex.status, "idle");
  assert.equal(body.selectedSession.latestOutput, "answer for: what does 3 + 2");
  assert.equal(body.text, "answer for: what does 3 + 2");
  assert.equal(body.output.text, "answer for: what does 3 + 2");
  assert.equal(body.history.at(-1).text, "answer for: what does 3 + 2");
  assert.equal(
    body.messages.find((message) => message.type === "result")?.text,
    "answer for: what does 3 + 2",
  );
});

test("project row polling exposes active codex result without re-entering session", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      asyncResultMs: 10,
      promptReturnDelayMs: 60,
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  await request(baseUrl, "/api/sessions/project:p_alpha/history");
  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "project row answer race",
      clientRequestId: "codex-project-row-answer-race",
    },
  });
  assert.equal(prompt.status, 202);

  const projectMessages = await request(baseUrl, "/api/messages?sessionId=project:p_alpha");
  const projectMessagesBody = await projectMessages.json();
  assert.equal(projectMessagesBody.state, "idle");
  assert.equal(projectMessagesBody.activeSessionId, "codex-thread-1");
  assert.equal(
    projectMessagesBody.messages.find((message) => message.type === "result")?.text,
    "answer for: project row answer race",
  );

  const activeMessages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const activeMessagesBody = await activeMessages.json();
  assert.equal(
    activeMessagesBody.messages.find((message) => message.type === "result")?.text,
    "answer for: project row answer race",
  );
});

test("simulator client can create a Morpheus project session and read the stream", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 20 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  const logs = [];
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    eventSourceFactory: () => null,
    onLog: (line) => logs.push(line),
  });

  const connected = await client.connect();
  assert.equal(connected.mode, "projects");
  assert.match(connected.glassesText, /alpha/);

  const sessions = await client.activateSelected();
  assert.equal(sessions.mode, "sessions");
  assert.equal(sessions.selectedProject.id, "p_alpha");

  const submitted = await client.submitTranscript("local simulator smoke");
  assert.equal(submitted.mode, "session");
  assert.equal(submitted.activeSessionId, "codex-thread-1");
  assert.equal(submitted.displaySessionId, "project-session:p_alpha");

  const streamed = await client.waitForText(/answer for: local simulator smoke/, {
    timeoutMs: 2000,
    intervalMs: 50,
  });
  assert.match(streamed.glassesText, /answer for: local simulator smoke/);
  assert.ok(logs.some((line) => line.includes("EventSource unavailable")));
});
