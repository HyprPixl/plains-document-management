"""Shared pytest fixtures + offline mocks for the Document Hub suite.

The web/processing tiers talk to a Databricks SQL warehouse that is unreachable
from CI / a dev laptop. Everything here keeps the suite **offline and
deterministic** by faking the `db` layer (`query`/`execute`) and stubbing the
Azure Document Intelligence SDK that `processing.ocr` imports at module load.

Nothing in here performs real I/O.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest

# ── Stub Databricks WorkspaceClient before any module constructs one ──────────
# db.py / app.py / ingest.py / processing.job all do `_w = WorkspaceClient()` at
# import time, which otherwise demands real credentials (and a network probe).
# All warehouse I/O is faked per-test, so a MagicMock is a safe, offline stand-in
# and keeps the suite runnable with no ~/.databrickscfg (CI-friendly).
import databricks.sdk as _dbx_sdk  # noqa: E402
_dbx_sdk.WorkspaceClient = lambda *a, **k: MagicMock()


# ── Stub the Azure DI SDK before anything imports processing.job ─────────────
# processing/ocr.py does `from azure.ai.documentintelligence import ...` at import
# time; that package isn't installed in the test env (it's a job-only dep). Stub
# just enough of the module tree so `import processing.job` succeeds offline.
def _install_azure_stub():
    if "azure.ai.documentintelligence" in sys.modules:
        return
    azure = types.ModuleType("azure")
    azure.__path__ = []  # mark as package
    ai = types.ModuleType("azure.ai")
    ai.__path__ = []
    di = types.ModuleType("azure.ai.documentintelligence")
    di.__path__ = []
    di.DocumentIntelligenceClient = object
    di_models = types.ModuleType("azure.ai.documentintelligence.models")
    di_models.AnalyzeDocumentRequest = object
    core = types.ModuleType("azure.core")
    core.__path__ = []
    creds = types.ModuleType("azure.core.credentials")
    creds.AzureKeyCredential = object
    for name, mod in {
        "azure": azure,
        "azure.ai": ai,
        "azure.ai.documentintelligence": di,
        "azure.ai.documentintelligence.models": di_models,
        "azure.core": core,
        "azure.core.credentials": creds,
    }.items():
        sys.modules.setdefault(name, mod)


_install_azure_stub()


# ── Fake db layer ───────────────────────────────────────────────────────────
class FakeDB:
    """Records SQL and returns canned rows via a `responder(sql) -> list[dict]`.

    `query`/`execute` share the same call log so tests can assert on the exact
    statements the code under test emitted (column names, guards, provenance…).
    """

    def __init__(self, responder=None):
        self.queries: list[str] = []
        self.executes: list[str] = []
        self.responder = responder or (lambda sql: [])

    def query(self, sql, timeout_s=120):
        self.queries.append(sql)
        return self.responder(sql)

    def execute(self, sql, timeout_s=120):
        self.executes.append(sql)
        return None

    # convenience assertions -------------------------------------------------
    def executed_matching(self, needle: str) -> list[str]:
        return [s for s in self.executes if needle in s]

    def queried_matching(self, needle: str) -> list[str]:
        return [s for s in self.queries if needle in s]


def route(rules, default=None):
    """Build a responder from ordered (substring, rows-or-callable) rules.

    First matching substring wins; `rows` may be a list or a callable(sql)->list.
    Falls back to `default` (or []) when nothing matches.
    """
    def _responder(sql):
        for needle, rows in rules:
            if needle in sql:
                return rows(sql) if callable(rows) else rows
        return [] if default is None else default
    return _responder


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture
def bind_db(monkeypatch):
    """Bind a FakeDB's query/execute into one or more modules under test.

    Modules do `from db import query, execute` so each holds its own binding —
    patch every module that issues SQL.
    """
    def _bind(fake, *modules):
        for m in modules:
            monkeypatch.setattr(m, "query", fake.query, raising=False)
            monkeypatch.setattr(m, "execute", fake.execute, raising=False)
    return _bind


# ── Flask API fixtures ──────────────────────────────────────────────────────
@pytest.fixture
def app_module(fake_db, monkeypatch):
    import app as app_module
    import ingest as ingest_module
    monkeypatch.setattr(app_module, "query", fake_db.query)
    monkeypatch.setattr(app_module, "execute", fake_db.execute)
    # api_upload delegates to ingest.register_bytes which issues its own SQL.
    monkeypatch.setattr(ingest_module, "query", fake_db.query)
    monkeypatch.setattr(ingest_module, "execute", fake_db.execute)
    # Let the registered error handlers run instead of re-raising into the test.
    app_module.app.config["PROPAGATE_EXCEPTIONS"] = False
    return app_module


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


@pytest.fixture
def app_logs(app_module):
    """Capture records emitted on app.logger (propagate=False, so caplog misses it)."""
    import logging

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logger = app_module.app.logger
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
