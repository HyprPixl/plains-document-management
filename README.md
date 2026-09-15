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

📄 **Spec / design phase.** No code yet. `SPEC.md` is the source of truth; it is written
to be built incrementally (see the Roadmap section).

## Prior art (patterns reused, in this workspace)

- `contracts-ver` — document-level AI extraction + human verification queue + staging
  states (`needs_review` / `auto_verified` / `verified`). Flask + Databricks App.
- `contract-explorer` — AI-augmented search, filters, shareable URLs, BU-based
  permissions table.
- **Land Records OCR Pipeline** (Databricks notebook, `Data_Engineering` repo) — Azure
  Document Intelligence (`prebuilt-read` → searchable PDF), SHA-256 dedup with reuse, and
  an idempotent Delta tracking table. The processing layer (§10) lifts directly from this.
