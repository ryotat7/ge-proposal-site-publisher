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

"""External Customer Authentication & Private GCS Streaming Gateway on Cloud Run."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import html
import logging
import os
import secrets
import time
from typing import Any

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="Proposal Hosting Gateway")

_EPHEMERAL_SECRET: bytes | None = None


def _get_project_id() -> str:
    return (
        os.environ.get("PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or "your-gcp-project-id"
    )


def _get_bucket_name() -> str:
    return os.environ.get(
        "PROPOSAL_GCS_BUCKET", f"{_get_project_id()}-proposals"
    )


def _get_firestore_collection() -> str:
    return os.environ.get("PROPOSAL_FIRESTORE_COLLECTION", "presentations")


def _get_brand_name() -> str:
    return os.environ.get("PROPOSAL_BRAND_NAME", "Strategic AI Partners")


def _get_brand_badge() -> str:
    return os.environ.get("PROPOSAL_BRAND_BADGE", "SP")


def _get_cookie_name(presentation_id: str) -> str:
    prefix = os.environ.get("PROPOSAL_COOKIE_PREFIX", "proposal_session")
    return f"{prefix}_{presentation_id}"


def _get_session_secret() -> bytes:
    env_secret = os.environ.get("GATEWAY_SESSION_SECRET")
    if env_secret:
        return env_secret.encode("utf-8")
    return hashlib.sha256(
        f"proposal-gateway-session:{_get_project_id()}:{_get_bucket_name()}".encode(
            "utf-8"
        )
    ).digest()


def verify_pbkdf2_password(
    password: str, expected_hash_hex: str, salt_hex: str
) -> bool:
    """Verifies a plaintext password against PBKDF2-HMAC-SHA256 hash and salt."""
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.strip().encode("utf-8"),
            bytes.fromhex(salt_hex),
            120_000,
        )
        return hmac.compare_digest(dk.hex(), expected_hash_hex)
    except Exception:
        return False


def create_session_token(
    presentation_id: str,
    viewer_id: str,
    ttl_seconds: int = 86400,
    password_version: str = "",
) -> str:
    """Creates an HMAC-SHA256 signed session token."""
    exp = int(time.time()) + ttl_seconds
    ver = password_version[:12] if password_version else "v1"
    payload = f"{presentation_id}:{viewer_id}:{exp}:{ver}"
    sig = hmac.new(
        _get_session_secret(), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    raw = f"{payload}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def verify_session_token(
    token: str,
    expected_presentation_id: str,
    expected_password_version: str = "",
) -> str | None:
    """Verifies an HMAC-SHA256 signed session token and returns viewer_id if valid."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        parts = decoded.split(":")
        if len(parts) != 5:
            return None
        presentation_id, viewer_id, exp_str, ver, sig = parts
        if presentation_id != expected_presentation_id:
            return None
        if int(exp_str) < int(time.time()):
            return None
        expected_ver = (
            expected_password_version[:12] if expected_password_version else "v1"
        )
        if not hmac.compare_digest(ver, expected_ver):
            return None
        payload = f"{presentation_id}:{viewer_id}:{exp_str}:{ver}"
        expected_sig = hmac.new(
            _get_session_secret(), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        return viewer_id
    except Exception:
        return None


def _get_firestore_client() -> Any:
    from google.cloud import firestore

    project_id = _get_project_id()
    fs_client = firestore.Client(project=project_id)
    db_name = getattr(fs_client, "_database", "(default)") or "(default)"
    if not isinstance(db_name, str):
        db_name = "(default)"
    fs_client._database_string_internal = f"projects/{project_id}/databases/{db_name}"
    return fs_client


def _get_firestore_doc(presentation_id: str) -> dict[str, Any] | None:
    """Fetches the presentation metadata document from Firestore."""
    fs_client = _get_firestore_client()
    doc_snap = (
        fs_client.collection(_get_firestore_collection())
        .document(presentation_id)
        .get()
    )
    if not doc_snap.exists:
        return None
    return doc_snap.to_dict()


def _record_access_log(
    presentation_id: str,
    viewer_id: str,
    auth_method: str,
    request: Request,
) -> None:
    """Writes an access audit log to presentations/{presentation_id}/access_logs."""
    try:
        fs_client = _get_firestore_client()
        forwarded_for = request.headers.get("x-forwarded-for")
        ip_addr = (
            forwarded_for.split(",")[0].strip()
            if forwarded_for
            else (request.client.host if request.client else "unknown")
        )
        user_agent = request.headers.get("user-agent", "unknown")
        (
            fs_client.collection(_get_firestore_collection())
            .document(presentation_id)
            .collection("access_logs")
            .add(
                {
                    "accessed_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "viewer_id": viewer_id,
                    "auth_method": auth_method,
                    "ip_address": ip_addr,
                    "user_agent": user_agent,
                }
            )
        )
    except Exception as exc:
        logger.warning("Failed to write access log: %s", exc)


def _fetch_html_from_gcs(bucket_name: str, blob_path: str) -> bytes:
    """Downloads the private HTML presentation file from Cloud Storage."""
    from google.cloud import storage

    storage_client = storage.Client(project=_get_project_id())
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    return blob.download_as_bytes()


def _is_expired(expires_at_str: str | None) -> bool:
    if not expires_at_str:
        return False
    try:
        exp_dt = datetime.datetime.fromisoformat(expires_at_str)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
        return exp_dt < datetime.datetime.now(datetime.timezone.utc)
    except Exception:
        return False


def _is_revoked_or_inactive(doc: dict[str, Any]) -> bool:
    if not doc.get("is_active", True):
        return True
    if str(doc.get("status", "active")).lower() == "revoked":
        return True
    if _is_expired(doc.get("expires_at")):
        return True
    return False


def _render_login_html(
    presentation_id: str,
    client_name: str = "Client",
    error_message: str = "",
) -> str:
    safe_pres_id = html.escape(presentation_id, quote=True)
    safe_client = html.escape(client_name, quote=True)
    safe_brand = html.escape(_get_brand_name(), quote=True)
    safe_badge = html.escape(_get_brand_badge(), quote=True)
    safe_error = html.escape(error_message, quote=True)
    error_block = (
        f'<div class="mb-4 p-3 rounded-lg bg-rose-500/20 border border-rose-400/40 text-rose-200 text-xs">{safe_error}</div>'
        if safe_error
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>認証 | {safe_client} 御中 提案プレゼンテーション - {safe_brand}</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-slate-950 text-slate-100 flex items-center justify-center p-4">
  <div class="w-full max-w-md rounded-2xl bg-slate-900/90 border border-slate-800 p-8 shadow-2xl">
    <div class="flex items-center gap-3 mb-6">
      <span class="inline-flex items-center justify-center w-9 h-9 rounded-lg bg-gradient-to-br from-sky-400 to-indigo-600 text-white font-bold text-sm">{safe_badge}</span>
      <div>
        <div class="text-xs uppercase tracking-widest text-sky-400 font-mono">{safe_brand} // Secure Proposal Portal</div>
        <div class="text-sm font-bold text-slate-200">{safe_client} 御中 専用プレゼンテーション</div>
      </div>
    </div>
    {error_block}
    <form method="POST" action="/p/{safe_pres_id}/auth" class="space-y-4">
      <div>
        <label class="block text-xs text-slate-400 mb-1">閲覧用ID (Viewer ID)</label>
        <input type="text" name="viewer_id" required autocomplete="username"
          class="w-full px-3.5 py-2.5 rounded-lg bg-slate-800 border border-slate-700 text-sm text-white focus:outline-none focus:border-sky-400" />
      </div>
      <div>
        <label class="block text-xs text-slate-400 mb-1">パスワード (Password)</label>
        <input type="password" name="password" required autocomplete="current-password"
          class="w-full px-3.5 py-2.5 rounded-lg bg-slate-800 border border-slate-700 text-sm text-white focus:outline-none focus:border-sky-400" />
      </div>
      <button type="submit"
        class="w-full py-2.5 rounded-lg bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold text-sm transition">
        プレゼンテーションを表示
      </button>
    </form>
  </div>
</body>
</html>"""


_GENERATION_PHASE_LABELS_JS = """{
  queued: "生成キューに登録しました",
  knowledge_search: "社内ナレッジ（過去提案・事例）を検索中",
  managed_agents: "Managed Agents API（Antigravity ハーネス）が提案構成を設計中",
  managed_agents_repair: "生成された構成データをスキーマに整形中",
  gemini_fast: "高速生成モード（gemini-3.8-flash）に切り替えて生成中",
  deterministic_template: "テンプレートで即時生成中",
  rendering: "HTML5スライドをレンダリング・公開中",
  ready: "完成しました。ページを切り替えています…",
  failed: "生成に失敗しました"
}"""


def _render_generating_html(
    presentation_id: str,
    client_name: str = "Client",
    proposal_title: str = "",
    generation_phase: str = "queued",
    requested_at: str = "",
) -> str:
    """Interim page shown while the deck is generated; polls /p/{id}/status and reloads when ready."""
    safe_pres_id = html.escape(presentation_id, quote=True)
    safe_client = html.escape(client_name, quote=True)
    safe_title = html.escape(proposal_title, quote=True)
    safe_brand = html.escape(_get_brand_name(), quote=True)
    safe_badge = html.escape(_get_brand_badge(), quote=True)
    safe_phase = html.escape(generation_phase or "queued", quote=True)
    safe_requested = html.escape(requested_at or "", quote=True)
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="robots" content="noindex, nofollow" />
  <title>生成中 | {safe_client} 御中 提案プレゼンテーション - {safe_brand}</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-slate-950 text-slate-100 flex items-center justify-center p-4"
      data-presentation-id="{safe_pres_id}" data-generation-phase="{safe_phase}" data-requested-at="{safe_requested}">
  <div class="w-full max-w-xl rounded-2xl bg-slate-900/90 border border-slate-800 p-8 shadow-2xl">
    <div class="flex items-center gap-3 mb-6">
      <span class="inline-flex items-center justify-center w-9 h-9 rounded-lg bg-gradient-to-br from-sky-400 to-indigo-600 text-white font-bold text-sm">{safe_badge}</span>
      <div>
        <div class="text-xs uppercase tracking-widest text-sky-400 font-mono">{safe_brand} // Secure Proposal Portal</div>
        <div class="text-sm font-bold text-slate-200">{safe_client} 御中 専用プレゼンテーション</div>
      </div>
    </div>
    <div class="flex items-center gap-4 mb-5">
      <span class="relative flex h-12 w-12 shrink-0">
        <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-sky-400 opacity-30"></span>
        <span class="relative inline-flex rounded-full h-12 w-12 bg-sky-500/20 border border-sky-400/60 items-center justify-center">
          <svg class="animate-spin h-6 w-6 text-sky-300" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg>
        </span>
      </span>
      <div>
        <h1 class="text-lg font-bold text-white">AIが提案プレゼンテーションを生成しています</h1>
        <p class="text-xs text-slate-400 mt-1">{safe_title}</p>
      </div>
    </div>
    <div class="rounded-xl bg-slate-800/70 border border-slate-700 p-4 mb-5">
      <div class="text-[11px] uppercase tracking-wider text-slate-400 font-mono mb-1">Current step</div>
      <div id="phase-label" class="text-sm font-semibold text-sky-200">生成状況を確認しています…</div>
      <div class="mt-2 h-1.5 w-full rounded-full bg-slate-700 overflow-hidden"><div id="phase-bar" class="h-full bg-gradient-to-r from-sky-400 to-indigo-500 transition-all duration-700" style="width: 8%"></div></div>
      <div class="mt-2 text-[11px] text-slate-500 font-mono">経過時間: <span id="elapsed">0</span> 秒 / 自動更新 5 秒ごと</div>
    </div>
    <ol class="text-xs text-slate-300 space-y-1.5 mb-5 list-decimal list-inside">
      <li>社内ナレッジ検索（過去提案・導入事例）</li>
      <li>Managed Agents API（Antigravity ハーネス）による6枚構成の設計 <span class="text-slate-500">— 時間予算超過時は gemini-3.8-flash 高速生成へ自動切替</span></li>
      <li>HTML5スライドのレンダリングと非公開 Cloud Storage への公開</li>
    </ol>
    <p class="text-xs text-slate-400 leading-relaxed">
      通常1〜5分（最長でも約10分）で完成し、<strong class="text-slate-200">完成すると自動的にこの画面が提案ページへ切り替わります</strong>。
      このページを閉じても、同じURL・閲覧用ID・パスワードで後からご覧いただけます。
    </p>
    <div id="error-box" class="hidden mt-4 p-3 rounded-lg bg-rose-500/20 border border-rose-400/40 text-rose-200 text-xs"></div>
    <div class="mt-5 flex items-center justify-between">
      <span class="text-[11px] text-slate-500 font-mono">ID: {safe_pres_id}</span>
      <button onclick="location.reload()" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs text-slate-200">今すぐ再読み込み</button>
    </div>
  </div>
  <script>
    (function () {{
      var labels = {_GENERATION_PHASE_LABELS_JS};
      var order = ["queued", "knowledge_search", "managed_agents", "managed_agents_repair", "gemini_fast", "deterministic_template", "rendering", "ready"];
      var body = document.body;
      var presentationId = body.getAttribute("data-presentation-id");
      var requestedAt = Date.parse(body.getAttribute("data-requested-at") || "") || Date.now();
      var phaseLabel = document.getElementById("phase-label");
      var phaseBar = document.getElementById("phase-bar");
      var elapsedEl = document.getElementById("elapsed");
      var errorBox = document.getElementById("error-box");
      function setPhase(phase) {{
        var key = (phase || "queued").split(":")[0];
        phaseLabel.textContent = labels[key] || ("処理中: " + key);
        var idx = order.indexOf(key);
        var pct = idx < 0 ? 15 : Math.max(8, Math.round(((idx + 1) / order.length) * 100));
        phaseBar.style.width = pct + "%";
      }}
      setPhase(body.getAttribute("data-generation-phase"));
      setInterval(function () {{
        elapsedEl.textContent = Math.max(0, Math.round((Date.now() - requestedAt) / 1000));
      }}, 1000);
      var failures = 0;
      function poll() {{
        fetch("/p/" + encodeURIComponent(presentationId) + "/status?ts=" + Date.now(), {{ credentials: "same-origin", cache: "no-store" }})
          .then(function (r) {{
            if (r.status === 401) {{ location.reload(); return null; }}
            if (!r.ok) {{ throw new Error("HTTP " + r.status); }}
            return r.json();
          }})
          .then(function (data) {{
            if (!data) {{ return; }}
            failures = 0;
            if (data.generation_requested_at) {{
              var parsed = Date.parse(data.generation_requested_at);
              if (!isNaN(parsed)) {{ requestedAt = parsed; }}
            }}
            setPhase(data.generation_phase);
            if (data.generation_status === "ready") {{
              phaseLabel.textContent = labels.ready;
              phaseBar.style.width = "100%";
              setTimeout(function () {{ location.reload(); }}, 600);
            }} else if (data.generation_status === "failed") {{
              errorBox.textContent = "生成に失敗しました。お手数ですが担当者までご連絡ください。(" + (data.generation_error || "") + ")";
              errorBox.classList.remove("hidden");
            }}
          }})
          .catch(function (err) {{
            failures += 1;
            if (failures >= 6) {{
              errorBox.textContent = "状態の取得に失敗しています。ネットワークをご確認のうえ、再読み込みしてください。(" + err + ")";
              errorBox.classList.remove("hidden");
            }}
          }});
      }}
      poll();
      setInterval(poll, 5000);
    }})();
  </script>
</body>
</html>"""


def _render_failed_html(presentation_id: str, client_name: str = "Client", error: str = "") -> str:
    safe_pres_id = html.escape(presentation_id, quote=True)
    safe_client = html.escape(client_name, quote=True)
    safe_brand = html.escape(_get_brand_name(), quote=True)
    safe_error = html.escape(error[:200], quote=True)
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>生成エラー | {safe_client} 御中 - {safe_brand}</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="min-h-screen bg-slate-950 text-slate-100 flex items-center justify-center p-4">
<div class="w-full max-w-md rounded-2xl bg-slate-900/90 border border-rose-900 p-8 shadow-2xl">
<h1 class="text-lg font-bold text-rose-200 mb-2">プレゼンテーションの生成に失敗しました</h1>
<p class="text-xs text-slate-300">お手数ですが、提案担当者までご連絡ください。担当者はチャットで「{safe_pres_id} の生成状況を教えて」と依頼すると自動復旧できます。</p>
<p class="mt-3 text-[11px] text-slate-500 font-mono">{safe_error}</p>
</div></body></html>"""


def _authenticate_request(
    doc: dict[str, Any], presentation_id: str, request: Request
) -> tuple[str | None, str]:
    """Returns (viewer_id, auth_method) using Basic auth first, then the signed session cookie."""
    pw_version = str(doc.get("password_hash", ""))
    cookie_name = _get_cookie_name(presentation_id)
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("basic "):
        try:
            encoded = auth_header.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
            username_clean = username.strip()
            if hmac.compare_digest(
                username_clean, str(doc.get("viewer_id", "")).strip()
            ) and verify_pbkdf2_password(
                password,
                pw_version,
                str(doc.get("password_salt", "")),
            ):
                return username_clean, "basic_auth"
        except Exception:
            pass

    cookie_token = request.cookies.get(cookie_name)
    if cookie_token:
        verified_viewer = verify_session_token(
            cookie_token,
            presentation_id,
            expected_password_version=pw_version,
        )
        if verified_viewer and verified_viewer == str(doc.get("viewer_id", "")).strip():
            return verified_viewer, "session_cookie"
    return None, ""


def _generation_status_of(doc: dict[str, Any]) -> str:
    status = str(doc.get("generation_status") or "").lower()
    if status:
        return status
    return "ready"


@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/p/{presentation_id}/status")
def presentation_status(presentation_id: str, request: Request) -> Response:
    """Authenticated JSON status used by the interim page to auto-switch when the deck is ready."""
    doc = _get_firestore_doc(presentation_id)
    if not doc:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if _is_revoked_or_inactive(doc):
        return JSONResponse({"error": "revoked_or_expired"}, status_code=403)
    viewer, _method = _authenticate_request(doc, presentation_id, request)
    if not viewer:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    payload = {
        "presentation_id": presentation_id,
        "generation_status": _generation_status_of(doc),
        "generation_phase": str(doc.get("generation_phase") or ""),
        "generation_detail": str(doc.get("generation_detail") or ""),
        "generation_engine": str(doc.get("generation_engine") or ""),
        "generation_engine_label": str(doc.get("generation_engine_label") or ""),
        "generation_requested_at": str(doc.get("generation_requested_at") or doc.get("created_at") or ""),
        "generation_error": str(doc.get("generation_error") or ""),
        "ready_at": str(doc.get("ready_at") or ""),
        "updated_at": str(doc.get("updated_at") or ""),
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store, private"})


@app.get("/p/{presentation_id}")
def view_presentation(presentation_id: str, request: Request) -> Response:
    doc = _get_firestore_doc(presentation_id)
    if not doc:
        return HTMLResponse(
            content="<h1>404 Not Found: 指定されたプレゼンテーションは存在しません。</h1>",
            status_code=404,
        )

    if _is_revoked_or_inactive(doc):
        return HTMLResponse(
            content="<h1>403 Forbidden: このプレゼンテーションの公開期限が終了しているか、無効化されています。</h1>",
            status_code=403,
        )

    pw_version = str(doc.get("password_hash", ""))
    cookie_name = _get_cookie_name(presentation_id)
    authenticated_viewer, auth_method = _authenticate_request(doc, presentation_id, request)

    if not authenticated_viewer:
        login_html = _render_login_html(
            presentation_id=presentation_id,
            client_name=doc.get("client_name", "Client"),
        )
        accept_hdr = request.headers.get("accept", "").lower()
        resp_headers = (
            {}
            if "text/html" in accept_hdr
            else {
                "WWW-Authenticate": f'Basic realm="{_get_brand_name()} Proposal Portal"'
            }
        )
        return HTMLResponse(
            content=login_html,
            status_code=401,
            headers=resp_headers,
        )

    # 3. Record Audit Log
    _record_access_log(presentation_id, authenticated_viewer, auth_method, request)

    common_headers = {
        "Cache-Control": "no-store, private",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }

    # 4. Interim pages while the background generation is running / failed
    gen_status = _generation_status_of(doc)
    if gen_status in ("generating", "queued"):
        response: Response = HTMLResponse(
            content=_render_generating_html(
                presentation_id=presentation_id,
                client_name=str(doc.get("client_name", "Client")),
                proposal_title=str(doc.get("proposal_title", "")),
                generation_phase=str(doc.get("generation_phase") or "queued"),
                requested_at=str(doc.get("generation_requested_at") or doc.get("created_at") or ""),
            ),
            status_code=200,
            headers=common_headers,
        )
    elif gen_status == "failed":
        response = HTMLResponse(
            content=_render_failed_html(
                presentation_id,
                str(doc.get("client_name", "Client")),
                str(doc.get("generation_error", "")),
            ),
            status_code=503,
            headers=common_headers,
        )
    else:
        # 5. Stream Private GCS HTML
        bucket_name = doc.get("gcs_bucket") or _get_bucket_name()
        blob_path = doc.get("gcs_blob_path") or f"presentations/{presentation_id}/index.html"
        try:
            html_bytes = _fetch_html_from_gcs(bucket_name, blob_path)
        except Exception:
            return HTMLResponse(
                content="<h1>403 Forbidden: プレゼンテーションファイルが削除されているかアクセスできません。</h1>",
                status_code=403,
            )
        response = Response(
            content=html_bytes,
            media_type="text/html; charset=utf-8",
            headers=common_headers,
        )

    if auth_method == "basic_auth":
        token = create_session_token(
            presentation_id,
            authenticated_viewer,
            password_version=pw_version,
        )
        response.set_cookie(
            key=cookie_name,
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=86400,
            path=f"/p/{presentation_id}",
        )
    return response


@app.post("/p/{presentation_id}/auth")
def authenticate_presentation(
    presentation_id: str,
    request: Request,
    viewer_id: str = Form(...),
    password: str = Form(...),
) -> Response:
    doc = _get_firestore_doc(presentation_id)
    if not doc:
        return HTMLResponse(content="<h1>404 Not Found</h1>", status_code=404)

    if _is_revoked_or_inactive(doc):
        return HTMLResponse(
            content="<h1>403 Forbidden: Expired or Revoked</h1>",
            status_code=403,
        )

    pw_version = str(doc.get("password_hash", ""))
    clean_vid = viewer_id.strip()
    if hmac.compare_digest(
        clean_vid, str(doc.get("viewer_id", "")).strip()
    ) and verify_pbkdf2_password(
        password,
        pw_version,
        str(doc.get("password_salt", "")),
    ):
        token = create_session_token(
            presentation_id,
            clean_vid,
            password_version=pw_version,
        )
        cookie_name = _get_cookie_name(presentation_id)
        redirect = RedirectResponse(
            url=f"/p/{presentation_id}",
            status_code=303,
        )
        redirect.set_cookie(
            key=cookie_name,
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=86400,
            path=f"/p/{presentation_id}",
        )
        return redirect

    login_html = _render_login_html(
        presentation_id=presentation_id,
        client_name=doc.get("client_name", "Client"),
        error_message="閲覧用IDまたはパスワードが正しくありません。",
    )
    return HTMLResponse(content=login_html, status_code=401)
