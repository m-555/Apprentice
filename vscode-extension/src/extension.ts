/**
 * Extension entry point: commands, status bar, and the chat view registration.
 *
 * Everything here is a FRONTEND concern. The agent's behavior lives in the Python CLI;
 * if something is missing, the fix belongs in the protocol, not in this file.
 */

import { execFile } from "child_process";
import * as vscode from "vscode";
import { AgentSettings, runArgs, chatArgs } from "./config";
import { ChatViewProvider, pickFolder } from "./chatView";
import { locate, NotFoundError, Resolved } from "./locate";
import { Usage } from "./protocol";
import { runInTerminal } from "./terminal";

const LAST_SESSION_KEY = "apprentice.lastSessionId";

let log: vscode.OutputChannel;
let status: vscode.StatusBarItem;
let view: ChatViewProvider;

function settings(): AgentSettings {
  const c = vscode.workspace.getConfiguration("apprentice");
  return {
    provider: c.get<string>("provider"),
    model: c.get<string>("model"),
    verify: c.get<string>("verify"),
    testCommand: c.get<string>("testCommand"),
    autoApproveCommands: c.get<boolean>("autoApproveCommands"),
    allowDirty: c.get<boolean>("allowDirty"),
  };
}

/** Resolve the agent, or show an actionable error and return null. */
async function resolveOrReport(): Promise<Resolved | null> {
  const c = vscode.workspace.getConfiguration("apprentice");
  try {
    return locate({
      executable: c.get<string>("executable"),
      pythonPath: c.get<string>("pythonPath"),
      repoPath: c.get<string>("repoPath"),
    });
  } catch (err) {
    if (err instanceof NotFoundError) {
      log.appendLine(err.message);
      const pick = await vscode.window.showErrorMessage(
        "Apprentice was not found. Install it, or point the extension at your checkout.",
        "Open Settings",
        "Show Log"
      );
      if (pick === "Open Settings") {
        void vscode.commands.executeCommand("workbench.action.openSettings", "apprentice");
      } else if (pick === "Show Log") {
        log.show();
      }
      return null;
    }
    throw err;
  }
}

/** Run a short CLI sub-command and capture its output (doctor, sessions). */
function capture(resolved: Resolved, args: string[], cwd?: string): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      resolved.command,
      [...resolved.prefixArgs, ...args],
      { cwd, windowsHide: true, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        const text = (stdout || "") + (stderr || "");
        if (err && !text) reject(err);
        else resolve(text);
      }
    );
  });
}

function updateStatus(usage: Usage | null, provider?: string, verify?: string): void {
  const c = vscode.workspace.getConfiguration("apprentice");
  const prov = provider || c.get<string>("provider") || "default";
  const ver = verify || c.get<string>("verify") || "config";
  let text = `$(beaker) ${prov} | ${ver}`;
  if (usage && usage.est_cost_usd > 0) text += ` | $${usage.est_cost_usd.toFixed(4)}`;
  else if (usage && usage.turns) text += ` | ${usage.turns} turns`;
  status.text = text;
  status.tooltip = usage
    ? `Apprentice — tokens in/out ${usage.tokens_in}/${usage.tokens_out}, ` +
      `estimated $${usage.est_cost_usd.toFixed(4)}`
    : "Apprentice — click for actions";
}

export function activate(context: vscode.ExtensionContext): void {
  log = vscode.window.createOutputChannel("Apprentice");
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "apprentice.statusMenu";
  updateStatus(null);
  status.show();

  view = new ChatViewProvider(context, log, updateStatus);
  context.subscriptions.push(
    log,
    status,
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, view, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    { dispose: () => view.dispose() }
  );

  const register = (id: string, fn: (...a: any[]) => any) =>
    context.subscriptions.push(vscode.commands.registerCommand(id, fn));

  register("apprentice.openPanel", () => view.reveal());
  register("apprentice.newSession", () => view.newSession());
  register("apprentice.stop", () => view.stop());
  register("apprentice.showLog", () => log.show());

  // Slash commands the agent already understands — surfaced as real UI affordances.
  const slash = (id: string, line: string, idleHint: string) =>
    register(id, () => {
      if (!view.sendCommand(line)) {
        vscode.window.showInformationMessage(idleHint);
      }
    });
  slash("apprentice.undo", "/undo", "No running Apprentice session to undo.");
  slash("apprentice.cost", "/cost", "No running Apprentice session yet.");
  slash("apprentice.files", "/files", "No running Apprentice session yet.");

  // --- Phase 1: terminal ---------------------------------------------------
  register("apprentice.chatInTerminal", async () => {
    const folder = await pickFolder();
    const resolved = folder && (await resolveOrReport());
    if (!folder || !resolved) return;
    runInTerminal(resolved, chatArgs(settings(), { repo: folder.uri.fsPath }),
                  folder.uri.fsPath);
  });

  /** Shared prompt for both headless entry points. */
  async function askTask(): Promise<{ task: string; doneWhen: string } | undefined> {
    const task = await vscode.window.showInputBox({
      title: "Apprentice: run a task until it passes",
      prompt: "What should the agent do?",
      placeHolder: "e.g. add a formatBytes(n) helper in src/utils.ts",
      ignoreFocusOut: true,
    });
    if (!task) return undefined;
    const doneWhen = await vscode.window.showInputBox({
      title: "Acceptance command",
      prompt: "A command that exits 0 when the task is done",
      value:
        vscode.workspace.getConfiguration("apprentice").get<string>("testCommand") || "",
      placeHolder: "e.g. npx vitest run",
      ignoreFocusOut: true,
    });
    if (!doneWhen) return undefined;
    return { task, doneWhen };
  }

  // Headless runs in the PANEL by default — same rendering (tool rows, verification
  // badges, changed-file diffs) as an interactive session.
  register("apprentice.run", async () => {
    const folder = await pickFolder();
    if (!folder) return;
    const spec = await askTask();
    if (!spec) return;
    view.reveal();
    await view.startHeadless(folder, spec.task, spec.doneWhen);
  });

  register("apprentice.runInTerminal", async () => {
    const folder = await pickFolder();
    if (!folder) return;
    const spec = await askTask();
    if (!spec) return;
    const resolved = await resolveOrReport();
    if (!resolved) return;
    runInTerminal(
      resolved,
      runArgs(settings(), { repo: folder.uri.fsPath, ...spec }),
      folder.uri.fsPath
    );
  });

  // --- sessions ------------------------------------------------------------
  register("apprentice.resumeLast", async () => {
    const id = context.workspaceState.get<string>(LAST_SESSION_KEY);
    if (!id) {
      vscode.window.showInformationMessage(
        "No previous Apprentice session for this workspace yet."
      );
      return;
    }
    view.reveal();
    await view.resume(id);
  });

  register("apprentice.sessions", async () => {
    const resolved = await resolveOrReport();
    if (!resolved) return;
    let out: string;
    try {
      out = await capture(resolved, ["sessions"]);
    } catch (err) {
      vscode.window.showErrorMessage(`Apprentice: ${String(err)}`);
      return;
    }
    // `apprentice sessions` prints "  <id>  <provider>  <repo>" then an indented task line.
    const items: vscode.QuickPickItem[] = [];
    let pending: vscode.QuickPickItem | null = null;
    for (const raw of out.split(/\r?\n/)) {
      const line = raw.trimEnd();
      const head = line.match(/^\s{2}(\S+)\s+(\S+)\s+(.*)$/);
      if (head) {
        if (pending) items.push(pending);
        pending = { label: head[1], description: head[2], detail: head[3] };
      } else if (pending && line.trim() && !line.startsWith("Resume")) {
        pending.detail = `${pending.detail} — ${line.trim()}`;
      }
    }
    if (pending) items.push(pending);
    if (items.length === 0) {
      vscode.window.showInformationMessage("No saved Apprentice sessions found.");
      return;
    }
    const pick = await vscode.window.showQuickPick(items, {
      title: "Resume an Apprentice session",
      placeHolder: "Pick a session",
    });
    if (!pick) return;
    view.reveal();
    await view.resume(pick.label);
  });

  // --- settings quick-picks -------------------------------------------------
  register("apprentice.setProvider", async () => {
    const value = await vscode.window.showInputBox({
      title: "Apprentice: model provider",
      prompt: "Provider name (empty = use Apprentice's own config)",
      value: vscode.workspace.getConfiguration("apprentice").get<string>("provider") || "",
      placeHolder: "qwen | gemini | openai | a provider you configured",
    });
    if (value === undefined) return;
    await vscode.workspace
      .getConfiguration("apprentice")
      .update("provider", value, vscode.ConfigurationTarget.Workspace);
    updateStatus(null);
  });

  register("apprentice.setVerify", async () => {
    const pick = await vscode.window.showQuickPick(
      [
        { label: "tests", detail: "Compile/lint AND your project's tests must pass" },
        { label: "gate", detail: "Edited files must compile/lint" },
        { label: "off", detail: "No checking — edits land immediately" },
        { label: "(use Apprentice's config)", detail: "Clear the VS Code override" },
      ],
      { title: "Apprentice: verification mode" }
    );
    if (!pick) return;
    const value = pick.label.startsWith("(") ? "" : pick.label;
    await vscode.workspace
      .getConfiguration("apprentice")
      .update("verify", value, vscode.ConfigurationTarget.Workspace);
    updateStatus(null);
  });

  register("apprentice.doctor", async () => {
    const resolved = await resolveOrReport();
    if (!resolved) return;
    log.show();
    log.appendLine(`\n$ apprentice doctor   (resolved via: ${resolved.source})`);
    try {
      log.appendLine(await capture(resolved, ["doctor"]));
    } catch (err) {
      log.appendLine(`failed: ${String(err)}`);
    }
  });

  register("apprentice.statusMenu", async () => {
    const pick = await vscode.window.showQuickPick(
      [
        { label: "$(comment-discussion) Open Agent Panel", id: "apprentice.openPanel" },
        { label: "$(terminal) Start Chat in Terminal", id: "apprentice.chatInTerminal" },
        { label: "$(play) Run Task Until Tests Pass", id: "apprentice.run" },
        { label: "$(history) Open a Past Session…", id: "apprentice.sessions" },
        { label: "$(server-process) Select Model Provider…", id: "apprentice.setProvider" },
        { label: "$(shield) Select Verification Mode…", id: "apprentice.setVerify" },
        { label: "$(pulse) Check Setup (doctor)", id: "apprentice.doctor" },
        { label: "$(output) Show Log", id: "apprentice.showLog" },
      ],
      { title: "Apprentice" }
    );
    if (pick) void vscode.commands.executeCommand((pick as any).id);
  });

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("apprentice")) updateStatus(null);
    })
  );
}

export function deactivate(): void {
  view?.dispose();
}
