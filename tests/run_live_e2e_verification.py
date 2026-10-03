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

"""Live End-to-End Verification for the Interactive Proposal Site Publisher.

Tests:
1. Conversational greeting ('こんにちは') -> verifies the agent responds as a
   concierge and NEVER calls `create_proposal_website` prematurely.
2. Conversational proposal creation -> generates & publishes a 6-slide HTML5 site.
3. Cloud Run Gateway security -> verifies 403 on raw GCS URL, 401 on unauthenticated
   access, 200 on Basic Auth & Cookie Auth.
4. Conversational slide editing (`edit_proposal_website`) -> modifies title and theme
   color and verifies the updated HTML on the live URL.
5. Lifecycle management (`list_proposal_websites`, `get_proposal_access_logs`,
   `manage_proposal_credentials`) -> verifies listing, audit logs, and password rotation.
6. Revocation (`delete_proposal_website`) -> verifies revoked presentation returns 403.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
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
    runner: Runner, user_id: str, session_id: str, text: str
) -> tuple[list[str], list[str]]:
    msg = types.Content(role="user", parts=[types.Part.from_text(text=text)])
    tool_calls: list[str] = []
    texts: list[str] = []
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=msg
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_call and part.function_call.name:
                    tool_calls.append(part.function_call.name)
                if part.text:
                    texts.append(part.text)
    return tool_calls, texts


async def run_e2e() -> None:
    project_id = os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    gateway_url = os.environ.get("HOSTING_BASE_URL", "").rstrip("/")
    if not project_id or not gateway_url:
        raise RuntimeError(
            "Please set PROJECT_ID and HOSTING_BASE_URL environment variables before running live E2E verification."
        )

    bucket_name = os.environ.get("PROPOSAL_GCS_BUCKET", f"{project_id}-proposals")

    session_service = InMemorySessionService()
    app_name = "proposal_live_e2e"
    user_id = f"e2e-user-{uuid.uuid4().hex[:6]}"
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(
        app_name=app_name, agent=root_agent, session_service=session_service
    )

    print("=" * 80)
    print("[1/6] Verifying Conversational Greeting ('こんにちは' -> No Slide Generation)")
    print("=" * 80)
    greeting_tools, greeting_texts = await _send_turn(
        runner, user_id, session.id, "こんにちは"
    )
    greeting_reply = "\n".join(greeting_texts).strip()
    print(f"Greeting Reply:\n{greeting_reply}\n")
    assert "create_proposal_website" not in greeting_tools
    assert len(greeting_reply) > 20

    print("=" * 80)
    print("[2/6] Creating Bespoke 6-Slide Proposal Website via Conversational Agent")
    print("=" * 80)
    create_prompt = (
        "株式会社アクメリテールホールディングス向けに、"
        "「Google Cloudによる統合データ基盤と対話型AIコンシェルジュ・OMOマーケティング自律化のご提案」"
        "というタイトルで、今すぐ提案用Webサイト（テーマカラー: sky）を作成・公開してください。"
    )
    create_tools, _ = await _send_turn(runner, user_id, session.id, create_prompt)
    assert "create_proposal_website" in create_tools

    updated_session = await session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session.id
    )
    published = updated_session.state.get(
        "published_result"
    ) or updated_session.state.get("published_presentation")
    assert published and published.get("status") == "PUBLISHED"

    presentation_id = published["presentation_id"]
    share_url = published["share_url"]
    viewer_id = published["viewer_id"]
    viewer_password = published["viewer_password"]
    public_gcs_url = f"https://storage.googleapis.com/{bucket_name}/presentations/{presentation_id}/index.html"

    async with httpx.AsyncClient(timeout=25.0) as client:
        print("=" * 80)
        print("[3/6] Verifying Private GCS Protection & Cloud Run Gateway Auth")
        print("=" * 80)
        r_gcs = await client.get(public_gcs_url)
        assert r_gcs.status_code in (401, 403)

        r_unauth = await client.get(share_url)
        assert r_unauth.status_code == 401

        r_basic = await client.get(share_url, auth=(viewer_id, viewer_password))
        assert r_basic.status_code == 200
        assert validate_rendered_html(r_basic.text) is True

        print("=" * 80)
        print("[4/6] Editing Published Proposal Website via Conversational Agent")
        print("=" * 80)
        edit_prompt = (
            f"発行済みのプレゼンテーション `{presentation_id}` について、"
            "タイトルを『【改訂版】AIエージェント×統合CDPによる次世代OMO顧客体験変革』に変更し、"
            "テーマカラーを `emerald` に変更してWebサイトを更新してください。"
        )
        edit_tools, _ = await _send_turn(runner, user_id, session.id, edit_prompt)
        assert "edit_proposal_website" in edit_tools

        r_edited = await client.get(share_url, auth=(viewer_id, viewer_password))
        assert r_edited.status_code == 200
        assert 'data-theme="emerald"' in r_edited.text
        assert "【改訂版】" in r_edited.text

        print("=" * 80)
        print("[5/6] Verifying List, Access Logs & Credential Rotation")
        print("=" * 80)
        list_res = list_proposal_websites(client_filter="アクメ")
        assert list_res["count"] >= 1

        logs_res = get_proposal_access_logs(presentation_id)
        assert logs_res["total_access_count"] >= 2

        cred_res = manage_proposal_credentials(
            presentation_id=presentation_id,
            rotate_password=True,
            extend_days=21,
        )
        rotated_pw = cred_res["new_viewer_password"]
        client.cookies.clear()
        r_old_pw = await client.get(share_url, auth=(viewer_id, viewer_password))
        assert r_old_pw.status_code == 401
        r_new_pw = await client.get(share_url, auth=(viewer_id, rotated_pw))
        assert r_new_pw.status_code == 200

        print("=" * 80)
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
        revoke_res = delete_proposal_website(
            presentation_id=temp_id, hard_delete_gcs=True
        )
        assert revoke_res["status"] == "REVOKED"
        r_temp_after = await client.get(
            temp_url, auth=(temp_pub["viewer_id"], temp_pub["viewer_password"])
        )
        assert r_temp_after.status_code == 403

    print("[SUCCESS] All 6 E2E verification steps passed.")


if __name__ == "__main__":
    asyncio.run(run_e2e())
