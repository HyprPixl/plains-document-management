"""Central configuration, read from environment (see app.yaml)."""
import os

CATALOG = os.getenv("CATALOG", "product_dev")
SCHEMA = os.getenv("SCHEMA", "document_hub")
FQ = f"{CATALOG}.{SCHEMA}"  # fully-qualified schema prefix

DOCS_VOLUME = os.getenv("DOCS_VOLUME", f"/Volumes/{CATALOG}/{SCHEMA}/docs")
SQL_WAREHOUSE_ID = os.getenv("SQL_WAREHOUSE_ID", "4d7f25b1bd5fddf1")

# Structured app/job logs go to stdout, which the Databricks App/Job log captures
# (pattern from contract-explorer / contracts-ver). LOG_LEVEL tunes verbosity.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Per-statement timing in db.py logs any warehouse call slower than this (ms) at WARNING,
# so slow queries surface in the app/job log. Baseline knob for the Phase 2/3 optimization
# work — tune down to catch more, up to quiet a known-slow path.
SLOW_QUERY_MS = int(os.getenv("SLOW_QUERY_MS", "1000"))

# ai_query (batch inference) doesn't support the newest sonnet-5/opus-5 endpoints yet;
# sonnet-4-5 is the current model that works with ai_query batch calls.
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "databricks-claude-sonnet-4-5")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")

# Processing job / external services (used by processing/*, not the web app)
DI_ENDPOINT = os.getenv("DI_ENDPOINT", "https://westus2.api.cognitive.microsoft.com/")
DI_SECRET_SCOPE = os.getenv("DI_SECRET_SCOPE", "pna-wu2-dm-dev-data-keyv")
DI_SECRET_KEY = os.getenv("DI_SECRET_KEY", "pna-wu2-datamgt-dev-di01--key")
SP_SECRET_SCOPE = os.getenv("SP_SECRET_SCOPE", "pna-wu2-dm-dev-data-keyv")
SP_CLIENT_ID_KEY = os.getenv("SP_CLIENT_ID_KEY", "sharepointspn--clientid")
SP_CLIENT_SECRET_KEY = os.getenv("SP_CLIENT_SECRET_KEY", "sharepointspn--clientsecret")
SP_TENANT_ID_KEY = os.getenv("SP_TENANT_ID_KEY", "plains--tenant-id")

# Delegated (on-behalf-of-user) SharePoint — auth-code flow, reuses the plains-nexus
# app registration (appId bf2ea6db-…, tenant e3267a76-…). Distinct from the app-only
# SPN above. Scopes are already admin-consented tenant-wide. See SHAREPOINT_CONNECTION.md.
SP_DELEG_SECRET_SCOPE = os.getenv("SP_DELEG_SECRET_SCOPE", "pna-wu2-dm-dev-data-keyv")
SP_DELEG_CLIENT_ID_KEY = os.getenv("SP_DELEG_CLIENT_ID_KEY", "pna-datamgmt-terraform-appreg-client-id")
SP_DELEG_CLIENT_SECRET_KEY = os.getenv("SP_DELEG_CLIENT_SECRET_KEY", "pna-datamgmt-terraform-appreg-client-secret")
SP_DELEG_TENANT_ID_KEY = os.getenv("SP_DELEG_TENANT_ID_KEY", "plains-tenant-id")
SP_DELEG_SCOPES = os.getenv("SP_DELEG_SCOPES", "openid profile offline_access Files.Read.All Sites.Read.All")
# Explicit public base URL of the deployed app (else derived from X-Forwarded-* headers).
APP_BASE_URL = os.getenv("APP_BASE_URL", "")
SP_SYNC_INTERVAL = int(os.getenv("SP_SYNC_INTERVAL", "1800"))  # delegated sync cadence (sec)

# Processing job id — the web app best-effort triggers a run when work is enqueued
# (import queued) so it doesn't wait for the schedule. Empty = no auto-trigger.
PROCESSING_JOB_ID = os.getenv("PROCESSING_JOB_ID", "")

# Work-claim lease length (seconds) for the processing job
CLAIM_LEASE_SECONDS = int(os.getenv("CLAIM_LEASE_SECONDS", "600"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "5"))

# Table names
def t(name: str) -> str:
    return f"{FQ}.{name}"

DOCUMENTS = t("documents")
DOCUMENT_FIELDS = t("document_fields")
FIELD_DEFS = t("field_defs")
TAXONOMY = t("taxonomy")
SOURCES = t("sources")
DOCUMENT_LINKS = t("document_links")
DOCUMENT_TEXT = t("document_text")
EXTRACTION_CACHE = t("extraction_cache")
PERMISSIONS = t("permissions")
JOB_STATE = t("job_state")
AUDIT_LOG = t("audit_log")
SP_OAUTH_STATE = t("sp_oauth_state")
SP_SESSIONS = t("sp_sessions")
SHAREPOINT_SYNCS = t("sharepoint_syncs")
IMPORT_JOBS = t("import_jobs")
DOCUMENT_TAGS = t("document_tags")
