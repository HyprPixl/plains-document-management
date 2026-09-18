"""Pure-unit tier — no I/O, no mocking required (highest-value, lowest-flake).

Covers: guess_document_type, dedup hashing, extract prompt building + JSON
parsing, and perms_where SQL generation (the last needs the db layer faked
because get_perms issues a query).
"""
import hashlib

import pytest

import ingest
from processing import extract


# ── guess_document_type ──────────────────────────────────────────────────────
@pytest.mark.parametrize("filename,expected", [
    ("First Amendment to Lease.pdf", "Amendment"),        # amendment beats agreement/lease
    ("2024_addendum.docx", "Amendment"),
    ("PO 4500123.pdf", "Purchase Order"),                 # " po " needs word boundaries
    ("invoice_98765.pdf", "Invoice"),
    ("ACORD_COI_2024.pdf", "Insurance / Certificate"),
    ("Pipeline Easement Grant.pdf", "Land / Right-of-Way"),
    ("Air Permit Authorization.pdf", "Permit / Regulatory"),
    ("Cathodic Protection Inspection.pdf", "Inspection / Integrity Report"),
    ("Q3 Balance Sheet.xlsx", "Financial Statement"),
    ("Employee Handbook.pdf", "HR / Personnel"),
    ("Safety SOP 12.pdf", "Policy / Procedure"),
    ("Pump Station P&ID.pdf", "Project (Engineering)"),
    ("Letter to Regulator.docx", "Correspondence"),
    ("Master Service Agreement.pdf", "Contract / Agreement"),
    ("random_photo.jpg", None),
    ("", None),
])
def test_guess_document_type(filename, expected):
    assert ingest.guess_document_type(filename) == expected


def test_guess_document_type_is_case_insensitive():
    assert ingest.guess_document_type("MASTER SERVICE AGREEMENT.PDF") == "Contract / Agreement"


def test_guess_document_type_uses_sp_path_when_no_filename():
    assert ingest.guess_document_type(None, "/sites/Legal/Contracts/lease.pdf") == "Contract / Agreement"


def test_guess_document_type_ordering_amendment_wins_over_contract():
    # An amendment is also an agreement; most-specific must win.
    assert ingest.guess_document_type("Amendment to Master Service Agreement.pdf") == "Amendment"


def test_guess_document_type_handles_none_inputs():
    assert ingest.guess_document_type(None, None) is None


# ── dedup hashing ────────────────────────────────────────────────────────────
def test_sha256_matches_hashlib_and_is_deterministic():
    data = b"identical bytes"
    expected = hashlib.sha256(data).hexdigest()
    assert ingest.sha256(data) == expected
    assert ingest.sha256(data) == ingest.sha256(data)


def test_sha256_differs_for_different_bytes():
    assert ingest.sha256(b"a") != ingest.sha256(b"b")


def test_processing_and_ingest_hash_agree():
    from processing import job
    data = b"round-trip"
    assert job._sha256(data) == ingest.sha256(data) == hashlib.sha256(data).hexdigest()


# ── extract prompt building ──────────────────────────────────────────────────
def _specs():
    return [
        {"field_key": "title", "label": "Title", "data_type": "text",
         "extraction_prompt_hint": "The document title", "picklist_source": None},
        {"field_key": "effective_date", "label": "Effective Date", "data_type": "date",
         "extraction_prompt_hint": None, "picklist_source": None},
        {"field_key": "status", "label": "Status", "data_type": "text",
         "extraction_prompt_hint": None, "picklist_source": "Active|Expired|Draft"},
    ]


def test_build_prompt_includes_type_keys_and_hints():
    prompt = extract._build_prompt("Contract / Agreement", _specs(), "body text")
    assert "Contract / Agreement" in prompt
    assert "Return ONLY a JSON object" in prompt
    assert '"title": The document title' in prompt          # uses hint over label
    assert '"effective_date"' in prompt
    assert "(ISO date YYYY-MM-DD)" in prompt                # date hint appended
    assert "(one of: Active, Expired, Draft)" in prompt     # picklist enumerated
    assert 'body text' in prompt


def test_build_prompt_truncates_long_text():
    long_text = "x" * (extract.MAX_TEXT_CHARS + 5000)
    prompt = extract._build_prompt("X", _specs(), long_text)
    assert ("x" * extract.MAX_TEXT_CHARS) in prompt
    assert ("x" * (extract.MAX_TEXT_CHARS + 1)) not in prompt


def test_build_prompt_falls_back_to_label_when_no_hint():
    prompt = extract._build_prompt("X", _specs(), "t")
    assert '"effective_date": Effective Date' in prompt


# ── extract JSON parsing ─────────────────────────────────────────────────────
def test_parse_json_plain_object():
    assert extract._parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_strips_code_fence():
    raw = '```json\n{"a": "b"}\n```'
    assert extract._parse_json_object(raw) == {"a": "b"}


def test_parse_json_extracts_object_from_surrounding_prose():
    raw = 'Sure! Here is the data: {"a": "b"} — hope that helps.'
    assert extract._parse_json_object(raw) == {"a": "b"}


def test_parse_json_returns_empty_on_garbage():
    assert extract._parse_json_object("not json at all") == {}
    assert extract._parse_json_object("") == {}


# ── perms_where (needs get_perms → query faked) ──────────────────────────────
def _perms(rows):
    """Fake db returning `rows` for the permissions lookup."""
    from conftest import FakeDB
    return FakeDB(responder=lambda sql: rows)


def test_perms_where_full_access_is_unrestricted(bind_db):
    import app as app_module
    fake = _perms([{"access_type": "FULL", "allowed_site": None}])
    bind_db(fake, app_module)
    assert app_module.perms_where("u@x.com") == ""


def test_perms_where_admin_is_unrestricted(bind_db):
    import app as app_module
    fake = _perms([{"access_type": "ADMIN", "allowed_site": None}])
    bind_db(fake, app_module)
    assert app_module.perms_where("u@x.com") == ""


def test_perms_where_scopes_to_allowed_sites(bind_db):
    import app as app_module
    fake = _perms([
        {"access_type": "READ", "allowed_site": "site-A"},
        {"access_type": "READ", "allowed_site": "site-B"},
    ])
    bind_db(fake, app_module)
    where = app_module.perms_where("u@x.com")
    assert where == (
        " AND (sp_site_id IN ('site-A','site-B') "
        "OR (sp_site_id IS NULL AND created_by = 'u@x.com')) "
    )


def test_perms_where_no_permissions_sees_only_own_uploads(bind_db):
    # A user with no site grants still sees their own non-SharePoint uploads (NULL site).
    import app as app_module
    fake = _perms([])
    bind_db(fake, app_module)
    assert app_module.perms_where("u@x.com") == (
        " AND (1=0 OR (sp_site_id IS NULL AND created_by = 'u@x.com')) "
    )


def test_perms_where_respects_custom_column(bind_db):
    import app as app_module
    fake = _perms([{"access_type": "READ", "allowed_site": "site-A"}])
    bind_db(fake, app_module)
    where = app_module.perms_where("u@x.com", "d.sp_site_id")
    assert where == (
        " AND (d.sp_site_id IN ('site-A') "
        "OR (d.sp_site_id IS NULL AND d.created_by = 'u@x.com')) "
    )
