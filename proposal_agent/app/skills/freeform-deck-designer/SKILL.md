---
name: freeform-deck-designer
description: >-
  Output contract and quality rubric for free-form HTML presentations written by
  the ADK designer agent (gemini-3.8-flash) through GCS-backed file tools. The
  agent writes plain HTML/CSS, SVG diagrams, ECharts option JSON and optional AI
  image requests; a shared runtime adds scaling, navigation, animations, charts
  and badges. Use when drafting, reviewing (from screenshots) or editing a
  free-form deck under /workspace/job/deck/.
---

# 自由デザイン プレゼンテーション 制作ルール（DESIGN_RULES）

あなたが書くのはスライドの「中身」だけです。拡大縮小・ページ送り・進捗バー・印刷・「更新中」表示・グラフ描画・AI 画像の表示は、公開時に差し込まれる共通ランタイム（`/_rt/v1/`）が担当します。ルールに反する要素は公開前に自動で削除されます。

## 1. ファイル構成（すべて `/workspace/job/deck/` の下）

| ファイル | 必須 | 内容 |
|---|---|---|
| `index.html` | 必須 | スライド本体。UTF-8 |
| `charts/<name>.json` | 任意 | ECharts の option（純粋な JSON） |
| `assets/<name>.svg` | 任意 | 図解・アイコン（スクリプトなし） |
| `image_requests.json` | 任意 | AI 画像の依頼（最大 4 件） |
| `manifest.json` | 必須 | コンセプト・スライド一覧・数値の出典 |

作業用のスクリプトやメモを `deck/` に置いても公開されません（`assets/` `charts/` 以外は無視）。

## 2. index.html の骨格

```html
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>提案タイトル</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&display=swap">
  <style>
    :root { --ink: #0f172a; --accent: #2563eb; }
    .pd-slide { background: #fff; color: var(--ink); padding: 96px 120px; font-family: 'Noto Sans JP', sans-serif; }
  </style>
</head>
<body>
  <main id="pd-deck">
    <section class="pd-slide" data-pd-title="表紙"> … </section>
    <section class="pd-slide" data-pd-title="課題"> … </section>
  </main>
</body>
</html>
```

- `<main id="pd-deck">` はちょうど 1 つ。その**直下**の `<section class="pd-slide">` が 1 枚のスライドです（3〜20 枚。特に指定がなければ 8〜12 枚）。
- 各スライドは **1920×1080 px の固定キャンバス**です。ランタイムが画面に合わせて拡大縮小します。`vw` `vh` ではなく px で設計してください。
- `data-pd-title` に短いスライド名を付けます（ナビゲーションと検査結果に使われます）。
- スライドの外側（`body` など）の装飾は不要です。各 `.pd-slide` に背景を付けてください。
- 中身が枠からはみ出してはいけません。スクロールは発生しません。

## 3. 禁止事項（自動で削除されます）

- `<script>`、`on*` 属性（`onclick` など）、`javascript:` URL
- 外部リソース（Google Fonts 以外）。Tailwind CDN・GSAP・Font Awesome・jsDelivr などの CDN は使えません。CSS とインライン SVG で表現してください
- Web 上の画像、`<iframe>` `<video>` `<audio>` `<form>` `<input>` `<base>` `<meta http-equiv>`
- CSS の `@import`（Google Fonts の CSS を除く）と `url()`（`assets/` 内のファイルと `data:image/...` を除く）
- 予約済み id：`pd-stage` `pd-nav` `pd-progress` `pd-counter` `pd-live-update` `pd-update-banner`
- `position: fixed`（ランタイムの拡大縮小と衝突します）

## 4. 動き（JavaScript は書かない。data 属性で指定）

| 属性 | 効果 |
|---|---|
| `data-pd-reveal`（値：空・`fade`・`zoom`・`left`・`right`） | スライド表示時に順番に現れる |
| `data-pd-delay="200"` | 表示開始の遅延（ミリ秒） |
| `data-pd-countup="38"` ＋ `data-pd-suffix="%"` `data-pd-prefix="¥"` `data-pd-decimals="1"` `data-pd-duration="1200"` | 数字のカウントアップ（要素の中身は最終値を入れておく） |
| `data-pd-tabs`（親）、`data-pd-tab="a"`（ボタン）、`data-pd-panel="a"`（パネル） | タブ切り替え。表示中のパネルとタブに `.is-active` が付く |
| `data-pd-bleed` | 意図的に枠外へ広げる装飾（はみ出し検査の対象外） |

動きは控えめに。1 枚あたり 3〜6 要素まで。

## 5. グラフ（ECharts 6.1）

```html
<div class="pd-chart" data-chart="charts/inquiries.json" data-pd-estimate style="width: 1100px; height: 560px;"></div>
```

- `charts/<name>.json` は ECharts の option オブジェクトです。関数・`<`・`>` は使えません（文字列から自動除去）。
- コンテナには必ず px で幅と高さを指定してください。
- 文字サイズは未指定なら読みやすい大きさ（本文 22px・軸 20px）が補われます。小さくしないでください。
- 暗い背景の上では `data-chart-theme="dark"` を付けます。
- **数値の出典ルール**：数値は `brief.md` と `knowledge.md` にあるものだけを使います。それ以外の数値（目標値・効果の見込みなど）を描くときは、グラフに `data-pd-estimate`（「試算」バッジ）または `data-pd-estimate="イメージ"` を付け、`manifest.json` の `data_sources` に `estimate` と記録します。
- 軸は 0 起点・等間隔を基本にし、単位を必ず書きます。

## 6. 図解（SVG）

- `assets/*.svg` に保存して `<img src="assets/flow.svg" alt="…">` で置くか、HTML にインライン `<svg>` で書きます。
- SVG 内の文字は、スライド上で 20px 以上に見える大きさにします。
- ラベル同士・ラベルと線を重ねません。矢印のラベルは線から 8px 以上離すか、ラベルの背面に背景色の矩形を先に描きます。
- 箱の中の文字は、箱の幅に収まる長さに改行・要約します（`<tspan>` で改行）。文字を描いたあとに、同じ場所へ塗りのある図形を描かないでください。
- 描画サービスは SVG 内の文字の重なり・隠れ・はみ出しと、線・矢印・枠線がラベルの中央を横切る箇所を自動で検査し、エラーとして返します。
- `<script>`・`<foreignObject>`・外部参照は使えません。

## 7. AI 生成画像（任意・最大 4 枚）

```json
[
  {"path": "assets/ai/hero.png", "prompt": "Abstract flowing light lines over a deep navy gradient, calm and premium, no text", "aspect_ratio": "16:9"}
]
```

- `image_requests.json` に書くと、下書きのあとでワーカーが `gemini-3.1-flash-image` で生成し、指定パスに保存します。下書きの時点では画像ファイルはまだありません（それで問題ありません）。
- `path` は `assets/ai/<英小文字・数字・ハイフン>.png`。`aspect_ratio` は `16:9` `4:3` `1:1` `3:4` `9:16` のいずれか。
- 画像に文字・ロゴ・人物の顔のアップを入れない指示にしてください（プロンプトは英語可）。
- 配置は `<img src="assets/ai/hero.png" data-pd-ai-image alt="…">`。ランタイムが「AI生成イメージ」と表示します。CSS の背景には使えません。
- 実在の製品画面・人物・ロゴの代わりには使いません。雰囲気づくりや抽象的なイメージに限ります。
- 見た目の確認で画像を差し替えたいときは、同じ `path` のまま `prompt` を書き換えます（ワーカーが再生成します）。生成された画像ファイル自体は編集・削除しないでください。

## 8. manifest.json

```json
{
  "concept": "配色・書体・レイアウトの考え方を 1〜2 文で",
  "slides": [{"index": 1, "title": "表紙", "message": "このスライドで伝えること"}],
  "data_sources": [{"slide": 4, "item": "問い合わせ件数", "source": "brief|knowledge|estimate"}]
}
```

## 9. 品質基準（見た目の確認でもこの観点で見ます）

1. **1 枚 1 メッセージ**。タイトルは要点を言い切る（「課題」ではなく「問い合わせの 6 割が定型質問」）。
2. **文字サイズ**：タイトル 56px 以上、本文 24px 以上（脚注でも 18px 以上）。1 行は全角 40 文字程度まで。
3. **コントラスト**：本文は WCAG AA（4.5:1）以上。背景画像の上の文字には下地を敷く。
4. **余白と整列**：外周 80px 以上の安全域。要素は格子に沿って揃える。
5. **変化のあるレイアウト**：箇条書きだけのスライドを続けない。数字・図解・グラフ・対比・タイムラインを使い分ける。
6. **一貫したデザインシステム**：色は 3〜5 色、書体は 2 種類まで、角丸や線の太さを統一。
7. **自然な日本語**。製品名は現行の正式名称（Gemini Enterprise、Gemini Enterprise Agent Platform、Agent Runtime、Gemini 3.8 Flash、BigQuery、Cloud Run など）で書き、旧ブランド名や旧世代モデル名は使わない。
8. **根拠のある数値だけ**：数値（％・金額・件数）・事例・連絡先は brief.md と knowledge.md（修正時は依頼文と公開中の版）にあるものだけを使う。それ以外の数値は「試算」「イメージ」と明記し、メールアドレスや URL を作らない。brief.md などの作業用ファイル名や /workspace/job のパスもスライドに書かない。`check_deck` が根拠のない数値・メールアドレス・作業用ファイル名を指摘したら直す。

## 10. 見た目の確認ターン（2 回目以降のメッセージ）

ワーカーが公開用に無害化した deck をヘッドレス Chrome で描画し、各スライドのスクリーンショットと自動検査の結果を送ります。画像を実際に見て、必要なら `deck/` のファイルを直接修正してください。新しいスクリーンショットは撮れません（修正後にワーカーが再描画します）。

返答の最終行には必ず次のどちらかを書きます。

- `REVIEW_STATUS: FIXED`（ファイルを修正した）
- `REVIEW_STATUS: APPROVED`（修正の必要がない）
