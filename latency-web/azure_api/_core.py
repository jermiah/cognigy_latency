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

from datetime import datetime, time, timezone
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
VG2_MARKERS       = ("voicegateway2", "voicegateway", "vg2", "endpoint-vg2client")


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

def _extract_items(data):
    if isinstance(data, list):
        return data
    embedded = data.get("_embedded", {})
    for key in ("logEntry", "logs", "items", "data", "endpoints", "endpoint"):
        value = embedded.get(key) if key in embedded else data.get(key)
        if isinstance(value, list):
            return value
    return []


def _next_link(data):
    links = data.get("_links", {}) if isinstance(data, dict) else {}
    next_link = links.get("next", {})
    if isinstance(next_link, dict):
        return next_link.get("href", "")
    if isinstance(next_link, str):
        return next_link
    return ""


def _clean_next_url(next_href: str) -> str:
    parsed = urlparse(next_href)
    params_qs = parse_qs(parsed.query, keep_blank_values=False)
    params_qs.pop("previous", None)
    clean_query = urlencode({k: v[0] for k, v in params_qs.items()})
    return urlunparse(parsed._replace(scheme="https", query=clean_query))


def _collect_log_pages(first_data, headers, limit: int) -> list:
    all_items = []
    current_data = first_data
    unlimited = (limit == 0)

    while True:
        items = _extract_items(current_data)
        all_items.extend(items)

        next_href = _next_link(current_data)
        if not next_href or not items:
            break
        if not unlimited and len(all_items) >= limit:
            break

        current_data = _request_json(_clean_next_url(next_href), headers, None)

    if not unlimited:
        all_items = all_items[:limit]
    return all_items


def _request_json(url, headers, params):
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
    except requests.exceptions.ConnectionError:
        raise CognigyError(f"Could not connect to {url}. Check the base URL.", 502)
    except requests.exceptions.Timeout:
        raise CognigyError("Request to Cognigy timed out. Try a smaller limit.", 504)

    if resp.status_code == 401:
        raise CognigyError("Authentication failed (401). Check your API key.", 401)
    if resp.status_code == 403:
        raise CognigyError("Access denied (403). Check that your API key can read project logs.", 403)
    if resp.status_code == 429:
        raise CognigyError("Cognigy API rate limit reached (429). Try again in a minute.", 429)
    if resp.status_code == 404:
        raise CognigyError("Not found (404). Check your Project ID and base URL.", 404)
    if not resp.ok:
        raise CognigyError(f"Cognigy API returned {resp.status_code}.", 502)

    try:
        data = resp.json()
    except ValueError:
        content_type = resp.headers.get("content-type", "unknown")
        raise CognigyError(
            "Cognigy did not return JSON. Check that the Base URL is the "
            f"API root, not a login or app page. Response content type: {content_type}.",
            502,
        )
    if not isinstance(data, (dict, list)):
        raise CognigyError("Cognigy returned an unexpected response shape.", 502)
    return data


def _clean_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    if base_url.endswith("/openapi"):
        base_url = base_url[: -len("/openapi")]
    return base_url


def _auth_candidates(api_key: str):
    return [
        ("X-API-Key", {"X-API-Key": api_key}),
        ("Bearer", {"Authorization": f"Bearer {api_key}"}),
        ("Token", {"Authorization": api_key}),
    ]


def _iso_date(value: str, end_of_day: bool = False) -> str:
    dt = parse_date_boundary(value, end_of_day=end_of_day)
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _with_filters(
    params: dict,
    date_from: str = "",
    date_to: str = "",
    endpoint_id: str = "",
    date_style: str = "from_to",
    endpoint_style: str = "endpointId",
) -> dict:
    params = dict(params)
    start = _iso_date(date_from)
    end = _iso_date(date_to, end_of_day=True)
    if date_style == "from_to":
        if start:
            params["from"] = start
        if end:
            params["to"] = end
    elif date_style == "start_end":
        if start:
            params["startDate"] = start
        if end:
            params["endDate"] = end
    elif date_style == "date_from_to":
        if start:
            params["dateFrom"] = start
        if end:
            params["dateTo"] = end
    elif date_style == "timestamp_from_to":
        if start:
            params["timestampFrom"] = start
        if end:
            params["timestampTo"] = end
    elif date_style == "odata_timestamp":
        clauses = []
        if start:
            clauses.append(f"timestamp gt '{start}'")
        if end:
            clauses.append(f"timestamp lt '{end}'")
        if clauses:
            params["$filter"] = " and ".join(clauses)
    if endpoint_id:
        params[endpoint_style] = endpoint_id
    return {k: v for k, v in params.items() if v not in ("", None)}


def _filter_variants(date_from: str, date_to: str, endpoint_id: str):
    has_date = bool(date_from or date_to)
    date_styles = (
        "odata_timestamp",
        "from_to",
        "start_end",
        "date_from_to",
        "timestamp_from_to",
    ) if has_date else ("from_to",)
    endpoint_styles = ("endpointId", "endpointReference", "endpoint")
    if endpoint_id:
        for endpoint_style in endpoint_styles:
            for date_style in date_styles:
                yield date_style, endpoint_style, endpoint_id
    if has_date or not endpoint_id:
        for date_style in date_styles:
            yield date_style, "", ""


def _items_overlap_date_filter(items: list, date_from: str = "", date_to: str = "") -> bool:
    if not (date_from or date_to):
        return True
    start = parse_date_boundary(date_from or "")
    end = parse_date_boundary(date_to or "", end_of_day=True)
    saw_dated_item = False
    for item in items:
        timestamp = item.get("timestamp") if isinstance(item, dict) else ""
        if not timestamp:
            continue
        try:
            dt = parse_ts(timestamp)
        except (TypeError, ValueError):
            continue
        saw_dated_item = True
        if start and dt < start:
            continue
        if end and dt > end:
            continue
        return True
    return not saw_dated_item


def _endpoint_value(item: dict, names) -> str:
    for name in names:
        value = first_matching_value(item, {name.lower()})
        if value:
            return value
    return ""


def fetch_endpoints(base_url: str, api_key: str, project_id: str) -> list:
    """Fetch project endpoints for the endpoint dropdown."""
    base_url = _clean_base_url(base_url)
    if not base_url:
        raise CognigyError("Base URL is required.")
    if not api_key:
        raise CognigyError("API key is required.")
    if not project_id:
        raise CognigyError("Project ID is required.")

    url_candidates = [
        (f"{base_url}/v2.0/projects/{project_id}/endpoints", {"limit": 100}),
        (f"{base_url}/new/v2.0/endpoints", {"limit": 100, "projectId": project_id}),
    ]
    last_error = None

    for _, headers in _auth_candidates(api_key):
        for start_url, params in url_candidates:
            try:
                data = _request_json(start_url, headers, params)
            except CognigyError as e:
                last_error = e
                if e.status == 429:
                    raise
                continue

            endpoints = []
            for item in _extract_items(data):
                if not isinstance(item, dict):
                    continue
                endpoint_id = _endpoint_value(
                    item,
                    ("_id", "id", "endpointId", "endpoint_id", "referenceId", "endpointReference"),
                )
                name = _endpoint_value(
                    item,
                    ("name", "displayName", "endpointName", "urlName", "slug"),
                )
                type_name = _endpoint_value(item, ("type", "channel", "endpointType"))
                if endpoint_id or name:
                    endpoints.append({
                        "id": endpoint_id or name,
                        "name": name or endpoint_id,
                        "type": type_name,
                    })
            if endpoints:
                return sorted(endpoints, key=lambda e: (e["name"] or "").lower())

    if last_error:
        if last_error.status == 401:
            raise CognigyError(
                "Authentication failed (401) while loading endpoints. Check that "
                "the API key belongs to this NICE/Cognigy tenant.",
                401,
            )
        raise last_error
    return []


def _fetch_recent_logs_without_dates(
    base_url: str,
    api_key: str,
    project_id: str,
    limit: int,
    endpoint_id: str = "",
) -> list:
    """Fallback to the original CLI strategy: paginate recent logs without date params."""
    page_size = 25
    for _, endpoint_style, endpoint_value in _filter_variants("", "", endpoint_id):
        url_candidates = [
            ("classic", f"{base_url}/v2.0/projects/{project_id}/logs", _with_filters(
                {"limit": page_size}, "", "", endpoint_value, "from_to", endpoint_style or "endpointId",
            )),
            ("nice", f"{base_url}/new/v2.0/logs", _with_filters(
                {"limit": page_size, "projectId": project_id}, "", "", endpoint_value, "from_to", endpoint_style or "endpointId",
            )),
        ]
        for _, headers in _auth_candidates(api_key):
            for _, start_url, start_params in url_candidates:
                try:
                    first_data = _request_json(start_url, headers, start_params)
                    all_items = _collect_log_pages(first_data, headers, limit)
                except CognigyError as e:
                    if e.status == 429:
                        raise
                    continue
                if all_items:
                    all_items.sort(key=lambda x: x.get("timestamp", ""))
                    return all_items
    return []


def fetch_logs(
    base_url: str,
    api_key: str,
    project_id: str,
    limit: int = 2000,
    date_from: str = "",
    date_to: str = "",
    endpoint_id: str = "",
) -> list:
    """
    Fetch log entries from the Cognigy Logs API, paginating automatically.
    limit == 0 means fetch everything. Raises CognigyError on any failure.
    """
    base_url = _clean_base_url(base_url)

    if not base_url:
        raise CognigyError("Base URL is required.")
    if not api_key:
        raise CognigyError("API key is required.")
    if not project_id:
        raise CognigyError("Project ID is required.")

    page_size = 25
    last_error = None
    saw_successful_response = False
    ignored_date_candidate = None

    for date_style, endpoint_style, endpoint_value in _filter_variants(date_from, date_to, endpoint_id):
        url_candidates = [
            ("classic", f"{base_url}/v2.0/projects/{project_id}/logs", _with_filters(
                {"limit": page_size}, date_from, date_to, endpoint_value, date_style, endpoint_style or "endpointId",
            )),
            ("nice", f"{base_url}/new/v2.0/logs", _with_filters(
                {"limit": page_size, "projectId": project_id}, date_from, date_to, endpoint_value, date_style, endpoint_style or "endpointId",
            )),
        ]

        for _, headers in _auth_candidates(api_key):
            for _, start_url, start_params in url_candidates:
                try:
                    first_data = _request_json(start_url, headers, start_params)
                    saw_successful_response = True
                    first_items = _extract_items(first_data)
                    if first_items and not _items_overlap_date_filter(first_items, date_from, date_to):
                        if ignored_date_candidate is None:
                            ignored_date_candidate = (headers, first_data)
                        continue

                    all_items = _collect_log_pages(first_data, headers, limit)
                except CognigyError as e:
                    last_error = e
                    if e.status == 429:
                        raise
                    continue

                if all_items:
                    all_items.sort(key=lambda x: x.get("timestamp", ""))
                    return all_items

    if saw_successful_response:
        if date_from or date_to:
            recent_items = _fetch_recent_logs_without_dates(base_url, api_key, project_id, limit, endpoint_id)
            if recent_items:
                return recent_items
        if ignored_date_candidate:
            headers, first_data = ignored_date_candidate
            ignored_date_items = _collect_log_pages(first_data, headers, limit)
            ignored_date_items.sort(key=lambda x: x.get("timestamp", ""))
            return ignored_date_items
        return []

    if last_error:
        if last_error.status == 401:
            raise CognigyError(
                "Authentication failed (401). Tried X-API-Key and Authorization "
                "against both Cognigy log URL styles. Check that this key is an "
                "API key for this exact NICE/Cognigy tenant and has log-read access.",
                401,
            )
        raise last_error

    return []


# ── Filtering, grouping, latency ──────────────────────────────────────────────

def parse_ts(ts_str: str) -> datetime:
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_date_boundary(value: str, end_of_day: bool = False):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = datetime.combine(dt.date(), time.max if end_of_day else time.min, timezone.utc)
    return dt


def first_matching_value(data, names):
    if isinstance(data, dict):
        for key, value in data.items():
            key_l = key.lower()
            if key_l in names and value not in (None, ""):
                return str(value)
            found = first_matching_value(value, names)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = first_matching_value(value, names)
            if found:
                return found
    return ""


def filter_vg2(items: list) -> list:
    out = []
    for item in items:
        meta     = item.get("meta", {})
        ch       = str(meta.get("channel", "")).lower()
        trace_id = str(item.get("traceId", "")).lower()
        msg      = str(item.get("msg", "")).lower()
        source   = str(meta.get("source", "")).lower()
        if any(marker in " ".join([ch, trace_id, msg, source]) for marker in VG2_MARKERS):
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
    endpoint_id  = first_matching_value(
        entries,
        {"endpointid", "endpoint_id", "endpointreference", "endpointreferenceid"},
    )
    endpoint_name = first_matching_value(
        entries,
        {"endpointname", "endpoint_name", "endpoint", "urlname"},
    )

    first_ms = int((first_out_ts - inbound_ts).total_seconds() * 1000)
    full_ms  = int((last_out_ts  - inbound_ts).total_seconds() * 1000)

    return {
        "session_id":               inbound.get("meta", {}).get("sessionId", "unknown"),
        "trace_id":                 inbound.get("traceId", ""),
        "endpoint_id":              endpoint_id,
        "endpoint_name":            endpoint_name,
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


def sample_values(items: list, key_path, limit: int = 8) -> list:
    seen = []
    for item in items:
        value = item
        for key in key_path:
            value = value.get(key, {}) if isinstance(value, dict) else {}
        if value not in ({}, None, ""):
            text = str(value)
            if text not in seen:
                seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def diagnostics(items: list, all_turns: list, filters: dict, limit=None) -> str:
    vg2_items = filter_vg2(items)
    if limit == 0:
        fetch_label = "matching raw log entries"
    elif limit:
        fetch_label = f"most recent matching raw log entries (limit {limit})"
    else:
        fetch_label = "raw log entries"
    parts = [
        f"Fetched {len(items)} {fetch_label}.",
        f"Found {len(vg2_items)} possible VoiceGateway log entries.",
        f"Built {len(all_turns)} complete latency turn(s) before filters.",
    ]
    turn_dates = sorted(t["inbound_time"][:10] for t in all_turns if t.get("inbound_time"))
    if turn_dates:
        start, end = turn_dates[0], turn_dates[-1]
        if start == end:
            parts.append(f"Complete turns are dated {start}.")
        else:
            parts.append(f"Complete turns range from {start} to {end}.")
        requested_start = parse_date_boundary(filters.get("date_from") or "")
        requested_end = parse_date_boundary(filters.get("date_to") or "", end_of_day=True)
        if requested_start or requested_end:
            has_requested_turn = False
            for turn in all_turns:
                try:
                    inbound_dt = parse_ts(turn["inbound_time"])
                except (KeyError, TypeError, ValueError):
                    continue
                if requested_start and inbound_dt < requested_start:
                    continue
                if requested_end and inbound_dt > requested_end:
                    continue
                has_requested_turn = True
                break
            if not has_requested_turn:
                parts.append(
                    "The fetched logs are outside the requested date range, so "
                    "Cognigy likely ignored that date-filter style and returned "
                    "recent logs instead."
                )
    active = []
    for label, key in (
        ("date from", "date_from"),
        ("date to", "date_to"),
        ("endpoint", "endpoint"),
        ("session", "session_id"),
        ("trace", "trace_id"),
        ("text", "text"),
        ("tier", "tier"),
        ("min latency", "min_latency_ms"),
        ("max latency", "max_latency_ms"),
    ):
        value = filters.get(key)
        if value and value != "all":
            active.append(f"{label}={value}")
    if active:
        parts.append("Active filters: " + ", ".join(active) + ".")
    channels = sample_values(items, ("meta", "channel"))
    messages = sample_values(items, ("msg",))
    if channels:
        parts.append("Sample channels: " + ", ".join(channels) + ".")
    if messages:
        parts.append("Sample messages: " + " | ".join(messages[:4]) + ".")
    endpoint_filter = (filters.get("endpoint") or "").strip()
    if endpoint_filter and not all_turns:
        parts.append(
            "The endpoint exists, but no complete inbound/outbound VG2 turns were "
            "built from the fetched logs. Clear the endpoint filter first, then "
            "try the long endpoint ID from the WebSocket URL if results appear."
        )
    elif endpoint_filter:
        parts.append(
            "If this is only an endpoint-filter issue, clear the endpoint field "
            "or use the long endpoint ID from the WebSocket URL instead of the display name."
        )
    return " ".join(parts)


def _query_terms(value) -> list:
    if isinstance(value, list):
        values = value
    else:
        values = str(value or "").replace(",", " ").replace(";", " ").split()
    terms = []
    for value in values:
        term = str(value or "").strip().lower()
        if term and term not in terms:
            terms.append(term)
    return terms


def filter_turns(turns: list, filters: dict) -> list:
    session_terms = _query_terms(filters.get("session_ids") or filters.get("session_id"))
    endpoint_q = (filters.get("endpoint") or "").strip().lower()
    trace_q = (filters.get("trace_id") or "").strip().lower()
    text_q = (filters.get("text") or "").strip().lower()
    tier_q = (filters.get("tier") or "all").strip().lower()
    date_from = parse_date_boundary(filters.get("date_from") or "")
    date_to = parse_date_boundary(filters.get("date_to") or "", end_of_day=True)

    try:
        min_latency = int(filters.get("min_latency_ms") or 0)
    except (TypeError, ValueError):
        min_latency = 0
    try:
        max_latency = int(filters.get("max_latency_ms") or 0)
    except (TypeError, ValueError):
        max_latency = 0

    out = []
    for turn in turns:
        inbound_dt = parse_ts(turn["inbound_time"])
        if date_from and inbound_dt < date_from:
            continue
        if date_to and inbound_dt > date_to:
            continue
        if session_terms and not any(q in turn.get("session_id", "").lower() for q in session_terms):
            continue
        endpoint_blob = " ".join([
            turn.get("endpoint_id", ""),
            turn.get("endpoint_name", ""),
            turn.get("trace_id", ""),
        ]).lower()
        if endpoint_q and endpoint_q not in endpoint_blob:
            continue
        if trace_q and trace_q not in turn.get("trace_id", "").lower():
            continue
        if text_q:
            text_blob = " ".join([
                turn.get("user_text", ""),
                turn.get("first_bot_text", ""),
            ]).lower()
            if text_q not in text_blob:
                continue
        if tier_q in {"green", "yellow", "red"} and get_tier(turn["first_token_latency_ms"]) != tier_q:
            continue
        if min_latency and turn["first_token_latency_ms"] < min_latency:
            continue
        if max_latency and turn["first_token_latency_ms"] > max_latency:
            continue
        out.append(turn)
    return out


# ── Dashboard data assembly ────────────────────────────────────────────────────

def build_dashboard_data(turns: list, project_name: str, filters: dict) -> dict:
    """Turn the per-turn list into the JSON structure the frontend renders."""
    selected_endpoint_id = (filters.get("endpoint_id") or "").strip()
    selected_endpoint_name = (filters.get("endpoint_label") or "").strip()
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
                "trace_id":            t.get("trace_id", ""),
                "endpoint_id":         t.get("endpoint_id", "") or selected_endpoint_id,
                "endpoint_name":       t.get("endpoint_name", "") or selected_endpoint_name,
                "inbound_time":        t.get("inbound_time", ""),
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
        show_all_turns = bool(filters.get("show_all_turns"))
        session_data.append({
            "session_number":  s_num,
            "session_id":      session_id,
            "session_start":   s_rows[0]["inbound_time"],
            "turn_count":      len(out_turns),
            "avg_latency_ms":  int(sum(s_lats) / len(s_lats)),
            "peak_latency_ms": s_max,
            "tier":            get_tier(s_max),
            "tier_label":      tier_label(s_max),
            "turns":           out_turns,
            "display_turns":   out_turns if show_all_turns else flagged_turns,
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
        "filters":        filters,
        "selected_endpoint_id": selected_endpoint_id,
        "selected_endpoint_name": selected_endpoint_name,
        "sessions":       session_data,
    }


def compute(payload: dict) -> dict:
    """
    Top-level entry used by both backends.
    payload: { name, api_key, project_id, base_url, limit, filters }
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
        date_from=payload.get("date_from", ""),
        date_to=payload.get("date_to", ""),
        endpoint_id=payload.get("endpoint_id", ""),
    )
    all_turns = analyze_turns(items)
    filters = {
        "date_from":       payload.get("date_from", ""),
        "date_to":         payload.get("date_to", ""),
        "endpoint_id":     payload.get("endpoint_id", ""),
        "endpoint_label":  payload.get("endpoint_label", ""),
        "endpoint":        payload.get("endpoint", ""),
        "session_id":      payload.get("session_id", ""),
        "session_ids":     payload.get("session_ids", []),
        "trace_id":        payload.get("trace_id", ""),
        "text":            payload.get("text", ""),
        "tier":            payload.get("tier", "all"),
        "min_latency_ms":  payload.get("min_latency_ms", ""),
        "max_latency_ms":  payload.get("max_latency_ms", ""),
        "show_all_turns":  bool(payload.get("show_all_turns")),
    }
    turns = filter_turns(all_turns, filters)
    if not turns:
        raise CognigyError(
            "No voiceGateway2 turns matched the current filters. Clear filters, "
            "increase the log limit, or wait 1–2 minutes for new logs to appear. "
            + diagnostics(items, all_turns, filters, limit),
            200,
        )
    data = build_dashboard_data(turns, name, filters)
    data["raw_log_entries"] = len(items)
    data["log_limit"] = limit
    data["analyzed_turns_before_filters"] = len(all_turns)
    return data
