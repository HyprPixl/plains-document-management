#!/usr/bin/env python3
"""Client-side latency benchmark for Document Hub's hot endpoints.

Hits the deployed app over HTTP as a plain client (no warehouse access needed here) and
records p50/p95/max latency per endpoint over N samples. Writes a timestamped JSON blob
and a short markdown table under bench/results/ so runs are comparable over time — these
are the Phase 1 baselines the Lakebase/optimization work (Phase 2/3) is measured against.

Stdlib only (urllib) so it runs anywhere with no extra deps. See bench/README.md for the
exact "run on deploy" invocation and auth.

Endpoints exercised (roadmap Phase 1): /api/stats, /api/documents, /api/search,
and /api/documents/<doc_id> (a doc id is auto-discovered from /api/documents unless
--doc-id is given).
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE_URL = "https://plains-document-management-1979327425712808.8.azure.databricksapps.com"
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def _headers(token: str | None, extra: list[str]) -> dict:
    """Build request headers. Databricks Apps sit behind SSO — pass a bearer token
    (OAuth/PAT) via --token/env, or an arbitrary --header 'Name: value' for other schemes."""
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    for raw in extra:
        name, _, value = raw.partition(":")
        if value:
            h[name.strip()] = value.strip()
    return h


def _get(url: str, headers: dict, timeout: float):
    """Return (status_code, elapsed_ms, body_bytes). Raises on transport failure."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (time.perf_counter() - t0) * 1000, body
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t0) * 1000, e.read()


def _pct(samples: list[float], p: float) -> float:
    """Nearest-rank percentile over sorted samples (p in 0..100)."""
    if not samples:
        return 0.0
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(round((p / 100) * len(s) + 0.5)) - 1))
    return s[k]


def _bench_endpoint(name: str, url: str, headers: dict, n: int, timeout: float) -> dict:
    """Sample one endpoint n times; collect latency percentiles + a status breakdown."""
    latencies, statuses = [], {}
    for _ in range(n):
        try:
            code, ms, _ = _get(url, headers, timeout)
        except Exception as exc:  # transport/timeout — record and keep going
            statuses["error"] = statuses.get("error", 0) + 1
            statuses[f"error:{type(exc).__name__}"] = statuses.get(f"error:{type(exc).__name__}", 0) + 1
            continue
        latencies.append(ms)
        statuses[str(code)] = statuses.get(str(code), 0) + 1
    return {
        "name": name,
        "url": url,
        "samples": len(latencies),
        "statuses": statuses,
        "p50_ms": round(_pct(latencies, 50), 1) if latencies else None,
        "p95_ms": round(_pct(latencies, 95), 1) if latencies else None,
        "max_ms": round(max(latencies), 1) if latencies else None,
        "mean_ms": round(statistics.mean(latencies), 1) if latencies else None,
    }


def _discover_doc_id(base_url: str, headers: dict, timeout: float) -> str | None:
    """Pull one doc_id from /api/documents so the detail endpoint can be benched."""
    try:
        code, _, body = _get(f"{base_url}/api/documents?limit=1", headers, timeout)
        if code != 200:
            return None
        rows = json.loads(body)
        if isinstance(rows, list) and rows:
            return rows[0].get("doc_id")
    except Exception:
        return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Latency benchmark for Document Hub hot endpoints.")
    ap.add_argument("--base-url", default=os.getenv("DOC_HUB_BASE_URL", DEFAULT_BASE_URL),
                    help="Deployed app base URL (default: the production app).")
    ap.add_argument("-n", "--samples", type=int, default=int(os.getenv("DOC_HUB_BENCH_N", "30")),
                    help="Requests per endpoint (default 30).")
    ap.add_argument("--token", default=os.getenv("DOC_HUB_BENCH_TOKEN"),
                    help="Bearer token for Databricks Apps SSO (or set DOC_HUB_BENCH_TOKEN).")
    ap.add_argument("--header", action="append", default=[], metavar="NAME: value",
                    help="Extra request header; repeatable (for non-bearer auth schemes).")
    ap.add_argument("--query", default="contract", help="Search term for /api/search.")
    ap.add_argument("--doc-id", default=None, help="doc_id for the detail endpoint (else auto-discovered).")
    ap.add_argument("--timeout", type=float, default=130.0, help="Per-request timeout (s).")
    ap.add_argument("--label", default="", help="Optional label recorded in the output (e.g. 'pre-lakebase').")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    headers = _headers(args.token, args.header)

    doc_id = args.doc_id or _discover_doc_id(base, headers, args.timeout)
    if not doc_id:
        print("warn: no doc_id (arg or discovery) — skipping /api/documents/<doc_id>", file=sys.stderr)

    import urllib.parse
    targets = [
        ("stats", f"{base}/api/stats"),
        ("documents", f"{base}/api/documents"),
        ("search", f"{base}/api/search?q={urllib.parse.quote(args.query)}"),
    ]
    if doc_id:
        targets.append(("document_detail", f"{base}/api/documents/{urllib.parse.quote(str(doc_id))}"))

    print(f"Benchmarking {base} — {args.samples} samples/endpoint"
          + (f" [{args.label}]" if args.label else ""), file=sys.stderr)
    results = []
    for name, url in targets:
        r = _bench_endpoint(name, url, headers, args.samples, args.timeout)
        results.append(r)
        print(f"  {name:16s} p50={r['p50_ms']} p95={r['p95_ms']} max={r['max_ms']} "
              f"statuses={r['statuses']}", file=sys.stderr)

    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base,
        "samples_per_endpoint": args.samples,
        "label": args.label,
        "authenticated": bool(args.token or args.header),
        "results": results,
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = os.path.join(RESULTS_DIR, f"{stamp}.json")
    md_path = os.path.join(RESULTS_DIR, f"{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_markdown(report))

    print(f"\nWrote {json_path}\n      {md_path}", file=sys.stderr)
    return 0


def _markdown(report: dict) -> str:
    lines = [
        f"# Document Hub bench — {report['captured_at']}",
        "",
        f"- Base URL: `{report['base_url']}`",
        f"- Samples/endpoint: {report['samples_per_endpoint']}",
        f"- Authenticated: {report['authenticated']}",
    ]
    if report.get("label"):
        lines.append(f"- Label: {report['label']}")
    lines += [
        "",
        "| Endpoint | p50 (ms) | p95 (ms) | max (ms) | mean (ms) | samples | statuses |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        lines.append(
            f"| {r['name']} | {r['p50_ms']} | {r['p95_ms']} | {r['max_ms']} | "
            f"{r['mean_ms']} | {r['samples']} | {r['statuses']} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
