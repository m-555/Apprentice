# Change Log — Apprentice for VS Code

All notable changes to the extension. The [Apprentice agent](https://github.com/m-555/Apprentice)
itself is versioned separately; see its `CHANGELOG.md`.

## [0.2.0] — 2026-09-05

- Requires Apprentice 0.3 with the OpenCode task controller.
- Model/mode/role selectors, read-only Ask/Plan, and verified Build delivery.
- Native message identities prevent duplicate bubbles; internal reasoning/checkpoints
  are filtered by the backend. Code blocks support Copy and VS Code theme fonts.
- Stop cancels upstream work; New/Resume wait for cleanup. Restored transcripts retain
  selections, active approval state and task-specific before/after diffs.
- Added real extension-host and renderer tests. Long model waits show elapsed status.

## [0.1.3] — 2026-08-09

### Fixed

- **Resuming a session no longer shows an empty panel.** The transcript was cleared on
  resume and nothing redrew it, because the protocol had no way to say what had been said.
  The agent now sends a `history` event after `session_start`, and the panel replays the
  conversation between *earlier in this session* / *resumed here* markers.

### Notes

- Needs the matching agent build. Three fixes that make the panel usable live on the
  Python side: the first message no longer deadlocks the session, Gemini tool calls no
  longer fail on the following request, and a plain greeting no longer sets the agent off
  running tests. See the agent's `CHANGELOG.md`.

## [0.1.0] — 2026-07-25

First release. A frontend for the Apprentice agent — it spawns the CLI and renders its
`--json` event stream; all agent behavior stays in the Python side.

### Added

- **Agent panel** (Activity Bar sidebar): streaming replies, tool calls as expandable
  rows, green **verified** / red **reverted** badges, and a header showing the active
  provider, model and verification mode.
- **Machine-verified edits made visible** — when a change fails the project's tests it is
  reverted, and the panel shows the verbatim failure rather than a silent retry.
- **Inline command approval** — a shell command that isn't allowlisted shows Allow/Deny in
  the panel; nothing runs behind your back.
- **Changed-file chips** that open VS Code's native diff against `HEAD`.
- **Headless runs in the panel** — *Run Task Until Tests Pass* asks for a task and an
  acceptance command, then renders the whole grind; a terminal variant is also available.
- **Terminal mode** — *Start Chat in Terminal* for the plain CLI REPL.
- **Sessions** — transcripts are saved by the agent; resume the last one or pick from a
  list. Slash controls (`/undo`, `/cost`, `/files`) are surfaced as commands and toolbar
  buttons.
- **Setup discovery** — finds `apprentice` via the `apprentice.executable` setting, then
  `PATH`, then a source checkout (`pythonPath` + `repoPath`); *Check Setup (doctor)* runs
  the agent's own environment check.
- **Crash handling** — if the agent dies mid-turn you get an explicit message plus
  **Show Log** / **Retry**, instead of the panel going quiet.
- **Keybindings** — `Ctrl/Cmd+Shift+A` opens the panel, `Ctrl/Cmd+Shift+N` starts a new
  session, `Escape` stops a running agent while the panel is focused.
- **Settings** for provider, model, verification mode, test command, command
  auto-approval and dirty-tree override. Every setting is optional: leave it empty and the
  agent's own configuration decides.

### Also renders
- `escalation_offer` — the agent is stuck and asks to move to a stronger (paid) model:
  **Switch / Stay** buttons in the panel.
- `ask` — any yes/no decision from the agent (e.g. approving a plan).
- `nudge` — shown when the agent is caught repeating itself and told to change approach.

### Notes

- The agent must be installed separately (`pipx install git+https://github.com/m-555/Apprentice.git`).
- Assistant text streams for local (Ollama) and OpenAI-compatible providers; Vertex/Gemini
  replies arrive complete rather than token-by-token.
