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

"""Private deck preview renderer (Cloud Run, IAM-only).

The generation worker calls POST /v1/render with a GCS prefix that holds a *sanitised* free-form deck
(output of deck_contract.build_publishable). The renderer serves those exact bytes from 127.0.0.1 with the
same Content-Security-Policy as the public hosting gateway, opens them in headless Chromium, and returns one
screenshot + layout audit per slide plus console/network/CSP problems. The worker then shows the screenshots
to the designer agent's ADK session as image input so the agent can look at its own slides and fix them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal

import websockets
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
try:  # copied next to main.py by infra/deploy.sh
    import deck_contract  # type: ignore[import-not-found]
except ImportError:  # local development / tests: use the canonical copy
    sys.path.insert(0, str(HERE.parent / "proposal_agent" / "app"))
    import deck_contract  # type: ignore[import-not-found,no-redef]

RUNTIME_DIR = next(
    (p for p in (HERE / "deck_runtime", HERE.parent / "proposal_agent" / "app" / "deck_runtime") if p.is_dir()),
    HERE / "deck_runtime",
)
CHROME_BIN = os.environ.get("CHROME_BIN") or next(
    (p for p in (shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome")) if p),
    "chromium",
)
ALLOWED_BUCKETS = {b.strip() for b in os.environ.get("ALLOWED_BUCKETS", "").split(",") if b.strip()}
ALLOWED_PREFIXES = ("staging/", "presentations/")
MAX_DOWNLOAD_FILES = 200
MAX_DOWNLOAD_BYTES = 40_000_000
VIEW_W, VIEW_H = 1920, 1080

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deck_renderer")
app = FastAPI(title="Deck preview renderer", docs_url=None, redoc_url=None, openapi_url=None)
_render_lock = asyncio.Semaphore(1)  # one Chromium per instance (Cloud Run --concurrency=1 as well)

_CSP_PROBE = """
window.__nyCsp = [];
document.addEventListener('securitypolicyviolation', function (e) {
  if (window.__nyCsp.length < 30) {
    window.__nyCsp.push({directive: e.violatedDirective, blocked: String(e.blockedURI || '').slice(0, 200),
                         sample: String(e.sample || '').slice(0, 80)});
  }
});
"""


MAX_SVG_AUDITS = 12

# Text collision audit for one SVG root (an inline <svg> or the root of an SVG document). Returns
# {ok, text_count, issues[]}. Errors: text on text, text hidden under a shape drawn later, text outside the
# drawing area, and a visible line / arrow / border running through the middle of a label.
_SVG_AUDIT_FN = r"""
function (root) {
  var out = { ok: true, text_count: 0, issues: [] };
  if (!root || !root.getBoundingClientRect || !root.querySelectorAll) { out.ok = false; return out; }
  var doc = root.ownerDocument || document;
  var vp = root.getBoundingClientRect();
  function label(s) { s = String(s || '').replace(/\s+/g, ' ').trim(); return s.length > 18 ? s.slice(0, 18) + '…' : s; }
  function push(type, severity, detail) { if (out.issues.length < 24) { out.issues.push({ type: type, severity: severity, detail: detail }); } }
  function visible(el) { var cs = getComputedStyle(el); return cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity || '1') > 0.05; }
  var texts = [];
  Array.prototype.forEach.call(root.querySelectorAll('text'), function (el) {
    if (!visible(el)) { return; }
    var r = el.getBoundingClientRect();
    var s = label(el.textContent);
    if (s && r.width > 0.5 && r.height > 0.5) { texts.push({ el: el, r: r, s: s }); }
  });
  out.text_count = texts.length;
  var i, j;
  for (i = 0; i < texts.length; i++) {
    var c = texts[i].r;
    if (c.left < vp.left - 2 || c.right > vp.right + 2 || c.top < vp.top - 2 || c.bottom > vp.bottom + 2) {
      push('svg_text_clipped', 'error', '「' + texts[i].s + '」が図の描画範囲からはみ出しています');
    }
  }
  for (i = 0; i < texts.length; i++) {
    for (j = i + 1; j < texts.length; j++) {
      var a = texts[i], b = texts[j];
      if (a.el.contains(b.el) || b.el.contains(a.el)) { continue; }
      var w = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
      var h = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
      if (w > 4 && h > 0.4 * Math.min(a.r.height, b.r.height)) {
        push('svg_text_overlap', 'error', '「' + a.s + '」と「' + b.s + '」が重なっています');
      }
    }
  }
  for (i = 0; i < texts.length; i++) {
    var x = texts[i], hidden = 0, fr = [0.2, 0.5, 0.8], cy = x.r.top + x.r.height / 2;
    for (j = 0; j < fr.length; j++) {
      var hit = doc.elementFromPoint(x.r.left + x.r.width * fr[j], cy);
      if (!hit || hit === root || !root.contains(hit) || x.el.contains(hit)) { continue; }
      if (hit.closest && hit.closest('text')) { continue; }
      if (x.el.compareDocumentPosition(hit) & 4) { hidden += 1; }
    }
    if (hidden >= 2) { push('svg_text_hidden', 'error', '「' + x.s + '」が後から描いた図形の下に隠れています'); }
  }
  var crossed = {};
  Array.prototype.forEach.call(root.querySelectorAll('line, polyline, path, rect, polygon'), function (el) {
    if (!visible(el) || el.closest('defs, marker, clipPath, mask, pattern')) { return; }
    var cs = getComputedStyle(el);
    if (!cs.stroke || cs.stroke === 'none' || !(parseFloat(cs.strokeWidth) > 0)) { return; }
    var total = 0;
    try { total = el.getTotalLength(); } catch (e) { return; }
    var m = el.getScreenCTM ? el.getScreenCTM() : null;
    if (!m || !(total > 0)) { return; }
    var n = Math.min(120, Math.max(8, Math.ceil(total / 5)));
    for (var k = 0; k <= n; k++) {
      var p = el.getPointAtLength(total * k / n);
      var q = new DOMPoint(p.x, p.y).matrixTransform(m);
      for (var u = 0; u < texts.length; u++) {
        if (crossed[u]) { continue; }
        var rr = texts[u].r, padY = rr.height * 0.25;
        if (!(q.x > rr.left + 2 && q.x < rr.right - 2 && q.y > rr.top + padY && q.y < rr.bottom - padY)) { continue; }
        var stack = doc.elementsFromPoint(q.x, q.y), top = null;
        for (var s = 0; s < stack.length; s++) { if (!texts[u].el.contains(stack[s])) { top = stack[s]; break; } }
        if (top === el) {
          crossed[u] = true;
          push('svg_line_over_text', 'error', '線・矢印・枠線が「' + texts[u].s + '」に重なっています');
        }
      }
    }
  });
  return out;
}
"""

# Per slide: audits the inline <svg> diagrams of the visible slide and lists the SVG files it shows via <img>.
_SLIDE_SVG_JS = (
    "function (idx) {"
    " var s = document.querySelectorAll('#pd-deck > section.pd-slide')[idx];"
    " if (!s) { return { refs: [], issues: [] }; }"
    " var audit = " + _SVG_AUDIT_FN + ";"
    " var issues = [];"
    " Array.prototype.forEach.call(s.querySelectorAll('svg'), function (svg) {"
    "   if (svg.ownerSVGElement || svg.closest('.pd-chart')) { return; }"
    "   var r = audit(svg);"
    "   (r.issues || []).forEach(function (it) { it.element = 'inline svg'; issues.push(it); });"
    " });"
    " var refs = Array.prototype.map.call(s.querySelectorAll('img'), function (im) { return im.getAttribute('src') || ''; })"
    "   .filter(function (src) { return /\\.svg([?#]|$)/i.test(src); });"
    " return { refs: refs, issues: issues.slice(0, 20) };"
    "}"
)


def _normalise_svg_ref(ref: str) -> str:
    rel = str(ref or "").strip().split("#", 1)[0].split("?", 1)[0]
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel.startswith(("/", "http:", "https:", "data:")) or ".." in rel.split("/"):
        return ""
    return rel if rel.lower().endswith(".svg") and deck_contract.is_servable_asset_path(rel) else ""


def _count_events(cdp: "CDP", method: str, session: str) -> int:
    return sum(1 for e in cdp.events if e.get("method") == method and e.get("sessionId") == session)


async def _audit_svg_assets(
    cdp: "CDP",
    session: str,
    port: int,
    files: dict[str, bytes],
    refs: dict[str, list[int]],
    result: dict[str, Any],
    deadline: float,
) -> dict[str, Any]:
    """Opens each referenced SVG file as its own document and runs the collision audit. Never raises."""
    audits: dict[str, Any] = {}
    by_rel: dict[str, list[int]] = {}
    for raw, positions in refs.items():
        rel = _normalise_svg_ref(raw)
        if rel and rel in files:
            by_rel.setdefault(rel, []).extend(positions)
    for rel, positions in list(by_rel.items())[:MAX_SVG_AUDITS]:
        if time.monotonic() > deadline - 3:
            result["runtime_warnings"].append({"type": "svg_audit_timeout", "detail": "時間切れのため一部の図（SVG）を検査できませんでした"})
            break
        try:
            before = _count_events(cdp, "Page.loadEventFired", session)
            await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/deck/{rel}"}, session=session)
            end = time.monotonic() + 8
            while time.monotonic() < end and _count_events(cdp, "Page.loadEventFired", session) <= before:
                await asyncio.sleep(0.05)
            res = await cdp.call(
                "Runtime.evaluate",
                {"expression": "JSON.stringify((" + _SVG_AUDIT_FN + ")(document.documentElement))", "returnByValue": True},
                session=session,
                timeout=10,
            )
            audit = json.loads(res.get("result", {}).get("value") or "{}")
        except Exception as exc:  # noqa: BLE001 - one broken diagram must not fail the render
            result["runtime_warnings"].append({"type": "svg_audit_failed", "detail": f"{rel}: {type(exc).__name__}"})
            continue
        issues = [dict(i, element=rel) for i in audit.get("issues", [])[:12]]
        audits[rel] = {"text_count": audit.get("text_count", 0), "issues": issues}
        for pos in sorted(set(positions)):
            if 0 <= pos < len(result["slides"]):
                result["slides"][pos]["issues"].extend(dict(i) for i in issues)
    return audits


class RenderRequest(BaseModel):
    bucket: str | None = None
    prefix: str | None = None
    files: dict[str, str] | None = Field(default=None, description="path -> base64 (local testing only)")
    width: int = Field(1280, ge=320, le=1920)
    format: Literal["jpeg", "png"] = "jpeg"
    quality: int = Field(80, ge=30, le=95)
    timeout_seconds: int = Field(150, ge=10, le=290)
    max_slides: int = Field(20, ge=1, le=30)


class CDPError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Local static server (exact bytes + same CSP as the hosting gateway)
# ---------------------------------------------------------------------------
def _runtime_bytes() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name in deck_contract.RUNTIME_FILES:
        path = RUNTIME_DIR / name
        if path.is_file():
            out[name] = path.read_bytes()
    return out


def _start_server(files: dict[str, bytes]) -> tuple[ThreadingHTTPServer, int]:
    runtime = _runtime_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # quiet
            return

        def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if path == "/favicon.ico":  # browser noise, not a deck problem
                self._send(204, b"", "image/x-icon")
                return
            if path in ("/deck", "/deck/", "/deck/index.html"):
                body = files.get("index.html")
                if body is None:
                    self._send(404, b"missing index.html", "text/plain")
                    return
                self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": deck_contract.freeform_csp()})
                return
            if path.startswith(deck_contract.RUNTIME_BASE):
                name = path[len(deck_contract.RUNTIME_BASE) :]
                if name in runtime:
                    ctype = "text/css; charset=utf-8" if name.endswith(".css") else "application/javascript; charset=utf-8"
                    self._send(200, runtime[name], ctype)
                    return
            if path.startswith("/deck/"):
                rel = path[len("/deck/") :]
                if deck_contract.is_servable_asset_path(rel) and rel in files:
                    extra = {"Content-Security-Policy": deck_contract.SVG_ASSET_CSP} if rel.endswith(".svg") else None
                    self._send(200, files[rel], deck_contract.content_type_for(rel), extra)
                    return
            self._send(404, b"not found", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="deck-static", daemon=True).start()
    return server, int(server.server_address[1])


# ---------------------------------------------------------------------------
# Minimal CDP client (flattened sessions)
# ---------------------------------------------------------------------------
class CDP:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.seq = 0
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid is not None and mid in self.pending:
                    fut = self.pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                else:
                    self.events.append(msg)
        except Exception as exc:  # connection closed / browser crashed
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(CDPError(f"CDP connection lost: {exc}"))
            self.pending.clear()

    async def call(self, method: str, params: dict[str, Any] | None = None, session: str | None = None, timeout: float = 30) -> dict[str, Any]:
        self.seq += 1
        mid = self.seq
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        payload: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        await self.ws.send(json.dumps(payload))
        try:
            msg = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(mid, None)
        if "error" in msg:
            raise CDPError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def wait_event(self, method: str, session: str | None, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(e.get("method") == method and e.get("sessionId") == session for e in self.events):
                return True
            await asyncio.sleep(0.1)
        return False

    async def close(self) -> None:
        self.reader.cancel()
        with contextlib.suppress(Exception):
            await self.ws.close()


async def _launch_chrome(profile: str) -> tuple[subprocess.Popen[bytes], str]:
    args = [
        CHROME_BIN,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        "--mute-audio",
        "--force-color-profile=srgb",
        "--font-render-hinting=none",
        "--lang=ja-JP",
        f"--window-size={VIEW_W},{VIEW_H}",
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "about:blank",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = Path(profile) / "DevToolsActivePort"
    for _ in range(150):
        if proc.poll() is not None:
            raise CDPError(f"chromium exited early (code {proc.returncode})")
        if port_file.exists():
            lines = port_file.read_text().split("\n")
            if len(lines) >= 2 and lines[0].strip().isdigit():
                return proc, f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
        await asyncio.sleep(0.1)
    raise CDPError("chromium did not expose a DevTools port")


def _summarise_events(events: list[dict[str, Any]], session: str) -> dict[str, list[dict[str, str]]]:
    console_errors: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    urls: dict[str, str] = {}
    for e in events:
        if e.get("sessionId") != session:
            continue
        method = e.get("method")
        p = e.get("params", {})
        if method == "Network.requestWillBeSent":
            urls[p.get("requestId", "")] = p.get("request", {}).get("url", "")
        elif method == "Network.responseReceived":
            status = int(p.get("response", {}).get("status", 0) or 0)
            if status >= 400:
                failed.append({"url": p.get("response", {}).get("url", "")[:200], "error": f"HTTP {status}"})
        elif method == "Network.loadingFailed":
            if p.get("canceled"):
                continue
            failed.append({"url": urls.get(p.get("requestId", ""), "")[:200], "error": str(p.get("errorText", ""))[:120]})
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            desc = d.get("exception", {}).get("description") or d.get("text", "")
            console_errors.append({"type": "exception", "text": str(desc)[:300]})
        elif method == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
            console_errors.append({"type": f"console.{p.get('type')}", "text": text[:300]})
        elif method == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
            entry = p["entry"]
            console_errors.append({"type": f"log.{entry.get('source', '')}", "text": str(entry.get("text", ""))[:300]})
    return {"console_errors": console_errors[:40], "failed_requests": failed[:40]}


async def render_deck(files: dict[str, bytes], req: RenderRequest) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": False,
        "slide_count": 0,
        "slides": [],
        "console_errors": [],
        "failed_requests": [],
        "csp_violations": [],
        "runtime_errors": [],
        "runtime_warnings": [],
        "chart_errors": [],
        "error": "",
    }
    if "index.html" not in files:
        result["error"] = "index.html がありません"
        return result
    server, port = _start_server(files)
    profile = tempfile.mkdtemp(prefix="deck-render-")
    proc: subprocess.Popen[bytes] | None = None
    cdp: CDP | None = None
    session = ""
    deadline = started + req.timeout_seconds
    try:
        proc, ws_url = await _launch_chrome(profile)
        ws = await websockets.connect(ws_url, max_size=128 * 1024 * 1024, open_timeout=15)
        cdp = CDP(ws)
        target = await cdp.call("Target.createTarget", {"url": "about:blank"})
        attached = await cdp.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        session = attached["sessionId"]
        for method in ("Page.enable", "Runtime.enable", "Network.enable", "Log.enable"):
            await cdp.call(method, session=session)
        await cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": VIEW_W, "height": VIEW_H, "deviceScaleFactor": 1, "mobile": False},
            session=session,
        )
        await cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": _CSP_PROBE}, session=session)

        async def evaluate(expr: str, await_promise: bool = False, timeout: float = 30) -> Any:
            remaining = max(3.0, deadline - time.monotonic())
            res = await cdp.call(  # type: ignore[union-attr]
                "Runtime.evaluate",
                {"expression": expr, "awaitPromise": await_promise, "returnByValue": True},
                session=session,
                timeout=min(timeout, remaining),
            )
            if res.get("exceptionDetails"):
                details = res["exceptionDetails"]
                raise CDPError(str(details.get("exception", {}).get("description") or details.get("text"))[:300])
            return res.get("result", {}).get("value")

        await cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/deck/?pd_capture=1"}, session=session)
        await cdp.wait_event("Page.loadEventFired", session, timeout=min(25.0, max(3.0, deadline - time.monotonic())))
        has_runtime = await evaluate("typeof window.PdDeck === 'object' && !!window.PdDeck.ready")
        svg_refs = {}
        clip_scale = req.width / VIEW_W
        shot_params: dict[str, Any] = {
            "format": req.format,
            "clip": {"x": 0, "y": 0, "width": VIEW_W, "height": VIEW_H, "scale": clip_scale},
        }
        if req.format == "jpeg":
            shot_params["quality"] = req.quality
        if not has_runtime:
            result["runtime_errors"].append({"type": "runtime_missing", "detail": "共通ランタイムを読み込めませんでした"})
            shot = await cdp.call("Page.captureScreenshot", shot_params, session=session)
            result["slides"].append({"index": 0, "title": "(ページ全体)", "issues": [], "stats": {}, "screenshot_b64": shot["data"]})
        else:
            await evaluate("window.PdDeck.ready.then(function () { return true; })", await_promise=True, timeout=25)
            slides = await evaluate("JSON.stringify(window.PdDeck.listSlides())") or "[]"
            slide_list = json.loads(slides)
            result["slide_count"] = len(slide_list)
            svg_refs: dict[str, list[int]] = {}
            for item in slide_list[: req.max_slides]:
                if time.monotonic() > deadline - 2:
                    result["runtime_warnings"].append({"type": "render_timeout", "detail": "時間切れのため一部のスライドを撮影できませんでした"})
                    break
                idx = int(item.get("index", 0))
                await evaluate(f"window.PdDeck.capture({idx})", await_promise=True, timeout=15)
                await asyncio.sleep(0.15)
                audit_raw = await evaluate(f"JSON.stringify(window.PdDeck.audit({idx}))") or "{}"
                audit = json.loads(audit_raw)
                slide_issues = list(audit.get("issues", []))
                try:
                    svg_info = json.loads(await evaluate("JSON.stringify((" + _SLIDE_SVG_JS + ")(" + str(idx) + "))") or "{}")
                except Exception as exc:  # noqa: BLE001 - the diagram audit is best effort
                    svg_info = {}
                    result["runtime_warnings"].append({"type": "svg_audit_failed", "detail": f"slide {idx + 1}: {type(exc).__name__}"})
                slide_issues.extend(svg_info.get("issues", []))
                for ref in svg_info.get("refs", []):
                    svg_refs.setdefault(str(ref), []).append(len(result["slides"]))
                shot = await cdp.call("Page.captureScreenshot", shot_params, session=session, timeout=30)
                result["slides"].append(
                    {
                        "index": idx,
                        "title": audit.get("title") or item.get("title", ""),
                        "issues": slide_issues,
                        "stats": audit.get("stats", {}),
                        "screenshot_b64": shot["data"],
                    }
                )
            state = await evaluate(
                "JSON.stringify({errors: PdDeck.errors, warnings: PdDeck.warnings, charts: PdDeck.chartErrors})"
            )
            parsed = json.loads(state or "{}")
            result["runtime_errors"].extend(parsed.get("errors", [])[:20])
            result["runtime_warnings"].extend(parsed.get("warnings", [])[:20])
            result["chart_errors"] = parsed.get("charts", [])[:20]
        csp_raw = await evaluate("JSON.stringify(window.__nyCsp || [])")
        result["csp_violations"] = json.loads(csp_raw or "[]")
        await asyncio.sleep(0.3)  # let trailing network/console events arrive
        result.update(_summarise_events(cdp.events, session))
        # Diagram files are opened as their own documents AFTER the deck's console/network summary.
        if has_runtime and svg_refs:
            result["svg_audits"] = await _audit_svg_assets(cdp, session, port, files, svg_refs, result, deadline)
        result["ok"] = bool(result["slides"])
    except Exception as exc:  # never raise to the caller; the worker decides what to do
        log.exception("render failed")
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        if cdp is not None and session:
            result.update(_summarise_events(cdp.events, session))
    finally:
        if cdp is not None:
            await cdp.close()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        server.shutdown()
        server.server_close()
        shutil.rmtree(profile, ignore_errors=True)
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------
def _download_prefix(bucket_name: str, prefix: str) -> dict[str, bytes]:
    from google.cloud import storage  # imported lazily so local tests do not need credentials

    client = storage.Client()
    files: dict[str, bytes] = {}
    total = 0
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        rel = blob.name[len(prefix) :]
        if not rel or rel.endswith("/"):
            continue
        if len(files) >= MAX_DOWNLOAD_FILES:
            break
        total += int(blob.size or 0)
        if total > MAX_DOWNLOAD_BYTES:
            break
        files[rel] = blob.download_as_bytes()
    return files


def _load_files(req: RenderRequest) -> dict[str, bytes]:
    if req.files is not None:
        try:
            return {k: base64.b64decode(v) for k, v in req.files.items()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid base64: {exc}") from exc
    if not req.bucket or not req.prefix:
        raise HTTPException(status_code=400, detail="bucket and prefix (or files) are required")
    if ALLOWED_BUCKETS and req.bucket not in ALLOWED_BUCKETS:
        raise HTTPException(status_code=403, detail="bucket not allowed")
    prefix = req.prefix if req.prefix.endswith("/") else req.prefix + "/"
    if ".." in prefix or not prefix.startswith(ALLOWED_PREFIXES):
        raise HTTPException(status_code=400, detail="prefix not allowed")
    return _download_prefix(req.bucket, prefix)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "chrome": CHROME_BIN,
        "chrome_found": bool(shutil.which(CHROME_BIN) or Path(CHROME_BIN).exists()),
        "runtime_files": sorted(_runtime_bytes()),
    }


@app.post("/v1/render")
async def render(req: RenderRequest) -> dict[str, Any]:
    files = await asyncio.to_thread(_load_files, req)
    async with _render_lock:
        result = await render_deck(files, req)
    log.info(
        json.dumps(
            {
                "event": "render",
                "prefix": req.prefix or "(inline)",
                "ok": result["ok"],
                "slides": len(result["slides"]),
                "elapsed_ms": result["elapsed_ms"],
                "error": result["error"],
            },
            ensure_ascii=False,
        )
    )
    return result
