"""
The MCP side of the BFF: an MCP client, not a re-description of the server.

Tool schemas come from `tools/list` and the model's steering text comes from
the server's own `instructions`, so nothing about the tool surface is restated
here. A docstring edit in server.py reaches the UI on the next deploy without
a change to this package.

Credentials never leave this module. The Cloud Run identity token is minted
from the metadata server, cached until shortly before it expires, and attached
as a request header; it is not logged, not returned, and not placed in any SSE
frame.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from . import attach, config, guard_probe

log = logging.getLogger("webchat.mcp")

# The three guard messages in bq.guard(). Matched on a distinctive substring so
# a guard rejection is classified as a refusal rather than a crash, without
# depending on the exception class surviving the MCP boundary (it does not --
# it arrives as error text).
_GUARD_SIGNATURES = (
    "which is a placeholder for most",
    "references the raw `language` column",
    "on a 1.85M-row table; name the columns",
)


@dataclass
class ToolOutcome:
    """One tool call, classified for rendering."""

    tool: str
    params: dict[str, Any]
    kind: str  # ok | param_error | guard | transport | probe
    envelope: dict[str, Any] | None = None
    message: str = ""
    mcp_ms: float = 0.0
    caveats: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind in ("ok", "probe")

    def for_model(self) -> str:
        """
        What the model sees. The full envelope, including caveats -- the model
        needs them to explain a figure's limits, it just may not restate the
        figure itself.
        """
        if self.envelope is not None:
            return json.dumps(self.envelope, default=str)
        return json.dumps({"error": self.message, "kind": self.kind})

    def for_model_dict(self) -> dict:
        """
        The same content as an object, for a provider whose tool-result slot
        takes one.

        Gemini's `functionResponse.response` is a struct, not a string. Routed
        through `for_model()` and back rather than built separately, so the two
        providers cannot come to show the model different things -- the
        round-trip is also what applies `default=str` to whatever BigQuery
        returned that JSON has no opinion about.
        """
        return json.loads(self.for_model())


def classify_error(text: str) -> str:
    """
    guard | schema | transport, from an MCP error string.

    `schema` is the layer above ParamError and is easy to miss: a parameter
    declared `Annotated[int, Field(ge=1)]` is validated by FastMCP against the
    tool schema BEFORE the tool body runs, so `require_min_ratings()` never
    executes and its explanation never reaches the caller. That arrives as a
    pydantic validation error, and it is a refusal -- an informative result --
    not a transport failure.
    """
    lowered = text.lower()
    if any(sig.lower() in lowered for sig in _GUARD_SIGNATURES):
        return "guard"
    if "queryguarderror" in lowered:
        return "guard"
    if "validation error" in lowered or "input should be" in lowered:
        return "schema"
    return "transport"


def tidy_validation(text: str) -> str:
    """
    Pydantic's message without its machine-readable tail.

    "Input should be greater than or equal to 1 [type=greater_than_equal,
    input_value=0, ...]" -> the sentence, which is the part a reader needs.
    """
    lines = []
    for line in text.splitlines():
        line = line.split(" [type=")[0].strip()
        if not line or line.startswith("For further information visit"):
            continue
        lines.append(line)
    return "\n".join(lines)


class TokenSource:
    """
    Cloud Run identity tokens, minted for the service's audience and cached.

    With no audience configured we send nothing: that is the `proxy.sh` path,
    where `gcloud run services proxy` injects credentials for us.
    """

    def __init__(self, audience: str | None, static_token: str | None):
        self._audience = audience
        self._static = static_token
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        if self._audience:
            return "oidc"
        return "static-token" if self._static else "proxy"

    async def header(self) -> dict[str, str]:
        if self._static:
            return {"Authorization": f"Bearer {self._static}"}
        if not self._audience:
            return {}
        token = await self._fresh()
        return {"Authorization": f"Bearer {token}"}

    async def _fresh(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            # Blocking call against the metadata server; keep the loop free.
            token = await asyncio.to_thread(self._mint)
            self._token = token
            # Refresh five minutes early. Falls back to 45 minutes if the
            # token carries no readable exp.
            self._expires_at = (_jwt_exp(token) or (time.time() + 3000)) - 300
            return token

    def _mint(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        return id_token.fetch_id_token(Request(), self._audience)


def _jwt_exp(token: str) -> float | None:
    """`exp` from an unverified JWT payload. Google verifies it; we only cache."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:  # noqa: BLE001 -- a bad token is the caller's problem
        return None


class MCPBridge:
    """Tool discovery and invocation against the goodreads-stats MCP server."""

    def __init__(
        self,
        url: str | None = None,
        audience: str | None = None,
        static_token: str | None = None,
    ):
        self.url = url or config.MCP_URL
        self._tokens = TokenSource(
            audience if audience is not None else config.MCP_AUDIENCE,
            static_token if static_token is not None else config.MCP_TOKEN,
        )
        self._tools: list[dict] | None = None
        self._instructions: str = ""
        self._lock = asyncio.Lock()

    @property
    def auth_mode(self) -> str:
        return self._tokens.mode

    async def _client(self) -> Client:
        headers = await self._tokens.header()
        return Client(
            StreamableHttpTransport(self.url, headers=headers),
            timeout=config.MCP_TIMEOUT_S,
        )

    async def connect_check(self) -> dict:
        """For /api/health. Cheap: initialize plus tools/list, no tool call."""
        try:
            await self.describe()
            return {"mcp": "ok", "tools": len(self._tools or []), "auth": self.auth_mode}
        except Exception as exc:  # noqa: BLE001
            return {"mcp": "unreachable", "error": _safe(str(exc)), "auth": self.auth_mode}

    async def describe(self, refresh: bool = False) -> tuple[list[dict], str]:
        """
        (anthropic tool definitions, server instructions).

        Cached for the process. Tool order is sorted by name so the `tools`
        array is byte-stable across requests, which is what makes it a usable
        prompt-cache prefix. The guard probe is appended last, after every MCP
        tool, so the MCP prefix does not shift if the probe changes.
        """
        async with self._lock:
            if self._tools is not None and not refresh:
                return self._tools, self._instructions
            async with await self._client() as client:
                tools = await client.list_tools()
                init = getattr(client, "initialize_result", None)
                self._instructions = (getattr(init, "instructions", "") or "").strip()
            defs = [
                {
                    "name": t.name,
                    "description": (t.description or "").strip(),
                    "input_schema": t.inputSchema,
                }
                for t in sorted(tools, key=lambda t: t.name)
            ]
            defs.append(guard_probe.anthropic_tool())
            self._tools = defs
            return self._tools, self._instructions

    async def catalogue(self, refresh: bool = False) -> list[dict]:
        """
        The tool surface, for the no-model mode's parameter forms.

        Deliberately `describe()`'s own cached output, reshaped -- not a second
        fetch and not a second description. The form the user fills in is
        generated from the exact JSON Schema the model is given, so a `Field`
        description edited in `server.py` moves both at once and neither can
        drift from the other.

        `origin` is the only thing added, and it is the same distinction the
        cards draw: everything from `tools/list` is `mcp`, the guard probe is
        `bff`.
        """
        tools, _ = await self.describe(refresh=refresh)
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "schema": t["input_schema"] or {"type": "object", "properties": {}},
                "origin": "bff" if t["name"] == guard_probe.TOOL_NAME else "mcp",
            }
            for t in tools
        ]

    @property
    def mcp_tool_names(self) -> set[str]:
        return {t["name"] for t in (self._tools or []) if t["name"] != guard_probe.TOOL_NAME}

    async def call(self, name: str, params: dict[str, Any]) -> ToolOutcome:
        """
        Invoke one tool and classify the result.

        A `ParamError` is not an exception on the wire: the server turns it
        into `{"error": ..., "data": [], "caveats": [...]}` so a caller can act
        on it. That shape is detected here and rendered as a refusal, which is
        what it is -- an informative result, not a failure.
        """
        if name == guard_probe.TOOL_NAME:
            started = time.perf_counter()
            data = guard_probe.probe(str(params.get("column", "")))
            return ToolOutcome(
                tool=name,
                params=params,
                kind="probe",
                envelope=data,
                mcp_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        started = time.perf_counter()
        try:
            async with await self._client() as client:
                result = await client.call_tool(name, params, raise_on_error=False)
        except Exception as exc:  # noqa: BLE001 -- transport, auth, timeout
            return ToolOutcome(
                tool=name,
                params=params,
                kind="transport",
                message=_safe(str(exc)),
                mcp_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        mcp_ms = round((time.perf_counter() - started) * 1000, 2)

        if result.is_error:
            text = _result_text(result)
            kind = classify_error(text)
            if kind == "schema":
                # The schema rejected an argument before the tool ran, so the
                # server's own reasoning for the constraint is not in the
                # error. Put it back from the caveat registry -- these are the
                # server's words about its own data, not new prose from here.
                return ToolOutcome(
                    tool=name,
                    params=params,
                    kind=kind,
                    message=_safe(tidy_validation(text)),
                    caveats=_param_caveat_text(params),
                    mcp_ms=mcp_ms,
                )
            return ToolOutcome(
                tool=name,
                params=params,
                kind=kind,
                message=_safe(text),
                mcp_ms=mcp_ms,
            )

        payload = result.data
        if payload is None:
            payload = result.structured_content
        if not isinstance(payload, dict):
            return ToolOutcome(
                tool=name,
                params=params,
                kind="transport",
                message="tool returned a non-object result",
                mcp_ms=mcp_ms,
            )

        if "error" in payload:
            return ToolOutcome(
                tool=name,
                params=params,
                kind="param_error",
                envelope=payload,
                message=str(payload["error"]),
                caveats=list(payload.get("caveats") or []),
                mcp_ms=mcp_ms,
            )

        return ToolOutcome(
            tool=name, params=params, kind="ok", envelope=payload, mcp_ms=mcp_ms
        )


def _param_caveat_text(params: dict[str, Any]) -> list[str]:
    """Rendered caveats for the parameters this call passed, deduplicated."""
    seen: dict[str, None] = {}
    for name in params:
        for caveat in attach.caveats_for_param(name):
            seen.setdefault(f"[{caveat['source']}] {caveat['text']}", None)
    return list(seen)


def _result_text(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) or "tool call failed with no message"


def _safe(text: str) -> str:
    """
    Never let a credential reach a client or a log line.

    Transport errors can quote request headers. Cheap belt-and-braces on top of
    never putting the token anywhere but the header itself.
    """
    out = text
    for needle in ("Authorization", "Bearer ", "authorization"):
        if needle in out:
            out = out.replace(needle, "[redacted]")
    return out[:2000]
