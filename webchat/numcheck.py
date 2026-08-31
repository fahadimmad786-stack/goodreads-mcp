"""
Enforcement of the rendering contract.

The contract is that figures reach the user rendered from tool JSON, and the
model writes only the surrounding explanation -- it must not restate, round or
paraphrase a number. A system prompt can *ask* for that. This module checks it.

Every numeral in the model's prose is canonicalised and looked up in the set of
numerals the server has actually put in front of this session:

  * values and keys anywhere in a tool result envelope, caveat prose included;
  * the parameters a tool was called with;
  * the MCP server's own `instructions` string;
  * the user's own questions.

Anything else is unsourced and is reported to the client, which marks it in
place. A rounded figure is unsourced by construction -- "4.4" does not match
"4.42" -- which is the point: rounding is one of the things the contract bans.

The check is a report, not a block. Suppressing the answer would hide the
violation; marking it shows the reader exactly which words are not backed by a
tool result.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# A numeral, with an optional magnitude or percent suffix.
#
# The lookbehind keeps us out of the middle of identifiers and dotted versions
# ("rating_dist_1", "v1.2"); the trailing lookahead on a magnitude letter keeps
# "3M" apart from "3Mb" and from a word starting with M.
_NUM_RE = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|percent|[KMB](?![\w])|[×x](?![\w]))?",
    re.IGNORECASE,
)

_MAGNITUDE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _norm(value: str) -> str:
    """Canonical form of a numeric literal: no commas, no trailing zeros."""
    v = value.replace(",", "").replace(" ", "").lstrip("+")
    if "." in v:
        v = v.rstrip("0").rstrip(".")
        if v in ("", "-"):
            v = "0"
    return v or "0"


def _forms(base: str, suffix: str | None) -> set[str]:
    """
    Every canonical form a written numeral could legitimately correspond to.

    "1.85M" yields both "1.85" and "1850000", so it matches whichever the
    server used. It does NOT yield 1850115, so writing "1.85M" where the
    server said 1,850,115 is still reported -- that is a rounding.
    """
    forms = {_norm(base)}
    if suffix:
        mult = _MAGNITUDE.get(suffix.lower())
        if mult:
            try:
                scaled = float(base.replace(",", "")) * mult
            except ValueError:
                scaled = None
            if scaled is not None:
                forms.add(_norm(f"{scaled:.6f}"))
                forms.add(_norm(str(int(scaled))) if scaled.is_integer() else _norm(f"{scaled:.6f}"))
    return forms


def numerals_in_text(text: str) -> set[str]:
    """Canonical forms of every numeral appearing in a string."""
    out: set[str] = set()
    for m in _NUM_RE.finditer(text):
        out |= _forms(m.group(1), m.group(2))
    return out


def collect(obj: Any) -> set[str]:
    """
    Canonical forms of every number reachable in a JSON-shaped object.

    Walks values *and* dict keys: `"5_star"` and `"100-199"` are things the
    server said, so a model naming them is quoting, not inventing.
    """
    out: set[str] = set()
    _walk(obj, out)
    return out


def _walk(obj: Any, out: set[str]) -> None:
    if obj is None or isinstance(obj, bool):
        return
    if isinstance(obj, int):
        out.add(_norm(str(obj)))
        return
    if isinstance(obj, float):
        out.add(_norm(repr(obj)))
        out.add(_norm(f"{obj:.4f}"))
        return
    if isinstance(obj, str):
        out |= numerals_in_text(obj)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                out |= numerals_in_text(k)
            _walk(v, out)
        return
    if isinstance(obj, Iterable):
        for v in obj:
            _walk(v, out)


def check(text: str, sourced: set[str]) -> list[dict]:
    """
    Numerals in `text` that no tool result, tool parameter, server instruction
    or user question accounts for.

    Returns one record per occurrence, with character offsets into `text`, so
    the client marks the exact span rather than every copy of the digits.
    """
    findings: list[dict] = []
    for m in _NUM_RE.finditer(text):
        forms = _forms(m.group(1), m.group(2))
        if forms & sourced:
            continue
        findings.append(
            {
                "value": m.group(0).strip(),
                "start": m.start(),
                "end": m.end(),
                "canonical": sorted(forms),
            }
        )
    return findings
