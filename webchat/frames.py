"""
The frame shapes the client renders, built in one place for both modes.

The console has two paths to a tool call -- the model choosing one in chat
mode, and a person filling in its parameters in tool mode -- and they must put
identical JSON in front of `cards.js`. Identical is the whole point: the claim
this console makes is that a figure is rendered from the tool's own envelope,
so the rendering must not depend on which path fetched it.

So the frame builders live here rather than in `agent.py`, which imports the
Anthropic SDK. Tool mode never imports that module, and therefore never needs
an API key to draw a card.
"""

from __future__ import annotations

from . import attach, guard_probe
from .mcp_client import ToolOutcome


def _origin(tool_name: str) -> str:
    return "bff" if tool_name == guard_probe.TOOL_NAME else "mcp"


def _result_frame(call_id: str, outcome: ToolOutcome) -> dict:
    """
    Structure a tool outcome for rendering.

    Caveats are replaced in place by their structured form -- id, source, text,
    and the fields each one qualifies -- so the client has exactly one
    representation to draw from and cannot fall back to prose.
    """
    if outcome.kind in ("ok", "probe"):
        envelope = dict(outcome.envelope or {})
        if outcome.kind == "ok":
            envelope["caveats"] = attach.structure(list(envelope.get("caveats") or []))
        return {
            "type": "tool_result",
            "id": call_id,
            "tool": outcome.tool,
            "origin": _origin(outcome.tool),
            "params": outcome.params,
            "kind": outcome.kind,
            "envelope": envelope,
            "mcp_ms": outcome.mcp_ms,
        }
    return {
        "type": "tool_refusal",
        "id": call_id,
        "tool": outcome.tool,
        "origin": _origin(outcome.tool),
        "params": outcome.params,
        "kind": outcome.kind,
        "message": outcome.message,
        "caveats": attach.structure(outcome.caveats),
        "mcp_ms": outcome.mcp_ms,
    }
