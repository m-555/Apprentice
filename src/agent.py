"""Phase 7 — file-aware worker agents (Aider) run in a disposable git worktree.

Division of labour (the whole point):
  • Claude (boss): DEFINE THE TASK + DEFINE DONE (a `done_when` command) + delegate + commit.
  • This module: a worker agent (Aider driving a local/cloud model) reads the target repo
    ITSELF in an isolated worktree and edits toward the task; the pipeline runs `done_when`
    as the OBJECTIVE acceptance check and bounces failures back to the worker until it passes
    or `max_iters`. The real working tree is never touched — the deliverable is a diff.

PROJECT-AGNOSTIC & REUSABLE: works on ANY git repo (`repo=<abs path>`), any `done_when`
shell command. Per-repo overrides via an optional `<repo>/.qwen-pipeline.json`. Nothing here
is specific to UE_MCP — drop it into another project by pointing `repo` at it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 600,
         env: dict[str, str] | None = None) -> tuple[int | None, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True,
                              text=True, env=env)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError as exc:
        return None, f"not found: {exc}"
    except subprocess.TimeoutExpired:
        return None, "timed out"


def load_project_cfg(repo: str, agent_cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge the pipeline's agent config with an optional per-repo `.qwen-pipeline.json`
    (repo-local wins). This is what makes the pipeline reusable across projects."""
    merged = dict(agent_cfg)
    proj_path = Path(repo) / ".qwen-pipeline.json"
    if proj_path.exists():
        try:
            proj = json.loads(proj_path.read_text(encoding="utf-8"))
            merged.update(proj.get("agent", proj))
        except (json.JSONDecodeError, OSError):
            pass
    return merged


def _model_for(provider: str, cfg: dict[str, Any],
               model: str = "") -> tuple[str, dict[str, str]]:
    """Resolve (litellm model id, extra env) for a provider + optional tier/model hint.

    `cfg` is the merged `agent` config block. A provider entry may be either:
      • single-model (qwen): {"model": "ollama_chat/...", "env": {...}}
      • two-tier   (gemini): {"default_model": "flash", "env": {...},
                              "models": {"flash": "vertex_ai/gemini-...",
                                         "pro":   "vertex_ai/gemini-..."}}
    `model` selects the tier ("flash"/"pro") or is a raw litellm id passed through.
    NOTE for Vertex: the id MUST use litellm's `vertex_ai/` prefix (service-account/ADC),
    NOT `gemini/` (which is the AI-Studio API-key path and ignores the service account).
    """
    m = cfg.get("models", {}).get(provider or "qwen", {})
    env = m.get("env", {}) or {}
    submodels = m.get("models")
    if isinstance(submodels, dict) and submodels:
        tier = model if model in submodels else m.get("default_model", "flash")
        model_id = submodels.get(tier) or model or next(iter(submodels.values()))
        return model_id, env
    return (model or m.get("model", "ollama_chat/qwen3-coder-next:latest")), env


def make_worktree(repo: str, root: str) -> str:
    """Create a disposable detached worktree of `repo` at HEAD. Returns its path."""
    base = root or tempfile.gettempdir()
    Path(base).mkdir(parents=True, exist_ok=True)
    wt = str(Path(base) / f"qwen_wt_{uuid.uuid4().hex[:10]}")
    rc, out = _run(["git", "-C", repo, "worktree", "add", "--detach", wt, "HEAD"], timeout=120)
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {out.strip()}")
    return wt


def remove_worktree(repo: str, wt: str) -> None:
    _run(["git", "-C", repo, "worktree", "remove", "--force", wt], timeout=120)
    if Path(wt).exists():
        shutil.rmtree(wt, ignore_errors=True)


def _run_aider(wt: str, model: str, model_env: dict[str, str], message: str,
               files: list[str], cfg: dict[str, Any]) -> tuple[int | None, str]:
    aider = cfg.get("aider_exe", "aider")
    cmd = [aider, "--model", model, "--yes-always", "--no-auto-commits", "--no-stream",
           "--no-check-update", "--no-show-model-warnings", "--no-gitignore",
           "--map-tokens", str(cfg.get("map_tokens", 512)),
           "--message", message, *files]
    env = {**os.environ, **model_env}
    return _run(cmd, cwd=wt, timeout=int(cfg.get("aider_timeout_s", 600)), env=env)


# Worker/build junk excluded from the extracted diff so the patch is ONLY the real source
# change — never Aider history or artifacts the `done_when` build/compile produced in the
# worktree. Override per-repo via `<repo>/.qwen-pipeline.json` -> agent.diff_excludes.
_DEFAULT_DIFF_EXCLUDES = [
    ".aider*", "*.tags.cache*",                      # aider artifacts
    ".qwen_done.cmd",                                # our done_when wrapper script
    "__pycache__", "*.pyc", ".pytest_cache",         # python
    "node_modules", "dist", "out", "build",          # js/ts build outputs
    "*.js.map", "*.d.ts.map", "tsconfig.tsbuildinfo",
    "Binaries", "Intermediate", "*.obj", "*.o",      # UE / native build outputs
]


def _write_done_script(wt: str, done_when: str) -> str:
    """Write `done_when` into a .cmd script inside the worktree and return its path.

    Running the check as a SCRIPT (cmd /c <file>) instead of `cmd /c <string>` stops
    cmd.exe from re-tokenizing the command line — quoted multi-word args like
    `findstr /C:"export function foo"` or quoted exe paths survive intact. (This was a
    real failure: the re-tokenized findstr treated the quoted words as filenames, so
    done_when never passed even though the worker had done the work.)
    """
    path = Path(wt) / ".qwen_done.cmd"
    path.write_text("@echo off\r\n" + done_when + "\r\n", encoding="utf-8")
    return str(path)


def _excluded(path: str, patterns: list[str]) -> bool:
    """True if any path segment or the basename matches an exclude glob (handles nested
    dirs like __pycache__ without fragile pathspec magic)."""
    import fnmatch
    segs = path.replace("\\", "/").split("/")
    for pat in patterns:
        if fnmatch.fnmatch(path, pat) or any(fnmatch.fnmatch(s, pat) for s in segs):
            return True
    return False


def _worktree_diff(wt: str, excludes: list[str]) -> str:
    """Diff of the REAL changes (incl. new source files) vs HEAD, excluding worker/build
    junk (Aider history/caches, __pycache__, .pyc) so the patch is only the actual change."""
    _run(["git", "-C", wt, "add", "-A"], timeout=60)
    _rc, names = _run(["git", "-C", wt, "diff", "--cached", "HEAD", "--name-only"], timeout=60)
    keep = [p.strip() for p in names.splitlines()
            if p.strip() and not _excluded(p.strip(), excludes)]
    if not keep:
        return ""
    _rc, out = _run(["git", "-C", wt, "diff", "--cached", "HEAD", "--", *keep], timeout=60)
    return out


def apply_patch_to_repo(repo: str, patch_path: str) -> tuple[bool, str]:
    """Apply a worktree patch to the real repo working tree — autocrlf-safe (the fix from
    §6.2), with escalating leniency. git apply is atomic, so retries are safe."""
    base = ["git", "-c", "core.autocrlf=false", "-C", repo, "apply"]
    last = "git apply failed"
    for extra in ([], ["--3way"], ["--ignore-whitespace"]):
        rc, out = _run(base + extra + [patch_path], timeout=120)
        if rc == 0:
            return True, ""
        last = out.strip()
    return False, last


def run_agent_task(task: str, done_when: str, repo: str, provider: str,
                   files: list[str], max_iters: int, agent_cfg: dict[str, Any],
                   outputs_dir: Path, apply: bool = True, model: str = "") -> dict[str, Any]:
    """The Phase-7 TDD loop. Returns a Claude-cheap summary (NOT the full diff):
    {output_id, done_passed, iterations, files_changed, patch_path, done_log_tail, worker_log_tail}.

    `model` is an optional tier/model hint (e.g. gemini "flash"/"pro"); see _model_for.
    """
    cfg = load_project_cfg(repo, agent_cfg)
    model_id, model_env = _model_for(provider, cfg, model)
    max_iters = int(max_iters or cfg.get("max_iters", 3))
    wt = make_worktree(repo, cfg.get("worktree_root", ""))
    done_passed, iterations, done_log, worker_log = False, 0, "", ""
    try:
        message = task
        done_script = _write_done_script(wt, done_when)
        for i in range(1, max_iters + 1):
            iterations = i
            _wrc, worker_log = _run_aider(wt, model_id, model_env, message, files, cfg)
            drc, done_log = _run(["cmd", "/c", done_script], cwd=wt,
                                 timeout=int(cfg.get("done_timeout_s", 900)))
            if drc == 0:
                done_passed = True
                break
            # Feed the acceptance-check failure back to the worker as the next instruction.
            message = (f"{task}\n\n--- The acceptance check `{done_when}` is still FAILING. "
                       f"Fix the code so it passes. Verbatim output: ---\n{done_log[-3000:]}")
        diff = _worktree_diff(wt, cfg.get("diff_excludes", _DEFAULT_DIFF_EXCLUDES))
    finally:
        remove_worktree(repo, wt)

    output_id = uuid.uuid4().hex[:12]
    outputs_dir = Path(outputs_dir).resolve()  # absolute so `git apply` works from any cwd
    outputs_dir.mkdir(parents=True, exist_ok=True)
    patch_path = outputs_dir / f"{output_id}.patch"
    patch_path.write_text(diff, encoding="utf-8")
    files_changed = [ln[len("+++ b/"):] for ln in diff.splitlines()
                     if ln.startswith("+++ b/")]

    # On green, mechanically apply the diff to the real tree (autocrlf-safe) so Claude only
    # has to commit — never rewrite. Skipped if not done, empty, or apply=False.
    applied, apply_error = False, ""
    if apply and done_passed and diff.strip():
        applied, apply_error = apply_patch_to_repo(repo, str(patch_path))

    return {
        "output_id": output_id,
        "done_passed": done_passed,
        "iterations": iterations,
        "files_changed": files_changed,
        "applied": applied,
        "apply_error": apply_error,
        "patch_path": str(patch_path),
        "done_log_tail": "\n".join(done_log.splitlines()[-15:]),
        "worker_log_tail": "\n".join(worker_log.splitlines()[-8:]),
        "model": model_id,
    }
