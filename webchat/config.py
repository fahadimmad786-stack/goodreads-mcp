"""
Configuration, entirely from the environment.

Nothing here has a secret default. Exactly one value is mandatory -- the access
token -- because its failure mode is silent and expensive: an open endpoint
billing someone's account.

The Anthropic key is optional, and what it buys is a mode rather than the
service. Without it the console starts, serves, and answers tool calls; the
chat box is simply not offered, because the only thing the model does here is
choose the tool and write the prose around a card this console draws itself.
"""

from __future__ import annotations

import os


class ConfigError(RuntimeError):
    """A required environment variable is missing or unusable."""


# --- model ------------------------------------------------------------------

MODEL = os.environ.get("CHAT_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("CHAT_MAX_TOKENS", "8000"))
EFFORT = os.environ.get("CHAT_EFFORT", "high")

# --- the MCP server --------------------------------------------------------
#
# Two ways in, and the default is the local one that already exists.
#
#   proxy.sh running   -> GOODREADS_MCP_URL=http://127.0.0.1:8080/mcp and no
#                         credential of our own; the proxy injects it.
#   direct to Cloud Run -> GOODREADS_MCP_URL=https://...run.app/mcp plus
#                         GOODREADS_MCP_AUDIENCE set to the service's base URL,
#                         and we mint an ID token per the audience.
#
# GOODREADS_MCP_TOKEN is the local escape hatch: a token from
# `gcloud auth print-identity-token`, used verbatim. Never set in production --
# there the metadata server mints a fresh one and nothing is stored.

MCP_URL = os.environ.get("GOODREADS_MCP_URL", "http://127.0.0.1:8080/mcp")
MCP_AUDIENCE = os.environ.get("GOODREADS_MCP_AUDIENCE") or None
MCP_TOKEN = os.environ.get("GOODREADS_MCP_TOKEN") or None
MCP_TIMEOUT_S = float(os.environ.get("GOODREADS_MCP_TIMEOUT", "120"))

# --- spend ceilings --------------------------------------------------------

MAX_TOOL_CALLS_PER_TURN = int(os.environ.get("CHAT_MAX_TOOL_CALLS", "6"))
MAX_TURNS_PER_SESSION = int(os.environ.get("CHAT_MAX_TURNS", "25"))
RATE_LIMIT_TURNS = int(os.environ.get("CHAT_RATE_LIMIT_TURNS", "10"))
RATE_LIMIT_WINDOW_S = float(os.environ.get("CHAT_RATE_LIMIT_WINDOW", "300"))
MAX_SESSIONS = int(os.environ.get("CHAT_MAX_SESSIONS", "200"))
SESSION_TTL_S = float(os.environ.get("CHAT_SESSION_TTL", "7200"))
MAX_INPUT_CHARS = int(os.environ.get("CHAT_MAX_INPUT_CHARS", "1000"))

# Tool mode has its own window. A form submission costs BigQuery bytes but no
# Anthropic tokens, and one call per form is a far tighter loop than one call
# per sentence, so sharing the chat window would make the mode unusable long
# before it made it expensive. The MCP server's own 20 GiB per-query ceiling is
# the backstop either way.
TOOL_RATE_LIMIT_CALLS = int(os.environ.get("CHAT_TOOL_RATE_LIMIT_CALLS", "40"))
# A form field is a scalar. Anything longer or deeper is not something this UI
# can have produced, so it is refused before it reaches the MCP client.
MAX_PARAM_CHARS = int(os.environ.get("CHAT_MAX_PARAM_CHARS", "200"))

# --- secrets ---------------------------------------------------------------

# Optional. Present -> chat mode is offered; absent -> tool mode only.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or None

# Required, with no override. An unauthenticated Cloud Run service is the only
# way a browser can reach this, so the shared secret is the whole access
# control. There is deliberately no CHAT_ALLOW_OPEN_ACCESS flag: a flag that
# turns off billing protection is a flag that eventually gets set.
ACCESS_TOKEN = os.environ.get("CHAT_ACCESS_TOKEN") or None

# Two cookies, both HttpOnly: one records that the shared secret was presented,
# one names the server-side transcript.
AUTH_COOKIE = "gr_auth"
SESSION_COOKIE = "gr_sid"
# Off only for plain-HTTP local development; Cloud Run is always HTTPS.
COOKIE_SECURE = os.environ.get("CHAT_COOKIE_SECURE", "1") != "0"


def chat_enabled() -> bool:
    """
    Whether the model-backed mode may be offered.

    Read at call time, not captured at import, so a test can turn the mode off
    with monkeypatch and see the same code path a keyless deployment takes.
    """
    return bool(ANTHROPIC_API_KEY)


def verify() -> None:
    """Fail at startup rather than per request. Called from app factory."""
    missing = []
    if not ACCESS_TOKEN:
        missing.append(
            "CHAT_ACCESS_TOKEN -- the shared secret that gates this service. "
            "Generate one with `openssl rand -hex 24`. Without it the endpoint "
            "would be open to anyone with the URL, billing the Anthropic "
            "account on every turn and BigQuery on every tool call."
        )
    if missing:
        raise ConfigError(
            "refusing to start; missing required environment:\n  - "
            + "\n  - ".join(missing)
        )
    if MCP_AUDIENCE and MCP_TOKEN:
        raise ConfigError(
            "GOODREADS_MCP_AUDIENCE and GOODREADS_MCP_TOKEN are both set; the "
            "first mints a token per request, the second uses a fixed one. "
            "Pick one."
        )


def public_settings() -> dict:
    """Non-secret settings, safe to expose on /api/health."""
    return {
        # The client picks its default mode from this, and hides the chat box
        # entirely when it is false -- there is no half-working chat box.
        "chat_enabled": chat_enabled(),
        "model": MODEL if chat_enabled() else None,
        "mcp_url": MCP_URL,
        "auth_mode": (
            "oidc" if MCP_AUDIENCE else "static-token" if MCP_TOKEN else "proxy"
        ),
        "max_tool_calls_per_turn": MAX_TOOL_CALLS_PER_TURN,
        "max_turns_per_session": MAX_TURNS_PER_SESSION,
        "tool_rate_limit_calls": TOOL_RATE_LIMIT_CALLS,
        "rate_limit_window_s": RATE_LIMIT_WINDOW_S,
    }
