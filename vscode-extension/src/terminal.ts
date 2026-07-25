/**
 * Phase 1: run the agent in VS Code's integrated terminal.
 *
 * Kept alongside the panel on purpose — the terminal is the honest fallback when
 * something is wrong with the panel, and some people simply prefer the REPL.
 */

import * as vscode from "vscode";
import { commandLine } from "./config";
import { Resolved } from "./locate";

const TERMINAL_NAME = "Apprentice";

export function runInTerminal(
  resolved: Resolved,
  args: string[],
  cwd: string
): vscode.Terminal {
  // Reuse the Apprentice terminal if it is idle, so repeated runs don't pile up tabs.
  const existing = vscode.window.terminals.find((t) => t.name === TERMINAL_NAME);
  const terminal =
    existing ?? vscode.window.createTerminal({ name: TERMINAL_NAME, cwd });
  terminal.show();
  terminal.sendText(commandLine(resolved.command, [...resolved.prefixArgs, ...args]));
  return terminal;
}
