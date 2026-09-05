# Apprentice for VS Code

Chat with your local or cloud models through OpenCode. Apprentice checks candidate
edits before delivering them to your project; failed work stays isolated. Tests catch
some mistakes, but are not proof of correctness.

Requires Apprentice **0.3.0**, OpenCode **1.18.25**, Git, and a configured model endpoint.
Local inference uses llama.cpp, not Ollama. No models ship in this VSIX.

Open **Apprentice: Open Agent Panel** (`Ctrl+Shift+A`), choose your model and:

- **Ask:** explain or inspect code, read-only (default).
- **Plan:** investigate and propose a plan, read-only.
- **Build:** change an isolated copy, then run the configured checks before delivery.

Roles focus one worker: General, Explorer, Implementer, Reviewer. Explorer and Reviewer
stay read-only; these are not an automatic parallel team.

The panel has streaming answers, collapsed tools, VS Code theme/fonts, multiline
drafts, code-copy buttons, approvals, Stop, New Session, Resume and task-specific diffs.
Raw reasoning and internal checkpoints stay out of chat. Mid-task messages queue;
Stop cancels the active task and clears the queue.

## Setup

Backend discovery checks `apprentice.executable`, then `apprentice` on PATH, then
`apprentice.pythonPath` + `apprentice.repoPath`. Use **Check Setup (doctor)** to see
what it found. The Python backend and OpenCode must be installed separately.

| Setting | Purpose |
| --- | --- |
| `apprentice.pythonPath` / `repoPath` | Interpreter and Apprentice source checkout |
| `apprentice.provider` / `model` | Initial model selection |
| `apprentice.verify` | Project tests, configured language gate, or explicit unverified delivery |
| `apprentice.testCommand` | Acceptance command, such as `npm test` |
| `apprentice.autoApproveCommands` | Explicit shell auto-approval; off by default |
| `apprentice.allowDirty` | Legacy-only; OpenCode snapshots saved dirty Git files automatically |

Save editor buffers before tasks. Dependency folders are not copied: configure
approved workspace setup where necessary. Worktrees isolate edits, not OS access.

Full setup, migration, safety limits and testing:
[Agent guide](https://github.com/m-555/Apprentice/blob/main/docs/AGENT.md).

## Development

```text
npm ci
npm run build
npm run typecheck
npm test
npm run test:host
npm run package
```

`test:host` requires `APPRENTICE_TEST_VSCODE` pointing to the installed Code executable.
It uses a separate profile and fake model server; no GPU inference.

License: MIT.
