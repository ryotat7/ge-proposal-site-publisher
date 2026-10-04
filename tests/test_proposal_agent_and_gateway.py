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
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "proposal_agent"))
sys.path.insert(0, str(ROOT_DIR / "hosting_gateway"))

from app.agent import (  # noqa: E402
    ArchitectureNode,
    ChallengeItem,
    CxHighlight,
    PresentationDeckSpec,
    RoadmapPhase,
    create_proposal_website,
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
import main as gateway_main  # noqa: E402


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
                phase_name="Phase 1: データ統合・実証検証",
                period="Month 1 - 2",
                deliverables=[
                    "カスタマージャーニー設計と優先ユースケース定義",
                    "BigQuery CDPへの初期データ連携とAgent Search構築",
                    "AIエージェントのプロトタイプ実装・社内検証",
                ],
                milestone="プロトタイプ合意・実証効果の測定完了",
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
        # 1. Create / publish
        created = publish_presentation(mock_ctx)
        pres_id = created["presentation_id"]
        assert created["status"] == "PUBLISHED"
        assert mock_ctx.state["published_result"]["presentation_id"] == pres_id
        assert mock_ctx.state["published_presentation"]["presentation_id"] == pres_id
        assert firestore_store[pres_id]["status"] == "active"
        assert firestore_store[pres_id]["is_active"] is True

        # Seed a simulated access log
        access_logs_store[pres_id] = [
            {
                "accessed_at": "2026-10-03T04:00:00+00:00",
                "viewer_id": created["viewer_id"],
                "auth_method": "basic_auth",
                "ip_address": "203.0.113.10",
                "user_agent": "Mozilla/5.0",
            }
        ]

        # 2. Edit presentation (change title, theme color to emerald, and callout)
        edited = edit_proposal_website(
            presentation_id=pres_id,
            edit_instructions="テーマカラーをemeraldに変更し、タイトルを改訂版に更新",
            new_title="【改訂版】AIエージェント×統合CDPによるOMO顧客体験変革",
            new_theme_color="emerald",
            new_custom_callout="経営会議フィードバック反映済み",
            tool_context=mock_ctx,
        )
        assert edited["status"] == "UPDATED"
        assert edited["existing_html_loaded"] is True
        assert edited["theme_color"] == "emerald"
        assert "【改訂版】" in edited["proposal_title"]
        blob_key = next(iter(gcs_store.keys()))
        updated_html = gcs_store[blob_key].decode("utf-8")
        assert validate_rendered_html(updated_html) is True
        assert 'data-theme="emerald"' in updated_html
        assert "【改訂版】AIエージェント×統合CDPによるOMO顧客体験変革" in updated_html
        assert "経営会議フィードバック反映済み" in updated_html

        # 3. List presentations
        listed = list_proposal_websites(client_filter="アクメ")
        assert listed["count"] == 1
        assert listed["presentations"][0]["presentation_id"] == pres_id
        assert listed["presentations"][0]["access_log_count"] == 1

        # 4. Get access logs
        logs_res = get_proposal_access_logs(pres_id)
        assert logs_res["total_access_count"] == 1
        assert logs_res["access_logs"][0]["auth_method"] == "basic_auth"

        # 5. Rotate credentials & extend expiration
        old_pw = created["viewer_password"]
        cred_res = manage_proposal_credentials(
            presentation_id=pres_id,
            rotate_password=True,
            new_viewer_id="client-acme-vip",
            extend_days=30,
            tool_context=mock_ctx,
        )
        assert cred_res["status"] == "CREDENTIALS_UPDATED"
        assert cred_res["viewer_id"] == "client-acme-vip"
        new_pw = cred_res["new_viewer_password"]
        assert new_pw != old_pw
        assert verify_password(
            new_pw,
            firestore_store[pres_id]["password_hash"],
            firestore_store[pres_id]["password_salt"],
        )

        # 6. Delete / revoke presentation
        revoked = delete_proposal_website(
            presentation_id=pres_id,
            hard_delete_gcs=True,
            tool_context=mock_ctx,
        )
        assert revoked["status"] == "REVOKED"
        assert revoked["is_active"] is False
        assert revoked["gcs_blob_deleted"] is True
        assert firestore_store[pres_id]["status"] == "revoked"
        assert firestore_store[pres_id]["is_active"] is False


def test_gateway_auth_flows_401_basic_cookie_and_revoked_403() -> None:
    spec = _sample_deck_spec()
    html_bytes = render_deck_html(spec).encode("utf-8")
    pw = "TestPass-123456"
    pw_hash, pw_salt = hash_password(pw)
    pres_id = "prop-20261003-test0001"
    viewer_id = "client-acme-ab12"

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
    }
    audit_logs: list[dict[str, Any]] = []

    with (
        patch.object(gateway_main, "_get_firestore_doc", side_effect=lambda _: fake_doc),
        patch.object(gateway_main, "_fetch_html_from_gcs", return_value=html_bytes),
        patch.object(
            gateway_main,
            "_record_access_log",
            side_effect=lambda pid, vid, method, req: audit_logs.append(
                {"pid": pid, "vid": vid, "method": method}
            ),
        ),
    ):
        client = TestClient(gateway_main.app)

        # 0. Health check endpoints (/health and /healthz)
        assert client.get("/health").status_code == 200
        assert client.get("/healthz").status_code == 200

        # 1. Unauthenticated GET -> 401 with WWW-Authenticate & login form
        r_unauth = client.get(f"/p/{pres_id}")
        assert r_unauth.status_code == 401
        assert "Basic" in r_unauth.headers.get("www-authenticate", "")
        assert "Secure Proposal Portal" in r_unauth.text

        # 2. Wrong Basic Auth -> 401
        bad_b64 = base64.b64encode(f"{viewer_id}:wrong".encode()).decode()
        r_bad = client.get(
            f"/p/{pres_id}", headers={"Authorization": f"Basic {bad_b64}"}
        )
        assert r_bad.status_code == 401

        # 3. Valid Basic Auth -> 200 OK + HTML deck + audit log
        good_b64 = base64.b64encode(f"{viewer_id}:{pw}".encode()).decode()
        r_good = client.get(
            f"/p/{pres_id}", headers={"Authorization": f"Basic {good_b64}"}
        )
        assert r_good.status_code == 200
        assert "株式会社アクメリテールホールディングス" in r_good.text
        assert len(audit_logs) == 1
        assert audit_logs[0]["method"] == "basic_auth"

        # 4. Form POST Login -> 303 Redirect + Session Cookie -> Subsequent GET 200 OK
        client.cookies.clear()
        r_post = client.post(
            f"/p/{pres_id}/auth",
            data={"viewer_id": viewer_id, "password": pw},
            follow_redirects=False,
        )
        assert r_post.status_code == 303
        cookie_key = gateway_main._get_cookie_name(pres_id)
        assert cookie_key in r_post.cookies

        client.cookies.set(cookie_key, r_post.cookies[cookie_key])
        r_cookie = client.get(f"/p/{pres_id}")
        assert r_cookie.status_code == 200
        assert "AIエージェント×統合CDPによる次世代OMO顧客体験変革" in r_cookie.text
        assert audit_logs[-1]["method"] == "session_cookie"

        # 5. Revoked status -> 403 Forbidden even with valid credentials
        fake_doc["status"] = "revoked"
        r_revoked = client.get(
            f"/p/{pres_id}", headers={"Authorization": f"Basic {good_b64}"}
        )
        assert r_revoked.status_code == 403


def test_login_html_escapes_xss_payloads() -> None:
    rendered = gateway_main._render_login_html(
        presentation_id='prop-1"><script>alert(1)</script>',
        client_name='<img src=x onerror=alert(1)>',
        error_message='<script>evil()</script>',
    )
    assert "<script>alert(1)</script>" not in rendered
    assert "<img src=x onerror=alert(1)>" not in rendered
    assert "<script>evil()</script>" not in rendered
    assert "&lt;script&gt;evil()&lt;/script&gt;" in rendered


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

        # 1. stream_query automatically routes to async_stream_query
        r1 = client.post(
            "/api/stream_reasoning_engine",
            json={"class_method": "stream_query", "input": {"message": "hello"}},
        )
        assert r1.status_code == 200
        assert '"async_chunk"' in r1.text

        # 2. sync-only generator also streams without TypeError
        r2 = client.post(
            "/api/stream_reasoning_engine",
            json={"class_method": "sync_only_stream", "input": {"message": "sync"}},
        )
        assert r2.status_code == 200
        assert '"sync_chunk"' in r2.text


def test_genai_location_routing_and_global_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent import _get_genai_location, synthesize_deck_spec_with_skill

    # Default gemini-3.8-flash routes to global even when GOOGLE_CLOUD_LOCATION is regional
    monkeypatch.delenv("GENAI_LOCATION", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    assert _get_genai_location("gemini-3.8-flash") == "global"

    # Explicit GENAI_LOCATION overrides regional default
    monkeypatch.setenv("GENAI_LOCATION", "europe-west1")
    assert _get_genai_location("gemini-3.8-flash") == "europe-west1"

    # Verify automatic retry on "global" when regional endpoint raises an error
    attempted_locations: list[str] = []
    valid_spec_json = _sample_deck_spec().model_dump_json()

    class _FakeModels:
        def __init__(self, loc: str) -> None:
            self.loc = loc

        def generate_content(self, **kwargs: Any) -> Any:
            if self.loc != "global":
                raise RuntimeError(f"Model not found in regional endpoint {self.loc}")
            m = MagicMock()
            m.text = valid_spec_json
            return m

    class _FakeGenAIClient:
        def __init__(self, *, vertexai: bool, project: str, location: str) -> None:
            attempted_locations.append(location)
            self.models = _FakeModels(location)

    with patch("app.agent.genai.Client", _FakeGenAIClient):
        spec, engine = synthesize_deck_spec_with_skill(
            client_name="株式会社アクメリテールホールディングス",
            proposal_title="OMO顧客体験変革",
            proposal_brief="AIコンシェルジュと統合CDPの構築",
            theme_color="emerald",
        )
    assert attempted_locations == ["europe-west1", "global"]
    assert engine.startswith("agent_platform_gemini_with_skill:")
    assert spec.theme_color == "emerald"




# ---------------------------------------------------------------------------
# Asynchronous generation flow (immediate credentials -> 生成中 page -> auto switch)
# ---------------------------------------------------------------------------


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
        "generation_phase": "gemini_fast",
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
            "generation_phase": "gemini_fast",
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
        "generation_phase": "gemini_fast",
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
        assert r_status.json()["generation_phase"] == "gemini_fast"

        # Worker flips Firestore to ready -> status says ready and the same URL now streams the deck
        fake_doc["generation_status"] = "ready"
        fake_doc["generation_engine"] = "agent_platform_gemini_with_skill:gemini-3.8-flash"
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


def test_engine_labels_name_the_adk_designer() -> None:
    from app.agent import describe_edit_engine, describe_generation_engine

    assert "ADK エージェント（gemini-3.8-flash）" in describe_generation_engine("adk_freeform:gemini-3.8-flash")
    assert "ADK エージェント（gemini-3.8-flash）" in describe_edit_engine("adk_freeform:gemini-3.8-flash")
    assert describe_generation_engine("agent_platform_gemini_with_skill:gemini-3.8-flash").startswith("gemini-3.8-flash")
    assert describe_generation_engine("unknown_engine:x") == "unknown_engine:x"


def _create_ready_presentation(backends: dict[str, Any], theme: str = "sky") -> str:
    spec = _sample_deck_spec(theme)
    created = create_proposal_website(deck_spec_json=spec.model_dump_json(), theme_color=theme)
    assert created["status"] == "PUBLISHED", created
    pres_id = created["presentation_id"]
    doc = backends["firestore"][pres_id]
    assert doc["generation_status"] == "ready"
    assert doc["content_version"] == 1
    assert doc["design_style"] == "immersive-dark"
    return pres_id


def _index_html(backends: dict[str, Any], pres_id: str) -> str:
    doc = backends["firestore"][pres_id]
    return backends["gcs"][f"{doc['gcs_bucket']}/{doc['gcs_blob_path']}"].decode("utf-8")


def test_design_styles_render_light_editorial_dark_and_aliases() -> None:
    from app.agent import normalize_design_style

    spec = _sample_deck_spec("emerald")
    spec.design_style = "clean-light"
    light = render_deck_html(spec)
    assert validate_rendered_html(light) is True
    assert '<body class="relative" data-theme="emerald" data-style="clean-light">' in light
    assert "bg-slate-950/80" not in light
    assert "blur-3xl" not in light
    assert "Noto+Serif+JP" not in light

    spec.design_style = "editorial-light"
    editorial = render_deck_html(spec)
    assert validate_rendered_html(editorial) is True
    assert 'data-style="editorial-light"' in editorial
    assert "Noto+Serif+JP" in editorial

    spec.design_style = "immersive-dark"
    dark = render_deck_html(spec)
    assert validate_rendered_html(dark) is True
    assert 'data-style="immersive-dark"' in dark
    assert "bg-grid-pattern" in dark

    assert normalize_design_style("white") == "clean-light"
    assert normalize_design_style("Light mode") == "clean-light"
    assert normalize_design_style("Editorial") == "editorial-light"
    assert normalize_design_style("unknown-style") == "immersive-dark"
    spec.design_style = "neon"
    assert 'data-style="immersive-dark"' in render_deck_html(spec)


def test_custom_css_sanitizer_neutralises_hostile_payloads() -> None:
    from app.agent import sanitize_custom_css

    hostile = (
        "/* comment */ body { color: #111; } </style><script>alert(1)</script>"
        '@import url("https://evil.example/x.css"); '
        ".a { background: url(javascript:alert(1)); } "
        ".b { background: \\75 rl(https://evil.example/y.png); } "
        ".c { width: expression(alert(1)); -moz-binding: x; } {{ injected }} "
        '[data-slide-index="0"] h2 { letter-spacing: .02em; }'
    )
    cleaned = sanitize_custom_css(hostile)
    lowered = cleaned.lower()
    assert "<" not in cleaned and "\\" not in cleaned
    assert "@import" not in lowered
    assert "url(" not in lowered
    assert "javascript:" not in lowered
    assert "expression(" not in lowered
    assert "-moz-binding" not in lowered
    assert "{{" not in cleaned and "}}" not in cleaned
    assert "letter-spacing: .02em" in cleaned
    assert sanitize_custom_css("a{color:red}" * 2000).endswith("}")
    assert len(sanitize_custom_css("a{color:red}" * 2000)) <= 8000

    spec = _sample_deck_spec()
    spec.design_style = "clean-light"
    spec.custom_css = hostile
    rendered = render_deck_html(spec)
    # A CSS selector mentioning data-slide-index must not confuse the DOM validator.
    assert validate_rendered_html(rendered) is True
    assert "<script>alert(1)" not in rendered
    assert "evil.example/x.css" not in rendered or "@import" not in rendered.lower()


def test_edit_llm_white_redesign_marks_updating_and_reports_verified_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: 「背景色が白でぜんぜん違う見た目に」 must visibly change the deck and be reported truthfully."""
    from app.agent import get_proposal_status

    backends = _make_fake_backends()
    monkeypatch.setenv("GENERATION_TRIGGER_MODE", "none")
    monkeypatch.setenv("ENABLE_LLM_DECK_EDIT", "true")
    holder: dict[str, str] = {}
    observed: list[str] = []

    class _FakeGenAIClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.models = MagicMock()
            self.models.generate_content.side_effect = self._generate

        @staticmethod
        def _generate(*args: Any, **kwargs: Any) -> MagicMock:
            doc = backends["firestore"][holder["pres_id"]]
            observed.extend([doc["generation_status"], doc["generation_phase"]])
            new_deck = PresentationDeckSpec.model_validate(doc["deck_spec"])
            new_deck.design_style = "clean-light"
            new_deck.theme_color = "violet"
            new_deck.custom_css = ".glass-card { border-radius: 2px; }"
            resp = MagicMock()
            resp.text = json.dumps(
                {
                    "deck_spec": new_deck.model_dump(),
                    "change_summary": ["白基調のクリーンなデザインに刷新"],
                    "unsupported_requests": [],
                },
                ensure_ascii=False,
            )
            return resp

    with (
        patch("google.cloud.storage.Client", side_effect=backends["storage_factory"]),
        patch("google.cloud.firestore.Client", side_effect=backends["firestore_factory"]),
        patch("app.agent.genai.Client", _FakeGenAIClient),
    ):
        pres_id = _create_ready_presentation(backends)
        holder["pres_id"] = pres_id
        dark_html = _index_html(backends, pres_id)
        assert 'data-style="immersive-dark"' in dark_html

        res = edit_proposal_website(pres_id, "背景色が白でぜんぜん違う見た目のプレゼンテーションに変えて")
        assert res["status"] == "UPDATED", res
        # While the model was working, the deck was marked "updating" (drives the 更新中 banner).
        assert observed[:2] == ["updating", "edit_designing"]
        assert res["design_style"] == "clean-light"
        assert res["theme_color"] == "violet"
        assert res["content_version"] == 2
        assert res["edit_engine"].startswith("gemini:")
        assert any(c.startswith("デザインスタイル") for c in res["verified_changes"])
        assert any(c.startswith("アクセントカラー") for c in res["verified_changes"])
        assert any(c.startswith("カスタムCSS") for c in res["verified_changes"])
        assert res["previous_version_saved"] is True

        doc = backends["firestore"][pres_id]
        assert doc["generation_status"] == "ready"
        assert doc["content_version"] == 2
        assert doc["design_style"] == "clean-light"
        assert doc["edit_request"] is None
        assert doc["previous_deck_spec"]["design_style"] == "immersive-dark"
        assert doc["last_edit_result"]["status"] == "applied"

        new_html = _index_html(backends, pres_id)
        assert validate_rendered_html(new_html) is True
        assert 'data-style="clean-light"' in new_html
        assert 'data-theme="violet"' in new_html
        assert "border-radius: 2px" in new_html
        version_keys = [k for k in backends["gcs"] if f"presentations/{pres_id}/versions/" in k]
        assert len(version_keys) == 1 and version_keys[0].endswith("-v1.html")
        assert backends["gcs"][version_keys[0]].decode("utf-8") == dark_html

        status = get_proposal_status(pres_id)
        assert status["generation_status"] == "ready"
        assert status["content_version"] == 2
        assert status["design_style"] == "clean-light"
        assert status["last_edit_result"]["status"] == "applied"
        assert "反映済み" in status["user_message"]


def test_edit_heuristics_without_llm_never_fake_changes_and_support_undo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backends = _make_fake_backends()
    monkeypatch.setenv("GENERATION_TRIGGER_MODE", "none")
    monkeypatch.setenv("ENABLE_LLM_DECK_EDIT", "false")
    with (
        patch("google.cloud.storage.Client", side_effect=backends["storage_factory"]),
        patch("google.cloud.firestore.Client", side_effect=backends["firestore_factory"]),
    ):
        pres_id = _create_ready_presentation(backends)

        white = edit_proposal_website(pres_id, "背景色を白にして")
        assert white["status"] == "UPDATED"
        assert white["design_style"] == "clean-light"
        assert any(c.startswith("デザインスタイル") for c in white["verified_changes"])
        assert 'data-style="clean-light"' in _index_html(backends, pres_id)

        before_vague = _index_html(backends, pres_id)
        callout_before = backends["firestore"][pres_id]["deck_spec"]["custom_callout"]
        vague = edit_proposal_website(pres_id, "もう少しいい感じにして")
        assert vague["status"] == "NO_CHANGE"
        assert vague["verified_changes"] == []
        assert vague["unsupported_requests"]
        assert vague["content_version"] == 2
        assert _index_html(backends, pres_id) == before_vague
        doc = backends["firestore"][pres_id]
        assert doc["deck_spec"]["custom_callout"] == callout_before  # no instruction text stuffed into the deck
        assert doc["generation_status"] == "ready"
        assert doc["last_edit_result"]["status"] == "no_change"

        dark = edit_proposal_website(pres_id, "やっぱりダークに戻して、アクセントはvioletで")
        assert dark["status"] == "UPDATED"
        assert dark["design_style"] == "immersive-dark"
        assert dark["theme_color"] == "violet"
        assert dark["content_version"] == 3

        undone = edit_proposal_website(pres_id, undo_last_edit=True)
        assert undone["status"] == "UPDATED"
        assert undone["design_style"] == "clean-light"
        assert undone["theme_color"] == "sky"
        assert undone["content_version"] == 4
        assert 'data-style="clean-light"' in _index_html(backends, pres_id)

        explicit = edit_proposal_website(pres_id, new_design_style="editorial", new_theme_color="amber")
        assert explicit["status"] == "UPDATED"
        assert explicit["design_style"] == "editorial-light"
        assert explicit["theme_color"] == "amber"

        bad = edit_proposal_website(pres_id, new_theme_color="gold-ish")
        assert bad["status"] == "NO_CHANGE"
        assert bad["unsupported_requests"]


def test_edit_busy_lock_stale_repair_and_failure_keeps_previous_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent import get_proposal_status

    backends = _make_fake_backends()
    monkeypatch.setenv("GENERATION_TRIGGER_MODE", "none")
    monkeypatch.setenv("ENABLE_LLM_DECK_EDIT", "false")
    with (
        patch("google.cloud.storage.Client", side_effect=backends["storage_factory"]),
        patch("google.cloud.firestore.Client", side_effect=backends["firestore_factory"]),
    ):
        pres_id = _create_ready_presentation(backends)
        doc = backends["firestore"][pres_id]
        html_before = _index_html(backends, pres_id)

        # A fresh "updating" lock -> BUSY, nothing touched.
        doc.update(
            {
                "generation_status": "updating",
                "generation_phase": "edit_designing",
                "edit_requested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )
        busy = edit_proposal_website(pres_id, "背景を白に")
        assert busy["status"] == "BUSY"
        assert _index_html(backends, pres_id) == html_before
        live = get_proposal_status(pres_id)
        assert live["generation_status"] == "updating"
        assert live["edit_stale_repair_applied"] is False
        assert "更新中" in live["user_message"]

        # An abandoned lock (crashed edit) is repaired by the status tool ...
        doc["edit_requested_at"] = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
        ).isoformat()
        repaired = get_proposal_status(pres_id)
        assert repaired["edit_stale_repair_applied"] is True
        assert repaired["generation_status"] == "ready"
        assert doc["generation_status"] == "ready"
        assert doc["last_edit_result"]["status"] == "failed"

        # ... and never blocks a new edit either.
        doc.update({"generation_status": "updating", "edit_requested_at": doc["last_edit_result"]["applied_at"]})
        doc["edit_requested_at"] = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
        ).isoformat()
        ok = edit_proposal_website(pres_id, "背景を白に")
        assert ok["status"] == "UPDATED"
        assert ok["design_style"] == "clean-light"
        html_ok = _index_html(backends, pres_id)
        version_ok = doc["content_version"]

        # Render failure -> EDIT_FAILED, previous HTML/version kept, lock released.
        with patch("app.agent.render_deck_html", side_effect=RuntimeError("render boom")):
            failed = edit_proposal_website(pres_id, "アクセントを紫にして")
        assert failed["status"] == "EDIT_FAILED"
        assert failed["verified_changes"] == []
        assert _index_html(backends, pres_id) == html_ok
        assert doc["generation_status"] == "ready"
        assert doc["content_version"] == version_ok
        assert doc["last_edit_result"]["status"] == "failed"


def test_worker_keeps_requested_design_style_and_bumps_content_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_worker import generate_presentation

    backends = _make_fake_backends()
    monkeypatch.setenv("GENERATION_TRIGGER_MODE", "none")
    monkeypatch.delenv("GENERATION_JOB_NAME", raising=False)

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
            proposal_title="統合CDP×AIによる顧客体験変革",
            proposal_brief="会員・EC・店舗データを統合する",
            design_style="white",
        )
        assert created["status"] == "GENERATING"
        pres_id = created["presentation_id"]
        doc = backends["firestore"][pres_id]
        assert doc["design_style"] == "clean-light"
        assert doc["content_version"] == 0
        assert generate_presentation(pres_id)["generation_status"] == "ready"
        assert doc["design_style"] == "clean-light"
        assert doc["content_version"] == 1
        assert doc["deck_spec"]["custom_css"] == ""
        assert 'data-style="clean-light"' in _index_html(backends, pres_id)


def test_gateway_live_update_watcher_updating_banner_and_stale_lock() -> None:
    spec = _sample_deck_spec()
    html_bytes = render_deck_html(spec).encode("utf-8")
    pw = "TestPass-777777"
    pw_hash, pw_salt = hash_password(pw)
    pres_id = "prop-20261004-live0001"
    viewer_id = "client-sample-ef56"
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
        "generation_status": "ready",
        "content_version": 3,
        "last_edit_result": {"status": "applied", "verified_changes": ["デザインスタイル: secret detail"]},
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
        auth = {"Authorization": f"Basic {good_b64}"}

        # Ready deck -> streamed with the watcher injected right before </body>
        r_ready = client.get(f"/p/{pres_id}", headers=auth)
        assert r_ready.status_code == 200
        body = r_ready.text
        assert spec.client_name in body
        assert 'id="pd-live-update"' in body
        assert 'data-content-version="3"' in body
        assert 'data-initial-status="ready"' in body
        assert "プレゼンテーションを更新中です" in body
        assert body.rfind('id="pd-live-update"') < body.lower().rfind("</body>")
        assert "innerHTML" not in body[body.find('id="pd-live-update"') :]
        assert r_ready.headers["x-content-type-options"] == "nosniff"

        status = client.get(f"/p/{pres_id}/status", headers=auth).json()
        assert status["generation_status"] == "ready"
        assert status["content_version"] == 3
        assert status["last_edit_status"] == "applied"
        assert "secret detail" not in json.dumps(status, ensure_ascii=False)

        # Edit in progress -> status says updating, the (previous) deck is still served with the banner armed
        fake_doc.update(
            {
                "generation_status": "updating",
                "generation_phase": "edit_designing",
                "edit_requested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )
        status_upd = client.get(f"/p/{pres_id}/status", headers=auth).json()
        assert status_upd["generation_status"] == "updating"
        assert status_upd["generation_phase"] == "edit_designing"
        r_upd = client.get(f"/p/{pres_id}", headers=auth)
        assert r_upd.status_code == 200
        assert 'data-initial-status="updating"' in r_upd.text
        assert 'data-initial-phase="edit_designing"' in r_upd.text

        # Abandoned lock -> treated as ready by the gateway (viewers are never stuck on 更新中)
        fake_doc["edit_requested_at"] = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
        ).isoformat()
        assert client.get(f"/p/{pres_id}/status", headers=auth).json()["generation_status"] == "ready"
        assert 'data-initial-status="ready"' in client.get(f"/p/{pres_id}", headers=auth).text
        assert len(gcs_calls) == 3

        # Kill switch
        with patch.dict("os.environ", {"LIVE_UPDATE_WATCHER": "false"}):
            assert 'id="pd-live-update"' not in client.get(f"/p/{pres_id}", headers=auth).text

    # Snippet attributes are escaped / allow-listed
    snippet = gateway_main._render_live_update_snippet(
        'x"><img src=x onerror=alert(1)>',
        {"content_version": "7", "generation_phase": '"><script>alert(1)</script>'},
        "updating",
    )
    assert "<img" not in snippet
    assert "<script>alert(1)" not in snippet
    assert 'data-content-version="7"' in snippet
    assert gateway_main._inject_live_update_watcher(b"<p>no body</p>", "<i>w</i>").endswith(b"<i>w</i>")


def test_concierge_instruction_requires_truthful_edit_reports() -> None:
    from app.agent import CONCIERGE_INSTRUCTION

    # ADK treats {name} as state placeholders -> the instruction must not contain braces.
    assert "{" not in CONCIERGE_INSTRUCTION and "}" not in CONCIERGE_INSTRUCTION
    assert "verified_changes" in CONCIERGE_INSTRUCTION
    assert "clean-light" in CONCIERGE_INSTRUCTION
    assert "更新中" in CONCIERGE_INSTRUCTION
    assert "（試算）" in CONCIERGE_INSTRUCTION and "search_internal_knowledge` の結果" in CONCIERGE_INSTRUCTION


def test_search_internal_knowledge_federates_drive_and_salesforce_datastores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent import _get_datastore_ids, search_internal_knowledge

    # Supports both comma-separated and colon-separated (deploy.sh safe) multi-datastore configs
    monkeypatch.setenv("AGENT_SEARCH_DATASTORE_ID", "drive-past-rfps-ds:salesforce-crm-ds, drive-past-rfps-ds")
    assert _get_datastore_ids() == ["drive-past-rfps-ds", "salesforce-crm-ds"]

    def _make_hit(doc_id: str, struct_data: dict[str, Any], derived_data: dict[str, Any]) -> MagicMock:
        hit = MagicMock()
        hit.document.id = doc_id
        hit.document.struct_data = struct_data
        hit.document.derived_struct_data = derived_data
        return hit

    drive_hit = _make_hit(
        "drive-doc-001",
        {},
        {
            "title": "【過去提案書】大手小売グループ様_OMO顧客体験基盤RFP回答書.pdf",
            "link": "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view",
            "snippets": [
                {"snippet": "店舗POS・EC・アプリの会員IDを統合し、購買直後のAI接客でリピート率を改善。"}
            ],
            "extractive_segments": [
                {"content": "Phase 1（2ヶ月）でBigQuery統合CDPとAgent Searchを構築し、CVR +28%を達成。"}
            ],
        },
    )
    sf_hit = _make_hit(
        "sf-opp-001",
        {
            "Name": "株式会社アクメリテール - 次世代OMO・AIコンシェルジュ刷新案件",
            "AccountName": "株式会社アクメリテール",
            "Industry": "小売・流通（オムニチャネル）",
            "StageName": "Proposal/Price Quote（提案・見積提示中）",
            "NextStep": "来週の経営会議向けに比較表付きインタラクティブWeb提案サイトを提出",
            "Description": "競合A社とコンペ中。デジタル承認ワークフローとBefore/After比較の明示が必須要件。",
            "Amount": "48,000,000 JPY",
        },
        {},
    )

    queried_configs: list[str] = []

    class _FakeSearchClient:
        def search(self, request: Any) -> MagicMock:
            queried_configs.append(request.serving_config)
            resp = MagicMock()
            if "drive-past-rfps-ds" in request.serving_config:
                resp.results = [drive_hit]
            elif "salesforce-crm-ds" in request.serving_config:
                resp.results = [sf_hit]
            else:
                resp.results = []
            return resp

    with patch("google.cloud.discoveryengine_v1.SearchServiceClient", return_value=_FakeSearchClient()):
        raw = search_internal_knowledge("小売 OMO アクメリテール")
    res = json.loads(raw)

    assert res["datastore_ids"] == ["drive-past-rfps-ds", "salesforce-crm-ds"]
    assert len(queried_configs) == 2
    assert len(res["matched_documents"]) == 2

    by_id = {r["id"]: r for r in res["matched_documents"]}
    drive_rec = by_id["drive-doc-001"]
    assert drive_rec["datastore_id"] == "drive-past-rfps-ds"
    assert drive_rec["source_type"] == "google_drive"
    assert drive_rec["source_uri"].startswith("https://drive.google.com/")
    assert "店舗POS・EC・アプリ" in drive_rec["summary"]
    assert "CVR +28%" in drive_rec["summary"]

    sf_rec = by_id["sf-opp-001"]
    assert sf_rec["datastore_id"] == "salesforce-crm-ds"
    assert sf_rec["source_type"] == "salesforce"
    assert sf_rec["deal_stage"] == "Proposal/Price Quote（提案・見積提示中）"
    assert "来週の経営会議" in sf_rec["recent_activity"]
    assert "48,000,000 JPY" in sf_rec["key_metrics"]
    assert "競合A社とコンペ中" in sf_rec["summary"]


def test_seed_datastore_preserves_real_connectors_and_binds_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.api_core import exceptions as gcp_exceptions

    sys.path.insert(0, str(ROOT_DIR / "infra"))
    import seed_datastore  # noqa: E402

    assert seed_datastore.parse_datastore_ids("drive-ds, salesforce-ds:drive-ds") == [
        "drive-ds",
        "salesforce-ds",
    ]
    assert (
        seed_datastore.resolve_engine_name("sample-gcp-project", "global", "my-ge-engine")
        == "projects/sample-gcp-project/locations/global/collections/default_collection/engines/my-ge-engine"
    )
    full_engine = "projects/111122223333/locations/global/collections/default_collection/engines/custom-eng"
    assert seed_datastore.resolve_engine_name("sample-gcp-project", "global", full_engine) == full_engine

    # Simulate an environment where drive-ds already exists (1P Google Drive connector)
    # and salesforce-ds does not exist yet.
    existing_stores: set[str] = {"drive-ds"}
    created_stores: list[str] = []
    upserted_docs: list[tuple[str, str]] = []
    engine_state: dict[str, Any] = {"dataStoreIds": ["legacy-ds"]}

    class _FakeDSClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_data_store(self, name: str) -> MagicMock:
            ds_id = name.rsplit("/", 1)[-1]
            if ds_id not in existing_stores:
                raise gcp_exceptions.NotFound(f"DataStore {ds_id} not found")
            return MagicMock()

        def create_data_store(self, parent: str, data_store: Any, data_store_id: str) -> MagicMock:
            created_stores.append(data_store_id)
            existing_stores.add(data_store_id)
            op = MagicMock()
            op.result.return_value = MagicMock()
            return op

    class _FakeDocClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def update_document(self, document: Any, allow_missing: bool = False) -> MagicMock:
            upserted_docs.append((document.name, document.id))
            return MagicMock()

    class _FakeAuthorizedSession:
        def __init__(self, credentials: Any) -> None:
            pass

        def get(self, url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.content = json.dumps(engine_state).encode("utf-8")
            resp.json.return_value = dict(engine_state)
            return resp

        def patch(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            json: dict[str, Any] | None = None,
            timeout: int = 30,
        ) -> MagicMock:
            assert json is not None
            engine_state["dataStoreIds"] = list(json["dataStoreIds"])
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = dict(engine_state)
            return resp

    backends = _make_fake_backends()
    monkeypatch.setenv("PROJECT_ID", "sample-gcp-project")
    monkeypatch.setenv("PROPOSAL_GCS_BUCKET", "test-bucket")
    monkeypatch.setenv("GE_APP_ID", "my-ge-engine")

    with (
        patch("google.cloud.storage.Client", side_effect=backends["storage_factory"]),
        patch("google.cloud.discoveryengine_v1.DataStoreServiceClient", _FakeDSClient),
        patch("google.cloud.discoveryengine_v1.DocumentServiceClient", _FakeDocClient),
        patch("google.auth.default", return_value=(MagicMock(), "sample-gcp-project")),
        patch("google.auth.transport.requests.AuthorizedSession", _FakeAuthorizedSession),
    ):
        # 1. SEED_MODE=real: existing drive-ds is preserved untouched; no synthetic docs are upserted
        monkeypatch.setenv("AGENT_SEARCH_DATASTORE_ID", "drive-ds")
        monkeypatch.setenv("SEED_MODE", "real")
        res_real = seed_datastore.seed_knowledge_files()
        assert res_real["existing_preserved"] == ["drive-ds"]
        assert res_real["synthetic_seeded"] == []
        assert upserted_docs == []
        assert res_real["engine_bound"] is True
        assert engine_state["dataStoreIds"] == ["legacy-ds", "drive-ds"]

        # 2. SEED_MODE=auto with drive-ds,salesforce-ds: drive-ds is preserved, salesforce-ds is created & seeded with CRM docs
        monkeypatch.setenv("AGENT_SEARCH_DATASTORE_ID", "drive-ds,salesforce-ds")
        monkeypatch.setenv("SEED_MODE", "auto")
        res_auto = seed_datastore.seed_knowledge_files()
        assert res_auto["existing_preserved"] == ["drive-ds"]
        assert res_auto["synthetic_seeded"] == ["salesforce-ds"]
        assert created_stores == ["salesforce-ds"]
        assert len(upserted_docs) >= 2
        assert all(doc_id.startswith("crm-") for _, doc_id in upserted_docs)
        assert engine_state["dataStoreIds"] == ["legacy-ds", "drive-ds", "salesforce-ds"]

        # 3. --bind-only mode only binds DataStores to GE_APP_ID without touching DataStores
        monkeypatch.setenv("AGENT_SEARCH_DATASTORE_ID", "drive-ds:salesforce-ds:extra-bq-ds")
        res_bind = seed_datastore.seed_knowledge_files(bind_only=True)
        assert res_bind["seed_mode"] == "bind_only"
        assert res_bind["engine_bound"] is True
        assert engine_state["dataStoreIds"] == ["legacy-ds", "drive-ds", "salesforce-ds", "extra-bq-ds"]


