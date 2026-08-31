"""
Configuration, entirely from the environment.

Nothing here has a secret default. Two values are mandatory and the process
refuses to start without them, because both failure modes are silent and
expensive: no Anthropic key means every turn 500s, and no access token means
an open endpoint billing someone's account.
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

# --- secrets ---------------------------------------------------------------

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


def verify() -> None:
    """Fail at startup rather than per request. Called from app factory."""
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append(
            "ANTHROPIC_API_KEY -- the Anthropic API key. In production supply "
            "it from Secret Manager (--set-secrets), never --set-env-vars."
        )
    if not ACCESS_TOKEN:
        missing.append(
            "CHAT_ACCESS_TOKEN -- the shared secret that gates this service. "
            "Generate one with `openssl rand -hex 24`. Without it the endpoint "
            "would be open to anyone with the URL, billing the Anthropic "
            "account on every turn."
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
        "model": MODEL,
        "mcp_url": MCP_URL,
        "auth_mode": (
            "oidc" if MCP_AUDIENCE else "static-token" if MCP_TOKEN else "proxy"
        ),
        "max_tool_calls_per_turn": MAX_TOOL_CALLS_PER_TURN,
        "max_turns_per_session": MAX_TURNS_PER_SESSION,
    }
