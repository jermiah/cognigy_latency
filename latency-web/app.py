"""
Vercel entrypoint — a single Flask app that serves both the frontend and the
latency API. Vercel's Python runtime auto-detects `app.py` with a top-level
`app` variable and runs it as a Flask app (a first-class framework preset),
so the app handles all routing itself — no /api function-file detection, no
static-vs-function ambiguity.

  GET  /              -> the dashboard page (index.html)
  POST /api/latency   -> fetch Cognigy logs + compute latency (returns JSON)

All latency logic lives in _core.py, which is shared verbatim with the Azure
backend (azure_api/_core.py).
"""

import os

from flask import Flask, request, jsonify, Response

from _core import compute, CognigyError

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
def index():
    with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.post("/api/latency")
def latency():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(compute(payload))
    except CognigyError as e:
        status = 200 if e.status == 200 else e.status
        return jsonify({"error": e.message}), status
    except Exception:  # noqa: BLE001
        app.logger.exception("Unhandled /api/latency error")
        return jsonify({"error": "Unexpected server error."}), 500


@app.get("/api/latency")
def latency_health():
    return jsonify({"status": "ok", "hint": "POST credentials to this endpoint."})


if __name__ == "__main__":
    # Local dev:  python app.py  ->  http://localhost:3000
    app.run(port=3000, debug=True)
