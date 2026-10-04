#!/usr/bin/env python3
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Configures Agent Search DataStores for real production data sources or synthetic sandbox seeding."""

from __future__ import annotations

import argparse
import os
import re
from typing import Any

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
Unified customer touchpoint logs in BigQuery and deployed a grounded ADK multi-agent concierge on Agent Runtime (Gemini Enterprise Agent Platform).

## 2. Architecture Highlights
- Layer 1 (Channels): Web Portal, Mobile App, Contact Center Desktop
- Layer 2 (AI Agent Runtime): Google ADK Concierge + Agent Search Grounding
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
        "source_system": "google_drive",
        "source_uri": "https://drive.google.com/drive/folders/sample-rfp-archive-retail",
        "title": "Acme Retail Holdings: Next-Gen OMO AI Concierge & Unified CDP Proposal",
        "client_name": "Acme Retail Holdings",
        "industry": "Retail & E-Commerce",
        "summary": (
            "Unifies mobile app, e-commerce, and physical POS data into a real-time BigQuery CDP. "
            "Deploys a conversational Gemini 3.8 Flash agent on Agent Runtime (Gemini Enterprise Agent Platform) for hyper-personalized "
            "styling recommendations and automated marketing campaign execution."
        ),
        "key_metrics": "Repeat purchase CVR +28%, Omnichannel member LTV +22%, Campaign production effort -65%",
        "recommended_architecture": (
            "Layer 1: Omnichannel Touchpoints (Mobile App / LINE / Web) -> "
            "Layer 2: Auth & Delivery Gateway (Cloud Run + Private Cloud Storage + Firestore) -> "
            "Layer 3: AI Agent Runtime (Agent Runtime / Gemini Enterprise / Agent Search) -> "
            "Layer 4: Unified Data Platform (BigQuery CDP / Private Cloud Storage / Firestore)"
        ),
        "deal_stage": "Closed Won (Reference Case Study)",
        "recent_activity": "Reference RFP response and delivery blueprint archived in Google Drive.",
    },
    {
        "id": "case-fintech-advisor-002",
        "source_system": "google_drive",
        "source_uri": "https://drive.google.com/drive/folders/sample-rfp-archive-fintech",
        "title": "Global Financial Corp: Wealth Management AI Concierge & Knowledge Grounding",
        "client_name": "Global Financial Corp",
        "industry": "Financial Services",
        "summary": (
            "Integrates product prospectuses, market research, and CRM history via Agent Search "
            "to power an interactive client proposal concierge with strict IAM and audit logging."
        ),
        "key_metrics": "Digital inquiry self-resolution +34%, Proposal preparation time -65%, Advisor NPS +19pt",
        "recommended_architecture": (
            "Layer 1: Advisor & Client Portal -> "
            "Layer 2: Cloud Run Zero-Trust Auth Gateway -> "
            "Layer 3: ADK Concierge Agent + Agent Search -> "
            "Layer 4: BigQuery Customer 360 + Firestore Audit Trail"
        ),
        "deal_stage": "Closed Won (Reference Case Study)",
        "recent_activity": "Architecture review deck and security whitepaper archived in Google Drive.",
    },
    {
        "id": "crm-opp-retail-2026-q4",
        "source_system": "salesforce",
        "source_uri": "https://example.my.salesforce.com/lightning/r/Opportunity/006000000000001AAA/view",
        "title": "Salesforce Opportunity: Acme Retail Holdings - FY2026 Omnichannel AI Expansion",
        "client_name": "Acme Retail Holdings",
        "industry": "Retail & E-Commerce",
        "summary": (
            "Latest CRM opportunity history and executive meeting notes from Salesforce: "
            "Client stakeholders agreed on a 6-month phased rollout starting with BigQuery Customer 360 "
            "and an ADK conversational concierge on Agent Runtime."
        ),
        "key_metrics": "Target ACV: $480,000 | Expected CVR Lift: +25% to +30% | Target Decision: Next Month",
        "recommended_architecture": (
            "Google Drive (Past RFPs) + Salesforce (Live CRM Context) -> Agent Search -> "
            "Agent Runtime (ADK Concierge) -> Cloud Run Auth Gateway + Private Cloud Storage + Firestore"
        ),
        "deal_stage": "Stage 03 - Technical Evaluation & Solution Proposal",
        "recent_activity": "CMO requested an interactive HTML5 proposal deck with a 3-phase rollout roadmap and per-client password protection.",
    },
    {
        "id": "crm-opp-fintech-2026-q4",
        "source_system": "salesforce",
        "source_uri": "https://example.my.salesforce.com/lightning/r/Opportunity/006000000000002AAA/view",
        "title": "Salesforce Opportunity: Global Financial Corp - Wealth Advisor Knowledge Portal",
        "client_name": "Global Financial Corp",
        "industry": "Financial Services",
        "summary": (
            "Latest CRM opportunity notes from Salesforce: Security team approved the zero-trust "
            "Cloud Run gateway pattern with private Cloud Storage and Firestore audit logging for external client sharing."
        ),
        "key_metrics": "Target ACV: $620,000 | Advisor Prep Time Reduction: -65% | Security Review: Passed",
        "recommended_architecture": (
            "Layer 1: Advisor & Client Portal -> "
            "Layer 2: Cloud Run Zero-Trust Auth Gateway -> "
            "Layer 3: ADK Concierge Agent + Agent Search -> "
            "Layer 4: BigQuery Customer 360 + Firestore Audit Trail"
        ),
        "deal_stage": "Stage 04 - Security & Architecture Review",
        "recent_activity": "CISO confirmed requirement for private Cloud Storage (publicAccessPrevention enforced) and PBKDF2 passcode authentication.",
    },
]


def parse_datastore_ids(raw: str | None = None) -> list[str]:
    """Splits comma/colon/semicolon-separated DataStore IDs while preserving order."""
    value = (
        raw
        if raw is not None
        else (
            os.environ.get("AGENT_SEARCH_DATASTORE_IDS")
            or os.environ.get("AGENT_SEARCH_DATASTORE_ID")
            or "proposal-knowledge-datastore"
        )
    )
    ids: list[str] = []
    for token in re.split(r"[,:;|\s]+", value):
        cleaned = token.strip()
        if cleaned and cleaned not in ids:
            ids.append(cleaned)
    return ids or ["proposal-knowledge-datastore"]


def resolve_engine_name(project_id: str, location: str, ge_app_id: str) -> str:
    """Expands a bare Gemini Enterprise engine ID into a full Discovery Engine resource name."""
    cleaned = ge_app_id.strip()
    if cleaned.startswith("projects/"):
        return cleaned
    return (
        f"projects/{project_id}/locations/{location}/collections/"
        f"default_collection/engines/{cleaned}"
    )


def bind_datastores_to_engine(
    project_id: str,
    location: str,
    datastore_ids: list[str],
    ge_app_id: str,
) -> bool:
    """Binds one or more DataStore IDs to a Gemini Enterprise (Discovery Engine) Engine via REST PATCH."""
    if not ge_app_id or not datastore_ids:
        return False
    engine_name = resolve_engine_name(project_id, location, ge_app_id)
    parts = engine_name.split("/")
    eng_loc = parts[3] if len(parts) >= 4 else location
    host = (
        f"https://{eng_loc}-discoveryengine.googleapis.com"
        if eng_loc != "global"
        else "https://discoveryengine.googleapis.com"
    )
    url = f"{host}/v1alpha/{engine_name}"
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        session = AuthorizedSession(credentials)
        headers = {"x-goog-user-project": project_id}
        get_resp = session.get(url, headers=headers, timeout=30)
        if get_resp.status_code >= 300:
            print(
                f"Warning: could not inspect Gemini Enterprise Engine {engine_name} "
                f"(HTTP {get_resp.status_code}): {get_resp.text[:240]}"
            )
            return False
        engine_data = get_resp.json() if get_resp.content else {}
        existing_ids = [
            str(x).strip()
            for x in (engine_data.get("dataStoreIds") or [])
            if str(x).strip()
        ]
        merged_ids = list(
            dict.fromkeys([*existing_ids, *[d.strip() for d in datastore_ids if d.strip()]])
        )
        if merged_ids == existing_ids:
            print(
                f"Gemini Enterprise Engine already bound to DataStore(s): {merged_ids}"
            )
            return True
        patch_resp = session.patch(
            f"{url}?updateMask=dataStoreIds",
            headers=headers,
            json={"name": engine_name, "dataStoreIds": merged_ids},
            timeout=30,
        )
        if patch_resp.status_code < 300:
            print(
                f"Bound DataStore(s) {merged_ids} to Gemini Enterprise Engine: {engine_name}"
            )
            return True
        print(
            f"Warning: could not auto-bind DataStore(s) {datastore_ids} to {engine_name} "
            f"(HTTP {patch_resp.status_code}): {patch_resp.text[:240]}"
        )
        return False
    except Exception as exc:
        print(f"Warning: Gemini Enterprise Engine DataStore binding skipped: {exc}")
        return False


def _records_for_datastore(ds_id: str) -> list[dict[str, str]]:
    """Selects synthetic records matching a DataStore's role (Google Drive RFPs vs Salesforce CRM vs combined)."""
    lower = ds_id.lower()
    has_drive = "drive" in lower or "rfp" in lower
    has_crm = "salesforce" in lower or "sfdc" in lower or "crm" in lower
    if has_drive and not has_crm:
        return [r for r in STRUCTURED_RECORDS if r.get("source_system") == "google_drive"]
    if has_crm and not has_drive:
        return [r for r in STRUCTURED_RECORDS if r.get("source_system") == "salesforce"]
    return list(STRUCTURED_RECORDS)


def _import_real_documents(
    doc_client: Any,
    branch_parent: str,
    project_id: str,
    gcs_uri: str,
    bq_table: str,
) -> bool:
    """Triggers a Discovery Engine document import from Cloud Storage or BigQuery."""
    from google.cloud import discoveryengine_v1 as discoveryengine

    imported = False
    if gcs_uri:
        uris = [u.strip() for u in gcs_uri.split(",") if u.strip()]
        schema = os.environ.get("KNOWLEDGE_GCS_DATA_SCHEMA", "content")
        req = discoveryengine.ImportDocumentsRequest(
            parent=branch_parent,
            gcs_source=discoveryengine.GcsSource(input_uris=uris, data_schema=schema),
            reconciliation_mode=discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL,
        )
        op = doc_client.import_documents(request=req)
        print(
            f"Triggered Cloud Storage import from {uris} into {branch_parent} "
            f"(operation: {getattr(op.operation, 'name', 'started')})"
        )
        imported = True

    if bq_table:
        parts = bq_table.strip().replace(":", ".").split(".")
        if len(parts) == 2:
            bq_proj, dataset_id, table_id = project_id, parts[0], parts[1]
        elif len(parts) == 3:
            bq_proj, dataset_id, table_id = parts[0], parts[1], parts[2]
        else:
            raise ValueError(
                f"KNOWLEDGE_BQ_TABLE must be 'dataset.table' or 'project.dataset.table', got: {bq_table}"
            )
        schema = os.environ.get("KNOWLEDGE_BQ_DATA_SCHEMA", "custom")
        req = discoveryengine.ImportDocumentsRequest(
            parent=branch_parent,
            bigquery_source=discoveryengine.BigQuerySource(
                project_id=bq_proj,
                dataset_id=dataset_id,
                table_id=table_id,
                data_schema=schema,
            ),
            reconciliation_mode=discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL,
        )
        op = doc_client.import_documents(request=req)
        print(
            f"Triggered BigQuery import from {bq_proj}.{dataset_id}.{table_id} into {branch_parent} "
            f"(operation: {getattr(op.operation, 'name', 'started')})"
        )
        imported = True

    return imported


def seed_knowledge_files(bind_only: bool = False) -> dict[str, Any]:
    """Configures Agent Search DataStores and optionally binds them to a Gemini Enterprise Engine."""
    project_id = os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        print("Skipping seed: PROJECT_ID not set.")
        return {"status": "skipped", "reason": "no_project_id"}

    bucket_name = os.environ.get("PROPOSAL_GCS_BUCKET", f"{project_id}-proposals")
    datastore_ids = parse_datastore_ids()
    location = os.environ.get("AGENT_SEARCH_LOCATION", "global")
    seed_mode = os.environ.get("SEED_MODE", "auto").strip().lower() or "auto"
    knowledge_gcs_uri = os.environ.get("KNOWLEDGE_GCS_URI", "").strip()
    knowledge_bq_table = os.environ.get("KNOWLEDGE_BQ_TABLE", "").strip()
    ge_app_id = os.environ.get("GE_APP_ID", "").strip()

    summary: dict[str, Any] = {
        "status": "ok",
        "seed_mode": "bind_only" if bind_only else seed_mode,
        "datastore_ids": datastore_ids,
        "existing_preserved": [],
        "synthetic_seeded": [],
        "real_imported": [],
        "engine_bound": False,
    }

    if bind_only or seed_mode == "none":
        if ge_app_id:
            summary["engine_bound"] = bind_datastores_to_engine(
                project_id, location, datastore_ids, ge_app_id
            )
        return summary

    synthetic_blobs_uploaded = False

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
        doc_client = discoveryengine.DocumentServiceClient(
            client_options=client_options
        )
        collection_parent = (
            f"projects/{project_id}/locations/{location}/collections/default_collection"
        )

        for ds_id in datastore_ids:
            datastore_name = f"{collection_parent}/dataStores/{ds_id}"
            branch_parent = f"{datastore_name}/branches/default_branch"
            exists = False
            try:
                ds_client.get_data_store(name=datastore_name)
                exists = True
                print(f"Agent Search DataStore already exists: {datastore_name}")
            except exceptions.NotFound:
                exists = False

            # 1. Existing DataStore in auto or real mode -> preserve real production connector data untouched
            if exists and seed_mode != "synthetic":
                summary["existing_preserved"].append(ds_id)
                if knowledge_gcs_uri or knowledge_bq_table:
                    if _import_real_documents(
                        doc_client,
                        branch_parent,
                        project_id,
                        knowledge_gcs_uri,
                        knowledge_bq_table,
                    ):
                        summary["real_imported"].append(ds_id)
                else:
                    print(
                        f"Preserving existing DataStore '{ds_id}' without synthetic seeding "
                        f"(SEED_MODE={seed_mode})."
                    )
                continue

            # 2. Missing DataStore when real data sources are requested (SEED_MODE=real or explicit GCS/BQ source)
            if not exists and (
                seed_mode == "real" or knowledge_gcs_uri or knowledge_bq_table
            ):
                if not knowledge_gcs_uri and not knowledge_bq_table:
                    print(
                        f"Notice: DataStore '{ds_id}' does not exist yet and SEED_MODE=real is set. "
                        "Skipping synthetic seed. Connect a 1st Party DataConnector (Google Drive / Salesforce) "
                        "in Gemini Enterprise or specify KNOWLEDGE_GCS_URI / KNOWLEDGE_BQ_TABLE."
                    )
                    continue
                content_cfg = (
                    discoveryengine.DataStore.ContentConfig.CONTENT_REQUIRED
                    if knowledge_gcs_uri
                    and os.environ.get("KNOWLEDGE_GCS_DATA_SCHEMA", "content") == "content"
                    else discoveryengine.DataStore.ContentConfig.NO_CONTENT
                )
                print(f"Creating production Agent Search DataStore: {datastore_name}...")
                ds = discoveryengine.DataStore(
                    display_name=f"Proposal Knowledge ({ds_id})",
                    industry_vertical=discoveryengine.IndustryVertical.GENERIC,
                    solution_types=[discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH],
                    content_config=content_cfg,
                )
                op = ds_client.create_data_store(
                    parent=collection_parent,
                    data_store=ds,
                    data_store_id=ds_id,
                )
                op.result(timeout=180)
                print(f"Created DataStore: {datastore_name}")
                if _import_real_documents(
                    doc_client,
                    branch_parent,
                    project_id,
                    knowledge_gcs_uri,
                    knowledge_bq_table,
                ):
                    summary["real_imported"].append(ds_id)
                continue

            # 3. Sandbox / synthetic seeding path (missing DataStore in auto mode, or explicit SEED_MODE=synthetic)
            if not synthetic_blobs_uploaded:
                try:
                    storage_client = storage.Client(project=project_id)
                    bucket = storage_client.bucket(bucket_name)
                    for blob_path, content in SYNTHETIC_DOCS.items():
                        blob = bucket.blob(blob_path)
                        blob.upload_from_string(
                            content.encode("utf-8"),
                            content_type="text/markdown; charset=utf-8",
                        )
                        print(f"Uploaded gs://{bucket_name}/{blob_path}")
                    synthetic_blobs_uploaded = True
                except Exception as gcs_exc:
                    print(f"Warning: synthetic markdown upload skipped: {gcs_exc}")

            if not exists:
                print(f"Creating Agent Search DataStore: {datastore_name}...")
                ds = discoveryengine.DataStore(
                    display_name=f"Proposal Knowledge ({ds_id})",
                    industry_vertical=discoveryengine.IndustryVertical.GENERIC,
                    solution_types=[discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH],
                    content_config=discoveryengine.DataStore.ContentConfig.NO_CONTENT,
                )
                op = ds_client.create_data_store(
                    parent=collection_parent,
                    data_store=ds,
                    data_store_id=ds_id,
                )
                op.result(timeout=180)
                print(f"Created DataStore: {datastore_name}")

            records = _records_for_datastore(ds_id)
            for record in records:
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
                    print(f"Seeded Agent Search document ({ds_id}): {doc_id}")
                except Exception as doc_exc:
                    print(f"Warning: could not seed document {doc_id} in {ds_id}: {doc_exc}")
            summary["synthetic_seeded"].append(ds_id)
    except Exception as exc:
        print(f"Warning: Agent Search datastore configuration skipped: {exc}")

    if ge_app_id:
        summary["engine_bound"] = bind_datastores_to_engine(
            project_id, location, datastore_ids, ge_app_id
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure Agent Search DataStores and optionally bind them to a Gemini Enterprise Engine."
    )
    parser.add_argument(
        "--bind-only",
        action="store_true",
        help="Skip DataStore creation/seeding and only bind AGENT_SEARCH_DATASTORE_ID(s) to GE_APP_ID.",
    )
    args = parser.parse_args()
    seed_knowledge_files(bind_only=args.bind_only)


if __name__ == "__main__":
    main()
