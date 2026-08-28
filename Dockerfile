# syntax=docker/dockerfile:1
#
# Cloud Run image. HTTP transport only -- stdio is a local concern and is
# never exercised in here.
#
# No credentials are copied in and GOOGLE_APPLICATION_CREDENTIALS is never
# set: the container gets Application Default Credentials from the Cloud Run
# metadata server, as the service account attached to the revision.

# ---- build ----------------------------------------------------------------
FROM python:3.14-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY goodreads_mcp ./goodreads_mcp

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install .

# ---- runtime --------------------------------------------------------------
FROM python:3.14-slim

COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    # Cloud Run reclaims instances without draining buffers; unbuffered stdout
    # is what makes the telemetry sink reliable rather than lossy.
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GOODREADS_TRANSPORT=http \
    GOODREADS_TELEMETRY=1 \
    GOODREADS_TELEMETRY_SINK=stdout \
    # 20 GiB. Set explicitly rather than relying on the default in bq.py, so
    # the ceiling is visible in `docker inspect` and in the revision's env.
    GOODREADS_MAX_BYTES_BILLED=21474836480 \
    PORT=8080

RUN useradd --system --uid 10001 --no-create-home app
USER 10001

EXPOSE 8080

# Reads $PORT and binds 0.0.0.0 via server.main(); no shell, so signals reach
# the process directly and Cloud Run can shut instances down cleanly.
CMD ["python", "-m", "goodreads_mcp"]
