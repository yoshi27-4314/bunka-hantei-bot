# 開発基準

## エラーハンドリング
- 全API呼び出し・外部通信にtry-exceptを入れること
- Slackにはユーザー向けの日本語メッセージのみ表示。内部エラー（{e}）は表示しない
- APIタイムアウトは最大30秒
- エラーが起きてもプロセス全体がクラッシュしないこと

## Bot自身への誤反応防止
- Bot自身の投稿には絶対に反応しない（bot_id または bot_profile で判定）
- 同じevent_idには1回だけ反応する（OrderedDictで管理）
- Slackリトライ（X-Slack-Retry-Num）は即座に200を返して無視

## ログ出力
- 全処理の開始と終了をprint()で出力
- Railwayではファイルログ不可。print()のstdout出力のみ
- ログにAPIキー・トークン・パスワードを絶対に含めない

## セキュリティ
- APIキー・トークンはコードに直書きしない。環境変数を使う
- Slack署名検証（SLACK_SIGNING_SECRET）でリクエストを検証
- /debug, /env-keysはADMIN_API_TOKEN認証必須
- /webhookはWEBHOOK_SECRET認証必須

## API制約
- Anthropic: Tier1 / 5RPM制限
- Slack Bot Token: Tier1
- Railway: インメモリデータは再起動でリセットされる
- Monday.com: 1分あたり10,000 complexityポイント
