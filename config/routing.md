# Routing rules — what Claude delegates, to whom

> Loaded via `E:\projects\UE_MCP\CLAUDE.local.md`. Claude Code is the orchestrator and
> decision-maker; it reads these rules to decide **whether** to delegate a sub-task and,
> if so, **which provider + role** to use. Iterate these rules based on what review finds
> (if a task type keeps coming back wrong, stop delegating it and note it here).

## 1. Delegate to a worker vs. keep on Claude

**Delegate** (well-specified, self-contained, low-ambiguity, cheap to verify):
- Implement a function to a clear signature / spec.
- Write tests for existing, stable code.
- Mechanical refactors that don't change behavior.
- Boilerplate (DTOs, schema objects, simple adapters) with a precise shape.

**Keep on Claude** (judgment-heavy or expensive to get wrong):
- Architecture, public API/contract design, cross-file or multi-step changes.
- Ambiguous or under-specified work (decide the spec first, *then* maybe delegate).
- **Anything security-sensitive** — this repo has an active Phase 0 security workstream:
  path handling, command execution, input validation, auth/token logic, transport bind.
- Anything where a subtle wrong answer is costly (engine-version compatibility decisions,
  data migrations, release-affecting code).
- The **review and final say** — Claude always reviews worker output and owns the commit.

> Rule of thumb: delegate the *typing*, keep the *thinking*. The win only exists when the
> worker is right often enough that review-and-occasionally-fix beats authoring from scratch.

## 2. Provider selection

**Two implementers, Claude is the boss.** There are exactly two worker brains — pick per task:

| Provider | `model` | Role | When to use | Cost |
|----------|---------|------|-------------|------|
| `qwen` (default) | — | **Implementer** | Bulk/routine delegation. Local, free, owns the GPU. **Start here.** | Free (local) |
| `gemini` | `"flash"` | **Implementer (routine cloud)** | Routine work when the GPU is busy (UE/ComfyUI) or qwen keeps failing a routine task. | Cloud, cheap |
| `gemini` | `"pro"` | **Implementer (hard)** | Genuinely hard but well-specified tasks beyond qwen. | Cloud, pricier |
| `openai`/Codex | — | Implementer | Not enabled (no key) — optional future slot. | — |

- **The `model` arg picks the Gemini tier.** `delegate(..., provider="gemini", model="pro")` or
  `assign(..., provider="gemini", model="flash")`. Empty `model` = the config `default_model`
  (`flash`). Flash for routine cloud typing; Pro only when the task is genuinely hard — Pro burns
  Vertex credits faster.
- **Same bar for every provider AND tier.** Gemini is a stronger *implementer*, not a special
  case: its output goes through the **exact same** §6.1 mechanical gate + worker-retry and the same
  `assign` `done_when` / Claude-authored strict tests as qwen. There is **no "reviewer" tier** —
  Claude is the only judge.
- **Default to `qwen`.** Reach for `gemini` `flash` when the GPU is contended or a routine task
  bounces; reach for `gemini` `pro` when a task is measurably too hard for qwen or needs very
  large context. Claude routes directly (not only via auto-escalation).
- **Cost logic: local = free but weaker; cloud = stronger but metered.** A weak model whose
  output passes the gate + the project's tests is worth the same as a strong one and cost
  nothing — so always try free first for routine work, and let the daily budgets
  (`metering.budgets`, enforced) bound cloud spend. Other providers (openai/GPT, or any
  config-added openai-compatible endpoint) slot into the same ladder by price.
- **Prefer the token-cheap delegate form for routine repo work:** pass `repo` +
  `context_files` (paths, NOT pasted code), `apply_to` + `test_cmd` (the project's own test —
  the server applies, verifies, reverts on red), and `return_mode="summary"`. Your cost per
  routine task ≈ spec in, two-line footer out. Use `return_mode="full"` only when you intend
  to actually review the code line-by-line.
- **Parallel fan-out (the two workers run at once):** for independent sub-tasks Claude may issue a
  `qwen` call and a `gemini` call in the **same turn** — only `qwen` is GPU-local, so the cloud
  worker adds no hardware contention. Each `assign` runs in its own disposable worktree, so two
  parallel assigns don't collide. (Avoid two heavy `qwen` calls at once: one model, one GPU. And
  the Tier-2 UE build `host_verify.py` stays serialized — it's editor-locked.)

## 3. Repo area → default role

| Repo area | Language | Default `role` |
|-----------|----------|----------------|
| `src/` (TypeScript MCP server) | TypeScript | `ts_implementer` |
| `plugins/McpAutomationBridge/` (UE bridge) | UE 5.x C++ | `cpp_implementer` |
| `plugins/UnrealAgent/` | UE 5.x C++ | `cpp_implementer` |
| `scripts/` | Python | `py_implementer` |
| tests for any of the above | per target lang | `test_writer` |
| behavior-preserving cleanup | any | `refactorer` |

## 4. Always close the loop
Every delegation ends with `log_correction(...)` — even when the worker was correct
(`error_category="none"`, `corrected_output == qwen_output`). See `CLAUDE.local.md` for the
full delegate → review → fix → log loop and the `error_category` values.

## 5. Special caution for `cpp_implementer`
UE C++ must build across **UE 5.0–5.8**. Delegated C++ is rarely final: review for
version-guarding (`#if ENGINE_MAJOR_VERSION`/`__has_include`/`MCP_HAS_*`), no new
third-party deps, and correct module/API usage. When in doubt, keep C++ on Claude.

## 6. Cost cascade + front gate (§6.4)
Two ways a harder task reaches the stronger implementer (gemini): **(a) Claude routes it
there directly** — the primary path (task looks too hard for qwen → `provider="gemini"`);
and **(b) auto-escalation** — the §6.1 gate still fails after qwen's retries, **or the
`test_cmd` acceptance run keeps failing after apply**, so the pipeline retries on gemini
(carrying the failed attempt + verbatim checker/test output — never a cold restart). Both run gemini's output through the **same** gate + strict tests as qwen.
Order of cost: **qwen (local, free) → gemini flash (cheap) → gemini pro (pricier) → Claude
(judgment only)**. Auto-escalation is a no-op until `providers.gemini.enabled` (creds pending;
escalation uses the tier's default model). **Gemini implements; it never "reviews" — Claude is the
only judge.**

**Front gate — what YOU (Claude) send straight to yourself, skipping the cascade.**
Escalation isn't free (a hard task that fails qwen, then gemini, then lands on Claude
cost GPU + a gemini call on top). So do NOT delegate these — author/review them directly:
- **security** — the Phase 0 workstream; never delegate (path/exec/auth/token/transport).
- **architecture / public API / cross-file / multi-step** changes.
- **ambiguous / under-specified** work — pin the spec first, *then* maybe delegate.
These mirror the "keep on Claude" list in §1 and the config `cascade.skip_cascade_categories`.

**Cache-friendly routing (§6.3).** Escalation crosses a prompt-cache boundary (each
model/tier caches in its own lane). Prefer to finish a task on ONE tier rather than
ping-ponging qwen→gemini→Claude within a single task; batch same-tier work together.
Claude Code already caches its own stable prefix (system + CLAUDE.local.md + routing.md +
MCP tool schemas) automatically — the pipeline keeps those byte-stable (the variable
gate/output_id status lives in delegate's *return*, never in the tool schema), so cache
reads stay warm. Keep the tool list small (`delegate`, `log_correction`, `assign`).
