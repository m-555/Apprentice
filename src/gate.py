"""§6.1 mechanical gate — compile/lint/test a worker's output with NO LLM.

Turns a worker's generated code into a pass/fail signal plus the *raw* checker
error text, so a failure can be bounced straight back to the worker (worker->worker
retry) without spending any Claude tokens. Only fast, local checks belong here.

Per IMPLEMENTATION_PLAN_agent §6.7:
  - Python is the reliable workhorse (`py_compile` is a real, isolated syntax gate).
  - TypeScript in isolation is unreliable (snippets miss imports/ambient types) —
    config-gated, OFF by default.
  - C++ (UE) is the EXCEPTION: a full engine build is far too slow for a per-task
    gate, so it is deferred to the batched §9 host-harness. OFF by default here.
Everything is driven by config/qwen.json -> "gate" so thresholds/toggles change
without code edits.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# role -> language for the gate. test_writer/refactorer are language-agnostic;
# their language is inferred from the fenced-block tag instead.
_ROLE_LANG = {
    "py_implementer": "python",
    "ts_implementer": "typescript",
    "cpp_implementer": "cpp",
}

# fenced-block tag -> language
_FENCE_LANG = {
    "py": "python", "python": "python",
    "ts": "typescript", "typescript": "typescript", "tsx": "typescript",
    "js": "typescript", "javascript": "typescript",
    "cpp": "cpp", "c++": "cpp", "cc": "cpp", "cxx": "cpp",
    "h": "cpp", "hpp": "cpp",
}

_LANG_EXT = {"python": ".py", "typescript": ".ts", "cpp": ".cpp"}

# error_category (log schema) inferred from which check failed.
_CHECK_CATEGORY = {"py_compile": "compile", "tsc": "compile", "ruff": "style",
                   "cpp_lint": "compile"}

_FENCE_RE = re.compile(r"```([A-Za-z0-9+#.]*)\r?\n(.*?)```", re.DOTALL)


@dataclass
class GateResult:
    status: str            # "pass" | "fail" | "skipped"
    check: str             # which check ran, or why it was skipped
    language: str | None = None
    error_text: str = ""   # raw checker output (only meaningful on "fail")
    category: str | None = None  # explicit error_category override (else from check)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def error_category(self) -> str:
        return self.category or _CHECK_CATEGORY.get(self.check, "compile")


def extract_code(text: str) -> tuple[str, str | None]:
    """Return (code, fence_lang) from the FIRST fenced block, else (text, None)."""
    m = _FENCE_RE.search(text or "")
    if not m:
        return (text or "").strip(), None
    tag = (m.group(1) or "").strip().lower()
    return m.group(2).strip(), _FENCE_LANG.get(tag)


def resolve_language(role: str, fence_lang: str | None) -> str | None:
    """Role wins (it's the caller's intent); fall back to the fenced tag."""
    return _ROLE_LANG.get(role) or fence_lang


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 60):
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError as exc:
        return None, f"tool not found: {exc}"
    except subprocess.TimeoutExpired:
        return None, "gate check timed out"


def _check_python(code: str, lang_cfg: dict[str, Any]) -> GateResult:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker_unit.py"
        path.write_text(code, encoding="utf-8")
        rc, out = _run([sys.executable, "-m", "py_compile", str(path)],
                       timeout=int(lang_cfg.get("timeout_s", 60)))
        if rc is None:
            return GateResult("skipped", "py_compile", "python", out)
        if rc != 0:
            # py_compile prints the offending temp path — normalize it away.
            return GateResult("fail", "py_compile", "python",
                              out.replace(str(path), "<worker_unit.py>").strip())
        # Optional lint pass (ruff), only if enabled AND installed.
        if "ruff" in lang_cfg.get("checks", []):
            rrc, rout = _run(["ruff", "check", str(path)],
                             timeout=int(lang_cfg.get("timeout_s", 60)))
            if rrc is not None and rrc != 0:
                return GateResult("fail", "ruff", "python",
                                  rout.replace(str(path), "<worker_unit.py>").strip())
        return GateResult("pass", "py_compile", "python")


def _strip_cpp(code: str) -> str:
    """Remove //-, /*…*/-comments and string/char/raw-string literals so a brace/paren
    balance check doesn't false-fail on a `}` that lives inside a string or comment."""
    out, i, n = [], 0, len(code)
    while i < n:
        two = code[i:i + 2]
        if two == "//":
            j = code.find("\n", i)
            i = n if j == -1 else j
        elif two == "/*":
            j = code.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif code[i] == "R" and i + 1 < n and code[i + 1] == '"':  # raw string R"delim(...)delim"
            j = code.find("(", i + 2)
            if j != -1:
                end = ")" + code[i + 2:j] + '"'
                k = code.find(end, j + 1)
                i = n if k == -1 else k + len(end)
            else:
                i += 2
        elif code[i] in "\"'":
            quote, i = code[i], i + 1
            while i < n:
                if code[i] == "\\":
                    i += 2; continue
                if code[i] == quote:
                    i += 1; break
                i += 1
        else:
            out.append(code[i]); i += 1
    return "".join(out)


def _check_cpp_heuristic(code: str, lang_cfg: dict[str, Any]) -> GateResult:
    """FAST (ms), no-compile structural + banned-pattern lint for UE C++ snippets.

    High-confidence checks only (a false 'fail' wastes worker retries — §6.7):
      1. balanced () [] {} (comment/string-aware),
      2. no leaked markdown fence inside the code,
      3. configurable banned/known-bad patterns (e.g. FMath::Rand for crypto, placeholders).
    It does NOT type-check or resolve UE headers — that's deferred to the batched §9
    host-harness build. Purpose: catch gross Qwen mistakes for free before Claude review.
    """
    problems: list[str] = []
    category = "compile"

    if "```" in code:
        problems.append("leaked markdown fence (```) inside the code block")

    stripped = _strip_cpp(code)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    balanced = True
    for ch in stripped:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                problems.append(f"unbalanced '{ch}' (no matching opener)")
                balanced = False
                break
            stack.pop()
    if balanced and stack:
        problems.append(f"unclosed '{stack[-1]}' — {len(stack)} opener(s) never closed")

    for spec in lang_cfg.get("banned_patterns", []):
        pat = spec.get("pattern", "")
        if not pat:
            continue
        try:
            if re.search(pat, stripped):
                problems.append(spec.get("message", f"banned pattern: {pat}"))
                category = spec.get("category", category)
        except re.error:
            continue

    if problems:
        return GateResult("fail", "cpp_lint", "cpp",
                          "cpp heuristic lint found:\n- " + "\n- ".join(problems),
                          category=category)
    return GateResult("pass", "cpp_lint", "cpp")


def _check_typescript(code: str, lang_cfg: dict[str, Any]) -> GateResult:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker_unit.ts"
        path.write_text(code, encoding="utf-8")
        cmd = list(lang_cfg.get("tsc_cmd", ["npx", "tsc", "--noEmit"]))
        rc, out = _run([*cmd, str(path)], timeout=int(lang_cfg.get("timeout_s", 180)))
        if rc is None:
            return GateResult("skipped", "tsc", "typescript", out)
        if rc != 0:
            return GateResult("fail", "tsc", "typescript",
                              out.replace(str(path), "<worker_unit.ts>").strip())
        return GateResult("pass", "tsc", "typescript")


def run_gate(text: str, role: str, cfg: dict[str, Any]) -> GateResult:
    """Run the mechanical gate on a worker's raw output.

    Returns a GateResult. "skipped" means no reliable check applied (unknown
    language, gate/language disabled, or a missing tool) — the caller should treat
    skipped like the pre-gate behavior (hand to Claude for normal review).
    """
    gate_cfg = cfg.get("gate", {})
    if not gate_cfg.get("enabled", False):
        return GateResult("skipped", "gate-disabled")

    code, fence_lang = extract_code(text)
    lang = resolve_language(role, fence_lang)
    if not lang:
        return GateResult("skipped", "unknown-language")
    if not code.strip():
        return GateResult("skipped", "empty-output", lang)

    lang_cfg = gate_cfg.get("languages", {}).get(lang, {})
    if not lang_cfg.get("enabled", False):
        return GateResult("skipped", f"{lang}-gate-disabled", lang)

    if lang == "python":
        return _check_python(code, lang_cfg)
    if lang == "typescript":
        return _check_typescript(code, lang_cfg)
    if lang == "cpp":
        # FAST heuristic lint only — a real UE compile is deferred to the batched §9
        # host-harness (a full build can't check an isolated fragment and is too slow).
        return _check_cpp_heuristic(code, lang_cfg)
    return GateResult("skipped", f"{lang}-deferred-to-host-harness", lang)


def build_retry_prompt(original_user: str, prior_output: str, error_text: str,
                       max_err_chars: int = 4000) -> str:
    """Compose the worker->worker fix prompt: original task + prior attempt + the
    VERBATIM checker error (§6.7: don't summarize errors — raw text is the best signal)."""
    err = (error_text or "").strip()
    if len(err) > max_err_chars:
        err = err[:max_err_chars] + "\n… (truncated)"
    return (
        f"{original_user}\n\n"
        "--- YOUR PREVIOUS ATTEMPT FAILED A MECHANICAL CHECK ---\n"
        "Below is your previous output and the exact checker error. Fix ONLY what the "
        "error requires and return the corrected, COMPLETE code in a single fenced "
        "block. Do not explain.\n\n"
        f"--- PREVIOUS OUTPUT ---\n{prior_output}\n\n"
        f"--- CHECKER ERROR (verbatim) ---\n{err}\n"
    )
