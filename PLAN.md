# Build plan & status

Living checklist for building Document Hub. See `SPEC.md` for the design.

## Databricks backend — DONE (2026-09-15)

Created in `product_dev.document_hub` (warehouse `4d7f25b1bd5fddf1`):

- Schema `product_dev.document_hub` + managed volume `docs`.
- Tables: `documents`, `document_fields`, `field_defs`, `taxonomy`, `sources`,
  `document_links`, `document_text`, `extraction_cache`, `permissions`, `job_state`,
  `audit_log`.
- Seeded: taxonomy (7 BUs, 10 depts, 14 doc types incl. BU-scoped `Project (Engineering)`),
  27 field defs (common + Contract / Invoice / Project (Engineering) / Land ROW),
  `permissions` row (caleb.fedyshen@plains.com = ADMIN), `sources` (upload + land_records).

## App scaffold (Databricks App, Flask) — DONE

- [x] `requirements.txt`, `app.yaml`, `config.py`, `db.py`
- [x] `app.py` — routes for both surfaces + `/api/*`
- [x] `templates/index.html`, `static/style.css`, `static/app.js` (business-facing UI)
- [x] Local smoke test against the warehouse — **PASSED** end-to-end:
      upload + SHA-256 dedup (dup correctly detected) → classify → save fields →
      required-field verify guard → verify → Explore search hit → download.
      Amendment-needs-parent guard + document linking verified.
- [x] Fixed: statement-execution API returns all cells as strings; `db.py` now coerces
      BOOLEAN/INT/LONG/DOUBLE to real Python types (bug: `"false"` was truthy →
      every field showed as required).

## Processing job (Databricks Job — heavy work off the web app) — DONE (code); DI validated on job cluster only

Lifts from the **Land Records OCR Pipeline** (§10.4).

- [x] `processing/graph.py` — SharePoint Graph client (SPN client-credentials)
- [x] `processing/ocr.py` — native text check + Azure DI `prebuilt-read` (`output=["pdf"]`)
- [x] `processing/extract.py` — field extraction via `ai_query`
- [x] `processing/job.py` — claim → process → commit (idempotent, lease-based)
- [x] Validated on a cluster against a **real land-records PDF** (`1051101.pdf`, 156 KB):
      - SPN client-credentials Graph auth ✓
      - site→drive→folder resolution via `{host}:/teams/...` (search API is unavailable
        to app-only tokens — fixed `graph.resolve_site_drive` + `land_records` source config) ✓
      - recursive PDF listing + real file download ✓
      - DI client authenticates with the DI secret key ✓
- [!] DI `output=["pdf"]` searchable-PDF call NOT runnable on the shared interactive
      cluster: it force-pins the retired `azure-ai-documentintelligence==1.0.0b1` beta
      (root-owned path; its API version returns HTTP 410 Gone). This confirms GA
      `>=1.0.0` (api 2024-11-30) is required — exactly what `requirements.txt` pins and
      `ocr.py` targets, matching Plains' production Land Records notebook. Runs correctly
      on a job cluster that installs from `requirements.txt`.

## Delegated SharePoint (on-behalf-of-user import + auto-sync) — DONE (code); needs redirect-URI registration

Users connect their own Microsoft identity, so access is scoped to what they can already
open — no per-site SPN grants. Distinct from the app-only sweep above. Reuses the
plains-nexus app registration (no new secret to provision).

- [x] `sharepoint.py` — delegated auth-code flow: authorize/exchange/refresh (token
      rotation), SQL-backed OAuth `state` + per-user `sp_sessions` (refresh tokens
      Fernet-encrypted at rest with a key derived from the app client secret), Graph
      browse (sites→drives→folders/files), import via shared `ingest.register_bytes`,
      sync CRUD (arm/list/remove/reconnect).
- [x] `app.py` routes — `/api/sharepoint/{login,callback,status,sites,drives,items}`,
      `POST /import`, `GET/POST /syncs`, `DELETE /syncs/<id>`, `POST /syncs/<id>/reconnect`.
      Every endpoint gated on the caller's import permission; refresh tokens never returned
      to the browser; exact redirect-URI derivation (APP_BASE_URL or X-Forwarded-*).
- [x] `processing/job.py` — delegated `sync_delegated()` tick: claim each armed sync with a
      lease, refresh+rotate the owner's token, pull files `lastModifiedDateTime > last_synced_at`
      (watermark), register via `ingest`, advance watermark; dead grant → `token_status='needs_reauth'`.
      Wired into `run_once(do_sync=...)` on its own `SP_SYNC_INTERVAL` cadence (`--sync`).
- [x] Frontend — "Import from SharePoint" entry, breadcrumb browser modal (sites/drives/
      folders/files, search, multi-select), optional classify-on-import, auto-sync opt-in with
      "connections can be fickle" warning, synced-folders panel with reconnect banner.
- [x] Local smoke test — routes register; `/status` → `configured:true` (plains-nexus secrets
      readable); `/login` builds a well-formed authorize URL against the Plains tenant with
      read-only scopes + persisted state; Fernet encrypt/decrypt round-trips; syncs list OK.
- [ ] **External dependency:** register `<APP_BASE_URL>/api/sharepoint/callback` as a redirect
      URI on the plains-nexus app registration before the OAuth flow will complete in prod.
- [ ] End-to-end delegated flow (real user consent → browse → import → auto-sync) — pending
      redirect-URI registration + deploy.

## Source control + deploy — IN PROGRESS

- [x] Private GitHub repo `HyprPixl/plains-document-management` (main pushed).
      `.gitignore` guards `__pycache__` + secrets; `app.yaml` carries only secret-scope
      **key names**, never values.
- [x] `APP_BASE_URL` set to the deployed app URL
      (`plains-document-management-1979327425712808.8.azure.databricksapps.com`).
- [x] `job.json` + `worker.py` — Databricks Job `document-hub-processing` (id 607689951574858),
      single-node cluster, git source (`main`), **scheduled every 10 min** running
      `worker.py --drain --sweep --sync` (drain = clear backlog then terminate; cost-optimal).
- [x] App deployed by user; app SP `app-4850yt` (`532acbc1-…`) granted USE_CATALOG on
      `product_dev`, USE_SCHEMA/SELECT/MODIFY on `document_hub`, READ/WRITE on `docs` volume,
      READ on secret scope `pna-wu2-dm-dev-data-keyv`. `/api/me` unblocked.
- [x] Job validated on a fresh job cluster: import bug fixed (see below), `--sweep` discovered
      + downloaded + registered the Land Records connected source (Graph auth → resolve →
      recursive list → download → volume write → Delta insert all working). Imported docs land
      `unclassified/pending` → Manage "Needs classification" queue.
- [!] Fixed: `.gitignore` rule `secrets.*` had excluded `processing/secrets.py` (code, not a
      secret) → first run failed `ImportError: cannot import name 'secrets'`. Rule narrowed;
      file committed.
- [ ] **User: redeploy the app from latest `main`** — the running deployment predates the
      `[hidden]` overlay CSS fix and the `APP_BASE_URL` value.
- [ ] User: register `<APP_BASE_URL>/api/sharepoint/callback` on the plains-nexus app reg.
- [ ] Close DI `output=["pdf"]` on the job cluster: classify a doc so a scheduled run OCRs it
      (nothing gets processed until at least one doc is `classified`).
- [ ] Decide whether the whole `land_records` source should stay auto-imported (it's enabled;
      disable in `sources` if only a sample was wanted). Sweep is incremental after first pull.

## What can be tested now vs. later

- **Now (local, warehouse creds):** taxonomy, upload + SHA-256 dedup UX, classify,
  verification queue, related-doc linking, explore search, download — everything that
  doesn't need secrets/DI/Graph.
- **Databricks-side (needs secrets):** SharePoint pull + Azure DI OCR + `ai_query`
  extraction — run via the processing job on a cluster (has secret scope access).

## Blocked

- New SharePoint site connections (mirror targets + non-land-records sources) — waiting on
  access. Land Records site (`LandRecordsResearch-UsrGrp`) is reachable for testing.
