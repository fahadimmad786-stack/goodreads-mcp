"""
The HTTP surface: static assets, a health probe, and one streaming chat route.

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

from . import config
from .agent import Agent
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

    agent: Agent = request.app.state.agent

    async def frames() -> AsyncIterator[bytes]:
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
        frames(),
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
        "every turn: Anthropic tokens for the model, and BigQuery bytes for each "
        "tool call.</p>"
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
            "goodreads-chat starting: model=%s mcp=%s auth=%s",
            config.MODEL,
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
            Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
        ],
    )
    app.state.bridge = bridge
    app.state.agent = Agent(bridge)
    app.state.sessions = SessionStore()
    app.state.limiter = RateLimiter()
    return app


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
