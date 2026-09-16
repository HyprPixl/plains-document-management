"""Document Hub — Flask backend (Databricks App).

Two surfaces: Manage (classify/extract/verify) and Explore (search/view/download).
Heavy processing (OCR/render/extraction) is handed off to the Databricks job in
processing/ — this web app only enqueues work and reads results from Delta.
"""
import hashlib
import io
import json
import os
import re
import uuid

from flask import Flask, Response, jsonify, request, send_file, render_template
from databricks.sdk import WorkspaceClient

import config
import ingest
import sharepoint as sp
from db import query, execute, lit

app = Flask(__name__)
_w = WorkspaceClient()

# ─────────────────────────────────────────────────────────── auth / permissions ──

def current_user() -> str:
    for h in ("X-Forwarded-Email", "X-Forwarded-Preferred-Username", "X-Forwarded-User"):
        v = request.headers.get(h)
        if v:
            return v.lower().strip()
    return os.getenv("DEV_USER", "caleb.fedyshen@plains.com").lower().strip()


def get_perms(email: str):
    """Return (is_admin, is_full, allowed_sites:list[str]).

    Access is scoped to the SharePoint *sites* a user can reach (site-level mirror). FULL /
    ADMIN see everything; otherwise a user sees docs whose sp_site_id is in allowed_sites.
    """
    rows = query(
        f"SELECT access_type, allowed_site FROM {config.PERMISSIONS} "
        f"WHERE lower(email) = {lit(email)}"
    )
    if not rows:
        return (False, False, [])
    types = {(r["access_type"] or "").upper() for r in rows}
    is_admin = "ADMIN" in types
    is_full = is_admin or "FULL" in types
    allowed = [r["allowed_site"] for r in rows if r.get("allowed_site")]
    return (is_admin, is_full, allowed)


def perms_where(email: str, col: str = "sp_site_id") -> str:
    """SQL predicate enforcing site-scoped access. Returns '' for full access."""
    is_admin, is_full, allowed = get_perms(email)
    if is_full:
        return ""
    if not allowed:
        return " AND 1=0 "  # no permissions → see nothing
    vals = ",".join(lit(b) for b in allowed)
    return f" AND {col} IN ({vals}) "


# ─────────────────────────────────────────────────────────────────────── pages ──

@app.route("/")
@app.route("/manage")
@app.route("/manage/<path:_p>")
@app.route("/explore")
@app.route("/explore/<path:_p>")
def index(_p=None):
    return render_template("index.html")


@app.get("/api/me")
def api_me():
    email = current_user()
    is_admin, is_full, allowed = get_perms(email)
    return jsonify(email=email, is_admin=is_admin, is_full=is_full, allowed_sites=allowed)


# ────────────────────────────────────────────────────────────────── taxonomy ──

@app.get("/api/taxonomy")
def api_taxonomy():
    rows = query(
        f"SELECT category, value, label, business_unit, sort_order FROM {config.TAXONOMY} "
        f"WHERE active = true ORDER BY category, sort_order"
    )
    out = {"department": [], "document_type": []}
    for r in rows:
        out.setdefault(r["category"], []).append(
            {"value": r["value"], "label": r["label"], "business_unit": r.get("business_unit")}
        )
    return jsonify(out)


@app.get("/api/field-defs")
def api_field_defs():
    doc_type = request.args.get("document_type")
    where = "active = true AND (applies_to = 'common'"
    if doc_type:
        where += f" OR applies_to = {lit(doc_type)}"
    where += ")"
    rows = query(
        f"SELECT field_key, label, data_type, applies_to, picklist_source, "
        f"extraction_prompt_hint, required_for_verify, sort_order FROM {config.FIELD_DEFS} WHERE {where} "
        f"ORDER BY (applies_to = 'common') DESC, sort_order"
    )
    for r in rows:
        if r.get("picklist_source") and "|" in str(r["picklist_source"]):
            r["options"] = str(r["picklist_source"]).split("|")
    return jsonify(rows)


# ──────────────────────────────────────────────── field-def management (admin) ──

@app.get("/api/field-defs/all")
def api_field_defs_all():
    """Every active field def, grouped by what it applies to — for the Fields admin screen."""
    rows = query(
        f"SELECT field_key, label, data_type, applies_to, picklist_source, "
        f"extraction_prompt_hint, required_for_verify, sort_order FROM {config.FIELD_DEFS} "
        f"WHERE active = true ORDER BY (applies_to = 'common') DESC, applies_to, sort_order"
    )
    doc_types = [r["value"] for r in query(
        f"SELECT value FROM {config.TAXONOMY} WHERE category = 'document_type' AND active = true "
        f"ORDER BY sort_order")]
    return jsonify(fields=rows, doc_types=doc_types)


def _require_admin(email):
    is_admin, _, _ = get_perms(email)
    return is_admin


@app.post("/api/field-defs")
def api_field_def_create():
    email = current_user()
    if not _require_admin(email):
        return jsonify(error="forbidden"), 403
    b = request.get_json(force=True)
    key = (b.get("field_key") or "").strip()
    label = (b.get("label") or "").strip()
    applies_to = (b.get("applies_to") or "common").strip()
    if not key or not label:
        return jsonify(error="field_key and label required"), 400
    if not re.fullmatch(r"[a-z0-9_]+", key):
        return jsonify(error="field_key must be lowercase letters, numbers, and underscores"), 400
    exists = query(f"SELECT 1 FROM {config.FIELD_DEFS} WHERE field_key = {lit(key)} AND active = true LIMIT 1")
    if exists:
        return jsonify(error="a field with that key already exists"), 409
    order = b.get("sort_order")
    if order is None:
        mx = query(f"SELECT max(sort_order) AS m FROM {config.FIELD_DEFS} "
                   f"WHERE applies_to = {lit(applies_to)} AND active = true")
        order = int((mx[0].get("m") or 0)) + 1 if mx else 1
    execute(
        f"INSERT INTO {config.FIELD_DEFS} (field_key, label, data_type, applies_to, picklist_source, "
        f"extraction_prompt_hint, required_for_verify, sort_order, active, created_by, updated_at) VALUES ("
        f"{lit(key)}, {lit(label)}, {lit(b.get('data_type') or 'text')}, {lit(applies_to)}, "
        f"{lit(b.get('picklist_source'))}, {lit(b.get('extraction_prompt_hint'))}, "
        f"{lit(bool(b.get('required_for_verify')))}, {int(order)}, true, {lit(email)}, current_timestamp())"
    )
    _audit(email, "field_def_create", key, b)
    return jsonify(ok=True, field_key=key)


@app.put("/api/field-defs/<field_key>")
def api_field_def_update(field_key):
    email = current_user()
    if not _require_admin(email):
        return jsonify(error="forbidden"), 403
    b = request.get_json(force=True)
    sets = ["updated_at = current_timestamp()"]
    for col in ("label", "data_type", "applies_to", "picklist_source", "extraction_prompt_hint"):
        if col in b:
            sets.append(f"{col} = {lit(b[col])}")
    if "required_for_verify" in b:
        sets.append(f"required_for_verify = {lit(bool(b['required_for_verify']))}")
    if "sort_order" in b and b["sort_order"] is not None:
        sets.append(f"sort_order = {int(b['sort_order'])}")
    execute(f"UPDATE {config.FIELD_DEFS} SET {', '.join(sets)} WHERE field_key = {lit(field_key)}")
    _audit(email, "field_def_update", field_key, b)
    return jsonify(ok=True)


@app.delete("/api/field-defs/<field_key>")
def api_field_def_delete(field_key):
    email = current_user()
    if not _require_admin(email):
        return jsonify(error="forbidden"), 403
    execute(f"UPDATE {config.FIELD_DEFS} SET active = false, updated_at = current_timestamp() "
            f"WHERE field_key = {lit(field_key)}")
    _audit(email, "field_def_delete", field_key, None)
    return jsonify(ok=True)


# ──────────────────────────────────────────────────────────── upload + dedup ──

@app.post("/api/upload")
def api_upload():
    """Hash-first upload. Returns per-file new/duplicate; writes new files to the volume."""
    email = current_user()
    batch_id = "b_" + uuid.uuid4().hex[:12]
    results = []
    for f in request.files.getlist("files"):
        r = ingest.register_bytes(
            f.read(), f.filename, f.mimetype,
            source_id="upload", source_ref="upload", created_by=email,
            subdir="uploads", batch_id=batch_id,
        )
        results.append(r)
    return jsonify(batch_id=batch_id, results=results)


# ─────────────────────────────────────────────────────────────── documents ──

@app.get("/api/documents")
def api_documents():
    email = current_user()
    status = request.args.get("verification_status")
    cstatus = request.args.get("classification_status")
    where = "1=1" + perms_where(email)
    if status:
        # A doc only enters the review pipeline once it's classified; unclassified docs stay
        # in the classification queue even though they carry a default needs_review status.
        where += f" AND verification_status = {lit(status)} AND classification_status = 'classified'"
    if cstatus:
        where += f" AND classification_status = {lit(cstatus)}"
    rows = query(
        f"SELECT doc_id, original_filename, document_type, department, sp_site_name, sp_path, "
        f"sp_web_url, mime_type, derived_pdf_path, "
        f"classification_status, extraction_status, verification_status, mirror_status, "
        f"source_id, batch_id, created_at FROM {config.DOCUMENTS} WHERE {where} "
        f"ORDER BY created_at DESC LIMIT 500"
    )
    return jsonify(rows)


@app.get("/api/stats")
def api_stats():
    email = current_user()
    where = "1=1" + perms_where(email)
    rows = query(
        f"SELECT verification_status AS s, count(*) AS n FROM {config.DOCUMENTS} "
        f"WHERE {where} AND classification_status = 'classified' GROUP BY verification_status"
    )
    unclassified = query(
        f"SELECT count(*) AS n FROM {config.DOCUMENTS} "
        f"WHERE classification_status = 'unclassified' {perms_where(email)}"
    )
    return jsonify(by_status={r["s"]: int(r["n"]) for r in rows},
                   unclassified=int(unclassified[0]["n"]) if unclassified else 0)


@app.post("/api/documents/classify")
def api_classify():
    """Bulk-apply preset categories to doc ids."""
    email = current_user()
    body = request.get_json(force=True)
    ids = body.get("doc_ids", [])
    dt = body.get("document_type")
    dept = body.get("department")
    if not ids:
        return jsonify(error="no doc_ids"), 400
    id_list = ",".join(lit(i) for i in ids)
    sets = [f"classification_status = 'classified'", "updated_at = current_timestamp()"]
    if dt is not None:
        sets.append(f"document_type = {lit(dt)}")
    if dept is not None:
        sets.append(f"department = {lit(dept)}")
    execute(f"UPDATE {config.DOCUMENTS} SET {', '.join(sets)} WHERE doc_id IN ({id_list})")
    _audit(email, "classify", f"{len(ids)} docs", body)
    return jsonify(updated=len(ids))


@app.post("/api/documents/enqueue")
def api_enqueue():
    """Mark classified docs pending so the processing job picks them up (idempotent)."""
    email = current_user()
    ids = request.get_json(force=True).get("doc_ids", [])
    if not ids:
        return jsonify(error="no doc_ids"), 400
    id_list = ",".join(lit(i) for i in ids)
    execute(
        f"UPDATE {config.DOCUMENTS} SET extraction_status = 'pending', "
        f"next_attempt_at = NULL, updated_at = current_timestamp() "
        f"WHERE doc_id IN ({id_list}) AND classification_status = 'classified'"
    )
    _trigger_processing_run()  # kick the job now so extraction doesn't wait for the schedule
    _audit(email, "enqueue", f"{len(ids)} docs", None)
    return jsonify(enqueued=len(ids))


# ───────────────────────────────────────────────────────────────────── tags ──

@app.get("/api/tags")
def api_tags():
    """Distinct tags across the corpus (facet / autocomplete)."""
    rows = query(
        f"SELECT tag, count(*) AS n FROM {config.DOCUMENT_TAGS} "
        f"GROUP BY tag ORDER BY n DESC, tag LIMIT 500"
    )
    return jsonify(rows)


@app.post("/api/documents/<doc_id>/tags")
def api_add_tag(doc_id):
    email = current_user()
    tag = (request.get_json(force=True).get("tag") or "").strip()
    if not tag:
        return jsonify(error="tag required"), 400
    # Idempotent: only insert if the (doc, tag) pair isn't already present.
    exists = query(
        f"SELECT 1 FROM {config.DOCUMENT_TAGS} WHERE doc_id = {lit(doc_id)} AND tag = {lit(tag)} LIMIT 1")
    if not exists:
        execute(
            f"INSERT INTO {config.DOCUMENT_TAGS} (doc_id, tag, created_by, created_at) "
            f"VALUES ({lit(doc_id)}, {lit(tag)}, {lit(email)}, current_timestamp())")
        _audit(email, "tag_add", doc_id, {"tag": tag})
    return jsonify(ok=True, tag=tag)


@app.delete("/api/documents/<doc_id>/tags/<path:tag>")
def api_remove_tag(doc_id, tag):
    email = current_user()
    execute(
        f"DELETE FROM {config.DOCUMENT_TAGS} WHERE doc_id = {lit(doc_id)} AND tag = {lit(tag)}")
    _audit(email, "tag_remove", doc_id, {"tag": tag})
    return jsonify(ok=True)


@app.get("/api/documents/<doc_id>")
def api_document(doc_id):
    docs = query(f"SELECT * FROM {config.DOCUMENTS} WHERE doc_id = {lit(doc_id)}")
    if not docs:
        return jsonify(error="not found"), 404
    doc = docs[0]
    fields = query(
        f"SELECT field_key, proposed_value, confirmed_value, source_provenance, confidence "
        f"FROM {config.DOCUMENT_FIELDS} WHERE doc_id = {lit(doc_id)}"
    )
    defs = query(
        f"SELECT field_key, label, data_type, picklist_source, required_for_verify, applies_to, sort_order "
        f"FROM {config.FIELD_DEFS} WHERE active = true AND "
        f"(applies_to = 'common' OR applies_to = {lit(doc.get('document_type'))}) "
        f"ORDER BY (applies_to='common') DESC, sort_order"
    )
    fmap = {f["field_key"]: f for f in fields}
    for d in defs:
        cur = fmap.get(d["field_key"], {})
        d["proposed_value"] = cur.get("proposed_value")
        d["confirmed_value"] = cur.get("confirmed_value")
        d["source_provenance"] = cur.get("source_provenance")
        if d.get("picklist_source") and "|" in str(d["picklist_source"]):
            d["options"] = str(d["picklist_source"]).split("|")
    links = query(
        f"SELECT l.relationship, l.child_doc_id, l.parent_doc_id, "
        f"d.original_filename, d.document_type FROM {config.DOCUMENT_LINKS} l "
        f"JOIN {config.DOCUMENTS} d ON d.doc_id = "
        f"  CASE WHEN l.parent_doc_id = {lit(doc_id)} THEN l.child_doc_id ELSE l.parent_doc_id END "
        f"WHERE l.parent_doc_id = {lit(doc_id)} OR l.child_doc_id = {lit(doc_id)}"
    )
    tags = [r["tag"] for r in query(
        f"SELECT tag FROM {config.DOCUMENT_TAGS} WHERE doc_id = {lit(doc_id)} ORDER BY tag")]
    return jsonify(document=doc, fields=defs, links=links, tags=tags)


@app.post("/api/documents/<doc_id>/fields")
def api_save_fields(doc_id):
    """Upsert confirmed field values (human edits)."""
    email = current_user()
    values = request.get_json(force=True).get("values", {})
    for key, val in values.items():
        execute(
            f"MERGE INTO {config.DOCUMENT_FIELDS} t "
            f"USING (SELECT {lit(doc_id)} AS doc_id, {lit(key)} AS field_key) s "
            f"ON t.doc_id = s.doc_id AND t.field_key = s.field_key "
            f"WHEN MATCHED THEN UPDATE SET confirmed_value = {lit(val)}, "
            f"  source_provenance = 'human', updated_at = current_timestamp(), updated_by = {lit(email)} "
            f"WHEN NOT MATCHED THEN INSERT (doc_id, field_key, confirmed_value, source_provenance, "
            f"  updated_at, updated_by) VALUES ({lit(doc_id)}, {lit(key)}, {lit(val)}, 'human', "
            f"  current_timestamp(), {lit(email)})"
        )
    return jsonify(saved=len(values))


@app.post("/api/documents/<doc_id>/verify")
def api_verify(doc_id):
    """Verify a document. Enforces required fields + amendment parent link."""
    email = current_user()
    docs = query(f"SELECT document_type FROM {config.DOCUMENTS} WHERE doc_id = {lit(doc_id)}")
    if not docs:
        return jsonify(error="not found"), 404
    dtype = docs[0].get("document_type")
    # required fields present?
    req = query(
        f"SELECT fd.field_key FROM {config.FIELD_DEFS} fd "
        f"WHERE fd.active = true AND fd.required_for_verify = true AND "
        f"(fd.applies_to = 'common' OR fd.applies_to = {lit(dtype)})"
    )
    have = {r["field_key"]: r for r in query(
        f"SELECT field_key, confirmed_value FROM {config.DOCUMENT_FIELDS} "
        f"WHERE doc_id = {lit(doc_id)} AND confirmed_value IS NOT NULL AND confirmed_value <> ''")}
    missing = [r["field_key"] for r in req if r["field_key"] not in have]
    if missing:
        return jsonify(error="missing_required", fields=missing), 400
    # amendments require a parent contract link
    if dtype == "Amendment":
        pl = query(
            f"SELECT 1 FROM {config.DOCUMENT_LINKS} WHERE child_doc_id = {lit(doc_id)} "
            f"AND relationship = 'amendment_of' LIMIT 1")
        if not pl:
            return jsonify(error="amendment_needs_parent"), 400
    execute(
        f"UPDATE {config.DOCUMENTS} SET verification_status = 'verified', "
        f"verified_by = {lit(email)}, verified_at = current_timestamp(), "
        f"mirror_status = 'not_mirrored', updated_at = current_timestamp() "
        f"WHERE doc_id = {lit(doc_id)}"
    )
    _audit(email, "verify", doc_id, None)
    return jsonify(verified=True)


@app.post("/api/documents/<doc_id>/unverify")
def api_unverify(doc_id):
    email = current_user()
    execute(
        f"UPDATE {config.DOCUMENTS} SET verification_status = 'needs_review', "
        f"updated_at = current_timestamp() WHERE doc_id = {lit(doc_id)}"
    )
    _audit(email, "unverify", doc_id, None)
    return jsonify(unverified=True)


# ──────────────────────────────────────────────────────────────────── links ──

@app.post("/api/links")
def api_link():
    email = current_user()
    b = request.get_json(force=True)
    parent, child, rel = b.get("parent_doc_id"), b.get("child_doc_id"), b.get("relationship", "related")
    if not parent or not child or parent == child:
        return jsonify(error="bad link"), 400
    execute(
        f"MERGE INTO {config.DOCUMENT_LINKS} t "
        f"USING (SELECT {lit(parent)} AS p, {lit(child)} AS c, {lit(rel)} AS r) s "
        f"ON t.parent_doc_id = s.p AND t.child_doc_id = s.c AND t.relationship = s.r "
        f"WHEN NOT MATCHED THEN INSERT (parent_doc_id, child_doc_id, relationship, created_by, created_at) "
        f"VALUES (s.p, s.c, s.r, {lit(email)}, current_timestamp())"
    )
    return jsonify(linked=True)


# ─────────────────────────────────────────────────────────────────── explore ──

@app.get("/api/search")
def api_search():
    email = current_user()
    q = (request.args.get("q") or "").strip()
    dt = request.args.get("document_type")
    dept = request.args.get("department")
    path = request.args.get("path")  # prefix filter on the SharePoint path
    tag = request.args.get("tag")
    where = "verification_status = 'verified'" + perms_where(email, "d.sp_site_id")
    if dt:
        where += f" AND d.document_type = {lit(dt)}"
    if dept:
        where += f" AND d.department = {lit(dept)}"
    if path:
        where += f" AND lower(d.sp_path) LIKE {lit(path.lower() + '%')}"
    join_txt = join_tag = ""
    if q:
        ql = lit(f"%{q.lower()}%")
        join_txt = (
            f"LEFT JOIN (SELECT doc_id, concat_ws(' ', collect_list(text)) AS body "
            f"FROM {config.DOCUMENT_TEXT} GROUP BY doc_id) tx ON tx.doc_id = d.doc_id "
        )
        where += (
            f" AND (lower(d.original_filename) LIKE {ql} "
            f"OR lower(coalesce(d.sp_path, '')) LIKE {ql} "
            f"OR lower(coalesce(f_title.confirmed_value, f_title.proposed_value, '')) LIKE {ql} "
            f"OR lower(coalesce(f_sum.confirmed_value, f_sum.proposed_value, '')) LIKE {ql} "
            f"OR lower(coalesce(tx.body, '')) LIKE {ql})"
        )
    if tag:
        join_tag = f"JOIN {config.DOCUMENT_TAGS} tg ON tg.doc_id = d.doc_id AND tg.tag = {lit(tag)} "
    rows = query(
        f"SELECT d.doc_id, d.original_filename, d.document_type, d.department, "
        f"d.sp_site_name, d.sp_path, d.sp_web_url, d.mime_type, d.derived_pdf_path, "
        f"coalesce(f_title.confirmed_value, f_title.proposed_value) AS title, "
        f"coalesce(f_sum.confirmed_value, f_sum.proposed_value) AS summary, d.created_at "
        f"FROM {config.DOCUMENTS} d "
        f"LEFT JOIN {config.DOCUMENT_FIELDS} f_title ON f_title.doc_id = d.doc_id AND f_title.field_key = 'title' "
        f"LEFT JOIN {config.DOCUMENT_FIELDS} f_sum ON f_sum.doc_id = d.doc_id AND f_sum.field_key = 'summary' "
        f"{join_txt}{join_tag}"
        f"WHERE {where} ORDER BY d.created_at DESC LIMIT 200"
    )
    return jsonify(rows)


@app.get("/api/download")
def api_download():
    """Stream a file from the volume (searchable PDF preferred, else original)."""
    doc_id = request.args.get("doc_id")
    which = request.args.get("which", "derived")
    docs = query(
        f"SELECT volume_path, derived_pdf_path, original_filename, mime_type "
        f"FROM {config.DOCUMENTS} WHERE doc_id = {lit(doc_id)}")
    if not docs:
        return jsonify(error="not found"), 404
    d = docs[0]
    path = d.get("derived_pdf_path") if which == "derived" and d.get("derived_pdf_path") else d["volume_path"]
    resp = _w.files.download(path)
    data = resp.contents.read()
    inline = request.args.get("inline") == "1"
    return send_file(
        io.BytesIO(data),
        mimetype=d.get("mime_type") or "application/octet-stream",
        as_attachment=not inline,
        download_name=d.get("original_filename") or "document",
    )


# ──────────────────────────────────────────────── SharePoint (delegated OAuth) ──

def _can_import(email: str) -> bool:
    """Any authenticated user may connect and browse their *own* SharePoint — delegated
    access naturally scopes what they see, and connecting is how we learn their sites."""
    return bool(email)


@app.get("/api/sharepoint/status")
def sp_status():
    email = current_user()
    if not sp.configured():
        return jsonify(configured=False, connected=False, can_import=_can_import(email))
    return jsonify(configured=True, connected=sp.session_connected(email),
                   has_refresh=sp.has_refresh(email), can_import=_can_import(email))


@app.get("/api/sharepoint/login")
def sp_login():
    email = current_user()
    if not _can_import(email):
        return jsonify(error="forbidden"), 403
    if not sp.configured():
        return jsonify(error="not_configured"), 400
    ruri = sp.redirect_uri(request.headers)
    return_to = request.args.get("return_to", "/manage")
    state = sp.save_state(email, return_to, ruri)
    return jsonify(authorize_url=sp.authorize_url(state, ruri))


@app.get("/api/sharepoint/callback")
def sp_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    err = request.args.get("error")
    st = sp.pop_state(state) if state else None
    if err:
        return _sp_close_page(f"SharePoint sign-in failed: {err}")
    if not code or not st:
        return _sp_close_page("SharePoint sign-in expired or was invalid. Please try again.")
    try:
        tok = sp.exchange_code(code, st["redirect_uri"])
        sp.store_session(st["email"], tok, display_name=st["email"])
        try:
            sp.sync_user_sites(st["email"], tok.get("access_token"))  # mirror SP sites → perms
        except Exception:
            pass  # best-effort; the user is connected regardless
    except sp.SPReauth as e:
        return _sp_close_page(f"Could not complete sign-in: {e}")
    return _sp_close_page(None, return_to=st.get("return_to") or "/manage")


def _sp_close_page(error: str | None, return_to: str = "/manage"):
    if error:
        body = f"<p>{error}</p><p>You can close this window.</p>"
    else:
        body = "<p>Connected to SharePoint. This window will close…</p>"
    js = ("<script>try{if(window.opener){window.opener.postMessage("
          f"{json.dumps({'sharepoint': 'connected', 'error': error})},'*');}}"
          "catch(e){}window.close();</script>")
    return Response(f"<!doctype html><meta charset=utf-8><body>{body}{js}</body>",
                    mimetype="text/html")


@app.get("/api/sharepoint/sites")
def sp_sites():
    email = current_user()
    if not _can_import(email):
        return jsonify(error="forbidden"), 403
    try:
        token = sp.access_token_for(email)
        return jsonify(sites=sp.list_sites(token, request.args.get("q", "")))
    except sp.SPReauth:
        return jsonify(error="reauth"), 401


@app.get("/api/sharepoint/drives")
def sp_drives():
    email = current_user()
    if not _can_import(email):
        return jsonify(error="forbidden"), 403
    site_id = request.args.get("site_id")
    if not site_id:
        return jsonify(error="site_id required"), 400
    try:
        token = sp.access_token_for(email)
        return jsonify(drives=sp.list_drives(token, site_id))
    except sp.SPReauth:
        return jsonify(error="reauth"), 401


@app.get("/api/sharepoint/items")
def sp_items():
    email = current_user()
    if not _can_import(email):
        return jsonify(error="forbidden"), 403
    drive_id = request.args.get("drive_id")
    if not drive_id:
        return jsonify(error="drive_id required"), 400
    item_id = request.args.get("item_id") or None
    try:
        token = sp.access_token_for(email)
        return jsonify(items=sp.list_children(token, drive_id, item_id))
    except sp.SPReauth:
        return jsonify(error="reauth"), 401


@app.post("/api/sharepoint/import")
def sp_import():
    email = current_user()
    if not _can_import(email):
        return jsonify(error="forbidden"), 403
    b = request.get_json(force=True)
    drive_id = b.get("drive_id")
    selections = b.get("selections", [])
    if not drive_id or not selections:
        return jsonify(error="drive_id and selections required"), 400
    # Fast path: a small, files-only selection is cheap enough to import inline (a few Graph
    # downloads well under the 120s request budget), so the user sees docs immediately instead
    # of waiting on the processing job's cold start. Folders (recursive) and big batches queue.
    files_only = all(not s.get("is_folder") for s in selections)
    if files_only and len(selections) <= 8:
        try:
            r = sp.import_selection(
                email, drive_id, selections, source_id=b.get("source_id", "sp_import"),
                site_id=b.get("site_id"), site_name=b.get("site_name"),
                document_type=b.get("document_type"), department=b.get("department"))
        except sp.SPReauth:
            return jsonify(error="reauth"), 401
        _audit(email, "sp_import", drive_id, {"inline": True, "selected": len(selections), **r})
        return jsonify(queued=False, imported=r["imported"], duplicates=r["duplicates"])
    try:
        req_id = sp.enqueue_import(
            email, drive_id, selections, source_id=b.get("source_id", "sp_import"),
            site_id=b.get("site_id"), site_name=b.get("site_name"), drive_name=b.get("drive_name"),
            document_type=b.get("document_type"), department=b.get("department"))
    except sp.SPReauth:
        return jsonify(error="reauth"), 401
    _trigger_processing_run()  # best-effort: don't make the user wait for the schedule
    _audit(email, "sp_import", drive_id, {"request_id": req_id, "selected": len(selections)})
    return jsonify(request_id=req_id, queued=True)


@app.get("/api/sharepoint/import/<req_id>")
def sp_import_status(req_id):
    email = current_user()
    if not _can_import(email):
        return jsonify(error="forbidden"), 403
    st = sp.import_job_status(req_id)
    if not st:
        return jsonify(error="not_found"), 404
    return jsonify(**st)


@app.post("/api/sharepoint/resync-sites")
def sp_resync_sites():
    """Re-mirror the caller's SharePoint site visibility into permissions (idempotent)."""
    email = current_user()
    try:
        n = sp.sync_user_sites(email)
        return jsonify(sites=n)
    except sp.SPReauth:
        return jsonify(error="reauth"), 401


@app.post("/api/admin/backfill-sp")
def sp_backfill():
    """Admin: fill SP-location columns on legacy docs using the admin's delegated token."""
    email = current_user()
    is_admin, _, _ = get_perms(email)
    if not is_admin:
        return jsonify(error="forbidden"), 403
    try:
        return jsonify(sp.backfill_sp_locations(email, int(request.args.get("limit", 500))))
    except sp.SPReauth:
        return jsonify(error="reauth"), 401


@app.get("/api/sharepoint/syncs")
def sp_syncs_list():
    email = current_user()
    is_admin, is_full, allowed = get_perms(email)
    sites = None if is_full else allowed
    return jsonify(syncs=sp.list_syncs(sites))


@app.post("/api/sharepoint/syncs")
def sp_syncs_create():
    email = current_user()
    if not _can_import(email):
        return jsonify(error="forbidden"), 403
    sel = request.get_json(force=True)
    for req_key in ("site_id", "drive_id"):
        if not sel.get(req_key):
            return jsonify(error=f"{req_key} required"), 400
    try:
        r = sp.arm_sync(email, sel)
    except sp.SPReauth as e:
        return jsonify(error="reauth", detail=str(e)), 401
    _audit(email, "sp_sync_create", r["id"], sel)
    return jsonify(**r)


@app.delete("/api/sharepoint/syncs/<sync_id>")
def sp_syncs_delete(sync_id):
    email = current_user()
    if not _can_import(email):
        return jsonify(error="forbidden"), 403
    sp.remove_sync(sync_id)
    _audit(email, "sp_sync_delete", sync_id, None)
    return jsonify(deleted=True)


@app.post("/api/sharepoint/syncs/<sync_id>/reconnect")
def sp_syncs_reconnect(sync_id):
    email = current_user()
    if not _can_import(email):
        return jsonify(error="forbidden"), 403
    try:
        sp.reconnect_sync(sync_id, email)
    except sp.SPReauth as e:
        return jsonify(error="reauth", detail=str(e)), 401
    _audit(email, "sp_sync_reconnect", sync_id, None)
    return jsonify(reconnected=True)


# ───────────────────────────────────────────────────────────────────── util ──

def _trigger_processing_run():
    """Best-effort: kick the processing job so queued work isn't stuck behind the schedule.

    No-op if PROCESSING_JOB_ID is unset or the app SP lacks run permission — the scheduled
    (or manual run-now) drain will still pick the work up. Never raises into the request.
    """
    if not config.PROCESSING_JOB_ID:
        return
    try:
        _w.jobs.run_now(job_id=int(config.PROCESSING_JOB_ID))
    except Exception as exc:
        print(f"processing job trigger skipped: {exc}")


def _audit(actor, action, target, detail):
    try:
        execute(
            f"INSERT INTO {config.AUDIT_LOG} (event_id, actor, action, target, detail, created_at) "
            f"VALUES ({lit('e_'+uuid.uuid4().hex[:12])}, {lit(actor)}, {lit(action)}, "
            f"{lit(target)}, {lit(json.dumps(detail) if detail else None)}, current_timestamp())"
        )
    except Exception:
        pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)
