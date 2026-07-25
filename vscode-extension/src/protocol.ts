/**
 * TypeScript mirror of Apprentice's `--json` event schema (see docs/AGENT.md).
 *
 * The schema is ADDITIVE on the agent side: new fields may appear, existing ones are not
 * renamed. So `AgentEvent` keeps an index signature and unknown `type`s are tolerated —
 * an older extension must never crash on a newer agent.
 */

export interface BaseEvent {
  ts: string;
  type: string;
  [key: string]: unknown;
}

export interface SessionStartEvent extends BaseEvent {
  type: "session_start";
  session_id: string;
  repo: string;
  provider: string;
  model: string;
  verify: string;
  test_cmd: string;
  mode?: "headless";
  task?: string;
  done_when?: string;
  resumed?: boolean;
}

export interface Usage {
  tokens_in: number;
  tokens_out: number;
  est_cost_usd: number;
  turns: number;
}

export interface SessionEndEvent extends BaseEvent {
  type: "session_end";
  session_id: string;
  files_changed: string[];
  usage: Usage;
  transcript: string;
  done_passed?: boolean;
  rounds?: number;
  done_log_tail?: string;
}

export interface ToolCallEvent extends BaseEvent {
  type: "tool_call";
  tool: string;
  args?: Record<string, unknown>;
}

export interface ToolResultEvent extends BaseEvent {
  type: "tool_result";
  tool: string;
  text?: string;
}

export interface VerifyEvent extends BaseEvent {
  type: "verify_passed" | "verify_failed";
  check: string;
  text?: string;
}

export interface ConfirmRequestEvent extends BaseEvent {
  type: "confirm_request";
  tool: string;
  detail: string;
}

export interface TextEvent extends BaseEvent {
  type: "text" | "user" | "stopped" | "error" | "escalated";
  text: string;
}

/**
 * A token fragment of the reply being generated. Many arrive per turn, and the complete
 * message still follows as a `text` event — so a renderer must show the deltas live and
 * then NOT duplicate the final text.
 */
export interface TextDeltaEvent extends BaseEvent {
  type: "text_delta";
  text: string;
}

export interface TurnEndEvent extends BaseEvent {
  type: "turn_end";
  usage: Usage;
}

export interface AckEvent extends BaseEvent {
  type: "ack";
  command: string;
}

export type AgentEvent =
  | SessionStartEvent
  | SessionEndEvent
  | ToolCallEvent
  | ToolResultEvent
  | VerifyEvent
  | ConfirmRequestEvent
  | TextEvent
  | TextDeltaEvent
  | TurnEndEvent
  | AckEvent
  | BaseEvent;

/** Tools whose `path` argument means a file was modified. */
export const MUTATING_TOOLS = new Set(["write_file", "edit_file"]);

/** The argument worth showing next to a tool name in a compact row. */
export function toolDetail(ev: ToolCallEvent): string {
  const args = ev.args || {};
  for (const key of ["path", "cmd", "pattern", "dir", "summary"]) {
    const value = args[key];
    if (typeof value === "string" && value) return value;
  }
  return "";
}

/**
 * Split a stdout chunk stream into complete JSON events.
 *
 * Stateful because a chunk boundary can fall mid-line. Malformed lines are returned as
 * `invalid` rather than thrown: a stray print from a provider SDK must not kill a session.
 */
export class EventParser {
  private buffer = "";

  push(chunk: string): { events: AgentEvent[]; invalid: string[] } {
    const events: AgentEvent[] = [];
    const invalid: string[] = [];
    this.buffer += chunk;

    let index: number;
    while ((index = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      if (!line) continue;
      try {
        const parsed = JSON.parse(line);
        if (parsed && typeof parsed === "object" && typeof parsed.type === "string") {
          events.push(parsed as AgentEvent);
        } else {
          invalid.push(line);
        }
      } catch {
        invalid.push(line);
      }
    }
    return { events, invalid };
  }

  /** Anything left unterminated when the process exits (usually nothing). */
  flush(): string {
    const rest = this.buffer;
    this.buffer = "";
    return rest.trim();
  }
}
