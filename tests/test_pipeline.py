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
import deliver        # noqa: E402
import providers      # noqa: E402
import paths          # noqa: E402
import cli            # noqa: E402
import chat_providers  # noqa: E402
import tools as tools_mod  # noqa: E402
import verify as verify_mod  # noqa: E402
import session as session_mod  # noqa: E402
import loop           # noqa: E402

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


# --- §6.4 cascade: escalation carries the failure history -------------------
def test_cascade_escalation_carries_failure():
    import copy
    tmp = Path(tempfile.mkdtemp())
    _isolate(tmp)
    old_cfg, old_gem = server._CFG, server.PROVIDERS["gemini"]
    cfg = copy.deepcopy(_CFG)
    cfg["gate"]["max_retries"] = 0                    # fail fast on the first tier
    cfg["providers"]["gemini"]["enabled"] = True
    cfg["cascade"]["escalate_to"] = "gemini"
    server._CFG = cfg
    seen: dict = {}
    server.PROVIDERS["qwen"] = (
        lambda s, u, c, usage=None, model="": "```python\ndef f(:\n```")  # broken
    def _gem(s, u, c, usage=None, model=""):
        seen["user"] = u
        return "```python\ndef f():\n    return 1\n```"
    server.PROVIDERS["gemini"] = _gem
    try:
        out, result, attempts, tier = server._delegate_cascade(
            "t", "py_implementer", "qwen", "sys", "the task")
        assert tier == "gemini" and result.status == "pass"
        # The escalated tier must see the failed attempt + verbatim checker error,
        # not just the raw task.
        assert "PREVIOUS OUTPUT" in seen["user"] and "CHECKER ERROR" in seen["user"]
        assert "the task" in seen["user"]
    finally:
        server._CFG = old_cfg
        server.PROVIDERS["gemini"] = old_gem


# --- budgets: enforced, not advisory -----------------------------------------
def test_budget_enforcement():
    import copy
    tmp = Path(tempfile.mkdtemp())
    metering._METRICS_PATH = tmp / "metrics.jsonl"
    old_cfg = server._CFG
    cfg = copy.deepcopy(_CFG)
    cfg["metering"]["budgets"]["gemini_tokens_per_day"] = 100
    server._CFG = cfg
    try:
        assert server._budget_exceeded("gemini") == ""      # nothing spent yet
        metering.record({"tier": "gemini", "tokens_out": 150}, cfg)
        assert "budget" in server._budget_exceeded("gemini")  # over cap → refusal msg
        assert server._budget_exceeded("qwen") == ""          # no cap configured
    finally:
        server._CFG = old_cfg


# --- agent: done_when runs via a script (Windows quoting survives) ----------
def test_done_script_wrapper():
    wt = tempfile.mkdtemp()
    script = agent._write_done_script(wt, "echo ok")
    assert script.endswith(".qwen_done.cmd")
    assert agent._excluded(".qwen_done.cmd", agent._DEFAULT_DIFF_EXCLUDES)
    rc, out = agent._run(["cmd", "/c", script], cwd=wt, timeout=30)
    assert rc == 0 and "ok" in out
    # a quoted multi-word arg must survive intact inside the script file
    script2 = agent._write_done_script(wt, 'findstr /C:"two words" missing.txt')
    assert '/C:"two words"' in Path(script2).read_text(encoding="utf-8")


# --- wave 2: config-driven provider registry ---------------------------------
def test_provider_registry_config_driven():
    cfg = {"providers": {
        "groq": {"kind": "openai-compatible", "base_url": "https://api.groq.com/openai/v1"},
        "mystery": {"kind": "quantum"},
    }}
    assert providers.resolve("qwen", cfg) is providers.PROVIDERS["qwen"]  # built-in wins
    assert providers.resolve("groq", cfg) is not None      # config-defined, known kind
    assert providers.resolve("mystery", cfg) is None       # unknown kind
    assert providers.resolve("nope", cfg) is None
    names = providers.provider_names(cfg)
    assert "groq" in names and "qwen" in names and "mystery" not in names


def test_resolve_model_tiers():
    p = {"models": {"flash": "m-flash", "pro": "m-pro"}, "default_model": "flash"}
    assert providers._resolve_model(p, "pro") == "m-pro"        # tier alias
    assert providers._resolve_model(p, "raw-id") == "raw-id"    # passthrough
    assert providers._resolve_model(p, "") == "m-flash"         # default tier
    assert providers._resolve_model({"model": "single"}, "") == "single"
    assert providers._resolve_model({}, "", "fallback") == "fallback"


# --- wave 2: cost metering + usd budgets --------------------------------------
def test_cost_estimation_and_usd_budget():
    import copy
    cfg = {"providers": {"gemini": {
        "default_model": "flash",
        "cost": {"flash": {"usd_per_mtok_in": 1.0, "usd_per_mtok_out": 4.0},
                 "pro": {"usd_per_mtok_in": 10.0, "usd_per_mtok_out": 40.0}},
    }, "flat": {"cost": {"usd_per_mtok_in": 2.0, "usd_per_mtok_out": 2.0}}}}
    # per-tier pricing; "" model falls back to default_model
    assert abs(metering.est_cost_usd(cfg, "gemini", "pro", 1_000_000, 0) - 10.0) < 1e-9
    assert abs(metering.est_cost_usd(cfg, "gemini", "", 0, 1_000_000) - 4.0) < 1e-9
    assert abs(metering.est_cost_usd(cfg, "flat", "", 500_000, 500_000) - 2.0) < 1e-9
    assert metering.est_cost_usd(cfg, "qwen", "", 9e6, 9e6) == 0.0  # unpriced = free

    # usd budget enforcement end-to-end through metering.record
    tmp = Path(tempfile.mkdtemp())
    metering._METRICS_PATH = tmp / "metrics.jsonl"
    old = server._CFG
    scfg = copy.deepcopy(_CFG)
    scfg["providers"]["gemini"]["cost"] = {
        "flash": {"usd_per_mtok_in": 1.0, "usd_per_mtok_out": 4.0}}
    scfg["providers"]["gemini"]["default_model"] = "flash"
    scfg["metering"]["budgets"]["gemini_usd_per_day"] = 0.5
    scfg["metering"]["budgets"]["gemini_tokens_per_day"] = 0
    server._CFG = scfg
    try:
        metering.record({"tier": "gemini", "model": "flash",
                         "tokens_in": 100_000, "tokens_out": 200_000}, scfg)
        # $0.1 in + $0.8 out = $0.90 spent >= the $0.50 daily cap → must refuse
        assert "USD budget" in server._budget_exceeded("gemini")
    finally:
        server._CFG = old


# --- wave 2: server-side context fetch + apply/test ---------------------------
def test_deliver_path_guard_and_context():
    repo = Path(tempfile.mkdtemp())
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    # traversal must be refused
    try:
        deliver.resolve_repo_path(str(repo), "../evil.txt")
        assert False, "traversal not refused"
    except ValueError:
        pass
    ctx = deliver.read_context(str(repo), ["src/a.py:2-4"])
    assert "L2" in ctx and "L4" in ctx and "L1" not in ctx and "L5" not in ctx
    assert "lines 2-4" in ctx
    # missing file is a clear error
    try:
        deliver.read_context(str(repo), ["src/missing.py"])
        assert False
    except ValueError:
        pass


def test_deliver_apply_modes_and_revert():
    repo = Path(tempfile.mkdtemp())
    f = repo / "m.py"
    f.write_text("def a():\n    return 1\n", encoding="utf-8")
    orig, path = deliver.apply_code(str(repo), "m.py", "def b():\n    return 2", "append")
    text = f.read_text(encoding="utf-8")
    assert "def a()" in text and "def b()" in text
    deliver.revert_apply(path, orig)
    assert f.read_text(encoding="utf-8") == "def a():\n    return 1\n"
    # create: new file, revert deletes it
    orig2, p2 = deliver.apply_code(str(repo), "new.py", "x = 1", "create")
    assert orig2 is None and p2.exists()
    deliver.revert_apply(p2, orig2)
    assert not p2.exists()
    # create on existing file must fail
    try:
        deliver.apply_code(str(repo), "m.py", "x", "create")
        assert False
    except ValueError:
        pass


def test_delegate_apply_and_test_loop():
    """Full offline TDD loop: worker output fails the project test, gets reverted,
    worker fixes from verbatim test output, re-applied, tests pass."""
    import copy
    tmp = Path(tempfile.mkdtemp())
    _isolate(tmp)
    repo = Path(tempfile.mkdtemp())
    check = ("import pathlib, sys; "
             "t = pathlib.Path('u.py').read_text(); "
             "sys.exit(0 if 'return a + b' in t else 1)")
    test_cmd = f'"{sys.executable}" -c "{check}"'

    old_cfg = server._CFG
    server._CFG = copy.deepcopy(_CFG)
    seq = iter(["```python\ndef add(a, b):\n    return a + b\n```"])  # the fix
    server.PROVIDERS["qwen"] = lambda s, u, c, usage=None, model="": next(seq)
    try:
        out, info = server._apply_and_test(
            task="add", role="py_implementer", tier="qwen", system="s",
            user="u", output="```python\ndef add(a, b):\n    return a - b\n```",
            repo=str(repo), apply_to="u.py", apply_mode="create", test_cmd=test_cmd)
        assert info["test_status"] == "pass", info
        assert info["applied"] and info["attempts"] == 2
        assert "return a + b" in (repo / "u.py").read_text(encoding="utf-8")
        # the worker-retry fix was logged as a machine-verified correction
        recs = [json.loads(l) for l
                in (tmp / "corrections.jsonl").read_text().splitlines()]
        assert any(r["corrected_by"] == "worker_retry"
                   and "acceptance command" in r["explanation"] for r in recs)
    finally:
        server._CFG = old_cfg


def test_delegate_test_fail_reverts():
    """If the worker never satisfies test_cmd, the file must be reverted (tree clean)."""
    import copy
    tmp = Path(tempfile.mkdtemp())
    _isolate(tmp)
    repo = Path(tempfile.mkdtemp())
    test_cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'  # always red
    old_cfg = server._CFG
    server._CFG = copy.deepcopy(_CFG)
    server._CFG["gate"]["max_retries"] = 1
    server.PROVIDERS["qwen"] = (
        lambda s, u, c, usage=None, model="": "```python\nx = 2\n```")
    try:
        out, info = server._apply_and_test(
            task="t", role="py_implementer", tier="qwen", system="s", user="u",
            output="```python\nx = 1\n```", repo=str(repo), apply_to="v.py",
            apply_mode="create", test_cmd=test_cmd)
        assert info["test_status"] == "fail"
        assert not info["applied"]
        assert not (repo / "v.py").exists()  # reverted (created file deleted)
    finally:
        server._CFG = old_cfg


def test_apply_test_escalates_to_stronger_tier():
    """qwen never satisfies test_cmd → the loop escalates to gemini carrying the
    failing code + verbatim test output; gemini's fix passes and is left applied."""
    import copy
    tmp = Path(tempfile.mkdtemp())
    _isolate(tmp)
    repo = Path(tempfile.mkdtemp())
    check = ("import pathlib, sys; "
             "t = pathlib.Path('w.py').read_text(); "
             "sys.exit(0 if 'return a + b' in t else 1)")
    test_cmd = f'"{sys.executable}" -c "{check}"'

    old_cfg, old_gem = server._CFG, server.PROVIDERS["gemini"]
    cfg = copy.deepcopy(_CFG)
    cfg["gate"]["max_retries"] = 1
    cfg["providers"]["gemini"]["enabled"] = True
    cfg["cascade"]["escalate_to"] = "gemini"
    server._CFG = cfg
    server.PROVIDERS["qwen"] = (  # qwen keeps producing the wrong operator
        lambda s, u, c, usage=None, model="": "```python\ndef add(a, b):\n    return a - b\n```")
    seen: dict = {}
    def _gem(s, u, c, usage=None, model=""):
        seen["user"] = u
        return "```python\ndef add(a, b):\n    return a + b\n```"
    server.PROVIDERS["gemini"] = _gem
    try:
        out, info = server._apply_and_test(
            task="add", role="py_implementer", tier="qwen", system="s", user="u",
            output="```python\ndef add(a, b):\n    return a - b\n```",
            repo=str(repo), apply_to="w.py", apply_mode="create", test_cmd=test_cmd)
        assert info["test_status"] == "pass" and info["tier"] == "gemini", info
        assert "return a + b" in (repo / "w.py").read_text(encoding="utf-8")
        # gemini got the failing attempt + the verbatim test failure, not a cold task
        assert "PREVIOUS OUTPUT" in seen["user"] and "FAILED" in seen["user"]
        recs = [json.loads(l) for l
                in (tmp / "corrections.jsonl").read_text().splitlines()]
        assert any(r["provider"] == "gemini" and r["corrected_by"] == "worker_retry"
                   for r in recs)
    finally:
        server._CFG = old_cfg
        server.PROVIDERS["gemini"] = old_gem


def test_apply_test_no_escalation_when_disabled():
    """With the escalation tier disabled (the default), a persistent test failure must
    NOT call it — fail cleanly, file reverted."""
    import copy
    tmp = Path(tempfile.mkdtemp())
    _isolate(tmp)
    repo = Path(tempfile.mkdtemp())
    test_cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'  # always red
    old_cfg, old_gem = server._CFG, server.PROVIDERS["gemini"]
    cfg = copy.deepcopy(_CFG)
    cfg["gate"]["max_retries"] = 1
    cfg["providers"]["gemini"]["enabled"] = False   # explicit: not available
    server._CFG = cfg
    server.PROVIDERS["qwen"] = (
        lambda s, u, c, usage=None, model="": "```python\nx = 1\n```")
    called = {"gemini": False}
    def _gem(s, u, c, usage=None, model=""):
        called["gemini"] = True
        return "```python\nx = 2\n```"
    server.PROVIDERS["gemini"] = _gem
    try:
        out, info = server._apply_and_test(
            task="t", role="py_implementer", tier="qwen", system="s", user="u",
            output="```python\nx = 0\n```", repo=str(repo), apply_to="z.py",
            apply_mode="create", test_cmd=test_cmd)
        assert info["test_status"] == "fail" and not called["gemini"]
        assert not (repo / "z.py").exists()  # reverted
    finally:
        server._CFG = old_cfg
        server.PROVIDERS["gemini"] = old_gem


# --- packaging: data-home resolution + CLI ----------------------------------
def test_paths_checkout_mode():
    # Running from the repo checkout → data home is the repo root, config exists.
    repo_root = Path(__file__).resolve().parent.parent
    assert paths.ROOT == repo_root
    assert paths.CONFIG_PATH == repo_root / "config" / "qwen.json"
    assert paths.CONFIG_PATH.exists()
    cfg = paths.load_config()
    assert cfg.get("providers", {}).get("default") == "qwen"


def test_cli_init_seeds_home_and_report_runs():
    home = Path(tempfile.mkdtemp())
    rc = cli.cmd_init(home=home, check_ollama=False)   # offline
    assert rc == 0
    for sub in ("config", "corrections", "outputs", "metrics"):
        assert (home / sub).is_dir()
    assert (home / "config" / "qwen.local.json").exists()  # starter overlay created
    assert cli.cmd_report(5) == 0
    assert cli.main(["help"]) == 0
    assert cli.main(["bogus-command"]) == 2


def test_repo_conventions_loaded():
    repo = Path(tempfile.mkdtemp())
    (repo / ".qwen-pipeline.json").write_text(
        json.dumps({"conventions": "Use snake_case.", "agent": {"max_iters": 2}}),
        encoding="utf-8")
    opts = deliver.load_repo_options(str(repo))
    assert opts.get("conventions") == "Use snake_case."
    assert deliver.load_repo_options(str(Path(tempfile.mkdtemp()))) == {}


# --- §6.5 metering report ---------------------------------------------------
def test_metering_stepin_vs_acceptance():
    tmp = Path(tempfile.mkdtemp())
    metering._METRICS_PATH = tmp / "metrics.jsonl"
    # an acceptance (log-ALWAYS discipline) and a real correction
    metering.record({"tier": "claude", "stepped_in": False,
                     "error_category": "none"}, _CFG)
    metering.record({"tier": "claude", "stepped_in": True,
                     "error_category": "logic"}, _CFG)
    rep = metering.report(10)
    assert "step in: 1 of 2" in rep  # acceptance is a review, NOT a step-in


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


# =============================================================================
# The standalone agent (chat_providers / tools / verify / session / loop)
# =============================================================================

def _agent_repo() -> Path:
    """A tiny scratch repo: calc.py + a test that only passes once mul() is right."""
    repo = Path(tempfile.mkdtemp())
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "check.py").write_text(
        "from calc import add, mul\n"
        "assert add(1, 2) == 3\n"
        "assert mul(3, 4) == 12\n"
        "print('ok')\n", encoding="utf-8")
    return repo


def _check_cmd() -> str:
    """The scratch repo's acceptance command. `-B` is required: the wrong and right
    calc.py are the SAME byte size, so a cached .pyc written within the same filesystem
    timestamp tick would be reused and the second run would import stale code."""
    return f'"{sys.executable}" -B check.py'


# --- chat_providers: normalization + text protocol ---------------------------
def test_chat_tool_call_normalization():
    # Ollama gives dict args; OpenAI gives a JSON string — both must normalize.
    assert chat_providers._coerce_args({"path": "a.py"}) == {"path": "a.py"}
    assert chat_providers._coerce_args('{"path": "a.py"}') == {"path": "a.py"}
    assert chat_providers._coerce_args("not json") == {"__raw__": "not json"}
    assert chat_providers._coerce_args(None) == {}


def test_text_protocol_parsing():
    turn = chat_providers.parse_text_action(
        'I will read it.\n```action\n{"tool": "read_file", "args": {"path": "x.py"}}\n```')
    assert turn.wants_tools and turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].args == {"path": "x.py"}
    assert "I will read it." in turn.content
    plain = chat_providers.parse_text_action("All done — no more actions needed.")
    assert not plain.wants_tools and "All done" in plain.content


def test_provider_kind_and_chat_support():
    cfg = {"providers": {"groq": {"kind": "openai-compatible"},
                         "weird": {"kind": "telepathy"}}}
    assert chat_providers.supports_chat(cfg, "qwen")     # built-in -> ollama-local
    assert chat_providers.supports_chat(cfg, "groq")
    assert not chat_providers.supports_chat(cfg, "weird")


# --- tools: safety + behavior ------------------------------------------------
def test_tools_path_guard_and_edit():
    repo = _agent_repo()
    ctx = tools_mod.ToolContext(repo=str(repo), cfg=_CFG)
    reg = tools_mod.build_tools(ctx)

    # traversal is refused as a normal tool result (not a crash)
    out = tools_mod.dispatch(reg, "read_file", {"path": "../../etc/passwd"})
    assert out.startswith("ERROR") and "escape" in out

    assert "def add" in tools_mod.dispatch(reg, "read_file", {"path": "calc.py"})
    assert "calc.py" in tools_mod.dispatch(reg, "list_files", {})
    assert "calc.py:1" in tools_mod.dispatch(reg, "search", {"pattern": "def add"})

    # exact-match edit: wrong text fails loudly instead of guessing
    bad = tools_mod.dispatch(reg, "edit_file",
                             {"path": "calc.py", "old": "def nope():", "new": "x"})
    assert bad.startswith("ERROR") and "not found" in bad
    ok = tools_mod.dispatch(reg, "edit_file",
                            {"path": "calc.py", "old": "a + b", "new": "a + b  # ok"})
    assert "Edited" in ok and "# ok" in (repo / "calc.py").read_text()

    # unknown tool / bad args are recoverable messages
    assert "unknown tool" in tools_mod.dispatch(reg, "nope", {})
    assert "bad arguments" in tools_mod.dispatch(reg, "read_file", {"bogus": 1})


def test_command_policy():
    cfg = _CFG.get("agent_chat", {})
    allowed, confirm, _ = tools_mod.command_allowed("pytest -q", cfg)
    assert allowed and not confirm                      # allowlisted -> runs silently
    allowed, confirm, _ = tools_mod.command_allowed("echo hello", cfg)
    assert allowed and confirm                          # unknown -> needs confirmation
    allowed, _c, why = tools_mod.command_allowed("rm -rf /", cfg)
    assert not allowed and "denied" in why              # denylisted -> never
    allowed, _c, _w = tools_mod.command_allowed("git push --force", cfg)
    assert not allowed


def test_run_cmd_respects_refusal():
    repo = _agent_repo()
    ctx = tools_mod.ToolContext(repo=str(repo), cfg=_CFG,
                                confirm=lambda name, detail: False)
    reg = tools_mod.build_tools(ctx)
    assert "REFUSED by the user" in tools_mod.dispatch(reg, "run_cmd", {"cmd": "echo hi"})
    assert "REFUSED" in tools_mod.dispatch(reg, "run_cmd", {"cmd": "rm -rf x"})


# --- verify: snapshot / revert / policies ------------------------------------
def test_verify_reverts_broken_edit():
    repo = _agent_repo()
    v = verify_mod.Verifier(str(repo), _CFG, policy="gate")
    reg = verify_mod.wrap_registry(
        tools_mod.build_tools(tools_mod.ToolContext(repo=str(repo), cfg=_CFG)), v)
    before = (repo / "calc.py").read_text(encoding="utf-8")

    tools_mod.dispatch(reg, "write_file",
                       {"path": "calc.py", "content": "def broken(:\n"})  # syntax error
    result = v.finish_turn()
    assert not result.ok and "gate" in result.check
    assert (repo / "calc.py").read_text(encoding="utf-8") == before   # byte-identical
    assert "REVERTED" in result.as_tool_note()


def test_verify_tests_policy_and_undo():
    repo = _agent_repo()
    test_cmd = _check_cmd()
    v = verify_mod.Verifier(str(repo), _CFG, policy="tests", test_cmd=test_cmd)
    reg = verify_mod.wrap_registry(
        tools_mod.build_tools(tools_mod.ToolContext(repo=str(repo), cfg=_CFG)), v)

    # wrong implementation: compiles fine, but the project's own test fails -> revert
    tools_mod.dispatch(reg, "write_file", {
        "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"
                                      "def mul(a, b):\n    return a + b\n"})
    bad = v.finish_turn()
    assert not bad.ok and bad.check == "tests" and "calc.py" in bad.reverted
    assert "def mul" not in (repo / "calc.py").read_text(encoding="utf-8")

    # correct implementation survives, and /undo can still roll it back
    tools_mod.dispatch(reg, "write_file", {
        "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"
                                      "def mul(a, b):\n    return a * b\n"})
    good = v.finish_turn()
    assert good.ok and good.check == "tests"
    assert "def mul" in (repo / "calc.py").read_text(encoding="utf-8")
    assert v.undo_last() == ["calc.py"]
    assert "def mul" not in (repo / "calc.py").read_text(encoding="utf-8")


def test_verify_tests_degrades_without_test_cmd():
    # Claiming "test-verified" with no test command would be a lie — degrade to gate.
    v = verify_mod.Verifier(str(_agent_repo()), _CFG, policy="tests", test_cmd="")
    assert v.policy == "gate"


def test_verify_off_lets_edits_land():
    repo = _agent_repo()
    v = verify_mod.Verifier(str(repo), _CFG, policy="off")
    reg = verify_mod.wrap_registry(
        tools_mod.build_tools(tools_mod.ToolContext(repo=str(repo), cfg=_CFG)), v)
    tools_mod.dispatch(reg, "write_file", {"path": "calc.py", "content": "def broken(:\n"})
    assert v.finish_turn().ok
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def broken(:\n"


# --- session: prompt, compaction, transcripts --------------------------------
def test_session_prompt_and_compaction():
    repo = _agent_repo()
    (repo / ".qwen-pipeline.json").write_text(
        json.dumps({"conventions": "SNAKE_CASE_RULE"}), encoding="utf-8")
    cfg = json.loads(json.dumps(_CFG))
    cfg["agent_chat"]["context_budget_tokens"] = 200      # force compaction
    cfg["agent_chat"]["compact_keep_recent"] = 4
    s = session_mod.Session(str(repo), cfg, "qwen", verify_policy="gate")
    assert "SNAKE_CASE_RULE" in s.messages[0]["content"]   # conventions injected
    assert "calc.py" in s.messages[0]["content"]           # repo map injected

    s.add_user("FIRST TASK")
    for i in range(12):
        s.add_user(f"filler message number {i} " + "x" * 200)
    assert s.maybe_compact()
    assert s.messages[0]["role"] == "system"
    assert s.messages[1]["content"] == "FIRST TASK"        # original intent survives
    assert "summarized to save context" in s.messages[2]["content"]
    assert len(s.messages) < 16
    assert s.messages[-1]["role"] != "tool"                # no orphaned tool result


def test_session_save_and_load(monkeypatch=None):
    home = Path(tempfile.mkdtemp())
    old_root = session_mod.paths.ROOT
    session_mod.paths.ROOT = home
    try:
        s = session_mod.Session(str(_agent_repo()), _CFG, "qwen", verify_policy="off")
        s.add_user("do a thing")
        p = s.save()
        assert p.exists()
        loaded = session_mod.Session.load(s.id, _CFG)
        assert loaded.id == s.id and loaded.messages[-1]["content"] == "do a thing"
        assert any(r["id"] == s.id for r in session_mod.Session.list_recent())
    finally:
        session_mod.paths.ROOT = old_root


# --- loop: the agent end-to-end with a scripted model ------------------------
class _ScriptedModel:
    """Stands in for a provider: replays a list of AssistantTurns."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen_messages = []

    def __call__(self, messages, tools, cfg, provider, model="", usage=None):
        self.seen_messages.append(list(messages))
        if usage is not None:
            usage.update({"tokens_in": 10, "tokens_out": 5, "duration_s": 0.1})
        return self.turns.pop(0) if self.turns else chat_providers.AssistantTurn(
            "out of scripted turns")


def _run_agent(repo, cfg, turns, policy, test_cmd=""):
    s = session_mod.Session(str(repo), cfg, "qwen", verify_policy=policy,
                            test_cmd=test_cmd)
    v = verify_mod.Verifier(str(repo), cfg, policy, test_cmd)
    reg = verify_mod.wrap_registry(
        tools_mod.build_tools(tools_mod.ToolContext(repo=str(repo), cfg=cfg,
                                                    test_cmd=test_cmd)), v)
    model = _ScriptedModel(turns)
    old = loop.chat_providers.chat
    loop.chat_providers.chat = model
    try:
        events = list(loop.run_turn(s, v, reg, "add mul()"))
    finally:
        loop.chat_providers.chat = old
    return s, v, events, model


def _turn(*calls, content=""):
    return chat_providers.AssistantTurn(content, [
        chat_providers.ToolCall(f"c{i}", name, args)
        for i, (name, args) in enumerate(calls)])


def test_loop_happy_path():
    repo = _agent_repo()
    good = ("def add(a, b):\n    return a + b\n"
            "def mul(a, b):\n    return a * b\n")
    s, v, events, _m = _run_agent(repo, _CFG, [
        _turn(("read_file", {"path": "calc.py"})),
        _turn(("write_file", {"path": "calc.py", "content": good})),
        _turn(("finish", {"summary": "added mul"})),
    ], policy="gate")
    kinds = [e.kind for e in events]
    assert "verify_passed" in kinds
    assert "def mul" in (repo / "calc.py").read_text(encoding="utf-8")
    assert s.usage["turns"] == 3 and s.usage["tokens_out"] == 15


def test_loop_reverts_then_model_fixes_it():
    """The core promise: a bad edit that passes compile but fails the project's tests is
    reverted, the model sees the real failure, and its second attempt survives."""
    repo = _agent_repo()
    test_cmd = _check_cmd()
    wrong = ("def add(a, b):\n    return a + b\n"
             "def mul(a, b):\n    return a + b\n")     # compiles, fails the test
    right = ("def add(a, b):\n    return a + b\n"
             "def mul(a, b):\n    return a * b\n")
    s, v, events, model = _run_agent(repo, _CFG, [
        _turn(("write_file", {"path": "calc.py", "content": wrong})),
        _turn(("finish", {"summary": "done (wrong)"})),
        _turn(("write_file", {"path": "calc.py", "content": right})),
        _turn(("finish", {"summary": "fixed"})),
    ], policy="tests", test_cmd=test_cmd)

    kinds = [e.kind for e in events]
    assert "verify_failed" in kinds and kinds[-1] == "verify_passed"
    assert "def mul(a, b):\n    return a * b" in (repo / "calc.py").read_text()
    # the model was actually TOLD what failed, verbatim
    note = [m for m in s.messages if "VERIFICATION FAILED" in (m.get("content") or "")]
    assert note and "AssertionError" in note[0]["content"]


def test_loop_step_cap_stops():
    repo = _agent_repo()
    cfg = json.loads(json.dumps(_CFG))
    cfg["agent_chat"]["max_steps"] = 2
    # a model that never finishes must still terminate
    s, v, events, _m = _run_agent(repo, cfg,
                                  [_turn(("list_files", {})) for _ in range(10)],
                                  policy="off")
    assert events[-1].kind == "stopped" and "Step limit" in events[-1].text


def test_loop_escalates_after_repeated_failures():
    repo = _agent_repo()
    test_cmd = _check_cmd()
    cfg = json.loads(json.dumps(_CFG))
    cfg["providers"]["gemini"]["enabled"] = True     # escalation target available
    cfg["cascade"]["escalate_to"] = "gemini"
    cfg["agent_chat"]["escalate_after_failed_verifies"] = 1
    wrong = "def add(a, b):\n    return a + b\ndef mul(a, b):\n    return a + b\n"
    s, v, events, _m = _run_agent(repo, cfg, [
        _turn(("write_file", {"path": "calc.py", "content": wrong})),
        _turn(("finish", {"summary": "nope"})),
        _turn(("finish", {"summary": "still nope"})),
    ], policy="tests", test_cmd=test_cmd)
    assert any(e.kind == "escalated" for e in events)
    assert s.provider == "gemini"          # the session switched tiers


def test_loop_budget_blocks_before_calling_model():
    repo = _agent_repo()
    cfg = json.loads(json.dumps(_CFG))
    cfg["metering"]["budgets"]["qwen_tokens_per_day"] = 1
    tmp = Path(tempfile.mkdtemp())
    metering._METRICS_PATH = tmp / "metrics.jsonl"
    metering.record({"tier": "qwen", "tokens_out": 999}, cfg)
    s, v, events, model = _run_agent(repo, cfg, [_turn(("list_files", {}))],
                                     policy="off")
    assert events[0].kind == "stopped" and "budget" in events[0].text
    assert not model.seen_messages          # the provider was never called


# --- --json event protocol (what a VS Code / web UI consumes) ----------------
def test_event_wire_format():
    e = loop.Event("tool_call", name="read_file", args={"path": "a.py"})
    assert e.to_dict() == {"type": "tool_call", "tool": "read_file",
                           "args": {"path": "a.py"}}
    assert loop.Event("tool_result", "1\tx = 1", name="read_file").to_dict() == {
        "type": "tool_result", "tool": "read_file", "text": "1\tx = 1"}
    assert loop.Event("verify_passed", "tests").to_dict() == {
        "type": "verify_passed", "check": "tests"}
    fail = loop.Event("verify_failed", "AssertionError", name="tests").to_dict()
    assert fail == {"type": "verify_failed", "check": "tests", "text": "AssertionError"}
    assert loop.Event("text", "hello").to_dict() == {"type": "text", "text": "hello"}


def test_json_mode_headless_stream():
    """A frontend must get parseable JSON lines: session_start ... session_end."""
    import io
    import contextlib
    import chat_ui

    repo = _agent_repo()
    right = ("def add(a, b):\n    return a + b\n"
             "def mul(a, b):\n    return a * b\n")
    turns = [_turn(("write_file", {"path": "calc.py", "content": right})),
             _turn(("finish", {"summary": "added mul"}))]
    model = _ScriptedModel(turns)
    old = loop.chat_providers.chat
    loop.chat_providers.chat = model
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = chat_ui.run_headless(str(repo), _CFG, "add mul", _check_cmd(),
                                      "qwen", verify="gate", json_mode=True)
    finally:
        loop.chat_providers.chat = old

    events = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    kinds = [e["type"] for e in events]
    assert rc == 0
    assert kinds[0] == "session_start" and kinds[-1] == "session_end"
    assert "tool_call" in kinds and "verify_passed" in kinds
    assert all("ts" in e for e in events)              # every event is timestamped
    start = events[0]
    assert start["provider"] == "qwen" and start["mode"] == "headless"
    end = events[-1]
    assert end["done_passed"] is True and end["files_changed"] == ["calc.py"]
    assert end["usage"]["turns"] == 2


def test_json_mode_confirm_protocol():
    """In --json mode a shell command asks over the wire and fails safe on EOF."""
    import io
    import contextlib
    import chat_ui

    confirm = chat_ui._confirmer(auto_yes=False, json_mode=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), _stdin('{"allow": true}\n'):
        allowed = confirm("run_cmd", "echo hi")
    req = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
    assert allowed is True
    assert req[0]["type"] == "confirm_request" and req[0]["detail"] == "echo hi"

    with contextlib.redirect_stdout(io.StringIO()):
        with _stdin(""):                       # EOF -> refuse (fail safe)
            assert confirm("run_cmd", "echo hi") is False
        with _stdin("y\n"):                    # plain answers still work
            assert confirm("run_cmd", "echo hi") is True


class _stdin:
    """Minimal stdin redirector (contextlib has no redirect_stdin)."""

    def __init__(self, text: str):
        self.text = text

    def __enter__(self):
        import io
        self._old = sys.stdin
        sys.stdin = io.StringIO(self.text)
        return self

    def __exit__(self, *exc):
        sys.stdin = self._old
        return False


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
