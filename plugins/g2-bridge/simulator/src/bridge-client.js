const DEFAULT_BRIDGE_URL = "http://127.0.0.1:3456";
const PROJECT_PREFIX = "project:";
const PROJECT_SESSION_PREFIX = "project-session:";
const NAV_PROJECTS_ID = "nav:projects";

function trimTrailingSlash(value) {
  return String(value || "").replace(/\/+$/, "");
}

function requestId(prefix) {
  const random =
    globalThis.crypto?.randomUUID?.() ||
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${random}`;
}

function isProjectRow(id) {
  return typeof id === "string" && id.startsWith(PROJECT_PREFIX);
}

function projectIdFromRow(id) {
  return isProjectRow(id) ? id.slice(PROJECT_PREFIX.length) : "";
}

function coerceArray(value) {
  return Array.isArray(value) ? value : [];
}

function normalizeMessage(message, fallback = {}) {
  const id = Number(message?.id || fallback.id || 0);
  return {
    ...message,
    id,
    at: message?.at || fallback.at || new Date().toISOString(),
  };
}

function textFromMessage(message) {
  if (!message) return "";
  if (message.text) return String(message.text);
  if (message.answer) return String(message.answer);
  if (message.response) return String(message.response);
  if (message.output?.text) return String(message.output.text);
  if (message.type === "selected_project") return `Selected project ${message.projectId || ""}`.trim();
  if (message.type === "selected_session") return `Selected session ${message.sessionId || ""}`.trim();
  if (message.type === "session_started") return "Session started";
  if (message.type === "prompt_submitted") return `Transcript sent (${message.textChars || 0} chars)`;
  if (message.type === "status") return `Status: ${message.state || "unknown"}`;
  if (message.type === "error") return String(message.error || "Bridge error");
  return "";
}

function roleFromMessage(message) {
  if (message?.role) return message.role;
  if (message?.type === "user_prompt" || message?.type === "prompt_submitted") return "user";
  if (message?.type === "result" || message?.type === "text_delta") return "assistant";
  return "system";
}

function lastMessageId(messages) {
  return coerceArray(messages).reduce((max, message) => Math.max(max, Number(message?.id || 0)), 0);
}

function historyToMessages(history) {
  return coerceArray(history).map((entry, index) =>
    normalizeMessage(
      {
        type: entry.role === "user" ? "user_prompt" : "result",
        role: entry.role || "assistant",
        text: entry.text || "",
      },
      { id: -(index + 1) },
    ),
  );
}

function truncateLine(value, max = 76) {
  const clean = String(value || "").replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  return `${clean.slice(0, Math.max(0, max - 3))}...`;
}

export function buildGlassesText(state) {
  const lines = [];
  const mode = state.mode || "projects";
  const projectName = state.selectedProject?.name || state.selectedProject?.id || "";
  const sessionTitle = state.selectedSession?.title || state.selectedSession?.id || "";
  const status = state.status || state.selectedSession?.status || "";

  lines.push("MORPHEUS");
  lines.push(
    [
      mode.toUpperCase(),
      projectName ? `project ${projectName}` : "",
      status ? `status ${status}` : "",
    ]
      .filter(Boolean)
      .join(" / "),
  );

  if (state.error) {
    lines.push("");
    lines.push(`ERROR ${truncateLine(state.error, 70)}`);
  }

  if (mode === "session") {
    lines.push("");
    if (sessionTitle) lines.push(truncateLine(sessionTitle, 76));
    const visibleMessages = coerceArray(state.messages).slice(-8);
    for (const message of visibleMessages) {
      const text = textFromMessage(message);
      if (!text) continue;
      const role = roleFromMessage(message);
      const prefix = role === "user" ? "YOU" : role === "assistant" ? "AI" : "SYS";
      lines.push(`${prefix}: ${truncateLine(text, 72)}`);
    }
    if (!visibleMessages.length && !state.error) {
      lines.push("Waiting for stream...");
    }
  } else {
    lines.push("");
    const rows = coerceArray(state.rows);
    if (!rows.length) {
      lines.push(mode === "projects" ? "No Morpheus projects." : "No sessions yet.");
    }
    rows.slice(0, 9).forEach((row, index) => {
      const cursor = index === state.selectedIndex ? ">" : " ";
      const title = row.title || row.name || row.id || "Untitled";
      const rowStatus = row.status ? ` ${row.status}` : "";
      lines.push(`${cursor} ${truncateLine(`${title}${rowStatus}`, 72)}`);
    });
  }

  return lines.filter((line) => line !== undefined).join("\n").slice(0, 1800);
}

export class G2BridgeClient {
  constructor(options = {}) {
    this.bridgeUrl = trimTrailingSlash(options.bridgeUrl || DEFAULT_BRIDGE_URL);
    this.token = options.token || "";
    this.fetchImpl = options.fetchImpl || globalThis.fetch?.bind(globalThis);
    this.eventSourceFactory =
      options.eventSourceFactory ||
      ((url) => (globalThis.EventSource ? new globalThis.EventSource(url) : null));
    this.onChange = options.onChange || (() => {});
    this.onLog = options.onLog || (() => {});
    this.eventSource = null;
    this.state = {
      info: null,
      mode: "projects",
      rows: [],
      selectedIndex: 0,
      selectedProject: null,
      selectedSession: null,
      displaySessionId: "",
      activeSessionId: "",
      messages: [],
      status: "idle",
      error: "",
    };
  }

  configure({ bridgeUrl, token } = {}) {
    if (bridgeUrl) this.bridgeUrl = trimTrailingSlash(bridgeUrl);
    if (token !== undefined) this.token = token;
    this.emit();
  }

  close() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }

  snapshot() {
    return {
      ...this.state,
      glassesText: buildGlassesText(this.state),
    };
  }

  emit() {
    this.onChange(this.snapshot());
  }

  log(message) {
    this.onLog(String(message));
  }

  requireFetch() {
    if (!this.fetchImpl) throw new Error("fetch is not available in this runtime");
  }

  authHeaders(extra = {}) {
    if (!this.token) throw new Error("Missing bridge token");
    return {
      Authorization: `Bearer ${this.token}`,
      ...extra,
    };
  }

  async api(path, options = {}) {
    this.requireFetch();
    const method = options.method || (options.body ? "POST" : "GET");
    const headers = this.authHeaders(options.body ? { "Content-Type": "application/json" } : {});
    const response = await this.fetchImpl(`${this.bridgeUrl}${path}`, {
      method,
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    const contentType = response.headers?.get?.("content-type") || "";
    const body = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const error = typeof body === "object" ? body.error || body.code || JSON.stringify(body) : body;
      throw new Error(error || `${method} ${path} failed with ${response.status}`);
    }
    return body;
  }

  async connect() {
    const info = await this.api("/api/info");
    this.state = {
      ...this.state,
      info,
      error: "",
    };
    this.log(`Connected to ${this.bridgeUrl}`);
    await this.refreshProjects();
    return this.snapshot();
  }

  async refreshProjects() {
    const body = await this.api("/api/sessions?view=projects");
    this.close();
    this.state = {
      ...this.state,
      mode: "projects",
      rows: coerceArray(body.sessions),
      selectedIndex: 0,
      selectedProject: body.selectedProject || null,
      selectedSession: null,
      displaySessionId: "",
      activeSessionId: "",
      messages: [],
      status: "idle",
      error: "",
    };
    this.emit();
    return this.snapshot();
  }

  async refreshSessions() {
    const body = await this.api("/api/sessions");
    const rows = coerceArray(body.sessions);
    const firstSelectable = Math.min(rows.length - 1, rows.findIndex((row) => row.id !== NAV_PROJECTS_ID));
    this.state = {
      ...this.state,
      mode: body.mode || "sessions",
      rows,
      selectedIndex: firstSelectable >= 0 ? firstSelectable : 0,
      selectedProject: body.selectedProject || this.state.selectedProject,
      selectedSession: body.selectedSession || null,
      status: "idle",
      error: "",
    };
    this.emit();
    return this.snapshot();
  }

  selectedRow() {
    return coerceArray(this.state.rows)[this.state.selectedIndex] || null;
  }

  move(delta) {
    const rows = coerceArray(this.state.rows);
    if (!rows.length) return this.snapshot();
    const next = (this.state.selectedIndex + delta + rows.length) % rows.length;
    this.state = { ...this.state, selectedIndex: next, error: "" };
    this.emit();
    return this.snapshot();
  }

  async activateSelected() {
    const row = this.selectedRow();
    if (!row?.id) return this.snapshot();
    if (row.id === NAV_PROJECTS_ID) return this.navigateBack();
    if (isProjectRow(row.id)) return this.selectProject(projectIdFromRow(row.id));
    return this.selectSession(row.id);
  }

  async selectProject(projectId) {
    const body = await this.api("/api/select-project", {
      body: {
        projectId,
        clientRequestId: requestId("sim-select-project"),
      },
    });
    this.state = {
      ...this.state,
      selectedProject: body.selectedProject || this.state.selectedProject,
      selectedSession: null,
      displaySessionId: `${PROJECT_PREFIX}${projectId}`,
      activeSessionId: "",
      messages: [],
      mode: "sessions",
      error: "",
    };
    this.emit();
    await this.refreshSessions();
    return this.snapshot();
  }

  async selectSession(sessionId) {
    const body = await this.api("/api/select-session", {
      body: {
        sessionId,
        clientRequestId: requestId("sim-select-session"),
      },
    });
    const selected = body.selectedSession || null;
    const displaySessionId = selected?.id || body.projectActiveSessionId || sessionId;
    const activeSessionId = body.activeSessionId || selected?.activeSessionId || selected?.realSessionId || sessionId;
    this.state = {
      ...this.state,
      mode: "session",
      selectedProject: body.selectedProject || this.state.selectedProject,
      selectedSession: selected || this.state.selectedSession,
      displaySessionId,
      activeSessionId,
      messages: [],
      status: selected?.status || "idle",
      error: "",
    };
    this.openEventStream(displaySessionId);
    await this.loadHistory(displaySessionId);
    await this.refreshMessages(displaySessionId);
    return this.snapshot();
  }

  async navigateBack() {
    const body = await this.api("/api/back", {
      body: { clientRequestId: requestId("sim-back") },
    });
    this.state = {
      ...this.state,
      selectedProject: body.selectedProject || null,
      selectedSession: body.selectedSession || null,
      displaySessionId: "",
      activeSessionId: "",
      messages: [],
      mode: body.to || "projects",
      error: "",
    };
    this.close();
    this.emit();
    if (body.to === "sessions") return this.refreshSessions();
    return this.refreshProjects();
  }

  async submitTranscript(text) {
    const clean = String(text || "").trim();
    if (!clean) return this.snapshot();
    const body = await this.api("/api/transcript/finalize", {
      body: {
        text: clean,
        sessionId: this.state.displaySessionId || undefined,
        clientRequestId: requestId("sim-transcript"),
      },
    });
    const displaySessionId =
      body.displaySessionId ||
      body.projectActiveSessionId ||
      body.selectedSession?.projectActiveSessionId ||
      body.selectedSession?.id ||
      this.state.displaySessionId ||
      body.sessionId;
    const activeSessionId =
      body.activeSessionId ||
      body.selectedSession?.activeSessionId ||
      body.selectedSession?.realSessionId ||
      body.sessionId ||
      this.state.activeSessionId;
    const responseMessages = coerceArray(body.messages).length
      ? body.messages
      : coerceArray(body.activeMessages);
    this.state = {
      ...this.state,
      mode: "session",
      selectedProject: body.selectedProject || this.state.selectedProject,
      selectedSession: body.selectedSession || this.state.selectedSession,
      displaySessionId,
      activeSessionId,
      messages: responseMessages.map((message) => normalizeMessage(message)),
      status: body.state || "busy",
      error: "",
    };
    this.openEventStream(displaySessionId);
    await this.refreshMessages(displaySessionId);
    this.emit();
    return this.snapshot();
  }

  async loadHistory(sessionId = this.state.displaySessionId) {
    if (!sessionId) return [];
    const body = await this.api(`/api/sessions/${encodeURIComponent(sessionId)}/history?limit=10`);
    const historyMessages = historyToMessages(body.history);
    if (historyMessages.length) {
      this.state = {
        ...this.state,
        messages: historyMessages,
        selectedProject: body.selectedProject || this.state.selectedProject,
        selectedSession: body.selectedSession || this.state.selectedSession,
      };
      this.emit();
    }
    return historyMessages;
  }

  async refreshMessages(sessionId = this.state.displaySessionId) {
    if (!sessionId) return [];
    const after = lastMessageId(this.state.messages);
    const body = await this.api(
      `/api/messages?sessionId=${encodeURIComponent(sessionId)}&after=${encodeURIComponent(after)}`,
    );
    const messages = coerceArray(body.messages).map((message) => normalizeMessage(message));
    if (messages.length || body.state) {
      this.state = {
        ...this.state,
        messages: this.mergeMessages(messages),
        status: body.state || this.state.status,
        activeSessionId: body.activeSessionId || this.state.activeSessionId,
        displaySessionId: body.projectActiveSessionId || this.state.displaySessionId,
        error: "",
      };
      this.emit();
    }
    return messages;
  }

  mergeMessages(messages) {
    const byKey = new Map();
    for (const message of coerceArray(this.state.messages)) {
      byKey.set(`${message.id}:${message.type}:${textFromMessage(message)}`, message);
    }
    for (const message of coerceArray(messages)) {
      byKey.set(`${message.id}:${message.type}:${textFromMessage(message)}`, message);
    }
    return [...byKey.values()].slice(-30);
  }

  openEventStream(sessionId = this.state.displaySessionId) {
    if (!sessionId || !this.token) return null;
    this.close();
    const url = `${this.bridgeUrl}/api/events?sessionId=${encodeURIComponent(sessionId)}&token=${encodeURIComponent(this.token)}`;
    const source = this.eventSourceFactory(url);
    if (!source) {
      this.log("EventSource unavailable; using message polling only");
      return null;
    }
    source.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data);
        const message = normalizeMessage(parsed, { id: Number(event.lastEventId || 0) });
        this.state = {
          ...this.state,
          messages: this.mergeMessages([message]),
          status: parsed.state || this.state.status,
        };
        this.emit();
      } catch (err) {
        this.log(`Could not parse event stream message: ${err.message}`);
      }
    };
    source.onerror = () => {
      this.log("Event stream disconnected");
    };
    this.eventSource = source;
    this.log(`Streaming ${sessionId}`);
    return source;
  }

  async waitForText(pattern, options = {}) {
    const timeoutMs = options.timeoutMs || 3000;
    const intervalMs = options.intervalMs || 100;
    const matcher = typeof pattern === "string" ? (text) => text.includes(pattern) : (text) => pattern.test(text);
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const text = buildGlassesText(this.state);
      if (matcher(text)) return this.snapshot();
      await this.refreshMessages().catch((err) => {
        this.state = { ...this.state, error: err.message };
        this.emit();
      });
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    throw new Error(`Timed out waiting for ${String(pattern)}`);
  }
}

export { DEFAULT_BRIDGE_URL, NAV_PROJECTS_ID, PROJECT_PREFIX, PROJECT_SESSION_PREFIX, textFromMessage };
