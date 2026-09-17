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
8. **Permissions are site-scoped.** `perms_where()` filters every Manage/Explore query by
   `sp_site_id` (`FULL`/`ADMIN` unrestricted). A user's accessible sites are learned from their own
   delegated browse (`sp.sync_user_sites`), not a manual grant table.
9. **Identical bytes = free reuse (SPEC §9/§10.3).** Everything is keyed on `content_sha256`. A
   duplicate upload should reuse cached OCR/text + derived PDF + AI fields, and — when the original
   is `verified` — copy its `confirmed_value`s so the dup lands already-verified. (Roadmap item R;
   confirm the ingest path actually takes this shortcut end-to-end.)

## Tests (Phase 1 item DONE — commit fb3485a)

`tests/` is a pytest suite, **91 tests, fully offline** (`pytest` from repo root; `pytest.ini` sets
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
- `POST /api/admin/bench` (admin-gated, same `_require_admin` as field-def CRUD): the **server-side**
  companion — times the hot-path warehouse queries (documents/stats/search/single-doc) in-process
  over `n` samples (default 20, capped 100) and returns per-op `{p50,p95,max,mean,n}` + wall-time +
  UTC timestamp + commit. Queries run with the caller's `perms_where` scope (admin = unrestricted).
  Triggered by the **"Run benchmark"** button in the admin Fields panel ("Modify fields" → header);
  renders the table + a Copy JSON affordance. Read-only (no writes/ai_query). This measures the
  warehouse round-trip the Lakebase migration is measured against, without CLI/token juggling.

## Phase 2 — Lakebase migration (permissions cutover LIVE; documents cutover BUILT, flag OFF)

**Status (thin slice shipped):** `lakebase.py` (Pattern A) + the `permissions` read cutover
are in. Offline suite green (103 tests). What exists now:

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

**Document family FULL cutover — BUILT, flag OFF (commit `0c615d6`).** The whole `document_*`
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
  documents/stats/classify/enqueue/tags/save_fields/verify/unverify/link/search/download/document +
  `/api/admin/bench`. `api_document` can't cross-store join, so it reads **field_defs from the
  warehouse**, values from Lakebase, and merges by `field_key` in Python.
- **`ingest.py` / `processing/job.py`**: dedup + insert, and the job's claim/lease/commit paths,
  branch on `docs_enabled()`. `upsert_proposed_field` preserves human provenance via
  `coalesce(document_fields.source_provenance,'ai')`.

**Job → Lakebase connectivity: WIRED + VALIDATED (2026-09-17).** This was the blocker before flipping
`USE_LAKEBASE_DOCUMENTS=true`: the processing job (`worker.py` → `processing/job.py`) is a **separate
Databricks Job**, not the web app — it does **not** get the `database` resource binding, so no
`PGHOST/PGUSER/PGPORT` is injected. Flip the flag while the job can't reach Lakebase and you split the
brain (app writes Lakebase, job drains an empty warehouse). Resolved as follows — and note the
**autoscale** wrinkle: our Lakebase (`ep-flat-moon`, `projects/plains-lakebase`, owned by Caleb) is an
**autoscale** instance, so it needs an **OAuth JWT** as the Postgres password. A job's ambient token
is a PAT (rejected: "not a valid JWT"), and `generate_database_credential` is a *provisioned*-instance
API that doesn't apply. So the only headless-job path is OIDC client-credentials with the app SP's
`client_id`+`secret`:

- Created an OAuth secret for the app SP (`app id 532acbc1-b288-4dd3-9a2d-6896d76706b1`, SCIM id
  `143845247881551`) via `databricks service-principal-secrets-proxy create`; stored `client_id` +
  `secret` in Databricks-backed scope **`document-hub`** (keys `sp-client-id`, `sp-oauth-secret`).
- Job `607689951574858` (partial `databricks jobs update`, so its paused periodic trigger survived):
  added lib `psycopg2-binary` + `spark_env_vars` `PGHOST/PGPORT/PGDATABASE/PGUSER=<SP app id>/
  LAKEBASE_SCHEMA=document_hub/LAKEBASE_OIDC_HOST=https://adb-1979327425712808.8.azuredatabricks.net/
  LAKEBASE_CLIENT_ID={{secrets/document-hub/sp-client-id}}/LAKEBASE_CLIENT_SECRET={{secrets/
  document-hub/sp-oauth-secret}}`. **`run_as` stays Caleb** — the Postgres identity is decoupled from
  the job's Databricks identity, so no SP re-grants for secrets/git/warehouse/volume were needed.
- `_get_sp_token()` prefers **`LAKEBASE_*`-prefixed** creds over `DATABRICKS_*` precisely so setting
  them on the job does NOT trip the databricks-sdk default-auth chain into re-identifying the whole job
  as the SP (which would break its run-as-user SharePoint/DI/volume calls). Don't rename these back.
- Validate anytime with `worker.py --check-lakebase` (non-destructive `SELECT 1`; probe-only, exits) —
  also runs passively at startup once `lakebase.enabled()`. Proven green: run `182572589457336`
  logged `lakebase check OK`. (Gotcha baked into the fix: a bare `sys.exit(0)` raises `SystemExit`,
  which the spark_python_task executor flags as FAILED — the probe returns cleanly on success instead.)

`ai_query` extraction stays on the warehouse regardless (it's fed OCR text inline, not read from a
table).

**To flip the cutover ON (not yet done):** set `USE_LAKEBASE_DOCUMENTS=true` in BOTH `app.yaml` (push
+ deploy the app) AND the job's `spark_env_vars` (`databricks jobs update`). First touch auto-creates
the 5 tables (advisory-lock DDL, no backfill); the 3 existing docs re-land via the next SharePoint
sweep or re-upload. Rollback = set the flag `false` in both places (warehouse path is untouched).
🔒 The SP OAuth secret was printed to a terminal during setup — **rotate it** (create a fresh one,
update the scope, `service-principal-secrets-proxy delete` the old id) once satisfied.

**Suite:** 108 tests, still fully offline/green.

**Consider next:** cache-busting `permissions` op in the admin bench (the perm win doesn't show
there — its one lookup is `g`-cached outside the timed loops).

### Original brief (kept for context)

**Goal:** kill the Statement-Execution round-trip tax (see `bench/BASELINE.md`: single-row
permission lookup **~1300 ms**, fired 2–3×/page; 4-query document fetch **~1900 ms p50**). Lakebase
is Databricks-managed Postgres (single-digit-ms round trips) → expected **10–100×** on the hot path.

### What's already true (unblocks the work)
- The app now has a **DB resource binding**: `Database plains-lakebase / databricks_postgres`,
  permission **"Can connect and create"**, resource key `database`. On a Databricks App this injects
  env `PGHOST / PGDATABASE / PGUSER / PGPORT` (and a `PGPASSWORD`) for the app SP.
- Instance: `ep-flat-moon-ee1bjbvj.database.westus2.azuredatabricks.net`, DB `databricks_postgres`,
  Postgres **17.11**. Probed live: app SP has CREATE rights.
- ⚠️ **SHARED instance.** `dbx-deal-capture-app` owns this exact endpoint and writes `deals` /
  `deal_log` etc. into `public`. **Document Hub MUST use its own `document_hub` schema** — never
  touch `public`. `CREATE SCHEMA IF NOT EXISTS document_hub` on bootstrap.

### Connection recipe (proven in sibling apps, in this workspace)
- **Pattern A — `dbx-deal-capture-app/` (RECOMMENDED, same instance we're on):** `psycopg2-binary`
  (no version pin). Reads `PGHOST/PGDATABASE/PGUSER/PGPORT` from env; **password = a freshly minted
  OAuth token**, not a static secret. `_get_sp_token()` POSTs `{DATABRICKS_HOST}/oidc/v1/token` with
  `grant_type=client_credentials, scope=all-apis`, HTTP-basic `(DATABRICKS_CLIENT_ID,
  DATABRICKS_CLIENT_SECRET)` — all three are auto-injected to the app SP on a Databricks App. Caches
  the token in a module dict + lock with a 60s early-refresh margin. Connect with
  `psycopg2.connect(host,dbname,port,user,password=token,sslmode="require",connect_timeout=10)`,
  `autocommit=True`. Thread-local single connection (`_tls`) wrapped so `.close()` is a **no-op**;
  recreated on credential change / closed conn. Tables guarded by a `_tables_ready` flag +
  `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
- **Pattern B — `conman/` (simpler, different endpoint):** static `PG_USER`/`PG_PASSWORD` app
  secrets, 55-min connection recycle (`_PG_CONN_TTL=3300`), retry-once-on-`OperationalError`. No
  token minting. Simpler but needs provisioned secrets and doesn't rotate cleanly.
- `contract-explorer` / `contracts-ver` / `plains-nexus` have **no Postgres** — warehouse only. Don't
  look to them for a Lakebase pattern.
- **Recommendation:** Pattern A. It matches the app we share the instance with, needs no new secrets
  (uses the SP identity already present), and rotates tokens correctly for a long-running gunicorn
  worker. (The injected `PGPASSWORD` is a viable shortcut but isn't refreshed in-process, so a
  long-lived worker can see it expire — that's exactly why deal-capture mints its own.)

### Build sequence (thin slice first — prove the win before a broad migration)
1. **`lakebase.py`** — new module implementing Pattern A: `pg_query(sql, params)` /
   `pg_execute(sql, params)` returning `list[dict]` (RealDictCursor), thread-local no-op-close conn,
   token minting + cache, `SCHEMA = "document_hub"` bootstrap. **Add a `LAKEBASE_TOKEN` env override**
   for the connection password so it can be tested locally with a transient personal token (the app's
   real auth path mints its own). **Offline/local + tests: if `PGHOST` is unset, the module must be
   inert** and callers fall back to the warehouse — the 97-test offline suite MUST stay green and the
   modules must stay import-safe under `conftest`'s stubs.
2. `requirements.txt` += `psycopg2-binary`; `app.yaml` declare the `database` resource
   (`- name: ... valueFrom: database`) + any `LAKEBASE_*` / feature-flag envs. Confirm the exact
   app.yaml resource syntax against `dbx-deal-capture-app/app.yaml`.
3. **Thin slice = `permissions`** (biggest per-request win — it's on every page, 2–3×). Create
   `document_hub.permissions`, **one-time backfill from the warehouse `permissions` table** if the PG
   table is empty, then point `get_perms()` reads (in `app.py`, already `g`-cached) at Lakebase behind
   a feature flag (`USE_LAKEBASE_PERMISSIONS`, default on when `PGHOST` present, else warehouse).
   **Dual-write** permission changes (`sp.sync_user_sites`, any admin grant) to BOTH stores so
   rollback stays safe and nothing else that reads the warehouse table breaks.
4. Measure via the **"Run benchmark"** admin button / `POST /api/admin/bench` and compare to
   `bench/BASELINE.md`. Only after the permission slice proves out, migrate `documents` +
   `document_fields` reads the same way.

### 🔒 Security constraint (non-negotiable)
The user shared a **1-hour personal Postgres token** for transient probing only. It is a personal
credential — **NEVER write it to any file, `app.yaml`, commit, or log.** The app's production auth is
the SP DB-resource binding (Pattern A token minting), never a pasted token. Use `LAKEBASE_TOKEN` only
as an ephemeral shell env for local probing, then discard.

### Watch-outs
- `psycopg2` uses real params (`%s`) — do NOT reuse `db.lit()` string-interpolation; use parameterized
  queries. Postgres returns real types (no STRING-coercion gotcha), but JSON array columns still hold
  `json.dumps`'d strings by our own convention (see gotcha #4).
- Keep the warehouse `db.py` path intact during migration — Lakebase is additive/behind a flag until
  each slice is proven. Don't rip out warehouse reads.
</content>
