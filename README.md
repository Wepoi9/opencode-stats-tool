# OpenCode 統計表示ツール

OpenCode Desktop 2.0 の共有バックグラウンドサービス API から統計を読み取り、Tk の小型ウィンドウに表示する独立ツール。

## 起動方法

リポジトリをクローンしたディレクトリで以下を実行する。

```powershell
python opencode_stats.py
```

- OpenCode Desktop が起動済みの状態で実行する。ツール単体で共有サービスは起動しない。
- サービス登録は `~/.local/state/opencode/service.json` を読み、認証は Basic(`opencode:<password>`)。
- 登録ファイルは API リクエストごとに mtime を比較し、変化があれば URL・認証情報を再読込する。

## 表示内容

- 実行状態: 未接続 / 生成中 / 待機。生成中判定は `session.execution.started` イベントと
  `/api/session/active` への実行状態照会に基づく。step 終了だけでは待機に戻さない。
- 実効速度(tok/s): 同一セッション・同一モデルの直近 10 回の加重平均
  `Σ(output + reasoning) / Σ(streamed - created)`。
  - 計測区間はモデル応答区間(`time.created` → `time.streamed`)を優先し、ツール実行や後処理を含めない。
  - `streamed` を持たない応答は平均に混ぜず、直近 1 回の `(completed - created)` 基準の値を参考値(橙色表示)として扱う。
  - 生成途中やトークンを持たないメッセージ、経過 0.05 秒未満・600 秒超の応答はサンプルから除外。
  - 起動時・セッション切替・`session.step.ended` ごとに `/api/session/{sid}/message?type=assistant&limit=20&order=desc`
    から履歴を昇順で集計し、最新モデルの正常完了 step を最大 10 件復元する。
  - 古い応答は `(time.created, メッセージID)` の順比較で集計より先に破棄する。履歴再取得や
    重複イベントが平均を壊さない。条件は 15 秒ごとの定期再取得でも同様。
  - モデルまたは対象セッションが変わると平均をリセットする。
- last 行: 直近 1 step の単発 tok/s と計測区間(応答区間 / 完了区間)。平均との突合・デバッグ用。
- cache hit 率(累積): セッションの累積値 `read / (input + read + write) * 100`。
  read/write が両方 0 のときは「未計測」。
- キャッシュ read / write / input トークン数(累積)。いずれも `/api/session/{sid}` の
  セッション累積 `tokens` をそのまま表示し、ツール側で独自に積算しない。
- 対象モデルと対象セッションのタイトル(補助表示)。セッション切替時は統計をリセットし、前セッションの数値を引き継がない。

## 操作

- 右クリックメニュー:
  - 統計を再取得
  - 対象セッションの選択(自動追従 or 固定)。子セッション(parentID を持つサブエージェント)には `[子]` を表示
  - 常に最前面
  - 閉じる

## 実装メモ

- OpenCode 2.0.30 の `/api/event`(SSE) を購読し、起動時・イベント発火時・15 秒ごとの定期取得で
  `/api/session`、`/api/session/{sid}`、`/api/session/{sid}/message`、`/api/session/active` を読み取る。
  `session.step.ended` では該当セッションの対象同期(セッション情報 + 最新 20 件履歴)を再実行する。
  単一メッセージの詳細取得エンドポイントと再取得タイマーは使用しない。
- OpenCode には `created`(モデルリクエスト開始) / `streamed`(応答ストリーム終了) / `completed`(後処理込み完了) の
  3 つの時刻がある。`completed - created` はツール実行時間を含むため、速度計測には使わない。
- セッション一覧とメッセージ取得は API 側に `limit` / `order` / `type` を指定し、全件取得しない。
  メッセージは `type=assistant&limit=20&order=desc` で最新 20 件のみ。
- 集計はメッセージ ID と `time.created` の順序比較で重複と古い応答を除外する。
  履歴が複数モデルを含む場合、昇順の通過で新しいモデルに到達した時点で平均をリセットし、
  最終状態は最新モデルの記録のみになる。
- 実行セッションの追従時は `/api/session/{sid}` の `parentID` を確認し、子セッション(サブエージェント)の
  実行で対象を切り替えない。親セッション優先。固定(ピン)中はピン以外の実行を無視する。
- event frames は `{"id", "type", "data": {sessionID, ...}}`。15 秒ごとの `: heartbeat` コメント行も破棄する。
- SSE は live-only。切断時は 2 秒後に再接続し、対象セッションの最新確定値を再取得する。
- HTTP は標準の urllib.request で、次の制限をかける。
  - 接続先は `service.json` 登録 URL のうち scheme `http` かつホストがループバック(`localhost` / `127.0.0.1` / `::1`)の場合のみ。それ以外は未登録扱いで停止。
  - リダイレクト禁止(`HTTPRedirectHandler.redirect_request` を常時 None)。
  - プロキシ不使用(空の `ProxyHandler` を登録し、システムプロキシ設定を経由しない)。
- Tk メインスレッドは描画とキュー投入のみで、HTTP 通信は SSE 読み取りスレッドと API ワーカースレッドで実行する。
- 401 応答時は URL・認証・mtime キャッシュを破棄し、次回 `service.json` から再読み込みする。
- 標準ライブラリのみ。外部通信は 127.0.0.1 の既存サービスのみで、Secrets・会話本文を永続保存しない。
- テスト: `python -m unittest test_opencode_stats`（計測ロジック・集計・API クエリ生成、外部通信なし）。
- コード量: opencode_stats.py 約 674 行 / test_opencode_stats.py 約 202 行（空行を含む総行数）。
