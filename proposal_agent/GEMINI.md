# Coding Agent Guide

## Prerequisites

Install the CLI (one-time):
```bash
uv tool install google-agents-cli
```

---

## Development Phases

### Phase 1: Understand Requirements
Before writing any code, understand the project's requirements, constraints, and success criteria.

### Phase 2: Build and Implement
Implement agent logic in `app/`. Use `agents-cli playground` for interactive testing. Iterate based on user feedback.

### Phase 3: The Evaluation Loop (Main Iteration Phase)
Start with 1-2 eval cases, run `agents-cli eval run`, iterate by making changes and rerunning it until satisfied. Expect 5-10+ iterations. Once you have a baseline, reach for `agents-cli eval compare` (regression diffs), `agents-cli eval analyze` (cluster failure modes), and `agents-cli eval optimize` (auto-tune prompts). See the **Evaluation Guide** for metrics, dataset schema, LLM-as-judge config, and common gotchas.

### Phase 4: Pre-Deployment Tests
Run `uv run pytest tests/unit tests/integration`. Fix issues until all tests pass.

### Phase 5: Deploy to Dev
**Requires explicit human approval.** Run `agents-cli deploy` only after user confirms. See the **Deployment Guide** for details.

### Phase 6: Production Deployment
Ask the user: Option A (simple single-project) or Option B (full CI/CD pipeline with `agents-cli infra cicd`).

## Development Commands

| Command | Purpose |
|---------|---------|
| `agents-cli playground` | Interactive local testing |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests |
| `agents-cli eval dataset synthesize` | Synthesize multi-turn eval scenarios for your agent |
| `agents-cli eval run` | Run the agent over the eval dataset and grade the traces |
| `agents-cli eval generate` / `agents-cli eval grade` | Decoupled form: produce traces, then grade them |
| `agents-cli eval compare` | Compare two grade-results files (regression check) |
| `agents-cli eval analyze` | Cluster failure modes from grade results |
| `agents-cli eval metric list` | List built-in metrics available in the SDK |
| `agents-cli eval optimize` | Auto-tune agent prompts using eval data |
| `agents-cli lint` | Check code quality |
| `agents-cli infra single-project` | Set up project infrastructure (Terraform) |
| `agents-cli deploy` | Deploy to dev |
| `agents-cli scaffold enhance` | Add deployment target or CI/CD to project |
| `agents-cli scaffold upgrade` | Upgrade project to latest version |

---

## Operational Guidelines for Coding Agents

- **Code preservation**: Only modify code directly targeted by the user's request. Preserve all surrounding code, config values (e.g., `model`), comments, and formatting.
- **NEVER change the model** unless explicitly asked.
- **Model 404 errors**: Fix `GOOGLE_CLOUD_LOCATION` (e.g., `global` instead of `us-central1`), not the model name.
- **ADK tool imports**: Import the tool instance, not the module: `from google.adk.tools.load_web_page import load_web_page`
- **Run Python with `uv`**: `uv run python script.py`. Run `agents-cli install` first.
- **Stop on repeated errors**: If the same error appears 3+ times, fix the root cause instead of retrying.
- **Terraform conflicts** (Error 409): Use `terraform import` instead of retrying creation.

---

## Project-specific invariants (proposal concierge)

- Tools must never raise: an exception escaping an ADK tool aborts the GE turn (tool chip with no answer). Return `status` payloads (`_tool_error`).
- `CONCIERGE_INSTRUCTION` must not contain `{` / `}` (ADK state injection raises `KeyError`).
- Deck look = `design_style` (`immersive-dark` / `clean-light` / `editorial-light`) + accent `theme_color`. Keep every Tailwind class string complete in the template (CDN JIT cannot see concatenated fragments at runtime).
- Edits: `verified_changes` comes only from `describe_deck_changes()` (deterministic diff). Never report LLM `change_summary` as applied. `NO_CHANGE` must not upload HTML.
- Edit state machine: `ready` → `updating` (`edit_queued` / `edit_designing` / `edit_rendering` / `edit_publishing`) → `ready` (`last_edit_result.status` = `applied` / `no_change` / `failed`). Every publication bumps `content_version`; the gateway watcher reloads open tabs when it increases.
- `custom_css` is edit-only and must pass `sanitize_custom_css()`; creation always clears it.
- Run tests from the repo root: `cd proposal_agent && uv run --no-sync python -m pytest ../tests/test_proposal_agent_and_gateway.py ../tests/test_freeform.py ../tests/test_deck_contract.py ../tests/test_freeform_integration.py ../tests/test_deck_renderer_svg.py ../tests/test_adk_designer.py`.

## Free-form design invariants (`design_mode=freeform`)

- The designer is an ADK `LlmAgent` (`app/adk_designer.py`, `FREEFORM_ADK_MODEL`, default `gemini-3.8-flash`) run in-process by the worker. It is off unless `FREEFORM_DESIGN_ENABLED=true`.
- `app/deck_contract.py` is the single source of truth for the output contract (`index.html` + `assets/**` + `charts/*.json` + `manifest.json`, chart attribute `data-chart`, `RUNTIME_FILES`, CSP, `describe_changes()`). `infra/deploy.sh` copies it and `app/deck_runtime/` into `hosting_gateway/` and `deck_renderer/`; never edit those copies. They are gitignored, and each service directory has its own `.gcloudignore` (without `#!include:.gitignore`) so `gcloud run deploy --source` still uploads them.
- The designer agent writes no JavaScript. The gateway serves free-form HTML with a per-request nonce CSP that allows only our runtime under `/_rt/v1/`; assets are limited to `is_servable_asset_path()`.
- Review loop: render with the private `proposal-deck-renderer`, then send the screenshots as image parts of the next user message in the SAME ADK session (`designer.start(input, session_id)`). Keep screenshots in the user message, not in tool results. Max `FREEFORM_REVIEW_ROUNDS` (2); the agent answers `REVIEW_STATUS: FIXED` or `APPROVED`. On timeout publish the best valid build (`choose_build()`); never publish a build with static errors.
- Round 2 attaches only slides whose screenshot changed plus slides still flagged (`focus_slides()`), so the agent sees its own fix; skip it when nothing visible changed and checks are clean. The renderer also audits inline/asset SVG text collisions and overflow (`_SVG_AUDIT_FN`, `_audit_svg_assets`) because screenshot review misses them.
- `freeform.make_designer()` returns `AdkDesigner`; `start(prompt, freeform.workspace_spec(run))` opens a session on the run's staging prefix and `start(prompt, session_id)` continues it. ADK tools never raise and `ADK_INSTRUCTION` must stay brace-free. Tool writes are synchronous, so the pipeline reads GCS right after a turn (no settle wait).
- Grounding check (`freeform.ungrounded_content()`): percentages that appear in no input (`GROUNDING_INPUTS`) and carry no 試算/イメージ label, unknown email addresses and work-file names are reported by `check_deck`, sent to the review turn and kept as `freeform.grounding_issues`; they never block publishing.
- Free-form edits are asynchronous (`EDIT_QUEUED`, Cloud Run Job `JOB_MODE=freeform_edit`). Report only `verified_changes` from `deck_contract.describe_changes()`; designer text is labelled 自己申告.
- Versions live under `presentations/<id>/v<N>/` and in `freeform_versions` (max 10). Every version stores `based_on` / `based_on_render_mode`; undo only switches the pointer (`_freeform_undo`), it never regenerates.
- Fallback to the template pipeline only when no free-form build is publishable; tag `generation_engine` with `+freeform_fallback:<reason>` and tell the user.
- Canonical URLs: free-form `/p/<id>/`, template `/p/<id>`; the gateway answers the other form with 307.
