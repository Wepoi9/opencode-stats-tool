# OpenCode 統計表示ツール

OpenCode Desktop 2.0 の共有バックグラウンドサービス API から統計を読み取り、Tk の小型ウィンドウに表示する独立ツール。

## 起動方法

リポジトリをクローンしたディレクトリで以下を実行する。

```powershell
python opencode_stats.py
```

- OpenCode Desktop が起動済みの状態で実行する。ツール単体で共有サービスは起動しない。
- サービス登録は `~/.local/state/opencode/service.json` を読み、認証は Basic(`opencode:<password>`)。
- 登録ファイルの変化は 5 秒毎に確認し、Desktop 再起動で URL・認証情報が変わっても自動で再接続する。

## 表示内容

- 実行状態: 未接続 / 生成中 / 待機。生成中判定は `session.execution.started` イベントと
  `/api/session/active` への実行状態照会に基づく。step 終了だけでは待機に戻さない。
- 実効速度: 最新の正常完了 step の `(output + reasoning) / (created - completed)`。
  - 生成開始から完了までの実効 tok/s。括弧内は計測に使った経過秒。
  - 経過 0.05 秒未満の一括出力・600 秒超・時刻やトークンを持たない応答は「計測不可」表示。
  - 一覧は最新のものから最大 5 件まで遡り、`time.completed` と `tokens` を持つ最初の assistant メッセージだけを採用する。生成途中のメッセージで確定済みの数値を上書きしない。
- cache hit 率: `read / (input + read + write) * 100`、同一 step 単位。read/write が両方 0 のときは「未計測」。
- キャッシュ読み取り・書き込みトークン数。
- 対象モデルと対象セッションのタイトル(補助表示)。セッション切替時は統計をリセットし、前セッションの数値を引き継がない。

## 操作

- 右クリックメニュー:
  - 統計を再取得
  - 対象セッションの選択(自動追従 or 固定)
  - 常に最前面
  - 閉じる

## 実装メモ

- OpenCode 2.0.30 の `/api/event`(SSE) を購読し、起動時とイベント発火時に `/api/session`、`/api/session/{sid}`、`/api/session/{sid}/message`、`/api/session/{sid}/message/{mid}`、`/api/session/active` を読み取る。統計は `session.step.ended` 発火時に該当メッセージ詳細から再計算する。
- event frames は `{"id", "type", "data": {sessionID, ...}}`。15 秒ごとの `: heartbeat` コメント行も破棄する。
- SSE は live-only。切断時は 2 秒後に再接続し、対象セッションの最新確定値を再取得する。
- HTTP は標準の urllib.request で、次の制限をかける。
  - 接続先は `service.json` 登録 URL のうち scheme `http` かつホストがループバック(`localhost` / `127.0.0.1` / `::1`)の場合のみ。それ以外は未登録扱いで停止。
  - リダイレクト禁止(`HTTPRedirectHandler.redirect_request` を常時 None)。
  - プロキシ不使用(空の `ProxyHandler` を登録し、システムプロキシ設定を経由しない)。
- Tk メインスレッドは描画とキュー投入のみで、HTTP 通信は SSE 読み取りスレッドと API ワーカースレッドで実行する。
- 401 応答時は URL・認証・mtime キャッシュを破棄し、次回 `service.json` から再読み込みする。
- 標準ライブラリのみ。外部通信は 127.0.0.1 の既存サービスのみで、Secrets・会話本文を永続保存しない。
- コード量約 525 行（空行を含む総行数）。
