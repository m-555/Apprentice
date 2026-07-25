/**
 * Settings -> CLI arguments. One place, shared by the terminal launcher and the panel.
 *
 * Rule: an EMPTY setting contributes no flag. VS Code settings must never fight the
 * agent's own config (`qwen.local.json`, `<repo>/.qwen-pipeline.json`) — they only
 * override when the user actually set something.
 */

export interface AgentSettings {
  provider?: string;
  model?: string;
  verify?: string;
  testCommand?: string;
  autoApproveCommands?: boolean;
  allowDirty?: boolean;
}

export interface ChatArgOptions {
  repo: string;
  resume?: string;
  json?: boolean;
}

export interface RunArgOptions extends ChatArgOptions {
  task: string;
  doneWhen: string;
}

function common(settings: AgentSettings, repo: string): string[] {
  const args = ["--repo", repo];
  if (settings.provider?.trim()) args.push("--provider", settings.provider.trim());
  if (settings.model?.trim()) args.push("--model", settings.model.trim());
  if (settings.verify?.trim()) args.push("--verify", settings.verify.trim());
  if (settings.testCommand?.trim()) args.push("--test-cmd", settings.testCommand.trim());
  if (settings.autoApproveCommands) args.push("--yes");
  if (settings.allowDirty) args.push("--allow-dirty");
  return args;
}

export function chatArgs(settings: AgentSettings, opts: ChatArgOptions): string[] {
  const args = ["chat", ...common(settings, opts.repo)];
  if (opts.resume) args.push("--resume", opts.resume);
  if (opts.json) args.push("--json");
  return args;
}

export function runArgs(settings: AgentSettings, opts: RunArgOptions): string[] {
  const args = ["run", opts.task, "--done-when", opts.doneWhen,
                ...common(settings, opts.repo)];
  if (opts.json) args.push("--json");
  return args;
}

/**
 * Quote a single argument for display/terminal use ONLY.
 *
 * Process spawning passes an args ARRAY and must never use this — that is what keeps
 * Windows `cmd` from re-tokenizing values like `--test-cmd "npx vitest run"` (the exact
 * class of bug that broke `done_when` twice in this project's history).
 */
export function quoteForTerminal(arg: string): string {
  if (arg.length > 0 && !/[\s"'&|<>^()]/.test(arg)) return arg;
  return '"' + arg.replace(/(["\\])/g, "\\$1") + '"';
}

export function commandLine(command: string, args: string[]): string {
  return [command, ...args].map(quoteForTerminal).join(" ");
}
