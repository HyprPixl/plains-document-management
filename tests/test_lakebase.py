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


def test_set_access_grant_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(lakebase, "enabled", lambda: False)
    called = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda *a, **k: called.append(a))
    lakebase.set_access_grant("u@x.com", "ADMIN")
    assert called == []


def test_set_access_grant_replaces_elevated_rows_when_enabled(monkeypatch):
    import config
    monkeypatch.setattr(lakebase, "enabled", lambda: True)
    monkeypatch.setattr(config, "USE_LAKEBASE_PERMISSIONS", True)
    monkeypatch.setattr(lakebase, "_ensure_permissions_ready", lambda: None)
    calls = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda sql, params=None: calls.append((sql, params)))
    lakebase.set_access_grant("U@X.com", "full")
    assert any("DELETE" in s and "<> 'SITE'" in s for s, _ in calls)      # elevated rows cleared
    ins = [(s, p) for s, p in calls if "INSERT" in s]
    assert ins and ins[0][1] == ("u@x.com", "FULL")                       # normalised + upper-cased


def test_write_audit_inserts_row_to_lakebase(monkeypatch):
    monkeypatch.setattr(lakebase, "_ensure_documents_ready", lambda: None)
    calls = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda sql, params=None: calls.append((sql, params)))
    lakebase.write_audit("u@x.com", "classify", "3 docs", {"document_type": "Invoice"})
    assert len(calls) == 1
    sql, params = calls[0]
    assert "INSERT INTO" in sql and "audit_log" in sql
    # event_id generated, then actor/action/target, then the JSON-encoded detail.
    assert params[0].startswith("e_")
    assert params[1:4] == ("u@x.com", "classify", "3 docs")
    assert '"document_type": "Invoice"' in params[4]


def test_save_fields_batches_all_keys_into_one_upsert(monkeypatch):
    monkeypatch.setattr(lakebase, "_ensure_documents_ready", lambda: None)
    calls = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda sql, params=None: calls.append((sql, params)))
    lakebase.save_fields("d1", {"title": "New Title", "amount": "42"}, "u@x.com")
    assert len(calls) == 1                                  # one round-trip, not one-per-field
    sql, params = calls[0]
    assert "INSERT INTO" in sql and "document_fields" in sql
    assert "ON CONFLICT" in sql and "'human'" in sql
    assert sql.count("(%s, %s, %s, 'human', now(), %s)") == 2  # two value tuples
    # doc_id / field_key / value / updated_by, flattened per row in insertion order
    assert params == ("d1", "title", "New Title", "u@x.com",
                      "d1", "amount", "42", "u@x.com")


def test_upsert_proposed_fields_batches_and_preserves_provenance(monkeypatch):
    monkeypatch.setattr(lakebase, "_ensure_documents_ready", lambda: None)
    calls = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda sql, params=None: calls.append((sql, params)))
    lakebase.upsert_proposed_fields("d1", {"title": "AI title", "amount": "9"})
    assert len(calls) == 1
    sql, params = calls[0]
    assert "INSERT INTO" in sql and "document_fields" in sql
    assert sql.count("(%s, %s, %s, 'ai', now())") == 2       # two proposed rows, one statement
    assert "ON CONFLICT" in sql and "coalesce(" in sql       # never clobbers human provenance
    assert params == ("d1", "title", "AI title", "d1", "amount", "9")


def test_replace_text_inserts_all_pages_in_one_statement(monkeypatch):
    monkeypatch.setattr(lakebase, "_ensure_documents_ready", lambda: None)
    calls = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda sql, params=None: calls.append((sql, params)))
    lakebase.replace_text("d1", [{"page": 1, "text": "a"}, {"page": 2, "text": "  "},
                                 {"page": 3, "text": "c"}])
    assert len(calls) == 2                                   # one DELETE, one multi-row INSERT
    assert "DELETE FROM" in calls[0][0]
    ins_sql, ins_params = calls[1]
    assert ins_sql.count("(%s, %s, %s, now())") == 2         # blank page 2 dropped
    assert ins_params == ("d1", 1, "a", "d1", 3, "c")


def test_replace_text_with_no_text_pages_skips_insert(monkeypatch):
    monkeypatch.setattr(lakebase, "_ensure_documents_ready", lambda: None)
    calls = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda sql, params=None: calls.append((sql, params)))
    lakebase.replace_text("d1", [{"page": 1, "text": "   "}])
    assert len(calls) == 1 and "DELETE FROM" in calls[0][0]  # delete still runs, no insert


def test_save_fields_empty_is_a_noop(monkeypatch):
    monkeypatch.setattr(lakebase, "_ensure_documents_ready",
                        lambda: (_ for _ in ()).throw(AssertionError("should not connect")))
    calls = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda *a, **k: calls.append(a))
    lakebase.save_fields("d1", {}, "u@x.com")
    assert calls == []                                     # nothing posted → no write


def test_set_access_grant_revoke_deletes_without_insert(monkeypatch):
    import config
    monkeypatch.setattr(lakebase, "enabled", lambda: True)
    monkeypatch.setattr(config, "USE_LAKEBASE_PERMISSIONS", True)
    monkeypatch.setattr(lakebase, "_ensure_permissions_ready", lambda: None)
    calls = []
    monkeypatch.setattr(lakebase, "pg_execute", lambda sql, params=None: calls.append((sql, params)))
    lakebase.set_access_grant("u@x.com", "NONE")
    assert any("DELETE" in s for s, _ in calls)
    assert not any("INSERT" in s for s, _ in calls)


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


def test_sp_token_prefers_lakebase_prefixed_creds(monkeypatch):
    # The job supplies LAKEBASE_CLIENT_ID/SECRET so the SDK default-auth chain does NOT
    # pick up DATABRICKS_CLIENT_ID/SECRET and re-identify the whole job as the SP.
    import lakebase as lb
    lb._token_cache.clear()
    monkeypatch.setenv("LAKEBASE_OIDC_HOST", "https://ws.example.net")
    monkeypatch.setenv("LAKEBASE_CLIENT_ID", "sp-id")
    monkeypatch.setenv("LAKEBASE_CLIENT_SECRET", "sp-secret")
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"access_token": "minted-jwt", "expires_in": 3600}

    def _fake_post(url, data=None, auth=None, timeout=None):
        captured["url"], captured["auth"] = url, auth
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    assert lb._get_sp_token() == "minted-jwt"
    assert captured["url"] == "https://ws.example.net/oidc/v1/token"
    assert captured["auth"] == ("sp-id", "sp-secret")
    lb._token_cache.clear()


def test_pg_query_reconnects_and_retries_once_on_lost_connection(monkeypatch):
    # A stale/idle-closed socket must not fail the request: pg_query reconnects and retries
    # once. Second connection serves the rows (scale/reliability guard for long-lived workers).
    import lakebase as lb
    import psycopg2
    calls = {"connect": 0, "reset": 0}

    class _Cur:
        def __init__(self, boom): self.boom = boom
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params):
            if self.boom:
                raise psycopg2.OperationalError("server closed the connection unexpectedly")
        description = [("ok",)]
        def fetchall(self): return [{"ok": 1}]

    class _Conn:
        def __init__(self, boom): self.boom = boom
        def cursor(self, **k): return _Cur(self.boom)

    def fake_connect():
        calls["connect"] += 1
        return _Conn(boom=(calls["connect"] == 1))  # first socket dead, second healthy

    monkeypatch.setattr(lb, "_connect", fake_connect)
    monkeypatch.setattr(lb, "_reset_conn", lambda: calls.__setitem__("reset", calls["reset"] + 1))
    assert lb.pg_query("SELECT 1") == [{"ok": 1}]
    assert calls["connect"] == 2 and calls["reset"] == 1  # reconnected exactly once


def test_pg_query_gives_up_after_one_retry(monkeypatch):
    import lakebase as lb
    import psycopg2

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params):
            raise psycopg2.OperationalError("still down")
        description = None
        def fetchall(self): return []

    monkeypatch.setattr(lb, "_connect", lambda: type("C", (), {"cursor": lambda self, **k: _Cur()})())
    monkeypatch.setattr(lb, "_reset_conn", lambda: None)
    import pytest
    with pytest.raises(psycopg2.OperationalError):
        lb.pg_query("SELECT 1")


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
    # api_document reads the whole drawer (document + values + links + tags) in one bundle.
    monkeypatch.setattr(app_module.lakebase, "get_document_bundle",
                        lambda doc_id: {
                            "document": {"doc_id": doc_id, "document_type": "Invoice"},
                            "fields": [{"field_key": "title", "proposed_value": "AI title",
                                        "confirmed_value": "Human title", "source_provenance": "human",
                                        "confidence": None}],
                            "links": [],
                            "tags": ["t1"],
                        })
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


# ── Phase 6 lifecycle: soft-delete / move-refresh / hash-rehydrate (item S) ──
def _capture_pg(monkeypatch, rows=None):
    """Stub the pg layer + readiness so lifecycle SQL can be asserted offline."""
    calls = []
    monkeypatch.setattr(lakebase, "_ensure_documents_ready", lambda: None)
    monkeypatch.setattr(lakebase, "pg_query",
                        lambda sql, params=None: calls.append((sql, params)) or (rows or []))
    return calls


def test_find_by_sha_excludes_soft_deleted(monkeypatch):
    calls = _capture_pg(monkeypatch)
    lakebase.find_by_sha("abc")
    assert "deleted_at IS NULL" in calls[0][0]              # dedup only sees live rows


def test_soft_delete_by_source_ref_returns_ids(monkeypatch):
    calls = _capture_pg(monkeypatch, rows=[{"doc_id": "d1"}, {"doc_id": "d2"}])
    out = lakebase.soft_delete_by_source_ref("drv/i1")
    assert out == ["d1", "d2"]
    sql, params = calls[0]
    assert "SET deleted_at = now()" in sql and "deleted_at IS NULL" in sql and "RETURNING doc_id" in sql
    assert params == ("drv/i1",)


def test_refresh_location_counts_only_real_changes(monkeypatch):
    calls = _capture_pg(monkeypatch, rows=[{"doc_id": "d1"}])   # one row actually changed
    n = lakebase.refresh_location("drv/i1", "/new/path.pdf", "http://new", "2024-01-01T00:00:00Z")
    assert n == 1
    sql = calls[0][0]
    assert "IS DISTINCT FROM" in sql and "deleted_at IS NULL" in sql   # guarded no-op when unchanged


def test_rehydrate_clears_deleted_and_repoints(monkeypatch):
    calls = _capture_pg(monkeypatch)
    lakebase.rehydrate("d1", "drv/i9", "/p.pdf", "http://u")
    sql, params = calls[0]
    assert "deleted_at = NULL" in sql and "source_ref = %s" in sql
    assert params == ("drv/i9", "/p.pdf", "http://u", "d1")


def test_list_documents_hides_soft_deleted(monkeypatch):
    calls = _capture_pg(monkeypatch)
    lakebase.list_documents(None, "u@x.com")
    assert "deleted_at IS NULL" in calls[0][0]


# ── Phase 5 F: search sort + pagination + total count ────────────────────────
def test_search_sort_maps_to_whitelisted_order_and_strips_total(monkeypatch):
    calls = _capture_pg(monkeypatch, rows=[{"doc_id": "d1", "_total": 7},
                                           {"doc_id": "d2", "_total": 7}])
    rows, total = lakebase.search(None, "u@x.com", sort="title", limit=25, offset=50)
    assert total == 7                                   # count(*) OVER() read once
    assert rows == [{"doc_id": "d1"}, {"doc_id": "d2"}]  # _total stripped from every row
    sql, params = calls[0]
    assert "count(*) OVER() AS _total" in sql
    # raw sort value is never interpolated — the whitelist expression is used verbatim
    assert ("ORDER BY lower(coalesce(f_title.confirmed_value, f_title.proposed_value, "
            "d.original_filename)) ASC") in sql
    assert sql.rstrip().endswith("LIMIT %s OFFSET %s")
    assert params[-2:] == [25, 50]                      # LIMIT/OFFSET bind last


def test_search_unknown_sort_falls_back_to_newest(monkeypatch):
    calls = _capture_pg(monkeypatch)
    lakebase.search(None, "u@x.com", sort="; DROP TABLE--")
    assert "ORDER BY d.created_at DESC" in calls[0][0]


def test_search_tag_param_binds_before_limit_offset(monkeypatch):
    calls = _capture_pg(monkeypatch)
    lakebase.search(None, "u@x.com", tag="finance", limit=10, offset=0)
    _sql, params = calls[0]
    assert params[0] == "finance"      # tag JOIN precedes WHERE → binds first
    assert params[-2:] == [10, 0]      # LIMIT/OFFSET still bind last


def test_api_search_returns_rows_and_total_and_clamps_paging(client, fake_db, monkeypatch):
    import app as app_module
    from conftest import route
    fake_db.responder = route([("access_type, allowed_site",
                                [{"access_type": "FULL", "allowed_site": None}])])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    captured = {}

    def _search(sites, email, q, dt, dept, path, tag, sort, limit, offset):
        captured.update(sort=sort, limit=limit, offset=offset)
        return ([{"doc_id": "d1"}], 42)

    monkeypatch.setattr(app_module.lakebase, "search", _search)
    r = client.get("/api/search?sort=oldest&limit=999&offset=-5")
    assert r.status_code == 200
    assert r.get_json() == {"rows": [{"doc_id": "d1"}], "total": 42}
    assert captured == {"sort": "oldest", "limit": 200, "offset": 0}  # clamped to 1..200 / >=0


# ── item O: retrieval passages (char-budget accumulation) ────────────────────
def test_passages_for_docs_joins_filename_and_caps_budget(monkeypatch):
    calls = _capture_pg(monkeypatch, rows=[
        {"doc_id": "d1", "page": 1, "text": "a" * 8000, "filename": "a.pdf"},
        {"doc_id": "d1", "page": 2, "text": "b" * 8000, "filename": "a.pdf"},
        {"doc_id": "d2", "page": 1, "text": "c" * 8000, "filename": "b.pdf"},
    ])
    out = lakebase.passages_for_docs(["d1", "d2"], char_budget=12000)
    sql, params = calls[0]
    assert "document_text" in sql and "JOIN" in sql and "= ANY(%s)" in sql
    assert "ORDER BY t.doc_id, t.page" in sql
    assert params == (["d1", "d2"],)
    # first passage (8000) fits; second pushes past 12000 → included then loop stops; third dropped
    assert [(p["doc_id"], p["page"]) for p in out] == [("d1", 1), ("d1", 2)]
    assert out[0]["filename"] == "a.pdf"


def test_passages_for_docs_empty_ids_is_noop(monkeypatch):
    calls = _capture_pg(monkeypatch)
    assert lakebase.passages_for_docs([]) == []
    assert calls == []                                   # no round-trip when nothing to fetch


# ── item L: relation graph (recursive CTE, both directions, cycle guard) ──────
def test_link_graph_recursive_both_directions_and_cycle_guard(monkeypatch):
    calls = _capture_pg(monkeypatch, rows=[
        {"doc_id": "d1", "original_filename": "a", "document_type": "X",
         "parent_doc_id": "d1", "child_doc_id": "d2", "relationship": "related"}])
    g = lakebase.link_graph("d1", max_depth=25)
    sql0, params0 = calls[0]
    assert "WITH RECURSIVE" in sql0
    # traverses BOTH directions: parent→child and child→parent inside the lateral
    assert "WHERE parent_doc_id = r.doc_id" in sql0 and "WHERE child_doc_id = r.doc_id" in sql0
    assert "NOT (nb.nb = ANY(r.path))" in sql0          # cycle guard via visited path
    assert "array_length(r.path, 1) < %s" in sql0       # depth bound
    assert params0 == ("d1", "d1", 25)
    # then edges among the component, then node metadata (live docs only)
    assert any("SELECT DISTINCT parent_doc_id, child_doc_id, relationship" in c[0] for c in calls)
    assert any("deleted_at IS NULL" in c[0] and "original_filename" in c[0] for c in calls)
    assert isinstance(g["nodes"], list) and isinstance(g["edges"], list)


# ── item L: obligations query (date field_keys, site scope, non-null value) ───
def test_obligations_builds_site_scoped_date_field_query(monkeypatch):
    calls = _capture_pg(monkeypatch, rows=[])
    lakebase.obligations(["site-A"], "u@x.com", ["expiry_date", "effective_date"],
                         document_type="Contract")
    sql, params = calls[0]
    assert "df.field_key = ANY(%s)" in sql
    assert "coalesce(df.confirmed_value, df.proposed_value) IS NOT NULL" in sql
    assert "d.deleted_at IS NULL" in sql
    assert "d.sp_site_id = ANY(%s)" in sql               # site scope applied
    assert "d.document_type = %s" in sql
    # params textual order: date_keys, then site list + email, then document_type
    assert params[0] == ["expiry_date", "effective_date"]
    assert params[1] == ["site-A"] and params[2] == "u@x.com"
    assert params[-1] == "Contract"


def test_obligations_empty_date_keys_short_circuits(monkeypatch):
    calls = _capture_pg(monkeypatch)
    assert lakebase.obligations(None, "u@x.com", []) == []
    assert calls == []                                   # no query when there are no date fields


# ── Phase 4 fix: classify requires a document_type ───────────────────────────
def test_classify_rejects_missing_document_type(client, fake_db, monkeypatch):
    import app as app_module
    from conftest import route
    fake_db.responder = route([("access_type, allowed_site",
                                [{"access_type": "FULL", "allowed_site": None}])])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    called = []
    monkeypatch.setattr(app_module.lakebase, "classify",
                        lambda *a: called.append(a))
    r = client.post("/api/documents/classify", json={"doc_ids": ["d1"], "department": "AP"})
    assert r.status_code == 400 and r.get_json() == {"error": "document_type_required"}
    assert called == []      # nothing classified when the type is absent
