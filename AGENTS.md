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
</content>
