"""Delegated (on-behalf-of-user) SharePoint access — OAuth authorization-code flow.

This is the *delegated* model: each person connects their own Microsoft identity, so
access is naturally scoped to what they can already open in SharePoint — no per-site
service-principal grants. Distinct from the app-only SPN used by processing/graph.py.

Multi-worker safe: OAuth states and per-user tokens live in Databricks tables (doc-hub
runs 4 gunicorn workers), not in-process dicts. Refresh tokens are encrypted at rest with
a Fernet key derived from the app client secret (no new secret to provision).

See SHAREPOINT_CONNECTION.md for the full design and the plains-nexus reference.
"""
import base64
import hashlib
import os
import time
import urllib.parse
import uuid

import requests
from cryptography.fernet import Fernet
from databricks.sdk import WorkspaceClient

import config
import ingest
from db import query, execute, lit

_w = WorkspaceClient()

AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
_DEAD_GRANT = ("invalid_grant", "interaction_required", "unauthorized_client", "consent_required")
STATE_TTL = 600  # seconds


class SPReauth(Exception):
    """Raised when a delegated grant is dead and the user must reconnect."""


# ───────────────────────────────────────────────────────────── secrets / config ──

def _secret(scope: str, key: str, env_name: str | None = None) -> str | None:
    if env_name and os.getenv(env_name):
        return os.getenv(env_name)
    try:
        return _w.dbutils.secrets.get(scope=scope, key=key)
    except Exception:
        return None


def _tenant() -> str | None:
    return _secret(config.SP_DELEG_SECRET_SCOPE, config.SP_DELEG_TENANT_ID_KEY, "SP_DELEG_TENANT_ID")


def _client_id() -> str | None:
    return _secret(config.SP_DELEG_SECRET_SCOPE, config.SP_DELEG_CLIENT_ID_KEY, "SP_DELEG_CLIENT_ID")


def _client_secret() -> str | None:
    return _secret(config.SP_DELEG_SECRET_SCOPE, config.SP_DELEG_CLIENT_SECRET_KEY, "SP_DELEG_CLIENT_SECRET")


def configured() -> bool:
    return bool(_tenant() and _client_id() and _client_secret())


# ─────────────────────────────────────────────────────────────────── encryption ──

def _fernet() -> Fernet:
    cs = _client_secret()
    if not cs:
        raise SPReauth("delegated client secret unavailable")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(cs.encode()).digest()))


def encrypt(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s: str) -> str:
    return _fernet().decrypt(s.encode()).decode()


def encryption_available() -> bool:
    try:
        _fernet(); return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────── redirect / auth ──

def redirect_uri(headers) -> str:
    if config.APP_BASE_URL:
        base = config.APP_BASE_URL.rstrip("/")
    else:
        proto = headers.get("X-Forwarded-Proto", "https")
        host = headers.get("X-Forwarded-Host") or headers.get("Host", "")
        base = f"{proto}://{host}"
    return f"{base}/api/sharepoint/callback"


def authorize_url(state: str, ruri: str) -> str:
    params = {
        "client_id": _client_id(), "response_type": "code", "redirect_uri": ruri,
        "response_mode": "query", "scope": config.SP_DELEG_SCOPES, "state": state,
    }
    return f"{AUTHORITY}/{_tenant()}/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params)


def _token_endpoint() -> str:
    return f"{AUTHORITY}/{_tenant()}/oauth2/v2.0/token"


def exchange_code(code: str, ruri: str) -> dict:
    r = requests.post(_token_endpoint(), timeout=30, data={
        "client_id": _client_id(), "client_secret": _client_secret(),
        "grant_type": "authorization_code", "code": code, "redirect_uri": ruri,
        "scope": config.SP_DELEG_SCOPES,
    })
    tok = r.json()
    if "access_token" not in tok:
        raise SPReauth(tok.get("error_description", tok.get("error", "token exchange failed")))
    return tok


def refresh_access(refresh_token: str) -> tuple[str, str, int]:
    """Return (access_token, new_refresh_token, expires_in). Entra rotates the refresh token."""
    r = requests.post(_token_endpoint(), timeout=30, data={
        "client_id": _client_id(), "client_secret": _client_secret(),
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "scope": config.SP_DELEG_SCOPES,
    })
    tok = r.json()
    if "access_token" not in tok:
        err = tok.get("error", "")
        if err in _DEAD_GRANT:
            raise SPReauth(tok.get("error_description", err))
        raise RuntimeError(tok.get("error_description", err or "refresh failed"))
    return tok["access_token"], tok.get("refresh_token", refresh_token), int(tok.get("expires_in", 3600))


# ────────────────────────────────────────────────────── OAuth state (SQL-backed) ──

def save_state(email: str, return_to: str, ruri: str) -> str:
    state = uuid.uuid4().hex
    execute(f"DELETE FROM {config.SP_OAUTH_STATE} "
            f"WHERE created_at < current_timestamp() - INTERVAL {STATE_TTL} SECONDS")
    execute(
        f"INSERT INTO {config.SP_OAUTH_STATE} (state, email, return_to, redirect_uri, created_at) "
        f"VALUES ({lit(state)}, {lit(email)}, {lit(return_to)}, {lit(ruri)}, current_timestamp())"
    )
    return state


def pop_state(state: str) -> dict | None:
    rows = query(
        f"SELECT email, return_to, redirect_uri FROM {config.SP_OAUTH_STATE} "
        f"WHERE state = {lit(state)} "
        f"AND created_at >= current_timestamp() - INTERVAL {STATE_TTL} SECONDS LIMIT 1"
    )
    execute(f"DELETE FROM {config.SP_OAUTH_STATE} WHERE state = {lit(state)}")
    return rows[0] if rows else None


# ────────────────────────────────────────────────────── per-user session tokens ──

def store_session(email: str, tok: dict, display_name: str | None = None) -> None:
    access_enc = encrypt(tok["access_token"])
    refresh_enc = encrypt(tok["refresh_token"]) if tok.get("refresh_token") else None
    exp = int(time.time()) + int(tok.get("expires_in", 3600)) - 120
    execute(
        f"MERGE INTO {config.SP_SESSIONS} t USING (SELECT {lit(email)} AS email) s "
        f"ON t.email = s.email "
        f"WHEN MATCHED THEN UPDATE SET access_token_enc = {lit(access_enc)}, "
        f"  refresh_token_enc = coalesce({lit(refresh_enc)}, t.refresh_token_enc), "
        f"  access_expires_at = to_timestamp({exp}), display_name = coalesce({lit(display_name)}, t.display_name), "
        f"  updated_at = current_timestamp() "
        f"WHEN NOT MATCHED THEN INSERT (email, refresh_token_enc, access_token_enc, access_expires_at, "
        f"  display_name, updated_at) VALUES ({lit(email)}, {lit(refresh_enc)}, {lit(access_enc)}, "
        f"  to_timestamp({exp}), {lit(display_name)}, current_timestamp())"
    )


def _session(email: str) -> dict | None:
    rows = query(
        f"SELECT refresh_token_enc, access_token_enc, "
        f"unix_timestamp(access_expires_at) AS exp FROM {config.SP_SESSIONS} "
        f"WHERE lower(email) = {lit(email.lower())} LIMIT 1"
    )
    return rows[0] if rows else None


def session_connected(email: str) -> bool:
    return _session(email) is not None


def has_refresh(email: str) -> bool:
    s = _session(email)
    return bool(s and s.get("refresh_token_enc"))


def access_token_for(email: str) -> str:
    """Return a valid delegated access token, refreshing + rotating storage if needed."""
    s = _session(email)
    if not s:
        raise SPReauth("not connected")
    if s.get("access_token_enc") and s.get("exp") and int(s["exp"]) > time.time():
        return decrypt(s["access_token_enc"])
    if not s.get("refresh_token_enc"):
        raise SPReauth("no refresh token")
    access, new_refresh, expires_in = refresh_access(decrypt(s["refresh_token_enc"]))
    store_session(email, {"access_token": access, "refresh_token": new_refresh, "expires_in": expires_in})
    return access


# ───────────────────────────────────────────────────────────────── Graph browse ──

def _graph(token: str, url: str, params: dict | None = None) -> dict:
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def list_sites(token: str, q: str = "") -> list[dict]:
    url = f"{GRAPH}/sites"
    params = {"search": q} if q else {"$top": 50}
    if not q:
        # 'search=*' returns sites the user follows / can see broadly
        params = {"search": "*"}
    data = _graph(token, url, params)
    return [{"id": s["id"], "name": s.get("displayName") or s.get("name"),
             "web_url": s.get("webUrl")} for s in data.get("value", [])]


def list_drives(token: str, site_id: str) -> list[dict]:
    data = _graph(token, f"{GRAPH}/sites/{site_id}/drives", {"$top": 100})
    return [{"id": d["id"], "name": d.get("name"), "web_url": d.get("webUrl")}
            for d in data.get("value", [])]


def _item_path(it: dict) -> str:
    """Human folder path (incl. filename) from a Graph item's parentReference, e.g.
    '/General/2023/file.pdf'. Empty parent → '/name'."""
    raw = ((it.get("parentReference") or {}).get("path") or "")
    folder = raw.split("root:", 1)[1] if "root:" in raw else ""
    return f"{folder}/{it['name']}"


def list_children(token: str, drive_id: str, item_id: str | None) -> list[dict]:
    base = f"{GRAPH}/drives/{drive_id}/items/{item_id or 'root'}/children"
    out, url = [], base
    params = {"$select": "id,name,size,folder,file,lastModifiedDateTime,webUrl,parentReference",
              "$top": 200}
    while url:
        data = _graph(token, url, params)
        params = None  # nextLink already carries them
        for it in data.get("value", []):
            out.append({
                "id": it["id"], "name": it["name"], "size": it.get("size", 0),
                "is_folder": "folder" in it, "child_count": (it.get("folder") or {}).get("childCount"),
                "mime": (it.get("file") or {}).get("mimeType"),
                "modified": it.get("lastModifiedDateTime"),
                "path": _item_path(it), "web_url": it.get("webUrl"),
            })
        url = data.get("@odata.nextLink")
    out.sort(key=lambda x: (not x["is_folder"], x["name"].lower()))
    return out


def walk_files(token: str, drive_id: str, item_id: str | None, depth: int = 0,
               modified_after: str | None = None) -> list[dict]:
    """Recurse a folder subtree (depth ≤ 12) into files, optionally filtered by modified time."""
    if depth > 12:
        return []
    files = []
    for it in list_children(token, drive_id, item_id):
        if it["is_folder"]:
            files.extend(walk_files(token, drive_id, it["id"], depth + 1, modified_after))
        elif not modified_after or (it.get("modified") or "") > modified_after:
            files.append(it)
    return files


def download(token: str, drive_id: str, item_id: str) -> bytes:
    r = requests.get(f"{GRAPH}/drives/{drive_id}/items/{item_id}/content",
                     headers={"Authorization": f"Bearer {token}"}, timeout=180)
    r.raise_for_status()
    return r.content


# ─────────────────────────────────────────────────────────────────────── import ──

def import_file(token: str, drive_id: str, item: dict, *, created_by: str, source_id: str,
                site_id=None, site_name=None, business_unit=None, document_type=None,
                department=None) -> dict:
    data = download(token, drive_id, item["id"])
    return ingest.register_bytes(
        data, item["name"], item.get("mime"),
        source_id=source_id, source_ref=f"{drive_id}/{item['id']}", created_by=created_by,
        subdir=f"sharepoint/{source_id}", business_unit=business_unit,
        document_type=document_type, department=department, file_modified_at=item.get("modified"),
        sp_site_id=site_id, sp_site_name=site_name, sp_drive_id=drive_id,
        sp_path=item.get("path"), sp_web_url=item.get("web_url"),
    )


def import_selection(email: str, drive_id: str, selections: list[dict], *, source_id: str,
                     site_id=None, site_name=None, business_unit=None, document_type=None,
                     department=None) -> dict:
    """Import a mix of files and folders (folders are walked). Returns a summary.

    Synchronous, in-process. Fine for a handful of files; for larger selections use
    enqueue_import (below) so the download+register work runs in the processing job and
    doesn't hit the web request timeout.
    """
    token = access_token_for(email)
    new = dup = 0
    for sel in selections:
        items = [sel] if not sel.get("is_folder") else walk_files(token, drive_id, sel["id"])
        for it in items:
            r = import_file(token, drive_id, it, created_by=email, source_id=source_id,
                            site_id=site_id, site_name=site_name, business_unit=business_unit,
                            document_type=document_type, department=department)
            if r["status"] == "new":
                new += 1
            else:
                dup += 1
    return {"imported": new, "duplicates": dup}


# ─────────────────────────────────────────────────── queued import (off-app) ──

def enqueue_import(email: str, drive_id: str, selections: list[dict], *, source_id: str,
                   site_id=None, site_name=None, drive_name=None,
                   business_unit=None, document_type=None, department=None) -> str:
    """Record an import request for the processing job to fulfil, and return its id.

    The web request returns immediately; the job claims the row with a lease, walks any
    folders, downloads + registers each file (dedup by SHA-256, so re-runs are safe), and
    updates progress counters. This keeps bulk imports off the 120s gunicorn request.
    """
    import json
    req_id = "imp_" + uuid.uuid4().hex[:12]
    execute(
        f"INSERT INTO {config.IMPORT_JOBS} "
        f"(id, user_email, drive_id, selections, source_id, site_id, site_name, drive_name, "
        f" business_unit, document_type, department, status, total_files, imported, duplicates, "
        f" errors, created_at, updated_at) "
        f"VALUES ({lit(req_id)}, {lit(email)}, {lit(drive_id)}, {lit(json.dumps(selections))}, "
        f"{lit(source_id)}, {lit(site_id)}, {lit(site_name)}, {lit(drive_name)}, "
        f"{lit(business_unit)}, {lit(document_type)}, {lit(department)}, "
        f"'queued', NULL, 0, 0, 0, current_timestamp(), current_timestamp())"
    )
    return req_id


def import_job_status(req_id: str) -> dict | None:
    rows = query(
        f"SELECT id, status, total_files, imported, duplicates, errors, last_error "
        f"FROM {config.IMPORT_JOBS} WHERE id = {lit(req_id)} LIMIT 1"
    )
    return rows[0] if rows else None


def recent_import_jobs(email: str, limit: int = 10) -> list[dict]:
    return query(
        f"SELECT id, status, total_files, imported, duplicates, errors, last_error, "
        f"unix_timestamp(created_at) AS created "
        f"FROM {config.IMPORT_JOBS} WHERE lower(user_email) = {lit(email.lower())} "
        f"ORDER BY created_at DESC LIMIT {int(limit)}"
    )


# ──────────────────────────────────────────────────────────────────── auto-sync ──

def ensure_source(site_name, drive_name, folder_name, cfg_json, created_by) -> str:
    source_id = "sp_" + uuid.uuid4().hex[:12]
    execute(
        f"INSERT INTO {config.SOURCES} (source_id, kind, display_name, config, enabled, created_by, created_at) "
        f"VALUES ({lit(source_id)}, 'sharepoint', {lit(f'{site_name} / {drive_name} / {folder_name}')}, "
        f"{lit(cfg_json)}, true, {lit(created_by)}, current_timestamp())"
    )
    return source_id


def arm_sync(email: str, sel: dict) -> dict:
    """Persist an auto-sync for a folder, storing the user's refresh token (encrypted)."""
    if not encryption_available():
        raise SPReauth("encryption unavailable")
    s = _session(email)
    if not s or not s.get("refresh_token_enc"):
        raise SPReauth("connect with a refreshable session first")
    import json
    cfg_json = json.dumps({"site_id": sel["site_id"], "drive_id": sel["drive_id"],
                           "folder_id": sel.get("folder_id")})
    source_id = ensure_source(sel.get("site_name", ""), sel.get("drive_name", ""),
                              sel.get("folder_name", "root"), cfg_json, email)
    sync_id = "sync_" + uuid.uuid4().hex[:12]
    execute(
        f"INSERT INTO {config.SHAREPOINT_SYNCS} "
        f"(id, source_id, site_id, site_name, drive_id, drive_name, folder_id, folder_name, "
        f" business_unit, department, document_type, user_email, refresh_token_enc, token_status, "
        f" created_at, created_by, last_synced_at) VALUES ("
        f"{lit(sync_id)}, {lit(source_id)}, {lit(sel['site_id'])}, {lit(sel.get('site_name'))}, "
        f"{lit(sel['drive_id'])}, {lit(sel.get('drive_name'))}, {lit(sel.get('folder_id'))}, "
        f"{lit(sel.get('folder_name'))}, {lit(sel.get('business_unit'))}, {lit(sel.get('department'))}, "
        f"{lit(sel.get('document_type'))}, {lit(email)}, {lit(s['refresh_token_enc'])}, 'ok', "
        f"current_timestamp(), {lit(email)}, NULL)"
    )
    return {"id": sync_id, "source_id": source_id}


def list_syncs(site_ids: list[str] | None = None) -> list[dict]:
    where = "1=1"
    if site_ids is not None:
        if not site_ids:
            where = "1=0"
        else:
            vals = ",".join(lit(s) for s in site_ids)
            where = f"(site_id IS NULL OR site_id IN ({vals}))"
    return query(
        f"SELECT id, source_id, site_name, drive_name, folder_name, document_type, "
        f"user_email, token_status, last_error, unix_timestamp(last_synced_at) AS last_synced, "
        f"unix_timestamp(created_at) AS created FROM {config.SHAREPOINT_SYNCS} WHERE {where} "
        f"ORDER BY created_at DESC"
    )


def remove_sync(sync_id: str) -> None:
    execute(f"DELETE FROM {config.SHAREPOINT_SYNCS} WHERE id = {lit(sync_id)}")


def reconnect_sync(sync_id: str, email: str) -> None:
    """Re-arm a dead sync with the reconnecting user's fresh refresh token."""
    s = _session(email)
    if not s or not s.get("refresh_token_enc"):
        raise SPReauth("connect first")
    execute(
        f"UPDATE {config.SHAREPOINT_SYNCS} SET refresh_token_enc = {lit(s['refresh_token_enc'])}, "
        f"user_email = {lit(email)}, token_status = 'ok', last_error = NULL "
        f"WHERE id = {lit(sync_id)}"
    )
