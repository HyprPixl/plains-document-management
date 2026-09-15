# Document Hub

An enterprise document management system for Plains: classify any document, let AI
extract the fields that matter for its type, have a human verify, and make everything
searchable, viewable, and downloadable.

Two surfaces in one app:

- **Manage** — connect sources, upload, classify, AI-extract, verify, submit.
- **Explore** — search, filter, preview, and download across the whole corpus.

**System of record:** Databricks (Unity Catalog volumes for files, Delta tables for
metadata), mirrored to SharePoint on a fast cadence. See [`SPEC.md`](./SPEC.md).

## Status

🟢 **Built, in deployment.** The app and processing job are implemented and locally
smoke-tested end-to-end against the `product_dev.document_hub` warehouse. `SPEC.md` remains
the design source of truth; [`PLAN.md`](./PLAN.md) tracks build status per component.

- **Web app** (Flask, this repo root) — both surfaces, upload + SHA-256 dedup, classify,
  verify, search, download; permission-gated by business unit. Deployed as a Databricks App.
- **Processing job** (`processing/`) — OCR (Azure Document Intelligence → searchable PDF),
  `ai_query` field extraction, idempotent lease-based claim/retry. Runs on a Databricks Job.
- **SharePoint** — two independent paths: an app-only sweep of connected sources, and a
  delegated (on-behalf-of-user) import + auto-sync with Fernet-encrypted refresh tokens.
  See [`SHAREPOINT_CONNECTION.md`](./SHAREPOINT_CONNECTION.md).

## Architecture

```
Browser ── Databricks App (app.py, gunicorn -w 4) ── SQL warehouse (Delta metadata)
                                                   └─ UC volume /Volumes/product_dev/document_hub/docs
Databricks Job (python -m processing.job --loop --sweep --sync)
   ├─ claims pending docs → OCR → extract → commit          (heavy work, off the web app)
   ├─ --sweep : app-only SharePoint pull of connected sources
   └─ --sync  : delegated per-user auto-sync (watermark-based)
```

Catalog/schema: `product_dev.document_hub`. Warehouse: `4d7f25b1bd5fddf1`. Both the app and
the job read config from env (`app.yaml` for the app; job params below) and secrets from the
`pna-wu2-dm-dev-data-keyv` scope.

## Deploy

1. **App** — point the Databricks App at this repo; it runs `app.yaml`
   (`gunicorn app:app -w 4`). Grant the app's service principal access to the schema, the
   `docs` volume, and the secret scope.
2. **Redirect URI** — before delegated SharePoint OAuth works, register
   `<APP_BASE_URL>/api/sharepoint/callback` on the plains-nexus app registration.
   `APP_BASE_URL` is set in `app.yaml`.
3. **Job** — see [`job.json`](./job.json); create/update with
   `databricks jobs create --json @job.json` (or the equivalent in the bundle). Runs
   `python -m processing.job --loop --sweep --sync` on a cluster that installs
   `requirements.txt`.

## Prior art (patterns reused, in this workspace)

- `contracts-ver` — document-level AI extraction + human verification queue + staging
  states (`needs_review` / `auto_verified` / `verified`). Flask + Databricks App.
- `contract-explorer` — AI-augmented search, filters, shareable URLs, BU-based
  permissions table.
- **Land Records OCR Pipeline** (Databricks notebook, `Data_Engineering` repo) — Azure
  Document Intelligence (`prebuilt-read` → searchable PDF), SHA-256 dedup with reuse, and
  an idempotent Delta tracking table. The processing layer (§10) lifts directly from this.
