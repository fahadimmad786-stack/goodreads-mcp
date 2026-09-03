"""
The HTTP surface: static assets, a health probe, and two ways to reach a tool.

`/api/chat` streams a model turn; `/api/run` invokes one tool with parameters a
person filled in, and `/api/tools` hands the client the schemas to build that
form from. `/api/telemetry` summarises the local telemetry log through the
CLI's own functions. `/robots.txt` and a response header ask every crawler to stay out:
the console is private, its URL carries an access key, and nothing here should
ever appear in an index. Chat needs an Anthropic key and is simply not offered without one;
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
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from goodreads_mcp import telemetry_cli

from . import config, frames, pages
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
        return _locked(request)
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    response = HTMLResponse(html)
    response.headers["Cache-Control"] = "no-store"
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
    # available rather than 500ing inside a model SDK.
    if not config.chat_enabled():
        return JSONResponse(
            {
                "error": (
                    "chat mode is off: this deployment has neither "
                    "ANTHROPIC_API_KEY nor GEMINI_API_KEY. Either one turns it "
                    "on, and Google AI Studio's free tier needs no paid "
                    "credits. The tool mode needs no model at all — pick a tool "
                    "and fill in its parameters instead."
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


async def telemetry(request: Request) -> JSONResponse:
    """
    The local telemetry log, summarised by the summariser's own code.

    `telemetry_cli.load()` and `summarise()` are the functions behind the
    `goodreads-telemetry` command; this route calls them and adds nothing, so
    the console and the CLI cannot disagree about a figure. The log it reads
    is the one the server writes when it runs on this machine under stdio --
    hence `scope: local-session`. A deployed server writes structured lines to
    Cloud Logging instead, and there is deliberately no path from here to
    those: the console holds no logging credential.

    A missing log is an answer, not an error: `exists: false` with the path
    that was looked for, so the view can say so.
    """
    if not _authorised(request):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    path = telemetry_cli.log_path()
    base = {"scope": "local-session", "path": str(path)}
    if str(path) == "-" or not path.exists():
        return JSONResponse({**base, "exists": False, "calls": 0})
    try:
        rows, malformed = telemetry_cli.load(path, tool=None, since=None)
    except (OSError, ValueError) as exc:
        return JSONResponse(
            {**base, "exists": True, "error": f"could not read the log ({type(exc).__name__})"},
            status_code=500,
        )
    summary = telemetry_cli.summarise(rows)
    # The CLI's render step derives each parameter value's share of calls with
    # `pct()`; the same function, so the view's share is the CLI's share.
    summary["params_pct"] = {
        key: {value: telemetry_cli.pct(n, summary["calls"]) for value, n in counts.items()}
        for key, counts in summary["params"].items()
    }
    return JSONResponse({**base, "exists": True, "malformed": malformed, **summary})


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


def _locked(request: Request) -> HTMLResponse:
    """
    401 as a page, not as a bare status line.

    Someone who reaches this is either a person who mislaid the URL or a
    stranger who guessed the host. The page explains what `?k=` is and where
    the key is kept; it contains no key and no hint of one.
    """
    response = HTMLResponse(pages.locked(), status_code=401)
    response.headers["Cache-Control"] = "no-store"
    return response


async def not_found(request: Request, exc: Exception) -> HTMLResponse | JSONResponse:
    """
    404 in the shape the caller was asking for.

    An unknown /api/ path gets JSON, because that is what a fetch() there can
    read; anything else gets the styled page.
    """
    path = request.url.path
    if path.startswith("/api/"):
        return JSONResponse({"error": "no such endpoint"}, status_code=404)
    return HTMLResponse(pages.not_found(path), status_code=404)


async def robots(request: Request) -> PlainTextResponse:
    """
    Deliberately anti-SEO, and deliberately public.

    This is the one thing on the service that must be readable without the
    access token: a crawler that cannot fetch robots.txt does not learn to
    stay away. `X-Robots-Tag` on every response is the belt to this braces --
    robots.txt asks a crawler not to fetch, the header tells one that fetched
    anyway not to index.
    """
    return PlainTextResponse(
        "# This console is private and token-gated. Nothing here is for indexing.\n"
        "User-agent: *\n"
        "Disallow: /\n",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------


# The page loads its own stylesheet and its own modules and nothing else: no
# fonts, no CDN, no analytics, no third-party anything. A CSP is the only way
# to say that in a form a browser will enforce, so a future edit that adds an
# external asset fails loudly in the console instead of quietly shipping.
#
# `img-src data:` is for the inline SVG favicon. There is no 'unsafe-inline'
# anywhere: no markup in this service carries a <style> block or a style
# attribute. (Assigning `element.style.x` from a module is unaffected -- CSP
# governs style parsed from markup, not the CSSOM.)
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    # Anti-indexing, the header half. robots.txt asks a crawler not to fetch;
    # this tells one that fetched anyway not to index, and applies to every
    # response including the static assets and the JSON routes.
    "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet, noimageindex",
    # The URL can carry ?k=<access token>. Without this, following any
    # outbound link would put that token in a third party's Referer log. The
    # page has no outbound links, and this makes that non-negotiable rather
    # than merely true today.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CSP,
}


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


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
            "goodreads-chat starting: mode=%s provider=%s model=%s mcp=%s auth=%s",
            "chat+tools" if config.chat_enabled()
            else "tools only (no ANTHROPIC_API_KEY or GEMINI_API_KEY)",
            config.active_provider() or "-",
            config.active_model() or "-",
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
        middleware=[Middleware(SecurityHeaders)],
        exception_handlers={404: not_found},
        routes=[
            Route("/", index, methods=["GET"]),
            Route("/robots.txt", robots, methods=["GET"]),
            Route("/api/health", health, methods=["GET"]),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/tools", tools, methods=["GET"]),
            Route("/api/run", run, methods=["POST"]),
            Route("/api/telemetry", telemetry, methods=["GET"]),
            Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
        ],
    )
    app.state.bridge = bridge
    # Built only when there is a key. Constructing a model client without one is
    # the kind of latent failure this service is built to avoid, and it would
    # also import an SDK into a deployment that has no use for it.
    app.state.agent = _make_agent(bridge) if config.chat_enabled() else None
    app.state.sessions = SessionStore()
    app.state.limiter = RateLimiter()
    app.state.tool_limiter = RateLimiter(
        limit=config.TOOL_RATE_LIMIT_CALLS, window_s=config.RATE_LIMIT_WINDOW_S
    )
    return app


def _make_agent(bridge: MCPBridge):
    """
    Imported here, not at module scope: no key, no model SDK import at all.

    `Agent` selects its provider, and `provider.select()` imports only the one
    SDK it picked -- so a Gemini deployment never loads the Anthropic package
    and a keyless one loads neither.
    """
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
