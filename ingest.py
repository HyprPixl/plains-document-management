"""Shared document-ingestion primitive.

One code path for landing bytes into the store + registering a `documents` row,
used by in-app upload, SharePoint import, and the SharePoint sync loop. Dedup is
by SHA-256: identical bytes already stored return the existing doc as a duplicate
rather than creating a second copy.
"""
import hashlib
import io
import os
import uuid

from databricks.sdk import WorkspaceClient

import config
from db import query, execute, lit

_w = WorkspaceClient()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
) -> dict:
    """Land bytes + register a document (idempotent by content hash).

    Returns {status: new|duplicate, doc_id, filename, ...}. When classification is
    supplied the doc is marked classified + pending so the processing job picks it up;
    otherwise it lands unclassified for the Manage queue.
    """
    sha = sha256(data)
    existing = query(
        f"SELECT doc_id, original_filename, verification_status FROM {config.DOCUMENTS} "
        f"WHERE content_sha256 = {lit(sha)} LIMIT 1"
    )
    if existing:
        e = existing[0]
        return {"status": "duplicate", "filename": filename,
                "existing_doc_id": e["doc_id"], "existing_name": e["original_filename"],
                "existing_status": e["verification_status"]}

    doc_id = "d_" + uuid.uuid4().hex
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    vpath = f"{config.DOCS_VOLUME}/{subdir}/{sha}{ext}"
    _w.files.upload(vpath, io.BytesIO(data), overwrite=True)

    classified = bool(business_unit or document_type)
    execute(
        f"INSERT INTO {config.DOCUMENTS} "
        f"(doc_id, content_sha256, volume_path, original_filename, mime_type, size_bytes, "
        f" source_id, source_ref, batch_id, business_unit, document_type, department, "
        f" classification_status, extraction_status, verification_status, mirror_status, "
        f" attempt_count, file_modified_at, created_at, created_by, updated_at) "
        f"VALUES ({lit(doc_id)}, {lit(sha)}, {lit(vpath)}, {lit(filename)}, {lit(mime)}, {len(data)}, "
        f"{lit(source_id)}, {lit(source_ref)}, {lit(batch_id)}, {lit(business_unit)}, "
        f"{lit(document_type)}, {lit(department)}, "
        f"{lit('classified' if classified else 'unclassified')}, 'pending', 'needs_review', "
        f"'not_mirrored', 0, {lit(file_modified_at)}, current_timestamp(), {lit(created_by)}, "
        f"current_timestamp())"
    )
    return {"status": "new", "doc_id": doc_id, "filename": filename}
