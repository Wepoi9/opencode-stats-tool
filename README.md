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

- 実行状態: 未接続 / 生成中 / 待機
- 参考応答速度: 最新の確定 step の `(output + reasoning) / (completed - streamed)`。
  - 計測時間が極端に短い一括出力や時刻を持たない応答は「計測不可」表示。
  - 実デコード速度ではなく応答処理時間に基づく参考値。
- cache hit 率: `read / (input + read + write) * 100`、最新応答step単位。
- キャッシュ読み取り・書き込みトークン数。
- 対象モデルと対象セッションのタイトル(補助表示)。

## 操作

- 右クリックメニュー:
  - 統計を再取得
  - 対象セッションの選択(自動追従 or 固定)
  - 常に最前面
  - 閉じる

## 実装メモ

- OpenCode 2.0.30 の `/api/event`(SSE) を購読し、起動時とイベント発火時に `/api/session`、`/api/session/{sid}`、`/api/session/{sid}/message`、`/api/session/{sid}/message/{mid}`、`/api/session/active` を読み取る。
- event frames は `{"id", "type", "data": {sessionID, ...}}`。15 秒ごとの `: heartbeat` コメント行も破棄する。
- SSE は live-only。切断時は 2 秒後に再接続し、対象セッションの最新確定値を再取得する。
- 標準ライブラリのみ。外部通信は 127.0.0.1 の既存サービスのみで、 Secrets・会話本文を永続保存しない。
- コード量約 430 行。
