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

"""Renderer SVG diagram audit (needs a local Chrome / Chromium; skipped otherwise)."""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHROME = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
pytestmark = pytest.mark.skipif(CHROME is None, reason="Chrome / Chromium is not installed")


def _renderer():
    sys.path.insert(0, str(ROOT / "proposal_agent" / "app"))
    spec = importlib.util.spec_from_file_location("deck_renderer_main_test", ROOT / "deck_renderer" / "main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # pydantic resolves postponed annotations through sys.modules
    spec.loader.exec_module(module)
    return module


SVG_HEAD = '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400" viewBox="0 0 800 400">'
BAD_SVG = (
    SVG_HEAD
    + '<text x="40" y="60" font-size="28" fill="#111">隠れるラベル</text>'
    + '<rect x="30" y="20" width="260" height="60" fill="#10b981"/>'  # drawn AFTER the label -> hides it
    + '<line x1="380" y1="150" x2="700" y2="150" stroke="#334155" stroke-width="3"/>'
    + '<text x="420" y="160" font-size="28" fill="#111">矢印が横切る</text>'  # line runs through the middle
    + '<text x="700" y="300" font-size="28" fill="#111">右端からはみ出す長いラベル</text>'
    + "</svg>"
)
GOOD_SVG = (
    SVG_HEAD
    + '<rect x="40" y="40" width="300" height="100" rx="12" fill="#ecfdf5" stroke="#10b981" stroke-width="2"/>'
    + '<text x="190" y="100" font-size="28" text-anchor="middle" fill="#064e3b">Agent Runtime</text>'
    + '<line x1="340" y1="90" x2="460" y2="90" stroke="#334155" stroke-width="3"/>'
    + '<rect x="460" y="40" width="300" height="100" rx="12" fill="#ecfdf5" stroke="#10b981" stroke-width="2"/>'
    + '<text x="610" y="100" font-size="28" text-anchor="middle" fill="#064e3b">BigQuery</text>'
    + "</svg>"
)
INLINE_OVERLAP = (
    '<svg viewBox="0 0 800 300" width="800" height="300">'
    '<text x="60" y="120" font-size="32">データ照合</text>'
    '<text x="90" y="124" font-size="32">販促案選定</text>'
    "</svg>"
)


def _deck() -> dict[str, bytes]:
    slides = [
        ("インライン図", INLINE_OVERLAP),
        ("ファイルの図（不具合あり）", '<img src="assets/bad.svg" alt="不具合のある図" width="800" height="400">'),
        ("ファイルの図（正常）", '<img src="assets/good.svg" alt="正常な図" width="800" height="400">'),
    ]
    body = "".join(
        f'<section class="pd-slide" data-pd-title="{title}"><h2>{title}</h2>{inner}</section>' for title, inner in slides
    )
    html = (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>SVG audit</title></head>'
        f'<body><main id="pd-deck">{body}</main></body></html>'
    )
    return {
        "index.html": html.encode("utf-8"),
        "manifest.json": b'{"concept": "test"}',
        "assets/bad.svg": BAD_SVG.encode("utf-8"),
        "assets/good.svg": GOOD_SVG.encode("utf-8"),
    }


def test_svg_audit_flags_collisions_in_inline_and_file_diagrams() -> None:
    from app import deck_contract as dc

    build = dc.build_publishable(_deck(), "SVG audit")
    assert not build.errors, [e.message for e in build.errors]
    renderer = _renderer()
    req = renderer.RenderRequest(width=640, max_slides=5, timeout_seconds=90)
    result = asyncio.run(renderer.render_deck(build.files, req))
    assert result["ok"], result.get("error")
    by_slide = {s["index"]: s["issues"] for s in result["slides"]}

    inline = {(i["type"], i["severity"]) for i in by_slide[0]}
    assert ("svg_text_overlap", "error") in inline

    bad = {(i["type"], i.get("element")) for i in by_slide[1] if i["severity"] == "error"}
    assert ("svg_text_hidden", "assets/bad.svg") in bad
    assert ("svg_line_over_text", "assets/bad.svg") in bad
    assert ("svg_text_clipped", "assets/bad.svg") in bad

    assert not [i for i in by_slide[2] if i["type"].startswith("svg_")]  # the clean diagram has no findings
    assert set(result["svg_audits"]) == {"assets/bad.svg", "assets/good.svg"}
