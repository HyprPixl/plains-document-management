# Performance baseline — pre-Lakebase

Captured **2026-09-16** against the deployed app (commit `f672316`) via the in-app
`POST /api/admin/bench` endpoint (n=20, admin/unrestricted scope — worst-case widest read).
This is the reference the Phase 2 (Lakebase) and Phase 3 (optimization) work is measured against.
Re-run the button after each change and compare.

## Hot-path query latency (server-side warehouse round-trip, ms)

| Operation | p50 | p95 |
|---|---|---|
| documents (queue list) | 549 | 951 |
| stats (by-status + unclassified) | 1618 | 1799 |
| search (title/summary/text joins) | 575 | 2344 |
| document (row + fields + defs + tags, 4 queries) | 1937 | 3106 |

Whole bench run: `wall_ms ≈ 98,000` for ~80 queries.

## What this proves

The numbers are dominated by **Statement Execution API fixed overhead**, not data volume — the
corpus is currently tiny. Evidence from the same session's slow-query log:

- `SELECT doc_id … LIMIT 1` (returns one string) → **5,490 ms** once.
- `SELECT access_type, allowed_site FROM permissions WHERE lower(email)=…` (single row) →
  **1,246–1,301 ms**, and it fires **2–3× per page load** (every request runs `perms_where`).
- `document` fetch is ~1.9 s p50 because it issues **4 sequential** warehouse calls; the per-call
  tax compounds.

So the cost is ~0.5–2 s of *fixed round-trip latency per statement*, paid on every request, several
times over. A tiny single-row lookup taking >1 s is the tell.

## Implications for Phase 2 / 3

- **Lakebase (Postgres OLTP):** single-digit-ms round trips. The permission lookup goes
  ~1300 ms → ~1–5 ms; the 4-query document fetch goes ~1900 ms → tens of ms. Expected **10–100×**
  on the hot path — this is the core efficiency goal.
- **Cheap pre-Lakebase wins (Phase 3, or now):** the per-request `perms_where` permission lookup is
  issued redundantly (2–3×/page) — cache it per-request/session. Batch the `document` endpoint's 4
  sequential queries. These help regardless of the data layer.
- Target after Lakebase: hot endpoints p95 well under ~100 ms.

## Phase 2 result — permissions slice (measured 2026-09-16, on deploy)

The thin slice (permissions read cutover to Lakebase behind `USE_LAKEBASE_PERMISSIONS`, see
`lakebase.py` + AGENTS.md "Phase 2") is deployed and proven. Measured from the app log on a
**warm** load (cold-start right after deploy is ~12 s and not representative):

- **`/api/me` (essentially just `get_perms()`): ~1,300 ms → 5 ms** — the single-row permission
  lookup is now a Postgres round-trip, not a Statement-Execution call. **~260× on that path.**
- The `SELECT access_type, allowed_site FROM …permissions` warehouse slow-query line is **gone**;
  no `lakebase perms read failed` fallbacks.

Not yet migrated (still warehouse, so unchanged): `documents` (~1.1 s warm), `stats`, `search`,
`document`, `taxonomy`, `sp_sessions`. These are the next slice — migrating their reads the same
way is what will move the admin "Run benchmark" numbers (that endpoint times these warehouse
queries; the permission win doesn't show there because its one lookup is `g`-cached outside the
timed loops).
</content>
