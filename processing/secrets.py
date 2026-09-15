"""Secret access that works both in a Databricks job (dbutils global) and via the SDK."""
from databricks.sdk import WorkspaceClient

_w = WorkspaceClient()


def get(scope: str, key: str) -> str:
    # Prefer the notebook/job dbutils global if present.
    g = globals().get("dbutils") or __builtins__.get("dbutils") if isinstance(__builtins__, dict) else getattr(__builtins__, "dbutils", None)
    if g is not None:
        return g.secrets.get(scope=scope, key=key)
    # Fall back to the SDK dbutils (works on serverless / from the driver).
    return _w.dbutils.secrets.get(scope=scope, key=key)
