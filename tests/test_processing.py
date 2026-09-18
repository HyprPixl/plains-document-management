"""Processing tier — claim/lease idempotency, extract cache hit/miss, and
import/sync watermarking. The db layer and SharePoint client are faked; the
Azure DI SDK is stubbed in conftest so `processing.job` imports offline.
"""
import json

import pytest

from conftest import route
from processing import extract, job

SPECS = "data_type, extraction_prompt_hint, picklist_source"  # field_specs SELECT


# ── extract cache hit / miss ─────────────────────────────────────────────────
def test_extract_cache_hit_skips_ai_query(fake_db, bind_db):
    bind_db(fake_db, extract)
    fake_db.responder = route([
        (SPECS, [{"field_key": "title", "label": "Title", "data_type": "text",
                  "extraction_prompt_hint": None, "picklist_source": None}]),
        ("SELECT proposed_json FROM", [{"proposed_json": '{"title": "Cached Title"}'}]),
    ])
    out = extract.extract_fields("Invoice", "some text", "sha-abc")
    assert out == {"title": "Cached Title"}
    assert fake_db.queried_matching("ai_query(") == []          # no model call on a hit
    assert fake_db.executed_matching("INSERT INTO") == []       # nothing re-cached


def test_extract_cache_miss_calls_model_and_caches(fake_db, bind_db):
    bind_db(fake_db, extract)
    fake_db.responder = route([
        (SPECS, [{"field_key": "title", "label": "Title", "data_type": "text",
                  "extraction_prompt_hint": None, "picklist_source": None}]),
        ("SELECT proposed_json FROM", []),                       # cache miss
        ("ai_query(", [{"out": '{"title": "Fresh", "bogus": "drop me"}'}]),
    ])
    out = extract.extract_fields("Invoice", "some text", "sha-xyz")
    assert out == {"title": "Fresh"}                             # unknown key filtered out
    cached = fake_db.executed_matching("INSERT INTO")
    assert cached and "sha-xyz" in cached[0] and "extraction_cache" in cached[0].lower()


def test_extract_no_specs_returns_empty(fake_db, bind_db):
    bind_db(fake_db, extract)
    fake_db.responder = route([(SPECS, [])])
    assert extract.extract_fields("Invoice", "text", "sha") == {}
    assert fake_db.queried_matching("ai_query(") == []


def test_extract_blank_text_returns_empty(fake_db, bind_db):
    bind_db(fake_db, extract)
    fake_db.responder = route([(SPECS, [{"field_key": "title", "label": "T",
                                         "data_type": "text", "extraction_prompt_hint": None,
                                         "picklist_source": None}])])
    assert extract.extract_fields("Invoice", "   ", "sha") == {}


# ── claim/lease idempotency ──────────────────────────────────────────────────
def test_claim_batch_claims_with_guarded_lease(fake_db, bind_db):
    bind_db(fake_db, job)
    claimed_rows = [{"doc_id": "d1", "content_sha256": "s", "volume_path": "/v",
                     "original_filename": "a.pdf", "document_type": "Invoice", "attempt_count": 0}]
    fake_db.responder = route([
        ("content_sha256, volume_path, original_filename, document_type, attempt_count", claimed_rows),
        ("SELECT doc_id FROM", [{"doc_id": "d1"}]),              # candidates
    ])
    out = job.claim_batch(5)
    assert out == claimed_rows
    upd = fake_db.executed_matching("UPDATE")
    assert upd, "expected a guarded claim UPDATE"
    sql = upd[0]
    assert "extraction_status = 'processing'" in sql
    assert "extraction_status = 'pending'" in sql               # guard: only still-pending rows
    assert "claim_expires_at IS NULL OR claim_expires_at <= current_timestamp()" in sql


def test_claim_batch_no_candidates_is_noop(fake_db, bind_db):
    bind_db(fake_db, job)
    fake_db.responder = route([("SELECT doc_id FROM", [])])
    assert job.claim_batch(5) == []
    assert fake_db.executes == []                                # nothing claimed, nothing written


# ── watermark helper ─────────────────────────────────────────────────────────
def test_iso_from_epoch():
    assert job._iso_from_epoch(None) is None
    assert job._iso_from_epoch(0) is None
    assert job._iso_from_epoch(86400) == "1970-01-02T00:00:00Z"


# ── delegated sync watermarking + token rotation ─────────────────────────────
def _sync_row():
    return {"id": "sy1", "source_id": "src", "site_id": "st", "site_name": "Site",
            "drive_id": "dr", "folder_id": None, "business_unit": None, "department": None,
            "document_type": None, "user_email": "u@x.com", "refresh_token_enc": "enc"}


def _patch_sp(monkeypatch, **overrides):
    import sharepoint as sp
    defaults = {
        "decrypt": lambda x: "decoded",
        "encrypt": lambda x: "reencrypted",
        "refresh_access": lambda ref: ("access-tok", "new-refresh", 3600),
        "download": lambda a, d, i: b"filebytes",
        "item_has_unique_acl": lambda a, d, i: None,   # no real Graph call from sync tests
    }
    defaults.update(overrides)
    for name, fn in defaults.items():
        monkeypatch.setattr(sp, name, fn, raising=False)
    return sp


def test_sync_one_delta_crawls_seeds_and_persists_link(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    captured = {}

    def delta_changes(access, drive, item_id, delta_link=None, seed_latest=False):
        captured["item_id"] = item_id
        captured["delta_link"] = delta_link
        captured["seed"] = seed_latest
        files = [{"id": "i1", "name": "a.pdf", "mime": "application/pdf",
                  "path": "/p/a.pdf", "web_url": "http://u", "modified": "2024-01-01T00:00:00Z"}]
        return files, [], "DELTA_LINK_2"

    _patch_sp(monkeypatch, delta_changes=delta_changes)
    monkeypatch.setattr(job.ingest, "register_bytes", lambda *a, **k: {"status": "new"})

    n = job._sync_one(_sync_row(), "2020-01-01T00:00:00Z")        # already caught up (has watermark)
    assert n == 1
    assert captured["item_id"] is None                           # whole-drive target
    assert captured["delta_link"] is None                        # none stored yet
    assert captured["seed"] is True                              # seed from now, don't re-crawl
    assert any("refresh_token_enc =" in s for s in fake_db.executed_matching("UPDATE"))
    assert any("delta_link = 'DELTA_LINK_2'" in s for s in fake_db.executed_matching("UPDATE"))


def test_sync_one_delta_resumes_from_stored_link(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    captured = {}

    def delta_changes(access, drive, item_id, delta_link=None, seed_latest=False):
        captured["delta_link"] = delta_link
        captured["seed"] = seed_latest
        return [], [], delta_link                                # unchanged link → no re-persist

    _patch_sp(monkeypatch, delta_changes=delta_changes)
    monkeypatch.setattr(job.ingest, "register_bytes", lambda *a, **k: {"status": "new"})
    s = _sync_row()
    s["delta_link"] = "STORED_LINK"

    assert job._sync_one(s, "2020-01-01T00:00:00Z") == 0
    assert captured["delta_link"] == "STORED_LINK"              # resumes where it left off
    assert captured["seed"] is False                           # never seed once a link exists
    assert not any("delta_link =" in s for s in fake_db.executed_matching("UPDATE"))


def test_sync_one_single_file_skipped_when_older_than_watermark(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    _patch_sp(monkeypatch,
              get_file_meta=lambda a, d, t: {"id": "f1", "name": "old.pdf",
                                             "modified": "2019-01-01T00:00:00Z"})
    monkeypatch.setattr(job.ingest, "register_bytes", lambda *a, **k: {"status": "new"})
    s = _sync_row()
    s["folder_id"] = "f1"
    assert job._sync_one(s, "2020-01-01T00:00:00Z") == 0         # older than watermark -> skipped


def test_sync_one_single_file_synced_when_newer(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    _patch_sp(monkeypatch,
              get_file_meta=lambda a, d, t: {"id": "f1", "name": "new.pdf",
                                             "modified": "2021-01-01T00:00:00Z"})
    monkeypatch.setattr(job.ingest, "register_bytes", lambda *a, **k: {"status": "new"})
    s = _sync_row()
    s["folder_id"] = "f1"
    assert job._sync_one(s, "2020-01-01T00:00:00Z") == 1


def test_sync_one_caps_acl_probes_per_tick(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    monkeypatch.setattr(job, "ACL_PROBE_BUDGET", 2)                  # tiny budget for the test
    probes = []

    def delta_changes(access, drive, item_id, delta_link=None, seed_latest=False):
        files = [{"id": f"i{i}", "name": f"a{i}.pdf", "mime": "application/pdf",
                  "path": f"/p/a{i}.pdf", "web_url": "http://u",
                  "modified": "2024-01-01T00:00:00Z"} for i in range(5)]
        return files, [], "DL"

    def item_has_unique_acl(access, drive, item_id):
        probes.append(item_id)
        return False

    _patch_sp(monkeypatch, delta_changes=delta_changes, item_has_unique_acl=item_has_unique_acl)
    captured = []
    monkeypatch.setattr(job.ingest, "register_bytes",
                        lambda *a, **k: captured.append(k.get("has_unique_acl")) or {"status": "new"})

    n = job._sync_one(_sync_row(), "2020-01-01T00:00:00Z")
    assert n == 5                                                    # all files still registered
    assert probes == ["i0", "i1"]                                   # only budget-many items probed
    assert captured == [False, False, None, None, None]             # past budget -> unknown


# ── Graph delta crawl (sharepoint client) ────────────────────────────────────
def test_delta_changes_pages_files_and_deletes(monkeypatch):
    import sharepoint as sp
    calls = []
    pages = [
        {"value": [
            {"id": "root", "name": "root", "folder": {}},                       # drive root, skipped
            {"id": "f1", "name": "a.pdf", "file": {"mimeType": "application/pdf"},
             "lastModifiedDateTime": "2024-01-01T00:00:00Z", "parentReference": {}},
        ], "@odata.nextLink": "PAGE2"},
        {"value": [
            {"id": "g1", "name": "sub", "folder": {}},                          # folder, skipped
            {"id": "f2", "name": "b.pdf", "file": {}, "parentReference": {}},
            {"id": "gone", "deleted": {"state": "deleted"}},                    # removal
        ], "@odata.deltaLink": "NEW_LINK"},
    ]

    def fake_graph(token, url, params=None):
        calls.append((url, params))
        return pages[len(calls) - 1]

    monkeypatch.setattr(sp, "_graph", fake_graph)
    files, deleted, link = sp.delta_changes("tok", "drv", None)
    assert [f["id"] for f in files] == ["f1", "f2"]              # folders + root filtered out
    assert deleted == ["drv/gone"]
    assert link == "NEW_LINK"
    assert calls[0][0].endswith("/drives/drv/root/delta")       # whole-drive endpoint
    assert calls[0][1]["token"] != "latest" if "token" in calls[0][1] else True
    assert calls[1][0] == "PAGE2" and calls[1][1] is None       # nextLink carries its own query


def test_delta_changes_folder_endpoint_and_seed_latest(monkeypatch):
    import sharepoint as sp
    calls = []
    monkeypatch.setattr(sp, "_graph",
                        lambda t, u, p=None: calls.append((u, p)) or {"value": [], "@odata.deltaLink": "L"})
    files, deleted, link = sp.delta_changes("tok", "drv", "fold1", seed_latest=True)
    assert files == [] and deleted == [] and link == "L"
    assert calls[0][0].endswith("/drives/drv/items/fold1/delta")  # folder-scoped endpoint
    assert calls[0][1]["token"] == "latest"                       # seeded from now


def test_delta_changes_stale_link_triggers_full_resync(monkeypatch):
    import sharepoint as sp
    import requests
    calls = []

    def fake_graph(token, url, params=None):
        calls.append(url)
        if url == "OLD_LINK":
            resp = requests.Response()
            resp.status_code = 410
            raise requests.HTTPError(response=resp)
        return {"value": [{"id": "f1", "name": "a.pdf", "file": {}, "parentReference": {}}],
                "@odata.deltaLink": "FRESH"}

    monkeypatch.setattr(sp, "_graph", fake_graph)
    files, deleted, link = sp.delta_changes("tok", "drv", None, delta_link="OLD_LINK")
    assert [f["id"] for f in files] == ["f1"]                     # recovered via full crawl
    assert link == "FRESH"
    assert calls[0] == "OLD_LINK"                                 # tried the stale link first
    assert calls[1].endswith("/drives/drv/root/delta")           # then restarted full


def test_sync_delegated_claims_with_lease_guard(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    import sharepoint as sp
    monkeypatch.setattr(sp, "encryption_available", lambda: True, raising=False)
    fake_db.responder = route([
        ("token_status = 'ok'", [{"id": "sy1"}]),               # candidates
        ("claimed_by = ", []),                                  # re-read after claim -> not ours
    ])
    assert job.sync_delegated() == 0
    claim = [s for s in fake_db.executed_matching("UPDATE") if "claim_expires_at = current_timestamp()" in s]
    assert claim, "expected a guarded sync claim"
    assert "claim_expires_at IS NULL OR claim_expires_at <= current_timestamp()" in claim[0]


# ── import expansion + counting ──────────────────────────────────────────────
def test_run_import_counts_new_files(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    import sharepoint as sp
    monkeypatch.setattr(sp, "access_token_for", lambda e: "tok", raising=False)
    monkeypatch.setattr(sp, "import_file", lambda *a, **k: {"status": "new"}, raising=False)
    j = {"id": "j1", "user_email": "u@x.com", "drive_id": "dr",
         "selections": json.dumps([{"id": "i1", "name": "a.pdf", "is_folder": False}]),
         "source_id": "src", "site_id": None, "site_name": None,
         "business_unit": None, "document_type": None, "department": None}
    assert job._run_import(j) == 1
    assert any("total_files = 1" in s for s in fake_db.executed_matching("UPDATE"))


def test_run_import_expands_folders(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    import sharepoint as sp
    monkeypatch.setattr(sp, "access_token_for", lambda e: "tok", raising=False)
    monkeypatch.setattr(sp, "import_file", lambda *a, **k: {"status": "new"}, raising=False)
    monkeypatch.setattr(sp, "walk_files",
                        lambda t, d, fid: [{"id": "a"}, {"id": "b"}], raising=False)
    j = {"id": "j2", "user_email": "u@x.com", "drive_id": "dr",
         "selections": json.dumps([{"id": "f1", "name": "folder", "is_folder": True}]),
         "source_id": "src", "site_id": None, "site_name": None,
         "business_unit": None, "document_type": None, "department": None}
    assert job._run_import(j) == 2
    assert any("total_files = 2" in s for s in fake_db.executed_matching("UPDATE"))


# ── free reuse of identical documents (SPEC §9) ──────────────────────────────
def _dup_doc(**kw):
    d = {"doc_id": "d2", "content_sha256": "sha-1", "volume_path": "/v/d2.pdf",
         "original_filename": "dup.pdf", "document_type": "Invoice", "attempt_count": 0}
    d.update(kw)
    return d


def _twin(**kw):
    t = {"doc_id": "d1", "derived_pdf_path": "/v/sha-1.searchable.pdf", "text_source": "native",
         "page_count": 3, "extraction_sig": "v1:native",
         "verification_status": "needs_review", "verified_by": None}
    t.update(kw)
    return t


def test_find_twin_filters_by_sha_type_and_prompt_version(fake_db, bind_db):
    bind_db(fake_db, job)
    fake_db.responder = route([("content_sha256 =", [{"doc_id": "d1"}])], default=[])
    assert job._find_twin(_dup_doc()) == {"doc_id": "d1"}
    q = fake_db.queried_matching("content_sha256 =")[0]
    assert "extraction_status = 'done'" in q        # only finished twins
    assert "document_type <=>" in q                 # null-safe same-type match
    assert "extraction_sig LIKE" in q and "v1:" in q  # current prompt/schema only
    assert "doc_id <> " in q                         # never itself


def test_find_twin_none_without_sha(fake_db, bind_db):
    bind_db(fake_db, job)
    assert job._find_twin(_dup_doc(content_sha256=None)) is None
    assert fake_db.queries == []                     # short-circuits, no lookup


def test_process_doc_reuses_twin_and_skips_ocr(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    fake_db.responder = route([("content_sha256 =", [_twin()])], default=[])
    monkeypatch.setattr(job.ocr, "process",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OCR must be skipped")))
    job.process_doc(_dup_doc())
    # Text + fields copied; doc marked done; NOT verified (twin wasn't verified).
    assert fake_db.executed_matching("INSERT INTO product_dev.document_hub.document_text")
    assert fake_db.executed_matching("MERGE INTO product_dev.document_hub.document_fields")
    done = fake_db.executed_matching("extraction_status = 'done'")
    assert done and "verification_status = 'verified'" not in done[0]


def test_process_doc_lands_verified_from_verified_twin(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    fake_db.responder = route(
        [("content_sha256 =", [_twin(verification_status="verified", verified_by="u@x.com")])],
        default=[])
    monkeypatch.setattr(job.ocr, "process",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OCR must be skipped")))
    job.process_doc(_dup_doc())
    done = fake_db.executed_matching("extraction_status = 'done'")
    assert done and "verification_status = 'verified'" in done[0] and "u@x.com" in done[0]
    assert "mirror_status = 'not_mirrored'" in done[0]  # re-mirror to its own SP location


def test_process_doc_no_twin_runs_normal_ocr(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    fake_db.responder = route([("content_sha256 =", [])], default=[])  # no twin
    ocr_called = {}
    monkeypatch.setattr(job, "_read_volume", lambda p: b"bytes")
    monkeypatch.setattr(job.ocr, "process", lambda data, name: ocr_called.setdefault("hit", True) or
                        {"searchable_pdf": None, "pages": [], "text_source": "native", "page_count": 0})
    monkeypatch.setattr(job.extract, "extract_fields", lambda *a, **k: {})
    job.process_doc(_dup_doc())
    assert ocr_called.get("hit")                     # fell through to real extraction


def test_process_imports_claims_with_lease_guard(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    import sharepoint as sp
    monkeypatch.setattr(sp, "encryption_available", lambda: True, raising=False)
    fake_db.responder = route([
        ("status IN ('queued', 'processing')", [{"id": "j1"}]),  # candidates
        ("SELECT id, user_email, drive_id, selections", []),     # re-read after claim -> not ours
    ])
    assert job.process_imports() == 0
    claim = [s for s in fake_db.executed_matching("UPDATE") if "status = 'processing'" in s]
    assert claim, "expected a guarded import claim"
    assert "claim_expires_at IS NULL OR claim_expires_at <= current_timestamp()" in claim[0]
