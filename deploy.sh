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

# The project is not hard-coded. It comes from $PROJECT, or from the active
# gcloud configuration -- which is the account this would deploy under anyway,
# so on a configured machine it resolves with nothing set. Empty is fatal
# rather than silently deploying somewhere unintended.
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"

if [ -z "${PROJECT}" ]; then
  echo "no GCP project. Set PROJECT=<id>, or pick one with:" >&2
  echo "  gcloud config set project <id>" >&2
  exit 1
fi
REGION="${REGION:-us-central1}"        # matches the dataset's US location
SERVICE="${SERVICE:-goodreads-mcp}"
DATASET="${DATASET:-goodreads}"
SA_NAME="${SA_NAME:-goodreads-mcp-run}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# Separate identity for Cloud Build. The project's default compute service
# account has no roles, and `--source` builds need storage + Artifact Registry
# + logging access. Granting those to the shared default SA would widen an
# identity used by other resources, so the build gets its own -- and the
# runtime SA above stays read-only with no build permissions.
BUILD_SA_NAME="${BUILD_SA_NAME:-goodreads-mcp-build}"
BUILD_SA="${BUILD_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

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
# Done with the BigQuery client rather than bq, for two reasons:
#   * `bq add-iam-policy-binding` on a DATASET returns "This feature requires
#     allowlisting" -- it is available for tables and routines, not datasets.
#   * `bq show | edit | bq update --source` works but round-trips output-only
#     fields (etag, creationTime, selfLink) back into an update, which can
#     clobber dataset properties.
# update_dataset with an explicit field mask PATCHes access_entries alone, and
# the call is additive and idempotent.
PY_BIN="${PY_BIN:-$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
"${PY_BIN}" - "${PROJECT}" "${DATASET}" "${SA}" <<'PY'
import sys
from google.cloud import bigquery

project, dataset_id, sa = sys.argv[1], sys.argv[2], sys.argv[3]
client = bigquery.Client(project=project)
ds = client.get_dataset(f"{project}.{dataset_id}")

# READER on a dataset is the dataset-level equivalent of bigquery.dataViewer.
already = any(
    e.entity_type == "userByEmail" and e.entity_id == sa and e.role == "READER"
    for e in ds.access_entries
)
if already:
    print(f"dataset READER for {sa} already present")
else:
    ds.access_entries = list(ds.access_entries) + [
        bigquery.AccessEntry("READER", "userByEmail", sa)
    ]
    client.update_dataset(ds, ["access_entries"])   # field mask: nothing else
    print(f"granted dataset READER to {sa}")
PY

echo "IAM: jobUser + logWriter on project, dataViewer on ${DATASET} only"
echo

# ---------------------------------------------------------------------------
# 1b. Build service account
# ---------------------------------------------------------------------------
# roles/cloudbuild.builds.builder is the role Google maintains for exactly
# this job (source bucket read, Artifact Registry write, build logs). Scoped
# to a dedicated identity rather than the default compute SA.

if gcloud iam service-accounts describe "${BUILD_SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "build service account ${BUILD_SA} already exists"
else
  gcloud iam service-accounts create "${BUILD_SA_NAME}" \
    --project "${PROJECT}" \
    --display-name "goodreads-mcp Cloud Build"
fi

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/cloudbuild.builds.builder" \
  --condition=None >/dev/null

echo "IAM: cloudbuild.builds.builder on the build SA (build-time only)"
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
  --build-service-account "projects/${PROJECT}/serviceAccounts/${BUILD_SA}" \
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
