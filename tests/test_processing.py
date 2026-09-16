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
    }
    defaults.update(overrides)
    for name, fn in defaults.items():
        monkeypatch.setattr(sp, name, fn, raising=False)
    return sp


def test_sync_one_passes_watermark_and_rotates_token(fake_db, bind_db, monkeypatch):
    bind_db(fake_db, job)
    captured = {}

    def walk_files(access, drive, folder, modified_after=None):
        captured["watermark"] = modified_after
        return [{"id": "i1", "name": "a.pdf", "mime": "application/pdf",
                 "path": "/p/a.pdf", "web_url": "http://u", "modified": "2024-01-01T00:00:00Z"}]

    _patch_sp(monkeypatch, walk_files=walk_files)
    monkeypatch.setattr(job.ingest, "register_bytes", lambda *a, **k: {"status": "new"})

    n = job._sync_one(_sync_row(), "2020-01-01T00:00:00Z")
    assert n == 1
    assert captured["watermark"] == "2020-01-01T00:00:00Z"       # only-newer watermark forwarded
    assert any("refresh_token_enc =" in s for s in fake_db.executed_matching("UPDATE"))


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
