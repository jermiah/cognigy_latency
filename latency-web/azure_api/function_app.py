"""
Azure Functions (Python v2 model) — POST /api/latency

When linked to an Azure Static Web App this is reachable at /api/latency,
matching the path the frontend calls. Anonymous auth: the user supplies their
own Cognigy credentials per request, so there is nothing to protect here.
"""

import json
import azure.functions as func

from _core import compute, fetch_endpoints, CognigyError

app = func.FunctionApp()


@app.route(route="latency", methods=["POST", "GET"], auth_level=func.AuthLevel.ANONYMOUS)
def latency(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "GET":
        return _json(200, {"status": "ok", "hint": "POST credentials to this endpoint."})

    try:
        payload = req.get_json()
    except ValueError:
        return _json(400, {"error": "Invalid request body."})

    try:
        return _json(200, compute(payload))
    except CognigyError as e:
        code = 200 if e.status == 200 else e.status
        return _json(code, {"error": e.message})
    except Exception:  # noqa: BLE001
        return _json(500, {"error": "Unexpected server error."})


@app.route(route="endpoints", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def endpoints(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = req.get_json()
    except ValueError:
        return _json(400, {"error": "Invalid request body."})

    try:
        return _json(200, {"endpoints": fetch_endpoints(
            base_url=payload.get("base_url", ""),
            api_key=payload.get("api_key", ""),
            project_id=payload.get("project_id", ""),
        )})
    except CognigyError as e:
        code = 200 if e.status == 200 else e.status
        return _json(code, {"error": e.message})
    except Exception:  # noqa: BLE001
        return _json(500, {"error": "Unexpected server error."})


def _json(status: int, body: dict) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(body), status_code=status, mimetype="application/json"
    )
