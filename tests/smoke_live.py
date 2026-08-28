"""
Live smoke run: calls every tool against BigQuery and prints a compact
summary. Needs Application Default Credentials. Run with:

    .venv/bin/python tests/smoke_live.py
"""

from __future__ import annotations

import json
import sys
import time

from goodreads_mcp import server, telemetry


def call(name, **kw):
    fn = getattr(server, name)
    fn = getattr(fn, "fn", fn)
    return name, fn(**kw)


CALLS = [
    ("dataset_overview", {}),
    ("rating_distribution", {"min_ratings": 100, "bucket_size": 0.5}),
    ("top_books_by_rating", {"min_ratings": 5000, "limit": 5}),
    ("top_books_by_rating", {"min_ratings": 5000, "limit": 5, "direction": "asc"}),
    ("top_books_by_rating", {"min_ratings": 5000, "limit": 5, "unit": "works"}),
    ("stats_by_language", {"min_ratings": 100, "limit": 8}),
    ("stats_by_language", {"min_ratings": 100, "limit": 8, "unit": "works"}),
    ("stats_by_year", {"year_from": 2015, "year_to": 2020, "min_ratings": 100}),
    ("stats_by_year", {"year_from": 2015, "year_to": 2020, "min_ratings": 100, "unit": "works"}),
    ("stats_by_publisher", {"min_ratings": 100, "min_books": 50, "limit": 5}),
    ("stats_by_publisher", {"min_ratings": 100, "min_books": 50, "limit": 5, "unit": "works"}),
    ("stats_by_author", {"min_ratings": 100, "min_books": 10, "limit": 5}),
    ("stats_by_author", {"min_ratings": 100, "min_books": 10, "limit": 5, "unit": "editions"}),
    ("page_count_stats", {"min_ratings": 100}),
    ("publish_month_seasonality", {"min_ratings": 100}),
    ("user_ratings_overview", {}),
    ("top_titles_by_user_ratings", {"min_ratings": 30, "limit": 5}),
    ("compare_user_vs_book_ratings", {"min_user_ratings": 30, "min_book_ratings": 1000, "limit": 5}),
]


def main() -> int:
    failures = []
    for name, kw in CALLS:
        try:
            _, out = call(name, **kw)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}{kw}: {type(exc).__name__}: {exc}")
            print(f"FAIL  {name} {kw}\n      {type(exc).__name__}: {exc}")
            continue
        if "error" in out:
            failures.append(f"{name}: {out['error']}")
            print(f"FAIL  {name}: {out['error']}")
            continue
        if not out.get("n"):
            failures.append(f"{name}: empty n")
        if not out.get("caveats"):
            failures.append(f"{name}: no caveats")
        print(f"OK    {name} {kw}")
        print(f"      n={json.dumps(out['n'])}")
        print(f"      caveats={len(out['caveats'])} bytes_billed={out['query_meta'].get('bytes_billed')}")
    # --- telemetry ---------------------------------------------------------
    # BigQuery bills 0 bytes for a cache hit and reports 0 bytes processed, so
    # "bytes_billed > 0" is vacuously false on a warm cache. One probe with a
    # parameter value that cannot already be cached forces a real miss, which
    # is what actually proves the job metadata is wired through.
    probe_kw = {"min_ratings": 1000 + int(time.time()) % 977, "limit": 3}
    n_expected = len(CALLS) + 1
    try:
        call("top_books_by_rating", **probe_kw)
        print(f"OK    cache-miss probe top_books_by_rating {probe_kw}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"cache-miss probe: {exc}")
        print(f"FAIL  cache-miss probe: {exc}")

    path = telemetry.log_path()
    print()
    if not telemetry.enabled():
        print("telemetry disabled (GOODREADS_TELEMETRY=0) -- skipping its checks")
    elif not path.exists():
        failures.append(f"telemetry: no log at {path}")
        print(f"FAIL  telemetry: no log written at {path}")
    else:
        lines = []
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    lines.append(json.loads(raw))   # raises if not valid JSON
        recent = lines[-n_expected:]
        probe = recent[-1]
        unpopulated = [r["tool"] for r in recent if r.get("bytes_billed") is None]
        with_jobs = [r for r in recent if r.get("job_ids")]
        misses = [r for r in recent if r.get("cache_hit") is False]

        if len(recent) != n_expected:
            failures.append(f"telemetry: {len(recent)} lines for {n_expected} calls")
        if unpopulated:
            failures.append(f"telemetry: bytes_billed is None for {unpopulated}")
        if not with_jobs:
            failures.append("telemetry: no BigQuery job id recorded")
        if probe.get("cache_hit") is not False:
            failures.append("telemetry: probe did not miss the cache; cannot verify billing")
        elif not (probe.get("bytes_billed") or 0) > 0:
            failures.append("telemetry: bytes_billed is 0 on a cache MISS")

        print(f"OK    telemetry {path}")
        print(f"      {len(recent)} lines, valid JSON, bytes_billed populated on all")
        print(f"      job ids on {len(with_jobs)}/{len(recent)}"
              f", cache misses {len(misses)}/{len(recent)}")
        print(f"      probe billed {probe.get('bytes_billed'):,} bytes"
              f" / processed {probe.get('bytes_processed'):,}"
              f" (cache_hit={probe.get('cache_hit')})")
        print(f"      total billed {sum(r.get('bytes_billed') or 0 for r in recent):,} bytes")

    print()
    if failures:
        print(f"{len(failures)} FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"all {len(CALLS)} live calls ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
