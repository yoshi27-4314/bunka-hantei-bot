# 分荷判定Bot

テイクバック事業のAI分荷判定システム。Slack Bot + Claude API + Railway。

> ルール・開発基準・インシデント記録は `.claude/rules/` に分離済み。
> 詳細な仕様はコード（各モジュールのdocstring）が正。

---

## プロジェクト構成（2026/03/24 ファイル分割済み）

```
app.py                  ← Flask routes + メッセージルーティング（673行）
config.py               ← 全定数・環境変数・STAFF_MAP・BOT_PERSONA
prompts.py              ← SYSTEM_PROMPT・GENBA_SYSTEM_PROMPT
services/
    slack.py            ← post_to_slack, send_dm
    claude.py           ← call_claude, fetch_image_as_base64
    monday.py           ← monday_graphql, 管理番号発行, 登録・検索, ファイルアップロード
    google_drive.py     ← Drive操作（アップロード・一覧・削除・メイン写真DL）
    spreadsheet.py      ← send_to_spreadsheet（GAS経由）
utils/
    commands.py         ← parse_command, normalize_keyword
    slack_thread.py     ← スレッド解析（判定データ抽出・確定確認）
    checklist.py        ← 動作確認チェックリスト
    work_activity.py    ← 作業ログ・削除処理
handlers/
    bunika.py           ← 分荷判定（北大路魯山人）
    satsuei.py          ← 撮影確認（白洲次郎）
    shuppinon.py        ← 出品保管（岩崎弥太郎）
    konpo.py            ← 梱包出荷（黒田官兵衛）
    genba.py            ← 現場査定（渋沢栄一）
    status.py           ← ステータス確認（ステータス松本）
    attendance.py       ← 出退勤（二宮金次郎）
    kintai.py           ← 勤怠連絡（サイレント記録）
```

---

## 分荷判定フロー（2026/03/24 刷新）

```
1. スタッフが写真投稿 → AIが商品を特定
2. スタッフが「はい」→ AIが状態確認を指示（商品別1〜3点 + 現物確認ガイド）
3. スタッフが状態報告（例：B 電源つく）
4. AIが販売チャンネルを1つ自動決定（スタッフに選ばせない）
5. スタッフは「確定」か「相談」と入力するだけ
```

### 判定基準
- 回転率重視（同じ利益なら早く売れるチャンネル優先）
- 経費基準込み（作業工数・保管コスト・発送コスト・在庫リスク）
- 壊れている＝価値なし、ではない（インテリア・パーツ取り等の用途変更を検討）

### 浅野承認が必要な条件（1つでも該当）
1. 予想販売価格 ¥30,000以上
2. 希少品・コレクターズアイテム
3. アンティーク・骨董
4. 貴金属・宝石・アクセサリー
5. 含み益が期待できるもの
6. 社内利用の候補
7. AIの判断に自信がないもの

### コマンド
| コマンド | 動作 |
|---|---|
| `確定` | AI判定通りに確定 |
| `相談` | 浅野さんにメンション通知 |
| `再判定` | 再度AI判定 |
| `キャンセル` / `削除` | 確定取消 |
| `在庫検索 キーワード` | Monday.com検索 |

---

## 販売チャンネル（9種）

1. eBayシングル / 2. eBayまとめ / 3. ヤフオクヴィンテージ / 4. ヤフオク現行 / 5. ヤフオクまとめ
6. ロット販売 / 7. 社内利用 / 8. スクラップ / 9. 廃棄

通販5チャンネル（1〜5）→ 管理番号発行 + Monday.com登録
非通販（6〜9）→ スプレッドシートのみ

---

## 管理番号：YYMM-連番4桁（例：2603-0001）

旧形式（2603V0001）は廃止。参照のみ可。

---

## 環境変数

| 変数名 | 用途 |
|---|---|
| ANTHROPIC_API_KEY | Claude API認証 |
| SLACK_BOT_TOKEN | Slack Bot認証 |
| SLACK_SIGNING_SECRET | Slack署名検証 |
| ADMIN_API_TOKEN | /debug等の認証 |
| WEBHOOK_SECRET | /webhook認証 |
| MONDAY_TOKEN | Monday.com API |
| GAS_URL | スプレッドシート連携 |
| GOOGLE_SERVICE_ACCOUNT_JSON | Google Drive認証（base64） |
| GOOGLE_DRIVE_FOLDER_ID | Driveルートフォルダ |
| SATSUEI/SHUPPINON/KONPO/STATUS/ATTENDANCE/GENBA/KINTAI_CHANNEL_ID | 各チャンネルID |
| ALERT_CHANNEL_ID | #system-alert |

---

## 経費基準（判定スコアの内部計算に使用）

- 倉庫125㎡: ¥195,000/月
- 人件費: ¥420,000/月
- 販管費: ¥85,000/月
- 営業利益目標: ¥800,000/月
- サイズ別最低出品金額: 60-80=¥2,200 / 100-140=¥4,500 / 160-200=¥7,500 / 220+=¥12,000

---

## インフラ

- Railway: https://web-production-e7e9d.up.railway.app
- GitHub: https://github.com/yoshi27-4314/bunka-hantei-bot
- スプレッドシート: https://docs.google.com/spreadsheets/d/1CWG9MVrsw9gJwp31lCrUs9KB0a1zptZY1cPO47ZNmVU
- Google Drive: https://drive.google.com/drive/u/1/folders/16G96z45K2coor4QuH5NW9DWxyPNX_56D
- ヘルスチェック: GET /health
- Make.comで15分ごとに監視

---

## ステータスフロー（2026/03/25 整理済み）

分荷確定 → 撮影完了 → 出品中 → 落札済み → 入金待ち → 入金確認済み → 梱包作業 → 出荷待ち → 完了
その他: 確認/相談, 再分荷, 再リスト, キャンセル

## メイン写真（2026/03/25 追加）

撮影完了時、Driveの1枚目商品画像（テプラ除外）をMonday.comの「メイン写真」カラム(file_mm1rwrna)に自動アップロード。
出品・梱包スタッフが管理番号+画像でダブルチェックする用途。

## 管理用エンドポイント

| パス | 用途 |
|---|---|
| /monday-columns | ボードの全カラムID・名前・型を表示 |

## 作業の引き継ぎ

**作業を始めるとき → まず `STATUS.md` を読んでください。**
今やっていること・次にやることが書いてあります。

**作業を終えるとき → 「作業終了」と伝えてください。**
STATUS.mdを自動更新してGitHubに保存します。
