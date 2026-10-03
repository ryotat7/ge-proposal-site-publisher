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
import threading
import time
import uuid
from collections.abc import Callable
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

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
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


def _get_model_name() -> str:
    return os.environ.get("GEMINI_MODEL", MODEL)


def _get_genai_location(model_name: str | None = None) -> str:
    explicit = os.environ.get("GENAI_LOCATION", "").strip()
    if explicit:
        return explicit
    target = (model_name or _get_model_name()).lower()
    if target.startswith("gemini-3") or target.startswith("antigravity"):
        return "global"
    return _get_location()


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
        "AGENT_SEARCH_DATASTORE_ID", "proposal-knowledge-datastore"
    )


def _get_datastore_location() -> str:
    return os.environ.get("AGENT_SEARCH_LOCATION", "global")


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
            f"{_get_brand_name()}のUXデザイン知見とGoogle Cloud (BigQuery + Gemini Enterprise Agent Platform + Gemini Enterprise) を融合し、"
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
                components=["Cloud Run 認証GW", "Firestore セッション管理", "非公開 Cloud Storage"],
                description="取引先・顧客向けにセキュアかつゼロ遅延なWeb配信とアクセス制御を提供。",
            ),
            ArchitectureNode(
                layer_name="3. AIエージェント実行層",
                icon="fa-brain",
                components=["Agent Runtime", "Gemini Enterprise", "Agent Search"],
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
                    "BigQuery・Agent Searchへの初期データ統合",
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


def _managed_agents_enabled() -> bool:
    return os.environ.get("ENABLE_MANAGED_AGENTS_API", "true").lower() in ("true", "1")


def _get_managed_agent_deadline_seconds() -> int:
    """Time budget for the Managed Agents API phase before falling back (default 600s = 10 min)."""
    try:
        return max(30, int(os.environ.get("MANAGED_AGENT_DEADLINE_SECONDS", "600")))
    except ValueError:
        return 600


def _get_managed_agent_poll_interval_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("MANAGED_AGENT_POLL_INTERVAL_SECONDS", "8")))
    except ValueError:
        return 8.0


def _get_fast_model_timeout_seconds() -> int:
    try:
        return max(30, int(os.environ.get("FAST_MODEL_TIMEOUT_SECONDS", "180")))
    except ValueError:
        return 180


def _strip_code_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_json_object_text(text: str) -> str:
    """Returns the outermost JSON object substring of a free-form LLM answer (or the stripped text)."""
    cleaned = _strip_code_fences(text)
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    fenced = re.search(r"```(?:json|JSON)?\s*(\{.*\})\s*```", text or "", flags=re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first != -1 and last > first:
        return cleaned[first : last + 1]
    return cleaned


def parse_deck_spec_text(raw_text: str, theme_color: str = "sky") -> PresentationDeckSpec:
    """Parses an LLM answer (possibly fenced / with prose) into a validated PresentationDeckSpec."""
    deck_obj = PresentationDeckSpec.model_validate_json(_extract_json_object_text(raw_text))
    deck_obj.theme_color = theme_color if theme_color in SUPPORTED_THEME_COLORS else "sky"
    return deck_obj


def _build_synthesis_prompt(
    client_name: str,
    proposal_title: str,
    proposal_brief: str,
    theme: str,
    knowledge_context: str,
    outline_hint: str,
    skill_text: str,
) -> str:
    outline_block = (
        f"\n【ユーザーと合意済みの構成メモ（優先して反映）】:\n{outline_hint}\n" if outline_hint else ""
    )
    return f"""あなたはエグゼクティブ提案デザイナーです。
以下の `interactive-slide-designer` スキル定義と社内ナレッジ検索結果に基づき、
提案先クライアント専用の全6枚インタラクティブHTML5プレゼンテーション構成（`PresentationDeckSpec`）を作成してください。

【クライアント名】: {client_name}
【提案タイトル】: {proposal_title}
【提案ブリーフ・要望】: {proposal_brief}
【希望テーマカラー】: {theme}
{outline_block}
【社内ナレッジ検索結果】:
{knowledge_context}

【適用スキル (interactive-slide-designer)】:
{skill_text}
"""


_MANAGED_AGENT_OUTPUT_CONTRACT = """
【出力契約（厳守）】
- 最終回答は `PresentationDeckSpec` JSON オブジェクト **1つのみ** を返してください（前置き・解説・Markdown見出し禁止。```json フェンスは可）。
- ファイルの作成やコード実行は不要です。思考・下書きは内部で行い、最終メッセージには JSON だけを出力してください。
- 配列の要素数はスキーマどおり厳密に守ってください（current_challenges=3, before_state=3, after_state=3, cx_highlights=3, architecture_nodes=4, roadmap_phases=3, quantitative_roi=3, qualitative_roi=3, next_steps=3）。
- すべての文章は自然で説得力のある日本語で、クライアント固有の文脈（業界・課題・固有名詞）を反映してください。

【JSON Schema】
"""

_MANAGED_AGENT_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "incomplete",
    "requires_action",
    "errored",
    "error",
}


def _synthesize_via_managed_agents(
    prompt: str,
    theme: str,
    deadline_seconds: int,
    status_callback: Callable[[str, str], None] | None = None,
) -> tuple[PresentationDeckSpec | None, str, dict[str, Any]]:
    """Runs the Antigravity base agent through the Managed Agents API (Interactions API, locations/global).

    Uses `background=True` + polling with a hard deadline so a slow/hung sandbox can never block publication.
    Returns (deck_or_None, raw_output_text, meta).
    """
    project_id = _get_project_id()
    client = genai.Client(vertexai=True, project=project_id, location="global")
    interactions_api = getattr(client, "interactions", None)
    if interactions_api is None or not hasattr(interactions_api, "create"):
        raise RuntimeError("google-genai SDK without Interactions API support")

    full_prompt = (
        prompt
        + _MANAGED_AGENT_OUTPUT_CONTRACT
        + json.dumps(PresentationDeckSpec.model_json_schema(), ensure_ascii=False)
    )
    started = time.monotonic()
    interaction = interactions_api.create(
        agent=MANAGED_AGENT_MODEL,
        input=full_prompt,
        environment={"type": "remote"},
        background=True,
        store=True,
        stream=False,
        timeout=120,
    )
    interaction_id = str(getattr(interaction, "id", "") or "")
    if status_callback:
        status_callback("managed_agents", f"interaction={interaction_id} started")
    logger.info("Managed Agents interaction %s started (deadline=%ss)", interaction_id, deadline_seconds)

    final = interaction
    poll_interval = _get_managed_agent_poll_interval_seconds()
    polls = 0
    while True:
        status = str(getattr(final, "status", "") or "").lower()
        if status in _MANAGED_AGENT_TERMINAL_STATUSES:
            break
        elapsed = time.monotonic() - started
        if elapsed > deadline_seconds:
            try:
                interactions_api.cancel(interaction_id)
            except Exception as cancel_exc:  # noqa: BLE001
                logger.info("Managed Agents cancel skipped: %s", cancel_exc)
            raise TimeoutError(
                f"Managed Agents interaction {interaction_id} exceeded {deadline_seconds}s (status={status})"
            )
        if poll_interval:
            time.sleep(poll_interval)
        polls += 1
        final = interactions_api.get(interaction_id, timeout=60)
        if status_callback and polls % 4 == 0:
            status_callback(
                "managed_agents",
                f"interaction={interaction_id} status={getattr(final, 'status', '')} elapsed={int(time.monotonic() - started)}s",
            )

    elapsed_total = time.monotonic() - started
    status = str(getattr(final, "status", "") or "").lower()
    meta = {
        "interaction_id": interaction_id,
        "status": status,
        "elapsed_seconds": round(elapsed_total, 1),
    }
    if status != "completed":
        raise RuntimeError(
            f"Managed Agents interaction {interaction_id} ended with status={status} errors={getattr(final, 'errors', None)}"
        )
    raw_text = str(getattr(final, "output_text", "") or "")
    if not raw_text:
        raise RuntimeError(f"Managed Agents interaction {interaction_id} completed without text output")
    try:
        return parse_deck_spec_text(raw_text, theme), raw_text, meta
    except Exception as parse_exc:  # noqa: BLE001
        logger.info("Managed Agents output needs schema repair: %s", parse_exc)
        return None, raw_text, meta


def _synthesize_via_gemini(
    prompt: str,
    theme: str,
    model_name: str,
    timeout_seconds: int,
    status_callback: Callable[[str, str], None] | None = None,
) -> tuple[PresentationDeckSpec, str]:
    """Structured-output synthesis with the fast Gemini model (regional endpoint, then global)."""
    project_id = _get_project_id()
    genai_location = _get_genai_location(model_name)
    candidate_locations = [genai_location]
    if "global" not in candidate_locations:
        candidate_locations.append("global")
    last_exc: Exception | None = None
    for loc in candidate_locations:
        try:
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=loc,
                http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
            )
            if status_callback:
                status_callback("gemini_fast", f"model={model_name} location={loc}")
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=PresentationDeckSpec,
                    temperature=0.25,
                ),
            )
            if resp.text:
                return parse_deck_spec_text(resp.text, theme), f"{model_name}@{loc}"
            raise RuntimeError("empty response text")
        except TypeError:
            # Fake/legacy clients without http_options support (unit tests) – retry without it.
            try:
                client = genai.Client(vertexai=True, project=project_id, location=loc)
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=PresentationDeckSpec,
                        temperature=0.25,
                    ),
                )
                if resp.text:
                    return parse_deck_spec_text(resp.text, theme), f"{model_name}@{loc}"
                raise RuntimeError("empty response text")
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("Gemini structured synthesis (%s at %s) failed: %s", model_name, loc, exc)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("Gemini structured synthesis (%s at %s) failed: %s", model_name, loc, exc)
    raise RuntimeError(f"Gemini synthesis failed on {candidate_locations}: {last_exc}")


def _repair_deck_with_gemini(
    raw_text: str,
    theme: str,
    model_name: str,
    timeout_seconds: int,
    status_callback: Callable[[str, str], None] | None = None,
) -> PresentationDeckSpec:
    """Normalizes a creative but non-conforming draft into the strict PresentationDeckSpec schema."""
    repair_prompt = f"""以下は提案プレゼンテーション構成の下書き（JSONまたは自由記述）です。
内容・固有名詞・数値をできる限り保持したまま、スキーマに**厳密に**適合する `PresentationDeckSpec` JSON に整形してください。
要素数の不足は文脈に沿って補完し、超過分は重要度の高い順に絞り込んでください。希望テーマカラーは `{theme}` です。

【下書き】:
{raw_text[:20000]}
"""
    if status_callback:
        status_callback("managed_agents_repair", f"schema repair with {model_name}")
    deck, _ = _synthesize_via_gemini(repair_prompt, theme, model_name, timeout_seconds)
    return deck


def synthesize_deck_spec_with_skill(
    client_name: str,
    proposal_title: str,
    proposal_brief: str,
    theme_color: str = "sky",
    knowledge_context: str = "",
    outline_hint: str = "",
    managed_agent_deadline_seconds: int | None = None,
    status_callback: Callable[[str, str], None] | None = None,
) -> tuple[PresentationDeckSpec, str]:
    """Synthesizes a 6-slide PresentationDeckSpec guided by the interactive-slide-designer skill.

    Tiered strategy (each tier is time-boxed so publication is always guaranteed):
      1. Managed Agents API (`antigravity-preview-05-2026`, locations/global) within `MANAGED_AGENT_DEADLINE_SECONDS`
         (default 10 min). Non-conforming output is schema-repaired with the fast Gemini model.
      2. Fast Gemini structured output (`GEMINI_MODEL`, default gemini-3.8-flash).
      3. Deterministic skill template (offline / last resort).
    Returns (deck_spec, engine_used).
    """
    skill_text = load_interactive_slide_designer_skill()
    theme = theme_color if theme_color in SUPPORTED_THEME_COLORS else "sky"
    prompt = _build_synthesis_prompt(
        client_name=client_name,
        proposal_title=proposal_title,
        proposal_brief=proposal_brief,
        theme=theme,
        knowledge_context=knowledge_context,
        outline_hint=outline_hint,
        skill_text=skill_text,
    )
    fast_model = _get_model_name()
    fast_timeout = _get_fast_model_timeout_seconds()
    deadline = managed_agent_deadline_seconds or _get_managed_agent_deadline_seconds()

    # 1. Managed Agents API (Antigravity harness) with hard deadline
    if _managed_agents_enabled():
        try:
            deck_obj, raw_text, meta = _synthesize_via_managed_agents(
                prompt, theme, deadline, status_callback
            )
            if deck_obj is not None:
                return deck_obj, f"managed_agents_api:{MANAGED_AGENT_MODEL}"
            try:
                repaired = _repair_deck_with_gemini(
                    raw_text, theme, fast_model, fast_timeout, status_callback
                )
                return repaired, f"managed_agents_api:{MANAGED_AGENT_MODEL}+schema_repair:{fast_model}"
            except Exception as repair_exc:  # noqa: BLE001
                logger.warning(
                    "Managed Agents output (%s) could not be repaired, falling back: %s",
                    meta.get("interaction_id"),
                    repair_exc,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Managed Agents API (%s) fallback to %s: %s",
                MANAGED_AGENT_MODEL,
                fast_model,
                exc,
            )

    # 2. Fast Gemini structured output with skill instructions
    try:
        deck_obj, _ = _synthesize_via_gemini(
            prompt, theme, fast_model, fast_timeout, status_callback
        )
        return deck_obj, f"agent_platform_gemini_with_skill:{fast_model}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini structured synthesis fallback triggered: %s", exc)

    # 3. Deterministic fallback (e.g. unit test environment without external network)
    if status_callback:
        status_callback("deterministic_template", "offline template synthesis")
    return (
        _default_deck_spec_from_brief(
            client_name=client_name,
            proposal_title=proposal_title,
            proposal_brief=proposal_brief,
            theme_color=theme,
        ),
        "deterministic_skill_template",
    )


def describe_generation_engine(engine: str) -> str:
    """Human-readable Japanese label for a `generation_engine` value stored in Firestore."""
    eng = (engine or "").lower()
    if eng.startswith("managed_agents_api"):
        label = "Managed Agents API（Antigravity ハーネス）で生成"
        if "schema_repair" in eng:
            label += "（スキーマ整形は gemini-3.8-flash が補助）"
        return label
    if eng.startswith("agent_platform_gemini_with_skill"):
        return "gemini-3.8-flash 高速生成（Managed Agents API が時間予算超過または失敗したため自動切替）"
    if eng.startswith("deterministic_skill_template"):
        return "スキルテンプレートによる即時生成（オフライン／最終フォールバック）"
    if eng == "state_deck_spec":
        return "対話で確定した構成データをそのまま反映"
    return engine or "不明"


# ---------------------------------------------------------------------------
# Tool 1: Agent Search (Discovery Engine) Knowledge Search Tool
# ---------------------------------------------------------------------------


def search_internal_knowledge(query: str) -> str:
    """Searches internal Agent Search datastore for past proposals, RFPs, case studies, and CRM context."""
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
        logger.warning("Agent Search query fallback triggered: %s", exc)

    if not results_list:
        results_list = [
            {
                "id": "sample-case-retail-cdp-ai-001",
                "title": f"{_get_brand_name()} 標準実績：大手リテール・商業施設向け AIコンシェルジュ＆統合データ基盤提案",
                "client_name": query,
                "industry": "リテール・流通・金融・B2Bサービス",
                "summary": (
                    "会員アプリ・EC・店舗POSの分断された顧客データをGoogle Cloud (BigQuery + Gemini Enterprise Agent Platform) 上の"
                    "リアルタイムデータ基盤に統合。対話型AIエージェントにより、"
                    "顧客一人ひとりの購買文脈に合わせたパーソナライズ接客とマーケティング施策の自動生成を実現。"
                ),
                "key_metrics": "リピート購買転換率(CVR) +28%向上、LTV +22%伸長、キャンペーン制作・運用工数 65%削減",
                "recommended_architecture": (
                    "Layer 1: マルチチャネル接点(会員アプリ/Web/店舗端末) -> "
                    "Layer 2: 認証・配信基盤(Cloud Run / 非公開 Cloud Storage / Firestore) -> "
                    "Layer 3: AIエージェント基盤(Agent Runtime / Gemini Enterprise / Agent Search) -> "
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
        return json.loads(_strip_code_fences(raw_deck))
    raise ValueError(f"Unsupported deck_spec format: {type(raw_deck)}")


def _coerce_deck_spec(
    raw_spec: Any, theme_color: str
) -> tuple[PresentationDeckSpec | None, str]:
    """Returns (valid_deck_or_None, outline_hint_text).

    The LLM sometimes passes a free-form outline (e.g. a dict with `slides`) instead of a
    schema-compliant PresentationDeckSpec. Instead of raising (which previously aborted the
    whole agent turn), the non-conforming payload is preserved as an outline hint for synthesis.
    """
    if not raw_spec:
        return None, ""
    try:
        deck_dict = _extract_deck_dict_from_state(raw_spec)
    except Exception as exc:  # noqa: BLE001
        logger.info("deck_spec is not JSON; treating as free-form outline hint: %s", exc)
        return None, str(raw_spec)[:6000]
    if isinstance(deck_dict, dict) and theme_color in SUPPORTED_THEME_COLORS:
        deck_dict.setdefault("theme_color", theme_color)
    try:
        return PresentationDeckSpec.model_validate(deck_dict), ""
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "deck_spec failed schema validation; using it as outline hint instead: %s",
            str(exc)[:300],
        )
        return None, json.dumps(deck_dict, ensure_ascii=False)[:6000]


def _get_generation_trigger_mode() -> str:
    """auto (Cloud Run Job -> inline thread), cloud_run_job, inline_thread, sync, or none."""
    return os.environ.get("GENERATION_TRIGGER_MODE", "auto").strip().lower() or "auto"


def _get_generation_job_name() -> str:
    """Fully-qualified Cloud Run Job name used for background generation (empty = not configured)."""
    explicit = os.environ.get("GENERATION_JOB_NAME", "").strip()
    if explicit:
        return explicit
    short = os.environ.get("GENERATION_JOB_ID", "").strip()
    if short:
        return f"projects/{_get_project_id()}/locations/{_get_location()}/jobs/{short}"
    return ""


def _get_generation_stale_minutes() -> int:
    try:
        return max(5, int(os.environ.get("GENERATION_STALE_MINUTES", "13")))
    except ValueError:
        return 13


def _run_cloud_run_generation_job(job_name: str, presentation_id: str) -> str:
    """Triggers the generation Cloud Run Job (REST v2 `jobs.run` with env overrides). Returns the execution/operation name."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    session = AuthorizedSession(credentials)
    url = f"https://run.googleapis.com/v2/{job_name}:run"
    body = {
        "overrides": {
            "containerOverrides": [
                {"env": [{"name": "PRESENTATION_ID", "value": presentation_id}]}
            ],
            "taskCount": 1,
        }
    }
    resp = session.post(url, json=body, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(
            f"jobs.run failed HTTP {resp.status_code}: {resp.text[:400]}"
        )
    payload = resp.json() if resp.content else {}
    return str(
        (payload.get("metadata") or {}).get("name") or payload.get("name") or ""
    )


def _run_generation_inline_thread(presentation_id: str) -> None:
    from app.generation_worker import generate_presentation

    try:
        generate_presentation(presentation_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Inline background generation failed for %s: %s", presentation_id, exc)


def _start_background_generation(presentation_id: str) -> dict[str, Any]:
    """Kicks off asynchronous deck generation and returns how it was dispatched (never raises)."""
    mode = _get_generation_trigger_mode()
    job_name = _get_generation_job_name()
    if mode == "none":
        return {"mode": "none"}
    if mode == "sync":
        from app.generation_worker import generate_presentation

        result = generate_presentation(presentation_id)
        return {"mode": "sync", "worker_result": result}
    if mode in ("auto", "cloud_run_job") and job_name:
        try:
            execution = _run_cloud_run_generation_job(job_name, presentation_id)
            return {"mode": "cloud_run_job", "job_name": job_name, "execution": execution}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Cloud Run Job trigger failed (%s); falling back to inline thread: %s",
                job_name,
                exc,
            )
            if mode == "cloud_run_job":
                return {"mode": "cloud_run_job_failed", "error": str(exc)[:300]}
    worker = threading.Thread(
        target=_run_generation_inline_thread,
        args=(presentation_id,),
        name=f"proposal-gen-{presentation_id}",
        daemon=True,
    )
    worker.start()
    return {"mode": "inline_thread"}


def _tool_error(exc: Exception, action: str, **extra: Any) -> dict[str, Any]:
    """Uniform non-raising error payload so a tool failure never aborts the agent turn."""
    logger.exception("%s failed: %s", action, exc)
    payload: dict[str, Any] = {
        "status": "ERROR",
        "action": action,
        "error_type": type(exc).__name__,
        "error": str(exc)[:600],
        "user_message": (
            "処理中にエラーが発生しました。内容を確認のうえ、必要に応じて条件を変えて再度お試しください。"
        ),
    }
    if isinstance(exc, ValueError) and "not found" in str(exc).lower():
        payload["status"] = "NOT_FOUND"
        payload["user_message"] = (
            "指定されたプレゼンテーションIDが見つかりませんでした。list_proposal_websites で一覧を確認してください。"
        )
    payload.update(extra)
    return payload


def create_proposal_website(
    client_name: str = "",
    proposal_title: str = "",
    proposal_brief: str = "",
    theme_color: str = "sky",
    expiration_days: int = 14,
    deck_spec_json: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Issues the share URL / viewer ID / password immediately and generates the 6-slide HTML5 proposal website in the background.

    Call this tool ONLY when the user explicitly asks to generate/publish a proposal website or approves an outline.
    Do NOT call this tool when the user only says a greeting like 'こんにちは'.
    Pass the agreed outline as natural language inside `proposal_brief`; use `deck_spec_json` only for a complete
    PresentationDeckSpec JSON (non-conforming JSON is accepted and treated as an outline hint, never an error).

    Args:
        client_name: Target client company name (e.g., '株式会社サンプル商事').
        proposal_title: Main title of the proposal presentation.
        proposal_brief: Summary of client challenges, proposed solution, architecture, target ROI, and the agreed slide outline.
        theme_color: Visual accent theme ('sky', 'emerald', 'violet', 'amber', or 'rose').
        expiration_days: Number of days until the shared URL expires (default 14).
        deck_spec_json: Optional full JSON string matching PresentationDeckSpec.
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary with `status` ('GENERATING' while the background generation runs, 'PUBLISHED' when the deck is already
        complete, or 'ERROR'), `presentation_id`, `share_url`, `viewer_id`, `viewer_password`, `expires_at`,
        `generation_status`, and `next_action` guidance. The share URL shows a "生成中" page until the deck is ready and
        then switches to the finished presentation automatically.
    """
    try:
        return _create_proposal_website_impl(
            client_name=client_name,
            proposal_title=proposal_title,
            proposal_brief=proposal_brief,
            theme_color=theme_color,
            expiration_days=expiration_days,
            deck_spec_json=deck_spec_json,
            tool_context=tool_context,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(
            exc,
            "create_proposal_website",
            next_action="クライアント名・提案タイトル・提案概要を確認し、再度 create_proposal_website を呼び出してください。",
        )


def _create_proposal_website_impl(
    client_name: str,
    proposal_title: str,
    proposal_brief: str,
    theme_color: str,
    expiration_days: int,
    deck_spec_json: str,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    raw_spec = tool_context.state.get("deck_spec") if tool_context else None
    if not raw_spec and deck_spec_json:
        raw_spec = deck_spec_json

    theme = theme_color if theme_color in SUPPORTED_THEME_COLORS else "sky"
    deck_obj, outline_hint = _coerce_deck_spec(raw_spec, theme)

    eff_client = (client_name or "").strip()
    eff_title = (proposal_title or "").strip()
    eff_brief = (proposal_brief or "").strip()
    if deck_obj is not None:
        eff_client = eff_client or deck_obj.client_name
        eff_title = eff_title or deck_obj.proposal_title
        eff_brief = eff_brief or deck_obj.subtitle
    if not eff_client and not eff_title and not eff_brief and not outline_hint:
        return {
            "status": "ERROR",
            "action": "create_proposal_website",
            "error_type": "MissingInput",
            "error": "Either (client_name, proposal_title, proposal_brief) or deck_spec must be provided.",
            "user_message": "クライアント名・提案タイトル・提案概要のいずれかをご指定ください。",
            "next_action": "ユーザーにクライアント企業名と提案テーマをヒアリングしてから再度呼び出してください。",
        }
    eff_client = eff_client or "クライアント企業"
    eff_title = eff_title or f"{eff_client}様向け AI×UX変革ご提案プレゼンテーション"
    eff_brief = eff_brief or eff_title

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_jst = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    exp_days = max(1, min(int(expiration_days or 14), 365))
    expires_utc = now_utc + datetime.timedelta(days=exp_days)

    short_id = uuid.uuid4().hex[:8]
    date_prefix = now_jst.strftime("%Y%m%d")
    presentation_id = f"prop-{date_prefix}-{short_id}"

    slug_source = deck_obj.client_slug if deck_obj is not None else eff_client
    safe_slug = re.sub(r"[^a-z0-9-]+", "-", slug_source.lower()).strip("-")
    if safe_slug:
        viewer_id = f"client-{safe_slug[:16]}-{secrets.token_hex(2)}"
    else:
        # Non-ASCII client names (e.g. Japanese) produce no slug -> use the presentation short id.
        viewer_id = f"client-{short_id}-{secrets.token_hex(2)}"
    viewer_password = secrets.token_urlsafe(12)
    password_hash, password_salt = hash_password(viewer_password)

    project_id = _get_project_id()
    bucket_name = _get_bucket_name()
    collection_name = _get_firestore_collection()
    hosting_base_url = _get_hosting_base_url()
    blob_path = f"presentations/{presentation_id}/index.html"
    gcs_uri = f"gs://{bucket_name}/{blob_path}"
    share_url = f"{hosting_base_url}/p/{presentation_id}"
    expires_label = expires_utc.strftime(f"%Y-%m-%d %H:%M UTC ({exp_days}日間有効)")

    base_doc: dict[str, Any] = {
        "presentation_id": presentation_id,
        "viewer_id": viewer_id,
        "password_hash": password_hash,
        "password_salt": password_salt,
        "gcs_bucket": bucket_name,
        "gcs_blob_path": blob_path,
        "gcs_uri": gcs_uri,
        "client_name": eff_client,
        "proposal_title": eff_title,
        "subtitle": eff_brief[:160],
        "theme_color": theme,
        "skill_applied": "interactive-slide-designer",
        "created_at": now_utc.isoformat(),
        "updated_at": now_utc.isoformat(),
        "expires_at": expires_utc.isoformat(),
        "status": "active",
        "is_active": True,
    }

    fs_client = _get_firestore_client(project_id)
    doc_ref = fs_client.collection(collection_name).document(presentation_id)

    # --- Fast path: a complete, schema-valid deck was supplied -> render & publish synchronously (no LLM work)
    if deck_obj is not None:
        deck_obj.theme_color = theme
        html_content = render_deck_html(
            deck_obj, generated_date=now_jst.strftime("%Y-%m-%d %H:%M JST")
        )
        from google.cloud import storage

        storage_client = storage.Client(project=project_id)
        blob = storage_client.bucket(bucket_name).blob(blob_path)
        blob.cache_control = "no-store, private"
        blob.upload_from_string(
            html_content.encode("utf-8"), content_type="text/html; charset=utf-8"
        )
        doc_ref.set(
            {
                **base_doc,
                "client_name": deck_obj.client_name,
                "proposal_title": deck_obj.proposal_title,
                "subtitle": deck_obj.subtitle,
                "deck_spec": deck_obj.model_dump(),
                "generation_status": "ready",
                "generation_phase": "ready",
                "generation_engine": "state_deck_spec",
                "generation_requested_at": now_utc.isoformat(),
                "ready_at": now_utc.isoformat(),
            }
        )
        result = {
            "status": "PUBLISHED",
            "generation_status": "ready",
            "presentation_id": presentation_id,
            "client_name": deck_obj.client_name,
            "proposal_title": deck_obj.proposal_title,
            "theme_color": deck_obj.theme_color,
            "generation_engine": "state_deck_spec",
            "generation_engine_label": describe_generation_engine("state_deck_spec"),
            "skill_applied": "interactive-slide-designer",
            "share_url": share_url,
            "viewer_id": viewer_id,
            "viewer_password": viewer_password,
            "expires_at": expires_label,
            "gcs_uri": gcs_uri,
            "slide_outline": _build_slide_outline(deck_obj),
            "next_action": "URL・閲覧用ID・パスワード・有効期限をユーザーに提示してください。",
        }
        if tool_context is not None:
            tool_context.state["published_result"] = result
            tool_context.state["published_presentation"] = result
        return result

    # --- Async path: issue credentials now, generate the deck in the background
    doc_ref.set(
        {
            **base_doc,
            "generation_status": "generating",
            "generation_phase": "queued",
            "generation_engine": "",
            "generation_requested_at": now_utc.isoformat(),
            "generation_inputs": {
                "client_name": eff_client,
                "proposal_title": eff_title,
                "proposal_brief": eff_brief,
                "theme_color": theme,
                "outline_hint": outline_hint,
                "expiration_days": exp_days,
            },
        }
    )

    dispatch = _start_background_generation(presentation_id)
    dispatch_mode = str(dispatch.get("mode", "unknown"))
    try:
        doc_ref.update(
            {
                "generation_dispatch": dispatch_mode,
                "generation_execution": str(dispatch.get("execution", ""))[:300],
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("dispatch bookkeeping skipped: %s", exc)

    status_value = "GENERATING"
    generation_status = "generating"
    generation_engine = ""
    if dispatch_mode == "sync":
        worker_result = dispatch.get("worker_result") or {}
        generation_status = str(worker_result.get("generation_status", "ready"))
        generation_engine = str(worker_result.get("generation_engine", ""))
        status_value = "PUBLISHED" if generation_status == "ready" else "GENERATING"

    result = {
        "status": status_value,
        "generation_status": generation_status,
        "generation_dispatch": dispatch_mode,
        "presentation_id": presentation_id,
        "client_name": eff_client,
        "proposal_title": eff_title,
        "theme_color": theme,
        "generation_engine": generation_engine,
        "generation_engine_label": describe_generation_engine(generation_engine)
        if generation_engine
        else "生成中（Managed Agents API → 時間予算超過時は gemini-3.8-flash へ自動切替）",
        "generation_plan": (
            f"1) Managed Agents API ({MANAGED_AGENT_MODEL}) 最大約{_get_managed_agent_deadline_seconds() // 60}分 → "
            f"2) {_get_model_name()} 高速生成 → 3) テンプレート即時生成（必ず完成させます）"
        ),
        "estimated_completion": "通常1〜5分（最長でも約10分で自動完成）",
        "skill_applied": "interactive-slide-designer",
        "share_url": share_url,
        "viewer_id": viewer_id,
        "viewer_password": viewer_password,
        "expires_at": expires_label,
        "gcs_uri": gcs_uri,
        "outline_hint_applied": bool(outline_hint),
        "next_action": (
            "URL・閲覧用ID・パスワード・有効期限を今すぐユーザーに提示し、"
            "『現在AIが生成中で、同じURLを開くと生成中画面が表示され、完成すると自動的に提案ページへ切り替わります』と案内してください。"
            "進捗を聞かれたら get_proposal_status を呼び出してください。"
        ),
    }
    if tool_context is not None:
        tool_context.state["published_result"] = result
        tool_context.state["published_presentation"] = result
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
# Tool 2b: Generation Status (with stale-generation self-repair)
# ---------------------------------------------------------------------------


def get_proposal_status(
    presentation_id: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Reports the generation status / engine of a proposal website (and self-heals stale generations).

    Use when the user asks '生成状況を教えて', 'まだ完成しない？', or 'どのエンジンで生成された？'.
    If a background generation has been running longer than the time budget, this tool completes it
    immediately with the skill template so the share URL always ends up with a finished deck.

    Args:
        presentation_id: Target presentation ID (e.g., 'prop-20261003-xxxxxxxx').
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary with `generation_status` ('generating' | 'ready' | 'failed'), `generation_phase`,
        `generation_engine`, `generation_engine_label`, `elapsed_seconds`, `share_url`, and `viewer_id`.
    """
    try:
        project_id = _get_project_id()
        collection_name = _get_firestore_collection()
        hosting_base_url = _get_hosting_base_url()
        fs_client = _get_firestore_client(project_id)
        doc_ref = fs_client.collection(collection_name).document(presentation_id)
        doc_snap = doc_ref.get()
        if not doc_snap.exists:
            return {
                "status": "NOT_FOUND",
                "presentation_id": presentation_id,
                "user_message": f"プレゼンテーション '{presentation_id}' は見つかりませんでした。",
            }
        data = doc_snap.to_dict() or {}
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        gen_status = str(data.get("generation_status") or "").lower()
        if not gen_status:
            gen_status = "ready" if data.get("deck_spec") else "unknown"
        requested_raw = str(
            data.get("generation_requested_at") or data.get("created_at") or ""
        )
        elapsed_seconds: float | None = None
        try:
            requested_dt = datetime.datetime.fromisoformat(requested_raw)
            if requested_dt.tzinfo is None:
                requested_dt = requested_dt.replace(tzinfo=datetime.timezone.utc)
            elapsed_seconds = round((now_utc - requested_dt).total_seconds(), 1)
        except Exception:  # noqa: BLE001
            elapsed_seconds = None

        repaired = False
        stale_after = _get_generation_stale_minutes() * 60
        if gen_status == "generating" and elapsed_seconds is not None and elapsed_seconds > stale_after:
            from app.generation_worker import finalize_with_fallback

            repair = finalize_with_fallback(
                presentation_id, reason=f"stale_after_{int(elapsed_seconds)}s"
            )
            data.update(repair.get("doc_updates", {}))
            gen_status = str(data.get("generation_status") or "ready")
            repaired = True

        engine = str(data.get("generation_engine") or "")
        phase = str(data.get("generation_phase") or gen_status)
        if gen_status == "ready":
            message = f"生成は完了しています（{describe_generation_engine(engine)}）。共有URLを開くと提案ページが表示されます。"
        elif gen_status == "generating":
            message = (
                f"現在生成中です（フェーズ: {phase}、経過 {int(elapsed_seconds or 0)} 秒）。"
                "共有URLでは生成中画面が表示され、完成すると自動的に提案ページへ切り替わります。"
            )
        elif gen_status == "failed":
            message = f"生成に失敗しました: {str(data.get('generation_error') or '')[:200]}"
        else:
            message = "生成状況を判定できませんでした。"
        result = {
            "status": "STATUS",
            "presentation_id": presentation_id,
            "client_name": data.get("client_name", ""),
            "proposal_title": data.get("proposal_title", ""),
            "generation_status": gen_status,
            "generation_phase": phase,
            "generation_detail": data.get("generation_detail", ""),
            "generation_engine": engine,
            "generation_engine_label": describe_generation_engine(engine) if engine else "",
            "generation_dispatch": data.get("generation_dispatch", ""),
            "elapsed_seconds": elapsed_seconds,
            "stale_repair_applied": repaired,
            "ready_at": data.get("ready_at", ""),
            "share_url": f"{hosting_base_url}/p/{presentation_id}",
            "viewer_id": data.get("viewer_id", ""),
            "expires_at": data.get("expires_at", ""),
            "is_active": bool(data.get("is_active", True)),
            "user_message": message,
        }
        if tool_context is not None:
            tool_context.state["last_status_result"] = result
        return result
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "get_proposal_status", presentation_id=presentation_id)


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
    """Edits an existing published proposal website and updates the live HTML in Private Cloud Storage.

    If the deck is still being generated in the background, returns `status='GENERATING'` instead of editing.

    Args:
        presentation_id: ID of the existing presentation (e.g., 'prop-20261003-xxxxxxxx').
        edit_instructions: Natural-language description of changes to apply to the slides.
        new_title: Optional explicit replacement for the proposal title.
        new_subtitle: Optional explicit replacement for the proposal subtitle.
        new_theme_color: Optional new accent color ('sky', 'emerald', 'violet', 'amber', 'rose').
        new_custom_callout: Optional callout badge text to display on the cover slide.
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary containing `status` ('UPDATED', 'GENERATING', 'NOT_FOUND' or 'ERROR'), `presentation_id`,
        `share_url`, `updated_fields`, and `slide_outline`.
    """
    try:
        return _edit_proposal_website_impl(
            presentation_id=presentation_id,
            edit_instructions=edit_instructions,
            new_title=new_title,
            new_subtitle=new_subtitle,
            new_theme_color=new_theme_color,
            new_custom_callout=new_custom_callout,
            tool_context=tool_context,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "edit_proposal_website", presentation_id=presentation_id)


def _edit_proposal_website_impl(
    presentation_id: str,
    edit_instructions: str,
    new_title: str = "",
    new_subtitle: str = "",
    new_theme_color: str = "",
    new_custom_callout: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Edits an existing published proposal website and updates the live HTML in Private Cloud Storage.

    Args:
        presentation_id: ID of the existing presentation (e.g., 'prop-20261003-xxxxxxxx').
        edit_instructions: Natural-language description of changes to apply to the slides.
        new_title: Optional explicit replacement for the proposal title.
        new_subtitle: Optional explicit replacement for the proposal subtitle.
        new_theme_color: Optional new accent color ('sky', 'emerald', 'violet', 'amber', 'rose').
        new_custom_callout: Optional callout badge text to display on the cover slide.
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary containing `status='UPDATED'`, `presentation_id`, `share_url`, `updated_fields`, and `slide_outline`.
    """
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
    if str(doc_data.get("generation_status") or "").lower() == "generating":
        return {
            "status": "GENERATING",
            "presentation_id": presentation_id,
            "generation_phase": doc_data.get("generation_phase", ""),
            "share_url": f"{hosting_base_url}/p/{presentation_id}",
            "user_message": (
                "このプレゼンテーションはまだ生成中のため、まだ修正できません。"
                "完成後（共有URLが提案ページに切り替わった後）に再度ご指示ください。"
            ),
        }
    bucket_name = doc_data.get("gcs_bucket") or _get_bucket_name()
    blob_path = (
        doc_data.get("gcs_blob_path") or f"presentations/{presentation_id}/index.html"
    )
    storage_client = storage.Client(project=project_id)
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    existing_html = ""
    try:
        if hasattr(blob, "download_as_text"):
            existing_html = blob.download_as_text(encoding="utf-8")
        elif hasattr(blob, "download_as_bytes"):
            existing_html = blob.download_as_bytes().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.info("Existing HTML blob read skipped or unavailable: %s", exc)

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
        active_model = _get_model_name()
        genai_location = _get_genai_location(active_model)
        candidate_locations = [genai_location]
        if genai_location != "global":
            candidate_locations.append("global")
        html_excerpt = existing_html[:1500] if existing_html else "(Not cached)"
        edit_prompt = f"""既存の6枚構成プレゼンテーションデータ（JSON）およびCloud Storage上の現行HTMLに対して、ユーザーの修正指示を反映した新しい `PresentationDeckSpec` JSONを出力してください。
変更指示がないフィールドは既存の値を維持してください。

【Cloud Storage上の現行HTML抜粋 ({blob_path})】:
{html_excerpt}

【現在のPresentationDeckSpec JSON】:
{json.dumps(deck_obj.model_dump(), ensure_ascii=False, indent=2)}

【ユーザーの修正指示】:
{edit_instructions}
"""
        for loc in candidate_locations:
            try:
                client = genai.Client(
                    vertexai=True,
                    project=project_id,
                    location=loc,
                )
                resp = client.models.generate_content(
                    model=active_model,
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
                    break
            except Exception as exc:
                logger.info(
                    "LLM edit (%s at %s) fallback to deterministic field updates: %s",
                    active_model,
                    loc,
                    exc,
                )

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
        "existing_html_loaded": bool(existing_html),
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
    """Lists existing proposal websites registered in Firestore with status, generation state, URLs, and access counts.

    Args:
        client_filter: Optional substring to filter by client_name or proposal_title.
        include_revoked: Whether to include revoked/inactive presentations (default True).
        limit: Maximum number of presentations to return (default 20).

    Returns:
        Dictionary with `count` and `presentations` list (each with `generation_status` / `generation_engine`).
    """
    try:
        return _list_proposal_websites_impl(
            client_filter=client_filter, include_revoked=include_revoked, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "list_proposal_websites")


def _list_proposal_websites_impl(
    client_filter: str = "",
    include_revoked: bool = True,
    limit: int = 20,
) -> dict[str, Any]:
    """Lists existing proposal websites registered in Firestore with status, URLs, and access counts.

    Args:
        client_filter: Optional substring to filter by client_name or proposal_title.
        include_revoked: Whether to include revoked/inactive presentations (default True).
        limit: Maximum number of presentations to return (default 20).

    Returns:
        Dictionary with `count` and `presentations` list.
    """
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
                "generation_status": data.get("generation_status", "ready" if data.get("deck_spec") else ""),
                "generation_engine": data.get("generation_engine", ""),
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
    """Retrieves viewer authentication and access audit logs for a specific presentation.

    Args:
        presentation_id: Target presentation ID (e.g., 'prop-20261003-xxxxxxxx').
        limit: Maximum number of recent log entries to return (default 20).

    Returns:
        Dictionary with presentation summary and `access_logs` list (or `status='NOT_FOUND'`/`'ERROR'`).
    """
    try:
        return _get_proposal_access_logs_impl(presentation_id=presentation_id, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "get_proposal_access_logs", presentation_id=presentation_id)


def _get_proposal_access_logs_impl(
    presentation_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Retrieves viewer authentication and access audit logs for a specific presentation.

    Args:
        presentation_id: Target presentation ID (e.g., 'prop-20261003-xxxxxxxx').
        limit: Maximum number of recent log entries to return (default 20).

    Returns:
        Dictionary with presentation summary and `access_logs` list.
    """
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
    """Rotates the viewer password, updates viewer_id, or extends the expiration date for a presentation.

    Args:
        presentation_id: Target presentation ID.
        rotate_password: If True, generates a new random password and updates its PBKDF2 hash in Firestore.
        new_viewer_id: Optional custom viewer_id to set.
        extend_days: If > 0, extends `expires_at` by this many days from now and ensures the presentation is active.
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary with updated `viewer_id`, `new_viewer_password` (if rotated), `expires_at`, and `share_url`
        (or `status='NOT_FOUND'`/`'ERROR'`).
    """
    try:
        return _manage_proposal_credentials_impl(
            presentation_id=presentation_id,
            rotate_password=rotate_password,
            new_viewer_id=new_viewer_id,
            extend_days=extend_days,
            tool_context=tool_context,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "manage_proposal_credentials", presentation_id=presentation_id)


def _manage_proposal_credentials_impl(
    presentation_id: str,
    rotate_password: bool = True,
    new_viewer_id: str = "",
    extend_days: int = 0,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Rotates the viewer password, updates viewer_id, or extends the expiration date for a presentation.

    Args:
        presentation_id: Target presentation ID.
        rotate_password: If True, generates a new random password and updates its PBKDF2 hash in Firestore.
        new_viewer_id: Optional custom viewer_id to set.
        extend_days: If > 0, extends `expires_at` by this many days from now and ensures the presentation is active.
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary with updated `viewer_id`, `new_viewer_password` (if rotated), `expires_at`, and `share_url`.
    """
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
    """Revokes external access to a proposal website (and optionally deletes the HTML blob from GCS).

    Once called, the Cloud Run Hosting Gateway immediately returns HTTP 403 Forbidden for `/p/<presentation_id>`.

    Args:
        presentation_id: Target presentation ID to revoke/delete.
        hard_delete_gcs: If True, also deletes `presentations/<presentation_id>/index.html` from Private GCS.
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary confirming `status='REVOKED'` and whether the GCS blob was deleted (or `status='NOT_FOUND'`/`'ERROR'`).
    """
    try:
        return _delete_proposal_website_impl(
            presentation_id=presentation_id,
            hard_delete_gcs=hard_delete_gcs,
            tool_context=tool_context,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "delete_proposal_website", presentation_id=presentation_id)


def _delete_proposal_website_impl(
    presentation_id: str,
    hard_delete_gcs: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Revokes external access to a proposal website (and optionally deletes the HTML blob from GCS).

    Once called, the Cloud Run Hosting Gateway immediately returns HTTP 403 Forbidden for `/p/<presentation_id>`.

    Args:
        presentation_id: Target presentation ID to revoke/delete.
        hard_delete_gcs: If True, also deletes `presentations/<presentation_id>/index.html` from Private GCS.
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary confirming `status='REVOKED'` and whether the GCS blob was deleted.
    """
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
   - ユーザーがクライアント名と提案テーマを指定して「提案プレゼンテーションHTMLを作成・公開してください」「この内容でWebサイトを発行して」「はい、お願いします」と明示的に依頼・承認した場合にのみ、`create_proposal_website` を呼び出してください。
   - 呼び出し時は `client_name` / `proposal_title` / `proposal_brief` / `theme_color` を自然言語で渡してください。チャットで合意した構成案やスライドごとの要点は **`proposal_brief` の中に文章として含めてください**。`deck_spec_json` には完全な `PresentationDeckSpec` JSON が手元にある場合以外は何も渡さないでください（独自形式の構成JSONを渡す必要はありません）。
   - このツールは **即座に** 共有URL・閲覧用ID・パスワードを発行して返します（`status` が `GENERATING`）。スライド本体は裏側で Managed Agents API（Antigravity ハーネス）が生成し、最大約10分の時間予算を超えた場合は自動的に gemini-3.8-flash の高速生成へ切り替わるため、必ず完成します。
   - ツール応答を受け取ったら、**その同じターン内で必ず** 以下を日本語でわかりやすく提示してください（決して無言で終わらないこと）：
     1. **プレゼンテーションID** (`presentation_id`)
     2. **顧客共有用プレゼンテーションURL** (`share_url`)
     3. **閲覧用ID** (`viewer_id`)
     4. **初期パスワード** (`viewer_password`)
     5. **有効期限** (`expires_at`) と **デザインテーマ** (`theme_color`)
     6. 生成状況の案内：「現在AIがスライドを生成中です。URLを開くと生成中画面が表示され、完成すると自動的に提案ページへ切り替わります（通常1〜5分、最長でも約10分）。」
   - `status` が `PUBLISHED` の場合は、既に完成済みであることと `slide_outline` の構成サマリーを提示してください。
   - `status` が `ERROR` の場合は、`user_message` と `next_action` に従ってユーザーに状況を説明し、必要な情報を確認してください。

4. **生成状況の確認 (`get_proposal_status`)**:
   - 「生成状況を教えて」「まだ完成しない？」「どのエンジンで作られた？」と聞かれたら `get_proposal_status` を呼び出し、`generation_status`（generating / ready / failed）、`generation_phase`、経過時間、完成時は `generation_engine_label`（Managed Agents API で完成したのか、gemini-3.8-flash 高速生成に自動切替されたのか）を報告してください。

5. **発行済みWebサイトの管理・修正・削除（ライフサイクル管理ツール）**:
   - **一覧確認**: 「発行済みのサイト一覧を見せて」と言われたら `list_proposal_websites` を呼び出してください。
   - **閲覧監査ログ確認**: 「誰がいつアクセスしたかログを見せて」と言われたら `get_proposal_access_logs` を呼び出してください。
   - **内容・デザインの修正**: 「発行済みの `<presentation_id>` のタイトルやテーマカラー、内容を修正して」と言われたら `edit_proposal_website` を呼び出し、同じURLのまま最新HTMLへ更新したことを伝えてください。`status` が `GENERATING` なら、まだ生成中のため完成後に再度依頼いただくよう案内してください。
   - **パスワード再発行・期限延長**: 「パスワードを再発行して」「有効期限を延長して」と言われたら `manage_proposal_credentials` を呼び出し、新しい認証情報を提示してください。
   - **公開停止・削除**: 「`<presentation_id>` の公開を停止（削除）して」と言われたら `delete_proposal_website` を呼び出し、外部からのアクセスが即座に遮断（HTTP 403）されたことを報告してください。
   - いずれのツールも `status` が `NOT_FOUND` / `ERROR` の場合は、その旨と `user_message` をユーザーに伝えてください。
"""

root_agent = LlmAgent(
    name="proposal_site_publisher_agent",
    model=Gemini(
        model=MODEL,
        client_kwargs={"location": _get_genai_location(MODEL)},
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "対話型コンシェルジュによるクライアント提案用HTML5スライドWebサイト生成・限定公開・ライフサイクル管理エージェント。"
        "ヒアリングと社内ナレッジ検索、6枚構成インタラクティブHTMLサイトの非同期生成（Managed Agents API → gemini-3.8-flash 自動フォールバック）、"
        "発行後の修正・閲覧ログ確認・パスワード再発行・公開停止（削除）を一気通貫で実行します。"
    ),
    instruction=CONCIERGE_INSTRUCTION,
    tools=[
        search_internal_knowledge,
        create_proposal_website,
        get_proposal_status,
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
