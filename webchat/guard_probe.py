"""
The guard probe: a demonstration tool, not part of the server's tool surface.

`QueryGuardError` is unreachable from the twelve MCP tools by construction --
no tool interpolates a caller-supplied column name into SQL, so no argument
can produce a query the guard rejects (see CLAUDE.md, "QueryGuardError is
unreachable from the twelve tools"). That is the design working. It also means
a user who asks about a banned column gets nothing from the guard, because
there is no tool through which to ask.

This probe exists so that question has a structural answer instead of a prose
one. It calls the server's real `bq.guard()` on a candidate query, reports the
verdict, and never executes anything. It is labelled as a demonstration probe
in the UI and in the README, and it is deliberately implemented here rather
than as a thirteenth MCP tool: adding a tool that reaches a banned column
would convert a structural impossibility into a runtime rejection.

No credentials, no BigQuery client, no network. `bq.guard()` is a pure regex
check; importing `bq` constructs no client -- `client()` is lazy.
"""

from __future__ import annotations

from goodreads_mcp import bq

from . import attach

TOOL_NAME = "check_column_available"

TOOL_DESCRIPTION = (
    "Demonstration probe, not a data tool. Checks whether a raw column of the "
    "`books` table may be read at all, by running the server's query guard "
    "against a candidate query WITHOUT executing it. No data is read and no "
    "BigQuery query is billed.\n\n"
    "Call this when the user asks for a figure that no tool exposes because "
    "the underlying column is banned or unusable -- publication day-of-week or "
    "day-of-month (publish_day), or anything requiring the raw unnormalised "
    "`language` column. It returns the guard's own verdict and the server's "
    "own caveat text for that column, which is a better answer than explaining "
    "in your own words. Do not call it for questions the twelve statistical "
    "tools can answer."
)

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "column": {
            "type": "string",
            "description": (
                "Raw column name to test, e.g. 'publish_day', 'language', "
                "'publish_month', 'pages_number'."
            ),
        }
    },
    "required": ["column"],
    "additionalProperties": False,
}

# A guard rejection carries a rule id; these are the three rules in bq.guard().
# Mapped to plain descriptions of what the rule protects, for the card header.
_RULE_SUMMARY = {
    "publish_day_banned": "column is a placeholder for most rows",
    "bare_language": "column is unnormalised; a normalised one exists",
    "select_star": "unbounded projection over a 1.85M-row table",
}


def anthropic_tool() -> dict:
    return {
        "name": TOOL_NAME,
        "description": TOOL_DESCRIPTION,
        "input_schema": TOOL_SCHEMA,
    }


def probe(column: str) -> dict:
    """
    Run the real guard on a candidate query for `column`.

    The query text is built only to be handed to `guard()`, and is returned so
    the UI can show exactly what was tested. It is never sent anywhere.
    """
    column = (column or "").strip()
    if not column or not _plausible(column):
        return {
            "probe": "guard",
            "column": column,
            "rejected": False,
            "verdict": "not_a_column",
            "message": (
                "That is not a bare column name, so there is nothing for the "
                "guard to rule on. Name a single column of the `books` table."
            ),
            "candidate_sql": None,
            "caveats": [],
            "executed": False,
        }

    candidate = f"SELECT {column} FROM {bq.BOOKS} LIMIT 1"
    result: dict = {
        "probe": "guard",
        "column": column,
        "candidate_sql": candidate,
        "executed": False,
        "caveats": attach.caveats_for_column(column),
    }
    try:
        bq.guard(candidate)
    except bq.QueryGuardError as exc:
        result.update(
            rejected=True,
            verdict="rejected_by_guard",
            rule=exc.rule,
            rule_summary=_RULE_SUMMARY.get(exc.rule, ""),
            guard_column=exc.column,
            message=str(exc),
        )
        return result

    result.update(
        rejected=False,
        verdict="not_rejected",
        message=(
            f"The guard does not reject `{column}`. That is not the same as "
            "there being a tool for it: this server exposes aggregate tools "
            "only, and no tool returns raw column values. Use the statistical "
            "tools for anything this column could support."
        ),
    )
    return result


def _plausible(column: str) -> bool:
    """A bare identifier. Keeps the probe from being used as a SQL sandbox."""
    return column.replace("_", "").isalnum() and not column[0].isdigit()
