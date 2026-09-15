"""Databricks Job entrypoint for the processing worker.

`spark_python_task` runs `python worker.py` (it can't use `python -m`), and
`processing/job.py` uses package-relative imports, so this thin root-level shim imports
the package properly and hands off to its CLI. Job parameters become argv, e.g.:

    worker.py --drain --sweep --sync        # scheduled: clear backlog then exit
    worker.py --loop --sweep --sync         # continuous: near-real-time
"""
from processing import job

if __name__ == "__main__":
    job.main()
