"""Databricks Job entrypoint for the processing worker.

`spark_python_task` runs `python worker.py` (it can't use `python -m`), and
`processing/job.py` uses package-relative imports, so this thin root-level shim imports
the package properly and hands off to its CLI. Job parameters become argv, e.g.:

    worker.py --drain --sweep --sync        # scheduled: clear backlog then exit
    worker.py --loop --sweep --sync         # continuous: near-real-time
"""
import os


def _bootstrap_job_env() -> None:
    """Supply Lakebase connection env for the headless job BEFORE `config`/`job` import.

    The web App gets PGHOST/PGUSER/PGDATABASE injected by its `database` resource binding;
    this Databricks Job does not — and since it now runs on a **shared existing cluster**
    (no `spark_env_vars`), nothing supplies them. We set them here from the run-as identity.

    Auth is the run-as user's *ambient* SDK-minted Lakebase JWT (see
    `lakebase._get_sdk_credential`) — no SP OAuth secret and nothing written onto the shared
    cluster. `config` snapshots the cutover flags at import time, so this must run first; hence
    `job` is imported below, after this returns. `setdefault` lets an explicit override (env or
    a rollback to the SP `spark_env_vars` path) still win.
    """
    os.environ.setdefault("PGHOST", "ep-flat-moon-ee1bjbvj.database.westus2.azuredatabricks.net")
    os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGDATABASE", "databricks_postgres")
    os.environ.setdefault("LAKEBASE_SCHEMA", "document_hub")
    # The job must match the App: documents + permissions both live on Lakebase.
    os.environ.setdefault("USE_LAKEBASE_DOCUMENTS", "true")
    os.environ.setdefault("USE_LAKEBASE_PERMISSIONS", "true")
    # Postgres identity = the run-as user (owner of the instance). Resolve via ambient auth.
    if not os.getenv("PGUSER"):
        try:
            from databricks.sdk import WorkspaceClient
            os.environ["PGUSER"] = WorkspaceClient().current_user.me().user_name
        except Exception as e:  # leave PGUSER unset → _connect fails loudly with context
            print(f"[worker] could not resolve run-as identity for PGUSER: {e}")


if __name__ == "__main__":
    _bootstrap_job_env()
    from processing import job
    job.main()
