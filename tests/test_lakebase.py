"""Lakebase Phase 2 thin slice — offline behaviour and the permissions cutover.

No real Postgres here (psycopg2 may not even connect): the module must be **inert**
when PGHOST is unset so callers fall back to the warehouse, and the flag routing in
`get_perms()` must be exercisable with `lakebase` faked. These pin the two guarantees
the migration rests on: (1) offline suite stays green, (2) the flag actually swaps the
read path without touching the warehouse.
"""
import lakebase


# ── inertness offline (the load-bearing guarantee for the offline suite) ─────
def test_lakebase_inert_without_pghost(monkeypatch):
    monkeypatch.delenv("PGHOST", raising=False)
    assert lakebase.enabled() is False


def test_lakebase_enabled_needs_both_psycopg2_and_pghost(monkeypatch):
    monkeypatch.setenv("PGHOST", "somehost")
    monkeypatch.setattr(lakebase, "psycopg2", None)
    assert lakebase.enabled() is False  # host set but driver missing → still inert


def test_mirror_user_sites_is_noop_when_disabled(monkeypatch):
    # Disabled → must not attempt any connection/query (would raise offline).
    monkeypatch.setattr(lakebase, "enabled", lambda: False)
    called = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda *a, **k: called.append(a))
    lakebase.mirror_user_sites("u@x.com", ["site-A", "site-B"])
    assert called == []


# ── get_perms flag routing ───────────────────────────────────────────────────
def test_get_perms_uses_warehouse_when_lakebase_disabled(bind_db, monkeypatch):
    import app as app_module
    from conftest import FakeDB
    monkeypatch.setattr(app_module.lakebase, "enabled", lambda: False)
    fake = FakeDB(responder=lambda sql: [{"access_type": "FULL", "allowed_site": None}])
    bind_db(fake, app_module)
    is_admin, is_full, allowed = app_module.get_perms("u@x.com")
    assert is_full is True
    assert len(fake.queried_matching("access_type, allowed_site")) == 1  # warehouse hit


def test_get_perms_reads_lakebase_when_enabled(bind_db, monkeypatch):
    import app as app_module
    import config
    from conftest import FakeDB
    monkeypatch.setattr(config, "USE_LAKEBASE_PERMISSIONS", True)
    monkeypatch.setattr(app_module.lakebase, "enabled", lambda: True)
    monkeypatch.setattr(app_module.lakebase, "read_permissions",
                        lambda email: [{"access_type": "READ", "allowed_site": "site-A"}])
    # Warehouse must NOT be consulted when Lakebase serves the read.
    fake = FakeDB(responder=lambda sql: (_ for _ in ()).throw(AssertionError("warehouse hit")))
    bind_db(fake, app_module)
    is_admin, is_full, allowed = app_module.get_perms("u@x.com")
    assert (is_admin, is_full, allowed) == (False, False, ["site-A"])
    assert fake.queried_matching("access_type, allowed_site") == []


def test_get_perms_falls_back_to_warehouse_on_lakebase_error(bind_db, monkeypatch):
    import app as app_module
    import config
    from conftest import FakeDB
    monkeypatch.setattr(config, "USE_LAKEBASE_PERMISSIONS", True)
    monkeypatch.setattr(app_module.lakebase, "enabled", lambda: True)

    def _boom(email):
        raise RuntimeError("pg down")
    monkeypatch.setattr(app_module.lakebase, "read_permissions", _boom)
    fake = FakeDB(responder=lambda sql: [{"access_type": "ADMIN", "allowed_site": None}])
    bind_db(fake, app_module)
    with app_module.app.test_request_context("/api/me"):  # _req_ctx() needs a request
        is_admin, is_full, allowed = app_module.get_perms("u@x.com")
    assert is_admin is True  # warehouse fallback served the read
    assert len(fake.queried_matching("access_type, allowed_site")) == 1
