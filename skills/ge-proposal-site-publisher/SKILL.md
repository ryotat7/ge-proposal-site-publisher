---
name: ge-proposal-site-publisher
description: >-
  Scaffolds, tests, deploys, and registers a full-stack Interactive Proposal
  Website Publisher Agent on Google Cloud (Agent Runtime on Gemini Enterprise
  Agent Platform + Gemini Enterprise + ADK designer agent on gemini-3.8-flash +
  Cloud Run generation job + private Cloud Run deck renderer + Cloud Run
  authentication gateway + private Cloud Storage + Firestore + Agent Search).
  Use when building an enterprise conversational agent that consults with
  business users, issues client-scoped ID/password URLs immediately, designs
  HTML proposal decks in the background (free-form design reviewed from
  rendered screenshots, or a fast 6-slide template), and manages the
  post-publication lifecycle (status, edit, versions/undo with a live
  updating banner, list, audit logs, credential rotation, and revocation).
---

# Gemini Enterprise Proposal Website Publisher Skill

End-to-end engineering blueprint and automation skill for deploying an interactive HTML proposal website generator and lifecycle manager on **Agent Runtime (Gemini Enterprise Agent Platform)** and **Gemini Enterprise**. All model calls use `gemini-3.8-flash` through Google ADK; there is no other agent backend.

## 1. Architecture Overview

```text
[Business User in Gemini Enterprise Chat UI]
       │ (1. Consultation / storyline confirmation / lifecycle commands)
       ▼
[Agent Runtime (Gemini Enterprise Agent Platform): Interactive Concierge LlmAgent (google-adk, gemini-3.8-flash)]
  ├─ Tool: search_internal_knowledge    ──► [Agent Search DataStore (RFPs / cases)]
  ├─ Tool: create_proposal_website      ──► Issues URL / viewer ID / password in ~2 s (status=GENERATING)
  │                                          ├─► [Firestore: presentations/<id> (PBKDF2 hash, TTL, generation_status)]
  │                                          └─► [Cloud Run job: app/generation_worker.py (PRESENTATION_ID, JOB_MODE=generate)]
  │                                                 ├─ design_mode=freeform (default): ADK designer agent (app/adk_designer.py)
  │                                                 │    writes staging/<id>/<run>/deck/ via GCS file tools + check_deck
  │                                                 │    ⇄ [Cloud Run deck renderer (private): POST /v1/render → screenshots + layout audit]
  │                                                 │    review rounds (max 2) in the SAME ADK session → publish presentations/<id>/v<N>/
  │                                                 ├─ design_mode=template (fast mode / fallback): gemini-3.8-flash structured output
  │                                                 │    → deterministic template → presentations/<id>/index.html
  │                                                 └─► Firestore generation_status=ready, content_version, freeform_versions
  ├─ Tool: get_proposal_status          ──► Phase / engine / elapsed / ETA; finalises stale runs with the template
  ├─ Tool: edit_proposal_website        ──► free-form: EDIT_QUEUED → job JOB_MODE=freeform_edit; template: synchronous edit;
  │                                          undo_last_edit switches the version pointer; reports verified_changes only
  ├─ Tool: list_proposal_websites       ──► Lists active/revoked decks + viewer access counts
  ├─ Tool: get_proposal_access_logs     ──► Reads presentations/<id>/access_logs
  ├─ Tool: manage_proposal_credentials  ──► Rotates viewer ID/password or extends the expiration date
  └─ Tool: delete_proposal_website      ──► Sets status="revoked" (optionally deletes GCS objects)
                                                       │
[External Client Browser (no Google account needed)]   │
       │ (2. Visits https://<gateway>/p/<id>/ with viewer ID + password)
       ▼                                               ▼
[Cloud Run Hosting Gateway]
  ├─ Verifies status=="active", TTL, PBKDF2-HMAC-SHA256 / HMAC session cookie; writes access_logs
  ├─ generating  -> branded 'generating' page polling GET /p/<id>/status
  ├─ updating    -> current version + live 「更新中」 banner; reloads when content_version increases
  └─ ready       -> streams the deck from private GCS (free-form: per-request nonce CSP, runtime under /_rt/v1/)
```

## 2. Critical Implementation Guardrails (Gotchas)

1. **Conversational first — never generate slides on greetings**: use an interactive `LlmAgent` concierge as `root_agent`; greet, explain capabilities, ask clarifying questions (client, challenge, free design vs fast mode), and only call `create_proposal_website` when the user gives concrete parameters or approves an outline.
2. **No curly braces `{var}` in ADK instructions**: ADK interpolates `{variable}` against `session.state` and raises `KeyError` if unset. This applies to `CONCIERGE_INSTRUCTION` and to the designer's `ADK_INSTRUCTION`.
3. **Cloud Run health check path is `/health`, not `/healthz`**: the Google Frontend on `*.run.app` reserves paths ending in `z`.
4. **Agent Search quota project**: pass `ClientOptions(quota_project_id=project_id)` to `discoveryengine.SearchServiceClient` to avoid `403 PERMISSION_DENIED` under ADC.
5. **Cloud Build PyPI index in `Dockerfile`**: run `uv sync --default-index https://pypi.org/simple` in `proposal_agent/Dockerfile` so Cloud Build never depends on workstation-local mirror URLs.
6. **Tools must never raise — return `status` payloads**: an exception escaping an ADK tool aborts the whole turn (`DynamicNodeFailError`) and Gemini Enterprise shows a tool chip with no answer. Wrap every tool and return `{"status": "ERROR" | "NOT_FOUND" | "GENERATING" | "EDIT_QUEUED" | ..., "user_message": ..., "next_action": ...}`. Coerce an invalid `deck_spec_json` into an outline hint instead of failing.
7. **Keep tool calls short — design asynchronously**: free-form design takes 通常 7〜11 分（最長約 20 分）, free-form edits 通常 5〜7 分（最長約 15 分）. `create_proposal_website` and free-form `edit_proposal_website` only write Firestore and trigger the Cloud Run job (`run.jobs.runWithOverrides` with `PRESENTATION_ID` and `JOB_MODE`), then return. `GENERATION_TRIGGER_MODE=auto` falls back to an in-process thread when the job cannot be triggered. The job cold start (≈1.5 minutes) is part of the ETA.
8. **The designer is an ADK `LlmAgent` run in-process by the worker**: `app/adk_designer.py` builds the agent (`FREEFORM_ADK_MODEL`, default `gemini-3.8-flash`, `location=global`) with GCS-backed tools only (`list_files`, `read_file`, `write_file`, `replace_in_file`, `delete_file`, `check_deck`) scoped to `staging/<id>/<run>/input/` (read-only) and `deck/` (read-write). It writes no JavaScript, runs no code and has no network access. Tools never raise. It is executed with an ADK `Runner` inside the job, not deployed separately.
9. **Review rounds use screenshots as user input in the same session**: render with the private deck renderer, then send the screenshots as image parts of the next *user* message in the same ADK session (not inside tool results). Round 2 only sends slides whose screenshot changed plus slides still flagged, and is skipped when nothing visible changed and checks are clean. On timeout, publish the best valid build; never publish a build with static contract errors.
10. **Grounding check**: `freeform.ungrounded_content()` flags percentages that appear in no input (hedge words such as 試算 / イメージ only count within the same clause), e-mail addresses not in the inputs, and work-file names. Findings go to `check_deck`, the review turn and Firestore (`freeform.grounding_issues`); they do not block publishing.
11. **Edits report verified changes only**: `verified_changes` comes from a deterministic diff (`deck_contract.describe_changes()` for free-form, `describe_deck_changes()` for template decks). Designer text is labelled 自己申告. `NO_CHANGE` must not upload anything. Otherwise the concierge confidently reports edits that never happened.
12. **Versions and undo**: free-form versions live under `presentations/<id>/v<N>/` and in `freeform_versions` (max 10, each with `based_on`). Undo only switches the pointer back to `based_on`; it never regenerates. Every publication bumps `content_version`, which drives the gateway's 「更新中」 watcher.
13. **Single source of truth for the deck contract**: `proposal_agent/app/deck_contract.py` and `proposal_agent/app/deck_runtime/` are copied into `hosting_gateway/` and `deck_renderer/` by `infra/deploy.sh` (git-ignored copies; each service has its own `.gcloudignore` so `gcloud run deploy --source` still uploads them). Never edit the copies.
14. **Browser polling reuses the session cookie**: the generating page and the 「更新中」 watcher call `/p/<id>/status` with `credentials: 'same-origin'`; the gateway sets the HMAC session cookie on the first authenticated response. `/status` is itself authenticated and served with `Cache-Control: no-store, private`, and it never exposes `verified_changes` to viewers.
15. **Gemini Enterprise registration is create-only**: `agents-cli publish gemini-enterprise` (v1.4.0+) needs the full engine resource name (`projects/<project-number>/locations/global/collections/default_collection/engines/<engine-id>`) and always creates a *new* registration. The registration points at the Agent Runtime resource, so `agents-cli deploy` updates the live agent in place — do not re-publish on every deploy. To change the display text of an existing registration, `PATCH https://discoveryengine.googleapis.com/v1alpha/<agent-name>?updateMask=description,adkAgentDefinition.toolSettings.toolDescription`.

## 3. Step-by-Step Deployment Recipe

### Step 1: Run unit and sanitization tests locally

```bash
cd proposal_agent && uv sync && cd ..
proposal_agent/.venv/bin/python -m pytest tests -q
python3 skills/interactive-slide-designer/scripts/validate_slide_deck.py --help
python3 skills/ge-proposal-site-publisher/scripts/verify_sanitization.py .
```

### Step 2: Provision infrastructure and deploy all workloads

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
export PROPOSAL_GCS_BUCKET="${PROJECT_ID}-proposals"            # default
export AGENT_SEARCH_DATASTORE_ID="proposal-knowledge-datastore"   # default
export FREEFORM_ADK_MODEL="gemini-3.8-flash"                      # default designer model
# First deployment only (full engine resource name or bare engine id):
export GE_APP_ID="projects/<project-number>/locations/global/collections/default_collection/engines/<engine-id>"

bash infra/deploy.sh
```

| Phase | What `infra/deploy.sh` does | Skip flag |
|---|---|---|
| 1–3 | Enables APIs; creates the private bucket (uniform access, public access prevention) and grants IAM; ensures the Firestore Native database | `SKIP_INFRA=1` |
| 4 | Seeds synthetic RFP / case-study documents into Agent Search | `SKIP_SEED=1` |
| 5 | Copies the deck contract/runtime and deploys the hosting gateway (`proposal-hosting-gateway`, public, app-level auth) | `SKIP_GATEWAY=1` |
| 6 | Copies the deck contract/runtime and deploys the deck renderer (`proposal-deck-renderer`, `--no-allow-unauthenticated`, `ALLOWED_BUCKETS=<bucket>`), then grants `roles/run.invoker` to the job's service account | `SKIP_RENDERER=1` (also skipped when `FREEFORM_DESIGN_ENABLED!=true`) |
| 7 | Deploys the generation job (`proposal-deck-generator`, 2 GiB, `--task-timeout=1500s`) with `FREEFORM_DESIGN_ENABLED`, `FREEFORM_ADK_MODEL`, `DECK_RENDERER_URL`, `IMAGE_MODEL`, `FREEFORM_TOTAL_BUDGET_SECONDS`, `FREEFORM_REVIEW_ROUNDS` | `SKIP_JOB=1` |
| 8 | Deploys the concierge to Agent Runtime with `agents-cli deploy`; registers it with Gemini Enterprise when `GE_APP_ID` is set | `SKIP_AGENT=1` |

Iterate on the agent only: `SKIP_INFRA=1 SKIP_SEED=1 SKIP_GATEWAY=1 SKIP_RENDERER=1 SKIP_JOB=1 bash infra/deploy.sh`. Set `HOSTING_BASE_URL` to keep a fixed share-URL host (custom domain or the deterministic `https://<service>-<project-number>.<region>.run.app` form). Set `FREEFORM_DESIGN_ENABLED=false` for a template-only deployment without the renderer.

See [references/architecture_and_iam.md](references/architecture_and_iam.md) for the IAM roles of every workload and the full environment-variable reference.

### Step 3: Register in Gemini Enterprise

- First deployment: set `GE_APP_ID` and run phase 8 (or `agents-cli publish gemini-enterprise` from `proposal_agent/`).
- Later deployments: leave `GE_APP_ID` unset; `agents-cli deploy` updates the registered agent in place. Use the `PATCH` call in gotcha 15 to change the description shown to users.

### Step 4: Run end-to-end live verification

```bash
export HOSTING_BASE_URL="$(gcloud run services describe proposal-hosting-gateway --region=${REGION} --project=${PROJECT_ID} --format='value(status.url)')"
proposal_agent/.venv/bin/python tests/run_live_e2e_verification.py

# Multi-turn conversation against the deployed Agent Runtime:
#   turn 1: outline consultation → turn 2: 「はい。skyで。」 → URL / ID / password in the final text
#   → 'generating' page → /status polling → finished deck → wrong password rejected (401)
export REASONING_ENGINE_ID="projects/<project-number>/locations/us-central1/reasoningEngines/<id>"
proposal_agent/.venv/bin/python tests/run_remote_multiturn_e2e.py
```

This verifies:
- Sending `"こんにちは"` returns a conversational response without calling `create_proposal_website`.
- A proposal website is generated, stored in private Cloud Storage (direct public GCS URLs return `HTTP 403`), and served through the gateway (`401` unauthenticated, `200` with Basic auth or the signed session cookie, access logs written to Firestore).
- Editing (`edit_proposal_website`), credential rotation (`manage_proposal_credentials`) and revocation (`delete_proposal_website` → `HTTP 403`) work end to end.
- `run_remote_multiturn_e2e.py` asserts that turn 2 ends with an answer containing the share URL, viewer ID and password, that the job reaches `generation_status=ready` with `generation_engine` recorded (e.g. `adk_freeform:gemini-3.8-flash`), and that the finished deck is served through the same authenticated URL.
