const DEFAULT_BRIDGE_URL = "http://127.0.0.1:3456";
const PROJECT_PREFIX = "project:";
const PROJECT_SESSION_PREFIX = "project-session:";
const NAV_PROJECTS_ID = "project:__projects__";
const LEGACY_NAV_PROJECTS_ID = "nav:projects";
// Omnipresence feed pseudo-session (bridge PRD §3.1): the bridge exposes the
// feed as session row `feed:main`, so the feed view reuses the exact
// select-session / history / SSE machinery a concrete session uses.
const FEED_SESSION_ID = "feed:main";
// Local navigation row appended to the feed list so conversations stay one
// tap away from the ambient feed (PRD §2). Never sent to the bridge.
const FEED_NAV_CONVERSATIONS_ID = "feed:nav:conversations";
const FEED_ITEM_ROW_PREFIX = "feed-item:";
// SSE-aware feed poll cadence: coarse while the event stream delivers items,
// tighter only when no stream is open (mirrors waitForResultViaMessages).
const DEFAULT_FEED_POLL_MS = 3000;
const DEFAULT_FEED_STREAM_POLL_MS = 20000;
// Even location courier dedupe radius (PRD §3.2): skip fixes that moved less
// than this since the last posted fix.
const LOCATION_DEDUPE_METERS = 25;

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

function outputTextFromBody(body) {
  if (!body || typeof body !== "object") return "";
  if (body.text) return String(body.text);
  if (body.answer) return String(body.answer);
  if (body.message) return String(body.message);
  if (body.response) return String(body.response);
  if (body.output?.text) return String(body.output.text);
  if (body.selectedSession?.latestOutput) return String(body.selectedSession.latestOutput);
  return "";
}

function messagesFromResponseBody(body) {
  const messages = coerceArray(body?.messages).map((message) => normalizeMessage(message));
  if (messages.length) return messages;

  const historyMessages = historyToMessages(body?.history || body?.selectedSession?.history);
  if (historyMessages.length) return historyMessages;

  const text = outputTextFromBody(body);
  return text
    ? [
        normalizeMessage(
          {
            type: "result",
            role: "assistant",
            text,
          },
          { id: 0 },
        ),
      ]
    : [];
}

// Shared fallback chain for the session ids carried by bridge responses
// (/api/sessions, /api/transcript/finalize). body.sessionId is only present on
// finalize responses; /api/sessions never sets it, so including it in the
// superset is safe for both endpoints. selectSession intentionally uses a
// different chain (the just-requested sessionId, not prior state, is the
// fallback there) and does not go through this helper.
function sessionIdsFromResponse(body, state) {
  const selected = body?.selectedSession || null;
  return {
    displaySessionId:
      body?.displaySessionId ||
      body?.projectActiveSessionId ||
      selected?.projectActiveSessionId ||
      selected?.id ||
      state.displaySessionId ||
      body?.sessionId ||
      "",
    activeSessionId:
      body?.activeSessionId ||
      selected?.activeSessionId ||
      selected?.realSessionId ||
      body?.sessionId ||
      state.activeSessionId ||
      "",
  };
}

// Fabricated messages (history rows, optimistic local prompts, latest-output
// snapshots) never carry a real server id; they get id <= 0 so they can never
// advance the /api/messages cursor and dedupe purely by role + content.
function isServerMessage(message) {
  return Number(message?.id || 0) > 0;
}

function messageContentKey(message) {
  return `${roleFromMessage(message)}:${textFromMessage(message)}`;
}

function truncateLine(value, max = 76) {
  const clean = String(value || "").replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  return `${clean.slice(0, Math.max(0, max - 3))}...`;
}

// --- Omnipresence feed helpers ---

// Full-shape feed item from GET /api/feed (snake_case) or an already
// normalized local item (camelCase). Returns null for anything without a
// positive integer id — those can never be acked or deduped.
function feedItemFromRaw(raw) {
  const id = Number(raw?.id || 0);
  if (!Number.isInteger(id) || id <= 0) return null;
  const metadata = raw?.metadata && typeof raw.metadata === "object" ? raw.metadata : {};
  return {
    id,
    ts: Number(raw?.ts || 0),
    title: String(raw?.title || ""),
    body: String(raw?.body || ""),
    priority: Number(raw?.priority || 0),
    sourceKind: String(raw?.source_kind ?? raw?.sourceKind ?? ""),
    sourceRef: String(raw?.source_ref ?? raw?.sourceRef ?? ""),
    metadata,
    // /api/feed serves the full item shape; hydrated fields are authoritative
    // over the lossy stream copy (see mergeFeedItem).
    hydrated: true,
  };
}

// Feed item derived from a streamed/buffered feed:main message. The bridge
// attaches `feedItem` metadata ({id, ts, priority, sourceKind, sourceRef}) to
// each published feed message and renders `title\nbody` as the text.
function feedItemFromMessage(message) {
  const meta = message?.feedItem;
  const id = Number(meta?.id || 0);
  if (!Number.isInteger(id) || id <= 0) return null;
  const text = String(message?.text || "");
  const newline = text.indexOf("\n");
  return {
    id,
    ts: Number(meta?.ts || 0),
    title: newline >= 0 ? text.slice(0, newline) : text,
    body: newline >= 0 ? text.slice(newline + 1) : "",
    priority: Number(meta?.priority || 0),
    sourceKind: String(meta?.sourceKind || ""),
    sourceRef: String(meta?.sourceRef || ""),
    metadata: {},
    // The bridge message path whitespace-collapses the title and caps the
    // body, so stream-derived text is lossy and must never win a merge
    // against hydrated /api/feed text.
    hydrated: false,
  };
}

// Picks the richer of two text fields when merging feed item copies. A
// hydrated (/api/feed) incoming value is authoritative; a stream-derived one
// only replaces text it strictly enriches — never a shorter copy, never a
// whitespace-collapsed copy of a multi-line field.
function pickRicherFeedText(existing, incoming, incomingHydrated) {
  if (!incoming) return existing;
  if (!existing) return incoming;
  if (incomingHydrated) return incoming;
  if (incoming.length < existing.length) return existing;
  if (existing.includes("\n") && !incoming.includes("\n")) return existing;
  return incoming;
}

// Richer fields win: a stream-derived item (no metadata, whitespace-collapsed
// title, body capped by the bridge message path) must never degrade the
// full-shape item hydrated from /api/feed — regardless of arrival order.
function mergeFeedItem(existing, incoming) {
  const incomingHydrated = incoming.hydrated === true;
  return {
    ...existing,
    ...incoming,
    hydrated: existing.hydrated === true || incomingHydrated,
    title: pickRicherFeedText(existing.title, incoming.title, incomingHydrated),
    body: pickRicherFeedText(existing.body, incoming.body, incomingHydrated),
    priority: incoming.priority || existing.priority,
    ts: incoming.ts || existing.ts,
    sourceKind: incoming.sourceKind || existing.sourceKind,
    sourceRef: incoming.sourceRef || existing.sourceRef,
    metadata: Object.keys(incoming.metadata || {}).length ? incoming.metadata : existing.metadata,
  };
}

function isFeedItemRowId(id) {
  return typeof id === "string" && id.startsWith(FEED_ITEM_ROW_PREFIX);
}

function feedItemRow(item) {
  return {
    id: `${FEED_ITEM_ROW_PREFIX}${item.id}`,
    feedItemId: item.id,
    title: item.title || "(untitled push)",
    priority: Number(item.priority || 0),
    status: "",
  };
}

function feedNavRow() {
  return {
    id: FEED_NAV_CONVERSATIONS_ID,
    title: "Conversations",
    priority: 0,
    status: "",
  };
}

// Word-wraps a feed item into display lines for the expanded view.
function wrapFeedText(value, max = 72) {
  const lines = [];
  for (const paragraph of String(value || "").split("\n")) {
    const words = paragraph.replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
    if (!words.length) continue;
    let line = "";
    for (const word of words) {
      if (line && line.length + 1 + word.length > max) {
        lines.push(line);
        line = word;
      } else {
        line = line ? `${line} ${word}` : word;
      }
    }
    if (line) lines.push(line);
  }
  return lines;
}

const FEED_EXPANDED_PAGE_LINES = 7;

function expandedFeedLines(item) {
  const lines = [];
  const marker = Number(item.priority || 0) > 0 ? "[!] " : "";
  lines.push(...wrapFeedText(`${marker}${item.title || "(untitled push)"}`));
  const bodyLines = wrapFeedText(item.body);
  if (bodyLines.length) {
    lines.push("");
    lines.push(...bodyLines);
  }
  // Judge metadata (PRD §3.5): every push can answer "why did you show me
  // this" — surface the rationale (and score) when the judge wrote them.
  // The real pipeline nests the verdict under metadata.judge (feeds.py
  // _route_on_threshold); the flat shape is kept as a legacy fallback.
  const judge =
    item.metadata?.judge && typeof item.metadata.judge === "object" ? item.metadata.judge : null;
  const rationale = judge?.rationale ?? item.metadata?.rationale;
  if (rationale) {
    lines.push("");
    const score = Number(judge?.score ?? item.metadata?.score);
    const scoreSuffix = Number.isFinite(score) ? ` (score ${score})` : "";
    lines.push(...wrapFeedText(`why: ${rationale}${scoreSuffix}`));
  }
  return lines;
}

function feedExpandedPageCount(item) {
  return Math.max(1, Math.ceil(expandedFeedLines(item).length / FEED_EXPANDED_PAGE_LINES));
}

// Epoch normalization: JS SDKs (Date.now(), Even location fixes) report
// millisecond epochs while the bridge/CLI contract is seconds. Any value
// above 1e12 (~year 33658 as seconds) can only be milliseconds — convert it;
// seconds epochs pass through untouched.
export function normalizeEpochSeconds(value) {
  const ts = Number(value);
  if (!Number.isFinite(ts)) return ts;
  return ts > 1e12 ? ts / 1000 : ts;
}

// Haversine distance in meters, for the <25m location dedupe.
function distanceMeters(a, b) {
  const toRad = (deg) => (deg * Math.PI) / 180;
  const earthRadius = 6371000;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const sinLat = Math.sin(dLat / 2);
  const sinLon = Math.sin(dLon / 2);
  const h = sinLat * sinLat + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * sinLon * sinLon;
  return 2 * earthRadius * Math.asin(Math.min(1, Math.sqrt(h)));
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

  if (mode === "feed") {
    lines.push("");
    const items = coerceArray(state.feedItems);
    const expanded = items.find((item) => item.id === state.expandedFeedItemId) || null;
    if (expanded) {
      const content = expandedFeedLines(expanded);
      const pages = Math.max(1, Math.ceil(content.length / FEED_EXPANDED_PAGE_LINES));
      const page = Math.min(Math.max(0, Number(state.feedExpandedPage || 0)), pages - 1);
      lines.push(...content.slice(page * FEED_EXPANDED_PAGE_LINES, (page + 1) * FEED_EXPANDED_PAGE_LINES));
      if (pages > 1) lines.push(`-- page ${page + 1}/${pages} (scroll) --`);
      lines.push("tap: collapse / double-tap: dismiss");
    } else {
      if (!items.length) lines.push("Feed is quiet. New pushes appear here.");
      // Ambient list, newest first: one line per item, priority > 0 marked.
      coerceArray(state.rows)
        .slice(0, 9)
        .forEach((row, index) => {
          const cursor = index === state.selectedIndex ? ">" : " ";
          const marker = Number(row.priority || 0) > 0 ? "!" : " ";
          lines.push(`${cursor}${marker}${truncateLine(row.title || "", 70)}`);
        });
    }
  } else if (mode === "session") {
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

function stockClientTitle(state) {
  const mode = state.mode || "projects";
  if (state.selectedSession?.title) return state.selectedSession.title;
  if (mode === "session") return "Session";
  if (state.selectedProject?.name || state.selectedProject?.id) {
    return state.selectedProject.name || state.selectedProject.id;
  }
  return "Sessions";
}

function stockRowLabel(row) {
  const title = row?.title || row?.name || row?.id || "Untitled";
  if (row?.id === NAV_PROJECTS_ID || row?.id === LEGACY_NAV_PROJECTS_ID) return title;
  return title.startsWith("/") ? title : `/ ${title}`;
}

function stockMessageLabel(message) {
  const text = textFromMessage(message);
  if (!text) return "";
  const role = roleFromMessage(message);
  if (role === "user") return `/ you ${truncateLine(text, 84)}`;
  if (role === "assistant") return `/ codex ${truncateLine(text, 80)}`;
  return `/ ${truncateLine(text, 88)}`;
}

export function buildEvenClientModel(state) {
  const mode = state.mode || "projects";
  const rows = coerceArray(state.rows).slice(0, 8).map((row, index) => ({
    id: row?.id || "",
    label: stockRowLabel(row),
    status: row?.status || "",
    selected: index === state.selectedIndex,
  }));
  const messages = coerceArray(state.messages)
    .slice(-7)
    .map((message) => stockMessageLabel(message))
    .filter(Boolean);

  return {
    mode,
    title: stockClientTitle(state),
    addSessionLabel: "+ Add session",
    rows,
    messages,
    status: state.status || state.selectedSession?.status || "idle",
    error: state.error || "",
  };
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
    // Server-side /api/messages cursor. Tracked apart from display messages so
    // fabricated history/local ids can never reset or advance it.
    this.serverCursor = 0;
    this.localMessageSeq = 0;
    // Background wait for the reply to the last submitted transcript. Kept as
    // a handle so tests (and callers that care) can await it; bumping the
    // epoch cancels a stale wait when a newer submit or reconfigure wins.
    this.pendingResultWait = null;
    this.resultWaitEpoch = 0;
    // Omnipresence feed state. forceLegacyView (the simulator's `?omni=0`
    // URL param) keeps the legacy projects landing even when the bridge
    // reports omnipresence enabled.
    this.forceLegacyView = Boolean(options.forceLegacyView);
    this.feedPollMs = options.feedPollMs !== undefined ? Number(options.feedPollMs) : DEFAULT_FEED_POLL_MS;
    this.feedStreamPollMs =
      options.feedStreamPollMs !== undefined ? Number(options.feedStreamPollMs) : DEFAULT_FEED_STREAM_POLL_MS;
    // Highest feed item id ever seen (from /api/feed and streamed feedItem
    // metadata); /api/feed refreshes page with `after` on this cursor.
    this.feedCursor = 0;
    // Client-side ack guard: one ack per item+action, reserved before the
    // request is sent so rapid duplicate gestures can never double-fire.
    this.feedAcks = new Set();
    // Dismissed ids never re-enter the local list (SSE replays and after=0
    // hydration would otherwise resurrect them).
    this.dismissedFeedIds = new Set();
    this.feedRefreshTimer = null;
    this.feedLastRefreshAt = 0;
    // Last successfully posted location fix, for the <25m dedupe.
    this.lastPostedFix = null;
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
      feedItems: [],
      expandedFeedItemId: 0,
      feedExpandedPage: 0,
      status: "idle",
      error: "",
    };
  }

  configure({ bridgeUrl, token } = {}) {
    // An emptied field falls back to the default bridge URL instead of being
    // silently ignored while the input keeps showing the stale value.
    const nextBridgeUrl =
      bridgeUrl !== undefined ? trimTrailingSlash(bridgeUrl) || DEFAULT_BRIDGE_URL : this.bridgeUrl;
    const nextToken = token !== undefined ? token : this.token;
    const changed = nextBridgeUrl !== this.bridgeUrl || nextToken !== this.token;
    this.bridgeUrl = nextBridgeUrl;
    this.token = nextToken;
    if (changed) {
      // A different bridge (or credential) invalidates the open stream and the
      // /api/messages cursor: keeping the old cursor would make the new bridge
      // skip every buffered message (`after` too high) while the old bridge's
      // stream kept writing into the view.
      this.close();
      this.resultWaitEpoch += 1;
      this.serverCursor = 0;
      // A different bridge also has a different feed: cursor, ack guard, and
      // dismissed set are all per-bridge state.
      this.feedCursor = 0;
      this.feedAcks.clear();
      this.dismissedFeedIds.clear();
      this.lastPostedFix = null;
      this.state = {
        ...this.state,
        messages: [],
        feedItems: [],
        expandedFeedItemId: 0,
        feedExpandedPage: 0,
        status: "idle",
        error: "",
      };
    }
    this.emit();
  }

  close() {
    this.stopFeedAutoRefresh();
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
    const raw = await response.text();
    let body = raw;
    if (contentType.includes("application/json") && raw) {
      try {
        body = JSON.parse(raw);
      } catch (err) {
        if (response.ok) throw new Error(`${method} ${path} returned invalid JSON: ${err.message}`);
        // Fall through with the raw text so the HTTP status is surfaced below.
      }
    }
    if (!response.ok) {
      const error =
        body && typeof body === "object"
          ? body.error || body.code || JSON.stringify(body)
          : String(body || "").trim();
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
    // Omnipresence is the default mode of a connected G2 (PRD §2): when the
    // bridge advertises it, land on the ambient feed. `?omni=0`
    // (forceLegacyView) and disabled omnipresence keep today's projects
    // landing byte-for-byte.
    if (info?.omnipresence?.enabled === true && !this.forceLegacyView) {
      await this.openFeed();
    } else {
      await this.refreshProjects();
    }
    return this.snapshot();
  }

  async refreshProjects() {
    const body = await this.api("/api/sessions?view=projects");
    this.close();
    this.serverCursor = 0;
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
      feedItems: [],
      expandedFeedItemId: 0,
      feedExpandedPage: 0,
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
    const view = body.mode || body.view || "sessions";
    const selectedSession = body.selectedSession || null;
    const messages = messagesFromResponseBody(body);
    const { displaySessionId, activeSessionId } = sessionIdsFromResponse(body, this.state);
    this.state = {
      ...this.state,
      mode: view,
      rows,
      selectedIndex: firstSelectable >= 0 ? firstSelectable : 0,
      selectedProject: body.selectedProject || this.state.selectedProject,
      selectedSession,
      displaySessionId: selectedSession ? displaySessionId : this.state.displaySessionId,
      activeSessionId: activeSessionId || "",
      messages: messages.length ? messages : view === "session" ? this.state.messages : [],
      status: body.state || selectedSession?.status || "idle",
      error: "",
    };
    this.emit();
    return this.snapshot();
  }

  selectedRow() {
    return coerceArray(this.state.rows)[this.state.selectedIndex] || null;
  }

  move(delta) {
    // Feed view with an expanded item: scroll pages within the item instead
    // of moving the list selection (PRD §3.6: "scroll pages within an
    // expanded push").
    if (this.state.mode === "feed" && this.state.expandedFeedItemId) {
      const expanded = coerceArray(this.state.feedItems).find(
        (item) => item.id === this.state.expandedFeedItemId,
      );
      const pages = expanded ? feedExpandedPageCount(expanded) : 1;
      const next = Math.min(Math.max(0, Number(this.state.feedExpandedPage || 0) + delta), pages - 1);
      if (next !== this.state.feedExpandedPage) {
        this.state = { ...this.state, feedExpandedPage: next, error: "" };
        this.emit();
      }
      return this.snapshot();
    }
    const rows = coerceArray(this.state.rows);
    if (!rows.length) return this.snapshot();
    const next = (this.state.selectedIndex + delta + rows.length) % rows.length;
    this.state = { ...this.state, selectedIndex: next, error: "" };
    this.emit();
    return this.snapshot();
  }

  async activateSelected() {
    if (this.state.mode === "feed") return this.activateFeedSelection();
    const row = this.selectedRow();
    if (!row?.id) return this.snapshot();
    if (row.id === NAV_PROJECTS_ID || row.id === LEGACY_NAV_PROJECTS_ID) return this.navigateBack();
    // The bridge prepends the Morpheus Feed pseudo-session to every session
    // list while omnipresence is enabled; tapping it opens the feed view.
    if (row.id === FEED_SESSION_ID) return this.openFeed();
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
    this.serverCursor = 0;
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
    // Intentionally not sessionIdsFromResponse: after an explicit selection the
    // just-requested sessionId (not prior state) is the correct fallback, and
    // the selected row's own id outranks projectActiveSessionId.
    const displaySessionId = selected?.id || body.projectActiveSessionId || sessionId;
    const activeSessionId = body.activeSessionId || selected?.activeSessionId || selected?.realSessionId || sessionId;
    this.serverCursor = 0;
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
    // Load history before the stream opens so replayed SSE messages merge on
    // top of the history baseline instead of being wiped by it.
    await this.loadHistory(displaySessionId);
    this.openEventStream(displaySessionId);
    await this.refreshMessages(displaySessionId);
    return this.snapshot();
  }

  // --- Omnipresence feed view ---

  // Opens the ambient feed. feed:main behaves like a session on the bridge,
  // so this reuses the exact select-session -> history -> SSE -> messages
  // machinery a concrete session row uses (simulator-fidelity requirement:
  // stay aligned with how stock Even clients open rows), then hydrates the
  // full item shape (body, priority, judge metadata) from GET /api/feed.
  async openFeed() {
    const body = await this.api("/api/select-session", {
      body: {
        sessionId: FEED_SESSION_ID,
        clientRequestId: requestId("sim-select-feed"),
      },
    });
    this.serverCursor = 0;
    this.state = {
      ...this.state,
      mode: "feed",
      selectedProject: body.selectedProject || this.state.selectedProject,
      selectedSession: body.selectedSession || { id: FEED_SESSION_ID, title: "Morpheus Feed" },
      displaySessionId: FEED_SESSION_ID,
      activeSessionId: "",
      messages: [],
      feedItems: [],
      rows: [feedNavRow()],
      selectedIndex: 0,
      expandedFeedItemId: 0,
      feedExpandedPage: 0,
      status: "idle",
      error: "",
    };
    this.emit();
    await this.loadHistory(FEED_SESSION_ID);
    this.openEventStream(FEED_SESSION_ID);
    // Buffered feed messages carry feedItem metadata and are ingested into
    // the item list by refreshMessages; the /api/feed hydration below then
    // upgrades them to the full item shape.
    await this.refreshMessages(FEED_SESSION_ID);
    await this.refreshFeed({ initial: true }).catch((err) => {
      this.log(`Feed hydration failed: ${err.message}`);
    });
    this.startFeedAutoRefresh();
    return this.snapshot();
  }

  // Pulls raw feed items. `initial` hydrates from scratch (after=0 returns
  // the newest page); refreshes page strictly after the highest id seen.
  async refreshFeed({ initial = false } = {}) {
    const after = initial ? 0 : this.feedCursor;
    const body = await this.api(`/api/feed?after=${encodeURIComponent(after)}&limit=20`);
    const items = coerceArray(body.items).map(feedItemFromRaw).filter(Boolean);
    if (this.upsertFeedItems(items)) this.emit();
    return items;
  }

  // Upserts items into the newest-first local list. Dismissed ids never
  // re-enter; richer versions of an item win over stream-derived ones.
  upsertFeedItems(items) {
    const byId = new Map(coerceArray(this.state.feedItems).map((item) => [item.id, item]));
    let changed = false;
    for (const item of coerceArray(items)) {
      if (!item || this.dismissedFeedIds.has(item.id)) continue;
      const existing = byId.get(item.id);
      byId.set(item.id, existing ? mergeFeedItem(existing, item) : item);
      if (item.id > this.feedCursor) this.feedCursor = item.id;
      changed = true;
    }
    if (!changed) return false;
    this.applyFeedItems([...byId.values()]);
    return true;
  }

  // Rebuilds rows (newest first + trailing Conversations nav row) and keeps
  // the cursor on the same row when it survives, clamped otherwise. A cursor
  // parked on the nav row of an item-less list does not stick: when the first
  // items hydrate, the ambient default is the newest push, not the nav row.
  applyFeedItems(items) {
    const sorted = [...coerceArray(items)].sort((a, b) => b.id - a.id);
    const rows = [...sorted.map(feedItemRow), feedNavRow()];
    const previousRows = coerceArray(this.state.rows);
    const previousRowId = previousRows[this.state.selectedIndex]?.id;
    const hadItems = previousRows.some((row) => isFeedItemRowId(row.id));
    let selectedIndex = -1;
    if (previousRowId && (isFeedItemRowId(previousRowId) || hadItems)) {
      selectedIndex = rows.findIndex((row) => row.id === previousRowId);
    }
    if (selectedIndex < 0) {
      selectedIndex = Math.min(Math.max(0, this.state.selectedIndex), rows.length - 1);
    }
    this.state = { ...this.state, feedItems: sorted, rows, selectedIndex };
  }

  // Streamed/buffered feed:main messages carry feedItem metadata; fold them
  // into the item list so pushes appear with zero user action.
  ingestFeedMessages(messages) {
    const items = coerceArray(messages).map(feedItemFromMessage).filter(Boolean);
    if (items.length && this.upsertFeedItems(items)) this.emit();
  }

  // Single tap / Enter in the feed view: expand is the cheap, reversible
  // action (PRD §3.6). Tapping an expanded item collapses it again without a
  // second ack; the Conversations nav row is the one-tap path back to the
  // legacy projects flow.
  async activateFeedSelection() {
    const row = this.selectedRow();
    if (!row?.id) return this.snapshot();
    if (row.id === FEED_NAV_CONVERSATIONS_ID) {
      return this.refreshProjects();
    }
    const itemId = Number(row.feedItemId || 0);
    if (!itemId) return this.snapshot();
    if (this.state.expandedFeedItemId === itemId) {
      this.state = { ...this.state, expandedFeedItemId: 0, feedExpandedPage: 0, error: "" };
      this.emit();
      return this.snapshot();
    }
    this.state = { ...this.state, expandedFeedItemId: itemId, feedExpandedPage: 0, error: "" };
    this.emit();
    await this.ackFeedItem(itemId, "expanded");
    return this.snapshot();
  }

  // Dismiss: ack, drop from the local list (it never comes back), collapse
  // if it was the expanded item.
  async dismissFeedItem(itemId) {
    this.dismissedFeedIds.add(itemId);
    const remaining = coerceArray(this.state.feedItems).filter((item) => item.id !== itemId);
    this.applyFeedItems(remaining);
    if (this.state.expandedFeedItemId === itemId) {
      this.state = { ...this.state, expandedFeedItemId: 0, feedExpandedPage: 0 };
    }
    this.emit();
    await this.ackFeedItem(itemId, "dismissed");
    return this.snapshot();
  }

  // POST /api/feed/ack with a client-side duplicate guard: the item+action
  // key is reserved before the request goes out, so the same ack can never
  // fire twice -- not even from rapid duplicate gestures racing the network.
  // A transport failure releases the reservation so a later retry can land.
  async ackFeedItem(itemId, action) {
    const key = `${itemId}:${action}`;
    if (this.feedAcks.has(key)) return null;
    this.feedAcks.add(key);
    try {
      return await this.api("/api/feed/ack", {
        body: {
          itemId,
          action,
          clientRequestId: requestId(`sim-feed-${action}`),
        },
      });
    } catch (err) {
      this.feedAcks.delete(key);
      this.log(`Feed ack (${action}) failed: ${err.message}`);
      return null;
    }
  }

  // SSE-aware feed refresh cadence: with the event stream open, new items
  // arrive over SSE and /api/feed is only polled coarsely as a safety net;
  // without a stream, polling runs at the tighter cadence. feedPollMs <= 0
  // disables the timer entirely (tests drive refreshes explicitly).
  startFeedAutoRefresh() {
    this.stopFeedAutoRefresh();
    if (!(this.feedPollMs > 0)) return;
    this.feedLastRefreshAt = Date.now();
    const tickMs = Math.min(this.feedPollMs, this.feedStreamPollMs);
    this.feedRefreshTimer = setInterval(() => {
      void this.feedAutoRefreshTick();
    }, tickMs);
    if (typeof this.feedRefreshTimer.unref === "function") this.feedRefreshTimer.unref();
  }

  stopFeedAutoRefresh() {
    if (this.feedRefreshTimer) {
      clearInterval(this.feedRefreshTimer);
      this.feedRefreshTimer = null;
    }
  }

  async feedAutoRefreshTick() {
    if (this.state.mode !== "feed") return;
    const every = this.eventSource ? this.feedStreamPollMs : this.feedPollMs;
    if (Date.now() - this.feedLastRefreshAt < every) return;
    this.feedLastRefreshAt = Date.now();
    await this.refreshFeed().catch((err) => {
      this.log(`Feed refresh failed: ${err.message}`);
    });
  }

  // --- Location context (PRD §3.2) ---

  // POSTs a location fix to /api/context. Validates client-side (mirrors the
  // bridge's validateLocationContext) so empty/NaN input never leaves the
  // client. options.dedupeMeters skips fixes that moved less than that since
  // the last posted fix (used by the Even location courier with 25m);
  // returns null when a fix was deduped away.
  async sendLocation(fix = {}, options = {}) {
    // Number("") is 0, which would silently post the null island; empty and
    // missing inputs must reject instead.
    const coerce = (value) =>
      value === undefined || value === null || (typeof value === "string" && value.trim() === "")
        ? Number.NaN
        : Number(value);
    const lat = coerce(fix.lat);
    const lon = coerce(fix.lon);
    if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
      throw new Error("Location lat must be a number between -90 and 90");
    }
    if (!Number.isFinite(lon) || lon < -180 || lon > 180) {
      throw new Error("Location lon must be a number between -180 and 180");
    }
    const dedupeMeters = Number(options.dedupeMeters || 0);
    if (
      dedupeMeters > 0 &&
      this.lastPostedFix &&
      distanceMeters(this.lastPostedFix, { lat, lon }) < dedupeMeters
    ) {
      return null;
    }
    const body = { kind: "location", lat, lon, clientRequestId: requestId("sim-location") };
    const accuracy = Number(fix.accuracy);
    if (fix.accuracy !== undefined && fix.accuracy !== null && Number.isFinite(accuracy) && accuracy >= 0) {
      body.accuracy = accuracy;
    }
    // Callers (Even SDK fixes, Date.now()) may pass millisecond epochs; the
    // bridge stores seconds, and a raw ms value would defeat staleness checks.
    const ts = normalizeEpochSeconds(fix.ts);
    if (fix.ts !== undefined && fix.ts !== null && Number.isFinite(ts) && ts > 0) {
      body.ts = ts;
    }
    const result = await this.api("/api/context", { body });
    this.lastPostedFix = { lat, lon };
    return result;
  }

  // Double-tap / Escape. Gesture precedence in the feed view (PRD §3.6 plus
  // the OS double-tap convention caveat), most-deliberate context first:
  //   1. an item is EXPANDED            -> dismiss it (ack, remove, collapse)
  //   2. the cursor is on a feed item   -> dismiss that item
  //   3. otherwise (empty feed, or the cursor on the local "Conversations"
  //      nav row -- i.e. no item is selected) -> fall through to the
  //      OS-conventional exit/back behavior, exactly like today.
  // Dismiss is never mapped to single tap; tap stays the cheap, reversible
  // expand.
  async navigateBack() {
    if (this.state.mode === "feed") {
      const expandedId = Number(this.state.expandedFeedItemId || 0);
      if (expandedId) return this.dismissFeedItem(expandedId);
      const row = this.selectedRow();
      const selectedItemId = Number(row?.feedItemId || 0);
      if (isFeedItemRowId(row?.id) && selectedItemId) return this.dismissFeedItem(selectedItemId);
      // No item selected: legacy back/exit below.
    }
    const body = await this.api("/api/back", {
      body: { clientRequestId: requestId("sim-back") },
    });
    this.serverCursor = 0;
    this.state = {
      ...this.state,
      selectedProject: body.selectedProject || null,
      selectedSession: body.selectedSession || null,
      displaySessionId: "",
      activeSessionId: "",
      messages: [],
      feedItems: [],
      expandedFeedItemId: 0,
      feedExpandedPage: 0,
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
    const { displaySessionId, activeSessionId } = sessionIdsFromResponse(body, this.state);
    const responseMessages = (coerceArray(body.messages).length
      ? body.messages
      : coerceArray(body.activeMessages)
    ).map((message) => normalizeMessage(message));
    this.advanceServerCursor(responseMessages);
    this.state = {
      ...this.state,
      mode: "session",
      selectedProject: body.selectedProject || this.state.selectedProject,
      selectedSession: body.selectedSession || this.state.selectedSession,
      displaySessionId,
      activeSessionId,
      messages: responseMessages,
      status: body.state || "busy",
      error: "",
    };
    this.openEventStream(displaySessionId);
    await this.refreshMessages(displaySessionId);
    this.emit();
    return this.snapshot();
  }

  async submitTranscriptViaSessionPolling(text, options = {}) {
    const clean = String(text || "").trim();
    if (!clean) return this.snapshot();
    this.localMessageSeq -= 1;
    const localMessage = normalizeMessage(
      {
        type: "user_prompt",
        role: "user",
        text: clean,
      },
      { id: this.localMessageSeq },
    );
    this.state = {
      ...this.state,
      mode: "session",
      messages: this.mergeMessages([localMessage]),
      status: "busy",
      error: "",
    };
    this.emit();

    const submittedAfter = this.serverCursor;
    const body = await this.api("/api/transcript/finalize", {
      body: {
        text: clean,
        sessionId: this.state.displaySessionId || undefined,
        clientRequestId: requestId("sim-stock-transcript"),
      },
    });

    const { displaySessionId, activeSessionId } = sessionIdsFromResponse(body, this.state);
    this.state = {
      ...this.state,
      selectedProject: body.selectedProject || this.state.selectedProject,
      selectedSession: body.selectedSession || this.state.selectedSession,
      displaySessionId,
      activeSessionId: activeSessionId || "",
      status: body.state || this.state.status,
    };
    this.emit();
    this.openEventStream(displaySessionId);

    const waitFor = options.waitFor || options.pattern || null;
    if (waitFor) {
      return this.waitForTextViaSessions(waitFor, options);
    }
    // The transcript is accepted and the event stream is open: resolve now so
    // callers (the simulator's single-action UI gate) are not blocked for a
    // whole agent turn. The result wait keeps running in the background and
    // lands its updates through the normal onChange path; tests and curious
    // callers can await the handle.
    const epoch = ++this.resultWaitEpoch;
    this.pendingResultWait = this.waitForResultViaMessages({
      ...options,
      after: submittedAfter,
      epoch,
    });
    return this.snapshot();
  }

  async loadHistory(sessionId = this.state.displaySessionId) {
    if (!sessionId) return [];
    const body = await this.api(`/api/sessions/${encodeURIComponent(sessionId)}/history?limit=10`);
    const historyMessages = historyToMessages(body.history);
    if (historyMessages.length) {
      this.state = {
        ...this.state,
        messages: this.mergeMessages(historyMessages),
        selectedProject: body.selectedProject || this.state.selectedProject,
        selectedSession: body.selectedSession || this.state.selectedSession,
      };
      this.emit();
    }
    return historyMessages;
  }

  async refreshMessages(sessionId = this.state.displaySessionId) {
    if (!sessionId) return [];
    const after = this.serverCursor;
    const body = await this.api(
      `/api/messages?sessionId=${encodeURIComponent(sessionId)}&after=${encodeURIComponent(after)}`,
    );
    const messages = coerceArray(body.messages).map((message) => normalizeMessage(message));
    this.advanceServerCursor(messages);
    this.ingestFeedMessages(messages);
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

  advanceServerCursor(messages) {
    for (const message of coerceArray(messages)) {
      const id = Number(message?.id || 0);
      if (id > this.serverCursor) this.serverCursor = id;
    }
  }

  mergeMessages(messages) {
    const incoming = coerceArray(messages);
    // Server-buffered copies supersede their fabricated history/local twins so
    // each exchange renders exactly once, in buffer order.
    const incomingServerKeys = new Set(
      incoming.filter((message) => isServerMessage(message)).map((message) => messageContentKey(message)),
    );
    const existing = coerceArray(this.state.messages).filter(
      (message) => isServerMessage(message) || !incomingServerKeys.has(messageContentKey(message)),
    );

    const merged = [];
    const seenIds = new Set();
    const seenContent = new Set();
    for (const message of [...existing, ...incoming]) {
      const contentKey = messageContentKey(message);
      if (isServerMessage(message)) {
        const idKey = `${message.id}:${message.type}:${textFromMessage(message)}`;
        if (seenIds.has(idKey)) continue;
        seenIds.add(idKey);
      } else if (seenContent.has(contentKey)) {
        // Fabricated rows dedupe purely by role + content against everything
        // already rendered, including buffered copies of the same exchange.
        continue;
      }
      seenContent.add(contentKey);
      merged.push(message);
    }
    return merged.slice(-30);
  }

  openEventStream(sessionId = this.state.displaySessionId) {
    if (!sessionId || !this.token) return null;
    this.close();
    const url = `${this.bridgeUrl}/api/events?sessionId=${encodeURIComponent(sessionId)}&needReplay=true&token=${encodeURIComponent(this.token)}`;
    const source = this.eventSourceFactory(url);
    if (!source) {
      this.log("EventSource unavailable; using message polling only");
      return null;
    }
    source.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data);
        const message = normalizeMessage(parsed, { id: Number(event.lastEventId || 0) });
        this.advanceServerCursor([message]);
        this.state = {
          ...this.state,
          messages: this.mergeMessages([message]),
          status: parsed.state || this.state.status,
        };
        // New feed pushes stream as result messages with feedItem metadata;
        // fold them straight into the ambient list.
        this.ingestFeedMessages([message]);
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

  // Bounded wait for the reply to a just-submitted transcript: returns once a
  // new terminal message landed and the session settled, or after timeoutMs
  // without throwing (the open event stream keeps delivering afterwards).
  // While the SSE stream is open it already delivers every message, so the
  // wait resolves off SSE-driven state changes and only polls /api/messages as
  // a coarse fallback; the tight intervalMs polling cadence is reserved for
  // runs without an open stream.
  async waitForResultViaMessages(options = {}) {
    const timeoutMs = options.timeoutMs || 15000;
    const intervalMs = options.intervalMs || 250;
    const streamPollIntervalMs = options.streamPollIntervalMs || 2000;
    const epoch = options.epoch;
    const after = Number(options.after || 0);
    const started = Date.now();
    // With an open stream the first fallback poll waits a full coarse
    // interval; without one, polling starts immediately as before.
    let lastPollAt = this.eventSource ? Date.now() : 0;
    while (Date.now() - started < timeoutMs) {
      // A newer submit or a reconfigure supersedes this wait.
      if (epoch !== undefined && epoch !== this.resultWaitEpoch) return this.snapshot();
      const pollEvery = this.eventSource ? streamPollIntervalMs : intervalMs;
      if (Date.now() - lastPollAt >= pollEvery) {
        lastPollAt = Date.now();
        await this.refreshMessages().catch((err) => {
          this.state = { ...this.state, error: err.message };
          this.emit();
        });
      }
      const answered = coerceArray(this.state.messages).some(
        (message) =>
          isServerMessage(message) &&
          message.id > after &&
          (message.type === "result" || message.type === "error"),
      );
      if (answered && this.state.status !== "busy") return this.snapshot();
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    this.log(`No result after ${timeoutMs}ms; the event stream keeps listening`);
    return this.snapshot();
  }

  async waitForTextViaSessions(pattern, options = {}) {
    const timeoutMs = options.timeoutMs || 3000;
    const intervalMs = options.intervalMs || 100;
    const matcher = typeof pattern === "string" ? (text) => text.includes(pattern) : (text) => pattern.test(text);
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const text = buildGlassesText(this.state);
      if (matcher(text)) return this.snapshot();
      await this.refreshSessions().catch((err) => {
        this.state = { ...this.state, error: err.message };
        this.emit();
      });
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    throw new Error(`Timed out waiting for ${String(pattern)} through /api/sessions polling`);
  }
}

export {
  DEFAULT_BRIDGE_URL,
  FEED_NAV_CONVERSATIONS_ID,
  FEED_SESSION_ID,
  LOCATION_DEDUPE_METERS,
  NAV_PROJECTS_ID,
  PROJECT_PREFIX,
  PROJECT_SESSION_PREFIX,
  textFromMessage,
};
