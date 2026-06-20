"""
Shared latency core
====================
Pure logic for the VoiceGateway2 latency web app — no CLI, no file I/O, no
framework. Given Cognigy credentials it fetches logs and returns a JSON-ready
dashboard data structure (the same shape the original generate_dashboard.py
produced).

Used by BOTH backends:
  - api/latency.py        (Vercel serverless function)
  - azure_api/function_app.py (Azure Functions)

Keep this file identical in both folders. It is duplicated rather than shared
because each platform packages only its own backend directory at deploy time.
"""

from datetime import datetime, timezone
from collections import defaultdict
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests


# ── Constants ───────────────────────────────────────────────────────────────

INBOUND_MSG       = "Received message from user"
OUTBOUND_MSG      = "Sent output to Endpoint"
TARGET_CHANNEL    = "voiceGateway2"
LATENCY_THRESHOLD = 5000   # ms — turns above this are flagged
GREEN_MAX         = 2500
YELLOW_MAX        = 5000


class CognigyError(Exception):
    """Raised for any user-facing failure (bad key, bad project, no logs)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ── Tier helpers ──────────────────────────────────────────────────────────────

def get_tier(ms: int) -> str:
    if ms < GREEN_MAX:  return "green"
    if ms < YELLOW_MAX: return "yellow"
    return "red"


def tier_label(ms: int) -> str:
    return {"green": "Acceptable", "yellow": "Degraded", "red": "Unacceptable"}[get_tier(ms)]


# ── API fetching ────────────────────────────────────────────────────────────

def fetch_logs(base_url: str, api_key: str, project_id: str, limit: int = 2000) -> list:
    """
    Fetch log entries from the Cognigy Logs API, paginating automatically.
    limit == 0 means fetch everything. Raises CognigyError on any failure.
    """
    base_url = (base_url or "").strip().rstrip("/")
    if base_url.endswith("/openapi"):
        base_url = base_url[: -len("/openapi")]

    if not base_url:
        raise CognigyError("Base URL is required.")
    if not api_key:
        raise CognigyError("API key is required.")
    if not project_id:
        raise CognigyError("Project ID is required.")

    headers   = {"X-API-Key": api_key}
    all_items = []
    page      = 1
    page_size = 25
    unlimited = (limit == 0)

    current_url    = f"{base_url}/v2.0/projects/{project_id}/logs"
    current_params = {"limit": page_size}

    while True:
        try:
            resp = requests.get(
                current_url, headers=headers, params=current_params, timeout=30
            )
        except requests.exceptions.ConnectionError:
            raise CognigyError(f"Could not connect to {base_url}. Check the base URL.", 502)
        except requests.exceptions.Timeout:
            raise CognigyError("Request to Cognigy timed out. Try a smaller limit.", 504)

        if resp.status_code == 401:
            raise CognigyError("Authentication failed (401). Check your API key.", 401)
        if resp.status_code == 404:
            raise CognigyError("Not found (404). Check your Project ID and base URL.", 404)
        if not resp.ok:
            raise CognigyError(f"Cognigy API returned {resp.status_code}.", 502)

        data     = resp.json()
        embedded = data.get("_embedded", {})
        items    = embedded.get("logEntry", [])
        all_items.extend(items)

        next_href = data.get("_links", {}).get("next", {}).get("href", "")
        if next_href:
            parsed    = urlparse(next_href)
            params_qs = parse_qs(parsed.query, keep_blank_values=False)
            params_qs.pop("previous", None)
            clean_query    = urlencode({k: v[0] for k, v in params_qs.items()})
            next_href      = urlunparse(parsed._replace(scheme="https", query=clean_query))
            current_url    = next_href
            current_params = None
        else:
            next_href = None

        page += 1
        if not next_href or not items:
            break
        if not unlimited and len(all_items) >= limit:
            break

    if not unlimited:
        all_items = all_items[:limit]

    all_items.sort(key=lambda x: x.get("timestamp", ""))
    return all_items


# ── Filtering, grouping, latency ──────────────────────────────────────────────

def parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def filter_vg2(items: list) -> list:
    out = []
    for item in items:
        ch       = item.get("meta", {}).get("channel", "")
        trace_id = item.get("traceId", "")
        if ch == TARGET_CHANNEL or trace_id.startswith("endpoint-vg2client-"):
            out.append(item)
    return out


def group_by_trace(items: list) -> dict:
    groups = defaultdict(list)
    for item in items:
        groups[item.get("traceId", "unknown")].append(item)
    return groups


def compute_turn_latency(entries: list):
    """Compute latency for one turn (entries sharing a traceId)."""
    inbound   = None
    outbounds = []
    for e in sorted(entries, key=lambda x: x.get("timestamp", "")):
        msg  = e.get("msg", "")
        meta = e.get("meta", {})
        if msg == INBOUND_MSG and inbound is None:
            inbound = e
        if msg == OUTBOUND_MSG and meta.get("text", ""):
            outbounds.append(e)

    if inbound is None or not outbounds:
        return None

    inbound_ts   = parse_ts(inbound["timestamp"])
    first_out_ts = parse_ts(outbounds[0]["timestamp"])
    last_out_ts  = parse_ts(outbounds[-1]["timestamp"])
    user_text    = inbound.get("meta", {}).get("text", "") or ""

    first_ms = int((first_out_ts - inbound_ts).total_seconds() * 1000)
    full_ms  = int((last_out_ts  - inbound_ts).total_seconds() * 1000)

    return {
        "session_id":               inbound.get("meta", {}).get("sessionId", "unknown"),
        "trace_id":                 inbound.get("traceId", ""),
        "inbound_time":             inbound["timestamp"],
        "first_output_time":        outbounds[0]["timestamp"],
        "last_output_time":         outbounds[-1]["timestamp"],
        "first_token_latency_ms":   first_ms,
        "full_response_latency_ms": full_ms,
        "exceeds_threshold":        first_ms > LATENCY_THRESHOLD,
        "user_text":                user_text,
        "user_text_length":         len(user_text),
        "first_bot_text":           outbounds[0].get("meta", {}).get("text", ""),
        "bot_output_segments":      len(outbounds),
    }


def analyze_turns(items: list) -> list:
    groups = group_by_trace(filter_vg2(items))
    turns  = []
    for _, entries in sorted(
        groups.items(), key=lambda kv: min(e["timestamp"] for e in kv[1])
    ):
        result = compute_turn_latency(entries)
        if result:
            turns.append(result)
    return turns


# ── Dashboard data assembly ────────────────────────────────────────────────────

def build_dashboard_data(turns: list, project_name: str) -> dict:
    """Turn the per-turn list into the JSON structure the frontend renders."""
    sessions_map = defaultdict(list)
    for t in turns:
        sessions_map[t["session_id"]].append(t)

    sorted_sessions = sorted(
        sessions_map.items(), key=lambda kv: kv[1][0]["inbound_time"]
    )

    all_latencies = [t["first_token_latency_ms"] for t in turns]
    tier_counts   = {"green": 0, "yellow": 0, "red": 0}

    session_data = []
    for s_num, (session_id, s_rows) in enumerate(sorted_sessions, 1):
        s_lats = []
        out_turns = []
        for t_num, t in enumerate(s_rows, 1):
            ms   = t["first_token_latency_ms"]
            tier = get_tier(ms)
            tier_counts[tier] += 1
            s_lats.append(ms)
            out_turns.append({
                "turn_number":         t_num,
                "first_token_ms":      ms,
                "full_response_ms":    t["full_response_latency_ms"],
                "tier":                tier,
                "tier_label":          tier_label(ms),
                "user_text":           t.get("user_text", ""),
                "first_bot_text":      t.get("first_bot_text", ""),
                "user_text_length":    t.get("user_text_length", 0),
                "bot_output_segments": t.get("bot_output_segments", 0),
            })

        s_max = max(s_lats)
        flagged_turns = [t for t in out_turns if t["tier"] != "green"]
        session_data.append({
            "session_number":  s_num,
            "session_id":      session_id,
            "session_start":   s_rows[0]["inbound_time"],
            "turn_count":      len(out_turns),
            "avg_latency_ms":  int(sum(s_lats) / len(s_lats)),
            "peak_latency_ms": s_max,
            "tier":            get_tier(s_max),
            "tier_label":      tier_label(s_max),
            "flagged_turns":   flagged_turns,
            "has_flags":       len(flagged_turns) > 0,
        })

    timestamps = sorted(t["inbound_time"] for t in turns if t.get("inbound_time"))
    date_str = ""
    if timestamps:
        start, end = timestamps[0][:10], timestamps[-1][:10]
        date_str = start if start == end else f"{start} – {end}"

    total = len(all_latencies)
    return {
        "project":        project_name,
        "generated_at":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "date_range":     date_str,
        "total_turns":    total,
        "total_sessions": len(session_data),
        "avg_latency_ms": int(sum(all_latencies) / total),
        "min_latency_ms": min(all_latencies),
        "max_latency_ms": max(all_latencies),
        "tier_counts":    tier_counts,
        "tier_pct":       {k: round(v / total * 100) for k, v in tier_counts.items()},
        "green_max_ms":   GREEN_MAX,
        "yellow_max_ms":  YELLOW_MAX,
        "sessions":       session_data,
    }


def compute(payload: dict) -> dict:
    """
    Top-level entry used by both backends.
    payload: { name, api_key, project_id, base_url, limit }
    Returns the dashboard data dict. Raises CognigyError for user-facing issues.
    """
    name = (payload.get("name") or "Cognigy Project").strip()
    try:
        limit = int(payload.get("limit", 2000))
    except (TypeError, ValueError):
        limit = 2000

    items = fetch_logs(
        base_url=payload.get("base_url", ""),
        api_key=payload.get("api_key", ""),
        project_id=payload.get("project_id", ""),
        limit=limit,
    )
    turns = analyze_turns(items)
    if not turns:
        raise CognigyError(
            "No voiceGateway2 turns found. Make some voice calls first, then "
            "wait 1–2 minutes for logs to appear and try again.", 200
        )
    return build_dashboard_data(turns, name)
