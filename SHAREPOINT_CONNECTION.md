# SharePoint Connection — design & porting guide

This describes the SharePoint import + auto-sync feature built in **plains-nexus**,
and how to bring the same capability into **doc-hub**. Read the "Porting to
doc-hub" section carefully — doc-hub's architecture (4 gunicorn workers,
SQL-warehouse-backed state, an existing processing job) means the plains-nexus
code cannot be copied verbatim; several pieces must be adapted.

Reference implementation lives in `plains-nexus/app.py` (search for `_sp_` /
`sharepoint`) and `plains-nexus/static/index.html` (search for `spState` /
`sharePoint`). Commits: `f832148` (Stage 1), `38d534a` (yaml), `bcb4a4c` (scope
fix), `ae12049` (Stage 2). All on GitHub/dev.

---

## 1. What it does (product)

A **file manager** for a project connects **their own Microsoft account** and:

- **One-time import** — browse the SharePoint sites / document libraries /
  folders *they can already see*, tick files or folders, and copy them into the
  project. Imported files flow through the normal preview / summary / search
  pipeline exactly like an upload.
- **Auto-sync (opt-in, "fickle")** — mark a folder to "keep in sync". A
  background loop periodically re-imports files added or changed in that folder,
  running **as that user** via a stored refresh token. When the stored sign-in
  expires or is revoked, the sync pauses and the UI nudges a manager to reconnect.

The whole thing is **delegated** — see §3 for why that choice matters.

---

## 2. Key design decisions

### Delegated (on-behalf-of the user), NOT app-only
The user did **not** want to add a service principal to every individual
SharePoint site. So instead of an app-only SPN with `Sites.Read.All`
application permission (which would need per-site or tenant-wide grants and
would see *everything*), each uploader authenticates with their **own** Microsoft
identity via the OAuth **authorization-code** flow. Access is then naturally
scoped to whatever that person can already open in SharePoint. Nothing to
provision per site.

> ⚠️ **doc-hub already has an *app-only* SPN wired** (`SP_CLIENT_ID_KEY` /
> `SP_CLIENT_SECRET_KEY` / `SP_TENANT_ID_KEY` in `config.py`, used by the
> processing job). That is a *different* auth model. This feature is delegated.
> Don't conflate them — see §7.

### Read-only, minimal scopes
`openid profile offline_access Files.Read.All Sites.Read.All`. All are
**admin-consented tenant-wide** already. Notes:
- `User.Read` was deliberately **removed** — it triggered an admin-approval wall
  in a tenant with user consent disabled, and we don't need it (the app knows
  the user from the `X-Forwarded-Email` header, not from Graph `/me`).
- `offline_access` is what yields the **refresh token** that powers auto-sync.

### Refresh tokens: encrypted, persisted, rotated
- Stored **encrypted** at rest. The Fernet key is **derived from the Azure
  client secret** (`base64.urlsafe_b64encode(sha256(client_secret))`), so there
  is **no new secret to provision**. Rotating the client secret invalidates
  stored tokens (they'd need re-auth) — acceptable.
- Entra **rotates** the refresh token on every use, so the loop must persist the
  newly-returned token each cycle.
- A dead grant (`invalid_grant` / `interaction_required` / `unauthorized_client`
  / `consent_required`) flips the sync to `needs_reauth` instead of retrying
  forever.

### "Fickle" by nature
Delegated refresh tokens expire / get revoked readily (password change, admin
session revoke, ~90-day inactivity, conditional-access). The UI is honest about
this: a warning at setup + a per-project reconnect banner when a token dies.

---

## 3. Azure / Entra prerequisites

App registration used by plains-nexus: **`pna-datamgmt-terraform-appreg`**
(appId `bf2ea6db-ddc2-4278-b5cb-ac8aec369156`, tenant
`e3267a76-a858-4ead-a71a-fa49332a6879`), Terraform-managed.

For any app that uses this delegated flow you need:

1. **Redirect URI** (Web platform) registered **exactly** matching the app's
   callback: `https://<app-host>/api/sharepoint/callback`. Each environment/host
   is a distinct URI and must be added. plains-nexus derives the host from
   `X-Forwarded-Proto` / `X-Forwarded-Host` at runtime (or an explicit
   `APP_BASE_URL`) so the value it sends matches what's registered.
2. **Delegated** Graph permissions with **admin consent granted**:
   `Files.Read.All`, `Sites.Read.All`, `offline_access`, `openid`, `profile`.
3. A **client secret** available to the app via a Databricks secret scope.

> For **doc-hub** you must add doc-hub's callback URL as a redirect URI on the
> chosen app registration, and confirm the delegated scopes above are
> admin-consented for it. Easiest path: reuse the **same** app registration as
> plains-nexus (its scopes + consent are already in place; just add doc-hub's
> redirect URI).

---

## 4. Backend shape (plains-nexus reference)

Config / state:
```
_SP_AUTHORITY   = "https://login.microsoftonline.com"
_SP_GRAPH       = "https://graph.microsoft.com/v1.0"
_SP_SCOPES      = "openid profile offline_access Files.Read.All Sites.Read.All"
_SP_SYNC_FILE   = "sharepoint_sync.json"       # per-project sidecar on the volume
_SP_SYNC_INTERVAL = 1800                        # background cadence (sec)

_sp_oauth_states  : dict   # state -> {email, project_id, ts}   (in memory)
_sp_user_tokens   : dict   # email -> {access_token, expires_at, refresh_token} (in memory)
_sp_lock, _sp_sync_lock : Lock
```

Helpers (token-based so both the interactive flow and the background loop reuse them):
- `_sp_configured()` — all of tenant/client/secret present.
- `_sp_redirect_uri()` — from `APP_BASE_URL` or `X-Forwarded-*`.
- `_sp_access_token(email)` — cached delegated access token (or None if expired).
- `_sp_graph_token(token, url, params)` / `_sp_graph_get(email, ...)` — Graph GET.
- `_sp_list_children_token(...)` / `_sp_list_children(email, ...)` — paginated,
  `$select=id,name,size,folder,file,lastModifiedDateTime`.
- `_sp_walk_files_token(...)` — recurse a folder subtree (depth ≤ 12) into files.
- `_sp_download_token(...)` / `_sp_download_file(email, ...)`.
- `_sp_store_file(w, vol_path, project_id, token, drive_id, item)` — download +
  upload into the project store + evict caches + trigger prerender/summary.
- `_sp_fernet()` / `_sp_encrypt()` / `_sp_decrypt()` — token encryption.
- `_sp_load_syncs(pid)` / `_sp_save_syncs(pid, data)` — sidecar read/write.
- `_sp_refresh_access(refresh_token) -> (access_token, new_refresh_token)` —
  raises `_SPReauth` on a dead grant.
- `_sp_run_sync(pid, sync, w)` — refresh, rotate+store token, import files whose
  `lastModifiedDateTime` > watermark, update `last_synced`.
- `_sp_sync_tick()` — enumerate projects, run each active sync; merge-save under
  lock so concurrent add/delete isn't clobbered.
- `_sp_scheduler_loop()` / `_sp_start_scheduler()` — daemon thread, started once
  at boot (`_sp_start_scheduler()` after `_prewarm()`).

Endpoints:
| Method | Route | Purpose |
|---|---|---|
| GET  | `/api/sharepoint/login`   | Redirect to Entra authorize (stores state, gated by file-manager) |
| GET  | `/api/sharepoint/callback`| Exchange code → tokens (stores access+refresh in memory), popup closes |
| GET  | `/api/sharepoint/status`  | `{configured, connected, sync_supported, can_sync}` |
| GET  | `/api/sharepoint/sites`   | Search sites the user can see |
| GET  | `/api/sharepoint/drives`  | Drives (document libraries) of a site |
| GET  | `/api/sharepoint/items`   | Children of a drive folder |
| POST | `/api/sharepoint/import`  | Copy selected files/folders into the project |
| GET  | `/api/sharepoint/syncs`   | List a project's sync configs (no secrets) |
| POST | `/api/sharepoint/syncs`   | Arm auto-sync for a folder (stores encrypted refresh token) |
| DELETE | `/api/sharepoint/syncs/<id>` | Remove a sync |
| POST | `/api/sharepoint/syncs/<id>/reconnect` | Re-arm a dead sync with the current user's fresh token |

Sync config record (persisted, minus secret when returned to the UI):
```json
{
  "id": "...", "site_name": "...", "drive_id": "...", "drive_name": "...",
  "folder_id": "... or null (library root)", "folder_name": "...",
  "user_email": "...", "refresh_token_enc": "<fernet>",
  "token_status": "ok | needs_reauth", "last_error": null,
  "created": 1700000000.0, "last_synced": 1700000000.0
}
```

**Change detection** is watermark-based (import files with
`lastModifiedDateTime` newer than `last_synced`), not Graph delta tokens —
simpler and robust; no delta-link bookkeeping. Trade-off: it lists the whole
subtree each tick.

---

## 5. Frontend shape (plains-nexus reference)

- **Import modal** (`#sharepointModal`): connect prompt → breadcrumb browser
  (sites → drives → folders/files) with search + multi-select.
- **Auto-sync opt-in** (`#sp-sync-control`): a "Keep <folder> in sync" checkbox
  shown only when inside a library/folder and `sync_supported && can_sync`, with
  an explicit **fickle warning**. The import button becomes "Import & sync" /
  "Set up sync".
- **Synced folders list** (`#sp-synced-section`): each sync with owner, last-sync
  time, active/needs-reconnect badge, remove + reconnect buttons.
- **Project reauth banner** (`#sp-reauth-banner` in the sidebar): shown to file
  managers when any of the project's syncs is `needs_reauth`; Reconnect runs the
  OAuth popup then calls `/syncs/<id>/reconnect`.
- JS entry points: `openSharePointModal`, `sharePointConnect`, `spLoadSites/
  Drives/Items`, `sharePointImport`, `spLoadSyncs`, `spReconnectSync`,
  `spRemoveSync`, `refreshSharePointBanner` (called from `updateFileManagerUI`).

---

## 6. Lifecycle / persistence facts (FAQ answered)

- **In-memory** `_sp_user_tokens` / `_sp_oauth_states` are cleared on app
  restart. Effect: a user re-clicks **Connect** next session. No data lost.
- **Auto-sync refresh tokens persist** (encrypted, on the volume sidecar in
  plains-nexus / in a table for doc-hub), so **nightly stop/start does NOT wipe
  syncs**. The loop reads them back each cycle.
- While the app is **off overnight**, no syncs run; on startup the first tick
  catches up anything modified since the last successful sync (watermark-based).
- The **Microsoft refresh token itself** can still expire/revoke server-side
  (~90 days, password change, admin revoke) — that's the "fickle" path handled
  by `needs_reauth` + reconnect.

---

## 7. Porting to doc-hub — what must change

doc-hub is **not** a drop-in target. Key differences and required adaptations:

### 7a. Multiple gunicorn workers (CRITICAL)
doc-hub runs `-w 4` (`app.yaml`). The plains-nexus design assumes **one**
worker for three reasons that all break with 4:
1. `_sp_oauth_states` and `_sp_user_tokens` are in-process dicts. With 4 workers,
   the `/login` request and the `/callback` (or later `/sites`) can land on
   different workers → "not connected" / lost OAuth state.
2. The background scheduler thread would run **4×** concurrently → duplicate
   imports, racey token rotation.

**Options (pick one):**
- **Simplest:** run the web app with **1 worker** (like plains-nexus). Fine if
  load is low. Uses more memory per request slot but removes all shared-state
  problems.
- **Proper:** externalize the ephemeral state:
  - Store `oauth_states` and delegated tokens in the **SQL warehouse** (short-TTL
    rows) instead of dicts, keyed by state / user email. Then any worker can
    complete the flow. (Access tokens are short-lived; you can also just re-mint
    from the stored refresh token on demand and skip caching access tokens.)
  - Move the recurring sync **out of the web app entirely** — see 7c.

### 7b. SQL-backed storage, not JSON sidecars
doc-hub keeps everything in Databricks tables via `db.py` (`config.py` table
list). Replace the `sharepoint_sync.json` sidecar with a table, e.g.:
```
product_dev.document_hub.sharepoint_syncs(
  id STRING, source_id STRING /* or project scoping */,
  site_name STRING, drive_id STRING, drive_name STRING,
  folder_id STRING, folder_name STRING, user_email STRING,
  refresh_token_enc STRING, token_status STRING, last_error STRING,
  created DOUBLE, last_synced DOUBLE)
```
There's already a `SOURCES` table and `DOCUMENT_LINKS` — decide whether a synced
SharePoint folder is modeled as a `source`. That's the more idiomatic fit and
would let provenance (`sources` / `document_links`) point back at SharePoint.

### 7c. Use the existing processing job for the recurring sync
doc-hub already has a **processing job** with work-claim leases
(`CLAIM_LEASE_SECONDS`, `MAX_ATTEMPTS`, `JOB_STATE`, `processing/`). The
recurring sync belongs there, not in an in-web-app daemon thread:
- A scheduled job run enumerates `sharepoint_syncs`, claims each with a lease
  (so concurrent/duplicate runs are prevented — solves 7a #2 cleanly), refreshes
  the token, and enqueues/imports changed files through doc-hub's existing
  ingestion path (Document Intelligence extraction, `documents` / `document_text`
  tables) rather than plains-nexus's `w.files.upload` + prerender.
- The web app then only handles the **interactive** OAuth + browse + arm/remove,
  and reads sync status for the UI.

### 7d. Ingestion path differs
plains-nexus `_sp_store_file` uploads raw bytes to a volume and lets its own
preview/summary pipeline run. doc-hub ingests into tables via its processing
pipeline. So the "import a file" primitive must call **doc-hub's** ingestion
(land bytes in `DOCS_VOLUME`, insert a `documents` row, let the job extract),
not copy plains-nexus's caching/prerender calls.

### 7e. Config / secret wiring
- Add delegated app-registration config. Reuse plains-nexus's app reg
  (appId `bf2ea6db-…`, tenant `e3267a76-…`) **or** add delegated redirect +
  consent to doc-hub's own. Note doc-hub's current `sharepointspn--*` secret is
  the **app-only** SPN — likely a different registration without a delegated
  redirect URI or `offline_access` consent; verify before reusing it.
- Register doc-hub's `https://<host>/api/sharepoint/callback` redirect URI.
- Encryption key: same trick (derive Fernet key from the delegated client
  secret) — no new secret. Add `cryptography` to `requirements.txt`.
- Auth/identity: plains-nexus gates on a project "file manager" role via
  `X-Forwarded-Email`. doc-hub must map this to its own `permissions` model.

### 7f. Scope check
`sync_supported` should reflect whether encryption is available; `can_sync`
whether the connected session returned a refresh token. Keep both so the UI can
degrade gracefully if delegated config is missing.

---

## 8. Security checklist (applies to both apps)

- Client secret only via secret scope — never plaintext in git-tracked yaml.
- Read-only Graph scopes only.
- Refresh tokens encrypted at rest; never returned to the browser.
- Redirect URI is an exact allow-list match; don't accept arbitrary hosts.
- Gate every endpoint on the caller's file-manager / permission role.
- The reconnecting user becomes the sync's effective identity — intended, but
  document it (their access governs what the sync can pull thereafter).
