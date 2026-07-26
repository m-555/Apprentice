// End-to-end bridge test against the fake agent — no Python, no Ollama, no network.
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const { AgentProcess } = require("../../dist/test-bundle.js");

const FAKE = path.join(__dirname, "fixtures", "fake-apprentice.js");

function collect(extraArgs = []) {
  const agent = new AgentProcess();
  const events = [];
  const invalid = [];
  agent.on("event", (e) => events.push(e));
  agent.on("invalid", (l) => invalid.push(l));
  const done = new Promise((resolve) => agent.on("exit", resolve));
  agent.start({ command: process.execPath, args: [FAKE, ...extraArgs], cwd: __dirname });
  return { agent, events, invalid, done };
}

const until = (events, type, ms = 5000) =>
  new Promise((resolve, reject) => {
    const t0 = Date.now();
    const tick = () => {
      const found = events.find((e) => e.type === type);
      if (found) return resolve(found);
      if (Date.now() - t0 > ms) return reject(new Error(`timeout waiting for ${type}`));
      setTimeout(tick, 10);
    };
    tick();
  });

test("streams a full turn and ends cleanly", async () => {
  const { agent, events, done } = collect();
  await until(events, "session_start");
  agent.send("add mul(a,b)");
  await until(events, "turn_end");
  agent.end();
  await done;

  const types = events.map((e) => e.type);
  assert.ok(types.includes("user"));
  assert.ok(types.includes("tool_call"));
  assert.ok(types.includes("verify_passed"));
  assert.equal(types[types.length - 1], "session_end");
  const end = events[events.length - 1];
  assert.deepEqual(end.files_changed, ["calc.py"]);
  assert.ok(!agent.running);
});

test("events split across chunk boundaries still parse", async () => {
  const { agent, events, invalid, done } = collect(["--split"]);
  await until(events, "session_start");
  agent.send("hello");
  await until(events, "turn_end");
  agent.end();
  await done;
  assert.equal(invalid.length, 0);
  assert.ok(events.some((e) => e.type === "tool_call" && e.args.path === "calc.py"));
});

test("non-JSON output is isolated, the session continues", async () => {
  const { agent, events, invalid, done } = collect(["--noise"]);
  await until(events, "session_start");
  agent.send("hello");
  await until(events, "turn_end");
  agent.end();
  await done;
  assert.ok(invalid.some((l) => l.includes("warning")));
  assert.ok(events.some((e) => e.type === "verify_passed"));
});

test("confirm_request is answered with exactly one stdin line", async () => {
  const { agent, events, done } = collect();
  await until(events, "session_start");
  agent.send("do something danger");
  const req = await until(events, "confirm_request");
  assert.equal(req.detail, "echo danger");

  agent.answerConfirm(true);
  const result = await until(events, "turn_end");
  assert.ok(result);
  const runResult = events.find((e) => e.type === "tool_result" && e.tool === "run_cmd");
  assert.ok(runResult.text.includes("exit=0"));
  agent.end();
  await done;
});

test("denying a command refuses it", async () => {
  const { agent, events, done } = collect();
  await until(events, "session_start");
  agent.send("do something danger");
  await until(events, "confirm_request");
  agent.answerConfirm(false);
  await until(events, "turn_end");
  const runResult = events.find((e) => e.type === "tool_result" && e.tool === "run_cmd");
  assert.ok(runResult.text.includes("REFUSED"));
  agent.end();
  await done;
});

test("streamed deltas arrive in order before the complete text", async () => {
  const { agent, events, done } = collect();
  await until(events, "session_start");
  agent.send("add mul(a,b)");
  await until(events, "turn_end");
  agent.end();
  await done;

  const deltas = events.filter((e) => e.type === "text_delta").map((e) => e.text);
  assert.deepEqual(deltas, ["Add", "ed ", "mul()."]);
  const full = events.find((e) => e.type === "text");
  assert.equal(full.text, deltas.join(""));
  // ordering matters: a renderer appends deltas, then swaps in the final text
  assert.ok(events.indexOf(full) > events.findIndex((e) => e.type === "text_delta"));
});

test("escalation offers are answered on the same channel as approvals", async () => {
  // A stuck agent asks to move to a paid tier; the UI answers with one stdin line.
  const { agent, events, done } = collect();
  await until(events, "session_start");
  agent.send("i am stuck");
  const nudge = await until(events, "nudge");
  assert.match(nudge.text, /repeated/);
  const offer = await until(events, "escalation_offer");
  assert.match(offer.text, /gemini\/flash/);

  agent.answerConfirm(true);
  await until(events, "turn_end");
  agent.end();
  await done;
});

test("stop() terminates a running agent", async () => {
  const { agent, events, done } = collect();
  await until(events, "session_start");
  assert.ok(agent.running);
  agent.stop();
  await done;
  assert.ok(!agent.running);
});

test("send before start throws rather than silently dropping input", () => {
  const agent = new AgentProcess();
  assert.throws(() => agent.send("hi"), /not running/);
});
