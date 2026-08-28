"""
BigQuery access: one client, Application Default Credentials, and the query
guards that enforce the dataset's hard rules before anything reaches BigQuery.

The guards are deliberately in the transport layer rather than in individual
tools. A tool author cannot bypass them by writing a new query.
"""

from __future__ import annotations

import os
import re
import time
from functools import lru_cache
from typing import Any

from google.cloud import bigquery

from . import telemetry

PROJECT = os.environ.get("GOODREADS_BQ_PROJECT", "example-project")
DATASET = os.environ.get("GOODREADS_BQ_DATASET", "goodreads")
LOCATION = os.environ.get("GOODREADS_BQ_LOCATION", "US")

BOOKS = f"`{PROJECT}.{DATASET}.books`"
USER_RATINGS = f"`{PROJECT}.{DATASET}.user_ratings`"

# Cost ceiling per query. The books table is ~1.85M rows; a few-column scan is
# well under this. A query that would exceed it fails loudly rather than
# quietly billing.
MAX_BYTES_BILLED = int(os.environ.get("GOODREADS_MAX_BYTES_BILLED", 20 * 2**30))


class QueryGuardError(RuntimeError):
    """
    A query violated a dataset rule and was never sent to BigQuery.

    `rule` and `column` are structured so telemetry can record WHICH rule
    fired without recording the SQL that tripped it.
    """

    def __init__(self, message: str, *, rule: str, column: str | None = None):
        super().__init__(message)
        self.rule = rule
        self.column = column


# `publish_day` is 48.25% placeholder and must never be read (DATA_NOTES #1).
_BANNED_COLUMN = re.compile(r"\bpublish_day\b", re.IGNORECASE)

# Bare `language` fragments eng / en-US / en-GB. `\b` does not match between
# "language" and "_normalised" (both word characters), so this pattern hits the
# raw column only and leaves language_normalised alone.
_BARE_LANGUAGE = re.compile(r"\blanguage\b", re.IGNORECASE)

_SELECT_STAR = re.compile(r"SELECT\s+\*", re.IGNORECASE)


def guard(sql: str) -> None:
    """Raise if `sql` breaks a dataset rule. Called on every query."""
    if _BANNED_COLUMN.search(sql):
        raise QueryGuardError(
            "query references publish_day, which is a placeholder for most "
            "rows and unusable (DATA_NOTES.md #1)",
            rule="publish_day_banned",
            column="publish_day",
        )
    if _BARE_LANGUAGE.search(sql):
        raise QueryGuardError(
            "query references the raw `language` column; group by "
            "language_normalised instead (DATA_NOTES.md guidance)",
            rule="bare_language",
            column="language",
        )
    if _SELECT_STAR.search(sql):
        raise QueryGuardError(
            "query uses SELECT * on a 1.85M-row table; name the columns "
            "(DATA_NOTES.md query guidance)",
            rule="select_star",
            column=None,
        )


@lru_cache(maxsize=1)
def client() -> bigquery.Client:
    """BigQuery client on Application Default Credentials."""
    return bigquery.Client(project=PROJECT)


def _scalar(name: str, value: Any) -> bigquery.ScalarQueryParameter:
    if isinstance(value, bool):
        t = "BOOL"
    elif isinstance(value, int):
        t = "INT64"
    elif isinstance(value, float):
        t = "FLOAT64"
    else:
        t = "STRING"
    return bigquery.ScalarQueryParameter(name, t, value)


def run(sql: str, params: dict[str, Any] | None = None) -> tuple[list[dict], dict]:
    """
    Execute `sql` with named parameters. Returns (rows, meta).

    Every caller goes through here, so every query is guarded, parameterised
    and cost-capped.
    """
    guard(sql)
    params = params or {}
    job_config = bigquery.QueryJobConfig(
        query_parameters=[_scalar(k, v) for k, v in params.items()],
        maximum_bytes_billed=MAX_BYTES_BILLED,
        use_query_cache=True,
    )
    started = time.perf_counter()
    job = client().query(sql, job_config=job_config, location=LOCATION)
    rows = [dict(r) for r in job.result()]
    bq_ms = round((time.perf_counter() - started) * 1000, 2)
    meta = {
        "bytes_processed": job.total_bytes_processed,
        "bytes_billed": job.total_bytes_billed,
        "cache_hit": job.cache_hit,
        "job_id": job.job_id,
        "bq_ms": bq_ms,
    }
    # Reported to the telemetry accumulator for the enclosing tool call; a
    # no-op when there is no instrumented call in progress.
    telemetry.record_query(
        job_id=job.job_id,
        bytes_billed=job.total_bytes_billed,
        bytes_processed=job.total_bytes_processed,
        cache_hit=job.cache_hit,
        bq_ms=bq_ms,
    )
    return rows, meta
