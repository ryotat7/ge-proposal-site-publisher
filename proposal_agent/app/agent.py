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

"""Interactive Proposal HTML Website Concierge & Lifecycle Manager Agent (ADK on Agent Runtime)."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Any

from google import genai
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MANAGED_AGENT_MODEL = os.environ.get(
    "MANAGED_AGENT_MODEL", "antigravity-preview-05-2026"
)
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
SKILL_DIR = Path(__file__).resolve().parent / "skills" / "interactive-slide-designer"

SUPPORTED_THEME_COLORS = {"sky", "emerald", "violet", "amber", "rose"}


def _get_project_id() -> str:
    return (
        os.environ.get("PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or "your-gcp-project-id"
    )


def _get_location() -> str:
    return os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")


os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", _get_project_id())
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", _get_location())


def _get_bucket_name() -> str:
    return os.environ.get(
        "PROPOSAL_GCS_BUCKET", f"{_get_project_id()}-proposal-sites"
    )


def _get_firestore_collection() -> str:
    return os.environ.get("PROPOSAL_FIRESTORE_COLLECTION", "presentations")


def _get_firestore_client(project_id: str) -> Any:
    from google.cloud import firestore

    fs_client = firestore.Client(project=project_id)
    db_name = getattr(fs_client, "_database", "(default)") or "(default)"
    if not isinstance(db_name, str):
        db_name = "(default)"
    fs_client._database_string_internal = f"projects/{project_id}/databases/{db_name}"
    return fs_client


def _get_hosting_base_url() -> str:
    return os.environ.get(
        "HOSTING_BASE_URL",
        "https://proposal-hosting-gateway.example.run.app",
    ).rstrip("/")


def _get_datastore_id() -> str:
    return os.environ.get(
        "VERTEX_SEARCH_DATASTORE_ID", "proposal-knowledge-datastore"
    )


def _get_datastore_location() -> str:
    return os.environ.get("VERTEX_SEARCH_LOCATION", "global")


def _get_brand_name() -> str:
    return os.environ.get("PROPOSAL_BRAND_NAME", "Strategic AI Partners")


def _get_brand_badge() -> str:
    return os.environ.get("PROPOSAL_BRAND_BADGE", "SP")


# ---------------------------------------------------------------------------
# Skill Loader (interactive-slide-designer)
# ---------------------------------------------------------------------------


def load_interactive_slide_designer_skill() -> str:
    """Loads the bundled interactive-slide-designer SKILL.md and design_patterns.md."""
    parts: list[str] = []
    skill_md = SKILL_DIR / "SKILL.md"
    patterns_md = SKILL_DIR / "references" / "design_patterns.md"
    if skill_md.exists():
        parts.append(skill_md.read_text(encoding="utf-8"))
    if patterns_md.exists():
        parts.append(patterns_md.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Pydantic Schema for 6-Slide Bespoke Presentation Deck
# ---------------------------------------------------------------------------


class ChallengeItem(BaseModel):
    title: str = Field(description="課題の短い見出し（25文字以内）")
    description: str = Field(description="課題の具体的な内容と背景（80文字以内）")


class CxHighlight(BaseModel):
    title: str = Field(description="顧客体験（UX/AI）変革のポイント見出し")
    detail: str = Field(description="具体的な変革内容と効果（70文字以内）")


class ArchitectureNode(BaseModel):
    layer_name: str = Field(description="アーキテクチャ層の名称（例：1. 顧客接点チャネル層）")
    icon: str = Field(
        default="fa-layer-group",
        description="FontAwesomeアイコン名（例：fa-mobile-screen, fa-brain, fa-database, fa-cloud）",
    )
    components: list[str] = Field(
        description="この層に含まれる主要コンポーネント（2〜3個）"
    )
    description: str = Field(description="この層の役割とデータ連携の説明（60文字以内）")


class RoadmapPhase(BaseModel):
    phase_name: str = Field(description="フェーズ名（例：Phase 1: 基盤構築・PoC）")
    period: str = Field(description="期間目安（例：Month 1 - 2）")
    deliverables: list[str] = Field(description="主な実施事項・成果物（3項目）")
    milestone: str = Field(description="フェーズ完了時のマイルストーン")


class PresentationDeckSpec(BaseModel):
    client_name: str = Field(description="提案先クライアント企業名（『御中』『様』は含めない）")
    client_slug: str = Field(
        description="クライアント識別用の英数字スラッグ（例：acme-retail, apex-financial）"
    )
    proposal_title: str = Field(description="提案書メインタイトル（45文字以内）")
    subtitle: str = Field(description="提案書のサブタイトル・価値提案メッセージ（80文字以内）")
    theme_color: str = Field(
        default="sky",
        description="デザインアクセントテーマカラー（sky, emerald, violet, amber, rose のいずれか）",
    )
    custom_callout: str = Field(
        default="",
        description="表紙に表示する特別ハイライトメッセージ（任意・60文字以内）",
    )
    current_challenges: list[ChallengeItem] = Field(
        description="Slide 02: 現状の主要課題（必ず3項目）",
        min_length=3,
        max_length=3,
    )
    executive_conclusion: str = Field(
        description="Slide 02: エグゼクティブサマリーの結論メッセージ（120文字以内）"
    )
    before_state: list[str] = Field(
        description="Slide 03: 従来の業務・顧客体験の課題状態（3項目）",
        min_length=3,
        max_length=3,
    )
    after_state: list[str] = Field(
        description="Slide 03: 変革後の業務・顧客体験の理想状態（3項目）",
        min_length=3,
        max_length=3,
    )
    cx_highlights: list[CxHighlight] = Field(
        description="Slide 03: UX/AI変革のハイライト（3項目）",
        min_length=3,
        max_length=3,
    )
    architecture_nodes: list[ArchitectureNode] = Field(
        description="Slide 04: システム・データ連携アーキテクチャの4層構造（左から右へ4項目）",
        min_length=4,
        max_length=4,
    )
    roadmap_phases: list[RoadmapPhase] = Field(
        description="Slide 05: 導入ロードマップの3フェーズ（Phase 1〜3の3項目）",
        min_length=3,
        max_length=3,
    )
    quantitative_roi: list[str] = Field(
        description="Slide 06: 定量的な期待効果・KPI改善目標（3項目）",
        min_length=3,
        max_length=3,
    )
    qualitative_roi: list[str] = Field(
        description="Slide 06: 定性的な期待効果・組織変革メリット（3項目）",
        min_length=3,
        max_length=3,
    )
    next_steps: list[str] = Field(
        description="Slide 06: 直近のネクストステップ・アクションアイテム（3項目）",
        min_length=3,
        max_length=3,
    )


# ---------------------------------------------------------------------------
# Password Hashing Helpers (PBKDF2-HMAC-SHA256)
# ---------------------------------------------------------------------------


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    """Hashes a plaintext password with PBKDF2-HMAC-SHA256 and returns (hash_hex, salt_hex)."""
    if not salt_hex:
        salt_hex = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        120_000,
    )
    return dk.hex(), salt_hex


def verify_password(password: str, expected_hash_hex: str, salt_hex: str) -> bool:
    """Constant-time verification of a plaintext password against (expected_hash_hex, salt_hex)."""
    actual_hash_hex, _ = hash_password(password, salt_hex)
    return hmac.compare_digest(actual_hash_hex, expected_hash_hex)


# ---------------------------------------------------------------------------
# HTML Rendering & Structural DOM Validation
# ---------------------------------------------------------------------------


def render_deck_html(
    deck_spec: PresentationDeckSpec | dict[str, Any],
    generated_date: str | None = None,
) -> str:
    """Renders the 6-slide bespoke HTML5 presentation using deck_base.html.j2."""
    if isinstance(deck_spec, dict):
        deck_obj = PresentationDeckSpec.model_validate(deck_spec)
    else:
        deck_obj = deck_spec

    if deck_obj.theme_color not in SUPPORTED_THEME_COLORS:
        deck_obj.theme_color = "sky"

    if not generated_date:
        generated_date = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=9))
        ).strftime("%Y-%m-%d %H:%M JST")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
    )
    template = env.get_template("deck_base.html.j2")
    html_output = template.render(
        deck=deck_obj,
        generated_date=generated_date,
        brand_name=_get_brand_name(),
        brand_badge=_get_brand_badge(),
    )
    validate_rendered_html(html_output)
    return html_output


def validate_rendered_html(html_str: str) -> bool:
    """Validates that the rendered single-file HTML deck has 6 complete slides, distinct layouts, and required assets."""
    if "<!DOCTYPE html>" not in html_str and "<!doctype html>" not in html_str.lower():
        raise ValueError("Missing <!DOCTYPE html> declaration.")
    if "{{" in html_str or "}}" in html_str:
        raise ValueError("Unrendered Jinja2 template expressions detected in HTML.")
    slide_matches = re.findall(r'data-slide-index="(\d+)"', html_str)
    if slide_matches != ["0", "1", "2", "3", "4", "5"]:
        raise ValueError(
            f"Expected 6 slides with indices 0..5, found: {slide_matches}"
        )
    layouts = re.findall(r'data-layout="([^"]+)"', html_str)
    if len(set(layouts)) < 4:
        raise ValueError(
            f"Expected bespoke per-slide layouts (at least 4 distinct data-layout values), found: {layouts}"
        )
    required_markers = [
        "https://cdn.tailwindcss.com",
        "gsap.min.js",
        "font-awesome",
        "Noto+Sans+JP",
        "Plus+Jakarta+Sans",
        "JetBrains+Mono",
    ]
    for marker in required_markers:
        if marker not in html_str:
            raise ValueError(f"Missing required design asset marker: {marker}")
    return True


def _build_slide_outline(deck_obj: PresentationDeckSpec) -> list[str]:
    return [
        f"Slide 01 [Cover / {deck_obj.theme_color}]: {deck_obj.client_name} 御中 - {deck_obj.proposal_title}",
        "Slide 02 [Executive Summary Bento]: "
        + " / ".join(c.title for c in deck_obj.current_challenges)
        + f" → {deck_obj.executive_conclusion}",
        "Slide 03 [Solution & UX/AI Comparison]: "
        + " / ".join(cx.title for cx in deck_obj.cx_highlights),
        "Slide 04 [Architecture Flow (4 Layers)]: "
        + " → ".join(n.layer_name for n in deck_obj.architecture_nodes),
        "Slide 05 [Roadmap Timeline (3 Phases)]: "
        + " → ".join(f"{p.phase_name} ({p.period})" for p in deck_obj.roadmap_phases),
        "Slide 06 [Expected ROI & Next Steps]: "
        + " / ".join(deck_obj.quantitative_roi),
    ]


def _default_deck_spec_from_brief(
    client_name: str,
    proposal_title: str,
    proposal_brief: str,
    theme_color: str = "sky",
) -> PresentationDeckSpec:
    """Deterministic fallback generator for PresentationDeckSpec when LLM API is mocked or offline."""
    clean_client = re.sub(r"(御中|様)$", "", client_name.strip()).strip() or "Sample Client Inc."
    slug = re.sub(r"[^a-z0-9-]+", "-", clean_client.lower()).strip("-") or "sample-client"
    theme = theme_color if theme_color in SUPPORTED_THEME_COLORS else "sky"
    return PresentationDeckSpec(
        client_name=clean_client,
        client_slug=slug,
        proposal_title=proposal_title[:45]
        or "AIエージェント×統合データ基盤による次世代顧客体験変革",
        subtitle=(
            proposal_brief[:80]
            if proposal_brief
            else "店舗・EC・アプリの顧客接点をリアルタイム統合し、LTV最大化を実現するビジネス変革の羅針盤"
        ),
        theme_color=theme,
        custom_callout="Managed Agents API & Interactive Slide Designer Skill 適用済み",
        current_challenges=[
            ChallengeItem(
                title="チャネル間の顧客データ分断",
                description="店舗・EC・アプリの会員データがサイロ化し、顧客文脈に応じた一貫した体験提供が困難。",
            ),
            ChallengeItem(
                title="施策立案・制作リードタイムの長期化",
                description="データ抽出から企画・クリエイティブ作成まで手作業が多く、迅速なPDCAが回らない。",
            ),
            ChallengeItem(
                title="パーソナライズ精度の限界",
                description="画一的なメルマガ・キャンペーン配信に留まり、リピート転換率とLTVが伸び悩んでいる。",
            ),
        ],
        executive_conclusion=(
            f"{_get_brand_name()}のUXデザイン知見とGoogle Cloud (BigQuery + Vertex AI + Gemini Enterprise) を融合し、"
            f"{clean_client}様の対話型AI体験とマーケティング自律化を最短2ヶ月で実現します。"
        )[:120],
        before_state=[
            "手動リスト抽出と属人的な企画作成で施策実行まで平均5営業日以上",
            "全会員への画一的な配信による反応率低下と離脱リスク",
            "チャネルごとに分断された接客により2回目購買への転換が停滞",
        ],
        after_state=[
            "AIエージェントが購買直後のマイクロモーメントを検知しリアルタイム提案",
            "顧客一人ひとりの行動文脈に合わせた1to1パーソナライズ接客",
            "Gemini Enterpriseによる社内知見横断検索と提案・施策の自律生成",
        ],
        cx_highlights=[
            CxHighlight(
                title="対話型AIコンシェルジュ体験",
                detail="アプリ・Web上で自然言語対話を通じて顧客の潜在ニーズを引き出し最適提案。",
            ),
            CxHighlight(
                title="リアルタイムOMOデータ連携",
                detail="オンライン行動履歴とオフライン接点をBigQueryデータ基盤で即時統合。",
            ),
            CxHighlight(
                title="マーケティング・営業活動の自律化",
                detail="Gemini Enterprise上で過去実績検索からWeb提案書発行までワンストップ実行。",
            ),
        ],
        architecture_nodes=[
            ArchitectureNode(
                layer_name="1. 顧客接点チャネル層",
                icon="fa-mobile-screen",
                components=["公式モバイルアプリ", "Webポータル", "店舗・営業タブレット"],
                description="マルチチャネルでの顧客行動ログと対話リクエストをリアルタイム収集。",
            ),
            ArchitectureNode(
                layer_name="2. 認証・軽量配信基盤層",
                icon="fa-shield-halved",
                components=["Firebase Hosting", "Cloud Run 認証GW", "Firestore セッション管理"],
                description="取引先・顧客向けにセキュアかつゼロ遅延なWeb配信とアクセス制御を提供。",
            ),
            ArchitectureNode(
                layer_name="3. AIエージェント実行層",
                icon="fa-brain",
                components=["Vertex AI Agent Runtime", "Gemini Enterprise", "Vertex AI Search"],
                description="ADKエージェントとデザインSkillが社内知識を検索し高度な推論・生成を実行。",
            ),
            ArchitectureNode(
                layer_name="4. 統合データ・ストレージ層",
                icon="fa-database",
                components=["BigQuery データ基盤", "Private Cloud Storage", "Firestore 監査ログ"],
                description="非公開バケットとデータウェアハウスによりガバナンスと高速分析を両立。",
            ),
        ],
        roadmap_phases=[
            RoadmapPhase(
                phase_name="Phase 1: 構想設計・PoC検証",
                period="Month 1 - 2",
                deliverables=[
                    "カスタマージャーニー設計と優先ユースケース定義",
                    "BigQuery・Vertex AI Searchへの初期データ統合",
                    "AIエージェントのプロトタイプ実装と社内検証",
                ],
                milestone="プロトタイプ合意・PoC効果測定完了",
            ),
            RoadmapPhase(
                phase_name="Phase 2: パイロット導入・セキュリティ適用",
                period="Month 3 - 4",
                deliverables=[
                    "限定チャネルでのAIコンシェルジュ・提案基盤パイロット公開",
                    "Cloud Run + Private GCS 配信基盤の本番セキュリティ審査完了",
                    "A/BテストによるCVR・業務工数削減リフトの定量検証",
                ],
                milestone="パイロット環境でのKPI目標達成",
            ),
            RoadmapPhase(
                phase_name="Phase 3: 全社展開・内製化定着",
                period="Month 5 - 6",
                deliverables=[
                    "全営業・全マーケティングチャネルへの本格ロールアウト",
                    "Gemini Enterpriseを活用した現場担当者の自走体制構築",
                    "継続的改善ダッシュボードと運用ガバナンスの定着",
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
            "チャネルの垣根を超えたブランド体験の一貫性確立",
            "データとAIに基づく迅速な意思決定カルチャーの醸成",
            "属人化していた企画・提案ノウハウの全社ナレッジ資産化",
        ],
        next_steps=[
            "今週中：対象データソースおよび既存テンプレート資産の棚卸し",
            "来週：UX/AIデザインチームとの共同ワークショップ開催",
            "2週間後：Phase 1 詳細スコープ定義書およびお見積りのご提示",
        ],
    )


def synthesize_deck_spec_with_skill(
    client_name: str,
    proposal_title: str,
    proposal_brief: str,
    theme_color: str = "sky",
    knowledge_context: str = "",
) -> tuple[PresentationDeckSpec, str]:
    """Synthesizes a 6-slide PresentationDeckSpec guided by interactive-slide-designer skill."""
    skill_text = load_interactive_slide_designer_skill()
    theme = theme_color if theme_color in SUPPORTED_THEME_COLORS else "sky"
    prompt = f"""あなたはエグゼクティブ提案デザイナーです。
以下の `interactive-slide-designer` スキル定義と社内ナレッジ検索結果に基づき、
提案先クライアント専用の全6枚インタラクティブHTML5プレゼンテーション構成（`PresentationDeckSpec`）を作成してください。

【クライアント名】: {client_name}
【提案タイトル】: {proposal_title}
【提案ブリーフ・要望】: {proposal_brief}
【希望テーマカラー】: {theme}

【社内ナレッジ検索結果】:
{knowledge_context}

【適用スキル (interactive-slide-designer)】:
{skill_text}
"""
    project_id = _get_project_id()
    location = _get_location()

    if os.environ.get("ENABLE_MANAGED_AGENTS_API", "true").lower() in ("true", "1"):
        try:
            global_client = genai.Client(
                vertexai=True, project=project_id, location="global"
            )
            interactions_api = getattr(global_client, "interactions", None)
            if interactions_api and hasattr(interactions_api, "create"):
                interaction_resp = interactions_api.create(
                    model=MANAGED_AGENT_MODEL,
                    input=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": PresentationDeckSpec.model_json_schema(),
                    },
                )
                raw_text = getattr(interaction_resp, "output_text", None) or str(
                    interaction_resp
                )
                deck_obj = PresentationDeckSpec.model_validate_json(raw_text)
                deck_obj.theme_color = theme
                return deck_obj, f"managed_agents_api:{MANAGED_AGENT_MODEL}"
        except Exception as exc:
            logger.info(
                "Managed Agents API (%s) fallback to Vertex AI Gemini (%s): %s",
                MANAGED_AGENT_MODEL,
                MODEL,
                exc,
            )

    try:
        client = genai.Client(vertexai=True, project=project_id, location=location)
        resp = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PresentationDeckSpec,
                temperature=0.25,
            ),
        )
        if resp.text:
            deck_obj = PresentationDeckSpec.model_validate_json(resp.text)
            deck_obj.theme_color = theme
            return deck_obj, f"vertex_gemini_with_skill:{MODEL}"
    except Exception as exc:
        logger.warning("Gemini structured synthesis fallback triggered: %s", exc)

    return (
        _default_deck_spec_from_brief(
            client_name=client_name,
            proposal_title=proposal_title,
            proposal_brief=proposal_brief,
            theme_color=theme,
        ),
        "deterministic_skill_template",
    )


# ---------------------------------------------------------------------------
# Tool 1: Vertex AI Search (Discovery Engine) Knowledge Search Tool
# ---------------------------------------------------------------------------


def search_internal_knowledge(query: str) -> str:
    """Searches internal Vertex AI Search datastore for past proposals, RFPs, case studies, and CRM context."""
    project_id = _get_project_id()
    location = _get_datastore_location()
    datastore_id = _get_datastore_id()

    results_list: list[dict[str, Any]] = []
    try:
        from google.api_core.client_options import ClientOptions
        from google.cloud import discoveryengine_v1 as discoveryengine

        api_endpoint = (
            f"{location}-discoveryengine.googleapis.com"
            if location != "global"
            else "discoveryengine.googleapis.com"
        )
        client_options = ClientOptions(
            api_endpoint=api_endpoint,
            quota_project_id=project_id,
        )
        client = discoveryengine.SearchServiceClient(client_options=client_options)
        serving_config = (
            f"projects/{project_id}/locations/{location}/collections/"
            f"default_collection/dataStores/{datastore_id}/servingConfigs/default_search"
        )
        request = discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=query,
            page_size=5,
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                    return_snippet=True
                ),
            ),
        )
        response = client.search(request)
        for result in response.results:
            doc = result.document
            struct_data = dict(doc.struct_data) if doc.struct_data else {}
            derived_data = (
                dict(doc.derived_struct_data) if doc.derived_struct_data else {}
            )
            snippets = []
            if "snippets" in derived_data:
                for s in derived_data["snippets"]:
                    if isinstance(s, dict) and s.get("snippet"):
                        snippets.append(s["snippet"])
            results_list.append(
                {
                    "id": doc.id,
                    "title": struct_data.get("title")
                    or derived_data.get("title")
                    or doc.id,
                    "client_name": struct_data.get("client_name", ""),
                    "industry": struct_data.get("industry", ""),
                    "summary": struct_data.get("summary")
                    or struct_data.get("content")
                    or " ".join(snippets),
                    "key_metrics": struct_data.get("key_metrics", ""),
                    "recommended_architecture": struct_data.get(
                        "recommended_architecture", ""
                    ),
                }
            )
    except Exception as exc:
        logger.warning("Vertex AI Search query fallback triggered: %s", exc)

    if not results_list:
        results_list = [
            {
                "id": "sample-case-retail-cdp-ai-001",
                "title": f"{_get_brand_name()} 標準実績：大手リテール・商業施設向け AIコンシェルジュ＆統合データ基盤提案",
                "client_name": query,
                "industry": "リテール・流通・金融・B2Bサービス",
                "summary": (
                    "会員アプリ・EC・店舗POSの分断された顧客データをGoogle Cloud (BigQuery + Vertex AI) 上の"
                    "リアルタイムデータ基盤に統合。対話型AIエージェントにより、"
                    "顧客一人ひとりの購買文脈に合わせたパーソナライズ接客とマーケティング施策の自動生成を実現。"
                ),
                "key_metrics": "リピート購買転換率(CVR) +28%向上、LTV +22%伸長、キャンペーン制作・運用工数 65%削減",
                "recommended_architecture": (
                    "Layer 1: マルチチャネル接点(会員アプリ/Web/店舗端末) -> "
                    "Layer 2: 認証・配信基盤(Cloud Run / Firebase Hosting) -> "
                    "Layer 3: AIエージェント基盤(Vertex AI Agent Runtime / Gemini Enterprise / Vertex AI Search) -> "
                    "Layer 4: 統合データ基盤(BigQuery / Cloud Storage / Firestore)"
                ),
            }
        ]

    return json.dumps(
        {
            "datastore_id": datastore_id,
            "project_id": project_id,
            "query": query,
            "matched_documents": results_list,
        },
        ensure_ascii=False,
        indent=2,
    )


search_proposal_datastore = search_internal_knowledge


# ---------------------------------------------------------------------------
# Tool 2: Create & Publish Proposal Website to Private GCS + Firestore
# ---------------------------------------------------------------------------


def _extract_deck_dict_from_state(raw_deck: Any) -> dict[str, Any]:
    if isinstance(raw_deck, PresentationDeckSpec):
        return raw_deck.model_dump()
    if isinstance(raw_deck, dict):
        return raw_deck
    if isinstance(raw_deck, str):
        cleaned = raw_deck.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return json.loads(cleaned)
    raise ValueError(f"Unsupported deck_spec format: {type(raw_deck)}")


def create_proposal_website(
    client_name: str = "",
    proposal_title: str = "",
    proposal_brief: str = "",
    theme_color: str = "sky",
    expiration_days: int = 14,
    deck_spec_json: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Generates a bespoke 6-slide HTML5 proposal website, uploads it to Private GCS, and issues credentials in Firestore."""
    raw_spec = tool_context.state.get("deck_spec") if tool_context else None
    if not raw_spec and deck_spec_json:
        raw_spec = deck_spec_json

    generation_engine = "state_deck_spec"
    if raw_spec:
        deck_dict = _extract_deck_dict_from_state(raw_spec)
        if theme_color and theme_color in SUPPORTED_THEME_COLORS:
            deck_dict.setdefault("theme_color", theme_color)
        deck_obj = PresentationDeckSpec.model_validate(deck_dict)
    else:
        if not client_name and not proposal_title and not proposal_brief:
            raise ValueError(
                "Either (client_name, proposal_title, proposal_brief) or deck_spec must be provided."
            )
        eff_client = client_name or "Sample Client Inc."
        eff_title = (
            proposal_title or f"{eff_client}様向け AI×UX変革ご提案プレゼンテーション"
        )
        eff_brief = proposal_brief or eff_title
        knowledge_json = search_internal_knowledge(
            f"{eff_client} {eff_title} {eff_brief}"
        )
        deck_obj, generation_engine = synthesize_deck_spec_with_skill(
            client_name=eff_client,
            proposal_title=eff_title,
            proposal_brief=eff_brief,
            theme_color=theme_color,
            knowledge_context=knowledge_json,
        )

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_jst = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    exp_days = max(1, min(expiration_days, 365))
    expires_utc = now_utc + datetime.timedelta(days=exp_days)

    html_content = render_deck_html(
        deck_obj,
        generated_date=now_jst.strftime("%Y-%m-%d %H:%M JST"),
    )

    short_id = uuid.uuid4().hex[:8]
    date_prefix = now_jst.strftime("%Y%m%d")
    presentation_id = f"prop-{date_prefix}-{short_id}"

    safe_slug = re.sub(r"[^a-z0-9-]+", "-", deck_obj.client_slug.lower()).strip("-")
    if not safe_slug:
        safe_slug = "client"
    viewer_id = f"client-{safe_slug[:16]}-{secrets.token_hex(2)}"
    viewer_password = secrets.token_urlsafe(12)
    password_hash, password_salt = hash_password(viewer_password)

    project_id = _get_project_id()
    bucket_name = _get_bucket_name()
    collection_name = _get_firestore_collection()
    hosting_base_url = _get_hosting_base_url()
    blob_path = f"presentations/{presentation_id}/index.html"
    gcs_uri = f"gs://{bucket_name}/{blob_path}"

    from google.cloud import firestore, storage

    storage_client = storage.Client(project=project_id)
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.cache_control = "no-store, private"
    blob.upload_from_string(
        html_content.encode("utf-8"),
        content_type="text/html; charset=utf-8",
    )

    fs_client = _get_firestore_client(project_id)
    doc_ref = fs_client.collection(collection_name).document(presentation_id)
    doc_ref.set(
        {
            "presentation_id": presentation_id,
            "viewer_id": viewer_id,
            "password_hash": password_hash,
            "password_salt": password_salt,
            "gcs_bucket": bucket_name,
            "gcs_blob_path": blob_path,
            "gcs_uri": gcs_uri,
            "client_name": deck_obj.client_name,
            "proposal_title": deck_obj.proposal_title,
            "subtitle": deck_obj.subtitle,
            "theme_color": deck_obj.theme_color,
            "deck_spec": deck_obj.model_dump(),
            "generation_engine": generation_engine,
            "skill_applied": "interactive-slide-designer",
            "created_at": now_utc.isoformat(),
            "updated_at": now_utc.isoformat(),
            "expires_at": expires_utc.isoformat(),
            "status": "active",
            "is_active": True,
        }
    )

    share_url = f"{hosting_base_url}/p/{presentation_id}"
    slide_outline = _build_slide_outline(deck_obj)

    result = {
        "status": "PUBLISHED",
        "presentation_id": presentation_id,
        "client_name": deck_obj.client_name,
        "proposal_title": deck_obj.proposal_title,
        "theme_color": deck_obj.theme_color,
        "generation_engine": generation_engine,
        "skill_applied": "interactive-slide-designer",
        "share_url": share_url,
        "viewer_id": viewer_id,
        "viewer_password": viewer_password,
        "expires_at": expires_utc.strftime(f"%Y-%m-%d %H:%M UTC ({exp_days}日間有効)"),
        "gcs_uri": gcs_uri,
        "slide_outline": slide_outline,
    }
    if tool_context is not None:
        tool_context.state["published_result"] = result
    return result


def publish_presentation(
    tool_context: ToolContext | None = None,
    deck_spec_json: str = "",
) -> dict[str, Any]:
    return create_proposal_website(
        deck_spec_json=deck_spec_json,
        tool_context=tool_context,
    )


# ---------------------------------------------------------------------------
# Tool 3: Edit an Existing Proposal Website in Place
# ---------------------------------------------------------------------------


def edit_proposal_website(
    presentation_id: str,
    edit_instructions: str,
    new_title: str = "",
    new_subtitle: str = "",
    new_theme_color: str = "",
    new_custom_callout: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Edits an existing published proposal website and updates the live HTML in Private Cloud Storage."""
    project_id = _get_project_id()
    location = _get_location()
    collection_name = _get_firestore_collection()
    hosting_base_url = _get_hosting_base_url()

    from google.cloud import firestore, storage

    fs_client = _get_firestore_client(project_id)
    doc_ref = fs_client.collection(collection_name).document(presentation_id)
    doc_snap = doc_ref.get()
    if not doc_snap.exists:
        raise ValueError(f"Presentation '{presentation_id}' not found in Firestore.")

    doc_data = doc_snap.to_dict() or {}
    raw_spec = doc_data.get("deck_spec")
    if raw_spec:
        deck_obj = PresentationDeckSpec.model_validate(raw_spec)
    else:
        deck_obj = _default_deck_spec_from_brief(
            client_name=doc_data.get("client_name", "Sample Client Inc."),
            proposal_title=doc_data.get("proposal_title", "Proposal"),
            proposal_brief=doc_data.get("subtitle", ""),
            theme_color=doc_data.get("theme_color", "sky"),
        )

    updated_fields: list[str] = []

    if edit_instructions and os.environ.get(
        "ENABLE_LLM_DECK_EDIT", "true"
    ).lower() in ("true", "1"):
        try:
            client = genai.Client(vertexai=True, project=project_id, location=location)
            edit_prompt = f"""既存の6枚構成プレゼンテーションデータ（JSON）に対して、ユーザーの修正指示を反映した新しい `PresentationDeckSpec` JSONを出力してください。
変更指示がないフィールドは既存の値を維持してください。

【現在のPresentationDeckSpec JSON】:
{json.dumps(deck_obj.model_dump(), ensure_ascii=False, indent=2)}

【ユーザーの修正指示】:
{edit_instructions}
"""
            resp = client.models.generate_content(
                model=MODEL,
                contents=edit_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=PresentationDeckSpec,
                    temperature=0.2,
                ),
            )
            if resp.text:
                deck_obj = PresentationDeckSpec.model_validate_json(resp.text)
                updated_fields.append("llm_deck_refinement")
        except Exception as exc:
            logger.info("LLM edit fallback to deterministic field updates: %s", exc)

    if new_title:
        deck_obj.proposal_title = new_title
        updated_fields.append("proposal_title")
    if new_subtitle:
        deck_obj.subtitle = new_subtitle
        updated_fields.append("subtitle")
    if new_theme_color and new_theme_color in SUPPORTED_THEME_COLORS:
        deck_obj.theme_color = new_theme_color
        updated_fields.append("theme_color")
    else:
        for color_name in SUPPORTED_THEME_COLORS:
            if color_name in edit_instructions.lower():
                deck_obj.theme_color = color_name
                updated_fields.append("theme_color")
                break
    if new_custom_callout:
        deck_obj.custom_callout = new_custom_callout
        updated_fields.append("custom_callout")
    elif edit_instructions and not updated_fields:
        deck_obj.custom_callout = edit_instructions[:60]
        updated_fields.append("custom_callout")

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_jst = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    html_content = render_deck_html(
        deck_obj,
        generated_date=now_jst.strftime("%Y-%m-%d %H:%M JST (Updated)"),
    )

    bucket_name = doc_data.get("gcs_bucket") or _get_bucket_name()
    blob_path = (
        doc_data.get("gcs_blob_path") or f"presentations/{presentation_id}/index.html"
    )
    storage_client = storage.Client(project=project_id)
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.cache_control = "no-store, private"
    blob.upload_from_string(
        html_content.encode("utf-8"),
        content_type="text/html; charset=utf-8",
    )

    updates = {
        "client_name": deck_obj.client_name,
        "proposal_title": deck_obj.proposal_title,
        "subtitle": deck_obj.subtitle,
        "theme_color": deck_obj.theme_color,
        "deck_spec": deck_obj.model_dump(),
        "updated_at": now_utc.isoformat(),
        "last_edit_instructions": edit_instructions,
    }
    doc_ref.update(updates)

    share_url = f"{hosting_base_url}/p/{presentation_id}"
    result = {
        "status": "UPDATED",
        "presentation_id": presentation_id,
        "client_name": deck_obj.client_name,
        "proposal_title": deck_obj.proposal_title,
        "subtitle": deck_obj.subtitle,
        "theme_color": deck_obj.theme_color,
        "custom_callout": deck_obj.custom_callout,
        "updated_fields": updated_fields,
        "share_url": share_url,
        "updated_at": now_utc.isoformat(),
        "slide_outline": _build_slide_outline(deck_obj),
    }
    if tool_context is not None:
        tool_context.state["last_edited_result"] = result
    return result


edit_presentation = edit_proposal_website


# ---------------------------------------------------------------------------
# Tool 4: List Proposal Websites
# ---------------------------------------------------------------------------


def list_proposal_websites(
    client_filter: str = "",
    include_revoked: bool = True,
    limit: int = 20,
) -> dict[str, Any]:
    """Lists existing proposal websites registered in Firestore with status, URLs, and access counts."""
    project_id = _get_project_id()
    collection_name = _get_firestore_collection()
    hosting_base_url = _get_hosting_base_url()

    from google.cloud import firestore

    fs_client = _get_firestore_client(project_id)
    col_ref = fs_client.collection(collection_name)
    docs = list(col_ref.stream())

    items: list[dict[str, Any]] = []
    for doc_snap in docs:
        data = doc_snap.to_dict() or {}
        pres_id = data.get("presentation_id") or doc_snap.id
        is_active = bool(data.get("is_active", True))
        status_val = str(
            data.get("status") or ("active" if is_active else "revoked")
        ).lower()
        if not include_revoked and (not is_active or status_val == "revoked"):
            continue
        client_name = str(data.get("client_name", ""))
        proposal_title = str(data.get("proposal_title", ""))
        if client_filter:
            needle = client_filter.lower()
            if (
                needle not in client_name.lower()
                and needle not in proposal_title.lower()
                and needle not in pres_id.lower()
            ):
                continue

        access_count = 0
        try:
            logs_iter = col_ref.document(pres_id).collection("access_logs").stream()
            access_count = sum(1 for _ in logs_iter)
        except Exception:
            access_count = 0

        items.append(
            {
                "presentation_id": pres_id,
                "client_name": client_name,
                "proposal_title": proposal_title,
                "theme_color": data.get("theme_color", "sky"),
                "status": status_val,
                "is_active": is_active,
                "share_url": f"{hosting_base_url}/p/{pres_id}",
                "viewer_id": data.get("viewer_id", ""),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", data.get("created_at", "")),
                "expires_at": data.get("expires_at", ""),
                "access_log_count": access_count,
            }
        )

    items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    capped = items[: max(1, min(limit, 100))]
    return {
        "count": len(capped),
        "presentations": capped,
    }


list_presentations = list_proposal_websites


# ---------------------------------------------------------------------------
# Tool 5: Get Proposal Access Audit Logs
# ---------------------------------------------------------------------------


def get_proposal_access_logs(
    presentation_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Retrieves viewer authentication and access audit logs for a specific presentation."""
    project_id = _get_project_id()
    collection_name = _get_firestore_collection()

    from google.cloud import firestore

    fs_client = _get_firestore_client(project_id)
    doc_ref = fs_client.collection(collection_name).document(presentation_id)
    doc_snap = doc_ref.get()
    if not doc_snap.exists:
        raise ValueError(f"Presentation '{presentation_id}' not found in Firestore.")

    data = doc_snap.to_dict() or {}
    logs_stream = doc_ref.collection("access_logs").stream()
    logs = [entry.to_dict() or {} for entry in logs_stream]
    logs.sort(key=lambda x: str(x.get("accessed_at", "")), reverse=True)
    capped = logs[: max(1, min(limit, 100))]

    return {
        "presentation_id": presentation_id,
        "client_name": data.get("client_name", ""),
        "proposal_title": data.get("proposal_title", ""),
        "status": data.get(
            "status", "active" if data.get("is_active", True) else "revoked"
        ),
        "total_access_count": len(logs),
        "access_logs": capped,
    }


get_presentation_access_logs = get_proposal_access_logs


# ---------------------------------------------------------------------------
# Tool 6: Manage Proposal Credentials & Expiration
# ---------------------------------------------------------------------------


def manage_proposal_credentials(
    presentation_id: str,
    rotate_password: bool = True,
    new_viewer_id: str = "",
    extend_days: int = 0,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Rotates the viewer password, updates viewer_id, or extends the expiration date for a presentation."""
    project_id = _get_project_id()
    collection_name = _get_firestore_collection()
    hosting_base_url = _get_hosting_base_url()

    from google.cloud import firestore

    fs_client = _get_firestore_client(project_id)
    doc_ref = fs_client.collection(collection_name).document(presentation_id)
    doc_snap = doc_ref.get()
    if not doc_snap.exists:
        raise ValueError(f"Presentation '{presentation_id}' not found in Firestore.")

    data = doc_snap.to_dict() or {}
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    updates: dict[str, Any] = {"updated_at": now_utc.isoformat()}

    viewer_id = str(data.get("viewer_id", ""))
    if new_viewer_id.strip():
        viewer_id = new_viewer_id.strip()
        updates["viewer_id"] = viewer_id

    new_password: str | None = None
    if rotate_password:
        new_password = secrets.token_urlsafe(12)
        pw_hash, pw_salt = hash_password(new_password)
        updates["password_hash"] = pw_hash
        updates["password_salt"] = pw_salt

    expires_at_iso = str(data.get("expires_at", ""))
    if extend_days > 0:
        new_exp = now_utc + datetime.timedelta(days=min(extend_days, 365))
        expires_at_iso = new_exp.isoformat()
        updates["expires_at"] = expires_at_iso
        updates["is_active"] = True
        updates["status"] = "active"

    doc_ref.update(updates)

    result = {
        "status": "CREDENTIALS_UPDATED",
        "presentation_id": presentation_id,
        "client_name": data.get("client_name", ""),
        "proposal_title": data.get("proposal_title", ""),
        "share_url": f"{hosting_base_url}/p/{presentation_id}",
        "viewer_id": viewer_id,
        "password_rotated": rotate_password,
        "new_viewer_password": new_password or "(unchanged)",
        "expires_at": expires_at_iso,
    }
    if tool_context is not None:
        tool_context.state["last_credential_update"] = result
    return result


manage_presentation_access = manage_proposal_credentials


# ---------------------------------------------------------------------------
# Tool 7: Delete / Revoke a Proposal Website
# ---------------------------------------------------------------------------


def delete_proposal_website(
    presentation_id: str,
    hard_delete_gcs: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Revokes external access to a proposal website (and optionally deletes the HTML blob from GCS)."""
    project_id = _get_project_id()
    collection_name = _get_firestore_collection()

    from google.cloud import firestore, storage

    fs_client = _get_firestore_client(project_id)
    doc_ref = fs_client.collection(collection_name).document(presentation_id)
    doc_snap = doc_ref.get()
    if not doc_snap.exists:
        raise ValueError(f"Presentation '{presentation_id}' not found in Firestore.")

    data = doc_snap.to_dict() or {}
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    doc_ref.update(
        {
            "status": "revoked",
            "is_active": False,
            "revoked_at": now_utc.isoformat(),
            "updated_at": now_utc.isoformat(),
        }
    )

    gcs_deleted = False
    if hard_delete_gcs:
        try:
            bucket_name = data.get("gcs_bucket") or _get_bucket_name()
            blob_path = (
                data.get("gcs_blob_path")
                or f"presentations/{presentation_id}/index.html"
            )
            storage_client = storage.Client(project=project_id)
            blob = storage_client.bucket(bucket_name).blob(blob_path)
            if blob.exists():
                blob.delete()
                gcs_deleted = True
        except Exception as exc:
            logger.warning("Failed to delete GCS blob on revoke: %s", exc)

    result = {
        "status": "REVOKED",
        "presentation_id": presentation_id,
        "client_name": data.get("client_name", ""),
        "proposal_title": data.get("proposal_title", ""),
        "is_active": False,
        "gcs_blob_deleted": gcs_deleted,
        "revoked_at": now_utc.isoformat(),
    }
    if tool_context is not None:
        tool_context.state["last_revoked_result"] = result
    return result


delete_presentation = delete_proposal_website


# ---------------------------------------------------------------------------
# Interactive Conversational Concierge Root Agent (LlmAgent)
# ---------------------------------------------------------------------------

CONCIERGE_INSTRUCTION = """あなたは提案書Webサイト制作・配信・ライフサイクル管理を担う「インタラクティブ提案コンシェルジュ」です。
ユーザーが対話を通じて高品質な6枚構成HTML5プレゼンテーションサイト（16:9・Tailwind CSS・GSAPアニメーション・カスタムスライドレイアウト）を企画・発行し、発行後の修正・閲覧ログ確認・パスワード変更・公開停止までをチャットだけで完結できるよう支援します。

【最重要ルール：挨拶や曖昧な発話で勝手にWebサイトを生成しないこと】
1. **挨拶・初回相談時の対応（ツール呼び出し禁止）**:
   - ユーザーが「こんにちは」「はじめまして」「何ができますか？」「提案書を作りたい」など、具体的なクライアント名や作成指示を含まない挨拶・相談をしてきた場合は、**絶対に `create_proposal_website` を呼び出さないでください**。
   - まずは丁寧な日本語で挨拶し、あなたが提供できる機能（①社内ナレッジ検索と構成案の壁打ち、②クライアント専用HTMLプレゼンサイトの新規発行と限定公開URL・ID/Pass発行、③発行済みサイトの自然言語での修正・閲覧ログ確認・パスワード再発行・公開停止）を案内してください。
   - その上で、以下のヒアリング項目を問いかけてください：
     - ① 提案先のクライアント企業名・業界
     - ② 解決したい課題や提案テーマ（例：AIコンシェルジュ、統合データ基盤、OMOマーケなど）
     - ③ ご希望のデザインテーマカラー（`sky` / `emerald` / `violet` / `amber` / `rose`）や強調したい実績数値

2. **構成案の相談・社内ナレッジ検索 (`search_internal_knowledge`)**:
   - ユーザーが「まずは構成案を相談したい」「過去の類似事例を調べて」と依頼した場合は、`search_internal_knowledge` を呼び出して社内データストアの過去RFP・導入事例・標準メソドロジーを検索し、全6スライドの構成案をチャット上で提示して「この内容でWebサイトを発行してよろしいでしょうか？」と確認してください。

3. **提案Webサイトの新規生成・限定公開 (`create_proposal_website`)**:
   - ユーザーがクライアント名と提案テーマを指定して「提案プレゼンテーションHTMLを作成・公開してください」「この内容でWebサイトを発行して」と明示的に依頼した場合は、`create_proposal_website` を呼び出してHTML5サイトを生成・非公開Cloud Storageへ保存し、Firestoreに認証情報を登録してください。
   - 発行完了後は、以下の項目をわかりやすく日本語で提示してください：
     1. **プレゼンテーションID** (`presentation_id`)
     2. **顧客共有用プレゼンテーションURL** (`share_url`)
     3. **閲覧用ID** (`viewer_id`)
     4. **初期パスワード** (`viewer_password`)
     5. **有効期限** (`expires_at`) と **デザインテーマ** (`theme_color`)
     6. **全6スライドの構成サマリー** (`slide_outline`)

4. **発行済みWebサイトの管理・修正・削除（ライフサイクル管理ツール）**:
   - **一覧確認**: 「発行済みのサイト一覧を見せて」と言われたら `list_proposal_websites` を呼び出してください。
   - **閲覧監査ログ確認**: 「誰がいつアクセスしたかログを見せて」と言われたら `get_proposal_access_logs` を呼び出してください。
   - **内容・デザインの修正**: 「発行済みの `<presentation_id>` のタイトルやテーマカラー、内容を修正して」と言われたら `edit_proposal_website` を呼び出し、同じURLのまま最新HTMLへ更新したことを伝えてください。
   - **パスワード再発行・期限延長**: 「パスワードを再発行して」「有効期限を延長して」と言われたら `manage_proposal_credentials` を呼び出し、新しい認証情報を提示してください。
   - **公開停止・削除**: 「`<presentation_id>` の公開を停止（削除）して」と言われたら `delete_proposal_website` を呼び出し、外部からのアクセスが即座に遮断（HTTP 403）されたことを報告してください。
"""

root_agent = LlmAgent(
    name="proposal_site_publisher_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "対話型コンシェルジュによるクライアント提案用HTML5スライドWebサイト生成・限定公開・ライフサイクル管理エージェント。"
        "ヒアリングと社内ナレッジ検索、6枚構成インタラクティブHTMLサイトの生成、発行後の修正・閲覧ログ確認・"
        "パスワード再発行・公開停止（削除）を一気通貫で実行します。"
    ),
    instruction=CONCIERGE_INSTRUCTION,
    tools=[
        search_internal_knowledge,
        create_proposal_website,
        edit_proposal_website,
        list_proposal_websites,
        get_proposal_access_logs,
        manage_proposal_credentials,
        delete_proposal_website,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
