# syntax=docker/dockerfile:1
#
# Image for the goodreads-chat service. Built from the repository root -- it
# needs `goodreads_mcp` for the caveat registry and the query guard -- but it is
# a separate image from the MCP server's, so adding the Anthropic SDK and a web
# framework here does not change what runs behind the IAM boundary.
#
# Build it with cloudbuild-chat.yaml (the root Dockerfile builds the MCP
# server, and `gcloud run deploy --source` only ever picks that one up).
#
# No credentials are copied in. The Anthropic key arrives from Secret Manager
# as an env var; the Cloud Run identity token is minted per request from the
# metadata server.

# ---- build ----------------------------------------------------------------
FROM python:3.14-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY goodreads_mcp ./goodreads_mcp
COPY webchat ./webchat

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install '.[web]'

# ---- runtime --------------------------------------------------------------
FROM python:3.14-slim

COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

RUN useradd --system --uid 10001 --no-create-home app
USER 10001

EXPOSE 8080

# No shell, so signals reach the process and Cloud Run drains instances cleanly.
CMD ["python", "-m", "webchat"]
