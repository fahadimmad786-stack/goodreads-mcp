"""
Goodreads statistics MCP server.

Design rules, enforced rather than documented:

  * Aggregates, not rows. Ranking tools return a bounded, ordered list; there
    is no tool that browses the table.
  * Every ranking takes a minimum-ratings threshold, floored at 1. The median
    book has 5 ratings and 451,777 books have none at all, stored as
    rating = 0.0 -- without a threshold both distort every answer.
  * Every average ships with the n it rests on, and with the count the
    threshold excluded.
  * Every tool emits the caveats belonging to the code path it took, so the
    limitation travels with the number.
  * Grouping is on language_normalised. `publish_day` is never read; a guard
    in the transport layer rejects any query that mentions it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from . import bq, caveats, telemetry
from .queries import (
    DEFAULT_MIN_RATINGS,
    RATING_AGGS,
    RATING_AGGS_WORKS,
    ParamError,
    book_filters,
    clamp_limit,
    envelope,
    merge_meta,
    require_direction,
    require_min_books,
    require_min_ratings,
    require_order_by,
    require_unit,
    threshold_exclusions,
    title_norm,
    work_dedup_cte,
    work_key,
    works_in_scope,
)

mcp = FastMCP(
    name="goodreads-stats",
    instructions=(
        "Statistical tools over a 1.85M-row Goodreads books table and a "
        "separate 357k-row user ratings table in BigQuery.\n\n"
        "Call dataset_overview() first if you have not already -- the dataset "
        "has defects that make naive readings wrong. In particular: 451,777 "
        "books have no ratings and are stored as rating = 0.0; "
        "language_normalised is populated for only 13.6% of rows; and the two "
        "tables can only be joined on title text, covering half the titles.\n\n"
        "Every tool returns {data, n, excluded, filters, caveats, query_meta}. "
        "The `caveats` list is not decoration -- report the relevant ones "
        "alongside any figure you quote. Averages come in two forms: "
        "avg_book_rating counts each book once, pooled_rating weights by how "
        "many ratings each book has."
    ),
)


def _fail(exc: Exception) -> dict[str, Any]:
    """Turn a parameter error into a result the model can act on."""
    return {
        "error": str(exc),
        "data": [],
        "n": {},
        "caveats": caveats.collect("rating_skew", "unrated_books"),
    }


def _unit_caveats(unit: str) -> tuple[str, ...]:
    """
    Which edition-related caveats apply, given the unit the caller chose.

    Under "works" both apply: work_dedup says what the collapse did and where
    it is still approximate, edition_duplication says why it was needed.
    """
    if unit == "works":
        return ("edition_duplication", "work_dedup")
    return ("edition_duplication",)


# --------------------------------------------------------------------------
# 1. Grounding
# --------------------------------------------------------------------------


@mcp.tool
@telemetry.instrument
def dataset_overview() -> dict[str, Any]:
    """
    Shape, coverage and known defects of the Goodreads dataset.

    Call this before answering anything substantive. It reports live row and
    population counts for every column that has a coverage problem, and
    returns the full caveat list, including three defects measured from the
    loaded tables that the project's own DATA_NOTES.md does not mention.
    """
    books_sql = f"""
    SELECT
      COUNT(*) AS n_books,
      COUNTIF(rating_dist_total > 0) AS n_books_with_ratings,
      COUNTIF(rating_dist_total = 0) AS n_books_unrated,
      COUNTIF(rating_dist_total >= 100) AS n_books_100plus,
      SUM(rating_dist_total) AS n_ratings_total,
      APPROX_QUANTILES(rating_dist_total, 100)[OFFSET(50)] AS median_ratings_per_book,
      APPROX_QUANTILES(rating_dist_total, 100)[OFFSET(90)] AS p90_ratings_per_book,
      MAX(rating_dist_total) AS max_ratings_per_book,
      COUNTIF(language_normalised IS NOT NULL) AS n_language_normalised,
      COUNT(DISTINCT language_normalised) AS n_distinct_languages,
      COUNTIF(pages_number IS NOT NULL) AS n_pages_number,
      COUNTIF(description IS NOT NULL) AS n_description,
      COUNTIF(count_of_text_reviews IS NOT NULL) AS n_count_of_text_reviews,
      COUNTIF(publisher IS NOT NULL) AS n_publisher,
      COUNT(DISTINCT publisher) AS n_distinct_publishers,
      COUNT(DISTINCT authors) AS n_distinct_author_strings,
      COUNTIF(publish_year IS NOT NULL) AS n_publish_year,
      MIN(publish_year) AS min_publish_year,
      MAX(publish_year) AS max_publish_year
    FROM {bq.BOOKS}
    """
    ratings_sql = f"""
    SELECT
      COUNT(*) AS n_user_ratings,
      COUNT(DISTINCT user_id) AS n_users,
      COUNT(DISTINCT book_title_normalised) AS n_distinct_titles,
      ROUND(AVG(rating), 4) AS avg_user_rating
    FROM {bq.USER_RATINGS}
    """
    # A row is an edition, and each edition repeats its work's full rating
    # total. Measure that here so the overview states the true scale of it.
    editions_sql = f"""
    WITH per_title AS (
      SELECT
        {work_key("name")} AS t,
        MAX(rating_dist_total) AS work_ratings,
        MIN(rating_dist_total) AS min_edition_ratings,
        SUM(rating_dist_total) AS edition_summed_ratings,
        COUNT(*) AS n_editions
      FROM {bq.BOOKS}
      WHERE name IS NOT NULL AND rating_dist_total >= 100
      GROUP BY t
    )
    SELECT
      COUNT(*) AS n_distinct_titles,
      SUM(work_ratings) AS n_ratings_deduplicated,
      SUM(edition_summed_ratings) AS n_ratings_summed_over_editions,
      MAX(n_editions) AS max_editions_for_one_title,
      COUNTIF(n_editions > 1) AS n_titles_multi_edition,
      COUNTIF(n_editions > 1 AND work_ratings != min_edition_ratings)
        AS n_titles_with_differing_edition_totals
    FROM per_title
    """
    brows, bmeta = bq.run(books_sql)
    urows, umeta = bq.run(ratings_sql)
    erows, emeta = bq.run(editions_sql)
    b, u, e = brows[0], urows[0], erows[0]

    total = b["n_books"]
    def pct(x: int) -> float:
        return round(100.0 * x / total, 2) if total else 0.0

    data = {
        "books": {
            **b,
            "coverage_pct": {
                "with_ratings": pct(b["n_books_with_ratings"]),
                "language_normalised": pct(b["n_language_normalised"]),
                "pages_number": pct(b["n_pages_number"]),
                "description": pct(b["n_description"]),
                "count_of_text_reviews": pct(b["n_count_of_text_reviews"]),
            },
        },
        "user_ratings": u,
        "edition_duplication": {
            "scope": "books with >= 100 ratings",
            "n_edition_rows": b["n_books_100plus"],
            "n_distinct_titles": e["n_distinct_titles"],
            "max_editions_for_one_title": e["max_editions_for_one_title"],
            "n_ratings_summed_over_editions": e["n_ratings_summed_over_editions"],
            "n_ratings_deduplicated_by_title": e["n_ratings_deduplicated"],
            # Editions repeat their work's rating pool, but not exactly. Where
            # they differ, no exact work total is recoverable: summing
            # overcounts and taking the max undercounts. unit="works" takes the
            # max, so its n_ratings is a floor.
            "n_titles_multi_edition": e["n_titles_multi_edition"],
            "n_titles_with_differing_edition_totals": e[
                "n_titles_with_differing_edition_totals"
            ],
        },
        "unusable_columns": {
            "publish_day": "placeholder for most rows; no tool reads it and a query guard rejects it",
            "language": "unnormalised; use language_normalised",
        },
        "join": {
            "key": "normalised title text",
            "matched_titles": 52016,
            "rated_titles": 98686,
            "coverage_pct": 52.71,
        },
    }
    dedup = e["n_ratings_deduplicated"]
    data["edition_duplication"]["overcount_factor"] = (
        round(e["n_ratings_summed_over_editions"] / dedup, 2) if dedup else None
    )

    return envelope(
        data,
        n={
            "n_books": b["n_books"],
            "n_user_ratings": u["n_user_ratings"],
            "n_users": u["n_users"],
        },
        caveats=caveats.all_caveats(),
        meta=merge_meta(bmeta, umeta, emeta),
    )


# --------------------------------------------------------------------------
# 2. `books` distribution and rankings
# --------------------------------------------------------------------------


@mcp.tool
@telemetry.instrument
def rating_distribution(
    min_ratings: Annotated[int, Field(ge=1, description=(
        "Minimum ratings a book must have to be counted. Floor 1. The median "
        "book has 5 ratings, so low values fill the distribution with noise."
    ))] = DEFAULT_MIN_RATINGS,
    bucket_size: Annotated[float, Field(gt=0, le=1.0, description=(
        "Width of each rating bucket, 0.05 to 1.0."
    ))] = 0.25,
    language: Annotated[str | None, Field(description=(
        "Restrict to one language_normalised ISO code, e.g. 'en'. Only 13.6% "
        "of books carry a language label at all."
    ))] = None,
    year_from: Annotated[int | None, Field(description="Earliest publish_year, inclusive.")] = None,
    year_to: Annotated[int | None, Field(description="Latest publish_year, inclusive.")] = None,
) -> dict[str, Any]:
    """
    How book ratings are distributed: a histogram of per-book mean ratings,
    plus the pooled share of 1-5 star ratings across every rating in scope.

    Answers "are ratings clustered high?", "what does a typical rating look
    like?", "how unusual is a 4.5?".
    """
    try:
        min_ratings = require_min_ratings(min_ratings)
        if not (0.05 <= float(bucket_size) <= 1.0):
            raise ParamError("bucket_size must be between 0.05 and 1.0")
        where, params, applied = book_filters(language, year_from, year_to)
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    sql = f"""
    SELECT
      ROUND(FLOOR(rating / @bucket_size) * @bucket_size, 4) AS bucket_floor,
      COUNT(*) AS n_books,
      SUM(rating_dist_total) AS n_ratings,
      SUM(rating) AS sum_of_book_ratings,
      SUM(rating_dist_1) AS s1, SUM(rating_dist_2) AS s2,
      SUM(rating_dist_3) AS s3, SUM(rating_dist_4) AS s4,
      SUM(rating_dist_5) AS s5
    FROM {bq.BOOKS}
    WHERE {where} AND rating_dist_total >= @min_ratings
    GROUP BY bucket_floor
    ORDER BY bucket_floor
    """
    rows, meta = bq.run(sql, {**params, "min_ratings": min_ratings, "bucket_size": float(bucket_size)})
    excl, emeta = threshold_exclusions(where, params, min_ratings)

    n_books = sum(r["n_books"] for r in rows)
    n_ratings = sum(r["n_ratings"] or 0 for r in rows)
    sum_book_ratings = sum(r["sum_of_book_ratings"] or 0.0 for r in rows)
    stars = {i: sum(r[f"s{i}"] or 0 for r in rows) for i in range(1, 6)}
    star_total = sum(stars.values())

    buckets = [
        {
            "bucket": f"{r['bucket_floor']:.2f}-{r['bucket_floor'] + bucket_size:.2f}",
            "bucket_floor": r["bucket_floor"],
            "n_books": r["n_books"],
            "n_ratings": r["n_ratings"],
            "pct_of_books": round(100.0 * r["n_books"] / n_books, 3) if n_books else 0.0,
        }
        for r in rows
    ]
    data = {
        "histogram": buckets,
        "summary": {
            "avg_book_rating": round(sum_book_ratings / n_books, 4) if n_books else None,
            "pooled_rating": round(
                sum(i * stars[i] for i in range(1, 6)) / star_total, 4
            ) if star_total else None,
        },
        "star_share_pct": {
            f"{i}_star": round(100.0 * stars[i] / star_total, 3) if star_total else 0.0
            for i in range(1, 6)
        },
    }
    return envelope(
        data,
        n={"n_books": n_books, "n_ratings": n_ratings},
        excluded={**excl, "min_ratings": min_ratings},
        filters={**applied, "min_ratings": min_ratings, "bucket_size": bucket_size},
        caveats=caveats.collect(
            "rating_skew", "unrated_books", "dual_average", "edition_duplication",
            *(("language_coverage", "language_grouping") if language else ()),
        ),
        meta=merge_meta(meta, emeta),
    )


@mcp.tool
@telemetry.instrument
def top_books_by_rating(
    min_ratings: Annotated[int, Field(ge=1, description=(
        "Minimum ratings a book must have to be ranked. Floor 1. At low values "
        "the top of the list is obscure books with a handful of 5-star ratings."
    ))] = DEFAULT_MIN_RATINGS,
    limit: Annotated[int, Field(ge=1, le=100, description="Books to return, max 100.")] = 20,
    direction: Annotated[str, Field(description=(
        "'desc' for highest rated first, 'asc' for lowest rated first."
    ))] = "desc",
    unit: Annotated[str, Field(description=(
        "'editions' ranks the table as stored, so several editions of one "
        "work can occupy several places in the list. 'works' collapses "
        "editions sharing a normalised title and ranks the best-rated edition "
        "of each, giving a list of distinct works."
    ))] = "editions",
    language: Annotated[str | None, Field(description="language_normalised ISO code, e.g. 'en'.")] = None,
    year_from: Annotated[int | None, Field(description="Earliest publish_year, inclusive.")] = None,
    year_to: Annotated[int | None, Field(description="Latest publish_year, inclusive.")] = None,
) -> dict[str, Any]:
    """
    Highest- or lowest-rated books, subject to a minimum-ratings threshold.

    The threshold is the whole point: raise it for a result about well-known
    books, lower it to reach the long tail. Ties break toward the more heavily
    rated book.

    Under the default unit="editions" a work with several editions can take
    several places in the list -- all with the same rating, since editions of
    one work largely share a rating pool. Pass unit="works" for a list of
    distinct works.
    """
    try:
        min_ratings = require_min_ratings(min_ratings)
        limit = clamp_limit(limit)
        direction = require_direction(direction)
        unit = require_unit(unit)
        where, params, applied = book_filters(language, year_from, year_to)
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    if unit == "works":
        # One representative edition per work: the one carrying the most
        # ratings, ties broken by lowest id so the pick is stable across runs.
        # This is the same representative rule the grouped tools use.
        sql = f"""
        WITH ranked AS (
          SELECT
            name, authors, publish_year, language_normalised, publisher,
            rating, rating_dist_total, pages_number,
            COUNT(*) OVER (PARTITION BY {work_key("name")}) AS n_editions,
            ROW_NUMBER() OVER (
              PARTITION BY {work_key("name")}
              ORDER BY rating_dist_total DESC, id ASC
            ) AS edition_rank
          FROM {bq.BOOKS}
          WHERE {where} AND name IS NOT NULL
            AND rating_dist_total >= @min_ratings
        )
        SELECT
          name,
          authors,
          publish_year,
          language_normalised,
          publisher,
          ROUND(rating, 2) AS rating,
          rating_dist_total AS n_ratings,
          pages_number,
          n_editions
        FROM ranked
        WHERE edition_rank = 1
        ORDER BY rating {direction.upper()}, rating_dist_total DESC
        LIMIT @limit
        """
    else:
        sql = f"""
        SELECT
          name,
          authors,
          publish_year,
          language_normalised,
          publisher,
          ROUND(rating, 2) AS rating,
          rating_dist_total AS n_ratings,
          pages_number
        FROM {bq.BOOKS}
        WHERE {where} AND rating_dist_total >= @min_ratings
        ORDER BY rating {direction.upper()}, rating_dist_total DESC
        LIMIT @limit
        """
    rows, meta = bq.run(sql, {**params, "min_ratings": min_ratings, "limit": limit})
    excl, emeta = threshold_exclusions(where, params, min_ratings)
    metas = [meta, emeta]
    n_editions_ranked = excl["n_books_in_scope"] - excl["n_books_below_threshold"]

    n: dict[str, Any] = {"rows_returned": len(rows)}
    if unit == "works":
        n_works, wmeta = works_in_scope(where, params, min_ratings)
        metas.append(wmeta)
        n["n_works_ranked"] = n_works
        n["n_edition_rows_in_scope"] = n_editions_ranked
    else:
        n["n_books_ranked"] = n_editions_ranked

    return envelope(
        rows,
        n=n,
        excluded={**excl, "min_ratings": min_ratings},
        filters={
            **applied,
            "min_ratings": min_ratings,
            "direction": direction,
            "unit": unit,
        },
        caveats=caveats.collect(
            "rating_skew", "unrated_books", *_unit_caveats(unit),
            *(("language_coverage", "language_grouping") if language else ()),
        ),
        meta=merge_meta(*metas),
    )


# --------------------------------------------------------------------------
# 3. `books` grouped aggregates
# --------------------------------------------------------------------------


def _grouped(
    *,
    group_expr: str,
    group_alias: str,
    extra_where: str,
    where: str,
    params: dict,
    min_ratings: int,
    min_books: int,
    order_by: str,
    direction: str,
    limit: int,
    unit: str = "editions",
) -> tuple[list[dict], dict, dict, dict]:
    """
    One grouped aggregate over `books`, shared by the language / year /
    publisher / author tools. All four apply the same threshold, report the
    same dual averages and exclude the same unrated rows.

    `unit` selects what a row counts. Under "editions" the table is read as
    stored. Under "works" editions sharing a normalised title collapse to one
    representative row within each group first, so a work with five editions
    contributes once rather than five times. Both branches emit the same
    column names, so order_by and the envelope are unaffected by the choice.
    """
    if unit == "works":
        sql = f"""
        {work_dedup_cte(group_expr, group_alias, where, extra_where)}
        SELECT
          {group_alias},
          {RATING_AGGS_WORKS.strip()}
        FROM per_work
        GROUP BY {group_alias}
        HAVING n_books >= @min_books
        ORDER BY {order_by} {direction.upper()}
        LIMIT @limit
        """
    else:
        sql = f"""
        SELECT
          {group_expr} AS {group_alias},
          {RATING_AGGS.strip()}
        FROM {bq.BOOKS}
        WHERE {where} AND {extra_where} AND rating_dist_total >= @min_ratings
        GROUP BY {group_alias}
        HAVING n_books >= @min_books
        ORDER BY {order_by} {direction.upper()}
        LIMIT @limit
        """
    rows, meta = bq.run(
        sql,
        {**params, "min_ratings": min_ratings, "min_books": min_books, "limit": limit},
    )
    excl, emeta = threshold_exclusions(where, params, min_ratings)
    return rows, excl, meta, emeta


@mcp.tool
@telemetry.instrument
def stats_by_language(
    min_ratings: Annotated[int, Field(ge=1, description="Minimum ratings per book. Floor 1.")] = DEFAULT_MIN_RATINGS,
    min_books: Annotated[int, Field(ge=1, description=(
        "Minimum books a language must contribute to appear. Small languages "
        "are noisy: Italian has 1,156 labelled books, Portuguese 406."
    ))] = 10,
    limit: Annotated[int, Field(ge=1, le=100, description="Languages to return, max 100.")] = 25,
    order_by: Annotated[str, Field(description=(
        "n_books, n_ratings, avg_book_rating or pooled_rating."
    ))] = "n_books",
    direction: Annotated[str, Field(description="'desc' or 'asc'.")] = "desc",
    year_from: Annotated[int | None, Field(description="Earliest publish_year, inclusive.")] = None,
    year_to: Annotated[int | None, Field(description="Latest publish_year, inclusive.")] = None,
    unit: Annotated[str, Field(description=(
        "'editions' counts one row per edition as stored; 'works' collapses editions sharing a normalised title to one row first. Use 'works' to ask how many distinct works a language has, 'editions' to ask how much was published in it."
    ))] = "editions",
) -> dict[str, Any]:
    """
    Rating statistics grouped by language.

    Grouped on language_normalised, never the raw `language` column. Read the
    coverage caveat before quoting anything from this: only 13.6% of books
    carry a language label, and 83% of those are English.
    """
    try:
        min_ratings = require_min_ratings(min_ratings)
        min_books = require_min_books(min_books)
        limit = clamp_limit(limit)
        order_by = require_order_by(order_by)
        direction = require_direction(direction)
        unit = require_unit(unit)
        where, params, applied = book_filters(None, year_from, year_to)
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    rows, excl, meta, emeta = _grouped(
        group_expr="language_normalised",
        group_alias="language_normalised",
        extra_where="language_normalised IS NOT NULL",
        where=where,
        params=params,
        min_ratings=min_ratings,
        min_books=min_books,
        order_by=order_by,
        direction=direction,
        limit=limit,
        unit=unit,
    )
    labelled = sum(r["n_books"] for r in rows)
    return envelope(
        rows,
        n={"n_books_in_returned_groups": labelled, "groups_returned": len(rows)},
        excluded={
            **excl,
            "min_ratings": min_ratings,
            "min_books": min_books,
            "note": "books with a NULL language_normalised are excluded entirely",
        },
        filters={**applied, "min_ratings": min_ratings, "order_by": order_by, "unit": unit},
        caveats=caveats.collect(
            "language_coverage", "language_grouping", "rating_skew",
            "unrated_books", "dual_average", *_unit_caveats(unit),
        ),
        meta=merge_meta(meta, emeta),
    )


@mcp.tool
@telemetry.instrument
def stats_by_year(
    year_from: Annotated[int, Field(description="Earliest publish_year, inclusive.")] = 1950,
    year_to: Annotated[int, Field(description="Latest publish_year, inclusive.")] = 2022,
    min_ratings: Annotated[int, Field(ge=1, description="Minimum ratings per book. Floor 1.")] = DEFAULT_MIN_RATINGS,
    min_books: Annotated[int, Field(ge=1, description="Minimum books a year must contribute to appear.")] = 1,
    language: Annotated[str | None, Field(description="language_normalised ISO code, e.g. 'en'.")] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Years to return, max 200.")] = 200,
    unit: Annotated[str, Field(description=(
        "'editions' counts one row per edition as stored, which is what a publication-volume series usually wants. 'works' collapses editions sharing a normalised title, but note a reissue is dated to its own publish_year, so a work can still appear in several years."
    ))] = "editions",
) -> dict[str, Any]:
    """
    Rating statistics and publication volume per publication year.

    publish_year is the only reliable temporal field in this dataset -- use
    this rather than publish_month for any real time series. Always ordered
    chronologically.
    """
    try:
        min_ratings = require_min_ratings(min_ratings)
        min_books = require_min_books(min_books)
        limit = clamp_limit(limit, cap=200)
        unit = require_unit(unit)
        where, params, applied = book_filters(language, year_from, year_to)
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    rows, excl, meta, emeta = _grouped(
        group_expr="publish_year",
        group_alias="publish_year",
        extra_where="publish_year IS NOT NULL",
        where=where,
        params=params,
        min_ratings=min_ratings,
        min_books=min_books,
        order_by="publish_year",
        direction="asc",
        limit=limit,
        unit=unit,
    )
    return envelope(
        rows,
        n={
            "n_books": sum(r["n_books"] for r in rows),
            "n_ratings": sum(r["n_ratings"] or 0 for r in rows),
            "years_returned": len(rows),
        },
        excluded={**excl, "min_ratings": min_ratings, "min_books": min_books},
        filters={**applied, "min_ratings": min_ratings, "unit": unit},
        caveats=caveats.collect(
            "publish_year_reliable", "rating_skew", "unrated_books", "dual_average", *_unit_caveats(unit),
            *(("language_coverage", "language_grouping") if language else ()),
        ),
        meta=merge_meta(meta, emeta),
    )


@mcp.tool
@telemetry.instrument
def stats_by_publisher(
    min_ratings: Annotated[int, Field(ge=1, description="Minimum ratings per book. Floor 1.")] = DEFAULT_MIN_RATINGS,
    min_books: Annotated[int, Field(ge=1, description=(
        "Minimum books a publisher string must have to appear. Because the "
        "column is unnormalised, a real publisher's output is split across "
        "several strings, so this filters spellings, not publishers."
    ))] = 20,
    limit: Annotated[int, Field(ge=1, le=100, description="Publisher strings to return, max 100.")] = 25,
    order_by: Annotated[str, Field(description="n_books, n_ratings, avg_book_rating or pooled_rating.")] = "n_books",
    direction: Annotated[str, Field(description="'desc' or 'asc'.")] = "desc",
    year_from: Annotated[int | None, Field(description="Earliest publish_year, inclusive.")] = None,
    year_to: Annotated[int | None, Field(description="Latest publish_year, inclusive.")] = None,
    unit: Annotated[str, Field(description=(
        "'editions' counts one row per edition as stored -- the right unit for 'most prolific publisher', since issuing five editions is five editions of work. 'works' collapses editions sharing a normalised title within each publisher."
    ))] = "editions",
) -> dict[str, Any]:
    """
    Rating statistics grouped by publisher string.

    `publisher` is unnormalised free text with 79,423 distinct values, so each
    row is one spelling rather than one publisher. Penguin alone occupies six
    or more separate rows. Treat every figure here as a lower bound on that
    imprint's real output.
    """
    try:
        min_ratings = require_min_ratings(min_ratings)
        min_books = require_min_books(min_books)
        limit = clamp_limit(limit)
        order_by = require_order_by(order_by)
        direction = require_direction(direction)
        unit = require_unit(unit)
        where, params, applied = book_filters(None, year_from, year_to)
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    rows, excl, meta, emeta = _grouped(
        group_expr="publisher",
        group_alias="publisher",
        extra_where="publisher IS NOT NULL AND TRIM(publisher) != ''",
        where=where,
        params=params,
        min_ratings=min_ratings,
        min_books=min_books,
        order_by=order_by,
        direction=direction,
        limit=limit,
        unit=unit,
    )
    return envelope(
        rows,
        n={
            "n_books_in_returned_groups": sum(r["n_books"] for r in rows),
            "groups_returned": len(rows),
        },
        excluded={**excl, "min_ratings": min_ratings, "min_books": min_books},
        filters={**applied, "min_ratings": min_ratings, "order_by": order_by, "unit": unit},
        caveats=caveats.collect(
            "publisher_unnormalised", "rating_skew", "unrated_books", "dual_average",
            *_unit_caveats(unit)
        ),
        meta=merge_meta(meta, emeta),
    )


@mcp.tool
@telemetry.instrument
def stats_by_author(
    min_ratings: Annotated[int, Field(ge=1, description="Minimum ratings per book. Floor 1.")] = DEFAULT_MIN_RATINGS,
    min_books: Annotated[int, Field(ge=1, description=(
        "Minimum books an author string must have to appear. Raise this to "
        "avoid ranking one-book authors against prolific ones."
    ))] = 5,
    limit: Annotated[int, Field(ge=1, le=100, description="Author strings to return, max 100.")] = 25,
    order_by: Annotated[str, Field(description="n_books, n_ratings, avg_book_rating or pooled_rating.")] = "n_ratings",
    direction: Annotated[str, Field(description="'desc' or 'asc'.")] = "desc",
    language: Annotated[str | None, Field(description="language_normalised ISO code, e.g. 'en'.")] = None,
    year_from: Annotated[int | None, Field(description="Earliest publish_year, inclusive.")] = None,
    year_to: Annotated[int | None, Field(description="Latest publish_year, inclusive.")] = None,
    unit: Annotated[str, Field(description=(
        "'works' collapses editions sharing a normalised title to one row -- the right unit for 'most-read author', so a novel with five editions counts once rather than five times. 'editions' counts one row per edition as stored, matching the raw table."
    ))] = "works",
) -> dict[str, Any]:
    """
    Rating statistics grouped by author string.

    `authors` is one free-text field per book, not a list, so a co-authored
    book forms its own group rather than counting toward each author. There
    are 675,289 distinct author strings.
    """
    try:
        min_ratings = require_min_ratings(min_ratings)
        min_books = require_min_books(min_books)
        limit = clamp_limit(limit)
        order_by = require_order_by(order_by)
        direction = require_direction(direction)
        unit = require_unit(unit)
        where, params, applied = book_filters(language, year_from, year_to)
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    rows, excl, meta, emeta = _grouped(
        group_expr="authors",
        group_alias="authors",
        extra_where="authors IS NOT NULL AND TRIM(authors) != ''",
        where=where,
        params=params,
        min_ratings=min_ratings,
        min_books=min_books,
        order_by=order_by,
        direction=direction,
        limit=limit,
        unit=unit,
    )
    return envelope(
        rows,
        n={
            "n_books_in_returned_groups": sum(r["n_books"] for r in rows),
            "groups_returned": len(rows),
        },
        excluded={**excl, "min_ratings": min_ratings, "min_books": min_books},
        filters={**applied, "min_ratings": min_ratings, "order_by": order_by, "unit": unit},
        caveats=caveats.collect(
            "authors_freetext", "rating_skew", "unrated_books", "dual_average", *_unit_caveats(unit),
            *(("language_coverage", "language_grouping") if language else ()),
        ),
        meta=merge_meta(meta, emeta),
    )


# --------------------------------------------------------------------------
# 4. `books`: pages and seasonality
# --------------------------------------------------------------------------

_PAGE_BANDS = ["<100", "100-199", "200-299", "300-399", "400-499", "500-699", "700-999", "1000+"]


@mcp.tool
@telemetry.instrument
def page_count_stats(
    min_ratings: Annotated[int, Field(ge=1, description="Minimum ratings per book. Floor 1.")] = DEFAULT_MIN_RATINGS,
    language: Annotated[str | None, Field(description="language_normalised ISO code, e.g. 'en'.")] = None,
    year_from: Annotated[int | None, Field(description="Earliest publish_year, inclusive.")] = None,
    year_to: Annotated[int | None, Field(description="Latest publish_year, inclusive.")] = None,
) -> dict[str, Any]:
    """
    Book length against rating: page-count quartiles overall, and rating
    statistics for each band of book length.

    Answers "do longer books rate higher?". Books with a NULL pages_number
    are excluded and counted separately -- 11,216 implausible values were
    nulled during cleaning.
    """
    try:
        min_ratings = require_min_ratings(min_ratings)
        where, params, applied = book_filters(language, year_from, year_to)
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    p = {**params, "min_ratings": min_ratings}
    sql = f"""
    WITH banded AS (
      SELECT
        name, rating, pages_number, rating_dist_total,
        rating_dist_1, rating_dist_2, rating_dist_3, rating_dist_4, rating_dist_5,
        CASE
          WHEN pages_number < 100 THEN 0
          WHEN pages_number < 200 THEN 1
          WHEN pages_number < 300 THEN 2
          WHEN pages_number < 400 THEN 3
          WHEN pages_number < 500 THEN 4
          WHEN pages_number < 700 THEN 5
          WHEN pages_number < 1000 THEN 6
          ELSE 7
        END AS band_index
      FROM {bq.BOOKS}
      WHERE {where} AND rating_dist_total >= @min_ratings AND pages_number IS NOT NULL
    )
    SELECT
      band_index,
      {RATING_AGGS.strip()},
      ROUND(AVG(pages_number), 1) AS avg_pages
    FROM banded
    GROUP BY band_index
    ORDER BY band_index
    """
    rows, meta = bq.run(sql, p)

    quant_sql = f"""
    SELECT
      COUNT(*) AS n_in_scope,
      COUNTIF(pages_number IS NULL) AS n_pages_null,
      APPROX_QUANTILES(pages_number, 4)[OFFSET(1)] AS p25_pages,
      APPROX_QUANTILES(pages_number, 4)[OFFSET(2)] AS median_pages,
      APPROX_QUANTILES(pages_number, 4)[OFFSET(3)] AS p75_pages,
      ROUND(AVG(pages_number), 1) AS mean_pages
    FROM {bq.BOOKS}
    WHERE {where} AND rating_dist_total >= @min_ratings
    """
    qrows, qmeta = bq.run(quant_sql, p)
    excl, emeta = threshold_exclusions(where, params, min_ratings)
    q = qrows[0]

    for r in rows:
        r["pages_band"] = _PAGE_BANDS[r["band_index"]]

    return envelope(
        {"by_band": rows, "page_count_quartiles": {k: q[k] for k in
            ("p25_pages", "median_pages", "p75_pages", "mean_pages")}},
        n={
            "n_books": sum(r["n_books"] for r in rows),
            "n_ratings": sum(r["n_ratings"] or 0 for r in rows),
        },
        excluded={
            **excl,
            "min_ratings": min_ratings,
            "n_books_null_pages": q["n_pages_null"],
        },
        filters={**applied, "min_ratings": min_ratings},
        caveats=caveats.collect(
            "pages_nulled", "rating_skew", "unrated_books", "dual_average", "edition_duplication",
            *(("language_coverage", "language_grouping") if language else ()),
        ),
        meta=merge_meta(meta, qmeta, emeta),
    )


@mcp.tool
@telemetry.instrument
def publish_month_seasonality(
    year_from: Annotated[int | None, Field(description="Earliest publish_year, inclusive.")] = None,
    year_to: Annotated[int | None, Field(description="Latest publish_year, inclusive.")] = None,
    min_ratings: Annotated[int, Field(ge=1, description=(
        "Minimum ratings for a book to contribute to the rating averages. "
        "Publication counts are unaffected by this and cover every book."
    ))] = DEFAULT_MIN_RATINGS,
    language: Annotated[str | None, Field(description="language_normalised ISO code, e.g. 'en'.")] = None,
) -> dict[str, Any]:
    """
    Coarse publishing seasonality by month, plus per-month rating averages.

    January is inflated -- it holds 17.72% of rows against a uniform 8.3%
    because unknown dates were recorded as January 1. The January row is
    flagged in the output. Prefer stats_by_year for real time-series work.
    """
    try:
        min_ratings = require_min_ratings(min_ratings)
        where, params, applied = book_filters(language, year_from, year_to)
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    sql = f"""
    SELECT
      publish_month AS month,
      COUNT(*) AS n_books_published,
      COUNTIF(rating_dist_total >= @min_ratings) AS n_books_rated,
      SUM(IF(rating_dist_total >= @min_ratings, rating_dist_total, 0)) AS n_ratings,
      ROUND(AVG(IF(rating_dist_total >= @min_ratings, rating, NULL)), 4) AS avg_book_rating,
      ROUND(SAFE_DIVIDE(
        SUM(IF(rating_dist_total >= @min_ratings,
               rating_dist_1 * 1 + rating_dist_2 * 2 + rating_dist_3 * 3
               + rating_dist_4 * 4 + rating_dist_5 * 5, 0)),
        SUM(IF(rating_dist_total >= @min_ratings, rating_dist_total, 0))), 4) AS pooled_rating
    FROM {bq.BOOKS}
    WHERE {where} AND publish_month IS NOT NULL
    GROUP BY month
    ORDER BY month
    """
    rows, meta = bq.run(sql, {**params, "min_ratings": min_ratings})
    total = sum(r["n_books_published"] for r in rows)
    for r in rows:
        r["pct_of_books"] = round(100.0 * r["n_books_published"] / total, 2) if total else 0.0
        r["placeholder_inflated"] = r["month"] == 1

    return envelope(
        rows,
        n={
            "n_books_published": total,
            "n_books_rated": sum(r["n_books_rated"] for r in rows),
            "n_ratings": sum(r["n_ratings"] or 0 for r in rows),
        },
        excluded={
            "min_ratings": min_ratings,
            "note": (
                "min_ratings gates the rating averages only; n_books_published "
                "counts every book in scope regardless of rating count"
            ),
        },
        filters={**applied, "min_ratings": min_ratings},
        caveats=caveats.collect(
            "january_inflation", "publish_year_reliable", "unrated_books", "dual_average", "edition_duplication",
            *(("language_coverage", "language_grouping") if language else ()),
        ),
        meta=merge_meta(meta),
    )


# --------------------------------------------------------------------------
# 5. `user_ratings` (the small, separate table)
# --------------------------------------------------------------------------


@mcp.tool
@telemetry.instrument
def user_ratings_overview() -> dict[str, Any]:
    """
    Shape of the user_ratings table: how the 1-5 stars are distributed, and
    how active the users are.

    This describes 4,154 users only. It is a separate dataset from `books`,
    not a sample of it, and does not generalise to Goodreads.
    """
    dist_sql = f"""
    SELECT
      rating,
      ANY_VALUE(rating_label) AS rating_label,
      COUNT(*) AS n_ratings
    FROM {bq.USER_RATINGS}
    GROUP BY rating
    ORDER BY rating
    """
    summary_sql = f"""
    WITH per_user AS (
      SELECT user_id, COUNT(*) AS n FROM {bq.USER_RATINGS} GROUP BY user_id
    )
    SELECT
      (SELECT COUNT(*) FROM {bq.USER_RATINGS}) AS n_ratings,
      (SELECT COUNT(DISTINCT user_id) FROM {bq.USER_RATINGS}) AS n_users,
      (SELECT COUNT(DISTINCT book_title_normalised) FROM {bq.USER_RATINGS}) AS n_distinct_titles,
      (SELECT ROUND(AVG(rating), 4) FROM {bq.USER_RATINGS}) AS avg_user_rating,
      (SELECT ROUND(AVG(n), 1) FROM per_user) AS mean_ratings_per_user,
      (SELECT APPROX_QUANTILES(n, 4)[OFFSET(1)] FROM per_user) AS p25_ratings_per_user,
      (SELECT APPROX_QUANTILES(n, 4)[OFFSET(2)] FROM per_user) AS median_ratings_per_user,
      (SELECT APPROX_QUANTILES(n, 4)[OFFSET(3)] FROM per_user) AS p75_ratings_per_user,
      (SELECT MAX(n) FROM per_user) AS max_ratings_per_user
    """
    drows, dmeta = bq.run(dist_sql)
    srows, smeta = bq.run(summary_sql)
    s = srows[0]
    total = s["n_ratings"]
    for r in drows:
        r["pct_of_ratings"] = round(100.0 * r["n_ratings"] / total, 2) if total else 0.0

    return envelope(
        {"star_distribution": drows, "summary": s},
        n={"n_ratings": total, "n_users": s["n_users"], "n_titles": s["n_distinct_titles"]},
        caveats=caveats.collect("tables_independent"),
        meta=merge_meta(dmeta, smeta),
    )


@mcp.tool
@telemetry.instrument
def top_titles_by_user_ratings(
    min_ratings: Annotated[int, Field(ge=1, description=(
        "Minimum number of user ratings a title must have to be ranked. "
        "Floor 1. With only 4,154 users, titles thin out fast -- keep this "
        "well above 1 for a meaningful ranking."
    ))] = 20,
    limit: Annotated[int, Field(ge=1, le=100, description="Titles to return, max 100.")] = 20,
    direction: Annotated[str, Field(description=(
        "'desc' for best-liked first, 'asc' for worst-liked first."
    ))] = "desc",
    order_by: Annotated[str, Field(description=(
        "'avg_user_rating' to rank by score, 'n_user_ratings' to rank by how "
        "many of these users rated it."
    ))] = "avg_user_rating",
) -> dict[str, Any]:
    """
    Best- or worst-liked titles among the 4,154 users in user_ratings.

    Stays entirely inside user_ratings -- no join, so no title-matching loss.
    These are the opinions of a small user panel, not the books table.
    """
    try:
        min_ratings = require_min_ratings(min_ratings)
        limit = clamp_limit(limit)
        direction = require_direction(direction)
        order_by = require_order_by(order_by, ("avg_user_rating", "n_user_ratings"))
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    sql = f"""
    SELECT
      book_title_normalised AS title_normalised,
      ANY_VALUE(book_title) AS example_raw_title,
      COUNT(*) AS n_user_ratings,
      COUNT(DISTINCT user_id) AS n_users,
      ROUND(AVG(rating), 4) AS avg_user_rating
    FROM {bq.USER_RATINGS}
    GROUP BY title_normalised
    HAVING n_user_ratings >= @min_ratings
    ORDER BY {order_by} {direction.upper()}, n_user_ratings DESC
    LIMIT @limit
    """
    rows, meta = bq.run(sql, {"min_ratings": min_ratings, "limit": limit})

    scope_sql = f"""
    WITH t AS (
      SELECT book_title_normalised, COUNT(*) AS n
      FROM {bq.USER_RATINGS} GROUP BY book_title_normalised
    )
    SELECT
      COUNT(*) AS n_titles_total,
      COUNTIF(n < @min_ratings) AS n_titles_below_threshold
    FROM t
    """
    srows, smeta = bq.run(scope_sql, {"min_ratings": min_ratings})
    s = srows[0]

    return envelope(
        rows,
        n={
            "n_titles_ranked": s["n_titles_total"] - s["n_titles_below_threshold"],
            "rows_returned": len(rows),
            "n_user_ratings_in_returned_rows": sum(r["n_user_ratings"] for r in rows),
        },
        excluded={
            "n_titles_total": s["n_titles_total"],
            "n_titles_below_threshold": s["n_titles_below_threshold"],
            "min_ratings": min_ratings,
        },
        filters={"min_ratings": min_ratings, "order_by": order_by, "direction": direction},
        caveats=caveats.collect("tables_independent"),
        meta=merge_meta(meta, smeta),
    )


# --------------------------------------------------------------------------
# 6. The only cross-table tool
# --------------------------------------------------------------------------


@mcp.tool
@telemetry.instrument
def compare_user_vs_book_ratings(
    min_user_ratings: Annotated[int, Field(ge=1, description=(
        "Minimum ratings from the 4,154-user panel for a title to appear."
    ))] = 20,
    min_book_ratings: Annotated[int, Field(ge=1, description=(
        "Minimum Goodreads ratings, summed across editions, for a title to appear."
    ))] = DEFAULT_MIN_RATINGS,
    limit: Annotated[int, Field(ge=1, le=100, description="Titles to return, max 100.")] = 25,
    order_by: Annotated[str, Field(description=(
        "'abs_divergence' for the biggest disagreements either way, "
        "'user_higher' where the panel rates above Goodreads, 'book_higher' "
        "for the reverse, 'popularity' for the most-rated titles."
    ))] = "abs_divergence",
) -> dict[str, Any]:
    """
    Where the 4,154-user panel disagrees with the wider Goodreads rating.

    This is the only tool that crosses the two tables, and the join is on
    normalised title text because user_ratings carries no book ID. It reaches
    52,016 of 98,686 rated titles (52.7%) -- roughly half the panel's ratings
    have no book row to match and are simply absent. Editions of the same
    title are pooled, so book rating counts are summed across up to 36 rows.
    """
    try:
        min_user_ratings = require_min_ratings(min_user_ratings)
        min_book_ratings = require_min_ratings(min_book_ratings)
        limit = clamp_limit(limit)
        order_by = require_order_by(
            order_by, ("abs_divergence", "user_higher", "book_higher", "popularity")
        )
    except (ParamError, ValueError) as exc:
        return _fail(exc)

    order_sql = {
        "abs_divergence": "ABS(divergence) DESC",
        "user_higher": "divergence DESC",
        "book_higher": "divergence ASC",
        "popularity": "book_n_ratings DESC",
    }[order_by]

    norm = title_norm("name")
    sql = f"""
    WITH b AS (
      SELECT
        {norm} AS t,
        COUNT(*) AS n_editions,
        SUM(rating_dist_total) AS book_n_ratings,
        SUM(rating_dist_1 * 1 + rating_dist_2 * 2 + rating_dist_3 * 3
            + rating_dist_4 * 4 + rating_dist_5 * 5) AS star_sum
      FROM {bq.BOOKS}
      WHERE name IS NOT NULL AND rating_dist_total > 0
      GROUP BY t
    ),
    u AS (
      SELECT
        book_title_normalised AS t,
        ANY_VALUE(book_title) AS example_raw_title,
        COUNT(*) AS user_n_ratings,
        AVG(rating) AS user_avg
      FROM {bq.USER_RATINGS}
      GROUP BY t
    )
    SELECT
      u.t AS title_normalised,
      u.example_raw_title,
      b.n_editions,
      u.user_n_ratings,
      ROUND(u.user_avg, 4) AS user_avg_rating,
      b.book_n_ratings,
      ROUND(SAFE_DIVIDE(b.star_sum, b.book_n_ratings), 4) AS book_pooled_rating,
      ROUND(u.user_avg - SAFE_DIVIDE(b.star_sum, b.book_n_ratings), 4) AS divergence
    FROM u
    JOIN b USING (t)
    WHERE u.user_n_ratings >= @min_user_ratings
      AND b.book_n_ratings >= @min_book_ratings
    ORDER BY {order_sql}
    LIMIT @limit
    """
    rows, meta = bq.run(
        sql,
        {
            "min_user_ratings": min_user_ratings,
            "min_book_ratings": min_book_ratings,
            "limit": limit,
        },
    )

    cov_sql = f"""
    WITH bn AS (SELECT DISTINCT {norm} AS t FROM {bq.BOOKS} WHERE name IS NOT NULL),
    ur AS (SELECT DISTINCT book_title_normalised AS t FROM {bq.USER_RATINGS})
    SELECT
      (SELECT COUNT(*) FROM ur) AS n_rated_titles,
      (SELECT COUNT(*) FROM ur JOIN bn USING (t)) AS n_matched_titles
    """
    crows, cmeta = bq.run(cov_sql)
    c = crows[0]
    matched, rated = c["n_matched_titles"], c["n_rated_titles"]

    return envelope(
        rows,
        n={
            "n_titles_compared": len(rows),
            "n_user_ratings_in_returned_rows": sum(r["user_n_ratings"] for r in rows),
            "n_book_ratings_in_returned_rows": sum(r["book_n_ratings"] for r in rows),
        },
        excluded={
            "n_rated_titles": rated,
            "n_matched_titles": matched,
            "n_unmatched_titles": rated - matched,
            "match_coverage_pct": round(100.0 * matched / rated, 2) if rated else 0.0,
            "min_user_ratings": min_user_ratings,
            "min_book_ratings": min_book_ratings,
        },
        filters={"order_by": order_by},
        caveats=caveats.collect(
            "title_join", "title_editions", "edition_duplication",
            "tables_independent", "rating_skew", "dual_average",
        ),
        meta=merge_meta(meta, cmeta),
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
