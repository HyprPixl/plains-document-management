"""Admin-schema-driven field extraction.

The set of fields is not hard-coded — it is whatever field_defs says applies to the
document's type (common + type-specific). We build a prompt from those defs and ask
the model (via ai_query on the SQL warehouse) for a JSON object keyed by field_key.

Results are cached by (content_sha256, prompt_version, document_type) so re-processing
the same bytes with the same schema is free and deterministic (SPEC §10.3).
"""
import json

import config
from db import query, execute, lit

MAX_TEXT_CHARS = 60000  # cap prompt size; extend later with chunk/summarize if needed


def field_specs(document_type: str) -> list[dict]:
    return query(
        f"SELECT field_key, label, data_type, extraction_prompt_hint, picklist_source "
        f"FROM {config.FIELD_DEFS} WHERE active = true AND "
        f"(applies_to = 'common' OR applies_to = {lit(document_type)}) ORDER BY sort_order"
    )


def _build_prompt(document_type: str, specs: list[dict], text: str) -> str:
    lines = []
    for s in specs:
        desc = s.get("extraction_prompt_hint") or s["label"]
        if s.get("picklist_source") and "|" in str(s["picklist_source"]):
            desc += " (one of: " + ", ".join(str(s["picklist_source"]).split("|")) + ")"
        elif s.get("data_type") == "date":
            desc += " (ISO date YYYY-MM-DD)"
        lines.append(f'  "{s["field_key"]}": {desc}')
    schema_block = "{\n" + ",\n".join(lines) + "\n}"
    return (
        f"You extract structured metadata from a business document of type "
        f"'{document_type or 'unknown'}'. Return ONLY a JSON object with exactly these keys; "
        f"use null for anything not present in the text. Do not invent values.\n\n"
        f"Keys and what they mean:\n{schema_block}\n\n"
        f"Document text:\n\"\"\"\n{text[:MAX_TEXT_CHARS]}\n\"\"\""
    )


def _cached(content_sha256: str, document_type: str) -> dict | None:
    rows = query(
        f"SELECT proposed_json FROM {config.EXTRACTION_CACHE} "
        f"WHERE content_sha256 = {lit(content_sha256)} AND prompt_version = {lit(config.PROMPT_VERSION)} "
        f"AND document_type = {lit(document_type)} LIMIT 1"
    )
    if rows:
        try:
            return json.loads(rows[0]["proposed_json"])
        except Exception:
            return None
    return None


def extract_fields(document_type: str, text: str, content_sha256: str) -> dict:
    """Return {field_key: proposed_value}. Read-through cache; ai_query on cache miss."""
    specs = field_specs(document_type)
    if not specs or not (text or "").strip():
        return {}

    cached = _cached(content_sha256, document_type)
    if cached is not None:
        return cached

    prompt = _build_prompt(document_type, specs, text)
    rows = query(
        f"SELECT ai_query({lit(config.EXTRACT_MODEL)}, {lit(prompt)}) AS out", timeout_s=300
    )
    raw = (rows[0]["out"] if rows else "") or ""
    proposed = _parse_json_object(raw)
    # keep only known keys
    valid = {k: v for k, v in proposed.items() if k in {s["field_key"] for s in specs}}

    execute(
        f"INSERT INTO {config.EXTRACTION_CACHE} "
        f"(content_sha256, prompt_version, document_type, proposed_json, created_at) "
        f"VALUES ({lit(content_sha256)}, {lit(config.PROMPT_VERSION)}, {lit(document_type)}, "
        f"{lit(json.dumps(valid))}, current_timestamp())"
    )
    return valid


def _parse_json_object(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):] if "{" in raw else raw
    try:
        return json.loads(raw)
    except Exception:
        s, e = raw.find("{"), raw.rfind("}")
        if 0 <= s < e:
            try:
                return json.loads(raw[s:e + 1])
            except Exception:
                pass
    return {}
