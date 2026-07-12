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

Two worker "brains" Claude delegates to (Claude itself is the boss/judge, not a provider):

| Provider | Runtime | Notes |
|----------|---------|-------|
| `qwen`   | local Ollama | Default. Free, GPU-local. |
| `gemini` | Vertex AI | Two tiers, `flash` (routine) / `pro` (hard), picked per call via the `model` arg. |
| `openai` | — | Future slot, no key. |

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
         "credentials_file": "E:/path/to/service-account.json",
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

## Other config blocks (in `config/qwen.json`)

| Block | Purpose |
|-------|---------|
| `runner` / `worker_model` / `keep_alive` / `offload` | Ollama host, the local model tag, warm-keep window, expert-offload notes. |
| `retrieval` | In-context few-shot retrieval of past corrections (`top_k`, `role_filter`, mix). |
| `gate` | Per-language mechanical gate (Python `py_compile`, TS `tsc`, C++ heuristic lint) + `max_retries`. |
| `cascade` | Cost-ordered auto-escalation on persistent gate failure (`escalate_to`, `skip_cascade_categories`). |
| `metering` | Per-delegation cost/outcome log + optional per-provider daily token `budgets`. |
| `agent` | The `assign` file-aware agent: Aider exe path, worktree root, iteration/timeout caps, model map. |
| `host_harness` | Batched Tier-2 C++/UE build+test runner (project-specific; used by `host_verify.py`). |

## Per-repo overrides (for `assign` on other projects)

`assign` is project-agnostic. Drop a `<repo>/.qwen-pipeline.json` in any target repo; its `agent`
block merges over `config/qwen.json → agent` (repo wins). Example:

```json
{ "agent": { "max_iters": 4, "diff_excludes": [".aider*", "node_modules", "dist", "*.pyc"] } }
```
