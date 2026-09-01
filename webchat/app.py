"""
The HTTP surface: static assets, a health probe, and two ways to reach a tool.

`/api/chat` streams a model turn; `/api/run` invokes one tool with parameters a
person filled in, and `/api/tools` hands the client the schemas to build that
form from. Chat needs an Anthropic key and is simply not offered without one;
the tool routes need none, so the service starts and is fully useful with no
model behind it at all.

Both paths end in `MCPBridge.call()` and both return frames built by
`frames.py`, so the card, the caveats, the n/unit/threshold block and the
charts are identical whichever one fetched the envelope.

This service is the only publicly reachable part of the system, so everything
that limits spend lives here: a required shared secret, a per-IP window, a
per-session turn cap, and a per-turn tool-call cap. The MCP server behind it
stays IAM-private and is reached with an identity token minted per request.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import config, frames
from .mcp_client import MCPBridge
from .session import RateLimiter, SessionStore

STATIC = Path(__file__).parent / "static"

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("webchat")


# --------------------------------------------------------------------------
# access control
# --------------------------------------------------------------------------


def _authorised(request: Request) -> bool:
    """
    The shared secret, from the cookie, a header, or `?k=` on the first visit.

    Compared with compare_digest so a wrong token cannot be found a character
    at a time.
    """
    expected = config.ACCESS_TOKEN or ""
    for candidate in (
        request.cookies.get(config.AUTH_COOKIE),
        request.headers.get("x-chat-access"),
        request.query_params.get("k"),
    ):
        if candidate and secrets.compare_digest(candidate, expected):
            return True
    return False


def _set_auth_cookie(response, request: Request) -> None:
    if request.query_params.get("k"):
        response.set_cookie(
            config.AUTH_COOKIE,
            config.ACCESS_TOKEN or "",
            httponly=True,
            secure=config.COOKIE_SECURE,
            samesite="lax",
            max_age=60 * 60 * 12,
        )


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


async def index(request: Request) -> HTMLResponse:
    if not _authorised(request):
        return HTMLResponse(_locked_page(), status_code=401)
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    response = HTMLResponse(html)
    _set_auth_cookie(response, request)
    return response


async def health(request: Request) -> JSONResponse:
    """
    Public liveness only. Configuration detail, including the MCP server's URL,
    is behind the shared secret -- the URL is not a credential, but there is no
    reason to publish it to anonymous callers.
    """
    if not _authorised(request):
        return JSONResponse({"status": "ok"})
    bridge: MCPBridge = request.app.state.bridge
    return JSONResponse(
        {
            "status": "ok",
            "service": "goodreads-chat",
            **config.public_settings(),
            **await bridge.connect_check(),
            "sessions": len(request.app.state.sessions),
        }
    )


async def chat(request: Request) -> StreamingResponse | JSONResponse:
    if not _authorised(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)

    # The client hides the chat box when /api/health says the mode is off, so
    # reaching here means a stale page or a direct caller. Say which mode is
    # available rather than 500ing inside the Anthropic SDK.
    if not config.chat_enabled():
        return JSONResponse(
            {
                "error": (
                    "chat mode is off: this deployment has no ANTHROPIC_API_KEY. "
                    "The tool mode needs no model — pick a tool and fill in its "
                    "parameters instead."
                )
            },
            status_code=503,
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "expected a JSON body"}, status_code=400)

    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)
    if len(text) > config.MAX_INPUT_CHARS:
        return JSONResponse(
            {"error": f"message longer than {config.MAX_INPUT_CHARS} characters"},
            status_code=400,
        )

    allowed, retry_in = request.app.state.limiter.check(_client_key(request))
    if not allowed:
        return JSONResponse(
            {
                "error": (
                    f"rate limit: {config.RATE_LIMIT_TURNS} turns per "
                    f"{int(config.RATE_LIMIT_WINDOW_S / 60)} minutes. "
                    f"Try again in {retry_in}s."
                )
            },
            status_code=429,
        )

    store: SessionStore = request.app.state.sessions
    session, is_new = store.get_or_new(request.cookies.get(config.SESSION_COOKIE))

    if session.turns_left <= 0:
        return JSONResponse(
            {
                "error": (
                    f"this session has used its {config.MAX_TURNS_PER_SESSION} "
                    "turns. Reload to start a new one."
                )
            },
            status_code=429,
        )

    agent = request.app.state.agent

    async def stream() -> AsyncIterator[bytes]:
        yield _sse(
            {
                "type": "session",
                "is_new": is_new,
                "turns_left": session.turns_left,
                "restarted": is_new
                and bool(request.cookies.get(config.SESSION_COOKIE)),
            }
        )
        try:
            async for frame in agent.run_turn(session, text):
                yield _sse(frame)
        except Exception as exc:  # noqa: BLE001 -- a dead stream tells nobody why
            log.exception("turn failed")
            yield _sse({"type": "error", "message": f"turn failed: {type(exc).__name__}"})
        yield b"event: done\ndata: {}\n\n"

    response = StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            # Cloud Run buffers responses unless told otherwise; without this
            # the whole turn arrives at once and streaming is decorative.
            "X-Accel-Buffering": "no",
        },
    )
    response.set_cookie(
        config.SESSION_COOKIE,
        session.id,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        max_age=int(config.SESSION_TTL_S),
    )
    return response


async def tools(request: Request) -> JSONResponse:
    """
    The tool surface with its JSON Schemas, for the no-model mode's forms.

    Exactly what the model is given, reshaped -- see `MCPBridge.catalogue()`.
    The form is generated from it in the browser; nothing about any tool's
    parameters is written down in this package or in the client.
    """
    if not _authorised(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    bridge: MCPBridge = request.app.state.bridge
    try:
        catalogue = await bridge.catalogue()
    except Exception as exc:  # noqa: BLE001
        log.warning("tool discovery failed: %s", type(exc).__name__)
        return JSONResponse(
            {"error": f"could not reach the MCP server ({type(exc).__name__})"},
            status_code=503,
        )
    return JSONResponse({"tools": catalogue, "chat_enabled": config.chat_enabled()})


async def run(request: Request) -> JSONResponse:
    """
    One tool call with caller-supplied parameters, no model involved.

    The parameters are passed **verbatim** to the server. That is the point of
    this mode: a rejected argument is a result here, and substituting a safe
    default would replace the server's own explanation with silence. So the
    only checks made here are the ones that keep this route from being a
    general-purpose proxy -- a known tool name, a flat object, scalar values --
    never a check on whether a value is one the tool will like.
    """
    if not _authorised(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "expected a JSON body"}, status_code=400)

    bridge: MCPBridge = request.app.state.bridge
    try:
        known = {t["name"] for t in await bridge.catalogue()}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"error": f"could not reach the MCP server ({type(exc).__name__})"},
            status_code=503,
        )

    name = body.get("tool")
    if name not in known:
        return JSONResponse({"error": "no such tool"}, status_code=400)

    params = body.get("params") or {}
    problem = _reject_params(params)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)

    allowed, retry_in = request.app.state.tool_limiter.check(_client_key(request))
    if not allowed:
        return JSONResponse(
            {
                "error": (
                    f"rate limit: {config.TOOL_RATE_LIMIT_CALLS} tool calls per "
                    f"{int(config.RATE_LIMIT_WINDOW_S / 60)} minutes. "
                    f"Try again in {retry_in}s."
                )
            },
            status_code=429,
        )

    outcome = await bridge.call(name, params)
    return JSONResponse(
        frames._result_frame(f"run_{secrets.token_hex(6)}", outcome), status_code=200
    )


def _reject_params(params: object) -> str | None:
    """
    Structural limits only: this is a form, so its values are scalars.

    Nothing here judges a value. `min_ratings=0` and `unit="chapters"` both
    pass through untouched, because the server's refusal is the thing worth
    seeing.
    """
    if not isinstance(params, dict):
        return "params must be an object"
    if len(params) > 32:
        return "too many parameters"
    for key, value in params.items():
        if not isinstance(key, str) or not key.replace("_", "").isalnum():
            return "parameter names must be bare identifiers"
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            continue
        if isinstance(value, str):
            if len(value) > config.MAX_PARAM_CHARS:
                return f"parameter {key} is longer than {config.MAX_PARAM_CHARS} characters"
            continue
        return f"parameter {key} must be a string, number, boolean or null"
    return None


def _sse(frame: dict) -> bytes:
    return f"data: {json.dumps(frame, default=str)}\n\n".encode()


def _locked_page() -> str:
    return (
        "<!doctype html><meta charset=utf-8>"
        "<title>goodreads-stats</title>"
        "<style>body{font:15px/1.6 ui-sans-serif,system-ui,sans-serif;"
        "max-width:34rem;margin:18vh auto;padding:0 1.5rem;color:#1b1b1d}"
        "code{font-family:ui-monospace,monospace;background:#f0efec;padding:.1em .35em}"
        "</style>"
        "<h1 style='font-size:1.25rem;font-weight:600'>goodreads-stats console</h1>"
        "<p>This console needs an access key. Open it with "
        "<code>?k=&lt;key&gt;</code> appended to the URL.</p>"
        "<p style='color:#6b6b70'>The key exists because the service pays for "
        "what it serves: BigQuery bytes for every tool call, and Anthropic "
        "tokens too when the chat mode is configured.</p>"
    )


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------


def create_app() -> Starlette:
    config.verify()

    bridge = MCPBridge()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        """
        Fetch the tool list once at startup rather than on the first turn.

        Non-fatal: the console should load and report the failure per turn
        rather than refuse to boot because the MCP server is briefly away.
        """
        log.info(
            "goodreads-chat starting: mode=%s model=%s mcp=%s auth=%s",
            "chat+tools" if config.chat_enabled() else "tools only (no ANTHROPIC_API_KEY)",
            config.MODEL if config.chat_enabled() else "-",
            config.MCP_URL,
            bridge.auth_mode,
        )
        try:
            tools, _ = await bridge.describe()
            log.info("discovered %d tools from the MCP server", len(tools))
        except Exception as exc:  # noqa: BLE001
            log.warning("tool discovery failed at startup: %s", type(exc).__name__)
        yield

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/", index, methods=["GET"]),
            Route("/api/health", health, methods=["GET"]),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/tools", tools, methods=["GET"]),
            Route("/api/run", run, methods=["POST"]),
            Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
        ],
    )
    app.state.bridge = bridge
    # Built only when there is a key. Constructing an Anthropic client without
    # one is the kind of latent failure this service is built to avoid, and it
    # would also import the SDK into a deployment that has no use for it.
    app.state.agent = _make_agent(bridge) if config.chat_enabled() else None
    app.state.sessions = SessionStore()
    app.state.limiter = RateLimiter()
    app.state.tool_limiter = RateLimiter(
        limit=config.TOOL_RATE_LIMIT_CALLS, window_s=config.RATE_LIMIT_WINDOW_S
    )
    return app


def _make_agent(bridge: MCPBridge):
    """Imported here, not at module scope: no key, no Anthropic SDK import."""
    from .agent import Agent

    return Agent(bridge)


def main() -> None:
    import uvicorn

    import os

    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8081")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
