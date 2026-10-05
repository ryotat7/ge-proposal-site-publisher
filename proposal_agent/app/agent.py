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
from collections.abc import Callable, Mapping, Sequence
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
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
SKILL_DIR = Path(__file__).resolve().parent / "skills" / "interactive-slide-designer"

SUPPORTED_THEME_COLORS = {"sky", "emerald", "violet", "amber", "rose"}

# Overall look of the deck (background / text colour / card material). `theme_color` is only the accent colour.
SUPPORTED_DESIGN_STYLES = ("immersive-dark", "clean-light", "editorial-light")
DEFAULT_DESIGN_STYLE = "immersive-dark"
DESIGN_STYLE_LABELS = {
    "immersive-dark": "濃紺ダーク（immersive-dark）",
    "clean-light": "白基調クリーン（clean-light）",
    "editorial-light": "生成り色エディトリアル（editorial-light）",
}
SUPPORTED_UI_FORMATS = ("portal", "slides")
DEFAULT_UI_FORMAT = "portal"
UI_FORMAT_LABELS = {
    "portal": "4カラムWeb提案ポータル形式（portal）",
    "slides": "16:9 プレゼンスライド形式（slides）",
}
_UI_FORMAT_ALIASES = {
    "portal": "portal",
    "web": "portal",
    "article": "portal",
    "doc": "portal",
    "document": "portal",
    "kumihan": "portal",
    "ポータル": "portal",
    "記事": "portal",
    "ドキュメント": "portal",
    "4カラム": "portal",
    "slides": "slides",
    "slide": "slides",
    "deck": "slides",
    "16:9": "slides",
    "presentation": "slides",
    "スライド": "slides",
    "プレゼン": "slides",
}
_DESIGN_STYLE_ALIASES = {
    "editorial": "editorial-light",
    "magazine": "editorial-light",
    "paper": "editorial-light",
    "serif": "editorial-light",
    "dark": "immersive-dark",
    "immersive": "immersive-dark",
    "black": "immersive-dark",
    "navy": "immersive-dark",
    "light": "clean-light",
    "white": "clean-light",
    "clean": "clean-light",
    "minimal": "clean-light",
}
CUSTOM_CSS_MAX_CHARS = 8000

# Tokens that could load external resources, execute script, or smuggle markup out of the <style> element.
_CSS_FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"@\s*import", "@blocked-import"),
    (r"@\s*namespace", "@blocked-namespace"),
    (r"@\s*charset", "@blocked-charset"),
    (r"@\s*font-face", "@blocked-font-face"),
    (r"url\s*\(", "blocked("),
    (r"src\s*\(", "blocked("),
    (r"image-set\s*\(", "blocked("),
    (r"image\s*\(", "blocked("),
    (r"element\s*\(", "blocked("),
    (r"expression\s*\(", "blocked("),
    (r"javascript\s*:", "blocked:"),
    (r"vbscript\s*:", "blocked:"),
    (r"-moz-binding", "blocked-binding"),
    (r"behavior\s*:", "blocked:"),
)


def normalize_design_style(value: Any, default: str = DEFAULT_DESIGN_STYLE) -> str:
    """Maps free-form style names (e.g. 'white', 'Light mode') onto a supported design style."""
    raw = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not raw:
        return default
    if raw in SUPPORTED_DESIGN_STYLES:
        return raw
    if raw in _DESIGN_STYLE_ALIASES:
        return _DESIGN_STYLE_ALIASES[raw]
    for key, mapped in _DESIGN_STYLE_ALIASES.items():
        if key in raw:
            return mapped
    return default


def normalize_ui_format(value: Any, default: str = DEFAULT_UI_FORMAT) -> str:
    """Maps UI format names onto 'portal' (default 4-column web proposal portal) or 'slides' (16:9 slide deck)."""
    raw = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not raw:
        return default
    if raw in SUPPORTED_UI_FORMATS:
        return raw
    if raw in _UI_FORMAT_ALIASES:
        return _UI_FORMAT_ALIASES[raw]
    for key, mapped in _UI_FORMAT_ALIASES.items():
        if key in raw:
            return mapped
    return default


def sanitize_custom_css(css: Any, max_chars: int = CUSTOM_CSS_MAX_CHARS) -> str:
    """Neutralises LLM-authored CSS before it is embedded verbatim inside a <style> element.

    Defence in depth: removes '<' (no </style> breakout) and backslashes (no CSS escape tricks such as
    `\75 rl(`), strips comments and control characters, disables url()/@import/expression()/javascript: and
    similar constructs, breaks up '{{' / '}}' (reserved by the DOM validator) and caps the length.
    """
    if not css:
        return ""
    text = str(css)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = text.replace("<", "").replace("\\", "")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    for pattern, replacement in _CSS_FORBIDDEN_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    while "{{" in text or "}}" in text:
        text = text.replace("{{", "{ {").replace("}}", "} }")
    text = text.strip()
    if len(text) > max_chars:
        cut = text.rfind("}", 0, max_chars)
        text = text[: cut + 1] if cut > 0 else ""
    return text


def _normalize_deck_in_place(deck_obj: "PresentationDeckSpec") -> "PresentationDeckSpec":
    """Coerces theme / design style / UI format / custom CSS to safe, supported values (idempotent)."""
    if deck_obj.theme_color not in SUPPORTED_THEME_COLORS:
        deck_obj.theme_color = "sky"
    deck_obj.design_style = normalize_design_style(deck_obj.design_style)
    deck_obj.ui_format = normalize_ui_format(getattr(deck_obj, "ui_format", DEFAULT_UI_FORMAT))
    deck_obj.custom_css = sanitize_custom_css(deck_obj.custom_css)
    return deck_obj


def _get_project_id() -> str:
    return (
        os.environ.get("PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or "your-gcp-project-id"
    )


def _get_location() -> str:
    return os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")


def _get_model_name() -> str:
    return (
        os.environ.get("PROPOSAL_AGENT_MODEL")
        or os.environ.get("GEMINI_MODEL")
        or MODEL
    )


def _get_genai_location(model_name: str | None = None) -> str:
    explicit_loc = os.environ.get("GENAI_LOCATION")
    if explicit_loc:
        return explicit_loc
    target = (model_name or _get_model_name()).lower()
    if target.startswith("gemini-3"):
        return "global"
    return _get_location()


os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", _get_project_id())
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", _get_location())


def _get_bucket_name() -> str:
    return os.environ.get(
        "PROPOSAL_GCS_BUCKET", f"{_get_project_id()}-proposals"
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
    return (
        os.environ.get("AGENT_SEARCH_DATASTORE_ID")
        or os.environ.get("AGENT_SEARCH_DATASTORE_IDS")
        or "proposal-knowledge-datastore"
    )


def _normalize_datastore_id(token: str) -> str:
    """Extracts the bare DataStore ID if a full projects/.../dataStores/<id> resource name was provided."""
    cleaned = token.strip().strip("/")
    if "/dataStores/" in cleaned:
        cleaned = cleaned.split("/dataStores/", 1)[1].split("/", 1)[0].strip()
    return cleaned


def _get_datastore_ids() -> list[str]:
    """Parses one or more Agent Search DataStore IDs from AGENT_SEARCH_DATASTORE_ID(S) (comma/colon/semicolon-separated)."""
    raw = (
        os.environ.get("AGENT_SEARCH_DATASTORE_IDS")
        or os.environ.get("AGENT_SEARCH_DATASTORE_ID")
        or "proposal-knowledge-datastore"
    )
    parsed: list[str] = []
    for token in re.split(r"[,:;|\s]+", raw):
        cleaned = _normalize_datastore_id(token)
        if cleaned and cleaned not in parsed:
            parsed.append(cleaned)
    return parsed or ["proposal-knowledge-datastore"]


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
    phase_name: str = Field(description="フェーズ名（例：Phase 1: 基盤構築・実証）")
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
    design_style: str = Field(
        default=DEFAULT_DESIGN_STYLE,
        description=(
            "全体のデザインスタイル（背景・文字色・カードの質感）。immersive-dark（濃紺ダーク・グロー効果）、"
            "clean-light（白背景・濃いグレー文字・白カードと薄い影のミニマル）、"
            "editorial-light（生成り色の背景・明朝体見出し・フラットなカード）のいずれか"
        ),
    )
    ui_format: str = Field(
        default=DEFAULT_UI_FORMAT,
        description=(
            "UI形式（レイアウト）。portal（既定：4カラム構成のWeb提案ポータル形式＋スライド表示切替ボタン付き）"
            "または slides（16:9 固定キャンバスのプレゼンスライド形式）のいずれか"
        ),
    )
    custom_css: str = Field(
        default="",
        description=(
            "任意の追加CSS（デザイン微調整用・通常は空文字）。各宣言に !important を付け、url()・@import・外部リソース・"
            "山括弧・バックスラッシュは使わないこと（8000文字以内）"
        ),
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
    ui_format: str | None = None,
) -> str:
    """Renders the 6-slide bespoke HTML5 presentation using deck_base.html.j2."""
    if isinstance(deck_spec, dict):
        deck_obj = PresentationDeckSpec.model_validate(deck_spec)
    else:
        deck_obj = deck_spec

    if ui_format is not None:
        deck_obj.ui_format = normalize_ui_format(ui_format)
    _normalize_deck_in_place(deck_obj)

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
        design_style=deck_obj.design_style,
        ui_format=deck_obj.ui_format,
        custom_css=deck_obj.custom_css,
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
    slide_matches = re.findall(r'<[a-zA-Z][^<>]*?\sdata-slide-index="(\d+)"', html_str)
    if slide_matches != ["0", "1", "2", "3", "4", "5"]:
        raise ValueError(
            f"Expected 6 slides with indices 0..5, found: {slide_matches}"
        )
    layouts = re.findall(r'<[a-zA-Z][^<>]*?\sdata-layout="([^"]+)"', html_str)
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
        f"Slide 01 [Cover / {deck_obj.theme_color} / {deck_obj.design_style}]: {deck_obj.client_name} 御中 - {deck_obj.proposal_title}",
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
    clean_client = re.sub(r"(御中|様)$", "", client_name.strip()).strip() or "Client"
    slug = re.sub(r"[^a-z0-9-]+", "-", clean_client.lower()).strip("-") or "client"
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
        custom_callout="Interactive Slide Designer Skill 適用済み",
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
            f"{_get_brand_name()}のUXデザイン知見とGoogle Cloud (BigQuery + Gemini Enterprise Agent Platform) を融合し、"
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
                components=["Cloud Run 認証GW", "非公開 Cloud Storage", "Firestore セッション管理"],
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
                phase_name="Phase 1: 構想設計・実証検証",
                period="Month 1 - 2",
                deliverables=[
                    "カスタマージャーニー設計と優先ユースケース定義",
                    "BigQuery・Agent Searchへの初期データ統合",
                    "AIエージェントのプロトタイプ実装と社内検証",
                ],
                milestone="プロトタイプ合意・実証効果の測定完了",
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
    design_style: str = DEFAULT_DESIGN_STYLE,
) -> str:
    style_label = DESIGN_STYLE_LABELS.get(design_style, design_style)
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
【デザインスタイル】: {style_label}（design_style には {design_style} を設定し、custom_css は空文字にしてください）
{outline_block}
【社内ナレッジ検索結果】:
{knowledge_context}

【製品名の表記ルール】: Google Cloud 製品は現行の正式名称（Gemini Enterprise Agent Platform / Agent Runtime / Agent Search / Gemini 3.8 Flash / BigQuery / Cloud Run）で表記し、旧ブランド名（Gemini Enterprise Agent Platform へ改称する前の名称）や旧世代のモデル名は使わないでください。

【適用スキル (interactive-slide-designer)】:
{skill_text}
"""


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
    if genai_location != "global":
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


def synthesize_deck_spec_with_skill(
    client_name: str,
    proposal_title: str,
    proposal_brief: str,
    theme_color: str = "sky",
    knowledge_context: str = "",
    outline_hint: str = "",
    status_callback: Callable[[str, str], None] | None = None,
    design_style: str = DEFAULT_DESIGN_STYLE,
) -> tuple[PresentationDeckSpec, str]:
    """Synthesizes a 6-slide PresentationDeckSpec guided by the interactive-slide-designer skill.

    Tiered strategy (each tier is time-boxed so publication is always guaranteed):
      1. Gemini structured output (`GEMINI_MODEL`, default gemini-3.8-flash) within `FAST_MODEL_TIMEOUT_SECONDS`.
      2. Deterministic skill template (offline / last resort).
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
        design_style=normalize_design_style(design_style),
    )
    fast_model = _get_model_name()
    fast_timeout = _get_fast_model_timeout_seconds()

    # 1. Gemini structured output with skill instructions
    try:
        deck_obj, _ = _synthesize_via_gemini(
            prompt, theme, fast_model, fast_timeout, status_callback
        )
        return deck_obj, f"agent_platform_gemini_with_skill:{fast_model}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini structured synthesis fallback triggered: %s", exc)

    # 2. Deterministic fallback (e.g. unit test environment without external network)
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


_FREEFORM_FALLBACK_REASON_LABELS = {
    "freeform_disabled": "自由デザイン機能が無効のため",
    "no_index": "デザイナーエージェントがスライドを書き出せなかったため",
    "unpublishable": "検証を通過する自由デザイン版がなかったため",
    "error": "自由デザインの生成中にエラーが発生したため",
}


def describe_generation_engine(engine: str) -> str:
    """Human-readable Japanese label for a `generation_engine` value stored in Firestore."""
    raw = (engine or "").strip()
    base_raw, _, fallback = raw.partition("+freeform_fallback:")
    eng = base_raw.lower()
    if fallback:
        if eng.startswith("agent_platform_gemini_with_skill"):
            model = base_raw.split(":", 1)[1] if ":" in base_raw else _get_model_name()
            inner = f"{model} 高速生成"
        elif eng.startswith("deterministic_skill_template"):
            inner = "スキルテンプレートによる即時生成"
        else:
            inner = describe_generation_engine(base_raw)
        reason = _FREEFORM_FALLBACK_REASON_LABELS.get(
            fallback.strip().lower(), "自由デザイン版を公開できなかったため"
        )
        return f"{reason}、テンプレートで仕上げました（{inner}）"
    if eng.startswith("adk_freeform"):
        model = base_raw.split(":", 1)[1] if ":" in base_raw else _get_model_name()
        return f"ADK エージェント（{model}）による自由デザイン（描画結果をエージェント自身が確認して修正）"
    if eng.startswith("agent_platform_gemini_with_skill"):
        model = base_raw.split(":", 1)[1] if ":" in base_raw else _get_model_name()
        return f"{model} によるテンプレート高速生成（構造化出力）"
    if eng.startswith("deterministic_skill_template"):
        return "スキルテンプレートによる即時生成（オフライン／最終フォールバック）"
    if eng == "state_deck_spec":
        return "対話で確定した構成データをそのまま反映"
    return engine or "不明"


def describe_edit_engine(engine: str) -> str:
    """Human-readable Japanese label for the engine that applied an edit."""
    eng = (engine or "").strip()
    low = eng.lower()
    if low.startswith("adk_freeform"):
        model = eng.split(":", 1)[1] if ":" in eng else _get_model_name()
        return f"ADK エージェント（{model}）による自由デザインの修正（描画結果をエージェント自身が確認して修正）"
    if low == "freeform_undo":
        return "自由デザイン版の取り消し（1つ前の版に表示を切り替え）"
    if low.startswith("gemini:"):
        model = eng.split(":", 1)[1].split("@", 1)[0] or _get_model_name()
        return f"{model} による修正（構造化出力・デザインシステム適用）"
    if low.startswith("heuristic:llm_unavailable"):
        return "キーワード解析による修正（LLM が応答しなかったため自動切替）"
    if low.startswith("heuristic"):
        return "キーワード解析による修正"
    if low == "explicit":
        return "指定された値をそのまま反映"
    if low == "undo":
        return "直前の修正の取り消し（1つ前の版に復元）"
    return eng or "不明"


# ---------------------------------------------------------------------------
# Design modes: free-form (ADK designer agent + visual review loop) vs template (fast mode)
# ---------------------------------------------------------------------------
FREEFORM_CREATE_ETA = "通常 7〜11 分（最長約 20 分）"
FREEFORM_EDIT_ETA = "通常 5〜7 分（最長約 15 分）"

_PHASE_LABELS = {
    "queued": "開始待ち（ジョブ起動中）",
    "knowledge_search": "社内ナレッジ検索",
    "deterministic_template": "テンプレート生成",
    "rendering": "HTML の組み立て",
    "freeform_staging": "素材の準備",
    "freeform_drafting": "デザイナーエージェントが制作中",
    "freeform_images": "AI イメージの生成",
    "freeform_checking": "描画して見た目を検査中",
    "freeform_reviewing": "エージェントがスクリーンショットを見て修正中",
    "freeform_publishing": "公開処理",
    "freeform_fallback": "テンプレートで仕上げ中",
    "freeform_edit_queued": "修正の開始待ち（ジョブ起動中）",
    "edit_queued": "修正の開始待ち",
    "edit_designing": "修正内容の設計",
    "edit_rendering": "HTML の組み立て",
    "edit_publishing": "公開処理",
    "ready": "完了",
    "failed": "失敗",
}


def _phase_label(phase: str) -> str:
    return _PHASE_LABELS.get(phase or "", phase or "")


def _freeform_enabled() -> bool:
    return os.environ.get("FREEFORM_DESIGN_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _normalize_design_mode(value: Any) -> str:
    """Returns 'freeform', 'template' or '' (not specified)."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if any(key in text for key in ("template", "fast", "quick", "standard", "テンプレ", "高速", "定型")):
        return "template"
    if any(key in text for key in ("free", "custom", "creative", "自由")):
        return "freeform"
    return ""


def _resolve_design_mode(requested: Any) -> str:
    """Free-form is the default when enabled; the template is used on request (fast mode) or when disabled."""
    if _normalize_design_mode(requested) == "template":
        return "template"
    return "freeform" if _freeform_enabled() else "template"


# ---------------------------------------------------------------------------
# Deck Edit Engine (design-system aware edits + truthful, diff-based change reporting)
# ---------------------------------------------------------------------------


class DeckEditResult(BaseModel):
    """Structured output of the LLM edit step."""

    deck_spec: PresentationDeckSpec = Field(
        description="修正指示を反映した新しい PresentationDeckSpec（指示のない項目は現在の値のまま）"
    )
    change_summary: list[str] = Field(
        default_factory=list,
        description="実際に変更した点（日本語で具体的に、最大8項目。変更していないことは書かない）",
    )
    unsupported_requests: list[str] = Field(
        default_factory=list,
        description="テンプレートの制約で反映できなかった要望（例：スライドの追加、画像・動画の埋め込み）",
    )


DESIGN_SYSTEM_GUIDE = """【デザインシステム（このテンプレートで変更できること）】
- design_style（全体の見た目。背景色・文字色・カードの質感がまとめて切り替わります）:
  - immersive-dark: 濃紺〜黒の背景・白文字・半透明ガラス風カード・グロー効果（既定）
  - clean-light: 真っ白な背景・濃いグレーの文字・白カードと薄い影・上部のアクセントライン。明るくミニマルな印象
  - editorial-light: 生成り色（#faf7f2）の背景・明朝体の見出し・フラットで角ばったカード。雑誌やレポートのような上品な印象
- theme_color（アクセントカラー）: sky / emerald / violet / amber / rose
- custom_css（任意の追加CSS。フォント・角丸・余白・線・影・文字サイズなどの微調整用。通常は空文字）:
  - 使えるフォント: 'Plus Jakarta Sans'、'Noto Sans JP'、'JetBrains Mono'（editorial-light では 'Noto Serif JP' も可）、serif / sans-serif などの汎用フォント
  - 主なセレクタ: body[data-style], header, footer, .slide, section[data-layout="hero-cover"]（ほかに bento-executive-summary / as-is-to-be-comparison / architecture-flow / roadmap-timeline / roi-and-next-steps）, .glass-card, h1, h2, h3, h4, p, li, #progress-bar, #prev-btn, #next-btn
  - Tailwind のユーティリティより優先されるよう、各宣言の末尾に !important を付けてください
  - 禁止: url()・@import・@font-face・外部リソース・山括弧・バックスラッシュ。8000文字以内
- スライド構成は固定の6枚です（表紙／課題と結論／As-Is・To-Be／アーキテクチャ4層／ロードマップ3フェーズ／ROIとネクストステップ）。スライドの追加・削除・並べ替え、画像・動画・グラフの埋め込みはできません。
"""

_EDIT_REGEX_FLAGS = re.IGNORECASE | re.ASCII

_EDIT_STYLE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("editorial-light", r"エディトリアル|雑誌|マガジン|明朝|\beditorial\b|\bmagazine\b|\bserif\b"),
    (
        "clean-light",
        r"背景[^。、\n]{0,8}(白|ホワイト|明る)|(白|ホワイト)[^。、\n]{0,4}背景|白基調|白ベース"
        r"|(?<![空明])白(っぽ|く|に|系|色)|ホワイト|(?<![イト])ライト"
        r"|明るい(配色|デザイン|雰囲気|見た目|トーン|色|背景)|(配色|デザイン|雰囲気|見た目|トーン|背景)を?明るく"
        r"|\bwhite\b|\blight\b",
    ),
    (
        "immersive-dark",
        r"背景[^。、\n]{0,8}(黒|ダーク|暗)|(黒|ダーク)[^。、\n]{0,4}背景|黒基調|ダーク"
        r"|暗い(配色|デザイン|雰囲気|見た目|トーン|色|背景)|(配色|デザイン|雰囲気|見た目|トーン|背景)を?暗く"
        r"|\bdark\b|\bblack\b",
    ),
)
_COLOR_SUFFIX = r"(系|色|基調|っぽ|に|へ|で|を)"
_EDIT_THEME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("sky", r"\bsky\b|スカイ|水色|(青|ブルー)" + _COLOR_SUFFIX),
    ("emerald", r"\bemerald\b|エメラルド|(緑|グリーン)" + _COLOR_SUFFIX),
    ("violet", r"\bviolet\b|バイオレット|(紫|パープル)" + _COLOR_SUFFIX),
    ("amber", r"\bamber\b|アンバー|オレンジ|(黄|イエロー|ゴールド|金)" + _COLOR_SUFFIX),
    ("rose", r"\brose\b|ローズ|(ピンク|赤|レッド)" + _COLOR_SUFFIX),
)
_EDIT_DRASTIC_PATTERN = (
    r"ぜんぜん違|全然違|まったく違|全く違|がらっと|ガラッと|一新|刷新|別物"
    r"|雰囲気を変|見た目を変|デザインを変|印象を変|different\s+look|redesign|completely\s+different"
)
_ALTERNATE_THEME = {"sky": "violet", "violet": "emerald", "emerald": "amber", "amber": "rose", "rose": "sky"}


def _last_match_positions(patterns: tuple[tuple[str, str], ...], text: str) -> dict[str, int]:
    positions: dict[str, int] = {}
    for value, pattern in patterns:
        for match in re.finditer(pattern, text or "", flags=_EDIT_REGEX_FLAGS):
            positions[value] = match.start()
    return positions


def _detect_requested_style(text: str) -> str:
    """Returns the design style a natural-language request asks for (last mention wins), or ''."""
    positions = _last_match_positions(_EDIT_STYLE_PATTERNS, text)
    if not positions:
        return ""
    winner = max(positions, key=lambda key: positions[key])
    if winner == "clean-light" and "editorial-light" in positions:
        return "editorial-light"
    return winner


def _detect_requested_theme(text: str) -> str:
    """Returns the accent colour a request names (last mention wins), or ''."""
    positions = _last_match_positions(_EDIT_THEME_PATTERNS, text)
    if not positions:
        return ""
    return max(positions, key=lambda key: positions[key])


_EDIT_UI_FORMAT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("portal", r"ポータル|記事形式|ドキュメント形式|4カラム|４カラム|Web提案ポータル|\bportal\b"),
    ("slides", r"スライド形式|16:9|１６：９|プレゼンスライド形式|スライドデッキ形式|\bslides\s+mode\b|\bslide\s+mode\b"),
)


def _detect_requested_ui_format(text: str) -> str:
    """Returns 'portal' or 'slides' if explicitly requested in natural language, or ''."""
    positions = _last_match_positions(_EDIT_UI_FORMAT_PATTERNS, text)
    if not positions:
        return ""
    return max(positions, key=lambda key: positions[key])


def _is_drastic_redesign(text: str) -> bool:
    return bool(re.search(_EDIT_DRASTIC_PATTERN, text or "", flags=_EDIT_REGEX_FLAGS))


def _style_family(style: str) -> str:
    return "light" if style in ("clean-light", "editorial-light") else "dark"


def _apply_edit_heuristics(
    current: PresentationDeckSpec,
    candidate: PresentationDeckSpec,
    instructions: str,
    llm_used: bool,
) -> tuple[PresentationDeckSpec, list[str]]:
    """Deterministic safety net: explicit look-and-feel requests are always visibly applied.

    Without the LLM this is the whole edit engine; with the LLM it only corrects answers that ignored a clear
    request (e.g. "背景を白に" answered with a dark style).
    """
    notes: list[str] = []
    if not instructions:
        return candidate, notes
    candidate_style = normalize_design_style(candidate.design_style)
    req_style = _detect_requested_style(instructions)
    req_theme = _detect_requested_theme(instructions)
    if req_style:
        if not llm_used and candidate_style != req_style:
            candidate.design_style = req_style
            notes.append(f"キーワード解析: design_style を {req_style} に設定")
        elif llm_used and _style_family(candidate_style) != _style_family(req_style):
            candidate.design_style = req_style
            notes.append(f"補正: 指示と異なる design_style が返されたため {req_style} に修正")
    if req_theme and candidate.theme_color != req_theme:
        if not llm_used or candidate.theme_color == current.theme_color:
            candidate.theme_color = req_theme
            notes.append(f"キーワード解析: theme_color を {req_theme} に設定")
    req_ui_fmt = _detect_requested_ui_format(instructions)
    if req_ui_fmt and getattr(candidate, "ui_format", DEFAULT_UI_FORMAT) != req_ui_fmt:
        if not llm_used or getattr(candidate, "ui_format", DEFAULT_UI_FORMAT) == getattr(current, "ui_format", DEFAULT_UI_FORMAT):
            candidate.ui_format = req_ui_fmt
            notes.append(f"キーワード解析: ui_format を {req_ui_fmt} に設定")
    if _is_drastic_redesign(instructions):
        if normalize_design_style(candidate.design_style) == current.design_style and not req_style:
            candidate.design_style = (
                "clean-light" if _style_family(current.design_style) == "dark" else "immersive-dark"
            )
            notes.append(f"大幅な変更の指示のため design_style を {candidate.design_style} に変更")
        if candidate.theme_color == current.theme_color and not req_theme:
            candidate.theme_color = _ALTERNATE_THEME.get(current.theme_color, "violet")
            notes.append(f"大幅な変更の指示のため theme_color を {candidate.theme_color} に変更")
    return candidate, notes


_SPEC_CHANGE_LABELS: tuple[tuple[str, str], ...] = (
    ("ui_format", "UI形式（レイアウト）"),
    ("design_style", "デザインスタイル"),
    ("theme_color", "アクセントカラー"),
    ("custom_css", "カスタムCSS"),
    ("client_name", "クライアント名"),
    ("proposal_title", "タイトル（表紙）"),
    ("subtitle", "サブタイトル（表紙）"),
    ("custom_callout", "表紙のハイライト"),
    ("current_challenges", "スライド2：現状の課題"),
    ("executive_conclusion", "スライド2：結論メッセージ"),
    ("before_state", "スライド3：As-Is（従来）"),
    ("after_state", "スライド3：To-Be（変革後）"),
    ("cx_highlights", "スライド3：UX/AIハイライト"),
    ("architecture_nodes", "スライド4：アーキテクチャ4層"),
    ("roadmap_phases", "スライド5：ロードマップ"),
    ("quantitative_roi", "スライド6：定量効果"),
    ("qualitative_roi", "スライド6：定性効果"),
    ("next_steps", "スライド6：ネクストステップ"),
)


def _clip(value: Any, limit: int = 40) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_deck_changes(
    old: PresentationDeckSpec, new: PresentationDeckSpec
) -> tuple[list[str], list[str]]:
    """Deterministic diff -> (changed field names, human-readable Japanese list of what visibly changed)."""
    before, after = old.model_dump(), new.model_dump()
    changed: list[str] = []
    readable: list[str] = []
    for field_name, label in _SPEC_CHANGE_LABELS:
        old_value, new_value = before.get(field_name), after.get(field_name)
        if old_value == new_value:
            continue
        changed.append(field_name)
        if field_name == "ui_format":
            readable.append(
                f"{label}: {UI_FORMAT_LABELS.get(str(old_value), old_value)} → "
                f"{UI_FORMAT_LABELS.get(str(new_value), new_value)}"
            )
        elif field_name == "design_style":
            readable.append(
                f"{label}: {DESIGN_STYLE_LABELS.get(str(old_value), old_value)} → "
                f"{DESIGN_STYLE_LABELS.get(str(new_value), new_value)}"
            )
        elif field_name == "theme_color":
            readable.append(f"{label}: {old_value} → {new_value}")
        elif field_name == "custom_css":
            readable.append(f"{label}: 適用（{len(str(new_value))}文字）" if new_value else f"{label}: 削除")
        elif isinstance(new_value, list):
            old_list = old_value if isinstance(old_value, list) else []
            total = max(len(old_list), len(new_value))
            count = sum(
                1
                for idx in range(total)
                if (old_list[idx] if idx < len(old_list) else None)
                != (new_value[idx] if idx < len(new_value) else None)
            )
            readable.append(f"{label}: {count}項目を更新")
        else:
            readable.append(f"{label}: 「{_clip(old_value)}」→「{_clip(new_value)}」")
    return changed, readable


def _build_edit_prompt(
    deck_obj: PresentationDeckSpec,
    edit_instructions: str,
    explicit_changes: dict[str, str],
) -> str:
    explicit_block = ""
    if explicit_changes:
        lines = "\n".join(f"- {key}: {value}" for key, value in explicit_changes.items())
        explicit_block = f"\n【必ずこの値にする項目（ユーザー指定）】\n{lines}\n"
    instructions_text = edit_instructions or "（自然文の指示なし。下記の指定値のみ反映）"
    current_json = json.dumps(deck_obj.model_dump(), ensure_ascii=False, indent=2)
    return f"""あなたはエグゼクティブ提案デザイナーです。公開中の6枚構成HTMLプレゼンテーション（テンプレートでレンダリング）に対するユーザーの修正指示を、構成データ `PresentationDeckSpec` に反映してください。

{DESIGN_SYSTEM_GUIDE}
【反映ルール】
1. 指示された点は、見た目で分かるレベルで確実に反映してください。指示のない項目は現在の値をそのまま維持してください。
2. 「背景を白に」「明るく」なら design_style を clean-light に（上品・雑誌風なら editorial-light）。「暗く」「ダークに」なら immersive-dark にしてください。
3. 「ぜんぜん違う見た目」「雰囲気を一新」のような大幅な変更では、design_style と theme_color の両方を現在と違う値にし、必要に応じて custom_css でフォント・角丸・線などの質感も変えて、違いがひと目で分かるようにしてください。
4. 文言の修正は該当スライドのフィールドだけを書き換えてください。配列の要素数（課題3・As-Is 3・To-Be 3・CX 3・アーキテクチャ4・ロードマップ3・定量ROI 3・定性ROI 3・ネクストステップ3）は必ず守ってください。
5. change_summary には実際に変更した点だけを書いてください。テンプレートの制約で反映できない要望は unsupported_requests に入れてください。
6. 製品名は現行の正式名称（Gemini Enterprise Agent Platform / Agent Runtime / Agent Search / Gemini 3.8 Flash / BigQuery / Cloud Run）で表記し、旧称は使わないでください。

【現在の PresentationDeckSpec JSON】
{current_json}

【ユーザーの修正指示】
{instructions_text}
{explicit_block}"""


def parse_edit_result_text(raw_text: str) -> DeckEditResult:
    """Parses the LLM edit answer (DeckEditResult JSON, or a bare PresentationDeckSpec JSON) leniently."""
    payload_text = _extract_json_object_text(raw_text)
    try:
        return DeckEditResult.model_validate_json(payload_text)
    except Exception:  # noqa: BLE001
        return DeckEditResult(deck_spec=PresentationDeckSpec.model_validate_json(payload_text))


def _llm_edit_enabled() -> bool:
    return os.environ.get("ENABLE_LLM_DECK_EDIT", "true").lower() in ("true", "1")


def _get_edit_model_timeout_seconds() -> int:
    try:
        return max(20, int(os.environ.get("EDIT_MODEL_TIMEOUT_SECONDS", "75")))
    except ValueError:
        return 75


def _get_edit_stale_seconds() -> int:
    """An 'updating' marker older than this is treated as abandoned (crashed edit) and never blocks viewers."""
    try:
        return max(60, int(os.environ.get("EDIT_STALE_SECONDS", "300")))
    except ValueError:
        return 300


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _seconds_since(iso_value: Any) -> float | None:
    if not iso_value:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(iso_value))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return round((datetime.datetime.now(datetime.timezone.utc) - moment).total_seconds(), 1)
    except Exception:  # noqa: BLE001
        return None


def _get_freeform_edit_stale_seconds() -> int:
    """Free-form edits run as a background job (cold start + agent turns + review loop), so they get longer."""
    try:
        return max(300, int(os.environ.get("FREEFORM_EDIT_STALE_SECONDS", "1500")))
    except ValueError:
        return 1500


def _is_freeform_edit(doc: dict[str, Any]) -> bool:
    request = doc.get("edit_request")
    return isinstance(request, dict) and request.get("mode") == "freeform"


def _is_freeform_rendered(doc: dict[str, Any]) -> bool:
    """True when the share URL currently serves a free-form version (presentations/<id>/v<N>/)."""
    return doc.get("render_mode") == "freeform" and bool(doc.get("freeform_prefix"))


def _doc_design_mode(doc: dict[str, Any]) -> str:
    if _is_freeform_rendered(doc):
        return "freeform"
    inputs = doc.get("generation_inputs") if isinstance(doc.get("generation_inputs"), dict) else {}
    mode = str(doc.get("design_mode") or inputs.get("design_mode") or "")
    return "freeform" if mode == "freeform" else "template"


def _is_edit_stale(doc: dict[str, Any]) -> bool:
    elapsed = _seconds_since(doc.get("edit_requested_at") or doc.get("updated_at"))
    limit = _get_freeform_edit_stale_seconds() if _is_freeform_edit(doc) else _get_edit_stale_seconds()
    return elapsed is None or elapsed > limit


def _content_version_of(doc: dict[str, Any]) -> int:
    try:
        return max(0, int(doc.get("content_version") or 0))
    except (TypeError, ValueError):
        return 0


def _public_last_edit(last_edit: Any) -> dict[str, Any]:
    if not isinstance(last_edit, dict) or not last_edit:
        return {}
    keys = (
        "status",
        "kind",
        "edit_engine_label",
        "verified_changes",
        "designer_notes",
        "unsupported_requests",
        "content_version",
        "freeform_version",
        "previous_freeform_version",
        "review_rounds",
        "warnings",
        "design_style",
        "elapsed_seconds",
        "applied_at",
        "error",
    )
    return {key: last_edit.get(key) for key in keys if key in last_edit}


def _edit_deck_with_gemini(
    deck_obj: PresentationDeckSpec,
    edit_instructions: str,
    explicit_changes: dict[str, str],
) -> tuple[DeckEditResult, str]:
    """Structured-output edit with the fast Gemini model (time-boxed). Returns (result, 'model@location')."""
    model_name = _get_model_name()
    timeout_seconds = _get_edit_model_timeout_seconds()
    prompt = _build_edit_prompt(deck_obj, edit_instructions, explicit_changes)
    project_id = _get_project_id()
    genai_location = _get_genai_location(model_name)
    candidate_locations = [genai_location]
    if genai_location != "global":
        candidate_locations.append("global")
    last_exc: Exception | None = None
    for loc in candidate_locations:
        try:
            try:
                client = genai.Client(
                    vertexai=True,
                    project=project_id,
                    location=loc,
                    http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
                )
            except TypeError:
                client = genai.Client(vertexai=True, project=project_id, location=loc)
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=DeckEditResult,
                    temperature=0.3,
                ),
            )
            text = str(getattr(resp, "text", "") or "")
            if not text:
                raise RuntimeError("empty response text")
            return parse_edit_result_text(text), f"{model_name}@{loc}"
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("LLM deck edit (%s at %s) failed: %s", model_name, loc, exc)
    raise RuntimeError(f"LLM deck edit failed on {candidate_locations}: {last_exc}")


# ---------------------------------------------------------------------------
# Tool 1: Agent Search (Discovery Engine) Knowledge Search Tool
# ---------------------------------------------------------------------------


_SALESFORCE_STRUCT_KEYS = {
    "StageName",
    "stageName",
    "stage_name",
    "AccountId",
    "accountId",
    "OpportunityId",
    "opportunityId",
    "NextStep",
    "nextStep",
    "next_step",
    "Amount",
    "CloseDate",
    "closeDate",
    "AccountName",
    "accountName",
}


def _to_plain_value(value: Any) -> Any:
    """Recursively converts ProtoPlus MapComposite / RepeatedComposite values into plain Python dicts/lists."""
    if isinstance(value, Mapping):
        return {str(k): _to_plain_value(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_to_plain_value(item) for item in value]
    return value


def _to_plain_dict(mapping: Any) -> dict[str, Any]:
    if not mapping:
        return {}
    if isinstance(mapping, Mapping):
        return {str(k): _to_plain_value(v) for k, v in mapping.items()}
    try:
        return {str(k): _to_plain_value(v) for k, v in dict(mapping).items()}
    except Exception:  # noqa: BLE001
        return {}


def _infer_source_type(
    ds_id: str,
    struct_data: dict[str, Any],
    derived_data: dict[str, Any],
    source_uri: str,
) -> str:
    explicit = (
        struct_data.get("source_system")
        or struct_data.get("source_type")
        or derived_data.get("source_type")
    )
    if explicit:
        return str(explicit).strip().lower()
    ds_lower = ds_id.lower()
    uri_lower = source_uri.lower()
    if (
        "drive" in ds_lower
        or "docs.google.com" in uri_lower
        or "drive.google.com" in uri_lower
    ):
        return "google_drive"
    if (
        "salesforce" in ds_lower
        or "sfdc" in ds_lower
        or "crm" in ds_lower
        or "salesforce.com" in uri_lower
        or any(k in struct_data for k in _SALESFORCE_STRUCT_KEYS)
    ):
        return "salesforce"
    return "knowledge"


def _extract_document_record(doc: Any, ds_id: str) -> dict[str, Any]:
    struct_data = _to_plain_dict(getattr(doc, "struct_data", None))
    if not struct_data:
        raw_json_data = getattr(doc, "json_data", None)
        if isinstance(raw_json_data, str) and raw_json_data.strip():
            try:
                parsed_json = json.loads(raw_json_data)
                if isinstance(parsed_json, Mapping):
                    struct_data = _to_plain_dict(parsed_json)
            except Exception:  # noqa: BLE001
                pass
    derived_data = _to_plain_dict(getattr(doc, "derived_struct_data", None))

    snippets: list[str] = []
    for key in ("snippets", "extractive_answers", "extractive_segments", "chunks"):
        for item in derived_data.get(key) or []:
            if isinstance(item, Mapping):
                text = item.get("snippet") or item.get("content") or item.get("text")
                if text:
                    snippets.append(str(text).strip())
            elif isinstance(item, str) and item.strip():
                snippets.append(item.strip())

    account_obj = (
        struct_data.get("Account")
        if isinstance(struct_data.get("Account"), Mapping)
        else {}
    )
    raw_doc_uri = getattr(doc, "uri", None)
    doc_uri = raw_doc_uri if isinstance(raw_doc_uri, str) else ""
    source_uri = str(
        struct_data.get("source_uri")
        or struct_data.get("uri")
        or struct_data.get("link")
        or derived_data.get("link")
        or derived_data.get("uri")
        or doc_uri
        or ""
    )
    source_type = _infer_source_type(ds_id, struct_data, derived_data, source_uri)

    doc_id = str(getattr(doc, "id", "") or "")
    title = (
        struct_data.get("title")
        or struct_data.get("Name")
        or struct_data.get("name")
        or struct_data.get("Subject")
        or struct_data.get("subject")
        or derived_data.get("title")
        or doc_id
    )
    client_name = (
        struct_data.get("client_name")
        or struct_data.get("AccountName")
        or struct_data.get("account_name")
        or struct_data.get("accountName")
        or account_obj.get("Name")
        or account_obj.get("name")
        or ""
    )
    industry = (
        struct_data.get("industry")
        or struct_data.get("Industry")
        or account_obj.get("Industry")
        or account_obj.get("industry")
        or ""
    )
    deal_stage = (
        struct_data.get("deal_stage")
        or struct_data.get("StageName")
        or struct_data.get("stage_name")
        or struct_data.get("stageName")
        or struct_data.get("stage")
        or ""
    )
    recent_activity = (
        struct_data.get("recent_activity")
        or struct_data.get("NextStep")
        or struct_data.get("next_step")
        or struct_data.get("nextStep")
        or struct_data.get("latest_activity")
        or struct_data.get("recent_notes")
        or ""
    )
    summary = (
        struct_data.get("summary")
        or struct_data.get("content")
        or struct_data.get("Description")
        or struct_data.get("description")
        or " ".join(s for s in snippets if s)
    )
    amount = struct_data.get("Amount") or struct_data.get("amount")
    key_metrics = struct_data.get("key_metrics") or (
        f"Amount: {amount}" if amount else ""
    )
    recommended_architecture = struct_data.get("recommended_architecture", "")

    return {
        "id": doc_id,
        "datastore_id": ds_id,
        "source_type": source_type,
        "source_uri": source_uri,
        "title": str(title),
        "client_name": str(client_name),
        "industry": str(industry),
        "summary": str(summary),
        "key_metrics": str(key_metrics),
        "recommended_architecture": str(recommended_architecture),
        "deal_stage": str(deal_stage),
        "recent_activity": str(recent_activity),
    }


def search_internal_knowledge(query: str) -> str:
    """Searches internal Agent Search datastore(s) for past proposals, RFPs, case studies, and CRM context.

    Supports both single and comma/colon-separated multiple DataStore IDs in `AGENT_SEARCH_DATASTORE_ID`
    (e.g., a Google Drive 1st Party DataConnector DataStore for past RFPs/case studies plus a Salesforce
    1st Party DataConnector DataStore for live CRM opportunities and stakeholder notes).

    Args:
        query: Search keywords (such as client name, industry, UX/CDP/AI theme, or RFP requirements).

    Returns:
        JSON string containing matched internal documents, snippets, source types, and structured metadata.
    """
    project_id = _get_project_id()
    location = _get_datastore_location()
    datastore_id = _get_datastore_id()
    datastore_ids = _get_datastore_ids()

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
        for ds_id in datastore_ids:
            serving_config = (
                f"projects/{project_id}/locations/{location}/collections/"
                f"default_collection/dataStores/{ds_id}/servingConfigs/default_search"
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
            try:
                response = client.search(request)
                for result in response.results:
                    results_list.append(_extract_document_record(result.document, ds_id))
            except Exception as ds_exc:
                logger.warning(
                    "Agent Search query failed for datastore %s: %s", ds_id, ds_exc
                )
    except Exception as exc:
        logger.warning("Agent Search query fallback triggered: %s", exc)

    if not results_list:
        primary_ds = datastore_ids[0] if datastore_ids else datastore_id
        crm_ds = datastore_ids[1] if len(datastore_ids) > 1 else primary_ds
        results_list = [
            {
                "id": "sample-case-retail-cdp-ai-001",
                "datastore_id": primary_ds,
                "source_type": "google_drive",
                "source_uri": "https://drive.google.com/drive/folders/sample-rfp-archive",
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
                "deal_stage": "Closed Won (Reference Case)",
                "recent_activity": "Google ドライブの過去RFP・提案実績アーカイブより抽出",
            },
            {
                "id": "sample-crm-opportunity-context-002",
                "datastore_id": crm_ds,
                "source_type": "salesforce",
                "source_uri": "https://example.my.salesforce.com/lightning/r/Opportunity/006000000000001AAA/view",
                "title": f"Salesforce 商談履歴：{query} 様向け 次世代デジタル体験・AI活用基盤プロジェクト",
                "client_name": query,
                "industry": "リテール・流通・金融・B2Bサービス",
                "summary": (
                    "最新商談メモ：先方事業部門・DX推進室とのヒアリングにて、既存チャネルのデータ分断解消と"
                    "段階的なパイロット導入（6ヶ月ロードマップ）を重視していることを確認。セキュリティ要件として"
                    "外部共有資料への個別ID/パスワード認証が必須。"
                ),
                "key_metrics": "想定初期導入期間: 6ヶ月（Phase 1〜3）、目標リピートCVR改善: +25%〜+30%",
                "recommended_architecture": (
                    "Google ドライブ(過去RFP・実績集) + Salesforce(最新商談経緯) -> Agent Search -> "
                    "Agent Runtime (ADK Concierge) -> Cloud Run 認証ゲートウェイ + 非公開 Cloud Storage + Firestore"
                ),
                "deal_stage": "02 - Tech Eval / Solution Proposal",
                "recent_activity": "次回役員プレゼン向けに、システム構成図と導入ステップを含むインタラクティブHTML提案サイトの提示を合意",
            },
        ]

    return json.dumps(
        {
            "datastore_id": datastore_id,
            "datastore_ids": datastore_ids,
            "project_id": project_id,
            "query": query,
            "matched_documents": results_list,
        },
        ensure_ascii=False,
        indent=2,
    )


# Backwards-compatible alias
search_proposal_datastore = search_internal_knowledge


# ---------------------------------------------------------------------------
# Tool 2: Create & Publish Proposal Website to Private GCS + Firestore
# ---------------------------------------------------------------------------


def _extract_deck_dict_from_state(raw_deck: Any) -> dict[str, Any]:
    """Normalizes deck_spec stored in ToolContext state or JSON string into a dictionary."""
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


def _get_generation_stale_minutes(doc: dict[str, Any] | None = None) -> int:
    """Generations older than this are completed with the template. Free-form runs get a longer budget."""
    if doc is not None and _doc_design_mode(doc) == "freeform":
        try:
            return max(10, int(os.environ.get("FREEFORM_GENERATION_STALE_MINUTES", "22")))
        except ValueError:
            return 22
    try:
        return max(5, int(os.environ.get("GENERATION_STALE_MINUTES", "13")))
    except ValueError:
        return 13


def _run_cloud_run_generation_job(job_name: str, presentation_id: str, job_mode: str = "generate") -> str:
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
                {
                    "env": [
                        {"name": "PRESENTATION_ID", "value": presentation_id},
                        {"name": "JOB_MODE", "value": job_mode or "generate"},
                    ]
                }
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


def _run_generation_inline_thread(presentation_id: str, job_mode: str = "generate") -> None:
    from app.generation_worker import run_job

    try:
        run_job(presentation_id, job_mode)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Inline background %s failed for %s: %s", job_mode, presentation_id, exc)


def _start_background_generation(presentation_id: str, job_mode: str = "generate") -> dict[str, Any]:
    """Kicks off the background job and returns how it was dispatched (never raises).

    `job_mode` is `generate` (first generation) or `freeform_edit` (queued free-form edit / conversion).
    """
    mode = _get_generation_trigger_mode()
    job_name = _get_generation_job_name()
    if mode == "none":
        return {"mode": "none", "job_mode": job_mode}
    if mode == "sync":
        from app.generation_worker import run_job

        result = run_job(presentation_id, job_mode)
        return {"mode": "sync", "job_mode": job_mode, "worker_result": result}
    if mode in ("auto", "cloud_run_job") and job_name:
        try:
            execution = _run_cloud_run_generation_job(job_name, presentation_id, job_mode)
            return {
                "mode": "cloud_run_job",
                "job_mode": job_mode,
                "job_name": job_name,
                "execution": execution,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Cloud Run Job trigger failed (%s); falling back to inline thread: %s",
                job_name,
                exc,
            )
            if mode == "cloud_run_job":
                return {"mode": "cloud_run_job_failed", "job_mode": job_mode, "error": str(exc)[:300]}
    worker = threading.Thread(
        target=_run_generation_inline_thread,
        args=(presentation_id, job_mode),
        name=f"proposal-{job_mode}-{presentation_id}",
        daemon=True,
    )
    worker.start()
    return {"mode": "inline_thread", "job_mode": job_mode}


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
    design_style: str = "",
    design_mode: str = "",
    design_request: str = "",
    ui_format: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Issues the share URL / viewer ID / password immediately and generates the HTML5 proposal website in the background.

    Call this tool ONLY when the user explicitly asks to generate/publish a proposal website or approves an outline.
    Do NOT call this tool when the user only says a greeting like 'こんにちは'.
    Pass the agreed outline as natural language inside `proposal_brief`; use `deck_spec_json` only for a complete
    PresentationDeckSpec JSON (non-conforming JSON is accepted and treated as an outline hint, never an error).

    Design modes:
      * 'freeform' (default): an ADK designer agent (gemini-3.8-flash) freely designs the deck (number of slides,
        layouts, SVG diagrams, ECharts graphs and up to 4 AI-generated images when useful). The worker renders it
        in headless Chromium and shows the screenshots to the same agent, which fixes its own files (up to 2 review
        rounds) before publication. Typically 7-10 minutes.
      * 'template' (fast mode): the fixed 6-slide template, typically 1-5 minutes.

    Args:
        client_name: Target client company name (e.g., '株式会社サンプル商事').
        proposal_title: Main title of the proposal presentation.
        proposal_brief: Summary of client challenges, proposed solution, architecture, target ROI, and the agreed slide outline.
        theme_color: Visual accent theme ('sky', 'emerald', 'violet', 'amber', or 'rose').
        expiration_days: Number of days until the shared URL expires (default 14).
        deck_spec_json: Optional full JSON string matching PresentationDeckSpec.
        design_style: Optional overall look for the template: 'immersive-dark' (dark navy, default), 'clean-light'
            (white background, minimal) or 'editorial-light' (off-white, serif headings).
        design_mode: 'freeform' (default) or 'template' (only when the user asks for 高速モード / テンプレート / speed).
        design_request: The user's look-and-feel wishes in their own words (background colour, mood, charts, graphs,
            diagrams, images, number of slides). Used by the free-form designer.
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary with `status` ('GENERATING' while the background generation runs, 'PUBLISHED' when the deck is already
        complete, or 'ERROR'), `presentation_id`, `share_url`, `viewer_id`, `viewer_password`, `expires_at`,
        `design_mode`, `estimated_completion`, `generation_status`, and `next_action`.
        The share URL shows a "生成中" page until the deck is ready and then switches to it automatically.
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
            design_style=design_style,
            design_mode=design_mode,
            design_request=design_request,
            ui_format=ui_format,
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
    design_style: str = "",
    design_mode: str = "",
    design_request: str = "",
    ui_format: str = "",
) -> dict[str, Any]:
    raw_spec = tool_context.state.get("deck_spec") if tool_context else None
    if not raw_spec and deck_spec_json:
        raw_spec = deck_spec_json

    theme = theme_color if theme_color in SUPPORTED_THEME_COLORS else "sky"
    deck_obj, outline_hint = _coerce_deck_spec(raw_spec, theme)
    mode = _resolve_design_mode(design_mode)
    design_request_text = (design_request or "").strip()[:2000]
    requested_style = (
        normalize_design_style(design_style, default="") if (design_style or "").strip() else ""
    ) or _detect_requested_style(f"{proposal_title} {proposal_brief} {design_request_text}")
    requested_ui_format = (
        normalize_ui_format(ui_format, default="") if (ui_format or "").strip() else ""
    ) or _detect_requested_ui_format(f"{proposal_title} {proposal_brief} {design_request_text}") or DEFAULT_UI_FORMAT

    eff_client = (client_name or "").strip()
    eff_title = (proposal_title or "").strip()
    eff_brief = (proposal_brief or "").strip()
    if deck_obj is not None:
        eff_client = eff_client or deck_obj.client_name
        eff_title = eff_title or deck_obj.proposal_title
        eff_brief = eff_brief or deck_obj.subtitle
    if not eff_client and not eff_title and not eff_brief and not outline_hint and not design_request_text:
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
    if deck_obj is not None and mode == "freeform":
        # A complete template spec is still good content for the free-form designer.
        outline_hint = outline_hint or json.dumps(deck_obj.model_dump(), ensure_ascii=False)[:6000]

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
        "design_style": requested_style or DEFAULT_DESIGN_STYLE,
        "ui_format": requested_ui_format,
        "design_mode": mode,
        "content_version": 0,
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
    if deck_obj is not None and mode == "template":
        deck_obj.theme_color = theme
        if requested_style:
            deck_obj.design_style = requested_style
        if (ui_format or "").strip() or _detect_requested_ui_format(f"{proposal_title} {proposal_brief} {design_request_text}"):
            deck_obj.ui_format = requested_ui_format
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
                "design_style": deck_obj.design_style,
                "content_version": 1,
                "render_mode": "template",
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
            "design_style": deck_obj.design_style,
            "ui_format": deck_obj.ui_format,
            "design_mode": "template",
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
                "design_style": requested_style or DEFAULT_DESIGN_STYLE,
                "ui_format": requested_ui_format,
                "outline_hint": outline_hint,
                "design_mode": mode,
                "design_request": design_request_text,
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
        "design_style": requested_style or DEFAULT_DESIGN_STYLE,
        "ui_format": requested_ui_format,
        "design_mode": mode,
        "generation_engine": generation_engine,
        "generation_engine_label": describe_generation_engine(generation_engine)
        if generation_engine
        else f"生成中（{_get_model_name()} によるテンプレート高速生成）",
        "generation_plan": (
            f"1) {_get_model_name()} によるテンプレート高速生成 → 2) 応答がない場合はテンプレート即時生成（必ず完成させます）"
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
    if mode == "freeform":
        result.update(
            {
                "generation_plan": (
                    "1) ADK のデザイナーエージェントが、枚数・レイアウト・図解・グラフ・"
                    "必要に応じて AI 生成イメージまで自由に制作 → 2) ヘッドレス Chromium で描画し、スクリーンショットを同じ"
                    "エージェントに見せて最大 2 回修正 → 3) 検証を通過した版を公開（通過しない場合はテンプレートで必ず仕上げます）"
                ),
                "estimated_completion": FREEFORM_CREATE_ETA,
            }
        )
        if status_value == "GENERATING":
            result["generation_engine_label"] = (
                "生成中（ADK のデザイナーエージェントによる自由デザイン → 描画結果をエージェント自身が確認して修正 → 検証して公開）"
            )
            result["next_action"] = (
                "URL・閲覧用ID・パスワード・有効期限を今すぐユーザーに提示し、『自由デザインで生成中です。URL を開くと生成中画面が"
                "表示され、完成すると自動で切り替わります（通常 7〜11 分、最長約 20 分）。公開前にエージェントが描画結果を見て"
                "見直します』と案内してください。"
                "進捗を聞かれたら get_proposal_status を呼び出してください。"
            )
    if tool_context is not None:
        tool_context.state["published_result"] = result
        tool_context.state["published_presentation"] = result
    return result


def publish_presentation(
    tool_context: ToolContext | None = None,
    deck_spec_json: str = "",
) -> dict[str, Any]:
    """Backwards-compatible wrapper that publishes a presentation from tool_context.state['deck_spec'] or deck_spec_json."""
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
    """Reports the generation / edit status of a proposal website (and self-heals stale runs).

    Use when the user asks '生成状況を教えて', 'まだ完成しない？', '修正は反映された？' or 'どのエンジンで生成された？'.
    If a background generation has been running longer than its time budget, this tool completes it immediately
    with the skill template so the share URL always ends up with a finished deck.

    Args:
        presentation_id: Target presentation ID (e.g., 'prop-20261003-xxxxxxxx').
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary with `generation_status` ('generating' | 'updating' | 'ready' | 'failed'), `generation_phase`,
        `generation_phase_label`, `design_mode` ('freeform' | 'template'), `render_mode` (what the URL serves now),
        `freeform_version`, `review_rounds` (how many times the designer agent looked at its rendered screenshots
        before publication), `freeform_warnings`, `freeform_fallback_reason` (non-empty = free-form could not be
        published and the template finished the deck), `content_version`, `last_edit_result` (truthful result of the
        most recent edit), `generation_engine_label`, `elapsed_seconds`, `estimated_completion`,
        `share_url`, and `viewer_id`.
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
        stale_after = _get_generation_stale_minutes(data) * 60
        if gen_status == "generating" and elapsed_seconds is not None and elapsed_seconds > stale_after:
            from app.generation_worker import finalize_with_fallback

            repair = finalize_with_fallback(
                presentation_id, reason=f"stale_after_{int(elapsed_seconds)}s"
            )
            data.update(repair.get("doc_updates", {}))
            gen_status = str(data.get("generation_status") or "ready")
            repaired = True

        edit_repaired = False
        if gen_status == "updating" and _is_edit_stale(data):
            repaired_at = now_utc.isoformat()
            stale_updates = {
                "generation_status": "ready",
                "generation_phase": "ready",
                "generation_detail": "",
                "last_edit_result": {
                    "status": "failed",
                    "edit_engine": "",
                    "edit_engine_label": "",
                    "verified_changes": [],
                    "unsupported_requests": [],
                    "error": "修正処理が時間内に完了しなかったため中断しました（修正前の版を表示中）",
                    "content_version": _content_version_of(data),
                    "applied_at": repaired_at,
                },
                "edit_request": None,
                "updated_at": repaired_at,
            }
            doc_ref.update(stale_updates)
            data.update(stale_updates)
            gen_status = "ready"
            edit_repaired = True

        engine = str(data.get("generation_engine") or "")
        phase = str(data.get("generation_phase") or gen_status)
        content_version = _content_version_of(data)
        last_edit = data.get("last_edit_result") if isinstance(data.get("last_edit_result"), dict) else {}
        last_edit_status = str(last_edit.get("status") or "")
        design_mode = _doc_design_mode(data)
        freeform_now = _is_freeform_rendered(data)
        render_mode = "freeform" if freeform_now else ("template" if gen_status in ("ready", "updating") else "")
        ff_meta = data.get("freeform") if isinstance(data.get("freeform"), dict) else {}
        review_rounds = ff_meta.get("review_rounds") if isinstance(ff_meta.get("review_rounds"), list) else []
        freeform_version = int(ff_meta.get("current_version") or 0) if freeform_now else 0
        fallback_reason = str(data.get("freeform_fallback_reason") or "")
        freeform_edit_running = gen_status == "updating" and _is_freeform_edit(data)
        if gen_status == "ready":
            if freeform_now:
                message = f"自由デザイン版（v{freeform_version}）を公開しています（{describe_generation_engine(engine)}）。"
                if review_rounds:
                    message += f" 公開前にエージェントが描画結果を見て {len(review_rounds)} 回見直しました。"
            else:
                message = f"生成は完了しています（{describe_generation_engine(engine)}）。共有URLを開くと提案ページが表示されます。"
            if last_edit_status == "applied":
                if freeform_now:
                    message += " 直近の修正は反映済みです。"
                else:
                    message += f" 直近の修正は反映済みです（版 v{content_version}）。"
            elif last_edit_status == "failed":
                message += " 直近の修正は反映できなかったため、修正前の版を表示しています。"
            elif last_edit_status == "no_change":
                message += " 直近の修正依頼では、反映できる変更点がありませんでした。"
        elif gen_status == "updating":
            edit_elapsed = _seconds_since(data.get("edit_requested_at"))
            if freeform_edit_running:
                message = (
                    f"自由デザインの修正を反映中です（フェーズ: {_phase_label(phase)}、経過 {int(edit_elapsed or 0)} 秒、"
                    f"{FREEFORM_EDIT_ETA}）。共有URLでは修正前の版に『更新中』バナーが表示され、完了すると自動で最新版に切り替わります。"
                )
            else:
                message = (
                    f"修正を反映中です（フェーズ: {_phase_label(phase)}、経過 {int(edit_elapsed or 0)} 秒）。"
                    "共有URLを開いている画面には『更新中』バナーが表示され、完了すると自動で最新版に切り替わります。"
                )
        elif gen_status == "generating":
            if design_mode == "freeform":
                message = (
                    f"自由デザインで生成中です（フェーズ: {_phase_label(phase)}、経過 {int(elapsed_seconds or 0)} 秒、"
                    f"{FREEFORM_CREATE_ETA}）。共有URLでは生成中画面が表示され、完成すると自動的に提案ページへ切り替わります。"
                )
            else:
                message = (
                    f"現在生成中です（フェーズ: {_phase_label(phase)}、経過 {int(elapsed_seconds or 0)} 秒）。"
                    "共有URLでは生成中画面が表示され、完成すると自動的に提案ページへ切り替わります。"
                )
        elif gen_status == "failed":
            message = f"生成に失敗しました: {str(data.get('generation_error') or '')[:200]}"
        else:
            message = "生成状況を判定できませんでした。"
        if gen_status == "ready" and not freeform_now and fallback_reason:
            message += " 自由デザイン版は公開できなかったため、テンプレートで仕上げています。"
        if gen_status == "generating" and design_mode == "freeform":
            estimated = FREEFORM_CREATE_ETA
        elif freeform_edit_running:
            estimated = FREEFORM_EDIT_ETA
        else:
            estimated = ""
        deck_spec_data = data.get("deck_spec") if isinstance(data.get("deck_spec"), dict) else {}
        versions = data.get("freeform_versions") if isinstance(data.get("freeform_versions"), list) else []
        result = {
            "status": "STATUS",
            "presentation_id": presentation_id,
            "client_name": data.get("client_name", ""),
            "proposal_title": data.get("proposal_title", ""),
            "generation_status": gen_status,
            "generation_phase": phase,
            "generation_phase_label": _phase_label(phase),
            "generation_detail": data.get("generation_detail", ""),
            "generation_engine": engine,
            "generation_engine_label": describe_generation_engine(engine) if engine else "",
            "generation_dispatch": data.get("generation_dispatch", ""),
            "elapsed_seconds": elapsed_seconds,
            "estimated_completion": estimated,
            "stale_repair_applied": repaired,
            "edit_stale_repair_applied": edit_repaired,
            "ready_at": data.get("ready_at", ""),
            "share_url": f"{hosting_base_url}/p/{presentation_id}",
            "viewer_id": data.get("viewer_id", ""),
            "expires_at": data.get("expires_at", ""),
            "is_active": bool(data.get("is_active", True)),
            "design_mode": design_mode,
            "render_mode": render_mode,
            "ui_format": normalize_ui_format(data.get("ui_format") or deck_spec_data.get("ui_format") or DEFAULT_UI_FORMAT),
            "design_style": data.get("design_style") or deck_spec_data.get("design_style") or DEFAULT_DESIGN_STYLE,
            "content_version": content_version,
            "freeform_version": freeform_version or None,
            "available_freeform_versions": [
                int(v.get("version") or 0) for v in versions if isinstance(v, dict)
            ],
            "slide_count": data.get("slide_count") if freeform_now else None,
            "review_rounds": len(review_rounds) if freeform_now else 0,
            "review_round_details": [
                {
                    "round": r.get("round"),
                    "review_status": r.get("review_status"),
                    "files_changed": r.get("files_changed"),
                    "screenshots_sent": r.get("screenshots_sent"),
                }
                for r in review_rounds
                if isinstance(r, dict)
            ]
            if freeform_now
            else [],
            "freeform_warnings": [str(w)[:200] for w in (ff_meta.get("warnings") or [])][:6] if freeform_now else [],
            "freeform_fallback_reason": fallback_reason[:300],
            "last_edit_result": _public_last_edit(last_edit),
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
    edit_instructions: str = "",
    new_title: str = "",
    new_subtitle: str = "",
    new_theme_color: str = "",
    new_custom_callout: str = "",
    new_design_style: str = "",
    new_ui_format: str = "",
    ui_format: str = "",
    undo_last_edit: bool = False,
    convert_to_freeform: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Edits a published proposal website in place (same URL) and reports only the changes that were actually applied.

    Template decks (fast mode, fixed 6 slides): the edit runs synchronously (typically 15-60 seconds) and supports
    slide text edits, the accent colour (`theme_color`) and the overall look (`design_style`): 'immersive-dark'
    (dark navy, default), 'clean-light' (white background, minimal) and 'editorial-light' (off-white, serif headings).
    Slides cannot be added or removed and graphs/images cannot be added in the template.

    Free-form decks (`render_mode` 'freeform'): any change is possible (layout, colours, slides, graphs, diagrams,
    images). The request is queued and applied in the background by the ADK designer agent, which
    checks the rendered screenshots before publication (typically 5-7 minutes). The tool returns 'EDIT_QUEUED';
    the old version stays visible with an "更新中" banner and switches automatically when the new one is published.

    Set `convert_to_freeform=True` (after the user agrees) to rebuild a template deck as a free-form deck when the
    request cannot be expressed by the template (adding slides, graphs or images, a completely different layout).
    It typically takes 7-10 minutes and keeps the same URL.

    Args:
        presentation_id: ID of the existing presentation (e.g., 'prop-20261003-xxxxxxxx').
        edit_instructions: The user's change request in natural language (pass it verbatim).
        new_title: Optional explicit replacement for the proposal title.
        new_subtitle: Optional explicit replacement for the proposal subtitle.
        new_theme_color: Optional new accent color ('sky', 'emerald', 'violet', 'amber', 'rose').
        new_custom_callout: Optional callout badge text to display on the cover slide.
        new_design_style: Optional overall look ('immersive-dark', 'clean-light', 'editorial-light'),
            e.g. 'clean-light' for "背景を白に".
        undo_last_edit: If True, restores the version before the most recent edit (「元に戻して」). For free-form decks
            this switches back to the previous version instantly.
        convert_to_freeform: If True, rebuilds a template deck as a free-form deck (same URL).
        tool_context: Optional ADK ToolContext.

    Returns:
        Dictionary with `status`: 'UPDATED' (changes applied - report ONLY `verified_changes`), 'EDIT_QUEUED'
        (free-form edit accepted and running in the background - NOT finished yet), 'NO_CHANGE' (nothing could be
        applied - the deck is unchanged), 'BUSY' (another edit is still being applied), 'GENERATING' (deck not
        generated yet), 'EDIT_FAILED' / 'ERROR' (previous version kept) or 'NOT_FOUND'; plus `share_url`,
        `verified_changes`, `unsupported_requests`, `designer_notes`, `render_mode`, `content_version`,
        `estimated_completion`, `user_message` and `next_action`.
    """
    try:
        return _edit_proposal_website_impl(
            presentation_id=presentation_id,
            edit_instructions=edit_instructions,
            new_title=new_title,
            new_subtitle=new_subtitle,
            new_theme_color=new_theme_color,
            new_custom_callout=new_custom_callout,
            new_design_style=new_design_style,
            new_ui_format=new_ui_format or ui_format,
            undo_last_edit=undo_last_edit,
            convert_to_freeform=convert_to_freeform,
            tool_context=tool_context,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "edit_proposal_website", presentation_id=presentation_id)


def _apply_presentation_edit(
    *,
    presentation_id: str,
    doc_data: dict[str, Any],
    current_deck: PresentationDeckSpec,
    instructions: str,
    explicit: dict[str, str],
    rejected: list[str],
    undo_last_edit: bool,
    bucket_name: str,
    blob_path: str,
    old_version: int,
    set_phase: Callable[[str, str], None],
) -> dict[str, Any]:
    """Computes the edited deck, and (only if something visibly changed) backs up + republishes the HTML."""
    from google.cloud import storage

    storage_client = storage.Client(project=_get_project_id())
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    existing_html = ""
    try:
        if hasattr(blob, "download_as_text"):
            existing_html = blob.download_as_text(encoding="utf-8")
        else:
            existing_html = blob.download_as_bytes().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.info("Existing HTML for %s unavailable (%s); editing from deck_spec", presentation_id, exc)

    designer_notes: list[str] = []
    unsupported: list[str] = list(rejected)
    adjustments: list[str] = []
    base = {
        "existing_html_loaded": bool(existing_html),
        "version_blob": "",
    }
    if undo_last_edit:
        previous = doc_data.get("previous_deck_spec")
        if not previous:
            unsupported.append("元に戻せる直前の版がありません（まだ修正していないか、取り消し済みです）")
            return {
                **base,
                "kind": "no_change",
                "engine": "undo",
                "new_deck": current_deck,
                "verified_changes": [],
                "changed_fields": [],
                "designer_notes": [],
                "unsupported": unsupported,
                "adjustments": [],
            }
        new_deck = PresentationDeckSpec.model_validate(previous)
        engine = "undo"
        designer_notes = ["直前の修正を取り消し、1つ前の版に戻しました"]
    else:
        new_deck = current_deck.model_copy(deep=True)
        engine = "heuristic" if instructions else "explicit"
        llm_used = False
        if instructions and _llm_edit_enabled():
            set_phase("edit_designing", _get_model_name())
            try:
                edit_result, used = _edit_deck_with_gemini(current_deck, instructions, explicit)
                new_deck = edit_result.deck_spec
                designer_notes = [str(note)[:200] for note in edit_result.change_summary[:8]]
                unsupported.extend(str(item)[:200] for item in edit_result.unsupported_requests[:5])
                engine = f"gemini:{used}"
                llm_used = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM deck edit unavailable for %s; using keyword heuristics: %s", presentation_id, exc)
                engine = "heuristic:llm_unavailable"
        new_deck, adjustments = _apply_edit_heuristics(current_deck, new_deck, instructions, llm_used)
        for field_name, value in explicit.items():
            setattr(new_deck, field_name, value)
    _normalize_deck_in_place(new_deck)
    new_deck.client_slug = current_deck.client_slug

    changed_fields, verified_changes = describe_deck_changes(current_deck, new_deck)
    if not changed_fields:
        if instructions and not unsupported:
            unsupported.append(
                f"指示「{_clip(instructions, 60)}」から、テンプレートで反映できる具体的な変更点を特定できませんでした"
            )
        return {
            **base,
            "kind": "no_change",
            "engine": engine,
            "new_deck": current_deck,
            "verified_changes": [],
            "changed_fields": [],
            "designer_notes": [],
            "unsupported": unsupported,
            "adjustments": adjustments,
        }

    set_phase("edit_rendering", f"{len(changed_fields)} fields")
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_jst = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    html_content = render_deck_html(
        new_deck, generated_date=now_jst.strftime("%Y-%m-%d %H:%M JST (Updated)")
    )

    set_phase("edit_publishing", "")
    version_blob = ""
    if existing_html:
        version_blob = (
            f"presentations/{presentation_id}/versions/{now_utc.strftime('%Y%m%dT%H%M%SZ')}-v{old_version}.html"
        )
        try:
            backup = bucket.blob(version_blob)
            backup.cache_control = "no-store, private"
            backup.upload_from_string(
                existing_html.encode("utf-8"), content_type="text/html; charset=utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Version backup for %s failed (continuing): %s", presentation_id, exc)
            version_blob = ""
    blob.cache_control = "no-store, private"
    blob.upload_from_string(html_content.encode("utf-8"), content_type="text/html; charset=utf-8")
    return {
        **base,
        "kind": "applied",
        "engine": engine,
        "new_deck": new_deck,
        "verified_changes": verified_changes,
        "changed_fields": changed_fields,
        "designer_notes": designer_notes,
        "unsupported": unsupported,
        "adjustments": adjustments,
        "version_blob": version_blob,
    }


def _edit_proposal_website_impl(
    presentation_id: str,
    edit_instructions: str = "",
    new_title: str = "",
    new_subtitle: str = "",
    new_theme_color: str = "",
    new_custom_callout: str = "",
    new_design_style: str = "",
    new_ui_format: str = "",
    undo_last_edit: bool = False,
    convert_to_freeform: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    project_id = _get_project_id()
    collection_name = _get_firestore_collection()
    share_url = f"{_get_hosting_base_url()}/p/{presentation_id}"

    fs_client = _get_firestore_client(project_id)
    doc_ref = fs_client.collection(collection_name).document(presentation_id)
    doc_snap = doc_ref.get()
    if not doc_snap.exists:
        raise ValueError(f"Presentation '{presentation_id}' not found in Firestore.")
    doc_data = doc_snap.to_dict() or {}

    gen_status = str(doc_data.get("generation_status") or "").lower()
    if gen_status == "generating":
        return {
            "status": "GENERATING",
            "presentation_id": presentation_id,
            "generation_phase": doc_data.get("generation_phase", ""),
            "share_url": share_url,
            "user_message": (
                "このプレゼンテーションはまだ生成中のため、まだ修正できません。"
                "完成後（共有URLが提案ページに切り替わった後）に再度ご指示ください。"
            ),
        }
    if gen_status == "updating" and not _is_edit_stale(doc_data):
        return {
            "status": "BUSY",
            "presentation_id": presentation_id,
            "generation_phase": doc_data.get("generation_phase", ""),
            "share_url": share_url,
            "user_message": (
                f"前回の修正（自由デザイン）を反映中です（{FREEFORM_EDIT_ETA}）。完了後に改めてご指示ください。"
                if _is_freeform_edit(doc_data)
                else "前回の修正を反映中です（通常1分以内に完了します）。完了後に改めてご指示ください。"
            ),
            "next_action": "前の修正の反映中であることを伝え、少し待ってから再度依頼いただくよう案内してください。",
        }

    if _is_freeform_rendered(doc_data) or (convert_to_freeform and not undo_last_edit):
        raw_explicit = {
            key: str(value).strip()[:300]
            for key, value in (
                ("proposal_title", new_title),
                ("subtitle", new_subtitle),
                ("theme_color", new_theme_color),
                ("custom_callout", new_custom_callout),
                ("design_style", new_design_style),
                ("ui_format", normalize_ui_format(new_ui_format, default="") if str(new_ui_format or "").strip() else _detect_requested_ui_format(edit_instructions or "")),
            )
            if str(value or "").strip()
        }
        return _freeform_edit_entry(
            presentation_id=presentation_id,
            doc_ref=doc_ref,
            doc_data=doc_data,
            share_url=share_url,
            instructions=(edit_instructions or "").strip(),
            explicit=raw_explicit,
            undo_last_edit=bool(undo_last_edit),
            convert=not _is_freeform_rendered(doc_data),
            started=started,
            tool_context=tool_context,
        )

    instructions = (edit_instructions or "").strip()
    explicit: dict[str, str] = {}
    rejected: list[str] = []
    if (new_title or "").strip():
        explicit["proposal_title"] = new_title.strip()
    if (new_subtitle or "").strip():
        explicit["subtitle"] = new_subtitle.strip()
    theme_req = (new_theme_color or "").strip().lower()
    if theme_req:
        if theme_req in SUPPORTED_THEME_COLORS:
            explicit["theme_color"] = theme_req
        else:
            rejected.append(
                f"アクセントカラー「{new_theme_color}」は未対応です（sky / emerald / violet / amber / rose から選べます）"
            )
    if (new_custom_callout or "").strip():
        explicit["custom_callout"] = new_custom_callout.strip()[:120]
    if (new_design_style or "").strip():
        style_req = normalize_design_style(new_design_style, default="")
        if style_req:
            explicit["design_style"] = style_req
        else:
            rejected.append(
                f"デザインスタイル「{new_design_style}」は未対応です（immersive-dark / clean-light / editorial-light から選べます）"
            )
    if (new_ui_format or "").strip():
        ui_req = normalize_ui_format(new_ui_format, default="")
        if ui_req:
            explicit["ui_format"] = ui_req
        else:
            rejected.append(
                f"UI形式「{new_ui_format}」は未対応です（portal / slides から選べます）"
            )
    if not instructions and not explicit and not undo_last_edit and rejected:
        return {
            "status": "NO_CHANGE",
            "presentation_id": presentation_id,
            "share_url": share_url,
            "verified_changes": [],
            "changed_fields": [],
            "updated_fields": [],
            "designer_notes": [],
            "unsupported_requests": rejected,
            "content_version": _content_version_of(doc_data),
            "design_style": normalize_design_style(doc_data.get("design_style")),
            "theme_color": doc_data.get("theme_color", ""),
            "user_message": "指定された値はテンプレートで未対応のため、プレゼンテーションは変更していません。",
            "next_action": "変更していないことと unsupported_requests の理由を伝え、対応している選択肢から選んでもらってください。",
        }
    if not instructions and not explicit and not undo_last_edit:
        return {
            "status": "ERROR",
            "action": "edit_proposal_website",
            "error_type": "MissingInput",
            "presentation_id": presentation_id,
            "share_url": share_url,
            "unsupported_requests": rejected,
            "user_message": "修正内容（自然文の指示、または新しいタイトル・アクセントカラー・デザインスタイル等）を指定してください。",
            "next_action": "ユーザーに具体的な修正内容を確認してから再度呼び出してください。",
        }

    raw_spec = doc_data.get("deck_spec")
    if raw_spec:
        current_deck = PresentationDeckSpec.model_validate(raw_spec)
    else:
        current_deck = _default_deck_spec_from_brief(
            client_name=doc_data.get("client_name", "Client"),
            proposal_title=doc_data.get("proposal_title", "Proposal"),
            proposal_brief=doc_data.get("subtitle", ""),
            theme_color=doc_data.get("theme_color", "sky"),
        )
        current_deck.design_style = normalize_design_style(doc_data.get("design_style"))
    _normalize_deck_in_place(current_deck)
    old_version = _content_version_of(doc_data)
    bucket_name = str(doc_data.get("gcs_bucket") or _get_bucket_name())
    blob_path = str(doc_data.get("gcs_blob_path") or f"presentations/{presentation_id}/index.html")

    # 1) Mark the deck as "updating" so every open viewer immediately sees the 更新中 banner.
    request_id = uuid.uuid4().hex[:12]
    requested_at = _utc_now_iso()
    doc_ref.update(
        {
            "generation_status": "updating",
            "generation_phase": "edit_queued",
            "generation_detail": "",
            "edit_request": {
                "request_id": request_id,
                "instructions": instructions[:2000],
                "explicit_changes": dict(explicit),
                "undo": bool(undo_last_edit),
                "requested_at": requested_at,
            },
            "edit_requested_at": requested_at,
            "updated_at": requested_at,
        }
    )

    def set_phase(phase: str, detail: str = "") -> None:
        try:
            doc_ref.update(
                {"generation_phase": phase, "generation_detail": detail[:300], "updated_at": _utc_now_iso()}
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("edit progress update skipped: %s", exc)

    # 2) Apply the edit; on ANY failure revert to the previous (still published) version.
    try:
        outcome = _apply_presentation_edit(
            presentation_id=presentation_id,
            doc_data=doc_data,
            current_deck=current_deck,
            instructions=instructions,
            explicit=explicit,
            rejected=rejected,
            undo_last_edit=bool(undo_last_edit),
            bucket_name=bucket_name,
            blob_path=blob_path,
            old_version=old_version,
            set_phase=set_phase,
        )
        new_deck: PresentationDeckSpec = outcome["new_deck"]
        engine = str(outcome["engine"])
        applied = outcome["kind"] == "applied"
        new_version = old_version + 1 if applied else old_version
        finished_at = _utc_now_iso()
        last_edit_result = {
            "status": "applied" if applied else "no_change",
            "request_id": request_id,
            "edit_engine": engine,
            "edit_engine_label": describe_edit_engine(engine),
            "verified_changes": outcome["verified_changes"],
            "changed_fields": outcome["changed_fields"],
            "designer_notes": outcome["designer_notes"],
            "unsupported_requests": outcome["unsupported"],
            "heuristic_adjustments": outcome["adjustments"],
            "previous_version_blob": outcome["version_blob"],
            "content_version": new_version,
            "design_style": new_deck.design_style,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "applied_at": finished_at,
        }
        updates: dict[str, Any] = {
            "generation_status": "ready",
            "generation_phase": "ready",
            "generation_detail": "",
            "last_edit_result": last_edit_result,
            "last_edit_instructions": instructions[:2000],
            "edit_request": None,
            "updated_at": finished_at,
        }
        if applied:
            updates.update(
                {
                    "client_name": new_deck.client_name,
                    "proposal_title": new_deck.proposal_title,
                    "subtitle": new_deck.subtitle,
                    "theme_color": new_deck.theme_color,
                    "design_style": new_deck.design_style,
                    "ui_format": new_deck.ui_format,
                    "deck_spec": new_deck.model_dump(),
                    "previous_deck_spec": current_deck.model_dump(),
                    "content_version": new_version,
                    "last_edited_at": finished_at,
                }
            )
        doc_ref.update(updates)
    except Exception as exc:  # noqa: BLE001
        failed_at = _utc_now_iso()
        try:
            doc_ref.update(
                {
                    "generation_status": "ready",
                    "generation_phase": "ready",
                    "generation_detail": "",
                    "last_edit_result": {
                        "status": "failed",
                        "request_id": request_id,
                        "edit_engine": "",
                        "edit_engine_label": "",
                        "verified_changes": [],
                        "unsupported_requests": rejected,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        "content_version": old_version,
                        "applied_at": failed_at,
                    },
                    "edit_request": None,
                    "updated_at": failed_at,
                }
            )
        except Exception as revert_exc:  # noqa: BLE001
            logger.warning("Could not revert %s to ready after edit failure: %s", presentation_id, revert_exc)
        payload = _tool_error(exc, "edit_proposal_website", presentation_id=presentation_id, share_url=share_url)
        payload["status"] = "EDIT_FAILED"
        payload["verified_changes"] = []
        payload["user_message"] = (
            "修正の反映中にエラーが発生したため、プレゼンテーションは修正前の版のままです。時間をおいて再度お試しください。"
        )
        payload["next_action"] = "修正が反映されなかったことと、修正前の版が引き続き表示されていることを正直に伝えてください。"
        return payload

    if applied:
        user_message = (
            f"修正を反映しました（版 v{new_version}）。共有URLは変わらず、開いている画面も自動で最新版に切り替わります。"
        )
        next_action = (
            "『反映した変更点』として verified_changes の項目だけを箇条書きで伝えてください。designer_notes は見た目の補足説明にのみ使い、"
            "verified_changes にない変更を実施したと言わないでください。unsupported_requests があれば『反映できなかった点』として"
            "正直に伝え、代替案を示してください。元に戻したい場合は「元に戻して」と言えば取り消せることも案内してください。"
        )
    else:
        user_message = "修正指示から反映できる変更点を特定できなかったため、プレゼンテーションは変更していません。"
        next_action = (
            "変更されていないことを正直に伝え、unsupported_requests の理由を説明したうえで、どのスライドのどの文言・"
            "アクセントカラー・デザインスタイル（immersive-dark / clean-light / editorial-light）をどう変えたいかを具体的に聞き返してください。"
        )
    result = {
        "status": "UPDATED" if applied else "NO_CHANGE",
        "presentation_id": presentation_id,
        "share_url": share_url,
        "client_name": new_deck.client_name,
        "proposal_title": new_deck.proposal_title,
        "subtitle": new_deck.subtitle,
        "theme_color": new_deck.theme_color,
        "ui_format": new_deck.ui_format,
        "design_style": new_deck.design_style,
        "design_style_label": DESIGN_STYLE_LABELS.get(new_deck.design_style, new_deck.design_style),
        "custom_callout": new_deck.custom_callout,
        "verified_changes": outcome["verified_changes"],
        "changed_fields": outcome["changed_fields"],
        "updated_fields": outcome["changed_fields"],
        "designer_notes": outcome["designer_notes"],
        "unsupported_requests": outcome["unsupported"],
        "edit_engine": engine,
        "edit_engine_label": describe_edit_engine(engine),
        "content_version": new_version,
        "previous_version_saved": bool(outcome["version_blob"]),
        "existing_html_loaded": outcome["existing_html_loaded"],
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "updated_at": finished_at,
        "slide_outline": _build_slide_outline(new_deck),
        "user_message": user_message,
        "next_action": next_action,
    }
    if tool_context is not None:
        tool_context.state["last_edited_result"] = result
    return result


# ---------------------------------------------------------------------------
# Free-form edits: queued for the background job; undo is an instant pointer switch
# ---------------------------------------------------------------------------
_FREEFORM_EDIT_STATUS = {"applied": "UPDATED", "no_change": "NO_CHANGE", "failed": "EDIT_FAILED"}


def _freeform_edit_messages(status: str, kind: str, freeform_version: Any, error: str) -> tuple[str, str]:
    if status == "UPDATED":
        if kind == "undo":
            return (
                "1つ前の版に戻しました。共有URLは変わらず、開いている画面も自動で切り替わります。",
                "verified_changes の内容だけを伝えてください。",
            )
        return (
            f"修正を反映しました（自由デザイン版 v{freeform_version}）。共有URLは変わらず、開いている画面も自動で最新版に切り替わります。",
            "『反映した変更点』として verified_changes（公開前後のファイルを機械的に比べた結果）だけを箇条書きで伝えてください。"
            "designer_notes はデザイナーエージェント自身の説明（自己申告）なので、そうと分かるように区別して添える程度にとどめ、"
            "verified_changes にない変更を実施したと言わないでください。「元に戻して」で1つ前の版に戻せることも案内してください。",
        )
    if status == "NO_CHANGE":
        return (
            "反映できる変更がなかったため、プレゼンテーションは変更していません。",
            "変更されていないことを正直に伝え、どこをどう変えたいかを具体的に聞き返してください。",
        )
    if status == "EDIT_FAILED":
        return (
            f"修正を反映できなかったため、修正前の版のままです（{error[:120]}）。",
            "修正が反映されなかったことと、修正前の版が引き続き表示されていることを正直に伝えてください。",
        )
    return (
        "修正を受け付けました。裏側でデザイナーエージェントが修正し、描画結果を確認してから公開します。",
        "まだ完了していないことを伝え、完了したかは get_proposal_status で確認すると案内してください。",
    )


def _freeform_last_edit_payload(
    presentation_id: str, share_url: str, data: dict[str, Any], request_id: str, started: float
) -> dict[str, Any]:
    """Maps the worker's `last_edit_result` (sync trigger mode) to the tool response."""
    last = data.get("last_edit_result") if isinstance(data.get("last_edit_result"), dict) else {}
    if request_id and str(last.get("request_id") or "") != request_id:
        last = {}
    status = _FREEFORM_EDIT_STATUS.get(str(last.get("status") or ""), "EDIT_QUEUED")
    kind = str(last.get("kind") or "edit")
    user_message, next_action = _freeform_edit_messages(
        status, kind, last.get("freeform_version"), str(last.get("error") or "")
    )
    return {
        "status": status,
        "presentation_id": presentation_id,
        "share_url": share_url,
        "kind": kind,
        "render_mode": "freeform" if _is_freeform_rendered(data) else str(data.get("render_mode") or "template"),
        "ui_format": normalize_ui_format(data.get("ui_format") or DEFAULT_UI_FORMAT),
        "verified_changes": list(last.get("verified_changes") or []),
        "designer_notes": list(last.get("designer_notes") or []),
        "unsupported_requests": list(last.get("unsupported_requests") or []),
        "review_rounds": last.get("review_rounds", 0),
        "freeform_version": last.get("freeform_version"),
        "edit_engine": last.get("edit_engine", ""),
        "edit_engine_label": last.get("edit_engine_label", ""),
        "content_version": _content_version_of(data),
        "error": str(last.get("error") or ""),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "user_message": user_message,
        "next_action": next_action,
    }


def _queue_freeform_edit(
    *,
    presentation_id: str,
    doc_ref: Any,
    doc_data: dict[str, Any],
    share_url: str,
    instructions: str,
    explicit: dict[str, str],
    convert: bool,
    started: float,
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:12]
    requested_at = _utc_now_iso()
    kind = "convert" if convert else "edit"
    doc_ref.update(
        {
            "generation_status": "updating",
            "generation_phase": "freeform_edit_queued",
            "generation_detail": "",
            "edit_request": {
                "request_id": request_id,
                "instructions": instructions[:2000],
                "explicit_changes": dict(explicit),
                "undo": False,
                "requested_at": requested_at,
                "mode": "freeform",
                "kind": kind,
            },
            "edit_requested_at": requested_at,
            "updated_at": requested_at,
        }
    )
    dispatch = _start_background_generation(presentation_id, job_mode="freeform_edit")
    dispatch_mode = str(dispatch.get("mode") or "")
    if dispatch_mode == "cloud_run_job_failed":
        failed_at = _utc_now_iso()
        error = str(dispatch.get("error") or "job trigger failed")
        try:
            doc_ref.update(
                {
                    "generation_status": "ready",
                    "generation_phase": "ready",
                    "generation_detail": "",
                    "edit_request": None,
                    "last_edit_result": {
                        "status": "failed",
                        "request_id": request_id,
                        "kind": kind,
                        "edit_engine": "",
                        "edit_engine_label": "",
                        "verified_changes": [],
                        "unsupported_requests": [],
                        "error": f"修正ジョブを開始できませんでした: {error[:200]}",
                        "content_version": _content_version_of(doc_data),
                        "applied_at": failed_at,
                    },
                    "updated_at": failed_at,
                }
            )
        except Exception as revert_exc:  # noqa: BLE001
            logger.warning("Could not revert %s after job trigger failure: %s", presentation_id, revert_exc)
        user_message, next_action = _freeform_edit_messages("EDIT_FAILED", kind, None, "修正ジョブを開始できませんでした")
        return {
            "status": "EDIT_FAILED",
            "presentation_id": presentation_id,
            "share_url": share_url,
            "kind": kind,
            "verified_changes": [],
            "error": error[:300],
            "content_version": _content_version_of(doc_data),
            "user_message": user_message,
            "next_action": next_action,
        }
    try:
        doc_ref.update(
            {
                "edit_dispatch": dispatch_mode,
                "edit_execution": str(dispatch.get("execution", ""))[:300],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("edit dispatch bookkeeping skipped: %s", exc)
    if dispatch_mode == "sync":
        snap = doc_ref.get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        return _freeform_last_edit_payload(presentation_id, share_url, data, request_id, started)
    eta = FREEFORM_CREATE_ETA if convert else FREEFORM_EDIT_ETA
    user_message, _ = _freeform_edit_messages("EDIT_QUEUED", kind, None, "")
    if convert:
        user_message = (
            "テンプレート版を自由デザイン版に作り直す依頼を受け付けました。完成するまでは今のテンプレート版に『更新中』と表示され、"
            "完成すると同じURLのまま自動で切り替わります。"
        )
    return {
        "status": "EDIT_QUEUED",
        "presentation_id": presentation_id,
        "share_url": share_url,
        "kind": kind,
        "request_id": request_id,
        "render_mode": str(doc_data.get("render_mode") or "template"),
        "ui_format": normalize_ui_format(explicit.get("ui_format") or doc_data.get("ui_format") or DEFAULT_UI_FORMAT),
        "edit_dispatch": dispatch_mode,
        "estimated_completion": eta,
        "verified_changes": [],
        "content_version": _content_version_of(doc_data),
        "user_message": user_message,
        "next_action": (
            f"まだ完了していません。『修正を受け付け、反映中です（{eta}）。共有URLでは今の版に「更新中」と表示され、完了すると"
            "自動で新しい版に切り替わります』と伝えてください。反映済みと言ってはいけません。結果は get_proposal_status の"
            " last_edit_result で確認できます。"
        ),
    }


def _freeform_target_exists(bucket_name: str, prefix: str) -> bool:
    try:
        from google.cloud import storage

        blob = storage.Client(project=_get_project_id()).bucket(bucket_name).blob(prefix + "index.html")
        return bool(blob.exists())
    except Exception as exc:  # noqa: BLE001 - unknown -> let the gateway decide
        logger.info("version existence check skipped (%s): %s", prefix, exc)
        return True


def _freeform_undo(
    presentation_id: str,
    doc_ref: Any,
    doc_data: dict[str, Any],
    share_url: str,
    started: float,
) -> dict[str, Any]:
    """Switches the published pointer back to the version this one was based on (no regeneration)."""
    ff_meta = dict(doc_data.get("freeform") or {})
    versions = [v for v in (doc_data.get("freeform_versions") or []) if isinstance(v, dict)]
    by_number = {int(v.get("version") or 0): v for v in versions}
    current = int(ff_meta.get("current_version") or 0)
    entry = by_number.get(current)
    if entry is not None and "based_on" in entry:
        target_number = int(entry.get("based_on") or 0)
    else:
        lower = [number for number in by_number if 0 < number < current]
        target_number = max(lower) if lower else 0
    target = by_number.get(target_number) if target_number else None
    bucket_name = str(doc_data.get("gcs_bucket") or _get_bucket_name())
    updates: dict[str, Any]
    if target is not None and target.get("prefix") and _freeform_target_exists(bucket_name, str(target["prefix"])):
        prefix = str(target["prefix"])
        ff_meta.update({"current_version": target_number, "prefix": prefix})
        verified = [f"表示する自由デザイン版を v{current} から v{target_number} に戻しました"]
        updates = {
            "render_mode": "freeform",
            "freeform_prefix": prefix,
            "freeform": ff_meta,
            "gcs_bucket": bucket_name,
            "gcs_blob_path": prefix + "index.html",
            "gcs_uri": f"gs://{bucket_name}/{prefix}index.html",
        }
        if target.get("slide_count"):
            updates["slide_count"] = int(target.get("slide_count") or 0)
        if isinstance(target.get("slide_titles"), list):
            updates["slide_titles"] = [str(t) for t in target["slide_titles"]]
        new_render_mode = "freeform"
    elif doc_data.get("deck_spec"):
        blob_path = f"presentations/{presentation_id}/index.html"
        verified = [f"自由デザイン版 v{current} から、変換前のテンプレート版に戻しました"]
        updates = {
            "render_mode": "template",
            "gcs_bucket": bucket_name,
            "gcs_blob_path": blob_path,
            "gcs_uri": f"gs://{bucket_name}/{blob_path}",
        }
        target_number = 0
        new_render_mode = "template"
    else:
        return {
            "status": "NO_CHANGE",
            "presentation_id": presentation_id,
            "share_url": share_url,
            "kind": "undo",
            "render_mode": "freeform",
            "verified_changes": [],
            "designer_notes": [],
            "unsupported_requests": ["元に戻せる直前の版がありません（最初に作成された自由デザイン版を表示中です）"],
            "content_version": _content_version_of(doc_data),
            "user_message": "元に戻せる直前の版がないため、プレゼンテーションは変更していません。",
            "next_action": "変更していないことを正直に伝えてください。",
        }
    now = _utc_now_iso()
    new_content_version = _content_version_of(doc_data) + 1
    last_edit = {
        "status": "applied",
        "request_id": uuid.uuid4().hex[:12],
        "kind": "undo",
        "edit_engine": "freeform_undo",
        "edit_engine_label": describe_edit_engine("freeform_undo"),
        "verified_changes": verified,
        "designer_notes": [],
        "unsupported_requests": [],
        "previous_render_mode": "freeform",
        "previous_freeform_version": current,
        "freeform_version": target_number,
        "content_version": new_content_version,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "applied_at": now,
    }
    updates.update(
        {
            "content_version": new_content_version,
            "generation_status": "ready",
            "generation_phase": "ready",
            "generation_detail": "",
            "edit_request": None,
            "last_edit_result": last_edit,
            "last_edited_at": now,
            "updated_at": now,
        }
    )
    doc_ref.update(updates)
    user_message, next_action = _freeform_edit_messages("UPDATED", "undo", target_number, "")
    return {
        "status": "UPDATED",
        "presentation_id": presentation_id,
        "share_url": share_url,
        "kind": "undo",
        "render_mode": new_render_mode,
        "freeform_version": target_number or None,
        "verified_changes": verified,
        "designer_notes": [],
        "unsupported_requests": [],
        "edit_engine": "freeform_undo",
        "edit_engine_label": describe_edit_engine("freeform_undo"),
        "content_version": new_content_version,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "user_message": user_message,
        "next_action": next_action,
    }


def _freeform_edit_entry(
    *,
    presentation_id: str,
    doc_ref: Any,
    doc_data: dict[str, Any],
    share_url: str,
    instructions: str,
    explicit: dict[str, str],
    undo_last_edit: bool,
    convert: bool,
    started: float,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    """Free-form decks (and template -> free-form conversion) are edited by the background job."""
    if undo_last_edit:
        result = _freeform_undo(presentation_id, doc_ref, doc_data, share_url, started)
    elif not _freeform_enabled():
        result = {
            "status": "NO_CHANGE",
            "presentation_id": presentation_id,
            "share_url": share_url,
            "render_mode": str(doc_data.get("render_mode") or "template"),
            "verified_changes": [],
            "designer_notes": [],
            "unsupported_requests": [
                "自由デザイン機能（FREEFORM_DESIGN_ENABLED）が無効のため、自由デザインでの修正・作り直しはできません"
            ],
            "content_version": _content_version_of(doc_data),
            "user_message": "自由デザイン機能が無効のため、プレゼンテーションは変更していません。",
            "next_action": "変更していないことと、その理由を正直に伝えてください。",
        }
    elif not instructions and not explicit:
        result = {
            "status": "ERROR",
            "action": "edit_proposal_website",
            "error_type": "MissingInput",
            "presentation_id": presentation_id,
            "share_url": share_url,
            "user_message": "修正内容を自然文で指定してください（例：背景を白にして、売上推移のグラフを追加して）。",
            "next_action": "ユーザーに具体的な修正内容を確認してから再度呼び出してください。",
        }
    else:
        result = _queue_freeform_edit(
            presentation_id=presentation_id,
            doc_ref=doc_ref,
            doc_data=doc_data,
            share_url=share_url,
            instructions=instructions,
            explicit=explicit,
            convert=convert,
            started=started,
        )
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
                "generation_engine_label": describe_generation_engine(str(data.get("generation_engine") or ""))
                if data.get("generation_engine")
                else "",
                "design_style": data.get("design_style")
                or (data.get("deck_spec") or {}).get("design_style")
                or DEFAULT_DESIGN_STYLE,
                "content_version": _content_version_of(data),
                "last_edit_status": str((data.get("last_edit_result") or {}).get("status") or "")
                if isinstance(data.get("last_edit_result"), dict)
                else "",
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
    versions_deleted = 0
    freeform_objects_deleted = 0
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
            versions_prefix = f"presentations/{presentation_id}/versions/"
            for version_blob in storage_client.list_blobs(bucket_name, prefix=versions_prefix):
                try:
                    version_blob.delete()
                    versions_deleted += 1
                except Exception as version_exc:  # noqa: BLE001
                    logger.info("Version backup delete skipped: %s", version_exc)
            # Free-form versions (presentations/<id>/v<N>/...), the template copy and staging files.
            for prefix in (f"presentations/{presentation_id}/", f"staging/{presentation_id}/"):
                for extra_blob in storage_client.list_blobs(bucket_name, prefix=prefix):
                    name = str(getattr(extra_blob, "name", "") or "")
                    if not name or name == blob_path or name.startswith(versions_prefix):
                        continue
                    try:
                        extra_blob.delete()
                        freeform_objects_deleted += 1
                    except Exception as extra_exc:  # noqa: BLE001
                        logger.info("Free-form object delete skipped: %s", extra_exc)
        except Exception as exc:
            logger.warning("Failed to delete GCS blob on revoke: %s", exc)

    result = {
        "status": "REVOKED",
        "presentation_id": presentation_id,
        "client_name": data.get("client_name", ""),
        "proposal_title": data.get("proposal_title", ""),
        "is_active": False,
        "gcs_blob_deleted": gcs_deleted,
        "gcs_versions_deleted": versions_deleted,
        "gcs_freeform_objects_deleted": freeform_objects_deleted,
        "revoked_at": now_utc.isoformat(),
    }
    if tool_context is not None:
        tool_context.state["last_revoked_result"] = result
    return result


delete_presentation = delete_proposal_website


# ---------------------------------------------------------------------------
# Interactive Conversational Concierge Root Agent (LlmAgent)
# ---------------------------------------------------------------------------

CONCIERGE_INSTRUCTION = """あなたは提案書Webサイトの制作・配信・ライフサイクル管理を担う「インタラクティブ提案コンシェルジュ」です。
ユーザーが対話を通じてクライアント向けのHTML5プレゼンテーションサイト（16:9）を企画・発行し、発行後の修正・閲覧ログ確認・パスワード変更・公開停止までをチャットだけで完結できるよう支援します。

【UI形式（2通り）とデザインの作り方（2通り）】
- **UI形式（`ui_format`）**:
  - **Web提案ポータル形式（`portal`・既定）**: 4カラム構成（左章レール・自動スクロール追従目次・中央記事リーダー・右KPI/根拠リファレンス）で、図解・表・本文をスクロールして深く読める技術提案ポータルです。右上の「▢ スライドで見る」ボタンから、いつでもワンクリックでプレゼンスライド表示に切り替えられます。
  - **16:9 プレゼンスライド形式（`slides`）**: 従来の16:9固定キャンバスのスライド形式です。ユーザーが「スライド形式で」「16:9で」と明示した場合に `ui_format="slides"` を指定します。
- **デザインの作り方（`design_mode`）**:
  - **自由デザイン（`freeform`・既定）**: ADK のデザイナーエージェント（gemini-3.8-flash）が、章構成・レイアウト・配色・図解（SVG）・グラフ、必要に応じて AI 生成イメージ（最大4点）まで自由に設計します。公開前に描画結果のスクリーンショットをエージェント自身が見て、崩れや読みにくさを最大2回まで直します。所要時間は通常 7〜11 分（最長約 20 分）です。
  - **高速モード（`template`）**: 定型の6章構成テンプレートで、通常 1〜5 分で完成します。ユーザーが「高速モード」「テンプレートで」「急ぎで」と明示した場合だけ使います。

【最重要ルール：挨拶や曖昧な発話で勝手にWebサイトを生成しないこと】
1. **挨拶・初回相談時の対応（ツール呼び出し禁止）**:
   - ユーザーが「こんにちは」「はじめまして」「何ができますか？」「提案書を作りたい」など、具体的なクライアント名や作成指示を含まない挨拶・相談をしてきた場合は、**絶対に `create_proposal_website` を呼び出さないでください**。
   - まずは丁寧な日本語で挨拶し、提供できる機能（①社内ナレッジ検索と構成案の壁打ち、②クライアント専用HTMLプレゼンサイトの新規発行と限定公開URL・ID/Pass発行、③発行済みサイトの自然言語での修正・閲覧ログ確認・パスワード再発行・公開停止）を案内してください。
   - そのうえで、次の項目を問いかけてください：
     - ① 提案先のクライアント企業名・業界
     - ② 解決したい課題や提案テーマ（例：AIコンシェルジュ、統合データ基盤、OMOマーケなど）
     - ③ 見た目の希望（背景色・雰囲気・グラフや図解や画像の要否・枚数など）と、強調したい実績数値
     - ④ 自由デザイン（通常 7〜11 分）と高速モード（テンプレート、通常 1〜5 分）のどちらにするか（指定がなければ自由デザイン）

2. **構成案の相談・社内ナレッジ検索 (`search_internal_knowledge`)**:
   - 「まずは構成案を相談したい」「過去の類似事例を調べて」と依頼されたら、`search_internal_knowledge` で社内データストアの過去RFP・導入事例・標準メソドロジーを検索し、構成案をチャット上で提示して「この内容でWebサイトを発行してよろしいでしょうか？」と確認してください。自由デザインなら枚数は内容に合わせて提案してかまいません。

3. **提案Webサイトの新規生成・限定公開 (`create_proposal_website`)**:
   - ユーザーがクライアント名と提案テーマを指定して「この内容でWebサイトを発行して」「はい、お願いします」などと明示的に依頼・承認した場合にのみ呼び出してください。
   - `client_name` / `proposal_title` / `proposal_brief` / `theme_color` を自然言語で渡し、合意した構成案やスライドごとの要点は **`proposal_brief` の中に文章として含めてください**。
   - **数値の扱い（重要）**: `proposal_brief` に書く数値（割合・金額・件数・期間など）は、ユーザーが会話で述べたものか `search_internal_knowledge` の結果に書かれているものだけにしてください。あなたが考えた効果見込みを入れる場合は、数値の直後に必ず「（試算）」または「（目標）」と付け、根拠のある数値と区別してください。デザイナーはこの資料を根拠として公開前に数値を照合するため、ここで作った数値はそのままスライドに載ってしまいます。
   - `design_mode` は既定で `freeform` とし、高速モードを頼まれたときだけ `template` にしてください。見た目の希望（背景色・雰囲気・グラフ・図解・画像・枚数など）は、ユーザーの言葉のまま `design_request` に入れてください。テンプレートで白背景などの希望があれば `design_style` も指定します。
   - `deck_spec_json` には、完全な `PresentationDeckSpec` JSON が手元にある場合以外は何も渡さないでください。
   - このツールは **即座に** 共有URL・閲覧用ID・パスワードを発行して返します（`status` が `GENERATING`）。スライド本体は裏側で生成され、自由デザイン版を公開できない場合もテンプレートで必ず仕上がります。
   - ツール応答を受け取ったら、**その同じターン内で必ず** 次を日本語でわかりやすく提示してください（決して無言で終わらないこと）：
     1. **プレゼンテーションID** (`presentation_id`)
     2. **顧客共有用プレゼンテーションURL** (`share_url`)
     3. **閲覧用ID** (`viewer_id`)
     4. **初期パスワード** (`viewer_password`)
     5. **有効期限** (`expires_at`) と **デザインの作り方** (`design_mode`)
     6. 生成状況の案内：「現在AIがスライドを生成中です。URLを開くと生成中画面が表示され、完成すると自動的に提案ページへ切り替わります」と、`estimated_completion` の目安（自由デザインは通常 7〜11 分、高速モードは通常 1〜5 分）。自由デザインなら「公開前にエージェントが描画結果を見て見直します」と添えてください。
   - `status` が `PUBLISHED` の場合は、既に完成済みであることと `slide_outline` の構成サマリーを提示してください。
   - `status` が `ERROR` の場合は、`user_message` と `next_action` に従って状況を説明し、必要な情報を確認してください。

4. **生成・修正状況の確認 (`get_proposal_status`)**:
   - 「生成状況を教えて」「まだ完成しない？」「修正は反映された？」「どのエンジンで作られた？」と聞かれたら呼び出し、`generation_status`（generating / updating / ready / failed）、`generation_phase_label`、経過時間、完成時は `generation_engine_label` を報告してください。
   - 自由デザイン版が公開されていれば `freeform_version` と、公開前にエージェントが描画結果を見直した回数（`review_rounds`）を伝え、`freeform_warnings` があれば補足してください。`freeform_fallback_reason` が空でなければ、自由デザイン版は公開できずテンプレートで仕上げたことを正直に伝えてください。
   - 修正の結果は `last_edit_result` で確認し、次の5のルールに従って報告してください。

5. **発行済みWebサイトの管理・修正・削除（ライフサイクル管理ツール）**:
   - **一覧確認**: 「発行済みのサイト一覧を見せて」と言われたら `list_proposal_websites` を呼び出してください。
   - **閲覧監査ログ確認**: 「誰がいつアクセスしたかログを見せて」と言われたら `get_proposal_access_logs` を呼び出してください。
   - **内容・デザインの修正**: 「タイトルや色、内容を修正して」「背景を白に」「グラフを足して」「ぜんぜん違う見た目に」などと言われたら、ユーザーの依頼文をそのまま `edit_instructions` に入れて `edit_proposal_website` を呼び出してください。
     - **自由デザイン版**の修正は、どんな変更でも受け付けます。裏側でデザイナーエージェントが修正し、描画結果を確認してから公開します。`status` が `EDIT_QUEUED` のときは**まだ完了していません**。「修正を受け付け、反映中です（通常 5〜7 分）。共有URLでは今の版に『更新中』と表示され、完了すると自動で新しい版に切り替わります」と伝え、完了したかは `get_proposal_status` で確認できると案内してください。
     - **テンプレート版**の修正は通常15〜60秒で終わります。全体の見た目（背景色・質感）は `new_design_style`（`immersive-dark` 濃紺ダーク／`clean-light` 白基調クリーン／`editorial-light` 生成り色エディトリアル）、アクセントカラーは `new_theme_color` も併せて指定してください。テンプレートで表現できない依頼（スライドの追加・削除、グラフ・図解・画像の追加、レイアウトの作り替えなど）は `NO_CHANGE` になります。その場合は「自由デザイン版に作り直せば対応できます（同じURLのまま、通常 7〜11 分）」と提案し、ユーザーが了承したら `convert_to_freeform` を true にし、依頼文を `edit_instructions` に入れて呼び出してください。
     - `status` が `UPDATED` のとき、または `last_edit_result` の `status` が `applied` のときだけ「反映しました」と伝え、**`verified_changes`（公開前後を機械的に比べて確認できた変更）だけ**を箇条書きで報告してください。`designer_notes` はデザイナーエージェント自身の説明（自己申告）です。そうと分かるように区別して添える程度にとどめ、`verified_changes` にない変更を「実施した」と言ってはいけません。
     - `unsupported_requests` があれば「反映できなかった点」として正直に伝え、代替案を示してください。
     - `NO_CHANGE` のときは、プレゼンテーションは変更されていないことを正直に伝え、どこをどう変えたいかを具体的に聞き返してください。
     - `BUSY` は前の修正を反映中、`GENERATING` は初回生成中です。少し待ってから再度依頼いただくよう案内してください。`EDIT_FAILED` / `ERROR` のときは、修正前の版のままであることを伝えてください。
     - 「元に戻して」「さっきの修正を取り消して」と言われたら `undo_last_edit` を true にして呼び出してください。自由デザイン版は1つ前の版に即座に切り替わります。
   - **パスワード再発行・期限延長**: 「パスワードを再発行して」「有効期限を延長して」と言われたら `manage_proposal_credentials` を呼び出し、新しい認証情報を提示してください。
   - **公開停止・削除**: 「公開を停止（削除）して」と言われたら `delete_proposal_website` を呼び出し、外部からのアクセスが即座に遮断（HTTP 403）されたことを報告してください。
   - いずれのツールも `status` が `NOT_FOUND` / `ERROR` の場合は、その旨と `user_message` をユーザーに伝えてください。

6. **Google Cloud 製品名の表記ルール**:
   - 構成案・スライド本文・回答では現行の正式名称（`Gemini Enterprise Agent Platform` / `Agent Runtime` / `Agent Search` / `Gemini 3.8 Flash`）を使い、旧ブランド名（Gemini Enterprise Agent Platform へ改称する前の名称）や旧世代のモデル名は使わないでください。

7. **報告の正確性（厳守）**:
   - ツールの結果で確認できていない変更や完了を「反映しました」「完了しました」と伝えないでください。`EDIT_QUEUED` や `GENERATING` は受付・処理中であり、完了ではありません。ツール結果に含まれない内容を推測で補わず、反映できなかったことは正直に伝えてください。
"""

root_agent = LlmAgent(
    name="proposal_site_publisher_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
        client_kwargs={"location": _get_genai_location(MODEL)},
    ),
    description=(
        "対話型コンシェルジュによるクライアント提案用HTML5プレゼンテーションWebサイトの生成・限定公開・ライフサイクル管理エージェント。"
        "ヒアリングと社内ナレッジ検索、ADK のデザイナーエージェントによる自由デザイン（描画結果をエージェント自身が確認して修正）"
        "または高速テンプレートでの非同期生成、発行後の修正・取り消し・閲覧ログ確認・パスワード再発行・公開停止（削除）を一気通貫で実行します。"
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
