---
name: ge-proposal-site-publisher
description: >-
  Scaffolds, tests, deploys, and registers a full-stack Interactive Proposal
  Website Publisher Agent on Google Cloud (Vertex AI Agent Runtime + Gemini
  Enterprise + Managed Agents API / interactive-slide-designer Skill + Cloud Run
  Authentication Gateway + Private Cloud Storage + Firestore + Vertex AI
  Search). Use when building an enterprise conversational agent that consults
  with business users, generates bespoke 16:9 HTML5 proposal websites, issues
  client-scoped ID/password URLs, and manages the post-publication lifecycle
  (edit, list, audit logs, credential rotation, and revocation).
---

# Gemini Enterprise Proposal Website Publisher Skill

End-to-end engineering blueprint and automation skill for deploying an interactive HTML5 proposal website generator & lifecycle manager on **Vertex AI Agent Runtime** and **Gemini Enterprise**, paired with a zero-rebuild **Cloud Run Authentication Gateway**.

## 1. Architecture Overview

```text
[Business User in Gemini Enterprise Chat UI]
       │ (1. Interactive Consultation / Storyline Confirmation / Lifecycle Commands)
       ▼
[Vertex AI Agent Runtime: Interactive Concierge LlmAgent (google-adk)]
  ├─ Tool: search_internal_knowledge    ──► [Vertex AI Search DataStore (RFPs / Cases)]
  ├─ Tool: create_proposal_website      ──► [Managed Agents API + interactive-slide-designer Skill]
  │                                          ├─► [Private Cloud Storage: presentations/<id>/index.html]
  │                                          └─► [Firestore: presentations/<id> (PBKDF2 hash, TTL, spec)]
  ├─ Tool: edit_proposal_website        ──► Updates HTML in Private GCS & metadata in Firestore in-place
  ├─ Tool: list_proposal_websites       ──► Lists active/revoked decks + viewer access counts
  ├─ Tool: get_proposal_access_logs     ──► Reads viewer audit trail from presentations/<id>/access_logs
  ├─ Tool: manage_proposal_credentials  ──► Rotates viewer ID/password or extends expiration date
  └─ Tool: delete_proposal_website      ──► Sets status="revoked" (and optionally deletes GCS blob)
                                                       │
[External Client Browser (No Google Account Needed)]   │
       │ (2. Visits https://<gateway>/p/<id> with Viewer ID + Password)
       ▼                                               ▼
[Cloud Run Hosting Gateway (+ Optional Firebase Hosting)]
  └─ Verifies status=="active", TTL, and PBKDF2-HMAC-SHA256 / HMAC Cookie -> Streams Private GCS HTML
```

## 2. Critical Implementation Guardrails (Gotchas)

1. **Conversational First — Never Generate Slides on Greetings**:
   - Do NOT use a rigid `SequentialAgent` as `root_agent` that immediately triggers HTML generation when a user says `"こんにちは"` or `"Hello"`.
   - Always use an interactive `LlmAgent` concierge as `root_agent` with explicit instruction guardrails: greet the user, explain capabilities, ask clarifying questions (target client, business challenge, theme color), and only call `create_proposal_website` when the user provides concrete parameters or confirms an outline.
2. **No Curly Braces `{var}` in ADK `LlmAgent` Instructions**:
   - ADK interpolates `{variable}` placeholders in `instruction` strings against `session.state` and raises `KeyError` if unset. Always write `<variable>` or `[VARIABLE]` inside `instruction` strings.
3. **Cloud Run Health Check Path (`/health`, NOT `/healthz`)**:
   - Google Frontend (GFE) on `*.run.app` reserves paths ending in `z` (`/healthz`, `/statusz`) and returns HTTP 404 externally. Always expose `/health` on the Cloud Run gateway.
4. **Vertex AI Search Quota Project**:
   - Always pass `ClientOptions(quota_project_id=project_id)` when initializing `discoveryengine.SearchServiceClient` to avoid `403 PERMISSION_DENIED` under Application Default Credentials.
5. **Cloud Build PyPI Index in `Dockerfile`**:
   - Run `RUN uv sync --default-index https://pypi.org/simple` in `proposal_agent/Dockerfile` so Cloud Build never fails on workstation-local mirror URLs.

## 3. Step-by-Step Deployment Recipe

### Step 1: Run Unit & Sanitization Tests Locally

```bash
uv run --project proposal_agent pytest tests/test_proposal_agent_and_gateway.py -v
python3 skills/interactive-slide-designer/scripts/validate_slide_deck.py --help
python3 skills/ge-proposal-site-publisher/scripts/verify_sanitization.py .
```

### Step 2: Provision Infrastructure & Deploy Both Services

Set your target project variables and run `infra/deploy.sh`:

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
export PROPOSAL_GCS_BUCKET="${PROJECT_ID}-proposal-sites"
export VERTEX_SEARCH_DATASTORE_ID="proposal-knowledge-datastore"
export GE_APP_ID="your-gemini-enterprise-app-id"

bash infra/deploy.sh
```

See [references/architecture_and_iam.md](references/architecture_and_iam.md) for the required IAM bindings on the Reasoning Engine service agent (`service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com`).

### Step 3: Run End-to-End Live Verification

```bash
uv run --project proposal_agent python tests/run_live_e2e_verification.py
```

This verifies:
- Sending `"こんにちは"` returns a conversational hearing response without calling `create_proposal_website`.
- Requesting a proposal website generates a validated 6-slide HTML5 deck (`hero-cover`, `bento-executive-summary`, `as-is-to-be-comparison`, `architecture-flow`, `roadmap-timeline`, `roi-and-next-steps`), uploads it to Private GCS, and blocks direct public GCS URLs with `HTTP 403`.
- Cloud Run Hosting Gateway enforces `HTTP 401` when unauthenticated, `HTTP 200` on Basic Auth and signed session cookie auth, and logs viewer access in Firestore.
- Live editing (`edit_proposal_website`), credential rotation (`manage_proposal_credentials`), and revocation (`delete_proposal_website` -> `HTTP 403`) work end-to-end.
