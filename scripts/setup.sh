#!/usr/bin/env bash
# One-time Google Cloud setup for the Hardware Replacement agent. Safe to rerun.
#
#   cp .env.example .env    # fill in GOOGLE_CLOUD_PROJECT and SN_INSTANCE_URL
#   ./scripts/setup.sh
#
# Enables the APIs, creates the photo bucket and the agent's service account,
# and creates the Agent Runtime instance for conversations and memory (writing
# AGENT_ENGINE_ID into .env). Then deploy with ./scripts/deploy.sh.
# Needs: gcloud (signed in as a project Owner or equivalent) and uv.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "Create .env first: cp .env.example .env (then fill it in)"; exit 1; }
set -a; source .env; set +a
: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT in .env}"
PROJECT="$GOOGLE_CLOUD_PROJECT"
REGION="${REGION:-us-central1}"
BUCKET="${ARTIFACT_BUCKET:-${PROJECT}-hardware-ticket-photos}"
SA="hardware-agent@${PROJECT}.iam.gserviceaccount.com"

echo "== APIs (a first run takes a minute or two)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com storage.googleapis.com secretmanager.googleapis.com \
  discoveryengine.googleapis.com --project "$PROJECT"

echo "== photo bucket gs://$BUCKET"
gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://$BUCKET" --project "$PROJECT" --location "$REGION" \
       --uniform-bucket-level-access --public-access-prevention

echo "== service account $SA"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create hardware-agent --project "$PROJECT" --display-name "Hardware replacement agent"
for role in roles/aiplatform.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" \
    --condition None --quiet >/dev/null
done
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$SA" \
  --role roles/storage.objectUser --quiet >/dev/null

echo "== Agent Runtime instance (conversations and memory)"
if [[ -n "${AGENT_ENGINE_ID:-}" ]]; then
  echo "AGENT_ENGINE_ID=$AGENT_ENGINE_ID already set in .env; skipping"
else
  OUT="$(uv run python scripts/create_state_engine.py)"
  echo "$OUT"
  ID="$(echo "$OUT" | sed -n 's/^AGENT_ENGINE_ID=//p')"
  if grep -q '^AGENT_ENGINE_ID=' .env; then
    sed -i.bak "s/^AGENT_ENGINE_ID=.*/AGENT_ENGINE_ID=$ID/" .env && rm -f .env.bak
  else
    echo "AGENT_ENGINE_ID=$ID" >> .env
  fi
  echo "Wrote AGENT_ENGINE_ID=$ID to .env"
fi

echo
echo "Done. Next: check config/organization.yaml (uv run python -m app.profile), then ./scripts/deploy.sh"
