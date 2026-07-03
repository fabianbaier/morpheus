import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createBridge, createMorpheusProvider, runJsonCommand, startBridge } from "../src/server.mjs";
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
  deltaTexts = [],
  promptFailuresBeforeSuccess = 0,
  promptFailureMessage = "codex app-server failed to start (see [codex] logs above)",
  promptCalls = { count: 0 },
} = {}) {
  const sessions = [...seedSessions];
  let nextId = 1;
  let promptFailures = 0;

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
      promptCalls.count += 1;
      if (promptFailures < promptFailuresBeforeSuccess) {
        promptFailures += 1;
        throw new Error(promptFailureMessage);
      }
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
      for (const delta of deltaTexts) {
        emit(id, { type: "text_delta", text: delta, provider: "codex", sessionId: id });
      }
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
  snapshotSessions = [],
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
  const resolveMirrorOutputText =
    typeof mirrorOutputText === "function" ? mirrorOutputText : () => mirrorOutputText;
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
        sessions: snapshotSessions,
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
      const known =
        ref === mirroredTabRef || snapshotSessions.some((row) => row.tab_ref === ref);
      if (!known) throw new Error(`unexpected output target: ${ref}`);
      onOutputStart(ref);
      if (outputDelayMs > 0) await sleep(outputDelayMs);
      outputCalls += 1;
      if (outputCalls <= outputFailuresBeforeSuccess) {
        throw new Error(outputErrorMessage);
      }
      const outputText = String(resolveMirrorOutputText() || "");
      return {
        ok: true,
        session: {
          tab_ref: ref,
          mission_ref: "mirror-mission",
          state: outputState || (outputText ? "idle" : "working"),
          goal: "G2: mirrored Codex session",
        },
        output: {
          text: outputText,
          lines: outputText ? [outputText] : [],
          line_count: outputText ? 1 : 0,
          char_count: outputText.length,
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

test("opening a project row history returns project session rows and a session overview", async (t) => {
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
  // Stock Even clients render this history as the opened view, so the glasses
  // show the project's sessions instead of a blank "Waiting input" screen.
  assert.equal(body.history.length, 1);
  assert.equal(body.history[0].role, "assistant");
  assert.match(body.history[0].text, /Project alpha — 1 session/);
  assert.match(body.history[0].text, /1\. G2: Test Morpheus session \[idle\]/);
  assert.match(body.history[0].text, /resume|start a new session/i);
  assert.doesNotMatch(body.history[0].text, /Back to projects/);
});

test("opening an empty project row history invites starting a new session", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });

  const projectHistory = await request(baseUrl, "/api/sessions/project:p_beta/history");
  assert.equal(projectHistory.status, 200);
  const body = await projectHistory.json();
  assert.equal(body.mode, "sessions");
  assert.equal(body.history.length, 1);
  assert.match(body.history[0].text, /Project beta has no sessions yet/);
  assert.match(body.history[0].text, /start a new session/i);
});

test("projects menu history lists the project overview", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "projects-overview-select" },
  });

  const menuHistory = await request(baseUrl, "/api/sessions/project:__projects__/history");
  assert.equal(menuHistory.status, 200);
  const body = await menuHistory.json();
  assert.equal(body.navigation.view, "projects");
  assert.equal(body.history.length, 1);
  assert.equal(body.history[0].role, "assistant");
  assert.match(body.history[0].text, /Morpheus projects — 2/);
  assert.match(body.history[0].text, /1\. alpha \[1 live\]/);
  assert.match(body.history[0].text, /2\. beta/);
  assert.match(body.history[0].text, /Speaking here starts a new session in alpha/);
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
  assert.match(historyBody.history[0].text, /Project alpha — 1 session/);

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

test("prompting the projects menu row spawns in the remembered project", async (t) => {
  const { baseUrl } = await withBridge(t, { showProjectsFirst: true });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_beta", clientRequestId: "menu-prompt-select" },
  });
  // Stock Even clients can open the "Back to projects" row (which resets the
  // bridge to the projects view) and speak into that view.
  await request(baseUrl, "/api/sessions/project:__projects__/history");

  const res = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:__projects__",
      text: "what is 3 minus 4",
      clientRequestId: "menu-prompt-0001",
    },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.action, "spawn_session");
  assert.equal(body.sessionId, "g2spawn");
  assert.equal(body.selectedProject.id, "p_beta");
  assert.equal(body.result.session.prompt, "what is 3 minus 4");
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
  assert.match(historyBody.history[0].text, /Morpheus projects — 2/);
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
  assert.match(historyBody.history[0].text, /Morpheus projects — 2/);
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
  assert.match(historyBody.history[0].text, /Morpheus projects — 1/);
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

test("codex prompt retries while the app-server is still cold-starting", async (t) => {
  const promptCalls = { count: 0 };
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      promptFailuresBeforeSuccess: 1,
      promptCalls,
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    codexAppServerStartupWaitMs: 10_000,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-coldstart-select" },
  });

  const res = await request(baseUrl, "/api/prompt", {
    body: { text: "first prompt during cold start", clientRequestId: "codex-coldstart-prompt" },
  });
  assert.equal(res.status, 202);
  const body = await res.json();
  assert.equal(body.action, "spawn_session");
  assert.equal(body.sessionId, "codex-thread-1");
  assert.match(body.text, /answer for: first prompt during cold start/);
  assert.equal(promptCalls.count, 2);
});

test("codex prompt does not retry non-startup failures", async (t) => {
  const promptCalls = { count: 0 };
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      promptFailuresBeforeSuccess: 1,
      promptFailureMessage: "model refused the request",
      promptCalls,
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    codexAppServerStartupWaitMs: 10_000,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-norate-select" },
  });

  const res = await request(baseUrl, "/api/prompt", {
    body: { text: "fail fast please", clientRequestId: "codex-norate-prompt" },
  });
  assert.equal(res.status, 500);
  const body = await res.json();
  assert.equal(body.code, "spawn_failed");
  assert.match(body.error, /model refused the request/);
  assert.equal(promptCalls.count, 1);
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
  assert.match(projectHistoryBody.history[0].text, /Project alpha — 2 sessions/);
  assert.match(projectHistoryBody.history[0].text, /busy project should not auto-open \[busy\]/);
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

test("project-scoped messages stay live when selected session state is cleared", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 20 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "message survives project overview",
      clientRequestId: "codex-project-message-live",
    },
  });
  assert.equal(prompt.status, 202);

  const projectOverview = await request(baseUrl, "/api/select-session", {
    body: {
      sessionId: "project:p_alpha",
      clientRequestId: "codex-project-message-live-overview",
    },
  });
  assert.equal(projectOverview.status, 200);
  const overviewBody = await projectOverview.json();
  assert.equal(overviewBody.selectedSession, null);
  assert.equal(overviewBody.selectedProject.id, "p_alpha");

  const messages = await request(baseUrl, "/api/messages?sessionId=project:p_alpha");
  assert.equal(messages.status, 200);
  const body = await messages.json();
  assert.equal(body.activeSessionId, "codex-thread-1");
  assert.match(
    body.messages.map((message) => message.text || "").join("\n"),
    /answer for: message survives project overview/,
  );
});

test("project event stream replays buffered answer despite stale last event id", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 20 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "replay stale event id",
      clientRequestId: "codex-project-event-stale-id",
    },
  });
  assert.equal(prompt.status, 202);

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`, {
    headers: { "Last-Event-ID": "9999" },
  });
  assert.equal(events.status, 200);
  const streamed = await readStreamUntil(events.body, "answer for: replay stale event id", 1000);
  assert.match(streamed, /"type":"result"/);
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

test("client polling stays fast while background output poller owns missing codex history", async (t) => {
  let outputCalls = 0;
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
    outputPollAttempts: 45,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "terminal mirror answer is only for the background poller",
      mirrorDelayMs: 1200,
      outputDelayMs: 1200,
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
  let mirrorArgs = [];
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
      onSpawnCommand: (command, args) => {
        mirrorCommand = command;
        mirrorArgs = args;
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
  assert.match(mirrorCommand, /resume 'codex-thread-1'$/);
  assert.doesNotMatch(mirrorCommand, / #$/);
  assert.notEqual(mirrorArgs.indexOf("--prompt"), -1);
  assert.equal(mirrorArgs[mirrorArgs.indexOf("--prompt") + 1], "");
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
    staleMirrorGraceMs: 120,
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

test("follow-up prompt does not republish the previous answer while codex is working", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      asyncResultMs: (text) => (text.includes("second") ? 250 : 0),
      history: {
        "codex-thread-1": [
          { role: "user", text: "first question" },
          { role: "assistant", text: "answer for: first question" },
        ],
      },
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 20,
    outputPollAttempts: 30,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "answer for: first question",
    }),
  });

  const first = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "first question",
      clientRequestId: "no-stale-republish-first",
    },
  });
  assert.equal(first.status, 202);
  await readMessagesUntil(
    baseUrl,
    "project-session:p_alpha",
    (messages) =>
      messages.some((message) => message.type === "result" && message.text === "answer for: first question"),
  );

  const second = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "second question",
      clientRequestId: "no-stale-republish-second",
    },
  });
  assert.equal(second.status, 202);

  const deadline = Date.now() + 150;
  while (Date.now() < deadline) {
    const res = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
    const body = await res.json();
    const staleResults = body.messages.filter(
      (message) => message.type === "result" && message.text === "answer for: first question",
    );
    assert.equal(
      staleResults.length,
      1,
      "stale terminal/history text must not be republished as the follow-up answer",
    );
    assert.equal(body.state, "busy");
    await sleep(20);
  }

  const finalBody = await readMessagesUntil(
    baseUrl,
    "project-session:p_alpha",
    (messages) =>
      messages.some((message) => message.type === "result" && message.text === "answer for: second question"),
  );
  assert.equal(
    finalBody.messages.filter(
      (message) => message.type === "result" && message.text === "answer for: first question",
    ).length,
    1,
  );
});

test("terminal mirror updates stream to glasses after a follow-up prompt", async (t) => {
  let mirrorText = "answer for: first question";
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      history: {
        "codex-thread-1": [
          { role: "user", text: "first question" },
          { role: "assistant", text: "answer for: first question" },
        ],
      },
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 20,
    outputPollAttempts: 60,
    runner: fakeMorpheusRunner({
      mirrorOutputText: () => mirrorText,
    }),
  });

  const first = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "first question",
      clientRequestId: "mirror-follow-up-live-first",
    },
  });
  assert.equal(first.status, 202);
  await readMessagesUntil(
    baseUrl,
    "project-session:p_alpha",
    (messages) =>
      messages.some((message) => message.type === "result" && message.text === "answer for: first question"),
  );

  const second = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "what about now",
      clientRequestId: "mirror-follow-up-live-second",
    },
  });
  assert.equal(second.status, 202);
  await sleep(60);
  mirrorText = "fresh terminal output for the follow-up";

  const body = await readMessagesUntil(
    baseUrl,
    "project-session:p_alpha",
    (messages) =>
      messages.some(
        (message) => message.type === "result" && message.text === "fresh terminal output for the follow-up",
      ),
    2000,
  );
  assert.equal(
    body.messages.filter(
      (message) => message.type === "result" && message.text === "answer for: first question",
    ).length,
    1,
  );
});

test("client polling re-arms codex live events when the thread is no longer tracked", async (t) => {
  const resumedThreads = [];
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: (emit) => ({
      ...fakeCodexAgentProvider()(emit),
      getSubscribedSessions: () => [],
    }),
    codexClient: {
      threadResume: async ({ threadId }) => {
        resumedThreads.push(threadId);
        return {};
      },
    },
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: false,
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "keep events alive",
      clientRequestId: "live-events-rearm",
    },
  });
  assert.equal(prompt.status, 202);

  await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  assert.deepEqual(resumedThreads, ["codex-thread-1"]);
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

test("codex app-server backend lists old codex threads for resume by default", async (t) => {
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
      history: {
        "old-cached-thread": [
          { role: "user", text: "earlier question" },
          { role: "assistant", text: "earlier answer" },
        ],
      },
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "codex-resume-select-project" },
  });

  const sessions = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  const sessionsBody = await sessions.json();
  assert.equal(
    sessionsBody.sessions.some((session) => session.id === "old-cached-thread"),
    true,
  );

  const projectHistory = await request(baseUrl, "/api/sessions/project:p_alpha/history");
  const projectHistoryBody = await projectHistory.json();
  assert.match(projectHistoryBody.history[0].text, /Old cached Codex thread/);

  // Stock Even clients open a row by fetching its history: that must select
  // the thread so a follow-up prompt resumes it instead of spawning anew.
  const threadHistory = await request(baseUrl, "/api/sessions/old-cached-thread/history");
  assert.equal(threadHistory.status, 200);
  const threadHistoryBody = await threadHistory.json();
  assert.equal(threadHistoryBody.history.at(-1).text, "earlier answer");

  const navigation = await request(baseUrl, "/api/navigation");
  const navigationBody = await navigation.json();
  assert.equal(navigationBody.view, "session");
  assert.equal(navigationBody.selectedSession.id, "old-cached-thread");

  const prompt = await request(baseUrl, "/api/prompt", {
    body: { text: "continue this thread", clientRequestId: "codex-resume-followup" },
  });
  assert.equal(prompt.status, 202);
  const promptBody = await prompt.json();
  assert.equal(promptBody.action, "send_prompt");
  assert.equal(promptBody.sessionId, "old-cached-thread");
});

test("codex app-server backend hides old codex history when explicitly disabled", async (t) => {
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
    includeCodexHistory: false,
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

test("stock polling cursor survives mixed project and session buffer ids", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 30 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: true,
  });

  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "cursor-drift-select-0" },
  });
  const firstPrompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "cursor drift first turn",
      clientRequestId: "cursor-drift-prompt-1",
    },
  });
  assert.equal(firstPrompt.status, 202);

  // The stock client keeps one message cursor for the whole conversation while
  // it watches the project row, so its cursor follows project:p_alpha ids.
  const projectPoll = await request(baseUrl, "/api/messages?sessionId=project:p_alpha&after=0");
  const projectPollBody = await projectPoll.json();
  assert.equal(
    projectPollBody.messages.find((message) => message.type === "result")?.text,
    "answer for: cursor drift first turn",
  );

  // Re-opening the project a few times appends project-row-only events.
  for (let i = 1; i <= 3; i += 1) {
    await request(baseUrl, "/api/select-project", {
      body: { projectId: "p_alpha", clientRequestId: `cursor-drift-select-${i}` },
    });
  }
  const refreshedPoll = await request(baseUrl, "/api/messages?sessionId=project:p_alpha&after=0");
  const refreshedBody = await refreshedPoll.json();
  const cursor = refreshedBody.messages.reduce(
    (max, message) => Math.max(max, Number(message.id || 0)),
    0,
  );

  const secondPrompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "cursor drift second turn",
      clientRequestId: "cursor-drift-prompt-2",
    },
  });
  assert.equal(secondPrompt.status, 202);

  const fullSession = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha&after=0");
  const fullSessionBody = await fullSession.json();
  assert.equal(
    fullSessionBody.messages.find(
      (message) => message.type === "result" && message.text === "answer for: cursor drift second turn",
    )?.text,
    "answer for: cursor drift second turn",
  );

  // Polling the active session row with the cursor accumulated on the project
  // row must still deliver the new turn instead of silently skipping it.
  const cursorPoll = await request(
    baseUrl,
    `/api/messages?sessionId=project-session:p_alpha&after=${cursor}`,
  );
  const cursorPollBody = await cursorPoll.json();
  assert.equal(
    cursorPollBody.messages.find(
      (message) => message.type === "result" && message.text === "answer for: cursor drift second turn",
    )?.text,
    "answer for: cursor drift second turn",
  );
});

test("event stream for explicit codex session id stays live after back navigation", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 300, promptReturnDelayMs: 20 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: false,
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "answer after back",
      clientRequestId: "stream-after-back-0001",
    },
  });
  assert.equal(prompt.status, 202);
  const promptBody = await prompt.json();
  assert.equal(promptBody.activeSessionId, "codex-thread-1");

  const events = await fetch(`${baseUrl}/api/events?sessionId=codex-thread-1&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const streamed = readStreamUntil(events.body, "answer for: answer after back", 1500);

  const back = await request(baseUrl, "/api/back", {
    body: { clientRequestId: "stream-after-back-0002" },
  });
  assert.equal(back.status, 200);

  assert.match(await streamed, /"type":"result"/);
});

test("read polling has a separate rate budget from writes", async (t) => {
  const { baseUrl } = await withBridge(t, {
    rateLimitMax: 5,
    rateLimitWindowMs: 60_000,
  });

  for (let i = 0; i < 30; i += 1) {
    const res = await request(baseUrl, "/api/messages?sessionId=morpheus");
    assert.equal(res.status, 200, `read poll ${i + 1} should not be rate limited`);
  }

  let lastWriteStatus = 0;
  for (let i = 0; i < 6; i += 1) {
    const res = await request(baseUrl, "/api/back", {
      body: { clientRequestId: `rate-budget-back-${i}` },
    });
    lastWriteStatus = res.status;
  }
  assert.equal(lastWriteStatus, 429);
});

test("slow terminal mirror reads hand off to background instead of blocking client polls", async (t) => {
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
    clientPollOutputBudgetMs: 150,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "mirror answer after handoff",
      outputDelayMs: 700,
    }),
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "mirror handoff",
      clientRequestId: "mirror-handoff-0001",
    },
  });
  assert.equal(prompt.status, 202);

  const started = Date.now();
  const fast = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const elapsedMs = Date.now() - started;
  assert.equal(fast.status, 200);
  assert.ok(elapsedMs < 600, `/api/messages blocked for ${elapsedMs}ms on terminal output`);

  await sleep(900);
  const after = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const body = await after.json();
  assert.equal(
    body.messages.find((message) => message.type === "result")?.text,
    "mirror answer after handoff",
  );
});

test("identical stock prompt retries replay the in-flight response after selection changes", async (t) => {
  let promptCalls = 0;
  const baseProvider = fakeCodexAgentProvider({ asyncResultMs: 400, promptReturnDelayMs: 10 });
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: (emit, client) => {
      const provider = baseProvider(emit, client);
      const originalPrompt = provider.prompt.bind(provider);
      provider.prompt = async (...args) => {
        promptCalls += 1;
        return originalPrompt(...args);
      };
      return provider;
    },
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: true,
  });

  const body = { sessionId: "project:p_alpha", text: "same utterance twice" };
  const first = request(baseUrl, "/api/prompt", { body });

  const selectionDeadline = Date.now() + 2000;
  let selectionChanged = false;
  while (Date.now() < selectionDeadline) {
    const res = await request(baseUrl, "/api/selected-session");
    const selected = await res.json();
    if (selected.selectedSession) {
      selectionChanged = true;
      break;
    }
    await sleep(20);
  }
  assert.equal(selectionChanged, true);

  const second = await request(baseUrl, "/api/prompt", { body });
  const firstRes = await first;
  assert.equal(firstRes.status, 202);
  assert.equal(second.status, 202);
  const secondBody = await second.json();
  assert.equal(secondBody.duplicate, true);
  assert.equal(promptCalls, 1);
});

test("bridge restart remaps existing mirror tabs from morpheus snapshot rows", async (t) => {
  let mirrorSpawns = 0;
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 10,
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 20,
    outputPollAttempts: 20,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "mirror answer after restart",
      onMirrorStart: () => {
        mirrorSpawns += 1;
      },
      snapshotSessions: [
        {
          tab_ref: "old-mirror-tab",
          mission_ref: "old-mirror-mission",
          tenant_id: "p_alpha",
          project_root: "/tmp/morpheus-alpha",
          state: "working",
          goal: "G2: hello again",
          age_secs: 30,
          resume_ref: "codex-thread-1",
        },
      ],
    }),
  });

  // Listing project sessions ingests snapshot rows and re-attaches the mirror.
  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "remap-select-1" },
  });
  await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "hello again",
      clientRequestId: "remap-prompt-1",
    },
  });
  assert.equal(prompt.status, 202);
  assert.equal(mirrorSpawns, 0, "re-prompt must not spawn a duplicate mirror tab");

  const deadline = Date.now() + 1500;
  let resultText = "";
  while (Date.now() < deadline && !resultText) {
    const res = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
    const body = await res.json();
    resultText = body.messages.find((message) => message.type === "result")?.text || "";
    if (!resultText) await sleep(40);
  }
  assert.equal(resultText, "mirror answer after restart");
});

test("concurrent client polls share one terminal output read", async (t) => {
  let outputStarts = 0;
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      emitFinalResult: false,
      promptReturnDelayMs: 10,
    }),
    mirrorCodexTui: true,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 30000,
    outputPollAttempts: 0,
    clientPollOutputBudgetMs: 120,
    runner: fakeMorpheusRunner({
      mirrorOutputText: "single flight mirror answer",
      outputDelayMs: 400,
      onOutputStart: () => {
        outputStarts += 1;
      },
    }),
  });

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "single flight",
      clientRequestId: "single-flight-1",
    },
  });
  assert.equal(prompt.status, 202);

  await Promise.all([
    request(baseUrl, "/api/messages?sessionId=project-session:p_alpha"),
    request(baseUrl, "/api/messages?sessionId=project-session:p_alpha"),
    request(baseUrl, "/api/messages?sessionId=project-session:p_alpha"),
  ]);
  assert.equal(outputStarts, 1, "concurrent polls must share one in-flight terminal read");

  await sleep(600);
  const res = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const body = await res.json();
  assert.equal(
    body.messages.find((message) => message.type === "result")?.text,
    "single flight mirror answer",
  );
});

test("mid-turn history does not publish a truncated answer while deltas stream", async (t) => {
  const fullAnswer = "Doing fine. Ready to work in /tmp/morpheus-alpha whenever you are.";
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      asyncResultMs: 250,
      promptReturnDelayMs: 10,
      deltaTexts: ["Doing fine. ", "Ready to work in /tmp/morpheus-alpha whenever you are."],
      history: {
        // Codex persists partial assistant text while the turn still streams.
        "codex-thread-1": [
          { role: "user", text: "Hey, how are you doing?" },
          { role: "assistant", text: "Doing fine." },
        ],
      },
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: false,
    outputPollIntervalMs: 20,
    outputPollAttempts: 30,
  });

  // The fake provider always answers "answer for: <text>", so assert on that.
  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "Hey, how are you doing?",
      clientRequestId: "delta-hold-prompt-1",
    },
  });
  assert.equal(prompt.status, 202);

  // Poll while the turn is still streaming: the partial persisted history
  // must not surface as a result.
  const during = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const duringBody = await during.json();
  assert.equal(
    duringBody.messages.some(
      (message) => message.type === "result" && message.text === "Doing fine.",
    ),
    false,
    "partial mid-turn history must not be published as a result",
  );

  await sleep(400);
  const after = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const afterBody = await after.json();
  const results = afterBody.messages.filter((message) => message.type === "result");
  assert.equal(results.length, 1);
  assert.equal(results[0].text, "answer for: Hey, how are you doing?");
  assert.equal(fullAnswer.length > 0, true);
});

test("stream and poll messages carry the session id the client asked for", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 120, promptReturnDelayMs: 10 }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: false,
  });

  const events = await fetch(`${baseUrl}/api/events?sessionId=project:p_alpha&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const streamed = readStreamUntil(events.body, '"type":"result"', 1500);

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "present ids",
      clientRequestId: "present-ids-prompt-1",
    },
  });
  assert.equal(prompt.status, 202);

  const streamText = await streamed;
  const streamPayloads = streamText
    .split("\n")
    .filter((line) => line.startsWith("data: "))
    .map((line) => JSON.parse(line.slice(6)));
  assert.ok(streamPayloads.length > 0);
  for (const payload of streamPayloads) {
    if (!payload.sessionId) continue;
    assert.equal(
      payload.sessionId,
      "project:p_alpha",
      `stream message ${payload.type} must carry the subscribed session id`,
    );
  }
  const streamResult = streamPayloads.find((payload) => payload.type === "result");
  assert.equal(streamResult.activeSessionId, "codex-thread-1");

  await sleep(250);
  const polled = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const polledBody = await polled.json();
  const polledResult = polledBody.messages.find((message) => message.type === "result");
  assert.equal(polledResult.sessionId, "project-session:p_alpha");
  assert.equal(polledResult.activeSessionId, "codex-thread-1");
});

test("failed spawn publishes a failure result and returns the project row to idle", async (t) => {
  const { baseUrl, state } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider({
      promptFailuresBeforeSuccess: 1,
      promptFailureMessage: "model refused the request",
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  // A stale busy mark on the selected project row must also be cleared by the
  // spawn failure, exactly as the prompt-failure path clears its session.
  state.selectedSession = {
    id: "project:p_alpha",
    title: "alpha",
    provider: "codex",
    status: "busy",
  };

  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "this spawn will fail",
      clientRequestId: "spawn-failure-idle-prompt",
    },
  });
  assert.equal(prompt.status, 500);
  assert.equal((await prompt.json()).code, "spawn_failed");

  // Polls must not report the project row as busy forever after the failure.
  const messages = await request(baseUrl, "/api/messages?sessionId=project:p_alpha");
  const messagesBody = await messages.json();
  assert.equal(messagesBody.state, "idle");

  const buffered = state.sessions.get("project:p_alpha")?.messages || [];
  const failure = buffered.find((message) => message.type === "result" && message.success === false);
  assert.match(failure?.text || "", /model refused the request/);
  assert.equal(buffered.at(-1)?.type, "status");
  assert.equal(buffered.at(-1)?.state, "idle");

  // The internal busy marks are cleared too, so session polls report idle.
  assert.equal(state.selectedSession?.status, "idle", "spawn failure must mark the selected session idle");
  for (const projectSession of state.projectActiveSessions.values()) {
    assert.notEqual(projectSession?.status, "busy", "no project session may stay busy after a failed spawn");
  }
  const sessions = await request(baseUrl, "/api/sessions");
  assert.equal(sessions.status, 200);
  assert.equal((await sessions.json()).state, "idle");
});

test("failed follow-up prompt returns the session to idle with a failure message", async (t) => {
  let failNextPrompt = false;
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: (emit, client) => {
      const provider = fakeCodexAgentProvider()(emit, client);
      const originalPrompt = provider.prompt.bind(provider);
      provider.prompt = async (...args) => {
        if (failNextPrompt) {
          failNextPrompt = false;
          throw new Error("model refused the request");
        }
        return originalPrompt(...args);
      };
      return provider;
    },
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });

  const first = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "first working turn",
      clientRequestId: "prompt-failure-idle-first",
    },
  });
  assert.equal(first.status, 202);

  failNextPrompt = true;
  const second = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "second failing turn",
      clientRequestId: "prompt-failure-idle-second",
    },
  });
  assert.equal(second.status, 500);
  assert.equal((await second.json()).code, "prompt_failed");

  const messages = await request(baseUrl, "/api/messages?sessionId=codex-thread-1");
  const messagesBody = await messages.json();
  assert.equal(messagesBody.state, "idle");
  const failure = messagesBody.messages.find(
    (message) => message.type === "result" && message.success === false,
  );
  assert.match(failure?.text || "", /model refused the request/);
  const lastStatus = messagesBody.messages.filter((message) => message.type === "status").at(-1);
  assert.equal(lastStatus.state, "idle");

  // The failed turn must not leave a dangling stale-mirror hold: the project
  // row mirrors the same terminal messages.
  const projectMessages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const projectMessagesBody = await projectMessages.json();
  assert.equal(projectMessagesBody.state, "idle");
});

test("select-session project rows fail as JSON and use cached projects when the provider is down", async (t) => {
  let failProjects = false;
  const base = fakeMorpheusRunner();
  const runner = async (command, args, options) => {
    if (args[1] === "projects" && failProjects) throw new Error("project list down");
    return base(command, args, options);
  };
  const { baseUrl } = await withBridge(t, { runner, showProjectsFirst: true });

  // No cached project list yet: the failure must surface as JSON, not as
  // Express's default HTML 500 page.
  failProjects = true;
  const cold = await request(baseUrl, "/api/select-session", {
    body: {
      sessionId: "project-session:p_alpha",
      clientRequestId: "select-session-provider-down-cold",
    },
  });
  assert.equal(cold.status, 500);
  assert.match(cold.headers.get("content-type") || "", /application\/json/);
  assert.match((await cold.json()).error, /project list down/);

  // Prime the cache and remember an active session, then the same selection
  // succeeds from the cached project list while the provider stays down.
  failProjects = false;
  const projects = await request(baseUrl, `/api/sessions?token=${TOKEN}`, { token: null });
  assert.equal(projects.status, 200);
  const seed = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "seed active session",
      clientRequestId: "select-session-provider-down-seed",
    },
  });
  assert.equal(seed.status, 202);
  failProjects = true;
  const warm = await request(baseUrl, "/api/select-session", {
    body: {
      sessionId: "project-session:p_alpha",
      clientRequestId: "select-session-provider-down-warm",
    },
  });
  assert.equal(warm.status, 200);
  const warmBody = await warm.json();
  assert.equal(warmBody.ok, true);
  assert.equal(warmBody.selectedProject.id, "p_alpha");
  assert.equal(warmBody.activeSessionId, "mirror-tab");
});

test("prompt result wait ignores a stale buffered answer when the provider redirects the session id", async (t) => {
  const seeded = {
    id: "old-cached-thread",
    title: "Old cached Codex thread",
    timestamp: new Date(1_779_999_000_000).toISOString(),
    cwd: "/tmp/morpheus-alpha",
    status: "idle",
  };
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: (emit) => ({
      async getInfo() {
        return { provider: "codex", model: "Codex", version: "test" };
      },
      async listSessions(_limit, cwd) {
        return !cwd || seeded.cwd === cwd ? [seeded] : [];
      },
      getStatus() {
        return null;
      },
      async getSessionStatus() {
        return "idle";
      },
      async getHistory() {
        return [];
      },
      async prompt(sessionId, text) {
        const finish = () => {
          emit("codex-thread-stale", {
            type: "result",
            success: true,
            text: `answer for: ${text}`,
            provider: "codex",
            sessionId: "codex-thread-stale",
          });
          emit("codex-thread-stale", {
            type: "status",
            state: "idle",
            provider: "codex",
            sessionId: "codex-thread-stale",
          });
        };
        if (!sessionId) {
          // Turn one spawns the thread that later holds the stale answer.
          finish();
        } else {
          // Follow-ups on other threads resolve to the same redirected id,
          // whose buffer already holds the previous turn's answer.
          const timer = setTimeout(finish, 120);
          if (typeof timer.unref === "function") timer.unref();
        }
        return { sessionId: "codex-thread-stale", provider: "codex" };
      },
    }),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    waitForPromptResult: true,
    promptWaitForResultMs: 2000,
  });

  const first = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_beta",
      text: "first stale turn",
      clientRequestId: "redirect-stale-first",
    },
  });
  assert.equal(first.status, 202);
  assert.equal((await first.json()).text, "answer for: first stale turn");

  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "redirect-stale-select-project" },
  });
  // Opening the seeded thread row selects it as the outbound session.
  const open = await request(baseUrl, "/api/sessions/old-cached-thread/history");
  assert.equal(open.status, 200);

  const follow = await request(baseUrl, "/api/prompt", {
    body: {
      text: "redirected follow-up",
      clientRequestId: "redirect-stale-follow",
    },
  });
  assert.equal(follow.status, 202);
  const followBody = await follow.json();
  assert.equal(
    followBody.text,
    "answer for: redirected follow-up",
    "the previous turn's buffered answer must not resolve the new prompt",
  );
});

test("expired pre-auth rate limit buckets are swept from memory", async (t) => {
  const { baseUrl, state } = await withBridge(t);
  for (let i = 0; i < 50; i += 1) {
    state.rateLimits.set(`10.0.0.${i}:read`, { windowStart: 1, count: 3 });
  }

  const res = await request(baseUrl, "/api/info");
  assert.equal(res.status, 200);
  for (let i = 0; i < 50; i += 1) {
    assert.equal(state.rateLimits.has(`10.0.0.${i}:read`), false, "expired buckets must be swept");
  }
  assert.ok(state.rateLimits.size >= 1, "the live requester keeps its bucket");

  // Even unexpired buckets cannot grow past the hard cap.
  const now = Date.now();
  for (let i = 0; i < 4200; i += 1) {
    state.rateLimits.set(`10.1.${Math.floor(i / 250)}.${i % 250}:read`, {
      windowStart: now,
      count: 1,
    });
  }
  const capped = await request(baseUrl, "/api/info");
  assert.equal(capped.status, 200);
  assert.ok(
    state.rateLimits.size <= 4097,
    `rate limit map must stay capped, got ${state.rateLimits.size}`,
  );
});

test("evicts idle per-session state beyond the tracked session cap", async (t) => {
  const { baseUrl, state } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    maxTrackedSessions: 6,
  });
  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "keep this conversation",
      clientRequestId: "evict-keep-prompt",
    },
  });
  assert.equal(prompt.status, 202);

  // Clients can name arbitrary session ids; each opens (and closes) a stream.
  for (let i = 0; i < 12; i += 1) {
    const events = await fetch(`${baseUrl}/api/events?sessionId=junk-${i}&token=${TOKEN}`);
    assert.equal(events.status, 200);
    await readStreamFor(events.body, 10);
  }
  for (let attempt = 0; attempt < 20 && state.sessions.has("junk-0"); attempt += 1) {
    const extra = await fetch(`${baseUrl}/api/events?sessionId=junk-extra-${attempt}&token=${TOKEN}`);
    await readStreamFor(extra.body, 10);
    await sleep(20);
  }

  assert.equal(state.sessions.has("junk-0"), false, "idle junk session buffers must be evicted");
  assert.equal(state.sessions.has("codex-thread-1"), true);
  assert.equal(state.sessions.has("project-session:p_alpha"), true);
  assert.ok(
    state.sessions.size <= 8,
    `tracked sessions must stay near the cap, got ${state.sessions.size}`,
  );

  // The surviving conversation still serves its transcript.
  const messages = await request(baseUrl, "/api/messages?sessionId=project-session:p_alpha");
  const messagesBody = await messages.json();
  assert.equal(
    messagesBody.messages.find((message) => message.type === "result")?.text,
    "answer for: keep this conversation",
  );
});

test("runner semaphore hands released slots to queued waiters without over-admitting", async () => {
  let running = 0;
  let peak = 0;
  const gates = [];
  const provider = createMorpheusProvider({
    runnerConcurrency: 1,
    runner: () =>
      new Promise((resolve) => {
        running += 1;
        peak = Math.max(peak, running);
        gates.push(() => {
          running -= 1;
          resolve({ projects: [] });
        });
      }),
  });

  const first = provider.listProjects(1);
  const second = provider.listProjects(1);
  await sleep(0);
  assert.equal(gates.length, 1, "only one runner may start under concurrency 1");

  // Release the held slot and race a brand-new call against the queued waiter.
  gates.shift()();
  const third = Promise.resolve().then(() => provider.listProjects(1));
  await sleep(10);
  assert.equal(peak, 1, "a fresh call must not run concurrently with the woken waiter");

  for (let i = 0; i < 10 && gates.length; i += 1) {
    gates.shift()();
    await sleep(0);
  }
  await Promise.all([first, second, third]);
  assert.equal(peak, 1);
});

test("non-numeric limit query params fall back to defaults", async (t) => {
  const { baseUrl } = await withBridge(t);
  const sessions = await request(baseUrl, "/api/sessions?limit=abc");
  assert.equal(sessions.status, 200);
  assert.equal((await sessions.json()).sessions[0]?.id, "abc123");

  const projects = await request(baseUrl, "/api/projects?limit=abc");
  assert.equal(projects.status, 200);
  assert.equal((await projects.json()).projects.length, 2);

  const historyLimits = [];
  const codex = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: (emit, client) => {
      const provider = fakeCodexAgentProvider({
        history: {
          "codex-thread-1": [
            { role: "user", text: "limits" },
            { role: "assistant", text: "bounded answer" },
          ],
        },
      })(emit, client);
      const originalGetHistory = provider.getHistory.bind(provider);
      provider.getHistory = async (sessionId, limit) => {
        historyLimits.push(limit);
        return originalGetHistory(sessionId, limit);
      };
      return provider;
    },
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  const history = await request(codex.baseUrl, "/api/sessions/codex-thread-1/history?limit=abc");
  assert.equal(history.status, 200);
  assert.ok(historyLimits.length >= 1);
  for (const limit of historyLimits) {
    assert.equal(
      Number.isFinite(limit) && limit >= 1,
      true,
      `getHistory limit must be a bounded number, got ${limit}`,
    );
  }
});

test("duplicate in-flight selection requests replay instead of re-resolving", async (t) => {
  let projectsCalls = 0;
  let snapshotCalls = 0;
  const base = fakeMorpheusRunner({
    snapshotDelayMs: 40,
    snapshotSessions: [
      {
        tab_ref: "abc123",
        mission_ref: "missionalpha",
        tenant_id: "p_alpha",
        project_root: "/tmp/morpheus-alpha",
        state: "idle",
        goal: "G2: Test Morpheus session",
        age_secs: 4,
      },
    ],
  });
  const runner = async (command, args, options) => {
    if (args[1] === "projects") {
      projectsCalls += 1;
      await sleep(40);
    }
    if (args[1] === "snapshot") snapshotCalls += 1;
    return base(command, args, options);
  };
  const { baseUrl } = await withBridge(t, { runner });

  const [projectOne, projectTwo] = await Promise.all([
    request(baseUrl, "/api/select-project", {
      body: { projectId: "p_alpha", clientRequestId: "dup-select-project" },
    }),
    request(baseUrl, "/api/select-project", {
      body: { projectId: "p_alpha", clientRequestId: "dup-select-project" },
    }),
  ]);
  assert.equal(projectOne.status, 200);
  assert.equal(projectTwo.status, 200);
  const projectBodies = [await projectOne.json(), await projectTwo.json()];
  assert.equal(projectBodies.some((body) => body.duplicate === true), true);
  assert.equal(projectsCalls, 1, "duplicate select-project must not resolve twice");

  const [sessionOne, sessionTwo] = await Promise.all([
    request(baseUrl, "/api/select-session", {
      body: { sessionId: "abc123", clientRequestId: "dup-select-session" },
    }),
    request(baseUrl, "/api/select-session", {
      body: { sessionId: "abc123", clientRequestId: "dup-select-session" },
    }),
  ]);
  assert.equal(sessionOne.status, 200);
  assert.equal(sessionTwo.status, 200);
  const sessionBodies = [await sessionOne.json(), await sessionTwo.json()];
  assert.equal(sessionBodies.some((body) => body.duplicate === true), true);
  assert.equal(snapshotCalls, 1, "duplicate select-session must not resolve twice");
});

test("runJsonCommand reports oversized output and escalates to SIGKILL", async () => {
  // The child ignores SIGTERM and keeps spewing output, so only the SIGKILL
  // escalation can end it before the (much longer) timeout.
  const script = [
    'process.on("SIGTERM", () => {});',
    'setInterval(() => process.stdout.write("x".repeat(65536)), 5);',
  ].join("\n");
  const started = Date.now();
  await assert.rejects(
    runJsonCommand(process.execPath, ["-e", script], {
      timeoutMs: 8000,
      outputLimitBytes: 1024,
    }),
    /output exceeded 1024 bytes/,
  );
  assert.ok(
    Date.now() - started < 4000,
    "oversized output must be killed well before the runner timeout",
  );
});

test("runJsonCommand surfaces structured CLI failures printed to stdout", async () => {
  // The real CLI prints {"ok":false,"error":"..."} to STDOUT and exits 1 with
  // an empty stderr; the runner must relay that message, flagged as a CLI
  // rejection, instead of a generic exit-code error.
  const cliShape =
    'process.stdout.write(JSON.stringify({ ok: false, error: "unknown feed item: 7" }) + "\\n"); process.exit(1);';
  await assert.rejects(
    runJsonCommand(process.execPath, ["-e", cliShape], {
      timeoutMs: 8000,
      outputLimitBytes: 65536,
    }),
    (err) => {
      assert.equal(err.message, "unknown feed item: 7");
      assert.equal(err.cliRejection, true);
      return true;
    },
  );

  // Non-JSON stdout keeps the stderr-or-generic fallback for crashes.
  const stderrShape = 'process.stderr.write("boom\\n"); process.exit(2);';
  await assert.rejects(
    runJsonCommand(process.execPath, ["-e", stderrShape], {
      timeoutMs: 8000,
      outputLimitBytes: 65536,
    }),
    (err) => {
      assert.equal(err.message, "boom");
      assert.notEqual(err.cliRejection, true);
      return true;
    },
  );

  // JSON stdout without the failure shape (partial success output before a
  // crash) must not be mistaken for a CLI rejection.
  const partialShape = 'process.stdout.write(JSON.stringify({ ok: true }) + "\\n"); process.exit(3);';
  await assert.rejects(
    runJsonCommand(process.execPath, ["-e", partialShape], {
      timeoutMs: 8000,
      outputLimitBytes: 65536,
    }),
    /exited with code 3/,
  );
});

test("a whitespace-padded configured token still authenticates clients", async (t) => {
  // MORPHEUS_G2_TOKEN with a trailing space must mean the same secret as the
  // trimmed value clients send: the trimmed token is canonical at config time.
  const { baseUrl, config } = await withBridge(t, { token: "secret42 " });
  assert.equal(config.token, "secret42");
  const res = await request(baseUrl, "/api/sessions", { token: "secret42" });
  assert.equal(res.status, 200);
});

test("the session buffer being created is exempt from cap eviction", async (t) => {
  const { baseUrl, state } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    maxTrackedSessions: 6,
  });
  const open = [];
  t.after(async () => {
    for (const res of open) {
      await res.body.cancel().catch(() => {});
    }
  });

  // Fill the cap with streams that stay connected, so every existing buffer
  // is protected and the only evictable key is whatever comes next.
  for (let i = 0; i < 6; i += 1) {
    const res = await fetch(`${baseUrl}/api/events?sessionId=held-${i}&token=${TOKEN}`);
    assert.equal(res.status, 200);
    open.push(res);
  }
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const registered = [...Array(6).keys()].every(
      (i) => state.sessions.get(`held-${i}`)?.clients?.size === 1,
    );
    if (registered) break;
    await sleep(10);
  }

  // The (cap+1)-th subscriber names a brand-new session id; its just-created
  // buffer must not be evicted out from under the connecting client, or the
  // stream stays attached to an orphaned buffer and never delivers a message.
  const extra = await fetch(`${baseUrl}/api/events?sessionId=fresh-key&token=${TOKEN}`);
  assert.equal(extra.status, 200);
  open.push(extra);
  for (
    let attempt = 0;
    attempt < 100 && state.sessions.get("fresh-key")?.clients?.size !== 1;
    attempt += 1
  ) {
    await sleep(10);
  }
  assert.equal(state.sessions.has("fresh-key"), true, "the just-created buffer must survive eviction");
  assert.equal(
    state.sessions.get("fresh-key")?.clients?.size,
    1,
    "the SSE client must be attached to the tracked buffer",
  );
});

test("actively polled sessions keep their transcript across cap eviction", async (t) => {
  const { baseUrl, state } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
    maxTrackedSessions: 6,
  });

  async function openAndClose(sessionId) {
    const res = await fetch(`${baseUrl}/api/events?sessionId=${sessionId}&token=${TOKEN}`);
    assert.equal(res.status, 200);
    await readStreamFor(res.body, 10);
    // Wait for the stream to detach so the buffer becomes evictable again.
    for (
      let attempt = 0;
      attempt < 100 && state.sessions.get(sessionId)?.clients?.size;
      attempt += 1
    ) {
      await sleep(10);
    }
  }

  // A poll-only client: the stream that created the buffer is gone, and the
  // client keeps reading the transcript through GET /api/messages only.
  await openAndClose("poll-target");
  for (let i = 0; i < 10; i += 1) {
    await openAndClose(`junk-${i}`);
    const poll = await request(baseUrl, "/api/messages?sessionId=poll-target");
    assert.equal(poll.status, 200);
  }

  assert.equal(state.sessions.has("poll-target"), true, "a session read on every poll must not be evicted");
  assert.equal(state.sessions.has("junk-0"), false, "idle junk buffers are evicted instead");
});

test("codex live-event check marks stay bounded by the configured cap", async (t) => {
  const { provider, state } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: (emit, client) => ({
      ...fakeCodexAgentProvider()(emit, client),
      getSubscribedSessions: () => [],
    }),
    codexClient: { threadResume: async () => ({}) },
    mirrorCodexTui: false,
    showProjectsFirst: true,
    maxTrackedSessions: 10,
  });

  // A burst of distinct thread ids within one freshness window used to grow
  // the map unbounded, and the sweep ignored the configured cap.
  const now = Date.now();
  for (let i = 0; i < 40; i += 1) {
    state.codexLiveEventsCheckedAt.set(`burst-thread-${i}`, now);
  }
  const resubscribed = await provider.ensureLiveEvents("fresh-live-thread");
  assert.equal(resubscribed, true);
  assert.equal(state.codexLiveEventsCheckedAt.has("fresh-live-thread"), true);
  assert.ok(
    state.codexLiveEventsCheckedAt.size <= 10,
    `live-event check marks must stay within the cap, got ${state.codexLiveEventsCheckedAt.size}`,
  );
});

test("non-numeric after param on messages polls returns the transcript", async (t) => {
  const { baseUrl } = await withBridge(t, {
    agentBackend: "codex_app_server",
    createCodexAgentProvider: fakeCodexAgentProvider(),
    mirrorCodexTui: false,
    showProjectsFirst: true,
  });
  const prompt = await request(baseUrl, "/api/prompt", {
    body: {
      sessionId: "project:p_alpha",
      text: "cursor turn",
      clientRequestId: "after-garbage-prompt",
    },
  });
  assert.equal(prompt.status, 202);

  // `after=abc` used to parse to NaN, making every `id > NaN` comparison
  // false and blanking the transcript for that client forever.
  const res = await request(baseUrl, "/api/messages?sessionId=codex-thread-1&after=abc");
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(
    body.messages.some(
      (message) => message.type === "result" && message.text === "answer for: cursor turn",
    ),
    true,
    "garbage after cursors must fall back to the full transcript",
  );
});

test("ambiguous cached project references fail with 409 when the provider is down", async (t) => {
  let failProjects = false;
  const projects = [
    { id: "p_dup1", tenant_id: "p_dup1", name: "dup", root_path: "/tmp/dup-one", archived: false, usage: {} },
    { id: "p_dup2", tenant_id: "p_dup2", name: "dup", root_path: "/tmp/dup-two", archived: false, usage: {} },
  ];
  const runner = async (_command, args) => {
    if (args[1] === "projects") {
      if (failProjects) throw new Error("project list down");
      return { current_project_id: "p_dup1", projects };
    }
    throw new Error(`unexpected remote command: ${args.slice(1).join(" ")}`);
  };
  const { baseUrl } = await withBridge(t, { runner, showProjectsFirst: true });

  const prime = await request(baseUrl, "/api/sessions?view=projects");
  assert.equal(prime.status, 200);

  failProjects = true;
  const res = await request(baseUrl, "/api/select-session", {
    body: { sessionId: "project:dup", clientRequestId: "ambiguous-cached-select" },
  });
  assert.equal(res.status, 409, "cached ambiguity must match the live path's 409, not a 500");
  assert.match((await res.json()).error, /ambiguous project reference/);
});

test("refuses to start with an empty or whitespace bearer token", () => {
  assert.throws(
    () => createBridge({ token: "", morpheusBin: FIXTURE, auditPath: "", logger: silentLogger() }),
    /empty bearer token/i,
  );
  assert.throws(
    () => createBridge({ token: "   ", morpheusBin: FIXTURE, auditPath: "", logger: silentLogger() }),
    /empty bearer token/i,
  );
});

test("startBridge reports a friendly message when the port is already in use", async (t) => {
  const blocker = http.createServer(() => {});
  await new Promise((resolve) => blocker.listen(0, "127.0.0.1", resolve));
  const port = blocker.address().port;
  const logs = [];
  const errors = [];
  let shutdownCalls = 0;
  const previousExitCode = process.exitCode;
  const server = startBridge({
    host: "127.0.0.1",
    port,
    token: TOKEN,
    tokenSource: "env",
    morpheusBin: FIXTURE,
    agentBackend: "morpheus",
    auditPath: "",
    provider: {
      name: "fake",
      allowedActions: [],
      promptBehavior: "send_prompt",
      async shutdown() {
        shutdownCalls += 1;
      },
    },
    logger: {
      log: (line) => logs.push(String(line)),
      warn: (line) => logs.push(String(line)),
      error: (line) => errors.push(String(line)),
    },
  });
  t.after(() => {
    try {
      server.close();
    } catch {
      // the server never listened
    }
    blocker.close();
    process.exitCode = previousExitCode;
  });

  for (let attempt = 0; attempt < 100 && !errors.length; attempt += 1) {
    await sleep(10);
  }
  assert.ok(errors.length >= 1, "the listen failure must be reported");
  assert.match(errors[0], /already in use/);
  assert.match(errors[0], /--port|PORT/);
  assert.equal(process.exitCode, 1);
  assert.deepEqual(logs, [], "the startup banner must not print when listen fails");
  // Whatever startBridge started (provider warm-up state) must be torn down
  // again, otherwise the failed process hangs on a live event loop.
  for (let attempt = 0; attempt < 100 && !shutdownCalls; attempt += 1) {
    await sleep(10);
  }
  assert.equal(shutdownCalls, 1, "the listen failure must shut the provider down");
});

// --- Omnipresence feed (feed:main), feed acks, and context ingestion ---

const FEED_OMNI = {
  enabled: true,
  threshold: 0.7,
  push_per_hour: 6,
  quiet_hours: null,
  feed: "main",
};

function feedItem(id, title, body = "", extra = {}) {
  return {
    id,
    ts: 1_779_999_900 + id,
    title,
    body,
    priority: 1,
    source_kind: "loop",
    source_ref: `loop:test-${id}`,
    metadata: {},
    ...extra,
  };
}

function readFeedStateFile(statePath) {
  return JSON.parse(fs.readFileSync(statePath, "utf8"));
}

function writeFeedStateFile(statePath, stateObj) {
  // Write-then-rename keeps the fixture from ever reading a half-written file
  // while the bridge feed poller races test-side appends.
  const tmpPath = `${statePath}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(stateObj));
  fs.renameSync(tmpPath, statePath);
}

function appendFeedItem(statePath, item) {
  const stateObj = readFeedStateFile(statePath);
  stateObj.items = [...(stateObj.items || []), item];
  writeFeedStateFile(statePath, stateObj);
}

async function withFeedBridge(t, { enabled = true, items = [], options = {} } = {}) {
  const statePath = path.join(
    tmpdir(),
    `morpheus-g2-feed-${process.pid}-${t.name.replace(/[^A-Za-z0-9_-]/g, "_")}.json`,
  );
  writeFeedStateFile(statePath, {
    omni: { ...FEED_OMNI, enabled },
    items,
    acks: [],
    contexts: [],
  });
  process.env.MOCK_MORPHEUS_STATE_FILE = statePath;
  t.after(() => {
    delete process.env.MOCK_MORPHEUS_STATE_FILE;
    fs.rmSync(statePath, { force: true });
  });
  const bridge = await withBridge(t, options);
  t.after(() => {
    // Feed pollers stop themselves once subscribers disappear, but tests that
    // keep the feed selected would otherwise leave an interval spawning the
    // fixture for the rest of the suite.
    if (bridge.state.feedPoller) {
      clearInterval(bridge.state.feedPoller);
      bridge.state.feedPoller = null;
    }
  });
  return { ...bridge, statePath };
}

test("feed endpoint pages with an ascending cursor", async (t) => {
  const { baseUrl } = await withFeedBridge(t, {
    items: [feedItem(1, "first push"), feedItem(2, "second push"), feedItem(3, "third push")],
  });

  const unauthorized = await request(baseUrl, "/api/feed", { token: null });
  assert.equal(unauthorized.status, 401);

  const newest = await request(baseUrl, "/api/feed?limit=2");
  assert.equal(newest.status, 200);
  const newestBody = await newest.json();
  assert.deepEqual(
    newestBody.items.map((item) => item.id),
    [2, 3],
    "after=0 returns the newest limit items in ascending order",
  );
  assert.equal(newestBody.latest_id, 3);
  assert.equal(newestBody.omnipresence.enabled, true);

  const paged = await request(baseUrl, "/api/feed?after=1");
  const pagedBody = await paged.json();
  assert.deepEqual(
    pagedBody.items.map((item) => item.id),
    [2, 3],
    "after cursor returns only strictly newer items",
  );

  const drained = await request(baseUrl, "/api/feed?after=3");
  assert.deepEqual((await drained.json()).items, []);

  const garbage = await request(baseUrl, "/api/feed?after=abc&limit=zzz");
  assert.equal(garbage.status, 200);
  const garbageBody = await garbage.json();
  assert.deepEqual(
    garbageBody.items.map((item) => item.id),
    [1, 2, 3],
    "non-numeric params fall back to defaults instead of poisoning the cursor",
  );
});

test("feed ack validates actions and replays duplicates without re-acking", async (t) => {
  const { baseUrl, statePath, auditPath } = await withFeedBridge(t, {
    items: [feedItem(1, "push")],
  });

  const badAction = await request(baseUrl, "/api/feed/ack", {
    body: { itemId: 1, action: "archived", clientRequestId: "ack-bad-0001" },
  });
  assert.equal(badAction.status, 400);
  assert.equal((await badAction.json()).code, "invalid_feed_action");

  const missingItem = await request(baseUrl, "/api/feed/ack", {
    body: { action: "expanded", clientRequestId: "ack-bad-0002" },
  });
  assert.equal(missingItem.status, 400);
  assert.equal((await missingItem.json()).code, "missing_feed_item_id");

  const ack = await request(baseUrl, "/api/feed/ack", {
    body: { itemId: 1, action: "expanded", clientRequestId: "ack-0001" },
  });
  assert.equal(ack.status, 200);
  const ackBody = await ack.json();
  assert.equal(ackBody.ok, true);
  assert.equal(ackBody.item, 1);
  assert.equal(ackBody.action, "expanded");

  const replay = await request(baseUrl, "/api/feed/ack", {
    body: { itemId: 1, action: "expanded", clientRequestId: "ack-0001" },
  });
  assert.equal(replay.status, 200);
  assert.equal((await replay.json()).duplicate, true);

  assert.equal(
    readFeedStateFile(statePath).acks.length,
    1,
    "the duplicate request id must not reach the CLI a second time",
  );

  await sleep(50);
  const audit = fs.readFileSync(auditPath, "utf8");
  assert.match(audit, /feed_ack/);
});

test("context ingestion validates location strictly and never logs coordinates", async (t) => {
  const { baseUrl, statePath, auditPath } = await withFeedBridge(t, { items: [] });
  const rejected = [
    { kind: "location", lat: "48.13", lon: 11.58 },
    { kind: "location", lat: 48.13 },
    { kind: "location", lat: 91, lon: 11.58 },
    { kind: "location", lat: 48.13, lon: -191 },
    { kind: "location", lat: 48.13, lon: 11.58, accuracy: "12" },
    { kind: "battery", level: 80 },
  ];
  for (const [idx, body] of rejected.entries()) {
    const res = await request(baseUrl, "/api/context", {
      body: { ...body, clientRequestId: `ctx-bad-000${idx}` },
    });
    assert.equal(res.status, 400, `must reject ${JSON.stringify(body)}`);
  }
  assert.equal(readFeedStateFile(statePath).contexts.length, 0, "rejects never reach the CLI");

  const ok = await request(baseUrl, "/api/context", {
    body: {
      kind: "location",
      lat: 48.137154,
      lon: 11.576124,
      accuracy: 12.5,
      ts: 1_779_999_999.5,
      clientRequestId: "ctx-0001",
    },
  });
  assert.equal(ok.status, 200);
  const okBody = await ok.json();
  assert.equal(okBody.ok, true);
  assert.equal(okBody.kind, "location");
  assert.ok(Number.isInteger(okBody.id));

  const replay = await request(baseUrl, "/api/context", {
    body: {
      kind: "location",
      lat: 48.137154,
      lon: 11.576124,
      accuracy: 12.5,
      ts: 1_779_999_999.5,
      clientRequestId: "ctx-0001",
    },
  });
  assert.equal(replay.status, 200);
  assert.equal((await replay.json()).duplicate, true);

  const contexts = readFeedStateFile(statePath).contexts;
  assert.equal(contexts.length, 1, "the duplicate request id must not store a second signal");
  assert.equal(contexts[0].data.lat, 48.137154);
  assert.equal(contexts[0].data.lon, 11.576124);

  await sleep(50);
  const audit = fs.readFileSync(auditPath, "utf8");
  assert.match(audit, /context_add/);
  assert.match(audit, /payloadHash/);
  assert.doesNotMatch(audit, /48\.137154/);
  assert.doesNotMatch(audit, /11\.576124/);
});

test("feed row leads the session list when omnipresence is enabled", async (t) => {
  const { baseUrl } = await withFeedBridge(t, {
    items: [feedItem(1, "Espresso beans on promo 50m left")],
  });

  const sessions = await request(baseUrl, "/api/sessions");
  const body = await sessions.json();
  assert.equal(body.sessions[0].id, "feed:main");
  assert.equal(body.sessions[0].title, "Morpheus Feed");
  assert.ok(body.sessions.some((row) => row.id === "abc123"));

  const projects = await request(baseUrl, "/api/sessions?view=projects");
  const projectsBody = await projects.json();
  assert.equal(projectsBody.sessions[0].id, "feed:main");
  assert.ok(projectsBody.sessions.some((row) => row.id === "project:p_alpha"));

  await request(baseUrl, "/api/select-project", {
    body: { projectId: "p_alpha", clientRequestId: "feed-select-project-0001" },
  });
  const afterSelect = await request(baseUrl, "/api/sessions");
  const afterBody = await afterSelect.json();
  assert.equal(afterBody.sessions[0].id, "feed:main");

  const info = await request(baseUrl, "/api/info");
  assert.deepEqual((await info.json()).omnipresence, { enabled: true });
});

test("feed row is absent and info reports disabled when omnipresence is off", async (t) => {
  const { baseUrl } = await withFeedBridge(t, {
    enabled: false,
    items: [feedItem(1, "hidden push")],
  });
  const sessions = await request(baseUrl, "/api/sessions");
  const body = await sessions.json();
  assert.equal(body.sessions[0].id, "abc123");
  assert.ok(!body.sessions.some((row) => row.id === "feed:main"));

  const info = await request(baseUrl, "/api/info");
  assert.deepEqual((await info.json()).omnipresence, { enabled: false });
});

test("opening feed history returns pushes as assistant lines and selects the feed row", async (t) => {
  const { baseUrl } = await withFeedBridge(t, {
    items: [feedItem(1, "First push title", "First push body"), feedItem(2, "Second push title")],
  });

  const history = await request(baseUrl, "/api/sessions/feed:main/history");
  assert.equal(history.status, 200);
  const body = await history.json();
  assert.equal(body.selectedSession.id, "feed:main");
  assert.equal(body.view, "session");
  assert.ok(body.history.every((entry) => entry.role === "assistant"));
  assert.equal(body.history[0].text, "First push title\nFirst push body");
  assert.equal(body.history[1].text, "Second push title");

  const selected = await request(baseUrl, "/api/selected-session");
  assert.equal((await selected.json()).selectedSession.id, "feed:main");

  const status = await request(baseUrl, "/api/status?sessionId=feed:main");
  assert.equal((await status.json()).state, "idle");

  const select = await request(baseUrl, "/api/select-session", {
    body: { sessionId: "feed:main", clientRequestId: "feed-select-0001" },
  });
  assert.equal(select.status, 200);
  assert.equal((await select.json()).selectedSession.id, "feed:main");
});

test("new feed items stream to SSE subscribers and message polls under a cursor", async (t) => {
  const { baseUrl, statePath, state } = await withFeedBridge(t, {
    items: [feedItem(1, "First push title")],
    options: { feedPollMs: 25, feedSubscriberIdleMs: 60_000 },
  });

  const events = await fetch(`${baseUrl}/api/events?sessionId=feed:main&token=${TOKEN}`);
  assert.equal(events.status, 200);
  // The first fetch after startup baselines pre-existing items quietly; wait
  // for it so the appended item is a genuinely new push, not baseline cargo.
  const baselineUntil = Date.now() + 3000;
  while (!state.feedBaselined && Date.now() < baselineUntil) {
    await sleep(10);
  }
  assert.equal(state.feedBaselined, true, "the poller must baseline the feed on its first fetch");
  appendFeedItem(statePath, feedItem(2, "Second push title", "Second push body"));
  const streamed = await readStreamUntil(events.body, "Second push title", 5000);
  assert.doesNotMatch(
    streamed,
    /First push title/,
    "pre-existing items must not stream as fresh events",
  );
  assert.match(streamed, /"sessionId":"feed:main"/);
  assert.match(streamed, /"type":"result"/);

  const polled = await readMessagesUntil(
    baseUrl,
    "feed:main",
    (messages) => messages.some((message) => message.feedItem?.id === 2),
    3000,
  );
  const firstMessage = polled.messages.find((message) => message.feedItem?.id === 1);
  assert.ok(firstMessage, "the first feed item must be in the buffer");
  const afterCursor = await request(
    baseUrl,
    `/api/messages?sessionId=feed:main&after=${firstMessage.id}`,
  );
  const afterBody = await afterCursor.json();
  assert.ok(afterBody.messages.some((message) => message.feedItem?.id === 2));
  assert.ok(!afterBody.messages.some((message) => message.feedItem?.id === 1));
});

test("feed poller runs while subscribed and stops after subscribers disappear", async (t) => {
  const { baseUrl, state } = await withFeedBridge(t, {
    items: [feedItem(1, "Poller push")],
    options: { feedPollMs: 25, feedSubscriberIdleMs: 120 },
  });
  assert.equal(state.feedPoller, null);

  const history = await request(baseUrl, "/api/sessions/feed:main/history");
  assert.equal(history.status, 200);
  assert.ok(state.feedPoller, "opening the feed row must start the poller");

  // Leaving the feed clears the selection; once the idle window passes the
  // poller's next tick clears its own timer.
  const back = await request(baseUrl, "/api/back", {
    body: { clientRequestId: "feed-back-0001" },
  });
  assert.equal(back.status, 200);
  const until = Date.now() + 3000;
  while (state.feedPoller && Date.now() < until) {
    await sleep(25);
  }
  assert.equal(state.feedPoller, null, "the poller timer must be cleared without subscribers");
});

test("feed selection alone does not keep the poller alive past the idle window", async (t) => {
  const { baseUrl, state } = await withFeedBridge(t, {
    items: [feedItem(1, "Idle push")],
    options: { feedPollMs: 25, feedSubscriberIdleMs: 120 },
  });

  const select = await request(baseUrl, "/api/select-session", {
    body: { sessionId: "feed:main", clientRequestId: "feed-idle-select-0001" },
  });
  assert.equal(select.status, 200);
  assert.ok(state.feedPoller, "selecting the feed must start the poller");

  // Selection is server-side state that survives client disconnects; with no
  // SSE client and no history/messages/status polls, the idle window must
  // clear the timer even though feed:main stays selected.
  const until = Date.now() + 3000;
  while (state.feedPoller && Date.now() < until) {
    await sleep(25);
  }
  assert.equal(
    state.feedPoller,
    null,
    "a lingering selection must not keep the poller shelling out forever",
  );
  assert.equal(state.selectedSession?.id, "feed:main", "stopping the poller must not deselect");
});

test("full feed pages advance the cursor without dropping burst items", async (t) => {
  const { baseUrl, statePath, state } = await withFeedBridge(t, {
    items: [feedItem(1, "Baseline push")],
    // Long intervals: the test drives fetches deterministically via history
    // opens, and disarms the poller so background ticks cannot interleave.
    options: { feedPollMs: 60_000, feedSubscriberIdleMs: 60_000 },
  });
  const disarmPoller = () => {
    if (state.feedPoller) {
      clearInterval(state.feedPoller);
      state.feedPoller = null;
    }
  };

  // Baseline fetch: hydrates the pre-existing item and jumps to latest_id.
  const baseline = await request(baseUrl, "/api/sessions/feed:main/history");
  assert.equal(baseline.status, 200);
  disarmPoller();
  assert.equal(state.feedCursor, 1);

  // A 30-item burst against the default 20-item page. feeds.recent_after
  // returns the OLDEST page above the cursor, so jumping to latest_id on a
  // full page would silently drop items 22..31.
  for (let id = 2; id <= 31; id += 1) {
    appendFeedItem(statePath, feedItem(id, `Burst push ${id}`));
  }

  const firstPoll = await request(baseUrl, "/api/sessions/feed:main/history");
  assert.equal(firstPoll.status, 200);
  disarmPoller();
  assert.equal(
    state.feedCursor,
    21,
    "a full page must advance to the last published id, not jump to latest_id",
  );

  const secondPoll = await request(baseUrl, "/api/sessions/feed:main/history");
  assert.equal(secondPoll.status, 200);
  disarmPoller();
  assert.equal(state.feedCursor, 31, "the short second page drains the burst to latest_id");

  const messages = await request(baseUrl, "/api/messages?sessionId=feed:main");
  const ids = (await messages.json()).messages
    .filter((message) => message.feedItem)
    .map((message) => message.feedItem.id);
  assert.deepEqual(
    ids,
    Array.from({ length: 31 }, (_, index) => index + 1),
    "two polls must publish all burst items in order",
  );
});

test("restart baseline shows old items in history without re-pushing them", async (t) => {
  // Simulates a bridge restart: items 1-2 predate the process (the cursor
  // starts at 0), so the first fetch must hydrate them for history only.
  const { baseUrl, statePath, state } = await withFeedBridge(t, {
    items: [feedItem(1, "Old push one"), feedItem(2, "Old push two")],
    options: { feedPollMs: 25, feedSubscriberIdleMs: 60_000 },
  });

  // The SSE client attaches before the baseline fetch runs; pre-existing
  // items must not arrive on it as fresh events.
  const events = await fetch(`${baseUrl}/api/events?sessionId=feed:main&token=${TOKEN}`);
  assert.equal(events.status, 200);
  const baselineUntil = Date.now() + 3000;
  while (!state.feedBaselined && Date.now() < baselineUntil) {
    await sleep(10);
  }
  assert.equal(state.feedBaselined, true);
  assert.equal(state.feedCursor, 2, "the baseline fetch must jump the cursor to latest_id");

  appendFeedItem(statePath, feedItem(3, "Fresh push three"));
  const streamed = await readStreamUntil(events.body, "Fresh push three", 5000);
  assert.doesNotMatch(streamed, /Old push one/, "baseline items must not re-notify");
  assert.doesNotMatch(streamed, /Old push two/, "baseline items must not re-notify");

  // The history endpoint still shows the pre-existing items after restart.
  const history = await request(baseUrl, "/api/sessions/feed:main/history");
  assert.equal(history.status, 200);
  const texts = (await history.json()).history.map((entry) => entry.text);
  assert.ok(texts.some((text) => text.includes("Old push one")));
  assert.ok(texts.some((text) => text.includes("Old push two")));
  assert.ok(texts.some((text) => text.includes("Fresh push three")));
});

test("feed ack relays CLI validation rejections as 400s with the CLI error", async (t) => {
  const { baseUrl, statePath, auditPath } = await withFeedBridge(t, {
    items: [feedItem(1, "push")],
  });

  // Item 99 passes the bridge's own validation but the CLI rejects it with
  // {"ok":false,"error":"..."} on stdout and exit 1 (empty stderr).
  const rejected = await request(baseUrl, "/api/feed/ack", {
    body: { itemId: 99, action: "dismissed", clientRequestId: "ack-unknown-0001" },
  });
  assert.equal(rejected.status, 400, "a CLI validation rejection must not surface as a 500");
  const rejectedBody = await rejected.json();
  assert.equal(rejectedBody.code, "feed_ack_failed");
  assert.match(rejectedBody.error, /unknown feed item: 99/);

  // The replay cache remembers the 400, and the rejected ack never lands.
  const replay = await request(baseUrl, "/api/feed/ack", {
    body: { itemId: 99, action: "dismissed", clientRequestId: "ack-unknown-0001" },
  });
  assert.equal(replay.status, 400);
  assert.equal(readFeedStateFile(statePath).acks.length, 0);

  await sleep(50);
  const audit = fs.readFileSync(auditPath, "utf8");
  assert.match(audit, /feed_ack_failed/);
});

test("context ingestion maps CLI rejections to 400 and infrastructure failures to 500", async (t) => {
  let failureMode = "cli";
  const runner = async (_command, args) => {
    if (args[1] === "context-add") {
      if (failureMode === "cli") {
        // What runJsonCommand raises for the real CLI's stdout failure shape.
        const err = new Error("context location requires numeric lat and lon");
        err.cliRejection = true;
        throw err;
      }
      throw new Error("morpheus timed out after 10000ms");
    }
    throw new Error(`unexpected remote command: ${args.join(" ")}`);
  };
  const { baseUrl } = await withBridge(t, { runner });

  const cliRejected = await request(baseUrl, "/api/context", {
    body: { kind: "location", lat: 48.13, lon: 11.58, clientRequestId: "ctx-cli-reject-0001" },
  });
  assert.equal(cliRejected.status, 400);
  const cliBody = await cliRejected.json();
  assert.equal(cliBody.code, "context_add_failed");
  assert.match(cliBody.error, /numeric lat and lon/);

  failureMode = "infra";
  const infraFailed = await request(baseUrl, "/api/context", {
    body: { kind: "location", lat: 48.13, lon: 11.58, clientRequestId: "ctx-infra-0001" },
  });
  assert.equal(infraFailed.status, 500);
  assert.equal((await infraFailed.json()).code, "context_add_failed");
});

test("prompts to the feed row answer read-only and never spawn sessions", async (t) => {
  const { baseUrl, auditPath } = await withFeedBridge(t, {
    items: [feedItem(1, "Quiet push")],
  });
  await request(baseUrl, "/api/sessions/feed:main/history");

  const prompt = await request(baseUrl, "/api/prompt", {
    body: { text: "hello glasses feed", clientRequestId: "feed-prompt-0001" },
  });
  assert.equal(prompt.status, 200);
  const promptBody = await prompt.json();
  assert.equal(promptBody.action, "feed_read_only");
  assert.equal(promptBody.sessionId, "feed:main");
  assert.match(promptBody.text, /read-only/i);
  assert.equal(promptBody.answer, promptBody.text);

  const explicit = await request(baseUrl, "/api/prompt", {
    body: { sessionId: "feed:main", text: "hello again", clientRequestId: "feed-prompt-0002" },
  });
  assert.equal(explicit.status, 200);
  assert.equal((await explicit.json()).action, "feed_read_only");

  const replay = await request(baseUrl, "/api/prompt", {
    body: { text: "hello glasses feed", clientRequestId: "feed-prompt-0001" },
  });
  assert.equal((await replay.json()).duplicate, true);

  const selected = await request(baseUrl, "/api/selected-session");
  assert.equal((await selected.json()).selectedSession.id, "feed:main");

  const messages = await request(baseUrl, "/api/messages?sessionId=feed:main");
  const messagesBody = await messages.json();
  assert.ok(!messagesBody.messages.some((message) => message.type === "session_started"));
  assert.ok(messagesBody.messages.some((message) => message.feedNotice === true));

  await sleep(50);
  const audit = fs.readFileSync(auditPath, "utf8");
  assert.match(audit, /remote_prompt_feed_read_only/);
  assert.doesNotMatch(audit, /remote_spawn_session/);
  assert.doesNotMatch(audit, /hello glasses feed/);
});
