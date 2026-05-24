# OpenCTI connectors — 7-pack OSINT setup

Companion to [`opencti-railway.md`](opencti-railway.md). Each connector
is a small Docker service you add to the same `divine-contentment`
Railway project. The lifecycle workflow
[`Jarvis/.github/workflows/opencti-lifecycle.yml`](../../Jarvis/.github/workflows/opencti-lifecycle.yml)
spins them up / down with the core stack.

## What each connector gives you

| Connector | Service name in Railway | What it ingests | Type |
|---|---|---|---|
| **MITRE ATT&CK** | `connector-mitre` | Threat actors, intrusion sets, malware, techniques (TTPs) | external-import (scheduled) |
| **CISA KEV** | `connector-cisa-kev` | Known Exploited Vulnerabilities (CVEs the US gov says are actively exploited) | external-import |
| **URLhaus** | `connector-urlhaus` | Malicious URLs from abuse.ch | external-import |
| **AlienVault OTX** | `connector-alienvault` | Community-shared threat pulses (IOC bundles) | external-import |
| **AbuseIPDB blocklist** | `connector-abuseipdb` | High-confidence malicious IPs | external-import |
| **VirusTotal** | `connector-virustotal` | Auto-enriches every observable you add with VT verdict | **internal-enrichment** |
| **Shodan InternetDB** | `connector-shodan-internetdb` | Auto-enriches IP observables with exposed-service info | **internal-enrichment** |

"external-import" = pulls data on a schedule, fills your knowledge graph.
"internal-enrichment" = fires when *you* add an observable; calls the
external API; writes findings back onto your observable. Together they
give the "ingest + enrich" loop that powers the new voice command:
*"Hey Jarvis, enrich 8.8.8.8."*

## One-time API keys (3 free signups)

Sign up at each, copy the key, paste into the matching Railway env var.

| Connector | Sign-up URL | Free tier limit | Env var |
|---|---|---|---|
| AlienVault OTX | <https://otx.alienvault.com/api> | Unlimited (community key) | `ALIENVAULT_API_KEY` |
| AbuseIPDB | <https://www.abuseipdb.com/account/api> | 1,000 requests/day | `ABUSEIPDB_API_KEY` |
| VirusTotal | <https://www.virustotal.com/gui/my-apikey> | 4 req/min, 500/day, 15.5k/month | `VIRUSTOTAL_TOKEN` |

The other 4 connectors (`mitre`, `cisa-kev`, `urlhaus`,
`shodan-internetdb`) need no key.

## Railway service definitions — paste these

Same `divine-contentment` project, "New Service → Docker Image" for
each. Common env vars first (every connector needs them), then per-
connector additions.

### Common to ALL connectors

| Var | Value |
|---|---|
| `OPENCTI_URL` | `http://${{opencti.RAILWAY_PRIVATE_DOMAIN}}:8080` |
| `OPENCTI_TOKEN` | `e644bf12-cfc0-4844-a21d-5add52ae99bc` |
| `CONNECTOR_ID` | a fresh UUID (PowerShell: `[guid]::NewGuid()`) |
| `CONNECTOR_LOG_LEVEL` | `info` |

### 1. `connector-mitre`

- **Image:** `opencti/connector-mitre:6.4.7`
- **Extra env:**
  | Var | Value |
  |---|---|
  | `CONNECTOR_NAME` | `MITRE Datasets` |
  | `CONNECTOR_SCOPE` | `tool,report,malware,identity,campaign,intrusion-set,attack-pattern,course-of-action,x-mitre-data-source,x-mitre-data-component,x-mitre-matrix,x-mitre-tactic,x-mitre-collection` |
  | `MITRE_INTERVAL` | `7` (days between syncs) |

### 2. `connector-cisa-kev`

- **Image:** `opencti/connector-cisa-known-exploited-vulnerabilities:6.4.7`
- **Extra env:**
  | Var | Value |
  |---|---|
  | `CONNECTOR_NAME` | `CISA Known Exploited Vulnerabilities` |
  | `CONNECTOR_SCOPE` | `vulnerability` |
  | `CISA_CATALOG_URL` | `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json` |
  | `CISA_RUN_INTERVAL` | `86400` (seconds — daily) |

### 3. `connector-urlhaus`

- **Image:** `opencti/connector-urlhaus:6.4.7`
- **Extra env:**
  | Var | Value |
  |---|---|
  | `CONNECTOR_NAME` | `URLhaus` |
  | `CONNECTOR_SCOPE` | `urlhaus` |
  | `URLHAUS_CSV_URL` | `https://urlhaus.abuse.ch/downloads/csv_recent/` |
  | `URLHAUS_IMPORT_OFFLINE` | `false` |
  | `URLHAUS_INTERVAL` | `3` (days) |

### 4. `connector-alienvault`

- **Image:** `opencti/connector-alienvault:6.4.7`
- **Extra env:**
  | Var | Value |
  |---|---|
  | `CONNECTOR_NAME` | `AlienVault OTX` |
  | `CONNECTOR_SCOPE` | `alienvault` |
  | `ALIENVAULT_BASE_URL` | `https://otx.alienvault.com` |
  | `ALIENVAULT_API_KEY` | **your free OTX key** |
  | `ALIENVAULT_TLP` | `White` |
  | `ALIENVAULT_INTERVAL_SEC` | `1800` (30 min) |

### 5. `connector-abuseipdb`

- **Image:** `opencti/connector-abuseipdb-ipblacklist:6.4.7`
- **Extra env:**
  | Var | Value |
  |---|---|
  | `CONNECTOR_NAME` | `AbuseIPDB Blocklist` |
  | `CONNECTOR_SCOPE` | `abuseipdb` |
  | `ABUSEIPDB_URL` | `https://api.abuseipdb.com/api/v2/blacklist` |
  | `ABUSEIPDB_API_KEY` | **your free key** |
  | `ABUSEIPDB_SCORE` | `100` (only ingest the highest-confidence entries) |
  | `ABUSEIPDB_LIMIT` | `500000` |
  | `ABUSEIPDB_INTERVAL` | `1` (day) |

### 6. `connector-virustotal`

- **Image:** `opencti/connector-virustotal:6.4.7`
- **Extra env:**
  | Var | Value |
  |---|---|
  | `CONNECTOR_NAME` | `VirusTotal` |
  | `CONNECTOR_SCOPE` | `StixFile,Artifact,IPv4-Addr,Domain-Name,Url,Hostname` |
  | `CONNECTOR_AUTO` | `true` |
  | `VIRUSTOTAL_TOKEN` | **your free key** |
  | `VIRUSTOTAL_MAX_TLP` | `TLP:AMBER` |

### 7. `connector-shodan-internetdb`

- **Image:** `opencti/connector-shodan-internetdb:6.4.7`
- **Extra env:**
  | Var | Value |
  |---|---|
  | `CONNECTOR_NAME` | `Shodan InternetDB` |
  | `CONNECTOR_SCOPE` | `IPv4-Addr` |
  | `CONNECTOR_AUTO` | `true` |
  | `SHODAN_USE_ISP_NAME_AS_LABEL` | `true` |
  | `SHODAN_MAX_TLP` | `TLP:WHITE` |

(InternetDB is keyless. The fuller `connector-shodan` requires a paid
Shodan API key and gets richer data — out of scope for this phase.)

## Verification

After Railway shows all 7 connectors green:

1. **Connector list in OpenCTI UI**: Login → Data → Connectors → see
   all 7 with state `In progress` or `Running`.
2. **MITRE first sync** (~5 min): Threat Actors / Intrusion Sets pages
   start populating.
3. **CISA KEV first sync** (~30 s): Indicators page shows CVE entries.
4. **Voice ingest check**: *"Hey Jarvis, what came in today?"* →
   spoken count > 0.
5. **Voice enrich check**: *"Hey Jarvis, enrich 8.8.8.8"* → response
   mentions VirusTotal and/or Shodan.

## Cost addition

Each connector idles at ~150-300 MB RAM, no CPU when not actively
syncing. Adds ~$0.15/hr to the running state of the OpenCTI stack
(was $0.40/hr, now ~$0.55/hr). When the lifecycle workflow tears
everything down, connectors stop billing alongside the core.

| State | Cost |
|---|---|
| All 12 services Removed | ~$0.10/mo (volumes only) |
| All 12 services Running | ~$0.55/hr pro-rated |
| 30 min/day active | ~$8/mo |
| 1 hr/day active | ~$17/mo |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Connector logs `401 unauthorized` | Bad API key | Re-paste the key, redeploy |
| Connector status stuck at "Connecting" | `OPENCTI_URL` wrong | Confirm it's the *private* Railway domain on port 8080 |
| `CONNECTOR_ID` already in use | Same UUID on two services | Generate a fresh one per service |
| VirusTotal hammering quota | Default `CONNECTOR_AUTO=true` enriches every observable | Switch to `CONNECTOR_AUTO=false` and enrich only on demand via voice |
| Lifecycle workflow lists a connector that doesn't exist | Service name typo | Match the workflow's `--services` list EXACTLY to the Railway service names |
