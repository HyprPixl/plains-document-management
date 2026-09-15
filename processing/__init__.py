"""Document Hub processing package — runs on Databricks compute (a Job), never in the web process.

Modules:
  graph.py   — SharePoint / Microsoft Graph client (service-principal client-credentials)
  ocr.py     — native text check + Azure Document Intelligence (prebuilt-read → searchable PDF + page text)
  extract.py — admin-schema-driven field extraction via ai_query on the SQL warehouse
  job.py     — claim → process → commit loop; idempotent, concurrency-safe, resumable (SPEC §4A, §10)
"""
