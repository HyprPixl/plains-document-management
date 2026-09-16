# Build plan & status

Living checklist for building Document Hub. See `SPEC.md` for the design.

## ▶ Model revision — SharePoint as the spine (2026-09-16) — PLANNED

Supersedes the Business-Unit-centric model in SPEC §6/§14. Decisions locked with the user:

1. **Drop Business Unit** entirely (facet + permission scope). Already removed `Engineering`
   from BU (now a Department); the rest of the BU dimension goes too.
2. **Every document lives at a SharePoint location.** Imported docs already do; **uploads now
   write back to a SharePoint destination** (chosen at upload, or a configured default) before
   registering. One rule, no exceptions: a doc = bytes at a SharePoint site/library/folder.
3. **Permissions mirror SharePoint at the *site* level.** You can see a doc in the app iff you
   can access the SharePoint **site** it lives in. (Library/folder grain is a later phase.)
4. **Source path is first-class:** store the human-readable site/library/folder path per doc and
   make it searchable + a breadcrumb filter in Explore.
5. **Custom grouping = freeform tags** (many per doc, ad hoc) — the flexible replacement for BU.

### Schema deltas
- `documents`: add `sp_site_id`, `sp_site_name`, `sp_drive_id` (library), `sp_path`
  (human folder path incl. filename), `sp_web_url` (link back). `source_ref` keeps
  `drive_id/item_id`; these make site/path first-class + queryable. Retire `business_unit`
  (stop writing it; drop the column in a later cleanup).
- New `document_tags` (`doc_id`, `tag`, `created_by`, `created_at`) — one row per tag; filter
  is "has tag X". Simple to index and multi-select.
- `permissions`: replace `allowed_business_unit` → `allowed_site_id` (keep `FULL`/`ADMIN`).
- `taxonomy`: delete the `business_unit` rows (Department + Document Type remain).

### Permission model (site-scoped)
- `perms_where()` scopes every Explore/Manage query on `sp_site_id` instead of `business_unit`;
  `FULL`/`ADMIN` unrestricted.
- **How we learn a user's sites (the elegant part):** a user's *own* delegated browse already
  reveals exactly which sites they can access (`sp.list_sites` returns only visible sites). On
  connect + on a refresh cadence, capture that set into `permissions` as `allowed_site_id` rows.
  No manual grant table to maintain; it mirrors SharePoint by construction.
- App-uploaded docs inherit the ACL of the site they're written to (uploader must have write
  access there, enforced by the delegated write).

### Work breakdown
**Not blocked — can build now (read side + tags + path search + site-scoped read perms):**
- [ ] Schema: add doc SP-location columns, `document_tags`, swap `permissions` column, drop BU
      taxonomy rows.
- [ ] Import: capture + store `sp_site_id/name`, `sp_drive_id`, `sp_path`, `sp_web_url` on every
      registered doc (both queued import and delegated sync). Backfill existing docs from
      `source_ref` via Graph (site + path resolution).
- [ ] Permissions: `perms_where` on `sp_site_id`; site-membership capture on connect/refresh.
- [ ] Explore: path breadcrumb facet + path in full-text search; remove BU facet.
- [ ] Tags: add/remove UI on a doc; "has tag" filter; drop BU picker from classify/upload.

**Blocked on write scopes (upload → SharePoint):**
- [ ] **External:** expand delegated scopes to `Files.ReadWrite.All` / `Sites.ReadWrite.All` →
      **admin re-consent** on plains-nexus + users reconnect. (Same class of blocker as the
      redirect-URI registration.)
- [ ] Upload flow: destination picker (reuse the SharePoint browser modal) or configured default
      drop location; Graph upload of bytes; then register with the SP location.

### Sequencing note
Build the not-blocked slice first (it stands alone and makes the app coherent without BU). The
upload-write-back piece lands after write-scope consent — until then, uploads can be disabled or
kept local with a "destination required" notice.

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
- [x] Removed the bulk Land Records import (user didn't want the whole SharePoint corpus):
      deleted 454 `land_records` docs + their volume files, disabled the `land_records`
      source, dropped `--sweep` from the job (params now `--drain --sync`), and **paused the
      schedule**. Processing is now on-demand / delegated-only:
        - manual per-folder imports via the SharePoint UI, and
        - opt-in delegated auto-sync (`--sync`) for folders a user explicitly arms.
      Unpause the job (or run-now) once there are classified docs or armed syncs to process.
      Re-enable `sources.land_records` + re-add `--sweep` only if a full connected-source
      pull is ever wanted.

## Bulk SharePoint import moved off the web request — DONE (code) (2026-09-15)

Symptom: importing ~50 docs from SharePoint classified a few then died — gunicorn
`WORKER TIMEOUT` (`--timeout 120`) aborted the worker mid-batch. Root cause: the
`/api/sharepoint/import` route downloaded + registered every selected file inline
(Graph download + 3 warehouse round trips each, serial) inside the request. Fix keeps
the doctrine "heavy work off the web app":

- [x] New `import_jobs` queue table (created in `product_dev.document_hub`).
- [x] `sharepoint.enqueue_import()` / `import_job_status()` / `recent_import_jobs()`.
- [x] Route now enqueues + returns `{request_id, queued}` instantly; new
      `GET /api/sharepoint/import/<id>` status endpoint.
- [x] `processing/job.py` `process_imports()` + `_run_import()`: lease-claimed (like
      `sync_delegated`), walks folders, downloads + registers with progress counters,
      SHA-256 dedup → re-entrant/resumable. Wired into `run_once` (runs every pass).
- [x] Frontend enqueues then polls status (`pollImportJob`), refreshing the Manage queue
      as docs land.
- [x] App best-effort triggers a job run on enqueue (`_trigger_processing_run`,
      `PROCESSING_JOB_ID=607689951574858` in `app.yaml`) so imports don't wait for the
      (paused) schedule.
- [x] App SP granted **Can manage and run** on job `document-hub-processing` (resource
      key `job`) — the enqueue auto-trigger now fires instead of no-op'ing.
- [ ] **User: redeploy the app** from latest `main` (carries this fix) and **run-now /
      unpause** the job to drain the queue.
- [ ] End-to-end: queue a ~50-doc import → confirm it completes off-request and docs
      appear in the Manage queue.

## What can be tested now vs. later

- **Now (local, warehouse creds):** taxonomy, upload + SHA-256 dedup UX, classify,
  verification queue, related-doc linking, explore search, download — everything that
  doesn't need secrets/DI/Graph.
- **Databricks-side (needs secrets):** SharePoint pull + Azure DI OCR + `ai_query`
  extraction — run via the processing job on a cluster (has secret scope access).

## Blocked

- New SharePoint site connections (mirror targets + non-land-records sources) — waiting on
  access. Land Records site (`LandRecordsResearch-UsrGrp`) is reachable for testing.
