#!/usr/bin/env python3
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

"""Live End-to-End Verification against your deployed Google Cloud project:
(a) Conversational Greeting ('こんにちは' -> NO premature slide generation)
(b) Bespoke 6-slide HTML5 Website Generation & Private GCS + Cloud Run Hosting
(c) Live Website Editing via Natural Language ('edit_proposal_website')
(d) Lifecycle Management: Listing, Access Audit Logs, and Credential Rotation
(e) Revocation / Deletion ('delete_proposal_website' -> HTTP 403 Forbidden)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from google.adk.runners import InMemoryRunner
from google.genai import types

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "proposal_agent"))

from app.agent import (  # noqa: E402
    create_proposal_website,
    delete_proposal_website,
    get_proposal_access_logs,
    list_proposal_websites,
    manage_proposal_credentials,
    root_agent,
    validate_rendered_html,
)


async def _send_turn(
    runner: InMemoryRunner,
    user_id: str,
    session_id: str,
    text: str,
) -> tuple[list[str], list[str]]:
    """Sends a single user message turn and returns (tool_calls, text_responses)."""
    user_msg = types.Content(
        role="user",
        parts=[types.Part.from_text(text=text)],
    )
    tool_calls: list[str] = []
    texts: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_msg,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_call:
                    tool_calls.append(part.function_call.name)
                    print(f"  -> Tool Call: {part.function_call.name}")
                if part.function_response:
                    print(f"  <- Tool Response: {part.function_response.name}")
                if part.text:
                    texts.append(part.text)
    return tool_calls, texts


async def run_e2e() -> dict[str, Any]:
    project_id = os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    gateway_url = os.environ.get("HOSTING_BASE_URL", "").rstrip("/")
    if not project_id or not gateway_url:
        raise RuntimeError(
            "Please set PROJECT_ID and HOSTING_BASE_URL environment variables before running live E2E verification."
        )
    bucket_name = os.environ.get("PROPOSAL_GCS_BUCKET", f"{project_id}-proposals")

    runner = InMemoryRunner(agent=root_agent, app_name="proposal_live_e2e")
    user_id = "sales-concierge-tester"
    session = await runner.session_service.create_session(
        app_name="proposal_live_e2e",
        user_id=user_id,
    )

    # -------------------------------------------------------------------------
    # Step 1: Greeting Check ("こんにちは" MUST NOT generate slides)
    # -------------------------------------------------------------------------
    print("=" * 80)
    print("[1/6] Verifying Conversational Greeting ('こんにちは' -> No Slide Generation)")
    print("=" * 80)
    greeting_tools, greeting_texts = await _send_turn(
        runner, user_id, session.id, "こんにちは"
    )
    greeting_reply = "\n".join(greeting_texts).strip()
    print(f"Greeting Reply:\n{greeting_reply}\n")
    assert "create_proposal_website" not in greeting_tools, (
        f"Agent should NOT call create_proposal_website on 'こんにちは'! Called: {greeting_tools}"
    )
    assert len(greeting_reply) > 20, "Expected conversational greeting response."

    # -------------------------------------------------------------------------
    # Step 2: Bespoke 6-Slide HTML5 Generation & Publication via Chat
    # -------------------------------------------------------------------------
    print("=" * 80)
    print("[2/6] Creating Bespoke 6-Slide Proposal Website via Conversational Agent")
    print("=" * 80)
    create_prompt = (
        "株式会社アクメリテールホールディングス様向けに、"
        "店舗・EC・会員アプリの分断された顧客データをGoogle Cloud (BigQuery CDP + Gemini Enterprise Agent Platform + Gemini Enterprise) で統合し、"
        "対話型AIコンシェルジュとOMOマーケティング自律化を実現する6枚構成の提案プレゼンテーションWebサイトを、"
        "テーマカラー sky で作成・公開してください。"
    )
    create_tools, create_texts = await _send_turn(
        runner, user_id, session.id, create_prompt
    )
    assert "create_proposal_website" in create_tools, (
        f"Expected create_proposal_website tool call, got: {create_tools}"
    )

    updated_session = await runner.session_service.get_session(
        app_name="proposal_live_e2e",
        user_id=user_id,
        session_id=session.id,
    )
    published = updated_session.state.get("published_result")
    assert published, "published_result not found in session state!"

    presentation_id = published["presentation_id"]
    share_url = published["share_url"]
    viewer_id = published["viewer_id"]
    viewer_password = published["viewer_password"]
    print(f"[OK] Published presentation_id={presentation_id} at {share_url}")

    # -------------------------------------------------------------------------
    # Step 3: Verify Private GCS Block + Cloud Run Gateway Auth (Basic & Cookie)
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("[3/6] Verifying Private GCS Protection & Cloud Run Gateway Auth")
    print("=" * 80)
    public_gcs_url = f"https://storage.googleapis.com/{bucket_name}/presentations/{presentation_id}/index.html"
    async with httpx.AsyncClient(timeout=25.0) as client:
        r_gcs = await client.get(public_gcs_url)
        print(f"Direct public GCS URL -> HTTP {r_gcs.status_code}")
        assert r_gcs.status_code in (401, 403)

        r_unauth = await client.get(share_url)
        print(f"Unauthenticated GET {share_url} -> HTTP {r_unauth.status_code}")
        assert r_unauth.status_code == 401

        r_basic = await client.get(share_url, auth=(viewer_id, viewer_password))
        print(
            f"Valid Basic Auth GET {share_url} -> HTTP {r_basic.status_code} ({len(r_basic.text)} bytes)"
        )
        assert r_basic.status_code == 200
        assert validate_rendered_html(r_basic.text) is True
        assert 'data-layout="bento-executive-summary"' in r_basic.text
        assert 'data-layout="architecture-flow"' in r_basic.text

        auth_url = f"{gateway_url}/p/{presentation_id}/auth"
        r_login = await client.post(
            auth_url,
            data={"viewer_id": viewer_id, "password": viewer_password},
            follow_redirects=False,
        )
        assert r_login.status_code == 303
        cookie_name = f"{os.environ.get('PROPOSAL_COOKIE_PREFIX', 'proposal_session')}_{presentation_id}"
        session_cookie_val = r_login.cookies.get(cookie_name)
        assert session_cookie_val

        r_cookie = await client.get(
            share_url, cookies={cookie_name: session_cookie_val}
        )
        assert r_cookie.status_code == 200
        client.cookies.clear()

        # ---------------------------------------------------------------------
        # Step 4: Live Edit via Conversational Turn ('edit_proposal_website')
        # ---------------------------------------------------------------------
        print("\n" + "=" * 80)
        print("[4/6] Editing Published Proposal Website via Conversational Agent")
        print("=" * 80)
        edit_prompt = (
            f"発行済みのプレゼンテーション `{presentation_id}` について、"
            "タイトルを『【改訂版】AIエージェント×統合CDPによる次世代OMO顧客体験変革』に変更し、"
            "テーマカラーを `emerald` に変更してWebサイトを更新してください。"
        )
        edit_tools, edit_texts = await _send_turn(
            runner, user_id, session.id, edit_prompt
        )
        assert "edit_proposal_website" in edit_tools, (
            f"Expected edit_proposal_website tool call, got: {edit_tools}"
        )

        r_edited = await client.get(share_url, auth=(viewer_id, viewer_password))
        print(
            f"GET Edited Presentation {share_url} -> HTTP {r_edited.status_code} ({len(r_edited.text)} bytes)"
        )
        assert r_edited.status_code == 200
        assert validate_rendered_html(r_edited.text) is True
        assert 'data-theme="emerald"' in r_edited.text
        assert "【改訂版】" in r_edited.text

        # ---------------------------------------------------------------------
        # Step 5: Lifecycle Management (List, Access Logs, Credential Rotation)
        # ---------------------------------------------------------------------
        print("\n" + "=" * 80)
        print("[5/6] Verifying List, Access Logs & Credential Rotation")
        print("=" * 80)
        list_res = list_proposal_websites(client_filter="アクメ")
        assert list_res["count"] >= 1
        print(f"Listed {list_res['count']} matching presentation(s).")

        logs_res = get_proposal_access_logs(presentation_id)
        print(f"Access logs count for {presentation_id}: {logs_res['total_access_count']}")
        assert logs_res["total_access_count"] >= 2

        cred_res = manage_proposal_credentials(
            presentation_id=presentation_id,
            rotate_password=True,
            extend_days=21,
        )
        rotated_pw = cred_res["new_viewer_password"]
        assert rotated_pw != viewer_password

        # Old password must now be rejected with 401
        client.cookies.clear()
        r_old_pw = await client.get(share_url, auth=(viewer_id, viewer_password))
        assert r_old_pw.status_code == 401
        # New rotated password must succeed with 200
        r_new_pw = await client.get(share_url, auth=(viewer_id, rotated_pw))
        assert r_new_pw.status_code == 200
        published["viewer_password"] = rotated_pw
        published["expires_at"] = cred_res["expires_at"]
        print(f"[OK] Password rotation verified on live Cloud Run Gateway.")

        # ---------------------------------------------------------------------
        # Step 6: Revoke / Delete a Disposable Test Presentation -> HTTP 403
        # ---------------------------------------------------------------------
        print("\n" + "=" * 80)
        print("[6/6] Verifying Delete / Revoke Blocks External Access (HTTP 403)")
        print("=" * 80)
        temp_pub = create_proposal_website(
            client_name="テスト削除検証株式会社",
            proposal_title="一時検証用プレゼンテーション（削除テスト）",
            proposal_brief="公開停止機能のエンドツーエンド検証用デッキ",
            theme_color="violet",
        )
        temp_id = temp_pub["presentation_id"]
        temp_url = temp_pub["share_url"]
        r_temp_before = await client.get(
            temp_url, auth=(temp_pub["viewer_id"], temp_pub["viewer_password"])
        )
        assert r_temp_before.status_code == 200

        revoke_res = delete_proposal_website(
            presentation_id=temp_id, hard_delete_gcs=True
        )
        assert revoke_res["status"] == "REVOKED"

        r_temp_after = await client.get(
            temp_url, auth=(temp_pub["viewer_id"], temp_pub["viewer_password"])
        )
        print(
            f"Revoked presentation {temp_url} -> HTTP {r_temp_after.status_code} (expected 403)"
        )
        assert r_temp_after.status_code == 403

    # -------------------------------------------------------------------------
    # Step 7: Remote Agent Runtime (Reasoning Engine) stream_query
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("[7/7] Verifying Deployed Agent Runtime stream_query")
    print("=" * 80)
    re_id = os.environ.get("REASONING_ENGINE_ID", "").strip()
    if not re_id:
        # Fall back to the id recorded by `agents-cli deploy`.
        meta_path = ROOT_DIR / "proposal_agent" / "deployment_metadata.json"
        if meta_path.exists():
            re_id = json.loads(meta_path.read_text(encoding="utf-8")).get("remote_agent_runtime_id", "")
    remote_events: list[Any] = []
    if not re_id:
        print("REASONING_ENGINE_ID is not set and no deployment_metadata.json was found; skipping the remote probe.")
    else:
        import vertexai
        from vertexai import agent_engines

        vertexai.init(project=project_id, location=re_id.split("/")[3] if re_id.count("/") >= 5 else "us-central1")
        re_obj = agent_engines.get(re_id)
        remote_events = list(
            re_obj.stream_query(user_id="live-re-probe", message="こんにちは")
        )
        print(f"Remote Reasoning Engine stream_query returned {len(remote_events)} event(s).")
        assert len(remote_events) >= 1, "Expected at least 1 event from remote stream_query!"

    summary_path = ROOT_DIR / "live_e2e_verification_result.json"
    summary_data = {
        "greeting_test": {
            "user_input": "こんにちは",
            "tool_calls_triggered": greeting_tools,
            "premature_slide_generation": "create_proposal_website" in greeting_tools,
            "agent_greeting_reply": greeting_reply,
            "remote_reasoning_engine_id": re_id,
            "remote_stream_query_events_count": len(remote_events),
        },
        "published_and_edited": published,
        "edit_test": {
            "edit_tools_triggered": edit_tools,
            "updated_theme": "emerald",
            "updated_title_verified_in_live_html": "【改訂版】" in r_edited.text,
        },
        "lifecycle_test": {
            "listed_presentations_count": list_res["count"],
            "access_logs_recorded": logs_res["total_access_count"],
            "old_password_http_status_after_rotation": r_old_pw.status_code,
            "new_password_http_status_after_rotation": r_new_pw.status_code,
            "revoked_presentation_id": temp_id,
            "revoked_presentation_http_status": r_temp_after.status_code,
        },
        "public_gcs_http_status": r_gcs.status_code,
        "unauthenticated_gateway_http_status": r_unauth.status_code,
        "basic_auth_gateway_http_status": r_basic.status_code,
        "cookie_auth_gateway_http_status": r_cookie.status_code,
        "html_byte_length": len(r_edited.text),
    }
    summary_path.write_text(
        json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n[SUCCESS] Saved full E2E verification report to {summary_path}")
    return summary_data


if __name__ == "__main__":
    asyncio.run(run_e2e())
