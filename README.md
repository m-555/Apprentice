# Apprentice

**A local, multi-provider code-delegation pipeline.** A master orchestrator (e.g. Claude Code)
delegates routine coding to *apprentice* models — a **local Qwen 80B** on your own GPU (via
Ollama) and **Gemini** (Vertex AI) — then mechanically verifies, corrects, and *learns* from the
results over time via in-context retrieval. The expensive brain is spent on judgment; the cheap
brains do the typing.

The apprentices sit behind one small **MCP server** exposing three tools — `delegate`, `assign`,
`log_correction` — so any MCP-capable orchestrator can drive them.

New here? Start with **[docs/MULTI_AGENT.md](docs/MULTI_AGENT.md)** — it explains, in beginner
terms, what an "agent" is and how the boss + two-worker model fits together.

> **Note:** the project was formerly `qwen-pipeline`. Its default working directory and the MCP
> server id are still `qwen-pipeline` / `qwen`; only the project brand is **Apprentice**.

---

## Why this exists

Many orchestrators (Claude Code among them) can only run their own model family — there's no way
to point a sub-agent at a local model or at Gemini. So the worker models live **behind a local
MCP server** instead. The orchestrator calls a tool, the server runs the chosen model, the
orchestrator reviews the output.

```
        ┌──────────────────────────────────────────────┐
        │  ORCHESTRATOR  — the "boss" / decision-maker   │
        │  splits tasks, picks provider, REVIEWS output, │
        │  fixes mistakes, logs corrections, commits     │
        └───────────────┬────────────────────────────────┘
                        │ MCP tool call (stdio)
                        ▼
        ┌──────────────────────────────────────────────┐
        │  MCP server  (src/server.py, FastMCP)          │
        │   delegate(task, role, provider?, model?, …)   │
        │   assign(task, done_when, repo, …)             │
        │   log_correction(…)                            │
        └───┬───────────────┬───────────────┬────────────┘
            ▼               ▼               ▼
         qwen            gemini          openai
      (Ollama,        (Vertex AI:      (future,
       local GPU)      flash / pro)     no key)
```

The "specialized agents" (test writer, C++ implementer, …) are **not** separate models — they are
`role` values that select a different system prompt for the same worker.

---

## Requirements

- **Python 3.11+**
- **[Ollama](https://ollama.com)** running locally, with a worker model + an embedder pulled.
- *(optional, for the `assign` file-aware agent)* **[Aider](https://aider.chat)** in its own venv.
- *(optional, for the Gemini worker)* `google-genai` + Google Cloud **Vertex AI** credentials.
- An **MCP-capable orchestrator** (e.g. Claude Code) to drive the tools.

The reference machine is an RTX 5090 (32 GB VRAM) + 64 GB RAM, but the pipeline runs anywhere
Ollama can serve a model — scale the worker model to your hardware.

## Getting started

```bash
git clone https://github.com/m-555/Apprentice.git qwen-pipeline
cd qwen-pipeline

# 1. core deps (pinned)
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # Windows
# .venv/bin/pip install -r requirements.txt             # Linux/macOS

# 2. pull the worker + embedder models (sizes approximate)
ollama pull qwen3-coder-next        # ~51 GB, Q4_K_M — scale down for smaller GPUs
ollama pull nomic-embed-text        # ~274 MB — for retrieval

# 3. (optional) the file-aware `assign` agent, in an ISOLATED venv
python -m venv .aider-venv
.aider-venv/Scripts/pip install -r requirements-aider.txt

# 4. register the MCP server with your orchestrator (Claude Code example)
claude mcp add --scope local qwen -- ".venv/Scripts/python.exe" "src/server.py"
claude mcp list      # -> qwen ... ✓ Connected
```

Configuration lives in `config/` — see **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**. To add
the Gemini worker, see [Enabling Gemini](#enabling-gemini-vertex-ai) below.

---

## The worker model & expert-offload

| | Reference setup |
|---|---|
| Worker model | `qwen3-coder-next:latest` — 79.7B MoE (~3B active), **Q4_K_M**, 262k ctx, tools-capable |
| Embedder | `nomic-embed-text` (768-dim) — for retrieval |
| Runner | Ollama (HTTP API on `127.0.0.1:11434`) |

**Expert-offload:** the ~48 GB model does not fit in 32 GB VRAM. Ollama keeps the
attention/shared layers on the GPU and streams the MoE experts from system RAM. GPU utilisation
looks low because it is memory-bandwidth-bound — normal for an MoE with few active params. Warm
throughput ≈ 50–58 tok/s; cold load ≈ 55 s.

**Warm model:** Ollama keeps the model resident for `keep_alive` after the last request (default
**30m** here). Requests within that window skip the load (~0.1 s). It's deliberately not infinite
— a warm model holds the whole GPU, so a moderate timeout frees it for other work when idle.

> **Model storage gotcha (Ollama desktop app):** the desktop app stores its model location in
> `db.sqlite` and, when it spawns the server, sets `OLLAMA_MODELS` to that value — overriding your
> env var. If a large `ollama pull` fills the wrong disk, point both the env var **and** the app's
> DB at your intended path, then confirm with `ollama list`.

---

## The MCP tools

### `delegate(task, role, provider="", context="", model="")  ->  str`
Sends `{system: ROLES[role], user: task (+context)}` to the chosen provider and returns the
generated text, plus a status footer with the gate verdict and an `output_id`. The pipeline
mechanically verifies the output and auto-retries the worker on failure *before* returning.

- **roles:** `ts_implementer`, `cpp_implementer`, `py_implementer`, `test_writer`, `refactorer`
- **providers:** `qwen` (local, default), `gemini` (Vertex AI), `openai` (future)
- **model:** optional per-call override — for `gemini`, `"flash"` (routine) or `"pro"` (hard).

### `assign(task, done_when, repo, provider="", files="", max_iters=0, apply=True, model="")  ->  dict`
A **file-aware worker agent** (Aider) that reads `repo` itself and grinds a whole task to an
**objective "done"** with no orchestrator in the loop. The boss's role = **define task + define
done + commit**.

- Runs Aider (isolated venv, pinned) in a **disposable git worktree** off `repo`'s HEAD — the real
  tree is untouched. Loops: worker edits → run `done_when` (a shell cmd that must exit 0) → on
  failure feed the verbatim output back to the worker (up to `max_iters`).
- On green: extracts a **clean diff** (build/worker junk filtered) and, if `apply`, **mechanically
  applies it** to the real tree. You then just commit.
- Returns a cheap summary: `{done_passed, applied, iterations, files_changed, patch_path,
  done_log_tail, worker_log_tail, output_id}` — the full diff is in `patch_path`.

### `log_correction(role, task, error_category, explanation, output_id="", correction_patch="", …)  ->  {"ok": true}`
Appends one record to `corrections/corrections.jsonl` (and indexes it for retrieval). Call it
**after every delegation**, even when the worker was correct (`error_category="none"`, empty patch).

- **`error_category`:** `logic | compile | style | edge_case | security | api_misuse | none`.
- Prefer the **diff-only** form: pass `output_id` (from the `delegate` footer) + a unified-diff
  `correction_patch` instead of re-sending the code — the pipeline reconstructs both sides.

---

## The delegate → review → fix → log loop

1. **Split** the task; delegate only the well-specified, self-contained part.
2. **`delegate(...)`** (snippet) or **`assign(...)`** (whole file-aware task) with the right
   role/provider — see `config/routing.md`.
3. **Review** for: correctness, compiles/runs, project conventions, edge cases, security, and any
   language-specific concerns (e.g. version-guarding for C++).
4. **Fix** if needed (else corrected == worker output).
5. **`log_correction(...)`** — always.

The mechanical gate + worker→worker auto-retry handle most fixes with **zero orchestrator tokens**;
the boss only steps in for judgment. Full routing rules: `config/routing.md`.

---

## In-context retrieval — learning without training

The pipeline gets better over time **via retrieval, not weight training** (an 80B can't be
fine-tuned on one 32 GB GPU). The mechanism (`src/retrieval.py`):

- **On `log_correction`:** the task is embedded with `nomic-embed-text` and a compact entry
  (vector + role/provider/category + few-shot fields) is appended to `corrections/index.jsonl`.
- **On `delegate`:** the incoming task is embedded, the **top-k** most similar past corrections
  **for the same provider+role** are selected (favoring real mistakes per `mistake_vs_correct_mix`)
  and injected into the system prompt as few-shot examples.
- **Fail-safe:** if the embedder is unreachable, delegation still runs (just without examples) and
  corrections are still saved — re-embed later with `python src/retrieval.py reindex`.

Tunables in `config/qwen.json → retrieval`: `enabled`, `top_k`, `role_filter`,
`prefer_error_categories`, `mistake_vs_correct_mix`.

---

## Enabling Gemini (Vertex AI)

Secrets and machine-local values go in `config/qwen.local.json` (gitignored), which is
**deep-merged over** `config/qwen.json` at load time — so the committed config never holds a secret.

1. `.venv/Scripts/pip install -r requirements-gemini.txt`
2. `cp config/qwen.local.example.json config/qwen.local.json`, then fill in your **GCP project**,
   the **service-account JSON path** (`credentials_file`), the **model ids** for `flash`/`pro`, and
   set `enabled: true`.
3. Delegate to a tier: `delegate(..., provider="gemini", model="pro")` or
   `assign(..., provider="gemini", model="flash")`.

> ⚠️ The `assign` (Aider) model ids **must** use litellm's **`vertex_ai/`** prefix for a service
> account (e.g. `vertex_ai/gemini-2.5-pro`), NOT `gemini/` (the AI-Studio API-key path). Full
> walkthrough + the two-model-id-forms gotcha: **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

---

## Use on another project (it's project-agnostic)

Nothing in the gate/agent layer is tied to a particular codebase. To use it on another repo:

1. Point `assign(repo="/path/to/your-repo", done_when="<your test/lint cmd>", …)` at it.
2. *(Optional)* drop **`<your-repo>/.qwen-pipeline.json`** to override per-project settings — its
   `agent` block merges over `config/qwen.json → agent` (repo wins). Example:
   ```json
   { "agent": { "max_iters": 4, "diff_excludes": [".aider*", "node_modules", "dist", "*.pyc"] } }
   ```
3. Gate languages (`gate.languages.*`) and any batched build step are config-driven — enable/point
   them per project. The MCP surface (`delegate`, `assign`, `log_correction`) is unchanged.

---

## Repository layout

```
qwen-pipeline/
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE                       # MIT
├── requirements.txt              # core, PINNED (mcp, numpy)
├── requirements-gemini.txt       # optional: Gemini/Vertex provider
├── requirements-aider.txt        # optional: the `assign` agent (install in .aider-venv)
├── config/
│   ├── qwen.json                 # canonical config (committed, NO secrets)
│   ├── qwen.local.example.json   # template for the gitignored local overlay
│   ├── qwen.local.json           # GITIGNORED: project id, creds path, model ids, enabled flags
│   └── routing.md                # what to delegate, to which provider/role/tier
├── docs/
│   ├── CONFIGURATION.md          # config reference + enabling Gemini
│   └── MULTI_AGENT.md            # how the boss + two-worker model works (beginner-friendly)
├── src/
│   ├── server.py                 # FastMCP stdio server: delegate / assign / log_correction
│   ├── providers.py              # provider handlers: qwen, gemini, openai
│   ├── agent.py                  # the `assign` file-aware agent (Aider + disposable worktree)
│   ├── gate.py / gate_cli.py     # mechanical gate (compile/lint) + worker-retry
│   ├── store.py                  # output-id store + unified-diff apply
│   ├── retrieval.py              # embed + cosine retrieval of past corrections
│   ├── metering.py               # per-delegation cost/outcome log
│   ├── host_verify.py            # optional batched build/test runner (project-specific)
│   └── roles.py                  # role -> system-prompt map
├── tests/test_pipeline.py        # deterministic, offline (stubs providers/embeddings)
└── corrections/                  # GITIGNORED contents: corrections + retrieval index (local only)
```

Gitignored (never pushed): `config/qwen.local.json`, `secrets/`, `corrections/*.jsonl`, `outputs/`,
`metrics/`, `models/`, `.venv/`, `.aider-venv/`, `node_modules/`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `delegate` errors "Could not reach Ollama" | Server down. Run `ollama serve` (or start the desktop app). Check `ollama list`. |
| MCP server not connected | Run the launch command directly to see the error: `.venv/Scripts/python.exe src/server.py` |
| New tools not visible in a running session | They load in new sessions automatically; in a running one, reconnect (e.g. `/mcp`). |
| A pull fills the wrong disk | Ollama isn't using your intended path — see the model-storage gotcha above. |
| VRAM near OOM with big context | Cap `num_ctx` (KV cache grows with context). Prefer this over downgrading the quant. |
| Gemini "not enabled yet" | Expected until Vertex creds are configured — see [Enabling Gemini](#enabling-gemini-vertex-ai). |
| Retrieval not injecting examples | Index empty/stale or embedder down. Rebuild: `python src/retrieval.py reindex`. |

---

## Conventions & safety

- **Pin dependencies.** Never float the MCP SDK (a 2026 stdio command-injection advisory makes
  pinning the documented mitigation). The stdio server runs with full user privileges — keep scope
  tight.
- **Keep the tool surface small** (three tools) — schemas load into the orchestrator's context
  every turn.
- **`corrections/` may contain private code** — it stays local (gitignored) and is not committed.
- **Token generation on the worker; judgment on the boss.** That's the only place the cost win
  comes from — if a task type keeps coming back wrong, stop delegating it (`config/routing.md`).

---

## Documentation

- **[docs/MULTI_AGENT.md](docs/MULTI_AGENT.md)** — how the boss + two-worker model works, in
  beginner terms (what an agent is; Claude Code vs. Aider vs. Codex vs. OpenClaw; who does what).
- **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** — full config reference, the committed vs.
  local overlay, and enabling the Gemini/Vertex worker.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — dev setup, tests, and ground rules.
- **[CHANGELOG.md](CHANGELOG.md)** — notable changes.

## License

[MIT](LICENSE) © 2026 Mohsen Mirzaei.
