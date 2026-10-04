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

"""Unit tests for the free-form pipeline (draft -> render -> visual review loop -> final gate -> publish).

The ADK designer, GCS and the renderer are replaced with in-memory fakes, so these tests pin the
control flow: which build is published, what the agent is shown in each review turn, and what is stored.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "proposal_agent"))

from app import deck_contract as dc  # noqa: E402
from app import freeform  # noqa: E402

PNG = freeform.placeholder_png(4, 4)


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

    def write_many(self, prefix: str, files: dict[str, bytes]) -> None:
        for rel, data in files.items():
            self.write(prefix + rel, data)



class FakeTurn(types.SimpleNamespace):
    pass


class FakeDesigner:
    """Each scripted turn edits the staging deck (like the designer's file tools would) and returns its reply."""

    def __init__(self, store: FakeStore, run: freeform.Run, turns: list[Any], fail_on_call: int | None = None) -> None:
        self.store = store
        self.run = run
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self.fail_on_call = fail_on_call

    def start(self, prompt_input: Any, workspace: Any) -> FakeTurn:
        number = len(self.calls) + 1
        if self.fail_on_call == number:
            raise RuntimeError("the previous ADK designer turn is still running")
        self.calls.append({"input": prompt_input, "workspace": workspace})
        return FakeTurn(id=f"sess-1-t{number}", status="in_progress")

    def wait(self, run: freeform.Run, turn: Any, deadline_seconds: float, phase: str, detail: str):
        script = self.turns.pop(0)
        text = script(self.store, self.run.deck_prefix) or ""
        usage = types.SimpleNamespace(
            model_dump=lambda **_: {
                "total_tokens": 1000,
                "total_input_tokens": 900,
                "input_tokens_by_modality": [{"modality": "image", "tokens": 1100}],
            }
        )
        final = FakeTurn(id=turn.id, status="completed", session_id="sess-1", output_text=text, usage=usage)
        return final, False


def deck(slides: list[tuple[str, str]], head: str = "") -> bytes:
    body = "".join(f'<section class="pd-slide" data-pd-title="{t}"><h2>{t}</h2><p>{p}</p></section>' for t, p in slides)
    return (
        f'<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>Acme</title>{head}</head>'
        f'<body><main id="pd-deck">{body}</main></body></html>'
    ).encode("utf-8")


GOOD = [("表紙", "Acme Corp 様"), ("課題", "問い合わせの 6 割が定型"), ("提案", "Agent Runtime で自動化")]
OVERFLOW = [("表紙", "Acme Corp 様"), ("課題", "はみ出し"), ("提案", "Agent Runtime で自動化")]


def write_deck(slides: list[tuple[str, str]], extra: dict[str, bytes] | None = None, reply: str = "DRAFT_STATUS: DONE"):
    def turn(store: FakeStore, deck_prefix: str) -> str:
        store.write(deck_prefix + "index.html", deck(slides))
        store.write(deck_prefix + "manifest.json", b'{"concept": "clean"}')
        for rel, data in (extra or {}).items():
            store.write(deck_prefix + rel, data)
        return reply

    return turn


def reply_only(text: str):
    return lambda store, deck_prefix: text


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


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
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
    return types.SimpleNamespace(store=store, run=run, phases=phases, renders=renders)


def images_in(call: dict[str, Any]) -> int:
    return sum(1 for part in call["input"][0]["content"] if part["type"] == "image")


# ---------------------------------------------------------------------------
# Pipeline control flow
# ---------------------------------------------------------------------------
def test_review_loop_fixes_overflow_in_the_same_session(env) -> None:
    designer = FakeDesigner(
        env.store,
        env.run,
        [
            write_deck(OVERFLOW),
            write_deck(GOOD, reply="2 枚目の文字を縮めました\nREVIEW_STATUS: FIXED"),
            reply_only("修正後のスライドを確認しました\nREVIEW_STATUS: APPROVED"),
        ],
    )
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"}, title="Acme")
    assert outcome.ok and outcome.chosen is not None
    assert outcome.chosen.number == 2 and outcome.chosen.layout_errors == 0
    # Draft turn: new session on the staging prefix. Review turns: the SAME session id.
    assert designer.calls[0]["workspace"] == {"bucket": "bkt", "prefix": "staging/prop-test/r1/"}
    assert designer.calls[1]["workspace"] == "sess-1"
    review = designer.calls[1]["input"]
    assert review[0]["type"] == "user_input" and images_in(designer.calls[1]) == 3  # one screenshot per slide
    assert "overflow: 403px" in json.dumps(review, ensure_ascii=False)
    assert outcome.rounds[0]["review_status"] == "FIXED" and outcome.rounds[0]["files_changed"] is True
    assert outcome.rounds[0]["image_tokens"] == 1100 and outcome.rounds[0]["screenshots_sent"] == 3
    assert outcome.review_summaries == ["2 枚目の文字を縮めました", "修正後のスライドを確認しました"]
    assert env.renders == ["staging/prop-test/r1/build/r1/", "staging/prop-test/r1/build/r2/"]
    # Round 2: the agent sees its own fix (only the changed slide is attached) and approves it.
    assert len(designer.calls) == 3
    assert designer.calls[2]["workspace"] == "sess-1"
    assert images_in(designer.calls[2]) == 1
    assert outcome.rounds[1]["review_status"] == "APPROVED" and outcome.rounds[1]["files_changed"] is False
    assert outcome.rounds[1]["screenshots_sent"] == 1
    assert len(outcome.builds) == 2
    assert [p for p, _ in env.phases][:2] == ["freeform_staging", "freeform_drafting"]
    assert "freeform_reviewing" in [p for p, _ in env.phases]


def test_approved_without_changes_stops_after_one_review(env) -> None:
    designer = FakeDesigner(env.store, env.run, [write_deck(GOOD), reply_only("問題ありません\nREVIEW_STATUS: APPROVED")])
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"})
    assert outcome.ok and outcome.chosen.number == 1
    assert len(designer.calls) == 2 and len(outcome.builds) == 1
    assert outcome.rounds[0]["review_status"] == "APPROVED" and outcome.rounds[0]["files_changed"] is False


def test_second_round_only_attaches_changed_or_flagged_slides(env) -> None:
    still_broken = [("表紙", "Acme Corp 様"), ("課題", "はみ出し 2"), ("提案", "Agent Runtime で自動化")]
    designer = FakeDesigner(
        env.store,
        env.run,
        [
            write_deck(OVERFLOW),
            write_deck(still_broken, reply="REVIEW_STATUS: FIXED"),
            write_deck(GOOD, reply="REVIEW_STATUS: FIXED"),
        ],
    )
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"})
    assert len(designer.calls) == 3
    assert images_in(designer.calls[1]) == 3
    assert images_in(designer.calls[2]) == 1  # only slide 2 changed / still flagged
    assert "画像省略" in json.dumps(designer.calls[2]["input"], ensure_ascii=False)
    assert outcome.chosen.number == 3 and outcome.chosen.layout_errors == 0  # final render of r3


def test_second_round_skipped_when_fix_changed_nothing_visible(env) -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
    designer = FakeDesigner(
        env.store,
        env.run,
        [write_deck(GOOD), write_deck(GOOD, extra={"assets/unused.svg": svg}, reply="REVIEW_STATUS: FIXED")],
    )
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"})
    assert outcome.ok
    assert len(designer.calls) == 2  # identical screenshots and clean checks: no second look needed
    assert outcome.rounds[0]["files_changed"] is True and len(outcome.rounds) == 1


def test_missing_index_fails_without_publishing(env) -> None:
    designer = FakeDesigner(env.store, env.run, [reply_only("DRAFT_STATUS: DONE")])
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"})
    assert not outcome.ok and outcome.chosen is None and "index.html" in outcome.error


def test_static_errors_block_publishing_when_never_fixed(env) -> None:
    one_slide = [("表紙", "Acme")]  # fewer than 3 slides -> contract error
    designer = FakeDesigner(env.store, env.run, [write_deck(one_slide), reply_only("REVIEW_STATUS: APPROVED")])
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"})
    assert not outcome.ok and "公開できる版がありません" in outcome.error


def test_review_start_failure_keeps_verified_build(env) -> None:
    designer = FakeDesigner(env.store, env.run, [write_deck(OVERFLOW)], fail_on_call=2)
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"})
    assert outcome.ok and outcome.chosen.number == 1
    assert any("開始できませんでした" in w for w in outcome.warnings)
    assert any("レイアウト検査の指摘が 1 件" in w for w in outcome.warnings)


def test_ai_images_generated_once_and_regenerated_on_prompt_change(env, monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def fake_generate(project_id: str, prompt: str, aspect: str) -> bytes:
        prompts.append(prompt)
        return PNG

    monkeypatch.setattr(freeform, "_generate_image", fake_generate)
    hero = [("表紙", 'Acme<img src="assets/ai/hero.png" data-pd-ai-image alt="">'), ("課題", "はみ出し"), ("提案", "x")]
    request_v1 = json.dumps([{"path": "assets/ai/hero.png", "prompt": "calm navy gradient"}]).encode()
    request_v2 = json.dumps([{"path": "assets/ai/hero.png", "prompt": "warm sunrise gradient"}]).encode()

    def fix_and_change_prompt(store: FakeStore, deck_prefix: str) -> str:
        fixed = [("表紙", hero[0][1]), ("課題", "定型質問"), ("提案", "x")]
        store.write(deck_prefix + "index.html", deck(fixed))
        store.write(deck_prefix + "image_requests.json", request_v2)
        del store.objects[deck_prefix + "assets/ai/hero.png"]  # the sandbox sync does not mirror our image
        return "REVIEW_STATUS: FIXED"

    designer = FakeDesigner(
        env.store, env.run, [write_deck(hero, {"image_requests.json": request_v1}), fix_and_change_prompt]
    )
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"})
    assert outcome.ok and outcome.chosen.number == 2
    assert prompts == ["calm navy gradient", "warm sunrise gradient"]
    assert "assets/ai/hero.png" in outcome.chosen.build.files
    assert outcome.chosen.build.manifest["ai_images"] == ["assets/ai/hero.png"]


def test_ai_image_failure_uses_placeholder(env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any) -> bytes:
        raise RuntimeError("quota")

    monkeypatch.setattr(freeform, "_generate_image", boom)
    files = {"image_requests.json": json.dumps([{"path": "assets/ai/a.png", "prompt": "p"}]).encode()}
    produced, warnings = freeform.generate_ai_images(env.run, env.store, files, {}, {})
    assert dc.sniff_raster(produced["assets/ai/a.png"]) == "png"
    assert any("仮の画像" in w for w in warnings)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _record(number: int, errors: int | None, publishable: bool = True) -> freeform.BuildRecord:
    issues = [] if publishable else [dc.Issue("error", "x", "x")]
    build = dc.BuildResult(files={"index.html": b"x"}, issues=issues, slides=[], manifest={})
    render = None
    if errors is not None:
        render = {"ok": True, "slides": [{"issues": [{"severity": "error"}] * errors}]}
    return freeform.BuildRecord(number=number, build=build, prefix=f"p/r{number}/", render=render)


def test_choose_build_final_gate() -> None:
    assert freeform.choose_build([_record(1, 2, publishable=False)]) is None
    # The latest build came from a fix and was not re-rendered (time ran out): trust the fix.
    assert freeform.choose_build([_record(1, 3), _record(2, None)]).number == 2
    # Otherwise the rendered build with the fewest layout errors wins; ties go to the latest.
    assert freeform.choose_build([_record(1, 1), _record(2, 4)]).number == 1
    assert freeform.choose_build([_record(1, 1), _record(2, 1)]).number == 2
    assert freeform.choose_build([_record(1, 0), _record(2, 2, publishable=False)]).number == 1


def test_parse_image_requests_validation() -> None:
    reqs = [{"path": f"assets/ai/img{i}.png", "prompt": "p", "aspect_ratio": "21:9"} for i in range(6)]
    reqs.insert(0, {"path": "assets/../x.png", "prompt": "p"})
    parsed, warnings = freeform.parse_image_requests({"image_requests.json": json.dumps({"images": reqs}).encode()})
    assert [r["path"] for r in parsed] == [f"assets/ai/img{i}.png" for i in range(4)]
    assert all(r["aspect_ratio"] == "16:9" for r in parsed)
    assert len(warnings) == 3
    assert freeform.parse_image_requests({"image_requests.json": b"{oops"})[0] == []
    assert freeform.parse_image_requests({}) == ([], [])


def test_focus_slides_and_review_status_parsing() -> None:
    prev = {"slides": [{"index": 0, "screenshot_b64": "a"}, {"index": 1, "screenshot_b64": "b"}]}
    cur = {"slides": [{"index": 0, "screenshot_b64": "a"}, {"index": 1, "screenshot_b64": "c"}]}
    assert freeform.focus_slides(prev, cur) == {1}
    cur["slides"][0]["issues"] = [{"severity": "error"}]
    assert freeform.focus_slides(prev, cur) == {0, 1}
    assert freeform.focus_slides(None, cur) is None
    assert freeform.focus_slides(prev, {"slides": cur["slides"][:1]}) is None
    assert freeform.parse_review_status("直しました\nREVIEW_STATUS: fixed") == "FIXED"
    assert freeform.parse_review_status("no marker") == ""
    assert freeform.strip_status_lines("要約\nDRAFT_STATUS: DONE") == "要約"


def test_budget_from_env_clamps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREEFORM_REVIEW_ROUNDS", "99")
    monkeypatch.setenv("FREEFORM_TOTAL_BUDGET_SECONDS", "10")
    monkeypatch.setenv("FREEFORM_DRAFT_DEADLINE_SECONDS", "abc")
    budget = freeform.Budget.from_env()
    assert budget.rounds == 4 and budget.total == 240 and budget.draft == 540


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
def test_publish_outcome_versions_and_qa_screenshots(env) -> None:
    designer = FakeDesigner(env.store, env.run, [write_deck(GOOD), reply_only("REVIEW_STATUS: APPROVED")])
    outcome = freeform.run_pipeline(env.run, env.store, designer, mode="create", input_files={"brief.md": "x"})
    data = {"content_version": 4, "freeform_versions": [{"version": v, "prefix": f"p/v{v}/"} for v in range(1, 11)]}
    updates = freeform.publish_outcome(env.run, env.store, data, outcome)
    assert updates["freeform_prefix"] == "presentations/prop-test/v11/"
    assert updates["gcs_blob_path"] == "presentations/prop-test/v11/index.html"
    assert updates["content_version"] == 5 and updates["render_mode"] == "freeform"
    assert len(updates["freeform_versions"]) == freeform.MAX_VERSIONS
    assert updates["freeform_versions"][-1]["version"] == 11 and updates["freeform_versions"][0]["version"] == 2
    assert updates["freeform"]["review_rounds"][0]["review_status"] == "APPROVED"
    assert updates["slide_titles"] == ["表紙", "課題", "提案"]
    published = env.store.read_all("presentations/prop-test/v11/")
    assert {"index.html", "source.html", "manifest.json", "_qa/slide_01.jpg", "_qa/slide_03.jpg"} <= set(published)
    assert "generation_status" not in updates  # the worker owns status fields


def test_seed_from_published_restores_raw_html() -> None:
    store = FakeStore()
    store.write_many(
        "presentations/p/v2/",
        {"index.html": b"<sanitised>", "source.html": b"<raw>", "assets/a.svg": b"<svg/>", "_qa/slide_01.jpg": b"j"},
    )
    published, seed = freeform.seed_from_published(store, {"freeform_prefix": "presentations/p/v2/"})
    assert seed == {"index.html": b"<raw>", "assets/a.svg": b"<svg/>"}
    assert "_qa/slide_01.jpg" in published
    assert freeform.seed_from_published(store, {}) == ({}, {})
