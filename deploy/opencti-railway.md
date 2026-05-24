# OpenCTI on Railway (Path Y — voice-driven on-demand)

This supersedes [`opencti.md`](opencti.md) (Path B — laptop Docker). The
laptop bridge + `http_proxy` handler stay in place for any future
localhost service, but OpenCTI itself now lives on Railway with a
public URL, spun up and down by voice command via GitHub Actions
`workflow_dispatch`.

```
   "Activate Global Eyes"
            │
   ┌────────▼────────┐    workflow_dispatch    ┌──────────────────────┐
   │ LiveKit worker  │ ──────────────────────► │ GitHub Actions       │
   │ GitHubDispatch  │                          │ opencti-lifecycle.yml│
   │ Client          │                          │ → railway_schedule.py│
   └────────┬────────┘                          └──────────┬───────────┘
            │                                              │ GraphQL
            │ polls /graphql until healthy                 ▼
            │ (~3 min cold start)             ┌────────────────────────┐
            │                                  │ Railway project        │
            │                                  │   divine-contentment   │
            │ on healthy:                      │   • opencti            │
            │   speak "Global Eyes are         │   • opencti-worker     │
            │     online, sir."                │   • elasticsearch      │
            │   open cti widget (iframe)       │   • redis              │
            │   start idle watchdog (3 min)    │   • rabbitmq           │
            │                                  │   • minio              │
            ▼                                  └────────────────────────┘
   ┌─────────────────┐                                     ▲
   │ HUD cti widget  │ ◄────────── iframe (HTTPS) ─────────┘
   └─────────────────┘
```

## One-time setup (do once; everything after is voice-driven)

### Step 1 — Create the 5 OpenCTI services in Railway

In the `divine-contentment` project, create one service per row below.
**Easiest path:** use the OpenCTI official `docker-compose.yml` as a
reference and add each service in Railway's UI ("New Service → Docker
Image"). The exact image versions track OpenCTI release; pin to a known
stable.

| Service name (case-sensitive) | Image | Volume mount |
|---|---|---|
| `opencti` | `opencti/platform:6.4.7` | — |
| `opencti-worker` | `opencti/worker:6.4.7` | — |
| `elasticsearch` | `docker.elastic.co/elasticsearch/elasticsearch:8.11.0` | `/usr/share/elasticsearch/data` |
| `redis` | `redis:7-alpine` | — |
| `rabbitmq` | `rabbitmq:3-management` | — |
| `minio` | `minio/minio:latest` (start command: `server /data`) | `/data` |

> **Why these exact names?** The new GitHub workflow file
> [`Jarvis/.github/workflows/opencti-lifecycle.yml`](../../Jarvis/.github/workflows/opencti-lifecycle.yml)
> targets the service list:
> `opencti,opencti-worker,elasticsearch,redis,rabbitmq,minio`. If you
> rename anything in Railway, update the `--services` line in that
> workflow to match.

### Step 2 — Paste env vars (use the secrets I generated earlier)

| Service | Env var | Value |
|---|---|---|
| `opencti` | `NODE_OPTIONS` | `--max-old-space-size=8096` |
| `opencti` | `APP__PORT` | `8080` |
| `opencti` | `APP__BASE_URL` | *fill after Step 3* |
| `opencti` | `APP__SESSION_COOKIE` | `false` |
| `opencti` | `APP__PUBLIC_DASHBOARD_AUTHORIZED_DOMAINS` | `openjarvis-production-*.up.railway.app,friday-jarvis2-production-*.up.railway.app` |
| `opencti` | `OPENCTI_ADMIN_EMAIL` | `gelson_m@hotmail.com` |
| `opencti` | `OPENCTI_ADMIN_PASSWORD` | `LdxdGRhMkbwV8KcUiwsSRzYJ` |
| `opencti` | `OPENCTI_ADMIN_TOKEN` | `e644bf12-cfc0-4844-a21d-5add52ae99bc` |
| `opencti` | `ELASTICSEARCH__URL` | `http://${{elasticsearch.RAILWAY_PRIVATE_DOMAIN}}:9200` |
| `opencti` | `REDIS__HOSTNAME` | `${{redis.RAILWAY_PRIVATE_DOMAIN}}` |
| `opencti` | `REDIS__PORT` | `6379` |
| `opencti` | `RABBITMQ__HOSTNAME` | `${{rabbitmq.RAILWAY_PRIVATE_DOMAIN}}` |
| `opencti` | `RABBITMQ__PORT` | `5672` |
| `opencti` | `RABBITMQ__USERNAME` | `opencti` |
| `opencti` | `RABBITMQ__PASSWORD` | `cpaarQaagtw7PfthweOZRCo1` |
| `opencti` | `MINIO__ENDPOINT` | `${{minio.RAILWAY_PRIVATE_DOMAIN}}` |
| `opencti` | `MINIO__PORT` | `9000` |
| `opencti` | `MINIO__ACCESS_KEY` | `minioadmin` |
| `opencti` | `MINIO__SECRET_KEY` | `rGSy7CBxgDl4OHw6q2C7nWEC` |
| `opencti-worker` | `OPENCTI_URL` | `http://${{opencti.RAILWAY_PRIVATE_DOMAIN}}:8080` |
| `opencti-worker` | `OPENCTI_TOKEN` | `e644bf12-cfc0-4844-a21d-5add52ae99bc` |
| `opencti-worker` | `WORKER_LOG_LEVEL` | `info` |
| `elasticsearch` | `discovery.type` | `single-node` |
| `elasticsearch` | `xpack.security.enabled` | `false` |
| `elasticsearch` | `ES_JAVA_OPTS` | `-Xms2g -Xmx2g` |
| `elasticsearch` | `thread_pool.search.queue_size` | `5000` |
| `rabbitmq` | `RABBITMQ_DEFAULT_USER` | `opencti` |
| `rabbitmq` | `RABBITMQ_DEFAULT_PASS` | `cpaarQaagtw7PfthweOZRCo1` |
| `minio` | `MINIO_ROOT_USER` | `minioadmin` |
| `minio` | `MINIO_ROOT_PASSWORD` | `rGSy7CBxgDl4OHw6q2C7nWEC` |

### Step 3 — Generate the public domain on `opencti`

In Railway: open the `opencti` service → Settings → Networking →
**Generate Domain**. Copy the resulting URL (e.g.
`https://opencti-production-a3f9.up.railway.app`) and:

1. Paste it back into `opencti`'s `APP__BASE_URL` env var.
2. **Trigger one deploy** to pick up that value, then immediately
   **Remove the Deployment** so the platform sits at zero cost.

### Step 4 — Immediately Remove Deployment on all 5 services

For each service: Deployments tab → **⋮ → Remove**. Confirms the
"$0/mo when idle" cost baseline. The workflow will redeploy them on
the next voice command.

### Step 5 — Wire env vars on the OpenJarvis services (Railway)

| Service | Env var | Value |
|---|---|---|
| OpenJarvis backend | `OPENCTI_URL` | the Railway URL from Step 3 |
| OpenJarvis backend | `OPENCTI_TOKEN` | `e644bf12-cfc0-4844-a21d-5add52ae99bc` |
| OpenJarvis worker | `OPENCTI_URL` | same |
| OpenJarvis worker | `OPENCTI_TOKEN` | same |
| OpenJarvis worker | `GITHUB_DISPATCH_TOKEN` | **new PAT** (see Step 6) |
| OpenJarvis worker | `GITHUB_DISPATCH_OWNER` | `gelson12` |
| OpenJarvis worker | `GITHUB_DISPATCH_REPO` | `friday_jarvis2` |
| OpenJarvis worker | `GITHUB_DISPATCH_WORKFLOW` | `opencti-lifecycle.yml` |
| OpenJarvis worker | `GITHUB_DISPATCH_REF` | `main` (optional, default) |
| OpenJarvis frontend | `VITE_OPENCTI_URL` | the Railway URL (build-time — redeploy frontend after) |

### Step 6 — Generate a GitHub PAT

<https://github.com/settings/tokens/new>

- **Note:** "Jarvis OpenCTI lifecycle"
- **Expiration:** 1 year (rotate when it expires)
- **Scopes:** `repo` + `workflow`
- Generate token → copy → paste as `GITHUB_DISPATCH_TOKEN` on the
  OpenJarvis worker service.

### Step 7 — Confirm the workflow exists

The new workflow [`opencti-lifecycle.yml`](../../Jarvis/.github/workflows/opencti-lifecycle.yml)
ships in this same commit on the Jarvis repo. After push it's at
<https://github.com/gelson12/friday_jarvis2/actions/workflows/opencti-lifecycle.yml> —
you can also manually trigger it from there to smoke-test (Step 8).

### Step 8 — Smoke test

1. **Manual workflow run:** GitHub → Actions → "OpenCTI lifecycle
   (on-demand)" → Run workflow → `action: start`. After ~3 min, all 5
   services in Railway show "Active". Visit your OpenCTI URL — log in
   with `gelson_m@hotmail.com` / `LdxdGRhMkbwV8KcUiwsSRzYJ`.
2. **Same workflow with `action: stop`** → all 5 services back to
   "Removed" within ~30 s.
3. **Voice activate:** *"Hey Jarvis, activate Global Eyes."* → worker
   speaks the ack → ~3 min later speaks *"Global Eyes are online, sir."*
   + CTI widget mounts.
4. **Voice CTI op:** *"Hey Jarvis, log foo.com as a suspicious domain."*
   → spoken confirmation → entity visible in OpenCTI UI's Observables.
5. **Voice spindown:** *"Hey Jarvis, stand down Global Eyes."* → widget
   closes + Railway services back to "Removed" within ~30 s.
6. **Idle watchdog:** activate Global Eyes, then sit silent. After
   ~3 minutes, Jarvis spontaneously says: *"Global Eyes has been idle
   for over three minutes, sir — powering down to save computational
   resources."* and tears the stack down.
7. **Auto-spin-up:** with services stopped, ask
   *"Hey Jarvis, what came in today?"* → worker says *"Bringing Global
   Eyes online first, sir — one moment."* → after ~3 min answers the
   actual question + opens the widget.

## Cost expectation

| State | Cost |
|---|---|
| All 5 services in "Removed" | ~$0.05/mo (volume only) |
| All 5 services running | ~$0.40/hr pro-rated |
| Realistic monthly bill at 30 min/day usage | ~$6/mo |
| Realistic monthly bill at 1 hr/day usage | ~$12/mo |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Voice ack but never speaks "online" | Workflow failed | Check Actions tab; verify `RAILWAY_TOKEN` secret + service names |
| Workflow run = "Resource not accessible by integration" | GH PAT missing `workflow` scope | Regenerate with both `repo` + `workflow` |
| Spinup succeeds but CTI ops always fail with HTTP 401 | `OPENCTI_TOKEN` mismatch | Make sure the same UUID is in `opencti.OPENCTI_ADMIN_TOKEN` AND in the OpenJarvis worker/backend `OPENCTI_TOKEN` |
| Iframe shows OpenCTI login wall on phone | First-time login required | Log into OpenCTI once on that browser; session cookie persists |
| `Global Eyes didn't come up in time, sir` | 7-min healthy probe deadline exceeded | Cold start sometimes takes longer; redeploy `elasticsearch` first; bump `_CTI_HEALTHY_DEADLINE` if persistent |
| Idle watchdog firing too aggressively | Default is 3 min from last CTI op | Edit `_CTI_IDLE_SECONDS` in `livekit/worker.py` |

## Out of scope (still)

- **Custom OpenCTI connectors** (MITRE ATT&CK / CISA KEV / VirusTotal
  auto-ingest). Each is a separate Railway service with its own image.
  Add after the lifecycle proves stable.
- **SSO between Jarvis ↔ OpenCTI.** The public-dashboards whitelist
  covers the embed case; full OIDC is a later hardening step.
- **Configurable idle threshold via voice.** Constant for now; edit
  `_CTI_IDLE_SECONDS` and redeploy worker.
- **Friday_Jarvis2 mirror.** Same diff in the other repo once
  OpenJarvis path is proven.
