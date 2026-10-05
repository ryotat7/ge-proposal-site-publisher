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

"""End-to-end (offline) tests for free-form design: tools -> worker -> Firestore/GCS -> hosting gateway.

The free-form pipeline itself is covered by test_freeform.py and test_adk_designer.py. Here `freeform.run_pipeline` is replaced by a
scripted fake so the tests pin what happens around it: design-mode routing, the template fallback, queued edits,
truthful edit reports, versioning/undo, and how the gateway serves free-form decks (canonical URL, CSP, assets).
"""

from __future__ import annotations

import base64
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "proposal_agent"))
sys.path.insert(0, str(ROOT_DIR / "hosting_gateway"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import agent as agent_mod  # noqa: E402
from app import deck_contract as dc  # noqa: E402
from app import freeform  # noqa: E402
from app import generation_worker as worker  # noqa: E402
import main as gateway_main  # noqa: E402
from test_proposal_agent_and_gateway import _make_fake_backends, _sample_deck_spec  # noqa: E402

PNG = freeform.placeholder_png(4, 4)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#0af"/></svg>'
CHART = '{"xAxis":{"type":"category","data":["4月","5月"]},"yAxis":{"type":"value"},"series":[{"type":"bar","data":[1,2]}]}'.encode("utf-8")
GOOD = [("表紙", "Acme Corp 様"), ("課題", "問い合わせの 6 割が定型"), ("提案", "Agent Runtime で自動化")]
EDITED = [("表紙", "Acme Corp 様"), ("課題", "問い合わせの 6 割が定型"), ("提案", "Agent Runtime で 24 時間自動応答"), ("効果", "対応時間を半減（試算）")]


def _deck_files(slides: list[tuple[str, str]], extra: dict[str, bytes] | None = None, figure: bool = False) -> dict[str, bytes]:
    body = "".join(
        f'<section class="pd-slide" data-pd-title="{title}"><h2>{title}</h2><p>{text}</p></section>' for title, text in slides
    )
    if figure:
        body += (
            '<section class="pd-slide" data-pd-title="図解"><h2>図解</h2><img src="assets/diagram.svg" alt="構成図">'
            '<div class="chart" data-chart="charts/sales.json"></div></section>'
        )
    html = (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>Acme</title></head>'
        f'<body><main id="pd-deck">{body}</main></body></html>'
    ).encode("utf-8")
    files = {"index.html": html, "manifest.json": b'{"concept": "clean"}'}
    if figure:
        files.update({"assets/diagram.svg": SVG, "charts/sales.json": CHART})
    files.update(extra or {})
    return files


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class GcsDictStore:
    """freeform.Store backed by the same in-memory dict the fake google.cloud.storage client uses."""

    def __init__(self, gcs: dict[str, bytes], bucket: str) -> None:
        self.gcs = gcs
        self.bucket = bucket
        self.generation = 0
        self.gens: dict[str, int] = {}

    def _key(self, path: str) -> str:
        return f"{self.bucket}/{path}"

    def list_meta(self, prefix: str) -> dict[str, tuple[int, int]]:
        full = self._key(prefix)
        return {k[len(full):]: (len(v), self.gens.get(k, 0)) for k, v in self.gcs.items() if k.startswith(full)}

    def read_all(self, prefix: str, max_files: int = 250, max_bytes: int = 40_000_000) -> dict[str, bytes]:
        full = self._key(prefix)
        return {k[len(full):]: v for k, v in self.gcs.items() if k.startswith(full)}

    def read(self, path: str) -> bytes | None:
        return self.gcs.get(self._key(path))

    def write(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.generation += 1
        self.gcs[self._key(path)] = data
        self.gens[self._key(path)] = self.generation

    def write_many(self, prefix: str, files: dict[str, bytes]) -> None:
        for rel, data in files.items():
            self.write(prefix + rel, data)



Step = Callable[[freeform.Run, Any, str], freeform.Outcome]


class FakePipeline:
    """Stands in for freeform.run_pipeline; records every call and plays scripted outcomes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[Step] = []

    def __call__(self, run: freeform.Run, store: Any, designer: Any, *, mode: str, input_files: dict[str, str],
                 seed_files: dict[str, bytes] | None = None, title: str = "") -> freeform.Outcome:
        self.calls.append({"mode": mode, "input_files": dict(input_files), "seed_files": dict(seed_files or {}), "title": title})
        return self.script.pop(0)(run, store, title)


def publishes(slides: list[tuple[str, str]], rounds: int = 1, figure: bool = False) -> Step:
    def step(run: freeform.Run, store: Any, title: str) -> freeform.Outcome:
        run.status("freeform_reviewing", "エージェントがスクリーンショットを見て修正しています（1/2）")
        build = dc.build_publishable(_deck_files(slides, figure=figure), title)
        assert not build.errors, [i.message for i in build.errors]
        shots = [{"index": i, "issues": [], "screenshot_b64": base64.b64encode(PNG).decode()} for i in range(build.slide_count)]
        record = freeform.BuildRecord(number=rounds + 1, build=build, prefix=f"{run.staging}build/r{rounds + 1}/", render={"slides": shots})
        review_rounds = [
            {"round": n, "review_status": "FIXED" if n < rounds else "APPROVED", "files_changed": n < rounds, "screenshots_sent": build.slide_count}
            for n in range(1, rounds + 1)
        ]
        return freeform.Outcome(
            True, record, [record], "sess-1", [f"sess-1-t{n + 1}" for n in range(rounds + 1)], review_rounds,
            "白基調で構成しました", ["3 枚目の余白を調整しました"], [],
        )

    return step


def fails(error: str) -> Step:
    return lambda run, store, title: freeform.Outcome(False, None, [], "sess-1", ["sess-1-t1"], [], "", [], ["警告"], error)


def crashes(exc: Exception) -> Step:
    def step(run: freeform.Run, store: Any, title: str) -> freeform.Outcome:
        raise exc

    return step


class _OfflineGenAIClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.models = MagicMock()
        self.models.generate_content.side_effect = RuntimeError("offline unit test")


def _with_listing(backends: dict[str, Any]) -> dict[str, Any]:
    """Adds client.list_blobs(bucket, prefix=...) to the shared fake storage client (used by hard delete)."""
    base_factory = backends["storage_factory"]
    gcs = backends["gcs"]

    def factory(*args: Any, **kwargs: Any) -> MagicMock:
        client = base_factory(*args, **kwargs)

        def list_blobs(bucket_name: str, prefix: str = "") -> list[MagicMock]:
            blobs = []
            for key in list(gcs):
                bucket, _, name = key.partition("/")
                if bucket == bucket_name and name.startswith(prefix):
                    blob = MagicMock()
                    blob.name = name
                    blob.delete.side_effect = lambda key=key: gcs.pop(key, None)
                    blobs.append(blob)
            return blobs

        client.list_blobs.side_effect = list_blobs
        return client

    backends["storage_factory"] = factory
    return backends


@pytest.fixture
def ff(monkeypatch: pytest.MonkeyPatch):
    """Free-form enabled, no background trigger (tests run the worker explicitly), all backends in memory."""
    backends = _with_listing(_make_fake_backends())
    pipeline = FakePipeline()
    monkeypatch.setenv("FREEFORM_DESIGN_ENABLED", "true")
    monkeypatch.setenv("GENERATION_TRIGGER_MODE", "none")
    monkeypatch.setenv("ENABLE_LLM_DECK_EDIT", "false")
    monkeypatch.setenv("PROPOSAL_GCS_BUCKET", "test-bucket")
    monkeypatch.delenv("GENERATION_JOB_NAME", raising=False)
    monkeypatch.delenv("DECK_RENDERER_URL", raising=False)
    monkeypatch.setattr(agent_mod, "search_internal_knowledge", lambda query: '{"results": []}')
    monkeypatch.setattr(freeform, "Store", lambda project_id, bucket: GcsDictStore(backends["gcs"], bucket))
    monkeypatch.setattr(freeform, "make_designer", lambda project_id: object())
    monkeypatch.setattr(freeform, "run_pipeline", pipeline)
    with (
        patch("google.cloud.storage.Client", side_effect=backends["storage_factory"]),
        patch("google.cloud.firestore.Client", side_effect=backends["firestore_factory"]),
        patch("app.agent.genai.Client", _OfflineGenAIClient),
    ):
        yield backends, pipeline


def _create(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "client_name": "Acme Corp",
        "proposal_title": "問い合わせ対応の自動化",
        "proposal_brief": "定型問い合わせを AI で自動化する",
        "design_request": "背景は白、売上推移のグラフを入れて",
    }
    kwargs.update(overrides)
    return agent_mod.create_proposal_website(**kwargs)


def _publish_freeform(ff_env: Any, slides: list[tuple[str, str]] = GOOD) -> str:
    backends, pipeline = ff_env
    created = _create()
    pipeline.script.append(publishes(slides))
    result = worker.run_job(created["presentation_id"], "generate")
    assert result["status"] == "READY", result
    return created["presentation_id"]


# ---------------------------------------------------------------------------
# Creation: free-form default, template fallback, fast mode, disabled flag
# ---------------------------------------------------------------------------
def test_create_freeform_default_publishes_v1_with_review_metadata(ff) -> None:
    backends, pipeline = ff
    created = _create()
    assert created["status"] == "GENERATING"
    assert created["design_mode"] == "freeform"
    assert created["estimated_completion"] == agent_mod.FREEFORM_CREATE_ETA
    assert "ADK" in created["generation_plan"]
    assert "最大 2 回" in created["generation_plan"]
    pres_id = created["presentation_id"]
    doc = backends["firestore"][pres_id]
    assert doc["design_mode"] == "freeform"
    assert doc["generation_inputs"]["design_mode"] == "freeform"
    assert doc["generation_inputs"]["design_request"] == "背景は白、売上推移のグラフを入れて"

    generating = agent_mod.get_proposal_status(pres_id)
    assert generating["estimated_completion"] == agent_mod.FREEFORM_CREATE_ETA
    assert "自由デザインで生成中" in generating["user_message"]

    pipeline.script.append(publishes(GOOD, rounds=2))
    result = worker.run_job(pres_id, "generate")
    assert result["status"] == "READY"
    assert result["generation_engine"] == freeform.engine_name()
    assert result["review_rounds"] == 2

    call = pipeline.calls[0]
    assert call["mode"] == "create"
    assert set(call["input_files"]) == {"brief.md", "knowledge.md", "DESIGN_RULES.md"}
    assert "背景は白、売上推移のグラフを入れて" in call["input_files"]["brief.md"]
    assert "Acme Corp" in call["input_files"]["brief.md"]

    doc = backends["firestore"][pres_id]
    prefix = f"presentations/{pres_id}/v1/"
    assert doc["render_mode"] == "freeform"
    assert doc["freeform_prefix"] == prefix
    assert doc["gcs_blob_path"] == prefix + "index.html"
    assert doc["content_version"] == 1
    assert doc["freeform"]["current_version"] == 1
    assert doc["freeform_versions"][0]["based_on"] == 0
    assert doc["generation_status"] == "ready"
    assert f"test-bucket/{prefix}index.html" in backends["gcs"]
    assert f"test-bucket/{prefix}source.html" in backends["gcs"]
    assert f"test-bucket/{prefix}_qa/slide_01.jpg" in backends["gcs"]
    assert b"/_rt/v1/deck-runtime.js" in backends["gcs"][f"test-bucket/{prefix}index.html"]

    status = agent_mod.get_proposal_status(pres_id)
    assert status["render_mode"] == "freeform"
    assert status["freeform_version"] == 1
    assert status["review_rounds"] == 2
    assert [r["review_status"] for r in status["review_round_details"]] == ["FIXED", "APPROVED"]
    assert status["slide_count"] == 3
    assert "自由デザイン" in status["generation_engine_label"]
    assert "2 回見直しました" in status["user_message"]

    # A retried job execution never re-runs the designer on a published free-form deck.
    assert worker.run_job(pres_id, "generate")["status"] == "ALREADY_READY"
    assert len(pipeline.calls) == 1


def test_create_freeform_failure_falls_back_to_template_and_says_so(ff, monkeypatch: pytest.MonkeyPatch) -> None:
    backends, pipeline = ff
    seen: dict[str, Any] = {}
    original = agent_mod.synthesize_deck_spec_with_skill

    def spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(agent_mod, "synthesize_deck_spec_with_skill", spy)
    pres_id = _create()["presentation_id"]
    pipeline.script.append(fails("index.html が作成されませんでした"))
    result = worker.run_job(pres_id, "generate")

    assert result["generation_status"] == "ready"
    assert result["generation_engine"].endswith("+freeform_fallback:no_index")
    doc = backends["firestore"][pres_id]
    assert doc["render_mode"] == "template"
    assert doc["design_mode"] == "freeform"
    assert "index.html" in doc["freeform_fallback_reason"]
    assert doc["freeform"]["attempted"] is True
    assert doc["freeform"]["turn_ids"] == ["sess-1-t1"]
    assert f"test-bucket/presentations/{pres_id}/index.html" in backends["gcs"]
    assert "テンプレートで仕上げました" in doc["generation_engine_label"]

    status = agent_mod.get_proposal_status(pres_id)
    assert status["render_mode"] == "template"
    assert status["freeform_fallback_reason"]
    assert "テンプレートで仕上げています" in status["user_message"]


def test_create_freeform_crash_falls_back_with_error_reason(ff) -> None:
    backends, pipeline = ff
    pres_id = _create()["presentation_id"]
    pipeline.script.append(crashes(RuntimeError("sandbox exploded")))
    result = worker.run_job(pres_id, "generate")
    assert result["generation_status"] == "ready"
    assert result["generation_engine"].endswith("+freeform_fallback:error")
    assert "RuntimeError" in backends["firestore"][pres_id]["freeform_fallback_reason"]


def test_template_fast_mode_on_request_even_when_freeform_is_enabled(ff) -> None:
    backends, pipeline = ff
    spec = _sample_deck_spec()
    published = agent_mod.create_proposal_website(deck_spec_json=spec.model_dump_json(), design_mode="高速モード")
    assert published["status"] == "PUBLISHED"
    assert published["design_mode"] == "template"
    assert backends["firestore"][published["presentation_id"]]["render_mode"] == "template"

    queued = _create(design_mode="template")
    assert queued["design_mode"] == "template"
    assert queued["estimated_completion"].startswith("通常1〜5分")
    pipeline_calls_before = len(pipeline.calls)
    worker.run_job(queued["presentation_id"], "generate")
    assert len(pipeline.calls) == pipeline_calls_before  # the designer agent is not used in fast mode
    assert backends["firestore"][queued["presentation_id"]]["render_mode"] == "template"


def test_freeform_disabled_uses_template_and_refuses_conversion(ff, monkeypatch: pytest.MonkeyPatch) -> None:
    backends, pipeline = ff
    monkeypatch.setenv("FREEFORM_DESIGN_ENABLED", "false")
    created = _create(design_mode="freeform")
    assert created["design_mode"] == "template"
    worker.run_job(created["presentation_id"], "generate")
    assert pipeline.calls == []

    # A doc queued as free-form before the flag was turned off still completes (template, reason recorded).
    pres_id = _create()["presentation_id"]
    backends["firestore"][pres_id]["generation_inputs"]["design_mode"] = "freeform"
    result = worker.run_job(pres_id, "generate")
    assert result["generation_engine"].endswith("+freeform_fallback:freeform_disabled")

    refused = agent_mod.edit_proposal_website(pres_id, "グラフを追加して", convert_to_freeform=True)
    assert refused["status"] == "NO_CHANGE"
    assert "FREEFORM_DESIGN_ENABLED" in refused["unsupported_requests"][0]
    assert backends["firestore"][pres_id]["generation_status"] == "ready"


# ---------------------------------------------------------------------------
# Edits: queued -> applied (machine-verified diff), idempotent, undo via based_on
# ---------------------------------------------------------------------------
def test_freeform_edit_is_queued_applied_with_verified_diff_and_undone(ff) -> None:
    backends, pipeline = ff
    pres_id = _publish_freeform(ff)

    queued = agent_mod.edit_proposal_website(pres_id, "提案の文言を強くして、効果のスライドを追加して")
    assert queued["status"] == "EDIT_QUEUED"
    assert queued["kind"] == "edit"
    assert queued["estimated_completion"] == agent_mod.FREEFORM_EDIT_ETA
    assert queued["verified_changes"] == []
    assert "反映済みと言ってはいけません" in queued["next_action"]
    doc = backends["firestore"][pres_id]
    assert doc["generation_status"] == "updating"
    assert doc["generation_phase"] == "freeform_edit_queued"
    assert doc["edit_request"]["mode"] == "freeform"
    request_id = doc["edit_request"]["request_id"]

    busy = agent_mod.edit_proposal_website(pres_id, "色も変えて")
    assert busy["status"] == "BUSY"
    running = agent_mod.get_proposal_status(pres_id)
    assert running["generation_status"] == "updating"
    assert running["estimated_completion"] == agent_mod.FREEFORM_EDIT_ETA
    assert "更新中" in running["user_message"]

    pipeline.script.append(publishes(EDITED))
    applied = worker.run_job(pres_id, "freeform_edit")
    assert applied["status"] == "UPDATED"
    assert applied["freeform_version"] == 2
    call = pipeline.calls[-1]
    assert call["mode"] == "edit"
    assert "効果のスライドを追加して" in call["input_files"]["edit_request.md"]
    # The agent edits its own raw HTML (source.html), not the sanitised/runtime-injected index.html.
    assert call["seed_files"]["index.html"] == _deck_files(GOOD)["index.html"]
    assert not any(path.startswith("_qa/") for path in call["seed_files"])

    doc = backends["firestore"][pres_id]
    last = doc["last_edit_result"]
    assert last["status"] == "applied"
    assert last["request_id"] == request_id
    assert last["verified_changes"] == dc.describe_changes(_deck_files(GOOD), _deck_files(EDITED))
    assert "スライド枚数: 3 枚 → 4 枚" in last["verified_changes"]
    assert last["designer_notes"] == ["白基調で構成しました", "3 枚目の余白を調整しました"]
    assert last["previous_freeform_version"] == 1
    assert doc["freeform"]["current_version"] == 2
    assert doc["freeform_versions"][-1]["based_on"] == 1
    assert doc["content_version"] == 2
    assert doc["edit_request"] is None
    assert doc["generation_status"] == "ready"

    status = agent_mod.get_proposal_status(pres_id)
    assert status["freeform_version"] == 2
    assert status["available_freeform_versions"] == [1, 2]
    assert status["last_edit_result"]["status"] == "applied"
    assert "直近の修正は反映済み" in status["user_message"]

    # Cloud Run retries: nothing queued -> NO_REQUEST; same request replayed -> ALREADY_APPLIED.
    assert worker.run_job(pres_id, "freeform_edit")["status"] == "NO_REQUEST"
    backends["firestore"][pres_id]["edit_request"] = {"request_id": request_id, "mode": "freeform", "kind": "edit"}
    assert worker.run_job(pres_id, "freeform_edit")["status"] == "ALREADY_APPLIED"
    backends["firestore"][pres_id]["edit_request"] = None

    undone = agent_mod.edit_proposal_website(pres_id, "元に戻して", undo_last_edit=True)
    assert undone["status"] == "UPDATED"
    assert undone["kind"] == "undo"
    assert undone["freeform_version"] == 1
    assert undone["verified_changes"] == ["表示する自由デザイン版を v2 から v1 に戻しました"]
    doc = backends["firestore"][pres_id]
    assert doc["freeform_prefix"] == f"presentations/{pres_id}/v1/"
    assert doc["gcs_blob_path"] == f"presentations/{pres_id}/v1/index.html"
    assert doc["content_version"] == 3
    assert doc["last_edit_result"]["edit_engine"] == "freeform_undo"
    v1 = next(v for v in doc["freeform_versions"] if v["version"] == 1)
    assert doc["slide_count"] == v1["slide_count"]
    assert doc["slide_titles"] == v1["slide_titles"]

    nothing = agent_mod.edit_proposal_website(pres_id, "もう一度戻して", undo_last_edit=True)
    assert nothing["status"] == "NO_CHANGE"
    assert backends["firestore"][pres_id]["content_version"] == 3


def test_freeform_edit_no_change_failure_and_trigger_failure_keep_previous_version(ff, monkeypatch: pytest.MonkeyPatch) -> None:
    backends, pipeline = ff
    pres_id = _publish_freeform(ff)

    def queue_and_run(step: Step) -> dict[str, Any]:
        assert agent_mod.edit_proposal_website(pres_id, "もっと良くして")["status"] == "EDIT_QUEUED"
        pipeline.script.append(step)
        return worker.run_job(pres_id, "freeform_edit")

    assert queue_and_run(publishes(GOOD))["status"] == "NO_CHANGE"
    doc = backends["firestore"][pres_id]
    assert doc["last_edit_result"]["status"] == "no_change"
    assert doc["last_edit_result"]["verified_changes"] == []
    assert doc["last_edit_result"]["designer_notes"]  # self-reported, kept separate from verified changes
    assert doc["freeform"]["current_version"] == 1 and doc["content_version"] == 1
    assert "反映できる変更点がありませんでした" in agent_mod.get_proposal_status(pres_id)["user_message"]

    assert queue_and_run(fails("公開できる版がありません: x"))["status"] == "EDIT_FAILED"
    doc = backends["firestore"][pres_id]
    assert doc["last_edit_result"]["status"] == "failed"
    assert doc["freeform"]["current_version"] == 1 and doc["generation_status"] == "ready"
    assert "修正前の版" in agent_mod.get_proposal_status(pres_id)["user_message"]

    crashed = queue_and_run(crashes(RuntimeError("renderer down")))
    assert crashed["status"] == "EDIT_FAILED" and "RuntimeError" in crashed["error"]
    assert backends["firestore"][pres_id]["edit_request"] is None

    monkeypatch.setattr(
        agent_mod, "_start_background_generation", lambda pid, job_mode="generate": {"mode": "cloud_run_job_failed", "error": "403"}
    )
    failed = agent_mod.edit_proposal_website(pres_id, "背景を白にして")
    assert failed["status"] == "EDIT_FAILED"
    doc = backends["firestore"][pres_id]
    assert doc["generation_status"] == "ready" and doc["edit_request"] is None
    assert doc["last_edit_result"]["status"] == "failed"
    assert doc["freeform"]["current_version"] == 1


def test_convert_template_to_freeform_then_undo_back_to_template(ff) -> None:
    backends, pipeline = ff
    spec = _sample_deck_spec()
    pres_id = agent_mod.create_proposal_website(deck_spec_json=spec.model_dump_json(), design_mode="テンプレート")["presentation_id"]
    assert backends["firestore"][pres_id]["render_mode"] == "template"

    queued = agent_mod.edit_proposal_website(pres_id, "売上グラフを入れて自由デザインで作り直して", convert_to_freeform=True)
    assert queued["status"] == "EDIT_QUEUED"
    assert queued["kind"] == "convert"
    assert queued["estimated_completion"] == agent_mod.FREEFORM_CREATE_ETA
    assert backends["firestore"][pres_id]["edit_request"]["kind"] == "convert"

    pipeline.script.append(publishes(GOOD, figure=True))
    converted = worker.run_job(pres_id, "freeform_edit")
    assert converted["status"] == "UPDATED"
    call = pipeline.calls[-1]
    assert call["mode"] == "create"
    brief = call["input_files"]["brief.md"]
    assert "既存デッキの内容" in brief and spec.client_name in brief
    assert "売上グラフを入れて自由デザインで作り直して" in brief
    doc = backends["firestore"][pres_id]
    assert doc["render_mode"] == "freeform"
    assert doc["last_edit_result"]["verified_changes"][0].startswith("テンプレート版を自由デザイン版に作り直し（4 枚）")
    assert doc["freeform_versions"][0]["based_on"] == 0
    assert doc["freeform_versions"][0]["based_on_render_mode"] == "template"
    assert doc["deck_spec"]  # the template version is kept for undo
    assert agent_mod.get_proposal_status(pres_id)["render_mode"] == "freeform"

    undone = agent_mod.edit_proposal_website(pres_id, "元に戻して", undo_last_edit=True)
    assert undone["status"] == "UPDATED"
    assert undone["render_mode"] == "template"
    assert "テンプレート版に戻しました" in undone["verified_changes"][0]
    doc = backends["firestore"][pres_id]
    assert doc["render_mode"] == "template"
    assert doc["gcs_blob_path"] == f"presentations/{pres_id}/index.html"
    assert agent_mod.get_proposal_status(pres_id)["render_mode"] == "template"


def test_hard_delete_removes_all_freeform_versions(ff) -> None:
    backends, pipeline = ff
    pres_id = _publish_freeform(ff)
    assert any(key.startswith(f"test-bucket/presentations/{pres_id}/v1/") for key in backends["gcs"])
    result = agent_mod.delete_proposal_website(pres_id, hard_delete_gcs=True)
    assert result["status"] == "REVOKED", result
    assert result["gcs_freeform_objects_deleted"] >= 4  # index.html, source.html, manifest.json, _qa screenshots
    assert not any(key.startswith(f"test-bucket/presentations/{pres_id}/") for key in backends["gcs"])
    assert backends["firestore"][pres_id]["is_active"] is False


# ---------------------------------------------------------------------------
# Hosting gateway: canonical URL, CSP + nonce, deck assets, runtime, status
# ---------------------------------------------------------------------------
def _gateway_doc(pres_id: str, viewer_id: str, password: str, **fields: Any) -> dict[str, Any]:
    pw_hash, pw_salt = agent_mod.hash_password(password)
    doc = {
        "presentation_id": pres_id,
        "viewer_id": viewer_id,
        "password_hash": pw_hash,
        "password_salt": pw_salt,
        "gcs_bucket": "test-bucket",
        "client_name": "Acme Corp",
        "proposal_title": "問い合わせ対応の自動化",
        "expires_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)).isoformat(),
        "status": "active",
        "is_active": True,
        "design_mode": "freeform",
        "render_mode": "freeform",
        "freeform_prefix": f"presentations/{pres_id}/v1/",
        "gcs_blob_path": f"presentations/{pres_id}/v1/index.html",
        "generation_status": "ready",
        "generation_phase": "ready",
        "content_version": 1,
    }
    doc.update(fields)
    return doc


def _basic(viewer_id: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{viewer_id}:{password}".encode()).decode()}


def test_gateway_serves_freeform_deck_with_canonical_url_csp_assets_and_runtime() -> None:
    pres_id, viewer_id, password = "prop-20261004-ff000001", "client-acme-ab12", "FreeForm-123456"
    build = dc.build_publishable(_deck_files(GOOD, figure=True), "Acme")
    assert not build.errors
    blobs = {f"presentations/{pres_id}/v1/{path}": data for path, data in build.files.items()}
    doc = _gateway_doc(pres_id, viewer_id, password)

    def fetch(bucket: str, path: str) -> bytes:
        assert bucket == "test-bucket"
        return blobs[path]

    with (
        patch.object(gateway_main, "_get_firestore_doc", side_effect=lambda _: doc),
        patch.object(gateway_main, "_fetch_html_from_gcs", side_effect=fetch),
        patch.object(gateway_main, "_record_access_log", side_effect=lambda *a, **k: None),
    ):
        client = TestClient(gateway_main.app)
        # Free-form decks use relative asset URLs, so /p/<id> redirects to /p/<id>/ (query kept, cookie issued).
        redirect = client.get(f"/p/{pres_id}?from=ge", headers=_basic(viewer_id, password), follow_redirects=False)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == f"/p/{pres_id}/?from=ge"
        cookie_key = gateway_main._get_cookie_name(pres_id)
        assert cookie_key in redirect.cookies
        client.cookies.set(cookie_key, redirect.cookies[cookie_key])

        page = client.get(f"/p/{pres_id}/")
        assert page.status_code == 200
        csp = page.headers["content-security-policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        script_src = next(part for part in csp.split(";") if part.strip().startswith("script-src"))
        assert "unsafe-inline" not in script_src and "unsafe-eval" not in script_src
        assert f'<script nonce="{nonce}">' in page.text  # the live-update watcher carries the response nonce
        assert 'data-initial-mode=""' in page.text or "data-initial-mode" in page.text
        assert "/_rt/v1/deck-runtime.js" in page.text and "/_rt/v1/echarts.min.js" in page.text
        assert page.headers["cache-control"] == "no-store, private"
        second = client.get(f"/p/{pres_id}/")
        assert re.search(r"'nonce-([^']+)'", second.headers["content-security-policy"]).group(1) != nonce

        svg = client.get(f"/p/{pres_id}/assets/diagram.svg")
        assert svg.status_code == 200
        assert svg.headers["content-type"].startswith("image/svg+xml")
        assert svg.headers["content-security-policy"] == dc.SVG_ASSET_CSP
        assert svg.headers["cross-origin-resource-policy"] == "same-origin"
        assert svg.headers["cache-control"] == "no-store, private"
        chart = client.get(f"/p/{pres_id}/charts/sales.json")
        assert chart.status_code == 200 and chart.json()["series"][0]["type"] == "bar"
        assert client.get(f"/p/{pres_id}/assets/missing.png").status_code == 404
        assert client.get(f"/p/{pres_id}/assets/page.html").status_code == 404  # not an allowlisted asset type
        assert client.get(f"/p/{pres_id}/charts/sales.js").status_code == 404

        status = client.get(f"/p/{pres_id}/status").json()
        assert status["render_mode"] == "freeform" and status["design_mode"] == "freeform"
        assert status["edit_mode"] == ""

        anonymous = TestClient(gateway_main.app)
        assert anonymous.get(f"/p/{pres_id}/assets/diagram.svg").status_code == 401
        assert anonymous.get(f"/p/{pres_id}/charts/sales.json").status_code == 401

        for name in dc.RUNTIME_FILES:
            runtime = anonymous.get(f"{dc.RUNTIME_BASE}{name}")  # shared runtime, no customer data
            assert runtime.status_code == 200, name
            assert runtime.headers["cache-control"] == "no-cache"
        assert anonymous.get(f"{dc.RUNTIME_BASE}main.py").status_code == 404
        assert anonymous.get(f"{dc.RUNTIME_BASE}evil.js").status_code == 404

        # Template decks keep the slash-less canonical URL and expose no deck files.
        doc.update({"render_mode": "template", "freeform_prefix": "", "gcs_blob_path": f"presentations/{pres_id}/index.html"})
        blobs[f"presentations/{pres_id}/index.html"] = b"<html><body>template</body></html>"
        back = client.get(f"/p/{pres_id}/", follow_redirects=False)
        assert back.status_code == 307 and back.headers["location"] == f"/p/{pres_id}"
        template_page = client.get(f"/p/{pres_id}")
        assert template_page.status_code == 200
        assert "content-security-policy" not in template_page.headers
        assert client.get(f"/p/{pres_id}/assets/diagram.svg").status_code == 404

        doc["is_active"] = False
        assert client.get(f"/p/{pres_id}/assets/diagram.svg").status_code == 403


def test_gateway_freeform_generating_and_updating_pages() -> None:
    pres_id, viewer_id, password = "prop-20261004-ff000002", "client-acme-cd34", "FreeForm-654321"
    build = dc.build_publishable(_deck_files(GOOD), "Acme")
    now = datetime.datetime.now(datetime.timezone.utc)
    doc = _gateway_doc(
        pres_id, viewer_id, password,
        render_mode="", freeform_prefix="", generation_status="generating", generation_phase="freeform_drafting",
        generation_requested_at=now.isoformat(),
    )
    fetched: list[str] = []

    def fetch(bucket: str, path: str) -> bytes:
        fetched.append(path)
        return build.files["index.html"]

    with (
        patch.object(gateway_main, "_get_firestore_doc", side_effect=lambda _: doc),
        patch.object(gateway_main, "_fetch_html_from_gcs", side_effect=fetch),
        patch.object(gateway_main, "_record_access_log", side_effect=lambda *a, **k: None),
    ):
        client = TestClient(gateway_main.app)
        generating = client.get(f"/p/{pres_id}", headers=_basic(viewer_id, password))
        assert generating.status_code == 200
        assert 'data-design-mode="freeform"' in generating.text
        assert "通常 7〜11 分" in generating.text
        assert "スクリーンショットを見て修正" in generating.text
        assert fetched == []
        cookie_key = gateway_main._get_cookie_name(pres_id)
        client.cookies.set(cookie_key, generating.cookies[cookie_key])
        assert client.get(f"/p/{pres_id}/", follow_redirects=False).status_code == 200  # no redirect while generating

        # Published v1, then a free-form edit is running: the old version stays visible with the 更新中 watcher.
        doc.update(
            {
                "render_mode": "freeform",
                "freeform_prefix": f"presentations/{pres_id}/v1/",
                "generation_status": "updating",
                "generation_phase": "freeform_reviewing",
                "edit_request": {"request_id": "abc", "mode": "freeform", "kind": "edit"},
                "edit_requested_at": now.isoformat(),
            }
        )
        updating = client.get(f"/p/{pres_id}/")
        assert updating.status_code == 200
        assert 'data-initial-status="updating"' in updating.text
        assert 'data-initial-mode="freeform"' in updating.text
        assert "通常 5〜7 分" in updating.text
        assert fetched == [f"presentations/{pres_id}/v1/index.html"]
        status = client.get(f"/p/{pres_id}/status").json()
        assert status["generation_status"] == "updating" and status["edit_mode"] == "freeform"

        # A free-form edit lock older than FREEFORM_UPDATING_STALE_SECONDS (25 min) no longer blocks viewers.
        doc["edit_requested_at"] = (now - datetime.timedelta(minutes=30)).isoformat()
        stale = client.get(f"/p/{pres_id}/status").json()
        assert stale["generation_status"] == "ready" and stale["edit_mode"] == ""


def test_ui_format_portal_default_and_slides_override(ff) -> None:
    backends, pipeline = ff
    created_default = _create()
    assert created_default["ui_format"] == "portal"
    pres_id = created_default["presentation_id"]
    assert backends["firestore"][pres_id]["ui_format"] == "portal"
    assert backends["firestore"][pres_id]["generation_inputs"]["ui_format"] == "portal"

    pipeline.script.append(publishes(GOOD))
    worker.run_job(pres_id, "generate")
    brief_md = pipeline.calls[-1]["input_files"]["brief.md"]
    assert "**UI形式（レイアウト）**: portal" in brief_md
    assert 'data-pd-layout="portal"' in brief_md

    created_slides = _create(ui_format="スライドモード")
    assert created_slides["ui_format"] == "slides"
    pres_slides_id = created_slides["presentation_id"]
    assert backends["firestore"][pres_slides_id]["ui_format"] == "slides"
    pipeline.script.append(publishes(GOOD))
    worker.run_job(pres_slides_id, "generate")
    slides_brief = pipeline.calls[-1]["input_files"]["brief.md"]
    assert "**UI形式（レイアウト）**: slides" in slides_brief

    # Edit freeform with ui_format switch
    queued = agent_mod.edit_proposal_website(pres_id, "16:9のスライドモードに切り替えて", ui_format="slides")
    assert queued["status"] == "EDIT_QUEUED"
    assert queued["ui_format"] == "slides"
    pipeline.script.append(publishes(EDITED))
    worker.run_job(pres_id, "freeform_edit")
    edit_md = pipeline.calls[-1]["input_files"]["edit_request.md"]
    assert "UI形式（レイアウト）を次の形式にする" in edit_md and ": slides" in edit_md
    assert backends["firestore"][pres_id]["ui_format"] == "slides"
