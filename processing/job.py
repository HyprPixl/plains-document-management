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
import os
import socket
import time
import traceback
import uuid

from databricks.sdk import WorkspaceClient

import config
import ingest
import sharepoint as sp
from db import query, execute, lit
from . import graph, ocr, extract

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
            existing = query(
                f"SELECT doc_id FROM {config.DOCUMENTS} WHERE source_ref = {lit(ref)} LIMIT 1")
            if existing:
                continue
            data = graph.download(drive_id, item["item_id"])
            sha = _sha256(data)
            dup = query(
                f"SELECT doc_id FROM {config.DOCUMENTS} WHERE content_sha256 = {lit(sha)} LIMIT 1")
            doc_id = "d_" + uuid.uuid4().hex
            vpath = f"{config.DOCS_VOLUME}/sharepoint/{src['source_id']}/{sha}.pdf"
            if not dup:
                _write_volume(vpath, data)
            else:
                vpath = query(
                    f"SELECT volume_path FROM {config.DOCUMENTS} WHERE content_sha256 = {lit(sha)} LIMIT 1"
                )[0]["volume_path"]
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
            f"SELECT id, source_id, drive_id, folder_id, business_unit, department, document_type, "
            f"user_email, refresh_token_enc, unix_timestamp(last_synced_at) AS last_synced "
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
            print(f"  ⚠ sync {sync_id} needs reauth: {e}")
        except Exception as exc:
            execute(
                f"UPDATE {config.SHAREPOINT_SYNCS} SET last_error = {lit(str(exc)[:500])}, "
                f"claimed_by = NULL, claim_expires_at = NULL WHERE id = {lit(sync_id)}"
            )
            print(f"  ✗ sync {sync_id} error: {exc}")
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
    files = sp.walk_files(access, s["drive_id"], s.get("folder_id"), modified_after=watermark)
    n = 0
    for it in files:
        r = ingest.register_bytes(
            sp.download(access, s["drive_id"], it["id"]), it["name"], it.get("mime"),
            source_id=s["source_id"], source_ref=f"{s['drive_id']}/{it['id']}",
            created_by=s.get("user_email") or "sync", subdir=f"sharepoint/{s['source_id']}",
            business_unit=s.get("business_unit"), document_type=s.get("document_type"),
            department=s.get("department"), file_modified_at=it.get("modified"),
        )
        if r["status"] == "new":
            n += 1
    if files:
        print(f"  ⇊ sync {s['id']}: {n} new / {len(files)} changed")
    return n


def _iso_from_epoch(epoch) -> str | None:
    if not epoch:
        return None
    import datetime
    return datetime.datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ───────────────────────────────────────────────────────── claim/lease ──

def claim_batch(n: int) -> list[dict]:
    """Claim up to n pending, unleased/expired docs for this worker. Concurrency-safe."""
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
    execute(
        f"UPDATE {config.DOCUMENTS} SET "
        f"claim_expires_at = current_timestamp() + INTERVAL {config.CLAIM_LEASE_SECONDS} SECONDS, "
        f"updated_at = current_timestamp() WHERE doc_id = {lit(doc_id)} AND claimed_by = {lit(WORKER_ID)}"
    )


# ───────────────────────────────────────────────────── process one doc ──

def process_doc(doc: dict) -> None:
    doc_id = doc["doc_id"]
    try:
        _extend_lease(doc_id)
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
    sig = f"{config.PROMPT_VERSION}:{res['text_source']}"
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
    traceback.print_exc()
    if attempts >= config.MAX_ATTEMPTS:
        execute(
            f"UPDATE {config.DOCUMENTS} SET extraction_status = 'failed', attempt_count = {attempts}, "
            f"error_message = {lit(msg)}, claimed_by = NULL, claim_expires_at = NULL, "
            f"updated_at = current_timestamp() WHERE doc_id = {lit(doc_id)}"
        )
        print(f"  ✗ {doc_id} FAILED permanently after {attempts} attempts: {msg}")
    else:
        backoff = BACKOFF_BASE * (2 ** (attempts - 1))
        execute(
            f"UPDATE {config.DOCUMENTS} SET extraction_status = 'pending', attempt_count = {attempts}, "
            f"error_message = {lit(msg)}, claimed_by = NULL, claim_expires_at = NULL, "
            f"next_attempt_at = current_timestamp() + INTERVAL {backoff} SECONDS, "
            f"updated_at = current_timestamp() WHERE doc_id = {lit(doc_id)}"
        )
        print(f"  ↺ {doc_id} retry {attempts}/{config.MAX_ATTEMPTS} in {backoff}s: {msg}")


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
        except Exception:
            print("Sweep failed (continuing to processing):")
            traceback.print_exc()
    if do_sync:
        try:
            n = sync_delegated()
            if n:
                print(f"Delegated sync registered {n} new document(s).")
        except Exception:
            print("Delegated sync failed (continuing to processing):")
            traceback.print_exc()
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="run continuously (near-real-time)")
    ap.add_argument("--drain", action="store_true", help="clear the backlog then exit (scheduled mode)")
    ap.add_argument("--once", action="store_true", help="single batch then exit")
    ap.add_argument("--sweep", action="store_true", help="include app-only SharePoint sweep")
    ap.add_argument("--sync", action="store_true", help="include delegated auto-sync")
    ap.add_argument("--idle-sleep", type=int, default=30, help="seconds to sleep when idle in loop mode")
    args = ap.parse_args()

    print(f"Document Hub processing worker {WORKER_ID}")
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
