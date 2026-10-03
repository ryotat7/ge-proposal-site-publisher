#!/usr/bin/env python3
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Seeds synthetic RFP and past proposal reference documents into Cloud Storage and Vertex AI Search."""

from __future__ import annotations

import os
from google.cloud import storage

SYNTHETIC_DOCS: dict[str, str] = {
    "knowledge/rfp_acme_retail_omnichannel.md": """# Acme Retail Holdings - Next-Generation Omnichannel & AI Concierge RFP

## 1. Client Background
Acme Retail Holdings operates 140 physical stores and an e-commerce platform with 4.2M loyalty members.

## 2. Core Challenges
- Siloed POS and e-commerce customer data causing fragmented personalization.
- Low cross-channel conversion rate (only 11% of store buyers use the mobile app).
- High manual effort in weekly campaign segmentation and creative operations.

## 3. Target KPIs
- Repeat purchase conversion rate (CVR): +25% to +30% within 6 months.
- Omnichannel member LTV: +20% YoY.
- Campaign production and operations effort: -60% reduction via AI automation.
""",
    "knowledge/case_study_global_financial_cx.md": """# Case Study: Global Financial Corp - Enterprise Data Platform & AI Advisor

## 1. Solution Summary
Unified customer touchpoint logs in BigQuery and deployed a grounded ADK multi-agent concierge on Vertex AI Agent Runtime.

## 2. Architecture Highlights
- Layer 1 (Channels): Web Portal, Mobile App, Contact Center Desktop
- Layer 2 (AI Agent Runtime): Google ADK Concierge + Vertex AI Search Grounding
- Layer 3 (Unified Data): BigQuery Customer 360 + Real-time Feature Store
- Layer 4 (Governance): Cloud Armor, IAM Least Privilege, Private Cloud Storage

## 3. Measured Outcomes
- Digital inquiry self-resolution rate increased by +34%.
- Advisor preparation time reduced by 65%.
""",
    "knowledge/methodology_6_slide_proposal_standard.md": """# Standard 6-Slide Executive Proposal Methodology

All interactive HTML5 client proposals follow our 6-slide narrative structure:
1. Slide 01 (`hero-cover`): Client Name, Proposal Title, Subtitle, Presenter Organization
2. Slide 02 (`bento-executive-summary`): 3 Key Client Challenges + Core Value Proposition Banner
3. Slide 03 (`as-is-to-be-comparison`): 3 Solution Pillars with As-Is vs To-Be Transformation
4. Slide 04 (`architecture-flow`): 4-Layer Architecture & Data/AI Flow with SVG Connectors
5. Slide 05 (`roadmap-timeline`): 3-Phase Implementation Roadmap & Key Deliverables
6. Slide 06 (`roi-and-next-steps`): 3 Quantified KPI Cards + Immediate Next Action Items
""",
}


def seed_knowledge_files() -> None:
    project_id = os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        print("Skipping GCS seed: PROJECT_ID not set.")
        return
    bucket_name = os.environ.get("PROPOSAL_GCS_BUCKET", f"{project_id}-proposals")
    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)
    for blob_path, content in SYNTHETIC_DOCS.items():
        blob = bucket.blob(blob_path)
        blob.upload_from_string(content.encode("utf-8"), content_type="text/markdown; charset=utf-8")
        print(f"Uploaded gs://{bucket_name}/{blob_path}")


if __name__ == "__main__":
    seed_knowledge_files()
