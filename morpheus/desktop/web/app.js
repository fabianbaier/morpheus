// Morpheus desktop — front-end controller.
// Pure helpers (escapeHtml, renderMarkdown, parseCommand, stateDotClass) are
// exported so they can be unit-tested in Node without a DOM. The DOM wiring at
// the bottom only runs in a browser (guarded by `typeof document`).

// ───────────────────────── pure helpers ─────────────────────────

// Coerce any value (including objects from the API) to a display string —
// never "[object Object]". Pure → unit-tested in Node.
export function asText(v) {
  if (v == null) return "";
  if (typeof v === "object") {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return String(v);
}

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
      const item = el("button", "nav-item");
      item.innerHTML = `<span class="nav-goal">${escapeHtml(g.objective || g.goal_id)}</span>
        <span class="nav-age">${escapeHtml(g.status === "active" ? `${g.turns_used}/${g.max_turns}` : g.status)}</span>`;
      item.onclick = () => selectGoal(g.goal_id);
      goals.appendChild(item);
    }

    const loops = $("loops-list"); loops.innerHTML = "";
    const lp = f.loops || [];
    if (!lp.length) loops.appendChild(el("div", "empty-line", "No loops."));
    for (const l of lp) {
      const item = el("button", "nav-item");
      const mark = l.running ? "●" : l.status === "active" ? "↻" : "‖";
      item.innerHTML = `<span class="nav-emoji ${l.running ? "running-glyph" : ""}">${mark}</span>
        <span class="nav-goal">${escapeHtml(l.name)}</span>
        <span class="nav-age">${l.running ? "running" : escapeHtml(l.next_due || "")}</span>`;
      item.onclick = () => selectLoop(l.id);
      loops.appendChild(item);
    }
  }

  function renderTicker(items) {
    const feed = $("ticker-feed");
    feed.innerHTML = items.slice(0, 12).map(it =>
      `<span class="ticker-item"><span class="tk-kind">${escapeHtml(asText(it.kind))}</span>${escapeHtml(asText(it.text))}</span>`
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
        const bodyEl = thinking.querySelector(".msg-body");
        bodyEl.innerHTML = renderMarkdown(r.answer || "");
        // Ask Morpheus answers from the fleet snapshot only. If a live agent
        // CLI is installed, offer a one-click re-run with real tools/web.
        const live = bestLiveAgent();
        if (live) {
          const actions = el("div", "msg-actions");
          const chip = el("button", "chip",
            `🔎 Re-run with ${escapeHtml(live.label)} (tools + web search)`);
          chip.onclick = () => switchAgentAndResend(live.kind, message);
          actions.appendChild(chip);
          bodyEl.appendChild(actions);
        }
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
  const VIEWS = ["chat", "session", "cockpit", "feed", "loop", "goal"];
  function showView(v) {
    if (v !== "loop" && typeof stopLoopPoll === "function") stopLoopPoll();
    state.view = v;
    for (const name of VIEWS) {
      const elv = $("view-" + name);
      if (elv) elv.classList.toggle("hidden", v !== name);
    }
    document.querySelector(".nav-chat")?.classList.toggle("active", v === "chat");
    $("nav-cockpit")?.classList.toggle("active", v === "cockpit");
    $("nav-feed")?.classList.toggle("active", v === "feed");
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

  // ── modal forms ──
  function openModal(title, fields, onSubmit) {
    $("modal-title").textContent = title;
    const form = $("modal-form");
    form.innerHTML = "";
    for (const f of fields) {
      const row = el("div", "form-row");
      row.appendChild(el("label", null, escapeHtml(f.label)));
      let input;
      if (f.type === "select") {
        input = el("select", "ctl-select");
        for (const [val, lab] of f.options) {
          const o = el("option", null, escapeHtml(lab)); o.value = val;
          input.appendChild(o);
        }
        if (f.value) input.value = f.value;
      } else if (f.type === "textarea") {
        input = el("textarea"); input.rows = 3; input.value = f.value || "";
      } else {
        input = el("input"); input.value = f.value || "";
        if (f.placeholder) input.placeholder = f.placeholder;
      }
      input.name = f.name;
      row.appendChild(input);
      if (f.hint) row.appendChild(el("div", "form-hint", escapeHtml(f.hint)));
      form.appendChild(row);
    }
    const actions = el("div", "form-actions");
    const cancel = el("button", "btn-ghost", "Cancel"); cancel.type = "button";
    cancel.onclick = closeModal;
    const submit = el("button", "btn-primary", "Create"); submit.type = "submit";
    actions.appendChild(cancel); actions.appendChild(submit);
    form.appendChild(actions);
    form.onsubmit = async (e) => {
      e.preventDefault();
      const data = {};
      for (const f of fields) data[f.name] = form.elements[f.name].value;
      submit.disabled = true;
      try { await onSubmit(data); closeModal(); }
      catch (err) { toast(err.message, "blocked"); }
      submit.disabled = false;
    };
    $("modal-overlay").classList.remove("hidden");
    form.querySelector("input, textarea, select")?.focus();
  }
  function closeModal() { $("modal-overlay").classList.add("hidden"); }

  const FEED_POLICY_OPTIONS = [
    ["", "don't push to feed"],
    ["always", "always push"],
    ["on_change", "push when result changes"],
    ["on_match", "push when matching pattern"],
    ["on_failure", "push on failure only"],
  ];

  function newLoopModal() {
    openModal("New loop", [
      { name: "name", label: "Name", placeholder: "hn-watch" },
      { name: "prompt", label: "Prompt", type: "textarea",
        placeholder: "Scan Hacker News for AI agent tooling news; one-line summary." },
      { name: "every", label: "Every", value: "30m", hint: "e.g. 15m, 2h, 1d" },
      { name: "command", label: "Command", placeholder: "(default: codex exec)",
        hint: "agent CLI the prompt is piped to" },
      { name: "feed_policy", label: "Feed", type: "select", options: FEED_POLICY_OPTIONS },
      { name: "feed_pattern", label: "Feed pattern", placeholder: "breaking|error|>\\s*100",
        hint: "regex threshold — used with 'push when matching pattern'" },
    ], async (d) => {
      const r = await api("/api/loops", { method: "POST", body: JSON.stringify(d) });
      if (!r.ok) throw new Error(r.error || "create failed");
      toast(`Loop "${d.name}" created`);
      await refresh();
      selectLoop(r.loop.id);
    });
  }

  function newGoalModal() {
    openModal("New goal", [
      { name: "objective", label: "Objective", type: "textarea",
        placeholder: "Ship the desktop feeds MVP" },
      { name: "done_definition", label: "Done when", type: "textarea",
        placeholder: "All tests green and pushed" },
      { name: "source", label: "Source (optional)", placeholder: "PRD path or mission ref",
        hint: "leave empty to start from the objective alone" },
      { name: "autonomy_level", label: "Autonomy", type: "select", value: "ask_to_spawn",
        options: [["observe_only", "observe only"], ["ask_to_spawn", "ask to spawn"],
                  ["bounded_fanout", "bounded fanout"]] },
      { name: "max_turns", label: "Max turns", value: "20" },
      { name: "max_workers", label: "Max workers", value: "3" },
    ], async (d) => {
      d.max_turns = parseInt(d.max_turns, 10) || 20;
      d.max_workers = parseInt(d.max_workers, 10) || 3;
      const r = await api("/api/goals", { method: "POST", body: JSON.stringify(d) });
      if (!r.ok) throw new Error(r.error || "create failed");
      toast("Goal created");
      await refresh();
      selectGoal(r.goal_id);
    });
  }

  // ── loop detail view ──
  let loopPollTimer = null;
  function stopLoopPoll() { if (loopPollTimer) { clearTimeout(loopPollTimer); loopPollTimer = null; } }

  async function selectLoop(loopId, { quiet = false } = {}) {
    stopLoopPoll();
    showView("loop");
    const v = $("view-loop");
    if (!quiet) v.innerHTML = `<div class="detail-head"><h2>loading…</h2></div>`;
    let d;
    try { d = await api("/api/loops/" + loopId); }
    catch (e) { v.innerHTML = `<div class="detail-head"><h2>loop not found</h2></div>`; return; }
    const mark = d.running ? "●" : d.status === "active" ? "↻" : "‖";
    const runDot = (s) => s === "success" ? "dot-working" : s === "running" ? "dot-idle" : "dot-blocked";
    const runs = (d.runs || []).map((r) =>
      `<div class="run-row run-click" data-run-id="${r.id}" title="Click to view output">
       <span class="run-dot ${runDot(r.status)}"></span>
       <span class="run-summary">${r.status === "running" ? "● running… " : ""}${escapeHtml(r.summary || "(no summary)")}</span>
       <span class="run-age">${escapeHtml(r.age || "")}</span></div>`).join("")
      || `<div class="empty-line">No runs yet.</div>`;
    const rulePolicy = d.feed_rule ? d.feed_rule.policy : "";
    v.innerHTML = `
      <div class="detail-head">
        <h2>${mark} ${escapeHtml(d.name)}
          ${d.running ? `<span class="running-badge">● running…</span>` : ""}</h2>
        <div class="meta">${escapeHtml(d.status)} · every ${escapeHtml(d.interval)} ·
          next ${escapeHtml(d.next_due || "now")}</div>
        <div class="detail-actions">
          <button class="btn-ghost" data-act="${d.status === "active" ? "pause" : "resume"}">
            ${d.status === "active" ? "‖ Pause" : "↻ Resume"}</button>
          <button class="btn-ghost" data-act="run_now" ${d.running ? "disabled" : ""}>
            ${d.running ? "● running…" : "▶ Run now"}</button>
          <button class="btn-ghost btn-danger" data-act="delete">✕ Delete</button>
        </div>
      </div>
      <div class="detail-body">
        <div class="mc-field"><div class="label">Prompt</div>
          <div class="value mono">${escapeHtml(d.prompt)}</div></div>
        <div class="mc-field"><div class="label">Command</div>
          <div class="value mono">${escapeHtml(d.command || "(default)")}</div></div>
        <div class="mc-section-head">Feed subscription</div>
        <div class="feed-rule-row">
          <select class="ctl-select" id="loop-feed-policy">
            ${FEED_POLICY_OPTIONS.map(([val, lab]) =>
              `<option value="${val}" ${val === rulePolicy ? "selected" : ""}>${lab}</option>`).join("")}
          </select>
          <input id="loop-feed-pattern" placeholder="regex threshold (for on-match)"
            value="${escapeHtml(d.feed_rule?.pattern || "")}" />
          <button class="btn-ghost" id="loop-feed-save">Save</button>
        </div>
        <div class="mc-section-head">Run history <span class="form-hint">(click a run to see its output)</span></div>
        <div class="run-list">${runs}</div>
      </div>`;
    for (const btn of v.querySelectorAll("[data-act]")) {
      btn.onclick = async () => {
        const act = btn.dataset.act;
        if (act === "delete" && !confirm(`Delete loop "${d.name}"?`)) return;
        btn.disabled = true;
        if (act === "run_now") btn.textContent = "▶ starting…";
        const r = await api(`/api/loops/${loopId}/action`, {
          method: "POST", body: JSON.stringify({ action: act }) });
        if (!r.ok && r.error) toast(r.error, "blocked");
        else if (act === "run_now") toast(`▶ Run started — watching for the result…`);
        else toast(`Loop ${r.status || "deleted"}`);
        await refresh();
        if (act === "delete") showView("chat"); else selectLoop(loopId, { quiet: true });
      };
    }
    // expandable run output
    for (const row of v.querySelectorAll(".run-click")) {
      row.onclick = async () => {
        const existing = row.nextElementSibling;
        if (existing && existing.classList.contains("run-output")) { existing.remove(); return; }
        const rid = row.dataset.runId;
        const out = el("pre", "run-output", "loading…");
        row.after(out);
        try {
          const r = await api(`/api/loops/${loopId}/runs/${rid}/output`);
          out.textContent = r.output || "(no output)";
        } catch { out.textContent = "(output unavailable)"; }
      };
    }
    v.querySelector("#loop-feed-save").onclick = async () => {
      const policy = v.querySelector("#loop-feed-policy").value;
      const pattern = v.querySelector("#loop-feed-pattern").value;
      const r = await api(`/api/loops/${loopId}/feed-rule`, {
        method: "POST", body: JSON.stringify({ policy, pattern }) });
      toast(r.ok ? (policy ? `Pushing to feed: ${policy}` : "Feed push disabled")
                 : (r.error || "failed"), r.ok ? "" : "blocked");
    };
    // While a run is in flight, keep the view fresh so completion shows up.
    if (d.running && state.view === "loop") {
      loopPollTimer = setTimeout(() => selectLoop(loopId, { quiet: true }), 4000);
    }
  }

  // ── goal detail view ──
  async function selectGoal(goalId) {
    showView("goal");
    const v = $("view-goal");
    const g = (state.fleet?.goals || []).find((x) => x.goal_id === goalId);
    if (!g) { v.innerHTML = `<div class="detail-head"><h2>goal not found</h2></div>`; return; }
    const tasks = (g.tasks || []).map((t) =>
      `<div class="run-row"><span class="run-dot ${t.status === "done" ? "dot-working" : t.status === "failed" ? "dot-blocked" : "dot-idle"}"></span>
       <span class="run-summary">${escapeHtml(t.title)} <span class="mono">(${escapeHtml(t.status)})</span></span></div>`).join("")
      || `<div class="empty-line">No tasks yet.</div>`;
    v.innerHTML = `
      <div class="detail-head">
        <h2>◎ ${escapeHtml(g.objective || g.goal_id)}</h2>
        <div class="meta">${escapeHtml(g.status)} · turns ${g.turns_used}/${g.max_turns} ·
          workers ${g.active_workers}/${g.max_workers} · ${escapeHtml(g.autonomy_level)}</div>
        <div class="detail-actions">
          ${g.status === "active"
            ? `<button class="btn-ghost" data-act="pause">‖ Pause</button>`
            : `<button class="btn-ghost" data-act="resume">↻ Resume</button>`}
          <button class="btn-ghost" data-act="done">✓ Done</button>
          <button class="btn-ghost btn-danger" data-act="clear">✕ Clear</button>
        </div>
      </div>
      <div class="detail-body">
        ${g.done_definition ? `<div class="mc-field"><div class="label">Done when</div>
          <div class="value">${escapeHtml(g.done_definition)}</div></div>` : ""}
        ${g.last_judge_reason ? `<div class="mc-field"><div class="label">Last judge reason</div>
          <div class="value">${escapeHtml(g.last_judge_reason)}</div></div>` : ""}
        <div class="mc-section-head">Tasks</div>
        <div class="run-list">${tasks}</div>
      </div>`;
    for (const btn of v.querySelectorAll("[data-act]")) {
      btn.onclick = async () => {
        const act = btn.dataset.act;
        if (act === "clear" && !confirm("Clear this goal?")) return;
        const r = await api(`/api/goals/${encodeURIComponent(goalId)}/action`, {
          method: "POST", body: JSON.stringify({ action: act }) });
        if (!r.ok) toast(r.error || "failed", "blocked");
        else toast(`Goal ${r.status}`);
        await refresh();
        selectGoal(goalId);
      };
    }
  }

  // ── mission cockpit (overview) ──
  function renderCockpit() {
    const f = state.fleet;
    const grid = $("cockpit-grid");
    if (!f) { grid.innerHTML = `<div class="empty-line">loading…</div>`; return; }
    const sess = f.sessions.map((s) =>
      `<div class="run-row"><span>${s.emoji}</span>
       <span class="run-summary">${escapeHtml(s.goal || s.cmd || s.tab_id)}</span>
       <span class="run-age">${escapeHtml(s.age || "")}</span></div>`).join("")
      || `<div class="empty-line">No live sessions.</div>`;
    const goals = f.goals.map((g) =>
      `<div class="run-row"><span class="run-dot ${g.status === "active" ? "dot-working" : "dot-finished"}"></span>
       <span class="run-summary">${escapeHtml(g.objective || g.goal_id)}</span>
       <span class="run-age mono">${g.turns_used}/${g.max_turns}</span></div>`).join("")
      || `<div class="empty-line">No goals.</div>`;
    const loops = (f.loops || []).map((l) =>
      `<div class="run-row"><span>${l.running ? "●" : l.status === "active" ? "↻" : "‖"}</span>
       <span class="run-summary">${escapeHtml(l.name)} — ${l.running ? "running now…" : escapeHtml(l.last_summary || "no runs yet")}</span>
       <span class="run-age">${l.running ? "" : escapeHtml(l.next_due || "")}</span></div>`).join("")
      || `<div class="empty-line">No loops.</div>`;
    const feed = (state.feedItems || []).slice(0, 8).map((it) =>
      `<div class="run-row"><span>${it.priority > 0 ? "❗" : "·"}</span>
       <span class="run-summary">${escapeHtml(it.title)}</span>
       <span class="run-age">${escapeHtml(it.age || "")}</span></div>`).join("")
      || `<div class="empty-line">Feed is quiet.</div>`;
    const notes = (f.notes || []).slice(0, 6).map((n) =>
      `<div class="run-row"><span class="mono">${escapeHtml(n.kind)}</span>
       <span class="run-summary">${escapeHtml(n.text)}</span></div>`).join("")
      || `<div class="empty-line">No notes.</div>`;
    const h = f.health;
    grid.innerHTML = `
      <div class="cockpit-card wide">
        <div class="cockpit-title">Fleet</div>
        <div class="cockpit-health">
          <span class="h-item"><span class="h-dot dot-working"></span>${h.working} working</span>
          <span class="h-item"><span class="h-dot dot-idle"></span>${h.idle} idle</span>
          <span class="h-item"><span class="h-dot dot-blocked"></span>${h.blocked} blocked</span>
          <span class="h-item"><span class="h-dot dot-crashed"></span>${h.crashed} crashed</span>
          <span class="h-item mono">$${(f.spend?.today_usd ?? 0).toFixed(2)} today</span>
        </div>
      </div>
      <div class="cockpit-card"><div class="cockpit-title">Sessions</div>${sess}</div>
      <div class="cockpit-card"><div class="cockpit-title">📡 Feed</div>${feed}</div>
      <div class="cockpit-card"><div class="cockpit-title">Goals</div>${goals}</div>
      <div class="cockpit-card"><div class="cockpit-title">Loops</div>${loops}</div>
      <div class="cockpit-card wide"><div class="cockpit-title">Recent notes</div>${notes}</div>`;
  }

  // ── feed view ──
  function renderFeedItems() {
    const list = $("feed-list");
    const items = state.feedItems || [];
    list.innerHTML = items.map((it) =>
      `<div class="feed-item ${it.priority > 0 ? "prio" : ""}">
        <span class="feed-src mono">[${escapeHtml(it.source_kind)}]</span>
        <span class="feed-title">${escapeHtml(it.title)}</span>
        ${it.body ? `<span class="feed-body">${escapeHtml(it.body)}</span>` : ""}
        <span class="run-age">${escapeHtml(it.age || "")}</span>
      </div>`).join("") || `<div class="empty-line">Nothing yet — subscribe a loop to the feed, or post manually below.</div>`;
  }

  async function renderFeedRules() {
    try {
      const r = await api("/api/feed/rules");
      $("feed-rules").innerHTML = (r.rules || []).map((rule) =>
        `<div class="run-row"><span class="mono">${escapeHtml(rule.source_kind)}:${escapeHtml(rule.source_name)}</span>
         <span class="run-summary">${escapeHtml(rule.policy)}${rule.pattern ? ` · /${escapeHtml(rule.pattern)}/` : ""}</span></div>`)
        .join("") || `<div class="empty-line">No rules — open a loop and set its feed subscription.</div>`;
    } catch {}
  }

  async function openFeedView() {
    showView("feed");
    try {
      const r = await api("/api/feed");
      state.feedItems = r.items || [];
    } catch {}
    renderFeedItems();
    renderFeedRules();
    $("feed-badge").textContent = "";
  }

  // ── command palette ──
  function commands() {
    const cmds = [
      { label: "Chat with Morpheus", kind: "view", run: () => { showView("chat"); state.selected = null; renderSidebar(state.fleet); } },
      { label: "Mission Cockpit", kind: "view", run: () => { closeCmdk(); showView("cockpit"); renderCockpit(); } },
      { label: "Feed", kind: "view", run: () => { closeCmdk(); openFeedView(); } },
      { label: "New loop…", kind: "action", run: () => { closeCmdk(); newLoopModal(); } },
      { label: "New goal…", kind: "action", run: () => { closeCmdk(); newGoalModal(); } },
      { label: "Spawn a session…", kind: "action", run: () => focusComposer("/spawn ") },
      { label: "Broadcast to fleet…", kind: "action", run: () => focusComposer("/broadcast ") },
    ];
    for (const l of state.fleet?.loops || [])
      cmds.push({ label: `↻ ${l.name}`, kind: "loop", run: () => { closeCmdk(); selectLoop(l.id); } });
    for (const g of state.fleet?.goals || [])
      cmds.push({ label: `◎ ${g.objective || g.goal_id}`, kind: "goal", run: () => { closeCmdk(); selectGoal(g.goal_id); } });
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
      const [f, act, feed] = await Promise.all([
        api("/api/fleet"), api("/api/activity"), api("/api/feed?limit=50")]);
      f.loops = (await api("/api/loops")).loops;
      state.feedItems = feed.items || [];
      applyFleet(f);
      renderTicker(act.activity);
      if (state.view === "cockpit") renderCockpit();
      if (state.view === "feed") renderFeedItems();
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
        if (state.view === "cockpit") renderCockpit();
      } catch {}
    });
    es.addEventListener("feed", (ev) => {
      try {
        const { items } = JSON.parse(ev.data);
        if (!items?.length) return;
        state.feedItems = [...items, ...(state.feedItems || [])].slice(0, 100);
        for (const it of items) if (it.priority > 0) toast(`📡 ${it.title}`, "blocked");
        if (state.view === "feed") renderFeedItems();
        else if (state.view === "cockpit") renderCockpit();
        else $("feed-badge").textContent = "●";
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
      // Enter sends; Shift+Enter inserts a newline (the Claude Code / Codex
      // convention). Cmd/Ctrl+Enter also sends. Respect IME composition.
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        $("composer").requestSubmit();
      }
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
      if (e.key === "Escape") closeModal();
    });
    // cockpit / feed / create buttons
    $("nav-cockpit").onclick = () => { showView("cockpit"); renderCockpit(); };
    $("nav-feed").onclick = openFeedView;
    $("loop-add").onclick = (e) => { e.stopPropagation(); newLoopModal(); };
    $("goal-add").onclick = (e) => { e.stopPropagation(); newGoalModal(); };
    $("modal-overlay").addEventListener("click", (e) => {
      if (e.target === $("modal-overlay")) closeModal();
    });
    $("feed-composer").addEventListener("submit", async (e) => {
      e.preventDefault();
      const inp = $("feed-input"); const v = inp.value.trim(); if (!v) return;
      inp.value = "";
      const r = await api("/api/feed", { method: "POST", body: JSON.stringify({ title: v }) });
      if (!r.ok) toast(r.error || "post failed", "blocked");
      else { await refresh(); renderFeedItems(); }
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
    const ask = el("option", null, "✦ Ask Morpheus (fleet only)");
    ask.value = "ask"; sel.appendChild(ask);
    for (const a of state.agents) {
      const o = el("option", null, (a.available ? "" : "○ ") + a.label + (a.available ? "" : " (not installed)"));
      o.value = a.kind; o.disabled = !a.available;
      sel.appendChild(o);
    }
    // Default to the best live agent when one is installed: the chat should
    // feel like Claude Code out of the box — real tool use + web search.
    // "Ask Morpheus" stays one click away for instant, free fleet answers.
    if (state.agent === "ask") {
      const live = state.agents.find((a) => a.available && a.structured)
        || state.agents.find((a) => a.available);
      if (live) state.agent = live.kind;
    }
    sel.value = state.agent;
    onAgentChanged();
  }

  function bestLiveAgent() {
    return state.agents.find((a) => a.available && a.structured)
      || state.agents.find((a) => a.available) || null;
  }

  function switchAgentAndResend(kind, message) {
    state.agent = kind;
    $("agent-select").value = kind;
    onAgentChanged();
    sendChat(message);
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
      $("composer-input").placeholder =
        `Message ${a ? a.label : state.agent}…  (web search, tool use + Morpheus fleet tools)`;
    } else {
      $("composer-input").placeholder = "Message Morpheus…  (try /spawn, /broadcast, or just ask)";
    }
    renderAgentSessionChip();
  }

  window.addEventListener("DOMContentLoaded", init);
}
