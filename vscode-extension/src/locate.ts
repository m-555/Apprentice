/**
 * Finding the Apprentice agent on the user's machine.
 *
 * This is the #1 source of "the extension doesn't work", so resolution is explicit and
 * reported honestly: we return WHICH strategy matched, and on failure a message that
 * names what we looked for.
 *
 * Order (first hit wins):
 *   1. `apprentice.executable` setting — an explicit path or a command name.
 *   2. `apprentice` on PATH — the pip/pipx install case.
 *   3. `apprentice.pythonPath` + `<apprentice.repoPath>/src/cli.py` — the source-checkout
 *      case (what a developer of Apprentice itself has).
 */

import * as fs from "fs";
import * as path from "path";

export interface Resolved {
  /** Executable to spawn. */
  command: string;
  /** Leading args that must precede the sub-command (e.g. the cli.py path). */
  prefixArgs: string[];
  /** Which strategy matched — shown in the log so setup problems are diagnosable. */
  source: "setting" | "path" | "checkout";
}

export interface LocateSettings {
  executable?: string;
  pythonPath?: string;
  repoPath?: string;
}

/** Directories from PATH, plus the platform's executable extensions. */
function pathCandidates(name: string, env: NodeJS.ProcessEnv): string[] {
  const dirs = (env.PATH || env.Path || "").split(path.delimiter).filter(Boolean);
  const exts =
    process.platform === "win32"
      ? (env.PATHEXT || ".EXE;.CMD;.BAT").split(";").filter(Boolean)
      : [""];
  const out: string[] = [];
  for (const dir of dirs) {
    for (const ext of exts) {
      out.push(path.join(dir, name + ext));
    }
  }
  return out;
}

export function onPath(name: string, env: NodeJS.ProcessEnv = process.env): string | null {
  for (const candidate of pathCandidates(name, env)) {
    try {
      if (fs.statSync(candidate).isFile()) {
        return candidate;
      }
    } catch {
      /* not there — keep looking */
    }
  }
  return null;
}

export class NotFoundError extends Error {
  constructor(public readonly tried: string[]) {
    super(
      "Could not find Apprentice. Tried: " +
        tried.join("; ") +
        ". Install it (pipx install git+https://github.com/m-555/Apprentice.git) or set " +
        "apprentice.executable, or apprentice.pythonPath + apprentice.repoPath for a checkout."
    );
  }
}

/**
 * Resolve how to launch Apprentice. Throws NotFoundError (with what was tried) when no
 * strategy matches — callers turn that into an actionable notification.
 */
export function locate(
  settings: LocateSettings,
  env: NodeJS.ProcessEnv = process.env
): Resolved {
  const tried: string[] = [];

  const explicit = (settings.executable || "").trim();
  if (explicit) {
    // An explicit setting is trusted even if we can't stat it (it may be a shim or a
    // command name resolved by the shell); a wrong value fails loudly at spawn time.
    return { command: explicit, prefixArgs: [], source: "setting" };
  }
  tried.push("apprentice.executable setting (empty)");

  const found = onPath("apprentice", env);
  if (found) {
    return { command: found, prefixArgs: [], source: "path" };
  }
  tried.push("'apprentice' on PATH");

  const python = (settings.pythonPath || "").trim();
  const repo = (settings.repoPath || "").trim();
  if (python && repo) {
    const cli = path.join(repo, "src", "cli.py");
    if (fs.existsSync(cli)) {
      // -u: unbuffered, so --json events reach us as they happen rather than in blocks.
      return { command: python, prefixArgs: ["-u", cli], source: "checkout" };
    }
    tried.push(`${cli} (not found)`);
  } else {
    tried.push("apprentice.pythonPath + apprentice.repoPath (not both set)");
  }

  throw new NotFoundError(tried);
}
