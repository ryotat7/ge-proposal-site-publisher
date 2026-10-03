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


STRUCTURED_RECORDS: list[dict[str, str]] = [
    {
        "id": "case-retail-cdp-ai-001",
        "title": "Acme Retail Holdings: Next-Gen OMO AI Concierge & Unified CDP Proposal",
        "client_name": "Acme Retail Holdings",
        "industry": "Retail & E-Commerce",
        "summary": (
            "Unifies mobile app, e-commerce, and physical POS data into a real-time BigQuery CDP. "
            "Deploys a conversational Gemini 2.5 Flash agent on Vertex AI Agent Runtime for hyper-personalized "
            "styling recommendations and automated marketing campaign execution."
        ),
        "key_metrics": "Repeat purchase CVR +28%, Omnichannel member LTV +22%, Campaign production effort -65%",
        "recommended_architecture": (
            "Layer 1: Omnichannel Touchpoints (Mobile App / LINE / Web) -> "
            "Layer 2: Auth & Delivery Gateway (Cloud Run / Firebase Hosting) -> "
            "Layer 3: AI Agent Runtime (Vertex AI Agent Runtime / Gemini Enterprise / Vertex AI Search) -> "
            "Layer 4: Unified Data Platform (BigQuery CDP / Private Cloud Storage / Firestore)"
        ),
    },
    {
        "id": "case-fintech-advisor-002",
        "title": "Global Financial Corp: Wealth Management AI Concierge & Knowledge Grounding",
        "client_name": "Global Financial Corp",
        "industry": "Financial Services",
        "summary": (
            "Integrates product prospectuses, market research, and CRM history via Vertex AI Search "
            "to power an interactive client proposal concierge with strict IAM and audit logging."
        ),
        "key_metrics": "Digital inquiry self-resolution +34%, Proposal preparation time -65%, Advisor NPS +19pt",
        "recommended_architecture": (
            "Layer 1: Advisor & Client Portal -> "
            "Layer 2: Cloud Run Zero-Trust Auth Gateway -> "
            "Layer 3: ADK Concierge Agent + Vertex AI Search -> "
            "Layer 4: BigQuery Customer 360 + Firestore Audit Trail"
        ),
    },
]


def seed_knowledge_files() -> None:
    project_id = os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        print("Skipping seed: PROJECT_ID not set.")
        return
    bucket_name = os.environ.get("PROPOSAL_GCS_BUCKET", f"{project_id}-proposals")
    datastore_id = os.environ.get(
        "VERTEX_SEARCH_DATASTORE_ID", "proposal-knowledge-datastore"
    )
    location = os.environ.get("VERTEX_SEARCH_LOCATION", "global")

    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)
    for blob_path, content in SYNTHETIC_DOCS.items():
        blob = bucket.blob(blob_path)
        blob.upload_from_string(
            content.encode("utf-8"), content_type="text/markdown; charset=utf-8"
        )
        print(f"Uploaded gs://{bucket_name}/{blob_path}")

    try:
        from google.api_core import exceptions
        from google.api_core.client_options import ClientOptions
        from google.cloud import discoveryengine_v1 as discoveryengine
        from google.protobuf import struct_pb2

        api_endpoint = (
            f"{location}-discoveryengine.googleapis.com"
            if location != "global"
            else "discoveryengine.googleapis.com"
        )
        client_options = ClientOptions(
            api_endpoint=api_endpoint, quota_project_id=project_id
        )
        ds_client = discoveryengine.DataStoreServiceClient(
            client_options=client_options
        )
        collection_parent = f"projects/{project_id}/locations/{location}/collections/default_collection"
        datastore_name = f"{collection_parent}/dataStores/{datastore_id}"

        try:
            ds_client.get_data_store(name=datastore_name)
            print(f"Vertex AI Search DataStore already exists: {datastore_name}")
        except exceptions.NotFound:
            print(f"Creating Vertex AI Search DataStore: {datastore_name}...")
            ds = discoveryengine.DataStore(
                display_name="Proposal Knowledge DataStore",
                industry_vertical=discoveryengine.IndustryVertical.GENERIC,
                solution_types=[discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH],
                content_config=discoveryengine.DataStore.ContentConfig.NO_CONTENT,
            )
            op = ds_client.create_data_store(
                parent=collection_parent,
                data_store=ds,
                data_store_id=datastore_id,
            )
            op.result(timeout=180)
            print(f"Created DataStore: {datastore_name}")

        doc_client = discoveryengine.DocumentServiceClient(
            client_options=client_options
        )
        branch_parent = f"{datastore_name}/branches/default_branch"
        for record in STRUCTURED_RECORDS:
            doc_id = record["id"]
            struct_data = struct_pb2.Struct()
            struct_data.update(record)
            doc = discoveryengine.Document(
                id=doc_id,
                name=f"{branch_parent}/documents/{doc_id}",
                struct_data=struct_data,
            )
            try:
                doc_client.update_document(
                    document=doc,
                    allow_missing=True,
                )
                print(f"Seeded Vertex AI Search document: {doc_id}")
            except Exception as doc_exc:
                print(f"Warning: could not seed document {doc_id}: {doc_exc}")
    except Exception as exc:
        print(f"Warning: Vertex AI Search datastore seeding skipped: {exc}")


if __name__ == "__main__":
    seed_knowledge_files()
