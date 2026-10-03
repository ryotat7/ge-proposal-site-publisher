#!/usr/bin/env bash
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0
#
# End-to-end deployment script for the Interactive Proposal Site Publisher
# on Google Cloud (Cloud Storage, Firestore, Agent Search, Cloud Run service +
# Cloud Run job, Agent Runtime on Gemini Enterprise Agent Platform, and
# Gemini Enterprise).
#
# Phases can be skipped for faster iteration, e.g.:
#   SKIP_INFRA=1 SKIP_SEED=1 SKIP_GATEWAY=1 SKIP_JOB=1 ./infra/deploy.sh   # agent only

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Please set PROJECT_ID to your Google Cloud project ID}"
REGION="${REGION:-us-central1}"
PROPOSAL_GCS_BUCKET="${PROPOSAL_GCS_BUCKET:-${PROJECT_ID}-proposals}"
PROPOSAL_FIRESTORE_COLLECTION="${PROPOSAL_FIRESTORE_COLLECTION:-presentations}"
AGENT_SEARCH_DATASTORE_ID="${AGENT_SEARCH_DATASTORE_ID:-proposal-knowledge-datastore}"
GATEWAY_SERVICE_NAME="${GATEWAY_SERVICE_NAME:-proposal-hosting-gateway}"
GENERATION_JOB_ID="${GENERATION_JOB_ID:-proposal-deck-generator}"
PROPOSAL_BRAND_NAME="${PROPOSAL_BRAND_NAME:-Strategic AI Partners}"
PROPOSAL_BRAND_BADGE="${PROPOSAL_BRAND_BADGE:-SP}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.8-flash}"
MANAGED_AGENT_MODEL="${MANAGED_AGENT_MODEL:-antigravity-preview-05-2026}"
MANAGED_AGENT_DEADLINE_SECONDS="${MANAGED_AGENT_DEADLINE_SECONDS:-600}"

SKIP_INFRA="${SKIP_INFRA:-0}"
SKIP_SEED="${SKIP_SEED:-0}"
SKIP_GATEWAY="${SKIP_GATEWAY:-0}"
SKIP_JOB="${SKIP_JOB:-0}"
SKIP_AGENT="${SKIP_AGENT:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
RE_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
GENERATION_JOB_NAME="projects/${PROJECT_ID}/locations/${REGION}/jobs/${GENERATION_JOB_ID}"

if [[ "${SKIP_INFRA}" != "1" ]]; then
  echo "==> [1/7] Enabling required Google Cloud APIs in ${PROJECT_ID}..."
  gcloud services enable \
    aiplatform.googleapis.com \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    firestore.googleapis.com \
    storage.googleapis.com \
    discoveryengine.googleapis.com \
    secretmanager.googleapis.com \
    --project="${PROJECT_ID}"

  echo "==> [2/7] Provisioning private Cloud Storage bucket (gs://${PROPOSAL_GCS_BUCKET}) and IAM..."
  if ! gcloud storage buckets describe "gs://${PROPOSAL_GCS_BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${PROPOSAL_GCS_BUCKET}" \
      --project="${PROJECT_ID}" \
      --location="${REGION}" \
      --uniform-bucket-level-access \
      --public-access-prevention
  else
    gcloud storage buckets update "gs://${PROPOSAL_GCS_BUCKET}" \
      --project="${PROJECT_ID}" \
      --uniform-bucket-level-access \
      --public-access-prevention
  fi

  # Agent Runtime SA + Cloud Run default SA need: private bucket, Firestore, Agent Search,
  # Gemini / Managed Agents API, and (Agent Runtime -> Cloud Run job trigger) run.developer
  # (run.jobs.runWithOverrides) plus actAs on the job's service account.
  for sa in "${COMPUTE_SA}" "${RE_SA}"; do
    gcloud storage buckets add-iam-policy-binding "gs://${PROPOSAL_GCS_BUCKET}" \
      --member="serviceAccount:${sa}" \
      --role="roles/storage.objectAdmin" \
      --project="${PROJECT_ID}" >/dev/null 2>&1 || true

    for role in roles/datastore.user roles/discoveryengine.viewer roles/aiplatform.user roles/serviceusage.serviceUsageConsumer roles/run.developer; do
      gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${sa}" \
        --role="${role}" \
        --condition=None \
        --quiet >/dev/null 2>&1 || true
    done
  done
  gcloud iam service-accounts add-iam-policy-binding "${COMPUTE_SA}" \
    --member="serviceAccount:${RE_SA}" \
    --role="roles/iam.serviceAccountUser" \
    --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true

  echo "==> [3/7] Ensuring Firestore Native database exists..."
  if ! gcloud firestore databases describe --database="(default)" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud firestore databases create \
      --database="(default)" \
      --location="${REGION}" \
      --type=firestore-native \
      --project="${PROJECT_ID}"
  fi
fi

if [[ "${SKIP_SEED}" != "1" ]]; then
  echo "==> [4/7] Seeding sample knowledge documents into Agent Search datastore (${AGENT_SEARCH_DATASTORE_ID})..."
  PROJECT_ID="${PROJECT_ID}" \
  PROPOSAL_GCS_BUCKET="${PROPOSAL_GCS_BUCKET}" \
  AGENT_SEARCH_DATASTORE_ID="${AGENT_SEARCH_DATASTORE_ID}" \
  python3 "${SCRIPT_DIR}/seed_datastore.py"
fi

if [[ "${SKIP_GATEWAY}" != "1" ]]; then
  echo "==> [5/7] Deploying Cloud Run Hosting Gateway (${GATEWAY_SERVICE_NAME})..."
  if [[ -z "${GATEWAY_SESSION_SECRET:-}" ]]; then
    GATEWAY_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  fi

  gcloud run deploy "${GATEWAY_SERVICE_NAME}" \
    --source="${REPO_ROOT}/hosting_gateway" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --allow-unauthenticated \
    --set-env-vars="PROJECT_ID=${PROJECT_ID},PROPOSAL_GCS_BUCKET=${PROPOSAL_GCS_BUCKET},PROPOSAL_FIRESTORE_COLLECTION=${PROPOSAL_FIRESTORE_COLLECTION},PROPOSAL_BRAND_NAME=${PROPOSAL_BRAND_NAME},PROPOSAL_BRAND_BADGE=${PROPOSAL_BRAND_BADGE},GATEWAY_SESSION_SECRET=${GATEWAY_SESSION_SECRET}" \
    --quiet
fi

# Public base URL embedded into every share_url. Override with HOSTING_BASE_URL (e.g. a custom domain).
HOSTING_BASE_URL="${HOSTING_BASE_URL:-$(gcloud run services describe "${GATEWAY_SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.url)')}"
echo "    Hosting Gateway live at: ${HOSTING_BASE_URL}"

if [[ "${SKIP_JOB}" != "1" ]]; then
  echo "==> [6/7] Deploying background deck-generation Cloud Run job (${GENERATION_JOB_ID})..."
  # Same source/container as the agent; the entrypoint is overridden to the worker module.
  gcloud run jobs deploy "${GENERATION_JOB_ID}" \
    --source="${REPO_ROOT}/proposal_agent" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --command="uv" \
    --args="run,--no-sync,python,-m,app.generation_worker" \
    --tasks=1 \
    --max-retries=1 \
    --task-timeout=1500s \
    --cpu=1 \
    --memory=1Gi \
    --service-account="${COMPUTE_SA}" \
    --set-env-vars="PROJECT_ID=${PROJECT_ID},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GENAI_LOCATION=global,GEMINI_MODEL=${GEMINI_MODEL},MANAGED_AGENT_MODEL=${MANAGED_AGENT_MODEL},MANAGED_AGENT_DEADLINE_SECONDS=${MANAGED_AGENT_DEADLINE_SECONDS},PROPOSAL_GCS_BUCKET=${PROPOSAL_GCS_BUCKET},PROPOSAL_FIRESTORE_COLLECTION=${PROPOSAL_FIRESTORE_COLLECTION},AGENT_SEARCH_DATASTORE_ID=${AGENT_SEARCH_DATASTORE_ID},AGENT_SEARCH_LOCATION=global,HOSTING_BASE_URL=${HOSTING_BASE_URL},PROPOSAL_BRAND_NAME=${PROPOSAL_BRAND_NAME},PROPOSAL_BRAND_BADGE=${PROPOSAL_BRAND_BADGE}" \
    --quiet
  echo "    Generation job: ${GENERATION_JOB_NAME}"
fi

if [[ "${SKIP_AGENT}" != "1" ]]; then
  echo "==> [7/7] Deploying ADK Interactive Proposal Concierge Agent to Agent Runtime..."
  cd "${REPO_ROOT}/proposal_agent"
  agents-cli deploy \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --no-confirm-project \
    --update-env-vars="PROJECT_ID=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GENAI_LOCATION=global,GEMINI_MODEL=${GEMINI_MODEL},MANAGED_AGENT_MODEL=${MANAGED_AGENT_MODEL},MANAGED_AGENT_DEADLINE_SECONDS=${MANAGED_AGENT_DEADLINE_SECONDS},PROPOSAL_GCS_BUCKET=${PROPOSAL_GCS_BUCKET},PROPOSAL_FIRESTORE_COLLECTION=${PROPOSAL_FIRESTORE_COLLECTION},HOSTING_BASE_URL=${HOSTING_BASE_URL},AGENT_SEARCH_DATASTORE_ID=${AGENT_SEARCH_DATASTORE_ID},AGENT_SEARCH_LOCATION=global,PROPOSAL_BRAND_NAME=${PROPOSAL_BRAND_NAME},PROPOSAL_BRAND_BADGE=${PROPOSAL_BRAND_BADGE},GENERATION_JOB_NAME=${GENERATION_JOB_NAME},GENERATION_TRIGGER_MODE=auto"

  if [[ -n "${GE_APP_ID:-}" ]]; then
    echo "==> Publishing agent to Gemini Enterprise (App ID: ${GE_APP_ID})..."
    agents-cli publish gemini-enterprise \
      --project-id="${PROJECT_ID}" \
      --gemini-enterprise-app-id="${GE_APP_ID}" \
      --display-name="${GE_DISPLAY_NAME:-Interactive Proposal Site Concierge}" \
      --description="Interactive consultation for client proposals: issues a private proposal-site URL with viewer credentials immediately, generates the bespoke 6-slide HTML5 deck in the background (Managed Agents API with automatic Gemini fallback), and manages the published site afterwards." \
      --tool-description="Use this agent to consult on client proposals, search internal knowledge, issue a proposal website URL with viewer credentials immediately while the 6-slide HTML5 deck is generated in the background, check generation status, edit published decks, inspect viewer access logs, rotate credentials, or revoke published proposal sites."
  fi
fi

echo "==> Deployment complete!"
echo "    Hosting Gateway URL: ${HOSTING_BASE_URL}"
echo "    Generation job:      ${GENERATION_JOB_NAME}"
