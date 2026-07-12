# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
