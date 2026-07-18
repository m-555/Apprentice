# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — packaging: `pipx install` + `apprentice` CLI
- **Installable package** (`pyproject.toml`, hatchling): `pipx install
  git+https://github.com/m-555/Apprentice.git` (or `pip install apprentice-pipeline` once on
  PyPI). The repo's `src/` ships as the `apprentice` package; deps stay pinned; the Gemini
  provider is the `[gemini]` extra; Aider deliberately stays a separate venv, not an extra.
- **`apprentice` CLI**: `init` (idempotent setup wizard — creates the data home, seeds the
  default config bundled in the wheel, checks Ollama, prints the MCP registration command),
  `serve` (the stdio server — `claude mcp add … -- apprentice serve`), `doctor` (environment
  checks), `report`, `reindex`.
- **Data-home resolution** (`src/paths.py`): a repo checkout keeps everything in the repo as
  before; an installed package uses `$APPRENTICE_HOME` or `~/.apprentice`. All modules run
  both as the `apprentice` package and flat from `src/` (checkout scripts unchanged).

### Added — wave 2: general-purpose providers, cost control, token-cheap delegate
- **Config-driven provider registry.** Any `providers.<name>` entry with a known `kind` is a
  valid `provider=` value — no code changes: `ollama-local` (any local Ollama model),
  `openai-compatible` (OpenAI/GPT/Codex, Groq, OpenRouter, Mistral, LM Studio, vLLM,
  llama.cpp server — `base_url` + `model` + `api_key_env`; keys live in env vars, never in
  config), `vertex-ai`. The built-in `openai` provider is now a real, working
  OpenAI-compatible client (stdlib-only, no SDK).
- **Cost tracking + USD budgets.** `providers.<name>.cost` (USD per Mtok, flat or per
  model-tier) prices every call into `metrics.jsonl` (`est_cost_usd`); the metering report
  shows per-tier and total estimated spend; `metering.budgets.<name>_usd_per_day` is enforced
  alongside the token cap.
- **Token-cheap delegate** — new optional params that keep code out of the orchestrator's
  context entirely:
  - `repo` + `context_files="src/a.ts src/b.ts:20-80"` — server-side context fetch (send
    paths, not code; size-capped; path-traversal-guarded).
  - `apply_to` + `apply_mode` (`append`|`create`|`overwrite`) — the server writes gate-passed
    code into the real file.
  - `test_cmd` — the project's own acceptance command runs after apply; on red the file is
    **reverted** and the verbatim test output is bounced back to the worker (full TDD loop,
    zero orchestrator tokens; the tree is never left broken; recovered fixes are logged as
    machine-verified corrections). A **persistent test failure escalates through the cascade**
    (e.g. qwen → gemini) carrying the failing code + verbatim test output — same
    enabled/budget guards as gate escalation; the footer reports `test_tier=` when it happens.
  - `return_mode="summary"` — return only the status footer + a preview line.
- **Per-repo `conventions`** — a `conventions` string in `<repo>/.qwen-pipeline.json` is
  injected into the worker's system prompt for every `delegate(repo=…)` on that repo.

### Added
- **Sampling options per provider** (`providers.<name>.options`). qwen now defaults to
  `temperature 0.15 / top_p 0.9` (passed to Ollama per request); gemini forwards
  `temperature`/`top_p`/`max_output_tokens` to `GenerateContentConfig` (default temp 0.2).
  Implement-to-spec codegen wants low temperature — deterministic output means fewer gate
  failures and retry churn.
- **Daily token budgets are now enforced.** `metering.budgets.<provider>_tokens_per_day`
  (tokens_out since UTC midnight, from `metrics.jsonl`) was previously advisory dead config;
  now `delegate`/`assign` refuse an over-budget provider with a clear message, and the §6.4
  cascade skips escalating to an over-budget tier.

### Changed
- **Cascade escalation now carries the failure history.** When a tier exhausts its gate
  retries and the task escalates (e.g. qwen → gemini), the escalated tier receives the failed
  attempt + the verbatim checker error (same signal that powers worker→worker retry) instead
  of restarting from the raw task — better first-shot pass rate, fewer wasted tokens.
- **Retrieval few-shot moved from the system prompt to the user message.** The system prompt
  is now byte-stable per role, so provider-side prompt caching works (§6.3 — matters for
  Gemini billing; also speeds local prefill).
- **Metering now distinguishes reviews from step-ins.** The "log ALWAYS" discipline logs
  acceptances too; those were counted as "Claude had to step in", punishing the discipline the
  retrieval store depends on. `log_correction` now records `stepped_in` (true only when
  something actually changed or a real error category was flagged) and the report says
  "X of Y logged review(s)".

### Fixed
- **`assign`'s `done_when` no longer breaks on Windows quoting.** The acceptance check now
  runs via a generated `.qwen_done.cmd` script in the worktree instead of `cmd /c <string>`,
  so quoted multi-word args (`findstr /C:"export function foo"`, quoted exe paths) survive
  intact. The script is excluded from the extracted diff.
- **Gemini calls now report `duration_s`** in metering (was always 0).
- **Retrieval backfill deduped by dict equality** — two distinct corrections with identical
  content could wrongly dedupe (and the check was O(n²)); now compared by identity.

- **TypeScript gate falsely failed modern code.** The `tsc` gate ran without `--target`, so it
  defaulted to ES5 and rejected ES2015+ APIs (`Number.isFinite`, `Math.trunc`, `Object.entries`,
  optional chaining, …) with `TS2550`. Added `--target ES2020` to the default `tsc_cmd`. Set it to
  match your project's tsconfig `target`.

### Documentation
- Document that newer/preview Gemini models (e.g. Gemini 3.x) are served **only** on the Vertex
  `global` endpoint — a `404 NOT_FOUND` in a regional `location` means switch to `"global"`.
- Document the Windows `done_when` quoting gotcha (`cmd /c` + quoted exe paths).
- `qwen.local.example.json` now shows a Gemini 3.x + `global` configuration.

## [0.1.0] — 2026-07-12

First public release as **Apprentice** (formerly the internal `qwen-pipeline`).

### Added
- **MCP server** (`src/server.py`, FastMCP over stdio) exposing three tools:
  - `delegate(task, role, provider?, context?, model?)` — stateless snippet delegation with a
    mechanical gate + worker→worker auto-retry.
  - `assign(task, done_when, repo, …, model?)` — file-aware worker agent (Aider) that edits a
    disposable git worktree, grinds a task to an objective `done_when`, and mechanically applies
    the resulting diff to the real tree.
  - `log_correction(…)` — records corrections (diff-only, `output_id`-referenced) for retrieval.
- **Two-tier Gemini (Vertex AI) worker.** `delegate`/`assign` accept a `model` arg (`"flash"` /
  `"pro"`) that selects a Vertex model per task. Credentials configured once in `providers.gemini`
  and propagated to both the `google-genai` (delegate) and Aider/litellm (assign) paths.
- **Config overlay:** `config/qwen.local.json` (gitignored) is deep-merged over the committed
  `config/qwen.json`, keeping all secrets, project ids, credential paths, and machine-specific
  absolute paths out of version control.
- **In-context retrieval** (`src/retrieval.py`): past corrections are embedded and injected as
  few-shot examples for similar future tasks — learning without weight training.
- **Mechanical gate** (`src/gate.py`): per-language checks (Python `py_compile`, TypeScript `tsc`,
  C++ heuristic lint) with verbatim-error worker retries.
- **Cost cascade & metering** (`src/metering.py`): cost-ordered escalation and a per-delegation
  cost/outcome log with optional per-provider daily token budgets.
- **Docs:** `docs/MULTI_AGENT.md` (beginner-friendly explainer), `docs/CONFIGURATION.md` (config
  reference + enabling Gemini), `CONTRIBUTING.md`, and this changelog.
- **Tests:** deterministic offline suite (`tests/test_pipeline.py`, 13 cases; providers/embeddings
  stubbed).

### Fixed
- **Gemini via Aider used the wrong litellm prefix** (`gemini/…`, the AI-Studio API-key path,
  which ignores a service account). Corrected to `vertex_ai/…` for service-account / ADC auth.

### Security
- `.gitignore` excludes secrets (`config/qwen.local.json`, `secrets/`, `*-service-account*.json`,
  `.env*`), private data (`corrections/*.jsonl`, `outputs/`, `metrics/`), and large/local assets
  (`models/`, virtualenvs, `node_modules/`). The stdio server runs with full user privileges;
  dependencies are pinned (2026 stdio command-injection advisory).
