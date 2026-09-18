"""API tier — Flask test client with the db layer faked.

Covers the classify -> enqueue -> save -> verify happy path and its guards,
permission scoping via perms_where, field-def CRUD, and the structured error
handlers (friendly JSON, no traceback leak, context tags in the log).
"""
import io

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
        # _active_field_defs() reads all active defs; the endpoint filters required + applies_to
        ("active = true ORDER BY (applies_to = 'common')", [
            {"field_key": "title", "required_for_verify": True, "applies_to": "common", "data_type": "text"},
            {"field_key": "amount", "required_for_verify": True, "applies_to": "Invoice", "data_type": "number"},
        ]),
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


def test_documents_no_perms_sees_only_own_uploads(client, fake_db):
    # No site grants → no SharePoint docs, but a user still sees their own NULL-site uploads.
    fake_db.responder = route([(PERMS, NONE)], default=[])
    r = client.get("/api/documents")
    assert r.status_code == 200
    q = fake_db.queried_matching("LIMIT 500")
    assert any("1=0 OR (sp_site_id IS NULL AND created_by =" in s for s in q)


# ── upload site picker + filing under a site ─────────────────────────────────
def test_sites_lists_scoped_distinct_sites(client, fake_db):
    fake_db.responder = route([
        (PERMS, SITE_A),
        ("DISTINCT sp_site_id", [{"id": "site-A", "name": "Site A"}]),
    ], default=[])
    r = client.get("/api/sites")
    assert r.status_code == 200
    assert r.get_json()["sites"] == [{"id": "site-A", "name": "Site A"}]
    # the picker query is site-scoped via perms_where
    assert any("sp_site_id IN ('site-A')" in s for s in fake_db.queried_matching("DISTINCT sp_site_id"))


def test_upload_requires_a_site(client, fake_db):
    fake_db.responder = route([(PERMS, SITE_A)], default=[])
    r = client.post("/api/upload", data={"files": (io.BytesIO(b"x"), "a.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["error"] == "site_required"


def test_upload_rejects_site_without_access(client, fake_db):
    fake_db.responder = route([(PERMS, SITE_A)], default=[])
    r = client.post("/api/upload",
                    data={"files": (io.BytesIO(b"x"), "a.pdf"), "sp_site_id": "site-Z"},
                    content_type="multipart/form-data")
    assert r.status_code == 403


# ── Phase 6 ACL instrumentation readout (admin-only) ─────────────────────────
def test_acl_audit_forbidden_for_non_admin(client, fake_db):
    fake_db.responder = route([(PERMS, SITE_A)], default=[])
    r = client.get("/api/admin/acl-audit")
    assert r.status_code == 403


def test_acl_audit_admin_unavailable_off_lakebase(client, fake_db):
    # Admin passes the gate; the flag lives only in Lakebase, so the warehouse mode is 503.
    fake_db.responder = route([(PERMS, ADMIN)], default=[])
    r = client.get("/api/admin/acl-audit")
    assert r.status_code == 503
    assert r.get_json()["error"] == "unavailable"


# ── per-request permission caching (BASELINE.md: perms_where fired 2-3x/page) ─
def test_perms_lookup_issued_once_per_request(client, fake_db):
    """PIN: /api/stats calls perms_where twice, but the single-row permission lookup
    must hit the warehouse exactly once — memoized on flask.g for the request's life."""
    fake_db.responder = route([(PERMS, FULL)], default=[])
    r = client.get("/api/stats")
    assert r.status_code == 200
    assert len(fake_db.queried_matching(PERMS)) == 1


def test_perms_cache_does_not_leak_between_requests(client, fake_db):
    """Each request re-resolves perms (g is per-request) — two requests, two lookups."""
    fake_db.responder = route([(PERMS, FULL)], default=[])
    client.get("/api/stats")
    client.get("/api/stats")
    assert len(fake_db.queried_matching(PERMS)) == 2


# ── single-document endpoint (fewer round-trips, same JSON shape) ─────────────
def test_document_fetches_fields_and_defs_in_one_join(client, fake_db):
    """PIN: defs + this doc's field values come back in a single LEFT JOIN (one fewer
    warehouse round-trip), and the JSON shape the drawer expects is unchanged."""
    DOCS = __import__("config").DOCUMENTS
    FIELDS = __import__("config").DOCUMENT_FIELDS
    doc_row = {"doc_id": "d1", "document_type": "Invoice"}
    def_row = {"field_key": "title", "label": "Title", "data_type": "string",
               "picklist_source": "a|b", "required_for_verify": True, "applies_to": "common",
               "sort_order": 1, "proposed_value": "AI val", "confirmed_value": None,
               "source_provenance": "ai"}
    fake_db.responder = route([
        ("SELECT * FROM " + DOCS, [doc_row]),
        ("LEFT JOIN", [def_row]),
        ("l.relationship", []),
        ("SELECT tag", [{"tag": "alpha"}]),
    ], default=[])
    r = client.get("/api/documents/d1")
    assert r.status_code == 200
    body = r.get_json()
    # JSON shape unchanged: document / fields / links / tags
    assert body["document"] == doc_row
    assert body["tags"] == ["alpha"]
    fld = body["fields"][0]
    assert fld["field_key"] == "title"
    assert fld["proposed_value"] == "AI val"          # value carried by the JOIN, not a 2nd query
    assert fld["confirmed_value"] is None
    assert fld["source_provenance"] == "ai"
    assert fld["options"] == ["a", "b"]               # picklist still split
    # document_fields is touched exactly once, and only via the JOIN (no standalone fetch)
    dfq = fake_db.queried_matching(FIELDS)
    assert len(dfq) == 1 and "LEFT JOIN" in dfq[0]


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


# ── config-read caching (field_defs / taxonomy) ──────────────────────────────
DEFS_SELECT = "extraction_prompt_hint, required_for_verify, sort_order"  # field-defs list read


def test_field_defs_cached_across_requests(client, fake_db):
    fake_db.responder = route([(DEFS_SELECT, [])], default=[])
    client.get("/api/field-defs")
    client.get("/api/field-defs")
    # Same SQL, within TTL → the warehouse field_defs read runs exactly once.
    assert len(fake_db.queried_matching(DEFS_SELECT)) == 1


def test_field_def_write_busts_the_cache(client, fake_db):
    fake_db.responder = route([
        (DEFS_SELECT, []),
        ("SELECT 1 FROM", []),            # create's existence check
        ("max(sort_order)", [{"m": 1}]),
        (PERMS, ADMIN),
    ], default=[])
    client.get("/api/field-defs")                                  # populate cache
    client.post("/api/field-defs", json={"field_key": "po", "label": "PO"})  # busts it
    client.get("/api/field-defs")                                  # must re-read
    assert len(fake_db.queried_matching(DEFS_SELECT)) == 2


# ── admin access management ──────────────────────────────────────────────────
GRANTS = "upper(access_type) <> 'SITE'"  # distinctive substring of _elevated_grants query


def test_access_list_forbidden_for_non_admin(client, fake_db):
    fake_db.responder = route([(PERMS, SITE_A)])
    r = client.get("/api/admin/access")
    assert r.status_code == 403


def test_access_list_collapses_to_strongest_grant_per_user(client, fake_db):
    fake_db.responder = route([
        (PERMS, ADMIN),
        (GRANTS, [
            {"email": "a@plains.com", "access_type": "READ", "updated_at": None},
            {"email": "a@plains.com", "access_type": "ADMIN", "updated_at": None},
            {"email": "b@plains.com", "access_type": "FULL", "updated_at": None},
        ]),
    ])
    r = client.get("/api/admin/access")
    assert r.status_code == 200
    grants = {g["email"]: g["access_type"] for g in r.get_json()["grants"]}
    assert grants == {"a@plains.com": "ADMIN", "b@plains.com": "FULL"}


def test_access_set_grants_writes_warehouse(client, fake_db):
    fake_db.responder = route([(PERMS, ADMIN), (GRANTS, [])])
    r = client.post("/api/admin/access", json={"email": "New@Plains.com", "access_type": "FULL"})
    assert r.status_code == 200
    assert r.get_json()["email"] == "new@plains.com"       # normalised to lower-case
    assert any("<> 'SITE'" in s for s in fake_db.executed_matching("DELETE FROM"))
    ins = fake_db.executed_matching("INSERT INTO")
    assert any("'FULL'" in s and "new@plains.com" in s for s in ins)


def test_access_set_revoke_deletes_without_insert(client, fake_db):
    fake_db.responder = route([(PERMS, ADMIN), (GRANTS, [
        {"email": "gone@plains.com", "access_type": "READ", "updated_at": None}])])
    r = client.post("/api/admin/access", json={"email": "gone@plains.com", "access_type": "NONE"})
    assert r.status_code == 200
    assert any("<> 'SITE'" in s for s in fake_db.executed_matching("DELETE FROM"))
    # No permission row is inserted on revoke (the audit-log INSERT is unrelated).
    assert not any("allowed_site" in s for s in fake_db.executed_matching("INSERT INTO"))


def test_access_set_rejects_bad_email(client, fake_db):
    fake_db.responder = route([(PERMS, ADMIN)])
    r = client.post("/api/admin/access", json={"email": "not-an-email", "access_type": "FULL"})
    assert r.status_code == 400


def test_access_set_rejects_bad_type(client, fake_db):
    fake_db.responder = route([(PERMS, ADMIN)])
    r = client.post("/api/admin/access", json={"email": "x@plains.com", "access_type": "WHEEL"})
    assert r.status_code == 400


def test_access_set_blocks_removing_last_admin(client, fake_db):
    # The caller (default DEV_USER) is the sole admin — demoting them would lock everyone out.
    me = "caleb.fedyshen@plains.com"
    fake_db.responder = route([(PERMS, ADMIN), (GRANTS, [
        {"email": me, "access_type": "ADMIN", "updated_at": None}])])
    r = client.post("/api/admin/access", json={"email": me, "access_type": "FULL"})
    assert r.status_code == 409
    assert not fake_db.executed_matching("DELETE FROM")   # nothing written


def test_access_set_forbidden_for_non_admin(client, fake_db):
    fake_db.responder = route([(PERMS, SITE_A)])
    r = client.post("/api/admin/access", json={"email": "x@plains.com", "access_type": "FULL"})
    assert r.status_code == 403


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


# ── item E: Office render endpoint ────────────────────────────────────────────
DOCS = __import__("config").DOCUMENTS


def test_render_unsupported_ext_returns_415(client, fake_db):
    # Warehouse mode (no PGHOST in tests): the doc row is looked up, visibility passes (FULL),
    # then a non-Office extension short-circuits to 415 before any volume read.
    fake_db.responder = route([
        (PERMS, FULL),
        ("FROM " + DOCS, [{"volume_path": "/v/x.txt", "content_sha256": "sha1",
                           "original_filename": "notes.txt", "sp_site_id": None,
                           "created_by": "caleb.fedyshen@plains.com"}]),
    ], default=[])
    r = client.get("/api/render?doc_id=d1")
    assert r.status_code == 415
    assert r.get_json() == {"error": "unsupported"}


def test_render_not_found_returns_404(client, fake_db):
    fake_db.responder = route([(PERMS, FULL), ("FROM " + DOCS, [])], default=[])
    r = client.get("/api/render?doc_id=missing")
    assert r.status_code == 404


def test_render_forbidden_when_outside_sites(client, fake_db):
    # READ on site-A only; the doc lives on site-Z → not visible → 403 (never renders).
    fake_db.responder = route([
        (PERMS, SITE_A),
        ("FROM " + DOCS, [{"volume_path": "/v/x.docx", "content_sha256": "sha1",
                           "original_filename": "x.docx", "sp_site_id": "site-Z",
                           "created_by": "someone@else.com"}]),
    ], default=[])
    r = client.get("/api/render?doc_id=d1")
    assert r.status_code == 403


# ── item O: keyword-grounded corpus chat (SSE) ────────────────────────────────
class _FakeStreamClient:
    """Minimal stand-in for the OpenAI client: yields two content deltas."""
    def __init__(self, tokens):
        self._tokens = tokens

    class _Delta:
        def __init__(self, c): self.content = c

    class _Choice:
        def __init__(self, c): self._c = c
        @property
        def delta(self): return _FakeStreamClient._Delta(self._c)

    class _Chunk:
        def __init__(self, c): self.choices = [_FakeStreamClient._Choice(c)]

    @property
    def chat(self):
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.last_kwargs = kwargs
                return [_FakeStreamClient._Chunk(t) for t in outer._tokens]

        class _Chat:
            completions = _Completions()
        return _Chat()


def test_chat_unavailable_returns_503(client, fake_db, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "_get_serving_client", lambda force_rebuild=False: None)
    r = client.post("/api/chat", json={"question": "hello?"})
    assert r.status_code == 503
    assert r.get_json() == {"error": "chat_unavailable"}


def test_chat_streams_site_scoped_passages_and_citations(client, fake_db, monkeypatch):
    import app as app_module
    fake_db.responder = route([(PERMS, SITE_A)], default=[])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    captured = {}

    def _search(sites, email, q, limit=8):
        captured.update(sites=sites, q=q, limit=limit)
        return ([{"doc_id": "d1"}], 1)

    monkeypatch.setattr(app_module.lakebase, "search", _search)
    monkeypatch.setattr(app_module.lakebase, "passages_for_docs",
                        lambda ids, char_budget=12000: [
                            {"doc_id": "d1", "page": 3, "text": "the term is 5 years",
                             "filename": "lease.pdf"}])
    fake = _FakeStreamClient(["The term ", "is 5 years [lease.pdf p.3]"])
    monkeypatch.setattr(app_module, "_get_serving_client", lambda force_rebuild=False: fake)

    r = client.post("/api/chat", json={"question": "what is the term?", "history": []})
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert 'data: "The term "' in text
    assert "event: citations" in text
    assert '"filename": "lease.pdf"' in text and '"page": 3' in text
    assert "event: done" in text
    # retrieval was site-scoped: READ on site-A → perms_sites == ["site-A"], limit pinned to 8
    assert captured["sites"] == ["site-A"] and captured["limit"] == 8


# ── item L: relation tree + obligations calendar routes ───────────────────────
def test_tree_forbidden_when_root_not_visible(client, fake_db, monkeypatch):
    import app as app_module
    fake_db.responder = route([(PERMS, SITE_A)], default=[])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    monkeypatch.setattr(app_module.lakebase, "get_document",
                        lambda doc_id: {"doc_id": doc_id, "sp_site_id": "site-Z",
                                        "created_by": "x@y.com"})
    r = client.get("/api/documents/d1/tree")
    assert r.status_code == 403


def test_tree_returns_graph_when_visible(client, fake_db, monkeypatch):
    import app as app_module
    fake_db.responder = route([(PERMS, FULL)], default=[])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    monkeypatch.setattr(app_module.lakebase, "get_document",
                        lambda doc_id: {"doc_id": doc_id, "sp_site_id": None, "created_by": None})
    monkeypatch.setattr(app_module.lakebase, "link_graph", lambda doc_id: {
        "nodes": [{"doc_id": "d1", "original_filename": "a", "document_type": "X"}],
        "edges": [{"parent_doc_id": "d1", "child_doc_id": "d2", "relationship": "amendment_of"}]})
    r = client.get("/api/documents/d1/tree")
    assert r.status_code == 200
    body = r.get_json()
    assert body["root"] == "d1"
    assert body["nodes"][0]["doc_id"] == "d1"
    assert body["edges"][0]["relationship"] == "amendment_of"


def test_obligations_parses_and_filters_by_range(client, fake_db, monkeypatch):
    import app as app_module
    fake_db.responder = route([
        (PERMS, FULL),
        # _active_field_defs() returns all active defs; obligations filters data_type == 'date'
        ("active = true ORDER BY (applies_to = 'common')",
         [{"field_key": "expiry_date", "label": "Expiry Date", "data_type": "date"}]),
    ], default=[])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    rows = [
        {"doc_id": "d1", "original_filename": "a.pdf", "document_type": "Contract",
         "field_key": "expiry_date", "value": "2026-10-01",
         "sp_web_url": "http://a", "sp_site_name": "S"},
        {"doc_id": "d2", "original_filename": "b.pdf", "document_type": "Contract",
         "field_key": "expiry_date", "value": "2099-01-01",  # outside the [from,to] window
         "sp_web_url": None, "sp_site_name": None},
        {"doc_id": "d3", "original_filename": "c.pdf", "document_type": "Contract",
         "field_key": "expiry_date", "value": "not a date",  # unparseable → skipped
         "sp_web_url": None, "sp_site_name": None},
    ]
    monkeypatch.setattr(app_module.lakebase, "obligations", lambda *a, **k: rows)
    r = client.get("/api/obligations?from=2026-01-01&to=2026-12-31")
    assert r.status_code == 200
    body = r.get_json()
    assert [x["doc_id"] for x in body] == ["d1"]         # only the in-range, parseable row
    assert body[0]["field_label"] == "Expiry Date"
    assert body[0]["date"] == "2026-10-01"


def test_obligations_empty_when_no_date_fields(client, fake_db, monkeypatch):
    import app as app_module
    fake_db.responder = route([(PERMS, FULL), ("data_type='date'", [])], default=[])
    monkeypatch.setattr(app_module.lakebase, "docs_enabled", lambda: True)
    r = client.get("/api/obligations")
    assert r.status_code == 200
    assert r.get_json() == []


# ── config reads route through Lakebase mirror with warehouse fallback ────────
def test_active_field_defs_prefers_lakebase(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.lakebase, "config_enabled", lambda: True)
    monkeypatch.setattr(app_module.lakebase, "read_field_defs", lambda: [{"field_key": "lb"}])
    assert app_module._active_field_defs() == [{"field_key": "lb"}]     # served from the mirror


def test_active_field_defs_falls_back_to_warehouse_on_error(client, fake_db, monkeypatch):
    import app as app_module
    from conftest import route
    monkeypatch.setattr(app_module.lakebase, "config_enabled", lambda: True)

    def boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(app_module.lakebase, "read_field_defs", boom)
    fake_db.responder = route([("active = true ORDER BY (applies_to = 'common')",
                                [{"field_key": "wh"}])])
    with app_module.app.test_request_context():        # _req_ctx() in the warning reads flask.g
        assert app_module._active_field_defs() == [{"field_key": "wh"}]  # warehouse fallback


def test_active_taxonomy_falls_back_to_warehouse_on_error(client, fake_db, monkeypatch):
    import app as app_module
    from conftest import route
    monkeypatch.setattr(app_module.lakebase, "config_enabled", lambda: True)

    def boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(app_module.lakebase, "read_taxonomy", boom)
    fake_db.responder = route([("FROM " + app_module.config.TAXONOMY,
                                [{"category": "department", "value": "AP", "label": "AP",
                                  "business_unit": None, "sort_order": 1}])])
    with app_module.app.test_request_context():
        rows = app_module._active_taxonomy()
    assert rows and rows[0]["value"] == "AP"


def test_field_def_create_resyncs_config_mirror(client, fake_db, monkeypatch):
    import app as app_module
    from conftest import route
    fake_db.responder = route([
        ("access_type, allowed_site", [{"access_type": "ADMIN", "allowed_site": None}]),
        ("WHERE field_key =", []),                       # no existing key → create proceeds
        ("max(sort_order)", [{"m": 3}]),
    ])
    resynced = []
    monkeypatch.setattr(app_module.lakebase, "resync_config", lambda: resynced.append(1))
    r = client.post("/api/field-defs", json={"field_key": "new_key", "label": "New"})
    assert r.status_code == 200
    assert resynced == [1]                               # write pushed into the read mirror
