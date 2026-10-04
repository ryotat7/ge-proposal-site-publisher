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

"""Free-form deck contract: validation, sanitisation, runtime injection and diffing (stdlib only).

The same module runs in the generation worker (publish gate) and in the deck preview service (what the
designing agent sees), so the preview always renders exactly the bytes that will be published.

Agent output (staging `deck/` folder):
  index.html            <main id="pd-deck"> + <section class="pd-slide"> x 3..20 on a 1920x1080 canvas
  assets/**.{svg,png,jpg,jpeg,webp,gif}
  charts/*.json         ECharts option objects (pure JSON)
  manifest.json         slide list, data sources, design concept, review log
Publishable output: index.html (sanitised + runtime injected), source.html (raw), assets/**, charts/**,
manifest.json.
"""

from __future__ import annotations

import dataclasses
import hashlib
import html
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

RUNTIME_VERSION = "v1"
RUNTIME_BASE = f"/_rt/{RUNTIME_VERSION}/"
RUNTIME_FILES = ("deck-runtime.css", "deck-runtime.js", "echarts.min.js")

LIMITS = {
    "index_bytes": 600_000,
    "max_files": 60,
    "total_bytes": 15_000_000,
    "file_bytes": 5_000_000,
    "chart_bytes": 200_000,
    "svg_bytes": 1_000_000,
    "manifest_bytes": 100_000,
    "data_uri_bytes": 150_000,
    "min_slides": 3,
    "max_slides": 20,
}

ASSET_MIME = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
ASSET_PATH_RE = re.compile(
    r"^assets/(?:[A-Za-z0-9][A-Za-z0-9._-]{0,80}/){0,3}[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(svg|png|jpe?g|webp|gif)$"
)
CHART_PATH_RE = re.compile(r"^charts/[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.json$")
AI_IMAGE_PREFIX = "assets/ai/"
GOOGLE_FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")
DATA_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=\s]+$", re.IGNORECASE)

_DROP_WITH_CONTENT = {
    "script", "iframe", "object", "embed", "noscript", "template", "frame", "frameset", "applet",
    "portal", "video", "audio", "foreignobject", "textarea", "select", "math",
}
_DROP_TAG_ONLY = {"input", "source", "track", "base", "param", "option", "dialog"}
_RENAME = {"form": "div"}
_VOID = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_URL_ATTRS = {"href", "src", "xlink:href", "poster", "background", "data", "cite", "longdesc", "usemap"}
_BLOCKED_ATTRS = {"srcdoc", "formaction", "action", "ping", "http-equiv", "nonce", "integrity", "srcset", "imagesrcset"}
_RESERVED_IDS = {"pd-stage", "pd-nav", "pd-progress", "pd-counter", "pd-live-update", "pd-update-banner"}
_SVG_ANIMATION_TAGS = {"animate", "set", "animatemotion", "animatetransform"}


def _is_unsafe_svg_animation(attr_map: dict[str, str]) -> bool:
    """True when an SVG animation element could rewrite a link target (javascript: URL XSS vector)."""
    target = (attr_map.get("attributename") or "").strip().lower()
    if target in ("href", "xlink:href"):
        return True
    for key in ("to", "from", "values", "by"):
        value = re.sub(r"[\x00-\x20]", "", attr_map.get(key) or "").lower()
        if "javascript:" in value or "vbscript:" in value:
            return True
    return False


@dataclasses.dataclass
class Issue:
    severity: str  # "error" (blocks publishing) | "warning"
    code: str
    message: str
    file: str = ""

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class SlideInfo:
    index: int
    title: str
    text: str

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


@dataclasses.dataclass
class BuildResult:
    files: dict[str, bytes]
    issues: list[Issue]
    slides: list[SlideInfo]
    manifest: dict[str, Any]

    @property
    def slide_count(self) -> int:
        return len(self.slides)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity != "error"]

    def summary(self) -> dict[str, Any]:
        return {
            "slide_count": self.slide_count,
            "slide_titles": [s.title for s in self.slides],
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings][:40],
            "files": sorted(self.files),
        }


def normalize_path(raw: str) -> str | None:
    """Returns a clean deck-relative path (assets/... or charts/...) or None when not local/allowed."""
    value = (raw or "").strip().strip("'\"")
    if not value or value.startswith(("#", "data:", "/", "\\")) or "//" in value[:8]:
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    path = posixpath.normpath(parts.path.replace("\\", "/"))
    if path.startswith("./"):
        path = path[2:]
    if path.startswith("..") or "/../" in path:
        return None
    return path


def is_google_fonts_url(url: str) -> bool:
    try:
        parts = urlsplit((url or "").strip().strip("'\""))
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname in GOOGLE_FONT_HOSTS


# ---------------------------------------------------------------------------
# CSS sanitising
# ---------------------------------------------------------------------------
_CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?(['\"]?)([^'\")\s;]+)\1\s*\)?[^;]*;?", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
_CSS_DANGEROUS_RE = re.compile(r"(expression\s*\(|-moz-binding|behavior\s*:|javascript:)", re.IGNORECASE)


def sanitize_css(css: str, issues: list[Issue], refs: set[str], where: str, svg_scope: bool = False) -> str:
    def _import(m: re.Match[str]) -> str:
        url = m.group(2)
        if not svg_scope and is_google_fonts_url(url) and urlsplit(url).hostname == "fonts.googleapis.com":
            return f'@import url("{url}");'
        issues.append(Issue("warning", "css_import_removed", f"@import を削除しました: {url[:80]}", where))
        return ""

    def _url(m: re.Match[str]) -> str:
        target = (m.group(2) or "").strip()
        if target.startswith("#"):
            return f'url("{target}")'
        if DATA_IMAGE_RE.match(target) and len(target) <= LIMITS["data_uri_bytes"]:
            return f'url("{target}")'
        if not svg_scope and is_google_fonts_url(target):
            return f'url("{target}")'
        local = normalize_path(target)
        if not svg_scope and local and ASSET_PATH_RE.match(local):
            if local.startswith(AI_IMAGE_PREFIX):
                issues.append(
                    Issue(
                        "warning",
                        "ai_image_in_css",
                        f"AI 生成画像は <img data-pd-ai-image> で配置してください（CSS 背景では「AI生成イメージ」表示ができないため削除）: {local}",
                        where,
                    )
                )
                return 'url("data:,")'
            refs.add(local)
            return f'url("{local}")'
        issues.append(Issue("warning", "css_url_removed", f"許可されていない URL を削除しました: {target[:80]}", where))
        return 'url("data:,")'

    out = _CSS_IMPORT_RE.sub(_import, css or "")
    out = _CSS_URL_RE.sub(_url, out)
    if _CSS_DANGEROUS_RE.search(out):
        issues.append(Issue("warning", "css_dangerous_removed", "危険な CSS 構文を削除しました", where))
        out = _CSS_DANGEROUS_RE.sub("", out)
    return out.replace("</", "<\\/")


# ---------------------------------------------------------------------------
# HTML sanitising (+ structure extraction)
# ---------------------------------------------------------------------------
class _DeckSanitizer(HTMLParser):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__(convert_charrefs=True)
        self.issues = issues
        self.out: list[str] = []
        self.refs: set[str] = set()
        self.chart_refs: set[str] = set()
        self.ai_images: set[str] = set()
        self.drop_depth = 0
        self.drop_tag = ""
        self.stack: list[str] = []
        self.in_style = False
        self.deck_depth: int | None = None
        self.deck_count = 0
        self.slides: list[SlideInfo] = []
        self.slide_depth: int | None = None
        self.slide_text: list[str] = []
        self.slide_title = ""
        self.heading_capture: list[str] | None = None
        self.heading_depth: int | None = None
        self.saw_head = False
        self.saw_body = False
        self.removed: dict[str, int] = {}

    # -- helpers --
    def _note_removed(self, what: str) -> None:
        self.removed[what] = self.removed.get(what, 0) + 1

    def _clean_url(self, tag: str, name: str, value: str) -> str | None:
        v = (value or "").strip()
        lowered = re.sub(r"[\x00-\x20]", "", v).lower()
        if lowered.startswith(("javascript:", "vbscript:")):
            self._note_removed("javascript_url")
            return None
        if name in ("href", "xlink:href") and v.startswith("#"):
            return v
        if tag == "a" and name == "href":
            if v.startswith("https://") or v.startswith("mailto:"):
                return v
            self._note_removed("external_link")
            return None
        if tag == "link" and name == "href":
            return v if is_google_fonts_url(v) else None
        if DATA_IMAGE_RE.match(v) and len(v) <= LIMITS["data_uri_bytes"] and name in ("src", "href", "xlink:href"):
            return v
        local = normalize_path(v)
        if local and ASSET_PATH_RE.match(local) and name in ("src", "href", "xlink:href"):
            self.refs.add(local)
            if local.startswith(AI_IMAGE_PREFIX):
                self.ai_images.add(local)
            return local
        self._note_removed("disallowed_url")
        return None

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]] | None:
        clean: list[tuple[str, str]] = []
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if tag == "link":
            rel = attr_map.get("rel", "").lower()
            if rel not in ("stylesheet", "preconnect") or not is_google_fonts_url(attr_map.get("href", "")):
                self._note_removed("link")
                return None
        if tag == "meta":
            if "http-equiv" in attr_map:
                self._note_removed("meta_http_equiv")
                return None
        if tag in _SVG_ANIMATION_TAGS and _is_unsafe_svg_animation(attr_map):
            self._note_removed("svg_animation_href")
            return None
        for key, raw in attrs:
            name = key.lower()
            value = raw or ""
            if name.startswith("on") or name in _BLOCKED_ATTRS:
                self._note_removed("event_handler" if name.startswith("on") else name)
                continue
            if name == "style":
                value = sanitize_css(value, self.issues, self.refs, "index.html").replace("<\\/", "</")
            elif name in _URL_ATTRS:
                cleaned = self._clean_url(tag, name, value)
                if cleaned is None:
                    if tag == "img" and name == "src":
                        return None
                    continue
                value = cleaned
            elif name == "data-chart":
                local = normalize_path(value)
                if not local or not CHART_PATH_RE.match(local):
                    self.issues.append(
                        Issue("error", "chart_path_invalid", f"data-chart は charts/<name>.json 形式にしてください: {value[:60]}", "index.html")
                    )
                    continue
                self.chart_refs.add(local)
                value = local
            elif name == "id" and value in _RESERVED_IDS:
                self._note_removed("reserved_id")
                continue
            elif name == "target":
                continue
            clean.append((name, value))
        if tag == "a" and any(k == "href" and v.startswith("https://") for k, v in clean):
            clean.append(("target", "_blank"))
            clean.append(("rel", "noopener noreferrer"))
        if tag == "img":
            src = dict(clean).get("src", "")
            if src.startswith(AI_IMAGE_PREFIX) and "data-pd-ai-image" not in dict(clean):
                clean.append(("data-pd-ai-image", ""))
            if "alt" not in dict(clean):
                clean.append(("alt", ""))
        if tag == "button" and not any(k == "type" for k, _ in clean):
            clean.append(("type", "button"))
        return clean

    @staticmethod
    def _render_start(tag: str, attrs: list[tuple[str, str]], self_closing: bool = False) -> str:
        parts = [tag] + [f'{k}="{html.escape(v, quote=True)}"' for k, v in attrs]
        end = " />" if self_closing else ">"
        return "<" + " ".join(parts) + end

    # -- parser events --
    def handle_decl(self, decl: str) -> None:  # doctype
        return

    def handle_pi(self, data: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    def handle_comment(self, data: str) -> None:
        return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs, self_closing=True)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool) -> None:
        if self.drop_depth:
            if tag == self.drop_tag and not self_closing and tag not in _VOID:
                self.drop_depth += 1
            return
        if tag in _DROP_WITH_CONTENT:
            self._note_removed(tag)
            if not self_closing and tag not in _VOID:
                self.drop_depth = 1
                self.drop_tag = tag
            return
        if tag in _DROP_TAG_ONLY:
            self._note_removed(tag)
            return
        tag = _RENAME.get(tag, tag)
        if tag == "head":
            self.saw_head = True
        if tag == "body":
            self.saw_body = True
        cleaned = self._attrs(tag, attrs)
        if cleaned is None:
            return
        amap = dict(cleaned)
        depth = len(self.stack)
        if tag == "main" and amap.get("id") == "pd-deck":
            self.deck_count += 1
            if self.deck_depth is None:
                self.deck_depth = depth
        is_slide = (
            tag == "section"
            and self.deck_depth is not None
            and depth == self.deck_depth + 1
            and "pd-slide" in amap.get("class", "").split()
        )
        if is_slide:
            self.slide_depth = depth
            self.slide_text = []
            self.slide_title = amap.get("data-pd-title", "").strip()
        if self.slide_depth is not None and tag in ("h1", "h2") and self.heading_capture is None and not self.slide_title:
            self.heading_capture = []
            self.heading_depth = depth
        self.out.append(self._render_start(tag, cleaned, self_closing and tag in _VOID))
        if tag == "style":
            self.in_style = True
        if not self_closing and tag not in _VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth -= 1
            return
        if tag in _DROP_WITH_CONTENT or tag in _DROP_TAG_ONLY or tag in _VOID:
            return
        tag = _RENAME.get(tag, tag)
        if tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            depth = len(self.stack)
            if self.heading_depth is not None and depth == self.heading_depth:
                if self.heading_capture is not None and not self.slide_title:
                    self.slide_title = re.sub(r"\s+", " ", "".join(self.heading_capture)).strip()[:80]
                self.heading_capture = None
                self.heading_depth = None
            if self.slide_depth is not None and depth == self.slide_depth:
                text = re.sub(r"\s+", " ", " ".join(self.slide_text)).strip()
                index = len(self.slides)
                self.slides.append(SlideInfo(index=index, title=self.slide_title or f"スライド {index + 1}", text=text))
                self.slide_depth = None
            if self.deck_depth is not None and depth == self.deck_depth and top == "main":
                self.deck_depth = None
            if top == "style":
                self.in_style = False
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            return
        if self.in_style:
            self.out.append(sanitize_css(data, self.issues, self.refs, "index.html"))
            return
        if self.slide_depth is not None:
            self.slide_text.append(data)
        if self.heading_capture is not None:
            self.heading_capture.append(data)
        self.out.append(html.escape(data, quote=False))

    def close(self) -> None:
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")


def _inject_runtime(body_html: str, needs_charts: bool, title: str) -> str:
    """Wraps sanitised markup into a full document with the shared runtime injected."""
    css_link = f'<link rel="stylesheet" href="{RUNTIME_BASE}deck-runtime.css" data-pd-runtime>'
    scripts = ""
    if needs_charts:
        scripts += f'<script src="{RUNTIME_BASE}echarts.min.js" defer data-pd-runtime></script>'
    scripts += f'<script src="{RUNTIME_BASE}deck-runtime.js" defer data-pd-runtime></script>'
    text = body_html
    lower = text.lower()
    head_open = re.search(r"<head(\s[^>]*)?>", text, flags=re.IGNORECASE)
    if not head_open:
        if "<html" in lower:
            text = re.sub(r"(<html[^>]*>)", r"\1<head></head>", text, count=1, flags=re.IGNORECASE)
        else:
            text = f"<html lang=\"ja\"><head></head><body>{text}</body></html>"
        head_open = re.search(r"<head(\s[^>]*)?>", text, flags=re.IGNORECASE)
    assert head_open is not None
    meta = '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">'
    if not re.search(r"<title>", text, flags=re.IGNORECASE):
        meta += f"<title>{html.escape(title or 'Proposal', quote=False)}</title>"
    insert_at = head_open.end()
    text = text[:insert_at] + meta + css_link + text[insert_at:]
    body_close = text.lower().rfind("</body>")
    if body_close < 0:
        html_close = text.lower().rfind("</html>")
        if html_close < 0:
            text += scripts
        else:
            text = text[:html_close] + scripts + text[html_close:]
    else:
        text = text[:body_close] + scripts + text[body_close:]
    # Drop duplicated charset metas the author may have written.
    first_charset = text.lower().find("<meta charset")
    text = text[: first_charset + 1] + re.sub(r"<meta charset=\"[^\"]*\"\s*/?>", "", text[first_charset + 1 :], flags=re.IGNORECASE)
    return "<!DOCTYPE html>\n" + text


def sanitize_html(raw_html: str) -> tuple[str, list[Issue], list[SlideInfo], set[str], set[str], set[str], int]:
    issues: list[Issue] = []
    parser = _DeckSanitizer(issues)
    parser.feed(raw_html)
    parser.close()
    for what, count in sorted(parser.removed.items()):
        issues.append(Issue("warning", f"removed_{what}", f"安全のため {what} を {count} 件削除しました", "index.html"))
    return (
        "".join(parser.out),
        issues,
        parser.slides,
        parser.refs,
        parser.chart_refs,
        parser.ai_images,
        parser.deck_count,
    )


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", _SVG_NS)
ET.register_namespace("xlink", _XLINK_NS)
_SVG_DROP = {"script", "foreignobject", "iframe", "embed", "object", "audio", "video", "handler", "listener"}


def sanitize_svg(data: bytes, path: str, issues: list[Issue]) -> bytes | None:
    if len(data) > LIMITS["svg_bytes"]:
        issues.append(Issue("error", "svg_too_large", f"SVG が大きすぎます（{len(data)} bytes）", path))
        return None
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        issues.append(Issue("error", "svg_doctype", "SVG に DOCTYPE / ENTITY は使えません", path))
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        issues.append(Issue("error", "svg_parse", f"SVG を解析できません: {exc}", path))
        return None
    removed = 0

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""

    def walk(node: ET.Element) -> None:
        nonlocal removed
        for child in list(node):
            child_tag = local(child.tag)
            if child_tag in _SVG_DROP or (
                child_tag in _SVG_ANIMATION_TAGS
                and _is_unsafe_svg_animation({local(k) if k.startswith("{") else k.lower(): v for k, v in child.attrib.items()})
            ):
                node.remove(child)
                removed += 1
                continue
            walk(child)
        for attr in list(node.attrib):
            name = local(attr) if attr.startswith("{") else attr.lower()
            value = node.attrib[attr]
            if name.startswith("on"):
                del node.attrib[attr]
                removed += 1
            elif name == "href":
                v = value.strip()
                if not (v.startswith("#") or (DATA_IMAGE_RE.match(v) and len(v) <= LIMITS["data_uri_bytes"])):
                    del node.attrib[attr]
                    removed += 1
            elif name == "style":
                node.attrib[attr] = sanitize_css(value, issues, set(), path, svg_scope=True).replace("<\\/", "</")
        if local(node.tag) == "style" and node.text:
            node.text = sanitize_css(node.text, issues, set(), path, svg_scope=True).replace("<\\/", "</")

    if local(root.tag) != "svg":
        issues.append(Issue("error", "svg_root", "SVG のルート要素が svg ではありません", path))
        return None
    walk(root)
    if removed:
        issues.append(Issue("warning", "svg_sanitized", f"SVG から安全でない要素・属性を {removed} 件削除しました", path))
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def sniff_raster(data: bytes) -> str | None:
    """Returns the real raster type (png/jpeg/webp/gif) from magic bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return None


def check_raster(data: bytes, path: str, ext: str, issues: list[Issue]) -> bool:
    real = sniff_raster(data)
    if real is None:
        issues.append(Issue("error", "image_format", f"画像として読み込めないファイルです（.{ext}）", path))
        return False
    if real != ("jpeg" if ext in ("jpg", "jpeg") else ext):
        issues.append(Issue("warning", "image_ext_mismatch", f"拡張子 .{ext} ですが中身は {real} です（配信時は実際の形式で返します）", path))
    return True


def content_type_for_bytes(path: str, data: bytes) -> str:
    """Content-Type for a deck file; raster images use the sniffed type, not the extension."""
    real = sniff_raster(data) if ASSET_PATH_RE.match(path) and not path.endswith(".svg") else None
    return ASSET_MIME[real] if real else content_type_for(path)


def _scrub_json(value: Any, depth: int = 0) -> Any:
    if depth > 40:
        return None
    if isinstance(value, str):
        return value.replace("<", "").replace(">", "")
    if isinstance(value, list):
        return [_scrub_json(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): _scrub_json(v, depth + 1) for k, v in value.items() if str(k) not in ("__proto__", "constructor", "prototype")}
    return value


def sanitize_chart(data: bytes, path: str, issues: list[Issue]) -> bytes | None:
    if len(data) > LIMITS["chart_bytes"]:
        issues.append(Issue("error", "chart_too_large", f"グラフ JSON が大きすぎます（{len(data)} bytes）", path))
        return None
    try:
        option = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        issues.append(Issue("error", "chart_json", f"グラフ JSON を解析できません: {exc}", path))
        return None
    if not isinstance(option, dict):
        issues.append(Issue("error", "chart_json", "グラフ JSON はオブジェクトである必要があります", path))
        return None
    if not option.get("series"):
        issues.append(Issue("warning", "chart_no_series", "series がありません", path))
    return json.dumps(_scrub_json(option), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_publishable(files: dict[str, bytes], title: str = "") -> BuildResult:
    """Validates + sanitises an agent deck folder. Never raises; problems are returned as issues."""
    issues: list[Issue] = []
    out: dict[str, bytes] = {}
    if len(files) > LIMITS["max_files"] * 3:
        issues.append(Issue("error", "too_many_files", f"ファイル数が多すぎます（{len(files)}）"))
    raw = files.get("index.html")
    if raw is None:
        issues.append(Issue("error", "missing_index", "deck/index.html がありません", "index.html"))
        return BuildResult(files={}, issues=issues, slides=[], manifest={})
    if len(raw) > LIMITS["index_bytes"]:
        issues.append(Issue("error", "index_too_large", f"index.html が大きすぎます（{len(raw)} bytes）", "index.html"))
    try:
        raw_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raw_text = raw.decode("utf-8", errors="replace")
        issues.append(Issue("warning", "index_encoding", "index.html は UTF-8 で保存してください", "index.html"))

    body, html_issues, slides, refs, chart_refs, ai_images, deck_count = sanitize_html(raw_text)
    issues.extend(html_issues)
    if deck_count != 1:
        issues.append(Issue("error", "deck_root", f'<main id="pd-deck"> はちょうど 1 つ必要です（{deck_count} 個）', "index.html"))
    if not (LIMITS["min_slides"] <= len(slides) <= LIMITS["max_slides"]):
        issues.append(
            Issue(
                "error",
                "slide_count",
                f"スライド数は {LIMITS['min_slides']}〜{LIMITS['max_slides']} 枚にしてください（現在 {len(slides)} 枚）",
                "index.html",
            )
        )

    total = 0
    published_assets = 0
    for path in sorted(files):
        data = files[path]
        if path in ("index.html", "manifest.json"):
            continue
        asset = ASSET_PATH_RE.match(path)
        chart = CHART_PATH_RE.match(path)
        if not asset and not chart:
            continue  # scratch files (scripts, notes) are never published
        if len(data) > LIMITS["file_bytes"]:
            issues.append(Issue("error", "file_too_large", f"ファイルが大きすぎます（{len(data)} bytes）", path))
            continue
        if asset:
            ext = asset.group(1).lower()
            if ext == "svg":
                clean = sanitize_svg(data, path, issues)
            else:
                clean = data if check_raster(data, path, ext, issues) else None
        else:
            clean = sanitize_chart(data, path, issues)
        if clean is None:
            continue
        published_assets += 1
        total += len(clean)
        out[path] = clean
    if published_assets > LIMITS["max_files"]:
        issues.append(Issue("error", "too_many_files", f"公開ファイル数が上限（{LIMITS['max_files']}）を超えています"))
    if total > LIMITS["total_bytes"]:
        issues.append(Issue("error", "deck_too_large", f"素材の合計サイズが上限を超えています（{total} bytes）"))

    for ref in sorted(refs):
        if ref not in out:
            issues.append(Issue("error", "missing_asset", f"参照先のファイルがありません: {ref}", "index.html"))
    for ref in sorted(chart_refs):
        if ref not in out:
            issues.append(Issue("error", "missing_chart", f"グラフ JSON がありません: {ref}", "index.html"))
    unused = [p for p in out if p not in refs and p not in chart_refs]
    if unused:
        issues.append(Issue("warning", "unused_files", "参照されていないファイル: " + ", ".join(sorted(unused)[:10])))

    manifest: dict[str, Any] = {}
    mraw = files.get("manifest.json")
    if mraw is not None:
        try:
            if len(mraw) > LIMITS["manifest_bytes"]:
                raise ValueError("manifest.json too large")
            parsed = json.loads(mraw.decode("utf-8"))
            if isinstance(parsed, dict):
                manifest = _scrub_json(parsed)
            else:
                raise ValueError("manifest.json must be an object")
        except (ValueError, UnicodeDecodeError) as exc:
            issues.append(Issue("warning", "manifest_invalid", f"manifest.json を読めません: {exc}", "manifest.json"))
    else:
        issues.append(Issue("warning", "manifest_missing", "manifest.json がありません", "manifest.json"))
    manifest.setdefault("slides", [{"index": s.index + 1, "title": s.title} for s in slides])
    manifest["slide_count"] = len(slides)
    manifest["ai_images"] = sorted(ai_images)
    out["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")

    deck_title = title or (slides[0].title if slides else "")
    out["index.html"] = _inject_runtime(body, bool(chart_refs), deck_title).encode("utf-8")
    out["source.html"] = raw
    return BuildResult(files=out, issues=issues, slides=slides, manifest=manifest)


# ---------------------------------------------------------------------------
# Verified change list between two published versions (machine diff)
# ---------------------------------------------------------------------------
def extract_slides(raw_html: str) -> list[SlideInfo]:
    _body, _issues, slides, *_rest = sanitize_html(raw_html)
    return slides


def describe_changes(old_files: dict[str, bytes], new_files: dict[str, bytes]) -> list[str]:
    """Deterministic, verifiable change list (Japanese) computed from the files themselves."""
    changes: list[str] = []
    old_html = (old_files.get("source.html") or old_files.get("index.html") or b"").decode("utf-8", "replace")
    new_html = (new_files.get("source.html") or new_files.get("index.html") or b"").decode("utf-8", "replace")
    old_slides = extract_slides(old_html) if old_html else []
    new_slides = extract_slides(new_html) if new_html else []
    if len(old_slides) != len(new_slides):
        changes.append(f"スライド枚数: {len(old_slides)} 枚 → {len(new_slides)} 枚")
    for idx in range(max(len(old_slides), len(new_slides))):
        o = old_slides[idx] if idx < len(old_slides) else None
        n = new_slides[idx] if idx < len(new_slides) else None
        if o and n:
            if o.title != n.title:
                changes.append(f"{idx + 1} 枚目のタイトル: 「{o.title}」→「{n.title}」")
            elif o.text_hash != n.text_hash:
                changes.append(f"{idx + 1} 枚目「{n.title}」の本文を変更")
        elif n and not o:
            changes.append(f"{idx + 1} 枚目「{n.title}」を追加")
        elif o and not n:
            changes.append(f"{idx + 1} 枚目「{o.title}」を削除")
    old_css = "".join(re.findall(r"<style[^>]*>(.*?)</style>", old_html, flags=re.DOTALL | re.IGNORECASE))
    new_css = "".join(re.findall(r"<style[^>]*>(.*?)</style>", new_html, flags=re.DOTALL | re.IGNORECASE))
    if old_css != new_css:
        changes.append("デザイン（スタイルシート）を変更")
    elif old_html != new_html and not any("枚目" in c for c in changes):
        changes.append("HTML の構造・属性を変更")

    def digest(files: dict[str, bytes]) -> dict[str, str]:
        return {
            p: hashlib.sha256(b).hexdigest()
            for p, b in files.items()
            if ASSET_PATH_RE.match(p) or CHART_PATH_RE.match(p)
        }

    od, nd = digest(old_files), digest(new_files)
    added = sorted(set(nd) - set(od))
    removed = sorted(set(od) - set(nd))
    modified = sorted(p for p in set(od) & set(nd) if od[p] != nd[p])
    if added:
        changes.append("素材を追加: " + ", ".join(added[:8]))
    if removed:
        changes.append("素材を削除: " + ", ".join(removed[:8]))
    if modified:
        changes.append("素材を更新: " + ", ".join(modified[:8]))
    return changes


def content_type_for(path: str) -> str:
    if path.endswith(".json"):
        return "application/json; charset=utf-8"
    if path.endswith(".html"):
        return "text/html; charset=utf-8"
    ext = path.rsplit(".", 1)[-1].lower()
    return ASSET_MIME.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# Content-Security-Policy shared by the hosting gateway and the preview renderer
# ---------------------------------------------------------------------------
def freeform_csp(nonce: str = "") -> str:
    """CSP for free-form deck pages. Only our runtime (same origin) and nonce'd gateway scripts may run."""
    script_src = "script-src 'self'" + (f" 'nonce-{nonce}'" if nonce else "")
    return "; ".join(
        [
            "default-src 'none'",
            script_src,
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src https://fonts.gstatic.com data:",
            "img-src 'self' data:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'self'",
        ]
    )


# Standalone SVG assets opened directly must never run script.
SVG_ASSET_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"


def is_servable_asset_path(path: str) -> bool:
    """Gateway/renderer allowlist for deck sub-resources (assets/** and charts/*.json only)."""
    clean = normalize_path(path)
    return bool(clean and clean == path and (ASSET_PATH_RE.match(clean) or CHART_PATH_RE.match(clean)))
