"""Apprentice — MCP server (stdio). (Project formerly "qwen-pipeline"; the MCP server id
stays "qwen" and the per-repo override file stays ".qwen-pipeline.json" for compatibility.)

Three tools (schemas load into context every turn — keep the list small):
  - delegate(task, role, provider?, context?) -> generated code + gate/id footer (str)
  - log_correction(...) -> {"ok": True}
  - assign(task, done_when, repo, ...) -> Phase-7 file-aware agent TDD loop (dict)

Claude Code is the orchestrator/decision-maker: it picks the `provider` per task,
reviews the returned code, and logs corrections. Providers are just token generators
(local Qwen now; Gemini/Vertex and OpenAI are pluggable — see providers.py).

Phases layered in: §5 retrieval few-shot, §6.1 mechanical gate + worker->worker
auto-retry, §6.2 output-id store + diff-only logging, §6.4 cost cascade, §6.5 metering.

Runs as a subprocess Claude Code spawns over stdio — no network port, no auth surface.
Config is re-read on every tool call so tuning takes effect WITHOUT restarting the
subprocess (code changes still need a fresh session — Claude Code respawns the server).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from roles import ROLES, role_names
from providers import PROVIDERS, provider_names
import retrieval
import gate
import metering
import store
import agent

# --- paths (relative to this file: src/ -> repo root) -----------------------
_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "config" / "qwen.json"
# Optional gitignored overlay for machine-local, secret, or per-user values (real GCP
# project, credentials file path, exact model ids, enabled flags). Deep-merged OVER
# qwen.json so the committed config stays free of secrets. Ship qwen.local.example.json.
_LOCAL_CONFIG_PATH = _ROOT / "config" / "qwen.local.json"
_CORRECTIONS_PATH = _ROOT / "corrections" / "corrections.jsonl"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` into `base` (overlay wins on scalars; dicts merge)."""
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _load_config() -> dict[str, Any]:
    try:
        cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    if _LOCAL_CONFIG_PATH.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(_LOCAL_CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


_CFG = _load_config()
_DEFAULT_PROVIDER = _CFG.get("providers", {}).get("default", "qwen")

_VALID_ERROR_CATEGORIES = {
    "logic", "compile", "style", "edge_case", "security", "api_misuse", "none",
}

mcp = FastMCP("qwen")


def _refresh_config() -> None:
    """Reload config at the start of each tool call so edits to config/qwen.json (gate
    toggles, retries, retrieval, cascade, metering) take effect live — no need to
    restart the server subprocess mid-session. Cheap: it's a small JSON file."""
    global _CFG, _DEFAULT_PROVIDER
    _CFG = _load_config()
    _DEFAULT_PROVIDER = _CFG.get("providers", {}).get("default", "qwen")


def _provider_enabled(name: str) -> bool:
    return bool(_CFG.get("providers", {}).get(name, {}).get("enabled", False))


def _gemini_agent_env() -> dict[str, str]:
    """Vertex env for the Aider worker, derived from the single source (providers.gemini)
    so credentials live in ONE place. litellm's vertex_ai path reads these env vars."""
    g = _CFG.get("providers", {}).get("gemini", {})
    env: dict[str, str] = {}
    if g.get("credentials_file"):
        env["GOOGLE_APPLICATION_CREDENTIALS"] = g["credentials_file"]
    if g.get("project"):
        env["VERTEXAI_PROJECT"] = g["project"]
    if g.get("location"):
        env["VERTEXAI_LOCATION"] = g["location"]
    return env


def _write_correction(record: dict[str, Any]) -> bool:
    """Append a correction record to corrections.jsonl and index it for retrieval.

    Shared by the MCP `log_correction` tool (Claude-authored corrections) and the
    §6.1 worker->worker auto-retry loop (machine-verified corrections). Returns
    whether the record was indexed (embedding is fail-safe — the record is never lost).
    """
    _CORRECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _CORRECTIONS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        return retrieval.index_record(record, _CFG)
    except Exception:
        return False


def _call_provider(prov, system, user, usage_acc, model=""):
    """Call a provider, folding its per-call usage (tokens/duration) into usage_acc
    and counting worker calls — the raw material for §6.5 metering. `model` is an
    optional per-call model override (e.g. gemini flash vs pro); empty = provider default."""
    u: dict[str, Any] = {}
    text = PROVIDERS[prov](system, user, _CFG, u, model)
    usage_acc["worker_calls"] += 1
    usage_acc["tokens_in"] += int(u.get("tokens_in", 0) or 0)
    usage_acc["tokens_out"] += int(u.get("tokens_out", 0) or 0)
    usage_acc["duration_s"] += float(u.get("duration_s", 0.0) or 0.0)
    return text


def _gate_and_retry(task, role, prov, system, user, worker_output, usage_acc, model=""):
    """§6.1: run the mechanical gate on a worker output; on failure, bounce the
    verbatim checker error back to the SAME worker up to `max_retries` times — zero
    Claude tokens. Every passing worker->worker fix is logged as a correction record
    (corrected_by="worker_retry"), so the retrieval store grows for free.

    Returns (final_output, GateResult, attempts).
    """
    result = gate.run_gate(worker_output, role, _CFG)
    if result.status != "fail":
        return worker_output, result, 1

    max_retries = int(_CFG.get("gate", {}).get("max_retries", 2))
    prior, prior_result = worker_output, result
    for attempt in range(1, max_retries + 1):
        fix_user = gate.build_retry_prompt(user, prior, prior_result.error_text)
        try:
            fixed = _call_provider(prov, system, fix_user, usage_acc, model)
        except Exception:
            # Worker unreachable mid-retry: return the last attempt for Claude review.
            return prior, prior_result, attempt
        new_result = gate.run_gate(fixed, role, _CFG)
        if new_result.status != "fail":
            # The retry now passes → this is a valid, machine-verified correction.
            try:
                _write_correction({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "provider": prov,
                    "role": role,
                    "task": task,
                    "context": "",
                    "qwen_output": prior,
                    "corrected_output": fixed,
                    "error_category": prior_result.error_category,
                    "explanation": (
                        f"Failed mechanical gate ({prior_result.check}); worker fixed it "
                        f"on retry {attempt} from the verbatim checker error."
                    ),
                    "machine_verified": True,
                    "corrected_by": "worker_retry",
                })
            except Exception:
                pass
            return fixed, new_result, attempt + 1
        prior, prior_result = fixed, new_result

    # Still failing after N retries → the cascade (caller) decides escalation.
    return prior, prior_result, max_retries + 1


def _delegate_on_tier(task, role, prov, system, user, model=""):
    """One tier's full attempt: worker call + §6.1 gate/retry + a §6.5 metering event.
    Returns (final_output, GateResult, attempts)."""
    usage_acc = {"worker_calls": 0, "tokens_in": 0, "tokens_out": 0, "duration_s": 0.0}
    worker_output = _call_provider(prov, system, user, usage_acc, model)
    final_output, result, attempts = _gate_and_retry(
        task, role, prov, system, user, worker_output, usage_acc, model
    )
    metering.record({
        "tier": prov,
        "model": model,
        "role": role,
        "gate_status": result.status,
        "gate_check": result.check,
        "attempts": attempts,
        "machine_verified": result.status == "pass",
        **usage_acc,
        **metering.task_ref(task),
    }, _CFG)
    return final_output, result, attempts


def _delegate_cascade(task, role, prov, system, user, model=""):
    """§6.4 cost-ordered cascade. Try the requested tier; if it still FAILS the
    mechanical gate after retries and an escalation tier is configured+enabled, try
    that next-cheapest tier before falling through to Claude. Skipped/pass short-circuit.

    Returns (final_output, GateResult, attempts, tier_used).
    """
    final_output, result, attempts = _delegate_on_tier(task, role, prov, system, user, model)
    if result.status != "fail":
        return final_output, result, attempts, prov

    casc = _CFG.get("cascade", {})
    esc = casc.get("escalate_to", "")
    # Only escalate to a DIFFERENT, known, ENABLED provider (gemini is gated on creds).
    # The escalation tier uses its own default model (the `model` override was chosen for
    # the originally-requested tier, so it doesn't carry across a provider boundary).
    if esc and esc != prov and esc in PROVIDERS and _provider_enabled(esc):
        f2, r2, a2 = _delegate_on_tier(task, role, esc, system, user)
        # Return the escalated attempt regardless — if it also failed, it's still the
        # freshest attempt for Claude, clearly flagged NOT machine-verified.
        return f2, r2, a2, esc

    # No escalation available → hand the last attempt to Claude (per §6.4 fall-through).
    return final_output, result, attempts, prov


def _footer(result, attempts: int, tier: str, output_id: str) -> str:
    """One-line, out-of-fence status marker: gate verdict + tier + the §6.2 output_id
    (so a later log_correction can reference this exact worker output by id)."""
    tag = f"tier={tier} output_id={output_id}"
    if result.status == "pass":
        return (f"\n\n---\n[qwen-pipeline] machine_verified=true check={result.check} "
                f"attempts={attempts} {tag}")
    if result.status == "skipped":
        return (f"\n\n---\n[qwen-pipeline] machine_verified=skipped reason={result.check} "
                f"{tag} — review normally.")
    return (f"\n\n---\n[qwen-pipeline] machine_verified=false status=fail "
            f"check={result.check} attempts={attempts} {tag} — worker could not satisfy "
            f"the gate; review/fix needed. Last checker error:\n{result.error_text}")


@mcp.tool()
def delegate(task: str, role: str, provider: str = "", context: str = "",
             model: str = "") -> str:
    """Delegate a self-contained coding sub-task to a worker model.

    The pipeline mechanically verifies the output (compile/lint) and auto-retries the
    worker on failure — for free — before returning. The returned text ends with an
    out-of-fence status line: `machine_verified=…`, the tier used, and an `output_id`.
    Pass that `output_id` to `log_correction` so you only send a diff, not the whole file.

    Args:
        task: The exact sub-task to implement (clear, low-ambiguity).
        role: System-prompt selector. One of: ts_implementer, cpp_implementer,
            py_implementer, test_writer, refactorer.
        provider: Which worker to use: qwen (local, free, default), gemini (Vertex),
            openai (future). Empty = the configured default (qwen).
        context: Optional surrounding code, signature, or spec.
        model: Optional per-call model override. For gemini, use "flash" (routine, cheap)
            or "pro" (hard) — these map to providers.gemini.models in config. Empty =
            the provider's configured default model.

    Returns:
        The worker's generated code (review it) followed by the status/output_id footer.
    """
    _refresh_config()
    prov = provider or _DEFAULT_PROVIDER
    if prov not in PROVIDERS:
        raise ValueError(
            f"Unknown provider '{prov}'. Valid: {', '.join(provider_names())}."
        )
    if role not in ROLES:
        raise ValueError(
            f"Unknown role '{role}'. Valid roles: {', '.join(role_names())}."
        )
    user = task if not context else f"{task}\n\n--- CONTEXT ---\n{context}"

    # §5: retrieve top-k similar past corrections for (provider, role) and inject them
    # as few-shot. Fail-safe — retrieval problems must never block a delegation.
    system = ROLES[role]
    try:
        fewshot = retrieval.format_fewshot(
            retrieval.retrieve(task, prov, role, _CFG)
        )
        if fewshot:
            system = f"{system}\n\n{fewshot}"
    except Exception:
        pass

    # §6.1 gate + retry, wrapped in the §6.4 cascade (escalate a persistent gate failure
    # to the next-cheapest enabled tier before it ever reaches Claude).
    final_output, result, attempts, tier = _delegate_cascade(
        task, role, prov, system, user, model
    )

    # §6.2 store the returned worker output under an id so log_correction can diff it.
    output_id = store.new_id()
    try:
        store.put(output_id, final_output, provider=tier, role=role, task=task)
    except Exception:
        pass

    return final_output + _footer(result, attempts, tier, output_id)


@mcp.tool()
def log_correction(
    role: str,
    task: str,
    error_category: str,
    explanation: str,
    output_id: str = "",
    correction_patch: str = "",
    qwen_output: str = "",
    corrected_output: str = "",
    provider: str = "qwen",
    context: str = "",
) -> dict[str, Any]:
    """Log a correction (powers §5 retrieval). Call after every delegation — even when
    the worker was correct (error_category="none", empty correction_patch).

    PREFERRED (cheap, §6.2): pass `output_id` (from the delegate footer) + a unified-diff
    `correction_patch` of your changes. The pipeline already stored the worker output, so
    it reconstructs both sides — you never re-transmit the worker's code. An empty patch
    means "accepted as-is". LEGACY: omit output_id and pass qwen_output + corrected_output.

    Args:
        role: The role used for the delegation.
        task: The exact task string sent to the worker.
        error_category: logic | compile | style | edge_case | security | api_misuse | none.
        explanation: Short reusable why-it-was-wrong note.
        output_id: The id from the delegate footer (enables diff-only logging).
        correction_patch: Unified diff of your fix against the worker output (empty = as-is).
        qwen_output: LEGACY — verbatim worker output (only if not using output_id).
        corrected_output: LEGACY — your corrected version (only if not using output_id).
        provider: Which worker produced the output (qwen | gemini | openai).
        context: Any context sent with the task.
    """
    _refresh_config()
    if error_category not in _VALID_ERROR_CATEGORIES:
        raise ValueError(
            f"Invalid error_category '{error_category}'. "
            f"Valid: {', '.join(sorted(_VALID_ERROR_CATEGORIES))}."
        )

    patch_note = ""
    if output_id:
        stored = store.get(output_id)
        if stored is None:
            raise ValueError(
                f"Unknown output_id '{output_id}'. Pass the id from the delegate footer, "
                f"or use the legacy qwen_output/corrected_output fields."
            )
        qwen_output = stored.get("output", "")
        provider = stored.get("provider", provider)
        if not corrected_output:
            reconstructed, err = store.apply_patch(qwen_output, correction_patch)
            if reconstructed is None:
                # Apply failed — keep the record (don't lose signal); fall back to the
                # worker output as corrected and record the failure in the explanation.
                corrected_output = qwen_output
                patch_note = f" [patch apply failed: {err}]"
            else:
                corrected_output = reconstructed

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "role": role,
        "task": task,
        "context": context,
        "output_id": output_id,
        "correction_patch": correction_patch,
        "qwen_output": qwen_output,
        "corrected_output": corrected_output,
        "error_category": error_category,
        "explanation": explanation + patch_note,
        "machine_verified": False,
        "corrected_by": "claude",
    }
    # Fail-safe embed+index inside the shared writer — never lose the correction if the
    # embedder is unreachable (rebuild later via `python retrieval.py reindex`).
    indexed = _write_correction(record)

    # §6.5 metering — a Claude-authored correction means Claude had to step in. Token
    # counts aren't visible here (Claude runs in Claude Code), but the step-in itself is
    # the decision-relevant signal.
    metering.record({
        "tier": "claude",
        "role": role,
        "error_category": error_category,
        "machine_verified": False,
        **metering.task_ref(task),
    }, _CFG)

    return {"ok": True, "indexed": indexed}


@mcp.tool()
def assign(task: str, done_when: str, repo: str, provider: str = "",
           files: str = "", max_iters: int = 0, apply: bool = True,
           model: str = "") -> dict[str, Any]:
    """Phase 7 — delegate a whole task to a FILE-AWARE worker agent (Aider) that reads the
    repo itself, then grind it to an OBJECTIVE 'done' with NO Claude in the loop.

    Your job as orchestrator: DEFINE THE TASK, DEFINE DONE, delegate here, then review the
    small summary + commit. Author the acceptance test in the repo FIRST, then pass the
    command that runs it as `done_when` — the worker edits (in a throwaway git worktree, so
    your real tree is untouched) until `done_when` exits 0 or `max_iters` is hit. The worker's
    failures bounce back to the worker, not to you.

    Args:
        task: What to implement (clear goal). You define this.
        done_when: A shell command run in the worktree that must exit 0 = 'done' (e.g.
            "python -m pytest tests/test_x.py -q"). This is your machine-checkable spec.
        repo: Absolute path to the target git repo. ANY repo — this is project-agnostic.
        provider: Worker model: qwen (local, default) or gemini (when enabled).
        files: Optional space-separated file hints to focus the agent (else it uses the repo map).
        max_iters: Max worker attempts (0 = config default).
        model: Optional model override. For gemini, "flash" (routine) or "pro" (hard);
            maps to agent.models.gemini in config. Empty = the provider's default model.

    Returns:
        A Claude-cheap summary: {done_passed, iterations, files_changed, patch_path,
        done_log_tail, worker_log_tail, output_id}. Apply the change with `git apply <patch_path>`
        in `repo`, then commit. (The full diff is in the patch file — you needn't ingest it.)
    """
    _refresh_config()
    if provider and provider not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Valid: {', '.join(provider_names())}.")
    if not Path(repo).is_dir():
        raise ValueError(f"repo path does not exist: {repo}")
    file_list = files.split() if files else []
    eff_provider = provider or _DEFAULT_PROVIDER
    # Inject Vertex creds/env into the Aider worker for gemini, from the single source
    # (providers.gemini) — without mutating _CFG (deep-merge returns a fresh dict).
    agent_cfg = _CFG.get("agent", {})
    if eff_provider == "gemini":
        agent_cfg = _deep_merge(agent_cfg, {"models": {"gemini": {"env": _gemini_agent_env()}}})
    result = agent.run_agent_task(
        task=task, done_when=done_when, repo=repo, provider=eff_provider,
        files=file_list, max_iters=max_iters, agent_cfg=agent_cfg,
        outputs_dir=_ROOT / "outputs", apply=apply, model=model,
    )
    metering.record({
        "tier": eff_provider,
        "model": model,
        "mode": "assign",
        "done_passed": result["done_passed"],
        "iterations": result["iterations"],
        **metering.task_ref(task),
    }, _CFG)
    return result


if __name__ == "__main__":
    mcp.run()  # stdio transport
