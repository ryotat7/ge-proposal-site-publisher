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

Job modes (env `JOB_MODE`, default `generate`):

* generate — first generation of a presentation whose credentials were already issued.
    * template mode: queued -> knowledge_search -> gemini_fast (gemini-3.8-flash structured output)
      -> [deterministic_template] -> rendering -> ready
    * free-form mode (`FREEFORM_DESIGN_ENABLED=true`): knowledge_search -> freeform_staging
      -> freeform_drafting (the ADK designer agent writes HTML/CSS/SVG/ECharts JSON) -> freeform_images
      -> freeform_checking (private Chromium renderer) -> freeform_reviewing (the SAME agent sees the
      screenshots and fixes its files, up to FREEFORM_REVIEW_ROUNDS) -> freeform_publishing -> ready.
      If no free-form build passes the publish gate, the template engine finishes the deck and the fallback
      reason is recorded (`freeform_fallback_reason`).
* freeform_edit — applies the queued `edit_request` (mode `freeform`) to a free-form deck, or converts a
  template deck to free-form (`kind: convert`). The old version stays visible with the 「更新中」 banner until
  the new version is published; failures keep the old version and are reported truthfully.

The share URL served by the Cloud Run gateway shows a "生成中" page while `generation_status == "generating"`
and switches to the finished deck automatically once this worker flips it to `ready`.
Every publication increments `content_version` so open viewer tabs can detect the new HTML.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import time
from typing import Any

from app import agent as agent_mod
from app import deck_contract as dc
from app import freeform

logger = logging.getLogger(__name__)

_ENGINE_RANK = {
    "state_deck_spec": 3,
    "adk_freeform": 3,
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


def _template_blob_path(presentation_id: str) -> str:
    return f"presentations/{presentation_id}/index.html"


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
    """Renders + uploads the template deck and flips Firestore to `ready`. Returns the applied doc updates."""
    presentation_id = str(data.get("presentation_id") or doc_ref.id)
    bucket_name = str(data.get("gcs_bucket") or agent_mod._get_bucket_name())
    # The template deck always lives at the fixed path; free-form versions live under v<N>/.
    blob_path = _template_blob_path(presentation_id)
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
        fallback_style = agent_mod.normalize_design_style(getattr(deck_obj, "design_style", ""))
        deck_obj = agent_mod._default_deck_spec_from_brief(
            client_name=deck_obj.client_name,
            proposal_title=deck_obj.proposal_title,
            proposal_brief=deck_obj.subtitle,
            theme_color=deck_obj.theme_color,
        )
        deck_obj.design_style = fallback_style
        deck_obj.custom_css = ""
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
        "design_style": agent_mod.normalize_design_style(deck_obj.design_style),
        "deck_spec": deck_obj.model_dump(),
        "render_mode": "template",
        "content_version": agent_mod._content_version_of(data) + 1,
        "gcs_bucket": bucket_name,
        "gcs_blob_path": blob_path,
        "gcs_uri": f"gs://{bucket_name}/{blob_path}",
        "generation_status": "ready",
        "generation_phase": "ready",
        "generation_detail": "",
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


def _inputs_of(data: dict[str, Any]) -> dict[str, Any]:
    inputs = data.get("generation_inputs") or {}
    client_name = str(inputs.get("client_name") or data.get("client_name") or "クライアント企業")
    proposal_title = str(
        inputs.get("proposal_title")
        or data.get("proposal_title")
        or f"{client_name}様向け AI×UX変革ご提案プレゼンテーション"
    )
    return {
        "client_name": client_name,
        "proposal_title": proposal_title,
        "proposal_brief": str(inputs.get("proposal_brief") or data.get("subtitle") or proposal_title),
        "theme_color": str(inputs.get("theme_color") or data.get("theme_color") or "sky"),
        "design_style": agent_mod.normalize_design_style(inputs.get("design_style") or data.get("design_style")),
        "ui_format": agent_mod.normalize_ui_format(inputs.get("ui_format") or data.get("ui_format")),
        "outline_hint": str(inputs.get("outline_hint") or ""),
        "design_request": str(inputs.get("design_request") or ""),
        "design_mode": str(inputs.get("design_mode") or data.get("design_mode") or "template"),
    }


def _knowledge_for(inputs: dict[str, Any]) -> str:
    try:
        return agent_mod.search_internal_knowledge(
            f"{inputs['client_name']} {inputs['proposal_title']} {inputs['proposal_brief']}"[:500]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge search failed, continuing without context: %s", exc)
        return ""


def generate_presentation(presentation_id: str) -> dict[str, Any]:
    """Generates the deck for an already-issued presentation and publishes it (never raises on LLM failure)."""
    started = time.monotonic()
    doc_ref, data = _load_doc(presentation_id)

    current_status = str(data.get("generation_status") or "").lower()
    current_engine = str(data.get("generation_engine") or "")
    already_freeform = data.get("render_mode") == "freeform" and bool(data.get("freeform_prefix"))
    if current_status == "ready" and (already_freeform or (data.get("deck_spec") and _engine_rank(current_engine) >= 2)):
        logger.info("Presentation %s already ready via %s; skipping", presentation_id, current_engine)
        return {
            "status": "ALREADY_READY",
            "presentation_id": presentation_id,
            "generation_status": "ready",
            "generation_engine": current_engine,
        }

    inputs = _inputs_of(data)

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
    knowledge_json = _knowledge_for(inputs)

    if inputs["design_mode"] == "freeform":
        if freeform.freeform_enabled():
            return _generate_freeform(doc_ref, data, inputs, knowledge_json, status_cb, started)
        logger.info("FREEFORM_DESIGN_ENABLED is off; %s falls back to the template engine", presentation_id)
        return _generate_template(
            doc_ref, data, inputs, knowledge_json, status_cb, started, fallback_reason="freeform_disabled"
        )
    return _generate_template(doc_ref, data, inputs, knowledge_json, status_cb, started)


def _generate_template(
    doc_ref: Any,
    data: dict[str, Any],
    inputs: dict[str, Any],
    knowledge_json: str,
    status_cb: Any,
    started: float,
    fallback_reason: str = "",
    extra_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    presentation_id = str(data.get("presentation_id") or doc_ref.id)
    try:
        deck_obj, engine = agent_mod.synthesize_deck_spec_with_skill(
            client_name=inputs["client_name"],
            proposal_title=inputs["proposal_title"],
            proposal_brief=inputs["proposal_brief"],
            theme_color=inputs["theme_color"],
            knowledge_context=knowledge_json,
            outline_hint=inputs["outline_hint"],
            status_callback=status_cb,
            design_style=inputs["design_style"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("All synthesis tiers raised unexpectedly; using template: %s", exc)
        deck_obj = agent_mod._default_deck_spec_from_brief(
            client_name=inputs["client_name"],
            proposal_title=inputs["proposal_title"],
            proposal_brief=inputs["proposal_brief"],
            theme_color=inputs["theme_color"],
        )
        engine = "deterministic_skill_template:unexpected_error"

    # The look requested at creation always wins; free-form custom CSS is reserved for explicit edits.
    deck_obj.design_style = inputs["design_style"]
    deck_obj.ui_format = inputs.get("ui_format") or agent_mod.DEFAULT_UI_FORMAT
    deck_obj.custom_css = ""
    extra = dict(extra_updates or {})
    if fallback_reason:
        engine = f"{engine}+freeform_fallback:{fallback_reason[:40]}"
        extra.setdefault("freeform_fallback_reason", fallback_reason)

    try:
        updates = _finalize(doc_ref, data, deck_obj, engine, started, extra_updates=extra or None)
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


def _freeform_input_files(inputs: dict[str, Any], knowledge_json: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    files = {
        "brief.md": freeform.build_brief_md(inputs),
        "knowledge.md": freeform.build_knowledge_md(knowledge_json),
        "DESIGN_RULES.md": freeform.load_design_rules(),
    }
    files.update(extra or {})
    return files


def _fallback_meta(run: freeform.Run, outcome: freeform.Outcome | None, error: str) -> dict[str, Any]:
    return {
        "attempted": True,
        "error": error[:500],
        "session_id": outcome.session_id if outcome else "",
        "turn_ids": outcome.turn_ids if outcome else [],
        "review_rounds": outcome.rounds if outcome else [],
        "warnings": (outcome.warnings if outcome else [])[:12],
        "usage": dict(run.usage),
        "events": run.events[-40:],
        "run_id": run.run_id,
        "elapsed_seconds": round(run.elapsed(), 1),
        "model": freeform.designer_model(),
    }


def _generate_freeform(
    doc_ref: Any,
    data: dict[str, Any],
    inputs: dict[str, Any],
    knowledge_json: str,
    status_cb: Any,
    started: float,
) -> dict[str, Any]:
    presentation_id = str(data.get("presentation_id") or doc_ref.id)
    project_id = agent_mod._get_project_id()
    bucket_name = str(data.get("gcs_bucket") or agent_mod._get_bucket_name())
    run = freeform.new_run(presentation_id, project_id, bucket_name, status_cb)
    outcome: freeform.Outcome | None = None
    try:
        store = freeform.Store(project_id, bucket_name)
        designer = freeform.make_designer(project_id)
        outcome = freeform.run_pipeline(
            run,
            store,
            designer,
            mode="create",
            input_files=_freeform_input_files(inputs, knowledge_json),
            title=inputs["proposal_title"],
        )
        if outcome.ok and outcome.chosen is not None:
            updates = freeform.publish_outcome(run, store, data, outcome)
            engine = freeform.engine_name()
            now = _now_iso()
            updates.update(
                {
                    "client_name": inputs["client_name"],
                    "proposal_title": inputs["proposal_title"],
                    "ui_format": inputs.get("ui_format") or agent_mod.DEFAULT_UI_FORMAT,
                    "generation_status": "ready",
                    "generation_phase": "ready",
                    "generation_detail": "",
                    "generation_engine": engine,
                    "generation_engine_label": agent_mod.describe_generation_engine(engine),
                    "generation_elapsed_seconds": round(time.monotonic() - started, 1),
                    "generation_error": "",
                    "freeform_fallback_reason": "",
                    "ready_at": now,
                    "updated_at": now,
                }
            )
            doc_ref.update(updates)
            logger.info(
                "Presentation %s ready via %s in %.1fs (review rounds: %d)",
                presentation_id,
                engine,
                updates["generation_elapsed_seconds"],
                len(outcome.rounds),
            )
            return {
                "status": "READY",
                "presentation_id": presentation_id,
                "generation_status": "ready",
                "generation_engine": engine,
                "generation_elapsed_seconds": updates["generation_elapsed_seconds"],
                "review_rounds": len(outcome.rounds),
                "doc_updates": updates,
            }
        error = outcome.error or "no publishable free-form build"
    except Exception as exc:  # noqa: BLE001 - anything unexpected -> template fallback
        logger.exception("Free-form generation crashed for %s", presentation_id)
        error = f"{type(exc).__name__}: {str(exc)[:300]}"

    status_cb("freeform_fallback", "自由デザインを公開できなかったため、テンプレートで仕上げています")
    reason = "no_index" if "index.html" in error else ("unpublishable" if "公開できる版" in error else "error")
    return _generate_template(
        doc_ref,
        data,
        inputs,
        knowledge_json,
        status_cb,
        started,
        fallback_reason=reason,
        extra_updates={
            "design_mode": "freeform",
            "freeform_fallback_reason": error[:500],
            "freeform": _fallback_meta(run, outcome, error),
        },
    )


# ---------------------------------------------------------------------------
# Free-form edits (and template -> free-form conversion)
# ---------------------------------------------------------------------------
def _edit_request_md(request: dict[str, Any]) -> str:
    lines = ["# 修正依頼", "", str(request.get("instructions") or "").strip() or "（自然文の指示なし）", ""]
    explicit = request.get("explicit_changes") if isinstance(request.get("explicit_changes"), dict) else {}
    labels = {
        "proposal_title": "提案タイトルを次の文言にする",
        "subtitle": "サブタイトルを次の文言にする",
        "theme_color": "アクセントカラーを次の系統にする",
        "custom_callout": "表紙に次の強調ラベルを入れる",
        "design_style": "全体の見た目を次のスタイルにする（immersive-dark=濃紺ダーク、clean-light=白基調、editorial-light=生成り色・明朝見出し）",
        "ui_format": "UI形式（レイアウト）を次の形式にする（portal=4カラムWeb提案ポータル形式 <main id=\"ny-deck\" data-ny-layout=\"portal\">、slides=16:9プレゼンスライド形式 <main id=\"ny-deck\" data-ny-layout=\"slides\">）",
    }
    if explicit:
        lines.append("## 明示的な指定（必ず反映）")
        for key, value in explicit.items():
            lines.append(f"- {labels.get(key, key)}: {value}")
        lines.append("")
    lines.append("## 注意")
    lines.append("- 依頼にない部分は変えない（全面的なデザイン変更の依頼なら作り直してよい）。")
    lines.append("- 数値やメールアドレスは、この依頼文・brief.md・公開中の版にあるものだけを使う。")
    return "\n".join(lines) + "\n"


def _template_deck_text(data: dict[str, Any]) -> str:
    spec = data.get("deck_spec")
    if not isinstance(spec, dict):
        return ""
    try:
        text = json.dumps(spec, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        return ""
    return text[:12000]


def _edit_failed(doc_ref: Any, data: dict[str, Any], request: dict[str, Any], error: str, started: float, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    now = _now_iso()
    last_edit = {
        "status": "failed",
        "request_id": str(request.get("request_id") or ""),
        "edit_engine": freeform.engine_name(),
        "edit_engine_label": agent_mod.describe_edit_engine(freeform.engine_name()),
        "verified_changes": [],
        "designer_notes": [],
        "unsupported_requests": [],
        "error": error[:400],
        "content_version": agent_mod._content_version_of(data),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "applied_at": now,
        **(extra or {}),
    }
    _safe_update(
        doc_ref,
        {
            "generation_status": "ready",
            "generation_phase": "ready",
            "generation_detail": "",
            "last_edit_result": last_edit,
            "edit_request": None,
            "updated_at": now,
        },
    )
    return {"status": "EDIT_FAILED", "presentation_id": str(data.get("presentation_id") or doc_ref.id), "error": error[:400]}


def apply_freeform_edit(presentation_id: str) -> dict[str, Any]:
    """Applies the queued free-form edit (or template -> free-form conversion). Never raises."""
    started = time.monotonic()
    doc_ref, data = _load_doc(presentation_id)
    request = data.get("edit_request") if isinstance(data.get("edit_request"), dict) else {}
    if not request or request.get("mode") != "freeform":
        return {"status": "NO_REQUEST", "presentation_id": presentation_id}
    last = data.get("last_edit_result") if isinstance(data.get("last_edit_result"), dict) else {}
    if last.get("request_id") and last.get("request_id") == request.get("request_id"):
        return {"status": "ALREADY_APPLIED", "presentation_id": presentation_id}

    kind = str(request.get("kind") or "edit")
    project_id = agent_mod._get_project_id()
    bucket_name = str(data.get("gcs_bucket") or agent_mod._get_bucket_name())

    def status_cb(phase: str, detail: str) -> None:
        _safe_update(
            doc_ref,
            {
                "generation_status": "updating",
                "generation_phase": phase,
                "generation_detail": detail[:300],
                "updated_at": _now_iso(),
            },
        )

    if not freeform.freeform_enabled():
        return _edit_failed(doc_ref, data, request, "自由デザイン機能が無効です（FREEFORM_DESIGN_ENABLED）", started)

    run = freeform.new_run(presentation_id, project_id, bucket_name, status_cb)
    try:
        store = freeform.Store(project_id, bucket_name)
        designer = freeform.make_designer(project_id)
        inputs = _inputs_of(data)
        old_version = int((data.get("freeform") or {}).get("current_version") or 0) if data.get("render_mode") == "freeform" else 0
        if kind == "convert":
            inputs = {
                **inputs,
                "design_request": str(request.get("instructions") or inputs.get("design_request") or ""),
                "source_deck_text": _template_deck_text(data),
            }
            outcome = freeform.run_pipeline(
                run,
                store,
                designer,
                mode="create",
                input_files=_freeform_input_files(inputs, ""),
                title=str(data.get("proposal_title") or inputs["proposal_title"]),
            )
            published: dict[str, bytes] = {}
        else:
            published, seed = freeform.seed_from_published(store, data)
            if "index.html" not in seed:
                return _edit_failed(doc_ref, data, request, "公開中の自由デザイン版を読み込めませんでした", started)
            outcome = freeform.run_pipeline(
                run,
                store,
                designer,
                mode="edit",
                input_files={
                    "edit_request.md": _edit_request_md(request),
                    "brief.md": freeform.build_brief_md(inputs),
                    "DESIGN_RULES.md": freeform.load_design_rules(),
                },
                seed_files=seed,
                title=str(data.get("proposal_title") or inputs["proposal_title"]),
            )
        review_meta = {
            "review_rounds": len(outcome.rounds),
            "turn_ids": outcome.turn_ids,
            "warnings": outcome.warnings[:8],
        }
        if not outcome.ok or outcome.chosen is None:
            return _edit_failed(doc_ref, data, request, outcome.error or "公開できる版がありませんでした", started, review_meta)

        if kind == "convert":
            titles = [s.title for s in outcome.chosen.build.slides]
            verified = [
                f"テンプレート版を自由デザイン版に作り直し（{len(titles)} 枚）",
                "スライド構成: " + " / ".join(titles[:12]),
            ]
        else:
            verified = dc.describe_changes(published, outcome.chosen.build.files)
        if not verified:
            now = _now_iso()
            _safe_update(
                doc_ref,
                {
                    "generation_status": "ready",
                    "generation_phase": "ready",
                    "generation_detail": "",
                    "last_edit_result": {
                        "status": "no_change",
                        "request_id": str(request.get("request_id") or ""),
                        "edit_engine": freeform.engine_name(),
                        "edit_engine_label": agent_mod.describe_edit_engine(freeform.engine_name()),
                        "verified_changes": [],
                        "designer_notes": [s for s in [outcome.draft_summary, *outcome.review_summaries] if s][:4],
                        "unsupported_requests": [],
                        "content_version": agent_mod._content_version_of(data),
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                        "applied_at": now,
                        **review_meta,
                    },
                    "edit_request": None,
                    "updated_at": now,
                },
            )
            return {"status": "NO_CHANGE", "presentation_id": presentation_id}

        updates = freeform.publish_outcome(run, store, data, outcome)
        explicit_req = request.get("explicit_changes") if isinstance(request.get("explicit_changes"), dict) else {}
        req_ui_fmt = str(explicit_req.get("ui_format") or "").strip() or agent_mod._detect_requested_ui_format(str(request.get("instructions") or ""))
        if req_ui_fmt:
            updates["ui_format"] = agent_mod.normalize_ui_format(req_ui_fmt)
        now = _now_iso()
        engine = freeform.engine_name()
        updates.update(
            {
                "generation_status": "ready",
                "generation_phase": "ready",
                "generation_detail": "",
                "edit_request": None,
                "last_edit_instructions": str(request.get("instructions") or "")[:2000],
                "last_edited_at": now,
                "last_edit_result": {
                    "status": "applied",
                    "request_id": str(request.get("request_id") or ""),
                    "kind": kind,
                    "edit_engine": engine,
                    "edit_engine_label": agent_mod.describe_edit_engine(engine),
                    "verified_changes": verified,
                    "changed_fields": [],
                    # Agent's own summary: shown separately from the machine-verified change list.
                    "designer_notes": [s for s in [outcome.draft_summary, *outcome.review_summaries] if s][:4],
                    "unsupported_requests": [],
                    "previous_render_mode": str(data.get("render_mode") or "template"),
                    "previous_freeform_version": old_version,
                    "freeform_version": updates["freeform"]["current_version"],
                    "content_version": updates["content_version"],
                    "design_style": "",
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "applied_at": now,
                    **review_meta,
                },
            }
        )
        doc_ref.update(updates)
        return {
            "status": "UPDATED",
            "presentation_id": presentation_id,
            "verified_changes": verified,
            "freeform_version": updates["freeform"]["current_version"],
            "review_rounds": len(outcome.rounds),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Free-form edit crashed for %s", presentation_id)
        return _edit_failed(doc_ref, data, request, f"{type(exc).__name__}: {str(exc)[:300]}", started)


def finalize_with_fallback(presentation_id: str, reason: str = "manual") -> dict[str, Any]:
    """Completes a stuck generation immediately with the deterministic skill template."""
    started = time.monotonic()
    doc_ref, data = _load_doc(presentation_id)
    if str(data.get("generation_status") or "").lower() == "ready" and (
        data.get("deck_spec") or data.get("render_mode") == "freeform"
    ):
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
    deck_obj.design_style = agent_mod.normalize_design_style(
        inputs.get("design_style") or data.get("design_style")
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


def run_job(presentation_id: str, job_mode: str = "generate") -> dict[str, Any]:
    """Dispatches one job execution (also used by the inline-thread fallback)."""
    if (job_mode or "generate").strip().lower() == "freeform_edit":
        return apply_freeform_edit(presentation_id)
    return generate_presentation(presentation_id)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = list(argv if argv is not None else sys.argv[1:])
    presentation_id = (args[0] if args else os.environ.get("PRESENTATION_ID", "")).strip()
    job_mode = (args[1] if len(args) > 1 else os.environ.get("JOB_MODE", "generate")).strip().lower()
    if not presentation_id:
        print("PRESENTATION_ID env var (or argv[1]) is required", file=sys.stderr)
        return 2
    result = run_job(presentation_id, job_mode)
    print({k: v for k, v in result.items() if k != "doc_updates"})
    # Exit 0 whenever the request reached a terminal, truthful state (Cloud Run retries non-zero exits).
    if job_mode == "freeform_edit":
        return 0 if result.get("status") in ("UPDATED", "NO_CHANGE", "EDIT_FAILED", "ALREADY_APPLIED", "NO_REQUEST") else 1
    return 0 if result.get("generation_status") == "ready" or result.get("status") == "ALREADY_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
