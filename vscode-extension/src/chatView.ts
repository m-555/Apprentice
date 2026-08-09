/**
 * The sidebar chat panel.
 *
 * Extension host  <--postMessage-->  webview (no file/process access, by design)
 *        |
 *        +-- AgentProcess (spawn apprentice --json, stdin/stdout)
 *
 * The webview only renders and collects input; every privileged action (spawning,
 * reading files, opening diffs) happens here in the host.
 */

import * as vscode from "vscode";
import { AgentProcess } from "./agentProcess";
import { AgentSettings, chatArgs, runArgs } from "./config";
import { collectDiagnostics } from "./diagnostics";
import { showDiff } from "./diffs";
import { locate, NotFoundError } from "./locate";
import { AgentEvent, MUTATING_TOOLS, SessionEndEvent, SessionStartEvent, ToolCallEvent, Usage } from "./protocol";

const LAST_SESSION_KEY = "apprentice.lastSessionId";

export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "apprentice.chatView";

  private view?: vscode.WebviewView;
  private agent = new AgentProcess();
  private repo = "";
  private changed = new Set<string>();
  private sessionInfo?: { id: string; provider: string; model: string; verify: string };
  /** True while a turn is in flight — an exit here is a CRASH, not a normal end. */
  private inTurn = false;
  private stoppedByUser = false;
  /** An `error` event means the agent refused ON PURPOSE (dirty tree, bad provider).
   *  The exit that follows is a clean shutdown, NOT a crash — don't cry wolf. */
  private sawError = false;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly log: vscode.OutputChannel,
    private readonly onUsage: (usage: Usage | null, provider?: string, verify?: string) => void
  ) {}

  // --- webview lifecycle ---------------------------------------------------
  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, "media")],
    };
    view.webview.html = this.html(view.webview);
    view.webview.onDidReceiveMessage((msg) => this.onMessage(msg));
  }

  private post(msg: unknown): void {
    this.view?.webview.postMessage(msg);
  }

  private html(webview: vscode.Webview): string {
    const asset = (name: string) =>
      webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, "media", name));
    // A nonce-based CSP is required for webview scripts; no external resources at all.
    const nonce = Array.from({ length: 32 }, () =>
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789".charAt(
        Math.floor(Math.random() * 62)
      )
    ).join("");
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
  style-src ${webview.cspSource}; script-src 'nonce-${nonce}'; img-src ${webview.cspSource};">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="${asset("main.css")}" rel="stylesheet">
<title>Apprentice</title>
</head>
<body>
  <div id="header">
    <span id="hdr-provider" class="pill">not started</span>
    <span id="hdr-verify" class="pill"></span>
    <span id="hdr-session" class="pill dim"></span>
  </div>
  <div id="log" role="log" aria-live="polite"></div>
  <div id="changed" class="changed hidden"></div>
  <div id="composer">
    <textarea id="input" rows="2" placeholder="Ask Apprentice to change something…"></textarea>
    <button id="send" title="Send (Enter)">Send</button>
  </div>
  <script nonce="${nonce}" src="${asset("main.js")}"></script>
</body>
</html>`;
  }

  // --- messages from the webview -------------------------------------------
  private async onMessage(msg: any): Promise<void> {
    switch (msg?.type) {
      case "ready": {
        // The webview reloads when the view is hidden/shown; re-sync the state it
        // can't know by itself.
        this.post({ type: "busy", busy: false });
        this.post({ type: "changed", files: [...this.changed] });
        const c = vscode.workspace.getConfiguration("apprentice");
        this.post({
          type: "header",
          provider: this.sessionInfo?.provider || c.get<string>("provider") || "default",
          model: this.sessionInfo?.model || c.get<string>("model") || "",
          verify: this.sessionInfo?.verify || c.get<string>("verify") || "",
          session: this.sessionInfo?.id || "",
        });
        break;
      }
      case "user":
        await this.sendUser(String(msg.text || ""));
        break;
      case "confirm":
        if (this.agent.running) {
          this.agent.answerConfirm(Boolean(msg.allow));
          // The agent resumes work after an approval — show the indicator again.
          if (msg.allow) this.post({ type: "busy", busy: true });
        }
        break;
      case "openDiff":
        if (this.repo && typeof msg.path === "string") {
          await showDiff(this.repo, msg.path);
        }
        break;
      case "stop":
        this.stop();
        break;
    }
  }

  // --- session control ------------------------------------------------------
  private settings(): AgentSettings {
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

  async ensureStarted(
    folder: vscode.WorkspaceFolder,
    resume?: string,
    headless?: { task: string; doneWhen: string; autoApprove: boolean }
  ): Promise<boolean> {
    if (this.agent.running) return true;
    const c = vscode.workspace.getConfiguration("apprentice");
    let resolved;
    try {
      resolved = locate({
        executable: c.get<string>("executable"),
        pythonPath: c.get<string>("pythonPath"),
        repoPath: c.get<string>("repoPath"),
      });
    } catch (err) {
      if (err instanceof NotFoundError) {
        this.log.appendLine(err.message);
        const pick = await vscode.window.showErrorMessage(
          "Apprentice was not found on this machine.",
          "Open Settings",
          "Show Log"
        );
        if (pick === "Open Settings") {
          void vscode.commands.executeCommand(
            "workbench.action.openSettings",
            "apprentice"
          );
        } else if (pick === "Show Log") {
          this.log.show();
        }
        return false;
      }
      throw err;
    }

    this.repo = folder.uri.fsPath;
    this.changed.clear();
    this.sawError = false;
    // Headless runs `apprentice run <task> --done-when <cmd>`; interactive runs `chat`.
    // Both stream the SAME --json protocol, so the panel renders them identically.
    const settings = headless?.autoApprove
      ? { ...this.settings(), autoApproveCommands: true }
      : this.settings();
    const args = [
      ...resolved.prefixArgs,
      ...(headless
        ? runArgs(settings, { repo: this.repo, task: headless.task,
                              doneWhen: headless.doneWhen, json: true })
        : chatArgs(settings, { repo: this.repo, resume, json: true })),
      // We can answer host_request events (editor diagnostics) — the CLI only offers
      // the get_diagnostics tool to the model when a frontend advertises this.
      "--host-tools",
    ];
    this.log.appendLine(`[start:${resolved.source}] ${resolved.command} ${args.join(" ")}`);

    this.agent.removeAllListeners();
    this.agent.on("event", (ev) => this.onAgentEvent(ev));
    this.agent.on("stderr", (text) => this.log.append(text));
    this.agent.on("invalid", (line) => this.log.appendLine(`[non-json] ${line}`));
    this.agent.on("error", (err) => {
      this.post({ type: "notice", level: "error", text: `Failed to start: ${err.message}` });
      this.log.appendLine(`[spawn error] ${err.message}`);
    });
    this.agent.on("exit", (code) => {
      // Dying MID-TURN is a crash (Python traceback, OOM, killed process), not the
      // normal end of a session. Say so, and make the log one click away — otherwise
      // the panel just goes quiet and the user has no idea what happened.
      const crashed = this.inTurn && !this.stoppedByUser && !this.sawError;
      this.post({
        type: "exit",
        crashed,
        text: crashed
          ? `The agent stopped unexpectedly mid-task (exit code ${code ?? "unknown"}). ` +
            `Nothing further was changed; your files are as the last verified turn left them.`
          : "Agent session ended.",
      });
      this.inTurn = false;
      this.stoppedByUser = false;
      this.sawError = false;
      this.sessionInfo = undefined;
      void vscode.commands.executeCommand("setContext", "apprentice.running", false);
      this.onUsage(null);
      if (crashed) void this.reportCrash(code);
    });

    try {
      this.agent.start({ command: resolved.command, args, cwd: this.repo });
    } catch (err) {
      this.post({ type: "notice", level: "error", text: String(err) });
      return false;
    }
    return true;
  }

  private async sendUser(text: string): Promise<void> {
    if (!text.trim()) return;
    // Mid-turn, this is STEERING: the agent picks it up between steps and changes
    // course, instead of the message queuing until the whole task finishes.
    if (this.agent.running && this.inTurn) {
      this.agent.send(text);
      this.post({ type: "steering", text });
      return;
    }
    const folder = await pickFolder();
    if (!folder) return;
    if (!(await this.ensureStarted(folder))) return;
    this.inTurn = true;
    this.post({ type: "busy", busy: true });
    this.agent.send(text);
  }

  async newSession(): Promise<void> {
    this.stop();
    this.changed.clear();
    this.post({ type: "clear" });
  }

  /**
   * Run a headless task IN THE PANEL rather than a terminal, so its tool calls,
   * verification badges and diffs render like any other session.
   */
  async startHeadless(folder: vscode.WorkspaceFolder, task: string,
                      doneWhen: string): Promise<void> {
    this.stop();
    this.changed.clear();
    this.post({ type: "clear" });
    this.inTurn = true;
    this.post({ type: "busy", busy: true });
    const ok = await this.ensureStarted(folder, undefined,
                                        { task, doneWhen, autoApprove: true });
    if (!ok) {
      this.inTurn = false;
      this.post({ type: "busy", busy: false });
    }
  }

  async resume(sessionId: string): Promise<void> {
    const folder = await pickFolder();
    if (!folder) return;
    this.stop();
    this.post({ type: "clear" });
    await this.ensureStarted(folder, sessionId);
  }

  stop(): void {
    if (this.agent.running) {
      this.stoppedByUser = true;
      this.agent.stop();
      this.post({ type: "notice", level: "warn", text: "Stopped." });
    }
    this.post({ type: "busy", busy: false });
  }

  /** Send a slash command (/undo, /cost, /files) to a running session. */
  sendCommand(line: string): boolean {
    if (!this.agent.running) return false;
    this.agent.send(line);
    return true;
  }

  reveal(): void {
    void vscode.commands.executeCommand(`${ChatViewProvider.viewType}.focus`);
  }

  dispose(): void {
    this.agent.stop();
  }

  // --- agent events ---------------------------------------------------------
  private onAgentEvent(ev: AgentEvent): void {
    switch (ev.type) {
      case "session_start": {
        const s = ev as SessionStartEvent;
        this.repo = s.repo || this.repo;
        this.sessionInfo = { id: s.session_id, provider: s.provider,
                             model: s.model, verify: s.verify };
        void this.context.workspaceState.update(LAST_SESSION_KEY, s.session_id);
        void vscode.commands.executeCommand("setContext", "apprentice.running", true);
        this.post({ type: "header", provider: s.provider, model: s.model,
                    verify: s.verify, session: s.session_id });
        this.onUsage(null, s.provider, s.verify);
        break;
      }
      case "tool_call": {
        const call = ev as ToolCallEvent;
        const p = call.args?.["path"];
        if (MUTATING_TOOLS.has(call.tool) && typeof p === "string") {
          this.changed.add(p);
          this.post({ type: "changed", files: [...this.changed] });
        }
        break;
      }
      case "turn_end": {
        this.inTurn = false;
        this.post({ type: "busy", busy: false });
        this.onUsage((ev as any).usage as Usage);
        break;
      }
      case "session_end": {
        this.inTurn = false;
        const s = ev as SessionEndEvent;
        for (const f of s.files_changed || []) this.changed.add(f);
        this.post({ type: "changed", files: [...this.changed] });
        this.onUsage(s.usage);
        break;
      }
      case "host_request": {
        // The agent wants something only the editor knows. Reply on stdin with its id.
        const req = ev as any;
        let result = "";
        try {
          result = req.kind === "diagnostics"
            ? collectDiagnostics(this.repo, typeof req.path === "string" && req.path
                                            ? req.path : undefined)
            : `ERROR: unsupported host request '${req.kind}'`;
        } catch (err) {
          result = `ERROR: ${String(err)}`;
        }
        if (this.agent.running) this.agent.send(JSON.stringify({ id: req.id, result }));
        break;
      }
      case "error": {
        // The agent refuses a dirty/non-git tree — make that actionable instead of raw.
        this.sawError = true;
        const text = String((ev as any).text || "");
        if (/uncommitted changes|not a git repository/i.test(text)) {
          void this.offerAllowDirty(text);
        }
        break;
      }
    }
    this.post({ type: "event", event: ev });
  }

  /** A mid-turn death needs an actionable notification, not just a panel line. */
  private async reportCrash(code: number | null): Promise<void> {
    this.log.appendLine(`[crash] agent exited with code ${code} during a turn`);
    const pick = await vscode.window.showErrorMessage(
      `Apprentice stopped unexpectedly (exit ${code ?? "unknown"}).`,
      "Show Log",
      "Retry"
    );
    if (pick === "Show Log") {
      this.log.show();
    } else if (pick === "Retry") {
      const folder = await pickFolder();
      if (folder) await this.ensureStarted(folder);
    }
  }

  private async offerAllowDirty(text: string): Promise<void> {
    const pick = await vscode.window.showWarningMessage(
      text,
      "Run anyway (allow dirty)",
      "Open Settings"
    );
    if (pick === "Run anyway (allow dirty)") {
      await vscode.workspace
        .getConfiguration("apprentice")
        .update("allowDirty", true, vscode.ConfigurationTarget.Workspace);
      vscode.window.showInformationMessage(
        "Apprentice: allowDirty enabled for this workspace. Send your message again."
      );
    } else if (pick === "Open Settings") {
      void vscode.commands.executeCommand("workbench.action.openSettings", "apprentice");
    }
  }
}

/** The workspace folder to work in; asks when a multi-root workspace is open. */
export async function pickFolder(): Promise<vscode.WorkspaceFolder | undefined> {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) {
    vscode.window.showErrorMessage(
      "Apprentice needs an open folder — the agent works inside a repository."
    );
    return undefined;
  }
  if (folders.length === 1) return folders[0];
  return vscode.window.showWorkspaceFolderPick({
    placeHolder: "Which folder should Apprentice work in?",
  });
}
