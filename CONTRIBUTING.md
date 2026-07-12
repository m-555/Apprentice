# Contributing

Thanks for your interest! This is a small, focused project — a local multi-provider code-delegation
worker exposed to an orchestrator (Claude Code) over MCP.

## Ground rules

- **Pin dependencies.** Never float the MCP SDK or add unpinned installs (a 2026 stdio
  command-injection advisory makes pinning the documented mitigation). Bump deliberately + re-test.
- **Keep the tool surface small.** The MCP tools (`delegate`, `log_correction`, `assign`) load into
  the orchestrator's context every turn; don't add tools casually.
- **Never commit secrets or private data.** `config/qwen.local.json`, `secrets/`, `corrections/*.jsonl`,
  `outputs/`, `metrics/`, and `models/` are gitignored for good reasons — keep them that way.
- **Security posture.** The stdio server runs as a subprocess with full user privileges. Treat any
  new file-reading / command-running surface (especially in the `assign` agent) as attack surface.

## Dev setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # core (Windows path shown)
.venv/Scripts/pip install -r requirements-gemini.txt   # optional: Gemini/Vertex provider
python -m venv .aider-venv
.aider-venv/Scripts/pip install -r requirements-aider.txt   # optional: the `assign` agent
```

You also need [Ollama](https://ollama.com) running locally with a worker model pulled (see the
README). Configuration lives in `config/` — see [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Tests

The suite is deterministic and offline (providers/embeddings are stubbed — no Ollama or network
needed):

```bash
.venv/Scripts/python tests/test_pipeline.py     # self-running, prints PASS/FAIL
```

Please keep it green and add a case for any behavior change.
