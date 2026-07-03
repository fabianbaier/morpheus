import assert from "node:assert/strict";
import fs from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createBridge } from "../src/server.mjs";
import { DEFAULT_BRIDGE_URL, G2BridgeClient } from "../simulator/src/bridge-client.js";

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
  return path.join(tmpdir(), `morpheus-g2-sim-${process.pid}-${name}.jsonl`);
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
    agentBackend: "codex_app_server",
    mirrorCodexTui: false,
    showProjectsFirst: true,
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

function fakeCodexAgentProvider({ seedSessions = [], history = null, asyncResultMs = 0 } = {}) {
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
      if (asyncResultMs > 0) {
        const timer = setTimeout(finish, asyncResultMs);
        if (typeof timer.unref === "function") timer.unref();
      } else {
        finish();
      }
      return { sessionId: id, provider: "codex" };
    },
  });
}

class FakeEventSource {
  constructor(url, sink) {
    this.url = url;
    this.closed = false;
    this.onmessage = null;
    this.onerror = null;
    sink.push(this);
  }

  close() {
    this.closed = true;
  }

  emit(id, payload) {
    this.onmessage?.({ lastEventId: String(id), data: JSON.stringify(payload) });
  }
}

function countMatches(text, pattern) {
  return (String(text || "").match(pattern) || []).length;
}

test("send transcript without waitFor polls until the routed session answers", async (t) => {
  const { baseUrl } = await withBridge(t, {
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 60 }),
    waitForPromptResult: false,
  });
  const calls = [];
  const fetchImpl = async (url, options) => {
    const parsed = new URL(url);
    calls.push(`${parsed.pathname}${parsed.search}`);
    return fetch(url, options);
  };
  const streams = [];
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    fetchImpl,
    eventSourceFactory: (url) => {
      streams.push(url);
      return null;
    },
  });

  await client.connect();
  await client.activateSelected();
  const submitted = await client.submitTranscriptViaSessionPolling("hello from the button", {
    timeoutMs: 4000,
    intervalMs: 25,
  });

  // The submit resolves as soon as the transcript is accepted and the stream
  // is opened -- before the async answer lands -- so UI input is never gated
  // on a whole agent turn. The answer arrives via the background wait.
  assert.equal(submitted.mode, "session");
  assert.doesNotMatch(submitted.glassesText, /answer for: hello from the button/);
  assert.ok(client.pendingResultWait, "expected a background result wait handle");

  // The finalize response lands before the async result, so the answer can
  // only appear through bounded polling of the routed session.
  const settled = await client.pendingResultWait;
  assert.equal(settled.mode, "session");
  assert.equal(settled.status, "idle");
  assert.match(settled.glassesText, /answer for: hello from the button/);
  assert.equal(countMatches(settled.glassesText, /answer for: hello from the button/g), 1);
  assert.equal(settled.displaySessionId, "project-session:p_alpha");
  assert.equal(settled.activeSessionId, "codex-thread-1");

  // The event stream for the routed session is opened, like selecting a session.
  assert.equal(
    streams.some((url) =>
      url.includes("/api/events?sessionId=project-session%3Ap_alpha"),
    ),
    true,
    `expected an event stream for the routed session, got: ${streams.join(", ")}`,
  );

  // The fabricated optimistic prompt id must not advance the server cursor:
  // the first poll asks for everything the bridge buffered.
  const messagePolls = calls.filter((url) => url.startsWith("/api/messages"));
  assert.ok(messagePolls.length >= 1, "expected /api/messages polling for the answer");
  assert.match(messagePolls[0], /after=0(?:&|$)/);
});

test("re-opening a session renders each exchange exactly once", async (t) => {
  const { baseUrl } = await withBridge(t, {
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
      asyncResultMs: 20,
    }),
    waitForPromptResult: false,
  });
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    eventSourceFactory: () => null,
  });

  await client.connect();
  await client.selectProject("p_alpha");
  const opened = await client.selectSession("old-cached-thread");
  assert.match(opened.glassesText, /earlier answer/);

  await client.submitTranscriptViaSessionPolling("continue this thread", {
    timeoutMs: 4000,
    intervalMs: 25,
  });
  const submitted = await client.pendingResultWait;
  assert.match(submitted.glassesText, /answer for: continue this thread/);

  await client.navigateBack();
  const reopened = await client.selectSession("old-cached-thread");

  // History rows and the re-fetched server buffer describe the same exchange;
  // it must render once instead of doubling on every re-open.
  assert.equal(
    countMatches(reopened.glassesText, /AI: answer for: continue this thread/g),
    1,
    `duplicated exchange in:\n${reopened.glassesText}`,
  );

  const thirdOpen = await client.selectSession("old-cached-thread");
  assert.equal(countMatches(thirdOpen.glassesText, /AI: answer for: continue this thread/g), 1);
});

test("history load merges with streamed messages instead of replacing them", async (t) => {
  const { baseUrl } = await withBridge(t, {
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
  });
  const streams = [];
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    eventSourceFactory: (url) => new FakeEventSource(url, streams),
  });

  await client.connect();
  await client.selectProject("p_alpha");
  const opened = await client.selectSession("old-cached-thread");
  assert.match(opened.glassesText, /earlier answer/);
  assert.equal(streams.length, 1);

  streams[0].emit(500, {
    type: "result",
    success: true,
    text: "streamed reply after history",
    provider: "codex",
    sessionId: "old-cached-thread",
  });
  assert.match(client.snapshot().glassesText, /streamed reply after history/);

  // A history response arriving after stream messages must merge, not wipe.
  await client.loadHistory("old-cached-thread");
  const merged = client.snapshot();
  assert.match(merged.glassesText, /streamed reply after history/);
  assert.match(merged.glassesText, /earlier answer/);
});

test("open event stream resolves the transcript wait without duplicate message polling", async (t) => {
  const { baseUrl } = await withBridge(t, {
    // The result only arrives long after the test window, so the wait can
    // resolve exclusively through the (fake) event stream.
    createCodexAgentProvider: fakeCodexAgentProvider({ asyncResultMs: 60_000 }),
    waitForPromptResult: false,
  });
  const calls = [];
  const fetchImpl = async (url, options) => {
    const parsed = new URL(url);
    calls.push(`${parsed.pathname}${parsed.search}`);
    return fetch(url, options);
  };
  const streams = [];
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    fetchImpl,
    eventSourceFactory: (url) => new FakeEventSource(url, streams),
  });

  await client.connect();
  await client.activateSelected();
  const submitted = await client.submitTranscriptViaSessionPolling("stream should answer this", {
    timeoutMs: 3000,
    intervalMs: 25,
  });
  assert.equal(submitted.mode, "session");
  assert.ok(streams.length >= 1, "expected the submit to open an event stream");

  // The answer and the idle status land through SSE only.
  const stream = streams.at(-1);
  stream.emit(41, {
    type: "result",
    success: true,
    text: "streamed answer without polling",
    provider: "codex",
    sessionId: "codex-thread-1",
  });
  stream.emit(42, { type: "status", state: "idle", provider: "codex", sessionId: "codex-thread-1" });

  const settled = await client.pendingResultWait;
  assert.equal(settled.status, "idle");
  assert.match(settled.glassesText, /streamed answer without polling/);

  // With the stream open, the wait must not shadow it with tight
  // /api/messages polling; SSE-driven state resolves it directly.
  const messagePolls = calls.filter((url) => url.startsWith("/api/messages"));
  assert.deepEqual(messagePolls, [], "expected no /api/messages polls while the stream is open");
});

test("changing the bridge URL or token resets the stream and message cursor", async (t) => {
  const { baseUrl } = await withBridge(t, {
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
  });
  const streams = [];
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    eventSourceFactory: (url) => new FakeEventSource(url, streams),
  });

  await client.connect();
  await client.selectProject("p_alpha");
  await client.selectSession("old-cached-thread");
  assert.equal(streams.length, 1);
  streams[0].emit(700, {
    type: "result",
    success: true,
    text: "message that advances the cursor",
    provider: "codex",
    sessionId: "old-cached-thread",
  });
  assert.equal(client.serverCursor, 700);

  // Reconfiguring with identical values (the simulator saves config before
  // every action) must not reset anything.
  client.configure({ bridgeUrl: baseUrl, token: TOKEN });
  assert.equal(client.serverCursor, 700);
  assert.equal(streams[0].closed, false);
  assert.equal(client.eventSource, streams[0]);
  assert.match(client.snapshot().glassesText, /message that advances the cursor/);

  // An actual URL change closes the old bridge's stream and resets the
  // cursor and view so the new bridge is polled from scratch.
  client.configure({ bridgeUrl: "http://127.0.0.1:59999" });
  assert.equal(client.bridgeUrl, "http://127.0.0.1:59999");
  assert.equal(streams[0].closed, true);
  assert.equal(client.eventSource, null);
  assert.equal(client.serverCursor, 0);
  assert.deepEqual(client.snapshot().messages, []);
  assert.equal(client.snapshot().status, "idle");

  // A token change invalidates the stream and cursor the same way.
  client.serverCursor = 55;
  client.configure({ bridgeUrl: "http://127.0.0.1:59999", token: "rotated-token" });
  assert.equal(client.serverCursor, 0);
});

test("clearing the bridge URL falls back to the default", () => {
  const client = new G2BridgeClient({
    bridgeUrl: "http://10.0.0.5:9999",
    token: TOKEN,
    eventSourceFactory: () => null,
  });

  client.configure({ bridgeUrl: "" });
  assert.equal(client.bridgeUrl, DEFAULT_BRIDGE_URL);

  // Leaving bridgeUrl undefined keeps the current value.
  client.configure({ token: "rotated" });
  assert.equal(client.bridgeUrl, DEFAULT_BRIDGE_URL);
  assert.equal(client.token, "rotated");

  client.configure({ bridgeUrl: "http://10.0.0.6:8888/" });
  assert.equal(client.bridgeUrl, "http://10.0.0.6:8888");
});

test("API errors with empty or invalid JSON bodies keep method, path, and status", async () => {
  const responses = [];
  const client = new G2BridgeClient({
    bridgeUrl: "http://bridge.test",
    token: TOKEN,
    eventSourceFactory: () => null,
    fetchImpl: async () => responses.shift(),
  });

  responses.push({
    ok: false,
    status: 502,
    headers: { get: () => "application/json; charset=utf-8" },
    text: async () => "",
  });
  await assert.rejects(client.api("/api/info"), /GET \/api\/info failed with 502/);

  responses.push({
    ok: false,
    status: 504,
    headers: { get: () => "application/json" },
    text: async () => "<html>gateway timeout</html>",
  });
  await assert.rejects(client.api("/api/info"), /<html>gateway timeout<\/html>/);

  responses.push({
    ok: false,
    status: 401,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify({ error: "Missing bearer token" }),
  });
  await assert.rejects(client.api("/api/info"), /Missing bearer token/);

  responses.push({
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    text: async () => "not-json",
  });
  await assert.rejects(client.api("/api/info"), /GET \/api\/info returned invalid JSON/);
});
