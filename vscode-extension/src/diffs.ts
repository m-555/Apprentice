/**
 * Reviewing what the agent changed.
 *
 * No custom diff rendering — VS Code's own diff editor is better than anything we'd
 * build. We just construct the "before" side from git HEAD via the built-in Git
 * extension's URI scheme and hand both sides to the `vscode.diff` command.
 */

import * as vscode from "vscode";

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
export async function showDiff(repo: string, relPath: string): Promise<void> {
  const fileUri = vscode.Uri.joinPath(vscode.Uri.file(repo), relPath);
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
