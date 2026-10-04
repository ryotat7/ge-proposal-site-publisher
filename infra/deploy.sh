#!/usr/bin/env bash
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0
#
# End-to-end deployment script for the Interactive Proposal Site Publisher
# on Google Cloud (Cloud Storage, Firestore, Agent Search, Cloud Run hosting
# gateway + deck renderer services, Cloud Run generation job, Agent Runtime on
# Gemini Enterprise Agent Platform, and Gemini Enterprise).
#
# Phases can be skipped for faster iteration, e.g.:
#   SKIP_INFRA=1 SKIP_SEED=1 SKIP_GATEWAY=1 SKIP_RENDERER=1 SKIP_JOB=1 ./infra/deploy.sh   # agent only

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Please set PROJECT_ID to your Google Cloud project ID}"
REGION="${REGION:-us-central1}"
PROPOSAL_GCS_BUCKET="${PROPOSAL_GCS_BUCKET:-${PROJECT_ID}-proposals}"
PROPOSAL_FIRESTORE_COLLECTION="${PROPOSAL_FIRESTORE_COLLECTION:-presentations}"
AGENT_SEARCH_DATASTORE_ID="${AGENT_SEARCH_DATASTORE_ID:-proposal-knowledge-datastore}"
AGENT_SEARCH_LOCATION="${AGENT_SEARCH_LOCATION:-global}"
# Colon-joined form for gcloud --set-env-vars and agents-cli --update-env-vars (which split on commas);
# proposal_agent/app/agent.py and infra/seed_datastore.py accept both ',' and ':' as DataStore ID delimiters.
AGENT_SEARCH_DATASTORE_ENV="${AGENT_SEARCH_DATASTORE_ID//,/:}"
SEED_MODE="${SEED_MODE:-auto}"
KNOWLEDGE_GCS_URI="${KNOWLEDGE_GCS_URI:-}"
KNOWLEDGE_BQ_TABLE="${KNOWLEDGE_BQ_TABLE:-}"
GATEWAY_SERVICE_NAME="${GATEWAY_SERVICE_NAME:-proposal-hosting-gateway}"
RENDERER_SERVICE_NAME="${RENDERER_SERVICE_NAME:-proposal-deck-renderer}"
GENERATION_JOB_ID="${GENERATION_JOB_ID:-proposal-deck-generator}"
PROPOSAL_BRAND_NAME="${PROPOSAL_BRAND_NAME:-Strategic AI Partners}"
PROPOSAL_BRAND_BADGE="${PROPOSAL_BRAND_BADGE:-SP}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.8-flash}"
# Free-form design: an ADK designer agent (FREEFORM_ADK_MODEL) writes HTML/CSS/SVG/chart JSON through
# Cloud Storage-backed file tools; the job renders each build with the private deck renderer and shows the
# screenshots back to the same agent before publishing.
FREEFORM_DESIGN_ENABLED="${FREEFORM_DESIGN_ENABLED:-true}"
FREEFORM_ADK_MODEL="${FREEFORM_ADK_MODEL:-gemini-3.8-flash}"
IMAGE_MODEL="${IMAGE_MODEL:-gemini-3.1-flash-image}"
FREEFORM_TOTAL_BUDGET_SECONDS="${FREEFORM_TOTAL_BUDGET_SECONDS:-900}"
FREEFORM_REVIEW_ROUNDS="${FREEFORM_REVIEW_ROUNDS:-2}"

SKIP_INFRA="${SKIP_INFRA:-0}"
SKIP_SEED="${SKIP_SEED:-0}"
SKIP_GATEWAY="${SKIP_GATEWAY:-0}"
SKIP_RENDERER="${SKIP_RENDERER:-0}"
SKIP_JOB="${SKIP_JOB:-0}"
SKIP_AGENT="${SKIP_AGENT:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
RE_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
DISCOVERY_SA="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
GENERATION_JOB_NAME="projects/${PROJECT_ID}/locations/${REGION}/jobs/${GENERATION_JOB_ID}"

# The deck contract (sanitiser / validator / CSP) and the deck runtime live in proposal_agent/app (single
# source of truth). The gateway and the renderer receive build-time copies (git-ignored in their directories).
sync_deck_contract() {
  local target="$1"
  cp "${REPO_ROOT}/proposal_agent/app/deck_contract.py" "${target}/deck_contract.py"
  rm -rf "${target}/deck_runtime"
  cp -R "${REPO_ROOT}/proposal_agent/app/deck_runtime" "${target}/deck_runtime"
}

if [[ "${SKIP_INFRA}" != "1" ]]; then
  echo "==> [1/8] Enabling required Google Cloud APIs in ${PROJECT_ID}..."
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

  echo "==> [2/8] Provisioning private Cloud Storage bucket (gs://${PROPOSAL_GCS_BUCKET}) and IAM..."
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

  # Agent Runtime SA + Cloud Run default SA need: private bucket, Firestore, Agent Search, Gemini on
  # Gemini Enterprise Agent Platform, and (Agent Runtime -> Cloud Run job trigger) run.developer
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

  # Ensure Discovery Engine Service Agent has required permissions for 1st Party DataConnectors and imports.
  gcloud beta services identity create \
    --service=discoveryengine.googleapis.com \
    --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
  for role in roles/storage.objectViewer roles/bigquery.dataViewer roles/bigquery.jobUser roles/discoveryengine.viewer; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${DISCOVERY_SA}" \
      --role="${role}" \
      --condition=None \
      --quiet >/dev/null 2>&1 || true
  done

  echo "==> [3/8] Ensuring Firestore Native database exists..."
  if ! gcloud firestore databases describe --database="(default)" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud firestore databases create \
      --database="(default)" \
      --location="${REGION}" \
      --type=firestore-native \
      --project="${PROJECT_ID}"
  fi
fi

if [[ "${SKIP_SEED}" != "1" ]]; then
  echo "==> [4/8] Configuring Agent Search datastore(s) (${AGENT_SEARCH_DATASTORE_ID}, SEED_MODE=${SEED_MODE})..."
  PROJECT_ID="${PROJECT_ID}" \
  PROPOSAL_GCS_BUCKET="${PROPOSAL_GCS_BUCKET}" \
  AGENT_SEARCH_DATASTORE_ID="${AGENT_SEARCH_DATASTORE_ID}" \
  AGENT_SEARCH_LOCATION="${AGENT_SEARCH_LOCATION}" \
  SEED_MODE="${SEED_MODE}" \
  KNOWLEDGE_GCS_URI="${KNOWLEDGE_GCS_URI}" \
  KNOWLEDGE_BQ_TABLE="${KNOWLEDGE_BQ_TABLE}" \
  GE_APP_ID="${GE_APP_ID:-}" \
  python3 "${SCRIPT_DIR}/seed_datastore.py"
elif [[ -n "${GE_APP_ID:-}" ]]; then
  echo "==> [4/8] SKIP_SEED=1: Binding existing DataStore(s) (${AGENT_SEARCH_DATASTORE_ID}) to Gemini Enterprise Engine (${GE_APP_ID})..."
  PROJECT_ID="${PROJECT_ID}" \
  AGENT_SEARCH_DATASTORE_ID="${AGENT_SEARCH_DATASTORE_ID}" \
  AGENT_SEARCH_LOCATION="${AGENT_SEARCH_LOCATION}" \
  GE_APP_ID="${GE_APP_ID}" \
  python3 "${SCRIPT_DIR}/seed_datastore.py" --bind-only
fi

if [[ "${SKIP_GATEWAY}" != "1" ]]; then
  echo "==> [5/8] Deploying Cloud Run Hosting Gateway (${GATEWAY_SERVICE_NAME})..."
  if [[ -z "${GATEWAY_SESSION_SECRET:-}" ]]; then
    GATEWAY_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  fi
  sync_deck_contract "${REPO_ROOT}/hosting_gateway"

  gcloud run deploy "${GATEWAY_SERVICE_NAME}" \
    --source="${REPO_ROOT}/hosting_gateway" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --allow-unauthenticated \
    --set-env-vars="PROJECT_ID=${PROJECT_ID},PROPOSAL_GCS_BUCKET=${PROPOSAL_GCS_BUCKET},PROPOSAL_FIRESTORE_COLLECTION=${PROPOSAL_FIRESTORE_COLLECTION},PROPOSAL_BRAND_NAME=${PROPOSAL_BRAND_NAME},PROPOSAL_BRAND_BADGE=${PROPOSAL_BRAND_BADGE},GATEWAY_SESSION_SECRET=${GATEWAY_SESSION_SECRET}" \
    --quiet
fi

# Public base URL embedded into every share_url. Override with HOSTING_BASE_URL (e.g. a custom domain or the
# deterministic https://<service>-<project-number>.<region>.run.app form).
HOSTING_BASE_URL="${HOSTING_BASE_URL:-$(gcloud run services describe "${GATEWAY_SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.url)')}"
echo "    Hosting Gateway live at: ${HOSTING_BASE_URL}"

if [[ "${SKIP_RENDERER}" != "1" && "${FREEFORM_DESIGN_ENABLED}" == "true" ]]; then
  echo "==> [6/8] Deploying private Cloud Run Deck Renderer (${RENDERER_SERVICE_NAME})..."
  # Headless Chromium that renders a staging build from the bucket and returns screenshots + layout issues.
  # Private (IAM-authenticated); only the generation job's service account may invoke it.
  sync_deck_contract "${REPO_ROOT}/deck_renderer"
  gcloud run deploy "${RENDERER_SERVICE_NAME}" \
    --source="${REPO_ROOT}/deck_renderer" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --no-allow-unauthenticated \
    --service-account="${COMPUTE_SA}" \
    --concurrency=1 \
    --cpu=2 \
    --memory=2Gi \
    --timeout=300 \
    --set-env-vars="ALLOWED_BUCKETS=${PROPOSAL_GCS_BUCKET}" \
    --quiet
  gcloud run services add-iam-policy-binding "${RENDERER_SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --member="serviceAccount:${COMPUTE_SA}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null
fi
DECK_RENDERER_URL="${DECK_RENDERER_URL:-}"
if [[ -z "${DECK_RENDERER_URL}" && "${FREEFORM_DESIGN_ENABLED}" == "true" ]]; then
  DECK_RENDERER_URL="$(gcloud run services describe "${RENDERER_SERVICE_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format='value(status.url)' 2>/dev/null || true)"
fi
echo "    Deck Renderer: ${DECK_RENDERER_URL:-<none: screenshot review rounds are skipped>}"

if [[ "${SKIP_JOB}" != "1" ]]; then
  echo "==> [7/8] Deploying background deck-generation Cloud Run job (${GENERATION_JOB_ID})..."
  # Same source/container as the agent; the entrypoint is overridden to the worker module.
  # JOB_MODE (generate | freeform_edit) and PRESENTATION_ID are passed as per-execution overrides.
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
    --memory=2Gi \
    --service-account="${COMPUTE_SA}" \
    --set-env-vars="PROJECT_ID=${PROJECT_ID},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GENAI_LOCATION=global,GEMINI_MODEL=${GEMINI_MODEL},PROPOSAL_GCS_BUCKET=${PROPOSAL_GCS_BUCKET},PROPOSAL_FIRESTORE_COLLECTION=${PROPOSAL_FIRESTORE_COLLECTION},AGENT_SEARCH_DATASTORE_ID=${AGENT_SEARCH_DATASTORE_ENV},AGENT_SEARCH_LOCATION=${AGENT_SEARCH_LOCATION},HOSTING_BASE_URL=${HOSTING_BASE_URL},PROPOSAL_BRAND_NAME=${PROPOSAL_BRAND_NAME},PROPOSAL_BRAND_BADGE=${PROPOSAL_BRAND_BADGE},FREEFORM_DESIGN_ENABLED=${FREEFORM_DESIGN_ENABLED},FREEFORM_ADK_MODEL=${FREEFORM_ADK_MODEL},DECK_RENDERER_URL=${DECK_RENDERER_URL},IMAGE_MODEL=${IMAGE_MODEL},FREEFORM_TOTAL_BUDGET_SECONDS=${FREEFORM_TOTAL_BUDGET_SECONDS},FREEFORM_REVIEW_ROUNDS=${FREEFORM_REVIEW_ROUNDS}" \
    --quiet
  echo "    Generation job: ${GENERATION_JOB_NAME}"
fi

if [[ "${SKIP_AGENT}" != "1" ]]; then
  echo "==> [8/8] Deploying ADK Interactive Proposal Concierge Agent to Agent Runtime..."
  cd "${REPO_ROOT}/proposal_agent"
  agents-cli deploy \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --no-confirm-project \
    --update-env-vars="PROJECT_ID=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GENAI_LOCATION=global,GEMINI_MODEL=${GEMINI_MODEL},PROPOSAL_GCS_BUCKET=${PROPOSAL_GCS_BUCKET},PROPOSAL_FIRESTORE_COLLECTION=${PROPOSAL_FIRESTORE_COLLECTION},HOSTING_BASE_URL=${HOSTING_BASE_URL},AGENT_SEARCH_DATASTORE_ID=${AGENT_SEARCH_DATASTORE_ENV},AGENT_SEARCH_LOCATION=${AGENT_SEARCH_LOCATION},PROPOSAL_BRAND_NAME=${PROPOSAL_BRAND_NAME},PROPOSAL_BRAND_BADGE=${PROPOSAL_BRAND_BADGE},GENERATION_JOB_NAME=${GENERATION_JOB_NAME},GENERATION_TRIGGER_MODE=auto,FREEFORM_DESIGN_ENABLED=${FREEFORM_DESIGN_ENABLED}"

  # GE_APP_ID must be the FULL engine resource name (agents-cli >= 1.4.0):
  #   projects/<project-number>/locations/global/collections/default_collection/engines/<engine-id>
  # NOTE: `agents-cli publish` always creates a NEW agent registration. To change the description of an
  # existing registration in place, PATCH the Discovery Engine agent resource (see SKILL.md, gotcha 10).
  if [[ -n "${GE_APP_ID:-}" ]]; then
    if [[ "${GE_APP_ID}" != projects/* ]]; then
      GE_APP_ID="projects/${PROJECT_NUMBER}/locations/global/collections/default_collection/engines/${GE_APP_ID}"
    fi
    echo "==> Publishing agent to Gemini Enterprise (App: ${GE_APP_ID})..."
    agents-cli publish gemini-enterprise \
      --project-id="${PROJECT_ID}" \
      --gemini-enterprise-app-id="${GE_APP_ID}" \
      --display-name="${GE_DISPLAY_NAME:-Interactive Proposal Site Concierge}" \
      --description="クライアント提案用のインタラクティブ HTML プレゼンテーションを対話で企画し、限定公開 URL と閲覧用 ID・パスワードをその場で発行します。既定は ADK のデザイナーエージェント（gemini-3.8-flash）による自由デザインで、グラフ・図解・AI 生成イメージも使い、描画結果をエージェント自身が確認して直してから公開します。作成にかかる時間は通常 7〜11 分（最長約 20 分）です。高速モードは 6 枚構成のテンプレートです。完成すると同じ URL が提案ページに切り替わります。公開後の修正・取り消し・閲覧ログ確認・パスワード再発行・公開停止もチャットで完結し、自由デザイン版の修正は通常 5〜7 分（最長約 15 分）で反映されます。" \
      --tool-description="Use this agent to consult on client proposals, search internal knowledge, issue a proposal website URL with viewer credentials immediately while the deck is generated in the background (free-form design by an ADK designer agent that reviews its own rendered screenshots, or a fast 6-slide template), check generation status, edit published decks (free-form edits are queued and switch automatically), undo edits, inspect viewer access logs, rotate credentials, or revoke published proposal sites."
  fi
fi

echo "==> Deployment complete!"
echo "    Hosting Gateway URL: ${HOSTING_BASE_URL}"
echo "    Deck Renderer URL:   ${DECK_RENDERER_URL:-<none>}"
echo "    Generation job:      ${GENERATION_JOB_NAME}"
