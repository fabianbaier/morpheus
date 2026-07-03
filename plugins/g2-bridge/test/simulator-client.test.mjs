import assert from "node:assert/strict";
import fs from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createBridge } from "../src/server.mjs";
import {
  DEFAULT_BRIDGE_URL,
  FEED_NAV_CONVERSATIONS_ID,
  FEED_SESSION_ID,
  G2BridgeClient,
} from "../simulator/src/bridge-client.js";

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

// --- Omnipresence: ambient feed view + location context ---
// These run against the REAL bridge with the mock-morpheus fixture; the
// fixture serves feed/omni-status/context-add out of the env-fed state file.

const FEED_OMNI = {
  enabled: true,
  threshold: 0.7,
  push_per_hour: 6,
  quiet_hours: null,
  feed: "main",
};

function omniFeedItem(id, title, extra = {}) {
  return {
    id,
    ts: 1_779_999_900 + id,
    title,
    body: "",
    priority: 0,
    source_kind: "loop",
    source_ref: `loop:test-${id}`,
    metadata: {},
    ...extra,
  };
}

function writeOmniStateFile(statePath, stateObj) {
  // Write-then-rename so the fixture never reads a half-written file while
  // the bridge feed poller races test-side reads.
  const tmpPath = `${statePath}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(stateObj));
  fs.renameSync(tmpPath, statePath);
}

function readOmniStateFile(statePath) {
  return JSON.parse(fs.readFileSync(statePath, "utf8"));
}

async function withOmniBridge(t, { enabled = true, items = [], options = {} } = {}) {
  const statePath = path.join(
    tmpdir(),
    `morpheus-g2-sim-omni-${process.pid}-${t.name.replace(/[^A-Za-z0-9_-]/g, "_")}.json`,
  );
  writeOmniStateFile(statePath, { omni: { ...FEED_OMNI, enabled }, items, acks: [], contexts: [] });
  process.env.MOCK_MORPHEUS_STATE_FILE = statePath;
  t.after(() => {
    delete process.env.MOCK_MORPHEUS_STATE_FILE;
    fs.rmSync(statePath, { force: true });
  });
  const bridge = await withBridge(t, {
    // Feed tests never prompt Codex; the fake provider keeps the bridge from
    // spawning a real codex app-server (and its reconnect timers) per test.
    createCodexAgentProvider: fakeCodexAgentProvider(),
    ...options,
  });
  t.after(() => {
    // The bridge-side feed poller stops itself without subscribers, but a
    // test that leaves the feed selected would keep it spawning the fixture.
    if (bridge.state.feedPoller) {
      clearInterval(bridge.state.feedPoller);
      bridge.state.feedPoller = null;
    }
  });
  return { ...bridge, statePath };
}

function omniClient(t, baseUrl, options = {}) {
  const client = new G2BridgeClient({
    bridgeUrl: baseUrl,
    token: TOKEN,
    eventSourceFactory: () => null,
    // Tests drive feed refreshes explicitly; no background timer.
    feedPollMs: 0,
    ...options,
  });
  t.after(() => client.close());
  return client;
}

test("connect lands on the ambient feed view when omnipresence is enabled", async (t) => {
  const { baseUrl } = await withOmniBridge(t, {
    items: [
      omniFeedItem(1, "older quiet push"),
      omniFeedItem(2, "espresso beans on promo 50m left", {
        priority: 2,
        body: "Alnatura on Turmstrasse has your usual brand at -20% today.",
        // The real pipeline nests the judge verdict (feeds.py _route_on_threshold).
        metadata: {
          loop_id: 7,
          run_id: 42,
          judge: { score: 0.86, rationale: "out of espresso beans per memory" },
        },
      }),
    ],
  });
  const client = omniClient(t, baseUrl);
  const snapshot = await client.connect();

  assert.equal(snapshot.mode, "feed", "omnipresence-enabled connect lands on the feed");
  assert.deepEqual(
    snapshot.feedItems.map((item) => item.id),
    [2, 1],
    "ascending CLI items render newest first",
  );
  // Full item shape got hydrated from /api/feed, not just the stream text.
  assert.equal(snapshot.feedItems[0].metadata.judge.rationale, "out of espresso beans per memory");
  assert.equal(snapshot.rows.at(-1).id, FEED_NAV_CONVERSATIONS_ID, "conversations stay one tap away");

  const lines = snapshot.glassesText.split("\n");
  const newestLine = lines.find((line) => line.includes("espresso beans on promo"));
  const olderLine = lines.find((line) => line.includes("older quiet push"));
  assert.ok(newestLine && olderLine, "both items render as one line each");
  assert.ok(lines.indexOf(newestLine) < lines.indexOf(olderLine), "newest renders above older");
  assert.match(newestLine, /^>!/, "selected cursor plus priority>0 marker");
  assert.match(olderLine, /^ {2}/, "priority 0 items carry no marker");
});

test("connect keeps the legacy projects landing when omnipresence is disabled", async (t) => {
  const { baseUrl } = await withOmniBridge(t, {
    enabled: false,
    items: [omniFeedItem(1, "hidden push")],
  });
  const client = omniClient(t, baseUrl);
  const snapshot = await client.connect();

  assert.equal(snapshot.mode, "projects", "disabled omnipresence behaves exactly as today");
  assert.ok(snapshot.rows.some((row) => row.id === "project:p_alpha"));
  assert.ok(!snapshot.rows.some((row) => row.id === FEED_SESSION_ID));
  assert.deepEqual(snapshot.feedItems, []);
});

test("forceLegacyView (?omni=0) lands on projects even with omnipresence enabled", async (t) => {
  const { baseUrl } = await withOmniBridge(t, {
    items: [omniFeedItem(1, "push that must not open")],
  });
  const client = omniClient(t, baseUrl, { forceLegacyView: true });
  const snapshot = await client.connect();

  assert.equal(snapshot.mode, "projects");
  // The server-side session list still leads with the feed row; only the
  // client landing is forced to the legacy view.
  assert.equal(snapshot.rows[0].id, FEED_SESSION_ID);
  assert.deepEqual(snapshot.feedItems, []);
});

test("single tap expands the selected push and acks expanded exactly once", async (t) => {
  const { baseUrl, statePath } = await withOmniBridge(t, {
    items: [
      omniFeedItem(1, "promo push", {
        priority: 1,
        body: "Alnatura carries your beans at -20% until closing.",
        // Real nested judge shape (feeds.py _route_on_threshold).
        metadata: { judge: { score: 0.86, rationale: "espresso beans noted in memory" } },
      }),
    ],
  });
  const client = omniClient(t, baseUrl);
  await client.connect();

  const expanded = await client.activateSelected();
  assert.equal(expanded.expandedFeedItemId, 1);
  assert.match(expanded.glassesText, /Alnatura carries your beans at -20% until closing\./);
  assert.match(expanded.glassesText, /why: espresso beans noted in memory \(score 0\.86\)/);

  // Tap again collapses (reversible, no second ack); a third tap re-expands
  // and the client-side guard still keeps it to one ack per item+action.
  const collapsed = await client.activateSelected();
  assert.equal(collapsed.expandedFeedItemId, 0);
  const reexpanded = await client.activateSelected();
  assert.equal(reexpanded.expandedFeedItemId, 1);

  assert.deepEqual(
    readOmniStateFile(statePath).acks,
    [{ item: 1, action: "expanded" }],
    "the expanded ack fires exactly once for the item",
  );
});

test("double tap dismisses items and falls back to navigation with nothing selected", async (t) => {
  const { baseUrl, statePath } = await withOmniBridge(t, {
    items: [omniFeedItem(1, "older push"), omniFeedItem(2, "newest push")],
  });
  const client = omniClient(t, baseUrl);
  await client.connect();

  // Precedence 2: cursor on a feed item (newest, index 0) -> dismiss it.
  const afterFirstDismiss = await client.navigateBack();
  assert.equal(afterFirstDismiss.mode, "feed", "dismiss keeps the feed open");
  assert.deepEqual(afterFirstDismiss.feedItems.map((item) => item.id), [1]);
  assert.doesNotMatch(afterFirstDismiss.glassesText, /newest push/);
  assert.deepEqual(readOmniStateFile(statePath).acks, [{ item: 2, action: "dismissed" }]);

  // Precedence 1: an expanded item is dismissed and collapsed.
  const expanded = await client.activateSelected();
  assert.equal(expanded.expandedFeedItemId, 1);
  const afterSecondDismiss = await client.navigateBack();
  assert.equal(afterSecondDismiss.expandedFeedItemId, 0, "dismiss collapses the expanded item");
  assert.deepEqual(afterSecondDismiss.feedItems, []);
  assert.deepEqual(readOmniStateFile(statePath).acks, [
    { item: 2, action: "dismissed" },
    { item: 1, action: "expanded" },
    { item: 1, action: "dismissed" },
  ]);

  // Precedence 3: no item selected (cursor on the Conversations nav row of an
  // empty feed) -> the OS-conventional back/exit still works.
  assert.equal(afterSecondDismiss.rows[0].id, FEED_NAV_CONVERSATIONS_ID);
  const backedOut = await client.navigateBack();
  assert.equal(backedOut.mode, "projects", "double tap on the feed root exits as before");
});

test("a streamed feed push lands in the ambient list", async (t) => {
  const { baseUrl } = await withOmniBridge(t, { items: [omniFeedItem(1, "seed push")] });
  const streams = [];
  const client = omniClient(t, baseUrl, {
    eventSourceFactory: (url) => new FakeEventSource(url, streams),
  });
  await client.connect();

  assert.equal(streams.length, 1, "opening the feed opens its event stream");
  assert.match(streams[0].url, /sessionId=feed%3Amain/);

  streams[0].emit(901, {
    type: "result",
    success: true,
    provider: "morpheus-feed",
    sessionId: "feed:main",
    text: "Fresh push title\nFresh push body",
    feedItem: { id: 7, ts: 1_780_000_000, priority: 1, sourceKind: "loop", sourceRef: "loop:news" },
  });

  const snapshot = client.snapshot();
  assert.equal(snapshot.feedItems[0].id, 7);
  assert.equal(snapshot.feedItems[0].title, "Fresh push title");
  assert.equal(snapshot.feedItems[0].body, "Fresh push body");
  assert.equal(snapshot.feedItems[0].priority, 1);
  assert.match(snapshot.glassesText, /Fresh push title/);
  assert.ok(
    snapshot.glassesText.indexOf("Fresh push title") < snapshot.glassesText.indexOf("seed push"),
    "the streamed push renders on top",
  );
});

test("a stream copy never degrades the richer /api/feed hydration of the same item", async (t) => {
  // 1500+ char multi-line body: longer than the bridge message path's 1000
  // char body cap, with newlines the stream path collapses away.
  const paragraphs = [];
  for (let i = 0; i < 12; i += 1) {
    paragraphs.push(
      `Paragraph ${i + 1}: ${"the full hydrated body must survive stream replays ".repeat(3).trim()}`,
    );
  }
  const fullBody = paragraphs.join("\n");
  assert.ok(fullBody.length > 1500, "fixture body must exceed the bridge's 1000 char cap");

  const { baseUrl } = await withOmniBridge(t, {
    items: [omniFeedItem(9, "rich hydrated push", { priority: 1, body: fullBody })],
  });
  const streams = [];
  const client = omniClient(t, baseUrl, {
    eventSourceFactory: (url) => new FakeEventSource(url, streams),
  });
  await client.connect();

  // Hydrated from /api/feed (buffered stream copies may have landed first;
  // hydration must win either way).
  assert.equal(client.snapshot().feedItems[0].body, fullBody);

  // Replay the lossy bridge-message copy over SSE: whitespace-collapsed
  // title/body, body capped at 1000 chars. It must not clobber hydration.
  const collapsedBody = fullBody.replace(/\s+/g, " ").slice(0, 1000);
  streams[0].emit(902, {
    type: "result",
    success: true,
    provider: "morpheus-feed",
    sessionId: "feed:main",
    text: `rich hydrated push\n${collapsedBody}`,
    feedItem: { id: 9, ts: 1_779_999_909, priority: 1, sourceKind: "loop", sourceRef: "loop:test-9" },
  });

  const snapshot = client.snapshot();
  assert.equal(snapshot.feedItems.length, 1, "the stream copy merges into the existing item");
  assert.equal(
    snapshot.feedItems[0].body,
    fullBody,
    "the stream-derived capped body must not replace the hydrated body",
  );

  // The expanded view still renders from the full multi-line body: the last
  // paragraph (beyond the 1000-char cap) is reachable by paging.
  const expanded = await client.activateSelected();
  assert.equal(expanded.expandedFeedItemId, 9);
  let pagedText = "";
  for (let guard = 0; guard < 40; guard += 1) {
    pagedText += `\n${client.snapshot().glassesText}`;
    const before = client.snapshot().feedExpandedPage;
    client.move(1);
    if (client.snapshot().feedExpandedPage === before) break;
  }
  assert.match(pagedText, /Paragraph 12:/, "content past the stream cap stays reachable");
});

test("manual location control posts valid bodies and rejects bad input client-side", async (t) => {
  const { baseUrl, statePath } = await withOmniBridge(t, { items: [] });
  const contextCalls = [];
  const client = omniClient(t, baseUrl, {
    fetchImpl: async (url, options) => {
      if (new URL(url).pathname === "/api/context") contextCalls.push(url);
      return fetch(url, options);
    },
  });

  await assert.rejects(client.sendLocation({}), /lat must be a number between -90 and 90/);
  await assert.rejects(client.sendLocation({ lat: "", lon: 11.5 }), /lat must be a number/);
  await assert.rejects(
    client.sendLocation({ lat: Number.parseFloat(""), lon: 11.5 }),
    /lat must be a number/,
  );
  await assert.rejects(client.sendLocation({ lat: 91, lon: 11.5 }), /lat must be a number/);
  await assert.rejects(
    client.sendLocation({ lat: 48.1, lon: 200 }),
    /lon must be a number between -180 and 180/,
  );
  assert.deepEqual(contextCalls, [], "invalid input never reaches the bridge");
  assert.deepEqual(readOmniStateFile(statePath).contexts, []);

  const posted = await client.sendLocation({
    lat: 48.137154,
    lon: 11.576124,
    accuracy: 12.5,
    ts: 1_779_999_999.5,
  });
  assert.equal(posted.ok, true);
  const contexts = readOmniStateFile(statePath).contexts;
  assert.equal(contexts.length, 1);
  assert.deepEqual(contexts[0].data, {
    lat: 48.137154,
    lon: 11.576124,
    accuracy: 12.5,
    ts: 1_779_999_999.5,
  });

  // Even courier dedupe: a fix that moved <25m is skipped, >=25m posts again.
  const deduped = await client.sendLocation(
    { lat: 48.137154 + 0.0001, lon: 11.576124 },
    { dedupeMeters: 25 },
  );
  assert.equal(deduped, null, "sub-25m moves are deduped away");
  assert.equal(readOmniStateFile(statePath).contexts.length, 1);

  const moved = await client.sendLocation(
    { lat: 48.137154 + 0.0004, lon: 11.576124 },
    { dedupeMeters: 25 },
  );
  assert.equal(moved.ok, true);
  assert.equal(readOmniStateFile(statePath).contexts.length, 2);
});

test("sendLocation normalizes millisecond epochs to seconds in the POST body", async (t) => {
  const { baseUrl, statePath } = await withOmniBridge(t, { items: [] });
  const client = omniClient(t, baseUrl);

  // JS SDKs (Date.now(), Even location fixes) report millisecond epochs; a
  // raw ms value stored as a seconds epoch (~year 57468) defeats staleness
  // checks downstream.
  const posted = await client.sendLocation({
    lat: 48.137154,
    lon: 11.576124,
    ts: 1_779_999_999_500,
  });
  assert.equal(posted.ok, true);
  let contexts = readOmniStateFile(statePath).contexts;
  assert.equal(contexts.length, 1);
  assert.equal(contexts[0].data.ts, 1_779_999_999.5, "ms epoch converts to a seconds epoch");

  // Seconds epochs pass through untouched.
  const secondsPosted = await client.sendLocation({ lat: 48.2, lon: 11.7, ts: 1_779_999_998 });
  assert.equal(secondsPosted.ok, true);
  contexts = readOmniStateFile(statePath).contexts;
  assert.equal(contexts.length, 2);
  assert.equal(contexts[1].data.ts, 1_779_999_998);
});
