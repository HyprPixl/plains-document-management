"""Lakebase (Databricks-managed Postgres) access layer — Pattern A.

Additive, behind-a-flag alternative to the warehouse `db.py` for the hot
transactional reads whose latency is dominated by Statement-Execution round-trip
overhead (see `bench/BASELINE.md`). A Postgres round-trip is single-digit ms vs.
the ~0.5–2 s fixed tax per warehouse statement.

Design (matches the sibling `dbx-deal-capture-app` on the same Lakebase instance):

- **Inert offline / in tests.** If `PGHOST` is unset (or psycopg2 isn't installed)
  `enabled()` is False, every helper no-ops, and callers fall back to the warehouse
  `db.py`. The offline pytest suite stays green and the modules stay import-safe under
  `conftest`'s stubs — nothing here does real I/O at import time.
- **Own schema.** The Lakebase instance is SHARED with `dbx-deal-capture-app`, which
  owns `public`. Document Hub lives entirely in its own `document_hub` schema and never
  touches `public`; `CREATE SCHEMA IF NOT EXISTS` runs on bootstrap.
- **Auth = a freshly minted SP OAuth token**, not a static secret — so a long-lived
  gunicorn worker rotates cleanly (the injected `PGPASSWORD` isn't refreshed in-process).
  `LAKEBASE_TOKEN` is an ephemeral local-probing override only; it is NEVER persisted.
- **Parameterized queries only** (`%s`). Do NOT reuse `db.lit()` string interpolation.
"""
import logging
import os
import threading
import time

import config

try:  # job-only / prod dep — absent in the offline test env, which is fine (inert then)
    import psycopg2
    import psycopg2.extras
except Exception:  # pragma: no cover - import guard
    psycopg2 = None

# ─────────────────────────────────────────────────────────────── logging ──
# Same stdout shape as db.py / app.py; slow round-trips surface at WARNING.
logger = logging.getLogger("doc_hub.lakebase")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    logger.propagate = False

# Connection coordinates. On a Databricks App with the `database` resource bound, the
# platform injects PGHOST/PGDATABASE/PGUSER/PGPORT (+ PGPASSWORD). The LAKEBASE_* values
# are the same instance the sibling app uses, kept as a fallback default.
LAKEBASE_HOST = os.getenv("LAKEBASE_HOST", "ep-flat-moon-ee1bjbvj.database.westus2.azuredatabricks.net")
LAKEBASE_DB = os.getenv("LAKEBASE_DB", "databricks_postgres")
SCHEMA = os.getenv("LAKEBASE_SCHEMA", "document_hub")

PERMISSIONS = f"{SCHEMA}.permissions"


def enabled() -> bool:
    """True only when psycopg2 is importable AND a Postgres host is configured.

    Offline/tests leave PGHOST unset → inert → callers use the warehouse.
    """
    return bool(psycopg2) and bool(os.getenv("PGHOST"))


# ───────────────────────────────────────────────────────── token minting ──
_token_cache: dict = {}
_token_lock = threading.Lock()


def _get_sp_token():
    """Mint a short-lived Lakebase OAuth token from the app SP client credentials.

    Cached in-process with a 60s early-refresh margin (thread-safe). All three inputs
    are auto-injected to the app SP on a Databricks App. Returns None if unavailable.
    """
    with _token_lock:
        if _token_cache.get("token") and time.monotonic() < _token_cache.get("expires_at", 0) - 60:
            return _token_cache["token"]
        host = os.getenv("DATABRICKS_HOST", "").rstrip("/")
        if host and not host.startswith("http"):
            host = "https://" + host
        client_id = os.getenv("DATABRICKS_CLIENT_ID", "")
        client_secret = os.getenv("DATABRICKS_CLIENT_SECRET", "")
        if not (host and client_id and client_secret):
            return None
        try:
            import requests
            resp = requests.post(
                f"{host}/oidc/v1/token",
                data={"grant_type": "client_credentials", "scope": "all-apis"},
                auth=(client_id, client_secret),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            _token_cache["token"] = data["access_token"]
            _token_cache["expires_at"] = time.monotonic() + data.get("expires_in", 3600)
            return _token_cache["token"]
        except Exception as e:
            logger.error(f"SP token fetch failed: {e}")
            return None


def _get_sdk_credential():
    """Mint a Lakebase credential via the Databricks SDK using *ambient* auth.

    This is the path for the **processing job**: a Databricks Job running as the app SP
    (or any identity) authenticates the SDK ambiently, but — unlike an App — is NOT given
    `DATABRICKS_CLIENT_ID/SECRET` env, so `_get_sp_token()` can't mint via OIDC. The SDK's
    `generate_database_credential` returns a short-lived token for the run-as identity
    (which must hold a Postgres role on the instance). Cached in the same dict/lock as the
    OIDC token. Returns None when the SDK/instance is unavailable (→ inert, warehouse path).
    """
    instance = os.getenv("LAKEBASE_INSTANCE", "plains-lakebase")
    with _token_lock:
        if _token_cache.get("token") and time.monotonic() < _token_cache.get("expires_at", 0) - 60:
            return _token_cache["token"]
        try:
            import uuid
            from databricks.sdk import WorkspaceClient
            cred = WorkspaceClient().database.generate_database_credential(
                request_id=str(uuid.uuid4()), instance_names=[instance]
            )
            _token_cache["token"] = cred.token
            # SDK creds are ~1h; refresh a minute early like the OIDC path.
            _token_cache["expires_at"] = time.monotonic() + 3600
            return cred.token
        except Exception as e:
            logger.error(f"SDK Lakebase credential fetch failed: {e}")
            return None


def _password() -> str | None:
    """Resolve the connection password, most-preferred first.

    1. `LAKEBASE_TOKEN` — ephemeral local-probing override (a personal token). Never
       persisted anywhere; discarded when the shell env goes away.
    2. A freshly minted SP OAuth token — the App production path (OIDC client-credentials,
       rotates in-process). Needs `DATABRICKS_CLIENT_ID/SECRET` (auto-injected on Apps).
    3. An SDK-minted credential via ambient auth — the **Job** path (Apps-only client-creds
       env is absent there); works for the SP or user the job runs as.
    4. The injected `PGPASSWORD` — last-resort shortcut (not refreshed, can expire).
    """
    tok = os.getenv("LAKEBASE_TOKEN")
    if tok:
        return tok
    tok = _get_sp_token()
    if tok:
        return tok
    tok = _get_sdk_credential()
    if tok:
        return tok
    return os.getenv("PGPASSWORD")


# ─────────────────────────────────────────────────── thread-local conn ──
_tls = threading.local()


class _PooledConn:
    """Wraps a real psycopg2 connection so `.close()` is a no-op — the connection
    lives for the thread's life; only credential change / a dead socket recreates it."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def close(self):
        pass

    @property
    def closed(self):
        return self._conn.closed


def _connect():
    user = os.getenv("PGUSER", "")
    host = os.getenv("PGHOST", LAKEBASE_HOST)
    database = os.getenv("PGDATABASE", LAKEBASE_DB)
    port = int(os.getenv("PGPORT", "5432"))
    token = _password()
    key = (user, host, database)

    raw = getattr(_tls, "conn", None)
    if raw is not None and getattr(_tls, "conn_key", None) == key and raw.closed == 0:
        return _PooledConn(raw)
    if raw is not None:
        try:
            raw.close()
        except Exception:
            pass
    raw = psycopg2.connect(
        host=host, dbname=database, port=port, user=user, password=token,
        sslmode="require", connect_timeout=10,
    )
    raw.autocommit = True
    _tls.conn = raw
    _tls.conn_key = key
    return _PooledConn(raw)


# ─────────────────────────────────────────────────────────── query API ──
def _snippet(sql: str, limit: int = 200) -> str:
    flat = " ".join(sql.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def pg_query(sql: str, params=None) -> list[dict]:
    """Run a parameterized statement, return rows as list[dict] (RealDictCursor)."""
    t0 = time.perf_counter()
    try:
        conn = _connect()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            if cur.description is None:
                return []
            return [dict(r) for r in cur.fetchall()]
    finally:
        ms = (time.perf_counter() - t0) * 1000
        if ms >= config.SLOW_QUERY_MS:
            logger.warning(f"slow pg query duration_ms={ms:.0f} sql={_snippet(sql)}")


def pg_execute(sql: str, params=None) -> None:
    """Run a parameterized statement for its side effects (autocommit)."""
    pg_query(sql, params)


# ────────────────────────────────────────────── bootstrap + permissions ──
_tables_ready = False
_tables_lock = threading.Lock()
_perms_backfilled = False


# Advisory-lock key so only one worker/connection backfills at a time (4 gunicorn
# workers share the instance; a per-process threading.Lock can't coordinate them).
_BACKFILL_LOCK_KEY = 918273645

# Postgres raises these on concurrent `CREATE ... IF NOT EXISTS` even though the object
# ends up existing — the check-and-create isn't atomic across sessions. Treat as success.
_BENIGN_DDL = ("already exists", "duplicate key", "concurrently updated", "deadlock detected")


def _benign_ddl_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in _BENIGN_DDL)


def _bootstrap():
    """Idempotently ensure the schema + permissions table exist (once per worker).

    Tolerant of concurrent DDL from sibling workers (see _BENIGN_DDL) — autocommit means
    each statement is its own transaction, so one racing failure doesn't poison the rest.
    """
    global _tables_ready
    if _tables_ready:
        return
    with _tables_lock:
        if _tables_ready:
            return
        conn = _connect()
        # Base objects only. The UNIQUE index is created later under the advisory lock,
        # after de-duping, so a pre-existing duplicate (e.g. from an earlier no-constraint
        # backfill) can't make its creation fail.
        ddl = (
            f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
            f"CREATE TABLE IF NOT EXISTS {PERMISSIONS} ("
            f"  email        TEXT NOT NULL,"
            f"  access_type  TEXT NOT NULL,"
            f"  allowed_site TEXT,"
            f"  updated_at   TIMESTAMPTZ DEFAULT NOW()"
            f")",
            f"CREATE INDEX IF NOT EXISTS permissions_email_idx ON {PERMISSIONS} (lower(email))",
        )
        for stmt in ddl:
            try:
                with conn.cursor() as cur:
                    cur.execute(stmt)
            except Exception as e:
                if not _benign_ddl_error(e):
                    raise
        _tables_ready = True
        logger.info("Lakebase document_hub.permissions ready")


def _ensure_permissions_ready():
    """Bootstrap the table, then — once, under a Postgres advisory lock so all workers
    serialize — de-dup any legacy rows, add the UNIQUE index, and backfill from the
    warehouse if still empty. The lock guarantees this one-time setup runs exactly once
    across the 4 gunicorn workers (a per-process threading.Lock can't coordinate them)."""
    global _perms_backfilled
    _bootstrap()
    if _perms_backfilled:
        return
    with _tables_lock:
        if _perms_backfilled:
            return
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_BACKFILL_LOCK_KEY,))
            try:
                # Collapse any pre-existing duplicates (keep one per logical key) so the
                # UNIQUE index can be built, then make future writes idempotent.
                cur.execute(
                    f"DELETE FROM {PERMISSIONS} a USING {PERMISSIONS} b "
                    f"WHERE a.ctid < b.ctid "
                    f"AND lower(a.email) = lower(b.email) "
                    f"AND a.access_type = b.access_type "
                    f"AND COALESCE(a.allowed_site, '') = COALESCE(b.allowed_site, '')"
                )
                cur.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS permissions_uk "
                    f"ON {PERMISSIONS} (lower(email), access_type, COALESCE(allowed_site, ''))"
                )
                cur.execute(f"SELECT count(*) AS n FROM {PERMISSIONS}")
                n = cur.fetchone()[0]
                if n == 0:
                    _backfill_permissions()
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_BACKFILL_LOCK_KEY,))
        _perms_backfilled = True


def _backfill_permissions():
    """One-time seed of Lakebase permissions from the warehouse table (idempotent via
    the unique index — ON CONFLICT DO NOTHING, so a racing double-run can't duplicate)."""
    import db  # lazy — avoid import-time coupling; db is import-safe under conftest stubs
    src = db.query(
        f"SELECT email, access_type, allowed_site, updated_at FROM {config.PERMISSIONS}"
    )
    for r in src:
        pg_execute(
            f"INSERT INTO {PERMISSIONS} (email, access_type, allowed_site, updated_at) "
            f"VALUES (%s, %s, %s, COALESCE(%s::timestamptz, NOW())) ON CONFLICT DO NOTHING",
            (r.get("email"), r.get("access_type"), r.get("allowed_site"), r.get("updated_at")),
        )
    logger.info(f"backfilled {len(src)} permission rows from warehouse")


def read_permissions(email: str) -> list[dict]:
    """Return the caller's permission rows (access_type, allowed_site) from Lakebase.

    Mirrors the warehouse query `get_perms()` runs; only called when `enabled()`.
    """
    _ensure_permissions_ready()
    return pg_query(
        f"SELECT access_type, allowed_site FROM {PERMISSIONS} WHERE lower(email) = %s",
        ((email or "").lower(),),
    )


def mirror_user_sites(email: str, site_ids: list[str]) -> None:
    """Dual-write hook: mirror a user's SITE grants into Lakebase to match the
    warehouse write in `sp.sync_user_sites`. No-op unless Lakebase is active — keeps
    both stores consistent so the warehouse fallback stays safe. Never raises to the
    caller's success path (the warehouse write is the source of truth during cutover)."""
    if not (enabled() and config.USE_LAKEBASE_PERMISSIONS):
        return
    try:
        _ensure_permissions_ready()
        em = (email or "").lower()
        pg_execute(
            f"DELETE FROM {PERMISSIONS} WHERE lower(email) = %s AND upper(access_type) = 'SITE'",
            (em,),
        )
        for sid in site_ids:
            pg_execute(
                f"INSERT INTO {PERMISSIONS} (email, access_type, allowed_site, updated_at) "
                f"VALUES (%s, 'SITE', %s, NOW()) ON CONFLICT DO NOTHING",
                (em, sid),
            )
    except Exception as e:
        logger.warning(f"lakebase mirror_user_sites failed (warehouse write stands): {e}")


# ══════════════════════════════════════════════════════════════════════════
# Document family — FULL cutover behind config.USE_LAKEBASE_DOCUMENTS
# ══════════════════════════════════════════════════════════════════════════
# Unlike permissions (dual-write + warehouse fallback), the document_* tables move
# wholesale to Lakebase: reads AND writes, no warehouse copy. The five tables inter-join
# inside single statements (search joins document_text; related-docs joins document_links;
# explore filters document_tags) and Postgres can't join across the warehouse boundary, so
# they must live together. field_defs / taxonomy / extraction_cache stay on the warehouse
# (config + ai_query cache); ai_query itself is a pure warehouse function fed text inline.
#
# All values are passed as %s parameters (never lit()-interpolated) — OCR text and field
# values are arbitrary, and Spark vs Postgres escape backslashes differently.

DOCUMENTS = f"{SCHEMA}.documents"
DOCUMENT_FIELDS = f"{SCHEMA}.document_fields"
DOCUMENT_TEXT = f"{SCHEMA}.document_text"
DOCUMENT_TAGS = f"{SCHEMA}.document_tags"
DOCUMENT_LINKS = f"{SCHEMA}.document_links"

_DOCS_LOCK_KEY = 918273646  # distinct from the permissions backfill lock


def docs_enabled() -> bool:
    """True when Lakebase is live AND the document cutover flag is on."""
    return enabled() and config.USE_LAKEBASE_DOCUMENTS


# ─────────────────────────────────────────────── document DDL / bootstrap ──
_docs_ready = False
_docs_lock = threading.Lock()

_DOCS_DDL = (
    f"CREATE TABLE IF NOT EXISTS {DOCUMENTS} ("
    "  doc_id                TEXT PRIMARY KEY,"
    "  content_sha256        TEXT,"
    "  volume_path           TEXT,"
    "  derived_pdf_path      TEXT,"
    "  text_source           TEXT,"
    "  original_filename     TEXT,"
    "  mime_type             TEXT,"
    "  size_bytes            BIGINT,"
    "  page_count            INTEGER,"
    "  source_id             TEXT,"
    "  source_ref            TEXT,"
    "  batch_id              TEXT,"
    "  business_unit         TEXT,"
    "  document_type         TEXT,"
    "  department            TEXT,"
    "  classification_status TEXT,"
    "  extraction_status     TEXT,"
    "  verification_status   TEXT,"
    "  mirror_status         TEXT,"
    "  extraction_sig        TEXT,"
    "  file_modified_at      TIMESTAMPTZ,"
    "  claimed_by            TEXT,"
    "  claim_expires_at      TIMESTAMPTZ,"
    "  attempt_count         INTEGER,"
    "  next_attempt_at       TIMESTAMPTZ,"
    "  error_message         TEXT,"
    "  created_at            TIMESTAMPTZ,"
    "  created_by            TEXT,"
    "  verified_at           TIMESTAMPTZ,"
    "  verified_by           TEXT,"
    "  updated_at            TIMESTAMPTZ,"
    "  sp_site_id            TEXT,"
    "  sp_site_name          TEXT,"
    "  sp_drive_id           TEXT,"
    "  sp_path               TEXT,"
    "  sp_web_url            TEXT"
    ")",
    f"CREATE INDEX IF NOT EXISTS documents_sha_idx     ON {DOCUMENTS} (content_sha256)",
    f"CREATE INDEX IF NOT EXISTS documents_srcref_idx  ON {DOCUMENTS} (source_ref)",
    f"CREATE INDEX IF NOT EXISTS documents_ext_idx     ON {DOCUMENTS} (extraction_status)",
    f"CREATE INDEX IF NOT EXISTS documents_site_idx    ON {DOCUMENTS} (sp_site_id)",
    f"CREATE INDEX IF NOT EXISTS documents_created_idx ON {DOCUMENTS} (created_at DESC)",
    f"CREATE TABLE IF NOT EXISTS {DOCUMENT_FIELDS} ("
    "  doc_id            TEXT NOT NULL,"
    "  field_key         TEXT NOT NULL,"
    "  proposed_value    TEXT,"
    "  confirmed_value   TEXT,"
    "  confidence        DOUBLE PRECISION,"
    "  source_provenance TEXT,"
    "  updated_at        TIMESTAMPTZ,"
    "  updated_by        TEXT,"
    "  PRIMARY KEY (doc_id, field_key)"
    ")",
    f"CREATE TABLE IF NOT EXISTS {DOCUMENT_TEXT} ("
    "  doc_id     TEXT NOT NULL,"
    "  page       INTEGER NOT NULL,"
    "  text       TEXT,"
    "  bbox       TEXT,"
    "  updated_at TIMESTAMPTZ,"
    "  PRIMARY KEY (doc_id, page)"
    ")",
    f"CREATE TABLE IF NOT EXISTS {DOCUMENT_TAGS} ("
    "  doc_id     TEXT NOT NULL,"
    "  tag        TEXT NOT NULL,"
    "  created_by TEXT,"
    "  created_at TIMESTAMPTZ,"
    "  PRIMARY KEY (doc_id, tag)"
    ")",
    f"CREATE TABLE IF NOT EXISTS {DOCUMENT_LINKS} ("
    "  parent_doc_id TEXT NOT NULL,"
    "  child_doc_id  TEXT NOT NULL,"
    "  relationship  TEXT NOT NULL,"
    "  created_by    TEXT,"
    "  created_at    TIMESTAMPTZ,"
    "  PRIMARY KEY (parent_doc_id, child_doc_id, relationship)"
    ")",
)


def _ensure_documents_ready():
    """Create the document_* tables once per worker, serialized across the 4 gunicorn
    workers by a Postgres advisory lock (CREATE ... IF NOT EXISTS isn't atomic across
    sessions — same concurrent-DDL hazard the permissions bootstrap handles). No backfill:
    the cutover starts from an empty store by design (the tiny warehouse corpus is dropped)."""
    global _docs_ready
    if _docs_ready:
        return
    with _docs_lock:
        if _docs_ready:
            return
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_DOCS_LOCK_KEY,))
            try:
                for stmt in _DOCS_DDL:
                    try:
                        cur.execute(stmt)
                    except Exception as e:
                        if not _benign_ddl_error(e):
                            raise
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_DOCS_LOCK_KEY,))
        _docs_ready = True
        logger.info("Lakebase document_hub document_* tables ready")


def _sites_clause(sites, col: str = "sp_site_id"):
    """Translate the app's site scope into a Postgres predicate + params.

    `sites is None`  → unrestricted (FULL/ADMIN).  `sites == []` → see nothing.
    Otherwise → `col = ANY(%s)` (psycopg2 adapts the list to a PG array).
    """
    if sites is None:
        return "", []
    if not sites:
        return " AND 1=0", []
    return f" AND {col} = ANY(%s)", [list(sites)]


# ─────────────────────────────────────────────────────────── doc reads ──
_DOC_LIST_COLS = (
    "doc_id, original_filename, document_type, department, sp_site_name, sp_path, "
    "sp_web_url, mime_type, derived_pdf_path, classification_status, extraction_status, "
    "verification_status, mirror_status, source_id, batch_id, created_at"
)


def list_documents(sites, status: str | None = None, cstatus: str | None = None) -> list[dict]:
    """Queue list — mirrors api_documents' warehouse query, site-scoped."""
    _ensure_documents_ready()
    clause, params = _sites_clause(sites)
    where = "1=1" + clause
    if status:
        where += " AND verification_status = %s AND classification_status = 'classified'"
        params.append(status)
    if cstatus:
        where += " AND classification_status = %s"
        params.append(cstatus)
    return pg_query(
        f"SELECT {_DOC_LIST_COLS} FROM {DOCUMENTS} WHERE {where} "
        f"ORDER BY created_at DESC LIMIT 500",
        params,
    )


def stats(sites):
    """Return (by_status_rows, unclassified_count) — mirrors api_stats."""
    _ensure_documents_ready()
    clause, params = _sites_clause(sites)
    by = pg_query(
        f"SELECT verification_status AS s, count(*) AS n FROM {DOCUMENTS} "
        f"WHERE 1=1{clause} AND classification_status = 'classified' "
        f"GROUP BY verification_status",
        params,
    )
    clause2, params2 = _sites_clause(sites)
    un = pg_query(
        f"SELECT count(*) AS n FROM {DOCUMENTS} "
        f"WHERE classification_status = 'unclassified'{clause2}",
        params2,
    )
    return by, un


_SEARCH_SELECT = (
    "SELECT d.doc_id, d.original_filename, d.document_type, d.department, "
    "d.sp_site_name, d.sp_path, d.sp_web_url, d.mime_type, d.derived_pdf_path, "
    "coalesce(f_title.confirmed_value, f_title.proposed_value) AS title, "
    "coalesce(f_sum.confirmed_value, f_sum.proposed_value) AS summary, d.created_at "
    f"FROM {DOCUMENTS} d "
    f"LEFT JOIN {DOCUMENT_FIELDS} f_title ON f_title.doc_id = d.doc_id AND f_title.field_key = 'title' "
    f"LEFT JOIN {DOCUMENT_FIELDS} f_sum   ON f_sum.doc_id   = d.doc_id AND f_sum.field_key   = 'summary' "
)


def search(sites, q: str = "", document_type: str | None = None, department: str | None = None,
           path: str | None = None, tag: str | None = None) -> list[dict]:
    """Full search over verified docs — mirrors api_search (Spark concat_ws/collect_list
    becomes Postgres string_agg; LIKE terms are ILIKE params).

    Params are assembled in the SQL's textual %s order: the tag JOIN (which precedes WHERE)
    binds first, then the WHERE site scope, filters, and free-text LIKEs.
    """
    _ensure_documents_ready()
    site_clause, site_params = _sites_clause(sites, "d.sp_site_id")
    params: list = []
    join_txt = join_tag = ""
    if tag:  # JOIN clause is textually before WHERE → its %s must bind first
        join_tag = f"JOIN {DOCUMENT_TAGS} tg ON tg.doc_id = d.doc_id AND tg.tag = %s "
        params.append(tag)
    params += site_params
    where = "d.verification_status = 'verified'" + site_clause
    if document_type:
        where += " AND d.document_type = %s"
        params.append(document_type)
    if department:
        where += " AND d.department = %s"
        params.append(department)
    if path:
        where += " AND lower(d.sp_path) LIKE %s"
        params.append(path.lower() + "%")
    if q:
        join_txt = (
            f"LEFT JOIN (SELECT doc_id, string_agg(text, ' ') AS body "
            f"FROM {DOCUMENT_TEXT} GROUP BY doc_id) tx ON tx.doc_id = d.doc_id "
        )
        where += (
            " AND (lower(d.original_filename) LIKE %s "
            "OR lower(coalesce(d.sp_path, '')) LIKE %s "
            "OR lower(coalesce(f_title.confirmed_value, f_title.proposed_value, '')) LIKE %s "
            "OR lower(coalesce(f_sum.confirmed_value, f_sum.proposed_value, '')) LIKE %s "
            "OR lower(coalesce(tx.body, '')) LIKE %s)"
        )
        params += [f"%{q.lower()}%"] * 5
    return pg_query(
        f"{_SEARCH_SELECT}{join_txt}{join_tag}WHERE {where} "
        f"ORDER BY d.created_at DESC LIMIT 200",
        params,
    )


def get_document(doc_id: str) -> dict | None:
    _ensure_documents_ready()
    rows = pg_query(f"SELECT * FROM {DOCUMENTS} WHERE doc_id = %s", (doc_id,))
    return rows[0] if rows else None


def get_field_values(doc_id: str) -> list[dict]:
    """This doc's stored field rows (Lakebase). The drawer merges these against the
    warehouse field_defs in Python — a cross-store join isn't possible, so the single
    LEFT JOIN api_document used becomes two reads + a Python merge (both cheap here)."""
    _ensure_documents_ready()
    return pg_query(
        "SELECT field_key, proposed_value, confirmed_value, source_provenance, confidence "
        f"FROM {DOCUMENT_FIELDS} WHERE doc_id = %s",
        (doc_id,),
    )


def get_tags(doc_id: str) -> list[str]:
    _ensure_documents_ready()
    return [r["tag"] for r in pg_query(
        f"SELECT tag FROM {DOCUMENT_TAGS} WHERE doc_id = %s ORDER BY tag", (doc_id,))]


def tag_facets() -> list[dict]:
    _ensure_documents_ready()
    return pg_query(
        f"SELECT tag, count(*) AS n FROM {DOCUMENT_TAGS} "
        f"GROUP BY tag ORDER BY n DESC, tag LIMIT 500"
    )


def get_links(doc_id: str) -> list[dict]:
    _ensure_documents_ready()
    return pg_query(
        "SELECT l.relationship, l.child_doc_id, l.parent_doc_id, "
        f"d.original_filename, d.document_type FROM {DOCUMENT_LINKS} l "
        f"JOIN {DOCUMENTS} d ON d.doc_id = "
        "  CASE WHEN l.parent_doc_id = %s THEN l.child_doc_id ELSE l.parent_doc_id END "
        "WHERE l.parent_doc_id = %s OR l.child_doc_id = %s",
        (doc_id, doc_id, doc_id),
    )


def document_type(doc_id: str) -> str | None:
    _ensure_documents_ready()
    rows = pg_query(f"SELECT document_type FROM {DOCUMENTS} WHERE doc_id = %s", (doc_id,))
    return rows[0].get("document_type") if rows else None


def document_exists(doc_id: str) -> bool:
    _ensure_documents_ready()
    return bool(pg_query(f"SELECT 1 FROM {DOCUMENTS} WHERE doc_id = %s LIMIT 1", (doc_id,)))


def satisfied_field_keys(doc_id: str) -> set:
    """Field keys with a non-empty confirmed OR proposed value (verify gate)."""
    _ensure_documents_ready()
    rows = pg_query(
        f"SELECT field_key FROM {DOCUMENT_FIELDS} WHERE doc_id = %s "
        "AND coalesce(confirmed_value, proposed_value) IS NOT NULL "
        "AND coalesce(confirmed_value, proposed_value) <> ''",
        (doc_id,),
    )
    return {r["field_key"] for r in rows}


def has_amendment_parent(doc_id: str) -> bool:
    _ensure_documents_ready()
    return bool(pg_query(
        f"SELECT 1 FROM {DOCUMENT_LINKS} WHERE child_doc_id = %s "
        "AND relationship = 'amendment_of' LIMIT 1",
        (doc_id,),
    ))


def find_by_sha(sha: str) -> list[dict]:
    _ensure_documents_ready()
    return pg_query(
        "SELECT doc_id, original_filename, verification_status, volume_path "
        f"FROM {DOCUMENTS} WHERE content_sha256 = %s LIMIT 1",
        (sha,),
    )


def find_by_source_ref(ref: str) -> list[dict]:
    _ensure_documents_ready()
    return pg_query(f"SELECT doc_id FROM {DOCUMENTS} WHERE source_ref = %s LIMIT 1", (ref,))


# ─────────────────────────────────────────────────────────── doc writes ──
def insert_document(**cols) -> None:
    """Insert one documents row. created_at/updated_at are stamped server-side (now());
    any timestamp string params (e.g. file_modified_at) implicitly cast to timestamptz."""
    _ensure_documents_ready()
    cols.pop("created_at", None)
    cols.pop("updated_at", None)
    keys = [k for k in cols if cols[k] is not None]
    collist = ", ".join(keys + ["created_at", "updated_at"])
    placeholders = ", ".join(["%s"] * len(keys) + ["now()", "now()"])
    pg_execute(
        f"INSERT INTO {DOCUMENTS} ({collist}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
        [cols[k] for k in keys],
    )


def classify(doc_ids: list[str], document_type: str | None, department: str | None) -> None:
    _ensure_documents_ready()
    sets = ["classification_status = 'classified'", "updated_at = now()"]
    params: list = []
    if document_type is not None:
        sets.append("document_type = %s")
        params.append(document_type)
    if department is not None:
        sets.append("department = %s")
        params.append(department)
    params.append(list(doc_ids))
    pg_execute(f"UPDATE {DOCUMENTS} SET {', '.join(sets)} WHERE doc_id = ANY(%s)", params)


def enqueue(doc_ids: list[str]) -> None:
    _ensure_documents_ready()
    pg_execute(
        f"UPDATE {DOCUMENTS} SET extraction_status = 'pending', next_attempt_at = NULL, "
        "updated_at = now() WHERE doc_id = ANY(%s) AND classification_status = 'classified'",
        (list(doc_ids),),
    )


def save_field(doc_id: str, field_key: str, value, email: str) -> None:
    """Upsert a human-confirmed value (mirrors api_save_fields' MERGE)."""
    _ensure_documents_ready()
    pg_execute(
        f"INSERT INTO {DOCUMENT_FIELDS} "
        "(doc_id, field_key, confirmed_value, source_provenance, updated_at, updated_by) "
        "VALUES (%s, %s, %s, 'human', now(), %s) "
        "ON CONFLICT (doc_id, field_key) DO UPDATE SET "
        "confirmed_value = EXCLUDED.confirmed_value, source_provenance = 'human', "
        "updated_at = now(), updated_by = EXCLUDED.updated_by",
        (doc_id, field_key, value, email),
    )


def verify(doc_id: str, email: str) -> None:
    _ensure_documents_ready()
    pg_execute(
        f"UPDATE {DOCUMENTS} SET verification_status = 'verified', verified_by = %s, "
        "verified_at = now(), mirror_status = 'not_mirrored', updated_at = now() "
        "WHERE doc_id = %s",
        (email, doc_id),
    )


def unverify(doc_id: str) -> None:
    _ensure_documents_ready()
    pg_execute(
        f"UPDATE {DOCUMENTS} SET verification_status = 'needs_review', updated_at = now() "
        "WHERE doc_id = %s",
        (doc_id,),
    )


def tag_exists(doc_id: str, tag: str) -> bool:
    _ensure_documents_ready()
    return bool(pg_query(
        f"SELECT 1 FROM {DOCUMENT_TAGS} WHERE doc_id = %s AND tag = %s LIMIT 1", (doc_id, tag)))


def add_tag(doc_id: str, tag: str, email: str) -> None:
    _ensure_documents_ready()
    pg_execute(
        f"INSERT INTO {DOCUMENT_TAGS} (doc_id, tag, created_by, created_at) "
        "VALUES (%s, %s, %s, now()) ON CONFLICT DO NOTHING",
        (doc_id, tag, email),
    )


def remove_tag(doc_id: str, tag: str) -> None:
    _ensure_documents_ready()
    pg_execute(f"DELETE FROM {DOCUMENT_TAGS} WHERE doc_id = %s AND tag = %s", (doc_id, tag))


def add_link(parent: str, child: str, relationship: str, email: str) -> None:
    _ensure_documents_ready()
    pg_execute(
        f"INSERT INTO {DOCUMENT_LINKS} "
        "(parent_doc_id, child_doc_id, relationship, created_by, created_at) "
        "VALUES (%s, %s, %s, %s, now()) ON CONFLICT DO NOTHING",
        (parent, child, relationship, email),
    )


# ───────────────────────────────────────────── processing-job doc writes ──
def claim_batch(worker_id: str, n: int, max_attempts: int, lease_secs: int) -> list[dict]:
    """Claim up to n pending docs for this worker (mirrors job.claim_batch). Postgres
    serializes the guarded UPDATE, so racing workers can't double-claim a row."""
    _ensure_documents_ready()
    candidates = pg_query(
        f"SELECT doc_id FROM {DOCUMENTS} WHERE extraction_status = 'pending' "
        "AND classification_status = 'classified' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
        "AND (claim_expires_at IS NULL OR claim_expires_at <= now()) "
        "AND attempt_count < %s ORDER BY created_at LIMIT %s",
        (max_attempts, n),
    )
    if not candidates:
        return []
    ids = [c["doc_id"] for c in candidates]
    pg_execute(
        f"UPDATE {DOCUMENTS} SET claimed_by = %s, "
        "claim_expires_at = now() + make_interval(secs => %s), "
        "extraction_status = 'processing', updated_at = now() "
        "WHERE doc_id = ANY(%s) AND extraction_status = 'pending' "
        "AND (claim_expires_at IS NULL OR claim_expires_at <= now())",
        (worker_id, lease_secs, ids),
    )
    return pg_query(
        "SELECT doc_id, content_sha256, volume_path, original_filename, document_type, attempt_count "
        f"FROM {DOCUMENTS} WHERE claimed_by = %s AND extraction_status = 'processing'",
        (worker_id,),
    )


def extend_lease(doc_id: str, worker_id: str, lease_secs: int) -> None:
    _ensure_documents_ready()
    pg_execute(
        f"UPDATE {DOCUMENTS} SET claim_expires_at = now() + make_interval(secs => %s), "
        "updated_at = now() WHERE doc_id = %s AND claimed_by = %s",
        (lease_secs, doc_id, worker_id),
    )


def replace_text(doc_id: str, pages: list[dict]) -> None:
    """Replace this doc's page text atomically (delete-then-insert, idempotent per doc)."""
    _ensure_documents_ready()
    pg_execute(f"DELETE FROM {DOCUMENT_TEXT} WHERE doc_id = %s", (doc_id,))
    for p in pages:
        if (p.get("text") or "").strip():
            pg_execute(
                f"INSERT INTO {DOCUMENT_TEXT} (doc_id, page, text, updated_at) "
                "VALUES (%s, %s, %s, now())",
                (doc_id, int(p["page"]), p["text"]),
            )


def upsert_proposed_field(doc_id: str, field_key: str, value) -> None:
    """AI-proposed value upsert — never clobbers an existing human confirmed_value or its
    provenance (mirrors _commit_success' MERGE)."""
    _ensure_documents_ready()
    pg_execute(
        f"INSERT INTO {DOCUMENT_FIELDS} "
        "(doc_id, field_key, proposed_value, source_provenance, updated_at) "
        "VALUES (%s, %s, %s, 'ai', now()) "
        "ON CONFLICT (doc_id, field_key) DO UPDATE SET "
        "proposed_value = EXCLUDED.proposed_value, "
        f"source_provenance = coalesce({DOCUMENT_FIELDS}.source_provenance, 'ai'), "
        "updated_at = now()",
        (doc_id, field_key, value),
    )


def commit_extraction_done(doc_id: str, derived_path, text_source, page_count: int, sig: str) -> None:
    _ensure_documents_ready()
    pg_execute(
        f"UPDATE {DOCUMENTS} SET extraction_status = 'done', derived_pdf_path = %s, "
        "text_source = %s, page_count = %s, extraction_sig = %s, error_message = NULL, "
        "claimed_by = NULL, claim_expires_at = NULL, updated_at = now() WHERE doc_id = %s",
        (derived_path, text_source, page_count, sig, doc_id),
    )


def commit_extraction_failed(doc_id: str, attempts: int, message: str) -> None:
    _ensure_documents_ready()
    pg_execute(
        f"UPDATE {DOCUMENTS} SET extraction_status = 'failed', attempt_count = %s, "
        "error_message = %s, claimed_by = NULL, claim_expires_at = NULL, updated_at = now() "
        "WHERE doc_id = %s",
        (attempts, message, doc_id),
    )


def commit_extraction_retry(doc_id: str, attempts: int, message: str, backoff_secs: int) -> None:
    _ensure_documents_ready()
    pg_execute(
        f"UPDATE {DOCUMENTS} SET extraction_status = 'pending', attempt_count = %s, "
        "error_message = %s, claimed_by = NULL, claim_expires_at = NULL, "
        "next_attempt_at = now() + make_interval(secs => %s), updated_at = now() "
        "WHERE doc_id = %s",
        (attempts, message, backoff_secs, doc_id),
    )
