/**
 * Reviewing what the agent changed.
 *
 * No custom diff rendering — VS Code's own diff editor is better than anything we'd
 * build. We just construct the "before" side from git HEAD via the built-in Git
 * extension's URI scheme and hand both sides to the `vscode.diff` command.
 */

import * as vscode from "vscode";
import * as fs from "fs/promises";
import * as path from "path";

const snapshots = new Map<string, string>();
export function registerTaskDiffs(context: vscode.ExtensionContext): void {
  context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider("apprentice-task", {
    provideTextDocumentContent: uri => snapshots.get(uri.toString()) || "",
  }));
}

/**
 * Build a `git:` URI for the HEAD version of a file, using the built-in Git extension's
 * scheme. Returns null when the Git extension isn't available (then we just open the file).
 */
function headUri(fileUri: vscode.Uri): vscode.Uri | null {
  const git = vscode.extensions.getExtension("vscode.git");
  if (!git) return null;
  // The git extension registers a content provider for this scheme; `ref: ""` means the
  // index/HEAD version depending on the query, and "HEAD" is what we want to compare to.
  return fileUri.with({
    scheme: "git",
    query: JSON.stringify({ path: fileUri.fsPath, ref: "HEAD" }),
  });
}

/** Open `relPath` (relative to `repo`) as a diff against HEAD, or plain if unavailable. */
export async function showDiff(repo: string, relPath: string, manifestPath?: string): Promise<void> {
  const relative = path.relative(path.resolve(repo), path.resolve(repo, relPath));
  if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("Diff path escapes the workspace.");
  const fileUri = vscode.Uri.joinPath(vscode.Uri.file(repo), relPath);
  if (manifestPath) {
    if ((await fs.stat(manifestPath)).size > 384 * 1024 * 1024) throw new Error("Task diff manifest is too large to open safely.");
    const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
    if (path.resolve(manifest.repo) !== path.resolve(repo)) throw new Error("Task diff belongs to another workspace.");
    const change = manifest.changes[relPath];
    if (!change) throw new Error("This file was not changed by the task.");
    const bytes = ["before", "after"].map(side => Buffer.from(change[side].data || "", "base64"));
    if (bytes.some(value => value.includes(0))) {
      await vscode.window.showInformationMessage("This is a binary change. Exact before/after bytes are retained in the task manifest; use a binary-aware review tool.");
      return;
    }
    if (snapshots.size > 200) snapshots.clear();
    const uris = ["before", "after"].map(side => {
      const uri = vscode.Uri.from({ scheme: "apprentice-task", path: `/${path.basename(path.dirname(manifestPath))}/${side}/${relPath}` });
      snapshots.set(uri.toString(), change[side].data === null ? "" : Buffer.from(change[side].data, "base64").toString("utf8"));
      return uri;
    });
    await vscode.commands.executeCommand("vscode.diff", uris[0], uris[1], `${relPath} (this task)`, { preview: true });
    return;
  }
  const before = headUri(fileUri);
  if (!before) {
    await vscode.window.showTextDocument(fileUri, { preview: true });
    return;
  }
  try {
    await vscode.commands.executeCommand(
      "vscode.diff",
      before,
      fileUri,
      `${relPath} (agent changes)`,
      { preview: true }
    );
  } catch {
    // New files have no HEAD side; showing the file itself is the sensible fallback.
    await vscode.window.showTextDocument(fileUri, { preview: true });
  }
}
