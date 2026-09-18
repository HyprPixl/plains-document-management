# AGENTS.md — working notes for agents on Document Hub

Orientation + hard-won gotchas that are **not** obvious from the code. Read this, `SPEC.md`
(design), `ROADMAP.md` (what's next, prioritized), and `PLAN.md` (what's shipped) before starting.
This file is the tacit knowledge from the build sessions — keep it current when you learn something
the next agent would trip on.

## What this is

A Databricks App (Flask + gunicorn, 4 workers, `--timeout 120`) for Plains (midstream oil & gas):
classify → AI-extract → human-verify documents, then explore/search them. SharePoint is the
"shelf," Databricks is the "brain." Git-linked to `HyprPixl/plains-document-management`, deployed
SNAPSHOT from `main`.

- App URL: `https://plains-document-management-1979327425712808.8.azure.databricksapps.com`
- Catalog/schema: `product_dev.document_hub` · SQL warehouse `4d7f25b1bd5fddf1`
- Volume: `/Volumes/product_dev/document_hub/docs`

## Repo map

| File | Role |
|---|---|
| `app.py` | Flask routes: both surfaces + all `/api/*`. Web tier only — no heavy work here. |
| `db.py` | Warehouse access: `query(sql)`, `execute(sql)`, `lit(v)`. **See coercion gotcha below.** |
| `config.py` | All env/config + table-name constants (`config.DOCUMENTS`, etc.) and `t(name)`. |
| `ingest.py` | `register_bytes(...)` shared ingest + `guess_document_type(filename, sp_path)`. |
| `sharepoint.py` | Delegated (on-behalf-of-user) OAuth + Graph browse/import/sync. `SPReauth` exc. |
| `processing/` | The async job (OCR, extract, sync, imports). Heavy work lives here, off the web app. |
| `worker.py` | Job entrypoint: `python -m processing.job` via `worker.py --drain --sync`. |
| `templates/index.html`, `static/app.js`, `static/style.css` | Business-facing vanilla-JS UI. |

## Deploy & operate (commands that actually work)

```bash
# Deploy the app from latest main (SNAPSHOT)
databricks apps deploy plains-document-management \
  --json '{"git_source":{"branch":"main"},"mode":"SNAPSHOT"}' --output json

# Trigger the processing job (id 607689951574858) — also available via MCP manage_job_runs
databricks jobs run-now 607689951574858
```

- Processing job `document-hub-processing`, id **607689951574858**, git-linked to `main`, runs
  `worker.py --drain --sync`. The schedule is **paused** — processing is on-demand/delegated-only
  (see PLAN.md). Run-now (or the app's best-effort auto-trigger on import enqueue) to drain.
- MCP tools (Databricks) are **deferred** — `ToolSearch` for `select:mcp__databricks__execute_sql`
  or `mcp__databricks__manage_job_runs` before calling. `execute_sql` uses `warehouse_id`
  `4d7f25b1bd5fddf1`.

## Gotchas (each cost real time to find)

1. **`ai_query` / batch inference model.** `EXTRACT_MODEL = databricks-claude-sonnet-4-5`. sonnet-4
   is deprecated; **sonnet-5 / opus-5 are NOT supported for batch inference** (`ai_query` on the
   warehouse) — they fail. Verify a candidate with
   `SELECT ai_query('<model>', 'Reply with the single word OK')` before switching.
2. **The warehouse returns every cell as a STRING.** `db.py` coerces BOOLEAN/INT/LONG/DOUBLE to
   real Python types. If you add a query, don't hand-parse — trust the coercion, and if you see
   `"false"` behaving truthy, that's the class of bug (it was: every field showed as required).
3. **Real column names on `documents`** — there is **no** `doc_name` / `filename` / `title` column.
   Filename is `original_filename`; path is `sp_path`; SharePoint link is `sp_web_url`; site is
   `sp_site_id` / `sp_site_name`. `documents` no longer scopes by `business_unit` (dropped — the
   spine is now SharePoint site + freeform tags; see PLAN "SharePoint as the spine").
4. **`document_fields` has no `value_text`.** Columns are `proposed_value` (AI) / `confirmed_value`
   (human) / `source_provenance` (`ai`|`human`|`sharepoint`|`preset`) / `updated_by`. Arrays
   (topics, parties) are stored as `json.dumps`'d strings in these columns.
5. **Only send *changed* fields on save.** `api_save_fields` stamps `source_provenance='human'` +
   promotes to `confirmed_value` for every field it receives. The frontend dirty-tracks via
   `input.dataset.orig` and posts only edited fields; verify accepts an unedited AI proposal via
   `coalesce(confirmed_value, proposed_value)`. Sending all fields = the "everything says edited by
   a person" bug (fixed 7632ee1). Don't regress this.
6. **`el()` helper + boolean attributes.** The JS `el(tag, attrs)` helper does
   `setAttribute(k, v)` for any non-null value, so `el("div", {hidden: false})` still **sets** the
   `hidden` attribute and hides the element. Create without the attr and set the **property**
   (`node.hidden = false`) instead.
7. **`[hidden]` must win in CSS.** `style.css` has `[hidden] { display:none !important }` because
   class rules that set `display:flex`/`position:fixed` (modals/drawers) otherwise override the
   low-specificity `[hidden]` rule and leave overlays stuck open.
8. **Permissions are site-scoped.** `perms_where()` (warehouse) and `lakebase._sites_clause()`
   filter every Manage/Explore query by `sp_site_id` (`FULL`/`ADMIN` unrestricted). A user's
   accessible sites are learned from their own delegated browse (`sp.sync_user_sites`), not a manual
   grant table. Two extra rules: (a) a doc with `sp_site_id IS NULL` (legacy drag-drop uploads /
   the SPN sweep) is visible to its `created_by` uploader, so orphans aren't invisible; (b) new
   drag-drop uploads must be **filed under a site** — `api_upload` requires `sp_site_id` (validated
   against the uploader's own sites) so the upload inherits that site's audience. The picker's
   options come from `/api/sites` (distinct sites already in the caller's scope). "Import from
   SharePoint" is the primary upload path in the UI; local upload is the collapsed secondary.
   **Phase 6 ACL measure-first:** each synced item is probed for unique/broken-inheritance
   permissions (`sp.item_has_unique_acl`, best-effort, one Graph `/permissions` call) and the
   result stored in `documents.has_unique_acl` (NULL=unknown/not-probed, Lakebase-only column).
   This is *instrumentation only* — no enforcement change. Read the distribution via admin-only
   `GET /api/admin/acl-audit`; build per-item ACL enforcement only if the data shows meaningful
   broken inheritance. **Coverage note:** the sync-time probe only fires on new/changed items and
   the delta crawl never revisits stable files, so existing docs stay `has_unique_acl = NULL`. A
   one-off `python -m processing.job --acl-backfill [--acl-limit N]` (job.acl_backfill →
   `graph.item_has_unique_acl`, app-only token, one light /permissions call per doc, no downloads,
   429-backed-off) probes the unmeasured docs so the audit rests on real coverage. **DECIDED
   (2026-09-18): per-item ACL enforcement is NOT built.** After the backfill (run 772778508702186,
   probed 188) the audit is full-coverage 189 docs / **0 unique / 189 inherited / 0 unknown** —
   zero broken inheritance anywhere in the tenant, so site-level enforcement already mirrors
   SharePoint access exactly. Don't build the per-item ACL table / group expansion / hybrid
   enforcement unless a future re-audit shows unique>0. The probe + acl-audit + backfill stay as
   the standing tripwire.
   **Phase 6 delta crawl (item U):** folder/drive auto-syncs now pull changes via Graph's delta
   query (`sp.delta_changes`) instead of re-listing the subtree each tick — it returns only
   adds/edits/moves/deletes since a stored `sharepoint_syncs.delta_link` (added lazily by
   `job._ensure_delta_column`). A sync already caught up by the old watermark crawl is *seeded from
   now* (`token=latest`, no re-download); a brand-new sync (no watermark) enumerates fully once;
   a stale link (HTTP 410) transparently restarts as a full crawl. Deletes are surfaced/logged only
   (`delta_deletes=N`) and now drive item S (below). Single-file
   targets keep the watermark path (no subtree to delta). **Mirror fate decided (item U):** the
   SPEC §13 DBX→SP mirror is **retired** — SharePoint is the live source of truth, so there is no
   DBX→SP write-back. `documents.mirror_status` stays a vestigial column (always `'not_mirrored'`);
   nothing reads it for behavior and no mirror will be built. Don't wire new logic to it.
   **Phase 6 delete/move lifecycle (item S) — SHIPPED (Lakebase-only, gated by `docs_enabled()`).**
   The delta crawl's three change classes are now reconciled in `job._sync_one`: (a) **moves/renames**
   — an existing item (dedup returns `duplicate`) whose `sp_path`/`sp_web_url`/`file_modified_at`
   drifted is repaired in place via `lakebase.refresh_location(source_ref, ...)` (a no-op guarded by
   `IS DISTINCT FROM`, so unchanged files don't churn); (b) **deletes** — each delta `deleted` ref is
   soft-deleted via `lakebase.soft_delete_by_source_ref` (sets `documents.deleted_at`, keeps the row +
   `content_sha256` + extraction/fields; never hard-deletes); (c) **re-adds** — `ingest.register_bytes`
   now checks `find_deleted_by_sha` after the live-dup check and, on a hash match to a soft-deleted row,
   **rehydrates** it (`deleted_at=NULL`, re-point `source_ref`/`sp_path`/`sp_web_url`) rather than
   inserting a new doc, so a delete→re-add round-trip keeps the original doc + its verification. The
   `deleted_at IS NULL` filter is applied everywhere live rows are read (`list_documents`, `stats`,
   `search`, `find_by_sha`, `find_processed_twin`, `acl_stats`, `unprobed_acl_docs`) — soft-deleted
   docs vanish from every user-facing surface but stay recoverable. `_sync_one` logs
   `soft_deleted=N moved=N`. Column added by `_DOCS_DDL` + idempotent `ALTER ADD COLUMN IF NOT EXISTS`.
9. **Identical bytes = free reuse (SPEC §9/§10.3) — IMPLEMENTED (roadmap item R).** Everything is
   keyed on `content_sha256`. Two layers: (a) `extraction_cache` (keyed sha+prompt_version+type)
   already made the `ai_query` free on a re-run; (b) `process_doc` now short-circuits on a **twin** —
   `lakebase.find_processed_twin()` / the warehouse `_find_twin()` look for another doc with identical
   bytes, same `document_type`, `extraction_status='done'` under the current `PROMPT_VERSION` — and
   `copy_from_twin()` copies its text layer, derived-PDF pointer, and field values (provenance
   preserved) instead of re-OCR'ing. When the twin is `verified`, the dup lands `verified` too
   (`verified_by` copied, `mirror_status='not_mirrored'` so it re-mirrors to its own SP location).
   Same-byte dups arise legitimately from connectors (one file under two SharePoint locations → two
   rows). In-app upload dedup still returns the existing doc (no second row); the upload UI surfaces
   the reuse ("already verified — reused for free"). The searchable PDF is a sha-keyed volume artifact
   already shared, so only the pointer is copied, never bytes.

## Phases 4 & 5 — Manage UX + Explore build-out (SHIPPED 2026-09-18)

Built by fanning out two agents with **exclusive file ownership** (frontend owns
`static/app.js` + `templates/index.html` + `static/style.css`; backend owns `app.py` +
`lakebase.py` + `tests/` + `config.py` + `requirements.txt`) coordinated by pinned contracts —
the way to parallelize safely given the single ~1600-line imperative `app.js`.

- **Phase 4 Manage UX (item F) + item Q** (commits `29bc937`, `24df0d2`): a11y overlay kit
  (`openOverlay`/`closeOverlay`/focus trap), loading/error/empty states across stats/docs,
  keyboard access on stat cards + rows, double-submit guards, shared utils
  (`activate`/`debounce`/`withBusy`). Explore search bar gained count/sort/dept+path filters/
  pagination/multi-select + a read-only drawer (`openDoc(id,row,{readOnly})`). Q = client-side
  "Open folder" link derived from `sp_web_url` (no schema change, works for all existing docs).
- **Phase 5 Explore E/O/L** (commit `0114441`):
  - **E — Office viewer.** `GET /api/render?doc_id=&sheet=` renders `.docx` via **mammoth** and
    `.xlsx`/`.xlsm` via **xlsx2html + openpyxl** (tabbed sheets) to standalone HTML — pure-Python,
    replicated from **plains-nexus** (NO LibreOffice/Graph conversion). Content-addressed volume
    sidecar `{DOCS_VOLUME}/_render_cache/{content_sha256}.{docx|xlsx}.html` (read-through, best-
    effort write) + per-worker LRU. Frontend dispatches Office types into the drawer iframe via
    `srcdoc`; other types keep View/Download. Render libs imported lazily (import-safe offline).
  - **O — corpus chat.** `POST /api/chat` streams **keyword-grounded RAG** over SSE. OpenAI SDK →
    Databricks serving (`base_url=host/serving-endpoints`, key from `w.config.authenticate()`,
    model `config.CHAT_MODEL` default `databricks-claude-sonnet-4-6`), client cached ~50min +
    rebuild/retry-once on auth error. Retrieval is site-scoped: `lakebase.search(...limit=8)` +
    new `lakebase.passages_for_docs(doc_ids, char_budget=12000)`. Frames: `data: <json token>`,
    `event: citations` (`[{doc_id,page,filename}]`), `event: done`, `event: error`; **503
    `chat_unavailable`** before streaming when serving unconfigured. Client = Search|Chat|
    Obligations sub-tabs, multi-turn history, clickable citation chips. **The live streaming path
    is only verifiable against the real serving endpoint — not the offline suite** (tests mock the
    OpenAI client + cover the 503 path).
  - **L — relations + obligations.** `GET /api/documents/<id>/tree` → `{root,nodes,edges}` from
    `lakebase.link_graph(doc_id)` (both-direction, cycle-guarded `WITH RECURSIVE`; warehouse
    fallback = one-hop only) → indented relation tree in the drawer. `GET /api/obligations?from=&
    to=&document_type=` lists upcoming date-field obligations — date field keys from `FIELD_DEFS
    WHERE data_type='date'`, values are free-text EAV strings **parsed/range-filtered in Python**
    (`_parse_date`, never SQL-cast), default range today..+1yr, site-scoped via
    `lakebase.obligations(...)`; rendered as a read-only month-grouped calendar. Lakebase-first
    (warehouse mode returns `[]`).

New read routes route through `_doc_visibility()` (row-level `perms_where`/`_sites_clause`
equivalent). ⚠️ **`/api/download` has a pre-existing site-scope gap** (no visibility check) — left
untouched as out of scope; worth closing. **Deferred: item P** (plains-nexus handoff — disabled
"Open in plains-nexus" scaffold; needs nexus base URL + contract from user). Suite: **178 tests**.

## Tests (Phase 1 item DONE — commit fb3485a)

`tests/` is a pytest suite, **112 tests, fully offline** (`pytest` from repo root; `pytest.ini` sets
`pythonpath=.`). Dev deps in `requirements-dev.txt`; CI in `.github/workflows/tests.yml` runs on
push/PR — **keep main green.** Tiers: pure-unit (`test_unit.py`, `test_db.py`), API with a mocked
`db` (`test_api.py`), processing (`test_processing.py`). `tests/conftest.py` holds the shared
fakes.

- **The code is NOT import-safe offline.** Every module (`db`, `app`, `ingest`, `processing.job`)
  constructs `WorkspaceClient()` at **import time**, which hard-fails / slow-probes without
  Databricks creds. `conftest.py` stubs `WorkspaceClient` (and the Azure DI SDK, a job-only dep)
  before import. If you add a module or a test file, follow that pattern. A worthwhile Phase 3
  cleanup: make `_w` lazy so the code imports without creds.
- To add API/processing tests, mock `db.query`/`db.execute` (see `FakeDB` in conftest) and assert on
  the **emitted SQL** (column names, guards, provenance) — that's how the load-bearing rules
  (only-changed-fields save, `coalesce` verify, `'false'` coercion) are pinned. Don't hit a real
  warehouse.
- Frontend JS (`asStringList`/`itemToStr`) is untested — no JS harness in the repo.

## Error handling / logging (Phase 1 item DONE — commit 71e65d5)

Structured error logging is now in place, matching **contract-explorer**'s pattern (stdout
`StreamHandler`, format `[%(asctime)s] [%(levelname)s] %(message)s`; Databricks Apps capture
stdout — there is no error Delta table, by design, to match the sibling apps):

- `app.py`: `app.logger` → stdout handler at `config.LOG_LEVEL`; `@app.before_request` sets
  `g.request_id`/`g.user_email`; `@app.after_request` logs 4xx (WARNING) / 5xx (ERROR);
  `@app.errorhandler(HTTPException)` and `@app.errorhandler(Exception)` log with context and return
  friendly JSON **without** leaking tracebacks (SPEC §16). Context tag convention:
  `request_id=… method=… route=… user=…`.
- `processing/job.py`: named logger `doc_hub.processing`, same format; all failure paths now
  `logger.error/warning` with `doc_id=… stage=… <traceback>` (previously `print`/`traceback.print_exc`).
- The existing `_audit(...)` → `audit_log` Delta path is the who-did-what trail, **separate** from
  error logs; left untouched.

**Logging gotcha:** the databricks SDK configures the **root logger on import**, so
`logging.basicConfig(...)` is a silent no-op (your format is ignored). Attach a handler to your
named logger directly with `propagate=False` — don't rely on `basicConfig`.

## Time benchmarks / profiling (Phase 1 item DONE — commit 840ea9b)

Instrumentation + a bench harness; the real baseline numbers are captured **on deploy** (needs the
live warehouse + SSO), not offline.

- `db.py`: `query()` is wrapped with `perf_counter`; any statement slower than `config.SLOW_QUERY_MS`
  (env, default 1000ms; also in `app.yaml`) logs at WARNING via the `doc_hub.db` logger —
  `slow query duration_ms=… sql=<snippet>` — same stdout format/`propagate=False` as app/job.
  `execute()` routes through `query()`, so it's covered. **Return contract unchanged** (tests green).
- `app.py`: `before_request` stamps `g.req_started`; `after_request` logs one INFO line per request,
  `request complete — request_id=… method=… route=… user=… status=… duration_ms=…`.
- `bench/bench.py`: stdlib-only (urllib) client-side latency bench for the hot endpoints
  (`/api/stats`, `/api/documents`, `/api/search`, `/api/documents/<id>`). p50/p95/max over
  configurable `-n`, `--base-url` (defaults to prod), bearer token via `--token`/`DOC_HUB_BENCH_TOKEN`
  or raw `--header` (Databricks Apps SSO). Timestamped JSON+md land in `bench/results/` (gitignored).
  **Run-on-deploy:** `DOC_HUB_BENCH_TOKEN=<tok> python bench/bench.py -n 30 --label pre-lakebase`.
  Full instructions in `bench/README.md`. These are the baselines Phase 2/3 are measured against.
  (The in-app `POST /api/admin/bench` companion route was **removed** after Phase 3 — the baseline it
  captured now lives in `bench/BASELINE.md`; re-run `bench/bench.py` if you need fresh numbers.)
  Triggered by the **"Run benchmark"** button in the admin Fields panel ("Modify fields" → header);
  renders the table + a Copy JSON affordance. Read-only (no writes/ai_query). This measures the
  warehouse round-trip the Lakebase migration is measured against, without CLI/token juggling.

## Phase 2 — Lakebase migration (permissions cutover LIVE; documents cutover LIVE)

**Status:** `lakebase.py` (Pattern A) + the `permissions` read cutover + the full `document_*`
cutover are LIVE. Offline suite green (130 tests). What exists now:

- **`lakebase.py`** — Pattern A: `pg_query`/`pg_execute` (params, RealDictCursor), thread-local
  no-op-close conn, SP-token minting + cache (`LAKEBASE_TOKEN` env override for local probing —
  never persisted), `CREATE SCHEMA/TABLE IF NOT EXISTS` bootstrap in `document_hub`. **Inert when
  `PGHOST` is unset** (`enabled()` False → callers fall back to the warehouse); import-safe even
  without psycopg2 installed (guarded import).
- **`get_perms()` cutover** (`app.py`): reads Lakebase when `config.USE_LAKEBASE_PERMISSIONS` and
  `lakebase.enabled()`; **falls back to the warehouse on any Lakebase error** (page never hard-fails
  during cutover). Still `g`-cached (one lookup/request — pinned by test).
- **One-time backfill**: `lakebase.read_permissions()` → `_ensure_permissions_ready()` seeds the PG
  table from the warehouse `permissions` table the first time it's empty (per worker).
- **Dual-write**: `sp.sync_user_sites()` mirrors SITE grants into Lakebase too (`mirror_user_sites`,
  no-op when disabled, never breaks the warehouse write) so both stores stay consistent / rollback-safe.
- **App-admin management** (`/api/admin/access`, GET list + POST set; app.py `_elevated_grants` /
  `_set_access`): admin-only in-app console to grant/revoke ADMIN / FULL / READ — the elevated grants
  that used to be hand-seeded in the `permissions` table. SITE rows stay auto-mirrored from SharePoint
  and are **not** touched here. Same dual-store discipline (warehouse source-of-truth + `lakebase.
  set_access_grant` mirror). Guard: the **last remaining admin can't be demoted/revoked** (409). UI is
  the "App access" block in Manage → Modify fields (admin-gated). This is the "nexus-style permissions"
  roadmap item, scoped down to app-admin management (not the full nexus project/RBAC model).
- **Wiring**: `requirements.txt += psycopg2-binary`; `app.yaml` binds the `database` resource
  (`valueFrom: database`) + `LAKEBASE_HOST/DB/SCHEMA` + `USE_LAKEBASE_PERMISSIONS` (set `"false"` to
  roll back to the warehouse instantly).

**Measured (2026-09-16, deployed + proven):** the permission read cutover is live. `/api/me`
(≈ just `get_perms()`) went **~1,300 ms → 5 ms** warm (~260×); the warehouse `permissions`
slow-query line is gone, no fallbacks. See `bench/BASELINE.md` → "Phase 2 result". Gotcha proven
on deploy: **4 gunicorn workers bootstrap the PG table concurrently** — Postgres `CREATE … IF NOT
EXISTS` isn't atomic across sessions, so `_bootstrap` swallows benign concurrent-DDL errors, the
one-time dedup + UNIQUE index + backfill run under a `pg_advisory_lock`, and writes use
`ON CONFLICT DO NOTHING`. Don't regress that.

**Document family FULL cutover — LIVE (flipped 2026-09-17, commit `a718318`).** `USE_LAKEBASE_DOCUMENTS=true`
is now set in BOTH `app.yaml` (deployed, resolved_commit `a7183187`) AND the job's `spark_env_vars`.
Validated end-to-end: cutover run `558225007906501`/task `837641056755139` → **SUCCESS** — job
authenticated to Lakebase as the SP (`lakebase check OK`, `current_database=databricks_postgres`), the
5 `document_*` tables auto-created on first touch (`Lakebase document_hub document_* tables ready`), and
the drain completed (`processed 0 doc(s)` — queue was empty; the 3 existing docs re-land on the next
sweep/re-upload, per "delete current data, don't worry about backfill"). Rollback = flag `false` in both
places (warehouse path untouched). 🔒 **STILL TODO: rotate the SP OAuth secret** (printed to a terminal
during setup) — see the rotation steps below; the app/job read it fresh from scope `document-hub` each
run, so rotation is put-secret + proxy-delete + one validation run, no redeploy.

The whole `document_*`
family (documents / document_fields / document_text / document_tags / document_links) is wired to
Lakebase for **reads AND writes** behind `USE_LAKEBASE_DOCUMENTS` (default **off**). It's a full
cutover, not a dual-write copy: no warehouse mirror of these tables, no backfill (user's call — only
three docs in the system, "delete current data, don't worry about backfill"). Warehouse SQL stays in
every call site, so the flag is an instant rollback.

- **`lakebase.py`** document section: advisory-lock-serialized DDL (`_DOCS_LOCK_KEY`) for all 5
  tables (Spark→Postgres types), `docs_enabled()` = `enabled() and USE_LAKEBASE_DOCUMENTS`,
  `_sites_clause()` scope helper (None=unrestricted, []=`1=0`, else `= ANY(%s)`), and parameterized
  read/write/job functions. Dialect conversions: `current_timestamp()`→`now()`,
  `INTERVAL n SECONDS`→`make_interval(secs=>%s)`, Spark `MERGE`→`INSERT … ON CONFLICT`,
  `concat_ws/collect_list`→`string_agg`, `IN (list)`→`= ANY(%s)`. **All document-family functions
  are parameterized** (not `db.lit()`) — arbitrary OCR/field text + psycopg2's `%`-in-LIKE handling +
  cross-dialect backslash escaping make interpolation unsafe here. `search()` assembles params in the
  SQL's textual `%s` order (the tag JOIN precedes WHERE, so its param binds first).
- **`app.py`**: `perms_sites(email)` helper (None for full/admin, else site list) feeds the Lakebase
  scope. Every document call site got a `lakebase.docs_enabled()` branch:
  documents/stats/classify/enqueue/tags/save_fields/verify/unverify/link/search/download/document.
  `api_document` can't cross-store join, so it reads **field_defs from the warehouse**, values from
  Lakebase, and merges by `field_key` in Python (all four Lakebase reads folded into one round-trip
  via `get_document_bundle`).
- **`ingest.py` / `processing/job.py`**: dedup + insert, and the job's claim/lease/commit paths,
  branch on `docs_enabled()`. `upsert_proposed_field` preserves human provenance via
  `coalesce(document_fields.source_provenance,'ai')`.

**Job → Lakebase connectivity: ambient run-as JWT (reworked 2026-09-18).** The processing job
(`worker.py` → `processing/job.py`) is a **separate Databricks Job**, not the web app — it does **not**
get the `database` resource binding, so no `PGHOST/PGUSER/PGPORT` is injected. Our Lakebase
(`ep-flat-moon`, `projects/plains-lakebase`, owned by Caleb) is an **autoscale** instance, so it needs
an **OAuth JWT** as the Postgres password (a raw ambient PAT is rejected: "not a valid JWT"). The job
now mints that JWT as its **run-as identity**, with **no SP secret and nothing written onto its
cluster**:

- `lakebase._get_sdk_credential()` calls the credential REST API through the SDK's
  ambient-authenticated `api_client`: `POST /api/2.0/database/credentials` with
  `claims=[{"endpoint": "<autoscale endpoint>", "permission_set": "READ_ONLY"}]`. Two reasons for the
  raw call over `w.database.generate_database_credential`: it works on the cluster's **older
  databricks-sdk** (0.40, predates `w.database`), and autoscale is addressed by endpoint `claims`, not
  provisioned `instance_names` (those 404). `permission_set` is a required-but-nominal claim — real
  read/write is governed by the Postgres role, and the run-as user **owns** the instance → full RW
  (verified: a `READ_ONLY` cred has `transaction_read_only=off` and can write). Endpoint override:
  `LAKEBASE_ENDPOINT`.
- `worker.py._bootstrap_job_env()` sets `PGHOST/PGPORT/PGDATABASE/LAKEBASE_SCHEMA` + the cutover flags
  (`USE_LAKEBASE_DOCUMENTS`/`USE_LAKEBASE_PERMISSIONS=true`) and resolves `PGUSER` from
  `current_user.me()` — all via `setdefault`, **before** importing `config`/`job` (config snapshots the
  flags at import). This replaces the old dependency on cluster `spark_env_vars`.
- **Why the rework:** the job was moved onto the shared existing cluster `1008-171723-j7v5frie`, which
  dropped the dedicated `new_cluster` block that had carried all the Lakebase `spark_env_vars` (incl.
  the SP OAuth secret-scope refs) — so every connection var vanished at once and the job couldn't auth.
  The ambient-JWT path removes that fragility: no per-cluster env, works on any cluster the job lands
  on. `run_as` stays Caleb (the Postgres identity), so no SP re-grants were needed.
- **Superseded (old SP-secret path):** previously the job used OIDC client-credentials with the app SP's
  `client_id`+`secret` from scope `document-hub` (keys `sp-client-id`/`sp-oauth-secret`) via
  `LAKEBASE_*`-prefixed `spark_env_vars`. No longer used. Rollback to it by re-adding those
  `spark_env_vars` (setdefault yields to them). The `_get_sp_token()` LAKEBASE_*-over-DATABRICKS_*
  preference still matters for that path — don't rename it.
- Validate anytime with `worker.py --check-lakebase` (non-destructive `SELECT 1`; probe-only, exits) —
  also runs passively at startup once `lakebase.enabled()`. (Gotcha: a bare `sys.exit(0)` raises
  `SystemExit`, which the spark_python_task executor flags as FAILED — the probe returns cleanly on
  success instead.)

`ai_query` extraction stays on the warehouse regardless (it's fed OCR text inline, not read from a
table).

**Cutover is ON (done 2026-09-17).** For reference, the flip was: `USE_LAKEBASE_DOCUMENTS=true` in BOTH
`app.yaml` (push + deploy the app) AND the job's `spark_env_vars` (`databricks jobs update`). Rollback =
set the flag `false` in both places (warehouse path is untouched).

🔒 **Pending: revoke the exposed SP OAuth secret** (it was printed to a terminal during setup). As of
the 2026-09-18 ambient-JWT rework the **job no longer uses this secret**, and the App authenticates with
its own platform-injected SP creds — not this scope secret — so revoking it breaks nothing. Just revoke
(no reissue needed):
1. `databricks service-principal-secrets-proxy delete 143845247881551 a6786ee1239531cf3ad569992798e57c96161d3848b4a1f9c877f3d06ae6cf57` (revoke the exposed id).
2. Optionally delete the now-unused scope keys: `databricks secrets delete-secret document-hub sp-oauth-secret` and `... sp-client-id`.
3. Confirm the job still auths (it doesn't depend on the secret): `databricks jobs run-now 607689951574858 --python-params '--check-lakebase'`.

**Suite:** 112 tests, still fully offline/green.

**Consider next:** cache-busting `permissions` op in the admin bench (the perm win doesn't show
there — its one lookup is `g`-cached outside the timed loops).

### Durable facts + watch-outs
- ⚠️ **SHARED instance.** `dbx-deal-capture-app` owns endpoint `ep-flat-moon-ee1bjbvj` (Postgres 17,
  DB `databricks_postgres`) and writes into `public`. **Document Hub lives ONLY in its own
  `document_hub` schema** — never touch `public`.
- **Connection = Pattern A** (mirrors deal-capture): `psycopg2-binary`, password is a **freshly minted
  OAuth token** (not a static secret), thread-local no-op-close conn, token cached with early-refresh.
  The **autoscale** instance requires an OAuth JWT — the app SP's injected `DATABRICKS_CLIENT_ID/SECRET`
  mint it via `{host}/oidc/v1/token`; the injected `PGPASSWORD` is not refreshed in-process so a
  long-lived worker would see it expire. `LAKEBASE_TOKEN` env is a local-probe override only (ephemeral,
  never persisted).
- `psycopg2` uses real params (`%s`) — do NOT reuse `db.lit()` string-interpolation. Postgres returns
  real types (no STRING-coercion gotcha), but JSON array columns still hold `json.dumps`'d strings by
  our own convention (see gotcha #4).
- Keep the warehouse `db.py` path intact — the Lakebase cutovers are flag-guarded rollbacks, not
  rip-outs. `ai_query` extraction stays on the warehouse regardless (fed OCR text inline).
</content>
