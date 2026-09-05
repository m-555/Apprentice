# The Apprentice agent

Apprentice 0.3 uses OpenCode for `chat`, `run`, MCP `assign`, and the VS Code panel.
Apprentice supervises verification, delivery, memory and budgets; llama.cpp runs the
local model. No model weights or inference tuning were changed by this migration.

## Before and after

| Before | Now |
| --- | --- |
| MCP `assign` used Aider; chat used a separate home-built loop. | OpenCode handles conversation, file reads and coding tools in all three frontends. |
| Explanation requests could turn into coding tasks. | Ask is read-only by default; Plan is read-only too. Select Build to edit. |
| Aider started from the last commit; chat edited real files then attempted rollback. | Each task snapshots your current saved Git files, including relevant uncommitted changes. |
| File-tool snapshots could miss shell-created changes. | Delivery compares the scoped worktree, including additions, deletions and shell edits. |
| Failed edits needed to be reverted from your checkout. | Failed candidates remain isolated. Bounded retries can fix them; otherwise a patch is retained. |
| Diffs/undo could mix your work with agent changes. | Diffs show exact task snapshots. Apply and Undo refuse to overwrite newer edits. |
| Stop/New/Resume could race with old processes. | Stop aborts the native request; replacements wait for cleanup. Native history uses a stable, freshly recreated task directory. |
| Model choice mostly required commands/settings. | The panel has model, mode and role selectors, approvals, code-copy buttons and task diffs. |
| Learning could require loading an embedding model. | OpenCode retrieves repository-scoped lessons without model loading; inspect or disable them. |

Passing tests is evidence, not proof of correctness or security. A model can still
misunderstand a request, and a test can miss a bug.

## Setup and everyday use

Requirements: Python 3.11+, Git with at least one commit, OpenCode **1.18.25**, and
a reachable configured provider. Install the runtime with
`npm install -g opencode-ai@1.18.25`; run `apprentice doctor` to check setup.
Opening the panel or listing models does not load a model.

```text
apprentice chat --repo E:\projects\your-project
apprentice chat --mode build --test-cmd "npm test"
apprentice chat --resume SESSION_ID
apprentice run "Fix the parser" --done-when "npm test"
apprentice catalog
```

In VS Code open **Apprentice: Open Agent Panel**, select a model and Ask/Plan/Build,
then chat. Save dirty editor buffers first: the snapshot uses files on disk.
Short assistant progress, final answers and collapsed tool activity are shown;
raw reasoning and internal compaction messages are not rendered as conversation.
Formatting is deliberately conservative: plain text, inline code and fenced code.

Roles focus one worker: General, Explorer, Implementer, Reviewer. Explorer and Reviewer
remain read-only even with Build selected. This release does not create an automatic
parallel team: the native delegation tool is disabled. An external orchestrator can
still call MCP `assign`. A cross-process lock prevents simultaneous Apprentice tasks
on the same repository.

New messages during work **queue** for the next turn. Stop cancels the task and clears
that queue. It does not unload the shared model server automatically; another client
may be using it. Existing supervisor idle-unload settings remain in force.

## Verification and safe delivery

| Policy | Before delivery |
| --- | --- |
| `tests` | The caller-configured project command must succeed. Without a command, falls back to `gate`, reported by the check result. |
| `gate` | Configured language checks run on supported changed files. If none apply, delivery is refused. This is not full project validation. |
| `off` | Explicitly unverified delivery at completion, still conflict-checked; not shown as test-verified. |

Existing acceptance files matching `opencode.protected_checks` cannot change unless
`opencode.allow_acceptance_edits` is explicitly enabled. Defaults cover `tests/*`,
`test_*`, `*.test.*`, `*.spec.*`; add your project's other acceptance/build files.
This is a practical guard, not complete protection against malicious code.

Dependency folders are not copied. Use an absolute Python interpreter or configure
`workspace.setup_command`, for example `npm ci --ignore-scripts`, where necessary.
Setup asks permission and may use the network. The acceptance command was supplied
by the caller and runs automatically, including a baseline check before editing.
Only run trusted tests and commands.

Defaults: 24 OpenCode steps per attempt, up to 3 attempts, repeated-tool protection,
bounded tool output, 30-minute model-loop deadline, separately bounded subprocess
checks, and a 256 MiB snapshot cap. Two identical check failures stop retries.
Large asset-heavy repositories may exceed the cap; configure `workspace.max_bytes`.
Maintain `.gitignore` so generated output is not delivered as source changes.

Artifacts are `outputs/<task-id>/changes.patch` and `manifest.json`. Manifests retain
before/after bytes for diff and Undo, including binary changes. Review binaries with
an appropriate viewer. Outputs, lessons and transcripts may contain private code;
keep them local. Stale worktree directories after a hard crash require manual review,
not automatic deletion of files whose ownership is uncertain.

A worktree is **not an OS sandbox**: an approved shell command or test can access the
machine. OpenCode is authenticated and loopback-only, with deny-by-default tools,
external-directory denial, and shell approval in Build. Ask/Plan deny edits and shell.
Global OpenCode configuration and installed tools remain trusted. Project OpenCode
configuration does not configure this worker; project conventions still inform it.

## Models, spending and memory

The selector reads enabled `providers` entries, not an extension-specific list.
The adapter supports `openai-compatible` and `vertex-ai`. Set actual provider
`context_length` and `max_output_tokens`; defaults match Qwen 32K and DeepSeek 16K.
GPU offload, RAM residency and idle timeouts belong to the shared llama.cpp supervisor.

No automatic cloud escalation occurs. Interactive paid models require approval.
MCP `assign` has no interactive approval channel: only exact commands in
`opencode.approved_commands` (plus `done_when`) and explicitly configured
`opencode.approved_paid_providers` are allowed. `--yes` auto-approves commands,
not paid models. Set current provider prices for useful cost estimates.

Completed native messages, including compaction, are metered once. A hook checks
daily budgets before each subsequent model request. An in-flight call can exceed
the remaining cap; missing final usage is reported as incomplete. This is accounting,
not prepaid reservation across every process on the machine.

If a failed candidate is repaired and project tests pass, a lesson records the repo,
failure, verified patch and evidence path. These examples do not train model weights
and are not instructions for the next task. Bounded keyword retrieval excludes other
repositories. Disable it with `agent_chat.use_corrections=false` or
`retrieval.enabled=false`.

```text
apprentice lessons list --repo E:\projects\your-project
apprentice lessons show LESSON_ID
apprentice lessons disable LESSON_ID
apprentice lessons enable LESSON_ID
```

MCP `log_correction(..., repo=...)` can add a scoped review correction. Unscoped legacy
records are retained, but not silently injected into OpenCode tasks. Direct `delegate`
retains its existing snippet pipeline and vector retrieval; its contract is unchanged.

## Compatibility and testing

`apprentice chat/run --backend legacy` selects the old standalone loop. For legacy
`assign`, set `agent.backend="aider"`; its separate venv remains optional. Aider is not
uninstalled and history is not deleted. Old chats resume as an explicit handoff, not
a fake native OpenCode session. See [legacy documentation](AGENT_LEGACY.md).

```powershell
.venv/Scripts/python.exe tests/test_pipeline.py
.venv/Scripts/python.exe tests/test_opencode.py
$env:APPRENTICE_TEST_OPENCODE='1'
.venv/Scripts/python.exe tests/test_opencode.py OpenCodeContractTest.test_real_opencode_reads_edits_and_finishes_with_fake_model
cd vscode-extension
npm run build
npm run typecheck
npm test
$env:APPRENTICE_TEST_VSCODE='D:\Microsoft VS Code\Code.exe'
npm run test:host
```

The host test uses a temporary VS Code profile, disposable repository and scripted
HTTP model, with GPU rendering disabled. It runs actual Python/OpenCode processes and
covers selection, Ask, Build, independent tests, diff documents, Resume, panel reload,
approval denial, active-request cancellation and New Session.

Live GPU tests are separate and opt-in:
`python tests/live_opencode.py --provider qwen --allow-gpu` (or your DeepSeek provider).
They require an idle, unloaded router, use disposable code and unload their model.
