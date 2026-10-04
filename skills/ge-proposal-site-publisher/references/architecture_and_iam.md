# Architecture, Firestore Schema & IAM Reference

## 1. Firestore Data Model (`presentations` Collection)

Each published presentation is stored at `presentations/<presentation_id>`:

| Field | Type | Description |
| :--- | :--- | :--- |
| `presentation_id` | `string` | Unique identifier (`prop-YYYYMMDD-<8hex>`) |
| `client_name` / `proposal_title` / `subtitle` | `string` | Client and headline metadata |
| `design_mode` | `string` | `freeform` (ADK designer, default) or `template` (6-slide fast mode) — what the user asked for |
| `render_mode` | `string` | What is currently published: `freeform` or `template` (a free-form request can fall back to `template`) |
| `theme_color` / `design_style` | `string` | Template accent (`sky`, `emerald`, `violet`, `amber`, `rose`) and look (`immersive-dark`, `clean-light`, `editorial-light`) |
| `viewer_id` | `string` | Generated login ID for external viewers |
| `password_hash` / `password_salt` | `string` | PBKDF2-HMAC-SHA256 digest (120,000 iterations) and 16-byte salt, hex-encoded |
| `gcs_bucket` / `gcs_blob_path` | `string` | Private bucket and the published `index.html` (`presentations/<id>/index.html` or `presentations/<id>/v<N>/index.html`) |
| `deck_spec` / `previous_deck_spec` | `map` | Template decks: current `PresentationDeckSpec` and the spec before the last edit (undo) |
| `status` / `is_active` | `string` / `boolean` | `"active"` or `"revoked"` |
| `created_at` / `updated_at` / `expires_at` | `string` | ISO-8601 UTC timestamps |
| `generation_status` | `string` | `generating` → `ready` (or `failed`); `updating` while an edit is in progress — drives the gateway's generating page and 「更新中」 banner |
| `generation_phase` | `string` | `queued` / `knowledge_search` / `freeform_staging` / `freeform_drafting` / `freeform_images` / `freeform_checking` / `freeform_reviewing` / `freeform_publishing` / `freeform_fallback` / `deterministic_template` / `rendering` / `freeform_edit_queued` / `edit_queued` / `edit_designing` / `edit_rendering` / `edit_publishing` / `ready` / `failed` |
| `generation_engine` | `string` | `adk_freeform:<model>`, `agent_platform_gemini_with_skill:<model>`, `deterministic_skill_template[:reason]`, `state_deck_spec`; a template fallback after a free-form attempt is tagged `+freeform_fallback:<reason>` |
| `generation_engine_label` | `string` | Human-readable Japanese label of `generation_engine` |
| `generation_dispatch` / `generation_execution` | `string` | `cloud_run_job` / `inline_thread` / `sync`, and the Cloud Run job execution name |
| `generation_inputs` | `map` | Inputs consumed by the worker (`client_name`, `proposal_title`, `proposal_brief`, `theme_color`, `outline_hint`, design request, …) |
| `generation_error` | `string` | Last error message when `generation_status="failed"` |
| `content_version` | `number` | Incremented on every publication (first deck = 1); the gateway watcher reloads open tabs when it increases |
| `freeform_prefix` | `string` | Published free-form prefix (`presentations/<id>/v<N>/`) |
| `freeform` | `map` | Current free-form version: `current_version`, `prefix`, `session_id`, `review_rounds`, `layout_errors`, `grounding_issues`, `warnings`, `ai_images`, `usage` (tokens), `qa_screenshots` (`gs://…/_qa/`), `model`, `elapsed_seconds` |
| `freeform_versions` | `array<map>` | Last 10 versions: `version`, `prefix`, `created_at`, `slide_count`, `based_on`, `based_on_render_mode` — undo switches back to `based_on` |
| `last_edit_result` | `map` | `status` (`applied` / `no_change` / `failed`), `edit_engine`, `verified_changes`, `unsupported_requests`, `content_version`, `applied_at` |

### Subcollection: `presentations/<presentation_id>/access_logs`

Each successful viewer authentication appends `accessed_at`, `viewer_id`, `auth_method` (`"basic_auth"` or `"session_cookie"`; a browser that signed in through the login form is logged as `"session_cookie"` on its next page load), `ip_address` and `user_agent`.

### Cloud Storage layout (private bucket)

| Prefix | Written by | Content |
| :--- | :--- | :--- |
| `knowledge/` | `infra/seed_datastore.py` | Synthetic RFP / case-study markdown |
| `staging/<id>/<run_id>/input/` | worker | `brief.md`, `knowledge.md` (read-only for the designer) |
| `staging/<id>/<run_id>/deck/` | ADK designer agent | `index.html`, `assets/**`, `charts/*.json`, `manifest.json` |
| `presentations/<id>/v<N>/` | worker | Published free-form version + `_qa/` screenshots |
| `presentations/<id>/index.html` | agent / worker | Published template deck (`versions/` keeps backups before edits) |

## 2. Required IAM Roles

`infra/deploy.sh` grants the following roles to both the Compute Engine default service account (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`, which runs the generation job, the deck renderer and the hosting gateway) and the Agent Runtime service agent (`service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com`):

| Role | Scope | Why |
| :--- | :--- | :--- |
| `roles/storage.objectAdmin` | `gs://<PROPOSAL_GCS_BUCKET>` | Staging, publishing, versions, screenshots; the gateway and renderer read decks |
| `roles/datastore.user` | Project | Firestore read/write (credentials, status, versions, access logs) |
| `roles/discoveryengine.viewer` | Project | Agent Search grounding |
| `roles/aiplatform.user` | Project | `gemini-3.8-flash` (concierge, ADK designer, template path) and `gemini-3.1-flash-image` on Gemini Enterprise Agent Platform |
| `roles/serviceusage.serviceUsageConsumer` | Project | Quota project checks |
| `roles/run.developer` | Project | `run.jobs.runWithOverrides` — the agent starts the job with per-execution `PRESENTATION_ID` / `JOB_MODE` overrides (`roles/run.invoker` alone is not enough) |

Additional bindings:

- **Discovery Engine Service Agent (`service-<PROJECT_NUMBER>@gcp-sa-discoveryengine.iam.gserviceaccount.com`)**: provisioned via `gcloud beta services identity create --service=discoveryengine.googleapis.com` and granted `roles/storage.objectViewer`, `roles/bigquery.dataViewer`, `roles/bigquery.jobUser`, and `roles/discoveryengine.viewer` so Agent Search can ingest documents from Cloud Storage or BigQuery and operate across 1st Party DataConnectors (Google Drive, Salesforce).
- `roles/iam.serviceAccountUser` on the compute service account, granted to the Agent Runtime service agent (the job runs as the compute SA, so triggering it needs `actAs`).
- `roles/run.invoker` on the `proposal-deck-renderer` service, granted to the compute service account. The renderer is deployed with `--no-allow-unauthenticated`; the job calls it with an ID token, and the renderer only reads prefixes in `ALLOWED_BUCKETS`.
- The hosting gateway is deployed with `--allow-unauthenticated` because external viewers have no Google account; authentication happens in the application (per-deal viewer ID/password, HMAC session cookie).

If you run the workloads under dedicated service accounts instead of the compute default, give the job SA the bucket, Firestore, Agent Search and `aiplatform.user` roles plus `run.invoker` on the renderer; the renderer SA `storage.objectViewer` on the bucket; the gateway SA `storage.objectViewer` on the bucket and `datastore.user`; and the Agent Runtime service agent `run.developer` plus `iam.serviceAccountUser` on the job SA.

## 3. Workloads & Environment Variables

| Workload | Entry point | Key environment variables |
| :--- | :--- | :--- |
| Agent Runtime (concierge) | `proposal_agent/app/agent.py` (`root_agent`) | `GEMINI_MODEL` (`gemini-3.8-flash`), `GENAI_LOCATION` (`global`), `GENERATION_JOB_NAME` (`projects/<p>/locations/<r>/jobs/<job>`), `GENERATION_TRIGGER_MODE` (`auto` \| `cloud_run_job` \| `inline_thread` \| `sync` \| `none`), `GENERATION_STALE_MINUTES` (13), `FREEFORM_GENERATION_STALE_MINUTES` (22), `FREEFORM_EDIT_STALE_SECONDS` (1500), `EDIT_STALE_SECONDS` (300), `ENABLE_LLM_DECK_EDIT` (`true`), `FREEFORM_DESIGN_ENABLED`, `HOSTING_BASE_URL`, `PROPOSAL_GCS_BUCKET`, `PROPOSAL_FIRESTORE_COLLECTION`, `AGENT_SEARCH_DATASTORE_ID` (single ID or comma-/colon-separated list of DataStores, e.g., `drive-past-rfps-ds,salesforce-crm-ds`), `PROPOSAL_BRAND_NAME`, `PROPOSAL_BRAND_BADGE` |
| Cloud Run job (`proposal-deck-generator`) | `uv run --no-sync python -m app.generation_worker` (reads `PRESENTATION_ID`, `JOB_MODE`=`generate` \| `freeform_edit`) | Storage/model variables as above, plus `FREEFORM_DESIGN_ENABLED` (`true`), `FREEFORM_ADK_MODEL` (`gemini-3.8-flash`), `FREEFORM_ADK_MAX_LLM_CALLS` (120), `DECK_RENDERER_URL` (empty = skip review rounds), `IMAGE_MODEL` (`gemini-3.1-flash-image`), `FREEFORM_TOTAL_BUDGET_SECONDS` (900), `FREEFORM_REVIEW_ROUNDS` (2); `--task-timeout=1500s`, `--memory=2Gi`, `--max-retries=1` |
| DataStore seeder & binder | `infra/seed_datastore.py` (`--bind-only` optional) | `AGENT_SEARCH_DATASTORE_ID` (comma-/colon-separated IDs), `SEED_MODE` (`auto` \| `real` \| `synthetic` \| `none`), `KNOWLEDGE_GCS_URI` (optional `gs://…/*.jsonl`), `KNOWLEDGE_BQ_TABLE` (optional `project.dataset.table`), `GE_APP_ID` (optional engine ID or resource name for `dataStoreIds` binding) |
| Cloud Run service (deck renderer) | `deck_renderer/main.py` (`POST /v1/render`, `GET /health`) | `ALLOWED_BUCKETS`, `CHROME_BIN`; `--concurrency=1`, `--cpu=2`, `--memory=2Gi`, `--no-allow-unauthenticated` |
| Cloud Run service (hosting gateway) | `hosting_gateway/main.py` | `PROPOSAL_GCS_BUCKET`, `PROPOSAL_FIRESTORE_COLLECTION`, `GATEWAY_SESSION_SECRET`, `PROPOSAL_COOKIE_PREFIX`, `PROPOSAL_BRAND_NAME`, `PROPOSAL_BRAND_BADGE`, `LIVE_UPDATE_WATCHER` (`true`), `LIVE_UPDATE_POLL_SECONDS` (5), `UPDATING_STALE_SECONDS` (300), `FREEFORM_UPDATING_STALE_SECONDS` (1500), `DECK_RUNTIME_DIR` |

### Gateway endpoints

| Endpoint | Auth | Behaviour |
| :--- | :--- | :--- |
| `GET /p/{id}/` (free-form) · `GET /p/{id}` (template) | Basic auth, login form or session cookie | `generating` → branded interim page that polls `/status`; `updating` → current version with the 「更新中」 banner; `failed` → HTTP 503; `ready` → streams the deck from private GCS; revoked/expired → HTTP 403. The other URL form answers with 307 to the canonical one. Free-form HTML is served with a per-request nonce CSP. |
| `GET /p/{id}/assets/{path}` · `GET /p/{id}/charts/{path}` | Same as above | Free-form assets and ECharts option JSON (allow-listed paths only) |
| `GET /_rt/v1/{name}` | none | Shared deck runtime (`deck-runtime.css`, `deck-runtime.js`, `echarts.min.js`) |
| `GET /p/{id}/status` | Basic auth or session cookie | JSON (`generation_status`, `generation_phase`, `content_version`, `render_mode`, `design_mode`, `edit_mode`, `last_edit_status`, `generation_engine_label`, `ready_at`, …), `Cache-Control: no-store, private`; never exposes `verified_changes` |
| `POST /p/{id}/auth` | Login form | Verifies PBKDF2 credentials and sets the HMAC session cookie |
| `GET /health` | none | Liveness (`/healthz` is reserved by the Google Frontend on `*.run.app`) |

## 4. Gemini Enterprise Registration & DataStore Binding

1. **DataStore Binding (`dataStoreIds`)**: When `GE_APP_ID` is provided, `infra/seed_datastore.py` (or `infra/seed_datastore.py --bind-only` when `SKIP_SEED=1`) calls `PATCH https://discoveryengine.googleapis.com/v1alpha/<engine-name>?updateMask=dataStoreIds` to attach all configured `AGENT_SEARCH_DATASTORE_ID` stores (e.g., Google Drive 1st Party DataConnector + Salesforce 1st Party DataConnector) to the Gemini Enterprise Engine without dropping previously attached stores.
2. **Agent Registration**: Register once with `agents-cli publish gemini-enterprise --gemini-enterprise-app-id=projects/<project-number>/locations/global/collections/default_collection/engines/<engine-id>` (phase 8 of `infra/deploy.sh` when `GE_APP_ID` is set). The registration points at the Agent Runtime resource, so later `agents-cli deploy` runs update the live agent without re-publishing. Each publish call creates a new registration; update the description of an existing one with `PATCH https://discoveryengine.googleapis.com/v1alpha/<agent-name>?updateMask=description,adkAgentDefinition.toolSettings.toolDescription`.

## 5. Custom Domain Options

The hosting gateway serves `https://<gateway>/p/<presentation_id>/` over managed HTTPS (`*.run.app`) and supports custom domains via Cloud Run domain mapping or Cloud Load Balancing with Google-managed certificates. Pass the public host to `infra/deploy.sh` as `HOSTING_BASE_URL` so share URLs use it.
