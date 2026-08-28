"""
Per-call telemetry.

    STDOUT IS THE MCP PROTOCOL CHANNEL.

This server speaks MCP over stdio: stdout carries JSON-RPC framing. A single
stray byte written there corrupts the stream and silently kills the connection
to the client -- no error, no traceback, just a dead server. So nothing in this
module may write to stdout, ever.

How that is guaranteed here:

  * Telemetry lines go to a file opened by path. Never to a stream this process
    shares with the protocol.
  * The only fallback, used when the file cannot be written, is an explicit
    `file=sys.stderr`. There is no bare print() in this module.
  * No logging.basicConfig() call anywhere in the package. Were one added, the
    stdlib default is stderr, which would be survivable -- but the default is
    not relied on, because basicConfig(stream=sys.stdout) is one keyword away.
  * Telemetry failures are swallowed. A tool call must not fail, and must not
    emit a traceback, because the log could not be written.

`tests/test_guards.py::test_tool_calls_write_nothing_to_stdout` captures
sys.stdout at the file-descriptor level around a real tool call and asserts it
is empty.

Under HTTP transport (Cloud Run) stdout is NOT the protocol channel, and Cloud
Logging wants one JSON object per line there. That sink lives in a separate
module, `telemetry_stdout`, which this one imports lazily and only when
set_transport("http") has been called. The ban is therefore scoped by the
import graph rather than relaxed: under stdio the stdout-writing code is never
loaded into the process at all, which a subprocess test asserts directly.

One JSON object per line, appended. Query *results* are never recorded -- only
counts, timings and the BigQuery job metadata needed to tie a line back to
`bq ls -j`. Rejected SQL is never recorded either: a guard rejection logs the
rule that fired and the offending column, not the statement.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Default lives under the project, next to the package, and is gitignored.
# GOODREADS_TELEMETRY_PATH overrides it; the server's working directory is not
# predictable, so this is deliberately not relative to the CWD.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_PATH = _PROJECT_ROOT / "logs" / "telemetry.jsonl"

_DISABLED = ("0", "false", "no", "off")


def enabled() -> bool:
    """Read at call time, not import time, so tests and operators can toggle it."""
    return os.environ.get("GOODREADS_TELEMETRY", "1").strip().lower() not in _DISABLED


def log_path() -> Path:
    override = os.environ.get("GOODREADS_TELEMETRY_PATH")
    return Path(override).expanduser() if override else DEFAULT_LOG_PATH


# --------------------------------------------------------------------------
# Per-call BigQuery accumulation
# --------------------------------------------------------------------------
#
# A tool issues one to three queries. bq.run() reports each one here, and the
# decorator aggregates them into the single line for that call. A ContextVar
# rather than a module global so concurrent calls cannot mix their totals.

_QUERIES: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "goodreads_bq_queries", default=None
)


def record_query(
    *,
    job_id: str | None,
    bytes_billed: int | None,
    bytes_processed: int | None,
    cache_hit: bool | None,
    bq_ms: float,
) -> None:
    """Called by bq.run() for every job. A no-op outside an instrumented call."""
    bucket = _QUERIES.get()
    if bucket is None:
        return
    bucket.append(
        {
            "job_id": job_id,
            "bytes_billed": bytes_billed,
            "bytes_processed": bytes_processed,
            "cache_hit": cache_hit,
            "bq_ms": bq_ms,
        }
    )


# --------------------------------------------------------------------------
# Transport, and the sink it selects
# --------------------------------------------------------------------------
#
# The default is "stdio" -- the RESTRICTIVE mode. If set_transport() is never
# called, telemetry goes to a file and the stdout sink stays unimported. A
# missing call therefore fails safe rather than opening stdout.

TRANSPORTS = ("stdio", "http")

_TRANSPORT = "stdio"
_SINK = None  # resolved on first use; set_transport() may replace it


def transport() -> str:
    """The transport this process is serving. Never guesses -- defaults to stdio."""
    return _TRANSPORT


def set_transport(name: str) -> None:
    """
    Record the transport and select the telemetry sink for it.

    Called once at startup, before anything can log. Under "http" this is the
    only place `telemetry_stdout` is imported; under "stdio" that module is
    never loaded, so the stdout-writing code is not merely unused, it is
    absent from the process.

    GOODREADS_TELEMETRY_SINK overrides the choice ("file" or "stdout"), but
    asking for "stdout" under stdio raises rather than corrupting the protocol.
    """
    global _TRANSPORT, _SINK
    if name not in TRANSPORTS:
        raise ValueError(f"transport must be one of {TRANSPORTS}, not {name!r}")
    _TRANSPORT = name

    requested = os.environ.get("GOODREADS_TELEMETRY_SINK") or (
        "stdout" if name == "http" else "file"
    )
    if requested == "file":
        _SINK = write_to_file
    elif requested == "stdout":
        from . import telemetry_stdout  # lazy: never imported under stdio

        _SINK = telemetry_stdout.activate()
    else:
        raise ValueError(
            f"GOODREADS_TELEMETRY_SINK must be 'file' or 'stdout', not {requested!r}"
        )


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def write_to_file(payload: dict[str, Any]) -> None:
    """
    Append one JSON object to the log file. Never touches stdout.

    Opened per write in append mode: line-sized appends to a local file are
    atomic enough for this, and holding a handle across a long-lived stdio
    server risks losing buffered lines if the client disconnects abruptly.
    """
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, default=str, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def write_line(payload: dict[str, Any]) -> None:
    """Emit one record through the active sink. Never raises."""
    try:
        sink = _SINK or write_to_file
        sink(payload)
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break a call
        print(f"[telemetry] write failed: {exc!r}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Outcome classification
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _error_types() -> tuple[type, tuple[type, ...]]:
    """
    Imported lazily so this module has no import-time dependency on bq, which
    imports this one. google.api_core is optional at import time.
    """
    from .bq import QueryGuardError

    bq_errors: tuple[type, ...] = ()
    try:
        from google.api_core import exceptions as gexc

        bq_errors = (gexc.GoogleAPICallError, gexc.RetryError)
    except Exception:  # noqa: BLE001
        pass
    return QueryGuardError, bq_errors


def _row_count(result: Any) -> int | None:
    """
    Rows handed to the model. None where `data` is not a list -- several tools
    return a single object, for which a row count is meaningless. The row
    *contents* are never inspected beyond taking this length.
    """
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    return len(data) if isinstance(data, list) else None


def _params(fn: Any, args: tuple, kwargs: dict) -> dict[str, Any]:
    """
    Arguments as actually passed -- defaults the caller did not supply are
    omitted, so the log shows what the model chose rather than what the
    signature declares. All tool parameters are scalars; no result data here.
    """
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
        return dict(bound.arguments)
    except Exception:  # noqa: BLE001
        return {"_unbindable": True}


# --------------------------------------------------------------------------
# The decorator
# --------------------------------------------------------------------------


def instrument(fn):
    """
    Wrap one tool so every call emits a telemetry line.

    Applied *beneath* @mcp.tool, so FastMCP registers the wrapper. functools
    .wraps keeps __wrapped__, __annotations__ and the signature intact, which
    is what FastMCP reads to build the tool schema -- the wrapper is invisible
    to the protocol.

    A test asserts every @mcp.tool in server.py carries this, so a tool cannot
    be added without instrumentation. Same enforcement idea as the query
    guards: structural, not conventional.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not enabled():
            return fn(*args, **kwargs)

        guard_error, bq_errors = _error_types()
        bucket: list[dict[str, Any]] = []
        token = _QUERIES.set(bucket)
        started = time.perf_counter()

        outcome = "ok"
        error_type: str | None = None
        error_message: str | None = None
        guard_rule: str | None = None
        guard_column: str | None = None
        n_rows: int | None = None
        result = None

        try:
            result = fn(*args, **kwargs)
        except guard_error as exc:
            outcome = "guard_rejected"
            # The rule and the offending column only. Never the SQL.
            guard_rule = getattr(exc, "rule", None)
            guard_column = getattr(exc, "column", None)
            error_type = type(exc).__name__
            error_message = str(exc)
            raise
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            outcome = "bq_error" if bq_errors and isinstance(exc, bq_errors) else "other_error"
            error_type = type(exc).__name__
            error_message = str(exc)
            raise
        else:
            # Parameter failures come back in-band from _fail() rather than as
            # exceptions, so they would otherwise be logged as successes.
            if isinstance(result, dict) and result.get("error"):
                outcome = "other_error"
                error_type = "ParamError"
                error_message = str(result["error"])
            n_rows = _row_count(result)
        finally:
            _QUERIES.reset(token)
            total_ms = round((time.perf_counter() - started) * 1000, 2)
            bq_ms = round(sum(q["bq_ms"] for q in bucket), 2)
            payload: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "tool": getattr(fn, "__name__", "?"),
                "params": _params(fn, args, kwargs),
                "outcome": outcome,
                "n_rows": n_rows,
                "n_queries": len(bucket),
                "bytes_billed": sum(q["bytes_billed"] or 0 for q in bucket),
                "bytes_processed": sum(q["bytes_processed"] or 0 for q in bucket),
                "cache_hit": (
                    all(q["cache_hit"] for q in bucket) if bucket else None
                ),
                "job_ids": [q["job_id"] for q in bucket if q["job_id"]],
                "duration_ms": total_ms,
                "bq_ms": bq_ms,
                "overhead_ms": round(total_ms - bq_ms, 2),
                "queries": bucket,
            }
            if error_type:
                payload["error_type"] = error_type
                payload["error"] = error_message
            if guard_rule:
                payload["guard_rule"] = guard_rule
                payload["guard_column"] = guard_column
            write_line(payload)

        return result

    wrapper._telemetry_instrumented = True  # checked by the enforcement test
    return wrapper
