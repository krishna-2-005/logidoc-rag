# Changelog

Every release of LogiDoc-RAG, newest first. Versions follow [Semantic Versioning](https://semver.org): the version
is set in `src/release.py`, and this file's newest heading must name it (`scripts/check_release.py`). How a version
is assigned and promoted: [docs/RELEASE_RUNBOOK.md](docs/RELEASE_RUNBOOK.md), "Versions and promotion".

## 1.0.0-rc.1 (2026-11-20)

The first release candidate: everything built on `feature/production-readiness` up to Sprint 4. It has been
rehearsed on an isolated staging environment with synthetic data, and has never run in production. Open items
are in [docs/RELEASE_ACCEPTANCE_CHECKLIST.md](docs/RELEASE_ACCEPTANCE_CHECKLIST.md).

### Upgrading

- **The review database upgrades from schema 2 to 3 and the audit history from 3 to 6.** Both upgrades are
  forward-only, and run only when the server starts with a fresh, verified backup in `LOGIDOC_BACKUP_DIR`.
  Restoring that backup later discards everything recorded after it. See
  [docs/MIGRATION_REHEARSAL.md](docs/MIGRATION_REHEARSAL.md).
- **A new cache folder, `data/cache/`,** holds the text parsed from the documents. Protect it like the documents
  ([docs/DOCUMENT_TEXT_CACHE.md](docs/DOCUMENT_TEXT_CACHE.md)).

### Document intake and audit

- PDFs are uploaded, validated, classified by content, indexed and tracked in an ingestion register; unusable
  files are quarantined with a reason, and can be retried, replaced, reprocessed or removed (archived, never
  deleted).
- Each shipment's bill of lading, proof of delivery and invoice are reconciled (quantity, delivery, invoice
  arithmetic), with evidence for every finding and no value invented for a missing one.
- The freight audit checks every invoice charge against the rate confirmation under central tolerance rules,
  flags duplicate invoice numbers, and is recorded whole so a past audit can be reopened exactly as it was.
- Structured extraction with typed fields; corrections of a field or an invoice line are proposed, approved by a
  second person, and only then read by the audits.
- A controlled re-audit records the effect of approved corrections as a new run; earlier runs are never rewritten.

### Review, analytics and reporting

- An exception workflow (assign, start, note, resolve with evidence, dismiss, dispute, waive, reopen, optional
  approval), with an append-only, hash-chained history.
- Audit history with trends, per-shipment timelines and run comparison.
- Carrier Intelligence, a carrier registry with name links approved by a second administrator, document
  completeness and financial exposure.
- A versioned, explainable shipment risk score and its dashboard.
- Freight audit, discrepancy, carrier and correction reports, previewed and downloaded as CSV (protected against
  formula injection) and plain-text PDF.
- A grounded assistant that answers from the audit with citations, kept apart from the verified findings.

### Integration

- The API is served as `/api/v1` with a published OpenAPI contract and typed responses.
- Signed webhooks with retries, idempotency keys, delivery monitoring and recovery.
- A scheduler with leases, and a provider-neutral TMS/ERP framework. Only a synthetic sandbox adapter exists:
  no real TMS vendor is connected.

### Security

- Sign-in with roles (administrator, reviewer), HttpOnly SameSite sessions, CSRF protection, account lockout.
- Every one of the 126 API operations has a tested permission ([docs/API_PERMISSIONS.md](docs/API_PERMISSIONS.md)).
- Rate limits on expensive and sensitive operations; forwarded client addresses trusted from the proxy only.
- Read requests never write the review database.
- Production settings are validated at startup; outbound webhooks refuse private and loopback destinations.
- CI audits the pinned dependencies for known vulnerabilities.

### Operations

- Verified backups with a manifest, scheduled verification, restore drills and restores into a separate folder.
- A migration guard: the real databases upgrade only at startup, with a fresh verified backup.
- Health and readiness endpoints, structured logs with request IDs, metrics and alerts.
- Deployment examples for Caddy, nginx and systemd; a release runbook and a configuration reference.

### Performance

- The web application loads each page and the charts as separate chunks (first load 114.6 kB gzip), with a size
  budget checked in CI.
- A read request looks the audit's state up once; parsed page text is cached by content hash.

### Known limitations

- Not deployed to a real server: no domain, certificate or penetration test yet.
- No OCR: image-only PDFs are reported as needing it.
- Amounts are never converted between currencies.
- Extraction accuracy on real documents is unmeasured (no permitted real documents).
- One server process and SQLite: suited to a single site, not to horizontal scaling.
