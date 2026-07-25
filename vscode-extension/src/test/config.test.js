// Settings -> CLI args, and executable resolution.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chatArgs, runArgs, commandLine } = require("../../dist/test-bundle.js");
const { locate, onPath, NotFoundError } = require("../../dist/test-bundle.js");

test("an empty setting contributes no flag", () => {
  // VS Code settings must not fight the agent's own config when the user set nothing.
  const args = chatArgs({ provider: "", model: "  ", verify: "", testCommand: "" },
                        { repo: "C:/repo" });
  assert.deepEqual(args, ["chat", "--repo", "C:/repo"]);
});

test("set values become flags, booleans become switches", () => {
  const args = chatArgs(
    { provider: "gemini", model: "pro", verify: "tests", testCommand: "npx vitest run",
      autoApproveCommands: true, allowDirty: true },
    { repo: "/r", json: true }
  );
  assert.deepEqual(args, [
    "chat", "--repo", "/r", "--provider", "gemini", "--model", "pro",
    "--verify", "tests", "--test-cmd", "npx vitest run", "--yes", "--allow-dirty",
    "--json",
  ]);
});

test("test command with spaces stays ONE argv entry", () => {
  // The whole point of spawning with an array: no shell re-tokenization.
  const args = chatArgs({ testCommand: 'npx vitest run "my test.ts"' }, { repo: "/r" });
  const i = args.indexOf("--test-cmd");
  assert.equal(args[i + 1], 'npx vitest run "my test.ts"');
});

test("resume and headless run shapes", () => {
  assert.deepEqual(
    chatArgs({}, { repo: "/r", resume: "abc123" }),
    ["chat", "--repo", "/r", "--resume", "abc123"]
  );
  assert.deepEqual(
    runArgs({}, { repo: "/r", task: "add mul", doneWhen: "pytest -q" }),
    ["run", "add mul", "--done-when", "pytest -q", "--repo", "/r"]
  );
});

test("terminal display quoting only affects display", () => {
  const line = commandLine("apprentice", ["chat", "--test-cmd", "npx vitest run"]);
  assert.equal(line, 'apprentice chat --test-cmd "npx vitest run"');
});

test("locate prefers the explicit setting", () => {
  const r = locate({ executable: "C:/tools/apprentice.exe" }, { PATH: "" });
  assert.equal(r.source, "setting");
  assert.equal(r.command, "C:/tools/apprentice.exe");
  assert.deepEqual(r.prefixArgs, []);
});

test("locate falls back to a source checkout with -u", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "appr-"));
  fs.mkdirSync(path.join(repo, "src"));
  fs.writeFileSync(path.join(repo, "src", "cli.py"), "# stub");
  const r = locate({ pythonPath: "python", repoPath: repo }, { PATH: "" });
  assert.equal(r.source, "checkout");
  assert.equal(r.command, "python");
  // -u keeps the JSON stream unbuffered so events arrive live.
  assert.equal(r.prefixArgs[0], "-u");
  assert.ok(r.prefixArgs[1].endsWith("cli.py"));
});

test("locate reports what it tried when nothing resolves", () => {
  assert.throws(
    () => locate({ pythonPath: "python", repoPath: "/nope" }, { PATH: "" }),
    (err) => {
      assert.ok(err instanceof NotFoundError);
      assert.ok(err.tried.some((t) => t.includes("PATH")));
      assert.ok(err.message.includes("apprentice.executable"));
      return true;
    }
  );
});

test("onPath finds a real file on a synthetic PATH", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "appr-path-"));
  const name = process.platform === "win32" ? "apprentice.CMD" : "apprentice";
  fs.writeFileSync(path.join(dir, name), "");
  const found = onPath("apprentice", { PATH: dir, PATHEXT: ".CMD" });
  assert.ok(found && found.includes("apprentice"));
  assert.equal(onPath("apprentice", { PATH: "", PATHEXT: ".CMD" }), null);
});
