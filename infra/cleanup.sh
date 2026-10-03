#!/usr/bin/env bash
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0
#
# Teardown script for the Interactive Proposal Site Publisher resources.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Please set PROJECT_ID to your Google Cloud project ID}"
REGION="${REGION:-us-central1}"
GATEWAY_SERVICE_NAME="${GATEWAY_SERVICE_NAME:-proposal-hosting-gateway}"

echo "==> Deleting Cloud Run service ${GATEWAY_SERVICE_NAME} in ${PROJECT_ID} (${REGION})..."
gcloud run services delete "${GATEWAY_SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --quiet || true

echo "==> Cleanup complete. Note: Cloud Storage buckets and Firestore collections are preserved by default to prevent accidental data loss."
