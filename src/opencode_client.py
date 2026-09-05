"""Owned, authenticated loopback OpenCode process. No model is loaded on startup."""
from __future__ import annotations

import base64
import collections
import json
import os
import queue
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from . import opencode_config
except ImportError:
    import opencode_config


class Cancelled(Exception):
    pass


class OpenCode:
    def __init__(self, directory: str, config: dict, app_config: dict, cancelled: threading.Event):
        self.directory, self.config, self.app_config = directory, config, app_config
        self.cancelled = cancelled
        self.process = None
        self.events: queue.Queue = queue.Queue(maxsize=8192)
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.session_id = ""
        self.budget_gate = None
        self.check_budget = lambda: "Stopped" if self.cancelled.is_set() else ""
        self.diagnostics = collections.deque(maxlen=12)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.password = secrets.token_urlsafe(24)
        self.headers = {"Authorization": "Basic " + base64.b64encode(
            f"opencode:{self.password}".encode()).decode(), "Content-Type": "application/json"}

    def request(self, method: str, path: str, body=None, timeout: float = 10):
        url = self.url + path + ("&" if "?" in path else "?") + urllib.parse.urlencode({"directory": self.directory})
        req = urllib.request.Request(url, method=method, headers=self.headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise RuntimeError("OpenCode response exceeded the safety limit.")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"OpenCode {method} {path}: HTTP {exc.code}: " +
                               exc.read(1500).decode("utf-8", "replace")) from exc

    def start(self):
        try:
            from .opencode_budget import BudgetGate
        except ImportError:
            from opencode_budget import BudgetGate
        self.budget_gate = BudgetGate(self.check_budget)
        self.config["plugin"] = [Path(__file__).with_name("opencode_guardrails.mjs").as_uri()]
        env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(self.config),
               "OPENCODE_SERVER_USERNAME": "opencode", "OPENCODE_SERVER_PASSWORD": self.password,
               "APPRENTICE_BUDGET_URL": self.budget_gate.url, "APPRENTICE_BUDGET_TOKEN": self.budget_gate.token,
               "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true"}
        # Do not inherit another client's forced config directory/provider set.
        for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"):
            env.pop(key, None)
        self.process = subprocess.Popen([opencode_config.executable(self.app_config), "serve",
            "--hostname", "127.0.0.1", "--port", self.url.rsplit(":", 1)[1]],
            cwd=self.directory, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def drain():
            for raw in self.process.stdout:
                self.diagnostics.append(raw.decode("utf-8", "replace")[:500])
        self.drain_thread = threading.Thread(target=drain, daemon=True)
        self.drain_thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise Cancelled("Stopped during OpenCode startup.")
            if self.process.poll() is not None:
                raise RuntimeError("OpenCode failed to start. " + "".join(self.diagnostics)[-1500:])
            try:
                health = self.request("GET", "/global/health", timeout=1)
                if health.get("healthy"):
                    expected = self.app_config.get("opencode", {}).get("expected_version", "1.18.25")
                    if health.get("version") != expected:
                        raise TypeError(f"OpenCode {health.get('version')} is installed; this adapter is tested with {expected}. Install that version or validate a new contract before changing opencode.expected_version.")
                    break
            except (OSError, ValueError, RuntimeError):
                time.sleep(.15)
        else:
            raise RuntimeError("OpenCode did not become ready within 30 seconds.")
        threading.Thread(target=self._events, daemon=True).start()
        if not self.ready.wait(5):
            raise RuntimeError("OpenCode event subscription failed.")

    def _events(self):
        req = urllib.request.Request(self.url + "/event?" + urllib.parse.urlencode({"directory": self.directory}),
                                     headers={**self.headers, "Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=45) as stream:
                self.ready.set()
                while not self.closed.is_set():
                    line = stream.readline(1024 * 1024 + 1)
                    if not line:
                        raise RuntimeError("OpenCode event stream disconnected.")
                    if len(line) > 1024 * 1024:
                        raise RuntimeError("Oversized OpenCode event.")
                    if line.startswith(b"data:"):
                        self.events.put_nowait(json.loads(line[5:].strip()))
        except Exception as exc:
            if not self.closed.is_set():
                try:
                    self.events.put_nowait({"type": "transport.error", "error": str(exc)})
                except queue.Full:
                    self.cancelled.set()

    def session(self, previous: str = "") -> str:
        rules = []
        for permission, patterns in self.config["permission"].items():
            for pattern, action in (patterns.items() if isinstance(patterns, dict) else [("*", patterns)]):
                rules.append({"permission": permission, "pattern": pattern, "action": action})
        if previous:
            # The worktree belongs to the same Git project. Preserve native history.
            self.request("GET", f"/session/{previous}")
            self.session_id = previous
            self.request("PATCH", f"/session/{previous}", {"permission": rules})
        else:
            self.session_id = self.request("POST", "/session", {"title": "Apprentice session",
                "permission": rules})["id"]
        return self.session_id

    def abort(self):
        self.cancelled.set()
        if self.session_id:
            self.request("POST", f"/session/{self.session_id}/abort", timeout=10)

    def close(self):
        self.closed.set()
        try:
            if self.process and self.process.poll() is None:
                try:
                    if self.session_id:
                        self.request("POST", f"/session/{self.session_id}/abort", timeout=5)
                except (OSError, RuntimeError):
                    pass  # Process termination is the fallback for an unresponsive server.
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            if self.process and self.process.poll() is not None and self.process.stdout:
                self.drain_thread.join(timeout=2)
                self.process.stdout.close()
            if self.budget_gate:
                self.budget_gate.close()
                self.budget_gate = None
