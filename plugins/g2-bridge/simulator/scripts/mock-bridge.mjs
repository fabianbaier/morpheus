#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createBridge } from "../../src/server.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = path.resolve(here, "../../test/fixtures/mock-morpheus.mjs");
const host = process.env.HOST || "127.0.0.1";
const port = Number.parseInt(process.env.PORT || "3456", 10);
const token = process.env.MORPHEUS_G2_TOKEN || "dev-token";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function fakeCodexAgentProvider({ resultDelayMs = 650 } = {}) {
  const sessions = [];
  const history = new Map();
  let nextId = 1;

  function append(id, role, text) {
    const entries = history.get(id) || [];
    entries.push({ role, text });
    history.set(id, entries.slice(-20));
  }

  return (emit) => ({
    async getInfo() {
      return { provider: "codex", model: "Mock Codex", version: "simulator" };
    },

    async listSessions(_limit, cwd) {
      return sessions.filter((session) => !cwd || session.cwd === cwd);
    },

    getStatus(sessionId) {
      const session = sessions.find((item) => item.id === sessionId);
      return session ? { state: session.status || "idle", provider: "codex" } : null;
    },

    async getSessionStatus(sessionId) {
      return sessions.find((item) => item.id === sessionId)?.status || "idle";
    },

    async getHistory(sessionId, limit = 10) {
      return (history.get(sessionId) || []).slice(-Math.max(1, limit));
    },

    async prompt(sessionId, text, cwd) {
      const id = sessionId || `mock-codex-${nextId++}`;
      let session = sessions.find((item) => item.id === id);
      if (!session) {
        session = {
          id,
          title: `G2: ${String(text).slice(0, 48)}`,
          timestamp: new Date().toISOString(),
          cwd,
          status: "busy",
        };
        sessions.unshift(session);
      }
      session.status = "busy";
      append(id, "user", text);
      emit(id, { type: "user_prompt", text, provider: "codex", sessionId: id });
      emit(id, { type: "status", state: "busy", provider: "codex", sessionId: id });
      const answer = `Mock Morpheus answer for: ${text}. Session ${id} is streaming correctly.`;
      const chunks = answer.match(/.{1,24}(\s|$)/g) || [answer];
      void (async () => {
        await sleep(resultDelayMs);
        for (const chunk of chunks) {
          emit(id, { type: "text_delta", text: chunk, provider: "codex", sessionId: id });
          await sleep(90);
        }
        append(id, "assistant", answer);
        emit(id, { type: "result", success: true, text: answer, provider: "codex", sessionId: id });
        session.status = "idle";
        emit(id, { type: "status", state: "idle", provider: "codex", sessionId: id });
      })();
      return { sessionId: id, provider: "codex" };
    },
  });
}

fs.chmodSync(fixture, 0o755);

const bridge = createBridge({
  token,
  host,
  port,
  localUrl: `http://${host}:${port}`,
  publicUrl: `http://${host}:${port}`,
  morpheusBin: fixture,
  allowedOrigins: ["http://127.0.0.1:5173", "http://localhost:5173"],
  agentBackend: "codex_app_server",
  createCodexAgentProvider: fakeCodexAgentProvider(),
  mirrorCodexTui: false,
  showProjectsFirst: true,
  waitForPromptResult: false,
  auditPath: "",
});

const sockets = new Set();
const server = bridge.app.listen(port, host, () => {
  console.log(`Mock Morpheus G2 bridge: http://${host}:${port}`);
  console.log(`Token: ${token}`);
  console.log(`Simulator URL: http://127.0.0.1:5173/?bridge=http://${host}:${port}&token=${token}`);
});

server.on("connection", (socket) => {
  sockets.add(socket);
  socket.on("close", () => sockets.delete(socket));
});

let shuttingDown = false;
function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const socket of sockets) socket.destroy();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 1000).unref();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
