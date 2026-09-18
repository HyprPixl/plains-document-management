"""Document Hub processing job — the heavy-lifting worker (SPEC §4A, §10).

Runs on Databricks compute (a Job), NOT in the web process. Each pass:
  1. SharePoint sweep (optional): discover new/changed files in connected sources,
     dedupe by SHA-256, register new documents in `documents`.
  2. Claim a batch of pending documents with a time-boxed lease (concurrency-safe).
  3. For each: fetch bytes → OCR/text-recover → write derived searchable PDF to the
     volume → extract admin-schema fields → commit results.
  4. Release claims; failed docs get bounded retries with backoff.

Every step is idempotent and re-entrant: a killed worker's leases expire and the
work is re-claimed; committed docs are skipped; nothing is lost on restart.

Usage (on a cluster / job task):
    python -m processing.job --once            # one pass, then exit
    python -m processing.job --loop --sweep    # continuous, incl. SharePoint sweep
"""
import argparse
import hashlib
import io
import json
import logging
import os
import socket
import sys
import time
import traceback
import uuid

from databricks.sdk import WorkspaceClient

import config
import ingest
import lakebase
import sharepoint as sp
from db import query, execute, lit
from . import graph, ocr, extract

# Structured logs to stdout so the Databricks Job log captures every processing failure
# with context (doc_id, stage, traceback) — same format as the app side. We attach our own
# handler (rather than basicConfig, which is a no-op once the SDK has configured root) so the
# format is guaranteed regardless of import order.
logger = logging.getLogger("doc_hub.processing")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
logger.handlers = [_handler]
logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
logger.propagate = False  # our stdout handler is the only sink — avoid double lines

_w = WorkspaceClient()
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
BATCH = int(os.getenv("PROCESS_BATCH", "8"))
BACKOFF_BASE = 60  # seconds; exponential per attempt


# ─────────────────────────────────────────────────────────── volume IO ──

def _read_volume(path: str) -> bytes:
    return _w.files.download(path).contents.read()


def _write_volume(path: str, data: bytes) -> None:
    _w.files.upload(path, io.BytesIO(data), overwrite=True)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ─────────────────────────────────────────────────── SharePoint sweep ──

def sweep_sharepoint() -> int:
    """Discover new/changed SharePoint files and register them as pending documents.

    Dedupe is by SHA-256: identical bytes already stored are skipped (recorded as a
    duplicate reference, not re-downloaded through OCR). Idempotent — safe to re-run.
    """
    sources = query(
        f"SELECT source_id, config FROM {config.SOURCES} "
        f"WHERE kind = 'sharepoint' AND enabled = true"
    )
    registered = 0
    for src in sources:
        cfg = json.loads(src.get("config") or "{}")
        folder = cfg.get("root_folder", "")
        if not (cfg.get("site") or cfg.get("site_path")):
            continue
        _, drive_id = graph.resolve_site_drive(cfg)
        for item in graph.list_files(drive_id, folder, exts=(".pdf",)):
            # Skip if we already have this exact source item (by source_ref).
            ref = f"{drive_id}/{item['item_id']}"
            lake = lakebase.docs_enabled()
            if lake:
                if lakebase.find_by_source_ref(ref):
                    continue
            elif query(
                    f"SELECT doc_id FROM {config.DOCUMENTS} WHERE source_ref = {lit(ref)} LIMIT 1"):
                continue
            data = graph.download(drive_id, item["item_id"])
            sha = _sha256(data)
            dup = lakebase.find_by_sha(sha) if lake else query(
                f"SELECT doc_id, volume_path FROM {config.DOCUMENTS} WHERE content_sha256 = {lit(sha)} LIMIT 1")
            doc_id = "d_" + uuid.uuid4().hex
            vpath = f"{config.DOCS_VOLUME}/sharepoint/{src['source_id']}/{sha}.pdf"
            if not dup:
                _write_volume(vpath, data)
            else:
                vpath = dup[0]["volume_path"]
            if lake:
                lakebase.insert_document(
                    doc_id=doc_id, content_sha256=sha, volume_path=vpath,
                    original_filename=item["name"],
                    mime_type=item.get("mime") or "application/pdf", size_bytes=item.get("size", 0),
                    source_id=src["source_id"], source_ref=ref, classification_status="unclassified",
                    extraction_status="pending", verification_status="needs_review",
                    mirror_status="not_mirrored", attempt_count=0,
                    file_modified_at=item.get("modified"), created_by="sweep",
                )
            else:
                execute(
                    f"INSERT INTO {config.DOCUMENTS} "
                    f"(doc_id, content_sha256, volume_path, original_filename, mime_type, size_bytes, "
                    f" source_id, source_ref, classification_status, extraction_status, verification_status, "
                    f" mirror_status, attempt_count, file_modified_at, created_at, created_by, updated_at) "
                    f"VALUES ({lit(doc_id)}, {lit(sha)}, {lit(vpath)}, {lit(item['name'])}, "
                    f"{lit(item.get('mime') or 'application/pdf')}, {item.get('size', 0)}, "
                    f"{lit(src['source_id'])}, {lit(ref)}, 'unclassified', 'pending', 'needs_review', "
                    f"'not_mirrored', 0, {lit(item.get('modified'))}, current_timestamp(), 'sweep', "
                    f"current_timestamp())"
                )
            registered += 1
    _heartbeat("sweep", info=f"registered={registered}")
    return registered


# ──────────────────────────────────────────── delegated SharePoint sync ──

SYNC_LEASE_SECONDS = int(os.getenv("SP_SYNC_LEASE", "900"))


def sync_delegated() -> int:
    """Pull new/changed files for each armed auto-sync using its owner's delegated token.

    Watermark-based: only files with lastModifiedDateTime > last_synced_at are fetched,
    deduped by SHA-256, and registered as pending. Each sync is claimed with a lease so
    concurrent workers don't double-pull. A dead grant flips token_status → 'needs_reauth'
    (surfaced in the UI as a reconnect banner) rather than failing the pass.
    """
    if not sp.encryption_available():
        return 0
    candidates = query(
        f"SELECT id FROM {config.SHAREPOINT_SYNCS} WHERE token_status = 'ok' "
        f"AND (claim_expires_at IS NULL OR claim_expires_at <= current_timestamp()) "
        f"ORDER BY coalesce(last_synced_at, to_timestamp(0)) LIMIT 20"
    )
    imported = 0
    for c in candidates:
        sync_id = c["id"]
        # Guarded claim: only if still unclaimed do we take the lease.
        execute(
            f"UPDATE {config.SHAREPOINT_SYNCS} SET claimed_by = {lit(WORKER_ID)}, "
            f"claim_expires_at = current_timestamp() + INTERVAL {SYNC_LEASE_SECONDS} SECONDS "
            f"WHERE id = {lit(sync_id)} "
            f"AND (claim_expires_at IS NULL OR claim_expires_at <= current_timestamp())"
        )
        rows = query(
            f"SELECT id, source_id, site_id, site_name, drive_id, folder_id, business_unit, "
            f"department, document_type, user_email, refresh_token_enc, "
            f"unix_timestamp(last_synced_at) AS last_synced "
            f"FROM {config.SHAREPOINT_SYNCS} WHERE id = {lit(sync_id)} AND claimed_by = {lit(WORKER_ID)}"
        )
        if not rows:
            continue
        s = rows[0]
        watermark = _iso_from_epoch(s.get("last_synced"))
        try:
            imported += _sync_one(s, watermark)
            execute(
                f"UPDATE {config.SHAREPOINT_SYNCS} SET last_synced_at = current_timestamp(), "
                f"claimed_by = NULL, claim_expires_at = NULL, last_error = NULL "
                f"WHERE id = {lit(sync_id)}"
            )
        except sp.SPReauth as e:
            execute(
                f"UPDATE {config.SHAREPOINT_SYNCS} SET token_status = 'needs_reauth', "
                f"last_error = {lit(str(e)[:500])}, claimed_by = NULL, claim_expires_at = NULL "
                f"WHERE id = {lit(sync_id)}"
            )
            logger.warning("stage=sync sync_id=%s needs reauth: %s", sync_id, e)
        except Exception as exc:
            execute(
                f"UPDATE {config.SHAREPOINT_SYNCS} SET last_error = {lit(str(exc)[:500])}, "
                f"claimed_by = NULL, claim_expires_at = NULL WHERE id = {lit(sync_id)}"
            )
            logger.error("stage=sync sync_id=%s error: %s\n%s", sync_id, exc, traceback.format_exc())
    _heartbeat("sync", info=f"imported={imported}")
    return imported


def _sync_one(s: dict, watermark: str | None) -> int:
    """Refresh the sync owner's token, pull changed files, register them. Rotates the token."""
    access, new_refresh, expires_in = sp.refresh_access(sp.decrypt(s["refresh_token_enc"]))
    # Persist the rotated refresh token so the next tick doesn't fail on a spent grant.
    execute(
        f"UPDATE {config.SHAREPOINT_SYNCS} SET refresh_token_enc = {lit(sp.encrypt(new_refresh))} "
        f"WHERE id = {lit(s['id'])}"
    )
    # The sync target may be a whole library (folder_id NULL → root walk), a folder, or a
    # single file. Probe a set folder_id: if it resolves to a file, sync just that item.
    target = s.get("folder_id")
    if target:
        meta = sp.get_file_meta(access, s["drive_id"], target)  # None if it's a folder
        if meta is not None:
            files = [meta] if (not watermark or (meta.get("modified") or "") > watermark) else []
        else:
            files = sp.walk_files(access, s["drive_id"], target, modified_after=watermark)
    else:
        files = sp.walk_files(access, s["drive_id"], None, modified_after=watermark)
    n = 0
    for it in files:
        r = ingest.register_bytes(
            sp.download(access, s["drive_id"], it["id"]), it["name"], it.get("mime"),
            source_id=s["source_id"], source_ref=f"{s['drive_id']}/{it['id']}",
            created_by=s.get("user_email") or "sync", subdir=f"sharepoint/{s['source_id']}",
            business_unit=s.get("business_unit"), document_type=s.get("document_type"),
            department=s.get("department"), file_modified_at=it.get("modified"),
            sp_site_id=s.get("site_id"), sp_site_name=s.get("site_name"),
            sp_drive_id=s["drive_id"], sp_path=it.get("path"), sp_web_url=it.get("web_url"),
        )
        if r["status"] == "new":
            n += 1
    if files:
        print(f"  ⇊ sync {s['id']}: {n} new / {len(files)} changed")
    return n


# ──────────────────────────────────────────── queued SharePoint imports ──

IMPORT_LEASE_SECONDS = int(os.getenv("IMPORT_LEASE", "3600"))


def process_imports() -> int:
    """Fulfil user-queued SharePoint imports (see sharepoint.enqueue_import).

    Each request is claimed with a lease so a killed worker's work is re-claimed after
    expiry. Registration dedupes by SHA-256, so re-running a partially-done import is safe.
    Returns the number of documents newly registered across all requests handled.
    """
    if not sp.encryption_available():
        return 0
    candidates = query(
        f"SELECT id FROM {config.IMPORT_JOBS} WHERE status IN ('queued', 'processing') "
        f"AND (claim_expires_at IS NULL OR claim_expires_at <= current_timestamp()) "
        f"ORDER BY created_at LIMIT 5"
    )
    imported = 0
    for c in candidates:
        jid = c["id"]
        # Guarded claim: only take it if still unclaimed/expired.
        execute(
            f"UPDATE {config.IMPORT_JOBS} SET claimed_by = {lit(WORKER_ID)}, status = 'processing', "
            f"claim_expires_at = current_timestamp() + INTERVAL {IMPORT_LEASE_SECONDS} SECONDS, "
            f"updated_at = current_timestamp() WHERE id = {lit(jid)} "
            f"AND (claim_expires_at IS NULL OR claim_expires_at <= current_timestamp())"
        )
        rows = query(
            f"SELECT id, user_email, drive_id, selections, source_id, site_id, site_name, "
            f"business_unit, document_type, department FROM {config.IMPORT_JOBS} "
            f"WHERE id = {lit(jid)} AND claimed_by = {lit(WORKER_ID)}"
        )
        if not rows:
            continue
        try:
            imported += _run_import(rows[0])
            execute(
                f"UPDATE {config.IMPORT_JOBS} SET status = 'done', last_error = NULL, "
                f"claimed_by = NULL, claim_expires_at = NULL, updated_at = current_timestamp() "
                f"WHERE id = {lit(jid)}"
            )
        except sp.SPReauth as e:
            execute(
                f"UPDATE {config.IMPORT_JOBS} SET status = 'error', "
                f"last_error = {lit(('reconnect required: ' + str(e))[:500])}, "
                f"claimed_by = NULL, claim_expires_at = NULL, updated_at = current_timestamp() "
                f"WHERE id = {lit(jid)}"
            )
            logger.warning("stage=import job_id=%s needs reauth: %s", jid, e)
        except Exception as exc:
            execute(
                f"UPDATE {config.IMPORT_JOBS} SET status = 'error', last_error = {lit(str(exc)[:500])}, "
                f"claimed_by = NULL, claim_expires_at = NULL, updated_at = current_timestamp() "
                f"WHERE id = {lit(jid)}"
            )
            logger.error("stage=import job_id=%s error: %s\n%s", jid, exc, traceback.format_exc())
    _heartbeat("import", info=f"imported={imported}")
    return imported


def _run_import(j: dict) -> int:
    """Expand the selection to files, download + register each, updating progress counters."""
    email, drive_id = j["user_email"], j["drive_id"]
    selections = json.loads(j["selections"] or "[]")
    token = sp.access_token_for(email)

    items: list[dict] = []
    for sel in selections:
        if sel.get("is_folder"):
            items.extend(sp.walk_files(token, drive_id, sel["id"]))
        else:
            items.append(sel)
    execute(
        f"UPDATE {config.IMPORT_JOBS} SET total_files = {len(items)}, "
        f"updated_at = current_timestamp() WHERE id = {lit(j['id'])}"
    )

    new = dup = errs = 0
    for i, it in enumerate(items):
        try:
            r = sp.import_file(
                token, drive_id, it, created_by=email, source_id=j["source_id"],
                site_id=j.get("site_id"), site_name=j.get("site_name"),
                business_unit=j.get("business_unit"), document_type=j.get("document_type"),
                department=j.get("department"))
            if r["status"] == "new":
                new += 1
            else:
                dup += 1
        except Exception as exc:
            errs += 1
            logger.error("stage=import job_id=%s file=%r failed: %s\n%s",
                         j["id"], it.get("name"), exc, traceback.format_exc())
        # Checkpoint every few files: publish progress + extend the lease + refresh token.
        if (i + 1) % 5 == 0 or (i + 1) == len(items):
            execute(
                f"UPDATE {config.IMPORT_JOBS} SET imported = {new}, duplicates = {dup}, errors = {errs}, "
                f"claim_expires_at = current_timestamp() + INTERVAL {IMPORT_LEASE_SECONDS} SECONDS, "
                f"updated_at = current_timestamp() WHERE id = {lit(j['id'])}"
            )
            token = sp.access_token_for(email)  # re-fetch in case the access token expired
    print(f"  ⇊ import {j['id']}: {new} new / {dup} dup / {errs} err of {len(items)}")
    return new


def _iso_from_epoch(epoch) -> str | None:
    if not epoch:
        return None
    import datetime
    return datetime.datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ───────────────────────────────────────────────────────── claim/lease ──

def claim_batch(n: int) -> list[dict]:
    """Claim up to n pending, unleased/expired docs for this worker. Concurrency-safe."""
    if lakebase.docs_enabled():
        return lakebase.claim_batch(WORKER_ID, n, config.MAX_ATTEMPTS, config.CLAIM_LEASE_SECONDS)
    candidates = query(
        f"SELECT doc_id FROM {config.DOCUMENTS} WHERE extraction_status = 'pending' "
        f"AND classification_status = 'classified' "
        f"AND (next_attempt_at IS NULL OR next_attempt_at <= current_timestamp()) "
        f"AND (claim_expires_at IS NULL OR claim_expires_at <= current_timestamp()) "
        f"AND attempt_count < {config.MAX_ATTEMPTS} "
        f"ORDER BY created_at LIMIT {n}"
    )
    if not candidates:
        return []
    ids = ",".join(lit(c["doc_id"]) for c in candidates)
    # Guarded update: only rows still pending & unclaimed become ours. Delta serializes
    # writes, so racing workers conflict-and-retry; claimed_by = WORKER_ID disambiguates.
    execute(
        f"UPDATE {config.DOCUMENTS} SET claimed_by = {lit(WORKER_ID)}, "
        f"claim_expires_at = current_timestamp() + INTERVAL {config.CLAIM_LEASE_SECONDS} SECONDS, "
        f"extraction_status = 'processing', updated_at = current_timestamp() "
        f"WHERE doc_id IN ({ids}) AND extraction_status = 'pending' "
        f"AND (claim_expires_at IS NULL OR claim_expires_at <= current_timestamp())"
    )
    return query(
        f"SELECT doc_id, content_sha256, volume_path, original_filename, document_type, attempt_count "
        f"FROM {config.DOCUMENTS} WHERE claimed_by = {lit(WORKER_ID)} "
        f"AND extraction_status = 'processing'"
    )


def _extend_lease(doc_id: str) -> None:
    if lakebase.docs_enabled():
        lakebase.extend_lease(doc_id, WORKER_ID, config.CLAIM_LEASE_SECONDS)
        return
    execute(
        f"UPDATE {config.DOCUMENTS} SET "
        f"claim_expires_at = current_timestamp() + INTERVAL {config.CLAIM_LEASE_SECONDS} SECONDS, "
        f"updated_at = current_timestamp() WHERE doc_id = {lit(doc_id)} AND claimed_by = {lit(WORKER_ID)}"
    )


# ───────────────────────────────────────────────────── process one doc ──

def _find_twin(doc: dict) -> dict | None:
    """A byte-identical sibling this doc can copy finished work from, or None (SPEC §9)."""
    sha = doc.get("content_sha256")
    if not sha:
        return None
    sig_prefix = config.PROMPT_VERSION
    if lakebase.docs_enabled():
        return lakebase.find_processed_twin(doc["doc_id"], sha, doc.get("document_type"), sig_prefix)
    rows = query(
        f"SELECT doc_id, derived_pdf_path, text_source, page_count, extraction_sig, "
        f"verification_status, verified_by FROM {config.DOCUMENTS} "
        f"WHERE content_sha256 = {lit(sha)} AND doc_id <> {lit(doc['doc_id'])} "
        f"AND extraction_status = 'done' AND document_type <=> {lit(doc.get('document_type'))} "
        f"AND extraction_sig LIKE {lit(sig_prefix + ':%')} "
        f"ORDER BY (verification_status = 'verified') DESC, updated_at DESC LIMIT 1"
    )
    return rows[0] if rows else None


def _reuse_from_twin(doc: dict, twin: dict) -> None:
    """Copy the twin's text/derived-PDF/fields (and verified status) onto this doc — no OCR,
    no ai_query. The searchable PDF is a sha-keyed volume artifact already on disk, shared."""
    doc_id = doc["doc_id"]
    if lakebase.docs_enabled():
        lakebase.copy_from_twin(doc_id, twin)
    else:
        _copy_from_twin_warehouse(doc_id, twin)
    verified = twin.get("verification_status") == "verified"
    logger.info("doc_id=%s stage=process_doc reused from twin=%s%s",
                doc_id, twin["doc_id"], " (landed verified)" if verified else "")
    print(f"  ♻ {doc_id} reused from {twin['doc_id']}{' → verified' if verified else ''}")


def _copy_from_twin_warehouse(doc_id: str, twin: dict) -> None:
    twin_id = twin["doc_id"]
    execute(f"DELETE FROM {config.DOCUMENT_TEXT} WHERE doc_id = {lit(doc_id)}")
    execute(
        f"INSERT INTO {config.DOCUMENT_TEXT} (doc_id, page, text, updated_at) "
        f"SELECT {lit(doc_id)}, page, text, current_timestamp() "
        f"FROM {config.DOCUMENT_TEXT} WHERE doc_id = {lit(twin_id)}"
    )
    # Copy only field keys this doc lacks (never clobber preset/SharePoint metadata).
    execute(
        f"MERGE INTO {config.DOCUMENT_FIELDS} t "
        f"USING (SELECT {lit(doc_id)} AS doc_id, field_key, proposed_value, confirmed_value, "
        f"       confidence, source_provenance FROM {config.DOCUMENT_FIELDS} "
        f"       WHERE doc_id = {lit(twin_id)}) s "
        f"ON t.doc_id = s.doc_id AND t.field_key = s.field_key "
        f"WHEN NOT MATCHED THEN INSERT (doc_id, field_key, proposed_value, confirmed_value, "
        f"  confidence, source_provenance, updated_at) "
        f"VALUES (s.doc_id, s.field_key, s.proposed_value, s.confirmed_value, s.confidence, "
        f"  s.source_provenance, current_timestamp())"
    )
    sets = (
        f"extraction_status = 'done', derived_pdf_path = {lit(twin.get('derived_pdf_path'))}, "
        f"text_source = {lit(twin.get('text_source'))}, page_count = {int(twin.get('page_count') or 0)}, "
        f"extraction_sig = {lit(twin.get('extraction_sig'))}, error_message = NULL, "
        f"claimed_by = NULL, claim_expires_at = NULL, updated_at = current_timestamp()"
    )
    if twin.get("verification_status") == "verified":
        sets += (
            f", verification_status = 'verified', verified_by = {lit(twin.get('verified_by'))}, "
            f"verified_at = current_timestamp(), mirror_status = 'not_mirrored'"
        )
    execute(f"UPDATE {config.DOCUMENTS} SET {sets} WHERE doc_id = {lit(doc_id)}")


def process_doc(doc: dict) -> None:
    doc_id = doc["doc_id"]
    try:
        _extend_lease(doc_id)
        # Free reuse: if a byte-identical sibling is already processed, copy its results
        # instead of re-paying OCR + extraction (SPEC §9).
        twin = _find_twin(doc)
        if twin:
            _reuse_from_twin(doc, twin)
            return
        data = _read_volume(doc["volume_path"])
        res = ocr.process(data, doc["original_filename"] or "file.pdf")

        # Derived searchable PDF (only when DI produced one).
        derived_path = None
        if res["searchable_pdf"]:
            derived_path = doc["volume_path"].rsplit(".", 1)[0] + ".searchable.pdf"
            _write_volume(derived_path, res["searchable_pdf"])

        # Full text for search/extraction.
        full_text = "\n".join(p["text"] for p in res["pages"])

        # Admin-schema field extraction (cached by hash+prompt+type).
        proposed = extract.extract_fields(doc.get("document_type"), full_text, doc["content_sha256"])

        _commit_success(doc_id, derived_path, res, proposed)
    except Exception as exc:  # bounded retry with backoff
        _commit_failure(doc, exc)


def _commit_success(doc_id, derived_path, res, proposed) -> None:
    lake = lakebase.docs_enabled()
    sig = f"{config.PROMPT_VERSION}:{res['text_source']}"
    if lake:
        # Text: replace this doc's rows atomically (delete-then-insert is idempotent per doc).
        lakebase.replace_text(doc_id, res["pages"])
        # Proposed fields: upsert so we never clobber a human's confirmed_value (one round-trip).
        lakebase.upsert_proposed_fields(doc_id, {
            key: (None if val is None else (val if isinstance(val, str) else json.dumps(val)))
            for key, val in (proposed or {}).items()
        })
        lakebase.commit_extraction_done(
            doc_id, derived_path, res["text_source"], int(res["page_count"]), sig)
        print(f"  ✓ {doc_id} ({res['text_source']}, {res['page_count']}p, {len(proposed or {})} fields)")
        return
    # Text: replace this doc's rows atomically (delete-then-insert is idempotent per doc).
    execute(f"DELETE FROM {config.DOCUMENT_TEXT} WHERE doc_id = {lit(doc_id)}")
    for p in res["pages"]:
        if (p.get("text") or "").strip():
            execute(
                f"INSERT INTO {config.DOCUMENT_TEXT} (doc_id, page, text, updated_at) "
                f"VALUES ({lit(doc_id)}, {int(p['page'])}, {lit(p['text'])}, current_timestamp())"
            )
    # Proposed fields: MERGE so we never clobber a human's confirmed_value.
    for key, val in (proposed or {}).items():
        sval = None if val is None else (val if isinstance(val, str) else json.dumps(val))
        execute(
            f"MERGE INTO {config.DOCUMENT_FIELDS} t "
            f"USING (SELECT {lit(doc_id)} AS doc_id, {lit(key)} AS field_key) s "
            f"ON t.doc_id = s.doc_id AND t.field_key = s.field_key "
            f"WHEN MATCHED THEN UPDATE SET proposed_value = {lit(sval)}, "
            f"  source_provenance = coalesce(t.source_provenance, 'ai'), updated_at = current_timestamp() "
            f"WHEN NOT MATCHED THEN INSERT (doc_id, field_key, proposed_value, source_provenance, updated_at) "
            f"VALUES ({lit(doc_id)}, {lit(key)}, {lit(sval)}, 'ai', current_timestamp())"
        )
    execute(
        f"UPDATE {config.DOCUMENTS} SET extraction_status = 'done', "
        f"derived_pdf_path = {lit(derived_path)}, text_source = {lit(res['text_source'])}, "
        f"page_count = {int(res['page_count'])}, extraction_sig = {lit(sig)}, "
        f"error_message = NULL, claimed_by = NULL, claim_expires_at = NULL, "
        f"updated_at = current_timestamp() WHERE doc_id = {lit(doc_id)}"
    )
    print(f"  ✓ {doc_id} ({res['text_source']}, {res['page_count']}p, {len(proposed or {})} fields)")


def _commit_failure(doc, exc) -> None:
    doc_id = doc["doc_id"]
    attempts = (doc.get("attempt_count") or 0) + 1
    msg = f"{type(exc).__name__}: {exc}"[:1000]
    lake = lakebase.docs_enabled()
    if attempts >= config.MAX_ATTEMPTS:
        if lake:
            lakebase.commit_extraction_failed(doc_id, attempts, msg)
        else:
            execute(
                f"UPDATE {config.DOCUMENTS} SET extraction_status = 'failed', attempt_count = {attempts}, "
                f"error_message = {lit(msg)}, claimed_by = NULL, claim_expires_at = NULL, "
                f"updated_at = current_timestamp() WHERE doc_id = {lit(doc_id)}"
            )
        logger.error("doc_id=%s stage=process_doc FAILED permanently after %d attempts: %s\n%s",
                     doc_id, attempts, msg, traceback.format_exc())
    else:
        backoff = BACKOFF_BASE * (2 ** (attempts - 1))
        if lake:
            lakebase.commit_extraction_retry(doc_id, attempts, msg, backoff)
        else:
            execute(
                f"UPDATE {config.DOCUMENTS} SET extraction_status = 'pending', attempt_count = {attempts}, "
                f"error_message = {lit(msg)}, claimed_by = NULL, claim_expires_at = NULL, "
                f"next_attempt_at = current_timestamp() + INTERVAL {backoff} SECONDS, "
                f"updated_at = current_timestamp() WHERE doc_id = {lit(doc_id)}"
            )
        logger.warning("doc_id=%s stage=process_doc retry %d/%d in %ds: %s\n%s",
                       doc_id, attempts, config.MAX_ATTEMPTS, backoff, msg, traceback.format_exc())


# ─────────────────────────────────────────────────────────── heartbeat ──

def _heartbeat(job_name: str, info: str = "") -> None:
    execute(
        f"MERGE INTO {config.JOB_STATE} t USING (SELECT {lit(job_name)} AS job_name) s "
        f"ON t.job_name = s.job_name "
        f"WHEN MATCHED THEN UPDATE SET worker_id = {lit(WORKER_ID)}, heartbeat_at = current_timestamp(), info = {lit(info)} "
        f"WHEN NOT MATCHED THEN INSERT (job_name, worker_id, heartbeat_at, info) "
        f"VALUES ({lit(job_name)}, {lit(WORKER_ID)}, current_timestamp(), {lit(info)})"
    )


# ─────────────────────────────────────────────────────────────── main ──

def run_once(do_sweep: bool = False, do_sync: bool = False) -> int:
    if do_sweep:
        try:
            n = sweep_sharepoint()
            if n:
                print(f"Sweep registered {n} new document(s).")
        except Exception as exc:
            logger.error("stage=sweep failed (continuing to processing): %s\n%s",
                         exc, traceback.format_exc())
    if do_sync:
        try:
            n = sync_delegated()
            if n:
                print(f"Delegated sync registered {n} new document(s).")
        except Exception as exc:
            logger.error("stage=sync failed (continuing to processing): %s\n%s",
                         exc, traceback.format_exc())
    # Queued user imports run every pass (cheap when the queue is empty) so a late-arriving
    # request during a drain still gets picked up rather than waiting for the next run.
    try:
        n = process_imports()
        if n:
            print(f"Imports registered {n} new document(s).")
    except Exception as exc:
        logger.error("stage=import failed (continuing to processing): %s\n%s",
                     exc, traceback.format_exc())
    docs = claim_batch(BATCH)
    if docs:
        print(f"[{WORKER_ID}] claimed {len(docs)} doc(s)")
    for doc in docs:
        process_doc(doc)
    _heartbeat("processing", info=f"processed={len(docs)}")
    return len(docs)


def drain(do_sweep: bool = False, do_sync: bool = False, max_passes: int = 10000) -> int:
    """Discovery once, then process the pending backlog until empty, then return.

    This is the cost-optimal mode for a *scheduled* job: the cluster wakes, clears the
    queue, and terminates — no idle compute between triggers. Discovery (sweep/sync) runs
    only on the first pass so it isn't repeated per batch.
    """
    total = first = 0
    while first < max_passes:
        n = run_once(do_sweep=(do_sweep and first == 0), do_sync=(do_sync and first == 0))
        first += 1
        total += n
        if n == 0:
            break
    print(f"Drain complete: processed {total} doc(s) across {first} pass(es).")
    return total


def check_lakebase() -> int:
    """Probe Lakebase connectivity from the job compute (non-destructive SELECT 1) and log
    the result. Returns 0 on success, 1 otherwise. Used to validate the job's own Lakebase
    credentials/egress BEFORE the documents cutover flag (USE_LAKEBASE_DOCUMENTS) is flipped,
    since with the flag off the job never otherwise touches Postgres."""
    if not lakebase.enabled():
        logger.warning("lakebase check: not enabled (PGHOST unset or psycopg2 missing) — skipping")
        return 1
    try:
        rows = lakebase.pg_query("SELECT 1 AS ok, current_user, current_database()")
        logger.info(f"lakebase check OK: {rows}")
        return 0
    except Exception as e:
        logger.error(f"lakebase check FAILED: {e!r}")
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="run continuously (near-real-time)")
    ap.add_argument("--drain", action="store_true", help="clear the backlog then exit (scheduled mode)")
    ap.add_argument("--once", action="store_true", help="single batch then exit")
    ap.add_argument("--sweep", action="store_true", help="include app-only SharePoint sweep")
    ap.add_argument("--sync", action="store_true", help="include delegated auto-sync")
    ap.add_argument("--check-lakebase", action="store_true",
                    help="probe Lakebase connectivity (SELECT 1) then exit — pre-cutover validation")
    ap.add_argument("--idle-sleep", type=int, default=30, help="seconds to sleep when idle in loop mode")
    args = ap.parse_args()

    print(f"Document Hub processing worker {WORKER_ID}")
    if args.check_lakebase:
        # Return cleanly on success (a bare sys.exit(0) raises SystemExit, which the
        # Databricks spark_python_task executor flags as a workload error); fail loudly only
        # when the probe fails so the task result reflects real connectivity.
        if check_lakebase() != 0:
            raise SystemExit(1)
        return
    # Passive reachability signal on every run once Lakebase creds are wired (flag-independent).
    if lakebase.enabled():
        check_lakebase()
    if args.loop:
        last_sync = 0.0
        while True:
            # Delegated sync is comparatively expensive; run it on its own interval.
            do_sync = args.sync and (time.time() - last_sync) >= config.SP_SYNC_INTERVAL
            if do_sync:
                last_sync = time.time()
            n = run_once(do_sweep=args.sweep, do_sync=do_sync)
            if n == 0:
                time.sleep(args.idle_sleep)
    elif args.drain:
        drain(do_sweep=args.sweep, do_sync=args.sync)
    else:
        run_once(do_sweep=args.sweep, do_sync=args.sync)


if __name__ == "__main__":
    main()
