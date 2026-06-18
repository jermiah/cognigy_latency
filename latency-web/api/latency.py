"""
Vercel serverless function — POST /api/latency
Receives Cognigy credentials in the request body, returns dashboard JSON.
The frontend and this function share an origin, so no CORS handling is needed.
"""

import os
import sys
import json
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _core import compute, CognigyError  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "Invalid request body."})

        try:
            data = compute(payload)
            return self._send(200, data)
        except CognigyError as e:
            # status 200 with an error key for the "no turns" soft case
            code = 200 if e.status == 200 else e.status
            return self._send(code, {"error": e.message})
        except Exception:  # noqa: BLE001
            return self._send(500, {"error": "Unexpected server error."})

    def do_GET(self):
        self._send(200, {"status": "ok", "hint": "POST credentials to this endpoint."})
