# VoiceGateway2 Latency Calculator — Web App

A deployable web version of the VG2 latency QA tool. End users open the page,
paste their own Cognigy `base_url`, `project_id`, and `api_key`, and get a live
latency dashboard — first-token latency per turn, graded green / yellow / red,
grouped by session, with CSV export.

It deploys to **Vercel** or **Azure Static Web Apps** with no code changes.

---

## What it measures

**First-token latency** — the time between when the caller finishes speaking
("Received message from user" in the Cognigy logs) and when the bot's first
output leaves Cognigy ("Sent output to Endpoint"). This is the Cognigy-side
processing time the caller perceives as the pause. It does **not** include
speech-to-text finalization, text-to-speech synthesis, or telephony network
time — those happen in the voice gateway, outside Cognigy's logs.

Formula:

```
first-token latency = first "Sent output to Endpoint" timestamp
                    - matching "Received message from user" timestamp
```

Events are matched by `traceId`. The full-response latency shown per turn uses
the last output segment for that same trace.

| Tier | Range | Meaning |
|---|---|---|
| 🟢 Acceptable | < 2,500 ms | Normal |
| 🟡 Degraded | 2,500–5,000 ms | Noticeable delay |
| 🔴 Unacceptable | > 5,000 ms | Action required |

## Filters

The dashboard can load project endpoints from Cognigy and sends date plus
selected endpoint filters into the initial log request before pagination. This
keeps large historical reports from fetching unnecessary logs. After the
filtered log set is loaded, the remaining report filters are applied locally.
Available filters:

- Date from / date to — sent to Cognigy before logs are fetched
- Cognigy endpoint — loaded from Cognigy and sent before logs are fetched
- Endpoint text search — local search inside the loaded report
- Session ID search
- Trace ID search
- User/bot transcript text search
- Latency tier
- Minimum / maximum first-token latency
- Show all matching turns or only slow/degraded turns

## Report downloads

After generating a report, the dashboard can export the currently filtered
view as:

- PDF — opens a print-ready report that can be saved as PDF.
- Excel — downloads an Excel-readable `.xls` workbook with summary and turn
  detail tables.
- CSV — downloads raw filtered turn rows for analysis.

---

## How it works

```
Browser (index.html)  ──POST /api/latency──▶  Backend  ──▶  Cognigy Logs API
   form + dashboard         (credentials)      (_core.py)     (server-side, no CORS)
```

The user's API key is sent over HTTPS to the backend for that one request, used
to call Cognigy, and never stored or logged. Frontend and backend share an
origin on both platforms, so there is no CORS configuration.

The two platforms run the backend differently, so each has its own thin
entrypoint, but the latency logic in `_core.py` is identical:

- **Vercel** — a single Flask app (`app.py`) serves both the page and the API.
  Vercel's Python runtime auto-detects `app.py` as a Flask framework app.
- **Azure** — Static Web Apps serves `index.html` statically and routes
  `/api/*` to an Azure Function (`azure_api/function_app.py`).

```
latency-web/
├── index.html                 — frontend: credentials form + dashboard (shared)
├── app.py                     — Vercel entrypoint: Flask app (page + /api/latency)
├── _core.py                   — fetch + latency logic (shared core)
├── requirements.txt           — Vercel deps (flask, requests)
├── vercel.json                — Vercel config
├── azure_api/
│   ├── function_app.py        — Azure Functions handler
│   ├── _core.py               — identical copy of the shared core
│   ├── host.json
│   └── requirements.txt
├── staticwebapp.config.json   — Azure SWA routing + Python runtime
└── .github/workflows/azure-static-web-apps.yml
```

> **Note:** the root `_core.py` and `azure_api/_core.py` are intentionally
> identical. Each platform packages only its own files, so the core is
> duplicated rather than imported across folders. If you edit one, copy it to
> the other: `cp _core.py azure_api/_core.py`

---

## Deploy to Vercel

1. Install the CLI: `npm i -g vercel`
2. From this folder: `vercel` (follow prompts), then `vercel --prod`

Vercel auto-detects `app.py` as a Flask app (deps from `requirements.txt`) and
serves the page at `/` and the API at `/api/latency`. No build step.

To deploy from the Vercel dashboard instead: import the Git repo and set the
**Root Directory** to `cognigy-tools-/latency-web`.

---

## Deploy to Azure Static Web Apps

1. In the Azure portal, create a **Static Web App**, linked to your GitHub repo.
2. When prompted for build details, set:
   - **App location:** `cognigy-tools-/latency-web`
   - **Api location:** `cognigy-tools-/latency-web/azure_api`
   - **Output location:** *(leave empty)*
3. Azure adds a deploy token to your repo secrets as
   `AZURE_STATIC_WEB_APPS_API_TOKEN`. The included workflow
   (`.github/workflows/azure-static-web-apps.yml`) handles CI/CD on push to
   `main`. Adjust the paths in it if you move the folder.

Azure's managed Python API serves the function at `/api/latency`, the same path
the frontend calls.

---

## Run locally

**Vercel (Flask app directly — simplest):**
```bash
pip install -r requirements.txt
python app.py
# open http://localhost:3000
```
or with the Vercel CLI: `vercel dev`

**Azure:**
```bash
# Frontend: serve index.html with any static server, e.g.
python3 -m http.server 8000
# Backend (separate terminal):
cd azure_api
pip install -r requirements.txt
func start            # requires Azure Functions Core Tools
```
For local Azure dev you may need to point the frontend's fetch at the Functions
port, or use the SWA CLI (`swa start . --api-location azure_api`) which proxies
`/api` for you.

---

## Security notes

- Each user supplies their own Cognigy API key; nothing is hard-coded.
- The key is used per-request and never persisted server-side.
- The key field is a password input and forms use `autocomplete="off"`.
- Anyone with the URL can use the tool, but they can only see data for a
  Cognigy project they already have an API key for. If you need to lock the page
  down, add platform auth (Vercel Password Protection / Azure SWA auth roles).
