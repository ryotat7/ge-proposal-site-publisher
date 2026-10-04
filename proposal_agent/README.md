# proposal-site-publisher-agent

Simple ReAct agent
Agent generated with `agents-cli` version `1.4.0`

## 提案コンシェルジュ固有の仕様（生成・修正・配信）

### 2 つの作り方（`design_mode`）
| design_mode | 内容 | 作成の目安 | 修正の目安 |
| --- | --- | --- | --- |
| `freeform`（既定） | ADK のデザイナーエージェント（`gemini-3.8-flash`）が、レイアウト・配色・枚数・グラフ・図解・AI 画像まで自由に作ります。公開前に描画結果を自分で見て直します | 通常 7〜11 分（最長約 20 分） | 通常 5〜7 分（最長約 15 分） |
| `template`（高速モード） | 6 枚構成の決まったテンプレートに文章を流し込みます。ユーザーが高速モードを頼んだときと、自由デザインで公開できる版ができなかったときに使います | 通常 1〜5 分 | 通常 15〜60 秒 |

> [!IMPORTANT]
> 自由デザインのデザイナーは、ワーカー（Cloud Run Job）の中で動く ADK の `LlmAgent` です。使うのは一般提供（GA）の `gemini-3.8-flash` と、GCS のステージング領域だけを読み書きする関数ツールで、コードの実行、パッケージのインストール、外部サイトへのアクセスはしません。`FREEFORM_DESIGN_ENABLED` が `true` でなければ自由デザインは使わず、すべてテンプレートで作ります。

### 自由デザインの流れ（Cloud Run Job、`JOB_MODE=generate` / `freeform_edit`）
1. **素材の準備**：`gs://<bucket>/staging/<id>/<run_id>/input/` に依頼内容（`brief.md`）とナレッジ（`knowledge.md`）を置き、デザインルール（`app/skills/freeform-deck-designer/SKILL.md`）を指示に含めます。実行ごとに別のプレフィックスを使い、デザイナーの関数ツールはこのプレフィックスの `input/`（読み取り専用）と `deck/`（書き込み可）しか扱えません。
2. **下書き**：デザイナーエージェントが、関数ツールで `deck/` に `index.html`、`assets/`（SVG など）、`charts/*.json`（ECharts 6.1.0 の設定）、`manifest.json` を書きます。JavaScript は書かせません。
3. **AI 画像**：`manifest.json` の画像依頼（最大 4 枚、文字なし）をワーカーが `gemini-3.1-flash-image` で生成し、「AI生成イメージ」のバッジを付けます。
4. **静的検査**：`app/deck_contract.py` の `build_deck()` が、許可外のパス、外部 URL、スクリプト、壊れたグラフ JSON を検出し、配信用の `index.html` を組み立てます。
5. **描画とレイアウト検査**：非公開の Cloud Run サービス `proposal-deck-renderer`（ヘッドレス Chromium、`--no-allow-unauthenticated`）が 1 枚ずつスクリーンショットを撮り、はみ出し・重なり・グラフの描画失敗を返します。SVG の中の文字どうしの重なりと、図の枠からのはみ出しも検査します（SVG はスクリーンショットだけでは見落としやすいため）。
6. **見た目の確認ループ（最大 2 回）**：同じ ADK セッションの次のメッセージに、スクリーンショットを画像パートとして、検査結果を文章として渡します。入力資料にない数値（％）・メールアドレスや、作業用のファイル名がスライドに出ていれば、それも指摘します（根拠チェック。`check_deck` ツールでもエージェント自身が確かめます）。エージェントはファイルを直して `REVIEW_STATUS: FIXED`、直す所がなければ `REVIEW_STATUS: APPROVED` と答えます。2 回目は、見た目が変わったスライドと指摘が残ったスライドだけを送り、エージェントが自分の修正を目で確かめてから承認します。見た目が何も変わらず指摘もなければ、2 回目は省きます。
7. **公開**：検査結果が一番よい版を `presentations/<id>/v<N>/` に公開し、スクリーンショットを `_qa/` に残します。Firestore の `freeform` には、確認ループの結果（`review_rounds`）、トークン使用量、警告を記録します。
8. **代替**：公開できる版が 1 つもなければテンプレートで作り直し、`generation_engine` に `+freeform_fallback:<理由>` を付けて、その旨をユーザーに伝えます。

### デザイナーの実行基盤（ADK）
- デザイナーは `app/adk_designer.py` の ADK `LlmAgent`（既定 `gemini-3.8-flash`、`FREEFORM_ADK_MODEL`）です。GCS のステージング領域を `/workspace/job` として読み書きする関数ツール（`list_files`、`read_file`、`write_file`、`replace_in_file`、`delete_file`、`check_deck`）だけを持ちます。
- `check_deck` は公開前の契約チェック（`app/deck_contract.py`）と根拠チェック（入力資料にない数値・メールアドレス、作業用ファイル名）を返します。根拠チェックの指摘は公開を止めず、確認ループでエージェントに直させ、残った場合は警告として記録します。
- ワーカーの中に組み込んだ部品なので、`Runner` で直接動かします（`agents-cli` でデプロイする単体のエージェントではありません）。

### 自由デザイン版の修正と版管理
- `edit_proposal_website` は修正を受け付けると `EDIT_QUEUED` を返し、Cloud Run Job（`JOB_MODE=freeform_edit`）で非同期に処理します。処理中も共有 URL は修正前の版を表示して「更新中」バナーを出し、完了すると自動で新しい版に切り替えます。
- 完了後に報告するのは、公開前後のファイルを機械的に比べた差分（`verified_changes`：枚数、タイトル、本文、スタイルシート、HTML 構造の変更）だけです。デザイナーエージェントの説明は「自己申告」と明記して分けます。
- 版は直近 10 件まで残します（`freeform_versions`）。各版には修正元の版（`based_on`）を記録し、「元に戻して」は公開先を切り替えるだけで即時に戻します。テンプレート版から作り直した版を戻すと、テンプレート版に戻ります。
- テンプレート版で表現できない依頼は `NO_CHANGE` になり、コンシェルジュが自由デザイン版への作り直しを提案します。ユーザーが了承すると、`convert_to_freeform=True` で同じ URL のまま作り直します。

### 自由デザイン版の配信（Cloud Run ゲートウェイ）
- 正規 URL は `/p/<id>/`（末尾スラッシュ付き）で、テンプレート版は `/p/<id>` です。もう一方の形でアクセスされたときは 307 で正規 URL へ転送します。
- 素材は `/p/<id>/assets/...` と `/p/<id>/charts/*.json` だけを配信し、どちらもパスワード認証が必要です。描画用ランタイム（`deck-runtime.css`、`deck-runtime.js`、`echarts.min.js`）は `/_rt/v1/` から自前で配信します。
- 自由デザイン版の HTML には、リクエストごとの nonce を使った Content-Security-Policy を付け、自前のランタイム以外のスクリプトを動かしません。
- `/p/<id>/status` は `render_mode`、`design_mode`、`edit_mode` も返します。

### テンプレート版の生成（高速モード・非同期）
- `create_proposal_website` は URL・閲覧用 ID・パスワードを約 2 秒で発行し、Firestore `presentations/<id>` を `generation_status=generating` にしてから Cloud Run Job（`app/generation_worker.py`）を起動します。
- Job の生成順序: `gemini-3.8-flash` 構造化出力 → 決定的テンプレート。作成時に指定した `design_style` は全ティアで維持されます。
- 公開のたびに `content_version` を 1 つ上げます（生成完了で v1）。

### テンプレート版の修正（同期・真実性保証）
- `edit_proposal_website` はツール内で同期実行します（通常 15〜60 秒）。開始時に Firestore を `generation_status=updating`（`generation_phase`: `edit_queued` → `edit_designing` → `edit_rendering` → `edit_publishing`）にし、完了時に `ready` へ戻します。
- 修正エンジン: `gemini-3.8-flash` の構造化出力（`DeckEditResult` = 新しい `deck_spec` / `change_summary` / `unsupported_requests`）→ キーワード解析による安全網（「背景を白に」「ぜんぜん違う見た目に」など明確な見た目の指示が無視された場合だけ補正）→ 明示パラメータ（`new_title` / `new_theme_color` / `new_design_style` など）を最優先で適用。
- 返却する `verified_changes` は、修正前後の `deck_spec` を機械的に比較した結果だけです。LLM の自己申告（`designer_notes`）は補足扱いで、反映されていない変更を「反映した」と報告しない設計です。
- 変更点がなければ `NO_CHANGE` を返し、HTML は書き換えません（以前のように指示文を表紙の吹き出しへ流し込む「見せかけの変更」はしません）。
- 上書き前の HTML は `gs://<bucket>/presentations/<id>/versions/<UTC>-v<旧版>.html` に退避し、`previous_deck_spec` を保存します。「元に戻して」（`undo_last_edit=True`）で直前の版に戻せます。
- 修正中の二重依頼は `BUSY`、処理中の例外は `EDIT_FAILED`（修正前の版を維持し `ready` に戻す）。`updating` のまま 300 秒（`EDIT_STALE_SECONDS`）を超えた印は放棄されたものとみなし、`get_proposal_status` が自動で解除します。

### テンプレート版のデザインスタイル（`design_style`）とアクセントカラー（`theme_color`）
| design_style | 見た目 |
| --- | --- |
| `immersive-dark`（既定） | 濃紺背景・グロー・グラスカード（従来のデザイン） |
| `clean-light` | 白背景・スレート系の文字・白カード＋影・上部グラデーションライン |
| `editorial-light` | 生成り色（`#faf7f2`）背景・明朝見出し（Noto Serif JP）・フラットなカード・上部の黒罫線 |

- `theme_color` はアクセント色のみ（`sky` / `emerald` / `violet` / `amber` / `rose`）。
- 修正時に限り、LLM が質感調整用の `custom_css` を付けられます。`sanitize_custom_css()` が `<`・バックスラッシュ・コメントを除去し、`url(` / `@import` / `expression(` / `javascript:` などを無効化、8,000 文字に制限します。
- 6 枚構成（表紙／課題と結論／As-Is・To-Be／アーキテクチャ 4 層／ロードマップ 3 フェーズ／ROI とネクストステップ）は固定です。スライドの追加・削除や画像の埋め込みは未対応です。

### 「更新中」表示（Cloud Run ゲートウェイ）
- ゲートウェイは配信する HTML の `</body>` 直前に、静的な監視スクリプト（`#pd-live-update`）を差し込みます。スクリプトは `createElement` / `textContent` だけで DOM を組み立て、サーバー値をスクリプト本体へ埋め込みません。
- 認証付きの `GET /p/<id>/status` を 5 秒ごと（更新中は 2.5 秒ごと、タブ非表示中は停止）に確認し、`updating` なら画面上部に「プレゼンテーションを更新中です」とフェーズを表示します。`content_version` が上がると「最新版への更新が完了しました」と表示し、閲覧中のスライド位置を保ったまま自動で再読み込みします。公開停止（403）を検知した場合は表示を閉じます。
- `/status` は `content_version` と `last_edit_status` だけを返し、修正内容（`verified_changes` など）は閲覧者に公開しません。

### 主な環境変数
| 対象 | 変数 | 既定値 | 用途 |
| --- | --- | --- | --- |
| Agent | `ENABLE_LLM_DECK_EDIT` | `true` | `false` でキーワード解析のみの修正 |
| Agent | `EDIT_MODEL_TIMEOUT_SECONDS` | `75` | 修正用 Gemini 呼び出しのタイムアウト |
| Agent | `EDIT_STALE_SECONDS` | `300` | 放棄された `updating` 印の判定 |
| Gateway | `LIVE_UPDATE_WATCHER` | `true` | 「更新中」監視スクリプトの差し込み |
| Gateway | `LIVE_UPDATE_POLL_SECONDS` | `5` | 待機中のポーリング間隔 |
| Gateway | `UPDATING_STALE_SECONDS` | `300` | 古い `updating` を `ready` として配信 |
| Gateway | `FREEFORM_UPDATING_STALE_SECONDS` | `1500` | 自由デザイン版の古い `updating` を `ready` として配信 |
| Gateway | `DECK_RUNTIME_DIR` | （Docker では `/app/deck_runtime`） | `/_rt/v1/` で配信するランタイムの置き場所 |
| Agent / Job | `FREEFORM_DESIGN_ENABLED` | `false` | `true` で自由デザインを有効化（deploy.sh は `true` を設定） |
| Agent | `FREEFORM_GENERATION_STALE_MINUTES` | `22` | 自由デザインの生成が止まったとみなすまでの時間 |
| Agent | `FREEFORM_EDIT_STALE_SECONDS` | `1500` | 自由デザインの修正が止まったとみなすまでの時間 |
| Job | `DECK_RENDERER_URL` | （なし） | 非公開レンダラーの URL。未設定なら描画と見た目の確認を省略 |
| Job | `IMAGE_MODEL` | `gemini-3.1-flash-image` | AI 画像の生成モデル |
| Job | `FREEFORM_TOTAL_BUDGET_SECONDS` | `900` | 自由デザイン 1 回あたりの時間上限 |
| Job | `FREEFORM_DRAFT_DEADLINE_SECONDS` | `540` | 下書きターンの上限 |
| Job | `FREEFORM_REVIEW_ROUNDS` | `2` | 見た目の確認ループの最大回数 |
| Job | `FREEFORM_REVIEW_TURN_SECONDS` | `240` | 確認ターン 1 回あたりの上限 |
| Job | `FREEFORM_FINAL_RESERVE_SECONDS` | `60` | 公開処理のために残す時間 |
| Job | `FREEFORM_REVIEW_IMAGE_WIDTH` | `1280` | エージェントに見せるスクリーンショットの幅（px） |
| Job | `FREEFORM_ADK_MODEL` | `gemini-3.8-flash` | ADK デザイナーのモデル（`location=global`） |
| Job | `FREEFORM_ADK_MAX_LLM_CALLS` | `120` | ADK デザイナー 1 ターンあたりのモデル呼び出し上限（10〜400） |
| Renderer | `ALLOWED_BUCKETS` | （なし） | 描画を許可する GCS バケット（カンマ区切り） |

## Project Structure

```
proposal-site-publisher-agent/
├── app/         # Core agent code
│   ├── agent.py               # Main agent logic
│   ├── fast_api_app.py        # FastAPI Backend server
│   └── app_utils/             # App utilities and helpers
├── tests/                     # Unit, integration, and load tests
├── GEMINI.md                  # AI-assisted development guide
└── pyproject.toml             # Project dependencies
```

> 💡 **Tip:** Use [Antigravity CLI](https://antigravity.google/) for AI-assisted development - project context is pre-configured in `GEMINI.md`.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager (used for all dependency management in this project) - [Install](https://docs.astral.sh/uv/getting-started/installation/) ([add packages](https://docs.astral.sh/uv/concepts/dependencies/) with `uv add <package>`)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)


## Quick Start

Install `agents-cli` and its skills if not already installed:

```bash
uvx google-agents-cli setup
```

Install required packages:

```bash
agents-cli install
```

Test the agent with a local web server:

```bash
agents-cli playground
```

You can also use features from the [ADK](https://adk.dev/) CLI with `uv run adk`.

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `agents-cli install` | Install dependencies using uv                                                         |
| `agents-cli playground` | Launch local development environment                                                  |
| `agents-cli lint`    | Run code quality checks                                                               |
| `agents-cli eval`    | Evaluate agent behavior (generate, grade, analyze, and more — see `agents-cli eval --help`) |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests                                                        |
| `agents-cli deploy`  | Deploy agent to Agent Runtime                                                                |
| `agents-cli publish gemini-enterprise` | Register deployed agent to Gemini Enterprise                    || [A2A Inspector](https://github.com/a2aproject/a2a-inspector) | Launch A2A Protocol Inspector                                                        |

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
| `agents-cli scaffold enhance` | Add CI/CD pipelines and Terraform infrastructure |
| `agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Edit your agent logic in `app/agent.py` and test with `agents-cli playground` - it auto-reloads on save.

## Deployment

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```

To add CI/CD and Terraform, run `agents-cli scaffold enhance`.
To set up your production infrastructure, run `agents-cli infra cicd`.

## Observability

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.

## A2A Inspector

This agent supports the [A2A Protocol](https://a2a-protocol.org/). Use the [A2A Inspector](https://github.com/a2aproject/a2a-inspector) to test interoperability.
See the [A2A Inspector docs](https://github.com/a2aproject/a2a-inspector) for details.
