---
name: ge-proposal-site-publisher
description: >-
  Scaffolds, tests, deploys, and registers a full-stack Interactive Proposal
  Website Publisher Agent on Google Cloud (Agent Runtime on Gemini Enterprise
  Agent Platform + Gemini Enterprise + Managed Agents API /
  interactive-slide-designer Skill + Cloud Run Authentication Gateway + Private
  Cloud Storage + Firestore + Agent Search). Use when building an enterprise
  conversational agent that consults with business users, generates bespoke 16:9
  HTML5 proposal websites, issues client-scoped ID/password URLs immediately
  while the deck is generated asynchronously (Cloud Run job, Managed Agents API
  with Gemini fallback, auto-switching 'generating' page), and manages the
  post-publication lifecycle (status, edit, list, audit logs, credential
  rotation, and revocation).
---

# Gemini Enterprise Proposal Website Publisher Skill

End-to-end engineering blueprint and automation skill for deploying an interactive HTML5 proposal website generator & lifecycle manager on **Agent Runtime (Gemini Enterprise Agent Platform)** and **Gemini Enterprise**, paired with a zero-rebuild **Cloud Run Authentication Gateway**.

## 1. Architecture Overview

```text
[Business User in Gemini Enterprise Chat UI]
       │ (1. Interactive Consultation / Storyline Confirmation / Lifecycle Commands)
       ▼
[Agent Runtime (Gemini Enterprise Agent Platform): Interactive Concierge LlmAgent (google-adk)]
  ├─ Tool: search_internal_knowledge    ──► [Agent Search DataStore (RFPs / Cases)]
  ├─ Tool: create_proposal_website      ──► Issues URL / viewer ID / password in ~2 s (status=GENERATING)
  │                                          ├─► [Firestore: presentations/<id> (PBKDF2 hash, TTL, generation_status)]
  │                                          └─► [Cloud Run job: app/generation_worker.py  (jobs.run + PRESENTATION_ID)]
  │                                                 ├─ Managed Agents API (antigravity-preview-05-2026, 10 min budget)
  │                                                 ├─ fallback: gemini-3.8-flash structured output → deterministic template
  │                                                 └─► [Private Cloud Storage: presentations/<id>/index.html] + generation_status=ready
  ├─ Tool: get_proposal_status          ──► Reports phase / engine / elapsed; finalises stale generations inline
  ├─ Tool: edit_proposal_website        ──► Updates HTML in Private GCS & metadata in Firestore in-place
  ├─ Tool: list_proposal_websites       ──► Lists active/revoked decks + viewer access counts
  ├─ Tool: get_proposal_access_logs     ──► Reads viewer audit trail from presentations/<id>/access_logs
  ├─ Tool: manage_proposal_credentials  ──► Rotates viewer ID/password or extends expiration date
  └─ Tool: delete_proposal_website      ──► Sets status="revoked" (and optionally deletes GCS blob)
                                                       │
[External Client Browser (No Google Account Needed)]   │
       │ (2. Visits https://<gateway>/p/<id> with Viewer ID + Password)
       ▼                                               ▼
[Cloud Run Hosting Gateway]
  ├─ Verifies status=="active", TTL, and PBKDF2-HMAC-SHA256 / HMAC Cookie
  ├─ generation_status=="generating" -> branded 'generating' page polling GET /p/<id>/status every 5 s
  └─ generation_status=="ready"      -> Streams Private GCS HTML (the polling page reloads itself automatically)
```

## 2. Critical Implementation Guardrails (Gotchas)

1. **Conversational First — Never Generate Slides on Greetings**:
   - Do NOT use a rigid `SequentialAgent` as `root_agent` that immediately triggers HTML generation when a user says `"こんにちは"` or `"Hello"`.
   - Always use an interactive `LlmAgent` concierge as `root_agent` with explicit instruction guardrails: greet the user, explain capabilities, ask clarifying questions (target client, business challenge, theme color), and only call `create_proposal_website` when the user provides concrete parameters or confirms an outline.
2. **No Curly Braces `{var}` in ADK `LlmAgent` Instructions**:
   - ADK interpolates `{variable}` placeholders in `instruction` strings against `session.state` and raises `KeyError` if unset. Always write `<variable>` or `[VARIABLE]` inside `instruction` strings.
3. **Cloud Run Health Check Path (`/health`, NOT `/healthz`)**:
   - Google Frontend (GFE) on `*.run.app` reserves paths ending in `z` (`/healthz`, `/statusz`) and returns HTTP 404 externally. Always expose `/health` on the Cloud Run gateway.
4. **Agent Search Quota Project**:
   - Always pass `ClientOptions(quota_project_id=project_id)` when initializing `discoveryengine.SearchServiceClient` to avoid `403 PERMISSION_DENIED` under Application Default Credentials.
5. **Cloud Build PyPI Index in `Dockerfile`**:
   - Run `RUN uv sync --default-index https://pypi.org/simple` in `proposal_agent/Dockerfile` so Cloud Build never fails on workstation-local mirror URLs.
6. **Tools Must Never Raise — Return `status` Payloads**:
   - In Google ADK an exception escaping a tool aborts the whole turn (`google.adk.workflow._errors.DynamicNodeFailError: Dynamic node <agent> failed`). Gemini Enterprise then renders the tool chip but **no final answer** — the chat looks "stuck". Root cause seen in production: the LLM passed a free-form outline as `deck_spec_json` and `PresentationDeckSpec.model_validate()` raised `pydantic_core.ValidationError` inside `create_proposal_website`.
   - Wrap every tool body in `try/except` and return `{"status": "ERROR" | "NOT_FOUND" | "GENERATING", "user_message": ..., "next_action": ...}`. Coerce invalid `deck_spec_json` into an *outline hint* instead of failing, and tell the model (in the instruction) to put agreed outlines inside `proposal_brief`.
7. **Keep Tool Calls Short — Generate Asynchronously**:
   - Managed Agents runs take 30 s – several minutes; never block the tool on them. `create_proposal_website` writes the Firestore document with `generation_status="generating"`, triggers the Cloud Run job (`GENERATION_JOB_NAME`, `run.jobs.runWithOverrides` → requires `roles/run.developer` + `roles/iam.serviceAccountUser` on the job SA for the Reasoning Engine service agent) and returns within ~2 s. `GENERATION_TRIGGER_MODE=auto` falls back to an in-process daemon thread when the job cannot be triggered.
   - Tiered budget inside the worker: Managed Agents API (`MANAGED_AGENT_DEADLINE_SECONDS`, default 600) → `gemini-3.8-flash` structured output → deterministic skill template. Record the winner in `generation_engine` and surface it via `get_proposal_status`.
8. **Interactions API Request Shape (google-genai ≥ 2.28)**:
   - `client.interactions.create(agent=MANAGED_AGENT_MODEL, input=prompt, environment={"type": "remote"}, background=True, store=True, stream=False)` on `location="global"`, then poll `client.interactions.get(id)` until `status == "completed"` and read `output_text`. There is **no** `config=` keyword — passing one raises `TypeError: create() got unexpected keyword argument(s): config`. Cancel with `client.interactions.cancel(id)` when the deadline passes.
9. **Browser Polling Must Reuse the Session Cookie**:
   - The 'generating' page calls `fetch('/p/<id>/status', {credentials: 'same-origin'})`; the gateway sets the HMAC session cookie on the first Basic-auth response so the status poll and the final reload never prompt for credentials again. The `/status` endpoint is itself authenticated (401 without cookie/Basic) and served with `Cache-Control: no-store`.
10. **Gemini Enterprise Registration Is Create-Only**:
   - `agents-cli publish gemini-enterprise` (v1.4.0+) requires the full engine resource name (`projects/<project-number>/locations/global/collections/default_collection/engines/<engine-id>`) and always creates a *new* agent registration. Because the registration points at the Reasoning Engine resource, re-deploying the agent with `agents-cli deploy` updates the live GE agent in place — do **not** re-publish on every deploy. To change the display text of an existing registration, `PATCH https://discoveryengine.googleapis.com/v1alpha/<agent-name>?updateMask=description,adkAgentDefinition.toolSettings.toolDescription`.

## 3. Step-by-Step Deployment Recipe

### Step 1: Run Unit & Sanitization Tests Locally

```bash
uv run --project proposal_agent pytest tests/test_proposal_agent_and_gateway.py -v
python3 skills/interactive-slide-designer/scripts/validate_slide_deck.py --help
python3 skills/ge-proposal-site-publisher/scripts/verify_sanitization.py .
```

### Step 2: Provision Infrastructure & Deploy All Three Workloads

Set your target project variables and run `infra/deploy.sh`:

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
export PROPOSAL_GCS_BUCKET="${PROJECT_ID}-proposal-sites"
export AGENT_SEARCH_DATASTORE_ID="proposal-knowledge-datastore"
export GE_APP_ID="your-gemini-enterprise-app-id"

bash infra/deploy.sh
# 7 phases: APIs/IAM → bucket → Firestore → Agent Search seed → Cloud Run gateway → Cloud Run job → Agent Runtime (+ GE publish)
# Iterate on the agent only: SKIP_INFRA=1 SKIP_SEED=1 SKIP_GATEWAY=1 SKIP_JOB=1 bash infra/deploy.sh
```

See [references/architecture_and_iam.md](references/architecture_and_iam.md) for the required IAM bindings on the Reasoning Engine service agent (`service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com`).

### Step 3: Run End-to-End Live Verification

```bash
uv run --project proposal_agent python tests/run_live_e2e_verification.py

# Multi-turn conversation against the deployed Agent Runtime (the exact flow that used to stall):
#   turn 1: outline consultation → turn 2: 「はい。skyで。」 → URL / ID / password in the final text
#   → 'generating' page → /status polling → finished deck → wrong password rejected (401)
export REASONING_ENGINE_ID="projects/<project-number>/locations/us-central1/reasoningEngines/<id>"
uv run --project proposal_agent python tests/run_remote_multiturn_e2e.py
```

This verifies:
- Sending `"こんにちは"` returns a conversational hearing response without calling `create_proposal_website`.
- Requesting a proposal website generates a validated 6-slide HTML5 deck (`hero-cover`, `bento-executive-summary`, `as-is-to-be-comparison`, `architecture-flow`, `roadmap-timeline`, `roi-and-next-steps`), uploads it to Private GCS, and blocks direct public GCS URLs with `HTTP 403`.
- Cloud Run Hosting Gateway enforces `HTTP 401` when unauthenticated, `HTTP 200` on Basic Auth and signed session cookie auth, and logs viewer access in Firestore.
- Live editing (`edit_proposal_website`), credential rotation (`manage_proposal_credentials`), and revocation (`delete_proposal_website` -> `HTTP 403`) work end-to-end.
- `run_remote_multiturn_e2e.py` additionally asserts that turn 2 ends with a non-empty answer containing the share URL, viewer ID and password, that the Cloud Run job reaches `generation_status=ready` (engine recorded, e.g. `managed_agents_api:antigravity-preview-05-2026`), and that the finished deck is served through the same authenticated URL.
