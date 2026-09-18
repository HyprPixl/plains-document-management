"""Thin SQL access layer over the Databricks SQL warehouse.

All app data access goes through here. Uses the Databricks SDK statement-execution
API so it works both inside a Databricks App (service principal) and locally
(developer profile from ~/.databrickscfg).
"""
import logging
import time
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

import config

_w = WorkspaceClient()

# ─────────────────────────────────────────────────────────────── logging ──
# Per-statement timing logs to stdout in the same shape as app.py / processing.job
# (the Databricks App/Job log captures stdout). Slow warehouse calls surface at
# WARNING so the Phase 2/3 optimization work has something concrete to chase.
logger = logging.getLogger("doc_hub.db")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    logger.propagate = False  # basicConfig is a no-op here (SDK owns root) — own our sink


def _sql_snippet(sql: str, limit: int = 200) -> str:
    """One-line, truncated SQL for log lines — collapse whitespace so it stays greppable."""
    flat = " ".join(sql.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _lit(v) -> str:
    """Render a Python value as a safe SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def query(sql: str, timeout_s: int = 120) -> list[dict]:
    """Run a statement and return rows as list[dict]. Polls to completion.

    Wall-time is measured per statement (perf_counter, negligible overhead) and any call
    over config.SLOW_QUERY_MS is logged at WARNING — the return contract is unchanged.
    """
    _t0 = time.perf_counter()
    try:
        resp = _w.statement_execution.execute_statement(
            warehouse_id=config.SQL_WAREHOUSE_ID,
            statement=sql,
            wait_timeout="30s",
        )
        stmt_id = resp.statement_id
        deadline = time.time() + timeout_s
        while resp.status and resp.status.state in (
            StatementState.PENDING,
            StatementState.RUNNING,
        ):
            if time.time() > deadline:
                raise TimeoutError(f"SQL timed out after {timeout_s}s")
            time.sleep(1)
            resp = _w.statement_execution.get_statement(stmt_id)

        if not resp.status or resp.status.state != StatementState.SUCCEEDED:
            msg = resp.status.error.message if (resp.status and resp.status.error) else "unknown"
            raise RuntimeError(f"SQL failed: {msg}\n{sql[:500]}")

        result = resp.result
        if not result or not result.data_array:
            return []
        columns = resp.manifest.schema.columns
        cols = [c.name for c in columns]
        coercers = [_coercer(c.type_name) for c in columns]
        return [
            {cols[i]: coercers[i](v) for i, v in enumerate(row)}
            for row in result.data_array
        ]
    finally:
        _elapsed_ms = (time.perf_counter() - _t0) * 1000
        if _elapsed_ms >= config.SLOW_QUERY_MS:
            logger.warning(f"slow query duration_ms={_elapsed_ms:.0f} sql={_sql_snippet(sql)}")


def _coercer(type_name):
    """Return a fn that casts a string cell to its Python type.

    The statement-execution API returns every cell as a string; without this,
    booleans come back as the strings 'true'/'false' (both truthy in Python) and
    numbers as strings. Callers expect real types.
    """
    tn = getattr(type_name, "value", None) or str(type_name).rsplit(".", 1)[-1]
    if tn in ("BOOLEAN",):
        return lambda v: None if v is None else (v == "true")
    if tn in ("INT", "LONG", "SHORT", "BYTE"):
        return lambda v: None if v is None else int(v)
    if tn in ("FLOAT", "DOUBLE", "DECIMAL"):
        return lambda v: None if v is None else float(v)
    return lambda v: v


# ─────────────────────────────────────────────────────── config-read cache ──
# The warehouse config tables (field_defs, taxonomy) change rarely but are read on hot
# paths — every document open re-reads field_defs, ~1.2s of Statement Execution latency
# (see bench/BASELINE.md). A tiny per-process TTL cache keyed on the exact SQL string
# collapses those repeat reads to one warehouse round-trip per TTL window. Correctness
# holds because the cache key *is* the full query, so a cached hit is byte-identical to a
# fresh run; only staleness (≤ TTL) is traded. Config writes bust the cache in the writing
# worker; the other gunicorn workers converge within one TTL. Use ONLY for read-only queries
# over rarely-changing config — never for per-row / per-user / per-document data.
_cache: dict[str, tuple[float, list]] = {}
CONFIG_CACHE_TTL_S = 60


def cached_query(sql: str, ttl_s: int = CONFIG_CACHE_TTL_S) -> list[dict]:
    """query() with a per-process TTL cache keyed on the SQL string. Returns fresh row copies
    each call so a caller that mutates rows (e.g. adds an 'options' key) can't poison the cache."""
    now = time.monotonic()
    hit = _cache.get(sql)
    if hit is not None and now - hit[0] < ttl_s:
        return [dict(r) for r in hit[1]]
    rows = query(sql)
    _cache[sql] = (now, rows)
    return [dict(r) for r in rows]


def bust_cache() -> None:
    """Drop all cached config reads — call after a config-table write in this worker."""
    _cache.clear()


def execute(sql: str, timeout_s: int = 120) -> None:
    """Run a statement for its side effects."""
    query(sql, timeout_s)


def lit(v) -> str:
    return _lit(v)
