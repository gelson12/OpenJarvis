# Deploying OpenCTI on YOUR LAPTOP (Jarvis intel layer)

This is the "Path B — bridge-proxy" deployment chosen 2026-05-24. OpenCTI
runs **locally on the laptop in Docker**. The cloud OpenJarvis worker +
backend talk to it by proxying every HTTP call over the **existing**
LiveKit data channel that desktop-bridge already listens on — no public
URL, no tunnel, no extra Railway services.

## Why this works (the trick)

The desktop-bridge gained a single new handler — `http_proxy` — that
takes `{base_url, method, path, headers, body, timeout}` and runs the
HTTP request against the laptop's `localhost`. The cloud side talks to
OpenCTI as if it were on a public URL; the bridge transparently
shuttles each request to `http://localhost:8080` and back.

For the iframe in the Jarvis HUD: modern browsers treat
`http://localhost` as a **Secure Context**, so an HTTPS-served Jarvis
frontend can iframe `http://localhost:8080` without mixed-content
blocking — as long as the browser is on the same machine as OpenCTI
(the laptop).

```
                       ┌────────────────────────┐
                       │  laptop (Docker)        │
                       │   opencti          :8080│ ◄──┐
                       │   elasticsearch    :9200│    │ localhost
                       │   redis            :6379│    │ HTTP
                       │   rabbitmq         :5672│    │
                       │   minio            :9000│    │
                       │   desktop-bridge.py     │ ───┘
                       └───────────┬────────────┘
                                   │ outbound only
                              jarvis-control
                                LiveKit room
                                   │
              ┌────────────────────┴────────────────────┐
              │                                          │
   OpenJarvis backend (Railway)             OpenJarvis worker (Railway)
   _OpenCTIClient.proxy_send()              OpenCTIClient via DesktopBridge
              │                                          │
              └─────────────────────┬────────────────────┘
                                    │
                            laptop browser
                            iframes http://localhost:8080
                                    │
                             cti widget in HUD
```

## Step 1 — install Docker Desktop on the laptop

If you already have it, skip. Otherwise:
<https://www.docker.com/products/docker-desktop/>. Sign in / launch
Docker Desktop. Verify with:

```powershell
docker --version
docker compose version
```

## Step 2 — fetch OpenCTI's official compose

Pin to a tagged release (avoid `:latest` so an upstream change can't
break you on a restart):

```powershell
cd C:\Users\Gelson\Downloads
git clone --depth 1 --branch 6.4.7 https://github.com/OpenCTI-Platform/docker.git opencti-docker
cd opencti-docker
copy .env.sample .env
```

## Step 3 — fill in `.env`

Open `.env` in your editor. The values you already generated:

```env
OPENCTI_ADMIN_EMAIL=gelson_m@hotmail.com
OPENCTI_ADMIN_PASSWORD=LdxdGRhMkbwV8KcUiwsSRzYJ
OPENCTI_ADMIN_TOKEN=e644bf12-cfc0-4844-a21d-5add52ae99bc
OPENCTI_BASE_URL=http://localhost:8080
OPENCTI_HEALTHCHECK_ACCESS_KEY=e644bf12-cfc0-4844-a21d-5add52ae99bc

# Sidecars
RABBITMQ_DEFAULT_USER=opencti
RABBITMQ_DEFAULT_PASS=cpaarQaagtw7PfthweOZRCo1
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=rGSy7CBxgDl4OHw6q2C7nWEC
ELASTIC_MEMORY_SIZE=2G
CONNECTOR_HISTORY_ID=$(uuidgen)
CONNECTOR_EXPORT_FILE_STIX_ID=$(uuidgen)
CONNECTOR_EXPORT_FILE_CSV_ID=$(uuidgen)
CONNECTOR_IMPORT_FILE_STIX_ID=$(uuidgen)
CONNECTOR_IMPORT_DOCUMENT_ID=$(uuidgen)
SMTP_HOSTNAME=
```

For the `CONNECTOR_*_ID` lines that use `$(uuidgen)`: PowerShell can't
expand that. Replace each line with a fresh UUID — same generator we
used before:

```powershell
1..5 | ForEach-Object { [guid]::NewGuid().ToString() }
```

Paste the five UUIDs into the `CONNECTOR_*_ID` lines (one per line).

## Step 4 — start it

```powershell
docker compose pull
docker compose up -d
```

First pull is ~3 GB and 5–10 minutes. After it returns, give the
platform another ~60–90 s to finish bootstrap, then check:

```powershell
docker compose ps
# All services should show "Up" and the opencti row "(healthy)"

# Browser smoke test:
start http://localhost:8080
# Log in: gelson_m@hotmail.com / LdxdGRhMkbwV8KcUiwsSRzYJ
```

## Step 5 — confirm the desktop-bridge is up on the laptop

The bridge-proxy needs the existing desktop-bridge running. Confirm
either by checking Task Scheduler (`Get-ScheduledTask JarvisDesktopBridge`)
or by looking for the python+wscript processes from the earlier setup.
If it isn't running, start it via the scheduled task:

```powershell
Start-ScheduledTask -TaskName JarvisDesktopBridge
```

## Step 6 — wire env vars on the Railway OpenJarvis services

The token + URL are now **internal** to the laptop, but the cloud needs
to know how to address them through the bridge.

### OpenJarvis backend (Railway service)

| Variable | Value |
|---|---|
| `OPENCTI_URL` | `http://localhost:8080` |
| `OPENCTI_TOKEN` | `e644bf12-cfc0-4844-a21d-5add52ae99bc` |
| `OPENCTI_BRIDGE_MACHINE` | `laptop` |
| `LIVEKIT_URL` | *(should already be set for the existing desktop-control tool)* |
| `LIVEKIT_API_KEY` | *(ditto)* |
| `LIVEKIT_API_SECRET` | *(ditto)* |
| `JARVIS_CONTROL_ROOM` | `jarvis-control` *(default — only set if you changed it)* |

### OpenJarvis livekit worker (Railway service)

| Variable | Value |
|---|---|
| `OPENCTI_URL` | `http://localhost:8080` |
| `OPENCTI_TOKEN` | `e644bf12-cfc0-4844-a21d-5add52ae99bc` |
| `OPENCTI_BRIDGE_MACHINE` | `laptop` |

(LiveKit env is already wired here — it's the agent's runtime.)

### OpenJarvis frontend (Railway service)

| Variable | Value |
|---|---|
| `VITE_OPENCTI_URL` | `http://localhost:8080` |

Vite bakes this at **build time** — trigger a redeploy after setting.

Redeploy backend + worker + frontend, in any order.

## Step 7 — voice smoke test

Wake Jarvis, then try in this order:

1. *"Hey Jarvis, open the intel panel"*
   → CTI widget appears on the HUD with the OpenCTI dashboard rendered
   inside (you'll see the page you'd see at <http://localhost:8080>).
2. *"Hey Jarvis, log foo.com as a suspicious domain"*
   → spoken *"Logged foo.com as a domain, sir."* → refresh the
   OpenCTI Observables view, the new domain is there.
3. *"Hey Jarvis, what came in today?"*
   → spoken count of new objects in the last 24 hours.

If voice says *"OpenCTI hiccup, sir — the 'laptop' bridge is offline"*,
restart the scheduled task. If it says *"OpenCTI HTTP 401"*, double-
check the `OPENCTI_TOKEN` matches on both sides.

## Cost — actually free now

| Item | Cost |
|---|---|
| Docker Desktop | $0 (personal use) |
| OpenCTI software | $0 (AGPL) |
| Laptop electricity for the containers | a few watts; rounding error |
| **Additional Railway spend** | **$0** — no new Railway services |

## Out of scope

- **Connectors** (auto-ingest from MITRE ATT&CK / CISA KEV / AlienVault
  / VirusTotal / etc.). The official compose already includes the basic
  export/import connectors — add more by uncommenting the relevant
  `connector-*` services in `docker-compose.yml`. Each has its own
  `CONNECTOR_*_ID` (run the UUID generator again per new connector).
- **Public access to OpenCTI from your phone / the ROG.** By design,
  Path B keeps OpenCTI laptop-only. If you later want it reachable
  elsewhere, add Tailscale Funnel on top — the bridge-proxy continues
  to work unchanged.
- **Persisting the OpenCTI data across laptop wipes.** Docker volumes
  in the official compose handle this — `docker compose down` keeps
  data, `docker compose down -v` wipes it.
