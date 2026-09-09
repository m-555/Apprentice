"""Offline migration contracts. Opt-in real OpenCode uses a fake local model API."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opencode_config
import opencode_ui
import opencode_tasks
import paths
from opencode_client import OpenCode, Cancelled
from opencode_transcript import Transcript, final_answer
from workspace import Workspace, git, undo


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="apprentice-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.outputs = Path(self.temp.name) / "outputs"
        for name, value in (("ROOT", Path(self.temp.name)), ("OUTPUTS_DIR", self.outputs),
                            ("CORRECTIONS_PATH", Path(self.temp.name) / "corrections.jsonl")):
            isolated = patch.object(paths, name, value)
            isolated.start()
            self.addCleanup(isolated.stop)
        git(self.repo, "init")
        (self.repo / "calc.py").write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
        self.cfg = {"providers": {"default": "qwen", "qwen": {"enabled": True,
            "kind": "openai-compatible", "base_url": "http://127.0.0.1:8080/v1", "model": "fixture-model"}},
            "agent_chat": {"use_corrections": False}, "gate": {"enabled": False},
            "metering": {"enabled": False}, "opencode": {"max_attempts": 2, "task_timeout_s": 60}}


class RepositoryTest(RepositoryFixture):
    def test_current_dirty_state_and_byte_exact_undo(self):
        original = b"# user change\r\n\xff\x00"
        (self.repo / "data.bin").write_bytes(original)
        with Workspace(str(self.repo), self.outputs) as work:
            self.assertEqual((work.directory / "data.bin").read_bytes(), original)
            (work.directory / "data.bin").write_bytes(b"new\x00\xff\r\n")
            (work.directory / "calc.py").unlink()
            result = work.deliver("task", True)
        self.assertTrue(result["applied"])
        self.assertFalse((self.repo / "calc.py").exists())
        undo(Path(result["manifest_path"]), str(self.repo))
        self.assertEqual((self.repo / "data.bin").read_bytes(), original)
        self.assertEqual((self.repo / "calc.py").read_bytes(), b"def add(a, b):\r\n    return a - b\r\n")

    def test_conflicting_user_change_never_overwritten(self):
        with Workspace(str(self.repo), self.outputs) as work:
            (work.directory / "calc.py").write_text("candidate", encoding="utf-8")
            (self.repo / "calc.py").write_text("new user edit", encoding="utf-8")
            result = work.deliver("conflict", True)
        self.assertFalse(result["applied"])
        self.assertIn("calc.py", result["apply_error"])
        self.assertEqual((self.repo / "calc.py").read_text(), "new user edit")

    def test_undo_preserves_later_edits(self):
        with Workspace(str(self.repo), self.outputs) as work:
            (work.directory / "new.txt").write_text("candidate", encoding="utf-8")
            result = work.deliver("undo", True)
        (self.repo / "new.txt").write_text("later user edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "newer edits"):
            undo(Path(result["manifest_path"]), str(self.repo))

    def test_failed_task_keeps_candidate_only(self):
        with Workspace(str(self.repo), self.outputs) as work:
            (work.directory / "new.py").write_text("syntax error!", encoding="utf-8")
            result = work.deliver("failed", False)
        self.assertFalse((self.repo / "new.py").exists())
        self.assertTrue(Path(result["patch_path"]).exists())

    def test_catalog_and_mode_permissions(self):
        selected = opencode_config.resolve(self.cfg, "qwen")
        self.assertEqual(selected["output"], 8192)
        config = opencode_config.configuration(self.cfg, selected, "ask")
        self.assertEqual(config["permission"]["edit"], "deny")
        self.assertEqual(config["permission"]["bash"], "deny")
        self.assertEqual(config["permission"]["doom_loop"], "deny")
        with self.assertRaises(ValueError):
            opencode_config.resolve(self.cfg, "qwen", "not-configured")

    def test_resume_refuses_another_workspace(self):
        with patch.object(paths, "ROOT", Path(self.temp.name)):
            session = opencode_ui.new_session(str(self.repo), self.cfg, "qwen")
            opencode_ui.save(session)
            with self.assertRaisesRegex(ValueError, "another repository"):
                opencode_ui.new_session(self.temp.name, self.cfg, "qwen", resume=session["id"])

    def test_same_repo_cannot_have_two_task_worktrees(self):
        with Workspace(str(self.repo), self.outputs):
            with self.assertRaisesRegex(ValueError, "Another Apprentice task"):
                with Workspace(str(self.repo), self.outputs):
                    self.fail("second task was allowed")

    def test_session_worktree_is_stable_but_refreshed(self):
        with Workspace(str(self.repo), self.outputs, session_id="session") as first:
            directory = first.directory
            (first.directory / "calc.py").write_text("discarded", encoding="utf-8")
        with Workspace(str(self.repo), self.outputs, session_id="session") as second:
            self.assertEqual(second.directory, directory)
            self.assertIn("return a - b", (second.directory / "calc.py").read_text())

    def test_settings_are_atomic_and_select_alias(self):
        session = opencode_ui.new_session(str(self.repo), self.cfg, "qwen")
        ui = opencode_ui.UI(session, self.cfg, False)
        with self.assertRaises(ValueError):
            ui.settings({"provider": "not-found", "mode": "build"})
        self.assertEqual(session["mode"], "ask")
        self.assertEqual(session["provider"], "qwen")
        self.assertEqual(session["model"], "fixture-model")

    def test_failed_checks_cannot_be_weakened(self):
        import task_checks
        (self.repo / "test_calc.py").write_text("assert False\n", encoding="utf-8")
        with Workspace(str(self.repo), self.outputs) as work:
            (work.directory / "test_calc.py").write_text("assert True\n", encoding="utf-8")
            ok, name, _ = task_checks.check(work, self.cfg, "tests", "echo success", threading.Event())
        self.assertFalse(ok)
        self.assertEqual(name, "acceptance integrity")

    def test_no_applicable_gate_is_not_a_verified_pass(self):
        import task_checks
        with Workspace(str(self.repo), self.outputs) as work:
            (work.directory / "unknown.bin").write_bytes(b"\xff\x00")
            ok, name, _ = task_checks.check(work, self.cfg, "gate", "", threading.Event())
        self.assertFalse(ok)
        self.assertEqual(name, "no applicable checks")

    def test_git_index_is_preserved_when_delivering(self):
        (self.repo / "calc.py").write_text("# staged user change\ndef add(a,b): return a-b\n", encoding="utf-8")
        git(self.repo, "add", "calc.py")
        index = git(self.repo, "diff", "--cached", "--binary")
        with Workspace(str(self.repo), self.outputs) as work:
            (work.directory / "new.txt").write_text("new", encoding="utf-8")
            work.deliver("index", True)
        self.assertEqual(git(self.repo, "diff", "--cached", "--binary"), index)

    def test_memory_is_scoped_relevant_and_can_be_disabled(self):
        import lessons
        correction = Path(self.temp.name) / "corrections.jsonl"
        record = {"repo": str(self.repo), "task": "Fix parser newline handling", "corrected_output": "patch", "timestamp": "today"}
        correction.write_text(json.dumps(record) + "\n" + json.dumps({**record, "repo": self.temp.name}) + "\n", encoding="utf-8")
        with patch.object(paths, "CORRECTIONS_PATH", correction):
            self.assertEqual(lessons.select("hello", str(self.repo), self.cfg), [])
            found = lessons.select("Fix parser newline handling", str(self.repo), self.cfg)
            self.assertEqual(len(found), 1)
            lessons.set_disabled(found[0]["id"], True)
            self.assertEqual(lessons.select("Fix parser newline handling", str(self.repo), self.cfg), [])
            lessons.set_disabled(found[0]["id"], False)
            self.assertEqual(len(lessons.select("Fix parser newline handling", str(self.repo), self.cfg)), 1)


class TranscriptTest(unittest.TestCase):
    def test_metadata_first_reasoning_and_checkpoint_filter(self):
        out = []
        projector = Transcript(out.append)
        projector.apply({"type": "message.part.updated", "properties": {"part": {
            "id": "p", "messageID": "m", "type": "text", "text": "checkpoint"}}})
        self.assertEqual(out, [])
        projector.apply({"type": "message.updated", "properties": {"info": {
            "id": "m", "role": "assistant", "summary": True}}})
        self.assertEqual(out[-1]["type"], "message_remove")
        projector.apply({"type": "message.part.updated", "properties": {"part": {
            "id": "r", "messageID": "m", "type": "reasoning", "text": "scratchpad"}}})
        self.assertNotIn("r", projector.parts)

    def test_text_deltas_and_canonical_snapshot_do_not_duplicate(self):
        out = []
        p = Transcript(out.append)
        p.apply({"type": "message.updated", "properties": {"info": {"id": "a", "role": "assistant"}}})
        part = {"id": "t", "messageID": "a", "type": "text", "text": "I found "}
        p.apply({"type": "message.part.updated", "properties": {"part": part}})
        p.apply({"type": "message.part.delta", "properties": {"partID": "t", "field": "text", "delta": "the builder."}})
        self.assertEqual(out[-1]["text"], "I found the builder.")
        p.apply({"type": "message.part.updated", "properties": {"part": {**part, "text": "Final answer"}}})
        self.assertEqual(out[-1]["text"], "Final answer")

    def test_no_final_answer_is_a_failure(self):
        with self.assertRaisesRegex(RuntimeError, "output-token limit"):
            final_answer([{"info": {"role": "assistant", "finish": "length"}, "parts": []}])


class ScriptedRuntime:
    def __init__(self, directory, config, cfg, cancelled):
        self.directory = Path(directory)
        self.events = queue.Queue()
        self.session_id = ""
        self.calls = 0
    def start(self): pass
    def close(self): pass
    def abort(self): pass
    def session(self, previous=""):
        self.session_id = previous or "ses_fixture"
        return self.session_id
    def request(self, method, path, body=None):
        if path.endswith("prompt_async"):
            self.calls += 1
            (self.directory / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            for status in ("busy", "idle"):
                self.events.put({"type": "session.status", "properties": {"sessionID": self.session_id, "status": {"type": status}}})
        if path.endswith("/message"):
            if not self.calls:
                return []
            return [{"info": {"id": "m" + str(self.calls), "role": "assistant", "finish": "stop", "time": {"completed": 1}, "tokens": {"input": 10, "output": 20}},
                     "parts": [{"id": "t1", "messageID": "m1", "type": "text", "text": "Updated add to return the sum. Apprentice will verify the candidate independently."}]}]


class TaskTest(RepositoryFixture):
    def test_single_engine_build_and_usage_once(self):
        session = opencode_ui.new_session(str(self.repo), self.cfg, "qwen", verify="off")
        events = []
        with patch.object(paths, "OUTPUTS_DIR", self.outputs):
            task = opencode_tasks.Task(self.cfg, events.append, runtime_factory=ScriptedRuntime)
            result = task.run(session, "Fix add", mode="build")
        self.assertTrue(result["applied"])
        self.assertEqual(session["usage"]["tokens_out"], 20)
        self.assertEqual(session["usage"]["turns"], 1)

    def test_read_only_violation_never_reaches_user_files(self):
        session = opencode_ui.new_session(str(self.repo), self.cfg, "qwen")
        before = (self.repo / "calc.py").read_bytes()
        with patch.object(paths, "OUTPUTS_DIR", self.outputs):
            task = opencode_tasks.Task(self.cfg, lambda _: None, runtime_factory=ScriptedRuntime)
            with self.assertRaisesRegex(RuntimeError, "Read-only"):
                task.run(session, "Explain add", mode="ask")
        self.assertEqual((self.repo / "calc.py").read_bytes(), before)

    def test_cancel_before_start_never_invokes_model(self):
        session = opencode_ui.new_session(str(self.repo), self.cfg, "qwen")
        task = opencode_tasks.Task(self.cfg, lambda _: None, runtime_factory=ScriptedRuntime)
        task.cancelled.set()
        with self.assertRaises(Cancelled):
            task.run(session, "Fix add", mode="build")

    def test_budget_exhaustion_prevents_runtime_creation(self):
        session = opencode_ui.new_session(str(self.repo), self.cfg, "qwen")
        with patch.object(opencode_tasks.budgets, "exceeded", return_value="Daily budget exhausted"):
            task = opencode_tasks.Task(self.cfg, lambda _: None, runtime_factory=lambda *_: self.fail("runtime started"))
            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                task.run(session, "hello")

    def test_failure_is_bounded_and_patch_not_applied(self):
        session = opencode_ui.new_session(str(self.repo), self.cfg, "qwen", verify="tests", test_cmd="exit 1")
        with patch.object(paths, "OUTPUTS_DIR", self.outputs):
            result = opencode_tasks.Task(self.cfg, lambda _: None, runtime_factory=ScriptedRuntime).run(session, "Fix add", mode="build")
        self.assertFalse(result["applied"])
        self.assertFalse(result["done_passed"])
        self.assertEqual(result["iterations"], 2)
        self.assertTrue(Path(result["patch_path"]).exists())
        self.assertIn("a - b", (self.repo / "calc.py").read_text())

    def test_paid_model_requires_approval_without_escalation(self):
        self.cfg["providers"]["qwen"]["base_url"] = "https://cloud.example.invalid/v1"
        session = opencode_ui.new_session(str(self.repo), self.cfg, "qwen")
        with self.assertRaisesRegex(ValueError, "not approved"):
            opencode_tasks.Task(self.cfg, lambda _: None).run(session, "hello")

    def test_assign_uses_the_same_task_controller(self):
        import server
        run = opencode_tasks.Task.run
        def scripted(task, *args, **kwargs):
            task.runtime_factory = ScriptedRuntime
            return run(task, *args, **kwargs)
        with patch.object(server, "_CFG", self.cfg), patch.object(paths, "ROOT", Path(self.temp.name)), \
             patch.object(paths, "OUTPUTS_DIR", self.outputs), patch.object(opencode_tasks.Task, "run", scripted):
            result = server.assign("Fix add", f'"{sys.executable}" -c "from calc import add; assert add(2,3)==5"', str(self.repo))
        self.assertTrue(result["applied"])
        self.assertTrue(result["done_passed"])


@unittest.skipUnless(os.environ.get("APPRENTICE_TEST_OPENCODE") == "1", "opt-in real OpenCode with a fake model; no GPU")
class OpenCodeContractTest(RepositoryFixture):
    def test_real_opencode_reads_edits_and_finishes_with_fake_model(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                names = [c["function"]["name"] for m in body["messages"] for c in m.get("tool_calls", [])]
                if "read" not in names:
                    name, args = "read", {"filePath": "calc.py", "offset": 1, "limit": 100}
                elif "edit" not in names:
                    name, args = "edit", {"filePath": "calc.py", "oldString": "return a - b", "newString": "return a + b"}
                else:
                    name, args = "", {}
                delta = {"role": "assistant"}
                if name:
                    delta["tool_calls"] = [{"index": 0, "id": "call_" + str(len(requests)), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
                else:
                    delta["content"] = "The add function now returns a + b instead of subtracting. I inspected calc.py and changed that one expression. Apprentice will run the independent acceptance check before delivery."
                chunks = [{"id": "chat_" + str(len(requests)), "object": "chat.completion.chunk", "created": 1, "model": "fixture-model",
                           "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"id": "done", "object": "chat.completion.chunk", "created": 1, "model": "fixture-model",
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if name else "stop"}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}}]
                payload = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload.encode())))
                self.end_headers()
                self.wfile.write(payload.encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.cfg["providers"]["qwen"]["base_url"] = f"http://127.0.0.1:{server.server_port}/v1"
        self.cfg["opencode"]["task_timeout_s"] = 90
        session = opencode_ui.new_session(str(self.repo), self.cfg, "qwen", verify="tests",
            test_cmd=f'"{sys.executable}" -c "from calc import add; assert add(2, 3) == 5"')
        events = []
        with patch.object(paths, "OUTPUTS_DIR", self.outputs), patch.object(paths, "CORRECTIONS_PATH", self.outputs / "corrections.jsonl"):
            result = opencode_tasks.Task(self.cfg, events.append).run(session, "Fix add in calc.py and give a short final answer.", mode="build")
        self.assertTrue(result["done_passed"], result)
        self.assertTrue(result["applied"], result)
        self.assertEqual(len(requests), 3)
        self.assertIn("a + b", (self.repo / "calc.py").read_text())
        self.assertTrue(any(e["type"] == "tool_part" and e["tool"] == "read" for e in events))
        self.assertFalse(any(e.get("type") == "reasoning" for e in events))


if __name__ == "__main__":
    unittest.main()
