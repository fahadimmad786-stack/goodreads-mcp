#!/usr/bin/env bash
#
# Authenticated local tunnel to the Cloud Run service.
#
# Claude Code's `goodreads-remote` server points at http://127.0.0.1:8080/mcp
# and sends no credentials of its own -- this process injects them. It must be
# running for that server to connect.
#
# Needs the STANDALONE Cloud SDK: the `cloud-run-proxy` component cannot be
# installed into a distro-packaged gcloud ("managed by an external package
# manager"). CLOUDSDK_ROOT_DIR is cleared because a distro install exports it
# and it would point the standalone gcloud at the wrong root.

set -euo pipefail

GCLOUD="${GCLOUD:-$HOME/google-cloud-sdk/bin/gcloud}"
PROJECT="${PROJECT:-example-project}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-goodreads-mcp}"
PORT="${PORT:-8080}"

if [ ! -x "${GCLOUD}" ]; then
  echo "standalone gcloud not found at ${GCLOUD}" >&2
  echo "install: https://cloud.google.com/sdk/  then: gcloud components install cloud-run-proxy" >&2
  exit 1
fi

exec env -u CLOUDSDK_ROOT_DIR "${GCLOUD}" run services proxy "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" --port "${PORT}"
