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

"""Remote multi-turn E2E against the deployed Agent Runtime (Reasoning Engine).

Reproduces the Gemini Enterprise conversation that previously stalled:
  turn 1: ask for a proposal site (outline consultation)
  turn 2: 「はい。skyで。」  -> must end with URL / viewer ID / password in the final text
then polls the Cloud Run gateway until the deck is ready and verifies the finished HTML is served.

Usage: uv run python ../tests/run_remote_multiturn_e2e.py   (from proposal_agent/)
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT_DIR = Path(__file__).resolve().parent.parent
RE_ID = os.environ.get("REASONING_ENGINE_ID", "").strip()
if not RE_ID:
    # Fall back to the id recorded by `agents-cli deploy`.
    _meta = Path(__file__).resolve().parent.parent / "proposal_agent" / "deployment_metadata.json"
    if _meta.exists():
        RE_ID = json.loads(_meta.read_text(encoding="utf-8")).get("remote_agent_runtime_id", "")
if not RE_ID:
    raise SystemExit("Set REASONING_ENGINE_ID=projects/<project-number>/locations/<region>/reasoningEngines/<id>")
PROJECT_ID = os.environ.get("PROJECT_ID") or RE_ID.split("/")[1]
CLIENT_NAME = os.environ.get("E2E_CLIENT_NAME", "株式会社サンプル商事")
TURN_1 = os.environ.get(
    "E2E_TURN_1",
    f"{CLIENT_NAME}向けに、統合CDPとAIコンシェルジュによる顧客体験変革の提案Webサイトを作成したいです。"
    "まず全6枚の構成案を提示してください。",
)
TURN_2 = os.environ.get("E2E_TURN_2", "はい。skyで。")
READY_TIMEOUT_S = int(os.environ.get("E2E_READY_TIMEOUT_S", "900"))


def _extract(events: list[dict[str, Any]]) -> dict[str, Any]:
    texts: list[str] = []
    tool_calls: list[str] = []
    tool_results: list[dict[str, Any]] = []
    errors: list[str] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("error_message") or ev.get("error_code"):
            errors.append(str(ev.get("error_message") or ev.get("error_code")))
        content = ev.get("content") or {}
        for part in content.get("parts") or []:
            if part.get("text"):
                texts.append(part["text"])
            fc = part.get("function_call")
            if fc:
                tool_calls.append(str(fc.get("name")))
            fr = part.get("function_response")
            if fr:
                tool_results.append({"name": fr.get("name"), "response": fr.get("response")})
    return {
        "final_text": "\n".join(texts).strip(),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "errors": errors,
        "event_count": len(events),
    }


def _run_turn(re_obj: Any, user_id: str, session_id: str | None, message: str) -> tuple[list[dict[str, Any]], float]:
    kwargs: dict[str, Any] = {"user_id": user_id, "message": message}
    if session_id:
        kwargs["session_id"] = session_id
    t0 = time.time()
    events = list(re_obj.stream_query(**kwargs))
    return events, round(time.time() - t0, 1)


def main() -> int:
    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=PROJECT_ID, location="us-central1")
    re_obj = agent_engines.get(RE_ID)
    user_id = f"remote-e2e-{int(time.time())}"

    session_id: str | None = None
    try:
        session = re_obj.create_session(user_id=user_id)
        session_id = session.get("id") if isinstance(session, dict) else getattr(session, "id", None)
        print(f"[session] created {session_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"[session] create_session unavailable ({exc}); using implicit session")

    print(f"\n=== Turn 1 ({len(TURN_1)} chars) ===")
    ev1, t1 = _run_turn(re_obj, user_id, session_id, TURN_1)
    r1 = _extract(ev1)
    print(f"[turn1] {t1}s tools={r1['tool_calls']} errors={r1['errors']}")
    print(r1["final_text"][:1500])
    if session_id is None:
        # Try to recover the session id from the first event so turn 2 shares context.
        for ev in ev1:
            if isinstance(ev, dict) and ev.get("session_id"):
                session_id = ev["session_id"]
                break
        print(f"[session] inferred {session_id}")

    print(f"\n=== Turn 2: {TURN_2} ===")
    ev2, t2 = _run_turn(re_obj, user_id, session_id, TURN_2)
    r2 = _extract(ev2)
    print(f"[turn2] {t2}s tools={r2['tool_calls']} errors={r2['errors']}")
    print(r2["final_text"][:3000])

    created: dict[str, Any] | None = None
    for tr in r2["tool_results"]:
        if tr.get("name") == "create_proposal_website" and isinstance(tr.get("response"), dict):
            created = tr["response"]
            created = created.get("result", created) if isinstance(created, dict) else created
    assert "create_proposal_website" in r2["tool_calls"], f"create tool not called; tools={r2['tool_calls']}"
    assert not r2["errors"], f"agent errors: {r2['errors']}"
    final_text = r2["final_text"]
    assert final_text, "turn 2 ended with EMPTY final text (the original GE stall symptom)"
    # Markdown links render as [url](url); never let the greedy match swallow "](".
    url_match = re.search(r"https://[^\s\]\)]+/p/(prop-\d{8}-[0-9a-f]{8})", final_text)
    assert url_match, "final text does not contain the share URL"
    pres_id = url_match.group(1)
    share_url = (created or {}).get("share_url") or url_match.group(0).rstrip(").,、。」")
    assert share_url.endswith(f"/p/{pres_id}"), f"share_url mismatch: {share_url}"
    viewer_id = (created or {}).get("viewer_id") or ""
    password = (created or {}).get("viewer_password") or ""
    if not viewer_id:
        m = re.search(r"(client-[a-z0-9-]+-[0-9a-f]{4})", final_text)
        viewer_id = m.group(1) if m else ""
    assert viewer_id and viewer_id in final_text, "viewer_id missing from final text"
    assert password and password in final_text, "password missing from final text"
    print(f"\n[issued] {share_url}  id={viewer_id}  pw={'*' * len(password)}  status={(created or {}).get('status')}")

    basic = base64.b64encode(f"{viewer_id}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}"}
    poll_log: list[dict[str, Any]] = []
    t_poll = time.time()
    status_json: dict[str, Any] = {}
    with httpx.Client(timeout=30, follow_redirects=False) as http:
        first = http.get(share_url, headers=headers)
        print(f"[gateway] first GET -> {first.status_code} interim={'生成しています' in first.text}")
        assert first.status_code == 200
        while time.time() - t_poll < READY_TIMEOUT_S:
            rs = http.get(f"{share_url}/status", headers=headers)
            status_json = rs.json() if rs.status_code == 200 else {"http": rs.status_code}
            poll_log.append({"t": round(time.time() - t_poll, 1), **{k: status_json.get(k) for k in ("generation_status", "generation_phase")}})
            print(f"[poll +{poll_log[-1]['t']}s] {status_json.get('generation_status')} / {status_json.get('generation_phase')}")
            if status_json.get("generation_status") in ("ready", "failed"):
                break
            time.sleep(10)
        assert status_json.get("generation_status") == "ready", f"deck not ready: {status_json}"
        final = http.get(share_url, headers=headers)
        assert final.status_code == 200 and 'data-slide-index="5"' in final.text, "finished deck not served"
        assert CLIENT_NAME in final.text
    # Negative check with a fresh client (no session cookie from the successful requests above).
    with httpx.Client(timeout=30, follow_redirects=False) as fresh:
        wrong = fresh.get(share_url, headers={"Authorization": "Basic " + base64.b64encode(f"{viewer_id}:nope".encode()).decode()})
        assert wrong.status_code == 401, f"wrong password must be rejected, got {wrong.status_code}"
        anon = fresh.get(f"{share_url}/status")
        assert anon.status_code == 401, f"anonymous status poll must be rejected, got {anon.status_code}"

    result = {
        "reasoning_engine": RE_ID,
        "session_id": session_id,
        "turn1": {"latency_s": t1, "tool_calls": r1["tool_calls"], "text_head": r1["final_text"][:600]},
        "turn2": {"latency_s": t2, "tool_calls": r2["tool_calls"], "text": final_text},
        "issued": {"presentation_id": pres_id, "share_url": share_url, "viewer_id": viewer_id, "viewer_password": password,
                   "tool_status": (created or {}).get("status"), "dispatch": (created or {}).get("generation_dispatch")},
        "generation": {"ready_after_s": round(time.time() - t_poll, 1), **status_json},
        "poll_log": poll_log,
        "finished_html_bytes": len(final.text),
    }
    out = ROOT_DIR / "remote_multiturn_e2e_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[SUCCESS] engine={status_json.get('generation_engine')} ready_after={result['generation']['ready_after_s']}s -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
