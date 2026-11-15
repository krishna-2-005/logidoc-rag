# Performance baseline

The figures Sprint 4's performance work is measured against. All on synthetic data in temporary folders: no real
database or document is opened.

```
python scripts/benchmark_scale.py --sizes 100 1000 --pdfs 100 --json baseline.json
```

**Conditions:** Python 3.12.7 on the development laptop (16 GB RAM, shared with other applications), 2026-11-15.

**Compare runs made back to back only.** On this machine the same code ran up to 8 times slower when memory was
short (section 4).

## 1. Computation (in process)

| Step | 100 shipments | 1,000 shipments |
|---|---|---|
| Exception aggregation | 0.004 s | 0.031 s |
| Carrier aggregation | 0.019 s | 0.132 s |
| Freight audit engine | 0.071 s (0.71 ms a shipment) | 0.687 s (0.69 ms a shipment) |
| Review sync, first / unchanged | 0.012 s / 0.003 s | 0.075 s / 0.026 s |
| Recording a history run, first / unchanged | 0.054 s / 0.033 s | 0.523 s / 0.318 s |

## 2. End to end: 100 shipments, 400 PDFs

| Step | Time |
|---|---|
| Audit snapshot, cold (every PDF read) | 13.8 s (34.6 ms a PDF) |
| Audit snapshot, cached | 0.027 s |
| Carrier aggregation with the API's inputs | 3.4 s |

## 3. API, warm (median of 5 calls after one warming call)

| Endpoint | Median | Slowest |
|---|---|---|
| `GET /api/overview` | 57.7 ms | 58.4 ms |
| `GET /api/shipments` | 27.3 ms | 27.8 ms |
| `GET /api/shipments/{id}` | 65.9 ms | 66.5 ms |
| `GET /api/exceptions` | 49.1 ms | 50.3 ms |
| `GET /api/carriers` | 60.6 ms | 61.5 ms |
| `GET /api/risk/overview` | 63.0 ms | 64.1 ms |
| `GET /api/risk/shipments` | 64.1 ms | 64.6 ms |
| `GET /api/risk/shipments/{id}` | 61.7 ms | 62.7 ms |
| `GET /api/shipments/{id}/re-audit` (preview) | 17.0 ms | 24.7 ms |
| `GET /api/reporting/freight-audit-summary` (preview) | 63.6 ms | 66.6 ms |
| `GET /api/reporting/freight-audit-summary.csv` | 64.4 ms | 112.3 ms |
| `GET /api/reporting/freight-audit-summary.pdf` | 945.0 ms | 947.9 ms |

**Frontend:** the main bundle is 1,168.94 kB, 327.51 kB gzipped, in one chunk (the Sprint 3B build; phase 1 did
not change the frontend).

## 4. After Sprint 4 phase 1

Phase 1 changed the read path:

- **Reads no longer write the review store.** They read it into memory instead (`review_store.ReviewView`).
- **Some operations are rate limited** (`api.rate_limit`).

The committed code (`6002747`) and the phase 1 code were run alternately with the same command
(`--sizes 100 --pdfs 100`), so each pair ran under the same conditions.

| Run, in order | Code | Exceptions | Overview | Shipment list | Summary PDF |
|---|---|---|---|---|---|
| 1 | committed | 444 ms | 531 ms | 226 ms | 8,453 ms |
| 2 | phase 1 | 623 ms | 545 ms | 261 ms | 6,934 ms |
| 3 | committed | 452 ms | 503 ms | 281 ms | 6,851 ms |
| 4 (machine recovered) | phase 1 | 73 ms | 69 ms | 31 ms | 1,753 ms |
| 5 | committed | 446 ms | 467 ms | 300 ms | 6,725 ms |
| 6 | phase 1 | 553 ms | 552 ms | 237 ms | 7,752 ms |

**What the comparison shows:**

- **Most routes kept their time.** Within the run-to-run noise, both versions took the same time on:
  - routes that never read review states: the shipment list and the re-audit preview;
  - routes that read them through the view: carriers, risk and the reports.
- **The exception queue is about 20–25% slower in the worst case.** In the benchmark no detection is ever
  recorded, so each read plans every exception in memory (about 15 ms for 859 exceptions). On a server, the
  background loop records detections within 30 s. After that, a read costs less than the former sync: 5 ms
  instead of 10 ms for 859 exceptions.
- **The computation steps of section 1 are unchanged:** they were 0–10% faster in the same runs.
- **The rate limits cost no measurable time:** a regular expression match per request.
