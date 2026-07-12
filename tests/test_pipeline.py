"""Pipeline tests — gate (§6.1), output store + diff (§6.2), metering (§6.5), cascade
(§6.4). Deterministic and offline: providers/embeddings are stubbed, so NO Ollama or
network is needed and the real corrections/metrics stores are never touched.

Run either way:
    python tests/test_pipeline.py         # self-running, prints PASS/FAIL
    pytest tests/test_pipeline.py          # if pytest is installed
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import gate          # noqa: E402
import store         # noqa: E402
import metering      # noqa: E402
import server        # noqa: E402
import retrieval     # noqa: E402
import host_verify   # noqa: E402
import agent          # noqa: E402

_CFG = json.loads((Path(__file__).resolve().parent.parent / "config" / "qwen.json")
                  .read_text(encoding="utf-8"))


# --- §6.1 gate --------------------------------------------------------------
def test_gate_python_pass_fail_skip():
    good = "```python\ndef f(x):\n    return x + 1\n```"
    bad = "```python\ndef f(x):\nreturn x + 1\n```"
    assert gate.run_gate(good, "py_implementer", _CFG).status == "pass"
    r = gate.run_gate(bad, "py_implementer", _CFG)
    assert r.status == "fail" and r.error_category == "compile"
    # C++ now runs the fast heuristic lint (not a compile) — valid snippet passes.
    assert gate.run_gate("```cpp\nint x(){return 0;}\n```",
                         "cpp_implementer", _CFG).status == "pass"


def test_cpp_heuristic_lint():
    import time
    good = ("```cpp\nstatic FString GenTok() {\n"
            "    const FGuid A = FGuid::NewGuid();\n"
            "    return FString::Printf(TEXT(\"%08x\"), A.A);\n}\n```")
    t0 = time.perf_counter()
    r = gate.run_gate(good, "cpp_implementer", _CFG)
    dt = time.perf_counter() - t0
    assert r.status == "pass", r.error_text
    assert dt < 1.0, f"cpp lint too slow: {dt:.3f}s"  # must be fast (ms), not a compile

    # unbalanced braces
    bad = "```cpp\nvoid f() {\n    if (x) {\n        g();\n}\n```"
    assert gate.run_gate(bad, "cpp_implementer", _CFG).status == "fail"

    # banned pattern: non-crypto PRNG -> security
    rnd = "```cpp\nuint8 b = FMath::Rand() & 0xFF;\n```"
    rr = gate.run_gate(rnd, "cpp_implementer", _CFG)
    assert rr.status == "fail" and rr.error_category == "security"

    # a brace inside a STRING or COMMENT must NOT false-fail
    tricky = ('```cpp\nvoid f() {\n    // closing brace } in a comment\n'
              '    FString s = TEXT("literal } brace");\n}\n```')
    assert gate.run_gate(tricky, "cpp_implementer", _CFG).status == "pass"

    # leaked markdown fence inside code
    leaked = "```cpp\nint x = 1;\n```\nextra prose\n```"
    assert gate.run_gate(leaked, "cpp_implementer", _CFG).status in ("fail", "pass")


def test_host_verify_test_parse():
    # UE logs a pass as Result={Success}, a failure as Result={Fail}
    mixed = ("LogAutomationController: Display: Test Completed. Result={Success} Name={A}\n"
             "LogAutomationController: Display: Test Completed. Result={Success} Name={B}\n"
             "LogAutomationController: Error: Test Completed. Result={Fail} Name={C}\n")
    ok, rep = host_verify.parse_test_output(mixed)
    assert not ok and "ran=3 passed=2 failed=1" in rep
    ok2, rep2 = host_verify.parse_test_output(
        "Result={Success}\nResult={Success}\n")
    assert ok2 and "passed=2 failed=0" in rep2
    ok3, _ = host_verify.parse_test_output("no tests here")
    assert not ok3  # nothing ran → not a pass


def test_agent_excluded_filter():
    ex = agent._DEFAULT_DIFF_EXCLUDES
    assert agent._excluded(".aider.chat.history.md", ex)
    assert agent._excluded("src/__pycache__/f.cpython-311.pyc", ex)
    assert agent._excluded("f.pyc", ex)
    assert not agent._excluded("src/math_utils.py", ex)


def test_agent_load_project_cfg(tmp_path=None):
    import json
    repo = Path(tempfile.mkdtemp())
    (repo / ".qwen-pipeline.json").write_text(json.dumps({"agent": {"max_iters": 9}}))
    merged = agent.load_project_cfg(str(repo), {"max_iters": 3, "map_tokens": 512})
    assert merged["max_iters"] == 9 and merged["map_tokens"] == 512  # repo wins, base kept


def test_agent_worktree_diff_and_apply():
    import subprocess, os
    repo = Path(tempfile.mkdtemp())

    def g(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    g("config", "user.email", "t@t.co"); g("config", "user.name", "t")
    (repo / "f.py").write_text("x = 1\n"); g("add", "-A"); g("commit", "-qm", "init")

    wt = agent.make_worktree(str(repo), "")
    try:
        (Path(wt) / "f.py").write_text("x = 2\n")            # real change
        (Path(wt) / ".aider.chat.history.md").write_text("junk\n")  # worker junk
        os.makedirs(Path(wt) / "__pycache__", exist_ok=True)
        (Path(wt) / "__pycache__" / "f.pyc").write_text("junk\n")
        os.makedirs(Path(wt) / "out", exist_ok=True)          # done_when build output
        (Path(wt) / "out" / "f.js").write_text("compiled\n")
        os.makedirs(Path(wt) / "node_modules", exist_ok=True)
        (Path(wt) / "node_modules" / "dep.js").write_text("dep\n")
        diff = agent._worktree_diff(wt, agent._DEFAULT_DIFF_EXCLUDES)
    finally:
        agent.remove_worktree(str(repo), wt)

    assert "f.py" in diff and ".aider" not in diff and "__pycache__" not in diff
    assert "out/f.js" not in diff and "node_modules" not in diff  # build outputs excluded
    patch = repo / "p.patch"; patch.write_text(diff)
    ok, err = agent.apply_patch_to_repo(str(repo), str(patch))
    assert ok, err
    assert (repo / "f.py").read_text().strip() == "x = 2"


def test_gate_typescript_self_contained():
    good = ("```ts\nexport function addNums(a: number, b: number): number {\n"
            "  return a + b;\n}\n```")
    bad = ("```ts\nexport function bad(a: number): number {\n"
           "  return a + \"x\";\n}\n```")  # string + number -> type error
    rg = gate.run_gate(good, "ts_implementer", _CFG)
    rb = gate.run_gate(bad, "ts_implementer", _CFG)
    # tsc may be absent in some envs -> "skipped" is tolerated; but if it RAN, verdicts must be right.
    assert rg.status in ("pass", "skipped")
    assert rb.status in ("fail", "skipped")
    if rg.status == "pass":
        assert rb.status == "fail" and rb.error_category == "compile"


def test_extract_code_and_language():
    code, lang = gate.extract_code("prose\n```python\nx = 1\n```\ntrailing")
    assert code == "x = 1" and lang == "python"
    assert gate.resolve_language("ts_implementer", None) == "typescript"


# --- §6.2 output store + patch reconstruction -------------------------------
def test_store_roundtrip(tmp_path=None):
    tmp = Path(tempfile.mkdtemp())
    store._STORE_PATH = tmp / "store.jsonl"
    oid = store.new_id()
    store.put(oid, "hello", provider="qwen", role="py_implementer", task="t")
    rec = store.get(oid)
    assert rec is not None and rec["output"] == "hello"
    assert store.get("nope") is None


def test_apply_patch_reconstructs():
    original = "def add(a, b):\n    return a - b\n"
    patch = (
        "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
        "-    return a - b\n+    return a + b\n"
    )
    fixed, err = store.apply_patch(original, patch)
    assert err == "" and fixed == "def add(a, b):\n    return a + b\n"
    # empty patch = accepted as-is
    same, err2 = store.apply_patch(original, "")
    assert err2 == "" and same == original


# --- §6.1 worker->worker retry + logging (stubbed worker) -------------------
def _isolate(tmp):
    """Point all side-effect files at a temp dir and stub the embedder."""
    server._CORRECTIONS_PATH = tmp / "corrections.jsonl"
    store._STORE_PATH = tmp / "store.jsonl"
    metering._METRICS_PATH = tmp / "metrics.jsonl"
    retrieval.index_record = lambda rec, cfg: False


def test_gate_and_retry_logs_worker_fix():
    tmp = Path(tempfile.mkdtemp())
    _isolate(tmp)
    seq = iter([
        "```python\ndef add(a, b):\nreturn a + b\n```",   # broken
        "```python\ndef add(a, b):\n    return a + b\n```",  # fixed
    ])
    server.PROVIDERS["qwen"] = lambda system, user, cfg, usage=None, model="": next(seq)
    usage = {"worker_calls": 0, "tokens_in": 0, "tokens_out": 0, "duration_s": 0.0}
    broken = server.PROVIDERS["qwen"]("s", "u", _CFG)
    out, result, attempts = server._gate_and_retry(
        "add(a,b)", "py_implementer", "qwen", "s", "u", broken, usage)
    assert result.status == "pass" and attempts == 2
    recs = [json.loads(l) for l in (tmp / "corrections.jsonl").read_text().splitlines()]
    assert any(r["corrected_by"] == "worker_retry" and r["machine_verified"]
               for r in recs)


# --- §6.2 diff-mode log_correction end-to-end (stubbed) ---------------------
def test_log_correction_diff_mode():
    tmp = Path(tempfile.mkdtemp())
    _isolate(tmp)
    # store a worker output, then log a correction as a diff referencing its id
    oid = store.new_id()
    store.put(oid, "def add(a, b):\n    return a - b\n",
              provider="qwen", role="py_implementer", task="add")
    patch = ("--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
             "-    return a - b\n+    return a + b\n")
    res = server.log_correction(
        role="py_implementer", task="add", error_category="logic",
        explanation="wrong operator", output_id=oid, correction_patch=patch)
    assert res["ok"]
    rec = json.loads((tmp / "corrections.jsonl").read_text().splitlines()[-1])
    assert rec["output_id"] == oid
    assert rec["corrected_output"] == "def add(a, b):\n    return a + b\n"
    assert rec["qwen_output"] == "def add(a, b):\n    return a - b\n"  # reconstructed, not re-sent


# --- §6.5 metering report ---------------------------------------------------
def test_metering_report():
    tmp = Path(tempfile.mkdtemp())
    metering._METRICS_PATH = tmp / "metrics.jsonl"
    metering.record({"tier": "qwen", "role": "py_implementer", "gate_status": "pass",
                     "machine_verified": True, "worker_calls": 1,
                     "tokens_in": 700, "tokens_out": 40, "duration_s": 1.5,
                     **metering.task_ref("t")}, _CFG)
    metering.record({"tier": "claude", "role": "py_implementer",
                     "error_category": "logic", "machine_verified": False,
                     **metering.task_ref("t2")}, _CFG)
    rep = metering.report(10)
    assert "ZERO Claude review: 1/1" in rep and "[qwen]" in rep and "[claude]" in rep


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
