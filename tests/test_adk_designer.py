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

"""Unit tests for the ADK designer (`adk_designer.AdkDesigner`) that `freeform.run_pipeline` drives.

A scripted `BaseLlm` replaces Gemini and an in-memory store replaces GCS, so these tests pin the contract that
`freeform.run_pipeline` relies on: start()/wait(), one ADK session across the draft and review turns,
screenshots delivered as image parts, workspace permissions, cancellation and usage accounting.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import types as pytypes
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from pydantic import Field

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "proposal_agent"))

from google.adk.models import BaseLlm, LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402

from app import adk_designer  # noqa: E402
from app import deck_contract as dc  # noqa: E402
from app import freeform  # noqa: E402

PNG = freeform.placeholder_png(4, 4)
PREFIX = "staging/prop-test/r1/"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.generation = 0
        self.gens: dict[str, int] = {}

    def list_meta(self, prefix: str) -> dict[str, tuple[int, int]]:
        return {k[len(prefix):]: (len(v), self.gens[k]) for k, v in self.objects.items() if k.startswith(prefix)}

    def read_all(self, prefix: str, max_files: int = 250, max_bytes: int = 40_000_000) -> dict[str, bytes]:
        return {k[len(prefix):]: v for k, v in self.objects.items() if k.startswith(prefix)}

    def read(self, path: str) -> bytes | None:
        return self.objects.get(path)

    def write(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.generation += 1
        self.objects[path] = data
        self.gens[path] = self.generation

    def delete(self, path: str) -> None:
        self.objects.pop(path, None)
        self.gens.pop(path, None)

    def write_many(self, prefix: str, files: dict[str, bytes]) -> None:
        for rel, data in files.items():
            self.write(prefix + rel, data)



class ScriptedLlm(BaseLlm):
    """Replays scripted model responses in order and records every request it receives."""

    script: list[dict[str, Any]] = Field(default_factory=list)
    requests: list[Any] = Field(default_factory=list)

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
        step = self.script.pop(0)
        if step.get("delay"):
            await asyncio.sleep(step["delay"])
        yield LlmResponse(content=types.Content(role="model", parts=step["parts"]), usage_metadata=step.get("usage"))


def call(name: str, **args: Any) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def say(text: str) -> types.Part:
    return types.Part(text=text)


def usage(prompt: int, output: int, cached: int = 0, thought: int = 0, image: int = 0) -> Any:
    details = [types.ModalityTokenCount(modality=types.MediaModality.IMAGE, token_count=image)] if image else None
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt,
        candidates_token_count=output,
        cached_content_token_count=cached,
        thoughts_token_count=thought,
        total_token_count=prompt + output + thought,
        prompt_tokens_details=details,
    )


def step(*parts: types.Part, delay: float = 0.0, image: int = 0) -> dict[str, Any]:
    return {"parts": list(parts), "delay": delay, "usage": usage(1000, 100, cached=400, thought=20, image=image)}


def deck(slides: list[tuple[str, str]]) -> str:
    body = "".join(f'<section class="pd-slide" data-pd-title="{t}"><h2>{t}</h2><p>{p}</p></section>' for t, p in slides)
    return (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>Acme</title></head>'
        f'<body><main id="pd-deck">{body}</main></body></html>'
    )


GOOD = [("表紙", "Acme Corp 様"), ("課題", "問い合わせの 6 割が定型"), ("提案", "Agent Runtime で自動化")]
OVERFLOW = [("表紙", "Acme Corp 様"), ("課題", "はみ出し"), ("提案", "Agent Runtime で自動化")]


def fake_render_factory(store: FakeStore, calls: list[str]):
    """Renders a build prefix: one screenshot per slide; slides whose text says はみ出し get an overflow error."""

    def render(run: freeform.Run, prefix: str) -> dict[str, Any]:
        calls.append(prefix)
        source = (store.read(prefix + "source.html") or b"").decode("utf-8")
        slides = []
        for s in dc.extract_slides(source):
            issues = [{"type": "overflow", "severity": "error", "detail": "403px"}] if "はみ出し" in s.text else []
            shot = base64.b64encode(hashlib.sha256(s.text.encode()).digest()).decode()
            slides.append({"index": s.index, "title": s.title, "issues": issues, "screenshot_b64": shot})
        return {"ok": True, "slide_count": len(slides), "slides": slides, "console_errors": [], "failed_requests": [],
                "csp_violations": [], "runtime_errors": [], "chart_errors": []}

    return render


class RecordingDesigner(adk_designer.AdkDesigner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.turns: list[adk_designer.AdkTurn] = []

    def start(self, prompt_input: Any, workspace: Any) -> adk_designer.AdkTurn:
        turn = super().start(prompt_input, workspace)
        self.turns.append(turn)
        return turn


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    # AdkDesigner sets these with setdefault; pinning them here keeps the process environment clean.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    store = FakeStore()
    phases: list[tuple[str, str]] = []
    run = freeform.Run(
        presentation_id="prop-test",
        run_id="r1",
        bucket="bkt",
        project_id="proj",
        budget=freeform.Budget(total=900, draft=540, rounds=2, review_turn=240, reserve=60),
        status_cb=lambda phase, detail: phases.append((phase, detail)),
    )
    renders: list[str] = []
    monkeypatch.setattr(freeform, "render_build", fake_render_factory(store, renders))
    designers: list[adk_designer.AdkDesigner] = []

    def make(script: list[dict[str, Any]]) -> tuple[RecordingDesigner, ScriptedLlm]:
        llm = ScriptedLlm(model="scripted-model", script=script)
        designer = RecordingDesigner("proj", store=store, model=llm)
        designers.append(designer)
        return designer, llm

    yield pytypes.SimpleNamespace(store=store, run=run, phases=phases, renders=renders, make=make)
    for designer in designers:
        designer.close()


def user_images(llm_request: Any) -> int:
    content = llm_request.contents[-1]
    assert content.role == "user"
    return sum(1 for part in content.parts or [] if part.inline_data is not None)


def function_response(llm_request: Any, name: str) -> dict[str, Any]:
    for content in reversed(llm_request.contents):
        for part in content.parts or []:
            if part.function_response is not None and part.function_response.name == name:
                return dict(part.function_response.response or {})
    raise AssertionError(f"no function response for {name}")


# ---------------------------------------------------------------------------
# Prompt and input adaptation
# ---------------------------------------------------------------------------
def test_instruction_is_placeholder_free_and_prompts_use_check_deck() -> None:
    assert "{" not in adk_designer.ADK_INSTRUCTION and "}" not in adk_designer.ADK_INSTRUCTION
    for prompt in (freeform.DRAFT_PROMPT, freeform.EDIT_PROMPT):
        assert "check_deck" in prompt and "python3" not in prompt
        assert "パッケージのインストール" not in prompt  # the ADK agent has no sandbox to install into
    assert "brief.md と knowledge.md にあるものだけ" in freeform.DRAFT_PROMPT  # grounding rule


def test_to_content_turns_review_input_into_text_and_image_parts() -> None:
    review = [{"type": "user_input", "content": [
        {"type": "text", "text": "2 枚目を確認してください"},
        {"type": "image", "data": base64.b64encode(PNG).decode(), "mime_type": "image/png"},
    ]}]
    content = adk_designer.to_content(review)
    assert content.role == "user" and content.parts[0].text == "2 枚目を確認してください"
    assert content.parts[1].inline_data.mime_type == "image/png" and content.parts[1].inline_data.data == PNG
    assert adk_designer.to_content(freeform.DRAFT_PROMPT).parts[0].text == freeform.DRAFT_PROMPT
    assert adk_designer.to_content([]).parts[0].text  # never sends an empty message


def test_accumulate_usage_keys() -> None:
    totals: dict[str, Any] = {}
    adk_designer._accumulate_usage(totals, usage(100, 10, cached=40, thought=5, image=30))
    adk_designer._accumulate_usage(totals, usage(200, 20))
    assert totals["total_input_tokens"] == 300 and totals["total_output_tokens"] == 30
    assert totals["total_cached_tokens"] == 40 and totals["total_thought_tokens"] == 5
    assert totals["total_tokens"] == 335
    assert totals["input_tokens_by_modality"] == [{"modality": "image", "tokens": 30}]
    # freeform reads the per-turn usage dict of an ADK turn.
    assert freeform._usage_of(pytypes.SimpleNamespace(usage=totals))["image_input_tokens"] == 30


# ---------------------------------------------------------------------------
# Workspace (the GCS staging prefix seen as /workspace/job)
# ---------------------------------------------------------------------------
def test_workspace_path_rules() -> None:
    normalize = adk_designer.Workspace.normalize
    assert normalize("/workspace/job/deck/index.html", write=True) == "deck/index.html"
    assert normalize("workspace/job/input/brief.md") == "input/brief.md"
    assert normalize("deck/./charts/../assets/a.svg", write=True) == "deck/assets/a.svg"
    assert normalize("/workspace/job") == "" and normalize("/workspace/job/") == ""
    for outside in ("../x", "/workspace/job/../etc/passwd", "deck/../../x"):
        with pytest.raises(ValueError):
            normalize(outside, write=True)
    with pytest.raises(ValueError):
        normalize("/workspace/job/input/brief.md", write=True)  # input/ is read-only
    with pytest.raises(ValueError):
        normalize("/workspace/job/deck", write=True)  # a folder is not a file
    with pytest.raises(ValueError):
        normalize("build/r1/index.html")  # pipeline-internal builds are not part of the workspace


def test_workspace_file_operations() -> None:
    store = FakeStore()
    ws = adk_designer.Workspace(store, PREFIX.rstrip("/"))
    assert ws.prefix == PREFIX
    store.write(PREFIX + "input/brief.md", "架空の物流会社".encode())
    store.write(PREFIX + "build/r1/index.html", b"x")
    store.write(PREFIX + "deck/assets/ai/hero.png", PNG)

    assert ws.write("/workspace/job/deck/index.html", "<html>")["status"] == "ok"
    broken_json = ws.write("deck/charts/a.json", "{oops")
    assert broken_json["status"] == "ok" and "JSON" in broken_json["warning"]
    assert ws.write("deck/assets/b.png", "x")["status"] == "error"  # no binary writes
    assert ws.write("deck/big.html", "a" * (adk_designer.MAX_WRITE_BYTES + 1))["status"] == "error"

    listing = [f["path"] for f in ws.list("/workspace/job")["files"]]
    assert "/workspace/job/input/brief.md" in listing and "/workspace/job/deck/index.html" in listing
    assert not any("/build/" in path for path in listing)
    assert ws.read("/workspace/job/input/brief.md")["content"] == "架空の物流会社"
    assert "バイナリ" in ws.read("deck/assets/ai/hero.png")["note"]
    assert ws.read("deck/missing.html")["status"] == "error" and ws.read("deck")["status"] == "error"

    assert ws.replace("deck/index.html", "<html>", '<html lang="ja">')["status"] == "ok"
    assert store.read(PREFIX + "deck/index.html") == b'<html lang="ja">'
    store.write(PREFIX + "deck/dup.html", b"aa")
    assert "2 か所" in ws.replace("deck/dup.html", "a", "b")["message"]
    assert ws.replace("deck/index.html", "zzz", "b")["status"] == "error"
    assert ws.delete("/workspace/job/deck/dup.html")["deleted"] and store.read(PREFIX + "deck/dup.html") is None
    assert ws.writes == 3  # index.html, a.json and the replacement; rejected writes do not count

    class NoDeleteStore(FakeStore):
        delete = None

    assert adk_designer.Workspace(NoDeleteStore(), "p").delete("deck/x.html")["status"] == "error"


def test_check_deck_uses_placeholders_for_requested_ai_images() -> None:
    store = FakeStore()
    ws = adk_designer.Workspace(store, PREFIX)
    hero = [("表紙", 'Acme<img src="assets/ai/hero.png" data-pd-ai-image alt="">'), ("課題", "定型"), ("提案", "x")]
    store.write(PREFIX + "deck/index.html", deck(hero).encode())
    store.write(PREFIX + "deck/image_requests.json", json.dumps([{"path": "assets/ai/hero.png", "prompt": "calm"}]).encode())
    result = ws.check()
    assert result["publishable"] is True and result["slide_count"] == 3
    assert result["slide_titles"] == ["表紙", "課題", "提案"] and "仮の画像" in result["note"]
    assert PREFIX + "deck/assets/ai/hero.png" not in store.objects  # the check never writes placeholders
    store.delete(PREFIX + "deck/image_requests.json")
    missing = ws.check()
    assert missing["publishable"] is False and any("hero.png" in e for e in missing["errors"])


def test_check_deck_reports_ungrounded_numbers_emails_and_work_files() -> None:
    store = FakeStore()
    ws = adk_designer.Workspace(store, PREFIX)
    store.write(PREFIX + "input/brief.md", "問い合わせ対応の 40% を自動化したい。連絡先 ops@example.com".encode())
    store.write(PREFIX + "input/knowledge.md", "過去事例: 工数 25％ 削減".encode())
    slides = [
        ("表紙", "Acme 様 ご提案（brief.md より）"),
        ("効果", "対応 40% 自動化・工数 25% 削減・満足度 37% 向上・処理時間 12% 短縮（試算）"),
        ("連絡先", "ops@example.com / sales@acme.example"),
    ]
    store.write(PREFIX + "deck/index.html", deck(slides).encode())
    result = ws.check()
    assert result["publishable"] is True  # grounding findings are warnings for the designer, not publish errors
    assert result["ungrounded"] == [
        "数値「37%」",
        "メールアドレス「sales@acme.example」",
        "作業用のファイル名・パス「brief.md」",
    ]
    assert "試算" in result["grounding"][0] and "37%" in result["grounding"][0]
    store.write(PREFIX + "deck/index.html", deck([("表紙", "Acme"), ("効果", "対応 40% 自動化"), ("次へ", "x")]).encode())
    clean = ws.check()
    assert clean["ungrounded"] == [] and clean["grounding"] == []


# ---------------------------------------------------------------------------
# Pipeline on the ADK engine
# ---------------------------------------------------------------------------
def test_pipeline_on_adk_engine_reviews_screenshots_in_the_same_session(env) -> None:
    designer, llm = env.make([
        # Draft turn
        step(call("list_files", directory="/workspace/job/input")),
        step(
            call("write_file", path="/workspace/job/deck/index.html", content=deck(OVERFLOW)),
            call("write_file", path="/workspace/job/deck/manifest.json", content='{"concept": "clean"}'),
        ),
        step(call("check_deck")),
        step(say("白基調のシンプルな構成で 3 枚作りました\nDRAFT_STATUS: DONE")),
        # Review round 1: three screenshots, slide 2 overflows
        step(call("replace_in_file", path="/workspace/job/deck/index.html", old_text="はみ出し", new_text="定型質問"), image=3300),
        step(say("2 枚目の文字を縮めました\nREVIEW_STATUS: FIXED"), image=3300),
        # Review round 2: only the changed slide is attached
        step(say("修正後のスライドを確認しました\nREVIEW_STATUS: APPROVED"), image=1100),
    ])
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"}, title="Acme")

    assert outcome.ok and outcome.chosen is not None
    assert outcome.chosen.number == 2 and outcome.chosen.layout_errors == 0
    assert [r["review_status"] for r in outcome.rounds] == ["FIXED", "APPROVED"]
    assert outcome.rounds[0]["files_changed"] is True and outcome.rounds[1]["files_changed"] is False
    assert outcome.review_summaries == ["2 枚目の文字を縮めました", "修正後のスライドを確認しました"]
    assert env.renders == [PREFIX + "build/r1/", PREFIX + "build/r2/"]
    assert not llm.script  # every scripted response was used

    # One ADK session carries the draft and both reviews.
    session_id = outcome.session_id
    assert session_id.startswith("adk-")
    assert outcome.turn_ids == [f"{session_id}-t1", f"{session_id}-t2", f"{session_id}-t3"]
    assert [t.session_id for t in designer.turns] == [session_id] * 3
    assert [t.status for t in designer.turns] == ["completed"] * 3
    assert dict(designer.turns[0].tool_calls) == {"list_files": 1, "write_file": 2, "check_deck": 1}
    assert designer.turns[0].llm_calls == 4 and designer.turns[1].tool_calls["replace_in_file"] == 1

    # The model saw the draft prompt, the contract check result and the screenshots as image parts.
    first_user = llm.requests[0].contents[0]
    assert first_user.parts[0].text == freeform.DRAFT_PROMPT
    assert function_response(llm.requests[3], "check_deck")["publishable"] is True
    assert user_images(llm.requests[4]) == 3
    assert user_images(llm.requests[6]) == 1
    history = json.dumps([c.model_dump(mode="json", exclude_none=True) for c in llm.requests[6].contents], ensure_ascii=False)
    assert "DRAFT_STATUS: DONE" in history and "REVIEW_STATUS: FIXED" in history  # earlier turns are in context

    # Usage is summed per turn.
    assert env.run.usage["total_tokens"] == 7 * 1120
    assert env.run.usage["total_cached_tokens"] == 7 * 400
    assert env.run.usage["image_input_tokens"] == 3300 * 2 + 1100
    assert outcome.rounds[0]["image_tokens"] == 6600 and outcome.rounds[1]["image_tokens"] == 1100
    stats = designer.turn_stats()[session_id]
    assert stats["turns"] == 3 and stats["writes"] == 3 and stats["bytes_written"] > 0


def test_edit_mode_reads_the_request_and_edits_the_seeded_deck(env) -> None:
    designer, llm = env.make([
        step(call("read_file", path="/workspace/job/input/edit_request.md")),
        step(call("replace_in_file", path="/workspace/job/deck/index.html", old_text="定型", new_text="定型（8 割）")),
        step(say("2 枚目の数値を更新しました\nDRAFT_STATUS: DONE")),
        step(say("REVIEW_STATUS: APPROVED"), image=1100),
    ])
    outcome = freeform.run_pipeline(
        env.run,
        env.store,
        designer,
        mode="edit",
        input_files={"edit_request.md": "2 枚目の数値を 8 割に", "brief.md": "x"},
        seed_files={"index.html": deck(GOOD).encode(), "manifest.json": b'{"concept": "clean"}'},
    )
    assert outcome.ok and outcome.chosen.number == 1
    assert "定型（8 割）" in outcome.chosen.build.files["source.html"].decode("utf-8")
    assert function_response(llm.requests[1], "read_file")["content"] == "2 枚目の数値を 8 割に"
    assert "edit_request.md" in llm.requests[0].contents[0].parts[0].text
    assert [r["review_status"] for r in outcome.rounds] == ["APPROVED"]


def test_wait_deadline_cancels_and_blocks_overlapping_turns(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adk_designer, "BUSY_WAIT_SECONDS", 0.2)
    designer, llm = env.make([
        step(call("write_file", path="/workspace/job/deck/index.html", content="<html>"), delay=2.0),
        step(say("not reached")),
    ])
    turn = designer.start(freeform.DRAFT_PROMPT, freeform.workspace_spec(env.run))
    with pytest.raises(RuntimeError):
        designer.start("もう一度", turn.session_id)  # the session is still busy
    final, timed_out = designer.wait(env.run, turn, 0.5, "freeform_drafting", "x")
    assert timed_out and final.status == "cancelled"
    assert len(llm.requests) == 1  # the next model call was short-circuited by before_model_callback
    assert PREFIX + "deck/index.html" not in env.store.objects  # the pending tool call was skipped
    assert "時間切れ" in final.output_text


def test_llm_call_limit_maps_to_incomplete(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREEFORM_ADK_MAX_LLM_CALLS", "10")
    designer, llm = env.make([step(call("list_files", directory="/workspace/job")) for _ in range(12)])
    turn = designer.start(freeform.DRAFT_PROMPT, freeform.workspace_spec(env.run))
    final, timed_out = designer.wait(env.run, turn, 30, "freeform_drafting", "x")
    assert not timed_out and final.status == "incomplete" and "LlmCallsLimit" in final.error
    assert len(llm.requests) == 10 and final.llm_calls == 10


def test_unknown_session_and_bad_workspace_are_rejected(env) -> None:
    designer, _llm = env.make([])
    with pytest.raises(RuntimeError):
        designer.start("x", "adk-missing")
    with pytest.raises(TypeError):
        designer.start("x", {"prefix": "staging/p/r1/"})  # no bucket


def test_make_designer_builds_the_adk_designer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.delenv("FREEFORM_ADK_MODEL", raising=False)
    designer = freeform.make_designer("proj")
    try:
        assert isinstance(designer, adk_designer.AdkDesigner)
        assert designer._model_obj().model == "gemini-3.8-flash"
        assert freeform.engine_name() == "adk_freeform:gemini-3.8-flash"
        assert freeform.designer_model() == "gemini-3.8-flash"
    finally:
        designer.close()
