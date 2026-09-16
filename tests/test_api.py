"""API tier — Flask test client with the db layer faked.

Covers the classify -> enqueue -> save -> verify happy path and its guards,
permission scoping via perms_where, field-def CRUD, and the structured error
handlers (friendly JSON, no traceback leak, context tags in the log).
"""
from conftest import route

ADMIN = [{"access_type": "ADMIN", "allowed_site": None}]
FULL = [{"access_type": "FULL", "allowed_site": None}]
SITE_A = [{"access_type": "READ", "allowed_site": "site-A"}]
NONE = []

PERMS = "access_type, allowed_site"  # distinctive substring of the get_perms query


# ── classify ─────────────────────────────────────────────────────────────────
def test_classify_happy_path(client, fake_db):
    fake_db.responder = route([(PERMS, FULL)])
    r = client.post("/api/documents/classify",
                    json={"doc_ids": ["d1", "d2"], "document_type": "Invoice", "department": "AP"})
    assert r.status_code == 200
    assert r.get_json() == {"updated": 2}
    upd = fake_db.executed_matching("UPDATE")
    assert any("classification_status = 'classified'" in s and "document_type = 'Invoice'" in s
               for s in upd)


def test_classify_requires_doc_ids(client, fake_db):
    fake_db.responder = route([(PERMS, FULL)])
    r = client.post("/api/documents/classify", json={"doc_ids": []})
    assert r.status_code == 400
    assert r.get_json()["error"] == "no doc_ids"


# ── enqueue ──────────────────────────────────────────────────────────────────
def test_enqueue_happy_path(client, fake_db):
    fake_db.responder = route([(PERMS, FULL)])
    r = client.post("/api/documents/enqueue", json={"doc_ids": ["d1"]})
    assert r.status_code == 200
    assert r.get_json() == {"enqueued": 1}
    assert any("extraction_status = 'pending'" in s and "classification_status = 'classified'" in s
               for s in fake_db.executed_matching("UPDATE"))


def test_enqueue_requires_doc_ids(client, fake_db):
    r = client.post("/api/documents/enqueue", json={"doc_ids": []})
    assert r.status_code == 400


# ── save fields (only changed fields; provenance stamping) ───────────────────
def test_save_fields_only_writes_supplied_fields(client, fake_db):
    """PIN 7632ee1: save must MERGE exactly the fields posted — no others."""
    r = client.post("/api/documents/d1/fields", json={"values": {"title": "New Title"}})
    assert r.status_code == 200
    assert r.get_json() == {"saved": 1}
    merges = fake_db.executed_matching("MERGE INTO")
    assert len(merges) == 1                                   # exactly one field written
    sql = merges[0]
    assert "'title' AS field_key" in sql
    assert "confirmed_value = 'New Title'" in sql
    assert "source_provenance = 'human'" in sql


def test_save_fields_empty_writes_nothing(client, fake_db):
    r = client.post("/api/documents/d1/fields", json={"values": {}})
    assert r.status_code == 200
    assert r.get_json() == {"saved": 0}
    assert fake_db.executed_matching("MERGE INTO") == []


# ── verify happy path + guards ───────────────────────────────────────────────
def test_verify_happy_path_with_confirmed_value(client, fake_db):
    fake_db.responder = route([
        ("SELECT document_type FROM", [{"document_type": "Invoice"}]),
        ("required_for_verify = true", [{"field_key": "title"}]),
        ("coalesce(confirmed_value, proposed_value)", [{"field_key": "title"}]),
    ])
    r = client.post("/api/documents/d1/verify")
    assert r.status_code == 200
    assert r.get_json() == {"verified": True}
    assert any("verification_status = 'verified'" in s for s in fake_db.executed_matching("UPDATE"))


def test_verify_accepts_unedited_ai_proposal_via_coalesce(client, fake_db):
    """PIN: a required field satisfied only by proposed_value (no confirmed) still verifies,
    and the satisfaction check uses coalesce(confirmed_value, proposed_value)."""
    fake_db.responder = route([
        ("SELECT document_type FROM", [{"document_type": "Invoice"}]),
        ("required_for_verify = true", [{"field_key": "title"}]),
        # the "have" query returns the key because coalesce found the AI proposal
        ("coalesce(confirmed_value, proposed_value)", [{"field_key": "title"}]),
    ])
    r = client.post("/api/documents/d1/verify")
    assert r.status_code == 200
    # the load-bearing verify rule is expressed in SQL
    assert fake_db.queried_matching("coalesce(confirmed_value, proposed_value) IS NOT NULL")


def test_verify_blocks_on_missing_required(client, fake_db):
    fake_db.responder = route([
        ("SELECT document_type FROM", [{"document_type": "Invoice"}]),
        ("required_for_verify = true", [{"field_key": "title"}, {"field_key": "amount"}]),
        ("coalesce(confirmed_value, proposed_value)", [{"field_key": "title"}]),  # amount missing
    ])
    r = client.post("/api/documents/d1/verify")
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"] == "missing_required"
    assert body["fields"] == ["amount"]
    assert fake_db.executed_matching("verification_status = 'verified'") == []


def test_verify_amendment_needs_parent(client, fake_db):
    fake_db.responder = route([
        ("SELECT document_type FROM", [{"document_type": "Amendment"}]),
        ("required_for_verify = true", []),
        ("coalesce(confirmed_value, proposed_value)", []),
        ("amendment_of", []),                # no parent link
    ])
    r = client.post("/api/documents/d1/verify")
    assert r.status_code == 400
    assert r.get_json()["error"] == "amendment_needs_parent"


def test_verify_amendment_with_parent_succeeds(client, fake_db):
    fake_db.responder = route([
        ("SELECT document_type FROM", [{"document_type": "Amendment"}]),
        ("required_for_verify = true", []),
        ("coalesce(confirmed_value, proposed_value)", []),
        ("amendment_of", [{"1": 1}]),        # parent link exists
    ])
    r = client.post("/api/documents/d1/verify")
    assert r.status_code == 200
    assert r.get_json() == {"verified": True}


def test_verify_not_found(client, fake_db):
    fake_db.responder = route([("SELECT document_type FROM", [])])
    r = client.post("/api/documents/nope/verify")
    assert r.status_code == 404


# ── permission scoping via perms_where ───────────────────────────────────────
def test_documents_scoped_to_allowed_sites(client, fake_db):
    fake_db.responder = route([(PERMS, SITE_A)], default=[])
    r = client.get("/api/documents")
    assert r.status_code == 200
    docs_q = fake_db.queried_matching("FROM " + __import__("config").DOCUMENTS)
    assert any("sp_site_id IN ('site-A')" in s for s in docs_q)


def test_documents_full_access_unrestricted(client, fake_db):
    fake_db.responder = route([(PERMS, FULL)], default=[])
    r = client.get("/api/documents")
    assert r.status_code == 200
    docs_q = fake_db.queried_matching("ORDER BY created_at DESC LIMIT 500")
    assert docs_q and "1=0" not in docs_q[0] and "sp_site_id IN" not in docs_q[0]


def test_documents_no_perms_sees_nothing(client, fake_db):
    fake_db.responder = route([(PERMS, NONE)], default=[])
    r = client.get("/api/documents")
    assert r.status_code == 200
    assert any("1=1 AND 1=0" in s for s in fake_db.queried_matching("LIMIT 500"))


# ── field-def CRUD ───────────────────────────────────────────────────────────
def test_field_def_create_happy_path(client, fake_db):
    fake_db.responder = route([
        (PERMS, ADMIN),
        ("SELECT 1 FROM", []),                 # no existing key
        ("max(sort_order)", [{"m": 3}]),
    ])
    r = client.post("/api/field-defs",
                    json={"field_key": "po_number", "label": "PO Number", "applies_to": "Invoice"})
    assert r.status_code == 200
    assert r.get_json()["field_key"] == "po_number"
    ins = fake_db.executed_matching("INSERT INTO")
    assert any("field_key" in s and "po_number" in s for s in ins)


def test_field_def_create_forbidden_for_non_admin(client, fake_db):
    fake_db.responder = route([(PERMS, SITE_A)])
    r = client.post("/api/field-defs", json={"field_key": "x", "label": "X"})
    assert r.status_code == 403


def test_field_def_create_rejects_bad_key(client, fake_db):
    fake_db.responder = route([(PERMS, ADMIN)])
    r = client.post("/api/field-defs", json={"field_key": "BadKey!", "label": "X"})
    assert r.status_code == 400


def test_field_def_create_requires_key_and_label(client, fake_db):
    fake_db.responder = route([(PERMS, ADMIN)])
    r = client.post("/api/field-defs", json={"field_key": "", "label": ""})
    assert r.status_code == 400


def test_field_def_create_conflict_on_existing(client, fake_db):
    fake_db.responder = route([
        (PERMS, ADMIN),
        ("SELECT 1 FROM", [{"1": 1}]),         # key already exists
    ])
    r = client.post("/api/field-defs", json={"field_key": "dupe", "label": "Dupe"})
    assert r.status_code == 409


def test_field_def_update_sets_only_supplied_columns(client, fake_db):
    fake_db.responder = route([(PERMS, ADMIN)])
    r = client.put("/api/field-defs/po_number", json={"label": "PO #", "required_for_verify": True})
    assert r.status_code == 200
    upd = fake_db.executed_matching("UPDATE")
    assert any("label = 'PO #'" in s and "required_for_verify = true" in s
               and "data_type" not in s for s in upd)


def test_field_def_delete_is_soft(client, fake_db):
    fake_db.responder = route([(PERMS, ADMIN)])
    r = client.delete("/api/field-defs/po_number")
    assert r.status_code == 200
    assert any("active = false" in s for s in fake_db.executed_matching("UPDATE"))


# ── admin warehouse-latency bench ────────────────────────────────────────────
def test_admin_bench_forbidden_for_non_admin(client, fake_db):
    fake_db.responder = route([(PERMS, SITE_A)])
    r = client.post("/api/admin/bench")
    assert r.status_code == 403


def test_admin_bench_returns_expected_shape_for_admin(client, fake_db):
    # Admin (FULL scope) + a discoverable doc_id so the single-document op also runs.
    fake_db.responder = route([
        (PERMS, ADMIN),
        ("ORDER BY created_at DESC LIMIT 1", [{"doc_id": "d1"}]),
    ], default=[])
    r = client.post("/api/admin/bench?n=3")
    assert r.status_code == 200
    body = r.get_json()
    assert body["n"] == 3
    assert "captured_at" in body and "wall_ms" in body
    ops = body["operations"]
    for name in ("documents", "stats", "search", "document"):
        assert name in ops
        assert set(ops[name]) == {"p50", "p95", "max", "mean", "n"}
        assert ops[name]["n"] == 3


def test_admin_bench_caps_n(client, fake_db):
    fake_db.responder = route([(PERMS, ADMIN)], default=[])
    r = client.post("/api/admin/bench?n=9999")
    assert r.status_code == 200
    assert r.get_json()["n"] == 100


# ── structured error handlers ────────────────────────────────────────────────
def _raiser(msg):
    def _r(sql):
        raise RuntimeError(msg)
    return _r


def test_unhandled_exception_returns_friendly_json_no_traceback(client, fake_db, app_logs):
    fake_db.responder = _raiser("boom-secret-detail")
    r = client.get("/api/stats")
    assert r.status_code == 500
    body = r.get_json()
    assert body["error"] == "internal_error"
    assert "detail" in body and body["request_id"]
    text = r.get_data(as_text=True)
    assert "boom-secret-detail" not in text        # no internal message leaked
    assert "Traceback" not in text                 # no stack leaked to the browser


def test_error_is_logged_with_context_and_traceback(client, fake_db, app_logs):
    fake_db.responder = _raiser("boom-secret-detail")
    client.get("/api/stats")
    msgs = [rec.getMessage() for rec in app_logs]
    unhandled = [m for m in msgs if "unhandled exception" in m]
    assert unhandled, "expected an 'unhandled exception' log line"
    line = unhandled[0]
    # context tag convention (AGENTS.md): request_id / method / route / user
    assert "request_id=" in line and "method=GET" in line
    assert "route=/api/stats" in line and "user=" in line
    assert "Traceback" in line                      # full stack DOES go to the log


def test_httpexception_returns_friendly_json(client):
    r = client.get("/definitely-not-a-route")
    assert r.status_code == 404
    body = r.get_json()
    assert body["error"] == "not_found"
    assert "detail" in body
    assert "Traceback" not in r.get_data(as_text=True)
