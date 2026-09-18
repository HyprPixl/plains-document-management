"""Document Hub — Flask backend (Databricks App).

Two surfaces: Manage (classify/extract/verify) and Explore (search/view/download).
Heavy processing (OCR/render/extraction) is handed off to the Databricks job in
processing/ — this web app only enqueues work and reads results from Delta.
"""
import io
import json
import logging
import os
import re
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from flask import Flask, Response, g, jsonify, request, send_file, render_template
from werkzeug.exceptions import HTTPException
from databricks.sdk import WorkspaceClient

import config
import ingest
import lakebase
import sharepoint as sp
from db import query, execute, lit, cached_query, bust_cache

app = Flask(__name__)
_w = WorkspaceClient()

# Best-effort side-effects (audit-log writes, kicking the processing job) hit the warehouse or
# the Jobs API — each ~1s+ — but the response doesn't depend on them. Run them off the request
# thread so a submit (classify / enqueue / tag / verify) returns as soon as its fast Lakebase
# write lands. Small bounded pool per gunicorn worker; failures are swallowed by the callables.
_bg_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dochub-bg")


def _submit_bg(fn, *args, **kwargs):
    """Run a best-effort side-effect in the background. Runs inline under test for determinism."""
    if app.config.get("TESTING"):
        try:
            fn(*args, **kwargs)
        except Exception:
            pass
        return
    _bg_pool.submit(fn, *args, **kwargs)

# ─────────────────────────────────────────────────────────────── logging ──
# Structured logs to stdout so the Databricks App log captures every error on the
# request path (pattern from contract-explorer). Each line carries a request id, the
# route/method, and the forwarded user so a failure can be traced end-to-end.
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
app.logger.handlers = [_handler]
app.logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
app.logger.propagate = False  # our stdout handler is the only sink — avoid double lines


def _req_ctx() -> str:
    """Compact request context tag shared by every log line on the request path."""
    return (f"request_id={getattr(g, 'request_id', '-')} "
            f"method={request.method} route={request.path} "
            f"user={getattr(g, 'user_email', '-') or '-'}")


@app.before_request
def _assign_request_id():
    g.request_id = "r_" + uuid.uuid4().hex[:12]
    g.req_started = time.perf_counter()  # per-request wall-time baseline (see _log_response)
    # Best-effort user identity for logs; never let header parsing break the request.
    try:
        g.user_email = current_user()
    except Exception:
        g.user_email = None


@app.after_request
def _log_response(resp):
    # Per-request timing: one INFO line per request with total wall-time, so hot endpoints
    # can be profiled from the app log alongside db.py's slow-query lines (Phase 1 benches).
    dur_ms = (time.perf_counter() - g.req_started) * 1000 if hasattr(g, "req_started") else -1
    app.logger.info(f"request complete — {_req_ctx()} status={resp.status_code} duration_ms={dur_ms:.0f}")
    # Log every 4xx/5xx (not just 500s) so client errors are visible too. 5xx paths that
    # raised are already logged by the error handler; this catches handler-returned codes.
    if resp.status_code >= 400:
        level = logging.ERROR if resp.status_code >= 500 else logging.WARNING
        app.logger.log(level, f"{resp.status_code} response — {_req_ctx()}")
    return resp


@app.errorhandler(HTTPException)
def _handle_http_exc(exc: HTTPException):
    # Explicit aborts / 404s etc. — log at warning, preserve the intended status code and
    # a friendly JSON body (no stack trace to the business-facing UI).
    app.logger.warning(f"{exc.code} {exc.name} — {_req_ctx()}")
    return jsonify(error=exc.name.lower().replace(" ", "_"), detail=exc.description), exc.code


@app.errorhandler(Exception)
def _handle_unhandled_exc(exc: Exception):
    # Every unhandled exception on the request path: full traceback + context to the log,
    # a generic message to the browser (SPEC §16 — don't leak internals to business users).
    app.logger.error(f"unhandled exception — {_req_ctx()}\n{traceback.format_exc()}")
    return jsonify(error="internal_error",
                   detail="Something went wrong. The error has been logged.",
                   request_id=getattr(g, "request_id", None)), 500

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

    The permission row is a single-row warehouse lookup that costs ~1.2s of fixed
    Statement Execution API latency (see bench/BASELINE.md), yet perms_where() runs it 2-3x
    per page. Memoize the result on flask.g so it's fetched **once per request** and reused;
    g is per-request scope, so nothing leaks between requests. Outside a request context
    (some unit tests call this directly) we skip the cache and query straight through.
    """
    key = (email or "").lower()
    try:
        cache = g._perms_cache
    except (RuntimeError, AttributeError):
        try:
            cache = g._perms_cache = {}
        except RuntimeError:
            cache = None  # no request context — don't memoize
    if cache is not None and key in cache:
        return cache[key]

    # Phase 2: read the single-row permission lookup from Lakebase (single-digit-ms
    # round trip) when the flag + a bound Postgres host are present; otherwise the
    # warehouse path is unchanged. Any Lakebase failure falls back to the warehouse so
    # a page never hard-fails on the new store during cutover.
    rows = None
    if config.USE_LAKEBASE_PERMISSIONS and lakebase.enabled():
        try:
            rows = lakebase.read_permissions(email)
        except Exception as e:
            try:
                ctx = _req_ctx()
            except Exception:
                ctx = "no-request-context"
            app.logger.warning(f"lakebase perms read failed ({e!r}), falling back to warehouse — {ctx}")
            rows = None
    if rows is None:
        rows = query(
            f"SELECT access_type, allowed_site FROM {config.PERMISSIONS} "
            f"WHERE lower(email) = {lit(email)}"
        )
    if not rows:
        result = (False, False, [])
    else:
        types = {(r["access_type"] or "").upper() for r in rows}
        is_admin = "ADMIN" in types
        is_full = is_admin or "FULL" in types
        allowed = [r["allowed_site"] for r in rows if r.get("allowed_site")]
        result = (is_admin, is_full, allowed)
    if cache is not None:
        cache[key] = result
    return result


def perms_where(email: str, col: str = "sp_site_id") -> str:
    """SQL predicate enforcing site-scoped access. Returns '' for full access.

    Non-SharePoint docs (drag-drop uploads) carry sp_site_id = NULL, which matches no
    site scope — so without this they'd be invisible to everyone but FULL/ADMIN. A user
    always sees their own uploads: sp_site_id IS NULL AND created_by = them. This is
    confined to NULL-site rows, so it never widens visibility on SharePoint-sourced docs.
    """
    is_admin, is_full, allowed = get_perms(email)
    if is_full:
        return ""
    cb = (col.rsplit(".", 1)[0] + ".created_by") if "." in col else "created_by"
    own = f"({col} IS NULL AND {cb} = {lit(email)})"
    site = f"{col} IN ({','.join(lit(b) for b in allowed)})" if allowed else "1=0"
    return f" AND ({site} OR {own}) "


def perms_sites(email: str):
    """Site scope as data for the Lakebase document reads (parameterized, not a SQL
    fragment). Returns None for full/admin (unrestricted), else the allowed-sites list
    (possibly empty → see nothing) — the same three-way scope perms_where() encodes."""
    _is_admin, is_full, allowed = get_perms(email)
    return None if is_full else allowed


def _doc_visibility(d: dict, email: str) -> bool:
    """Whether the caller may see a single doc row under site scope — the row-level equivalent
    of perms_where / _sites_clause, for the by-doc-id endpoints (render, tree). FULL/ADMIN see
    all; otherwise the doc's sp_site_id must be in the caller's allowed sites, OR it's their own
    NULL-site upload (sp_site_id IS NULL AND created_by = them)."""
    sites = perms_sites(email)
    if sites is None:  # FULL / ADMIN — unrestricted
        return True
    site = d.get("sp_site_id")
    if site is None:
        return (d.get("created_by") or "").lower() == (email or "").lower()
    return site in sites


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
# field_defs / taxonomy are read on hot paths (every document open) but change rarely. They
# live in the warehouse (source of truth) and are mirrored into Lakebase for single-digit-ms
# reads (see lakebase.py config-mirror). These two accessors centralise the store choice:
# Lakebase when the cutover is on, warehouse (TTL-cached) as the fallback on any error.
# Callers filter the ACTIVE-row lists by applies_to / data_type / category in Python.

def _active_field_defs():
    """All active field defs (canonical order), Lakebase-first with warehouse fallback."""
    if lakebase.config_enabled():
        try:
            return lakebase.read_field_defs()
        except Exception as e:
            app.logger.warning(f"lakebase field_defs read failed, warehouse fallback ({e!r}) — {_req_ctx()}")
    return cached_query(
        f"SELECT field_key, label, data_type, applies_to, picklist_source, "
        f"extraction_prompt_hint, required_for_verify, sort_order FROM {config.FIELD_DEFS} "
        f"WHERE active = true ORDER BY (applies_to = 'common') DESC, sort_order")


def _active_taxonomy():
    """All active taxonomy option-set rows, Lakebase-first with warehouse fallback."""
    if lakebase.config_enabled():
        try:
            return lakebase.read_taxonomy()
        except Exception as e:
            app.logger.warning(f"lakebase taxonomy read failed, warehouse fallback ({e!r}) — {_req_ctx()}")
    return cached_query(
        f"SELECT category, value, label, business_unit, sort_order FROM {config.TAXONOMY} "
        f"WHERE active = true ORDER BY category, sort_order")


def _applies(defn, doc_type):
    """A field def applies to a doc when it's common or scoped to that document_type."""
    a = defn.get("applies_to")
    return a == "common" or (doc_type is not None and a == doc_type)


@app.get("/api/taxonomy")
def api_taxonomy():
    rows = _active_taxonomy()
    out = {"department": [], "document_type": []}
    for r in rows:
        out.setdefault(r["category"], []).append(
            {"value": r["value"], "label": r["label"], "business_unit": r.get("business_unit")}
        )
    return jsonify(out)


@app.get("/api/field-defs")
def api_field_defs():
    doc_type = request.args.get("document_type")
    rows = [r for r in _active_field_defs() if _applies(r, doc_type)]
    for r in rows:
        if r.get("picklist_source") and "|" in str(r["picklist_source"]):
            r["options"] = str(r["picklist_source"]).split("|")
    return jsonify(rows)


# ──────────────────────────────────────────────── field-def management (admin) ──

@app.get("/api/field-defs/all")
def api_field_defs_all():
    """Every active field def, grouped by what it applies to — for the Fields admin screen."""
    rows = sorted(_active_field_defs(),
                  key=lambda r: (r.get("applies_to") != "common", r.get("applies_to") or "",
                                 r.get("sort_order") or 0))
    doc_types = [r["value"] for r in _active_taxonomy() if r.get("category") == "document_type"]
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
    bust_cache()
    lakebase.resync_config()  # push the new def into the Lakebase read mirror immediately
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
    bust_cache()
    lakebase.resync_config()
    _audit(email, "field_def_update", field_key, b)
    return jsonify(ok=True)


@app.delete("/api/field-defs/<field_key>")
def api_field_def_delete(field_key):
    email = current_user()
    if not _require_admin(email):
        return jsonify(error="forbidden"), 403
    execute(f"UPDATE {config.FIELD_DEFS} SET active = false, updated_at = current_timestamp() "
            f"WHERE field_key = {lit(field_key)}")
    bust_cache()
    lakebase.resync_config()
    _audit(email, "field_def_delete", field_key, None)
    return jsonify(ok=True)


# ─────────────────────────────────────────────── admin: access management ──
# The in-app way to change who's an app admin (and grant FULL / READ). SITE rows are
# auto-mirrored from a user's own SharePoint browse (sp.sync_user_sites) and are NOT
# managed here — this console only touches the manually-granted ADMIN / FULL / READ rows.
# Dual-store like every permission write during Phase 2: the warehouse is the source of
# truth for the fallback path, and the change is mirrored into Lakebase (the live read path).

_ACCESS_TYPES = ("ADMIN", "FULL", "READ")
_ACCESS_RANK = {"ADMIN": 3, "FULL": 2, "READ": 1}


def _elevated_grants():
    """All elevated (non-SITE) grants, collapsed to the strongest access_type per user.

    Reads Lakebase (the live path) when enabled, falling back to the warehouse on any error —
    the same read discipline as get_perms()."""
    rows = None
    if config.USE_LAKEBASE_PERMISSIONS and lakebase.enabled():
        try:
            rows = lakebase.list_access_grants()
        except Exception as e:
            app.logger.warning(f"lakebase list_access_grants failed, warehouse fallback — {e!r}")
            rows = None
    if rows is None:
        rows = query(
            f"SELECT email, access_type, updated_at FROM {config.PERMISSIONS} "
            f"WHERE upper(access_type) <> 'SITE' ORDER BY email"
        )
    best = {}
    for r in rows:
        em = (r.get("email") or "").lower()
        at = (r.get("access_type") or "").upper()
        if not em or at not in _ACCESS_RANK:
            continue
        cur = best.get(em)
        if cur is None or _ACCESS_RANK[at] > _ACCESS_RANK[cur["access_type"]]:
            best[em] = {"email": em, "access_type": at,
                        "updated_at": str(r.get("updated_at")) if r.get("updated_at") else None}
    return sorted(best.values(), key=lambda g: g["email"])


def _set_access(email_l, access):
    """Replace a user's elevated (non-SITE) grant with a single access_type, or clear it on
    NONE (revoke). Warehouse write is the source of truth; then mirror into Lakebase. SITE
    rows (SharePoint-mirrored) are never touched here."""
    execute(f"DELETE FROM {config.PERMISSIONS} WHERE lower(email) = {lit(email_l)} "
            f"AND upper(access_type) <> 'SITE'")
    if access in _ACCESS_TYPES:
        execute(f"INSERT INTO {config.PERMISSIONS} (email, access_type, allowed_site, updated_at) "
                f"VALUES ({lit(email_l)}, {lit(access)}, NULL, current_timestamp())")
    lakebase.set_access_grant(email_l, access)


@app.get("/api/admin/access")
def api_admin_access_list():
    email = current_user()
    if not _require_admin(email):
        return jsonify(error="forbidden"), 403
    return jsonify(grants=_elevated_grants(), me=email)


@app.post("/api/admin/access")
def api_admin_access_set():
    email = current_user()
    if not _require_admin(email):
        return jsonify(error="forbidden"), 403
    b = request.get_json(force=True)
    target = (b.get("email") or "").strip().lower()
    access = (b.get("access_type") or "").strip().upper()
    if not target or "@" not in target:
        return jsonify(error="a valid email is required"), 400
    if access not in _ACCESS_TYPES and access != "NONE":
        return jsonify(error="access_type must be ADMIN, FULL, READ, or NONE"), 400
    # Guard against locking everyone out: the last remaining admin can't be demoted/revoked.
    if access != "ADMIN":
        admins = {g["email"] for g in _elevated_grants() if g["access_type"] == "ADMIN"}
        if target in admins and len(admins) == 1:
            return jsonify(error="cannot remove the last remaining admin"), 409
    _set_access(target, access)
    _audit(email, "access_set", target, {"access_type": access})
    return jsonify(ok=True, email=target, access_type=access)


# ──────────────────────────────────────────────────────────── upload + dedup ──

@app.post("/api/upload")
def api_upload():
    """Hash-first upload. Each file is filed under a SharePoint site the uploader can
    access, so it inherits that site's audience via the normal site-level scoping (a
    local file has no external ACL of its own). Returns per-file new/duplicate."""
    email = current_user()
    site_id = (request.form.get("sp_site_id") or "").strip() or None
    site_name = (request.form.get("sp_site_name") or "").strip() or None
    if not site_id:
        return jsonify(error="site_required",
                       detail="Choose a SharePoint site to file the upload under."), 400
    _is_admin, is_full, allowed = get_perms(email)
    if not is_full and site_id not in allowed:
        return jsonify(error="forbidden", detail="You don't have access to that site."), 403
    batch_id = "b_" + uuid.uuid4().hex[:12]
    results = []
    for f in request.files.getlist("files"):
        r = ingest.register_bytes(
            f.read(), f.filename, f.mimetype,
            source_id="upload", source_ref="upload", created_by=email,
            subdir="uploads", batch_id=batch_id,
            sp_site_id=site_id, sp_site_name=site_name,
        )
        results.append(r)
    return jsonify(batch_id=batch_id, results=results)


@app.get("/api/sites")
def api_sites():
    """Sites the caller may file an upload under — the distinct SharePoint sites already
    visible to them (site-scoped), so an upload's audience is always a site they truly
    have access to. Feeds the upload site picker."""
    email = current_user()
    if lakebase.docs_enabled():
        return jsonify(sites=lakebase.list_sites(perms_sites(email), email))
    where = "sp_site_id IS NOT NULL" + perms_where(email)
    rows = query(
        f"SELECT DISTINCT sp_site_id AS id, sp_site_name AS name FROM {config.DOCUMENTS} "
        f"WHERE {where} ORDER BY sp_site_name"
    )
    return jsonify(sites=[{"id": r["id"], "name": r.get("name")} for r in rows if r.get("id")])


@app.get("/api/admin/acl-audit")
def api_acl_audit():
    """Phase 6 measurement: distribution of unique vs inherited SharePoint permissions across
    synced docs. Admin-only, temporary — tells us whether per-item ACLs are worth building
    before we commit to the group-expansion machinery. Recorded only in Lakebase."""
    email = current_user()
    is_admin, _is_full, _allowed = get_perms(email)
    if not is_admin:
        return jsonify(error="forbidden"), 403
    if not lakebase.docs_enabled():
        return jsonify(error="unavailable",
                       detail="ACL instrumentation is recorded only in Lakebase."), 503
    return jsonify(lakebase.acl_stats())


# ─────────────────────────────────────────────────────────────── documents ──

@app.get("/api/documents")
def api_documents():
    email = current_user()
    status = request.args.get("verification_status")
    cstatus = request.args.get("classification_status")
    if lakebase.docs_enabled():
        return jsonify(lakebase.list_documents(perms_sites(email), email, status, cstatus))
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
    if lakebase.docs_enabled():
        rows, unclassified = lakebase.stats(perms_sites(email), email)
    else:
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
    # A doc classified with no document_type has no type and no extractable fields — reject it.
    if not dt:
        return jsonify(error="document_type_required"), 400
    if lakebase.docs_enabled():
        lakebase.classify(ids, dt, dept)
    else:
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
    if lakebase.docs_enabled():
        lakebase.enqueue(ids)
    else:
        id_list = ",".join(lit(i) for i in ids)
        execute(
            f"UPDATE {config.DOCUMENTS} SET extraction_status = 'pending', "
            f"next_attempt_at = NULL, updated_at = current_timestamp() "
            f"WHERE doc_id IN ({id_list}) AND classification_status = 'classified'"
        )
    _submit_bg(_trigger_processing_run)  # kick the job off-thread; extraction isn't part of the response
    _audit(email, "enqueue", f"{len(ids)} docs", None)
    return jsonify(enqueued=len(ids))


# ───────────────────────────────────────────────────────────────────── tags ──

@app.get("/api/tags")
def api_tags():
    """Distinct tags across the corpus (facet / autocomplete)."""
    if lakebase.docs_enabled():
        return jsonify(lakebase.tag_facets())
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
    if lakebase.docs_enabled():
        if not lakebase.tag_exists(doc_id, tag):
            lakebase.add_tag(doc_id, tag, email)
            _audit(email, "tag_add", doc_id, {"tag": tag})
        return jsonify(ok=True, tag=tag)
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
    if lakebase.docs_enabled():
        lakebase.remove_tag(doc_id, tag)
    else:
        execute(
            f"DELETE FROM {config.DOCUMENT_TAGS} WHERE doc_id = {lit(doc_id)} AND tag = {lit(tag)}")
    _audit(email, "tag_remove", doc_id, {"tag": tag})
    return jsonify(ok=True)


@app.get("/api/documents/<doc_id>")
def api_document(doc_id):
    if lakebase.docs_enabled():
        # Document row + field values + links + tags in ONE Lakebase round-trip (was four
        # serial reads — see bench/BASELINE.md). field_defs still comes from the warehouse:
        # a cross-store join isn't possible, so read the defs there and merge the doc's values
        # (from the bundle) by field_key in Python.
        bundle = lakebase.get_document_bundle(doc_id)
        if not bundle:
            return jsonify(error="not found"), 404
        doc = bundle["document"]
        # field_defs comes from the config mirror (Lakebase-first, warehouse fallback) — a
        # cross-store join isn't possible, so merge the doc's values (from the bundle) by
        # field_key in Python.
        defs = [r for r in _active_field_defs() if _applies(r, doc.get("document_type"))]
        vals = {r["field_key"]: r for r in bundle["fields"]}
        for d in defs:
            v = vals.get(d["field_key"], {})
            d["proposed_value"] = v.get("proposed_value")
            d["confirmed_value"] = v.get("confirmed_value")
            d["source_provenance"] = v.get("source_provenance")
            if d.get("picklist_source") and "|" in str(d["picklist_source"]):
                d["options"] = str(d["picklist_source"]).split("|")
        return jsonify(document=doc, fields=defs,
                       links=bundle["links"], tags=bundle["tags"])
    docs = query(f"SELECT * FROM {config.DOCUMENTS} WHERE doc_id = {lit(doc_id)}")
    if not docs:
        return jsonify(error="not found"), 404
    doc = docs[0]
    # Field defs + this doc's field values in one round-trip: the drawer is driven by the
    # defs list (each def carries its proposed/confirmed/provenance), so a LEFT JOIN of
    # field_defs -> document_fields yields exactly what the Python merge used to build from
    # two serial queries — one fewer Statement Execution round-trip (see bench/BASELINE.md).
    defs = query(
        f"SELECT fd.field_key, fd.label, fd.data_type, fd.picklist_source, fd.required_for_verify, "
        f"fd.applies_to, fd.sort_order, "
        f"df.proposed_value, df.confirmed_value, df.source_provenance "
        f"FROM {config.FIELD_DEFS} fd "
        f"LEFT JOIN {config.DOCUMENT_FIELDS} df "
        f"  ON df.field_key = fd.field_key AND df.doc_id = {lit(doc_id)} "
        f"WHERE fd.active = true AND "
        f"(fd.applies_to = 'common' OR fd.applies_to = {lit(doc.get('document_type'))}) "
        f"ORDER BY (fd.applies_to='common') DESC, fd.sort_order"
    )
    for d in defs:
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
    if lakebase.docs_enabled():
        lakebase.save_fields(doc_id, values, email)          # one round-trip for all fields
    else:
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
    lake = lakebase.docs_enabled()
    if lake:
        head = lakebase.document_head(doc_id)                # existence + type in one read
        if head is None:
            return jsonify(error="not found"), 404
        dtype = head.get("document_type")
    else:
        docs = query(f"SELECT document_type FROM {config.DOCUMENTS} WHERE doc_id = {lit(doc_id)}")
        if not docs:
            return jsonify(error="not found"), 404
        dtype = docs[0].get("document_type")
    # required fields present? (field_defs read from the config mirror — Lakebase-first,
    # warehouse fallback; it changes only via admin field-def edits, which resync the mirror)
    req = [{"field_key": r["field_key"]} for r in _active_field_defs()
           if r.get("required_for_verify") and _applies(r, dtype)]
    # A required field is satisfied by a human-confirmed value OR an unedited AI proposal —
    # the reviewer sees the suggested value in the drawer and vouches for it by verifying.
    if lake:
        have = lakebase.satisfied_field_keys(doc_id)
    else:
        have = {r["field_key"] for r in query(
            f"SELECT field_key FROM {config.DOCUMENT_FIELDS} "
            f"WHERE doc_id = {lit(doc_id)} AND coalesce(confirmed_value, proposed_value) IS NOT NULL "
            f"AND coalesce(confirmed_value, proposed_value) <> ''")}
    missing = [r["field_key"] for r in req if r["field_key"] not in have]
    if missing:
        return jsonify(error="missing_required", fields=missing), 400
    # amendments require a parent contract link
    if dtype == "Amendment":
        has_parent = (lakebase.has_amendment_parent(doc_id) if lake else bool(query(
            f"SELECT 1 FROM {config.DOCUMENT_LINKS} WHERE child_doc_id = {lit(doc_id)} "
            f"AND relationship = 'amendment_of' LIMIT 1")))
        if not has_parent:
            return jsonify(error="amendment_needs_parent"), 400
    if lake:
        lakebase.verify(doc_id, email)
    else:
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
    if lakebase.docs_enabled():
        lakebase.unverify(doc_id)
    else:
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
    if lakebase.docs_enabled():
        lakebase.add_link(parent, child, rel, email)
    else:
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
    # sort: whitelist keyword only — the raw value never reaches the SQL. limit/offset clamped.
    sort = request.args.get("sort") or "newest"
    if sort not in lakebase._SEARCH_ORDER_BY:
        sort = "newest"
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(200, limit))
    try:
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)
    if lakebase.docs_enabled():
        rows, total = lakebase.search(perms_sites(email), email, q, dt, dept, path, tag,
                                      sort, limit, offset)
        return jsonify(rows=rows, total=total)
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
        f"coalesce(f_sum.confirmed_value, f_sum.proposed_value) AS summary, d.created_at, "
        f"count(*) OVER() AS _total "
        f"FROM {config.DOCUMENTS} d "
        f"LEFT JOIN {config.DOCUMENT_FIELDS} f_title ON f_title.doc_id = d.doc_id AND f_title.field_key = 'title' "
        f"LEFT JOIN {config.DOCUMENT_FIELDS} f_sum ON f_sum.doc_id = d.doc_id AND f_sum.field_key = 'summary' "
        f"{join_txt}{join_tag}"
        f"WHERE {where} ORDER BY {lakebase._SEARCH_ORDER_BY[sort]} "
        f"LIMIT {int(limit)} OFFSET {int(offset)}"
    )
    total = int(rows[0]["_total"]) if rows else 0
    for r in rows:
        r.pop("_total", None)
    return jsonify(rows=rows, total=total)


@app.get("/api/download")
def api_download():
    """Stream a file from the volume (searchable PDF preferred, else original)."""
    doc_id = request.args.get("doc_id")
    which = request.args.get("which", "derived")
    if lakebase.docs_enabled():
        d = lakebase.get_document(doc_id)
        if not d:
            return jsonify(error="not found"), 404
    else:
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


# ──────────────────────────────────────── Office render (multi-filetype viewer) ──
# Item E: render Office docs (docx / xlsx) to standalone HTML so the viewer can show them
# inline. Content-addressed by the file's sha256: identical bytes render once, cached in a
# volume sidecar that survives across the 4 gunicorn workers and restarts. A tiny per-worker
# LRU sits on top so a hot doc doesn't re-read the sidecar every request.

_RENDER_MEM: "OrderedDict[str, str]" = OrderedDict()
_RENDER_MEM_MAX = 32
_RENDER_MEM_LOCK = threading.Lock()

_HTML_HEAD = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<style>"
    "body{max-width:900px;margin:2rem auto;padding:0 1.25rem;line-height:1.55;color:#1a1a1a;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;}"
    "table{border-collapse:collapse;margin:1rem 0;max-width:100%;}"
    "td,th{border:1px solid #cbd0d6;padding:4px 8px;vertical-align:top;}"
    "th{background:#f4f6f8;}img{max-width:100%;height:auto;}"
    "h1,h2,h3{line-height:1.25;}"
    ".xl-tabs{margin:0 0 1rem;border-bottom:1px solid #cbd0d6;}"
    ".xl-tab{background:none;border:0;padding:.5rem .9rem;cursor:pointer;font:inherit;"
    "border-bottom:2px solid transparent;color:#556;}"
    ".xl-tab.active{border-bottom-color:#2563eb;color:#111;font-weight:600;}"
    "</style></head><body>"
)
_HTML_TAIL = "</body></html>"


def _html_shell(body: str) -> str:
    return _HTML_HEAD + body + _HTML_TAIL


def _render_mem_get(key):
    with _RENDER_MEM_LOCK:
        html = _RENDER_MEM.get(key)
        if html is not None:
            _RENDER_MEM.move_to_end(key)
        return html


def _render_mem_put(key, html):
    with _RENDER_MEM_LOCK:
        _RENDER_MEM[key] = html
        _RENDER_MEM.move_to_end(key)
        while len(_RENDER_MEM) > _RENDER_MEM_MAX:
            _RENDER_MEM.popitem(last=False)


def _render_sidecar_read(path: str):
    """Read the cached HTML sidecar from the volume, or None if it's absent/unreadable."""
    try:
        return _w.files.download(path).contents.read().decode("utf-8")
    except Exception:
        return None


def _render_sidecar_write(path: str, html: str):
    """Best-effort write-through of the rendered HTML sidecar. Never raises into the request —
    a failed cache write just means the next request re-renders."""
    try:
        _w.files.upload(path, io.BytesIO(html.encode("utf-8")), overwrite=True)
    except Exception as exc:
        app.logger.warning(f"render cache write skipped ({exc!r}) — {_req_ctx()}")


def _render_docx(raw: bytes) -> str:
    import mammoth
    body = mammoth.convert_to_html(io.BytesIO(raw)).value
    return _html_shell(body)


def _render_xlsx(raw: bytes, sheet_arg=None) -> str:
    """Render every sheet of a workbook into one tabbed HTML page (buttons show/hide each
    sheet div). ``sheet_arg`` (from &sheet=) may pre-select a tab."""
    import openpyxl
    from xlsx2html import xlsx2html
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True)
    names = list(wb.sheetnames)
    wb.close()
    try:
        pre = int(sheet_arg) if sheet_arg is not None else 0
    except (TypeError, ValueError):
        pre = 0
    if pre < 0 or pre >= len(names):
        pre = 0
    tabs, panels = [], []
    for idx, name in enumerate(names):
        out = io.StringIO()
        try:
            xlsx2html(io.BytesIO(raw), out, sheet=idx)
            table = out.getvalue()
        except Exception as exc:
            table = f"<p>Could not render sheet: {exc}</p>"
        active = " active" if idx == pre else ""
        hidden = "" if idx == pre else " style=\"display:none\""
        safe = (name or f"Sheet {idx + 1}").replace("<", "&lt;").replace(">", "&gt;")
        tabs.append(f'<button class="xl-tab{active}" onclick="xlShow({idx})">{safe}</button>')
        panels.append(f'<div class="xl-sheet" id="xl-sheet-{idx}"{hidden}>{table}</div>')
    js = (
        "<script>function xlShow(i){"
        "document.querySelectorAll('.xl-sheet').forEach(function(d,j){"
        "d.style.display=(j===i)?'':'none';});"
        "document.querySelectorAll('.xl-tab').forEach(function(b,j){"
        "b.classList.toggle('active',j===i);});}</script>"
    )
    body = f'<div class="xl-tabs">{"".join(tabs)}</div>{"".join(panels)}{js}'
    return _html_shell(body)


@app.get("/api/render")
def api_render():
    """Render a docx / xlsx doc to standalone HTML for the inline viewer. Auth + site-scope
    like /api/download; content-addressed sidecar cache in the volume (read-through)."""
    email = current_user()
    doc_id = request.args.get("doc_id")
    if lakebase.docs_enabled():
        d = lakebase.get_document(doc_id)
    else:
        docs = query(
            f"SELECT volume_path, content_sha256, original_filename, sp_site_id, created_by "
            f"FROM {config.DOCUMENTS} WHERE doc_id = {lit(doc_id)}")
        d = docs[0] if docs else None
    if not d:
        return jsonify(error="not found"), 404
    if not _doc_visibility(d, email):
        return jsonify(error="forbidden"), 403
    ext = os.path.splitext(d.get("original_filename") or "")[1].lower()
    if ext == ".docx":
        kind = "docx"
    elif ext in (".xlsx", ".xlsm"):
        kind = "xlsx"
    else:
        return jsonify(error="unsupported"), 415
    sha = d.get("content_sha256") or ""
    sheet_arg = request.args.get("sheet")
    # The sidecar (and the mem LRU) are content-addressed, so they hold the full workbook (all
    # sheets); &sheet= only changes which tab is pre-selected client-side, so it's safe to serve
    # the cached all-sheets page and let the JS default to tab 0 — good enough for the contract.
    cache_key = f"{sha}.{kind}"
    cache_path = f"{config.DOCS_VOLUME}/_render_cache/{sha}.{kind}.html"
    html = _render_mem_get(cache_key)
    if html is None:
        html = _render_sidecar_read(cache_path)
        if html is not None:
            _render_mem_put(cache_key, html)
    if html is None:
        raw = _w.files.download(d["volume_path"]).contents.read()
        html = _render_docx(raw) if kind == "docx" else _render_xlsx(raw, sheet_arg)
        _render_sidecar_write(cache_path, html)
        _render_mem_put(cache_key, html)
    return Response(html, mimetype="text/html")


# ─────────────────────────────────────── keyword-grounded corpus chat (SSE) ──
# Item O: retrieval-augmented chat over the corpus. Retrieval reuses search()'s site scoping
# (so a user can never be answered from a doc outside their sites); the model is a Databricks
# serving endpoint reached through the OpenAI-compatible client, exactly as contract-explorer
# does. Answers stream back over Server-Sent Events with a trailing citations event.

_CHAT_CLIENT = {"client": None, "expires_at": 0.0}
_CHAT_CLIENT_LOCK = threading.Lock()


def _get_serving_client(force_rebuild: bool = False):
    """Build (and ~50-min cache) an OpenAI client pointed at the workspace serving endpoints,
    authed with a freshly minted Databricks token. Returns None when serving isn't configured
    (no host, or the openai package / SDK auth is unavailable) so the route can 503 cleanly."""
    now = time.monotonic()
    with _CHAT_CLIENT_LOCK:
        if (not force_rebuild and _CHAT_CLIENT["client"] is not None
                and now < _CHAT_CLIENT["expires_at"]):
            return _CHAT_CLIENT["client"]
        try:
            from openai import OpenAI
            auth = _w.config.authenticate()
            token = (auth or {}).get("Authorization", "").removeprefix("Bearer ")
            host = (_w.config.host or "").rstrip("/")
            if not host or not token:
                return None
            client = OpenAI(api_key=token, base_url=host + "/serving-endpoints")
        except Exception as exc:
            app.logger.warning(f"serving client unavailable ({exc!r})")
            return None
        _CHAT_CLIENT["client"] = client
        _CHAT_CLIENT["expires_at"] = now + 50 * 60
        return client


def _is_auth_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return (exc.__class__.__name__ in ("AuthenticationError", "PermissionDeniedError")
            or "401" in s or "unauthorized" in s or "authentication" in s
            or "invalid_api_key" in s or "expired" in s)


def _chat_retrieve(sites, email, question: str) -> list[dict]:
    """Site-scoped retrieval: candidate docs from search(), then their passages. Never surfaces
    a doc outside the caller's sites — search() applies the same site scope the rest of the app
    does, and passages_for_docs only reads the doc_ids it returns."""
    if not question:
        return []
    if lakebase.docs_enabled():
        rows, _total = lakebase.search(sites, email, question, limit=8)
        doc_ids = [r["doc_id"] for r in rows]
        return lakebase.passages_for_docs(doc_ids, char_budget=12000) if doc_ids else []
    # Warehouse fallback: mirror api_search's verified + site-scoped candidate query, then pull
    # page text for the matches and pack to the same char budget.
    where = "verification_status = 'verified'" + perms_where(email, "d.sp_site_id")
    ql = lit(f"%{question.lower()}%")
    rows = query(
        f"SELECT d.doc_id FROM {config.DOCUMENTS} d "
        f"LEFT JOIN (SELECT doc_id, concat_ws(' ', collect_list(text)) AS body "
        f"FROM {config.DOCUMENT_TEXT} GROUP BY doc_id) tx ON tx.doc_id = d.doc_id "
        f"WHERE {where} AND (lower(d.original_filename) LIKE {ql} "
        f"OR lower(coalesce(tx.body, '')) LIKE {ql}) ORDER BY d.created_at DESC LIMIT 8"
    )
    doc_ids = [r["doc_id"] for r in rows]
    if not doc_ids:
        return []
    id_list = ",".join(lit(i) for i in doc_ids)
    trows = query(
        f"SELECT t.doc_id, t.page, t.text, d.original_filename AS filename "
        f"FROM {config.DOCUMENT_TEXT} t JOIN {config.DOCUMENTS} d ON d.doc_id = t.doc_id "
        f"WHERE t.doc_id IN ({id_list}) ORDER BY t.doc_id, t.page"
    )
    out, used = [], 0
    for r in trows:
        text = r.get("text") or ""
        if not text.strip():
            continue
        out.append({"doc_id": r["doc_id"], "page": r["page"],
                    "text": text, "filename": r["filename"]})
        used += len(text)
        if used >= 12000:
            break
    return out


def _build_chat_messages(passages, history, question):
    system = (
        "You are a document assistant. Answer the user's question using ONLY the context "
        "passages provided below. If the answer is not contained in the context, say you don't "
        "have that information. Cite every claim inline as [<filename> p.<page>] using the "
        "filename and page from the passage you drew it from."
    )
    if passages:
        ctx = "\n\n".join(
            f"[doc_id={p['doc_id']} | {p['filename']} p.{p['page']}]\n{p['text']}"
            for p in passages)
    else:
        ctx = "(no matching documents were found in the corpus)"
    messages = [{"role": "system", "content": system},
                {"role": "system", "content": "Context:\n\n" + ctx}]
    for h in history or []:
        role, content = h.get("role"), h.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})
    return messages


@app.post("/api/chat")
def api_chat():
    email = current_user()
    body = request.get_json(force=True) or {}
    question = (body.get("question") or "").strip()
    history = body.get("history") or []
    if _get_serving_client() is None:
        return jsonify(error="chat_unavailable"), 503
    sites = perms_sites(email)
    passages = _chat_retrieve(sites, email, question)
    messages = _build_chat_messages(passages, history, question)
    citations = [{"doc_id": p["doc_id"], "page": p["page"], "filename": p["filename"]}
                 for p in passages]

    def generate():
        try:
            client = _get_serving_client()
            try:
                stream = client.chat.completions.create(
                    model=config.CHAT_MODEL, max_tokens=2048, messages=messages, stream=True)
            except Exception as exc:
                if not _is_auth_error(exc):
                    raise
                client = _get_serving_client(force_rebuild=True)  # rotate the token, retry once
                stream = client.chat.completions.create(
                    model=config.CHAT_MODEL, max_tokens=2048, messages=messages, stream=True)
            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                token = getattr(choices[0].delta, "content", None)
                if token:
                    yield f"data: {json.dumps(token)}\n\n"
            yield f"event: citations\ndata: {json.dumps(citations)}\n\n"
            yield "event: done\ndata: {}\n\n"
        except Exception as exc:
            app.logger.error(f"chat stream failed ({exc!r}) — {_req_ctx()}")
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# ─────────────────────────────────── relation tree + obligations calendar ──
# Item L: a document's connected relation graph (for a tree/graph view) and a corpus-wide
# calendar of upcoming date-field obligations, both site-scoped and Lakebase-first.

@app.get("/api/documents/<doc_id>/tree")
def api_document_tree(doc_id):
    email = current_user()
    if lakebase.docs_enabled():
        d = lakebase.get_document(doc_id)
    else:
        docs = query(
            f"SELECT doc_id, original_filename, document_type, sp_site_id, created_by "
            f"FROM {config.DOCUMENTS} WHERE doc_id = {lit(doc_id)}")
        d = docs[0] if docs else None
    if not d:
        return jsonify(error="not found"), 404
    if not _doc_visibility(d, email):
        return jsonify(error="forbidden"), 403
    if lakebase.docs_enabled():
        g = lakebase.link_graph(doc_id)
        return jsonify(root=doc_id, nodes=g["nodes"], edges=g["edges"])
    # Warehouse fallback: one-hop links only (no recursive CTE on the warehouse).
    rows = query(
        f"SELECT l.relationship, l.child_doc_id, l.parent_doc_id, d.original_filename, "
        f"d.document_type FROM {config.DOCUMENT_LINKS} l JOIN {config.DOCUMENTS} d ON d.doc_id = "
        f"  CASE WHEN l.parent_doc_id = {lit(doc_id)} THEN l.child_doc_id ELSE l.parent_doc_id END "
        f"WHERE l.parent_doc_id = {lit(doc_id)} OR l.child_doc_id = {lit(doc_id)}")
    nodes = {doc_id: {"doc_id": doc_id, "original_filename": d.get("original_filename"),
                      "document_type": d.get("document_type")}}
    edges = []
    for r in rows:
        nb = r["child_doc_id"] if r["parent_doc_id"] == doc_id else r["parent_doc_id"]
        nodes[nb] = {"doc_id": nb, "original_filename": r.get("original_filename"),
                     "document_type": r.get("document_type")}
        edges.append({"parent_doc_id": r["parent_doc_id"], "child_doc_id": r["child_doc_id"],
                      "relationship": r["relationship"]})
    return jsonify(root=doc_id, nodes=list(nodes.values()), edges=edges)


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y",
                 "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y/%m/%d")


def _parse_date(s):
    """Best-effort parse of a free-text date string to a date, or None if unparseable."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])  # ISO date or the date half of an ISO datetime
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


@app.get("/api/obligations")
def api_obligations():
    email = current_user()
    d_from = _parse_date(request.args.get("from")) or date.today()
    d_to = _parse_date(request.args.get("to")) or (date.today() + timedelta(days=365))
    document_type = request.args.get("document_type")
    defs = [r for r in _active_field_defs() if r.get("data_type") == "date"]
    date_keys = [r["field_key"] for r in defs]
    labels = {r["field_key"]: r.get("label") for r in defs}
    if not date_keys:
        return jsonify([])
    sites = perms_sites(email)
    if lakebase.docs_enabled():
        rows = lakebase.obligations(sites, email, date_keys, document_type)
    else:
        # Warehouse fallback is awkward (free-string dates, cross-store) — return empty + log.
        app.logger.info(f"obligations: warehouse mode — returning [] (Lakebase-first) — {_req_ctx()}")
        return jsonify([])
    out = []
    for r in rows:
        d = _parse_date(r.get("value"))
        if d is None or d < d_from or d > d_to:
            continue
        out.append({
            "doc_id": r["doc_id"], "original_filename": r.get("original_filename"),
            "document_type": r.get("document_type"), "field_key": r["field_key"],
            "field_label": labels.get(r["field_key"]) or r["field_key"],
            "date": d.isoformat(), "sp_web_url": r.get("sp_web_url"),
            "sp_site_name": r.get("sp_site_name"),
        })
    out.sort(key=lambda x: x["date"])
    return jsonify(out)


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
    connected, has_refresh = sp.session_status(email)        # one session read, not two
    return jsonify(configured=True, connected=connected,
                   has_refresh=has_refresh, can_import=_can_import(email))


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
    _submit_bg(_trigger_processing_run)  # best-effort, off-thread: don't make the user wait
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
    # Prefer Lakebase — a single-digit-ms write in the same store the mutation itself used, so it
    # stays synchronous and durable. Only if Lakebase is down do we fall back to the warehouse,
    # and that write (~1.2s) is fired off the request thread so a submit still returns promptly.
    if lakebase.enabled():
        try:
            lakebase.write_audit(actor, action, target, detail)
            return
        except Exception:
            pass  # Lakebase hiccup — fall through to the (backgrounded) warehouse write
    sql = (
        f"INSERT INTO {config.AUDIT_LOG} (event_id, actor, action, target, detail, created_at) "
        f"VALUES ({lit('e_'+uuid.uuid4().hex[:12])}, {lit(actor)}, {lit(action)}, "
        f"{lit(target)}, {lit(json.dumps(detail) if detail else None)}, current_timestamp())"
    )

    def _write():
        try:
            execute(sql)
        except Exception:
            pass
    _submit_bg(_write)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)
