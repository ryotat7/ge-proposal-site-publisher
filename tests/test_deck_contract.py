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

"""Unit tests for the free-form deck contract (sanitiser, validator, runtime injection, machine diff)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "proposal_agent"))

from app import deck_contract as dc  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#0af"/></svg>'


def _deck_html(slides: str, head: str = "") -> str:
    return (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>Acme 提案</title>'
        f"{head}</head><body><main id=\"pd-deck\">{slides}</main></body></html>"
    )


def _three_slides(extra: str = "") -> str:
    return (
        '<section class="pd-slide" data-pd-title="表紙"><h1>Acme Corp 様 ご提案</h1></section>'
        '<section class="pd-slide"><h2>課題</h2><p>現状の課題</p>'
        '<div class="pd-chart" data-chart="charts/sales.json" data-pd-estimate></div></section>'
        '<section class="pd-slide"><h2>構成</h2><img src="assets/arch.svg" alt="構成図">'
        f'<img src="assets/ai/hero.png">{extra}</section>'
    )


def _valid_files(extra_slide_markup: str = "") -> dict[str, bytes]:
    chart = {"xAxis": {"type": "category", "data": ["A", "B"]}, "yAxis": {}, "series": [{"type": "bar", "data": [1, 2]}]}
    return {
        "index.html": _deck_html(_three_slides(extra_slide_markup)).encode("utf-8"),
        "charts/sales.json": json.dumps(chart).encode("utf-8"),
        "assets/arch.svg": SVG,
        "assets/ai/hero.png": PNG,
        "manifest.json": json.dumps({"concept": "clean", "slides": [{"index": 1, "title": "表紙"}]}).encode("utf-8"),
        "generate_assets.py": b"print('scratch file, never published')",
    }


def test_valid_deck_builds_and_injects_runtime() -> None:
    files = _valid_files()
    result = dc.build_publishable(files, title="Acme 提案")
    assert result.errors == [], result.summary()
    assert result.slide_count == 3
    assert [s.title for s in result.slides] == ["表紙", "課題", "構成"]
    index = result.files["index.html"].decode("utf-8")
    assert index.startswith("<!DOCTYPE html>")
    assert f'href="{dc.RUNTIME_BASE}deck-runtime.css"' in index
    assert f'src="{dc.RUNTIME_BASE}echarts.min.js"' in index  # a chart exists
    assert f'src="{dc.RUNTIME_BASE}deck-runtime.js"' in index
    assert index.index("deck-runtime.css") < index.index("<title>")  # runtime CSS first, author CSS wins
    assert index.lower().count("<meta charset") == 1
    # AI images are tagged so the runtime can show the 「AI生成イメージ」 badge; alt is always present.
    assert 'src="assets/ai/hero.png" data-pd-ai-image="" alt=""' in index
    assert result.files["source.html"] == files["index.html"]
    assert "generate_assets.py" not in result.files
    assert set(result.files) >= {"charts/sales.json", "assets/arch.svg", "assets/ai/hero.png", "manifest.json"}
    manifest = json.loads(result.files["manifest.json"])
    assert manifest["slide_count"] == 3
    assert manifest["ai_images"] == ["assets/ai/hero.png"]


def test_echarts_is_only_injected_when_charts_are_used() -> None:
    html_text = _deck_html("".join(f'<section class="pd-slide"><h2>S{i}</h2></section>' for i in range(3)))
    result = dc.build_publishable({"index.html": html_text.encode()})
    index = result.files["index.html"].decode()
    assert "echarts.min.js" not in index
    assert "deck-runtime.js" in index


def test_scripts_handlers_and_external_resources_are_removed() -> None:
    head = (
        '<script src="https://cdn.tailwindcss.com"></script>'
        '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/x.css">'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP">'
        '<meta http-equiv="refresh" content="0;url=https://example.com">'
        '<base href="https://example.com/">'
    )
    extra = (
        '<script>alert(1)</script><p onclick="alert(2)" id="pd-stage">x</p>'
        '<a href="javascript:alert(3)">bad</a><a href="https://example.com/ref">ref</a>'
        '<iframe src="https://example.com"></iframe><img src="https://example.com/x.png">'
        '<form action="https://example.com"><input name="q"><button>送信</button></form>'
        '<svg><a><animate attributeName="href" values="javascript:alert(4)"/><text>t</text></a></svg>'
    )
    files = _valid_files(extra)
    files["index.html"] = _deck_html(_three_slides(extra), head=head).encode("utf-8")
    result = dc.build_publishable(files)
    index = result.files["index.html"].decode("utf-8")
    lowered = index.lower()
    for needle in (
        "alert(",
        "onclick",
        "javascript:",
        "<iframe",
        "cdn.tailwindcss.com",
        "cdn.jsdelivr.net",
        "http-equiv",
        "<base",
        "example.com/x.png",
        "<form",
        "<input",
        "<animate",
        'id="pd-stage"',
    ):
        assert needle not in lowered, needle
    assert "fonts.googleapis.com/css2?family=Noto+Sans+JP" in index
    assert 'href="https://example.com/ref" target="_blank" rel="noopener noreferrer"' in index
    assert '<button type="button">' in index
    # Only our runtime scripts remain.
    assert lowered.count("<script") == 2
    codes = {i.code for i in result.warnings}
    assert {"removed_script", "removed_event_handler", "removed_iframe", "removed_svg_animation_href"} <= codes
    assert result.errors == [], result.summary()


def test_css_sanitiser_allowlists_urls_and_blocks_breakout() -> None:
    issues: list[dc.Issue] = []
    refs: set[str] = set()
    css = (
        '@import url("https://evil.example/x.css");'
        '@import url("https://fonts.googleapis.com/css2?family=Inter");'
        ".a{background:url(https://evil.example/bg.png)}"
        ".b{background:url('assets/bg.svg')}"
        ".c{background:url(assets/ai/hero.png)}"
        ".d{width:expression(alert(1))}"
        ".e{content:'</style><script>alert(1)</script>'}"
    )
    out = dc.sanitize_css(css, issues, refs, "index.html")
    assert "evil.example" not in out
    assert 'url("https://fonts.googleapis.com/css2?family=Inter")' in out
    assert 'url("assets/bg.svg")' in out
    assert "assets/ai/hero.png" not in out
    assert "expression(" not in out
    assert "</style" not in out
    assert refs == {"assets/bg.svg"}
    codes = {i.code for i in issues}
    assert {"css_import_removed", "css_url_removed", "ai_image_in_css", "css_dangerous_removed"} <= codes


def test_structure_errors_block_publishing() -> None:
    no_root = dc.build_publishable({"index.html": b"<html><body><section class='pd-slide'>x</section></body></html>"})
    assert {"deck_root", "slide_count"} <= {i.code for i in no_root.errors}

    two = _deck_html('<section class="pd-slide"><h2>A</h2></section><section class="pd-slide"><h2>B</h2></section>')
    assert "slide_count" in {i.code for i in dc.build_publishable({"index.html": two.encode()}).errors}

    nested = _deck_html(
        '<div><section class="pd-slide"><h2>A</h2></section></div>'
        + "".join(f'<section class="pd-slide"><h2>S{i}</h2></section>' for i in range(3))
    )
    assert dc.build_publishable({"index.html": nested.encode()}).slide_count == 3

    files = _valid_files()
    del files["assets/arch.svg"]
    del files["charts/sales.json"]
    codes = {i.code for i in dc.build_publishable(files).errors}
    assert {"missing_asset", "missing_chart"} <= codes

    bad_chart = _deck_html(_three_slides().replace("charts/sales.json", "../secrets.json"))
    codes = {i.code for i in dc.build_publishable({"index.html": bad_chart.encode()}).errors}
    assert "chart_path_invalid" in codes

    assert {i.code for i in dc.build_publishable({}).errors} == {"missing_index"}


def test_svg_sanitiser() -> None:
    issues: list[dc.Issue] = []
    dirty = (
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" onload="alert(1)">'
        b"<script>alert(2)</script><foreignObject><div>x</div></foreignObject>"
        b'<a xlink:href="javascript:alert(3)"><text>t</text></a>'
        b'<image href="https://evil.example/x.png"/>'
        b'<set attributeName="href" to="javascript:alert(4)"/>'
        b'<use href="#ok"/></svg>'
    )
    out = dc.sanitize_svg(dirty, "assets/x.svg", issues)
    assert out is not None
    text = out.decode("utf-8")
    for needle in ("onload", "<script", "foreignObject", "javascript:", "evil.example", "<set"):
        assert needle not in text, needle
    assert 'href="#ok"' in text
    assert any(i.code == "svg_sanitized" for i in issues)

    issues = []
    assert dc.sanitize_svg(b'<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>', "a.svg", issues) is None
    assert issues[0].code == "svg_doctype"
    issues = []
    assert dc.sanitize_svg(b"<html/>", "a.svg", issues) is None
    assert issues[0].code == "svg_root"


def test_raster_magic_and_chart_scrub() -> None:
    files = _valid_files()
    files["assets/ai/hero.png"] = b"\xff\xd8\xff" + b"\x00" * 10  # JPEG bytes with a .png name
    result = dc.build_publishable(files)
    assert "image_format" not in {i.code for i in result.errors}  # a real raster is accepted ...
    assert "image_ext_mismatch" in {i.code for i in result.warnings}  # ... and the mismatch is reported
    files["assets/ai/hero.png"] = b"<svg onload=alert(1)>"  # not a raster at all
    assert "image_format" in {i.code for i in dc.build_publishable(files).errors}

    issues: list[dc.Issue] = []
    raw = json.dumps({"title": {"text": "<img src=x onerror=alert(1)>"}, "__proto__": {"x": 1}, "series": [{"type": "line", "data": [1]}]})
    cleaned = json.loads(dc.sanitize_chart(raw.encode(), "charts/a.json", issues) or b"{}")
    assert "<" not in cleaned["title"]["text"] and ">" not in cleaned["title"]["text"]
    assert "__proto__" not in cleaned
    assert dc.sanitize_chart(b"[1,2]", "charts/a.json", issues) is None
    assert dc.sanitize_chart(b"{" + b" " * dc.LIMITS["chart_bytes"] + b"}", "charts/a.json", issues) is None


def test_describe_changes_is_machine_verified() -> None:
    old = dc.build_publishable(_valid_files()).files
    new_files = _valid_files()
    new_html = new_files["index.html"].decode("utf-8")
    new_html = new_html.replace("<h2>課題</h2>", "<h2>解決したい課題</h2>")
    new_html = new_html.replace("</head>", "<style>body{background:#fff}</style></head>")
    new_html = new_html.replace("</main>", '<section class="pd-slide"><h2>まとめ</h2></section></main>')
    new_files["index.html"] = new_html.encode("utf-8")
    new_files["assets/arch.svg"] = SVG.replace(b"#0af", b"#f60")
    new = dc.build_publishable(new_files).files
    changes = dc.describe_changes(old, new)
    assert "スライド枚数: 3 枚 → 4 枚" in changes
    assert "2 枚目のタイトル: 「課題」→「解決したい課題」" in changes
    assert "4 枚目「まとめ」を追加" in changes
    assert "デザイン（スタイルシート）を変更" in changes
    assert "素材を更新: assets/arch.svg" in changes
    assert dc.describe_changes(old, old) == []


def test_content_types() -> None:
    assert dc.content_type_for("charts/a.json").startswith("application/json")
    assert dc.content_type_for("assets/a.svg") == "image/svg+xml"
    assert dc.content_type_for("assets/a.JPG") == "image/jpeg"
    assert dc.content_type_for("index.html").startswith("text/html")
    assert dc.content_type_for("x.bin") == "application/octet-stream"


def test_normalize_path_rejects_traversal_and_remote() -> None:
    assert dc.normalize_path("./assets/a.svg") == "assets/a.svg"
    assert dc.normalize_path("assets/../assets/a.svg") == "assets/a.svg"
    for bad in ("../a.svg", "/etc/passwd", "https://x/a.svg", "//x/a.svg", "data:image/png;base64,AA", "#x", ""):
        assert dc.normalize_path(bad) is None, bad


def test_sniff_raster_and_served_content_type() -> None:
    assert dc.sniff_raster(PNG) == "png"
    assert dc.sniff_raster(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == "jpeg"
    assert dc.sniff_raster(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert dc.sniff_raster(b"GIF89a" + b"\x00" * 6) == "gif"
    assert dc.sniff_raster(b"<svg/>") is None
    # Raster assets are served with their real type, whatever the extension says.
    assert dc.content_type_for_bytes("assets/ai/hero.png", b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert dc.content_type_for_bytes("assets/ai/hero.png", PNG) == "image/png"
    # SVG, JSON and HTML keep the extension-based type (never sniffed).
    assert dc.content_type_for_bytes("assets/arch.svg", SVG) == "image/svg+xml"
    assert dc.content_type_for_bytes("charts/a.json", b"{}").startswith("application/json")
    assert dc.content_type_for_bytes("index.html", b"<html>").startswith("text/html")


def test_freeform_csp_only_allows_runtime_and_nonce() -> None:
    csp = dc.freeform_csp()
    directives = dict(part.strip().split(" ", 1) for part in csp.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"  # no inline script, no CDN
    assert "unsafe-eval" not in csp and "unsafe-inline" not in directives["script-src"]
    assert directives["img-src"] == "'self' data:"
    assert directives["object-src"] == "'none'" and directives["base-uri"] == "'none'"
    assert "https://fonts.googleapis.com" in directives["style-src"]
    assert "https://fonts.gstatic.com" in directives["font-src"]
    with_nonce = dict(part.strip().split(" ", 1) for part in dc.freeform_csp("abc123").split(";"))
    assert with_nonce["script-src"] == "'self' 'nonce-abc123'"
    assert "sandbox" in dc.SVG_ASSET_CSP and "script" not in dc.SVG_ASSET_CSP


def test_is_servable_asset_path_allowlist() -> None:
    for ok in ("assets/arch.svg", "assets/ai/hero.png", "assets/icons/a-b_c.webp", "charts/sales.json"):
        assert dc.is_servable_asset_path(ok), ok
    for bad in (
        "index.html", "source.html", "manifest.json", "image_requests.json", "generate_assets.py",
        "_qa/slide_01.jpg", "assets/../index.html", "./assets/a.svg", "assets/a.svg?x=1",
        "charts/a.js", "assets/a.html", "/assets/a.svg", "https://x/assets/a.svg", "",
    ):
        assert not dc.is_servable_asset_path(bad), bad


def test_svg_animation_cannot_rewrite_links() -> None:
    issues: list[dc.Issue] = []
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<a xlink:href="#ok"><animate attributeName="href" to="javascript:alert(1)"/>'
        b'<set attributeName="fill" to="red"/><text>x</text></a></svg>'
    )
    out = dc.sanitize_svg(svg, "assets/a.svg", issues) or b""
    assert b"javascript" not in out and b"<animate" not in out.replace(b"ns0:", b"")
    assert b"set" in out  # harmless animation of a presentation attribute is kept
    files = _valid_files('<svg><a href="#x"><animate attributeName="href" values="javascript:alert(1)"/></a></svg>')
    index = dc.build_publishable(files).files["index.html"].decode("utf-8")
    assert "javascript" not in index



def test_portal_layout_and_describe_changes_layout_switch() -> None:
    portal_html = (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>Acme Portal</title></head>'
        '<body><main id="pd-deck" data-pd-layout="portal">'
        '<section class="pd-slide" data-pd-title="01. エグゼクティブサマリー" data-pd-subtitle="全体像">'
        '<h2>1. 現状の課題</h2><p>本文</p><h3>1.1 詳細</h3>'
        '<aside class="pd-refs"><h4>関連資料</h4><ul><li>RFP.pdf</li></ul></aside>'
        '</section>'
        '<section class="pd-slide" data-pd-title="02. 解決アプローチ"><h2>2. 提案内容</h2><p>本文</p></section>'
        '<section class="pd-slide" data-pd-title="03. アーキテクチャ"><h2>3. 構成図</h2><p>本文</p></section>'
        '</main></body></html>'
    )
    res_portal = dc.build_publishable({"index.html": portal_html.encode("utf-8")}, title="Acme Portal")
    assert res_portal.errors == [], res_portal.summary()
    published_index = res_portal.files["index.html"].decode("utf-8")
    assert 'data-pd-layout="portal"' in published_index
    assert 'class="pd-refs"' in published_index

    slides_html = portal_html.replace('data-pd-layout="portal"', 'data-pd-layout="slides"')
    res_slides = dc.build_publishable({"index.html": slides_html.encode("utf-8")}, title="Acme Portal")
    assert res_slides.errors == [], res_slides.summary()

    diff_to_slides = dc.describe_changes(res_portal.files, res_slides.files)
    assert any("UI形式（レイアウト）" in c and "slides" in c for c in diff_to_slides), diff_to_slides

    diff_to_portal = dc.describe_changes(res_slides.files, res_portal.files)
    assert any("UI形式（レイアウト）" in c and "portal" in c for c in diff_to_portal), diff_to_portal


def test_inline_svg_self_closing_shapes_are_closed() -> None:
    html_in = (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>SVG Test</title></head>'
        '<body><main id="pd-deck" data-pd-layout="portal">'
        '<section class="pd-slide" data-pd-title="01"><h2>1</h2>'
        '<svg viewBox="0 0 880 380" width="880" height="380">'
        '<rect x="10" y="12" width="860" height="70" fill="#f8fafc"/>'
        '<rect x="24" y="4" width="150" height="22" fill="#0369a1"/>'
        '<text x="99" y="19">LAYER 1</text>'
        '<line x1="440" y1="84" x2="440" y2="98" stroke="#0369a1"/>'
        '<polygon points="440,104 435,96 445,96" fill="#0369a1"/>'
        '</svg></section>'
        '<section class="pd-slide" data-pd-title="02"><h2>2</h2><p>b</p></section>'
        '<section class="pd-slide" data-pd-title="03"><h2>3</h2><p>c</p></section>'
        '</main></body></html>'
    )
    res = dc.build_publishable({"index.html": html_in.encode("utf-8")}, title="SVG Test")
    assert res.errors == [], res.summary()
    out = res.files["index.html"].decode("utf-8")
    assert "</rect><rect" in out
    assert "</line><polygon" in out
