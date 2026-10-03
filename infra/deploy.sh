#!/usr/bin/env bash
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0
#
# End-to-end deployment script for the Interactive Proposal Site Publisher
# on Google Cloud (Cloud Storage, Firestore, Agent Search, Cloud Run,
# Agent Runtime on Gemini Enterprise Agent Platform, and Gemini Enterprise).

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Please set PROJECT_ID to your Google Cloud project ID}"
REGION="${REGION:-us-central1}"
PROPOSAL_GCS_BUCKET="${PROPOSAL_GCS_BUCKET:-${PROJECT_ID}-proposals}"
PROPOSAL_FIRESTORE_COLLECTION="${PROPOSAL_FIRESTORE_COLLECTION:-presentations}"
VERTEX_SEARCH_DATASTORE_ID="${VERTEX_SEARCH_DATASTORE_ID:-proposal-knowledge-datastore}"
GATEWAY_SERVICE_NAME="${GATEWAY_SERVICE_NAME:-proposal-hosting-gateway}"
PROPOSAL_BRAND_NAME="${PROPOSAL_BRAND_NAME:-Strategic AI Partners}"
PROPOSAL_BRAND_BADGE="${PROPOSAL_BRAND_BADGE:-SP}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "==> [1/6] Enabling required Google Cloud APIs in ${PROJECT_ID}..."
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

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
RE_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"

echo "==> [2/6] Provisioning private Cloud Storage bucket (gs://${PROPOSAL_GCS_BUCKET})..."
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

for sa in "${COMPUTE_SA}" "${RE_SA}"; do
  gcloud storage buckets add-iam-policy-binding "gs://${PROPOSAL_GCS_BUCKET}" \
    --member="serviceAccount:${sa}" \
    --role="roles/storage.objectAdmin" \
    --project="${PROJECT_ID}" >/dev/null 2>&1 || true

  for role in roles/datastore.user roles/discoveryengine.viewer roles/aiplatform.user roles/serviceusage.serviceUsageConsumer; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${sa}" \
      --role="${role}" \
      --condition=None \
      --quiet >/dev/null 2>&1 || true
  done
done

echo "==> [3/6] Ensuring Firestore Native database exists..."
if ! gcloud firestore databases describe --database="(default)" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --database="(default)" \
    --location="${REGION}" \
    --type=firestore-native \
    --project="${PROJECT_ID}"
fi

echo "==> [4/6] Seeding sample knowledge documents into Agent Search datastore (${VERTEX_SEARCH_DATASTORE_ID})..."
PROJECT_ID="${PROJECT_ID}" \
PROPOSAL_GCS_BUCKET="${PROPOSAL_GCS_BUCKET}" \
VERTEX_SEARCH_DATASTORE_ID="${VERTEX_SEARCH_DATASTORE_ID}" \
python3 "${SCRIPT_DIR}/seed_datastore.py"

echo "==> [5/6] Deploying Cloud Run Hosting Gateway (${GATEWAY_SERVICE_NAME})..."
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

HOSTING_BASE_URL="$(gcloud run services describe "${GATEWAY_SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.url)')"
echo "    Hosting Gateway live at: ${HOSTING_BASE_URL}"

echo "==> [6/6] Deploying ADK Interactive Proposal Concierge Agent to Agent Runtime..."
cd "${REPO_ROOT}/proposal_agent"
agents-cli deploy \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --no-confirm-project \
  --update-env-vars="PROJECT_ID=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GEMINI_MODEL=gemini-3.8-flash,PROPOSAL_GCS_BUCKET=${PROPOSAL_GCS_BUCKET},PROPOSAL_FIRESTORE_COLLECTION=${PROPOSAL_FIRESTORE_COLLECTION},HOSTING_BASE_URL=${HOSTING_BASE_URL},VERTEX_SEARCH_DATASTORE_ID=${VERTEX_SEARCH_DATASTORE_ID},VERTEX_SEARCH_LOCATION=global,PROPOSAL_BRAND_NAME=${PROPOSAL_BRAND_NAME},PROPOSAL_BRAND_BADGE=${PROPOSAL_BRAND_BADGE}"

if [[ -n "${GE_APP_ID:-}" ]]; then
  echo "==> Publishing agent to Gemini Enterprise (App ID: ${GE_APP_ID})..."
  agents-cli publish gemini-enterprise \
    --project-id="${PROJECT_ID}" \
    --gemini-enterprise-app-id="${GE_APP_ID}" \
    --display-name="${GE_DISPLAY_NAME:-Interactive Proposal Site Concierge}" \
    --description="Interactive consultation, bespoke 6-slide HTML5 proposal website generation, and post-publication lifecycle management." \
    --tool-description="Use this agent to consult on client proposals, search internal knowledge, generate or edit 6-slide HTML5 proposal websites, inspect viewer access logs, rotate credentials, or revoke published proposal sites."
fi

echo "==> Deployment complete!"
echo "    Hosting Gateway URL: ${HOSTING_BASE_URL}"
