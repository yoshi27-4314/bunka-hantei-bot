# 分荷判定Bot - CLAUDE.md
# コードから読み取った実仕様（ドキュメント・伝聞ではなく app.py / yahooauction_sheet.gs が根拠）

---

## プロジェクト構成

| ファイル | 役割 |
|---|---|
| app.py | Flaskサーバー本体。Slack Events API受信・Claude API呼び出し・全チャンネルのロジック |
| yahooauction_sheet.gs | GAS。ヤフオク出品管理スプレッドシートのセットアップ・AI説明文/タイトル生成・CSV出力 |
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
| /monday-setup | GET | Monday.comボードにカラムを作成（初回のみ） |

---

## チャンネルルーティング（app.py: process_slack_message）

メッセージ受信時、以下の順で処理を振り分ける：

1. `在庫検索 キーワード` / `検索 キーワード` → どのチャンネルでも最優先で在庫検索
2. SATSUEI_CHANNEL_ID → `handle_satsuei_channel`（撮影確認・白洲次郎）
3. SHUPPINON_CHANNEL_ID → `handle_shuppinon_channel`（出品保管・岩崎弥太郎）
4. KONPO_CHANNEL_ID → `handle_konpo_channel`（梱包出荷・黒田官兵衛）
5. STATUS_CHANNEL_ID → `handle_status_channel`（ステータス確認・ステータス松本）
6. ATTENDANCE_CHANNEL_ID → `handle_attendance_channel`（出退勤）
7. GENBA_CHANNEL_ID → `handle_genba_channel`（現場査定・渋沢栄一）
8. KINTAI_CHANNEL_ID → `handle_kintai_channel`（勤怠連絡・サイレント記録）
9. その他すべて → 分荷判定フロー（北大路魯山人）

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
- 在庫回転スコア: 25点（〜1週間:25 / 〜1ヶ月:18 / 〜3ヶ月:10 / 3ヶ月超:3）
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

ボードID: `18403611418`

| カラムID | 型 | 内容 |
|---|---|---|
| kanri_bango | text | 管理番号 |
| hantei_channel | text | 判定チャンネル（確定チャンネル） |
| kakushin_do | text | 確信度（高/中/低）。キャンセル時は "キャンセル" |
| toshosha | text | 投稿者スタッフコード |
| zaiko_kikan | text | 在庫予測期間 |
| status | status | ステータスラベル（下記参照） |
| yosou_kakaku | numbers | 予想販売価格（数値のみ） |
| score | numbers | 総合スコア |
| sakugyou_jikan | numbers | 分荷作業時間（分） |

### ステータスラベルの遷移
```
査定待ち（確定時）
  → 動作確認済み（チェックリスト完了時）
  → 撮影済み（撮影チャンネルで完了入力時）
  → 出品中（出品チャンネルでロケーション入力時）
  → 梱包済み（梱包チャンネルで梱包完了入力時）
  → 出荷済み（送り状入力完了時）
  → キャンセル（キャンセルコマンド時）
  → 要確認（削除コマンド時）
```

---

## Claudeモデル使用（app.py内）

| 用途 | モデル | max_tokens |
|---|---|---|
| 分荷判定メイン | claude-sonnet-4-20250514 | 2048 |
| 現場査定（genba） | claude-sonnet-4-20250514 | 1024 |
| 身分証情報抽出 | claude-sonnet-4-20250514 | 512 |
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
    "U0AL10Q1HQC": "YA",  # 浅野儀頼  ← 確定済み
    "U0ALQ4BJNSV": "KH",  # 林和人    ← 確定済み
    "U0AL4R1EMMZ": "MH",  # 平野光雄  ← 確定済み
    # 横山優(YY) / 三島圭織(KM) / 松本豊彦(TM) / 北瀬孝(TK) / 桃井侑菜(YM) / 伊藤佐和子(SI) / 白木雄介(YS)
    # → UserID未確定・コメントアウト中
}
```
未登録UserIDはそのままUserIDをスタッフコードとして使用する（get_staff_code）。

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

## GAS（yahooauction_sheet.gs）主要機能

スプレッドシートID: `1CWG9MVrsw9gJwp31lCrUs9KB0a1zptZY1cPO47ZNmVU`（app.pyのGAS_URLと同一）

### シート構成
| シート名 | 用途 |
|---|---|
| 出品管理 | 全出品データのメインシート |
| 送料_佐川 | 佐川運賃表（契約価格・定価の両方） |
| 送料_西濃ミニ便 | 西濃運賃表（契約率80%適用済み） |
| 送料_アートデリバリー | アート料金表（手入力） |
| 説明文テンプレート | ビンテージ用・現行品用・まとめ売り用テンプレート |
| 設定マスタ | アカウント区分・発送会社コード・出品担当マーク等 |

### カスタムメニュー（出品管理）
1. DBから商品追加（分荷DBの管理番号を参照して行追加）
2. タイトル生成（AI）
3. 説明文生成（AI）
4. ヤフオクCSV出力（未出品のみ・BOM付きUTF-8）
5. スプレッドシート初期化

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

## 次の実装タスク（コードのTODOコメントから）

- `execute_listing` 内のTODO: ヤフオクAPI自動出品（オークタウンAPI確認後・4/1以降実装予定）
- `extract_tracking_number_from_image`: `anthropic.Anthropic()` を直接呼んでいる（`get_anthropic_client()` に統一すべき）
