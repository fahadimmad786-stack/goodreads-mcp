#!/usr/bin/env bash
#
# Deploy goodreads-chat to Cloud Run: the public web console in front of the
# private MCP server.
#
# Two services, deliberately. This one must be reachable by a browser, which
# has no Google identity; goodreads-mcp must stay --no-allow-unauthenticated.
# One Cloud Run service has one IAM policy, so they cannot be the same service.
#
# The IAM delta this script adds is exactly three bindings:
#
#   * this service's SA gets roles/run.invoker on goodreads-mcp -- the only new
#     access to the private service;
#   * this service's SA gets roles/secretmanager.secretAccessor on one secret;
#   * a SEPARATE build SA gets roles/cloudbuild.builds.builder on the project.
#     That is the only project-level binding, it belongs to an identity that
#     never runs the service, and it exists because the alternative is the
#     default compute service account -- which would put the same permissions
#     on an identity shared by everything else in the project.
#
# The runtime SA gets NO BigQuery role. It reaches BigQuery only as a consequence of an MCP
# tool call, executed under the MCP service's own identity, through the guarded
# tool surface. It also gets no iam.serviceAccountTokenCreator: minting an ID
# token for its OWN identity from the metadata server needs no role, and that
# is the usual place this gets over-granted.
#
# Idempotent: re-running builds a new image, deploys a new revision and
# re-asserts the bindings.

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
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-goodreads-chat}"
MCP_SERVICE="${MCP_SERVICE:-goodreads-mcp}"

SA_NAME="${SA_NAME:-goodreads-chat-run}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# The build runs as its own identity, as deploy.sh's does. Left unset,
# `gcloud builds submit` falls back to the default compute service account,
# which has no roles in a project set up the way this one is -- the build then
# fails reading its own uploaded source tarball, which reads like a bucket
# problem and is an identity one.
BUILD_SA_NAME="${BUILD_SA_NAME:-goodreads-chat-build}"
BUILD_SA="${BUILD_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

REPO="${REPO:-goodreads}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:latest}"

# Secret Manager secret holding the Anthropic API key. Created by this script
# from $ANTHROPIC_API_KEY on first run if it does not exist -- and skipped
# entirely if neither exists, which deploys the console in tool mode only: the
# forms, the cards and the guard probe, with no model and no Anthropic account.
KEY_SECRET="${KEY_SECRET:-anthropic-api-key}"
# Secret Manager secret holding the console's shared access token. Generated on
# first run if absent -- the console refuses to start without one.
ACCESS_SECRET="${ACCESS_SECRET:-chat-access-token}"

# Spend ceilings. max-instances is the blunt one: the console is public, and
# every turn costs Anthropic tokens and BigQuery bytes.
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-3}"
CPU="${CPU:-1}"
MEMORY="${MEMORY:-512Mi}"
CHAT_MAX_TOOL_CALLS="${CHAT_MAX_TOOL_CALLS:-6}"
CHAT_MAX_TURNS="${CHAT_MAX_TURNS:-25}"
CHAT_RATE_LIMIT_TURNS="${CHAT_RATE_LIMIT_TURNS:-10}"
CHAT_RATE_LIMIT_WINDOW="${CHAT_RATE_LIMIT_WINDOW:-300}"
CHAT_TOOL_RATE_LIMIT_CALLS="${CHAT_TOOL_RATE_LIMIT_CALLS:-40}"

echo "project=${PROJECT} region=${REGION} service=${SERVICE}"
echo "mcp service=${MCP_SERVICE}  max-instances=${MAX_INSTANCES}"
echo

# ---------------------------------------------------------------------------
# The MCP service must already exist: its URL is this service's audience.
# ---------------------------------------------------------------------------

MCP_BASE_URL="$(gcloud run services describe "${MCP_SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"

if [ -z "${MCP_BASE_URL}" ]; then
  echo "cannot find Cloud Run service ${MCP_SERVICE} in ${REGION}." >&2
  echo "run ./deploy.sh first -- this console is a front end for it." >&2
  exit 1
fi

echo "mcp base url: ${MCP_BASE_URL}"

# The audience is the service's BASE url, not the /mcp path: Cloud Run
# validates the token's `aud` against the service root.
MCP_AUDIENCE="${MCP_BASE_URL}"
MCP_URL="${MCP_BASE_URL}/mcp"

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project "${PROJECT}" >/dev/null

# ---------------------------------------------------------------------------
# Runtime identity
# ---------------------------------------------------------------------------

if gcloud iam service-accounts describe "${SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "service account ${SA} already exists"
else
  gcloud iam service-accounts create "${SA_NAME}" \
    --project "${PROJECT}" \
    --display-name "goodreads-chat Cloud Run runtime"
fi

# Build-time identity, separate from the runtime one above: it needs to read
# the source bucket and write to Artifact Registry, and the runtime service
# account must never have either. roles/cloudbuild.builds.builder is the role
# Google maintains for exactly this job.
if gcloud iam service-accounts describe "${BUILD_SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "build service account ${BUILD_SA} already exists"
else
  gcloud iam service-accounts create "${BUILD_SA_NAME}" \
    --project "${PROJECT}" \
    --display-name "goodreads-chat Cloud Build"
fi

# A service account is not immediately visible to the IAM policy API after it
# is created -- the binding fails with "does not exist" for a few seconds. That
# is a race, not a real error, so it is waited out rather than left to luck on
# a fresh project.
for attempt in $(seq 1 12); do
  if gcloud projects add-iam-policy-binding "${PROJECT}" \
       --member="serviceAccount:${BUILD_SA}" \
       --role="roles/cloudbuild.builds.builder" \
       --condition=None >/dev/null 2>&1; then
    break
  fi
  if [ "${attempt}" -eq 12 ]; then
    echo "could not bind roles/cloudbuild.builds.builder to ${BUILD_SA}" >&2
    gcloud projects add-iam-policy-binding "${PROJECT}" \
      --member="serviceAccount:${BUILD_SA}" \
      --role="roles/cloudbuild.builds.builder" \
      --condition=None >/dev/null    # once more, unsuppressed, to fail loudly
  fi
  sleep 5
done

echo "IAM: cloudbuild.builds.builder on the build SA (build-time only)"

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

ensure_secret() {   # name, value
  local name="$1" value="$2"
  if gcloud secrets describe "${name}" --project "${PROJECT}" >/dev/null 2>&1; then
    echo "secret ${name} already exists (leaving its value alone)"
  else
    if [ -z "${value}" ]; then
      echo "secret ${name} does not exist and no value was supplied." >&2
      return 1
    fi
    printf '%s' "${value}" | gcloud secrets create "${name}" \
      --project "${PROJECT}" --replication-policy=automatic --data-file=- >/dev/null
    echo "created secret ${name}"
  fi
  gcloud secrets add-iam-policy-binding "${name}" \
    --project "${PROJECT}" \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --condition=None >/dev/null
}

# The key is optional, so a missing one is a mode choice rather than an error.
# It is only mounted if the secret actually exists: --set-secrets naming an
# absent secret fails the deploy, and mounting an empty one would leave the
# console claiming a chat mode that 500s on every turn.
CHAT_MODE="chat and tool modes"
KEY_MOUNT="ANTHROPIC_API_KEY=${KEY_SECRET}:latest,"
if ! ensure_secret "${KEY_SECRET}" "${ANTHROPIC_API_KEY:-}"; then
  CHAT_MODE="tool mode only"
  KEY_MOUNT=""
  echo "no ${KEY_SECRET} secret and no ANTHROPIC_API_KEY: deploying without the chat mode."
  echo "the console still serves every tool; add the key later to turn chat on:"
  echo "  ANTHROPIC_API_KEY=sk-ant-... ./deploy-chat.sh"
fi

# Generated rather than prompted: a console with no access token would be an
# open endpoint billing the Anthropic account, so there is no path that skips it.
GENERATED_ACCESS=""
if ! gcloud secrets describe "${ACCESS_SECRET}" --project "${PROJECT}" >/dev/null 2>&1; then
  GENERATED_ACCESS="${CHAT_ACCESS_TOKEN:-$(openssl rand -hex 24)}"
fi
ensure_secret "${ACCESS_SECRET}" "${GENERATED_ACCESS}"

# ---------------------------------------------------------------------------
# Build. The root Dockerfile builds the MCP server; this image has its own.
# ---------------------------------------------------------------------------

if ! gcloud artifacts repositories describe "${REPO}" \
     --project "${PROJECT}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPO}" \
    --project "${PROJECT}" --location "${REGION}" --repository-format=docker \
    --description "goodreads service images"
fi

# A user-specified build service account requires the build to declare where
# its logs go, which cloudbuild-chat.yaml does with logging: CLOUD_LOGGING_ONLY.
gcloud builds submit \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --config cloudbuild-chat.yaml \
  --service-account "projects/${PROJECT}/serviceAccounts/${BUILD_SA}" \
  --substitutions "_IMAGE=${IMAGE}" \
  .

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
#
# --allow-unauthenticated is load-bearing and is the reason this is a separate
#   service: browsers carry no Google identity. Access control is the shared
#   token in CHAT_ACCESS_TOKEN, checked in app.py.
# --set-secrets, never --set-env-vars, for both secrets: the values never
#   appear in the revision's env, in `gcloud run services describe`, or in the
#   image.

gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${SA}" \
  --allow-unauthenticated \
  --ingress all \
  --cpu-boost \
  --cpu "${CPU}" \
  --memory "${MEMORY}" \
  --min-instances "${MIN_INSTANCES}" \
  --max-instances "${MAX_INSTANCES}" \
  --session-affinity \
  --timeout 600 \
  --port 8080 \
  --set-env-vars "GOODREADS_MCP_URL=${MCP_URL},GOODREADS_MCP_AUDIENCE=${MCP_AUDIENCE},CHAT_MAX_TOOL_CALLS=${CHAT_MAX_TOOL_CALLS},CHAT_MAX_TURNS=${CHAT_MAX_TURNS},CHAT_RATE_LIMIT_TURNS=${CHAT_RATE_LIMIT_TURNS},CHAT_RATE_LIMIT_WINDOW=${CHAT_RATE_LIMIT_WINDOW},CHAT_TOOL_RATE_LIMIT_CALLS=${CHAT_TOOL_RATE_LIMIT_CALLS}" \
  --set-secrets "${KEY_MOUNT}CHAT_ACCESS_TOKEN=${ACCESS_SECRET}:latest"

# ---------------------------------------------------------------------------
# The one new binding on the private service.
# ---------------------------------------------------------------------------

gcloud run services add-iam-policy-binding "${MCP_SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --member="serviceAccount:${SA}" \
  --role="roles/run.invoker" >/dev/null

echo
echo "granted roles/run.invoker on ${MCP_SERVICE} to ${SA}"
echo "  (no BigQuery role, no token-creator role, no project-level binding)"
echo "  the build SA's cloudbuild.builds.builder is a separate identity, build-time only"

CHAT_URL="$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" --format='value(status.url)')"

echo
echo "deployed: ${CHAT_URL}"
echo "  ${CHAT_MODE}"
if [ -n "${GENERATED_ACCESS}" ]; then
  echo
  echo "the console needs its access key on first visit:"
  echo "  ${CHAT_URL}/?k=${GENERATED_ACCESS}"
  echo
  echo "that key is now in Secret Manager as ${ACCESS_SECRET}. Read it again with:"
  echo "  gcloud secrets versions access latest --secret=${ACCESS_SECRET} --project ${PROJECT}"
else
  echo
  echo "open it with ?k=<key>, where the key is:"
  echo "  gcloud secrets versions access latest --secret=${ACCESS_SECRET} --project ${PROJECT}"
fi
