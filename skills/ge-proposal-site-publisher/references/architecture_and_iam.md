# Architecture, Firestore Schema & IAM Reference

## 1. Firestore Data Model (`presentations` Collection)

Each published presentation is stored at `presentations/<presentation_id>`:

| Field | Type | Description |
| :--- | :--- | :--- |
| `presentation_id` | `string` | Unique identifier (`prop-YYYYMMDD-<8hex>`) |
| `client_name` | `string` | Target client organization name |
| `proposal_title` | `string` | Presentation main title |
| `subtitle` | `string` | Executive subtitle / value proposition |
| `theme_color` | `string` | Accent palette (`sky`, `emerald`, `violet`, `amber`, `rose`) |
| `viewer_id` | `string` | Generated login ID for external client viewers |
| `password_hash` | `string` | Hex-encoded PBKDF2-HMAC-SHA256 digest (120,000 iterations) |
| `password_salt` | `string` | Hex-encoded 16-byte random salt |
| `gcs_bucket` | `string` | Private Cloud Storage bucket name |
| `gcs_blob_path` | `string` | `presentations/<presentation_id>/index.html` |
| `deck_spec` | `map` | Full serialized `PresentationDeckSpec` for live re-editing |
| `status` | `string` | `"active"` or `"revoked"` |
| `is_active` | `boolean` | `true` when active, `false` when revoked |
| `created_at` | `string` | ISO-8601 UTC timestamp |
| `updated_at` | `string` | ISO-8601 UTC timestamp |
| `expires_at` | `string` | ISO-8601 UTC expiration timestamp |
| `generation_status` | `string` | `"generating"` → `"ready"` (or `"failed"`) — drives the gateway's 'generating' page |
| `generation_phase` | `string` | `queued` / `knowledge_search` / `managed_agents` / `gemini_fast` / `deterministic` / `rendering` / `ready` |
| `generation_detail` | `string` | Free-text progress detail (e.g. interaction id, elapsed seconds) |
| `generation_engine` | `string` | Engine that produced the deck: `managed_agents_api:<model>`, `agent_platform_gemini_with_skill:<model>`, `deterministic_skill_template[:reason]`, `state_deck_spec` |
| `generation_engine_label` | `string` | Human-readable Japanese label of `generation_engine` |
| `generation_requested_at` / `ready_at` | `string` | ISO-8601 UTC timestamps of the request and completion |
| `generation_elapsed_seconds` | `number` | Wall-clock generation time |
| `generation_dispatch` | `string` | `cloud_run_job` / `inline_thread` / `sync` — how the background work was started |
| `generation_execution` | `string` | Cloud Run job execution name (when dispatched to the job) |
| `generation_inputs` | `map` | `client_name`, `proposal_title`, `proposal_brief`, `theme_color`, `outline_hint`, `expiration_days` consumed by the worker |
| `generation_error` | `string` | Last error message when `generation_status="failed"` |

### Subcollection: `presentations/<presentation_id>/access_logs`

Each successful viewer authentication appends a document containing:
- `accessed_at` (ISO-8601 UTC timestamp)
- `viewer_id` (`string`)
- `auth_method` (`"basic_auth"` or `"session_cookie"`)
- `ip_address` (`string`)
- `user_agent` (`string`)

## 2. Required IAM Roles

Grant the following roles to both the Compute Engine default service account (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`) and the Agent Runtime Reasoning Engine service agent (`service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com`):

- `roles/storage.objectAdmin` on `gs://<PROPOSAL_GCS_BUCKET>` (Bucket-level)
- `roles/datastore.user` (Project-level, for Firestore read/write)
- `roles/discoveryengine.viewer` (Project-level, for Agent Search grounding)
- `roles/aiplatform.user` (Project-level, for Agent Platform Gemini / Managed Agents API calls)
- `roles/serviceusage.serviceUsageConsumer` (Project-level, for quota project checks)
- `roles/run.developer` (Project-level, for `run.jobs.run` / `run.jobs.runWithOverrides` — the agent triggers the generation job with a per-execution `PRESENTATION_ID` override; `roles/run.invoker` alone is **not** sufficient)
- `roles/iam.serviceAccountUser` on the Compute Engine default service account, granted to the Reasoning Engine service agent (the job runs as the compute SA, so the trigger needs `actAs`)

## 2a. Workloads & Environment Variables

| Workload | Entry point | Key environment variables |
| :--- | :--- | :--- |
| Agent Runtime (Reasoning Engine) | `app/agent.py` (`root_agent`) | `GEMINI_MODEL`, `MANAGED_AGENT_MODEL`, `MANAGED_AGENT_DEADLINE_SECONDS`, `GENERATION_JOB_NAME` (`projects/<p>/locations/<r>/jobs/<job>`), `GENERATION_TRIGGER_MODE` (`auto` \| `cloud_run_job` \| `inline_thread` \| `sync` \| `none`), `GENERATION_STALE_MINUTES` (default 13), `HOSTING_BASE_URL`, `PROPOSAL_GCS_BUCKET`, `PROPOSAL_FIRESTORE_COLLECTION`, `AGENT_SEARCH_DATASTORE_ID` |
| Cloud Run job (`proposal-deck-generator`) | `uv run --no-sync python -m app.generation_worker` (reads `PRESENTATION_ID`) | Same model / storage variables as the agent; `--task-timeout=1500s`, `--max-retries=1`, runs as the compute SA |
| Cloud Run service (hosting gateway) | `hosting_gateway/main.py` | `PROPOSAL_GCS_BUCKET`, `PROPOSAL_FIRESTORE_COLLECTION`, `GATEWAY_SESSION_SECRET`, `PROPOSAL_COOKIE_PREFIX`, `PROPOSAL_BRAND_NAME`, `PROPOSAL_BRAND_BADGE` |

### Gateway endpoints

| Endpoint | Auth | Behaviour |
| :--- | :--- | :--- |
| `GET /p/{id}` | Basic auth or session cookie | `generating` → branded interim page (HTTP 200, polls `/status`); `failed` → HTTP 503; `ready` → streams `presentations/{id}/index.html` from private GCS; revoked/expired → HTTP 403 |
| `GET /p/{id}/status` | Basic auth or session cookie | JSON (`generation_status`, `generation_phase`, `generation_detail`, `generation_engine`, `generation_engine_label`, `ready_at`, …), `Cache-Control: no-store, private` |
| `POST /p/{id}/auth` | Login form | Verifies PBKDF2 credentials and sets the HMAC session cookie |
| `GET /health` | none | Liveness (`/healthz` is reserved by the Google Frontend on `*.run.app`) |

## 3. Custom Domain Options

The Cloud Run Hosting Gateway serves `https://<gateway>/p/<presentation_id>` directly over managed HTTPS (`*.run.app`) and supports custom domain mapping via Cloud Run Custom Domain Mapping or Cloud Load Balancing with Google-managed SSL certificates.

