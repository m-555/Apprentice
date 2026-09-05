"""Private per-call budget/cancellation gate used by OpenCode's chat.params hook."""
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class BudgetGate:
    def __init__(self, check):
        self.token = secrets.token_urlsafe(24)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path != "/budget" or self.headers.get("Authorization") != "Bearer " + owner.token:
                    self.send_error(403)
                    return
                try:
                    reason = check()
                    body = json.dumps({"allowed": not reason, "reason": reason}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    self.send_error(503, "Budget accounting unavailable")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/budget"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
