"""Thin SQL access layer over the Databricks SQL warehouse.

All app data access goes through here. Uses the Databricks SDK statement-execution
API so it works both inside a Databricks App (service principal) and locally
(developer profile from ~/.databrickscfg).
"""
import time
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

import config

_w = WorkspaceClient()


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
    """Run a statement and return rows as list[dict]. Polls to completion."""
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


def execute(sql: str, timeout_s: int = 120) -> None:
    """Run a statement for its side effects."""
    query(sql, timeout_s)


def lit(v) -> str:
    return _lit(v)
