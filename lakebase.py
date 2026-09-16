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


def _password() -> str | None:
    """Resolve the connection password, most-preferred first.

    1. `LAKEBASE_TOKEN` — ephemeral local-probing override (a personal token). Never
       persisted anywhere; discarded when the shell env goes away.
    2. A freshly minted SP OAuth token — the production path (rotates in-process).
    3. The injected `PGPASSWORD` — last-resort shortcut (not refreshed, can expire).
    """
    tok = os.getenv("LAKEBASE_TOKEN")
    if tok:
        return tok
    tok = _get_sp_token()
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
        ddl = (
            f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
            f"CREATE TABLE IF NOT EXISTS {PERMISSIONS} ("
            f"  email        TEXT NOT NULL,"
            f"  access_type  TEXT NOT NULL,"
            f"  allowed_site TEXT,"
            f"  updated_at   TIMESTAMPTZ DEFAULT NOW()"
            f")",
            f"CREATE INDEX IF NOT EXISTS permissions_email_idx ON {PERMISSIONS} (lower(email))",
            # Uniqueness makes backfill + dual-write idempotent across workers (NULL sites
            # coalesced so they compare equal). Enables ON CONFLICT DO NOTHING below.
            f"CREATE UNIQUE INDEX IF NOT EXISTS permissions_uk "
            f"ON {PERMISSIONS} (lower(email), access_type, COALESCE(allowed_site, ''))",
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
    """Bootstrap the table and, one time, backfill it from the warehouse if it is still
    empty — so the cutover is transparent and rollback-safe. A Postgres advisory lock
    serializes the check-and-backfill across all workers so it seeds exactly once."""
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
