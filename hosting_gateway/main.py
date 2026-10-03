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
from fastapi.responses import HTMLResponse, RedirectResponse

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
    global _EPHEMERAL_SECRET
    env_secret = os.environ.get("GATEWAY_SESSION_SECRET")
    if env_secret:
        return env_secret.encode("utf-8")
    if _EPHEMERAL_SECRET is None:
        logger.warning(
            "GATEWAY_SESSION_SECRET not set; generating ephemeral instance-isolated secret."
        )
        _EPHEMERAL_SECRET = secrets.token_hex(32).encode("utf-8")
    return _EPHEMERAL_SECRET


def verify_pbkdf2_password(
    password: str, expected_hash_hex: str, salt_hex: str
) -> bool:
    """Verifies a plaintext password against PBKDF2-HMAC-SHA256 hash and salt."""
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            120_000,
        )
        return hmac.compare_digest(dk.hex(), expected_hash_hex)
    except Exception:
        return False


def create_session_token(
    presentation_id: str, viewer_id: str, ttl_seconds: int = 86400
) -> str:
    """Creates an HMAC-SHA256 signed session token."""
    exp = int(time.time()) + ttl_seconds
    payload = f"{presentation_id}:{viewer_id}:{exp}"
    sig = hmac.new(
        _get_session_secret(), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    raw = f"{payload}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def verify_session_token(token: str, expected_presentation_id: str) -> str | None:
    """Verifies an HMAC-SHA256 signed session token and returns viewer_id if valid."""
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        parts = decoded.split(":")
        if len(parts) != 4:
            return None
        presentation_id, viewer_id, exp_str, sig = parts
        if presentation_id != expected_presentation_id:
            return None
        if int(exp_str) < int(time.time()):
            return None
        payload = f"{presentation_id}:{viewer_id}:{exp_str}"
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


@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


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

    authenticated_viewer: str | None = None
    auth_method = ""

    auth_header = request.headers.get("authorization", "")
    cookie_name = _get_cookie_name(presentation_id)
    if auth_header.lower().startswith("basic "):
        try:
            encoded = auth_header.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
            if hmac.compare_digest(
                username, str(doc.get("viewer_id", ""))
            ) and verify_pbkdf2_password(
                password,
                str(doc.get("password_hash", "")),
                str(doc.get("password_salt", "")),
            ):
                authenticated_viewer = username
                auth_method = "basic_auth"
        except Exception:
            authenticated_viewer = None
    else:
        cookie_token = request.cookies.get(cookie_name)
        if cookie_token:
            verified_viewer = verify_session_token(cookie_token, presentation_id)
            if verified_viewer and verified_viewer == doc.get("viewer_id"):
                authenticated_viewer = verified_viewer
                auth_method = "session_cookie"

    if not authenticated_viewer:
        login_html = _render_login_html(
            presentation_id=presentation_id,
            client_name=doc.get("client_name", "Client"),
        )
        return HTMLResponse(
            content=login_html,
            status_code=401,
            headers={
                "WWW-Authenticate": f'Basic realm="{_get_brand_name()} Proposal Portal"'
            },
        )

    _record_access_log(presentation_id, authenticated_viewer, auth_method, request)

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
        headers={
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        },
    )
    if auth_method == "basic_auth":
        token = create_session_token(presentation_id, authenticated_viewer)
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

    if hmac.compare_digest(
        viewer_id.strip(), str(doc.get("viewer_id", ""))
    ) and verify_pbkdf2_password(
        password,
        str(doc.get("password_hash", "")),
        str(doc.get("password_salt", "")),
    ):
        token = create_session_token(presentation_id, viewer_id.strip())
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
