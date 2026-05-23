# Deploying OpenCTI on Railway (Jarvis intel layer)

This is the deployment side of [Layer 1 of the OpenCTI integration plan](../).
OpenCTI is a vendor-grade platform — **we do not fork it**, we just deploy
the official containers and point Jarvis at them via env vars.

## What you're deploying

OpenCTI is a small constellation of services, not one container. The
official spec is at <https://github.com/OpenCTI-Platform/docker>. On
Railway, model each as a separate service in one project:

| Service | Image | Purpose |
|---|---|---|
| `opencti` | `opencti/platform:latest` | GraphQL API + Web UI (the one with a public hostname) |
| `worker` | `opencti/worker:latest` | Async ingest workers (run 2–3 replicas) |
| `connector-*` | `opencti/connector-*:latest` | Optional: import sources (MITRE ATT&CK, MISP feeds, CISA, AlienVault, etc.) |
| `elasticsearch` | `docker.elastic.co/elasticsearch/elasticsearch:8.x` | Knowledge graph storage |
| `redis` | `redis:7-alpine` | Live event stream |
| `rabbitmq` | `rabbitmq:3-management` | Worker job queue |
| `minio` | `minio/minio:latest` | File attachments / S3-compatible storage |

Resource sizing on Railway (minimums — bump if ingest is heavy):

- `opencti` platform: **2 vCPU / 4 GB RAM** (Node sets `--max-old-space-size`)
- `elasticsearch`: **2 vCPU / 4 GB RAM**, persistent volume
- `redis`: 1 GB RAM
- `rabbitmq`: 1 GB RAM
- `minio`: 256 MB + persistent volume

## Step 1 — generate the secrets

Run these once and **save the outputs in your secrets manager**:

```bash
# Admin login + API token. The token is what Jarvis uses to call GraphQL.
python -c "import uuid; print('OPENCTI_ADMIN_TOKEN=' + str(uuid.uuid4()))"

# A signing key for the platform.
python -c "import uuid; print('APP__ADMIN__TOKEN=' + str(uuid.uuid4()))"
```

Generate also: `RABBITMQ_DEFAULT_PASS`, `MINIO_ROOT_PASSWORD`, `ELASTIC_PASSWORD`
(strong randoms, 32+ chars).

## Step 2 — `opencti` platform service env

```env
NODE_OPTIONS=--max-old-space-size=8096
APP__PORT=8080
APP__BASE_URL=https://opencti-production-<your-id>.up.railway.app
APP__BASE_PATH=
APP__SESSION_COOKIE=false

# First-run admin. Email becomes your username.
OPENCTI_ADMIN_EMAIL=you@example.com
OPENCTI_ADMIN_PASSWORD=<strong>
OPENCTI_ADMIN_TOKEN=<uuid from step 1>

# Iframe embedding from the Jarvis frontends.  Replace with the real
# Railway hostnames once OpenJarvis + Friday_Jarvis2 are deployed.
APP__PUBLIC_DASHBOARD_AUTHORIZED_DOMAINS=openjarvis-production-*.up.railway.app,friday-jarvis2-production-*.up.railway.app

# Plumbing — point at the sibling Railway services
ELASTICSEARCH__URL=http://${{Elasticsearch.RAILWAY_PRIVATE_DOMAIN}}:9200
REDIS__HOSTNAME=${{Redis.RAILWAY_PRIVATE_DOMAIN}}
REDIS__PORT=6379
RABBITMQ__HOSTNAME=${{RabbitMQ.RAILWAY_PRIVATE_DOMAIN}}
RABBITMQ__PORT=5672
RABBITMQ__USERNAME=opencti
RABBITMQ__PASSWORD=<rabbitmq pass>
MINIO__ENDPOINT=${{MinIO.RAILWAY_PRIVATE_DOMAIN}}
MINIO__PORT=9000
MINIO__ACCESS_KEY=minioadmin
MINIO__SECRET_KEY=<minio root pass>
```

Set the Railway service to **expose port 8080** publicly.

## Step 3 — `worker` service env

Use the same image (`opencti/worker:latest`). Scale to 2–3 replicas.

```env
OPENCTI_URL=http://${{opencti.RAILWAY_PRIVATE_DOMAIN}}:8080
OPENCTI_TOKEN=<same OPENCTI_ADMIN_TOKEN>
WORKER_LOG_LEVEL=info
```

## Step 4 — sibling stores

- **Elasticsearch**: official image; set `discovery.type=single-node`,
  `xpack.security.enabled=false` (private network only), give it a
  persistent volume on `/usr/share/elasticsearch/data`.
- **Redis**: official image; default config is fine.
- **RabbitMQ**: set `RABBITMQ_DEFAULT_USER=opencti` and
  `RABBITMQ_DEFAULT_PASS=<your pass>`.
- **MinIO**: set `MINIO_ROOT_USER=minioadmin`,
  `MINIO_ROOT_PASSWORD=<your pass>`; mount a volume on `/data`; pass
  `server /data` as the command.

## Step 5 — point Jarvis at OpenCTI

On the **OpenJarvis backend** Railway service:

| Var | Value |
|---|---|
| `OPENCTI_URL` | `https://opencti-production-<id>.up.railway.app` |
| `OPENCTI_TOKEN` | the `OPENCTI_ADMIN_TOKEN` you generated above |

On the **OpenJarvis livekit worker** service (same project, different
service): set the **same two vars**.

On the **OpenJarvis frontend** Railway service:

| Var | Value |
|---|---|
| `VITE_OPENCTI_URL` | `https://opencti-production-<id>.up.railway.app` |

(Vite vars are baked at build time — redeploy the frontend after setting
this.)

## Step 6 — smoke test

After the OpenCTI platform comes up (give it 60–90 s on first boot):

```bash
# 1. Health
curl -sf https://opencti-production-<id>.up.railway.app/health
# → expects 200

# 2. Token works
curl -sH "Authorization: Bearer $OPENCTI_TOKEN" \
  -X POST https://opencti-production-<id>.up.railway.app/graphql \
  -H "Content-Type: application/json" \
  -d '{"query":"{ me { name user_email }}"}'
# → returns your admin user
```

Once OpenJarvis is redeployed with the new env vars, try the voice
loop:

- *"Hey Jarvis, what came in today?"* → spoken count of new objects (will
  be 0 on a fresh deploy until you wire connectors).
- *"Hey Jarvis, log foo.com as a suspicious domain"* → spoken "Logged,
  sir." and an Observable in the OpenCTI UI's Observables view.
- *"Hey Jarvis, open the intel panel"* → CTI widget appears on the HUD
  with the OpenCTI UI inside.

## Out of scope (deliberate)

- **Connectors** (ingest from MITRE ATT&CK / MISP / AlienVault OTX / CISA
  KEV / VirusTotal / etc.): follow up once the platform is live. Each
  connector is a separate Railway service running an
  `opencti/connector-<name>` image with its own env. Start with
  `connector-mitre` and `connector-cve` — they're zero-config.
- **SSO via OIDC**: harden later. Until then, the iframe embed uses
  OpenCTI's public-dashboards whitelist + a persistent session cookie.
- **Backup of Elasticsearch / MinIO**: set up Railway snapshots on the
  attached volumes before you trust it with real data.
