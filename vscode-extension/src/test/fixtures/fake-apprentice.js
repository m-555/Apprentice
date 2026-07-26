#!/usr/bin/env node
// A stand-in for the real `apprentice … --json` process.
//
// Emits a canned NDJSON event stream and reacts to stdin exactly as the agent does, so
// the extension can be tested with NO Python, NO Ollama and NO network. It also serves as
// the regression fixture when the protocol grows: if a new event type appears, add it
// here and the parser tests cover it immediately.
//
// Behavior:
//   * prints session_start on launch
//   * for each stdin line: echoes a `user` event, a tool_call/tool_result pair, then
//     verify_passed and turn_end
//   * if the line contains "danger", it first emits confirm_request and waits for one
//     line ({"allow":true} or y/yes) before continuing
//   * on EOF: session_end
//
// Flags: --split  write events in two chunks (exercises partial-line parsing)
//        --noise  interleave a non-JSON line (a stray SDK print)

const readline = require("readline");

const split = process.argv.includes("--split");
const noise = process.argv.includes("--noise");

function emit(obj) {
  const line = JSON.stringify({ ts: new Date().toISOString(), ...obj }) + "\n";
  if (split && line.length > 20) {
    process.stdout.write(line.slice(0, 12));
    process.stdout.write(line.slice(12));
  } else {
    process.stdout.write(line);
  }
}

emit({
  type: "session_start",
  session_id: "fake123",
  repo: process.cwd(),
  provider: "qwen",
  model: "",
  verify: "tests",
  test_cmd: "pytest -q",
});

let pendingConfirm = null;
let pendingHost = null;

const rl = readline.createInterface({ input: process.stdin });

rl.on("line", (line) => {
  const text = line.trim();
  if (!text) return;

  if (pendingHost) {
    const obj = JSON.parse(text);
    emit({ type: "tool_result", tool: "get_diagnostics", text: obj.result });
    pendingHost = null;
    emit({ type: "turn_end", usage: usage() });
    return;
  }

  if (pendingConfirm) {
    const allow =
      text.startsWith("{") ? !!JSON.parse(text).allow : /^(y|yes|true)$/i.test(text);
    emit({ type: "tool_result", tool: "run_cmd", text: allow ? "exit=0\nok" : "REFUSED by the user." });
    pendingConfirm = null;
    emit({ type: "verify_passed", check: "tests" });
    emit({ type: "turn_end", usage: usage() });
    return;
  }

  emit({ type: "user", text });

  if (noise) process.stdout.write("warning: a library printed this\n");

  if (text.startsWith("/")) {
    emit({ type: "ack", command: text.slice(1).split(" ")[0], usage: usage() });
    return;
  }

  emit({ type: "tool_call", tool: "read_file", args: { path: "calc.py" } });
  emit({ type: "tool_result", tool: "read_file", text: "1\tdef add(a, b):" });
  emit({ type: "tool_call", tool: "edit_file", args: { path: "calc.py" } });
  emit({ type: "tool_result", tool: "edit_file", text: "Edited calc.py (1 replacement(s))." });

  if (text.includes("diag")) {
    // ask the editor for diagnostics and wait for its reply (routed by id)
    pendingHost = true;
    emit({ type: "host_request", id: "h1", kind: "diagnostics", path: "calc.py" });
    return;
  }

  if (text.includes("stuck")) {
    pendingConfirm = true;
    emit({ type: "nudge", text: "repeated read_file x3" });
    emit({ type: "escalation_offer",
           text: "qwen has failed verification repeatedly. Switch to gemini/flash?",
           detail: "gemini/flash" });
    return;
  }

  if (text.includes("danger")) {
    pendingConfirm = true;
    emit({ type: "tool_call", tool: "run_cmd", args: { cmd: "echo danger" } });
    emit({ type: "confirm_request", tool: "run_cmd", detail: "echo danger" });
    return;
  }

  for (const piece of ["Add", "ed ", "mul()."]) {
    emit({ type: "text_delta", text: piece });
  }
  emit({ type: "text", text: "Added mul()." });
  emit({ type: "verify_passed", check: "tests" });
  emit({ type: "turn_end", usage: usage() });
});

let turns = 0;
function usage() {
  turns += 1;
  return { tokens_in: 100 * turns, tokens_out: 10 * turns, est_cost_usd: 0.0, turns };
}

rl.on("close", () => {
  emit({
    type: "session_end",
    session_id: "fake123",
    files_changed: ["calc.py"],
    usage: usage(),
    transcript: "/tmp/fake123.json",
  });
  process.exit(0);
});
