"""Shared document-ingestion primitive.

One code path for landing bytes into the store + registering a `documents` row,
used by in-app upload, SharePoint import, and the SharePoint sync loop. Dedup is
by SHA-256: identical bytes already stored return the existing doc as a duplicate
rather than creating a second copy.
"""
import hashlib
import io
import os
import re
import uuid

from databricks.sdk import WorkspaceClient

import config
import lakebase
from db import query, execute, lit

_w = WorkspaceClient()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Cheap document-type guess from the file name + SharePoint path. This only *suggests* a type
# (the doc still lands unclassified and waits for a human to confirm); it saves the reviewer
# from picking from scratch when the name/folder is a dead giveaway. Ordered most-specific
# first, since e.g. an "amendment" is also an "agreement" and a COI is also a "policy".
_TYPE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("Amendment", ("amendment", "addendum")),
    ("Purchase Order", ("purchase order", "purchaseorder", " po ", "po#", "p o number")),
    ("Invoice", ("invoice", "inv#", "remittance")),
    ("Insurance / Certificate", ("insurance", "certificate of insurance", "coi", "acord")),
    ("Land / Right-of-Way", ("right of way", "right-of-way", "row agreement", "easement",
                              "surface use", "damage settlement", "plat")),
    ("Permit / Regulatory", ("permit", "regulatory", "authorization to construct", "epa ",
                              "notice of violation")),
    ("Inspection / Integrity Report", ("inspection", "integrity", "corrosion", "ndt", " ili ",
                                        "dig report", "cathodic")),
    ("Financial Statement", ("financial statement", "balance sheet", "income statement",
                              "annual report", "10-k", "10-q")),
    ("HR / Personnel", ("resume", "offer letter", "personnel", "onboarding", "timesheet",
                         "payroll", "employee handbook", "performance review")),
    ("Policy / Procedure", ("policy", "procedure", "standard", "sop", "guideline", "manual",
                             "work instruction")),
    ("Project (Engineering)", ("as-built", "as built", "p&id", "isometric", "datasheet",
                                "data sheet", "afe", "engineering", "drawing", "spec sheet")),
    ("Correspondence", ("correspondence", "letter", "memo", "notice", "email")),
    ("Contract / Agreement", ("contract", "agreement", "msa", "nda", "lease", "sow",
                               "statement of work", "master service")),
]


def guess_document_type(filename: str | None, sp_path: str | None = None) -> str | None:
    """Best-effort document_type from the name/path, or None if nothing matches."""
    hay = " " + re.sub(r"[_\-.]+", " ", f"{filename or ''} {sp_path or ''}".lower()) + " "
    hay = re.sub(r"\s+", " ", hay)
    for dtype, needles in _TYPE_HINTS:
        if any(n in hay for n in needles):
            return dtype
    return None


def register_bytes(
    data: bytes,
    filename: str,
    mime: str | None,
    *,
    source_id: str,
    source_ref: str,
    created_by: str,
    subdir: str = "uploads",
    batch_id: str | None = None,
    business_unit: str | None = None,
    document_type: str | None = None,
    department: str | None = None,
    file_modified_at: str | None = None,
    sp_site_id: str | None = None,
    sp_site_name: str | None = None,
    sp_drive_id: str | None = None,
    sp_path: str | None = None,
    sp_web_url: str | None = None,
    has_unique_acl: bool | None = None,
) -> dict:
    """Land bytes + register a document (idempotent by content hash).

    Returns {status: new|duplicate, doc_id, filename, ...}. When classification is
    supplied the doc is marked classified + pending so the processing job picks it up;
    otherwise it lands unclassified for the Manage queue.
    """
    sha = sha256(data)
    if lakebase.docs_enabled():
        existing = lakebase.find_by_sha(sha)
    else:
        existing = query(
            f"SELECT doc_id, original_filename, verification_status FROM {config.DOCUMENTS} "
            f"WHERE content_sha256 = {lit(sha)} LIMIT 1"
        )
    if existing:
        e = existing[0]
        return {"status": "duplicate", "filename": filename,
                "existing_doc_id": e["doc_id"], "existing_name": e["original_filename"],
                "existing_status": e["verification_status"]}

    # Hash-rehydrate (Phase 6 lifecycle): identical bytes to a soft-deleted doc → revive that
    # row (keeping its extraction/fields) and re-point it at this location, rather than orphaning.
    if lakebase.docs_enabled():
        revived = lakebase.find_deleted_by_sha(sha)
        if revived:
            r = revived[0]
            lakebase.rehydrate(r["doc_id"], source_ref, sp_path, sp_web_url)
            return {"status": "rehydrated", "doc_id": r["doc_id"], "filename": filename,
                    "existing_doc_id": r["doc_id"], "existing_name": r["original_filename"]}

    doc_id = "d_" + uuid.uuid4().hex
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    vpath = f"{config.DOCS_VOLUME}/{subdir}/{sha}{ext}"
    _w.files.upload(vpath, io.BytesIO(data), overwrite=True)

    # An explicit type/business_unit (e.g. from a configured auto-sync) classifies the doc.
    # Otherwise leave it unclassified but pre-fill a *suggested* type from the name/path so the
    # reviewer just confirms rather than picking blind.
    classified = bool(business_unit or document_type)
    if not classified and not document_type:
        document_type = guess_document_type(filename, sp_path)
    cstatus = "classified" if classified else "unclassified"
    if lakebase.docs_enabled():
        lakebase.insert_document(
            doc_id=doc_id, content_sha256=sha, volume_path=vpath, original_filename=filename,
            mime_type=mime, size_bytes=len(data), source_id=source_id, source_ref=source_ref,
            batch_id=batch_id, business_unit=business_unit, document_type=document_type,
            department=department, sp_site_id=sp_site_id, sp_site_name=sp_site_name,
            sp_drive_id=sp_drive_id, sp_path=sp_path, sp_web_url=sp_web_url,
            has_unique_acl=has_unique_acl,
            classification_status=cstatus, extraction_status="pending",
            verification_status="needs_review", mirror_status="not_mirrored",
            attempt_count=0, file_modified_at=file_modified_at, created_by=created_by,
        )
    else:
        execute(
            f"INSERT INTO {config.DOCUMENTS} "
            f"(doc_id, content_sha256, volume_path, original_filename, mime_type, size_bytes, "
            f" source_id, source_ref, batch_id, business_unit, document_type, department, "
            f" sp_site_id, sp_site_name, sp_drive_id, sp_path, sp_web_url, "
            f" classification_status, extraction_status, verification_status, mirror_status, "
            f" attempt_count, file_modified_at, created_at, created_by, updated_at) "
            f"VALUES ({lit(doc_id)}, {lit(sha)}, {lit(vpath)}, {lit(filename)}, {lit(mime)}, {len(data)}, "
            f"{lit(source_id)}, {lit(source_ref)}, {lit(batch_id)}, {lit(business_unit)}, "
            f"{lit(document_type)}, {lit(department)}, "
            f"{lit(sp_site_id)}, {lit(sp_site_name)}, {lit(sp_drive_id)}, {lit(sp_path)}, {lit(sp_web_url)}, "
            f"{lit(cstatus)}, 'pending', 'needs_review', "
            f"'not_mirrored', 0, {lit(file_modified_at)}, current_timestamp(), {lit(created_by)}, "
            f"current_timestamp())"
        )
    return {"status": "new", "doc_id": doc_id, "filename": filename}
