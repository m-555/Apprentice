# Configuration reference

All runtime behavior is driven by JSON config — no code edits needed to tune it. The server
**re-reads config on every tool call**, so changes take effect live (a *code* change still needs
a fresh session, since Claude Code respawns the stdio subprocess).

## Two config files: committed vs. local

| File | Committed? | Holds |
|------|-----------|-------|
| `config/qwen.json` | ✅ yes | All non-secret defaults: model tags, gate/retrieval/cascade/metering knobs, provider *shapes* with empty secret fields. |
| `config/qwen.local.json` | ❌ **gitignored** | Your secrets and machine-local values: GCP project id, credential file path, exact model ids, `enabled` flags, budgets. |

At load time the server **deep-merges `qwen.local.json` over `qwen.json`** (local wins on scalars;
dicts merge). This keeps every secret out of the public repo. Start from the template:

```bash
cp config/qwen.local.example.json config/qwen.local.json
# then edit config/qwen.local.json with your real values
```

## Providers

The worker "brains" the orchestrator delegates to (the orchestrator itself is the boss/judge,
not a provider). Three built-ins:

| Provider | Runtime | Notes |
|----------|---------|-------|
| `qwen`   | local Ollama | Default. Free, GPU-local. |
| `gemini` | Vertex AI | Two tiers, `flash` (routine) / `pro` (hard), picked per call via the `model` arg. |
| `openai` | OpenAI API | GPT/Codex — set `model`, export `OPENAI_API_KEY`, set `enabled: true`. |

### Adding your own provider (no code)

Any entry under `providers.<name>` with a known `kind` becomes a valid `provider` value for
`delegate`. Kinds:

- **`openai-compatible`** — any OpenAI-style `/chat/completions` endpoint: OpenAI, **Groq,
  OpenRouter, Mistral, LM Studio, vLLM, llama.cpp server**… Configure `base_url`, `model`
  (or a `models` tier map + `default_model`), and `api_key_env` (the **name of the env var**
  holding the key — keys never go in config files; local endpoints can omit the key entirely).
- **`ollama-local`** — another local Ollama model (`host`, `model`, `keep_alive`, `options`).
- **`vertex-ai`** — a second Vertex entry, same shape as `gemini`.

Example (`config/qwen.local.json`):

```json
{
  "providers": {
    "groq": {
      "enabled": true,
      "kind": "openai-compatible",
      "base_url": "https://api.groq.com/openai/v1",
      "api_key_env": "GROQ_API_KEY",
      "model": "llama-3.3-70b-versatile",
      "options": { "temperature": 0.2 }
    },
    "lmstudio": {
      "enabled": true,
      "kind": "openai-compatible",
      "base_url": "http://127.0.0.1:1234/v1",
      "model": "qwen2.5-coder-14b-instruct"
    }
  }
}
```

Then just `delegate(..., provider="groq")`. Every provider goes through the **same** mechanical
gate, retries, retrieval, metering, and budgets.

### Sampling options (code quality lever)

`providers.<name>.options` is forwarded to the provider per request (`temperature`, `top_p`,
plus `max_tokens` for openai-compatible / `max_output_tokens` for Vertex). The defaults are
LOW temperature (0.15–0.2) on purpose: implement-to-spec codegen wants deterministic output —
fewer gate failures and retry churn. Raise it only for exploratory/creative tasks.

### Cost tracking & budgets (cloud spend)

Local models are free; cloud tokens are not. Two knobs:

- **Prices** — `providers.<name>.cost`, either flat (`{"usd_per_mtok_in": …, "usd_per_mtok_out": …}`)
  or keyed by model tier (`{"flash": {…}, "pro": {…}}`). When set, every metering event gets an
  `est_cost_usd` and `python src/metering.py` reports per-tier and total estimated spend.
- **Budgets** — `metering.budgets.<name>_tokens_per_day` and/or `<name>_usd_per_day`
  (since UTC midnight). These are **enforced**: `delegate`/`assign` refuse an over-budget
  provider with a clear error (route to the local model instead), and the cascade skips
  escalating to an over-budget tier.

### Enabling the Gemini (Vertex AI) worker

1. **Install the SDK** (delegate path): `pip install -r requirements-gemini.txt`
2. **Credentials.** Put your Vertex **service-account JSON** somewhere outside the repo (or in the
   gitignored `secrets/`). You need it referenced in **one** place — `providers.gemini` — and the
   server propagates it to both tools.
3. **Fill `config/qwen.local.json`:**
   ```json
   {
     "providers": {
       "gemini": {
         "enabled": true,
         "project": "your-gcp-project-id",
         "location": "us-central1",
         "credentials_file": "/path/to/service-account.json",
         "default_model": "flash",
         "models": { "flash": "gemini-2.5-flash", "pro": "gemini-2.5-pro" }
       }
     },
     "agent": {
       "models": {
         "gemini": {
           "default_model": "flash",
           "models": {
             "flash": "vertex_ai/gemini-2.5-flash",
             "pro":   "vertex_ai/gemini-2.5-pro"
           }
         }
       }
     }
   }
   ```

> ⚠️ **Region (`location`) matters for newer models.** Preview / newest-generation models
> (e.g. **Gemini 3.x**) are served **only on the `global` endpoint** — set
> `providers.gemini.location: "global"`. Older GA models (2.5) also work in regional endpoints
> like `us-central1`. A `404 NOT_FOUND` ("model not found … in the specified region") means the
> model isn't served in that `location` — switch to `"global"`. (Verified: `gemini-3.5-flash` and
> `gemini-3.1-pro-preview` return 404 in `us-central1` but work in `global`.)

> ⚠️ **The two model-id forms differ — this is the #1 gotcha.**
> - `providers.gemini.models.*` are **bare** Vertex ids (used by the `delegate` tool via the
>   `google-genai` SDK), e.g. `gemini-2.5-pro`.
> - `agent.models.gemini.models.*` are **litellm** ids (used by the `assign` tool via Aider) and
>   **must** use the **`vertex_ai/`** prefix for a service account, e.g. `vertex_ai/gemini-2.5-pro`.
>   The **`gemini/`** prefix is litellm's *AI-Studio API-key* path and will silently ignore your
>   service account.

The server derives `GOOGLE_APPLICATION_CREDENTIALS`, `VERTEXAI_PROJECT`, and `VERTEXAI_LOCATION`
for the Aider subprocess from `providers.gemini`, so credentials are configured **once**.

### Picking the tier per task

- `delegate(task, role, provider="gemini", model="pro")`
- `assign(task, done_when, repo, provider="gemini", model="flash")`

Empty `model` → the provider's `default_model` (`flash`). See [MULTI_AGENT.md](MULTI_AGENT.md) for
when to reach for each brain/tier.

### Writing `done_when` / `test_cmd` commands (Windows note)

Both `assign`'s `done_when` and `delegate`'s `test_cmd` are executed via a **generated `.cmd`
script**, not `cmd /c <string>`, so quoted multi-word arguments (quoted exe paths,
`findstr /C:"export function foo"`) survive intact — no special quoting rules to remember.
The command runs with the worktree (`done_when`) or repo (`test_cmd`) as its working directory
and must **exit 0** to count as passing.

## Other config blocks (in `config/qwen.json`)

| Block | Purpose |
|-------|---------|
| `runner` / `worker_model` / `keep_alive` / `offload` | Ollama host, the local model tag, warm-keep window, expert-offload notes. |
| `retrieval` | In-context few-shot retrieval of past corrections (`top_k`, `role_filter`, mix). |
| `gate` | Per-language mechanical gate (Python `py_compile`, TS `tsc`, C++ heuristic lint) + `max_retries`. |
| `delegate` | Token-cheap delegate options: `context_max_file_kb`/`context_max_total_kb` (server-side `context_files` caps), `test_timeout_s` (the `apply_to`+`test_cmd` acceptance run), `return_mode` default. |
| `cascade` | Cost-ordered auto-escalation on persistent gate failure (`escalate_to`, `skip_cascade_categories`). |
| `metering` | Per-delegation cost/outcome log, `est_cost_usd` pricing, and **enforced** daily `budgets` (tokens and/or USD per provider). |
| `agent` | The `assign` file-aware agent: Aider exe path, worktree root, iteration/timeout caps, model map. |
| `host_harness` | Batched Tier-2 C++/UE build+test runner (project-specific; used by `host_verify.py`). |

## Per-repo config (`<repo>/.qwen-pipeline.json`)

The pipeline is project-agnostic; drop a `.qwen-pipeline.json` in any target repo:

- **`conventions`** *(string)* — injected into the worker's system prompt for every
  `delegate(repo=…)` call on that repo. Put your project's style rules here (naming, typing,
  frameworks) so style corrections stop happening after the fact. It's stable per repo, so it
  stays prompt-cache-friendly.
- **`agent`** *(object)* — merges over `config/qwen.json → agent` (repo wins) for `assign`.

```json
{
  "conventions": "TypeScript strict; no `any`. Zod for validation. snake_case file names.",
  "agent": { "max_iters": 4, "diff_excludes": [".aider*", "node_modules", "dist", "*.pyc"] }
}
```
