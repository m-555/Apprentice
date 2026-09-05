/**
 * The bridge to a running `apprentice … --json` process.
 *
 * Deliberately dumb: it spawns, parses stdout into typed events, and writes lines to
 * stdin. It contains NO agent logic — no tools, no verification, no provider calls. If
 * the UI needs something new, the fix is a new event in the protocol, not behavior here.
 */

import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import { EventEmitter } from "events";
import { AgentEvent, EventParser } from "./protocol";

export interface SpawnOptions {
  command: string;
  args: string[];
  cwd: string;
  env?: NodeJS.ProcessEnv;
}

export declare interface AgentProcess {
  on(event: "event", listener: (ev: AgentEvent) => void): this;
  on(event: "stderr", listener: (text: string) => void): this;
  on(event: "invalid", listener: (line: string) => void): this;
  on(event: "exit", listener: (code: number | null) => void): this;
  on(event: "error", listener: (err: Error) => void): this;
}

export class AgentProcess extends EventEmitter {
  private child: ChildProcessWithoutNullStreams | null = null;
  private parser = new EventParser();
  private stopping = false;
  private ready = false;
  private protocolVersion = 1;

  get running(): boolean {
    return this.child !== null && this.child.exitCode === null;
  }
  get version(): number { return this.protocolVersion; }

  start(opts: SpawnOptions): void {
    if (this.running) throw new Error("agent already running");
    this.parser = new EventParser();
    this.stopping = false;
    this.ready = false;
    this.protocolVersion = 1;

    // NOTE: args array, shell:false. Never build a command string — that is what keeps
    // Windows cmd from re-tokenizing values like --test-cmd "npx vitest run".
    const child = spawn(opts.command, opts.args, {
      cwd: opts.cwd,
      env: { ...process.env, ...(opts.env || {}) },
      shell: false,
      windowsHide: true,
    });
    this.child = child;

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      const { events, invalid } = this.parser.push(chunk);
      for (const ev of events) {
        if (ev.type === "session_start") {
          this.ready = true;
          this.protocolVersion = Number(ev.protocol_version || 1);
        }
        this.emit("event", ev);
      }
      for (const line of invalid) this.emit("invalid", line);
    });

    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (text: string) => this.emit("stderr", text));

    child.on("error", (err) => this.emit("error", err));
    child.stdin.on("error", err => { if (!this.stopping) this.emit("error", err); });
    child.on("close", (code) => {
      const rest = this.parser.flush();
      if (rest) this.emit("invalid", rest);
      if (this.child === child) this.child = null;
      this.emit("exit", this.stopping ? 0 : code);
    });
  }

  /** Send one line to the agent's stdin (a user message, a slash command, or an answer). */
  send(line: string): void {
    if (!this.child) throw new Error("agent is not running");
    this.child.stdin.write((this.protocolVersion >= 2
      ? JSON.stringify({ type: "user", text: line }) : line.replace(/\r?\n/g, " ")) + "\n");
  }

  sendControl(value: Record<string, unknown>): void {
    if (!this.child) throw new Error("agent is not running");
    this.child.stdin.write(JSON.stringify(value) + "\n");
  }

  async waitUntilReady(): Promise<void> {
    const deadline = Date.now() + 15000;
    while (!this.ready) {
      if (!this.running) throw new Error("Apprentice exited before its session was ready.");
      if (Date.now() > deadline) throw new Error("Apprentice did not announce a session within 15 seconds.");
      await new Promise(resolve => setTimeout(resolve, 25));
    }
  }

  /** Answer a `confirm_request` — the agent reads exactly one line. */
  answerConfirm(allow: boolean, requestId?: string): void {
    this.sendControl(this.protocolVersion >= 2 ? { type: "answer", request_id: requestId, allow } : { allow });
  }

  cancel(): void {
    if (this.protocolVersion >= 2) this.sendControl({ type: "cancel" });
    else void this.stop();
  }

  /** Close stdin so a chat session ends the way EOF would in a terminal. */
  end(): void {
    this.child?.stdin.end();
  }

  /** Stop now. SIGINT first (lets the agent save its transcript), then hard kill. */
  async stop(): Promise<void> {
    if (!this.child) return;
    this.stopping = true;
    const child = this.child;
    const exited = new Promise<void>(resolve => child.once("close", () => resolve()));
    if (this.protocolVersion >= 2) {
      this.cancel();
      this.send("/quit");
    } else child.kill("SIGINT");
    let timer: NodeJS.Timeout | undefined;
    await Promise.race([exited, new Promise<void>(resolve => {
      timer = setTimeout(resolve, 15000);
    })]);
    if (timer) clearTimeout(timer);
    if (this.child === child) {
      if (process.platform === "win32") {
        const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true });
        await new Promise<void>(resolve => { killer.once("close", () => resolve()); killer.once("error", () => resolve()); });
      } else child.kill("SIGKILL");
      await exited;
    }
  }
}
