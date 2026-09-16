# bench/ — latency baselines for the hot endpoints

`bench.py` is a **client-side** HTTP benchmark. It hits the deployed Document Hub app as an
ordinary client (no warehouse creds needed on the machine running it) and records p50/p95/max
latency for the hot endpoints. The numbers it writes are the **Phase 1 baselines** that the
Lakebase migration and the optimization pass (Phase 2/3) are measured against — capture a run
*before* those changes, and again after, to prove the win is real.

Stdlib only (`urllib`) — no `pip install` required. Runs on any Python 3.10+.

## Endpoints exercised

- `GET /api/stats`
- `GET /api/documents`
- `GET /api/search?q=<term>`
- `GET /api/documents/<doc_id>` — the doc id is auto-discovered from `/api/documents`
  (or pass `--doc-id`).

## Output

Two timestamped files per run under `bench/results/` (gitignored except this README):

- `<UTC-timestamp>.json` — full machine-readable report (per-endpoint p50/p95/max/mean, sample
  count, HTTP status breakdown, base URL, label).
- `<UTC-timestamp>.md`   — a short markdown table for eyeballing / pasting into a PR.

## Auth — the app is behind Databricks Apps SSO

Requests need to authenticate to the Databricks Apps front door. Supply a **bearer token** via
`--token` or the `DOC_HUB_BENCH_TOKEN` env var (it is sent as `Authorization: Bearer <token>`).
A Databricks OAuth token or PAT for a principal that can reach the app works:

```bash
# short-lived OAuth token for the current CLI profile
export DOC_HUB_BENCH_TOKEN="$(databricks auth token --host https://<workspace-host> | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')"
```

For any non-bearer scheme, pass raw headers instead (repeatable): `--header 'Cookie: ...'`.
**Never hardcode a token** — always via env/CLI arg. If a run comes back all `401`/`403`, the
token is missing or can't reach the app.

## Run it on deploy (the one-liner)

```bash
# from the repo root, against the production app (default --base-url):
DOC_HUB_BENCH_TOKEN=<token> python bench/bench.py -n 30 --label pre-lakebase
```

Common overrides:

```bash
python bench/bench.py \
  --base-url https://plains-document-management-1979327425712808.8.azure.databricksapps.com \
  -n 50 \
  --query "master service agreement" \
  --doc-id <known_doc_id> \
  --token <token> \
  --label baseline-2026-09-16
```

- `--base-url` defaults to the production app (also settable via `DOC_HUB_BASE_URL`).
- `-n/--samples` requests per endpoint (default 30; also `DOC_HUB_BENCH_N`).
- `--label` is stamped into the output so you can tell runs apart (e.g. `pre-lakebase`).

Progress prints to stderr; the JSON + markdown land in `bench/results/`.

## Complementary server-side signal

Per-request timing and slow-query lines are also emitted to the **app log** (stdout, captured by
Databricks Apps): every request logs `duration_ms`, and any warehouse statement slower than
`SLOW_QUERY_MS` (default 1000ms, set in `app.yaml`) logs at WARNING. Use the app log to see
*where* the time goes; use this bench for stable client-observed percentiles over time.
