# How the multi-agent system works (beginner-friendly)

This doc explains the mental model behind the pipeline: **one boss brain directing two cheap
worker brains**, and why it's built this way.

## What is an "agent", really?

A plain LLM (the chat box) only talks — you paste code in, it prints code back, you copy it out by
hand. An **agent** is that same model wrapped in a *loop* that gives it **hands and eyes**: it can
read files, run commands, edit code, look at the result (did the test pass? did it compile?), and
decide the next action — repeating until a goal is met.

> **agent = a brain (the model) + a runtime (the loop + tools around it)**

The key idea that makes this whole project possible: **the brain and the runtime are separate
choices.** You can put a cheap/local brain inside a capable runtime.

## The cast

| Piece | What it is | Brain | Its job here |
|-------|-----------|-------|--------------|
| **Claude Code** | A coding-agent runtime | 🔒 Claude only | **The boss.** Splits work, writes the acceptance test, routes tasks, reviews, commits. Spends *few* tokens. |
| **Aider** | A model-agnostic coding-agent runtime | 🔓 any model (via *litellm*) | **The workers.** One instance drives local **qwen-coder** (free), one drives **Gemini** (cloud). They do the typing. |
| **qwen-coder** (Ollama) | Local 80B MoE model | — | Worker brain #1: routine, self-contained code. Free, runs on your GPU. |
| **Gemini** (Vertex AI) | Google's cloud model, two tiers | — | Worker brain #2: `flash` for routine cloud work, `pro` for genuinely hard tasks. |

Two other tools you may have heard of, and why they're **not** used as workers here:
- **Codex** — OpenAI's coding-agent CLI. Same idea as Claude Code but 🔒 locked to GPT. You *could*
  add GPT as an Aider worker, but frontier GPT/Claude cost more than Gemini Flash + free local Qwen,
  so they only make sense as a rare last resort — not a default worker.
- **OpenClaw** — a *personal assistant* that lives across messaging apps (WhatsApp/Telegram/…), with
  voice and a canvas. It's a **different category** — not a repo-editing coder. It could later be a
  phone/chat *front-end* to kick off tasks or get "done" pings, but it is never a code worker.

## Why not just point a Claude Code sub-agent at a local/Gemini model?

You can't — Claude Code sub-agents can only run Claude models. There is no setting to point one at a
local model or at Gemini. So the cheap worker brains live **behind a local MCP server** (this
project) that Claude Code calls as tools. That's the entire reason this pipeline exists.

## The division of labor

```
┌─ CLAUDE (boss) ──────────────────────────────────────────────┐
│  1. split the work                                           │  ← where the few tokens go
│  2. write a STRICT acceptance test (the objective "done")    │
│  3. route each task to the cheapest capable worker           │
│        routine        → qwen-coder      (free, local)        │
│        routine cloud  → gemini flash    (cheap)              │
│        hard           → gemini pro      (stronger)           │
│        security/arch/ambiguous → Claude keeps it (never delegated)
│  4. glance at the result + commit                            │
└──────────────────────────────────────────────────────────────┘
              │ MCP tool call (delegate / assign)
              ▼
┌─ WORKER (Aider + qwen or gemini) ────────────────────────────┐
│  reads the repo itself in a THROWAWAY git worktree,          │
│  edits code, runs the `done_when` check, and on failure      │
│  feeds the error back to ITSELF and retries — no Claude.     │
│  Delivers a clean diff; the pipeline applies it mechanically.│
└──────────────────────────────────────────────────────────────┘
```

**Golden rules**
1. **One boss brain; cheap worker brains for the typing.** The cost win only exists because the
   expensive brain (Claude) is spent on judgment, not on generating every line.
2. **Never let two code-editing agents touch the same files at once** — they'd overwrite each other.
   That's why each Aider worker runs in its own disposable git worktree; the real tree is untouched
   until the pipeline applies a verified diff.
3. **Machines check first, Claude judges last.** Anything a machine can decide (compile / lint /
   tests) is checked in-pipeline, and failures bounce back to the *worker*, costing zero Claude
   tokens. Claude only steps in for what machines can't decide.
4. **Workers implement; they never "review" each other.** A weak model grading a weak model adds
   cost, not safety. Claude is the only judge.

## The two tools Claude uses to delegate

- **`delegate(task, role, provider?, model?, context?)`** — a *stateless* snippet: Claude pastes
  context, the worker returns code, the mechanical gate verifies it (and auto-retries the worker on
  failure) before Claude ever sees it.
- **`assign(task, done_when, repo, provider?, model?, files?)`** — a *file-aware* whole task: the
  worker (Aider) reads the repo itself and grinds until `done_when` (a shell command Claude wrote)
  exits 0, then the pipeline applies the diff. Claude just commits.

See [CONFIGURATION.md](CONFIGURATION.md) for how to enable Gemini and pick tiers.
