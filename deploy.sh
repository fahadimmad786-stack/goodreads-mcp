#!/usr/bin/env bash
#
# Deploy goodreads-mcp to Cloud Run.
#
# Read README.md "Deploying to Cloud Run" before running this. In particular
# MIN_INSTANCES below is a cost/latency knob, not a fixed decision.
#
# Idempotent: re-running creates a new revision and re-asserts the IAM
# bindings. Creating the service account is the only step that is skipped if
# it already exists.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT="${PROJECT:-example-project}"
REGION="${REGION:-us-central1}"        # matches the dataset's US location
SERVICE="${SERVICE:-goodreads-mcp}"
DATASET="${DATASET:-goodreads}"
SA_NAME="${SA_NAME:-goodreads-mcp-run}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# The principal allowed to invoke the service. Everyone else gets 403.
# Defaults to the account running this script -- binding anyone else would
# deploy something the deployer cannot reach. Add others afterwards with:
#   gcloud run services add-iam-policy-binding SERVICE --region REGION \
#     --member='user:someone@example.com' --role=roles/run.invoker
INVOKER="${INVOKER:-user:$(gcloud config get-value account 2>/dev/null)}"

# --- THE KNOB --------------------------------------------------------------
# 1  keeps one instance warm: first call stays at the normal ~3.5s+ instead of
#    paying a ~3-5s cold start on top. Costs one always-on small instance.
# 0  costs nothing at idle; the first call after a scale-to-zero pays the cold
#    start. Fine for batch or tolerant use, poor for interactive MCP.
# Change here, or override: MIN_INSTANCES=0 ./deploy.sh
MIN_INSTANCES="${MIN_INSTANCES:-1}"
MAX_INSTANCES="${MAX_INSTANCES:-4}"
# ---------------------------------------------------------------------------

CPU="${CPU:-1}"
MEMORY="${MEMORY:-512Mi}"
MAX_BYTES_BILLED="${MAX_BYTES_BILLED:-21474836480}"   # 20 GiB

echo "project=${PROJECT} region=${REGION} service=${SERVICE}"
echo "invoker=${INVOKER}"
echo "min-instances=${MIN_INSTANCES}  (see README: cost/latency knob)"
echo

# ---------------------------------------------------------------------------
# 0. APIs
# ---------------------------------------------------------------------------
# `gcloud run deploy --source` needs all three. Enabled explicitly so the
# script is reproducible rather than depending on an interactive prompt.

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project "${PROJECT}"

# ---------------------------------------------------------------------------
# 1. Service account -- least privilege, no keys
# ---------------------------------------------------------------------------

if gcloud iam service-accounts describe "${SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "service account ${SA} already exists"
else
  gcloud iam service-accounts create "${SA_NAME}" \
    --project "${PROJECT}" \
    --display-name "goodreads-mcp Cloud Run runtime"
fi

# bigquery.jobs.create. Project-scoped because job creation cannot be granted
# on a dataset. Without it every tool call fails.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SA}" \
  --role="roles/bigquery.jobUser" \
  --condition=None >/dev/null

# Container stdout -> Cloud Logging, which is where telemetry goes in HTTP
# mode. A custom runtime SA without this has its logs silently dropped.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SA}" \
  --role="roles/logging.logWriter" \
  --condition=None >/dev/null

# Read the two tables -- scoped to the ONE dataset, not the project, so the SA
# cannot read anything else.
#
# add-iam-policy-binding rather than a `bq show | edit | bq update --source`
# round-trip: that pattern feeds output-only fields (etag, creationTime,
# selfLink) back into an update and can clobber dataset properties. This
# primitive touches only the binding.
bq add-iam-policy-binding \
  --member="serviceAccount:${SA}" \
  --role="roles/bigquery.dataViewer" \
  "${PROJECT}:${DATASET}" >/dev/null

echo "IAM: jobUser + logWriter on project, dataViewer on ${DATASET} only"
echo

# ---------------------------------------------------------------------------
# 2. Deploy
# ---------------------------------------------------------------------------
#
# --no-allow-unauthenticated: unauthenticated requests get 403 at Google's
#   edge. Reach it with `gcloud run services proxy` (see README).
# --cpu-boost: extra CPU during startup, which is where the ~2.6s of Python
#   import and BigQuery client construction is spent.
# --service-account: workload identity. No key file exists anywhere.

gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --source . \
  --service-account "${SA}" \
  --no-allow-unauthenticated \
  --ingress all \
  --cpu-boost \
  --cpu "${CPU}" \
  --memory "${MEMORY}" \
  --min-instances "${MIN_INSTANCES}" \
  --max-instances "${MAX_INSTANCES}" \
  --port 8080 \
  --set-env-vars "GOODREADS_TRANSPORT=http,GOODREADS_TELEMETRY=1,GOODREADS_TELEMETRY_SINK=stdout,GOODREADS_MAX_BYTES_BILLED=${MAX_BYTES_BILLED},GOODREADS_BQ_PROJECT=${PROJECT},GOODREADS_BQ_DATASET=${DATASET},GOODREADS_BQ_LOCATION=US"

# Only this principal may invoke it.
gcloud run services add-iam-policy-binding "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --member="${INVOKER}" \
  --role="roles/run.invoker" >/dev/null

echo
echo "deployed. the endpoint is NOT publicly reachable; connect with:"
echo "  gcloud run services proxy ${SERVICE} --project ${PROJECT} --region ${REGION} --port 8080"
echo "  claude mcp add --transport http goodreads-remote http://127.0.0.1:8080/mcp"
