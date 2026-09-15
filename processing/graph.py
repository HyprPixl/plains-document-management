"""Microsoft Graph client for SharePoint sources.

Uses the SharePoint service-principal (client-credentials flow). Read-only for v1:
we pull originals from connected SharePoint sites into the UC volume; the one-way
mirror back to SharePoint is a separate concern (SPEC §13).

Credentials come from the secret scope configured in config.py.
"""
import time
import requests

import config
from . import secrets

GRAPH = "https://graph.microsoft.com/v1.0"
_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

_token = None
_token_exp = 0.0


def _acquire_token() -> str:
    tenant = secrets.get(config.SP_SECRET_SCOPE, config.SP_TENANT_ID_KEY)
    client_id = secrets.get(config.SP_SECRET_SCOPE, config.SP_CLIENT_ID_KEY)
    client_secret = secrets.get(config.SP_SECRET_SCOPE, config.SP_CLIENT_SECRET_KEY)
    resp = requests.post(
        _TOKEN_URL.format(tenant=tenant),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    resp.raise_for_status()
    tok = resp.json()
    global _token, _token_exp
    _token = tok["access_token"]
    _token_exp = time.time() + int(tok.get("expires_in", 3600)) - 300  # refresh 5 min early
    return _token


def _headers() -> dict:
    if not _token or time.time() >= _token_exp:
        _acquire_token()
    return {"Authorization": f"Bearer {_token}", "Accept": "application/json"}


def _get(url: str, **kw):
    r = requests.get(url, headers=_headers(), timeout=60, **kw)
    if r.status_code == 401:  # token raced expiry — refresh once
        _acquire_token()
        r = requests.get(url, headers=_headers(), timeout=60, **kw)
    r.raise_for_status()
    return r


def resolve_site_drive(cfg: dict) -> tuple[str, str]:
    """Resolve a source config to (site_id, default drive_id).

    App-only (client-credentials) tokens can't use `/sites?search=`, so we resolve by
    the server-relative path: `{hostname}:{site_path}` (e.g.
    plainsmidstream.sharepoint.com:/teams/LandRecordsResearch-UsrGrp). Falls back to a
    bare `site` value if it already looks like a full path.
    """
    hostname = cfg.get("hostname", "plainsmidstream.sharepoint.com")
    site_path = cfg.get("site_path")
    if site_path:
        addr = f"{hostname}:{site_path if site_path.startswith('/') else '/' + site_path}"
    else:
        site = cfg.get("site", "")
        addr = site if (":" in site) else f"{hostname}:/teams/{site}"
    resp = _get(f"{GRAPH}/sites/{addr}")
    site = resp.json()
    if "id" not in site:
        raise RuntimeError(f"SharePoint site not found: {addr} ({site.get('error')})")
    drive = _get(f"{GRAPH}/sites/{site['id']}/drive").json()
    return site["id"], drive["id"]


def _item_by_path(drive_id: str, folder_path: str) -> dict:
    if not folder_path or folder_path == "/":
        return _get(f"{GRAPH}/drives/{drive_id}/root").json()
    return _get(f"{GRAPH}/drives/{drive_id}/root:/{folder_path.strip('/')}").json()


def list_files(drive_id: str, folder_path: str = "", exts: tuple = (".pdf",)) -> list[dict]:
    """Recursively list files under a folder. Returns normalized item dicts."""
    root = _item_by_path(drive_id, folder_path)
    out: list[dict] = []

    def walk(item_id: str, rel: str):
        url = f"{GRAPH}/drives/{drive_id}/items/{item_id}/children"
        while url:
            page = _get(url).json()
            for it in page.get("value", []):
                name = it["name"]
                sub = f"{rel}/{name}" if rel else name
                if "folder" in it:
                    walk(it["id"], sub)
                elif not exts or name.lower().endswith(exts):
                    out.append({
                        "item_id": it["id"],
                        "drive_id": drive_id,
                        "name": name,
                        "rel_path": sub,
                        "size": it.get("size", 0),
                        "etag": it.get("eTag"),
                        "modified": (it.get("lastModifiedDateTime")),
                        "mime": (it.get("file", {}) or {}).get("mimeType"),
                    })
            url = page.get("@odata.nextLink")

    walk(root["id"], "")
    return out


def download(drive_id: str, item_id: str) -> bytes:
    return _get(f"{GRAPH}/drives/{drive_id}/items/{item_id}/content").content
