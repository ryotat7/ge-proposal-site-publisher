# Interactive Proposal Concierge Agent (`proposal_agent`)

This directory contains the Google ADK agent (`root_agent` in `app/agent.py`) that powers the interactive consultation, bespoke 6-slide HTML5 presentation generation (grounded in `app/skills/interactive-slide-designer/SKILL.md`), and full post-publication lifecycle management on Google Cloud.

Design rules that must be preserved when editing this agent:

- Tools never raise: every public tool wraps `_<name>_impl` and returns a `status` payload (`GENERATING`, `NOT_FOUND`, `ERROR`, …). An exception escaping a tool aborts the ADK turn and Gemini Enterprise shows no answer.
- `create_proposal_website` must stay fast (~2 s): it issues credentials, writes `generation_status="generating"` and dispatches `app/generation_worker.py` (Cloud Run job via `GENERATION_JOB_NAME`, thread fallback). Long-running synthesis belongs in the worker.
- Synthesis tiers live in `synthesize_deck_spec_with_skill`: Managed Agents API (Interactions API, `background=True`, polled, cancelled at `MANAGED_AGENT_DEADLINE_SECONDS`) → `gemini-3.8-flash` structured output → deterministic template. Always record `generation_engine`.
- `CONCIERGE_INSTRUCTION` must not contain `{}` placeholders (ADK state interpolation) and must keep the 'present URL / ID / password in the same turn' rule.
