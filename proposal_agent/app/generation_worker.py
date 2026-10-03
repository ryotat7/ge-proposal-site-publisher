# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Background deck generation worker (Cloud Run Job entrypoint / inline fallback).

Flow per presentation (idempotent, state kept in Firestore `generation_status`):
  queued -> knowledge_search -> managed_agents (<= MANAGED_AGENT_DEADLINE_SECONDS)
         -> [gemini_fast] -> [deterministic_template] -> rendering -> ready
The share URL served by the Cloud Run gateway shows a "生成中" page while `generation_status == "generating"`
and switches to the finished deck automatically once this worker flips it to `ready`.
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
import time
from typing import Any

from app import agent as agent_mod

logger = logging.getLogger(__name__)

_ENGINE_RANK = {
    "state_deck_spec": 3,
    "managed_agents_api": 3,
    "agent_platform_gemini_with_skill": 2,
    "deterministic_skill_template": 1,
}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _engine_rank(engine: str) -> int:
    eng = (engine or "").lower()
    for prefix, rank in _ENGINE_RANK.items():
        if eng.startswith(prefix):
            return rank
    return 0


def _upload_html(bucket_name: str, blob_path: str, html_content: str) -> None:
    from google.cloud import storage

    storage_client = storage.Client(project=agent_mod._get_project_id())
    blob = storage_client.bucket(bucket_name).blob(blob_path)
    blob.cache_control = "no-store, private"
    blob.upload_from_string(
        html_content.encode("utf-8"), content_type="text/html; charset=utf-8"
    )


def _finalize(
    doc_ref: Any,
    data: dict[str, Any],
    deck_obj: agent_mod.PresentationDeckSpec,
    engine: str,
    started_monotonic: float,
    extra_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Renders + uploads the deck and flips Firestore to `ready`. Returns the applied doc updates."""
    presentation_id = str(data.get("presentation_id") or doc_ref.id)
    bucket_name = str(data.get("gcs_bucket") or agent_mod._get_bucket_name())
    blob_path = str(
        data.get("gcs_blob_path") or f"presentations/{presentation_id}/index.html"
    )
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_jst = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))

    _safe_update(doc_ref, {"generation_phase": "rendering", "updated_at": _now_iso()})
    try:
        html_content = agent_mod.render_deck_html(
            deck_obj, generated_date=now_jst.strftime("%Y-%m-%d %H:%M JST")
        )
    except Exception as render_exc:  # noqa: BLE001
        # An LLM deck that renders invalid DOM must never block publication.
        logger.warning(
            "Rendering %s deck from %s failed (%s); using deterministic template",
            presentation_id,
            engine,
            render_exc,
        )
        deck_obj = agent_mod._default_deck_spec_from_brief(
            client_name=deck_obj.client_name,
            proposal_title=deck_obj.proposal_title,
            proposal_brief=deck_obj.subtitle,
            theme_color=deck_obj.theme_color,
        )
        engine = f"deterministic_skill_template:render_fallback_from:{engine}"
        html_content = agent_mod.render_deck_html(
            deck_obj, generated_date=now_jst.strftime("%Y-%m-%d %H:%M JST")
        )

    _upload_html(bucket_name, blob_path, html_content)

    updates: dict[str, Any] = {
        "client_name": deck_obj.client_name,
        "proposal_title": deck_obj.proposal_title,
        "subtitle": deck_obj.subtitle,
        "theme_color": deck_obj.theme_color,
        "deck_spec": deck_obj.model_dump(),
        "gcs_bucket": bucket_name,
        "gcs_blob_path": blob_path,
        "gcs_uri": f"gs://{bucket_name}/{blob_path}",
        "generation_status": "ready",
        "generation_phase": "ready",
        "generation_engine": engine,
        "generation_engine_label": agent_mod.describe_generation_engine(engine),
        "generation_elapsed_seconds": round(time.monotonic() - started_monotonic, 1),
        "generation_error": "",
        "ready_at": now_utc.isoformat(),
        "updated_at": now_utc.isoformat(),
    }
    if extra_updates:
        updates.update(extra_updates)
    doc_ref.update(updates)
    logger.info(
        "Presentation %s ready via %s in %.1fs",
        presentation_id,
        engine,
        updates["generation_elapsed_seconds"],
    )
    return updates


def _safe_update(doc_ref: Any, updates: dict[str, Any]) -> None:
    try:
        doc_ref.update(updates)
    except Exception as exc:  # noqa: BLE001
        logger.info("Firestore progress update skipped: %s", exc)


def _load_doc(presentation_id: str) -> tuple[Any, dict[str, Any]]:
    project_id = agent_mod._get_project_id()
    fs_client = agent_mod._get_firestore_client(project_id)
    doc_ref = fs_client.collection(agent_mod._get_firestore_collection()).document(
        presentation_id
    )
    snap = doc_ref.get()
    if not snap.exists:
        raise ValueError(f"Presentation '{presentation_id}' not found in Firestore.")
    return doc_ref, (snap.to_dict() or {})


def generate_presentation(
    presentation_id: str,
    managed_agent_deadline_seconds: int | None = None,
) -> dict[str, Any]:
    """Generates the deck for an already-issued presentation and publishes it (never raises on LLM failure)."""
    started = time.monotonic()
    doc_ref, data = _load_doc(presentation_id)

    current_status = str(data.get("generation_status") or "").lower()
    current_engine = str(data.get("generation_engine") or "")
    if current_status == "ready" and data.get("deck_spec") and _engine_rank(current_engine) >= 2:
        logger.info("Presentation %s already ready via %s; skipping", presentation_id, current_engine)
        return {
            "status": "ALREADY_READY",
            "presentation_id": presentation_id,
            "generation_status": "ready",
            "generation_engine": current_engine,
        }

    inputs = data.get("generation_inputs") or {}
    client_name = str(inputs.get("client_name") or data.get("client_name") or "クライアント企業")
    proposal_title = str(
        inputs.get("proposal_title")
        or data.get("proposal_title")
        or f"{client_name}様向け AI×UX変革ご提案プレゼンテーション"
    )
    proposal_brief = str(inputs.get("proposal_brief") or data.get("subtitle") or proposal_title)
    theme = str(inputs.get("theme_color") or data.get("theme_color") or "sky")
    outline_hint = str(inputs.get("outline_hint") or "")

    def status_cb(phase: str, detail: str) -> None:
        _safe_update(
            doc_ref,
            {
                "generation_status": "generating",
                "generation_phase": phase,
                "generation_detail": detail[:300],
                "updated_at": _now_iso(),
            },
        )

    _safe_update(
        doc_ref,
        {
            "generation_status": "generating",
            "generation_phase": "knowledge_search",
            "generation_started_at": _now_iso(),
            "generation_worker": os.environ.get("CLOUD_RUN_EXECUTION", "inline"),
            "updated_at": _now_iso(),
        },
    )

    try:
        knowledge_json = agent_mod.search_internal_knowledge(
            f"{client_name} {proposal_title} {proposal_brief}"[:500]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge search failed, continuing without context: %s", exc)
        knowledge_json = ""

    try:
        deck_obj, engine = agent_mod.synthesize_deck_spec_with_skill(
            client_name=client_name,
            proposal_title=proposal_title,
            proposal_brief=proposal_brief,
            theme_color=theme,
            knowledge_context=knowledge_json,
            outline_hint=outline_hint,
            managed_agent_deadline_seconds=managed_agent_deadline_seconds,
            status_callback=status_cb,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("All synthesis tiers raised unexpectedly; using template: %s", exc)
        deck_obj = agent_mod._default_deck_spec_from_brief(
            client_name=client_name,
            proposal_title=proposal_title,
            proposal_brief=proposal_brief,
            theme_color=theme,
        )
        engine = "deterministic_skill_template:unexpected_error"

    try:
        updates = _finalize(doc_ref, data, deck_obj, engine, started)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Finalize failed for %s: %s", presentation_id, exc)
        _safe_update(
            doc_ref,
            {
                "generation_status": "failed",
                "generation_phase": "failed",
                "generation_error": str(exc)[:500],
                "updated_at": _now_iso(),
            },
        )
        return {
            "status": "FAILED",
            "presentation_id": presentation_id,
            "generation_status": "failed",
            "generation_engine": engine,
            "error": str(exc)[:500],
        }

    return {
        "status": "READY",
        "presentation_id": presentation_id,
        "generation_status": "ready",
        "generation_engine": updates["generation_engine"],
        "generation_elapsed_seconds": updates["generation_elapsed_seconds"],
        "doc_updates": updates,
    }


def finalize_with_fallback(presentation_id: str, reason: str = "manual") -> dict[str, Any]:
    """Completes a stuck generation immediately with the deterministic skill template."""
    started = time.monotonic()
    doc_ref, data = _load_doc(presentation_id)
    if str(data.get("generation_status") or "").lower() == "ready" and data.get("deck_spec"):
        return {
            "status": "ALREADY_READY",
            "presentation_id": presentation_id,
            "generation_status": "ready",
            "generation_engine": data.get("generation_engine", ""),
            "doc_updates": {},
        }
    inputs = data.get("generation_inputs") or {}
    deck_obj = agent_mod._default_deck_spec_from_brief(
        client_name=str(inputs.get("client_name") or data.get("client_name") or "クライアント企業"),
        proposal_title=str(inputs.get("proposal_title") or data.get("proposal_title") or "ご提案"),
        proposal_brief=str(inputs.get("proposal_brief") or data.get("subtitle") or ""),
        theme_color=str(inputs.get("theme_color") or data.get("theme_color") or "sky"),
    )
    updates = _finalize(
        doc_ref,
        data,
        deck_obj,
        f"deterministic_skill_template:{reason}",
        started,
        extra_updates={"generation_repaired_at": _now_iso(), "generation_repair_reason": reason},
    )
    return {
        "status": "READY",
        "presentation_id": presentation_id,
        "generation_status": "ready",
        "generation_engine": updates["generation_engine"],
        "doc_updates": updates,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = list(argv if argv is not None else sys.argv[1:])
    presentation_id = (args[0] if args else os.environ.get("PRESENTATION_ID", "")).strip()
    if not presentation_id:
        print("PRESENTATION_ID env var (or argv[1]) is required", file=sys.stderr)
        return 2
    result = generate_presentation(presentation_id)
    print(result)
    return 0 if result.get("generation_status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
