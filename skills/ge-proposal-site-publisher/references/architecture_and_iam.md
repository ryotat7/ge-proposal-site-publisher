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

### Subcollection: `presentations/<presentation_id>/access_logs`

Each successful viewer authentication appends a document containing:
- `accessed_at` (ISO-8601 UTC timestamp)
- `viewer_id` (`string`)
- `auth_method` (`"basic_auth"` or `"session_cookie"`)
- `ip_address` (`string`)
- `user_agent` (`string`)

## 2. Required IAM Roles

Grant the following roles to both the Compute Engine default service account (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`) and the Vertex AI Reasoning Engine service agent (`service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com`):

- `roles/storage.objectAdmin` on `gs://<PROPOSAL_GCS_BUCKET>` (Bucket-level)
- `roles/datastore.user` (Project-level, for Firestore read/write)
- `roles/discoveryengine.viewer` (Project-level, for Vertex AI Search grounding)
- `roles/aiplatform.user` (Project-level, for Gemini / Managed Agents API calls)
- `roles/serviceusage.serviceUsageConsumer` (Project-level, for quota project checks)
