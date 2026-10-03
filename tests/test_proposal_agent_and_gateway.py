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

"""Unit and integration tests for Proposal Website Concierge Agent and Cloud Run Hosting Gateway."""

from __future__ import annotations

import base64
import datetime
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "proposal_agent"))
sys.path.insert(0, str(ROOT_DIR))

from app.agent import (  # noqa: E402
    ArchitectureNode,
    ChallengeItem,
    create_proposal_website,
    CxHighlight,
    PresentationDeckSpec,
    RoadmapPhase,
    delete_proposal_website,
    edit_proposal_website,
    get_proposal_access_logs,
    hash_password,
    list_proposal_websites,
    load_interactive_slide_designer_skill,
    manage_proposal_credentials,
    publish_presentation,
    render_deck_html,
    root_agent,
    validate_rendered_html,
    verify_password,
)
from hosting_gateway import main as gateway_main  # noqa: E402


def _sample_deck_spec(theme_color: str = "sky") -> PresentationDeckSpec:
    return PresentationDeckSpec(
        client_name="株式会社アクメリテールホールディングス",
        client_slug="acme-retail",
        proposal_title="AIエージェント×統合CDPによる次世代OMO顧客体験変革",
        subtitle="店舗・EC・アプリの顧客接点をリアルタイム統合し、LTV最大化を実現するビジネス変革の羅針盤",
        theme_color=theme_color,
        custom_callout="エグゼクティブ特別提案",
        current_challenges=[
            ChallengeItem(
                title="チャネル間の顧客データ分断",
                description="店舗POS・EC・スマホアプリの会員データがサイロ化し、一貫した接客ができていない。",
            ),
            ChallengeItem(
                title="画一的なメルマガ・施策配信",
                description="セグメント抽出に数日を要し、顧客のリアルタイムな購買意図を捉えたアプローチが困難。",
            ),
            ChallengeItem(
                title="マーケティング運用工数の肥大化",
                description="キャンペーン企画・クリエイティブ制作・効果検証が手作業中心でPDCAが回らない。",
            ),
        ],
        executive_conclusion=(
            "UXデザイン知見とGoogle Cloud (BigQuery + Agent Runtime) を融合し、"
            "最短2ヶ月で『対話型AIコンシェルジュ』と『自律型マーケティング基盤』を立ち上げます。"
        ),
        before_state=[
            "SQLによる手動リスト抽出で施策実行まで平均5営業日のリードタイム",
            "全会員への一斉配信によるクーポン原価率の悪化とブロック率上昇",
            "購買後のフォローアップが分断され、2回目購買への転換率が停滞",
        ],
        after_state=[
            "AIエージェントが購買直後のマイクロモーメントを検知し即座に最適提案",
            "顧客一人ひとりの趣味嗜好・文脈に合わせた1to1スタイリング提案",
            "企画立案からHTML提案・レポーティングまでGemini Enterpriseで自律化",
        ],
        cx_highlights=[
            CxHighlight(
                title="対話型AIパーソナルスタイリスト",
                detail="LINE・会員アプリ上で自然言語と画像から最適なコーディネートを即時提案。",
            ),
            CxHighlight(
                title="リアルタイムOMO在庫・接客連携",
                detail="EC閲覧履歴と店舗試着予約を統合し、店舗スタッフへAI接客カルテを配信。",
            ),
            CxHighlight(
                title="マーケティング施策の自動生成",
                detail="Gemini Enterpriseから社内データ横断検索と施策クリエイティブ生成をワンストップ実行。",
            ),
        ],
        architecture_nodes=[
            ArchitectureNode(
                layer_name="1. 顧客接点チャネル層",
                icon="fa-mobile-screen",
                components=["公式モバイルアプリ", "LINEミニアプリ", "EC / 店舗タブレット"],
                description="オムニチャネルでの行動ログと対話リクエストをリアルタイム収集。",
            ),
            ArchitectureNode(
                layer_name="2. 認証・軽量配信基盤層",
                icon="fa-shield-halved",
                components=["Cloud Run 認証GW", "Firestore セッション管理", "非公開 Cloud Storage"],
                description="外部顧客・パートナー向けにセキュアかつ軽量なWeb配信と認証を提供。",
            ),
            ArchitectureNode(
                layer_name="3. AIエージェント実行層",
                icon="fa-brain",
                components=["Agent Runtime", "Gemini Enterprise", "Agent Search"],
                description="ADKマルチエージェントが社内知識を検索し、高度な推論とコンテンツ生成を実行。",
            ),
            ArchitectureNode(
                layer_name="4. 統合データ・ストレージ層",
                icon="fa-database",
                components=["BigQuery 統合CDP", "Private Cloud Storage", "Firestore 監査ログ"],
                description="非公開バケットとデータウェアハウスによりガバナンスと高速分析を両立。",
            ),
        ],
        roadmap_phases=[
            RoadmapPhase(
                phase_name="Phase 1: データ統合・PoC検証",
                period="Month 1 - 2",
                deliverables=[
                    "カスタマージャーニー設計と優先ユースケース定義",
                    "BigQuery CDPへの初期データ連携とAgent Search構築",
                    "AIエージェントのプロトタイプ実装・社内検証",
                ],
                milestone="プロトタイプ合意・PoC効果測定完了",
            ),
            RoadmapPhase(
                phase_name="Phase 2: パイロット導入・OMO連携",
                period="Month 3 - 4",
                deliverables=[
                    "会員アプリ・一部店舗でのAIコンシェルジュ限定公開",
                    "Cloud Run + Cloud Storage 配信基盤の本番セキュリティ適用",
                    "A/BテストによるCVR・LTVリフト検証",
                ],
                milestone="パイロット店舗・ECでのKPI目標達成",
            ),
            RoadmapPhase(
                phase_name="Phase 3: 全社展開・自律運用化",
                period="Month 5 - 6",
                deliverables=[
                    "全店舗・全会員チャネルへの本格ロールアウト",
                    "Gemini Enterpriseを活用したマーケター自走体制の構築",
                    "継続的改善ダッシュボードと運用ガバナンス定着",
                ],
                milestone="全社本番稼働・内製化移行完了",
            ),
        ],
        quantitative_roi=[
            "リピート購買転換率（CVR）: +28% 向上",
            "ロイヤル顧客LTV（年間購買単価）: +22% 伸長",
            "キャンペーン企画・提案書作成リードタイム: 65% 削減",
        ],
        qualitative_roi=[
            "店舗とECの垣根を超えたブランド体験の一貫性確立",
            "データとAIに基づく迅速な意思決定カルチャーの醸成",
            "属人化していた企画・提案ノウハウの全社ナレッジ資産化",
        ],
        next_steps=[
            "今週中：対象データソース（POS / EC / 会員DB）のサンプルスキーマ確認",
            "来週：UXデザインチームとの共同ワークショップ開催",
            "2週間後：Phase 1 スコープ定義書および詳細お見積りのご提示",
        ],
    )


def test_concierge_root_agent_and_skill_loaded() -> None:
    skill_content = load_interactive_slide_designer_skill()
    assert "interactive-slide-designer" in skill_content
    assert "bento-executive-summary" in skill_content
    tool_names = [getattr(t, "__name__", str(t)) for t in root_agent.tools]
    assert "search_internal_knowledge" in tool_names
    assert "create_proposal_website" in tool_names
    assert "edit_proposal_website" in tool_names
    assert "list_proposal_websites" in tool_names
    assert "get_proposal_access_logs" in tool_names
    assert "manage_proposal_credentials" in tool_names
    assert "delete_proposal_website" in tool_names


def test_render_and_validate_6_slide_html_with_themes() -> None:
    for theme in ("sky", "emerald", "violet", "amber", "rose"):
        spec = _sample_deck_spec(theme_color=theme)
        html = render_deck_html(spec, generated_date="2026-10-03 12:00 JST")
        assert validate_rendered_html(html) is True
        assert f'data-theme="{theme}"' in html
        assert 'data-layout="hero-cover"' in html
        assert 'data-layout="bento-executive-summary"' in html
        assert 'data-layout="as-is-to-be-comparison"' in html
        assert 'data-layout="architecture-flow"' in html
        assert 'data-layout="roadmap-timeline"' in html
        assert 'data-layout="roi-and-next-steps"' in html
        assert "株式会社アクメリテールホールディングス" in html


def test_validate_rendered_html_rejects_broken_dom() -> None:
    with pytest.raises(ValueError, match="DOCTYPE"):
        validate_rendered_html("<html><body>broken</body></html>")

    spec = _sample_deck_spec()
    valid_html = render_deck_html(spec)
    with pytest.raises(ValueError, match="Jinja2"):
        validate_rendered_html(valid_html + "{{ unexpanded }}")


def test_password_hash_and_verify() -> None:
    pw = "Proposal-Secret-2026!"
    h, salt = hash_password(pw)
    assert verify_password(pw, h, salt) is True
    assert verify_password("wrong-password", h, salt) is False
    assert gateway_main.verify_pbkdf2_password(pw, h, salt) is True
    assert gateway_main.verify_pbkdf2_password("wrong-password", h, salt) is False


def test_full_lifecycle_create_edit_list_logs_credentials_and_delete() -> None:
    spec = _sample_deck_spec()
    mock_ctx = MagicMock()
    mock_ctx.state = {"deck_spec": spec.model_dump()}

    gcs_store: dict[str, bytes] = {}
    firestore_store: dict[str, dict[str, Any]] = {}
    access_logs_store: dict[str, list[dict[str, Any]]] = {}

    def _make_mock_storage_client(*args: Any, **kwargs: Any) -> MagicMock:
        client = MagicMock()

        def _get_bucket(bname: str) -> MagicMock:
            bucket = MagicMock()

            def _get_blob(bpath: str) -> MagicMock:
                blob = MagicMock()
                key = f"{bname}/{bpath}"
                blob.upload_from_string.side_effect = (
                    lambda data, content_type=None: gcs_store.__setitem__(
                        key, data if isinstance(data, bytes) else data.encode("utf-8")
                    )
                )
                blob.download_as_bytes.side_effect = lambda: gcs_store[key]
                blob.download_as_text.side_effect = (
                    lambda encoding="utf-8": gcs_store[key].decode(encoding)
                )
                blob.exists.side_effect = lambda: key in gcs_store
                blob.delete.side_effect = lambda: gcs_store.pop(key, None)
                return blob

            bucket.blob.side_effect = _get_blob
            return bucket

        client.bucket.side_effect = _get_bucket
        return client

    def _make_mock_firestore_client(*args: Any, **kwargs: Any) -> MagicMock:
        client = MagicMock()

        def _get_collection(cname: str) -> MagicMock:
            col = MagicMock()

            def _get_doc_ref(doc_id: str) -> MagicMock:
                doc_ref = MagicMock()
                doc_ref.id = doc_id

                def _get_snap() -> MagicMock:
                    snap = MagicMock()
                    snap.id = doc_id
                    snap.exists = doc_id in firestore_store
                    snap.to_dict.side_effect = lambda: dict(
                        firestore_store.get(doc_id, {})
                    )
                    return snap

                doc_ref.get.side_effect = _get_snap
                doc_ref.set.side_effect = lambda data: firestore_store.__setitem__(
                    doc_id, dict(data)
                )
                doc_ref.update.side_effect = lambda updates: firestore_store[
                    doc_id
                ].update(updates)

                def _get_subcol(subname: str) -> MagicMock:
                    subcol = MagicMock()
                    logs = access_logs_store.setdefault(doc_id, [])

                    def _stream_logs() -> list[MagicMock]:
                        out = []
                        for entry in logs:
                            m = MagicMock()
                            m.to_dict.return_value = dict(entry)
                            out.append(m)
                        return out

                    subcol.stream.side_effect = _stream_logs
                    return subcol

                doc_ref.collection.side_effect = _get_subcol
                return doc_ref

            col.document.side_effect = _get_doc_ref

            def _stream_docs() -> list[MagicMock]:
                out = []
                for did, ddata in firestore_store.items():
                    m = MagicMock()
                    m.id = did
                    m.to_dict.return_value = dict(ddata)
                    out.append(m)
                return out

            col.stream.side_effect = _stream_docs
            return col

        client.collection.side_effect = _get_collection
        return client

    with (
        patch("google.cloud.storage.Client", side_effect=_make_mock_storage_client),
        patch("google.cloud.firestore.Client", side_effect=_make_mock_firestore_client),
        patch.dict("os.environ", {"ENABLE_LLM_DECK_EDIT": "false"}),
    ):
        created = publish_presentation(mock_ctx)
        pres_id = created["presentation_id"]
        assert created["status"] == "PUBLISHED"
        assert mock_ctx.state["published_result"]["presentation_id"] == pres_id
        assert mock_ctx.state["published_presentation"]["presentation_id"] == pres_id
        assert len(created["slide_outline"]) == 6

        edited = edit_proposal_website(
            presentation_id=pres_id,
            edit_instructions="テーマをemeraldに変更",
            new_title="【改訂版】次世代OMO顧客体験変革のご提案",
            new_theme_color="emerald",
            tool_context=mock_ctx,
        )
        assert edited["status"] == "UPDATED"
        assert edited["existing_html_loaded"] is True
        assert edited["theme_color"] == "emerald"
        assert edited["proposal_title"] == "【改訂版】次世代OMO顧客体験変革のご提案"

        listed = list_proposal_websites(client_filter="アクメ")
        assert listed["count"] == 1

        access_logs_store[pres_id] = [
            {
                "accessed_at": "2026-10-03T04:00:00+00:00",
                "viewer_id": created["viewer_id"],
                "auth_method": "basic_auth",
                "ip_address": "203.0.113.10",
                "user_agent": "pytest",
            }
        ]
        logs = get_proposal_access_logs(pres_id)
        assert logs["total_access_count"] == 1

        rotated = manage_proposal_credentials(
            presentation_id=pres_id,
            rotate_password=True,
            extend_days=30,
        )
        assert rotated["password_rotated"] is True
        assert rotated["new_viewer_password"] != created["viewer_password"]

        deleted = delete_proposal_website(
            presentation_id=pres_id,
            hard_delete_gcs=True,
        )
        assert deleted["status"] == "REVOKED"
        assert firestore_store[pres_id]["status"] == "revoked"
        assert firestore_store[pres_id]["is_active"] is False


def test_gateway_auth_flows_401_basic_cookie_and_revoked_403() -> None:
    spec = _sample_deck_spec()
    html_bytes = render_deck_html(spec).encode("utf-8")
    pw = "CorrectHorseBatteryStaple99"
    pw_hash, pw_salt = hash_password(pw)
    pres_id = "prop-20261003-test0001"
    viewer_id = "client-acme-01"

    doc_state: dict[str, Any] = {
        "presentation_id": pres_id,
        "client_name": spec.client_name,
        "viewer_id": viewer_id,
        "password_hash": pw_hash,
        "password_salt": pw_salt,
        "is_active": True,
        "status": "active",
        "expires_at": (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=7)
        ).isoformat(),
        "gcs_bucket": "test-bucket",
        "gcs_blob_path": f"presentations/{pres_id}/index.html",
    }

    with (
        patch(
            "hosting_gateway.main._get_firestore_doc",
            side_effect=lambda pid: doc_state if pid == pres_id else None,
        ),
        patch("hosting_gateway.main._record_access_log"),
        patch(
            "hosting_gateway.main._fetch_html_from_gcs",
            return_value=html_bytes,
        ),
    ):
        client = TestClient(gateway_main.app)

        assert client.get("/health").status_code == 200
        assert client.get("/healthz").status_code == 200

        r_unauth = client.get(f"/p/{pres_id}")
        assert r_unauth.status_code == 401

        r_basic = client.get(f"/p/{pres_id}", auth=(viewer_id, pw))
        assert r_basic.status_code == 200
        assert 'data-layout="hero-cover"' in r_basic.text

        doc_state["status"] = "revoked"
        r_revoked = client.get(f"/p/{pres_id}", auth=(viewer_id, pw))
        assert r_revoked.status_code == 403


def test_login_html_escapes_xss_payloads() -> None:
    malicious = '<script>alert("xss")</script>'
    rendered = gateway_main._render_login_html(
        presentation_id='"><script>alert(1)</script>',
        client_name=malicious,
        error_message=malicious,
    )
    assert '<script>alert("xss")</script>' not in rendered
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in rendered


def test_reasoning_engine_adapter_stream_sync_and_async() -> None:
    from fastapi import FastAPI
    from app.app_utils.reasoning_engine_adapter import attach_reasoning_engine_routes

    class _FakeAdkApp:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def set_up(self) -> None:
            pass

        def register_operations(self) -> dict[str, list[str]]:
            return {
                "": ["get_session"],
                "async": ["async_get_session"],
                "stream": ["stream_query", "sync_only_stream"],
                "async_stream": ["async_stream_query"],
            }

        def sync_only_stream(self, **kwargs: Any):
            yield {"event": "sync_chunk", "echo": kwargs.get("message")}

        async def async_stream_query(self, **kwargs: Any):
            yield {"event": "async_chunk", "echo": kwargs.get("message")}

    test_app = FastAPI()
    with patch("app.app_utils.reasoning_engine_adapter.AdkApp", _FakeAdkApp):
        attach_reasoning_engine_routes(test_app)
        client = TestClient(test_app)

        r1 = client.post(
            "/api/stream_reasoning_engine",
            json={"class_method": "stream_query", "input": {"message": "hello"}},
        )
        assert r1.status_code == 200
        assert '"async_chunk"' in r1.text

        r2 = client.post(
            "/api/stream_reasoning_engine",
            json={"class_method": "sync_only_stream", "input": {"message": "sync"}},
        )
        assert r2.status_code == 200
        assert '"sync_chunk"' in r2.text


def test_genai_location_routing_and_global_fallback() -> None:
    import os
    from app.agent import _get_genai_location, synthesize_deck_spec_with_skill

    with patch.dict("os.environ", {"GOOGLE_CLOUD_LOCATION": "us-central1"}, clear=False):
        os.environ.pop("GENAI_LOCATION", None)
        assert _get_genai_location("gemini-3.8-flash") == "global"
        assert _get_genai_location("antigravity-preview-05-2026") == "global"
        assert _get_genai_location("gemini-1.5-pro") == "us-central1"

    with patch.dict("os.environ", {"GENAI_LOCATION": "europe-west1"}, clear=False):
        assert _get_genai_location("gemini-3.8-flash") == "europe-west1"

    # Verify that if a regional GENAI_LOCATION fails for gemini-3.8-flash, it retries on 'global'
    spec = _sample_deck_spec()
    call_locations: list[str] = []

    def _fake_client(*args: Any, **kwargs: Any) -> MagicMock:
        loc = kwargs.get("location", "")
        call_locations.append(loc)
        c = MagicMock()
        if loc != "global":
            c.models.generate_content.side_effect = RuntimeError("404 Model not found in regional endpoint")
        else:
            resp = MagicMock()
            resp.text = spec.model_dump_json()
            c.models.generate_content.return_value = resp
        return c

    with (
        patch.dict(
            "os.environ",
            {
                "ENABLE_MANAGED_AGENTS_API": "false",
                "GENAI_LOCATION": "us-central1",
                "GEMINI_MODEL": "gemini-3.8-flash",
            },
            clear=False,
        ),
        patch("app.agent.genai.Client", side_effect=_fake_client),
    ):
        deck_out, engine = synthesize_deck_spec_with_skill(
            client_name="株式会社アクメリテールホールディングス",
            proposal_title="テスト提案",
            proposal_brief="テスト概要",
            theme_color="emerald",
        )
        assert call_locations == ["us-central1", "global"]
        assert engine == "agent_platform_gemini_with_skill:gemini-3.8-flash"
        assert deck_out.theme_color == "emerald"


def _make_fake_backends() -> dict[str, Any]:
    """In-memory stand-ins for google.cloud.storage.Client / google.cloud.firestore.Client."""
    gcs_store: dict[str, bytes] = {}
    firestore_store: dict[str, dict[str, Any]] = {}
    access_logs_store: dict[str, list[dict[str, Any]]] = {}

    def _make_mock_storage_client(*args: Any, **kwargs: Any) -> MagicMock:
        client = MagicMock()

        def _get_bucket(bname: str) -> MagicMock:
            bucket = MagicMock()

            def _get_blob(bpath: str) -> MagicMock:
                blob = MagicMock()
                key = f"{bname}/{bpath}"
                blob.upload_from_string.side_effect = (
                    lambda data, content_type=None: gcs_store.__setitem__(
                        key, data if isinstance(data, bytes) else data.encode("utf-8")
                    )
                )
                blob.download_as_bytes.side_effect = lambda: gcs_store[key]
                blob.download_as_text.side_effect = (
                    lambda encoding="utf-8": gcs_store[key].decode(encoding)
                )
                blob.exists.side_effect = lambda: key in gcs_store
                blob.delete.side_effect = lambda: gcs_store.pop(key, None)
                return blob

            bucket.blob.side_effect = _get_blob
            return bucket

        client.bucket.side_effect = _get_bucket
        return client

    def _make_mock_firestore_client(*args: Any, **kwargs: Any) -> MagicMock:
        client = MagicMock()

        def _get_collection(cname: str) -> MagicMock:
            col = MagicMock()

            def _get_doc_ref(doc_id: str) -> MagicMock:
                doc_ref = MagicMock()
                doc_ref.id = doc_id

                def _get_snap() -> MagicMock:
                    snap = MagicMock()
                    snap.id = doc_id
                    snap.exists = doc_id in firestore_store
                    snap.to_dict.side_effect = lambda: dict(firestore_store.get(doc_id, {}))
                    return snap

                doc_ref.get.side_effect = _get_snap
                doc_ref.set.side_effect = lambda data: firestore_store.__setitem__(doc_id, dict(data))
                doc_ref.update.side_effect = lambda updates: firestore_store[doc_id].update(updates)

                def _get_subcol(subname: str) -> MagicMock:
                    subcol = MagicMock()
                    logs = access_logs_store.setdefault(doc_id, [])

                    def _stream_logs() -> list[MagicMock]:
                        out = []
                        for entry in logs:
                            m = MagicMock()
                            m.to_dict.return_value = dict(entry)
                            out.append(m)
                        return out

                    subcol.stream.side_effect = _stream_logs
                    return subcol

                doc_ref.collection.side_effect = _get_subcol
                return doc_ref

            col.document.side_effect = _get_doc_ref

            def _stream_docs() -> list[MagicMock]:
                out = []
                for did, ddata in firestore_store.items():
                    m = MagicMock()
                    m.id = did
                    m.to_dict.return_value = dict(ddata)
                    out.append(m)
                return out

            col.stream.side_effect = _stream_docs
            return col

        client.collection.side_effect = _get_collection
        return client

    return {
        "storage_factory": _make_mock_storage_client,
        "firestore_factory": _make_mock_firestore_client,
        "gcs": gcs_store,
        "firestore": firestore_store,
        "access_logs": access_logs_store,
    }


_FREE_FORM_OUTLINE_JSON = (
    '{"slides": [{"slide_type": "cover", "title": "統合CDPによる顧客体験変革"},'
    ' {"slide_type": "challenges", "bullets": ["データ分断", "施策の属人化", "工数肥大"]}],'
    ' "theme_color": "sky"}'
)


def test_create_proposal_website_with_free_form_outline_issues_credentials_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the GE stall: a non-conforming deck_spec_json must never raise; credentials are issued first."""
    from app.agent import get_proposal_status
    from app.generation_worker import generate_presentation

    backends = _make_fake_backends()
    monkeypatch.setenv("GENERATION_TRIGGER_MODE", "none")
    monkeypatch.setenv("ENABLE_MANAGED_AGENTS_API", "false")
    monkeypatch.delenv("GENERATION_JOB_NAME", raising=False)
    mock_ctx = MagicMock()
    mock_ctx.state = {}

    class _OfflineGenAIClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.models = MagicMock()
            self.models.generate_content.side_effect = RuntimeError("offline unit test")

    with (
        patch("google.cloud.storage.Client", side_effect=backends["storage_factory"]),
        patch("google.cloud.firestore.Client", side_effect=backends["firestore_factory"]),
        patch("app.agent.genai.Client", _OfflineGenAIClient),
    ):
        created = create_proposal_website(
            client_name="株式会社サンプル商事",
            proposal_title="統合CDP×AIコンシェルジュによる顧客体験変革",
            proposal_brief="会員・EC・店舗データを統合し、AIで1to1接客を実現する",
            theme_color="sky",
            deck_spec_json=_FREE_FORM_OUTLINE_JSON,
            tool_context=mock_ctx,
        )
        # 1. Immediate issue of URL / ID / password while generation is pending
        assert created["status"] == "GENERATING"
        assert created["generation_status"] == "generating"
        pres_id = created["presentation_id"]
        assert created["share_url"].endswith(f"/p/{pres_id}")
        assert created["viewer_id"].startswith("client-")
        assert len(created["viewer_password"]) >= 12
        assert created["outline_hint_applied"] is True
        doc = backends["firestore"][pres_id]
        assert doc["generation_status"] == "generating"
        assert doc["status"] == "active" and doc["is_active"] is True
        assert "統合CDPによる顧客体験変革" in doc["generation_inputs"]["outline_hint"]
        assert not backends["gcs"]  # nothing rendered yet
        assert mock_ctx.state["published_result"]["presentation_id"] == pres_id

        # 2. Status tool reports generating (no stale repair yet)
        status_before = get_proposal_status(pres_id)
        assert status_before["generation_status"] == "generating"
        assert status_before["stale_repair_applied"] is False

        # 3. Background worker finalizes (offline -> deterministic tier) and flips to ready
        worker_result = generate_presentation(pres_id)
        assert worker_result["generation_status"] == "ready"
        assert worker_result["generation_engine"].startswith("deterministic_skill_template")
        doc = backends["firestore"][pres_id]
        assert doc["generation_status"] == "ready"
        assert doc["deck_spec"]["client_name"] == "株式会社サンプル商事"
        html = next(iter(backends["gcs"].values())).decode("utf-8")
        assert validate_rendered_html(html) is True
        assert "株式会社サンプル商事" in html

        # 4. Status tool now reports ready with a human-readable engine label
        status_after = get_proposal_status(pres_id)
        assert status_after["generation_status"] == "ready"
        assert "テンプレート" in status_after["generation_engine_label"]

        # 5. Worker is idempotent-ish: a second run on a ready doc keeps it ready
        again = generate_presentation(pres_id)
        assert again["generation_status"] == "ready"

        # 6. Listing exposes generation fields
        listed = list_proposal_websites(client_filter="サンプル商事")
        assert listed["presentations"][0]["generation_status"] == "ready"


def test_get_proposal_status_repairs_stale_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent import get_proposal_status

    backends = _make_fake_backends()
    stale_requested = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
    ).isoformat()
    pres_id = "prop-20261003-stale001"
    backends["firestore"][pres_id] = {
        "presentation_id": pres_id,
        "viewer_id": "client-stale-0001",
        "password_hash": "x",
        "password_salt": "y",
        "gcs_bucket": "test-bucket",
        "gcs_blob_path": f"presentations/{pres_id}/index.html",
        "client_name": "株式会社サンプル商事",
        "proposal_title": "ご提案",
        "theme_color": "violet",
        "status": "active",
        "is_active": True,
        "generation_status": "generating",
        "generation_phase": "managed_agents",
        "generation_requested_at": stale_requested,
        "generation_inputs": {
            "client_name": "株式会社サンプル商事",
            "proposal_title": "ご提案",
            "proposal_brief": "概要",
            "theme_color": "violet",
        },
    }
    monkeypatch.setenv("GENERATION_STALE_MINUTES", "13")
    with (
        patch("google.cloud.storage.Client", side_effect=backends["storage_factory"]),
        patch("google.cloud.firestore.Client", side_effect=backends["firestore_factory"]),
    ):
        status = get_proposal_status(pres_id)
    assert status["stale_repair_applied"] is True
    assert status["generation_status"] == "ready"
    assert status["generation_engine"].startswith("deterministic_skill_template:stale_after_")
    assert backends["firestore"][pres_id]["generation_status"] == "ready"
    html = backends["gcs"][f"test-bucket/presentations/{pres_id}/index.html"].decode("utf-8")
    assert 'data-theme="violet"' in html


def test_tools_return_error_payloads_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent import get_proposal_status

    backends = _make_fake_backends()
    monkeypatch.setenv("GENERATION_TRIGGER_MODE", "none")
    with (
        patch("google.cloud.storage.Client", side_effect=backends["storage_factory"]),
        patch("google.cloud.firestore.Client", side_effect=backends["firestore_factory"]),
    ):
        assert create_proposal_website()["status"] == "ERROR"
        assert edit_proposal_website("prop-missing", "タイトル変更")["status"] == "NOT_FOUND"
        assert get_proposal_access_logs("prop-missing")["status"] == "NOT_FOUND"
        assert manage_proposal_credentials("prop-missing")["status"] == "NOT_FOUND"
        assert delete_proposal_website("prop-missing")["status"] == "NOT_FOUND"
        assert get_proposal_status("prop-missing")["status"] == "NOT_FOUND"

        # Editing while generating is refused gracefully
        backends["firestore"]["prop-gen"] = {
            "presentation_id": "prop-gen",
            "generation_status": "generating",
            "generation_phase": "managed_agents",
        }
        res = edit_proposal_website("prop-gen", "色を変えて")
        assert res["status"] == "GENERATING"

    # A storage outage inside the fast path surfaces as ERROR, not an exception
    spec = _sample_deck_spec()
    with (
        patch("google.cloud.storage.Client", side_effect=RuntimeError("gcs down")),
        patch("google.cloud.firestore.Client", side_effect=backends["firestore_factory"]),
    ):
        res = create_proposal_website(deck_spec_json=spec.model_dump_json())
        assert res["status"] == "ERROR"
        assert res["error_type"] == "RuntimeError"


def test_gateway_generating_page_status_endpoint_and_auto_switch() -> None:
    spec = _sample_deck_spec()
    html_bytes = render_deck_html(spec).encode("utf-8")
    pw = "TestPass-654321"
    pw_hash, pw_salt = hash_password(pw)
    pres_id = "prop-20261003-gen00001"
    viewer_id = "client-sample-cd34"
    fake_doc: dict[str, Any] = {
        "presentation_id": pres_id,
        "viewer_id": viewer_id,
        "password_hash": pw_hash,
        "password_salt": pw_salt,
        "gcs_bucket": "test-bucket",
        "gcs_blob_path": f"presentations/{pres_id}/index.html",
        "client_name": spec.client_name,
        "proposal_title": spec.proposal_title,
        "expires_at": (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
        ).isoformat(),
        "status": "active",
        "is_active": True,
        "generation_status": "generating",
        "generation_phase": "managed_agents",
        "generation_requested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    gcs_calls: list[str] = []

    def _fake_fetch(bucket: str, path: str) -> bytes:
        gcs_calls.append(path)
        return html_bytes

    with (
        patch.object(gateway_main, "_get_firestore_doc", side_effect=lambda _: fake_doc),
        patch.object(gateway_main, "_fetch_html_from_gcs", side_effect=_fake_fetch),
        patch.object(gateway_main, "_record_access_log", side_effect=lambda *a, **k: None),
    ):
        client = TestClient(gateway_main.app)
        good_b64 = base64.b64encode(f"{viewer_id}:{pw}".encode()).decode()

        # Unauthenticated status -> 401 JSON (never leaks state)
        assert client.get(f"/p/{pres_id}/status").status_code == 401

        # Authenticated page while generating -> 200 interim page with polling script, GCS untouched
        r_gen = client.get(f"/p/{pres_id}", headers={"Authorization": f"Basic {good_b64}"})
        assert r_gen.status_code == 200
        assert "AIが提案プレゼンテーションを生成しています" in r_gen.text
        assert f"/p/{pres_id}/status" in r_gen.text or "/status?ts=" in r_gen.text
        assert "location.reload()" in r_gen.text
        assert gcs_calls == []
        cookie_key = gateway_main._get_cookie_name(pres_id)
        assert cookie_key in r_gen.cookies

        # Cookie-authenticated status endpoint -> JSON generating
        client.cookies.set(cookie_key, r_gen.cookies[cookie_key])
        r_status = client.get(f"/p/{pres_id}/status")
        assert r_status.status_code == 200
        assert r_status.headers["cache-control"] == "no-store, private"
        assert r_status.json()["generation_status"] == "generating"
        assert r_status.json()["generation_phase"] == "managed_agents"

        # Worker flips Firestore to ready -> status says ready and the same URL now streams the deck
        fake_doc["generation_status"] = "ready"
        fake_doc["generation_engine"] = "managed_agents_api:antigravity-preview-05-2026"
        assert client.get(f"/p/{pres_id}/status").json()["generation_status"] == "ready"
        r_ready = client.get(f"/p/{pres_id}")
        assert r_ready.status_code == 200
        assert spec.client_name in r_ready.text
        assert gcs_calls == [f"presentations/{pres_id}/index.html"]

        # Failed generation -> 503 explanatory page
        fake_doc["generation_status"] = "failed"
        fake_doc["generation_error"] = "boom"
        r_failed = client.get(f"/p/{pres_id}")
        assert r_failed.status_code == 503
        assert "生成に失敗しました" in r_failed.text


def test_managed_agents_tier_polls_parses_fenced_json_and_falls_back_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent import synthesize_deck_spec_with_skill

    monkeypatch.setenv("ENABLE_MANAGED_AGENTS_API", "true")
    monkeypatch.setenv("MANAGED_AGENT_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("GENAI_LOCATION", "global")
    valid_json = _sample_deck_spec(theme_color="amber").model_dump_json()
    calls: dict[str, Any] = {"create": [], "get": 0, "cancel": 0, "generate": 0}

    class _Interaction:
        def __init__(self, status: str, output_text: str = "") -> None:
            self.id = "int-123"
            self.status = status
            self.output_text = output_text
            self.errors = None

    class _InteractionsAPI:
        def __init__(self, mode: str) -> None:
            self.mode = mode
            self.polls = 0

        def create(self, **kwargs: Any) -> _Interaction:
            calls["create"].append(kwargs)
            return _Interaction("in_progress")

        def get(self, interaction_id: str, **kwargs: Any) -> _Interaction:
            calls["get"] += 1
            self.polls += 1
            if self.mode == "hang":
                return _Interaction("in_progress")
            if self.polls < 2:
                return _Interaction("in_progress")
            return _Interaction("completed", "設計が完了しました。\n```json\n" + valid_json + "\n```")

        def cancel(self, interaction_id: str) -> None:
            calls["cancel"] += 1

    class _Models:
        def generate_content(self, **kwargs: Any) -> Any:
            calls["generate"] += 1
            m = MagicMock()
            m.text = valid_json
            return m

    mode_holder = {"mode": "ok"}

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.interactions = _InteractionsAPI(mode_holder["mode"])
            self.models = _Models()

    with patch("app.agent.genai.Client", _FakeClient):
        # (a) Managed Agents completes -> parsed from fenced JSON, correct request shape
        deck, engine = synthesize_deck_spec_with_skill(
            client_name="株式会社サンプル商事",
            proposal_title="提案",
            proposal_brief="概要",
            theme_color="amber",
            outline_hint="スライド2は3つの課題",
        )
        assert engine == "managed_agents_api:antigravity-preview-05-2026"
        assert deck.theme_color == "amber"
        req = calls["create"][0]
        assert req["agent"] == "antigravity-preview-05-2026"
        assert req["background"] is True and req["store"] is True and req["stream"] is False
        assert req["environment"] == {"type": "remote"}
        assert "スライド2は3つの課題" in req["input"]
        assert "config" not in req  # the old (broken) kwarg must never be sent
        assert calls["generate"] == 0

        # (b) Managed Agents exceeds the time budget -> cancelled, fast Gemini tier used
        mode_holder["mode"] = "hang"
        deck2, engine2 = synthesize_deck_spec_with_skill(
            client_name="株式会社サンプル商事",
            proposal_title="提案",
            proposal_brief="概要",
            theme_color="rose",
            managed_agent_deadline_seconds=1,
        )
    assert engine2.startswith("agent_platform_gemini_with_skill:")
    assert deck2.theme_color == "rose"
    assert calls["cancel"] >= 1
