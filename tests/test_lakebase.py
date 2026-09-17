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


# ══ Credential resolution — App (OIDC) vs Job (SDK ambient) ══════════════════
def test_password_prefers_local_token_override(monkeypatch):
    monkeypatch.setenv("LAKEBASE_TOKEN", "personal-probe-tok")
    assert lakebase._password() == "personal-probe-tok"  # never reaches minting paths


def test_password_falls_back_to_sdk_credential_for_the_job(monkeypatch):
    # The processing job has no OIDC client-creds env → _get_sp_token() is None; the SDK
    # ambient path must serve the credential (before the stale PGPASSWORD shortcut).
    monkeypatch.delenv("LAKEBASE_TOKEN", raising=False)
    monkeypatch.setenv("PGPASSWORD", "stale-injected")
    monkeypatch.setattr(lakebase, "_get_sp_token", lambda: None)
    monkeypatch.setattr(lakebase, "_get_sdk_credential", lambda: "sdk-minted-tok")
    assert lakebase._password() == "sdk-minted-tok"


def test_password_uses_oidc_token_when_available(monkeypatch):
    # The App path: client-creds present → OIDC token wins, SDK path not consulted.
    monkeypatch.delenv("LAKEBASE_TOKEN", raising=False)
    monkeypatch.setattr(lakebase, "_get_sp_token", lambda: "oidc-sp-tok")
    monkeypatch.setattr(lakebase, "_get_sdk_credential",
                        lambda: (_ for _ in ()).throw(AssertionError("SDK path should not run")))
    assert lakebase._password() == "oidc-sp-tok"


# ══ Document family — full cutover routing (USE_LAKEBASE_DOCUMENTS) ═══════════
def test_docs_enabled_requires_both_enabled_and_flag(monkeypatch):
    import config
    monkeypatch.setattr(lakebase, "enabled", lambda: True)
    monkeypatch.setattr(config, "USE_LAKEBASE_DOCUMENTS", False)
    assert lakebase.docs_enabled() is False          # live but flag off → warehouse
    monkeypatch.setattr(config, "USE_LAKEBASE_DOCUMENTS", True)
    assert lakebase.docs_enabled() is True
    monkeypatch.setattr(lakebase, "enabled", lambda: False)
    assert lakebase.docs_enabled() is False           # flag on but not live → inert


def test_api_documents_reads_lakebase_not_warehouse_when_enabled(client, fake_db, monkeypatch):
    import app as app_module
    from conftest import route
    fake_db.responder = route([("access_type, allowed_site", [{"access_type": "FULL", "allowed_site": None}])])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    canned = [{"doc_id": "d9", "original_filename": "x.pdf"}]
    monkeypatch.setattr(app_module.lakebase, "list_documents", lambda *a, **k: canned)
    r = client.get("/api/documents")
    assert r.status_code == 200 and r.get_json() == canned
    # The warehouse documents SELECT must NOT have run (permissions read may still).
    assert fake_db.queried_matching("FROM product_dev.document_hub.documents") == []


def test_api_documents_uses_warehouse_when_disabled(client, fake_db, monkeypatch):
    import app as app_module
    from conftest import route
    fake_db.responder = route([("access_type, allowed_site", [{"access_type": "FULL", "allowed_site": None}])],
                              default=[])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: False)
    r = client.get("/api/documents")
    assert r.status_code == 200
    assert len(fake_db.queried_matching("FROM product_dev.document_hub.documents")) == 1


def test_api_classify_writes_lakebase_when_enabled(client, fake_db, monkeypatch):
    import app as app_module
    from conftest import route
    fake_db.responder = route([("access_type, allowed_site", [{"access_type": "FULL", "allowed_site": None}])])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(app_module.lakebase, "classify",
                        lambda ids, dt, dept: calls.append((ids, dt, dept)))
    r = client.post("/api/documents/classify",
                    json={"doc_ids": ["d1", "d2"], "document_type": "Invoice", "department": "AP"})
    assert r.status_code == 200 and r.get_json() == {"updated": 2}
    assert calls == [(["d1", "d2"], "Invoice", "AP")]
    assert fake_db.executed_matching("UPDATE product_dev.document_hub.documents") == []  # warehouse untouched


def test_api_document_merges_warehouse_defs_with_lakebase_values(client, fake_db, monkeypatch):
    import app as app_module
    from conftest import route
    # field_defs is warehouse-resident; document + values come from Lakebase.
    defs = [{"field_key": "title", "label": "Title", "data_type": "string",
             "picklist_source": None, "required_for_verify": True,
             "applies_to": "common", "sort_order": 1}]
    fake_db.responder = route([("access_type, allowed_site", [{"access_type": "FULL", "allowed_site": None}]),
                               ("field_defs", defs)])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    monkeypatch.setattr(app_module.lakebase, "get_document",
                        lambda doc_id: {"doc_id": doc_id, "document_type": "Invoice"})
    monkeypatch.setattr(app_module.lakebase, "get_field_values",
                        lambda doc_id: [{"field_key": "title", "proposed_value": "AI title",
                                         "confirmed_value": "Human title", "source_provenance": "human",
                                         "confidence": None}])
    monkeypatch.setattr(app_module.lakebase, "get_links", lambda doc_id: [])
    monkeypatch.setattr(app_module.lakebase, "get_tags", lambda doc_id: ["t1"])
    r = client.get("/api/documents/d1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["document"]["doc_id"] == "d1"
    assert body["tags"] == ["t1"]
    fld = body["fields"][0]
    assert fld["field_key"] == "title"
    assert fld["confirmed_value"] == "Human title" and fld["proposed_value"] == "AI title"
    # No cross-store join was attempted against the warehouse document_fields.
    assert fake_db.queried_matching("document_fields") == []
