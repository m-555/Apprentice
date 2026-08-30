# OpenCode and the local Qwen/Qwen Coder/DeepSeek models

Apprentice and OpenCode are both agent runtimes, but they solve different problems. This
repository now supports using the two local models in both runtimes without nesting one
tool loop inside the other.

## The four layers

```text
Model weights       Qwen3.8 / Qwen Coder / DeepSeek V4 GGUF files (the learned brain)
Inference engine    llama.cpp (loads weights and produces tokens)
Agent runtime       OpenCode or Apprentice (tools, loop, sessions, permissions)
Project rules       AGENTS.md, prompts, tests, verification policy
```

`Llama` is also the name of Meta's model family, which is a separate meaning from
`llama.cpp`. Here, **llama.cpp is software**, not the brain. It can run Qwen, DeepSeek,
Llama-family, and many other GGUF models.

## What Apprentice already did

Before this OpenCode configuration, Apprentice used:

- Ollama at the provider layer for `qwen3-coder-next` (now a deprecated compatibility
  path; normal coding and retrieval use the llama.cpp supervisor).
- Its own `apprentice chat` tool loop for standalone coding.
- Optional Aider in disposable worktrees for the MCP `assign` worker.
- Mechanical gates, project tests, automatic rollback, correction retrieval, metering,
  escalation, session persistence, and a JSON-lines UI protocol.

That means Apprentice was already an agent; Ollama/llama.cpp are model servers. OpenCode
is another agent runtime, not a model and not a replacement for Apprentice's verified
learning loop.

## Recommended relationship

Use the shared router in `E:\projects\local-opencode` as the inference layer:

```text
                           +-> OpenCode tools and subagents
Qwen/Qwen Coder/DeepSeek -> llama.cpp supervisor |
                           +-> Apprentice verification and learning loop
```

Do not make `apprentice chat` call OpenCode as if OpenCode were an LLM API. That would put
one agent loop inside another: both would decide which files to read, what to edit, and
which commands to run. Apprentice could no longer reliably associate one model decision
with one verified/reverted turn.

## Use OpenCode on Apprentice

Start the router first:

```powershell
cd E:\projects\local-opencode
.\scripts\start-router.ps1
```

Then open this repository:

```powershell
cd E:\projects\qwen-pipeline
opencode
```

The project configuration supplies `apprentice-maintainer` plus read-only explorer,
implementer, reviewer, and opt-in DeepSeek architect roles. Use `@apprentice-explorer` or
another `@name` directly, or let the primary maintainer delegate a bounded task.

## Use the same models inside Apprentice

The machine-local `config/qwen.local.json` can define OpenAI-compatible providers pointed
at `http://127.0.0.1:8080/v1`:

```json
{
  "providers": {
    "local_qwen38": {
      "enabled": true,
      "kind": "openai-compatible",
      "base_url": "http://127.0.0.1:8080/v1",
      "model": "qwen3.8-27b-q8-tuber",
      "options": {"temperature": 0.15}
    },
    "local_deepseek_v4": {
      "enabled": true,
      "kind": "openai-compatible",
      "base_url": "http://127.0.0.1:8080/v1",
      "model": "deepseek-v4-flash-0731-ud-iq2-m",
      "options": {"temperature": 0.1}
    }
  }
}
```

The built-in `qwen` provider now points to Qwen 3.8 on the same endpoint. The optional
`qwen_coder` provider selects `qwen3-coder-next-q4-k-m`. The Aider workers use
`openai/<model-id>` with `OPENAI_API_BASE=http://127.0.0.1:8080/v1`; no Ollama API is
needed. Nomic retrieval embeddings use the supervisor's `/v1/embeddings` endpoint.

Examples:

```powershell
apprentice chat --provider qwen --repo .
apprentice chat --provider qwen_coder --repo .
apprentice run "explain the provider registry" --provider local_deepseek_v4 --repo .
```

Qwen 3.8 should be the normal choice. The current compatible standalone Qwen Coder
runtime is CPU-only on this machine (measured near 5 tok/s), so it is opt-in. DeepSeek
maps roughly 91 GB of weights, the model profile
recommends 128 GB RAM, and this machine has 64 GB; it is therefore a slow, focused-review
option rather than a default worker.

## Multiple agents on one GPU

Several agents can have different prompts, context, permissions, and responsibilities
while sharing one model. They are a team in the organizational sense, not extra model
copies. The router is intentionally configured for one resident model and one inference
slot, so local subagent model calls are serialized. Parallel agents still help with clean
context separation; they do not multiply GPU speed.
