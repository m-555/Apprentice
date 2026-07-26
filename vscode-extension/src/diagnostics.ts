/**
 * Answering the agent's `host_request` for editor diagnostics.
 *
 * This is the one capability the extension has that a headless agent cannot get: real
 * language-server output — type errors, unresolved imports, lint — without running a
 * build. The agent asks, we read `vscode.languages.getDiagnostics()`, and reply on stdin.
 */

import * as vscode from "vscode";

const SEVERITY = ["error", "warning", "info", "hint"];

/** Format the workspace's diagnostics (optionally one file) as compact text. */
export function collectDiagnostics(repo: string, relPath?: string): string {
  const root = vscode.Uri.file(repo).fsPath.toLowerCase();
  const wanted = relPath
    ? vscode.Uri.joinPath(vscode.Uri.file(repo), relPath).fsPath.toLowerCase()
    : "";

  const lines: string[] = [];
  let files = 0;
  for (const [uri, diags] of vscode.languages.getDiagnostics()) {
    const fsPath = uri.fsPath;
    const lower = fsPath.toLowerCase();
    if (!lower.startsWith(root)) continue;          // never leak outside the repo
    if (wanted && lower !== wanted) continue;
    if (diags.length === 0) continue;

    // Errors first — that's what the agent should act on.
    const sorted = [...diags].sort((a, b) => a.severity - b.severity);
    const rel = fsPath.slice(repo.length).replace(/^[\\/]/, "").replace(/\\/g, "/");
    files += 1;
    for (const d of sorted.slice(0, 40)) {
      const sev = SEVERITY[d.severity] ?? "info";
      const line = d.range.start.line + 1;
      const col = d.range.start.character + 1;
      const src = d.source ? ` [${d.source}]` : "";
      lines.push(`${rel}:${line}:${col}: ${sev}${src}: ${d.message.split("\n")[0]}`);
    }
    if (sorted.length > 40) lines.push(`${rel}: … ${sorted.length - 40} more`);
    if (lines.length > 300) {
      lines.push("… (truncated)");
      break;
    }
  }

  if (lines.length === 0) {
    return relPath
      ? `No problems reported for ${relPath}.`
      : "No problems reported in this workspace.";
  }
  return `${lines.length} problem(s) in ${files} file(s):\n` + lines.join("\n");
}
