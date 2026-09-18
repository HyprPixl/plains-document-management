# Document Hub — Next-phase roadmap

Design source of truth is [`SPEC.md`](./SPEC.md); build history is [`PLAN.md`](./PLAN.md).
This file is the **prioritized plan for the next phase** — the ambitious goals agreed with the
user on 2026-09-16, sequenced by dependency and risk rather than by the order they were raised.

Guiding principle (unchanged from SPEC §): *heavy work off the web app; the request path stays
thin.* Two new principles for this phase: **measure before you optimize** (nothing is "faster"
without a benchmark), and **never refactor untested code** (tests land before the passes that
move code around).

## Sequencing rationale

The user's stated order was: Lakebase → UX walkthrough → optimization pass → Fable cleanup →
multi-filetype viewer → Explore → error logging → SharePoint-first upload → tests → benchmarks →
scale/permissions → Explore sub-UX (obligations) → chat → nexus integration → open-enclosing-folder.

We resequence three things forward, on purpose:

- **Error logging, tests, and benchmarks move to Phase 1 (foundation).** You cannot safely migrate
  the data layer (Lakebase), run a function-by-function optimization pass, or let Fable trim code
  without regression tests to catch breakage and baseline numbers to prove the gains are real.
- **Lakebase + scale + permissions collapse into one phase (Phase 2)** — they are the same data-layer
  change and should be designed together, not bolted on.
- **The UX walkthrough anchors Phase 4** rather than running early, so we tune the UI against the
  final (fast, tested) backend instead of re-doing it after the migration.

Everything the user asked for is preserved and mapped in the table at the bottom.

---

## Phase 0 — Wrap-up of the current phase — ✅ DONE (2026-09-16)

Cleanup shipped before starting the next phase:

- Extraction model fixed: `ai_query` now uses `databricks-claude-sonnet-4-5` (sonnet-4 deprecated,
  sonnet-5 has no batch inference). Extraction verified end-to-end.
- Fields UX: dropped Language; tall scrollable Summary; Topics/Parties render as editable chips
  (legacy `[{name,role}]` normalised); AI-suggested values tinted with one section legend instead
  of a per-field note; empty required fields highlighted inline.
- Ingest auto-suggests `document_type` from the file name/path (suggestion only — the doc stays
  unclassified for human confirmation).
- Related-document linking UI in the drawer — unblocks Amendment verification (previously required a
  parent link with no UI to create one).
- "Modify fields" toggle in the queue header swaps the table for the fields editor (collapse +
  scroll); queue tabs / stat cards swap back.
- **Bug fixed:** saving one field no longer stamps *every* field `source_provenance='human'` and
  promotes all AI guesses to confirmed values. Save now dirty-tracks and sends only changed fields;
  verify accepts an unedited AI proposal for required fields (`coalesce(confirmed, proposed)`).
  Mis-stamped rows in the corpus were reverted.

## Phase 1 — Foundation: observability, tests, profiling — ⏳ NEXT

Front-loaded because Phases 2–5 all depend on it. No user-facing changes; pure safety net.

- **Structured error logging** (req: *errors to the log like my other apps*). Every unhandled
  exception and 4xx/5xx on the request path, and every failure in the processing job, written to a
  durable sink — match the pattern in plains-nexus / contract-explorer (log table + stdout the
  Databricks app log captures). Add a route error handler + a job-side logger; include request id,
  user, route, and stack. Today failures only surface as a toast or a silent `pending` doc.
- **Test suite** (req: *tests for everything*). `pytest` with three tiers:
  - pure-unit (no I/O): `guess_document_type`, `asStringList`/`itemToStr`, `perms_where`, dedup hash,
    prompt building, JSON coercion in `db.py`.
  - API (Flask test client, seeded test schema or mocked `db`): classify → enqueue → save → verify
    happy path + guards (missing required, amendment-needs-parent), permission scoping, field-def CRUD.
  - processing: claim/lease idempotency, extract cache hit/miss, import/sync watermarking.
  - Wire into CI (GitHub Actions) so `main` stays green.
- **Time benchmarks / profiling** (req: *time benches for everything, to profile & improve*).
  Per-request timing middleware + per-statement timing in `db.py` (log slow queries), and a
  `bench/` script that exercises the hot endpoints (`/api/documents`, `/api/stats`, `/api/document/*`,
  `/api/search`) and records baselines. These baselines are what Phase 2/3 are measured against.

## Phase 2 — Data layer: Lakebase + scale + permissions — ✅ DONE (2026-09-18)

> **Status (2026-09-18):** ✅ **Phase 2 complete.** Lakebase backend + permissions cutover + full
> `document_*` cutover are LIVE (see AGENTS.md → "Phase 2 — Lakebase migration"). Permission read
> `/api/me` ~1,300 ms → 5 ms warm (~260×). Multi-user scale hardened (connection resilience:
> reconnect-and-retry-once on a dropped socket). App-admin management shipped (in-app grant/revoke of
> ADMIN/FULL/READ). 🔒 One follow-up remains: **rotate the SP OAuth secret** (deferred, tracked in
> AGENTS.md + memory).

- **Lakebase (Databricks Postgres OLTP) backend** — ✅ DONE. Move hot transactional reads/writes — `documents`, `document_fields`,
  `document_tags`, `document_links`, `sp_sessions`, `import_jobs`, `sharepoint_syncs`, `job_state` —
  onto Lakebase, where a query is a Postgres round-trip (single-digit ms) instead of a Statement
  Execution API call (hundreds of ms). Keep the SQL warehouse for what it's good at: `ai_query`
  extraction, full-text search over `document_text`, and analytics. Introduce a data-access layer so
  both coexist behind one interface; migrate table-by-table, each guarded by Phase 1 benches so the
  win is measured, not assumed.
- **Multi-user scale** (req: *ensure it scales to multiple users*) — ✅ DONE. Confirmed no global
  mutable state in the request path (perms memoized on per-request `flask.g`, no shared caches); the
  real multi-user gap was long-lived workers holding a socket that Lakebase idle-closes, so `pg_query`
  now reconnects and retries once on a dropped connection (safe: statements are idempotent by design).
  gunicorn worker count revisited once per-request latency drops.
- **plains-nexus-style permissions** (req: *similar permission handling to plains-nexus*) — ✅ DONE,
  scoped to **app-admin management** (per user: *"mostly … being able to change who's an app admin and
  stuff"*). Added an admin-only in-app console (`/api/admin/access`, "App access" panel in Manage) to
  grant/revoke ADMIN / FULL / READ — replacing hand-edits to the `permissions` table. SITE grants stay
  auto-mirrored from SharePoint. Guarded against demoting the last admin. The full nexus project/RBAC
  model was deliberately **not** ported (nexus is project-based; Doc Hub is site-based) — revisit only
  if a richer model is actually needed.

## Phase 3 — Quality passes (optimization + Fable cleanup)

Run after Phase 2 so we optimize the *final* architecture, and after Phase 1 so tests catch regressions.

- **Function-by-function optimization** (req: *go over each function, make sure it's optimal*).
  Kill N+1 round-trips (e.g. `api_document` issues four sequential queries — batch them); cache
  taxonomy and field-defs (they change rarely) instead of re-querying per request; trim redundant
  work in hot loops. Guided by the Phase 1 benchmarks.
- **Free reuse of results for identical documents** — ✅ DONE (2026-09-17). (req: *if an identical doc is uploaded and
  extraction has run — or better, it's verified — copy those for free*). Everything is already
  keyed on `content_sha256`, so on a duplicate upload we copy the original's OCR/text layer,
  derived searchable PDF, and AI-extracted field values instead of re-paying — and **when the
  original is `verified`, copy its human-confirmed values onto the duplicate so it lands
  already-verified** (provenance = copied-from source doc). Verify the extraction cache and
  ingest path actually take this shortcut end-to-end (not just OCR); surface it in the dedup UX
  ("already extracted / already verified — reused"). See SPEC §9 and §10.3.
- **Fable code-cleanup pass** (req: *cleanup pass using Fable — trim unnecessary code, single-use
  functions, redundancy, fix bugs*). Run the cleanup with the Fable model over the whole tree: remove
  dead/one-caller helpers, deduplicate, tighten, and fix latent bugs the tests now pin down.

## Phase 4 — Manage-side UX overhaul

- **Full UX walkthrough** (req: *go through the entire user flow, each button and screen, suggest
  changes and optimizations*). Screen-by-screen, click-by-click audit of Manage; findings written up,
  then applied.
- **SharePoint-first upload** (req: *re-prioritize the file upload UI so it's SharePoint first,
  optional "drop files here"*). Make SharePoint import the primary action; demote local drop to a
  secondary/optional affordance.
- **Multi-filetype viewer** (req: *view other filetypes like plains-nexus*). Preview docx / xlsx /
  pptx / email inline the way nexus does, not just PDFs and images.
- **Open enclosing folder in SharePoint** (req). Add alongside "Open in SharePoint" — link to the
  parent folder (`sp_web_url` of the parent), not just the file.
- **Doc-type suggestion UX** (req: *other UX for spec doc-type suggestions is encouraged*). Build on
  the filename heuristic — optional AI pre-classification, confidence, and a cleaner confirm affordance.

## Phase 5 — Explore build-out (highest ambition, most downstream)

Depends on the fast data layer (2), the viewer and permissions (4), and tested foundations (1).

- **Explore review + baseline improvements** (req: *we haven't even looked at the Explore page yet*).
- **Open selected docs in plains-nexus** (req: *combine the doc-centric features plains-nexus has —
  select a bunch of docs in a search and open them in plains-nexus*). Multi-select in results →
  hand-off to nexus.
- **Chat over the corpus** (req: *add similar chat features to Explore that contract-explorer has*).
- **Contract obligation management** (req: *sub-UX for contract obligation management — lifecycle
  timelines, calendars, relation trees*). Timelines from extracted dates, an obligations calendar, and
  a relation tree built on `document_links`.

## Phase 6 — SharePoint as the live source: lifecycle, ACL parity & sync fidelity — ⏳ NEW (raised 2026-09-18)

> **Decision (2026-09-18, user):** SharePoint is now treated as the **live source of truth for file
> lifecycle** (existence, location, per-item access). This **revises SPEC §13's locked
> "Databricks-primary, one-way DBX→SP mirror"** decision — Databricks stays the *brain* (extraction,
> search, metadata) but no longer owns whether/where a file exists or who may see it. **SPEC §13/§14
> must be updated to record this revision** (the one-way mirror is demoted; see S3). This aligns with
> the "SharePoint as the spine" model already in PLAN.md.

Today (audited 2026-09-18) all three of these are **gaps** — sync is additive-only (SP→DBX pull), never
reconciles, and there is no per-item ACL and no DBX→SP mirror. Details in AGENTS.md.

- **S1 — Lifecycle reconciliation: moves & deletes** (req: *what happens if a doc is moved / deleted in
  SharePoint? keep metadata and rehydrate on hash match*). Depends on S3's delta query.
  - **Moved:** today `sp_path`/`sp_web_url` are written only at first import and go stale on any move.
    Refresh them (and the enclosing-folder link, Phase 4 item Q) whenever a tracked `source_ref` reports
    a new parent/path.
  - **Deleted:** today the row + text + fields + volume bytes are silently orphaned. Add a
    **soft-delete** (`status='source_removed'` or a `source_deleted_at` marker) that **keeps all
    metadata + `content_sha256`**, hides the doc from Explore/Manage by default, and audits the event.
  - **Hash rehydration** (user's idea): when identical bytes reappear anywhere in SP (a re-add, or a
    move we couldn't track by `item_id`), **revive the soft-deleted row** — relink `source_ref`/`sp_path`
    and clear the deleted marker — instead of orphaning or re-processing from scratch. Make this
    deliberate (today it's an accidental no-op because we never delete). Reuses the existing
    `content_sha256` twin/dedup machinery (SPEC §9).
- **S2 — Per-file ACL parity** (req: *how do permissions work on viewing docs in the app?*). Today
  access is **site-level only** (`perms_where`/`perms_sites` filter by `sp_site_id`): see a site → see
  every doc from it, even files you couldn't open in SharePoint. Move to **item-level parity**: read each
  item's Graph `/permissions` during sync, persist the item's allowed principals, and extend the
  permission predicate to filter by item ACL (handling broken inheritance / item-level sharing), not just
  site membership. Big lift — call out Graph call volume + storage; reconcile ACLs on the same delta
  sweep as S1. **Also fix now (cheap):** in-app uploads have `sp_site_id = NULL`, so non-FULL users
  can't see them at all — give uploads a first-class scope so they're visible to the right users.
- **S3 — Sync-mechanism upgrade + mirror decision** (req: *how do we sync them with SharePoint?*).
  - **Delta query:** replace the `lastModifiedDateTime` **watermark crawl** with a **Graph delta-token**
    query per drive. Deltas surface **deletes and moves** (which a watermark cannot see) — this is the
    prerequisite that unblocks S1 and keeps S2's ACLs fresh.
  - **DBX→SP mirror (SPEC §13) is currently unbuilt** — `mirror_status` is only ever written
    `not_mirrored`, no worker pushes to SharePoint. Given the source-of-truth flip, **decide whether the
    one-way mirror is still wanted** (it may be demoted to "push confirmed metadata columns back onto the
    SP item" rather than a file mirror). Resolve before building anything against `mirror_status`.

---

## Requirement → phase map

| # | Requirement (user's words) | Phase |
|---|---|---|
| A | Add a Lakebase backend (efficiency) | 2 |
| B | Full UX walkthrough — every button/screen | 4 |
| C | Optimization pass — each function optimal | 3 |
| D | Fable code-cleanup pass (trim, dedupe, fix bugs) | 3 |
| E | View other filetypes like plains-nexus | 4 |
| F | Explore page review | 5 |
| G | All errors written to the log | 1 |
| H | SharePoint-first upload; optional drop | 4 |
| I | Tests for everything | 1 |
| J | Time benchmarks / profiling | 1 |
| K | Scale to multiple users; nexus-style permissions | 2 ✅ |
| L | Contract obligation mgmt (timelines, calendars, relation trees) | 5 |
| M | Fix "edited by a person" on all fields | 0 ✅ |
| N | Doc-type suggestion UX | 4 |
| O | Chat in Explore like contract-explorer | 5 |
| P | Select docs → open in plains-nexus | 5 |
| Q | "Open enclosing folder in SharePoint" | 4 |
| R | Copy extraction/verified results free for identical docs | 3 ✅ |
| S | Handle moved/deleted SP docs: keep metadata, hash-rehydrate | 6 |
| T | View permissions faithful to SharePoint (per-file ACL parity) | 6 |
| U | Sync fidelity — how docs sync with SharePoint (delta query + mirror decision) | 6 |
</content>
</invoke>
