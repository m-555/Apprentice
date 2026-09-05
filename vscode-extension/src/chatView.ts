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
import * as path from "path";
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
  private sessionInfo?: { id: string; provider: string; model: string; verify: string; mode?: string; role?: string };
  /** True while a turn is in flight — an exit here is a CRASH, not a normal end. */
  private inTurn = false;
  private stoppedByUser = false;
  /** An `error` event means the agent refused ON PURPOSE (dirty tree, bad provider).
   *  The exit that follows is a clean shutdown, NOT a crash — don't cry wolf. */
  private sawError = false;
  private replay: AgentEvent[] = [];
  private pendingStart?: Promise<boolean>;
  private manifests = new Map<string, string>();
  private sessionChange?: Promise<void>;
  private questionCancellation?: vscode.CancellationTokenSource;

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
  <div id="selectors">
    <label>Model<select id="model-select" aria-label="Model"><option value="">Loading models…</option></select></label>
    <label>Mode<select id="mode-select" aria-label="Mode"><option value="ask">Ask</option><option value="plan">Plan</option><option value="build">Build</option></select></label>
    <label>Role<select id="role-select" aria-label="Role"><option>general</option><option>explorer</option><option>implementer</option><option>reviewer</option></select></label>
  </div>
  <div id="log" role="log" aria-live="polite"></div>
  <div id="changed" class="changed hidden"></div>
  <div id="composer">
    <textarea id="input" rows="3" aria-label="Message" placeholder="Ask about your code, make a plan, or select Build to make changes…"></textarea>
    <button id="send" title="Send (Enter)">Send</button>
    <button id="stop" class="secondary" title="Cancel the current task">Stop</button>
  </div>
  <script nonce="${nonce}" src="${asset("main.js")}"></script>
</body>
</html>`;
  }

  // --- messages from the webview -------------------------------------------
  private async onMessage(msg: any): Promise<void> {
    switch (msg?.type) {
      case "ready": {
        if (this.sessionChange) await this.sessionChange;
        // The webview reloads when the view is hidden/shown; re-sync the state it
        // can't know by itself.
        this.post({ type: "busy", busy: this.inTurn });
        this.post({ type: "event", event: { type: "history_v2", events: this.replay } });
        if (!this.agent.running) {
          const folder = await pickFolder();
          if (folder) await this.ensureStarted(folder);
        }
        this.post({ type: "changed", files: [...this.changed] });
        const c = vscode.workspace.getConfiguration("apprentice");
        this.post({
          type: "header",
          provider: this.sessionInfo?.provider || c.get<string>("provider") || "default",
          model: this.sessionInfo?.model || c.get<string>("model") || "",
          verify: this.sessionInfo?.verify || c.get<string>("verify") || "",
          session: this.sessionInfo?.id || "",
          mode: this.sessionInfo?.mode,
          role: this.sessionInfo?.role,
        });
        break;
      }
      case "user":
        await this.sendUser(String(msg.text || ""));
        break;
      case "confirm":
        if (this.agent.running) {
          this.agent.answerConfirm(Boolean(msg.allow), msg.request_id);
          // The agent resumes work after an approval — show the indicator again.
          if (msg.allow) this.post({ type: "busy", busy: true });
        }
        break;
      case "openDiff":
        if (this.repo && typeof msg.path === "string") {
          await showDiff(this.repo, msg.path, this.manifests.get(msg.path));
        }
        break;
      case "stop":
        this.stop();
        break;
      case "select":
        if (this.inTurn) {
          this.post({ type: "notice", text: "Stop or finish this task before changing its model, mode, or role." });
          return;
        }
        if (this.agent.running) this.agent.sendControl({ type: "settings", provider: msg.provider, model: msg.model, mode: msg.mode, role: msg.role });
        break;
      case "copy":
        if (typeof msg.text === "string") await vscode.env.clipboard.writeText(msg.text);
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
    if (!vscode.workspace.isTrusted) {
      this.post({ type: "notice", text: "Trust this workspace before starting a coding agent." });
      return false;
    }
    if (this.pendingStart) return this.pendingStart;
    this.pendingStart = this.startSession(folder, resume, headless);
    try { return await this.pendingStart; } finally { this.pendingStart = undefined; }
  }

  private async startSession(folder: vscode.WorkspaceFolder, resume?: string,
    headless?: { task: string; doneWhen: string; autoApprove: boolean }): Promise<boolean> {
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
            `Unfinished OpenCode edits remain isolated. Review the last delivery result and log.`
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
      await this.agent.waitUntilReady();
    } catch (err) {
      this.post({ type: "notice", level: "error", text: String(err) });
      return false;
    }
    return true;
  }

  private async sendUser(text: string): Promise<void> {
    if (!text.trim()) return;
    if (this.sessionChange) await this.sessionChange;
    // Mid-turn requests queue; Stop discards that queue if the task is replaced.
    if (this.agent.running && this.inTurn) {
      this.agent.send(text);
      this.post({ type: "notice", text: "Message queued. Use Stop first if it replaces the current task." });
      return;
    }
    const folder = await pickFolder();
    if (!folder) return;
    if (!(await this.ensureStarted(folder))) { this.post({ type: "busy", busy: false }); return; }
    if (vscode.workspace.textDocuments.some(doc => {
      const relative = path.relative(this.repo, doc.uri.fsPath);
      return doc.isDirty && doc.uri.scheme === "file" && !relative.startsWith("..") && !path.isAbsolute(relative);
    })) {
      this.post({ type: "notice", text: "Save your edited files before sending. Apprentice snapshots files on disk, not unsaved buffers." });
      this.post({ type: "busy", busy: false });
      return;
    }
    this.inTurn = true;
    this.post({ type: "busy", busy: true });
    if (this.agent.version >= 2) this.agent.sendControl({ type: "user", text, diagnostics: collectDiagnostics(this.repo) });
    else this.agent.send(text);
  }

  private changeSession(action: () => Promise<void>): Promise<void> {
    const pending = (this.sessionChange || Promise.resolve()).then(action);
    this.sessionChange = pending;
    void pending.finally(() => { if (this.sessionChange === pending) this.sessionChange = undefined; }).catch(() => undefined);
    return pending;
  }

  newSession(): Promise<void> { return this.changeSession(() => this.newSessionNow()); }
  private async newSessionNow(): Promise<void> {
    if (this.pendingStart) await this.pendingStart;
    this.stoppedByUser = true;
    await this.agent.stop();
    this.changed.clear();
    this.replay = [];
    this.post({ type: "clear" });
    const folder = await pickFolder();
    if (folder) await this.ensureStarted(folder);
  }

  /**
   * Run a headless task IN THE PANEL rather than a terminal, so its tool calls,
   * verification badges and diffs render like any other session.
   */
  async startHeadless(folder: vscode.WorkspaceFolder, task: string,
                      doneWhen: string): Promise<void> {
    return this.changeSession(() => this.startHeadlessNow(folder, task, doneWhen));
  }
  private async startHeadlessNow(folder: vscode.WorkspaceFolder, task: string, doneWhen: string): Promise<void> {
    if (this.pendingStart) await this.pendingStart;
    this.stoppedByUser = true;
    await this.agent.stop();
    this.changed.clear();
    this.replay = [];
    this.post({ type: "clear" });
    this.inTurn = true;
    this.post({ type: "busy", busy: true });
    const ok = await this.ensureStarted(folder, undefined,
                                        { task, doneWhen, autoApprove: false });
    if (!ok) {
      this.inTurn = false;
      this.post({ type: "busy", busy: false });
    }
  }

  resume(sessionId: string): Promise<void> { return this.changeSession(() => this.resumeNow(sessionId)); }
  private async resumeNow(sessionId: string): Promise<void> {
    const folder = await pickFolder();
    if (!folder) return;
    if (this.pendingStart) await this.pendingStart;
    this.stoppedByUser = true;
    await this.agent.stop();
    this.replay = [];
    this.changed.clear();
    this.post({ type: "clear" });
    await this.ensureStarted(folder, sessionId);
  }

  stop(): void {
    this.questionCancellation?.cancel();
    if (this.agent.running) {
      this.stoppedByUser = true;
      this.agent.cancel();
      this.post({ type: "notice", level: "warn", text: "Stopping the task and cleaning up…" });
    }
  }

  /** Send a slash command (/undo, /cost, /files) to a running session. */
  sendCommand(line: string): boolean {
    if (!this.agent.running) return false;
    if (this.inTurn) {
      this.post({ type: "notice", text: "Finish or stop the active task before sending a session command." });
      return true;
    }
    this.agent.send(line);
    return true;
  }

  async selectModel(provider: string, model: string): Promise<void> {
    const folder = await pickFolder();
    if (folder && await this.ensureStarted(folder)) await this.onMessage({ type: "select", provider, model });
  }

  reveal(): void {
    void vscode.commands.executeCommand(`${ChatViewProvider.viewType}.focus`);
  }

  dispose(): void {
    void this.agent.stop();
  }

  // --- agent events ---------------------------------------------------------
  private onAgentEvent(ev: AgentEvent): void {
    if (ev.type !== "session_start" && ev.session_id && this.sessionInfo && ev.session_id !== this.sessionInfo.id) return;
    if (ev.type === "history_v2") {
      const catalog = this.replay.filter(item => item.type === "catalog");
      this.replay = [...catalog, ...(ev.events as AgentEvent[])];
      this.changed.clear(); this.manifests.clear();
      for (const item of this.replay) {
        if (item.type === "delivery" && item.applied) for (const file of item.files_changed as string[]) {
          this.changed.add(file); this.manifests.set(file, String(item.manifest_path));
        }
        if (item.type === "ack" && item.reverted) for (const file of item.reverted as string[]) {
          this.changed.delete(file); this.manifests.delete(file);
        }
      }
      ev = { ...ev, events: this.replay };
      this.post({ type: "changed", files: [...this.changed] });
    }
    else if (ev.type === "message_part" || ev.type === "tool_part") {
      const index = this.replay.findIndex(item => item.id === ev.id);
      if (index < 0) this.replay.push(ev); else this.replay[index] = ev;
    } else if (ev.type === "message_remove") this.replay = this.replay.filter(item => item.message_id !== ev.message_id);
    else if (ev.type === "part_remove") this.replay = this.replay.filter(item => item.id !== ev.id);
    else if (!["turn_end", "task_status", "session_start", "session_end"].includes(ev.type)) this.replay.push(ev);
    switch (ev.type) {
      case "task_status":
        this.inTurn = true;
        this.post({ type: "busy", busy: true });
        break;
      case "delivery":
        if (ev.applied && typeof ev.manifest_path === "string") for (const file of ev.files_changed as string[]) this.manifests.set(file, ev.manifest_path);
        if (ev.applied) for (const file of ev.files_changed as string[]) this.changed.add(file);
        this.post({ type: "changed", files: [...this.changed] });
        break;
      case "question_request":
        void this.answerQuestion(ev);
        break;
      case "session_start": {
        const s = ev as SessionStartEvent;
        this.stoppedByUser = false;
        this.manifests.clear();
        this.repo = s.repo || this.repo;
        this.sessionInfo = { id: s.session_id, provider: s.provider,
                             model: s.model, verify: s.verify, mode: String(ev.mode || "ask"), role: String(ev.role || "general") };
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
        this.questionCancellation?.cancel();
        this.inTurn = false;
        this.stoppedByUser = false;
        this.sawError = false;
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
        if (this.agent.running) this.agent.sendControl({ id: req.id, result });
        break;
      }
      case "ack":
        this.inTurn = false;
        if (this.sessionInfo) for (const key of ["provider", "model", "mode", "role", "verify"] as const) {
          if (typeof ev[key] === "string") this.sessionInfo[key] = String(ev[key]);
        }
        this.post({ type: "busy", busy: false });
        if (ev.reverted) for (const file of ev.reverted as string[]) { this.changed.delete(file); this.manifests.delete(file); }
        this.post({ type: "changed", files: [...this.changed] });
        break;
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

  private async answerQuestion(ev: AgentEvent): Promise<void> {
    this.questionCancellation?.cancel();
    const cancellation = new vscode.CancellationTokenSource();
    this.questionCancellation = cancellation;
    try {
    const answers: string[][] = [];
    for (const question of ev.questions as { question: string; options: { label: string; description: string }[]; multiple?: boolean }[]) {
      const value = await vscode.window.showInputBox({ prompt: question.question,
        placeHolder: (question.options || []).map(item => item.label).join(" / "), ignoreFocusOut: true }, cancellation.token);
      if (value === undefined) {
        if (this.agent.running) this.agent.sendControl({ type: "answer", request_id: ev.request_id, allow: false });
        return;
      }
      answers.push([value]);
    }
    if (this.agent.running) this.agent.sendControl({ type: "answer", request_id: ev.request_id, answers });
    } finally {
      cancellation.dispose();
      if (this.questionCancellation === cancellation) this.questionCancellation = undefined;
    }
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
