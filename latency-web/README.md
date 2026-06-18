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

| Tier | Range | Meaning |
|---|---|---|
| 🟢 Acceptable | < 2,500 ms | Normal |
| 🟡 Degraded | 2,500–5,000 ms | Noticeable delay |
| 🔴 Unacceptable | > 5,000 ms | Action required |

---

## How it works

```
Browser (index.html)  ──POST /api/latency──▶  Serverless function  ──▶  Cognigy Logs API
   form + dashboard         (credentials)         (_core.py)              (server-side, no CORS)
```

The user's API key is sent over HTTPS to the serverless function for that one
request, used to call Cognigy, and never stored or logged. Frontend and API
share an origin on both platforms, so there is no CORS configuration.

```
latency-web/
├── index.html                 — frontend: credentials form + dashboard (shared)
├── api/
│   ├── latency.py             — Vercel serverless handler
│   └── _core.py               — fetch + latency logic (shared core)
├── azure_api/
│   ├── function_app.py        — Azure Functions handler
│   ├── _core.py               — identical copy of the shared core
│   ├── host.json
│   └── requirements.txt
├── requirements.txt           — Vercel Python deps
├── vercel.json                — Vercel config
├── staticwebapp.config.json   — Azure SWA routing + Python runtime
└── .github/workflows/azure-static-web-apps.yml
```

> **Note:** `api/_core.py` and `azure_api/_core.py` are intentionally identical.
> Each platform packages only its own backend folder, so the core is duplicated
> rather than imported across folders. If you edit one, copy it to the other:
> `cp api/_core.py azure_api/_core.py`

---

## Deploy to Vercel

1. Install the CLI: `npm i -g vercel`
2. From this folder: `vercel` (follow prompts), then `vercel --prod`

Vercel auto-detects the static `index.html` and the Python function at
`api/latency.py` (Python deps come from `requirements.txt`). No build step.

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

**Vercel:**
```bash
npm i -g vercel
vercel dev
# open http://localhost:3000
```

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
