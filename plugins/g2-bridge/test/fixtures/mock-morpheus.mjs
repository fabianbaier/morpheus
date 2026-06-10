#!/usr/bin/env node

const args = process.argv.slice(2);

function write(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function fail(message, code = 1) {
  process.stderr.write(`${message}\n`);
  process.exit(code);
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
    fail(`unknown target: ${target}`, 2);
  }
  if (kind !== "note") {
    fail(`unexpected kind: ${kind}`, 2);
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
    fail(`unknown target: ${target}`, 2);
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
    fail(`unknown target: ${ref}`, 2);
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

fail(`unexpected remote command: ${args.slice(1).join(" ")}`);
