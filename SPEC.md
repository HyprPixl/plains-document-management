# Document Hub — Specification

> Status: **Design draft v0.1** · Date: 2026-09-15 · Owner: cfedyshen
>
> This spec describes a full-scale document management system. It is deliberately
> phased so it can be built and shipped incrementally (see [§18 Roadmap](#18-roadmap)).

### Locked decisions (2026-09-15)

| Area | Decision |
|---|---|
| System of record | ~~Databricks-primary, one-way file mirror to SharePoint~~ → **revised 2026-09-18**: SharePoint is the live source of truth for file lifecycle/access; Databricks is the brain (extraction/search/metadata). See §13 banner. |
| Stack | **Flask + gunicorn Databricks App**, server-rendered vanilla JS, service principal. |
| UI | **Business-facing**: clean, light, plain-language, Plains-branded (not the reference apps' dark theme). |
| Heavy processing | **Handed off** to an async Databricks Job — never in the web process (§10.0). |
| OCR engine | **MS Document Intelligence** (`prebuilt-read`, `output=["pdf"]` → searchable PDF directly), config in §10.0/§10.4; `ai_parse_document` common path; programmatic libs for born-digital. |
| Reference impl | Reuse the existing **Land Records OCR Pipeline** (Azure DI + SHA-256 dedup + idempotent tracking table) — §10.4. |
| File scope (v1) | **PDF-first** (incl. scanned); Word/Excel/PPT/image/email as fast-follow (Phase 1.5). |
| UC location | **`product_dev.document_hub`** schema (matches product convention) + a `docs` volume; promote to prod catalog later. |
| Permissions (v1) | ~~Manual, BU-scoped `permissions` table~~ → **site-scoped, auto-mirrored from each user's SP browse** (shipped); **per-item ACL parity is the committed v2 target** (Phase 6 item T). See §14. |
| Dedup | Content **SHA-256**, surfaced to the user. |
| Field schema | **Admin-configurable** at runtime via `field_defs` (no code deploy). |
| Sensitivity | **Optional field** per Document Type (AI-suggested, human-confirmed) — not a preset. |
| Departments | Finance and Accounting **separate**; IT department is named **IS**. |
| Relationships | **Amendments must link to a parent contract**; documents linkable into a general **tree** (§8.1). |
| Mirror cadence | ~~On-verify + N-minute file mirror sweep~~ → **file mirror dropped 2026-09-18** (never built). Sync is now SP→DBX reconciliation (delta query, move/delete + ACL); optional metadata write-back is a Phase 6 decision. See §13 banner. |
| Scale target | **Thousands → hundreds of thousands** of documents; everything idempotent, crash-safe, resumable (§4A). |

---

## 1. Overview & goals

Document Hub lets anyone at Plains **classify any document and later find it**. A user
(or a connected SharePoint site) brings documents in; the user assigns a small set of
**preset categories** (e.g. Business Unit); AI then **extracts the remaining fields**
determined by that classification; a human **verifies and edits** the results; and the
document becomes fully **searchable, previewable, and downloadable** on the Explore side.

Design principles:

1. **Classification drives extraction.** The document's *type* selects which fields AI
   tries to fill. No universal mega-schema — each type has its own field set, plus a
   small common set applied to everything.
2. **Human-in-the-loop.** AI proposes; a person confirms. Nothing is "done" until a
   human (or an explicit auto-verify rule) marks it verified.
3. **Incremental by construction.** Sources are connected once and then *chipped away
   at* — classify, extract, verify — in any order, at any pace, across many sources.
4. **Never process the same bytes twice.** Every file is content-hashed on the way in;
   duplicates are surfaced to the user instead of silently re-ingested.
5. **Databricks is the brain; SharePoint is the shelf.** All metadata, extraction, and
   search live in Databricks; verified documents + metadata are mirrored to SharePoint
   on a fast cadence so existing SharePoint consumers keep working.
6. **Any file becomes searchable text.** Whatever comes in — PDF, Word, Excel, image,
   photo, email — is turned into text (native parse → OCR → agentic fallback) and gets a
   searchable layer laid over the document, so nothing is a dead end.
7. **Built for business users.** The UI is neat, plain-language, and non-technical; the
   Databricks/AI machinery stays invisible.
8. **Idempotent, concurrent, crash-safe at scale.** Designed for 100k+ documents: every
   action is idempotent and retryable, work is claimed with leases, and progress is
   committed per-document so an interrupted run resumes without losing bulk progress (§4A).

### The two surfaces

| Surface | Audience | Purpose |
|---|---|---|
| **Manage** | Data stewards, contributors, admins | Connect sources, upload, classify, run/monitor extraction, verify & edit, submit. |
| **Explore** | Everyone with access | Search + filter the corpus, preview PDFs/metadata, download. |

The app remembers which surface the user was last on (see [§16](#16-uiux-requirements)).

---

## 2. Scope & non-goals

**In scope (eventually):** multi-source ingestion (in-app upload + connected SharePoint
sites), **any file type** (PDF, Word, Excel, PowerPoint, images/photos, text, email) with
programmatic + OCR + agentic text extraction and a **searchable text layer**, content-hash
dedup, preset + AI-extracted classification, admin-managed field schemas, human
verification queue, mirror-to-SharePoint, permission mirroring, full-text + faceted
search, uniform preview, download.

**Non-goals (v1):**

- Document *editing* / redlining inside the app (view + download only).
- Being an authoritative records-retention / legal-hold system (can feed one later).
- Replacing SharePoint as the place people *store* finished documents — we mirror to it.
- OCR of handwriting / complex forms beyond what `ai_parse_document` handles.
- Real-time (sub-minute) bidirectional sync — mirror is one-way (DBX → SP), fast cadence.

---

## 3. Personas & permissions

| Persona | Can do |
|---|---|
| **Viewer** | Explore: search, preview, download (within their permitted scope). |
| **Contributor** | Everything a Viewer can, plus upload, classify, run extraction, verify/edit, submit — within permitted scope. |
| **Steward** | Contributor across a broader scope (e.g. a whole Business Unit); can reassign/reclassify, bulk-verify. |
| **Admin** | Manage field schemas & taxonomy; connect/disconnect sources; manage the permissions table; see everything. |

Permissions and their mirroring from SharePoint are specified in [§14](#14-permissions).

---

## 4. Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │                 Document Hub (Flask app)       │
   Browser  ──────► │  Manage surface        Explore surface         │
   (Databricks      │  /manage/*             /explore/*              │
    Apps SSO)       │        │                      │                │
                    └────────┼──────────────────────┼────────────────┘
                             │                      │
        ┌────────────────────┼──────────────────────┼───────────────────────┐
        │                    ▼                      ▼                         │
        │   Unity Catalog volumes (files)     Delta tables (metadata, text,   │
        │   /Volumes/<cat>/<schema>/docs/     extraction cache, verifications,│
        │                                     hashes, taxonomy, permissions,  │
        │                                     job_state work-claims)          │
        │                                                                     │
        │   ┌──────────────────────────────────────────────────────────┐    │
        │   │ Document-processing JOB (async, off the web process)       │    │
        │   │  OCR + render + text-layer + field extraction              │    │
        │   │  ai_parse_document / ai_query · MS Document Intelligence · │    │
        │   │  programmatic libs · triggered via Jobs API + schedule     │    │
        │   └──────────────────────────────────────────────────────────┘    │
        └───────────────────────────────┬─────────────────────────────────────┘
                                         │  mirror job (fast cadence)
                                         ▼
                              SharePoint document libraries
                              (verified files + metadata columns)
                                         ▲
                                         │  connector job (pull)
        Connected SharePoint sites ──────┘  (files + existing metadata → volume + index)
```

**Why Databricks-primary (your decision):** the extraction pipeline, dedup index, and
search all want a queryable metadata store and cheap AI calls. SharePoint becomes a
mirror target and an *input* connector, not the transactional store. This mirrors how
`contracts-ver` (extraction/verification) and `contract-explorer` (search) already work.

### Tech stack (your decision: match `contracts-ver`)

- **Backend:** Python + **Flask 3.x**, served by **gunicorn**, deployed as a
  **Databricks App** (`app.yaml`). Runs as a **service principal**.
- **Frontend:** server-rendered HTML shell + **vanilla JS/CSS** (no build step), two
  route trees (`/manage`, `/explore`) sharing components. **Business-facing, non-technical
  look** — clean, light, corporate (Plains branding), *not* the dark "terminal" theme of
  the reference apps. See §16.
- **Data/AI plane:** Databricks SQL warehouse (statement execution API), Unity Catalog
  volumes + Delta tables, Databricks AI functions (`ai_parse_document`, `ai_query`),
  Foundation Model serving endpoint (default `databricks-claude-sonnet-4` or newer).
- **Connectors:** Microsoft Graph API (SharePoint sites, files, metadata, permissions)
  via `azure-identity`.
- **Key deps:** `flask`, `gunicorn`, `azure-identity`, `requests`, `databricks-sdk`,
  `msgraph`/`requests` for Graph, `openai` (for streaming chat/serving calls if reused).
- **Text extraction / OCR (run in the processing job, §10.0 — not the web app):**
  Databricks `ai_parse_document()` (primary OCR/layout), `azure-ai-documentintelligence`
  (MS Document Intelligence, high-accuracy OCR + bounding boxes), `python-docx`,
  `python-pptx`, `openpyxl`/`pandas`, `pypdf`/`pdfplumber` (native text + text-density
  check), `ocrmypdf` (searchable text layer), plus a PDF renderer for Office/image →
  PDF preview. See §10.1.

> **Scaling note carried from `contracts-ver`:** the extraction job lock there is
> per-gunicorn-process. At Document Hub's scale, use a **DB-backed lock / work-claim
> row** in a `job_state`-style table so multiple workers/apps don't double-process.

---

## 4A. Idempotency, concurrency & failure-safety (cross-cutting)

At **thousands → hundreds of thousands of documents**, everything is a resumable pipeline,
never a long-running transaction. **Every action must be idempotent, safely retryable, and
crash-tolerant** — an interrupted or restarted run resumes from the last committed
per-document state and loses at most one document's in-flight work, never bulk progress.

### 4A.1 Per-document state machine (the unit of progress)

Each document advances **independently** through explicit statuses on `documents`
(`classification_status`, `extraction_status`, `verification_status`, `mirror_status`).
Progress is committed **per document**, not per batch — so 99,000 done + 1 crashing means
1 is retried, not 99,000 redone. No stage ever holds a giant multi-doc transaction.

### 4A.2 Claim-based work queue with leases

Workers pull work by **atomically claiming** rows, so N job workers / gunicorn processes
never double-process:

- Claim = `UPDATE ... SET claimed_by, claim_expires_at = now()+lease WHERE status='pending'
  AND (claim_expires_at IS NULL OR claim_expires_at < now()) LIMIT k` (via Delta/SQL
  optimistic concurrency; conflicting writers retry).
- **Leases + heartbeat:** a claim is a time-boxed lease recorded in `job_state`. If a
  worker dies, its lease **expires** and the row becomes claimable again — crashed work is
  auto-recovered, not lost or stuck.
- Work is partitioned by `content_sha256` / hash range to spread load and cut write
  contention.

### 4A.3 Idempotent steps (natural keys + upsert)

Every step is keyed on a **natural key** and written with `MERGE` (upsert), so re-running
it yields the same result:

- **Ingest/dedup:** keyed on `content_sha256` — re-uploading the same bytes never creates a
  second document (§9).
- **OCR / text / render / extraction:** cached by `content_sha256` (+ `prompt_version` +
  `document_type`); a retry reuses the cache instead of re-OCR'ing or re-calling the model.
- **Linking:** `UNIQUE(parent, child, relationship)` — re-linking is a no-op (§8.1).
- **Mirror:** keyed on SP item id / hash — re-mirroring **updates**, never duplicates
  (§13.1).

### 4A.4 External side effects (Doc Intelligence, SharePoint, model calls)

Effects outside Delta are the risky part. Make them safe by: **check-then-act** (does the
SP item / cached result already exist?), **idempotency keys** where the API supports them,
and **write-result-before-advancing** (record the outcome in Delta, *then* flip the status
— so a crash between call and commit re-does an idempotent call, never skips it).

### 4A.5 Failure handling & retries

- Per-document `error_message`, `attempt_count`, `next_attempt_at` (exponential backoff).
- A **poison document** (repeatedly failing) is moved to a **dead-letter status** after N
  attempts and surfaced in the admin jobs panel — it never blocks the queue for others.
- All jobs are **safe to re-run at any time**; a full re-run only reprocesses rows whose
  inputs changed (by hash / `extraction_sig` / `file_modified_at`, §9).

### 4A.6 Incremental, watermark-driven sweeps (no full scans)

Background sweeps select by **watermark** (`updated_at` / `file_modified_at` / status)
against indexed/clustered columns — never a full-table scan of 100k+ rows. Delta tables use
liquid clustering / Z-order on `status`, `content_sha256`, and `updated_at`.

> Every subsequent section (ingest §9, processing §10, verify §11, mirror §13, connectors
> §7) inherits these guarantees; where a section says "enqueue" or "MERGE," this is why.

---

## 5. Data model (Delta tables)

All tables live in one schema, `product_dev.document_hub` (matching the
`product_dev.<product>` convention). Lazily created (`CREATE TABLE IF NOT EXISTS`). Files
live in a UC volume, `/Volumes/product_dev/document_hub/docs/`.

### 5.1 `documents` — one row per document (the spine)

| Column | Type | Notes |
|---|---|---|
| `doc_id` | STRING (uuid) | Primary key. |
| `content_sha256` | STRING | Hash of raw bytes. **Dedup key.** Indexed. |
| `volume_path` | STRING | Where the original file lives in the UC volume. |
| `derived_pdf_path` | STRING | Searchable PDF (OCR text layer over original / rendered from Office/image). See §10.2. |
| `text_source` | STRING | How text was recovered: `native` / `ocr` / `agentic`. |
| `original_filename` | STRING | As uploaded / as in SharePoint. |
| `mime_type`, `size_bytes`, `page_count` | | Basic file facts. |
| `source_id` | STRING | FK → `sources`. Where it came from. |
| `source_ref` | STRING | e.g. SharePoint item id / drive path, or `upload`. |
| `business_unit` | STRING | Preset category (see §6). |
| `document_type` | STRING | Preset category — **drives the extraction schema.** |
| `department` | STRING | Preset category. |
| `classification_status` | STRING | `unclassified` / `classified`. |
| `extraction_status` | STRING | `pending` / `extracting` / `extracted` / `error`. |
| `verification_status` | STRING | `needs_review` / `auto_verified` / `verified`. |
| `mirror_status` | STRING | `not_mirrored` / `mirrored` / `mirror_error`. |
| `extraction_sig` | STRING | md5 of proposed fields (change detection, see §9). |
| `file_modified_at` | TIMESTAMP | Re-queue trigger if source file changes. |
| `claimed_by`, `claim_expires_at` | STRING, TIMESTAMP | Work-lease for safe concurrent processing (§4A.2). |
| `attempt_count`, `next_attempt_at`, `error_message` | INT, TIMESTAMP, STRING | Retry/backoff + dead-letter (§4A.5). |
| `updated_at` | TIMESTAMP | Watermark for incremental sweeps (§4A.6). |
| `created_at`, `created_by` | | |
| `verified_at`, `verified_by` | | |

### 5.2 `document_fields` — extracted/verified field values (long form)

One row per (doc, field). Long form (not wide) so admins can add fields without DDL.

| Column | Type | Notes |
|---|---|---|
| `doc_id` | STRING | FK. |
| `field_key` | STRING | FK → `field_defs.field_key`. |
| `proposed_value` | STRING/JSON | What AI extracted. |
| `confirmed_value` | STRING/JSON | What the human accepted/edited. |
| `confidence` | FLOAT | Optional model/heuristic confidence. |
| `source_provenance` | STRING | `ai` / `human` / `sharepoint` / `preset`. |
| `updated_at`, `updated_by` | | |

### 5.3 `field_defs` — admin-managed field schema (the heart of §12)

| Column | Type | Notes |
|---|---|---|
| `field_key` | STRING | Stable machine key (e.g. `effective_date`). PK. |
| `label` | STRING | Human label. |
| `data_type` | STRING | `date` / `text` / `number` / `currency` / `picklist` / `multi` / `summary` / `entity_list`. |
| `applies_to` | STRING | `common` (all docs) **or** a `document_type` value. |
| `picklist_source` | STRING | Null, an inline option set, or a reference (e.g. a taxonomy table / SharePoint column). |
| `extraction_prompt_hint` | STRING | Optional per-field instruction appended to the prompt. |
| `required_for_verify` | BOOL | Blocks "verified" until confirmed. |
| `sort_order`, `active` | | Ordering & soft-delete. |
| `created_by`, `updated_at` | | Audit. |

> This table is what makes fields **admin-configurable at runtime** — the single biggest
> upgrade over `contracts-ver`, where field schemas were hardcoded in `ENTITY_EXTRACT_FIELDS`.

### 5.4 Supporting tables

- **`sources`** — connected data sources: `source_id`, `kind` (`upload` / `sharepoint`),
  `display_name`, `config` (JSON: site/drive ids, folder scope), `enabled`,
  `last_synced_at`, `created_by`.
- **`hashes`** — optional fast dedup index (`content_sha256` → `doc_id`), or just an
  index on `documents.content_sha256`. See §9.
- **`extraction_cache`** — read-through cache keyed `content_sha256` + `prompt_version` +
  `document_type`, so re-classifying or re-running never re-pays for identical inputs.
- **`document_text`** — recovered text per `doc_id` (+ page), with page offsets /
  bounding boxes where OCR provides them. Powers full-text search and highlight-in-preview
  (§10.2, §15). Cached by `content_sha256` so bytes are never re-OCR'd.
- **`taxonomy`** — option sets for preset categories (Business Unit / Document Type /
  Department) so they're editable without code (see §6).
- **`document_links`** — typed relationships between documents (§8.1): `parent_doc_id`,
  `child_doc_id`, `relationship` (`amendment_of` / `attachment_of` / `related` / …),
  `created_by`, `created_at`. A `UNIQUE(parent_doc_id, child_doc_id, relationship)`
  constraint makes linking idempotent. Powers the amendment→contract requirement and the
  general document tree.
- **`permissions`** — user access rows, mirrored from SharePoint (see §14).
- **`job_state`** — per-job heartbeat, lease/claim bookkeeping, and sweep watermarks for
  the processing/mirror/connector/permission jobs (§4A.2). Enables safe multi-worker
  concurrency and crash recovery.
- **`audit_log`** — who did what (classify / verify / edit / connect / schema change).

---

## 6. Classification taxonomy (starter proposal)

Two tiers: **preset categories** the *human picks at upload* (cheap, high-signal, and
they select the extraction schema), and **AI-extracted fields** (everything else).

### 6.1 Preset categories (human-selected, stored on `documents`)

> **Superseded (2026-09-16):** Business Unit is dropped; org grouping is now **freeform tags**
> and documents are organized by their **SharePoint location** (searchable path). See
> "Model revision — SharePoint as the spine" in `PLAN.md`. Department + Document Type remain.

Chosen to match Plains (midstream oil & gas) and general document-management norms.
All three are editable option sets in the `taxonomy` table.

1. **Business Unit** — *who owns it* (the line of business / asset side). e.g. `Crude Oil`,
   `NGL`, `Canada`, `Corporate`, `Transportation`, `Facilities`. (Aligns with
   `contract-explorer`'s BU-based model.) *Engineering is a **Department**, not a BU — it's a
   function that supports every business unit.*
2. **Document Type** — *what it is.* **This is the schema driver.** Starter set:
   `Contract / Agreement`, `Amendment`, `Invoice`, `Purchase Order`,
   `Land / Right-of-Way`, `Permit / Regulatory`, `Inspection / Integrity Report`,
   `Financial Statement`, `Insurance / Certificate`, `HR / Personnel`,
   `Policy / Procedure`, `Correspondence`, `Project (Engineering)`, `Other`.

   The taxonomy supports an optional `business_unit` scope on a Document Type so the type
   list can filter by the selected BU, while field schemas remain keyed on Document Type in
   `field_defs`. (`Project (Engineering)` is a document *type* — its name reflects the
   Engineering **Department**, and it is not BU-scoped.)
3. **Department / Function** — *who works it* (the internal team). `Commercial`,
   `Operations`, `Land`, `Legal`, `Finance`, `Accounting`, `HSE`, `HR`, `IS`
   (Information Services), `Regulatory`, `Engineering`. (Finance and Accounting are
   **separate**; the IT department is called **IS**.)

**Sensitivity is an optional field, not a preset.** `Public / Internal / Confidential /
Restricted`, defined per Document Type in `field_defs` (AI can suggest per 6.2, human
confirms). It never blocks classification and is only shown where a type opts into it.

> Industry note: the two dimensions that carry the most weight in document-management
> taxonomies are **Document Type/Class** (drives structure & retention) and an
> **org dimension** (Business Unit and/or Department). A third "function" axis is common.

### 6.2 Common AI-extracted fields (every document, `applies_to = common`)

Applied regardless of type — these make search work well and are cheap wins:

- **`title`** — a clean human title (better than the filename).
- **`summary`** — 1–3 sentence analyst-quality abstract of what the doc is/says.
- **`document_date`** — the primary date on the document.
- **`parties` / `entities`** — organizations/people named, with roles where inferable.
- **`topics` / `keywords`** — a few tags for facets & search.
- **`sensitivity`** *(optional, per §6.1)* — `Public/Internal/Confidential/Restricted`,
  AI-suggested and human-confirmed where a Document Type enables the field.
- **`effective_date` / `expiration_date`** — populated when the document has them.

### 6.3 Type-driven fields (examples, `applies_to = <Document Type>`)

Illustrative starter schemas — the real ones are admin-managed in `field_defs`:

- **Contract / Agreement:** `counterparty`, `contract_type`, `term_type`,
  `effective_date`, `expiration_date`, `auto_renewal`, `governing_law`,
  `total_value`, `key_commercial_terms`.
- **Invoice:** `vendor`, `invoice_number`, `invoice_date`, `due_date`, `po_number`,
  `total_amount`, `currency`, `tax_amount`.
- **Purchase Order:** `vendor`, `po_number`, `po_date`, `line_items`, `total_amount`.
- **Land / Right-of-Way:** `grantor`, `grantee`, `legal_description`, `tract_id`,
  `effective_date`, `term`, `consideration`.
- **Permit / Regulatory:** `agency`, `permit_number`, `issue_date`, `expiration_date`,
  `facility`, `permit_type`.
- **Inspection / Integrity Report:** `asset_id`, `inspection_date`, `inspector`,
  `method`, `findings_summary`, `next_due_date`.
- **Project (Engineering)** *(BU = Engineering)*: `asset_location_name` (Asset Location /
  Name), `date_of_record`, `in_service_date`, `afe_number` (AFE #), `record_type` (Type of
  record), `jurisdictional_status` — `Regulated` / `Non-regulated`, plus an optional
  `regulating_body` free-text/picklist (e.g. the specific regulator) when regulated.

Adding/removing any of these is an **admin action** ([§12](#12-admin-field-management)),
not a code change.

---

## 7. Ingestion & connectors

Two ways in; both land files in the UC volume and rows in `documents`.

### 7.1 In-app upload

- Drag/drop or file picker. Single file, or a batch, or **multiple batches (groups)**.
- On receipt: **hash first** (§9), then either flag as duplicate or stage as
  `unclassified`.
- The user then assigns preset categories to the batch (see §8) before extraction.

### 7.2 Connected SharePoint sites (admin)

- Admin connects a site/drive/folder scope via Graph (stored in `sources.config`).
- **Reuse the existing Graph plumbing** from the Land Records pipeline (§10.4): an SPN with
  secrets `sharepointspn--clientid` / `sharepointspn--clientsecret` / `plains--tenant-id`,
  and the `Data_Engineering` `dw_connectionManager` "sharepoint_graph_api" connection.
  Standard Graph calls: `/sites/{host}:{path}` → `/sites/{id}/drive` →
  `/drives/{id}/root:/{folder}` → recursive `children` → `/items/{id}/content` (download).
  (The `LandRecordsResearch-UsrGrp` site is already reachable this way; other target sites
  are pending access — §19.)
- A **connector job** pulls files into the volume + indexes them in `documents`, pulling
  along any *existing* SharePoint metadata (mapped into `document_fields` with
  `source_provenance = sharepoint`, pre-filling where possible).
- Same hashing/dedup applies — a file already in the system (by hash) is linked, not
  re-ingested.
- Runs on a schedule and on-demand; incremental (delta by `file_modified_at` / Graph
  change token).

> This is the "connect multiple data sources and chip away incrementally" requirement:
> sources accumulate; the verification queue simply grows and is worked down over time.

---

## 8. Classification workflow (Manage surface)

1. Documents arrive (§7) as `unclassified`.
2. User selects documents — **individually, as a group, or across multiple groups** —
   and assigns the **preset categories** (Business Unit / Document Type / Department).
   Bulk-apply is first-class (select-all-in-batch → set type).
3. Assigning **Document Type** determines which `field_defs` apply (common + type).
4. User sends the selection **off for AI extraction** (§10). Status → `extracting`.
5. Results return as `needs_review` (or `auto_verified`, §11). User verifies/edits (§11).
6. User **submits** → `verified`. The mirror job (§13) then pushes to SharePoint.

Grouping model: an upload batch or a connector pull is a **group**; users can act on a
whole group, a selection within it, or one document. Groups are a UI convenience over
`documents` rows (e.g. a `batch_id` column), not a separate storage concept.

### 8.1 Document relationships & linking

Documents can be linked to each other (stored in `document_links`, §5.4):

- **Amendments must link to a parent contract.** When a document is classified as
  `Amendment`, the UI **requires** linking it to an existing `Contract / Agreement`
  document (`relationship = amendment_of`) before it can be verified. The linker offers
  search/typeahead over existing contracts; the parent's key fields can pre-fill/anchor
  the amendment's extraction context.
- **General tree linking.** Any document can be linked to any other (`attachment_of`,
  `related`, etc.), forming a navigable **document tree**. A contract shows its amendments
  and attachments as children; the Explore detail view and the verify view both render the
  related-documents tree with click-through.
- **Integrity:** links are directional and typed; a document may have one primary parent
  (e.g. an amendment's contract) plus any number of `related` links. Cycles are prevented
  for hierarchical relationship types. Deleting a document soft-removes its links, never
  orphaning silently.

---

## 9. File hashing & dedup

- Compute **SHA-256 of the raw bytes** on ingest (upload *and* connector), before any
  processing. Store on `documents.content_sha256`; index it.
- On upload, check the hash **before** accepting:
  - **New hash** → accept, stage `unclassified`.
  - **Known hash** → **do not re-ingest.** Tell the user intuitively:
    > "🔁 3 of these 12 files are already in Document Hub." — with a per-file badge
    > (Duplicate / New), a link to the existing document, its current status, and where it
    > came from. User chooses: skip duplicates (default) or link this upload as another
    > source reference to the same doc.
- Dedup is on **content**, not filename — same bytes under a different name = duplicate.
- **Reuse extracted (and verified) results for free.** Because OCR text, extracted fields,
  and confirmed values are all keyed on `content_sha256`, a byte-identical document inherits
  whatever the original already produced instead of re-paying for it:
  - the original's **OCR/text layer** and **derived searchable PDF** (already cached, §10.2);
  - the original's **AI-extracted field values** (`extraction_cache`, §10.3) — no model call;
  - **best of all, if the original is `verified`**, the human-confirmed field values
    (`document_fields.confirmed_value`) can be **copied onto the duplicate** so it lands
    already-verified (or one click from it), with provenance recorded as a copy of the source
    doc — turning a re-upload of a known-good document into a zero-cost, zero-effort add.
  This is the §4A.3 idempotency guarantee paying off as a direct cost/time win, and it is why
  §10.3's cache is keyed the way it is.
- Reuse the `contracts-ver` **change-detection** idea for re-processing: an
  `extraction_sig` (md5 of the proposed field JSON) + `file_modified_at`. A verified doc
  stays verified only while both are unchanged; a changed source file or a schema/prompt
  bump re-queues it.

---

## 10. Text extraction, OCR & field extraction

The chain is **normalize → get text (OCR/parse) → build searchable layer → extract
fields → cache**. It builds on the `contracts-ver` pattern (keeps heavy text in SQL) but
generalizes beyond PDFs to any uploaded file type.

### 10.0 Execution model — heavy work is handed off (decision)

**The Flask web app never does OCR, rendering, or extraction inline.** Those are
CPU/GPU/IO-heavy and slow; running them in gunicorn would block workers and time out.
Instead:

- The web app only **enqueues work** (writes a `pending` row / claim into `job_state`)
  and **reads results** back from Delta. Uploads return immediately with a "being
  processed" status.
- A **Databricks Job / pipeline** (separate from the App) does the heavy lifting:
  OCR, PDF rendering of Office/image files, text-layer generation, and field extraction.
  It runs on a schedule *and* can be kicked on-demand by the app via the Jobs API.
- Work is claimed with a **DB-backed lock** (`job_state` work-claim row) so multiple
  workers never double-process a document.
- **Preferred engines, in order:**
  1. **Databricks AI functions** (`ai_parse_document`, `ai_query`) — primary; keeps data
     in-platform, no extra service, handles layout + OCR for most PDFs/images.
  2. **Microsoft Document Intelligence** (Azure AI Document Intelligence) — **available
     and used in v1** — for scans, photos, complex forms/tables where it outperforms, and
     to get precise word/line **bounding boxes** for the searchable text layer. Called from
     the job. **Concrete config (from the existing Land Records OCR Pipeline, §10.4):**
     - Endpoint `https://westus2.api.cognitive.microsoft.com/`
     - Key: `dbutils.secrets.get(scope="pna-wu2-dm-{ENV}-data-keyv", key="pna-wu2-datamgt-dev-di01--key")`
     - SDK `azure-ai-documentintelligence`; model **`prebuilt-read` with `output=["pdf"]`**
       — DI returns the **searchable PDF directly** (no separate text-layer step needed),
       via `begin_analyze_document(...)` → `get_analyze_result_pdf(model_id, result_id)`.
     - A `has_text_layer()` check skips files that are already searchable (the text-density
       gate of §10.1).
  3. **Programmatic libraries** (python-docx/pptx, openpyxl, pypdf, a PDF renderer) — for
     born-digital Office files and rendering, where no AI/OCR is needed at all.
- The searchable-PDF/text-layer build (§10.2) and the Office→PDF render also live in the
  job, not the app.

> Net: the App is a thin, responsive UI over Delta; all latency-heavy processing is
> asynchronous and horizontally scalable in Databricks compute.

### 10.1 Any file in — programmatic + agentic text extraction

Users can upload more than PDFs (Word, Excel, PowerPoint, images/photos, plain text,
emails). Text is obtained with the **cheapest reliable method first**, escalating to AI
only when needed:

1. **Native / programmatic (preferred, cheap, deterministic):** for born-digital files,
   pull text directly — `.docx`/`.pptx` (python-docx / python-pptx), `.xlsx`/`.csv`
   (openpyxl/pandas → structured text), `.txt`/`.md`, `.eml`/`.msg`, and PDFs that
   already have a text layer. Structured formats (Excel especially) keep sheet/table
   structure so numbers stay meaningful.
2. **OCR (for scans & images):** for image files (JPG/PNG/TIFF/HEIC), scanned PDFs, and
   any file with no/low extractable text, run **OCR** to recover the text — in the
   handoff job (§10.0), never in the web app. Primary path is Databricks
   `ai_parse_document()` (layout + OCR); **Microsoft Document Intelligence** is the
   higher-accuracy option for scans/photos/complex forms and for precise word/line
   bounding boxes. Detect "needs OCR" by low text-density heuristics on the native pass.
3. **Agentic extraction (last resort / hard cases):** for messy multi-format or
   low-confidence documents, a vision-capable model (`ai_query` with an image/document
   input) reads the rendered pages directly to produce text + fields in one pass. This is
   the catch-all so **no file type is a dead end**.

A per-document `text_source` records which path was used (`native` / `ocr` /
`agentic`) for transparency and debugging. Office/image files are also **rendered to a
PDF preview** so the Explore/verify PDF pane works uniformly (see 10.2, §11, §15).

### 10.2 Searchable text layer ("layer OCR on top of the doc")

Once text is recovered, we make it **live with the document**, not just in a table:

- Store the extracted/OCR'd text (with page offsets + bounding boxes where the OCR engine
  provides them) in a `document_text` table keyed by `doc_id` (+ page). This powers
  full-text search (§15) and highlight-in-preview.
- **Produce a searchable PDF**: for scans/images, generate a PDF with an **invisible OCR
  text layer** laid over the original image pages, so the document itself is selectable,
  copy-pasteable, and find-in-page works in the viewer and after download. For
  Office/image sources, this is the rendered PDF + text layer. Original bytes are always
  preserved; the searchable PDF is a derived artifact (`derived_pdf_path` on
  `documents`).
- **Logical point in the chain:** build the text layer *right after OCR/parse and before
  field extraction* — field extraction then reads the same normalized text, and the
  searchable artifact is ready by the time the doc hits the verify queue and Explore.

### 10.3 Field extraction

1. **Build prompt:** a prefix + the field list for this document's schema
   (`common` fields + fields where `applies_to = document_type`), pulled live from
   `field_defs`. Enforce: *"return ONLY a single minified JSON object"*, `null` for
   absent values, `YYYY-MM-DD` dates, picklists constrained to *exactly* their option
   set, per-field `extraction_prompt_hint` where present.
2. **Extract:** `ai_query(EXTRACT_MODEL, prompt + text, returnType => 'STRING')` over the
   normalized text from 10.1; parse the JSON in Python; write to
   `document_fields.proposed_value` (`source_provenance = 'ai'`). Model env-configurable
   (default a current Claude Foundation Model endpoint).
3. **Cache:** read-through `extraction_cache` keyed on
   `content_sha256 + prompt_version + document_type` — re-runs and identical files are
   free. Bump `PROMPT_VERSION` to invalidate after a schema/prompt change. OCR/native
   text is likewise cached by `content_sha256` so we never re-OCR the same bytes.
4. **Long statements:** poll statement execution to completion (large PDFs exceed the
   ~50s API wait cap; `contracts-ver` uses a 270s statement timeout).
5. **Auto-verify sidestep:** if all `required_for_verify` fields are already populated
   from a trustworthy source (e.g. SharePoint metadata) with nothing for AI to add, mark
   `auto_verified` and **skip the LLM call** — the main cost lever.

Cost model: roughly one text-extraction pass + one `ai_query` per document that has empty
required fields; native extraction and cache hits are free. Batch job stages the top-N
most-recently-modified documents; N is configurable and should be raised carefully for
full-tenant runs.

### 10.4 Reference implementation to reuse

An existing Databricks notebook — **"Land Records OCR Pipeline"**
(`/Users/caleb.fedyshen@plains.com/Data_Engineering/projects/product/contracts_obligation_mgmt/`)
— already implements this layer end-to-end and should be **lifted into the processing job**:

- Reads PDFs from the `LandRecordsResearch-UsrGrp` SharePoint site via **Graph API**,
  runs **Azure DI `prebuilt-read` (`output=["pdf"]`)** to produce a searchable PDF, and
  logs to a Delta tracking table.
- **SHA-256 dedup with reuse:** `sha256_hex(bytes)` indexes prior results; identical bytes
  are served the already-searchable PDF (`copied_from_duplicate_hash`) instead of
  re-OCR'ing — exactly the §9 / §4A.3 pattern.
- **Idempotent tracking table** `product_dev.land_records_ocr.ocr_processing_log`:
  `MERGE ... ON drive_item_id`, statuses `success` / `skipped_already_searchable` /
  `copied_from_duplicate_hash`, with `source_file_sha256` + `searchable_pdf_sha256`.
  Repeated runs skip done work and re-process only on source modification — validates §4A.
- `has_text_layer()` gate skips already-searchable files.

> **Divergence to note:** that notebook **overwrites the SharePoint original in place** with
> the searchable PDF. Document Hub is **Databricks-primary**, so instead it keeps the
> original bytes *and* the derived searchable PDF in the UC volume (`derived_pdf_path`) and
> only pushes to SharePoint via the mirror (§13). Reuse the DI/dedup/tracking **logic**, not
> the in-place write-back.
>
> **Catalog note:** this product uses the **`product_dev.<product>`** convention (e.g.
> `product_dev.land_records_ocr`); Document Hub follows it as `product_dev.document_hub`.

---

## 11. Verification workflow (Manage surface)

- **Queue** grouped and status-tabbed: `needs_review` / `auto_verified` / `verified`,
  filterable by source, Business Unit, Document Type, batch.
- **Reviewer view:** side-by-side **PDF preview** + **editable field form** rendered from
  that document's schema. Show proposed vs confirmed, provenance, and confidence where
  available. Field types render appropriately (date pickers, picklists from option sets,
  free text, entity lists).
- **Relationships:** the reviewer can link related documents; for `Amendment` docs a link
  to a parent contract is **required before verify** (§8.1). The related-documents tree is
  shown alongside the field form.
- Human edits → `confirmed_value`. **Submit** requires all `required_for_verify` fields
  filled (and, for amendments, a parent link) → sets `verification_status = 'verified'`,
  `verified_by/at`, recomputes `extraction_sig`.
- **Un-verify** supported (mistake recovery), returns to `needs_review`.
- Bulk verify for stewards on `auto_verified` items.
- "Verified" is the trigger for the mirror to SharePoint (§13) — this answers the
  open question in your brief: **the `verified` flag is what promotes a doc to SharePoint.**

---

## 12. Admin: field management

Admins manage `field_defs` through an admin UI (no code deploy):

- **Add a field** to a Document Type: key, label, data type, picklist options,
  prompt hint, `required_for_verify`. Takes effect on the next extraction for that type.
- **Add/edit a common field** applied to all documents.
- **Edit/retire** a field (`active = false`) — retiring hides it from new extractions
  but preserves history in `document_fields`.
- **Manage preset taxonomy** (`taxonomy` table): Business Unit / Document Type /
  Department option sets.
- Changing a field's schema bumps an effective `prompt_version` for the affected type so
  the extraction cache invalidates cleanly and existing docs can be re-queued.
- All schema/taxonomy edits are written to `audit_log`.

---

## 13. Mirror to SharePoint

> **Revised (2026-09-18) — source-of-truth flip.** The original locked decision
> ("Databricks-primary, one-way DBX→SharePoint file mirror") is **superseded**. SharePoint is now the
> **live source of truth for file lifecycle** — file existence, location, and per-item access all
> originate in SharePoint; Databricks stays the *brain* (extraction, search, metadata, verification)
> but no longer owns whether or where a file exists. Consequences:
> - The **file mirror (pushing bytes DBX→SP) is dropped.** `mirror_status` was never built (only ever
>   written `not_mirrored`) and is not resurrected as a file mirror.
> - What remains open is a **metadata write-back**: pushing Document Hub's confirmed field values back
>   onto the *existing* SharePoint item's columns (no new files created). Whether to build this is a
>   Phase 6 (`ROADMAP.md`) decision, not a locked commitment.
> - Reconciling the corpus **against** SharePoint (detect moves/deletes, refresh links, soft-delete +
>   hash-rehydrate) becomes the primary sync concern — see §14 and Phase 6 (S1/S3).
>
> The subsections below describe the *original* one-way-file-mirror design and are retained for
> history; treat the banner above as authoritative.

Original design (superseded) — one-way, **DBX → SharePoint**, on a fast cadence.

- **Trigger (both):** (a) **on-verify** — enqueue a mirror the moment a document is
  verified/changed, for near-real-time propagation; **and** (b) a **background sweep every
  N minutes** that picks up anything with `mirror_status IN (not_mirrored, mirror_error)`
  (catches missed events, retries failures, and reconciles). Both paths write the same
  `mirror` work queue, so on-verify is just a low-latency nudge and the sweep is the
  safety net — see §13.1 for the concurrency/idempotency guarantees.
- **What's pushed:** the file (if not already resident in the target library) + the
  confirmed metadata mapped to **SharePoint columns**. Map `field_defs.field_key` →
  SharePoint column (a mapping table / naming convention).
- **Target selection:** by Business Unit / Document Type → a specific site + library
  (configurable in `sources`/a mirror-map table).
- **Idempotent:** keyed on `content_sha256` / SharePoint item id; re-runs update, don't
  duplicate. `documents.mirror_status` tracks `mirrored` / `mirror_error`.
- **Conflict rule:** Document Hub's confirmed values win on mirror (it's downstream of
  human verification). For sites *connected as sources*, incoming SP metadata pre-fills
  but never overwrites a human `confirmed_value`.

### 13.1 Mirror concurrency & idempotency

The mirror is a claim-based worker over a `mirror` queue (per §4A):

- A document is claimed with a lease before its SP write; on success, record the SP item id
  + a content/metadata signature and set `mirror_status='mirrored'`; on failure set
  `mirror_error` with backoff. A crash mid-write leaves the row claimable again — the next
  run **re-checks SP by item id/hash and upserts**, so no duplicate item and no lost update.
- On-verify enqueue and the N-minute sweep both target the same queue and same claim, so a
  document is never mirrored twice concurrently, and the sweep safely reconciles anything
  the on-verify path missed or that errored.
- The mirror only re-pushes when the confirmed-metadata/content signature changed, so steady
  state is cheap even across 100k+ documents.

> Alternative considered & rejected for v1: SharePoint-primary. It complicates the
> extraction pipeline, dedup index, and search for little benefit given the AI-heavy
> workload. Revisit if a hard requirement to author in SharePoint emerges.
>
> **Update (2026-09-18): that alternative is now adopted** for lifecycle/access (the "hard requirement"
> materialised — users author and move/delete in SharePoint). Extraction, dedup, and search remain
> Databricks-side exactly as designed; only file existence/location/ACL move to SharePoint as source.

---

## 14. Permissions

> **Revised (2026-09-16):** v1 scope is now the **SharePoint site** a doc lives in (`allowed_site_id`),
> mirrored by capturing each user's visible sites from their delegated browse — not a manual
> `allowed_business_unit` table. See "Model revision — SharePoint as the spine" in `PLAN.md`.
>
> **Revised again (2026-09-18) — per-file ACL parity is now the target.** Site-level scope
> (current, shipped) is an **interim** state, not the destination: today anyone who can reach a site
> sees *every* Doc Hub doc from it, including files they could not open in SharePoint. The committed
> goal is now **per-item ACL parity** — reflect each SharePoint item's own `/permissions` (handling
> broken inheritance and item-level sharing), not just site membership. This is Phase 6 item T in
> `ROADMAP.md`. (Also fix there: in-app uploads have `sp_site_id = NULL` and are invisible to non-FULL
> users — uploads need a first-class scope.)

Goal: **mirror SharePoint permissions faithfully** (per-item where the ACL allows), degrade gracefully
where not.

- **App auth:** Databricks Apps **SSO** in front of the app; identity from forwarded
  headers (`X-Forwarded-Email` etc.), as in `contracts-ver`. Unauthenticated → 401.
- **Authorization model (v1 = manual, decided):** a `permissions` table (pattern from
  `contract-explorer`'s `user_contract_permissions_bu`): rows of `email`, `access_type`
  (`FULL` / `ADMIN` / scoped), and scope (e.g. `allowed_business_unit`, later
  `allowed_site`), **maintained by admins** to start. Every Explore query and Manage
  action is filtered by a `_build_permissions_where()`-style clause. AAD/SharePoint
  mirroring (below) is a later phase that populates this same table.
- **Mirroring from SharePoint:** a job reads site/library permissions via Graph and
  populates `permissions` (map SP groups/members → BU/site scope). Where SP ACLs are too
  granular to mirror 1:1, fall back to **Business-Unit-level** scoping (coarser but
  safe). Document per-item ACLs are a phase-2 refinement.
- **Admin** access is a `permissions` row with `access_type = ADMIN`; only admins reach
  §12 and source connection.

> Honest constraint: true per-document ACL parity with SharePoint is hard and was **not**
> promised for v1. V1 = BU/site-level mirroring (shipped); **v2 (Phase 6, item T) tightens to
> item-level** — this is now committed, not "if needed". Cost to weigh: a Graph `/permissions` read per
> item + storage of each item's allowed principals, reconciled on the same delta sweep as lifecycle (§13
> banner / Phase 6 S3).

---

## 15. Explore surface (search / view / download)

Reuses `contract-explorer` patterns:

- **Full-text search** across `title`, `summary`, `parties`, `topics`, and OCR/extracted
  text; **multi-term AND** (chip freezing).
- **Facets / filters:** Business Unit, Document Type, Department, date ranges
  (document/effective/expiration), source, verification status, sensitivity, custom
  fields.
- **Shareable URLs:** search + filter state encoded in the URL.
- **Result card:** title, type, BU, key dates, source, a status indicator, snippet.
- **Detail view:** metadata panel (all confirmed fields) + inline **preview** of the
  searchable derived PDF (§10.2) with find-in-page/highlight over the OCR text + **download**
  (searchable PDF and/or original file) + **related-documents tree** (§8.1) with
  click-through to amendments/attachments.
- **Optional AI chat** over the corpus (as `contract-explorer` does via a Claude serving
  endpoint) — nice-to-have, later phase.
- Everything filtered by the caller's `permissions` scope.

---

## 16. UI/UX requirements

**Audience: business users, not engineers.** The UI must be neat, clean, and
non-technical — it should feel like a polished corporate web app, not a developer tool.

- **Business-facing visual design:** light, professional theme with Plains branding;
  generous whitespace, clear typography, obvious primary actions. CSS variables for
  theming. Explicitly **not** the dark "terminal" aesthetic of `contracts-ver` /
  `contract-explorer` (reuse their *logic*, not their look).
- **Plain language everywhere:** no jargon, table names, model names, or status codes in
  the UI. Statuses read as human phrases ("Needs review", "Verified", "Being processed…"),
  not enum values. Errors are friendly and actionable.
- **Guided, low-friction flows:** upload → classify → review → done should be obvious
  without training; sensible defaults, inline help, empty states that explain what to do.
- **Two route trees:** `/manage/*` and `/explore/*`, shared shell/components.
- **Remember last surface:** persist the last-visited surface (Explore vs Manage) in
  `localStorage`; on load, route the user back there. (Explicit requirement.)
- **Duplicate feedback:** clear, non-blocking per-file Duplicate/New badges on upload
  (§9), phrased plainly ("Already in Document Hub").
- **Bulk actions:** multi-select with a sticky action bar (classify / send to extract /
  verify) across a group or a selection.
- **Status is always visible:** every document shows classification / extraction /
  verification / mirror status via consistent, human-readable chips.
- **Uniform document preview:** every document (PDF, Office, image) previews through the
  same viewer via the searchable derived PDF (§10.2), with find-in-page over the OCR text.
- **Responsive-enough** for laptop use; heavy tables virtualized for large corpora;
  accessible (keyboard nav, contrast, labels).

---

## 17. Deployment & operations

- **Databricks App**: `app.yaml` (gunicorn command + env), service principal identity.
- **Env/config:** schema & table names, volume path, `EXTRACT_MODEL`, `PROMPT_VERSION`,
  SQL warehouse id, Graph/Dataverse secrets, `JOB_*_LIMIT`s, mirror cadence.
- **Least privilege:** service principal gets `USE_SCHEMA` + `CREATE_TABLE` on the
  schema and `MODIFY`+`SELECT` on its own tables only — **no** write on read-only
  ingestion inputs (pattern from `contracts-ver`).
- **Jobs (all off the web process, §10.0):** document-processing (OCR + render +
  text-layer + field extraction), connector-sync, mirror, permission-sync — each with a
  DB-backed work-claim in `job_state` for multi-worker safety. The web App enqueues and
  triggers these via the Databricks Jobs API and reads results from Delta.
- **Observability:** `audit_log` + structured app logs; a jobs/health panel in the admin
  UI (queue depths, last sync times, error counts).
- **Secrets** via Databricks secret scopes; tokens refreshed before expiry (~55 min TTL
  pattern from `contracts-ver`).

---

## 18. Roadmap

Each phase is shippable on its own.

- **Phase 0 — Skeleton.** Databricks App scaffold, SSO, `documents` table, UC volume,
  in-app upload + **hashing/dedup** with duplicate UX. Business-facing Explore/Manage shell
  + last-surface memory.
- **Phase 1 — Text + classify + extract + verify (PDF-first).** Processing job for
  **PDF text + OCR (Doc Intelligence / `ai_parse_document`) + searchable text layer &
  derived PDF (§10)**, preset taxonomy, `field_defs` + starter schemas, field extraction,
  verification queue with uniform preview & editable form, `verified` flag. (Core loop =
  `contracts-ver` at larger scope.)
- **Phase 1.5 — All file types.** Add native extractors + render-to-PDF for Word, Excel,
  PowerPoint, images/photos, text, email into the same processing job (native → OCR →
  agentic chain, §10.1).
- **Phase 2 — Explore.** Full-text + faceted search, detail view, download, shareable
  URLs. (`contract-explorer` patterns.)
- **Phase 3 — Admin field management.** Runtime `field_defs` + taxonomy editing,
  cache/prompt versioning, audit log.
- **Phase 4 — Connectors + mirror.** Connect SharePoint sites (pull + metadata), mirror
  verified docs → SharePoint, incremental sync.
- **Phase 5 — Permissions mirroring.** BU/site-level from Graph, then tighten toward
  item-level.
- **Phase 6 — Nice-to-haves.** Corpus AI chat, sensitivity/PII automation, retention
  hooks, analytics dashboard.

---

## 19. Open questions

**Resolved (see Locked decisions):** UC location · permissions (manual BU table) · file
scope (PDF-first) · OCR (Doc Intelligence) · sensitivity (optional field) · departments
(Finance/Accounting split, IS) · relationships (amendment→contract + tree) · mirror cadence
(on-verify + sweep) · scale (100k+, idempotent/crash-safe).

Still open / blocked:

1. **Target SharePoint site(s) + library layout** — ⛔ *blocked: waiting on access to the
   SharePoint sites.* Need real SP coordinates + the BU/type → site+library mapping for the
   mirror and for any *new* sites to connect as sources. (The `LandRecordsResearch-UsrGrp`
   site is already reachable via the existing SPN — §7.2.) Does **not** block Phase 0.
2. **Preset taxonomy sign-off** — confirm the Business Unit / Document Type / Department
   option sets in §6 with the business (structure is settled; values may adjust).
3. **Retention / legal hold** — out of scope for v1; reserve schema fields now?

Resolved this session — **Doc Intelligence config** (§10.0/§10.4): endpoint
`westus2.api.cognitive.microsoft.com`, key secret `pna-wu2-datamgt-dev-di01--key` in scope
`pna-wu2-dm-{ENV}-data-keyv`, model `prebuilt-read` `output=["pdf"]`; and an existing
end-to-end reference pipeline to reuse.
