# Apprentice

**A local, multi-provider code-delegation pipeline.** A master orchestrator (e.g. Claude Code)
delegates routine coding to *apprentice* models — a **local model on your own GPU** (via Ollama),
**Gemini** (Vertex AI), **GPT/Codex** (OpenAI), or **any OpenAI-compatible endpoint** (Groq,
OpenRouter, LM Studio, vLLM, …) — then mechanically verifies, corrects, and *learns* from the
results over time via in-context retrieval. The expensive brain is spent on judgment; the cheap
brains do the typing.

The economics in one line: **local apprentices are free but weaker; cloud apprentices are
stronger but metered** — so the pipeline verifies everything mechanically (compile → lint →
your project's own tests), starts cheap, escalates only on failure, prices every cloud call,
and enforces daily budgets.

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
         qwen            gemini        openai + any
      (Ollama,        (Vertex AI:    openai-compatible
       local GPU,      flash / pro,   endpoint (GPT/Codex,
       FREE)           metered $)     Groq, LM Studio, …)
```

**Providers are config, not code.** Any entry in `config providers.<name>` with a known `kind`
(`ollama-local`, `openai-compatible`, `vertex-ai`) becomes a valid `provider=` value — adding
Groq or a second local model is a 6-line JSON block. See
[docs/CONFIGURATION.md](docs/CONFIGURATION.md#adding-your-own-provider-no-code). Every provider
runs through the same gate, retries, retrieval, metering, and budgets.

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

### Option A — install as a package (quickest)

```bash
pipx install git+https://github.com/m-555/Apprentice.git   # or: pip install apprentice-pipeline
apprentice init      # creates the data home (~/.apprentice or $APPRENTICE_HOME),
                     # seeds the config, checks Ollama, prints the MCP registration cmd
apprentice doctor    # environment check any time

# pull the worker + embedder models (scale the worker to your hardware)
ollama pull qwen3-coder-next
ollama pull nomic-embed-text

# register with your orchestrator (Claude Code example; `init` prints this too)
claude mcp add --scope local qwen -- apprentice serve
```

Config and data live in `~/.apprentice` (override with `APPRENTICE_HOME`). Edit
`~/.apprentice/config/qwen.local.json` for machine-local values and secrets. The Gemini
provider is an extra: `pipx install 'apprentice-pipeline[gemini] @ git+https://github.com/m-555/Apprentice.git'`.

### Option B — clone the repo (for hacking on the pipeline itself)

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

In a checkout, config and data live in the repo (`config/`, `corrections/`, `outputs/`,
`metrics/`) — see **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**. To add the Gemini worker,
see [Enabling Gemini](#enabling-gemini-vertex-ai) below.

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

### `delegate(task, role, provider="", context="", model="", repo="", context_files="", apply_to="", apply_mode="append", test_cmd="", return_mode="")  ->  str`
Sends `{system: ROLES[role] (+ repo conventions), user: task (+context)}` to the chosen provider
and returns the generated text, plus a status footer with the gate verdict and an `output_id`.
The pipeline mechanically verifies the output and auto-retries the worker on failure *before*
returning.

- **roles:** `ts_implementer`, `cpp_implementer`, `py_implementer`, `test_writer`, `refactorer`
- **providers:** `qwen` (local, default), `gemini` (Vertex AI), `openai` (GPT/Codex), or any
  config-defined provider
- **model:** optional per-call override — for `gemini`, `"flash"` (routine) or `"pro"` (hard).

**Token-cheap mode** — the orchestrator's expensive output tokens should never carry code:

- `repo=` + `context_files="src/a.ts src/b.ts:20-80"` — send **paths, not code**; the server
  reads the content locally (size-capped, path-traversal-guarded) and builds the context block.
- `apply_to="src/a.ts"` (+ `apply_mode`: `append`|`create`|`overwrite`) — the server writes the
  gate-passed code **into the real file** itself.
- `test_cmd="npx vitest run …"` — after applying, the server runs **your project's own
  acceptance command** in `repo`; on red it *reverts the file*, bounces the verbatim test output
  back to the worker, and retries — a full TDD loop with zero orchestrator tokens. The tree is
  never left broken. If the tier keeps failing, the task **escalates through the cascade**
  (e.g. local → gemini) carrying the failing code + test output, budget-guarded, before it ever
  reaches the orchestrator (footer shows `test_tier=` when that happened).
- `return_mode="summary"` — receive only the status footer + a one-line preview instead of the
  full code (it's already in the file and the output store).

A routine function then costs the orchestrator roughly: *task spec in, two-line footer out.*

```
delegate(task="Add mul(a,b)…", role="py_implementer",
         repo="/path/to/proj", context_files="mathx.py",
         apply_to="mathx.py", test_cmd="python test_mathx.py",
         return_mode="summary")
→ [summary] code_lines=3 first_line='def mul(a: float, b: float) -> float:'
  [qwen-pipeline] machine_verified=true check=py_compile attempts=1 tier=qwen
  output_id=… applied=true apply_to=mathx.py test=pass test_attempts=1
```

### `assign(task, done_when, repo, provider="", files="", max_iters=0, apply=True, model="")  ->  dict`
A **file-aware worker agent** (Aider) that reads `repo` itself and grinds a whole task to an
**objective "done"** with no orchestrator in the loop. The boss's role = **define task + define
done + commit**.

> **When to use which:** for a *known target file*, prefer `delegate` in token-cheap mode
> (`context_files` + `apply_to` + `test_cmd`) — it's simpler, faster, and needs no Aider install.
> Reach for `assign` when the task is genuinely **exploratory or multi-file** ("find where X is
> handled and fix it") — that's what the repo-map agent is for.

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

## Cost model — free-but-weaker vs. strong-but-metered

The routing philosophy the pipeline is built around:

| Tier | Strength | Cost | When |
|------|----------|------|------|
| local (`qwen`, or your Ollama model) | weakest | **free** | Start every routine task here. |
| cloud routine (e.g. `gemini` flash, GPT-mini) | medium | cheap | GPU busy, or local keeps failing a routine task. |
| cloud hard (e.g. `gemini` pro, GPT/Codex) | strong | pricier | Genuinely hard, well-specified tasks. |
| the orchestrator itself | judgment | most expensive | Security, architecture, ambiguity — never delegated. |

What keeps this honest:

- **The mechanical gate levels the field** — a weak model whose output compiles and passes
  *your* tests is worth the same as a strong one, and it cost nothing. Failures bounce back to
  the worker, not to the orchestrator.
- **Auto-escalation** (`cascade`) retries a persistently failing task one tier up, *carrying the
  failed attempt + checker error* so the stronger model doesn't start cold.
- **Pricing** — set `providers.<name>.cost` (USD per Mtok, flat or per tier) and every cloud
  call is priced into `metrics/metrics.jsonl`; `python src/metering.py` shows per-tier and total
  estimated spend.
- **Budgets are enforced** — `metering.budgets.<name>_tokens_per_day` / `<name>_usd_per_day`:
  over-budget providers are refused with a clear error and the cascade won't escalate to them.
  A runaway retry loop cannot drain your credits.

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

1. Point `delegate(repo="/path/to/your-repo", …)` / `assign(repo=…, done_when=…)` at it.
2. *(Optional)* drop **`<your-repo>/.qwen-pipeline.json`** for per-project settings:
   ```json
   {
     "conventions": "TypeScript strict; no `any`. Zod for validation. snake_case file names.",
     "agent": { "max_iters": 4, "diff_excludes": [".aider*", "node_modules", "dist", "*.pyc"] }
   }
   ```
   `conventions` is injected into the worker prompt on every `delegate(repo=…)` — your style
   rules are enforced up front instead of corrected after the fact. The `agent` block merges
   over `config/qwen.json → agent` (repo wins) for `assign`.
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
│   ├── providers.py              # provider registry: ollama-local / openai-compatible / vertex-ai
│   ├── deliver.py                # server-side context fetch + apply/test/revert (token-cheap mode)
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
| "Daily token/USD budget … exhausted" | Working as intended — the provider hit its `metering.budgets` cap. Use the local provider or raise the cap in `qwen.local.json`. |
| `delegate` `test=fail — REVERTED` | The worker never satisfied `test_cmd`; the target file was restored. Review the test output tail in the footer, tighten the task/spec, or take the task yourself. |
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
