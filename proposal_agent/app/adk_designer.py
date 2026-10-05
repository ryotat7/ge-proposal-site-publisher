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

"""ADK designer for free-form decks.

An ADK `LlmAgent` (default `gemini-3.8-flash`) whose function tools read and write the run's GCS staging
prefix, shown to the model as /workspace/job (input/ is read-only, deck/ is writable). `freeform.run_pipeline`
drives it through `start()` / `wait()` and a turn handle with `id`, `status`, `session_id`, `output_text` and
`usage`. AI images, the deck_contract build, the private renderer, the screenshot review turns, the final gate
and publishing live in app/freeform.py.

- No code execution, package installation or web access. The agent writes SVG and chart JSON as text.
- `check_deck` runs the publish contract check (deck_contract) plus the grounding check (numbers, email
  addresses and work-file names that the inputs do not support).
- Review screenshots arrive as image parts of the next user message in the SAME ADK session.
"""

import asyncio
import base64
import collections
import concurrent.futures
import json
import logging
import os
import posixpath
import threading
import time
import uuid
from typing import Any, Optional

from app import deck_contract as dc

logger = logging.getLogger(__name__)

ENGINE_PREFIX = "adk_freeform"
APP_NAME = "freeform_designer"
MOUNT_TARGET = "/workspace/job"
TEXT_EXTENSIONS = {".html", ".htm", ".css", ".svg", ".json", ".md", ".txt"}
MAX_WRITE_BYTES = 600_000
MAX_READ_CHARS = 200_000
CANCEL_GRACE_SECONDS = 45.0
BUSY_WAIT_SECONDS = 60.0


def adk_model() -> str:
    return os.environ.get("FREEFORM_ADK_MODEL", "gemini-3.8-flash")


def engine_name() -> str:
    return f"{ENGINE_PREFIX}:{adk_model()}"


def max_llm_calls() -> int:
    try:
        return max(10, min(400, int(os.environ.get("FREEFORM_ADK_MAX_LLM_CALLS", "120"))))
    except ValueError:
        return 120


# Must not contain curly braces: ADK treats them as session-state placeholders.
ADK_INSTRUCTION = """あなたは一流のWeb提案ポータル・プレゼンテーションデザイナー兼フロントエンドエンジニアです。
既定では4カラム構成のWeb提案ポータル（data-pd-layout="portal"、章タブ＋自動目次＋記事リーダー＋右リファレンス＋スライド表示切替）を設計し、スライド形式（data-pd-layout="slides"）が指定された場合は16:9スライドを設計します。
作業フォルダ /workspace/job/ は関数ツールでだけ読み書きできます。input/ は読み取り専用、deck/ が成果物の置き場所です。

使えるツール:
- list_files: フォルダ内のファイル一覧（サイズ付き）
- read_file: テキストファイルを読む
- write_file: deck/ の下にテキストファイル（HTML・CSS・SVG・JSON・Markdown）を書く。同じパスなら上書き
- replace_in_file: ファイル内の文字列を 1 か所だけ置き換える（小さな修正向け）
- delete_file: deck/ の下のファイルを消す
- check_deck: deck/ を公開前の契約チェックにかけ、エラー・警告と、入力資料に根拠のない数値・メールアドレス・作業用ファイル名を返す

注意:
- コードの実行、パッケージのインストール、Web 検索はできません。SVG やグラフ用 JSON は自分で書きます。
- PNG や JPEG は書けません。写真やイラストが必要なら DESIGN_RULES.md の手順で image_requests.json に依頼を書きます。
- write_file はファイル全体を書きます。小さな修正には replace_in_file を使います。
- 数値・メールアドレス・URL は input/ の資料にあるものだけを使い、作業用のファイル名やパスはスライドに書きません。
- 依頼された作業が終わったら、ツールを呼ばずに、依頼された形式のテキストで返答します。
"""

# ---------------------------------------------------------------------------
# GCS-backed workspace (the run's staging prefix)
# ---------------------------------------------------------------------------
class Workspace:
    """/workspace/job/ view of `gs://<bucket>/<prefix>`: input/ is read-only, deck/ is writable."""

    def __init__(self, store: Any, prefix: str) -> None:
        self.store = store
        self.prefix = prefix if prefix.endswith("/") else prefix + "/"
        self.writes = 0
        self.bytes_written = 0

    @staticmethod
    def normalize(raw: str, write: bool = False) -> str:
        value = str(raw or "").strip().replace("\\", "/")
        for head in (MOUNT_TARGET + "/", MOUNT_TARGET.lstrip("/") + "/"):
            if value.startswith(head):
                value = value[len(head) :]
                break
        if value in (MOUNT_TARGET, MOUNT_TARGET.lstrip("/")):
            value = ""
        value = value.lstrip("/")
        path = posixpath.normpath(value) if value else ""
        if path == ".":
            path = ""
        if path.startswith("..") or "/../" in f"/{path}/":
            raise ValueError("作業フォルダの外は扱えません")
        top = path.split("/", 1)[0] if path else ""
        if write:
            if top != "deck" or path == "deck":
                raise ValueError("書き込めるのは /workspace/job/deck/ の下のファイルだけです")
        elif path and top not in ("deck", "input"):
            raise ValueError("/workspace/job/input/ か /workspace/job/deck/ の下を指定してください")
        return path

    @staticmethod
    def display(rel: str) -> str:
        return f"{MOUNT_TARGET}/{rel}" if rel else f"{MOUNT_TARGET}/"

    def list(self, directory: str) -> dict[str, Any]:
        rel = self.normalize(directory)
        base = self.prefix + (rel + "/" if rel else "")
        entries = []
        for name, (size, _generation) in sorted(self.store.list_meta(base).items()):
            path = f"{rel}/{name}" if rel else name
            if path.split("/", 1)[0] not in ("deck", "input"):
                continue  # pipeline-internal folders (build/ ...) are not part of the workspace
            entries.append({"path": self.display(path), "bytes": size})
        return {"status": "ok", "directory": self.display(rel), "count": len(entries), "files": entries[:300]}

    def read(self, path: str) -> dict[str, Any]:
        rel = self.normalize(path)
        if rel in ("", "deck", "input"):
            return {"status": "error", "message": "ファイルのパスを指定してください（フォルダは list_files で見ます）"}
        data = self.store.read(self.prefix + rel)
        if data is None:
            return {"status": "error", "message": f"{self.display(rel)} はありません"}
        if posixpath.splitext(rel)[1].lower() not in TEXT_EXTENSIONS:
            return {"status": "ok", "path": self.display(rel), "bytes": len(data), "note": "バイナリファイルのため内容は表示しません"}
        text = data.decode("utf-8", errors="replace")
        return {
            "status": "ok",
            "path": self.display(rel),
            "chars": len(text),
            "truncated": len(text) > MAX_READ_CHARS,
            "content": text[:MAX_READ_CHARS],
        }

    def write(self, path: str, content: str) -> dict[str, Any]:
        rel = self.normalize(path, write=True)
        if posixpath.splitext(rel)[1].lower() not in TEXT_EXTENSIONS:
            return {"status": "error", "message": "書けるのはテキスト形式（.html .css .svg .json .md .txt）だけです"}
        data = str(content or "").encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            return {"status": "error", "message": f"ファイルが大きすぎます（{len(data)} bytes、上限 {MAX_WRITE_BYTES}）"}
        self.store.write(self.prefix + rel, data, dc.content_type_for_bytes(rel, data))
        self.writes += 1
        self.bytes_written += len(data)
        result: dict[str, Any] = {"status": "ok", "path": self.display(rel), "bytes": len(data)}
        if rel.endswith(".json"):
            try:
                json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                result["warning"] = f"JSON として読めません: {str(exc)[:160]}"
        return result

    def replace(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        rel = self.normalize(path, write=True)
        data = self.store.read(self.prefix + rel)
        if data is None:
            return {"status": "error", "message": f"{self.display(rel)} はありません"}
        text = data.decode("utf-8", errors="replace")
        count = text.count(old_text) if old_text else 0
        if count == 0:
            return {"status": "error", "message": "old_text が見つかりません。read_file で現在の内容を確認してください"}
        if count > 1:
            return {"status": "error", "message": f"old_text が {count} か所にあります。前後の文字を含めて 1 か所に絞ってください"}
        return self.write(rel, text.replace(old_text, str(new_text or ""), 1))

    def delete(self, path: str) -> dict[str, Any]:
        rel = self.normalize(path, write=True)
        deleter = getattr(self.store, "delete", None)
        if deleter is None:
            return {"status": "error", "message": "このワークスペースでは削除できません"}
        deleter(self.prefix + rel)
        return {"status": "ok", "path": self.display(rel), "deleted": True}

    def check(self) -> dict[str, Any]:
        from app import freeform as ff  # lazy: freeform imports this module lazily as well

        files = self.store.read_all(self.prefix + "deck/")
        requests, request_warnings = ff.parse_image_requests(files)
        placeholders = 0
        for req in requests:
            if req["path"] not in files:
                files[req["path"]] = ff.placeholder_png()
                placeholders += 1
        build = dc.build_publishable(files, "")
        inputs = {}
        for name in ff.GROUNDING_INPUTS:
            data = self.store.read(self.prefix + "input/" + name)
            inputs[name] = data.decode("utf-8", errors="replace") if data else ""
        grounding = ungrounded = []
        if "index.html" in build.files:
            ungrounded = ff.ungrounded_content(build.files["index.html"], ff.grounding_sources(inputs))
            grounding = [ff.grounding_note(ungrounded)] if ungrounded else []
        return {
            "status": "ok",
            "publishable": not build.errors,
            "slide_count": build.slide_count,
            "slide_titles": [s.title for s in build.slides][:20],
            "errors": [f"{i.file or '-'}: {i.message}" for i in build.errors][:15],
            "warnings": [f"{i.file or '-'}: {i.message}" for i in build.warnings][:15] + request_warnings[:5],
            "ungrounded": ungrounded,
            "grounding": grounding,
            "note": (
                f"AI 画像 {placeholders} 枚は下書きのあとでワーカーが生成するため、仮の画像を置いて検査しました"
                if placeholders
                else ""
            ),
        }


# ---------------------------------------------------------------------------
# Turns and sessions
# ---------------------------------------------------------------------------
class AdkTurn:
    """Turn handle returned by `AdkDesigner.start()`."""

    def __init__(self, turn_id: str, session_id: str) -> None:
        self.id = turn_id
        self.session_id = session_id
        self.status = "in_progress"
        self.output_text = ""
        self.usage: dict[str, Any] = {}
        self.tool_calls: collections.Counter[str] = collections.Counter()
        self.llm_calls = 0
        self.error = ""
        self.started = time.monotonic()
        self.finished: Optional[float] = None
        self.cancel = threading.Event()
        self.future: Optional[concurrent.futures.Future[None]] = None

    @property
    def elapsed_seconds(self) -> float:
        return round((self.finished or time.monotonic()) - self.started, 1)


def _accumulate_usage(usage: dict[str, Any], meta: Any) -> None:
    def add(key: str, value: Any) -> None:
        usage[key] = int(usage.get(key) or 0) + int(value or 0)

    add("total_input_tokens", getattr(meta, "prompt_token_count", 0))
    add("total_output_tokens", getattr(meta, "candidates_token_count", 0))
    add("total_cached_tokens", getattr(meta, "cached_content_token_count", 0))
    add("total_thought_tokens", getattr(meta, "thoughts_token_count", 0))
    add("total_tokens", getattr(meta, "total_token_count", 0))
    image = 0
    for detail in getattr(meta, "prompt_tokens_details", None) or []:
        if "image" in str(getattr(detail, "modality", "") or "").lower():
            image += int(getattr(detail, "token_count", 0) or 0)
    if image:
        by_modality = usage.setdefault("input_tokens_by_modality", [{"modality": "image", "tokens": 0}])
        by_modality[0]["tokens"] = int(by_modality[0]["tokens"]) + image


class _Session:
    def __init__(self, workspace: Workspace, session_id: str) -> None:
        self.workspace = workspace
        self.session_id = session_id
        self.user_id = "freeform-worker"
        self.turns = 0
        self.current: Optional[AdkTurn] = None
        self.created = False
        self.runner: Any = None
        self.session_service: Any = None


def to_content(prompt_input: Any) -> Any:
    """Converts a str or [{"type": "user_input", "content": [text/image chunks]}] input to Content."""
    from google.genai import types

    if isinstance(prompt_input, str):
        return types.Content(role="user", parts=[types.Part(text=prompt_input)])
    parts: list[Any] = []
    items = prompt_input if isinstance(prompt_input, list) else [prompt_input]
    for item in items:
        if not isinstance(item, dict):
            continue
        contents = item.get("content") if item.get("type") == "user_input" else [item]
        for chunk in contents or []:
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") == "text" and chunk.get("text"):
                parts.append(types.Part(text=str(chunk["text"])))
            elif chunk.get("type") == "image" and chunk.get("data"):
                parts.append(
                    types.Part.from_bytes(
                        data=base64.b64decode(chunk["data"]), mime_type=str(chunk.get("mime_type") or "image/jpeg")
                    )
                )
    if not parts:
        parts.append(types.Part(text="続けてください。"))
    return types.Content(role="user", parts=parts)


class AdkDesigner:
    """Runs designer turns on an ADK LlmAgent; `freeform.run_pipeline` calls `start()` and `wait()`."""

    def __init__(self, project_id: str, store: Any = None, model: Any = None) -> None:
        self.project_id = project_id
        self._store = store
        self._model = model  # tests inject a BaseLlm; production builds a Gemini model per session
        self._sessions: dict[str, _Session] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="adk-designer-loop", daemon=True)
        self._thread.start()
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)

    # -- construction ------------------------------------------------------
    def _model_obj(self) -> Any:
        if self._model is not None:
            return self._model
        from google.adk.models import Gemini
        from google.genai import types

        return Gemini(
            model=adk_model(),
            retry_options=types.HttpRetryOptions(attempts=3),
            client_kwargs={"vertexai": True, "project": self.project_id, "location": "global"},
        )

    def _build_agent(self, session: _Session) -> Any:
        from google.adk.agents import LlmAgent
        from google.adk.models.llm_response import LlmResponse
        from google.genai import types

        ws = session.workspace

        def guarded(fn: Any, *args: Any) -> dict[str, Any]:
            try:
                return fn(*args)
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
            except Exception as exc:  # noqa: BLE001 - tools never raise
                logger.info("adk tool %s failed: %s", getattr(fn, "__name__", "?"), exc)
                return {"status": "error", "message": f"{type(exc).__name__}: {str(exc)[:200]}"}

        def list_files(directory: str) -> dict:
            """Lists files under a folder of /workspace/job/ (for example /workspace/job/input or /workspace/job/deck).

            Args:
                directory: Folder path such as /workspace/job/deck. Use /workspace/job for everything.
            """
            return guarded(ws.list, directory)

        def read_file(path: str) -> dict:
            """Reads a text file (HTML, CSS, SVG, JSON, Markdown) under /workspace/job/input or /workspace/job/deck.

            Args:
                path: File path such as /workspace/job/input/brief.md.
            """
            return guarded(ws.read, path)

        def write_file(path: str, content: str) -> dict:
            """Writes (creates or overwrites) a whole text file under /workspace/job/deck.

            Args:
                path: File path such as /workspace/job/deck/index.html or /workspace/job/deck/charts/sales.json.
                content: The complete file content (UTF-8 text).
            """
            return guarded(ws.write, path, content)

        def replace_in_file(path: str, old_text: str, new_text: str) -> dict:
            """Replaces exactly one occurrence of old_text with new_text in a file under /workspace/job/deck.

            Args:
                path: File path under /workspace/job/deck.
                old_text: Exact text to find. It must occur exactly once; include surrounding text to make it unique.
                new_text: Replacement text.
            """
            return guarded(ws.replace, path, old_text, new_text)

        def delete_file(path: str) -> dict:
            """Deletes a file under /workspace/job/deck.

            Args:
                path: File path under /workspace/job/deck.
            """
            return guarded(ws.delete, path)

        def check_deck() -> dict:
            """Runs the publish contract check on /workspace/job/deck.

            Returns slide titles, errors, warnings and `ungrounded` items (numbers, email addresses or work-file
            names on the slides that the input files do not support).
            """
            return guarded(ws.check)

        def before_model(callback_context: Any, llm_request: Any) -> Any:
            turn = session.current
            if turn is not None and turn.cancel.is_set():
                return LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text="時間切れのため、ここで作業を止めました。")])
                )
            return None

        def before_tool(tool: Any, args: Any, tool_context: Any) -> Any:
            turn = session.current
            if turn is not None and turn.cancel.is_set():
                return {"status": "error", "message": "時間切れのため中断しました（ツールは実行していません）"}
            return None

        return LlmAgent(
            name="freeform_deck_designer",
            model=self._model_obj(),
            instruction=ADK_INSTRUCTION,
            tools=[list_files, read_file, write_file, replace_in_file, delete_file, check_deck],
            generate_content_config=types.GenerateContentConfig(max_output_tokens=65536),
            before_model_callback=before_model,
            before_tool_callback=before_tool,
        )

    def _make_store(self, bucket: str) -> Any:
        from app.freeform import Store

        return Store(self.project_id, bucket)

    def _session_for(self, workspace: Any) -> _Session:
        if isinstance(workspace, str) and workspace:
            session = self._sessions.get(workspace)
            if session is None:
                raise RuntimeError(f"unknown ADK designer session: {workspace}")
            return session
        if isinstance(workspace, dict) and workspace.get("bucket") and workspace.get("prefix"):
            store = self._store if self._store is not None else self._make_store(str(workspace["bucket"]))
            prefix = str(workspace["prefix"]).rstrip("/") + "/"
            session = _Session(Workspace(store, prefix), "adk-" + uuid.uuid4().hex[:16])
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService

            session.session_service = InMemorySessionService()
            session.runner = Runner(
                agent=self._build_agent(session), app_name=APP_NAME, session_service=session.session_service
            )
            self._sessions[session.session_id] = session
            return session
        raise TypeError("workspace must be a dict with bucket and prefix, or an ADK session id")

    # -- turns -----------------------------------------------------------------
    async def _run_turn(self, session: _Session, content: Any, turn: AdkTurn) -> None:
        from google.adk.agents.run_config import RunConfig

        session.current = turn
        texts: list[str] = []
        try:
            if not session.created:
                await session.session_service.create_session(
                    app_name=APP_NAME, user_id=session.user_id, session_id=session.session_id
                )
                session.created = True
            async for event in session.runner.run_async(
                user_id=session.user_id,
                session_id=session.session_id,
                new_message=content,
                run_config=RunConfig(max_llm_calls=max_llm_calls()),
            ):
                meta = getattr(event, "usage_metadata", None)
                if meta is not None:
                    turn.llm_calls += 1
                    _accumulate_usage(turn.usage, meta)
                parts = event.content.parts if event.content and event.content.parts else []
                for part in parts:
                    if part.function_call is not None:
                        turn.tool_calls[str(part.function_call.name or "?")] += 1
                if event.is_final_response():
                    texts.extend(p.text for p in parts if p.text and not getattr(p, "thought", False))
            turn.status = "cancelled" if turn.cancel.is_set() else "completed"
        except Exception as exc:  # noqa: BLE001 - the pipeline keeps whatever files exist
            name = type(exc).__name__
            turn.error = f"{name}: {str(exc)[:300]}"
            turn.status = "incomplete" if "LlmCallsLimit" in name else "failed"
            logger.warning("ADK designer turn %s ended with %s", turn.id, turn.error)
        finally:
            turn.output_text = "\n".join(texts).strip()
            turn.finished = time.monotonic()
            if session.current is turn:
                session.current = None

    def start(self, prompt_input: Any, workspace: Any) -> AdkTurn:
        """Starts a turn: `workspace` is `freeform.workspace_spec(run)` (new session) or a session id (continue)."""
        session = self._session_for(workspace)
        deadline = time.monotonic() + BUSY_WAIT_SECONDS
        while session.current is not None and time.monotonic() < deadline:
            time.sleep(0.5)  # a cancelled turn may still be finishing its last model call
        if session.current is not None:
            raise RuntimeError("the previous ADK designer turn is still running")
        session.turns += 1
        turn = AdkTurn(f"{session.session_id}-t{session.turns}", session.session_id)
        content = to_content(prompt_input)
        session.current = turn  # busy from now on, before the coroutine is scheduled
        turn.future = asyncio.run_coroutine_threadsafe(self._run_turn(session, content, turn), self._loop)
        return turn

    def wait(self, run: Any, interaction: AdkTurn, deadline_seconds: float, phase: str, detail: str) -> tuple[AdkTurn, bool]:
        """Waits for the turn; at the deadline asks it to stop and returns (turn, timed_out=True)."""
        started = time.monotonic()
        last_report = time.monotonic()
        future = interaction.future
        while future is not None and not future.done():
            if time.monotonic() - started > deadline_seconds:
                interaction.cancel.set()
                try:
                    future.result(timeout=CANCEL_GRACE_SECONDS)
                except Exception:  # noqa: BLE001 - still finishing its last model call
                    pass
                if interaction.status == "in_progress":
                    interaction.status = "cancelled"
                return interaction, True
            time.sleep(1.0)
            if time.monotonic() - last_report > 30:
                last_report = time.monotonic()
                run.status(phase, f"{detail}（{int(time.monotonic() - started)} 秒経過）")
        return interaction, False

    def turn_stats(self) -> dict[str, Any]:
        """Workspace write counters per session (diagnostics for the engine comparison)."""
        return {
            sid: {"turns": s.turns, "writes": s.workspace.writes, "bytes_written": s.workspace.bytes_written}
            for sid, s in self._sessions.items()
        }

    def close(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
