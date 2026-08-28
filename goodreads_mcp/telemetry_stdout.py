"""
The stdout telemetry sink. HTTP transport only -- Cloud Run.

This module contains the ONLY write to stdout in the server path. It is the
one module exempt from `test_no_server_module_can_reach_stdout`, so the
exemption is kept as narrow as it can be: one writer, one guard, nothing else.

Why it is safe despite the ban:

  * Under stdio, stdout carries JSON-RPC framing and this sink would corrupt
    it. Under HTTP it does not -- the protocol is on the socket, and Cloud
    Run's logging agent reads container stdout.
  * `telemetry.set_transport("http")` is the only importer. Under stdio this
    module is never imported, so the code below is absent from the process
    rather than merely unused. A subprocess test asserts exactly that.
  * activate() re-checks the transport at call time and refuses under stdio,
    so a direct import cannot arm it either.

Cloud Logging parses one JSON object per line on stdout into `jsonPayload`,
and promotes a top-level `severity` field to the entry's log level. Both are
why the format here is a single compact line with a severity attached.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# outcome -> Cloud Logging severity. A guard rejection is a caller mistake the
# operator should see, not a server fault; a bq_error is a genuine failure.
_SEVERITY = {
    "ok": "INFO",
    "guard_rejected": "WARNING",
    "bq_error": "ERROR",
    "other_error": "ERROR",
}


def _write(payload: dict[str, Any]) -> None:
    entry = {
        "severity": _SEVERITY.get(payload.get("outcome", ""), "DEFAULT"),
        "message": (
            f"{payload.get('tool', '?')} {payload.get('outcome', '?')} "
            f"in {payload.get('duration_ms', 0)}ms"
        ),
        **payload,
    }
    # One object, one line, flushed: Cloud Run drops buffered output when an
    # instance is reclaimed, and a partial line breaks the log parser.
    sys.stdout.write(json.dumps(entry, default=str, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def activate():
    """
    Return the stdout writer, or refuse if this process is serving stdio.

    Checked here as well as at the import site so that importing this module
    directly -- in a test, or by a future caller who did not read the module
    docstring -- still cannot arm a stdout write under stdio transport.
    """
    from . import telemetry

    if telemetry.transport() != "http":
        raise RuntimeError(
            "the stdout telemetry sink cannot be used under stdio transport: "
            "stdout carries JSON-RPC framing there, and writing to it would "
            "corrupt the protocol stream and silently kill the connection. "
            "Use the file sink (GOODREADS_TELEMETRY_SINK=file), which is the "
            "default for stdio."
        )
    return _write
