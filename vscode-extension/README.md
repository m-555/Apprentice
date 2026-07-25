# Apprentice for VS Code

A coding agent in your sidebar, driven by **your own model** — a free local one via Ollama,
or a cloud model (Gemini, GPT/Codex, Groq, …). No subscription to any particular vendor.

What makes it different: **nothing broken survives a turn.** After each turn the agent's
changes are checked against your project's own tests, and anything that fails is
**automatically reverted** with the error handed back to the model to fix. That's what
makes a cheap, weaker model safe to point at real code.

> This extension is a **frontend only**. All the intelligence — tools, verification,
> budgets, escalation — lives in the [Apprentice](https://github.com/m-555/Apprentice)
> agent, which it launches and talks to over a JSON event protocol.

## Requirements

Apprentice itself must be installed:

```bash
pipx install git+https://github.com/m-555/Apprentice.git
apprentice init
```

The extension finds it automatically in this order:

1. the `apprentice.executable` setting,
2. `apprentice` on your `PATH`,
3. `apprentice.pythonPath` + `apprentice.repoPath` (for a source checkout).

Run **Apprentice: Check Setup (doctor)** any time to see what it found and whether your
model backend is reachable.

## Using it

- **Apprentice: Open Agent Panel** — the sidebar chat. Type what you want changed.
- **Apprentice: Run Task Until Tests Pass** — unattended: give a task and an acceptance
  command, and the agent grinds until it passes.
- **Apprentice: Start Chat in Terminal** — the plain CLI REPL, if you prefer it.

In the panel you'll see each tool call as it happens (click a row to expand its output),
a green **verified** badge when a turn passes, and a red **reverted** badge with the real
test output when it doesn't. Changed files appear as chips — click one to open a diff
against `HEAD`.

When the agent wants to run a shell command that isn't on the allowlist, you get an
**Allow / Deny** prompt right in the panel. Nothing runs behind your back.

## Settings

| Setting | Purpose |
|---|---|
| `apprentice.executable` / `pythonPath` / `repoPath` | Where Apprentice lives (usually auto-detected) |
| `apprentice.provider` / `model` | Which model to use (e.g. `qwen`, or `gemini` + `pro`) |
| `apprentice.verify` | `off` (no checking) · `gate` (must compile/lint) · `tests` (must pass your tests) |
| `apprentice.testCommand` | How to run this project's tests, e.g. `npx vitest run` |
| `apprentice.autoApproveCommands` | Run shell commands without asking |
| `apprentice.allowDirty` | Allow starting on a non-git or uncommitted tree |

Every setting is optional — leave it empty and Apprentice's own configuration decides. The
extension never overrides what you didn't set.

## Notes

- **Git is your undo.** The agent refuses to start on a dirty tree by default, so
  `git diff` always shows exactly what it did.
- Replies stream in token-by-token for local (Ollama) and OpenAI-compatible providers.
  Vertex/Gemini replies arrive complete instead.
- A local 7B–80B model is genuinely weaker than a frontier model on long, multi-step work.
  Verification means its mistakes get caught — not that it stops making them.

## License

MIT.
