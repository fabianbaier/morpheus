// Unit tests for the pure helpers in app.js. Run with: node app.test.mjs
// (also invoked from tests/test_desktop_web.py so it runs under `make test`).
import assert from "node:assert";
import { renderMarkdown, parseCommand, escapeHtml, stateDotClass, SUGGESTIONS,
         parseSseBuffer, toolIcon, asText } from "./app.js";

// asText never yields "[object Object]"
assert.equal(asText("hi"), "hi");
assert.equal(asText(null), "");
assert.equal(asText(42), "42");
assert.equal(asText({ goal: "x" }), '{"goal":"x"}');
assert.ok(!asText({ a: 1 }).includes("[object"));

// escapeHtml escapes the dangerous five
assert.equal(escapeHtml('<b>&"x\''), "&lt;b&gt;&amp;&quot;x&#39;");

// markdown: headings, blockquotes, inline + fenced code
const md = renderMarkdown("## Hi\n\n> quote\n\nsome `code` here\n\n```\nx=1\n```");
assert.ok(md.includes("<h2>Hi</h2>"), "h2");
assert.ok(md.includes("<blockquote>quote</blockquote>"), "blockquote");
assert.ok(md.includes("<code>code</code>"), "inline code");
assert.ok(md.includes("<pre><code>x=1"), "fenced code");

// markdown is XSS-safe: raw HTML is escaped, never emitted as tags
const inj = renderMarkdown("<script>alert(1)</script>");
assert.ok(!inj.includes("<script>"), "no raw <script>");
assert.ok(inj.includes("&lt;script&gt;"), "script escaped");

// parseCommand routing
assert.deepEqual(parseCommand("/spawn fix bug -- codex"), { kind: "spawn", goal: "fix bug", command: "codex" });
assert.equal(parseCommand("/spawn codex").kind, "spawn");
assert.equal(parseCommand("/broadcast hold off").kind, "broadcast");
assert.equal(parseCommand("/note hi").kind, "note");
assert.equal(parseCommand("what is blocked?").kind, "chat");

// stateDotClass
assert.equal(stateDotClass("blocked"), "dot-blocked");
assert.equal(stateDotClass("working"), "dot-working");
assert.equal(stateDotClass("weird"), "dot-unknown");

assert.ok(SUGGESTIONS.length >= 3);

// parseSseBuffer: splits complete frames, keeps a trailing partial as `rest`
const sse = parseSseBuffer("event: text\ndata: {\"x\":1}\n\nevent: result\ndata: {\"y\":2}\n\nevent: partial\ndata: {");
assert.equal(sse.frames.length, 2);
assert.equal(sse.frames[0].event, "text");
assert.equal(sse.frames[0].data, '{"x":1}');
assert.equal(sse.frames[1].event, "result");
assert.ok(sse.rest.startsWith("event: partial"), "partial frame retained");
// a frame split across two reads is reassembled by feeding rest back in
const cont = parseSseBuffer(sse.rest + '"z":3}\n\n');
assert.equal(cont.frames.length, 1);
assert.equal(cont.frames[0].data, '{"z":3}');

// toolIcon
assert.equal(typeof toolIcon("Read"), "string");
assert.equal(toolIcon("WebSearch"), "🔎");
assert.ok(toolIcon("SomethingUnknown").length >= 1);

console.log("ok - app.js pure helper tests passed");
