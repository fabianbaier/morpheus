#!/usr/bin/env node

import fs from "node:fs";

const args = process.argv.slice(2);

function write(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function fail(message) {
  // The real CLI reports failures as {"ok":false,"error":"..."} JSON on
  // STDOUT and exits 1 with an empty stderr; the fixture must match so the
  // bridge's structured-failure parsing is exercised against the real shape.
  write({ ok: false, error: message });
  process.exit(1);
}

function argAfter(name, fallback = "") {
  const idx = args.indexOf(name);
  return idx >= 0 && idx + 1 < args.length ? args[idx + 1] : fallback;
}

// Omnipresence state (feed items, omni flag, recorded acks/contexts) lives in
// an env-fed JSON file so tests can append feed items between successive
// `remote feed` calls. Without the file, the fixture serves a static default
// feed and reports omnipresence disabled, matching the CLI's off-by-default.
const STATE_FILE = process.env.MOCK_MORPHEUS_STATE_FILE || "";

const DEFAULT_FEED_ITEMS = [
  {
    id: 1,
    ts: 1_779_999_990.0,
    title: "Supermarket 50m left: your espresso beans are on promo.",
    body: "Alnatura on Turmstrasse carries your usual brand at -20% today.",
    priority: 2,
    source_kind: "loop",
    source_ref: "loop:location",
    metadata: {},
  },
  {
    id: 2,
    ts: 1_779_999_995.0,
    title: "PR #42 approved and ready to merge.",
    body: "",
    priority: 1,
    source_kind: "loop",
    source_ref: "loop:github",
    metadata: {},
  },
];

function readStateFile() {
  if (!STATE_FILE) return null;
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
  } catch {
    // A partially written state file must not fall back to the default feed
    // (that would inject phantom items mid-test); serve an empty feed and let
    // the next call read the completed file.
    return { items: [], acks: [], contexts: [] };
  }
}

function writeStateFile(state) {
  if (!STATE_FILE) return;
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

if (args[0] !== "remote") {
  fail(`unexpected command: ${args.join(" ")}`);
}

if (args[1] === "snapshot") {
  const projectIndex = args.indexOf("--project");
  const project = projectIndex >= 0 ? args[projectIndex + 1] : "";
  if (project === "p_beta") {
    write({
      generated_at: 1_779_999_999,
      summary: "0 sessions.",
      counts: {},
      sessions: [],
      policy: {
        raw_terminal_buffers: false,
      },
    });
    process.exit(0);
  }
  write({
    generated_at: 1_779_999_999,
    summary: "1 session.",
    counts: { idle: 1 },
    sessions: [
      {
        tab_ref: "abc123",
        mission_ref: "missionalpha",
        state: "idle",
        goal: "G2: Test Morpheus session",
        phase: "testing",
        next_step: "Accept a safe voice note.",
        blocked_on: "",
        last_event: "ready",
        age_secs: 4,
        tenant_id: project || "p_alpha",
        project_root: "/tmp/morpheus-alpha",
      },
    ],
    policy: {
      raw_terminal_buffers: false,
    },
  });
  process.exit(0);
}

if (args[1] === "projects") {
  write({
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
        usage: {
          live_sessions: 1,
          graph_rows: 3,
        },
      },
      {
        id: "p_beta",
        tenant_id: "p_beta",
        name: "beta",
        root_path: "/tmp/morpheus-beta",
        root_kind: "git",
        created_at: 1_779_999_800,
        last_seen_at: 1_779_999_900,
        archived: false,
        usage: {
          live_sessions: 0,
          graph_rows: 0,
        },
      },
    ],
  });
  process.exit(0);
}

if (args[1] === "spawn") {
  const project = args[args.indexOf("--project") + 1];
  const command = args[args.indexOf("--cmd") + 1];
  const promptIndex = args.indexOf("--prompt");
  const prompt = promptIndex >= 0 ? args[promptIndex + 1] : "";
  const goal = args[args.length - 1];
  write({
    ok: true,
    session: {
      tab_ref: "g2spawn",
      mission_ref: "m_g2spawn",
      tab_id: "g2spawn-full-tab",
      mission_id: "m_g2spawn_full",
      session_id: "session-g2spawn",
      state: "working",
      goal,
      cmd: command,
      prompt,
      project: {
        id: project || "p_alpha",
        tenant_id: project || "p_alpha",
        name: project === "p_beta" ? "beta" : "alpha",
        root_path: project === "p_beta" ? "/tmp/morpheus-beta" : "/tmp/morpheus-alpha",
      },
    },
  });
  process.exit(0);
}

if (args[1] === "note") {
  const target = args[args.indexOf("--target") + 1];
  const kind = args[args.indexOf("--kind") + 1];
  const separator = args.indexOf("--");
  const text = separator >= 0 ? args.slice(separator + 1).join(" ") : "";
  if (target !== "abc123") {
    fail(`unknown target: ${target}`);
  }
  if (kind !== "note") {
    fail(`unexpected kind: ${kind}`);
  }
  write({
    ok: true,
    id: 42,
    kind,
    text,
    target: {
      tab_ref: "abc123",
      mission_ref: "missionalpha",
    },
  });
  process.exit(0);
}

if (args[1] === "prompt") {
  const target = args[args.indexOf("--target") + 1];
  const separator = args.indexOf("--");
  const text = separator >= 0 ? args.slice(separator + 1).join(" ") : "";
  if (target !== "abc123") {
    fail(`unknown target: ${target}`);
  }
  write({
    ok: true,
    target: {
      tab_ref: "abc123",
      mission_ref: "missionalpha",
      state: "idle",
    },
    text_chars: text.length,
    note_id: 43,
  });
  process.exit(0);
}

if (args[1] === "output") {
  const ref = args[args.length - 1];
  if (ref !== "abc123" && ref !== "g2spawn") {
    fail(`unknown target: ${ref}`);
  }
  write({
    ok: true,
    session: {
      tab_ref: ref,
      mission_ref: ref === "abc123" ? "missionalpha" : "m_g2spawn",
      state: "idle",
      goal: "G2: Test Morpheus session",
    },
    output: {
      text: "Here is the current directory tree: README.md, morpheus/, plugins/, tests/.",
      lines: ["Here is the current directory tree: README.md, morpheus/, plugins/, tests/."],
      line_count: 1,
      char_count: 76,
    },
  });
  process.exit(0);
}

if (args[1] === "feed") {
  const state = readStateFile();
  const after = Number.parseInt(argAfter("--after", "0"), 10) || 0;
  const limit = Math.max(1, Number.parseInt(argAfter("--limit", "20"), 10) || 20);
  const items = [...(state?.items ?? DEFAULT_FEED_ITEMS)].sort((a, b) => a.id - b.id);
  // Contract: ascending ids strictly greater than `after`; with after=0 the
  // newest `limit` items, still ascending. latest_id is always present.
  const page =
    after > 0 ? items.filter((item) => item.id > after).slice(0, limit) : items.slice(-limit);
  const latestFromItems = items.length ? items[items.length - 1].id : 0;
  write({
    items: page,
    latest_id: Number(state?.latest_id ?? latestFromItems) || latestFromItems,
  });
  process.exit(0);
}

if (args[1] === "feed-ack") {
  const item = Number.parseInt(argAfter("--item"), 10);
  const action = argAfter("--action");
  if (!Number.isInteger(item) || item <= 0) {
    fail(`invalid feed item: ${argAfter("--item")}`);
  }
  if (action !== "expanded" && action !== "dismissed") {
    fail(`invalid feed action: ${action}`);
  }
  const state = readStateFile();
  // Acking an item the feed does not know is a CLI-side validation rejection
  // ({"ok":false,...} on stdout, exit 1) that passes the bridge's own checks,
  // so tests can exercise the structured-failure path end to end.
  if (state && STATE_FILE && !(state.items || []).some((entry) => entry.id === item)) {
    fail(`unknown feed item: ${item}`);
  }
  if (state && STATE_FILE) {
    state.acks = Array.isArray(state.acks) ? state.acks : [];
    state.acks.push({ item, action });
    writeStateFile(state);
  }
  write({ ok: true, item, action });
  process.exit(0);
}

if (args[1] === "context-add") {
  const kind = argAfter("--kind");
  const raw = argAfter("--data");
  if (kind !== "location") {
    fail(`unsupported context kind: ${kind}`);
  }
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    fail("context --data is not valid JSON");
  }
  if (typeof data.lat !== "number" || typeof data.lon !== "number") {
    fail("context location requires numeric lat and lon");
  }
  const state = readStateFile();
  let id = 1;
  if (state && STATE_FILE) {
    state.contexts = Array.isArray(state.contexts) ? state.contexts : [];
    state.contexts.push({ kind, data });
    id = state.contexts.length;
    writeStateFile(state);
  }
  write({ ok: true, id });
  process.exit(0);
}

if (args[1] === "omni-status") {
  const state = readStateFile();
  write(
    state?.omni ?? {
      enabled: false,
      threshold: 0.7,
      push_per_hour: 6,
      quiet_hours: null,
      feed: "main",
    },
  );
  process.exit(0);
}

fail(`unexpected remote command: ${args.slice(1).join(" ")}`);
