# Proposal Site Publisher Agent (`proposal_agent`)

Google ADK interactive concierge `LlmAgent` for consulting on client proposals, issuing a private proposal-site URL with viewer credentials immediately, generating the bespoke 6-slide interactive HTML5 website in the background (`app/generation_worker.py` as a Cloud Run job: Managed Agents API → `gemini-3.8-flash` → deterministic template), publishing to private Cloud Storage + Firestore, and managing the full post-publication lifecycle.

```bash
# unit tests
uv run --no-sync pytest ../tests/test_proposal_agent_and_gateway.py -q
# run the background worker locally for one presentation
PRESENTATION_ID=prop-20261003-xxxxxxxx uv run --no-sync python -m app.generation_worker
```
