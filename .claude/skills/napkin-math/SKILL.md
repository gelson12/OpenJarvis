---
name: napkin-math
description: |
  Back-of-envelope numerical estimation from first principles. Use when the
  user asks "how fast", "how big", "how much", "can this scale to", "what's
  the cost of", or any rough sizing/feasibility question — disk/memory/network
  latency and throughput, query rates, storage cost, capacity planning. Cite
  the canonical numbers below; show the arithmetic; round aggressively.
  Auto-triggers on words like estimate, ballpark, feasibility, RPS, QPS, scale,
  capacity, throughput, latency, "how many", "how long", "fits in".
license: MIT
source: https://github.com/sirupsen/napkin-math (Simon Eskildsen)
---

# Napkin Math

Estimate system performance and cost from first principles using a small set
of memorized numbers. The goal is **order-of-magnitude correct in under a
minute**, not precision.

## How to use this skill

1. Restate the question in numerical form. Identify the operation, the data
   size, and the rate.
2. Pick the relevant rows from the latency / throughput tables below.
3. Compose: latency × rate, throughput ÷ size, etc. Show every step.
4. Round to 1 significant figure at each step. Precision is fake comfort;
   order-of-magnitude is what matters.
5. State assumptions explicitly. End with "is this in the ballpark you
   expected?" so the user can correct misframings.

## Canonical latency / throughput table

(Numbers rounded for memorization, not faux precision. Source: sirupsen/napkin-math, last refreshed 2026-03-08 on Intel Xeon 6985P-C.)

| Operation                           | Latency     | Throughput | 1 MiB  | 1 GiB  |
|-------------------------------------|-------------|------------|--------|--------|
| Sequential memory R/W (64 bytes)    | 0.5 ns      |            |        |        |
| ├ Single thread                     |             | 20 GiB/s   | 50 μs  | 50 ms  |
| ├ Threaded                          |             | 200 GiB/s  | 5 μs   | 5 ms   |
| Hashing, not crypto (64 bytes)      | 10 ns       | 5 GiB/s    | 200 μs | 200 ms |
| Random memory R/W (64 bytes)        | 20 ns       | 3 GiB/s    | 300 μs | 300 ms |
| System call                         | 300 ns      |            |        |        |
| Hashing, crypto-safe (64 bytes)     | 100 ns      | 1 GiB/s    | 1 ms   | 1 s    |
| Sequential SSD read (8 KiB)         | 1 μs        | 8 GiB/s    | 100 μs | 100 ms |
| Context switch                      | 10 μs       |            |        |        |
| Sequential SSD write, no fsync      | 2 μs        | 3 GiB/s    | 300 μs | 300 ms |
| TCP echo server (32 KiB)            | 50 μs       | 500 MiB/s  | 2 ms   | 2 s    |
| Proxy (Envoy/Nginx/HAProxy)         | 50 μs       |            |        |        |
| Random SSD read (8 KiB)             | 100 μs      | 70 MiB/s   | 15 ms  | 15 s   |
| Decompression                       |             | 1 GiB/s    | 1 ms   | 1 s    |
| Compression                         |             | 500 MiB/s  | 2 ms   | 2 s    |
| Network within zone/VPC (premium)   | 250 μs      | 25 GiB/s   | 50 μs  | 40 ms  |
| Network within region               | 250 μs      | 2 GiB/s    | 500 μs | 500 ms |
| Sequential SSD write + fsync        | 300 μs      | 30 MiB/s   | 30 ms  | 30 s   |
| MySQL/Redis/Memcached query         | 500 μs      |            |        |        |
| Serialization (JSON-ish)            |             | 100 MiB/s  | 10 ms  | 10 s   |
| Deserialization (JSON-ish)          |             | 100 MiB/s  | 10 ms  | 10 s   |
| Sequential HDD read (8 KiB)         | 10 ms       | 250 MiB/s  | 2 ms   | 2 s    |
| Random HDD read (8 KiB)             | 10 ms       | 0.7 MiB/s  | 2 s    | 30 min |
| Blob storage GET (304 not-modified) | 30 ms       |            |        |        |
| Blob storage GET, 1 conn (128 KiB)  | 80 ms       | 100 MiB/s  | 10 ms  | 10 s   |
| Blob storage LIST                   | 100 ms      |            |        |        |
| Blob storage PUT, 1 conn (128 KiB)  | 200 ms      | 100 MiB/s  | 10 ms  | 10 s   |
| Network NA central ↔ east           | 25 ms       | 25 MiB/s   | 40 ms  | 40 s   |
| Network NA central ↔ west           | 40 ms       | 25 MiB/s   | 40 ms  | 40 s   |
| Network NA east ↔ west              | 60 ms       | 25 MiB/s   | 40 ms  | 40 s   |
| Network EU west ↔ NA east           | 80 ms       | 25 MiB/s   | 40 ms  | 40 s   |
| Network EU west ↔ NA central        | 100 ms      | 25 MiB/s   | 40 ms  | 40 s   |
| Network EU west ↔ Singapore         | 160 ms      | 25 MiB/s   | 40 ms  | 40 s   |
| Network NA west ↔ Singapore         | 180 ms      | 25 MiB/s   | 40 ms  | 40 s   |

## Storage / cloud costs (rough, USD, 2026)

- Blob storage (S3/R2/GCS standard): ~$0.02/GB/month. Egress: $0.05–0.09/GB
  to internet (R2 is free; S3/GCS charge).
- Block storage (gp3-class SSD): ~$0.10/GB/month.
- Compute (general-purpose vCPU, 24/7): ~$30/month per vCPU on-demand,
  ~$10/month reserved. RAM ~$3/GB/month.
- Managed Postgres/MySQL: ~3–5× raw VM cost for equivalent capacity.

Use these only when the user hasn't supplied real prices.

## Worked examples (template)

**"Can we serve 10K RPS from a single Postgres box?"**
- 10 K queries/s × 500 μs/query = 5 s of CPU per second → need ≥5 cores doing
  nothing else. Real workloads add joins / IO / lock waits → bump 3–5×.
- Verdict: tight on a single box; viable on a beefy one if queries are
  cache-friendly. Plan for read replicas at 5–8K RPS sustained.

**"Storing 100 K logs/s, 1 KB each, 30-day retention — how much disk?"**
- 100 K × 1 KB = 100 MB/s ingress. Compressed ~10× → 10 MB/s on disk.
- 10 MB/s × 86400 s × 30 = ~26 TB. Round to 30 TB. At $0.02/GB/mo blob:
  ~$600/month. At $0.10/GB block: ~$3000/month. Use blob.

## Rules

- **Round at every step.** "About a hundred microseconds" beats "97 μs."
- **Cite the row** you took numbers from so the user can sanity-check.
- **Do the worst case AND the best case** when uncertainty is large
  (ratios > 3×). Say which one you think dominates and why.
- **Refuse precision** the data doesn't support. "Couldn't tell you to
  better than a factor of 3" is honest and useful.
- **Cross-check with reality.** If the answer is wildly different from
  prod metrics the user might know, ask before committing to it.
