// Protocol parsing — runs against the compiled dist output, no VS Code needed.
const test = require("node:test");
const assert = require("node:assert");
const { EventParser, toolDetail, MUTATING_TOOLS } = require("../../dist/test-bundle.js");

test("parses whole lines into typed events", () => {
  const p = new EventParser();
  const { events, invalid } = p.push(
    '{"ts":"t","type":"session_start","session_id":"a"}\n' +
    '{"ts":"t","type":"tool_call","tool":"read_file","args":{"path":"x.py"}}\n'
  );
  assert.equal(invalid.length, 0);
  assert.equal(events.length, 2);
  assert.equal(events[0].type, "session_start");
  assert.equal(events[1].args.path, "x.py");
});

test("survives a chunk boundary mid-JSON", () => {
  // The realistic failure: a pipe hands us half an event, then the rest.
  const p = new EventParser();
  const first = p.push('{"ts":"t","type":"tool_res');
  assert.equal(first.events.length, 0, "must not emit a partial event");
  const second = p.push('ult","tool":"read_file","text":"hi"}\n');
  assert.equal(second.events.length, 1);
  assert.equal(second.events[0].text, "hi");
});

test("a stray non-JSON line is reported, not thrown", () => {
  // A provider SDK printing a warning must never kill the session.
  const p = new EventParser();
  const { events, invalid } = p.push(
    'warning: some library noise\n{"ts":"t","type":"text","text":"ok"}\n'
  );
  assert.deepEqual(invalid, ["warning: some library noise"]);
  assert.equal(events.length, 1);
  assert.equal(events[0].text, "ok");
});

test("JSON that is not an event object counts as invalid", () => {
  const p = new EventParser();
  const { events, invalid } = p.push('[1,2,3]\n{"no":"type"}\n');
  assert.equal(events.length, 0);
  assert.equal(invalid.length, 2);
});

test("flush returns an unterminated tail", () => {
  const p = new EventParser();
  p.push('{"ts":"t","type":"text","text":"a"}\n{"partial":');
  assert.equal(p.flush(), '{"partial":');
  assert.equal(p.flush(), "", "flush clears the buffer");
});

test("toolDetail picks the argument worth showing", () => {
  assert.equal(toolDetail({ type: "tool_call", tool: "read_file", args: { path: "a.py" } }), "a.py");
  assert.equal(toolDetail({ type: "tool_call", tool: "run_cmd", args: { cmd: "pytest -q" } }), "pytest -q");
  assert.equal(toolDetail({ type: "tool_call", tool: "list_files", args: {} }), "");
  assert.equal(toolDetail({ type: "tool_call", tool: "list_files" }), "");
});

test("text_delta events parse as ordinary events", () => {
  // Streaming produces MANY of these per turn; they must survive chunking like any other.
  const p = new EventParser();
  const { events } = p.push(
    '{"ts":"t","type":"text_delta","text":"Hel"}\n' +
    '{"ts":"t","type":"text_delta","text":"lo"}\n' +
    '{"ts":"t","type":"text","text":"Hello"}\n'
  );
  assert.equal(events.length, 3);
  assert.deepEqual(events.slice(0, 2).map((e) => e.text), ["Hel", "lo"]);
  assert.equal(events[2].type, "text");
});

test("a delta split mid-escape still parses", () => {
  const p = new EventParser();
  p.push('{"ts":"t","type":"text_delta","text":"line\\n');
  const { events } = p.push('more"}\n');
  assert.equal(events.length, 1);
  assert.equal(events[0].text, "line\nmore");
});

test("only file-writing tools mark a file as changed", () => {
  assert.ok(MUTATING_TOOLS.has("write_file") && MUTATING_TOOLS.has("edit_file"));
  assert.ok(!MUTATING_TOOLS.has("read_file") && !MUTATING_TOOLS.has("run_cmd"));
});
