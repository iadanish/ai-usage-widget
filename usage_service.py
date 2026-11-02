"""Optional local JSON endpoint: GET http://127.0.0.1:8765/api/usage -> usage for every provider.

The widget hosts this in-process, so you don't need to run it yourself. Run it standalone
(pythonw usage_service.py) if you want the data for something else, e.g. a local dashboard.

Loopback only. Browser access is limited to file:// pages (Origin: null), so websites you visit can't
read it, and a Host check blocks DNS rebinding. Refreshes every REFRESH seconds; requests get the cache.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import usage_core

HOST = "127.0.0.1"
PORT = int(usage_core.config().get("port", 8765))
REFRESH = max(120, int(usage_core.config().get("refresh_seconds", 300)))  # don't hammer the providers
_cache = {"data": None}
_lock = threading.Lock()


def refresher():
    while True:
        try:
            refresh_now()
        except Exception:
            pass
        time.sleep(REFRESH)


def refresh_now():
    data = usage_core.collect()
    with _lock:
        _cache["data"] = data
    return data


def latest():
    with _lock:
        return _cache["data"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if self.headers.get("Origin") == "null":
            self.send_header("Access-Control-Allow-Origin", "null")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if (self.headers.get("Host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, b'{"error":"forbidden"}')
        path = self.path.split("?")[0]
        if path == "/api/usage":
            data = latest()
            return self._send(503, b'{"error":"warming up"}') if data is None else self._send(200, json.dumps(data).encode())
        if path == "/health":
            return self._send(200, b'{"ok":true}')
        self._send(404, b'{"error":"not found"}')


def start():
    """Serve on background threads inside the caller's process. False if the port is already taken."""
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        return False
    threading.Thread(target=refresher, daemon=True).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


if __name__ == "__main__":
    if not start():
        raise SystemExit(0)  # already running
    while True:
        time.sleep(3600)
