# 分荷判定Bot - CLAUDE.md
# コードから読み取った実仕様（ドキュメント・伝聞ではなく app.py / yahooauction_sheet.gs が根拠）

---

## 作業ルール（Claude Code 運用ルール）

### 作業開始時に必ず行うこと
1. このCLAUDE.mdを読み、現在の仕様・実装状況を把握する
2. Google共有ドライブ「AI開発管理」のシステム仕様書を確認する
   - システム仕様書: https://docs.google.com/spreadsheets/d/1Sty7dE9tOsYOLoCJmXtoFnj4S0cQmbgG_WogxxSthHg/edit
3. 前回の作業ログを確認し、未完了タスクがないか確認する
   - 作業ログ: https://docs.google.com/spreadsheets/d/1-jspSk-pi9Epm0Z5GoyppVfCw8mXSLhJ3B-yBPoRy8U/edit

### 作業終了時に必ず行うこと
1. **CLAUDE.mdを更新**する（変更した仕様・設定を正確に反映する）
2. **システム仕様書を更新**する（変更した機能・カラム・ルールをシートに反映する）
3. **作業ログに記録**する（日時・作業者・変更ファイル・変更内容・Gitコミットハッシュ）
4. **Gitにコミット・プッシュ**してRailwayにデプロイする

### 仕様変更時のルール
- **推測で仕様を書かない**。必ずapp.pyの実コードを根拠にする
- **管理番号フォーマットは `YYMM-連番4桁`（例：2603-0001）**。旧形式（V/G/M/E付き）は新規発行しない
- **Botが浅野のメッセージを無視する**仕様は意図的なもの（ADMIN_USER_ID = "U0AL10Q1HQC"）

### 在庫期間・再判定に関する戦略方針（2026/03/17 決定）
- **実態として全チャンネルで1年以上在庫されることが普通**。現行AIの在庫期間推定は短すぎる
- **自動再判定は導入しない**。現時点では販売実績データがなく、自動判断の根拠がない
- **データ収集フェーズとして運用する**。分荷確定〜販売完了までの期間を蓄積し、チャンネル別・カテゴリー別の実績値を作ることが当面の目標
- **定期確認通知のみ実装する**（将来）: 3ヶ月ごとに「相場確認」、1年超えで「再判定検討」をSlack通知
- **AIの在庫期間推定はあくまで参考値**として扱い、実績データが蓄積されたら随時キャリブレーションする

### API制約・インフラ注意事項（重要）

**Anthropic API（Claude）**
- Tier1 / **5RPM制限**（毎分5リクエストまで）
- 同時に複数ユーザーが使うとすぐに上限に達する。高負荷時はエラーになる
- 残高 約$29（2026/03/18時点）

**Slack Rate Limit**
- Bot Token / Tier1: **1リクエスト/分**（1req/min）※ 1req/secではない
- Slackへの返信が集中すると制限にかかる可能性あり

**Railwayのログ**
- Railwayでは**ファイルへのログ保存は不可**（ストレージが永続化されない）
- ログは必ず `print()` で stdout に出力する。Railwayのダッシュボードで確認できる
- 現在の実装はすべてprintで対応済み

**冪等性（重複処理防止）の制限**
- 現在の実装: `processed_events = set()` というインメモリセットで管理
- **Railwayが再起動するとリセットされる**。再起動直後は同じイベントが2回処理される可能性あり
- 完全な冪等性が必要な場合はRedis（Railway Add-on）またはスプレッドシートへの記録が必要
- 現状はSlackの3秒タイムアウト回避のためのスレッド処理と合わせて実用上は問題少ない

### Google共有ドライブ
- フォルダURL: https://drive.google.com/drive/u/1/folders/16G96z45K2coor4QuH5NW9DWxyPNX_56D
- 作成するファイルは原則このフォルダに保存する

---

## プロジェクト構成

| ファイル | 役割 |
|---|---|
| app.py | Flaskサーバー本体。Slack Events API受信・Claude API呼び出し・全チャンネルのロジック |
| yahooauction_sheet.gs | GAS。出品管理スプレッドシートのシート定義・AI説明文/タイトル生成・CSV出力・シート初期化 |
| webhook.gs | GAS。app.py からの全 POST を受信する `doPost` エンドポイント。全シートへのデータ書き込みロジック |
| Procfile | `web: gunicorn app:app`（Railwayデプロイ用） |
| .env / .env.example | 環境変数（ANTHROPIC_API_KEY / SLACK_BOT_TOKEN のみ.env.exampleに記載） |
| manifest.json | Slack Appマニフェスト（Socket Mode OFF / Events API使用） |

---

## 環境変数一覧（app.pyから確認）

| 変数名 | 用途 | 備考 |
|---|---|---|
| ANTHROPIC_API_KEY | Claude API認証 | 必須 |
| SLACK_BOT_TOKEN | Slack Bot認証（xoxb-） | 必須 |
| MONDAY_TOKEN または MONDAY_API_TOKEN | Monday.com GraphQL API | どちらか一方 |
| SATSUEI_CHANNEL_ID | 撮影確認チャンネルID | 未設定時はスキップ |
| SHUPPINON_CHANNEL_ID | 出品保管チャンネルID | 未設定時はスキップ |
| KONPO_CHANNEL_ID | 梱包出荷チャンネルID | 未設定時はスキップ |
| STATUS_CHANNEL_ID | ステータス確認チャンネルID | 未設定時はスキップ |
| ATTENDANCE_CHANNEL_ID | 出退勤チャンネルID | 未設定時はスキップ |
| GENBA_CHANNEL_ID | 現場査定チャンネルID | 未設定時はスキップ |
| KINTAI_CHANNEL_ID | 勤怠連絡チャンネルID | 未設定時はスキップ |
| GOOGLE_SERVICE_ACCOUNT_JSON | Google Drive API認証情報（base64エンコード） | 未設定時はDriveスキップ |
| GOOGLE_DRIVE_FOLDER_ID | Driveのルートフォルダ（TakeBack商品画像） | 未設定時はDriveスキップ |

---

## エンドポイント一覧

| パス | メソッド | 用途 |
|---|---|---|
| /slack/events | POST | Slack Events API受信口。全イベントのエントリーポイント |
| /webhook | POST | Make等からの外部Webhookで分荷判定を呼び出す（レガシー） |
| /debug | GET | 環境変数設定状況確認 |
| /env-keys | GET | 全環境変数のキー名一覧 |
| /monday-setup | GET | Monday.comボードにカラムを作成（初回のみ・バックグラウンド実行） |
| /monday-setup-status | GET | /monday-setup の進捗確認（done:trueで完了） |

---

## チャンネルルーティング（app.py: process_slack_message）

メッセージ受信時、以下の順で処理を振り分ける：

0. **全チャンネル共通処理**（チャンネルルーティングより前に判定）
   - `ADMIN_USER_ID`（浅野儀頼）の投稿かつ `浅野です` で始まる → 全員向けお知らせとしてそのチャンネルに転送（浅野からのお知らせ）
   - `@浅野儀頼` メンション + `相談` を含む → 相談モード起動（AIが相談内容を整形してお知らせ形式で投稿）
   - 上記どちらも、**チャンネルのBotキャラクターで返答**する（`get_bot_role_for_channel()` で解決）
1. `在庫検索 キーワード` / `検索 キーワード` → どのチャンネルでも最優先で在庫検索
2. SATSUEI_CHANNEL_ID → `handle_satsuei_channel`（撮影確認・白洲次郎）
3. SHUPPINON_CHANNEL_ID → `handle_shuppinon_channel`（出品保管・岩崎弥太郎）
4. KONPO_CHANNEL_ID → `handle_konpo_channel`（梱包出荷・黒田官兵衛）
5. STATUS_CHANNEL_ID → `handle_status_channel`（ステータス確認・ステータス松本）
6. ATTENDANCE_CHANNEL_ID → `handle_attendance_channel`（出退勤）
7. GENBA_CHANNEL_ID → `handle_genba_channel`（現場査定・渋沢栄一）
8. KINTAI_CHANNEL_ID → `handle_kintai_channel`（勤怠連絡・サイレント記録）
9. その他すべて → 分荷判定フロー（北大路魯山人）

### `get_bot_role_for_channel(channel_id)` （2026/03/17 追加）
チャンネルIDからbot_role文字列を返すヘルパー関数。
相談モード・浅野からのお知らせで使用。未登録チャンネルはデフォルト `"bunika"` を返す。

```python
mapping = {
    SATSUEI_CHANNEL_ID:   "satsuei",
    SHUPPINON_CHANNEL_ID: "shuppinon",
    KONPO_CHANNEL_ID:     "konpo",
    STATUS_CHANNEL_ID:    "status",
    GENBA_CHANNEL_ID:     "genba",
}
return mapping.get(channel_id, "bunika")
```

### 相談コマンドの入力パターン（複数形式すべて有効）
- `@浅野儀頼 相談`
- `@浅野儀頼 相談があります`
- `@浅野儀頼 相談です`
- その他 `@浅野儀頼` メンション + `相談` を含む任意のテキスト

---

## Botキャラクター一覧（BOT_NAMES / BOT_PERSONA）

| キー | 表示名 | キャラクター | 口調 |
|---|---|---|---|
| bunika | 北大路魯山人 | 分荷判定 | 「ふむ」「よかろう」「〜じゃな」 |
| satsuei | 白洲次郎 | 撮影確認 | 短く的確。「〜だ」「悪くない」 |
| shuppinon | 岩崎弥太郎 | 出品保管 | 「〜じゃ！」「ようやった！」豪快 |
| konpo | 黒田官兵衛 | 梱包出荷 | 「承知した」「案ずるな」冷静 |
| status | ステータス松本 | ステータス確認 | 「〜やで」「ありがとさん」関西ロック |
| genba | 渋沢栄一 | 現場査定 | 「〜であります」「算盤に合う」丁寧 |

---

## 販売チャンネル（9チャンネル）

1. eBayシングル
2. eBayまとめ
3. ヤフオクヴィンテージ
4. ヤフオク現行
5. ヤフオクまとめ
6. ロット販売
7. 社内利用（表記ゆれ: 自社使用・自社利用 → normalize_channelで正規化）
8. スクラップ
9. 廃棄

---

## 管理番号フォーマット（generate_management_number）

```
形式: YYMM-[連番4桁]
例:   2603-0001

YY   = 西暦下2桁
MM   = 月2桁
連番 = Monday.comの今月のアイテム数 + 1（4桁ゼロパディング）
```

※ 旧形式（2603V0001 / 区分コードV/G/M/E付き）は廃止。今後は新形式のみ発行。
　旧形式の管理番号はシステムで参照・表示できるが、新規発行しない。

### 管理番号の発行条件
- **発行対象**（通販チャンネル）: eBayシングル・eBayまとめ・ヤフオクヴィンテージ・ヤフオク現行・ヤフオクまとめ
- **発行なし**: ロット販売・社内利用・スクラップ・廃棄 → スプレッドシートのみ転記

---

## コマンド仕様（parse_command）

すべて全角→半角・漢数字→数字に正規化されてから判定。

| コマンド | 動作 | 制約 |
|---|---|---|
| `第一` / `第1` | 第一候補で確定 | スレッド内のみ |
| `第二` / `第2` | 第二候補で確定 | スレッド内のみ |
| `確定/チャンネル名` | 指定チャンネルで確定 | スレッド内のみ |
| `再判定` | 再度AI判定を呼び出す | スレッド内のみ |
| `保留` | 保留メッセージを返す（記録なし） | スレッド内のみ |
| `削除` / `テスト` / `キャンセル` / `取消` / `取り消し` | キャンセル処理 | スレッド内のみ |
| `在庫検索 キーワード` | Monday.comをキーワード検索 | どのチャンネルでも可 |
| `検索 キーワード` | 同上（短縮形） | どのチャンネルでも可 |

---

## AIシステムプロンプト（SYSTEM_PROMPT）

### 分荷判定（bunika チャンネル）
キャラクター: **北大路魯山人**（美食家・陶芸家）

**スコアリング（100点満点）**
- 収益期待スコア: 30点（予想販売価格・安定性・付加価値）
- 在庫回転スコア: 25点（〜2ヶ月:25 / 〜4ヶ月:18 / 〜8ヶ月:10 / 1年以上:3）
- 保管・物流コスト効率スコア: 25点（サイズ・発送コスト・梱包材）
- 作業コスト効率スコア: 20点（梱包難易度・撮影・問い合わせ複雑さ）

**確信度の計算**
- 75点以上 → 高
- 50〜74点 → 中
- 50点未満 → 低

**商品状態の選択肢（4択）**
- 中古 / ジャンク・現状品 / 中古美品 / 新品・未使用品

### 現場査定（genba チャンネル）
キャラクター: **渋沢栄一**（論語と算盤）

**チャンネル別目標粗利率**
- eBayシングル・ヤフオクヴィンテージ: 8%
- ヤフオク現行: 25%
- eBayまとめ: 20%
- ヤフオクまとめ・ロット販売: タダ引き推奨（0%）

**プラットフォーム手数料**
- ヤフオク: 10%
- eBay: 約13%

**3つの判定タイプ**
1. 買取査定（写真・商品テキスト）
2. 廃棄・処分判断（「廃棄」「処分」「捨てる」「どうする」キーワード）
3. 知識インプット（「メモ」「情報」「覚えておいて」「相場」「業者」「単価」「注意」「ポイント」「コツ」）

---

## 内部キーワード体系

```
形式: /[発送コード][サイズ]/[価格コード]/[期待値]
例:   /S140/4S/2
```

| 要素 | コード | 意味 |
|---|---|---|
| 発送コード | S | 佐川急便（標準） |
| | Y | ヤマト運輸 |
| | SU | 西濃運輸（大型家具・家電） |
| | AD | アートデリバリー（超大型・美術品） |
| | DC | 購入者直接引取り |
| サイズ（三辺合計cm） | 60/80/100/120/140/160/170/200 | |
| 価格コード | J | ×10（例: 5J = 50円） |
| | H | ×100（例: 5H = 500円） |
| | S | ×1,000（例: 4S = 4,000円） |
| | M | ×10,000（例: 3M = 30,000円） |
| 期待値 | 1 | 買い手市場 |
| | 2 | 市場拮抗 |
| | 3 | 売り手市場 |

---

## スプレッドシート転記カラム（GAS経由 / GAS_URL宛にPOST）

### 確定時（action指定なし）
| フィールド | 内容 |
|---|---|
| kanri_bango | 管理番号（通販系のみ。非通販は空文字） |
| kakutei_channel | 確定チャンネル名 |
| first_channel | AI第一候補チャンネル |
| second_channel | AI第二候補チャンネル |
| item_name | アイテム名 |
| maker | メーカー/ブランド |
| model_number | 品番/型式 |
| condition | 状態 |
| predicted_price | 予想販売価格（例: ¥3,000〜¥5,000） |
| start_price | 推奨スタート価格（数値） |
| target_price | 推奨目標価格（数値） |
| inventory_period | 予測在庫期間 |
| inventory_deadline | 推奨在庫期限 |
| score | 総合スコア（数値） |
| storage_cost | 保管コスト概算（数値） |
| packing_cost | 梱包・発送コスト概算（数値） |
| expected_roi | 期待ROI（数値%） |
| internal_keyword | 推定内部KW（例: /S140/4S/2） |
| staff_id | スタッフコード |
| sakugyou_jikan | 分荷作業時間（分。投稿〜確定コマンドの経過時間） |
| timestamp | 確定日時（YYYY/MM/DD HH:MM） |

### キャンセル時
| フィールド | 内容 |
|---|---|
| kanri_bango | 管理番号（なければ "---"） |
| kakutei_channel | "キャンセル（元チャンネル名）" |
| staff_id | ユーザーID |
| timestamp | 日時 |

### その他のアクション（actionフィールドで識別）
| action値 | 送信タイミング | 主要フィールド |
|---|---|---|
| checklist_update | 動作確認チェックリスト完了時 | kanri_bango / condition / checklist_comment |
| satsuei_update | 撮影完了（`完了`入力）時 | kanri_bango / drive_folder_url |
| work_activity | 作業ログ（完了・キャンセル・削除） | channel / kanri_bango / operation / duration_seconds |
| shuppinon_listing | 出品登録（ロケーション番号入力）時 | kanri_bango / title / description / condition / start_price / size / location |
| shipping_update | 出荷手配完了時 | kanri_bango / carrier / tracking_number |
| kobutsu_daichou | 古物台帳登録（`登録`入力）時 | timestamp / item_name / price / name / address / birthdate / id_number / doc_type |
| genba_memo | 知識インプット時 | staff_id / message |
| genba_satei | 現場査定結果時 | staff_id / input / result |
| attendance | 出退勤申告時 | staff_id / date / start_time / end_time / total_minutes / break_minutes / net_hours / completed_count |
| kintai_renraku | 勤怠連絡チャンネル投稿時 | staff_id / message |

---

## Monday.comカラム（通販チャンネル確定時のみ登録）

ボードID: `18404143384`
※54カラム作成済み（2026/03/17 /monday-setup 実行完了）

### 自動書き込みカラム一覧

| 工程 | カラムID | 型 | 書き込みタイミング |
|---|---|---|---|
| 共通 | kanri_bango | text | 分荷確定時 |
| 共通 | status | status | 各工程完了時に自動更新 |
| 分荷 | hantei_channel | text | 分荷確定時 |
| 分荷 | kakushin_do | text | 分荷確定時（高/中/低。キャンセル時は"キャンセル"） |
| 分荷 | toshosha | text | 分荷確定時（担当者名） |
| 分荷 | zaiko_kikan | text | 分荷確定時 |
| 分荷 | yosou_kakaku | numbers | 分荷確定時 |
| 分荷 | score | numbers | 分荷確定時 |
| 分荷 | sakugyou_jikan | numbers | 分荷確定時（分） |
| 分荷 | internal_keyword | text | 分荷確定時 |
| 分荷 | maker | text | 分荷確定時 |
| 分荷 | model_number | text | 分荷確定時 |
| 分荷 | condition | text | 分荷確定時・動作確認完了時に更新 |
| 分荷 | kaishi_kakaku | numbers | 分荷確定時（推奨スタート価格） |
| 分荷 | mokuhyo_kakaku | numbers | 分荷確定時（推奨目標価格） |
| 分荷 | bunka_date | date | 分荷確定時（今日の日付） |
| 撮影 | satsuei_tantosha | text | 撮影完了時 |
| 撮影 | satsuei_date | date | 撮影完了時 |
| 撮影 | drive_url | text | 撮影完了時（DriveフォルダURL） |
| 出品 | shuppinon_tantosha | text | 出品登録時 |
| 出品 | shuppinon_date | date | 出品登録時 |
| 出品 | location | text | 出品登録時（保管ロケーション番号） |
| 出品 | shuppinon_jikan | numbers | 出品登録時（作業時間・分） |
| 梱包 | konpo_tantosha | text | 梱包完了時 |
| 梱包 | konpo_date | date | 梱包完了時 |
| 出荷 | carrier | text | 追跡番号入力時 |
| 出荷 | tracking_number | text | 追跡番号入力時 |
| 出荷 | shukka_date | date | 追跡番号入力時 |

### 手動入力カラム（Monday.com上で直接入力）
satei_tantosha / satei_date / shiire_genka / category / item_name /
rakusatsu_date / rakusatsu_kakaku / nyusatsu_count / access_count / zaiko_days /
platform_fee / total_genka / total_rodo_jikan / total_rodohi / arari / junri / roi / rieki_ritsu / memo

### ステータスラベルの遷移
```
【メインフロー（自動）】
分荷確定     （分荷判定確定時）
  → 動作確認済み （チェックリスト完了時）
  → 撮影済み     （撮影チャンネルで完了入力時）
  → 出品待ち     （出品チャンネルでロケーション入力時）
  → 出品中       （Yahoo/eBayに実際に出品後・手動）
  → 落札済み     （落札後・手動）
  → 入金待ち     （落札後入金前・手動）
  → 入金確認済み （入金確認後・手動）※入金確認後に梱包チャンネルへ
  → 梱包作業     （梱包チャンネルで梱包完了入力時）
  → 出荷待ち     （追跡番号入力完了時）
  → 完了         （手動）

【例外・分岐（手動）】
  再リスト    ← 同チャンネルで再出品
  再分荷      ← チャンネル変更・分荷判定からやり直し
  確認／相談 ← 要判断・トラブル（削除コマンド時に自動）
  キャンセル  ← 処理終了（キャンセルコマンド時に自動）
```

---

## 工程別 自動化状況（2026/03/17 現在）

| 工程 | Slack操作 | Monday.com自動更新 | 備考 |
|---|---|---|---|
| 分荷確定 | 「第一」「第二」コマンド | 分荷確定 + 商品情報一括書込 | ✅ 完全自動 |
| 動作確認 | S/A/B/C/Dランク入力 | 動作確認済み + 状態更新 | ✅ 完全自動 |
| 撮影 | 白洲次郎チャンネルで「完了」 | 撮影済み + 担当者/日付/DriveURL | ✅ 完全自動 |
| 出品準備 | 岩崎弥太郎チャンネルでロケーション入力 | 出品待ち + 担当者/日付/場所 | ✅ 完全自動 |
| 出品中 | ー | ー | ⚠️ 手動（ヤフオク/eBay出品後にMonday.com更新） |
| 落札 | ー | ー | ⚠️ 手動 |
| 入金確認 | ー | ー | ⚠️ 手動（入金確認済みになったら梱包チャンネルへ） |
| 梱包 | 黒田官兵衛チャンネルで「梱包完了」 | 梱包作業 + 担当者/日付 | ✅ 完全自動 |
| 出荷 | 追跡番号入力（OCR対応） | 出荷待ち + 運送会社/追跡番号/出荷日 | ✅ 完全自動 |
| 完了 | ー | ー | ⚠️ 手動 |

**業務ルール**：入金確認が取れたものしか出荷しない（梱包チャンネルは入金確認済み後に使う）

---

## Claudeモデル使用（app.py内）

| 用途 | モデル | max_tokens |
|---|---|---|
| 分荷判定メイン | claude-sonnet-4-6 | 2048 |
| 現場査定（genba） | claude-sonnet-4-6 | 1024 |
| 身分証情報抽出 | claude-sonnet-4-6 | 512 |
| 管理番号読取（テプラOCR） | claude-haiku-4-5-20251001 | 50 |
| 追跡番号OCR（送り状） | claude-haiku-4-5-20251001 | 100 |
| 出品コンテンツ生成 | claude-haiku-4-5-20251001 | 600 |
| GAS（yahooauction_sheet.gs） | claude-opus-4-6 | 2000 |

---

## 各チャンネルの詳細フロー

### 分荷判定チャンネル（デフォルト）

1. 商品情報またはテプラ画像を投稿 → Claude判定結果を返信
2. スレッド内でコマンド入力 → 確定/再判定/保留/キャンセル
3. 確定後 → スプレッドシート転記 + 通販チャンネルならMonday.com登録 + 動作確認チェックリスト表示
4. チェックリストで `番号+コメント` 入力 → 状態記録 + Monday.comステータス更新

### 撮影確認チャンネル（handle_satsuei_channel）

1. テプラ写真または管理番号テキストを新規投稿 → セッション開始
   - テプラ写真: Claude Visionで管理番号OCR + Driveにテプラ画像保存
   - テキスト: 正規表現 `\d{4}[VGME]\d{4}` で管理番号抽出
2. スレッドに商品写真を投稿 → Driveにアップロード（02_商品.jpg, 03_商品.jpg...）
3. `完了` 入力 → Monday.com「撮影済み」に更新 + スプレッドシート記録

### 出品保管チャンネル（handle_shuppinon_channel）

1. テプラ写真または管理番号テキストを新規投稿
2. Monday.comからデータ取得 → Claude（Haiku）で出品タイトル・説明文・開始価格を生成
3. 修正コマンドで調整（`タイトル：` / `開始価格：` / `説明文：` / `サイズ：`）
4. ロケーション番号（A-12など）を入力 → 出品登録完了 + Monday.com「出品中」に更新

### 梱包出荷チャンネル（handle_konpo_channel）

1. 管理番号またはテプラ写真を新規投稿 → 梱包サイズ・判定チャンネル・予想価格を表示
2. `梱包完了` または `梱包` 入力 → 運送会社メニュー表示（1〜5）
3. 運送会社を選択:
   - 1(佐川) / 2(アート) / 3(西濃): 送り状ラベル写真を要求 → OCRで追跡番号抽出 → 出荷完了
   - 4(直接引き取り): 即時完了
   - 5(後日発送): セッション終了。後日 `管理番号 運送会社 伝票番号` 形式で新規投稿
4. Monday.com「出荷済み」に更新 + スプレッドシート記録

**後日発送の入力形式**:
```
2603G0001 佐川 123456789012
```
運送会社キーワード: 佐川→佐川急便 / アート→アートデリバリー / 西濃→西濃運輸

### ステータス確認チャンネル（handle_status_channel）

- 管理番号（テキスト or テプラ写真）を送ると、Monday.comから以下を返す
  - ステータス / 判定チャンネル / 予想販売価格 / 在庫予測期間 / スコア / 登録からの経過日数

### 出退勤チャンネル（handle_attendance_channel）

- `9:00~16:00` 形式で勤務時間を自己申告（`~` / `-` / `～` すべて対応）
- スタッフマスターから標準休憩時間を取得して実働計算
- 当日の作業実績（完了・キャンセル・削除件数）とともに記録
- GAS経由でスプレッドシートに転記後、daily_statsをリセット

### 現場査定チャンネル（handle_genba_channel）

**古物台帳フロー（最優先）**:
1. `買取確定 ¥3000` → フロー開始
2. 品物名テキストを入力
3. 身分証写真を送信 → Claude Visionで自動抽出（マイナンバーは非記録）
4. 内容確認後 `登録` → スプレッドシートの古物台帳シートに記録
5. 修正コマンド: `修正 氏名：正しい名前`（氏名/住所/生年月日/証明書番号/確認書類）

**通常フロー**:
- 知識キーワードがある場合 → スプレッドシートに保存して完了
- それ以外（写真・査定テキスト）→ Claude（Sonnet）で買取査定または廃棄判断を返す

### 勤怠連絡チャンネル（handle_kintai_channel）

- テキスト投稿をすべてサイレントでスプレッドシートに記録（返信なし）

---

## STAFF_MAP（UserID対応表）

```python
STAFF_MAP = {
    "U0AL10Q1HQC": "浅野儀頼",
    "U0ALQ4BJNSV": "林和人",
    "U0AL4R1EMMZ": "平野光雄",
    "U0ALKDQEC2F": "桃井侑菜",
    "U0ALV7C2EHJ": "伊藤佐和子",
    "U0AM4HG1PRP": "奥村亜優李",
    # 横山優 / 三島圭織 / 松本豊彦 / 北瀬孝 / 白木雄介 → UserID未確定・コメントアウト中
}
```
未登録UserIDはそのままUserIDをスタッフ名として使用する（get_staff_code）。

---

## Google Drive フォルダ構成

```
GOOGLE_DRIVE_FOLDER_ID（ルート）/
  └── YYMM/（例: 2603）
        └── 管理番号/（例: 2603-0001）
              ├── 01_テプラ.jpg（テプラ画像）
              ├── 02_商品.jpg
              └── 03_商品.jpg ...
```

---

## 重複処理防止

- `processed_events` セットで event_id を管理
- 1000件超えたら全クリア
- Bot自身の投稿・file_share以外のsubtypeは無視

---

## GAS（yahooauction_sheet.gs + webhook.gs）主要機能

スプレッドシートID: `1CWG9MVrsw9gJwp31lCrUs9KB0a1zptZY1cPO47ZNmVU`（app.pyのGAS_URLと同一）

### シート構成
| シート名 | 書き込みタイミング | 用途 |
|---|---|---|
| 出品管理 | 分荷確定（通販のみ）・撮影完了・出品登録・出荷完了時に自動更新 | CSV出力メインシート |
| 分荷確定ログ | 全確定時（通販・非通販問わず） | 全案件の履歴 |
| 古物台帳 | 現場査定チャンネルで「登録」時 | 法定台帳 |
| 出退勤記録 | 出退勤チャンネルで申告時 | 勤怠管理 |
| 作業ログ | 完了・キャンセル・削除時 | 作業実績 |
| 勤怠連絡 | 勤怠連絡チャンネル投稿時 | サイレント記録 |
| 現場査定記録 | 現場査定結果送信時 | 買取・廃棄履歴 |
| 現場メモ | 知識インプット時 | 相場・業者情報 |
| 送料_佐川 | 初回のみ（手動） | 佐川運賃表 |
| 送料_西濃ミニ便 | 初回のみ（手動） | 西濃運賃表 |
| 送料_アートデリバリー | 初回のみ（手動） | アート料金表 |
| 説明文テンプレート | 初回のみ（手動） | AI生成用テンプレート |
| 設定マスタ | 初回のみ（手動） | アカウント区分・担当マーク等 |

### 出品管理シート 列構成（CSV出力対象）
管理番号 / アカウント区分 / アイテム名 / メーカー/ブランド / 品番/型式 / 状態 / 内部KW / 担当者 / 分荷確定日時 /
発送会社 / 発送サイズ / 発送重量目安(kg) / **保管ロケーション** / 出品タイトル(65文字以内) / カテゴリID / 開始価格 /
説明文 / 画像フォルダURL / 画像1〜5URL / 出品期間(日) / 終了時刻 / 自動再出品回数 /
出品ステータス / 出品日時 / 終了予定日時 / 落札価格 / 落札者ID / 在庫日数(分荷〜落札) / 送料×11地域 / 沖縄

### 出品ステータスの遷移（出品管理シート）
```
未出品（分荷確定時）
  → 撮影済み（satsuei_update 受信時）
  → 出品中（shuppinon_listing 受信時）
  → 出荷済み（shipping_update 受信時）
  → キャンセル（キャンセルコマンド時）
```

### カスタムメニュー（出品管理）
1. DBから商品追加（手動：管理番号で出品管理に行追加）
2. タイトル生成（AI）
3. 説明文生成（AI）
4. ヤフオクCSV出力（未出品のみ・BOM付きUTF-8）
5. スプレッドシート初期化（初回のみ・データ消去注意）

### webhook.gs doPost のデプロイ手順（初回のみ）
1. GASエディタ → デプロイ → 新しいデプロイ
2. 種類: **ウェブアプリ** / 実行ユーザー: **自分** / アクセス: **全員**
3. 発行されたURLを `app.py の GAS_URL` に設定する

### 出品担当マーク（ヤフオクタイトル先頭）
| コード | マーク | 担当者 |
|---|---|---|
| KH | 〇 | 林和人 |
| YY | ▽ | 横山優 |
| TK | ◇ | 鶴岡 |

タイトルは65文字制限。マーク+スペース分を除いた文字数をAIに渡す。

### 発送元
岐阜県（SHIPPING_FROM）

### オークション設定固定値
- 出品期間: 4日
- 終了時刻: 22:00
- 自動再出品回数: 3回
- 返品: 不可
- 商品状態: 中古

---

## ドキュメント一覧（docs/）

| ファイル | 対象チャンネル | 最終更新 |
|---|---|---|
| `docs/分荷判定_作業マニュアル.html` | 分荷判定チャンネル（北大路魯山人） | 2026/03/17 |
| `docs/商品撮影_作業マニュアル.html` | 撮影確認チャンネル（白洲次郎） | 2026/03/17 新規作成 |

---

## 次の実装タスク（コードのTODOコメントから）

- `execute_listing` 内のTODO: ヤフオクAPI自動出品（オークタウンAPI確認後・4/1以降実装予定）
- `extract_tracking_number_from_image`: `anthropic.Anthropic()` を直接呼んでいる（`get_anthropic_client()` に統一すべき）
- 残りチャンネルのマニュアル作成: 出品保管（岩崎弥太郎）/ 梱包出荷（黒田官兵衛）/ ステータス確認（ステータス松本）/ 出退勤 / 現場査定（渋沢栄一）
