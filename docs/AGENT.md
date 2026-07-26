# The Apprentice agent (`apprentice chat` / `apprentice run`)

A coding agent you talk to in your terminal — it reads, searches, edits, and runs things in
your repo — driven by **any single model you choose**: a free local one via Ollama, or a
cloud model (Gemini, GPT/Codex, Groq, …). No orchestrator subscription required.

What makes it different from other agent CLIs: **nothing broken survives a turn.** After
each turn the agent's changes are checked, and anything that fails is **automatically
reverted** with the verbatim error handed back to the model to fix. That's what makes a
cheap, weaker model safe to use on real code.

---

## Quick start

```bash
cd /path/to/your-repo          # must be a git repo with a clean tree
apprentice chat                # uses the default provider (local qwen)
```

```
you > add a formatBytes(n) helper in src/utils.ts with unit tests

  -> read_file(src/utils.ts)
  -> edit_file(src/utils.ts)
  -> run_tests()
  [OK] verified (tests)
```

Useful variants:

```bash
apprentice chat --provider gemini --model pro     # a stronger cloud model
apprentice chat --verify off                      # fast mode, no checking
apprentice chat --resume 2408fdb38fd9             # continue a past session
apprentice run "add mul(a,b) to calc.py" --done-when "pytest -q"   # unattended
apprentice sessions                               # list recent sessions
```

## Verification: the point of this agent

| `--verify` | What must hold for a change to survive |
|---|---|
| `off` | nothing — edits land immediately (a normal agent, like Aider/Cline) |
| `gate` | every edited file passes the mechanical compile/lint gate for its language |
| `tests` | the gate **and** your project's own test command (default) |

On failure the touched files are restored **byte-for-byte**, and the model is told exactly
what broke:

```
  [FAILED] verification (tests) - change REVERTED, agent retrying
```

`tests` needs a test command. Resolution order: `--test-cmd` → `test_cmd` in
`<repo>/.qwen-pipeline.json` → `agent_chat.test_cmd` in your config. With none configured,
`tests` honestly degrades to `gate` rather than claiming a verification it didn't do.

## Plan mode — look before it leaps

`apprentice chat --plan` (or `/plan` mid-session) splits every request in two:

1. **Investigate & propose** — the agent's editing tools are *removed from its toolset*,
   so it physically cannot change anything. It reads, searches, and replies with a
   numbered plan.
2. **Approve & execute** — you say yes, and only then does it get the full toolset.

The guarantee is structural rather than a prompt instruction a weak model might ignore.
`run_tests` stays available while planning: knowing what currently fails is what makes a
plan concrete.

## When the model gets stuck

Two safeguards, both aimed at how *weak* models fail:

- **Thrash detection.** Re-issuing the same tool call with the same arguments
  `agent_chat.repeat_limit` times (default 3) appends a warning to the tool result — the
  one place the model always reads — telling it to change approach. Without this, a stuck
  local model can burn its whole step budget repeating one command.
- **The escalation ladder.** After `agent_chat.escalate_after_failed_verifies` failed
  verifications, the agent climbs `cascade.ladder` — **cheapest rung first**:

  ```jsonc
  "cascade": { "ladder": [
    { "provider": "gemini", "model": "flash", "why": "cheap cloud tier" },
    { "provider": "gemini", "model": "pro",   "why": "strongest tier, pricier" }
  ]}
  ```

  Local (free) → cheap cloud → strong cloud. A rung is skipped unless that provider is
  enabled and under its daily budget. Because cloud tiers cost money, **you are asked
  before each climb** (`escalation_offer`); set `agent_chat.auto_escalate: true` to skip
  the prompt. Headless runs always auto-approve — nobody is watching to answer.

## Learning from past corrections

Every logged correction and machine-verified worker fix is embedded into
`corrections/index.jsonl`. The agent now retrieves the most similar past lessons for your
repo and prepends them to each request, so it stops repeating mistakes this project has
already recorded. Turn off with `agent_chat.use_corrections: false`.

## Slash commands

| Command | Effect |
|---|---|
| `/undo` | revert the agent's last completed turn |
| `/verify off\|gate\|tests` | change the policy mid-session |
| `/plan` · `/plan off` | plan-then-approve before any edit |
| `/provider <name>` · `/model <tier>` | switch model mid-session |
| `/cost` | tokens + estimated spend so far |
| `/files` | files changed this session |
| `/save`, `/help`, `/quit` | transcript is also saved on exit |

## Per-repo setup (optional but recommended)

`<your-repo>/.qwen-pipeline.json`:

```json
{
  "conventions": "TypeScript strict, no any. Zod for validation. Tests colocated as *.test.ts.",
  "test_cmd": "npx vitest run"
}
```

`conventions` goes into the agent's system prompt, so your style is followed from the
start instead of corrected afterwards.

## Safety

The agent writes files and runs commands, so the boundaries are enforced in code, not by
asking the model nicely:

- **Repo-scoped paths.** Every file operation resolves inside the target repo; traversal
  (`../../etc/passwd`) is refused.
- **Command policy.** `agent_chat.denied_cmd_patterns` are *never* run (`rm -rf`,
  `git push`, `curl`, …). `agent_chat.allowed_cmds` (test/build/lint commands) run
  silently. Everything else asks you first — `--yes` disables prompting for unattended use.
- **Git is your undo.** It refuses to start on a non-git or dirty tree (`--allow-dirty` to
  override), so `git diff` always shows exactly what the agent did.
- **Caps.** `max_steps`, `max_seconds`, plus the per-provider daily token/USD budgets. A
  stuck agent stops; it cannot loop forever or drain your cloud credits.

```
  [ESCALATE?] qwen has failed verification repeatedly. Switch to gemini/flash
              (cheap cloud tier) for the rest of this task?
  [ESCALATED] qwen kept failing verification - switching to 'gemini/flash'.
```

## Long sessions

The conversation is compacted automatically when it exceeds
`agent_chat.context_budget_tokens`: the original request and the most recent turns are kept
verbatim, and the middle is replaced by a short digest. Transcripts live in
`<data home>/sessions/` and can be resumed with `--resume <id>`.

## `--json`: the event protocol (for UIs and CI)

`apprentice chat --json` / `apprentice run --json` replace the human output with
**JSON-lines events** — one object per line, flushed immediately. This is the integration
surface for a VS Code extension, a web UI, or a CI script; you never have to parse the
pretty output.

```bash
apprentice run "add mul(a,b) to calc.py" --done-when "pytest -q" --json
```
```json
{"ts":"…","type":"session_start","session_id":"bab410d4","repo":"…","provider":"qwen","verify":"gate","mode":"headless","task":"…","done_when":"…"}
{"ts":"…","type":"tool_call","tool":"read_file","args":{"path":"calc.py"}}
{"ts":"…","type":"tool_result","tool":"read_file","text":"1\t\"\"\"Small helpers.\"\"\"…"}
{"ts":"…","type":"verify_passed","check":"tests"}
{"ts":"…","type":"session_end","session_id":"bab410d4","files_changed":["calc.py"],"usage":{…},"done_passed":true,"rounds":1}
```

| `type` | Meaning | Key fields |
|---|---|---|
| `session_start` | run began | `session_id`, `repo`, `provider`, `model`, `verify`, `test_cmd` (+ `mode`/`task`/`done_when` headless, `resumed` in chat) |
| `user` | your message (chat) | `text` |
| `text_delta` | one token fragment of the reply, as it is generated | `text` |
| `text` | the assistant's complete message for the turn | `text` |
| `tool_call` | agent is calling a tool | `tool`, `args` |
| `tool_result` | what the tool returned | `tool`, `text` |
| `verify_passed` / `verify_failed` | verdict for the turn | `check` (`gate:…`/`tests`), `text` = verbatim failure |
| `escalated` | switched to a stronger tier | `text` |
| `confirm_request` | a shell command needs approval | `tool`, `detail` |
| `escalation_offer` | the agent is stuck and wants a stronger (paid) tier | `text`, `name` |
| `ask` | a yes/no decision for the user (plan approval, escalation) | `question`, `detail` |
| `nudge` | the agent was caught repeating itself and told to change approach | `text`, `tool` |
| `confirm_auto` | approved automatically (`--yes`) | `tool`, `detail` |
| `ack` | answer to a slash command | `command`, plus e.g. `usage`, `reverted` |
| `turn_end` | one chat turn finished | `usage` |
| `stopped` | cap hit / provider error / interrupt | `text` |
| `session_end` | run finished | `files_changed`, `usage`, `transcript` (+ `done_passed`, `rounds` headless) |
| `error` | startup refusal (bad provider, dirty tree) | `text` |

Every event carries `ts` (UTC ISO-8601) and `type`. The schema is **additive** — new
fields may appear, existing ones won't be renamed.

**Streaming.** The reply arrives as many `text_delta` events and then once more, complete,
as a single `text` event. A UI should append the deltas to a live bubble and then treat
`text` as the canonical version rather than printing it again. Disable with
`agent_chat.stream: false` (or per provider, `providers.<name>.stream: false`) and only
`text` is emitted. Supported for `ollama-local` and `openai-compatible` providers;
`vertex-ai` replies arrive whole.

**Approvals over the wire.** In `--json` mode there's no prompt to show, so when a
non-allowlisted command comes up the agent emits `confirm_request` and reads **one line
from stdin**: `{"allow": true}` (or plain `y`). EOF or anything else = refused, so an
unattended frontend fails safe. Pass `--yes` to skip approvals entirely (you get
`confirm_auto` events instead).

**Input in chat mode** is still plain text lines (one message per line), so you can pipe
into it; only the output changes.

## Honest limits

- **The model is the ceiling.** A local 7B–80B is genuinely weaker than a frontier model at
  long, multi-step work — it loses the thread and needs more turns. Verification means its
  mistakes get caught, not that it stops making them. Well-scoped tasks work well;
  "refactor my architecture" does not.
- **Tool-calling quality varies by model.** Models without native tool support can still be
  used via `providers.<name>.tool_protocol: "text"`, but it's slower and less reliable —
  and it can't stream, because the whole reply is needed before an action can be parsed.
- **`tests` is only as good as your tests.** The agent proves your suite passes — it can't
  know what your suite forgot to check.
