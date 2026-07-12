# Qwen Pipeline — local multi-provider code-delegation worker

A local worker pipeline that lets **Claude Code** (orchestrator + reviewer) offload
routine code generation to cheaper models — primarily a **local Qwen 80B** running on a
single RTX 5090 — then review, correct, and learn from the results.

This folder is a **sibling of the product repo** (`E:\projects\UE_MCP`), deliberately kept
**outside** it so the pipeline code and the (possibly proprietary) corrections store stay
out of that repo's git history.

> Plan of record: `E:\projects\UE_MCP\Design\IMPLEMENTATION_PLAN_agent.md`.
> Per-session dev workflow: `E:\projects\UE_MCP\CLAUDE.local.md` (gitignored).

---

## Why this exists

Claude Code subagents can only run Claude models — there is no way to point a subagent at
a local model or at Gemini/Codex. So the worker models live **behind a local MCP server**
instead. Claude calls a tool, the server runs the chosen model, Claude reviews the output.

```
        ┌──────────────────────────────────────────────┐
        │  CLAUDE CODE  — orchestrator / decision-maker  │
        │  splits tasks, picks provider, REVIEWS output, │
        │  fixes mistakes, logs corrections              │
        └───────────────┬────────────────────────────────┘
                        │ MCP tool call (stdio)
                        ▼
        ┌──────────────────────────────────────────────┐
        │  qwen MCP server  (src/server.py, FastMCP)     │
        │   delegate(task, role, provider?, context?)    │
        │   log_correction(...)                          │
        └───┬───────────────┬───────────────┬────────────┘
            ▼               ▼               ▼
         qwen            gemini          openai
      (Ollama,        (Vertex AI,      (future,
       local GPU)      when creds)      no key)
```

The "specialized agents" (test writer, C++ implementer, …) are **not** separate models —
they are `role` values that select a different system prompt for the same worker.

---

## Hardware & runtime

| | |
|---|---|
| GPU | NVIDIA RTX 5090, 32 GB VRAM |
| RAM | 64 GB |
| Worker model | `qwen3-coder-next:latest` — 79.7B MoE (~3B active), **Q4_K_M**, 262k ctx, tools-capable |
| Embedder | `nomic-embed-text` (768-dim) — for Phase-5 retrieval |
| Runner | Ollama 0.30.x (HTTP API on `127.0.0.1:11434`) |

**Expert-offload:** the 48 GB model does not fit in 32 GB VRAM. Ollama keeps the
attention/shared layers on the GPU (VRAM sits ~31–32 GB) and streams the MoE experts from
system RAM. GPU utilisation looks low because it is memory-bandwidth-bound, which is normal
for an MoE with few active params. Warm throughput ≈ 50–58 tok/s; cold load ≈ 55 s.

**Warm model (like ComfyUI):** Ollama keeps the model resident for `keep_alive` after the
last request (we use **30m** per request). Requests within that window skip the load
(~0.1 s). It is **not** infinite on purpose — a warm model holds ~32 GB VRAM (the whole
5090), so a moderate timeout frees the GPU for ComfyUI/UE when idle. The GPU is taken in
turns, not shared.

---

## ⚠️ Everything large lives on E:  (C: is space-constrained)

| What | Location | Controlled by |
|------|----------|---------------|
| Ollama models | `E:\projects\qwen-pipeline\models\ollama` | `OLLAMA_MODELS` (User env) **+** the Ollama app's `db.sqlite` |
| HuggingFace / torch caches (other projects) | `E:\ai-cache\…` | `HF_HOME`, `TORCH_HOME` (User env) |
| Python venv | `E:\projects\qwen-pipeline\.venv` | — |

**GOTCHA — the Ollama desktop app overrides `OLLAMA_MODELS`.** The app stores its model
location in `C:\Users\<you>\AppData\Local\Ollama\db.sqlite` (table `settings`, column
`models`) and, when it spawns the server, sets `OLLAMA_MODELS` to that value — overriding
your env var. It defaults to `C:\Users\<you>\.ollama\models`. This repo's setup already
fixed that DB to the E: path. **Before any large `ollama pull`, verify the running server's
real path:** check the `OLLAMA_MODELS:` entry in the "server config" line of
`%LOCALAPPDATA%\Ollama\server.log` (or the stdout of a manual `ollama serve`), or just run
`ollama list` — if it shows the models, the server is reading from E:.

---

## Folder layout

```
E:\projects\qwen-pipeline\
├── README.md                  # this file
├── requirements.txt           # PINNED deps (mcp==1.28.1)
├── .venv\                      # Python 3.11 venv
├── config\
│   ├── qwen.json              # canonical config (committed, no secrets): tags, gate, cascade, providers
│   ├── qwen.local.example.json # template for the gitignored local overlay
│   ├── qwen.local.json        # GITIGNORED: your project id, creds path, model ids, enabled flags
│   └── routing.md             # what Claude delegates, to which provider/role/tier
├── docs\
│   ├── CONFIGURATION.md       # config reference + enabling Gemini
│   └── MULTI_AGENT.md         # how the boss + two-worker model works (beginner-friendly)
├── src\
│   ├── server.py              # FastMCP stdio server: delegate(), log_correction()
│   ├── providers.py           # provider handlers: qwen (live), gemini (gated), openai (future)
│   ├── roles.py               # role -> system-prompt map (5 starter roles)
│   └── retrieval.py           # Phase 5: embed + cosine retrieval of past corrections
├── corrections\
│   ├── corrections.jsonl      # one JSON record per delegation (may contain proprietary code)
│   └── index.jsonl            # derived: per-correction embedding vectors for retrieval
└── models\ollama\             # Ollama blob/manifest store (48 GB+)
```

---

## The MCP tools

### `delegate(task, role, provider="", context="")  ->  str`
Sends `{system: ROLES[role], user: task (+context)}` to the chosen provider and returns the
generated text. `provider` defaults to the config `providers.default` (`qwen`).

- **roles:** `ts_implementer`, `cpp_implementer`, `py_implementer`, `test_writer`, `refactorer`
- **providers:** `qwen` (live), `gemini` (gated on creds), `openai` (future)
- Unknown role/provider → clear error listing valid values. Ollama unreachable → clear error.

### `log_correction(role, task, qwen_output, corrected_output, error_category, explanation, provider="qwen", context="")  ->  {"ok": true}`
Appends one record to `corrections/corrections.jsonl`. Call it **after every delegation**,
even when the worker was correct (`error_category="none"`, `corrected_output == qwen_output`).

**Record fields:** `timestamp, provider, role, task, context, qwen_output, corrected_output,
error_category, explanation` (+ `output_id`, `correction_patch`, `machine_verified`, `corrected_by`).
**`error_category`:** `logic | compile | style | edge_case | security | api_misuse | none`.
Prefer the **diff-only** form (§6.2): pass `output_id` (from the `delegate` footer) + a unified-diff
`correction_patch` instead of re-sending the code.

### `assign(task, done_when, repo, provider="", files="", max_iters=0, apply=True)  ->  dict`  (Phase 7)
A **file-aware worker agent** (Aider) that reads `repo` itself and grinds a whole task to an
**objective "done"** with no Claude in the loop. Claude's role = **define task + define done + commit**.

- Runs Aider (isolated `.aider-venv`, pinned) in a **disposable git worktree** off `repo`'s HEAD —
  the real tree is untouched. Loops: worker edits → run `done_when` (a shell cmd that must exit 0) →
  on failure feed the verbatim output back to the worker (up to `max_iters`).
- On green: extracts a **clean diff** (Aider/`__pycache__`/`.pyc` junk filtered) and, if `apply`,
  **mechanically applies it** to the real tree (autocrlf-safe). You then just commit.
- Returns a Claude-cheap summary: `{done_passed, applied, iterations, files_changed, patch_path,
  done_log_tail, worker_log_tail, output_id}` — the full diff is in `patch_path`, not the return.

## Reuse in ANOTHER project (the pipeline is project-agnostic)

Nothing in the gate/agent layer is UE_MCP-specific. To use it on another repo:
1. Point `assign(repo="E:/projects/<your-repo>", done_when="<your test/lint cmd>", …)` at it.
2. (Optional) drop **`<your-repo>/.qwen-pipeline.json`** to override per-project settings — its
   `agent` block merges over `config/qwen.json → agent` (repo wins). Example:
   ```json
   { "agent": { "max_iters": 4, "models": { "qwen": { "model": "ollama_chat/…" } },
                "diff_excludes": [".aider*", "node_modules", "dist", "*.pyc"] } }
   ```
3. Gate languages (`gate.languages.*`) and the batched build (`host_harness`, C++/UE-specific) are
   likewise config-driven — enable/point them per project. The MCP surface (`delegate`,
   `log_correction`, `assign`) is unchanged everywhere.

---

## The delegate → review → fix → log loop

1. **Split** the task; delegate only the well-specified, self-contained part.
2. **`delegate(...)`** with the right role/provider (see `config/routing.md`).
3. **Review** for: correctness, compiles/runs, project conventions, edge cases,
   **security**, and **UE 5.0–5.8 version-guarding** for C++.
4. **Fix** if needed (else corrected == worker output).
5. **`log_correction(...)`** — always.

Full rules: `config/routing.md` and `E:\projects\UE_MCP\CLAUDE.local.md`.

---

## In-context retrieval (Phase 5) — learning without training

The pipeline gets better over time **via retrieval, not weight training** (an 80B can't be
fine-tuned on one 32 GB GPU). The mechanism (`src/retrieval.py`):

- **On `log_correction`:** the task is embedded with `nomic-embed-text` and a compact entry
  (vector + role/provider/category + the few-shot fields) is appended to
  `corrections/index.jsonl`.
- **On `delegate`:** the incoming task is embedded, the **top-k** most similar past
  corrections **for the same provider+role** are selected (favoring real mistakes per
  `mistake_vs_correct_mix`), and injected into the system prompt as few-shot "here are
  similar tasks and their correct solutions" examples.
- **Fail-safe:** if the embedder is unreachable, delegation still runs (just without
  examples) and corrections are still saved — re-embed later with
  `python src\retrieval.py reindex`.

Tunables in `config/qwen.json` → `retrieval`: `enabled`, `top_k`, `role_filter`,
`prefer_error_categories`, `mistake_vs_correct_mix`. Verified working: a repeat-style task
retrieved a prior correction (cosine 0.877) and the worker reproduced the taught hardening.

> Deferred/optional escape hatch (NOT the plan of record): cloud QLoRA on the accumulated
> corrections — see Appendix A of `IMPLEMENTATION_PLAN_agent.md`. Only if retrieval proves
> insufficient.

---

## Setup (already done on this machine — for reference / re-provisioning)

```powershell
# 1. venv + pinned deps
python -m venv E:\projects\qwen-pipeline\.venv
E:\projects\qwen-pipeline\.venv\Scripts\python.exe -m pip install -r E:\projects\qwen-pipeline\requirements.txt

# 2. models on E: (env var + Ollama app db.sqlite must both point to E:)
setx OLLAMA_MODELS E:\projects\qwen-pipeline\models\ollama

# 3. pull models
ollama pull qwen3-coder-next        # ~51 GB, Q4_K_M
ollama pull nomic-embed-text        # ~274 MB

# 4. register the MCP server with Claude Code (LOCAL scope — not committed to the product repo)
#    run from E:\projects\UE_MCP
claude mcp add --scope local qwen -- "E:\projects\qwen-pipeline\.venv\Scripts\python.exe" "E:\projects\qwen-pipeline\src\server.py"
claude mcp list      # -> qwen ... ✓ Connected
```

### Enabling Gemini (Vertex AI) — the second worker
Secrets and machine-local values go in `config/qwen.local.json` (gitignored), which is
**deep-merged over** `config/qwen.json` at load time — so the committed config never holds a secret.

1. `.venv\Scripts\python.exe -m pip install -r requirements-gemini.txt`
2. `cp config/qwen.local.example.json config/qwen.local.json`, then fill in your **GCP project**,
   the **service-account JSON path** (`credentials_file`), the **model ids** for `flash`/`pro`, and
   set `enabled: true`.
3. Delegate to a tier: `delegate(..., provider="gemini", model="pro")` or
   `assign(..., provider="gemini", model="flash")`.

> ⚠️ The `assign` (Aider) model ids **must** use litellm's **`vertex_ai/`** prefix for a service
> account (e.g. `vertex_ai/gemini-2.5-pro`), NOT `gemini/` (the AI-Studio API-key path).
> Full walkthrough + the two-model-id-forms gotcha: **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `delegate` errors "Could not reach Ollama" | Server down. Run `ollama serve`, or start the Ollama desktop app. Check `ollama list`. |
| `claude mcp list` shows qwen not connected | Run the launch command directly to see the error: `…\.venv\Scripts\python.exe …\src\server.py` |
| New `delegate`/`log_correction` tools not visible in a running session | They load in new sessions automatically; in a running one, reconnect via `/mcp`. |
| A pull is filling C: | The server isn't using E: — verify `OLLAMA_MODELS` in `server.log` and the `db.sqlite` `settings.models` value. |
| VRAM near OOM with big context | Cap `num_ctx`. **Do not** downgrade the quant (Q4_K_M is fixed by decision). |
| Gemini "not enabled yet" | Expected until Vertex creds are configured — see above. |
| Retrieval not injecting examples | Index may be empty/stale or embedder down. Rebuild: `…\.venv\Scripts\python.exe src\retrieval.py reindex`. Set `retrieval.enabled` in `config/qwen.json`. |

---

## Conventions & safety

- **Pin deps** (`requirements.txt`); never float the MCP SDK (2026 stdio advisory). The
  stdio server runs with full user privileges — keep its scope tight.
- **Keep the tool list at two** — schemas load into context every turn.
- **`corrections.jsonl` may contain proprietary code** — it stays local and is not
  committed or backed up without the user's say-so.
- **Token generation on the worker; judgment on Claude.** That is the only place the cost
  win comes from — if a task type keeps coming back wrong, stop delegating it
  (`config/routing.md`).

---

## Documentation

- **[docs/MULTI_AGENT.md](docs/MULTI_AGENT.md)** — how the boss + two-worker model works, in
  beginner terms (what an agent is; Claude Code vs. Aider vs. Codex vs. OpenClaw; who does what).
- **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** — full config reference, the committed vs.
  local overlay, and enabling the Gemini/Vertex worker.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — dev setup, tests, and ground rules.

## License

[MIT](LICENSE) © 2026 Mohsen Mirzaei.
