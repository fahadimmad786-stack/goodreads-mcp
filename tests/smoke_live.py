"""
Live smoke run: calls every tool against BigQuery and prints a compact
summary. Needs Application Default Credentials. Run with:

    .venv/bin/python tests/smoke_live.py
"""

from __future__ import annotations

import json
import sys

from goodreads_mcp import server


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
