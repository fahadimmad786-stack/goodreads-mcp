"""
Shared SQL fragments, parameter validation and the result envelope.

Rules that apply to more than one tool live here so they are stated once:
the minimum-ratings floor, the dual-average expression, the title
normalisation used for the only cross-table join, and the shape every tool
returns.
"""

from __future__ import annotations

from typing import Any

from . import bq

# Hard ceiling on rows any tool will return. This is a stats server, not a
# row browser (DATA_NOTES.md query guidance).
MAX_LIMIT = 100

# Default minimum ratings for rankings. Retains 342,370 of 1,850,115 books
# (18.5%); the median book has only 5 ratings.
DEFAULT_MIN_RATINGS = 100


class ParamError(ValueError):
    """A tool was called with an argument that cannot be honoured safely."""


def require_min_ratings(value: int) -> int:
    """
    Validate a minimum-ratings threshold.

    The floor is 1, not 0. At 0 the 451,777 unrated books (stored as
    rating = 0.0) would enter every average and drag it toward zero.
    """
    value = int(value)
    if value < 1:
        raise ParamError(
            "min_ratings must be at least 1: at 0 the 451,777 books with no "
            "ratings (stored as rating = 0.0) enter the average and pull it "
            "toward zero. Use 100 for a meaningful ranking."
        )
    return value


def clamp_limit(value: int, cap: int = MAX_LIMIT) -> int:
    value = int(value)
    if value < 1:
        raise ParamError("limit must be at least 1")
    return min(value, cap)


def require_direction(value: str) -> str:
    v = str(value).lower().strip()
    if v not in ("desc", "asc"):
        raise ParamError("direction must be 'desc' (highest first) or 'asc' (lowest first)")
    return v


def title_norm(col: str) -> str:
    """
    Reproduce the cleaning script's normalise_title() in SQL.

    Lowercase, drop a trailing series suffix like ' (Harry Potter, #6)', strip
    punctuation, collapse whitespace. The character class is \\p{L}\\p{N}_ rather
    than \\w because RE2's \\w is ASCII-only while Python's is Unicode-aware;
    with the Unicode classes this reproduces the documented join coverage of
    52,016 matched titles exactly.
    """
    return (
        "TRIM(REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE("
        f"LOWER({col}), "
        r"r'\s*\([^()]*#\s*\d+[^()]*\)\s*$', ''), "
        r"r'[^\p{L}\p{N}_\s]', ' '), "
        r"r'\s+', ' '))"
    )


def work_key(col: str) -> str:
    """
    Identity of a WORK, for collapsing editions. Distinct from title_norm().

    title_norm() strips any trailing series suffix, which is right for the
    cross-table join -- it must reproduce the cleaning script's
    book_title_normalised exactly -- but wrong as a work identity, because it
    deletes the only thing separating one boxed set from another. "Harry
    Potter Boxed Set (Harry Potter, #1-5)" and "... (#1-7)" are different
    products and normalise to the same string.

    So this key keeps the series RANGE and nothing else:

        Harry Potter Boxed Set (Harry Potter, #1-5)  ->  ... boxed set #1-5
        Harry Potter Boxed Set (Harry Potter, #1-7)  ->  ... boxed set #1-7
        The Two Towers (The Lord of the Rings, #2)   ->  the two towers
        The Two Towers                               ->  the two towers

    Only a range (two numbers joined by a hyphen or en dash) is preserved. A
    single volume number is series metadata about the same work, not a
    different product, so it is dropped -- keeping it would split an edition
    that carries the suffix from one that does not, which is a regression the
    old key did not have.

    The range is appended after punctuation stripping, so its '#' and '-'
    survive a pass that would otherwise remove them.
    """
    ref = (
        "REGEXP_REPLACE(REGEXP_EXTRACT("
        f"LOWER({col}), "
        r"r'\([^()]*#\s*(\d+\s*[-–]\s*\d+)[^()]*\)\s*$'), "
        r"r'\s+', '')"
    )
    return f"CONCAT({title_norm(col)}, IFNULL(CONCAT(' #', {ref}), ''))"


# Dual averages, per the design decision to always report both.
#   avg_book_rating -- mean of each book's own mean; every book counts once.
#   pooled_rating   -- total stars / total ratings; popular books dominate.
# rating_dist_1..5 sums exactly to rating_dist_total on all 1,850,115 rows, so
# the pooled figure is exact rather than reconstructed.
#
# n_distinct_titles and editions_per_title expose the edition-duplication
# defect: a row here is an edition, and every edition of a work carries that
# WORK's full rating total, not its own. Five rows of Crichton's "The Lost
# World" each carry ~117,000 ratings and an identical 3.78. So n_ratings
# double-counts, and pooled_rating over-weights works with many editions.
# editions_per_title says by how much, for this particular group.
_WORK_KEY = work_key("name")

RATING_AGGS = f"""
    COUNT(*) AS n_books,
    APPROX_COUNT_DISTINCT({_WORK_KEY}) AS n_distinct_titles,
    ROUND(SAFE_DIVIDE(COUNT(*), APPROX_COUNT_DISTINCT({_WORK_KEY})), 3)
      AS editions_per_title,
    SUM(rating_dist_total) AS n_ratings,
    ROUND(AVG(rating), 4) AS avg_book_rating,
    ROUND(SAFE_DIVIDE(
      SUM(rating_dist_1 * 1 + rating_dist_2 * 2 + rating_dist_3 * 3
          + rating_dist_4 * 4 + rating_dist_5 * 5),
      SUM(rating_dist_total)), 4) AS pooled_rating
"""


# --------------------------------------------------------------------------
# Work-level deduplication
# --------------------------------------------------------------------------
#
# A row in `books` is an EDITION. `unit` selects what one row of a grouped
# result counts:
#
#   "editions" -- the table as stored. Correct for "most prolific publisher":
#                 a publisher that issued five editions of one work did five
#                 editions' worth of work.
#   "works"    -- editions sharing a normalised title collapse to one row.
#                 Correct for "most-read author": a work read once should not
#                 count five times because it has five editions.
#
# Collapsing picks a single REPRESENTATIVE edition per work -- the one with the
# largest rating_dist_total -- rather than summing. Summing would be badly
# wrong: editions of one work largely repeat the same rating pool. Measured on
# works with more than one edition at rating_dist_total >= 100, 62,794 of
# 68,921 (91%) have editions whose totals differ, mean relative spread 8.6%.
# So the true work total is not recoverable from this data: SUM overcounts,
# MAX undercounts. MAX is the conservative choice and is what is used here.
#
# The whole representative row is carried through as a struct rather than
# taking MAX() of each column independently, which would splice figures from
# different editions together.

UNITS = ("editions", "works")


def require_unit(value: str) -> str:
    v = str(value).strip().lower()
    if v not in UNITS:
        raise ParamError(
            "unit must be 'editions' (one row per edition, as stored in the "
            "table) or 'works' (editions sharing a normalised title collapsed "
            "to one representative row)"
        )
    return v


# The columns of the representative edition that survive the collapse.
_REP_STRUCT = """STRUCT(
          rating AS rating,
          rating_dist_total AS rt,
          rating_dist_1 AS d1, rating_dist_2 AS d2, rating_dist_3 AS d3,
          rating_dist_4 AS d4, rating_dist_5 AS d5
        )"""

# Deterministic pick: largest rating total, ties broken by the lowest id so the
# same edition is chosen on every run.
_REP_PICK = f"""ARRAY_AGG({_REP_STRUCT}
        ORDER BY rating_dist_total DESC, id ASC LIMIT 1)[OFFSET(0)] AS rep"""


# Mirrors RATING_AGGS, but over one row per work rather than per edition.
# n_books counts works here; n_edition_rows reports how many table rows those
# works were collapsed from, so the caller can see the size of the collapse.
RATING_AGGS_WORKS = """
    COUNT(*) AS n_books,
    COUNT(*) AS n_distinct_titles,
    ROUND(SAFE_DIVIDE(SUM(n_editions), COUNT(*)), 3) AS editions_per_title,
    SUM(n_editions) AS n_edition_rows,
    SUM(rep.rt) AS n_ratings,
    ROUND(AVG(rep.rating), 4) AS avg_book_rating,
    ROUND(SAFE_DIVIDE(
      SUM(rep.d1 * 1 + rep.d2 * 2 + rep.d3 * 3 + rep.d4 * 4 + rep.d5 * 5),
      SUM(rep.rt)), 4) AS pooled_rating
"""


def work_dedup_cte(
    group_expr: str, group_alias: str, where: str, extra_where: str
) -> str:
    """
    A CTE collapsing `books` to one representative edition per (group, work).

    Grouping happens on (group key, normalised title). A work published by two
    publishers therefore survives once in each publisher's group -- editions
    cannot be deduplicated across groups, only within one.
    """
    return f"""
    WITH per_work AS (
      SELECT
        {group_expr} AS {group_alias},
        COUNT(*) AS n_editions,
        {_REP_PICK}
      FROM {bq.BOOKS}
      WHERE {where} AND {extra_where}
        AND name IS NOT NULL AND rating_dist_total >= @min_ratings
      GROUP BY {group_alias}, {work_key("name")}
    )
    """


def works_in_scope(
    where_sql: str, params: dict[str, Any], min_ratings: int
) -> tuple[int, dict]:
    """Distinct normalised titles above the threshold, within the same filters."""
    sql = f"""
    SELECT COUNT(DISTINCT {work_key("name")}) AS n_works
    FROM {bq.BOOKS}
    WHERE {where_sql}
      AND name IS NOT NULL AND rating_dist_total >= @min_ratings
    """
    rows, meta = bq.run(sql, {**params, "min_ratings": min_ratings})
    return rows[0]["n_works"], meta


def book_filters(
    language: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """
    Build the non-threshold WHERE terms shared by the `books` tools.

    Returns (sql, params, applied) where `applied` is echoed back to the caller
    so the answer records what it was filtered to.
    """
    terms: list[str] = []
    params: dict[str, Any] = {}
    applied: dict[str, Any] = {}

    if language is not None:
        lang = str(language).strip().lower()
        if not lang:
            raise ParamError("language must be a non-empty ISO code such as 'en'")
        terms.append("language_normalised = @language_code")
        params["language_code"] = lang
        applied["language_normalised"] = lang

    if year_from is not None:
        terms.append("publish_year >= @year_from")
        params["year_from"] = int(year_from)
        applied["year_from"] = int(year_from)

    if year_to is not None:
        terms.append("publish_year <= @year_to")
        params["year_to"] = int(year_to)
        applied["year_to"] = int(year_to)

    if year_from is not None and year_to is not None and int(year_from) > int(year_to):
        raise ParamError("year_from must not be greater than year_to")

    return (" AND ".join(terms) if terms else "TRUE"), params, applied


def threshold_exclusions(
    where_sql: str, params: dict[str, Any], min_ratings: int
) -> tuple[dict[str, int], dict]:
    """
    How many books the min-ratings threshold removed, within the same filters.

    Reported alongside every thresholded result so the caller can see what
    fraction of the in-scope corpus the answer actually rests on.
    """
    sql = f"""
    SELECT
      COUNT(*) AS n_in_scope,
      COUNTIF(rating_dist_total < @min_ratings) AS n_below_threshold,
      COUNTIF(rating_dist_total = 0) AS n_unrated
    FROM {bq.BOOKS}
    WHERE {where_sql}
    """
    rows, meta = bq.run(sql, {**params, "min_ratings": min_ratings})
    r = rows[0]
    return {
        "n_books_in_scope": r["n_in_scope"],
        "n_books_below_threshold": r["n_below_threshold"],
        "n_books_unrated": r["n_unrated"],
    }, meta


def envelope(
    data: Any,
    *,
    n: dict[str, Any],
    caveats: list[str],
    excluded: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    The shape every tool returns.

    `n` is mandatory: no average leaves this server without the count it rests
    on. `caveats` travels with the number so the limitation cannot be lost by
    the model forgetting DATA_NOTES.md.
    """
    return {
        "data": data,
        "n": n,
        "excluded": excluded or {},
        "filters": filters or {},
        "caveats": caveats,
        "query_meta": meta or {},
    }


def merge_meta(*metas: dict) -> dict:
    """
    Combine per-job meta from the queries backing one tool call.

    `cache_hits` and `bq_ms` are carried here as well as into telemetry
    because telemetry is not readable from the response path: under the HTTP
    transport it goes to stdout for Cloud Logging, so a caller holding only
    the envelope has no other way to see what a figure cost or whether
    BigQuery served it from cache. Reported per tool call, not per job --
    `cache_hits` counts how many of `queries` were cache hits, so 2/2 and 1/2
    are distinguishable rather than collapsing to a single boolean.

    `statements` is the SQL behind the figure, one entry per job in the order
    the jobs ran, each with the values bound to its named parameters. It is
    what lets a reader check a number against the query that produced it
    without leaving the envelope. A meta without `sql` contributes no entry.
    """
    out = {
        "bytes_processed": 0,
        "bytes_billed": 0,
        "queries": 0,
        "cache_hits": 0,
        "bq_ms": 0.0,
        "statements": [],
    }
    for m in metas:
        out["bytes_processed"] += m.get("bytes_processed") or 0
        out["bytes_billed"] += m.get("bytes_billed") or 0
        out["queries"] += 1
        if m.get("cache_hit"):
            out["cache_hits"] += 1
        out["bq_ms"] += m.get("bq_ms") or 0.0
        if m.get("sql"):
            out["statements"].append({"sql": m["sql"], "params": dict(m.get("params") or {})})
    out["bq_ms"] = round(out["bq_ms"], 2)
    return out


# Sort keys a caller may name on a grouped tool. Whitelisted rather than
# interpolated so no caller-supplied text ever reaches the SQL text.
GROUP_ORDER_KEYS = ("n_books", "n_ratings", "avg_book_rating", "pooled_rating")


def require_order_by(value: str, allowed: tuple[str, ...] = GROUP_ORDER_KEYS) -> str:
    v = str(value).strip().lower()
    if v not in allowed:
        raise ParamError(f"order_by must be one of {', '.join(allowed)}")
    return v


def require_min_books(value: int) -> int:
    value = int(value)
    if value < 1:
        raise ParamError("min_books must be at least 1")
    return value
