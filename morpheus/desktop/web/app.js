// Morpheus desktop — front-end controller.
// Pure helpers (escapeHtml, renderMarkdown, parseCommand, stateDotClass) are
// exported so they can be unit-tested in Node without a DOM. The DOM wiring at
// the bottom only runs in a browser (guarded by `typeof document`).

// ───────────────────────── pure helpers ─────────────────────────

export function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Minimal, safe Markdown → HTML (escapes first, then re-introduces a small,
// known set of tags). Supports headings, bold, inline code, fenced code,
// blockquotes, and paragraphs — enough for ask.py's output.
export function renderMarkdown(src) {
  const text = escapeHtml(src || "");
  const lines = text.split("\n");
  const out = [];
  let inCode = false, codeBuf = [], para = [];
  const flushPara = () => {
    if (para.length) { out.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; }
  };
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (inCode) { out.push("<pre><code>" + codeBuf.join("\n") + "</code></pre>"); codeBuf = []; inCode = false; }
      else { flushPara(); inCode = true; }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { flushPara(); out.push(`<h2>${inline(h[2])}</h2>`); continue; }
    if (line.startsWith("&gt; ")) { flushPara(); out.push("<blockquote>" + inline(line.slice(5)) + "</blockquote>"); continue; }
    if (line.trim() === "") { flushPara(); continue; }
    para.push(line);
  }
  if (inCode) out.push("<pre><code>" + codeBuf.join("\n") + "</code></pre>");
  flushPara();
  return out.join("\n");
}

export function stateDotClass(state) {
  const known = ["working", "idle", "blocked", "crashed", "finished"];
  return "dot-" + (known.includes(state) ? state : "unknown");
}

// Parse a composer message into a structured intent. Slash commands map to
// bridge control ops; anything else is a chat question.
export function parseCommand(message) {
  const m = (message || "").trim();
  if (m.startsWith("/spawn ")) {
    const rest = m.slice(7).trim();
    // /spawn <goal> -- <command>   (or)   /spawn <command>
    const split = rest.split(" -- ");
    if (split.length === 2) return { kind: "spawn", goal: split[0].trim(), command: split[1].trim() };
    return { kind: "spawn", goal: rest, command: rest };
  }
  if (m.startsWith("/broadcast ")) return { kind: "broadcast", text: m.slice(11).trim() };
  if (m.startsWith("/note ")) return { kind: "note", text: m.slice(6).trim() };
  return { kind: "chat", message: m };
}

export const SUGGESTIONS = [
  "What needs my attention right now?",
  "Which sessions are blocked?",
  "Summarize the fleet",
  "What proof do we have that things work?",
  "/broadcast hold off on src/auth/*",
];

// Parse a Server-Sent-Events text buffer into complete frames, returning any
// trailing partial frame as `rest`. Used to read the streamed agent turn from a
// fetch() response body. Pure → unit-tested in Node.
export function parseSseBuffer(buf) {
  const frames = [];
  let idx;
  while ((idx = buf.indexOf("\n\n")) >= 0) {
    const raw = buf.slice(0, idx);
    buf = buf.slice(idx + 2);
    const ev = { event: "message", data: "" };
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) ev.event = line.slice(6).trim();
      else if (line.startsWith("data:")) ev.data += line.slice(5).trim();
    }
    frames.push(ev);
  }
  return { frames, rest: buf };
}

// Icon for a tool name so tool use reads like Claude Code / Codex.
export function toolIcon(name) {
  const map = {
    Read: "📄", Edit: "✏️", Write: "✏️", MultiEdit: "✏️", NotebookEdit: "✏️",
    Bash: "❯_", Grep: "🔍", Glob: "🔍", Task: "🤖", WebSearch: "🔎", WebFetch: "🌐",
  };
  return map[name] || "🔧";
}

// ───────────────────────── browser app ─────────────────────────

if (typeof document !== "undefined") {
  const params = new URLSearchParams(location.search);
  const TOKEN = params.get("token") || "";
  const authHeaders = { "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json" };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };

  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { ...authHeaders, ...(opts.headers || {}) } });
    if (!res.ok) throw new Error(`${path} → ${res.status}`);
    return res.json();
  }

  const state = {
    fleet: null, view: "chat", selected: null, chat: [],
    agent: "ask",          // "ask" = Morpheus oracle; else a real CLI: claude/codex/gemini
    agents: [],            // available agent CLIs
    agentSession: {},      // per-agent session_id for multi-turn continuity
    cwd: "",               // working directory agents operate in
    permission: "default",
  };

  // ── rendering ──
  function renderHealth(h) {
    const items = [
      ["working", h.working], ["idle", h.idle], ["blocked", h.blocked],
      ["crashed", h.crashed], ["finished", h.finished],
    ].filter(([, n]) => n > 0);
    $("health").innerHTML = items.map(([k, n]) =>
      `<span class="h-item"><span class="h-dot ${stateDotClass(k)}"></span>${n} ${k}</span>`).join("") ||
      `<span class="h-item">no sessions yet</span>`;
  }

  function renderSidebar(f) {
    $("spend").textContent = "$" + (f.spend?.today_usd ?? 0).toFixed(2);
    renderHealth(f.health);
    $("sessions-count").textContent = f.sessions.length;
    $("goals-count").textContent = f.goals.length;
    $("loops-count").textContent = f.loops?.length ?? 0;

    const list = $("sessions-list");
    list.innerHTML = "";
    if (!f.sessions.length) list.appendChild(el("div", "empty-line", "No live sessions."));
    for (const s of f.sessions) {
      const item = el("button", "nav-item");
      if (state.selected === (s.mission_id || s.tab_id)) item.classList.add("active");
      item.innerHTML = `<span class="nav-emoji">${s.emoji}</span>
        <span class="nav-goal">${escapeHtml(s.goal || s.cmd || s.tab_id)}</span>
        <span class="nav-age">${escapeHtml(s.age || "")}</span>`;
      item.onclick = () => selectSession(s);
      list.appendChild(item);
    }

    const goals = $("goals-list"); goals.innerHTML = "";
    if (!f.goals.length) goals.appendChild(el("div", "empty-line", "No goals."));
    for (const g of f.goals) {
      const item = el("div", "nav-item");
      item.innerHTML = `<span class="nav-goal">${escapeHtml(g.objective || g.goal_id)}</span>
        <span class="nav-age">${g.turns_used}/${g.max_turns}</span>`;
      goals.appendChild(item);
    }

    const loops = $("loops-list"); loops.innerHTML = "";
    const lp = f.loops || [];
    if (!lp.length) loops.appendChild(el("div", "empty-line", "No loops."));
    for (const l of lp) {
      const item = el("div", "nav-item");
      const mark = l.status === "active" ? "↻" : "‖";
      item.innerHTML = `<span class="nav-emoji">${mark}</span>
        <span class="nav-goal">${escapeHtml(l.name)}</span>
        <span class="nav-age">${escapeHtml(l.next_due || "")}</span>`;
      loops.appendChild(item);
    }
  }

  function renderTicker(items) {
    const feed = $("ticker-feed");
    feed.innerHTML = items.slice(0, 12).map(it =>
      `<span class="ticker-item"><span class="tk-kind">${escapeHtml(it.kind)}</span>${escapeHtml(it.text)}</span>`
    ).join("") || `<span class="ticker-item">fleet quiet</span>`;
  }

  function renderMissionCard(d) {
    $("inspector-empty").classList.add("hidden");
    const card = $("mission-card");
    card.classList.remove("hidden");
    const field = (label, value, cls = "") =>
      value ? `<div class="mc-field"><div class="label">${label}</div><div class="value ${cls}">${escapeHtml(value)}</div></div>` : "";
    const events = (d.events || []).map(e =>
      `<div class="timeline-item"><span class="t-kind">${escapeHtml(e.kind)}</span>
        <span>${escapeHtml(e.summary)}</span><span class="t-age">${escapeHtml(e.age)}</span></div>`).join("");
    const artStatus = (s) => ({ pass: "dot-working", fail: "dot-blocked", pending: "dot-idle" }[s] || "dot-unknown");
    const artifacts = (d.artifacts || []).map(a =>
      `<div class="artifact"><span class="a-status ${artStatus(a.status)}"></span>
        <span class="a-path" title="${escapeHtml(a.path_or_url)}">${escapeHtml(a.path_or_url)}</span></div>`).join("");
    card.innerHTML = `
      <div class="mc-title">${escapeHtml(d.title || d.mission_id)}</div>
      <div class="mc-badges">
        <span class="badge phase">${escapeHtml(d.phase || "")}</span>
        ${d.agent_kind ? `<span class="badge">${escapeHtml(d.agent_kind)}</span>` : ""}
        <span class="badge">confidence ${Math.round((d.confidence || 0) * 100)}%</span>
        ${d.archived ? `<span class="badge">archived</span>` : ""}
      </div>
      ${field("Why", d.why)}
      ${field("Done when", d.done_definition)}
      ${field("Plan", d.current_plan)}
      ${field("Next step", d.next_step)}
      ${field("Blocked on", d.blocked_on, "blocked")}
      ${events ? `<div class="mc-section-head">Timeline</div>${events}` : ""}
      ${artifacts ? `<div class="mc-section-head">Proof</div>${artifacts}` : ""}
    `;
  }

  // ── chat ──
  function addMessage(role, html) {
    $("chat-empty").classList.add("hidden");
    const m = el("div", "msg " + role);
    m.innerHTML = `<div class="msg-role">${role === "user" ? "You" : "Morpheus"}</div>
      <div class="msg-body">${html}</div>`;
    $("chat-scroll").appendChild(m);
    $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
    return m;
  }

  async function sendChat(message) {
    // A real agent CLI (claude/codex/gemini) is selected → drive it live.
    if (state.agent !== "ask") {
      return sendAgentTurn(message);
    }
    const intent = parseCommand(message);
    addMessage("user", escapeHtml(message));
    const thinking = addMessage("morpheus", `<div class="thinking"><span></span><span></span><span></span></div>`);
    try {
      if (intent.kind === "spawn") {
        const r = await api("/api/spawn", { method: "POST", body: JSON.stringify(intent) });
        thinking.querySelector(".msg-body").innerHTML = r.ok
          ? `Spawned <code>${escapeHtml(r.tab_id)}</code> — mission <code>${escapeHtml(r.mission_id)}</code>.`
          : `Couldn't spawn here. ${escapeHtml(r.error || "")}<div class="msg-actions"><span class="chip">${escapeHtml(r.hint || "")}</span></div>`;
      } else if (intent.kind === "broadcast") {
        const r = await api("/api/broadcast", { method: "POST", body: JSON.stringify({ text: intent.text }) });
        thinking.querySelector(".msg-body").innerHTML = `Broadcast recorded${r.delivery?.attempted ? " and delivered to live sessions" : " (live delivery needs macOS + iTerm)"}.`;
      } else if (intent.kind === "note") {
        await api("/api/notes", { method: "POST", body: JSON.stringify({ text: intent.text }) });
        thinking.querySelector(".msg-body").innerHTML = `Note posted.`;
      } else {
        const r = await api("/api/chat", { method: "POST", body: JSON.stringify({ message }) });
        thinking.querySelector(".msg-body").innerHTML = renderMarkdown(r.answer || "");
      }
    } catch (e) {
      thinking.querySelector(".msg-body").innerHTML = `<span style="color:var(--blocked)">Error: ${escapeHtml(e.message)}</span>`;
    }
    refresh();
  }

  // ── live agent turn (claude / codex / gemini under the hood) ──
  async function* sseStream(response) {
    const reader = response.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const { frames, rest } = parseSseBuffer(buf);
      buf = rest;
      for (const f of frames) yield f;
    }
  }

  async function sendAgentTurn(message) {
    addMessage("user", escapeHtml(message));
    const label = (state.agents.find((a) => a.kind === state.agent) || {}).label || state.agent;
    const msg = addMessage(state.agent, "");
    const body = msg.querySelector(".msg-role");
    body.textContent = label;
    const container = msg.querySelector(".msg-body");
    container.innerHTML = `<div class="agent-steps"></div>
      <div class="agent-prose"></div>
      <div class="agent-status"><span class="thinking"><span></span><span></span><span></span></span></div>`;
    const steps = container.querySelector(".agent-steps");
    const prose = container.querySelector(".agent-prose");
    const status = container.querySelector(".agent-status");
    let proseText = "";
    let lastToolCard = null;
    const scroll = () => { $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight; };

    const stepCard = (icon, title, sub, cls = "") => {
      const card = el("div", "step-card " + cls);
      card.innerHTML = `<div class="step-head"><span class="step-icon">${escapeHtml(icon)}</span>
        <span class="step-title">${escapeHtml(title)}</span>
        <span class="step-sub">${escapeHtml(sub || "")}</span></div>`;
      steps.appendChild(card); scroll();
      return card;
    };

    try {
      const res = await fetch("/api/agent/turn", {
        method: "POST", headers: authHeaders,
        body: JSON.stringify({
          agent: state.agent, message, cwd: state.cwd || undefined,
          session_ref: state.agentSession[state.agent] || "",
          permission_mode: state.permission,
        }),
      });
      if (!res.ok || !res.body) throw new Error("agent HTTP " + res.status);
      for await (const frame of sseStream(res)) {
        let ev; try { ev = JSON.parse(frame.data); } catch { continue; }
        if (ev.type === "session") {
          if (ev.session_id) {
            state.agentSession[state.agent] = ev.session_id;
            renderAgentSessionChip();
          }
        } else if (ev.type === "thinking") {
          status.innerHTML = `<span class="agent-thinking">✦ thinking…</span>`;
        } else if (ev.type === "text") {
          proseText += ev.text;
          prose.innerHTML = renderMarkdown(proseText);
          scroll();
        } else if (ev.type === "tool_use") {
          lastToolCard = stepCard(toolIcon(ev.name), ev.name, ev.summary);
        } else if (ev.type === "web_search") {
          stepCard("🔎", "Web search", ev.query, "step-web");
        } else if (ev.type === "web_fetch") {
          stepCard("🌐", "Fetch", ev.url, "step-web");
        } else if (ev.type === "tool_result") {
          if (lastToolCard) {
            const out = el("div", "step-result" + (ev.is_error ? " error" : ""));
            out.textContent = (ev.content || "").slice(0, 2000);
            lastToolCard.appendChild(out);
          }
        } else if (ev.type === "result") {
          status.innerHTML = ev.cost_usd
            ? `<span class="agent-done">done · $${ev.cost_usd.toFixed(4)}${ev.web_searches ? ` · ${ev.web_searches} web` : ""}</span>`
            : `<span class="agent-done">done</span>`;
          if (ev.text && !proseText) prose.innerHTML = renderMarkdown(ev.text);
        } else if (ev.type === "error") {
          status.innerHTML = `<span style="color:var(--blocked)">Error: ${escapeHtml(ev.message)}</span>`;
        }
      }
    } catch (e) {
      status.innerHTML = `<span style="color:var(--blocked)">Error: ${escapeHtml(e.message)}</span>`;
    }
    scroll();
    refresh();
  }

  function renderAgentSessionChip() {
    const sid = state.agentSession[state.agent];
    const chip = $("agent-session");
    if (state.agent !== "ask" && sid) {
      chip.textContent = "● " + sid.slice(0, 8);
      chip.classList.remove("hidden");
    } else {
      chip.classList.add("hidden");
    }
  }

  // ── views ──
  function showView(v) {
    state.view = v;
    $("view-chat").classList.toggle("hidden", v !== "chat");
    $("view-session").classList.toggle("hidden", v !== "session");
  }

  async function selectSession(s) {
    state.selected = s.mission_id || s.tab_id;
    showView("session");
    $("session-head").innerHTML = `<h2>${s.emoji} ${escapeHtml(s.goal || s.tab_id)}</h2>
      <div class="meta">${escapeHtml(s.state)} · ${escapeHtml(s.cmd || "")} · ${escapeHtml(s.tab_id)}</div>`;
    const tail = (s.activity?.tail || []).join("\n") || s.headline || "(no recent output captured)";
    $("transcript").textContent = tail;
    $("session-composer").dataset.tabId = s.tab_id;
    try {
      const ref = s.mission_id || s.tab_id;
      const detail = await api("/api/sessions/" + encodeURIComponent(ref));
      renderMissionCard(detail);
    } catch { /* no durable card yet */ }
    renderSidebar(state.fleet);
  }

  // ── command palette ──
  function commands() {
    const cmds = [
      { label: "Chat with Morpheus", kind: "view", run: () => { showView("chat"); state.selected = null; renderSidebar(state.fleet); } },
      { label: "Spawn a session…", kind: "action", run: () => focusComposer("/spawn ") },
      { label: "Broadcast to fleet…", kind: "action", run: () => focusComposer("/broadcast ") },
    ];
    for (const s of state.fleet?.sessions || [])
      cmds.push({ label: `${s.emoji} ${s.goal || s.tab_id}`, kind: "session", run: () => selectSession(s) });
    return cmds;
  }
  function focusComposer(prefix) {
    closeCmdk(); showView("chat");
    const inp = $("composer-input"); inp.value = prefix; inp.focus();
  }
  let cmdkActive = 0;
  function openCmdk() {
    $("cmdk-overlay").classList.remove("hidden");
    $("cmdk-input").value = ""; $("cmdk-input").focus(); cmdkActive = 0; renderCmdk("");
  }
  function closeCmdk() { $("cmdk-overlay").classList.add("hidden"); }
  function renderCmdk(query) {
    const q = query.toLowerCase();
    const matches = commands().filter(c => c.label.toLowerCase().includes(q));
    cmdkActive = Math.min(cmdkActive, Math.max(0, matches.length - 1));
    const box = $("cmdk-results"); box.innerHTML = "";
    matches.forEach((c, i) => {
      const item = el("div", "cmdk-item" + (i === cmdkActive ? " active" : ""));
      item.innerHTML = `<span>${escapeHtml(c.label)}</span><span class="ck-kind">${c.kind}</span>`;
      item.onclick = () => c.run();
      box.appendChild(item);
    });
    box._matches = matches;
  }

  function toast(text, cls = "") {
    const t = el("div", "toast " + cls, escapeHtml(text));
    $("toasts").appendChild(t);
    setTimeout(() => t.remove(), 6000);
  }

  // ── data refresh + SSE ──
  let prevAttention = new Set();
  function applyFleet(f) {
    state.fleet = f;
    renderSidebar(f);
    // surface newly blocked/crashed sessions as toasts
    const attention = new Set(f.sessions.filter(s => ["blocked", "crashed"].includes(s.state)).map(s => s.tab_id));
    for (const tab of attention) if (!prevAttention.has(tab)) {
      const s = f.sessions.find(x => x.tab_id === tab);
      toast(`${s.emoji} ${s.state}: ${s.goal || tab}`, "blocked");
    }
    prevAttention = attention;
  }
  async function refresh() {
    try {
      const [f, act] = await Promise.all([api("/api/fleet"), api("/api/activity")]);
      f.loops = (await api("/api/loops")).loops;
      applyFleet(f);
      renderTicker(act.activity);
    } catch (e) { /* transient */ }
  }
  function connectStream() {
    const es = new EventSource("/api/stream?token=" + encodeURIComponent(TOKEN));
    es.addEventListener("fleet", async (ev) => {
      try {
        const f = JSON.parse(ev.data);
        f.loops = (await api("/api/loops")).loops;
        applyFleet(f);
        $("conn-dot").classList.add("live");
        renderTicker((await api("/api/activity")).activity);
      } catch {}
    });
    es.onerror = () => { $("conn-dot").classList.remove("live"); };
  }

  // ── wiring ──
  function init() {
    $("suggestions").innerHTML = "";
    for (const s of SUGGESTIONS) {
      const b = el("button", "suggestion", escapeHtml(s));
      b.onclick = () => { $("composer-input").value = s; $("composer-input").focus(); };
      $("suggestions").appendChild(b);
    }
    $("composer").addEventListener("submit", (e) => {
      e.preventDefault();
      const inp = $("composer-input"); const v = inp.value.trim();
      if (!v) return; inp.value = ""; inp.style.height = "auto"; sendChat(v);
    });
    $("composer-input").addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); $("composer").requestSubmit(); }
    });
    $("composer-input").addEventListener("input", (e) => {
      e.target.style.height = "auto"; e.target.style.height = e.target.scrollHeight + "px";
    });
    $("session-composer").addEventListener("submit", async (e) => {
      e.preventDefault();
      const inp = $("session-input"); const v = inp.value.trim(); if (!v) return;
      const tabId = e.target.dataset.tabId; inp.value = "";
      const r = await api("/api/send", { method: "POST", body: JSON.stringify({ tab_id: tabId, text: v }) });
      if (!r.ok) toast(r.error || "send failed", "blocked");
    });
    document.querySelector(".nav-chat").onclick = () => { showView("chat"); state.selected = null; renderSidebar(state.fleet); };
    $("cmdk-btn").onclick = openCmdk;
    $("cmdk-input").addEventListener("input", (e) => { cmdkActive = 0; renderCmdk(e.target.value); });
    $("cmdk-input").addEventListener("keydown", (e) => {
      const matches = $("cmdk-results")._matches || [];
      if (e.key === "ArrowDown") { cmdkActive = Math.min(cmdkActive + 1, matches.length - 1); renderCmdk($("cmdk-input").value); e.preventDefault(); }
      else if (e.key === "ArrowUp") { cmdkActive = Math.max(cmdkActive - 1, 0); renderCmdk($("cmdk-input").value); e.preventDefault(); }
      else if (e.key === "Enter") { matches[cmdkActive]?.run(); }
      else if (e.key === "Escape") { closeCmdk(); }
    });
    $("cmdk-overlay").addEventListener("click", (e) => { if (e.target === $("cmdk-overlay")) closeCmdk(); });
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openCmdk(); }
    });
    // agent picker
    $("agent-select").addEventListener("change", (e) => {
      state.agent = e.target.value;
      onAgentChanged();
    });
    $("perm-select").addEventListener("change", (e) => { state.permission = e.target.value; });
    loadAgents();
    refresh();
    connectStream();
  }

  async function loadAgents() {
    try {
      const r = await api("/api/agents");
      state.agents = r.agents || [];
      state.cwd = r.cwd || "";
    } catch { state.agents = []; }
    const sel = $("agent-select");
    sel.innerHTML = "";
    const ask = el("option", null, "✦ Ask Morpheus");
    ask.value = "ask"; sel.appendChild(ask);
    for (const a of state.agents) {
      const o = el("option", null, (a.available ? "" : "○ ") + a.label + (a.available ? "" : " (not installed)"));
      o.value = a.kind; o.disabled = !a.available;
      sel.appendChild(o);
    }
    sel.value = state.agent;
    onAgentChanged();
  }

  function onAgentChanged() {
    const isAgent = state.agent !== "ask";
    $("cwd-chip").classList.toggle("hidden", !isAgent);
    $("perm-select").classList.toggle("hidden", !(isAgent && state.agent === "claude"));
    if (isAgent) {
      const home = (state.cwd || "").replace(/^.*\//, "") || state.cwd;
      $("cwd-chip").textContent = "📁 " + (home || "cwd");
      $("cwd-chip").title = state.cwd;
      const a = state.agents.find((x) => x.kind === state.agent);
      $("composer-input").placeholder = `Message ${a ? a.label : state.agent}…  (real tool use + web search)`;
    } else {
      $("composer-input").placeholder = "Message Morpheus…  (try /spawn, /broadcast, or just ask)";
    }
    renderAgentSessionChip();
  }

  window.addEventListener("DOMContentLoaded", init);
}
