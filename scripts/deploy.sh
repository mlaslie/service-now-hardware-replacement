#!/usr/bin/env bash
# Deploys the agent to Cloud Run and lets Gemini Enterprise call it.
#
#   ./scripts/deploy.sh
#
# Reads settings from .env (copy .env.example). Idempotent: rerun after code or
# config changes. Creates the photo bucket and the runtime service account if
# missing. The agent itself holds no ServiceNow credentials: users sign in
# through the agent's Gemini Enterprise authorization.
# Prerequisites and one-time setup: docs/INSTALL.md (scripts/setup.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then set -a; source .env; set +a; fi

: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT in .env (see .env.example)}"
: "${SN_INSTANCE_URL:?Set SN_INSTANCE_URL in .env, e.g. https://acme.service-now.com}"
: "${AGENT_ENGINE_ID:?Set AGENT_ENGINE_ID in .env (scripts/setup.sh creates the instance and prints it)}"
PROJECT="$GOOGLE_CLOUD_PROJECT"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE_NAME:-hardware-replacement-agent}"
MODEL="${MODEL:-gemini-3.8-flash}"
MODEL_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
BUCKET="${ARTIFACT_BUCKET:-${PROJECT}-hardware-ticket-photos}"
ENGINE_LOCATION="${AGENT_ENGINE_LOCATION:-$REGION}"
SA_NAME="hardware-agent"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "== checking the organization profile"
if command -v uv >/dev/null; then uv run python -m app.profile >/dev/null || { uv run python -m app.profile; exit 1; }; fi

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
# Predictable URL, so the agent card is right on the first deploy.
SERVICE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

echo "== infrastructure"
gcloud storage buckets describe "gs://$BUCKET" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access \
       --public-access-prevention --project="$PROJECT"

echo "== runtime service account"
gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --display-name="Hardware replacement agent" --project="$PROJECT"
for role in roles/aiplatform.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$role" \
    --condition=None --quiet >/dev/null
done
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" \
  --role=roles/storage.objectUser --quiet >/dev/null

echo "== deploy"
# concurrency 4: each request may buffer a ~25MB photo body.
gcloud run deploy "$SERVICE" --source . --project="$PROJECT" --region="$REGION" \
  --service-account="$SA" --no-allow-unauthenticated --ingress=all \
  --memory=1Gi --cpu=1 --concurrency=4 --timeout=300 --min-instances=1 \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=${PROJECT}|GOOGLE_CLOUD_LOCATION=${MODEL_LOCATION}|MODEL=${MODEL}|ARTIFACT_BUCKET=${BUCKET}|AGENT_ENGINE_ID=${AGENT_ENGINE_ID}|AGENT_ENGINE_LOCATION=${ENGINE_LOCATION}|REGION=${REGION}|SERVICE_URL=${SERVICE_URL}|SN_INSTANCE_URL=${SN_INSTANCE_URL}"

ACTUAL_URL="$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format='value(status.url)')"
echo "service url: $ACTUAL_URL"

echo "== let Gemini Enterprise invoke it"
# GE calls Cloud Run as the Discovery Engine service agent. Scoped to this service only.
gcloud run services add-iam-policy-binding "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com" \
  --role=roles/run.invoker --quiet >/dev/null

echo
echo "Agent card: ${SERVICE_URL}/.well-known/agent-card.json"
echo "Check it:   curl -s -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" ${SERVICE_URL}/.well-known/agent-card.json | jq .url"
