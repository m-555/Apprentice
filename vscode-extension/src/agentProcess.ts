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

  get running(): boolean {
    return this.child !== null && this.child.exitCode === null;
  }

  start(opts: SpawnOptions): void {
    if (this.running) throw new Error("agent already running");
    this.parser = new EventParser();
    this.stopping = false;

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
      for (const ev of events) this.emit("event", ev);
      for (const line of invalid) this.emit("invalid", line);
    });

    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (text: string) => this.emit("stderr", text));

    child.on("error", (err) => this.emit("error", err));
    child.on("close", (code) => {
      const rest = this.parser.flush();
      if (rest) this.emit("invalid", rest);
      this.child = null;
      this.emit("exit", this.stopping ? 0 : code);
    });
  }

  /** Send one line to the agent's stdin (a user message, a slash command, or an answer). */
  send(line: string): void {
    if (!this.child) throw new Error("agent is not running");
    this.child.stdin.write(line.replace(/\r?\n/g, " ") + "\n");
  }

  /** Answer a `confirm_request` — the agent reads exactly one line. */
  answerConfirm(allow: boolean): void {
    this.send(JSON.stringify({ allow }));
  }

  /** Close stdin so a chat session ends the way EOF would in a terminal. */
  end(): void {
    this.child?.stdin.end();
  }

  /** Stop now. SIGINT first (lets the agent save its transcript), then hard kill. */
  stop(): void {
    if (!this.child) return;
    this.stopping = true;
    const child = this.child;
    try {
      child.kill("SIGINT");
    } catch {
      /* already gone */
    }
    setTimeout(() => {
      if (child.exitCode === null) {
        try {
          child.kill("SIGKILL");
        } catch {
          /* already gone */
        }
      }
    }, 2000);
  }
}
