"""
AI分荷判定Bot - Step 1
Slack Events API → Claude API → Slack返信
"""

import os
import json
import base64
import re
import threading
import httpx
from datetime import datetime
from flask import Flask, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()
print(f"[起動時ENV一覧] {[k for k in os.environ.keys() if 'ANTHROPIC' in k or 'SLACK' in k]}")

app = Flask(__name__)


def get_anthropic_client():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return Anthropic(api_key=key) if key else None


def get_slack_token():
    return os.environ.get("SLACK_BOT_TOKEN", "")


def get_monday_token():
    return (
        os.environ.get("MONDAY_TOKEN", "")
        or os.environ.get("MONDAY_API_TOKEN", "")
    )


MONDAY_BOARD_ID = "18404143384"
MONDAY_OLD_BOARD_IDS = ["8199048609", "8199056494"]  # 旧ボード（手作業登録・現在出品中の商品）
TSUHAN_COMMUNITY_CHANNEL_ID = "C0AN99GAG2C"  # 通販業務_共有コミュニティ
MONDAY_API_URL = "https://api.monday.com/v2"
_monday_setup_log: list = []
GAS_URL = os.environ.get("GAS_URL", "https://script.google.com/macros/s/AKfycbx9JpYWvi3p0HgA9Bb0RLgEjkgzbF6iJRuAX7Ks2VL3hwIEnpuTR0J1ydtxegGKRXjh/exec")


def monday_graphql(query: str, variables: dict = None) -> dict:
    """monday.com GraphQL APIを呼び出す"""
    token = get_monday_token()
    if not token:
        raise RuntimeError("MONDAY_API_TOKEN が設定されていません")
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "API-Version": "2023-10",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = httpx.post(MONDAY_API_URL, headers=headers, json=payload, timeout=25)
    return response.json()


# 高額品メンション先（¥30,000以上で自動メンション）
ASANO_USER_ID = "U0AL10Q1HQC"  # 浅野儀頼

# 担当者Slack UserID → 氏名対応表
# UserIDはSlackプロフィール→「その他」→「メンバーIDをコピー」で取得
STAFF_MAP = {
    "U0AL10Q1HQC": "浅野儀頼",
    "U0ALQ4BJNSV": "林和人",
    "U0AL4R1EMMZ": "平野光雄",  # 確認済み 2026/03/18
    "U0ALHCGD3U7": "横山優",
    "U0AMXQ8JH6V": "三島圭織",
    # "UXXXXXXXX": "松本豊彦",
    # "UXXXXXXXX": "北瀬孝",
    "U0ALKDQEC2F": "桃井侑菜",
    "U0ALV7C2EHJ": "伊藤佐和子",
    # "UXXXXXXXX": "白木雄介",
    "U0AM4HG1PRP": "奥村亜優李",
}

# 出品担当マーク（ヤフオクタイトル先頭に付与。担当業務上不要な人は含めない）
STAFF_LISTING_MARKS = {
    "林和人":     "〇",
    "横山優":     "▽",
    "奥村亜優李": "☆",
    "桃井侑菜":  "◎",
}


def get_staff_code(user_id: str) -> str:
    """Slack UserIDからスタッフコードを返す。未登録の場合はUserIDをそのまま返す"""
    return STAFF_MAP.get(user_id, user_id)



def get_monthly_sequence() -> int:
    """今月の管理番号通し番号をmonday.comのアイテム数から取得する（全チャンネル共通）"""
    yymm = datetime.now().strftime("%y%m")
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items {
                    column_values(ids: ["kanri_bango"]) {
                        text
                    }
                }
            }
        }
    }
    """
    try:
        result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
        items = (result.get("data", {})
                 .get("boards", [{}])[0]
                 .get("items_page", {})
                 .get("items", []))
        count = sum(
            1 for item in items
            if item.get("column_values", [{}])[0].get("text", "").startswith(yymm)
        )
        return count + 1
    except Exception as e:
        print(f"[通し番号取得エラー] {e}")
        return int(datetime.now().strftime("%S%f")[:4]) + 1


# 管理番号の重複発行防止
# Monday.comへの登録遅延・失敗で同じ番号が発行されるのを防ぐ
_management_number_lock = threading.Lock()
_issued_numbers: set[str] = set()  # このプロセスセッションで発行済みの管理番号

# ── 管理者設定 ──────────────────────────────────────────────
# 浅野のSlack UserID（このユーザーのメッセージはボットが無視する）
ADMIN_USER_ID = "U0AL10Q1HQC"
# 相談モードのスレッド管理（@浅野+「相談」でトリガー → そのスレッドでボットが無反応になる）
_consultation_threads: set[str] = set()


def generate_management_number() -> str:
    """管理番号を生成する（例：2603-0001）
    西暦下2桁 + 月2桁 + ハイフン + 月次通し番号4桁（チャンネル共通連番）
    ※ロット販売・社内利用・スクラップ・廃棄は管理番号なし
    重複防止: Lockで同時発行をブロック + セッション内発行済みセットで衝突回避
    """
    with _management_number_lock:
        yymm = datetime.now().strftime("%y%m")
        seq = get_monthly_sequence()
        # Monday.comにまだ反映されていない番号との衝突を検知してインクリメント
        while True:
            candidate = f"{yymm}-{seq:04d}"
            if candidate not in _issued_numbers:
                _issued_numbers.add(candidate)
                print(f"[管理番号発行] {candidate} (発行済みセット: {len(_issued_numbers)}件)")
                return candidate
            print(f"[管理番号衝突] {candidate} は発行済み → seq+1 して再試行")
            seq += 1


def extract_judgment(response_text: str) -> dict:
    """Claude応答から判定結果を抽出する"""
    import re
    result = {
        "first_channel": "",
        "first_confidence": "",
        "second_channel": "",
        "first_score": "",
        "second_score": "",
        "internal_keyword": "",
        "item_name": "",
        "maker": "",
        "model_number": "",
        "condition": "",
        "start_price": "",
        "target_price": "",
        "inventory_deadline": "",
        "storage_cost": "",
        "packing_cost": "",
        "expected_roi": "",
    }
    score_count = 0
    in_first = False
    for line in response_text.split("\n"):
        line = line.strip()
        if line.startswith("【第一候補】"):
            result["first_channel"] = line.replace("【第一候補】", "").strip()
            in_first = True
        elif line.startswith("【第二候補】"):
            result["second_channel"] = line.replace("【第二候補】", "").strip()
            in_first = False
        elif "総合スコア：" in line:
            score_count += 1
            match = re.search(r"(\d+)点", line)
            if match:
                score = int(match.group(1))
                if score_count == 1:
                    result["first_score"] = str(score)
                    result["first_confidence"] = "高" if score >= 75 else ("中" if score >= 50 else "低")
                else:
                    result["second_score"] = str(score)
        elif "推定内部KW：" in line:
            kw_match = re.search(r"(/\S+)", line)
            if kw_match:
                result["internal_keyword"] = kw_match.group(1)
        elif line.startswith("📋 アイテム名："):
            result["item_name"] = line.replace("📋 アイテム名：", "").strip()
        elif line.startswith("🏭 メーカー/ブランド："):
            result["maker"] = line.replace("🏭 メーカー/ブランド：", "").strip()
        elif line.startswith("🔢 品番/型式："):
            result["model_number"] = line.replace("🔢 品番/型式：", "").strip()
        elif line.startswith("📊 状態："):
            result["condition"] = line.replace("📊 状態：", "").strip()
        elif "推奨スタート価格：" in line and in_first:
            m = re.search(r"¥([\d,]+)", line)
            if m:
                result["start_price"] = m.group(1).replace(",", "")
        elif "推奨目標価格：" in line and in_first:
            m = re.search(r"¥([\d,]+)", line)
            if m:
                result["target_price"] = m.group(1).replace(",", "")
        elif "推奨在庫期限：" in line and in_first:
            result["inventory_deadline"] = line.split("推奨在庫期限：", 1)[1].strip()
        elif "保管コスト概算：" in line and in_first:
            m = re.search(r"¥([\d,]+)", line)
            if m:
                result["storage_cost"] = m.group(1).replace(",", "")
        elif "梱包・発送コスト概算：" in line and in_first:
            m = re.search(r"¥([\d,]+)", line)
            if m:
                result["packing_cost"] = m.group(1).replace(",", "")
        elif "期待ROI：" in line and in_first:
            m = re.search(r"約([\d.]+)%", line)
            if m:
                result["expected_roi"] = m.group(1)
    return result


def register_to_monday(management_number: str, item_name: str, judgment: dict, user_id: str, sakugyou_jikan: int = 0, kakutei_channel: str = "") -> None:
    """monday.comにアイテムを登録する"""
    import re as _re
    # 予想販売価格から数値を抽出（例: "¥5,000〜¥8,000" → "5000"）
    price_str = judgment.get("predicted_price", "")
    price_num = ""
    if price_str:
        m = _re.search(r'[\d,]+', price_str.replace("¥", "").replace(",", ""))
        if m:
            price_num = _re.sub(r'[^\d]', '', m.group(0))

    col = {
        "kanri_bango": management_number,
        "hantei_channel": kakutei_channel or judgment.get("first_channel", ""),
        "kakushin_do": judgment.get("first_confidence", ""),
        "toshosha": get_staff_code(user_id),
        "zaiko_kikan": judgment.get("inventory_period", ""),
        "status": {"label": "分荷確定"},
        "bunka_date": {"date": datetime.now().strftime("%Y-%m-%d")},
    }
    if price_num:
        col["yosou_kakaku"] = price_num
    if judgment.get("first_score"):
        col["score"] = judgment.get("first_score")
    if sakugyou_jikan > 0:
        col["sakugyou_jikan"] = sakugyou_jikan
    if judgment.get("internal_keyword"):
        col["internal_keyword"] = judgment.get("internal_keyword")
    if judgment.get("maker"):
        col["maker"] = judgment.get("maker")
    if judgment.get("model_number"):
        col["model_number"] = judgment.get("model_number")
    if judgment.get("condition"):
        col["condition"] = judgment.get("condition")
    if judgment.get("start_price"):
        col["kaishi_kakaku"] = judgment.get("start_price")
    if judgment.get("target_price"):
        col["mokuhyo_kakaku"] = judgment.get("target_price")
    column_values = json.dumps(col, ensure_ascii=False)

    query = """
    mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {
        create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) {
            id
        }
    }
    """
    result = monday_graphql(query, {
        "board_id": MONDAY_BOARD_ID,
        "item_name": item_name[:50],
        "column_values": column_values,
    })
    if "errors" in result:
        raise RuntimeError(f"Monday.com API error: {result['errors']}")
    item_id = result.get("data", {}).get("create_item", {}).get("id")
    print(f"[Monday.com] アイテム作成完了 ID={item_id}")


# 重複処理防止（同じメッセージを2回処理しない）
processed_events = set()

SYSTEM_PROMPT = """あなたはテイクバック（中古品買取・転売会社）の分荷判定AIです。
キャラクターは「北大路魯山人」。目利きの職人。
返答は簡潔に。冗長・冗談・昔話調は禁止。「ふむ」「よかろう」程度の短い表現は可。
セクションが変わるときは必ず空行を入れて、内容を明確に区切ること。

【会話の流れ（ステップ制）】

■ ステップ1：最初のメッセージ（画像・テキストで商品情報を受け取ったとき）
　画像や情報から商品を特定し、以下の形式で確認を取る。判定はまだ出さない。
　「〜の商品と見たが、合っておるか？
　　品名：[商品名]
　　メーカー：[メーカー]
　　型番：[型番 or 不明]」

■ ステップ2：「はい」「合ってます」「OK」等の肯定返答を受けたとき
　→ ステップ4（査定）に進む

■ ステップ3：「違います」「別の商品」など否定、または商品情報の補足を受けたとき
　→ 新しい情報をもとに再特定し、ステップ1に戻る

■ ステップ4：査定（商品確認後に実行）
　製造年代・ブランド・状態・市場相場をもとに販売チャンネルを査定する。
　・eBay/ヤフオク（通販5チャンネル）への可能性あり → ステップ5（状態確認）へ
　・ロット販売・社内利用・スクラップ・廃棄のみ → 直接ステップ6（判定出力）へ

■ ステップ5：動作・状態確認を促す（通販向け候補のとき）
　以下の文面で状態確認を依頼する：
　「動作・外観を確認してください。

　• 電源・動作確認
　• ドア・引き出しなど開閉確認
　• 外観・傷・汚れの確認
　• パーツ・付属品の確認

　確認後、状態ランクを返信してください：

　🅢 S：新品・未使用（タグ／箱あり）
　🅐 A：未使用に近い（使用感ほぼなし）
　🅑 B：中古美品（使用感あり・目立つ傷なし）
　🅒 C：中古（使用感・傷・汚れあり）
　🅓 D：ジャンク（動作不良・部品取り）

　ランク＋一言で返信（例：`B 電源OK・外観に小傷`）」

■ ステップ6：判定結果を出力する
　スタッフが状態ランクを入力した後（またはステップ4で非通販確定の場合）、
　以下のフォーマットで判定結果を出力する。フォーマットは厳守すること。

【販売チャンネル】
1. eBayシングル：海外向け高値単品
2. eBayまとめ：海外向けまとめ売り
3. ヤフオクヴィンテージ：昭和レトロ・骨董
4. ヤフオク現行：現行品シングル
5. ヤフオクまとめ：まとめ売り
6. ロット販売：業者向けまとめ
7. 社内利用：会社で使う
8. スクラップ：金属資源として売却
9. 廃棄：処分

【スコアリング基準（100点満点）】

▍収益期待スコア（30点）
・予想販売価格の高さ（市場相場・オークション実績ベース）
・価格の安定性・相場確信度
・まとめ/セット売りによる付加価値可能性

▍在庫回転スコア（25点）
・予測在庫期間（〜2ヶ月:25点 / 〜4ヶ月:18点 / 〜8ヶ月:10点 / 1年以上:3点）
・季節適合性（今が需要ピーク時期か）
・市場需要強度（競合数・入札傾向・検索需要）

▍保管・物流コスト効率スコア（25点）
・保管コスト：商品サイズ × 予測在庫期間で推定
  └ 小型軽量（〜60サイズ）: 高得点 / 大型（170サイズ超）: 大幅減点 / 引取限定: 最低点
・発送コスト効率（サイズ・重量から推定）
・梱包材コスト（精密機器・陶器・ガラス等は梱包費増で減点）

▍作業コスト効率スコア（20点）
・梱包難易度（割れ物・精密品・異形・超大型: 大幅減点）
・撮影・リスト作成の容易さ
・問い合わせ対応の複雑さ予測

【市場需要区分】
- 1（買い手市場）：供給過多・売りにくい
- 2（市場拮抗）：需給バランス良好
- 3（売り手市場）：需要高・売りやすい

【内部キーワード生成ルール】
第一候補に対して社内管理用の内部キーワードを推定してください。
形式：/[発送コード][サイズ]/[価格コード]/[期待値]

発送会社コード（商品サイズ・重量から推定）：
- S = 佐川急便（標準）
- Y = ヤマト運輸
- SU = 西濃運輸（大型家具・家電）
- AD = アートデリバリー（超大型・美術品）
- DC = 購入者直接引取り（大型で発送困難な場合）

発送サイズ（縦+横+高さの合計cm）：
60 / 80 / 100 / 120 / 140 / 160 / 170 / 200

価格コード（予想販売価格の中央値を変換）：
- J = ×10（例：500円 → 50J）
- H = ×100（例：500円 → 5H）
- S = ×1,000（例：4,000円 → 4S）
- M = ×10,000（例：30,000円 → 3M）
※なるべくシンプルな表記を選ぶ（40Hより4Sが優先）

期待値：販売期待値（1/2/3）をそのまま使用

【ステップ6の出力フォーマット（厳守）】
━━━━━━━━━━━━━━━━
📦 分荷判定結果
━━━━━━━━━━━━━━━━

📋 アイテム名：[商品名]
🏭 メーカー/ブランド：[メーカー名]
🔢 品番/型式：[品番 or 不明]
📊 状態ランク：[S/A/B/C/D]（[状態名]）

─────────────────────────

【第一候補】[チャンネル名]
理由：[50字以内]

💰 予想販売価格：¥[下限]〜¥[上限]
🎯 推奨スタート価格：¥[数値]
🏆 推奨目標価格：¥[数値]
📅 予測在庫期間：[期間]
⏰ 推奨在庫期限：[例: 2026年6月末]
📊 市場需要：[1/2/3]（[買い手市場/市場拮抗/売り手市場]）
🏭 保管コスト概算：¥[数値]
📦 梱包・発送コスト概算：¥[数値]
💡 期待ROI：約[数値]%

⭐ 総合スコア：[合計]点
　└ 収益期待：[点]/30
　└ 在庫回転：[点]/25
　└ 保管物流効率：[点]/25
　└ 作業コスト効率：[点]/20

🏷️ 推定内部KW：/[発送コード][サイズ]/[価格コード]/[期待値]

─────────────────────────

【第二候補】[チャンネル名]
理由：[50字以内]

💰 予想販売価格：¥[下限]〜¥[上限]
🎯 推奨スタート価格：¥[数値]
📅 予測在庫期間：[期間]
📊 市場需要：[1/2/3]（[買い手市場/市場拮抗/売り手市場]）

⭐ 総合スコア：[合計]点
　└ 収益期待：[点]/30
　└ 在庫回転：[点]/25
　└ 保管物流効率：[点]/25
　└ 作業コスト効率：[点]/20

━━━━━━━━━━━━━━━━
確定：`第一`（[第一候補のチャンネル名を記入]） / `第二`（[第二候補のチャンネル名を記入]）
━━━━━━━━━━━━━━━━"""


def fetch_image_as_base64(image_url: str) -> tuple[str, str]:
    """画像URLをダウンロードしてbase64エンコードとメディアタイプを返す"""
    headers = {"Authorization": f"Bearer {get_slack_token()}"}
    response = httpx.get(image_url, headers=headers, timeout=30, follow_redirects=True)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if content_type not in supported:
        content_type = "image/jpeg"

    image_data = base64.standard_b64encode(response.content).decode("utf-8")
    return image_data, content_type


def fetch_thread_messages(channel_id: str, thread_ts: str, current_ts: str) -> list[dict]:
    """Slackスレッドの会話履歴を取得してClaude用のmessagesリストに変換する"""
    token = get_slack_token()
    if not token:
        return []
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"channel": channel_id, "ts": thread_ts}
    response = httpx.get(url, headers=headers, params=params, timeout=20)
    data = response.json()
    if not data.get("ok"):
        print(f"[スレッド履歴取得エラー] {data.get('error')}")
        return []

    messages = []
    for msg in data.get("messages", []):
        # 現在処理中のメッセージは除外（後で追加する）
        if msg.get("ts") == current_ts:
            continue
        text = msg.get("text", "").strip()
        if not text:
            continue
        role = "assistant" if (msg.get("bot_id") or msg.get("bot_profile")) else "user"
        # 直前と同じroleの場合はテキストを結合（Claudeは交互要求のため）
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += f"\n{text}"
        else:
            messages.append({"role": role, "content": text})

    # userから始まらないと Claude APIがエラーになるので調整
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    return messages


def call_claude(user_message: str, image_urls: list[str] | None = None, history: list[dict] | None = None) -> str:
    """Claude APIを呼び出して分荷判定を返す"""
    # 現在のメッセージのコンテンツを組み立て
    current_content = []

    # 画像を全て追加（複数対応）
    failed_images = []
    for image_url in (image_urls or []):
        try:
            image_data, media_type = fetch_image_as_base64(image_url)
            current_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data,
                },
            })
        except Exception as e:
            failed_images.append(str(e))

    # テキストを追加
    text = user_message
    if image_urls:
        text = f"添付画像も参考にして判定してください。\n\n{user_message}"
    if failed_images:
        text += f"\n\n※一部画像の取得に失敗しました: {', '.join(failed_images)}"
    current_content.append({"type": "text", "text": text})

    # 会話履歴 + 現在のメッセージを組み立て
    messages = list(history) if history else []
    messages.append({"role": "user", "content": current_content})

    client = get_anthropic_client()
    if not client:
        raise RuntimeError("ANTHROPIC_API_KEY が設定されていません")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text


def normalize_keyword(text: str) -> str:
    """全角→半角・漢数字→数字に正規化してコマンド判定しやすくする"""
    text = text.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    text = text.replace('／', '/').replace('　', ' ')
    return text.strip()


# チャンネル名直接入力での確定に対応する有効チャンネル一覧
VALID_CHANNELS = {
    "eBayシングル", "eBayまとめ",
    "ヤフオクヴィンテージ", "ヤフオク現行", "ヤフオクまとめ",
    "ロット販売", "社内利用", "自社使用", "自社利用", "スクラップ", "廃棄",
}


def parse_command(text: str):
    """テキストがコマンドかどうか判定し (command_type, option) を返す。
    コマンドでなければ (None, None)"""
    n = normalize_keyword(text)
    # 第一候補確定（第一/第1 どちらも対応）
    if n in ('第一', '第1'):
        return 'kakutei', '1'
    # 第二候補確定（第二/第2 どちらも対応）
    elif n in ('第二', '第2'):
        return 'kakutei', '2'
    elif n.startswith('確定/') and len(n) > 3:
        return 'kakutei', n[3:].strip()
    # チャンネル名をそのまま入力した場合も確定として認識
    elif normalize_channel(n) in VALID_CHANNELS:
        return 'kakutei', normalize_channel(n)
    elif n == '再判定':
        return 'saihantei', None
    elif n == '保留':
        return 'horyuu', None
    elif n in ('削除', 'テスト', 'キャンセル', '取消', '取り消し'):
        return 'cancel', None
    # 在庫検索（スレッド外・どのチャンネルでも）
    elif n.startswith('在庫検索 ') and len(n) > 5:
        return 'zaiko_search', n[5:].strip()
    elif n.startswith('検索 ') and len(n) > 3:
        return 'zaiko_search', n[3:].strip()
    return None, None


def normalize_channel(channel: str) -> str:
    """チャンネル名の表記ゆれを統一する"""
    aliases = {
        '自社使用': '社内利用',
        '自社利用': '社内利用',
    }
    return aliases.get(channel.strip(), channel.strip())


def get_judgment_from_thread(channel_id: str, thread_ts: str) -> dict:
    """スレッド内のBot判定メッセージから判定データを抽出する"""
    import re
    token = get_slack_token()
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"channel": channel_id, "ts": thread_ts}
    response = httpx.get(url, headers=headers, params=params, timeout=20)
    data = response.json()

    result = {}
    for msg in data.get("messages", []):
        if not (msg.get("bot_id") or msg.get("bot_profile")):
            continue
        text = msg.get("text", "")
        if "分荷判定結果" not in text:
            continue
        mn = re.search(r'管理番号\n　\*?(\d{4}(?:[VGME]\d{4}|-\d{4}))\*?', text)
        if mn:
            result["kanri_bango"] = mn.group(1)
        kw = re.search(r'推定内部KW：(/\S+)', text)
        if kw:
            result["internal_keyword"] = kw.group(1)
        # 第一候補
        ch1 = re.search(r'【第一候補】(.+)', text)
        if ch1:
            result["first_channel"] = ch1.group(1).strip()
        # 第二候補
        ch2 = re.search(r'【第二候補】(.+)', text)
        if ch2:
            result["second_channel"] = ch2.group(1).strip()
        # スコア（最初に出てくるもの）
        score = re.search(r'総合スコア：(\d+)点', text)
        if score:
            result["first_score"] = score.group(1)
        # 予想価格
        price = re.search(r'予想販売価格：(¥[\d,]+〜¥[\d,]+)', text)
        if price:
            result["predicted_price"] = price.group(1)
        # 在庫期間（新フォーマット対応）
        period = re.search(r'予測在庫期間：(.+)', text)
        if period:
            result["inventory_period"] = period.group(1).strip()
        # 商品情報（絵文字なしで安定マッチ）
        item = re.search(r'アイテム名：(.+)', text)
        if item:
            result["item_name"] = item.group(1).strip()
        maker = re.search(r'メーカー/ブランド：(.+)', text)
        if maker:
            result["maker"] = maker.group(1).strip()
        model = re.search(r'品番/型式：(.+)', text)
        if model:
            result["model_number"] = model.group(1).strip()
        cond = re.search(r'状態ランク：([SABCD])（(.+?)）', text)
        if cond:
            result["condition"] = f"{cond.group(1)}（{cond.group(2)}）"
        # 新フィールド
        sp = re.search(r'推奨スタート価格：¥([\d,]+)', text)
        if sp:
            result["start_price"] = sp.group(1).replace(",", "")
        tp = re.search(r'推奨目標価格：¥([\d,]+)', text)
        if tp:
            result["target_price"] = tp.group(1).replace(",", "")
        deadline = re.search(r'推奨在庫期限：(.+)', text)
        if deadline:
            result["inventory_deadline"] = deadline.group(1).strip()
        sc = re.search(r'保管コスト概算：¥([\d,]+)', text)
        if sc:
            result["storage_cost"] = sc.group(1).replace(",", "")
        pc = re.search(r'梱包・発送コスト概算：¥([\d,]+)', text)
        if pc:
            result["packing_cost"] = pc.group(1).replace(",", "")
        roi = re.search(r'期待ROI：約([\d.]+)%', text)
        if roi:
            result["expected_roi"] = roi.group(1)
        # breakしない → 全メッセージを走査し、最新の判定（再判定含む）で上書きされる
    return result


def get_confirmation_from_thread(channel_id: str, thread_ts: str) -> dict:
    """スレッド内のBot確定メッセージから管理番号と確定チャンネルを取得する。
    管理番号なしの確定（社内利用・スクラップ・廃棄・ロット販売）も検出する。
    戻り値: {"kanri_bango": str, "kakutei_channel": str}
    """
    import re
    token = get_slack_token()
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    response = httpx.get(url, headers=headers, params={"channel": channel_id, "ts": thread_ts}, timeout=10)
    for msg in response.json().get("messages", []):
        if not (msg.get("bot_id") or msg.get("bot_profile")):
            continue
        text = msg.get("text", "")
        # 確定メッセージかどうか判定（管理番号あり・なし両方）
        if "確定完了" not in text:
            continue
        kanri_bango = ""
        kakutei_channel = ""
        m_kanri = re.search(r'管理番号\n　\*?(\d{4}(?:[VGME]\d{4}|-\d{4}))\*?', text)
        if m_kanri:
            kanri_bango = m_kanri.group(1)
        m_channel = re.search(r'確定チャンネル\n　\*?(.+?)\*?(?:\n|$)', text)
        if m_channel:
            kakutei_channel = m_channel.group(1).strip()
        if kakutei_channel:
            return {"kanri_bango": kanri_bango, "kakutei_channel": kakutei_channel}
    return {"kanri_bango": "", "kakutei_channel": ""}


def get_confirmed_kanri_bango(channel_id: str, thread_ts: str) -> str:
    """後方互換用。get_confirmation_from_thread に委譲する"""
    return get_confirmation_from_thread(channel_id, thread_ts)["kanri_bango"]


def cancel_monday_item(kanri_bango: str) -> None:
    """monday.comの該当アイテムのステータスをキャンセルに変更する"""
    # 管理番号でアイテムを検索
    search_query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items { id column_values(ids: ["kanri_bango"]) { text } }
            }
        }
    }
    """
    result = monday_graphql(search_query, {"board_id": MONDAY_BOARD_ID})
    items = (result.get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    item_id = next(
        (i["id"] for i in items
         if i.get("column_values", [{}])[0].get("text") == kanri_bango),
        None
    )
    if not item_id:
        print(f"[Monday.com] 管理番号 {kanri_bango} のアイテムが見つかりません")
        return
    # ステータスをキャンセルに更新
    update_query = """
    mutation ($board_id: ID!, $item_id: ID!, $col_vals: JSON!) {
        change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $col_vals) { id }
    }
    """
    col_vals = json.dumps({"status": {"label": "キャンセル"}, "kakushin_do": "キャンセル"}, ensure_ascii=False)
    monday_graphql(update_query, {"board_id": MONDAY_BOARD_ID, "item_id": item_id, "col_vals": col_vals})
    print(f"[Monday.com] {kanri_bango} をキャンセルに更新")


def _find_monday_item_id(management_number: str) -> str | None:
    """管理番号でMonday.comアイテムIDを検索する"""
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items { id column_values(ids: ["kanri_bango"]) { text } }
            }
        }
    }
    """
    result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
    items = (result.get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    return next(
        (i["id"] for i in items
         if i.get("column_values", [{}])[0].get("text") == management_number),
        None
    )


def update_monday_columns(management_number: str, col_updates: dict) -> None:
    """Monday.comの指定アイテムの複数カラムを一括更新する"""
    item_id = _find_monday_item_id(management_number)
    if not item_id:
        print(f"[Monday.com] 管理番号 {management_number} が見つかりません")
        return
    col_vals = json.dumps(col_updates, ensure_ascii=False)
    query = """
    mutation ($board_id: ID!, $item_id: ID!, $col_vals: JSON!) {
        change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $col_vals) { id }
    }
    """
    monday_graphql(query, {"board_id": MONDAY_BOARD_ID, "item_id": item_id, "col_vals": col_vals})
    print(f"[Monday.com] {management_number} を更新: {list(col_updates.keys())}")


def search_inventory(keyword: str) -> list[dict]:
    """Monday.comのリスト作成ボードからキーワードで在庫検索する"""
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items {
                    id
                    name
                    column_values(ids: ["kanri_bango", "hantei_channel", "kakushin_do", "zaiko_kikan", "status"]) {
                        id
                        text
                    }
                }
            }
        }
    }
    """
    try:
        result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
        items = (result.get("data", {})
                 .get("boards", [{}])[0]
                 .get("items_page", {})
                 .get("items", []))
        kw_lower = keyword.lower()
        matched = []
        for item in items:
            name = item.get("name", "")
            col_map = {c["id"]: c["text"] for c in item.get("column_values", [])}
            kanri = col_map.get("kanri_bango", "")
            channel = col_map.get("hantei_channel", "")
            zaiko = col_map.get("zaiko_kikan", "")
            status = col_map.get("status", "")
            # アイテム名・管理番号・チャンネルにキーワードが含まれるものを返す
            search_target = f"{name} {kanri} {channel}".lower()
            if kw_lower in search_target:
                matched.append({
                    "id": item.get("id"),
                    "name": name,
                    "kanri_bango": kanri,
                    "channel": channel,
                    "zaiko_kikan": zaiko,
                    "status": status,
                })
        return matched
    except Exception as e:
        print(f"[在庫検索エラー] {e}")
        return []


# ── 動作確認チェックリスト ────────────────────────────────

# 商品状態の選択肢
CONDITION_MAP = {
    "S": "新品・未使用（タグ/箱あり）",
    "A": "未使用に近い（使用感ほぼなし）",
    "B": "中古美品（使用感あり・目立つ傷なし）",
    "C": "中古（使用感・傷・汚れあり）",
    "D": "ジャンク（動作不良・部品取り）",
}


def post_checklist(channel_id: str, thread_ts: str, management_number: str) -> None:
    """動作確認・現状確認チェックリストをスレッドに投稿する"""
    text = (
        f"ふむ、 *{management_number}* の現状確認を頼む。\n"
        "良い品を世に出すには、目利きの確認が欠かせぬ。\n\n"
        "*【確認項目】*\n"
        "• 電源を入れて動作確認\n"
        "• ドア・引き出し・蓋など開閉確認\n"
        "• 外観・傷・汚れの確認\n"
        "• パーツ・付属品の欠品確認\n\n"
        "*【状態ランクを知らせよ】*\n"
        "🅢 S：新品・未使用（タグ／箱あり）\n"
        "🅐 A：未使用に近い（使用感ほぼなし）\n"
        "🅑 B：中古美品（使用感あり・目立つ傷なし）\n"
        "🅒 C：中古（使用感・傷・汚れあり）\n"
        "🅓 D：ジャンク（動作不良・部品取り）\n\n"
        "ランクのアルファベット＋一言コメントを返してくれ\n"
        "例：`B 電源OK、外観に小傷あり、パーツ全部揃ってます`\n"
        "※音声入力でも構わぬ"
    )
    post_to_slack(channel_id, thread_ts, text)


def get_checklist_state(channel_id: str, thread_ts: str) -> dict:
    """スレッド内のチェックリスト状態を返す。
    戻り値: {"management_number": str, "is_completed": bool}
    チェックリストがなければ {}
    """
    import re
    token = get_slack_token()
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    response = httpx.get(url, headers=headers, params={"channel": channel_id, "ts": thread_ts}, timeout=10)
    data = response.json()

    management_number = None
    is_completed = False

    for msg in data.get("messages", []):
        text = msg.get("text", "")
        is_bot = bool(msg.get("bot_id") or msg.get("bot_profile"))
        if is_bot:
            m = re.search(r'\*([\w-]+)\* の現状確認を頼む', text)
            if m:
                management_number = m.group(1)
            if "動作確認完了" in text:
                is_completed = True

    if not management_number:
        return {}
    return {"management_number": management_number, "is_completed": is_completed}


def update_monday_item_status(management_number: str, status_label: str) -> None:
    """monday.comの該当アイテムのstatusを更新する"""
    search_query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items { id column_values(ids: ["kanri_bango"]) { text } }
            }
        }
    }
    """
    result = monday_graphql(search_query, {"board_id": MONDAY_BOARD_ID})
    items = (result.get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    item_id = next(
        (i["id"] for i in items
         if i.get("column_values", [{}])[0].get("text") == management_number),
        None
    )
    if not item_id:
        print(f"[Monday.com] 管理番号 {management_number} が見つかりません")
        return
    col_vals = json.dumps({"status": {"label": status_label}}, ensure_ascii=False)
    update_query = """
    mutation ($board_id: ID!, $item_id: ID!, $col_vals: JSON!) {
        change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $col_vals) { id }
    }
    """
    monday_graphql(update_query, {"board_id": MONDAY_BOARD_ID, "item_id": item_id, "col_vals": col_vals})
    print(f"[Monday.com] {management_number} のステータスを「{status_label}」に更新")


def send_to_spreadsheet(payload: dict) -> None:
    """GAS経由でGoogleスプレッドシートにデータを転記する"""
    response = httpx.post(GAS_URL, json=payload, timeout=30, follow_redirects=True)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"GAS error: {result.get('error')}")
    print("[スプレッドシート転記完了]")


# チャンネルごとのBot表示名
BOT_NAMES = {
    "bunika":    "北大路魯山人",  # 分荷判定
    "satsuei":   "白洲次郎",     # 写真撮影
    "shuppinon": "岩崎弥太郎",   # 出品・保管
    "konpo":     "黒田官兵衛",   # 梱包・出荷
    "status":    "ステータス松本", # ステータス確認
    "genba":     "渋沢栄一",      # 現場査定（買取・廃棄・知識インプット）
    "kintaro":   "二宮金次郎",   # 出退勤
}

# キャラクターごとの定型文
# 北大路魯山人: 美食家・陶芸家。職人気質で厳しいが公平。「ふむ」「よかろう」「〜じゃな」
# 白洲次郎:     GHQ相手に一歩も引かなかった反骨紳士。短く的確。「〜だ」「悪くない」
# 岩崎弥太郎:   三菱創業者。商才と情熱の塊。「〜じゃ！」「ようやった！」豪快で前向き
# 黒田官兵衛:   戦国最高の軍師。冷静・計画的・頼れる参謀。「承知した」「案ずるな」
# ステータス松本: トータス松本オマージュ。関西ロックマン。「〜やで」「ありがとさん」熱くて人情味たっぷり
BOT_PERSONA = {
    "bunika": {
        "search_none": (
            "🔍 *「{keyword}」* の在庫は見当たらない。\n\n"
            "別の言葉で試してくれ。"
        ),
        "search_found": (
            "🔍 *「{keyword}」* の在庫が *{count} 件* ある。"
        ),
        "confirm": (
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル：*{channel}*\n\n"
            "🔖 管理番号：*{kanri}*\n\n"
            "スプレッドシートに転記しました。\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "confirm_only": (
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル：*{channel}*\n\n"
            "スプレッドシートに転記しました。\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "horyuu": (
            "━━━━━━━━━━━━━━━━\n"
            "⏸️ *保留にしました*\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "cancel_kanri": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *取り消しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "🔖 管理番号：*{kanri}*\n\n"
            "作業時間は実績として記録されます。"
        ),
        "cancel_only": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *確定を取り消しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 対象チャンネル：*{channel}*\n\n"
            "スプレッドシートに記録しました。"
        ),
        "cancel_none": (
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ *確定前です*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "取り消す対象がありません。"
        ),
        "saihantei": "🔄 再判定します。",
    },
    "satsuei": {
        "search_none": (
            "…\n\n"
            "🔍 *「{keyword}」* は見当たらない。\n\n"
            "別の言葉で試してくれ。"
        ),
        "search_found": (
            "🔍 *「{keyword}」*\n\n"
            "*{count} 件* 確認できた。\n\n"
            "内容を確認してくれ。"
        ),
        "confirm": (
            "了解した。\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル\n"
            "　*{channel}*\n\n"
            "🔖 管理番号\n"
            "　*{kanri}*\n\n"
            "記録完了だ。\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "confirm_only": (
            "了解した。\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル\n"
            "　*{channel}*\n\n"
            "記録完了だ。\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "horyuu": (
            "━━━━━━━━━━━━━━━━\n"
            "⏸️ *保留にした*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "それでいい。"
        ),
        "cancel_kanri": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *取り消した*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "🔖 管理番号\n"
            "　*{kanri}*\n\n"
            "撮影作業の記録を取り消した。"
        ),
        "cancel_only": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *取り消した*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 対象チャンネル\n"
            "　*{channel}*\n\n"
            "確定を取り消した。"
        ),
        "cancel_none": (
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ *確定前です*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "取り消すものはない。"
        ),
        "saihantei": "🔄 もう一度やろう。",
    },
    "shuppinon": {
        "search_none": (
            "おおっ！\n\n"
            "🔍 *「{keyword}」* は在庫になかったか！\n\n"
            "商機はまだこれからじゃ！"
        ),
        "search_found": (
            "これは！\n\n"
            "🔍 *「{keyword}」* で\n"
            "*{count} 件* 見つかったぞ！\n\n"
            "儲かる予感がするのう！"
        ),
        "confirm": (
            "ようやった！\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル\n"
            "　*{channel}*\n\n"
            "🔖 管理番号\n"
            "　*{kanri}*\n\n"
            "転記完了じゃ！\n"
            "どんどん稼いでいこう！\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "confirm_only": (
            "ようやった！\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル\n"
            "　*{channel}*\n\n"
            "転記完了じゃ！\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "horyuu": (
            "━━━━━━━━━━━━━━━━\n"
            "⏸️ *保留にしました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "慎重なのも大事じゃ。\n"
            "その判断、儂は支持するぞ！"
        ),
        "cancel_kanri": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *取り消しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "🔖 管理番号\n"
            "　*{kanri}*\n\n"
            "次の商いで取り返せばよい！"
        ),
        "cancel_only": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *確定を取り消しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 対象チャンネル\n"
            "　*{channel}*\n\n"
            "気にするな、前を向こう！"
        ),
        "cancel_none": (
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ *まだ確定前です*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "取り消すものはないぞ。"
        ),
        "saihantei": "🔄 もう一度見てみよう！必ず活路はある！",
    },
    "konpo": {
        "search_none": (
            "━━━━━━━━━━━━━━━━\n"
            "🔍 *在庫なし*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "「{keyword}」は在庫に\n"
            "見当たらぬ。\n\n"
            "別の言葉で探されよ。"
        ),
        "search_found": (
            "🔍 *「{keyword}」*\n\n"
            "*{count} 件* 確認できた。\n\n"
            "詳細を確認されよ。"
        ),
        "confirm": (
            "承知した。\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル\n"
            "　*{channel}*\n\n"
            "🔖 管理番号\n"
            "　*{kanri}*\n\n"
            "万全の態勢で転記完了\n"
            "であるぞ。\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "confirm_only": (
            "承知した。\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル\n"
            "　*{channel}*\n\n"
            "転記完了であるぞ。\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "horyuu": (
            "━━━━━━━━━━━━━━━━\n"
            "⏸️ *保留にしました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "案ずるな。\n"
            "必要な時間をとることも\n"
            "重要な判断じゃ。"
        ),
        "cancel_kanri": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *取り消しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "🔖 管理番号\n"
            "　*{kanri}*\n\n"
            "作業時間は実績として残る。\n"
            "よきかな。"
        ),
        "cancel_only": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *確定を取り消しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 対象チャンネル\n"
            "　*{channel}*\n\n"
            "スプレッドシートに\n"
            "記録済みであるぞ。"
        ),
        "cancel_none": (
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ *まだ確定前です*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "取り消せるものはない。"
        ),
        "saihantei": "🔄 再度見定めよう。最善の判断を下すためじゃ。",
    },
    "status": {
        "search_none": (
            "おっと〜！\n\n"
            "🔍 *「{keyword}」* の在庫は\n"
            "見つからへんかったで〜。\n\n"
            "別の言葉で試してみてな！"
        ),
        "search_found": (
            "おっ、ありがとさん！\n\n"
            "🔍 *「{keyword}」* で\n"
            "*{count} 件* ヒットしたで〜！"
        ),
        "confirm": (
            "ありがとさん！\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了やで〜*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル\n"
            "　*{channel}*\n\n"
            "🔖 管理番号\n"
            "　*{kanri}*\n\n"
            "しっかり転記したで〜！\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "confirm_only": (
            "ありがとさん！\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ *確定完了やで〜*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 確定チャンネル\n"
            "　*{channel}*\n\n"
            "転記したで〜！\n"
            "━━━━━━━━━━━━━━━━"
        ),
        "horyuu": (
            "━━━━━━━━━━━━━━━━\n"
            "⏸️ *保留にしたで〜*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "そやな、焦らんでええで。\n"
            "ゆっくり考えてな！"
        ),
        "cancel_kanri": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *取り消したで〜*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "🔖 管理番号\n"
            "　*{kanri}*\n\n"
            "作業時間はちゃんと残るから\n"
            "安心してな！"
        ),
        "cancel_only": (
            "━━━━━━━━━━━━━━━━\n"
            "🗑️ *確定を取り消したで〜*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "📌 対象チャンネル\n"
            "　*{channel}*\n\n"
            "スプレッドシートに記録したわ。"
        ),
        "cancel_none": (
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ *まだ確定してへんで〜*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "取り消すもんはないわ。"
        ),
        "saihantei": "🔄 もっかい見てみよか〜！",
    },
    "genba": {
        "memo_saved": (
            "━━━━━━━━━━━━━━━━\n"
            "📝 *知識を記録いたしました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "算盤と道徳の両面から\n"
            "大切に蓄積いたします。"
        ),
        "error": (
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ *エラーが発生しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "これは失礼いたしました。\n"
            "今一度お試しいただけますでしょうか。"
        ),
        "thinking": (
            "🔍 算盤を弾いております。\n\n"
            "しばしお待ちを..."
        ),
    },
}


def post_to_slack(channel_id: str, thread_ts: str, text: str, mention_user: str = "", bot_role: str = "bunika") -> None:
    """Slackの指定スレッドにメッセージを返信する"""
    if mention_user:
        text = f"<@{mention_user}>\n{text}"
    token = get_slack_token()
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN が設定されていません")
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "channel": channel_id,
        "thread_ts": thread_ts,
        "text": text,
        "username": BOT_NAMES.get(bot_role, "北大路魯山人"),
    }
    response = httpx.post(url, headers=headers, json=payload, timeout=10)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Slack API error: {result.get('error')}")


def get_bot_role_for_channel(channel_id: str) -> str:
    """チャンネルIDに対応するbot_roleを返す"""
    mapping = {
        os.environ.get("SATSUEI_CHANNEL_ID", ""):    "satsuei",
        os.environ.get("SHUPPINON_CHANNEL_ID", ""):  "shuppinon",
        os.environ.get("KONPO_CHANNEL_ID", ""):      "konpo",
        os.environ.get("STATUS_CHANNEL_ID", ""):     "status",
        os.environ.get("GENBA_CHANNEL_ID", ""):      "genba",
    }
    return mapping.get(channel_id, "bunika")


def process_slack_message(event: dict) -> None:
    """Slackメッセージをバックグラウンドで処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_message = event.get("text", "")
    user_id = event.get("user", "")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    print(f"[処理開始] channel={channel_id} ts={thread_ts} user={user_id} message={user_message[:30]}")
    print(f"[ENV確認] ANTHROPIC_API_KEY={'設定済み' if anthropic_key else '未設定'} SLACK_BOT_TOKEN={'設定済み' if slack_token else '未設定'}")

    # ── 管理者（浅野）専用処理 ───────────────────────────────
    bot_role = get_bot_role_for_channel(channel_id)
    if user_id == ADMIN_USER_ID:
        # 「浅野です」で始まるメッセージ → スタッフへのお知らせとしてボットが整形して再送
        if user_message and user_message.strip().startswith("浅野です"):
            announcement = user_message.strip()[len("浅野です"):].strip()
            if announcement:
                post_to_slack(channel_id, thread_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "📢 *浅野からのお知らせ*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    f"{announcement}\n\n"
                    "━━━━━━━━━━━━━━━━",
                    bot_role=bot_role)
            return

    # ── 相談モード：@浅野 + 「相談」でトリガー ────────────────
    admin_mention = f"<@{ADMIN_USER_ID}>"
    if user_message and admin_mention in user_message and "相談" in user_message:
        _consultation_threads.add(thread_ts)
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "💬 *浅野さんへの相談スレッド*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"<@{ADMIN_USER_ID}> に通知しました。\n"
            "このスレッドではボットは反応しません。\n"
            "自由にご相談ください。\n\n"
            "━━━━━━━━━━━━━━━━",
            bot_role=bot_role)
        return

    # 相談モード中のスレッドはボットが無反応
    if thread_ts in _consultation_threads:
        return

    # ── 在庫検索はチャンネルに関わらず最優先で処理 ──────────
    if user_message:
        cmd_type, cmd_option = parse_command(user_message)
        if cmd_type == 'zaiko_search':
            try:
                _handle_zaiko_search(cmd_option, channel_id, thread_ts, event)
            except Exception as e:
                print(f"[在庫検索エラー] {e}")
                post_to_slack(channel_id, thread_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "⚠️ *在庫検索エラー*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    f"{e}")
            return

    # ── チャンネルルーティング ────────────────────────────
    satsuei_channel_id = os.environ.get("SATSUEI_CHANNEL_ID", "")
    if satsuei_channel_id and channel_id == satsuei_channel_id:
        handle_satsuei_channel(event)
        return

    shuppinon_channel_id = os.environ.get("SHUPPINON_CHANNEL_ID", "")
    if shuppinon_channel_id and channel_id == shuppinon_channel_id:
        handle_shuppinon_channel(event)
        return

    konpo_channel_id = os.environ.get("KONPO_CHANNEL_ID", "")
    if konpo_channel_id and channel_id == konpo_channel_id:
        handle_konpo_channel(event)
        return

    status_channel_id = os.environ.get("STATUS_CHANNEL_ID", "")
    if status_channel_id and channel_id == status_channel_id:
        handle_status_channel(event)
        return

    attendance_channel_id = os.environ.get("ATTENDANCE_CHANNEL_ID", "")
    if attendance_channel_id and channel_id == attendance_channel_id:
        handle_attendance_channel(event)
        return

    genba_channel_id = os.environ.get("GENBA_CHANNEL_ID", "")
    if genba_channel_id and channel_id == genba_channel_id:
        handle_genba_channel(event)
        return

    kintai_channel_id = os.environ.get("KINTAI_CHANNEL_ID", "")
    if kintai_channel_id and channel_id == kintai_channel_id:
        handle_kintai_channel(event)
        return

    # 添付画像のURLを取得（複数対応）
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]

    # 画像のみ投稿かどうかを記録（後のチェックリストガードで使用）
    image_only_post = bool(image_urls) and not user_message.strip()

    # テキストなしで画像のみの場合はデフォルトメッセージを使用
    if not user_message and image_urls:
        user_message = "添付画像の商品を分荷判定してください。"

    # ── まとめ売り選択待ちの確認（1/2/3 をコマンドより優先してキャッチ） ──
    if event.get("thread_ts") and user_message and user_message.strip() in ('1', '2', '3'):
        try:
            matome_channel = get_matome_pending_from_thread(channel_id, thread_ts)
            if matome_channel:
                _handle_matome_choice(user_message.strip(), matome_channel, channel_id, thread_ts, event)
                return
        except Exception as e:
            print(f"[まとめ選択処理エラー] {e}")

    # ── コマンド判定（確定・再判定・保留・キャンセルはスレッド内のみ） ──
    if user_message:
        cmd_type, cmd_option = parse_command(user_message)
        # 確定・再判定・保留・キャンセルはスレッド内のみ
        if event.get("thread_ts") and cmd_type:
            try:
                _handle_command(cmd_type, cmd_option, channel_id, thread_ts, event)
            except Exception as e:
                print(f"[コマンド処理エラー] {e}")
                post_to_slack(channel_id, thread_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "⚠️ *コマンド処理エラー*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    f"{e}")
            return  # コマンドならAI判定はしない

        # ── チェックリスト応答判定 ─────────────────────────
        checklist = get_checklist_state(channel_id, thread_ts)
        if checklist:
            if checklist["is_completed"]:
                # チェックリスト完了済みスレッド：画像のみ投稿は無視（再判定しない）
                if image_only_post:
                    return
            else:
                # チェックリスト未完了
                n = normalize_keyword(user_message)
                # 先頭がS/A/B/C/D → 状態ランク＋コメントの回答とみなす
                is_checklist_input = n and n[0].upper() in CONDITION_MAP
                if is_checklist_input:
                    try:
                        _handle_checklist(checklist, user_message, channel_id, thread_ts, event)
                    except Exception as e:
                        print(f"[チェックリスト処理エラー] {e}")
                        post_to_slack(channel_id, thread_ts,
                            "━━━━━━━━━━━━━━━━\n"
                            "⚠️ *チェックリスト処理エラー*\n"
                            "━━━━━━━━━━━━━━━━\n\n"
                            f"{e}")
                    return
                elif image_only_post:
                    # 番号なしで写真だけ送ってきた場合 → 番号入力を促す
                    post_to_slack(channel_id, thread_ts,
                        "写真を受け取りました。\n\n"
                        "状態ランク（S/A/B/C/D）を一言添えて返信してください。\n"
                        "例：`B 電源OK、外観に小傷あり`",
                        mention_user=event.get("user", ""))
                    return

    # ── 通常のAI判定フロー ────────────────────────────────
    # スレッド内の返信であれば会話履歴を取得
    history = []
    if event.get("thread_ts"):
        try:
            history = fetch_thread_messages(channel_id, thread_ts, current_ts)
            print(f"[会話履歴] {len(history)}件")
        except Exception as e:
            print(f"[会話履歴取得エラー] {e}")

    try:
        print("[Claude API呼び出し中...]")
        judgment_text = call_claude(user_message, image_urls, history)
        print(f"[Claude応答] {judgment_text[:50]}")

        # 管理番号は確定時に発行するため、ここでは判定結果のみ返信
        user_id = event.get("user", "")
        post_to_slack(channel_id, thread_ts, judgment_text, mention_user=user_id)
        print("[Slack返信完了]")
    except Exception as e:
        print(f"[エラー] {e}")
        try:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *エラーが発生しました*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"{e}")
        except Exception as e2:
            print(f"[Slack送信エラー] {e2}")


def _handle_zaiko_search(keyword: str, channel_id: str, thread_ts: str, event: dict) -> None:
    """在庫検索コマンドを処理する"""
    user_id = event.get("user", "")
    results = search_inventory(keyword)
    persona = BOT_PERSONA["status"]
    if not results:
        msg = persona["search_none"].format(keyword=keyword)
        post_to_slack(channel_id, thread_ts, f"🔍 {msg}", mention_user=user_id, bot_role="status")
        return

    header = persona["search_found"].format(keyword=keyword, count=len(results))
    monday_url = f"https://monday.com/boards/{MONDAY_BOARD_ID}"
    lines = [
        f"{header}",
        "━━━━━━━━━━━━━━━━",
    ]
    for i, r in enumerate(results[:10], 1):
        kanri = r["kanri_bango"] or "番号なし"
        status = r["status"] or "不明"
        zaiko = r["zaiko_kikan"] or "─"
        channel = r["channel"] or "─"
        line = (
            f"*{i}. {r['name']}*\n\n"
            f"　🔖 管理番号　：`{kanri}`\n\n"
            f"　📺 チャンネル：{channel}\n\n"
            f"　📊 ステータス：{status}\n\n"
            f"　📅 在庫期間　：{zaiko}\n\n"
            f"　<{monday_url}|📷 Monday.comで詳細・画像を確認>\n\n"
            "─────────────────"
        )
        lines.append(line)

    if len(results) > 10:
        lines.append(f"_他 {len(results) - 10} 件はMonday.comで確認できます。_")

    post_to_slack(channel_id, thread_ts, "\n".join(lines), mention_user=user_id, bot_role="status")


# まとめ売り系チャンネル（確定時に選択肢を表示する対象）
MATOME_CHANNELS = {"eBayまとめ", "ヤフオクまとめ", "ロット販売"}


def get_matome_pending_from_thread(channel_id: str, thread_ts: str):
    """スレッド内にまとめ売り選択待ちメッセージがあればチャンネル名を返す"""
    import re as _re
    token = get_slack_token()
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"channel": channel_id, "ts": thread_ts}
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=20)
        data = response.json()
    except Exception as e:
        print(f"[まとめ選択待ち確認エラー] {e}")
        return None
    for msg in data.get("messages", []):
        if not (msg.get("bot_id") or msg.get("bot_profile")):
            continue
        m = _re.search(r'\[まとめ選択待ち:([^\]]+)\]', msg.get("text", ""))
        if m:
            return m.group(1)
    return None


def _complete_kakutei(kakutei_channel: str, judgment: dict, user_id: str,
                      channel_id: str, thread_ts: str, event: dict,
                      with_management_number: bool = True,
                      send_reply: bool = True) -> None:
    """分荷確定の共通処理（管理番号発行・スプレッドシート転記・Slack返信）"""
    TSUHAN_CHANNELS = {
        "eBayシングル", "eBayまとめ",
        "ヤフオクヴィンテージ", "ヤフオク現行", "ヤフオクまとめ",
    }

    # 管理番号発行（通販チャンネル かつ with_management_number=True の場合のみ）
    management_number = ""
    if with_management_number and kakutei_channel in TSUHAN_CHANNELS:
        management_number = generate_management_number()
        print(f"[管理番号発行] {management_number} (チャンネル:{kakutei_channel})")

    # 分荷作業時間を計算
    sakugyou_jikan = 0
    try:
        post_ts = float(thread_ts)
        confirm_ts = float(event.get("ts", thread_ts))
        sakugyou_jikan = max(0, int((confirm_ts - post_ts) / 60))
        print(f"[作業時間] {sakugyou_jikan}分")
    except Exception as e:
        print(f"[作業時間計算エラー] {e}")

    # スプレッドシートに転記
    payload = {
        "kanri_bango":         management_number,
        "kakutei_channel":     kakutei_channel,
        "first_channel":       judgment.get("first_channel", ""),
        "second_channel":      judgment.get("second_channel", ""),
        "item_name":           judgment.get("item_name", ""),
        "maker":               judgment.get("maker", ""),
        "model_number":        judgment.get("model_number", ""),
        "condition":           judgment.get("condition", ""),
        "predicted_price":     judgment.get("predicted_price", ""),
        "start_price":         judgment.get("start_price", ""),
        "target_price":        judgment.get("target_price", ""),
        "inventory_period":    judgment.get("inventory_period", ""),
        "inventory_deadline":  judgment.get("inventory_deadline", ""),
        "score":               judgment.get("first_score", ""),
        "storage_cost":        judgment.get("storage_cost", ""),
        "packing_cost":        judgment.get("packing_cost", ""),
        "expected_roi":        judgment.get("expected_roi", ""),
        "internal_keyword":    judgment.get("internal_keyword", ""),
        "staff_id":            get_staff_code(user_id),
        "sakugyou_jikan":      sakugyou_jikan,
        "timestamp":           datetime.now().strftime("%Y/%m/%d %H:%M"),
    }
    send_to_spreadsheet(payload)

    # 通販チャンネル かつ 管理番号あり → Monday.com登録
    if management_number:
        try:
            item_name = judgment.get("item_name") or kakutei_channel
            register_to_monday(management_number, item_name, judgment, user_id, sakugyou_jikan, kakutei_channel=kakutei_channel)
            print("[Monday.com登録完了]")
        except Exception as me:
            print(f"[Monday.com登録エラー] {me}")

    # Slack確定返信（send_reply=Falseの場合は呼び出し元が返信を担当）
    if send_reply:
        persona = BOT_PERSONA["bunika"]
        if management_number:
            reply = persona["confirm"].format(channel=kakutei_channel, kanri=management_number)
        else:
            reply = persona["confirm_only"].format(channel=kakutei_channel)
        post_to_slack(channel_id, thread_ts, reply, mention_user=user_id)

    # 高額案件メンション（目標価格30,000円以上）
    try:
        target_price_val = int(str(judgment.get("target_price", "0")).replace(",", ""))
    except (ValueError, TypeError):
        target_price_val = 0
    if target_price_val >= 30000:
        post_to_slack(channel_id, thread_ts,
            f"<@{ASANO_USER_ID}> 高額案件の確定が入りました。\n"
            f"予想販売価格：¥{target_price_val:,}\n"
            f"チャンネル：{kakutei_channel}\n"
            f"担当：<@{user_id}>"
        )


def _handle_matome_choice(choice: str, kakutei_channel: str, channel_id: str,
                          thread_ts: str, event: dict) -> None:
    """まとめ売り選択（1/2/3）を処理する"""
    user_id = event.get("user", "")
    judgment = get_judgment_from_thread(channel_id, thread_ts)

    if choice == '1':
        # まとめ保管（管理番号なし）← デフォルト選択肢
        _complete_kakutei(kakutei_channel, judgment, user_id, channel_id, thread_ts, event, with_management_number=False, send_reply=False)
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "📦 *まとめ保管として記録しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "同カテゴリのまとめ対象商品と一緒に保管してください。\n"
            "まとめ販売が決まった時点で改めて管理番号を発行します。",
            mention_user=user_id
        )

    elif choice == '2':
        # 個別に管理番号を発行して通常確定
        _complete_kakutei(kakutei_channel, judgment, user_id, channel_id, thread_ts, event, with_management_number=True)

    elif choice == '3':
        # 保留・浅野に相談
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "⏸️ *保留にしました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"<@{ASANO_USER_ID}> 確定方法について相談があります。\n"
            f"担当：<@{user_id}>"
        )


def _handle_command(cmd_type: str, cmd_option: str, channel_id: str, thread_ts: str, event: dict) -> None:
    """コマンド（確定/再判定/保留）を処理する"""
    user_id = event.get("user", "不明")

    # 通販対象チャンネル（管理番号・monday.com登録対象）
    # ロット販売・社内利用・スクラップ・廃棄は管理番号なし・スプレッドシートのみ
    TSUHAN_CHANNELS = {
        "eBayシングル", "eBayまとめ",
        "ヤフオクヴィンテージ", "ヤフオク現行", "ヤフオクまとめ",
    }

    if cmd_type == 'kakutei':
        judgment = get_judgment_from_thread(channel_id, thread_ts)
        if not judgment.get("first_channel"):
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *判定データなし*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "判定データが見つかりませんでした。\n\n"
                "先に分荷判定を実行してください。")
            return

        # 確定チャンネルを決定（表記ゆれを正規化）
        if cmd_option == '1':
            kakutei_channel = normalize_channel(judgment.get("first_channel", ""))
        elif cmd_option == '2':
            kakutei_channel = normalize_channel(judgment.get("second_channel", ""))
        else:
            kakutei_channel = normalize_channel(cmd_option)  # 確定/○○ の場合

        # まとめ売り系チャンネルは選択肢を表示して一旦止める
        if kakutei_channel in MATOME_CHANNELS:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                f"📦 *{kakutei_channel}*（まとめ売り）が選択されました\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "どちらで進めますか？\n\n"
                "1️⃣  まとめ保管する（管理番号なし）← *ほとんどの場合はこちら*\n\n"
                "2️⃣  個別に管理番号を発行して確定する\n\n"
                "3️⃣  保留にして浅野に相談する\n\n"
                f"`1` `2` `3` のいずれかを返信してください。\n\n"
                f"_[まとめ選択待ち:{kakutei_channel}]_",
                mention_user=user_id
            )
            return

        # まとめ以外 → 通常確定処理
        _complete_kakutei(kakutei_channel, judgment, user_id, channel_id, thread_ts, event)

    elif cmd_type == 'saihantei':
        persona = BOT_PERSONA["bunika"]
        post_to_slack(channel_id, thread_ts, persona["saihantei"])
        try:
            history = fetch_thread_messages(channel_id, thread_ts, event.get("ts", ""))
        except Exception:
            history = []
        judgment_text = call_claude("添付の情報をもとに改めて分荷判定してください。", history=history)
        post_to_slack(channel_id, thread_ts, judgment_text)

    elif cmd_type == 'horyuu':
        persona = BOT_PERSONA["bunika"]
        post_to_slack(channel_id, thread_ts, persona["horyuu"])

    elif cmd_type == 'cancel':
        confirmation = get_confirmation_from_thread(channel_id, thread_ts)
        kanri_bango = confirmation["kanri_bango"]
        confirmed_channel = confirmation["kakutei_channel"]

        if confirmed_channel:
            # 確定済み（管理番号あり・なし両方）→ スプレッドシートにキャンセル行追記
            cancel_payload = {
                # 管理番号なしの場合は「---」を記入してキャンセル行と識別できるようにする
                "kanri_bango":      kanri_bango if kanri_bango else "---",
                "kakutei_channel":  f"キャンセル（{confirmed_channel}）",
                "first_channel":    "",
                "second_channel":   "",
                "predicted_price":  "",
                "inventory_period": "",
                "score":            "",
                "internal_keyword": "",
                "staff_id":         user_id,
                "timestamp":        datetime.now().strftime("%Y/%m/%d %H:%M"),
            }
            send_to_spreadsheet(cancel_payload)

            # 管理番号ありの場合のみMonday.comも更新
            persona = BOT_PERSONA["bunika"]
            if kanri_bango:
                try:
                    cancel_monday_item(kanri_bango)
                except Exception as e:
                    print(f"[Monday.comキャンセルエラー] {e}")
                post_to_slack(channel_id, thread_ts,
                    persona["cancel_kanri"].format(kanri=kanri_bango),
                    mention_user=user_id)
            else:
                post_to_slack(channel_id, thread_ts,
                    persona["cancel_only"].format(channel=confirmed_channel),
                    mention_user=user_id)
        else:
            # 確定前キャンセル → 記録なし
            persona = BOT_PERSONA["bunika"]
            post_to_slack(channel_id, thread_ts, persona["cancel_none"], mention_user=user_id)


def _handle_checklist(checklist: dict, raw_text: str, channel_id: str, thread_ts: str, event: dict) -> None:
    """チェックリスト応答（状態番号＋フリーコメント）を処理する"""
    user_id = event.get("user", "")
    management_number = checklist["management_number"]

    # 先頭のアルファベット（S/A/B/C/D）を状態ランクとして取得、残りをコメントとして扱う
    n = normalize_keyword(raw_text)
    condition_key = n[0].upper() if n else ""
    condition_label = CONDITION_MAP.get(condition_key, "")
    comment = n[1:].strip() if len(n) > 1 else ""

    reply = (
        "━━━━━━━━━━━━━━━━\n"
        "✅ *動作確認完了*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{management_number}*\n\n"
        f"📊 状態\n"
        f"　*{condition_label}*"
    )
    if comment:
        reply += f"\n\n💬 コメント\n　{comment}"
    reply += "\n━━━━━━━━━━━━━━━━"

    post_to_slack(channel_id, thread_ts, reply, mention_user=user_id)

    # Monday.comのステータス・状態を更新
    try:
        update_monday_columns(management_number, {
            "status": {"label": "動作確認済み"},
            "condition": condition_label,
        })
    except Exception as e:
        print(f"[Monday.com動作確認更新エラー] {e}")

    # スプレッドシートに動作確認結果を記録
    try:
        send_to_spreadsheet({
            "action":           "checklist_update",
            "kanri_bango":      management_number,
            "condition":        condition_label,
            "checklist_comment": comment,
            "staff_id":         get_staff_code(user_id),
            "timestamp":        datetime.now().strftime("%Y/%m/%d %H:%M"),
        })
    except Exception as e:
        print(f"[スプレッドシート動作確認更新エラー] {e}")


# ── 撮影確認チャンネル ────────────────────────────────

def get_drive_service():
    """Google Drive APIサービスを返す。認証情報未設定の場合はNone"""
    import base64
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    json_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not json_b64:
        print("[Google Drive] GOOGLE_SERVICE_ACCOUNT_JSON 未設定・スキップ")
        return None
    try:
        creds_dict = json.loads(base64.b64decode(json_b64).decode())
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"[Google Drive認証エラー] {e}")
        return None


def get_or_create_drive_folder(service, parent_id: str, folder_name: str) -> str:
    """指定フォルダ内のサブフォルダを取得または作成してIDを返す"""
    query = (
        f"name='{folder_name}' and '{parent_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return folder["id"]


def upload_images_to_drive(management_number: str, image_urls: list, is_tepura: bool = False) -> str:
    """画像をGoogle Driveの管理番号フォルダにアップロードし、フォルダURLを返す"""
    import io
    from googleapiclient.http import MediaIoBaseUpload

    service = get_drive_service()
    root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    if not service or not root_folder_id:
        return ""

    # フォルダ構成: TakeBack商品画像/YYMM/管理番号/
    yymm = management_number[:4]
    yymm_id = get_or_create_drive_folder(service, root_folder_id, yymm)
    item_id = get_or_create_drive_folder(service, yymm_id, management_number)

    # 既存ファイル数から採番開始番号を決定
    existing = service.files().list(
        q=f"'{item_id}' in parents and trashed=false",
        fields="files(name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute().get("files", [])
    next_num = len(existing) + 1

    token = get_slack_token()
    headers = {"Authorization": f"Bearer {token}"}
    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}

    for i, url in enumerate(image_urls):
        try:
            resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            ext = ext_map.get(content_type, "jpg")
            filename = f"01_テプラ.{ext}" if is_tepura else f"{next_num + i:02d}_商品.{ext}"
            media = MediaIoBaseUpload(io.BytesIO(resp.content), mimetype=content_type)
            service.files().create(
                body={"name": filename, "parents": [item_id]},
                media_body=media, fields="id",
                supportsAllDrives=True
            ).execute()
            print(f"[Drive] {filename} アップロード完了")
        except Exception as e:
            print(f"[Drive] アップロードエラー: {e}")

    return f"https://drive.google.com/drive/folders/{item_id}"


def get_drive_folder_id(management_number: str):
    """管理番号からDriveフォルダIDを取得する。存在しない場合はNone"""
    service = get_drive_service()
    root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    if not service or not root_folder_id:
        return None, None
    yymm = management_number[:4]
    try:
        # YYMMフォルダを検索
        query = f"name='{yymm}' and '{root_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        yymm_files = results.get("files", [])
        if not yymm_files:
            return None, service
        # 管理番号フォルダを検索
        query = f"name='{management_number}' and '{yymm_files[0]['id']}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        item_files = results.get("files", [])
        if not item_files:
            return None, service
        return item_files[0]["id"], service
    except Exception as e:
        print(f"[Drive] フォルダ検索エラー: {e}")
        return None, service


def list_drive_images(management_number: str, exclude_tepura: bool = True) -> list:
    """管理番号のDriveフォルダ内の画像一覧を返す。テプラ除外オプション付き"""
    folder_id, service = get_drive_folder_id(management_number)
    if not folder_id or not service:
        return []
    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType contains 'image/'",
            fields="files(id, name, webViewLink, webContentLink)",
            orderBy="name",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = results.get("files", [])
        if exclude_tepura:
            files = [f for f in files if "テプラ" not in f.get("name", "")]
        return files
    except Exception as e:
        print(f"[Drive] 画像一覧取得エラー: {e}")
        return []


def delete_drive_file(file_id: str) -> bool:
    """DriveファイルをゴミboxUnknownに移動（削除）"""
    service = get_drive_service()
    if not service:
        return False
    try:
        service.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
        return True
    except Exception as e:
        print(f"[Drive] ファイル削除エラー: {e}")
        return False


def upload_shuppinon_image(management_number: str, image_urls: list) -> list:
    """出品チャンネルから追加撮影した画像をDriveにアップロード（sp01_出品追加.jpg形式）"""
    import io
    from googleapiclient.http import MediaIoBaseUpload

    folder_id, service = get_drive_folder_id(management_number)
    if not folder_id or not service:
        return []

    # 既存のsp付きファイル数を取得して採番
    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false and name contains 'sp'",
            fields="files(name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        existing_sp = [f for f in results.get("files", []) if re.match(r'^sp\d+_', f["name"])]
        next_num = len(existing_sp) + 1
    except Exception:
        next_num = 1

    token = get_slack_token()
    headers = {"Authorization": f"Bearer {token}"}
    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
    uploaded = []

    for i, url in enumerate(image_urls):
        try:
            resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            ext = ext_map.get(content_type, "jpg")
            filename = f"sp{next_num + i:02d}_出品追加.{ext}"
            media = MediaIoBaseUpload(io.BytesIO(resp.content), mimetype=content_type)
            result = service.files().create(
                body={"name": filename, "parents": [folder_id]},
                media_body=media, fields="id,name",
                supportsAllDrives=True
            ).execute()
            uploaded.append(result)
            print(f"[Drive] {filename} 出品追加アップロード完了")
        except Exception as e:
            print(f"[Drive] 出品追加アップロードエラー: {e}")

    return uploaded


def replace_drive_file(file_id: str, image_url: str) -> bool:
    """既存のDriveファイルを新画像で上書き"""
    import io
    from googleapiclient.http import MediaIoBaseUpload

    service = get_drive_service()
    if not service:
        return False

    token = get_slack_token()
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = httpx.get(image_url, headers=headers, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        media = MediaIoBaseUpload(io.BytesIO(resp.content), mimetype=content_type)
        service.files().update(
            fileId=file_id, media_body=media,
            supportsAllDrives=True
        ).execute()
        return True
    except Exception as e:
        print(f"[Drive] ファイル上書きエラー: {e}")
        return False


def extract_management_number_from_image(image_url: str) -> str:
    """テプラ画像からClaude Visionで管理番号を読み取る"""
    import re
    try:
        image_data, media_type = fetch_image_as_base64(image_url)
        client = get_anthropic_client()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                    {"type": "text", "text": (
                        "この画像のテプラ（ラベル）に書かれた管理番号を読み取ってください。"
                        "管理番号は「2603-0001」または「2603G0001」のような形式です（年月4桁＋ハイフン＋4桁 または 年月4桁＋英字1文字＋4桁）。"
                        "管理番号だけを返してください。見つからない場合は「不明」と返してください。"
                    )}
                ]
            }]
        )
        text = response.content[0].text.strip()
        m = re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
        return m.group(0) if m else ""
    except Exception as e:
        print(f"[管理番号読取エラー] {e}")
        return ""


def get_management_number_from_satsuei_thread(channel_id: str, thread_ts: str) -> str:
    """撮影スレッド内のBot確認メッセージから管理番号を取得する"""
    import re
    token = get_slack_token()
    response = httpx.get(
        "https://slack.com/api/conversations.replies",
        headers={"Authorization": f"Bearer {token}"},
        params={"channel": channel_id, "ts": thread_ts}, timeout=10
    )
    for msg in response.json().get("messages", []):
        if not (msg.get("bot_id") or msg.get("bot_profile")):
            continue
        m = re.search(r'管理番号\s*\*?(\d{4}(?:[VGME]\d{4}|-\d{4}))\*?', msg.get("text", ""))
        if m:
            return m.group(1)
    return ""


def handle_satsuei_channel(event: dict) -> None:
    """撮影確認チャンネルのイベントを処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_id = event.get("user", "")
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]
    text = normalize_keyword(event.get("text", ""))
    is_new_post = not event.get("thread_ts")

    # ── 新規投稿（テプラ写真 or テキストで管理番号）──────
    if is_new_post:
        import re as _re
        # テキストで管理番号が直接入力された場合
        text_mn = _re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
        if not image_urls and not text_mn:
            print(f"[撮影CH無視] 管理番号なし・画像なし channel={channel_id} text={text[:30]!r}")
            return
        if text_mn and not image_urls:
            management_number = text_mn.group(0)
        elif image_urls:
            management_number = extract_management_number_from_image(image_urls[0])
            if not management_number:
                post_to_slack(channel_id, current_ts,
                    "⚠️ *読み取りエラー*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    "テプラの管理番号を\n"
                    "読み取れませんでした。\n\n"
                    "📌 *対処方法*\n"
                    "　① テプラをもう一度撮影して送る\n"
                    "　② または管理番号をテキストで入力\n"
                    "　　例） *2603-0001*",
                    bot_role="satsuei")
                return
            # テプラ画像をDriveに保存
            upload_images_to_drive(management_number, [image_urls[0]], is_tepura=True)
        else:
            return
        post_to_slack(channel_id, current_ts,
            "📸 *撮影セッション開始*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号　*{management_number}*\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 *作業手順*\n\n"
            "　① このスレッドに商品写真を投稿\n"
            "　　（複数枚まとめてOK）\n\n"
            "　② Botの確認メッセージが届いたら\n"
            "　　写真をチェックする\n\n"
            "　③ 問題なければ `完了` と送信\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "💡 *使えるコマンド*\n\n"
            "　`完了` ／ 撮影完了・Driveに保存\n"
            "　`やり直し` ／ 写真を全削除して1枚目から撮り直す\n"
            "　`キャンセル` ／ 作業を中断する",
            bot_role="satsuei")
        return

    # ── スレッド内（商品写真 or 完了 or キャンセル/削除）──
    # 削除確認待ちの処理
    if handle_delete_step2(channel_id, thread_ts, user_id, text):
        return

    management_number = get_management_number_from_satsuei_thread(channel_id, thread_ts)
    if not management_number:
        # 削除コマンド（セッションなし）
        if text == "削除":
            handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["satsuei"], "satsuei")
        else:
            print(f"[撮影CH無視] スレッド内・セッションなし channel={channel_id} text={text[:30]!r}")
        return

    # キャンセル・中断
    if text in CANCEL_WORDS:
        log_work_activity(CHANNEL_NAMES["satsuei"], management_number, get_staff_code(user_id), "キャンセル")
        post_to_slack(channel_id, thread_ts,
            "⏹️ *撮影作業を中断しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号　*{management_number}*\n\n"
            "作業を再開するときは\n"
            "もう一度管理番号を投稿してください。",
            mention_user=user_id, bot_role="satsuei")
        return

    # やり直しコマンド
    if text == "やり直し":
        deleted_count = 0
        try:
            svc = get_drive_service()
            root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
            if svc and root_folder_id:
                yymm_id = get_or_create_drive_folder(svc, root_folder_id, management_number[:4])
                item_id = get_or_create_drive_folder(svc, yymm_id, management_number)
                files = svc.files().list(
                    q=f"'{item_id}' in parents and trashed=false and not name contains '01_'",
                    fields="files(id,name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True
                ).execute().get("files", [])
                for f in files:
                    svc.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                    deleted_count += 1
        except Exception as e:
            print(f"[Drive やり直しエラー] {e}")
        post_to_slack(channel_id, thread_ts,
            "🔄 *写真をやり直します*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🗑️ 削除した写真　*{deleted_count}枚*\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 *1枚目から撮り直してください*\n\n"
            "このスレッドに\n"
            "新しい写真を投稿してください。\n\n"
            "　• テプラ画像は残してあります\n"
            "　• 商品写真のみ全て削除しました",
            mention_user=user_id, bot_role="satsuei")
        return

    # 削除コマンド
    if text == "削除":
        handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["satsuei"], "satsuei")
        return

    # 商品写真をDriveに保存
    folder_url = ""
    if image_urls:
        folder_url = upload_images_to_drive(management_number, image_urls, is_tepura=False)
        post_to_slack(channel_id, thread_ts,
            f"📷 *{len(image_urls)}枚* を受け取りました\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "🔍 *投稿した写真を確認してください*\n\n"
            "　□ ピントが合っているか\n"
            "　□ 明るさは適切か\n"
            "　□ 角度・アングルは揃っているか\n"
            "　□ 枚数は足りているか\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ 問題なければ `完了` と送信\n"
            "📷 追加写真があればそのまま投稿\n"
            "🔄 撮り直す場合は `やり直し` と送信",
            mention_user=user_id, bot_role="satsuei")

    # 完了コマンド
    if text == "完了":
        post_to_slack(channel_id, thread_ts,
            "✅ *撮影完了！お疲れ様でした*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号　*{management_number}*\n\n"
            "写真をDriveに保存しました。\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 *次の作業*\n\n"
            "　このトークの元メッセージを削除して\n"
            "　次の商品に進んでください。",
            mention_user=user_id, bot_role="satsuei")
        log_work_activity(CHANNEL_NAMES["satsuei"], management_number, get_staff_code(user_id), "完了")
        try:
            update_monday_columns(management_number, {
                "status": {"label": "撮影完了"},
                "satsuei_tantosha": get_staff_code(user_id),
                "satsuei_date": {"date": datetime.now().strftime("%Y-%m-%d")},
                "drive_url": folder_url,
            })
        except Exception as e:
            print(f"[Monday.com撮影完了更新エラー] {e}")
        # 完了メッセージと写真投稿が別メッセージの場合、folder_urlが空になるため取得し直す
        if not folder_url:
            try:
                _svc = get_drive_service()
                _root = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
                if _svc and _root:
                    _yymm_id = get_or_create_drive_folder(_svc, _root, management_number[:4])
                    _item_id = get_or_create_drive_folder(_svc, _yymm_id, management_number)
                    folder_url = f"https://drive.google.com/drive/folders/{_item_id}"
            except Exception as e:
                print(f"[DriveフォルダURL取得エラー] {e}")
        try:
            send_to_spreadsheet({
                "action":           "satsuei_update",
                "kanri_bango":      management_number,
                "drive_folder_url": folder_url,
                "staff_id":         get_staff_code(user_id),
                "timestamp":        datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        except Exception as e:
            print(f"[スプレッドシート撮影完了更新エラー] {e}")


# ── 共通：キャンセル・削除・作業ログ ──────────────────────

delete_confirm_sessions = {}  # {thread_ts: {"channel_id":..,"management_number":..,"channel_name":..,"staff_id":..}}
daily_stats = {}              # {staff_id: {"完了": 0, "キャンセル": 0, "削除": 0}}

CHANNEL_NAMES = {
    "satsuei":   "商品撮影",
    "shuppinon": "出品保管",
    "konpo":     "梱包出荷",
}

CANCEL_WORDS = ("キャンセル", "中断")


def log_work_activity(channel_name: str, management_number: str, staff_id: str,
                      operation: str, start_time=None) -> None:
    """作業ログをスプレッドシートに送り、日次カウントを更新する"""
    now = datetime.now()
    duration = int((now - start_time).total_seconds()) if start_time else 0
    if staff_id not in daily_stats:
        daily_stats[staff_id] = {"完了": 0, "キャンセル": 0, "削除": 0}
    if operation in daily_stats[staff_id]:
        daily_stats[staff_id][operation] += 1
    try:
        send_to_spreadsheet({
            "action":           "work_activity",
            "channel":          channel_name,
            "kanri_bango":      management_number,
            "staff_id":         staff_id,
            "operation":        operation,
            "duration_seconds": str(duration),
            "timestamp":        now.strftime("%Y/%m/%d %H:%M"),
        })
    except Exception as e:
        print(f"[作業ログ送信エラー] {e}")


def handle_delete_step1(channel_id: str, thread_ts: str, user_id: str, channel_name: str, bot_role: str) -> None:
    """削除コマンド受付：管理番号の入力を求める"""
    delete_confirm_sessions[thread_ts] = {
        "channel_id":   channel_id,
        "channel_name": channel_name,
        "staff_id":     get_staff_code(user_id),
        "bot_role":     bot_role,
    }
    post_to_slack(channel_id, thread_ts,
        "━━━━━━━━━━━━━━━━\n"
        "🗑️ *削除確認*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "削除する管理番号を入力してください。\n\n"
        "　例：`2603-0001`\n\n"
        "⚠️ 削除するとMonday.comのステータスが\n"
        "　「確認／相談」に戻ります。",
        mention_user=user_id, bot_role=bot_role)


def handle_delete_step2(channel_id: str, thread_ts: str, user_id: str, text: str) -> bool:
    """削除確認：管理番号が一致したら削除を実行。処理した場合Trueを返す"""
    import re as _re
    pending = delete_confirm_sessions.get(thread_ts)
    if not pending:
        return False
    mn_m = _re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
    if not mn_m:
        return False
    management_number = mn_m.group(0)
    channel_name = pending["channel_name"]
    bot_role = pending["bot_role"]
    staff_id = pending["staff_id"]
    del delete_confirm_sessions[thread_ts]
    try:
        update_monday_item_status(management_number, "確認／相談")
    except Exception as e:
        print(f"[Monday.com削除更新エラー] {e}")
    log_work_activity(channel_name, management_number, staff_id, "削除")
    post_to_slack(channel_id, thread_ts,
        "━━━━━━━━━━━━━━━━\n"
        "🗑️ *削除完了*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{management_number}*\n\n"
        "Monday.comのステータスを\n"
        "「確認／相談」に戻しました。",
        mention_user=user_id, bot_role=bot_role)
    return True


# ── 出品チャンネル ────────────────────────────────

# 出品データの一時保管（スレッドTS → 出品セッション）
listing_sessions = {}

LISTING_COMMANDS = {
    "タイトル":  "title",
    "開始価格":  "start_price",
    "説明文":    "description",
    "サイズ":    "size",
}

# ロケーション番号のバリデーションパターン
LOCATION_PATTERN = re.compile(
    r'^('
    r'[A-Za-z]-?\d{1,3}(\s?[A-Za-z横奥])?'   # A-12, A12, B2 x, A2横
    r'|倉庫[外奥]?'                              # 倉庫, 倉庫外, 倉庫奥
    r'|\d{1,2}[階F]'                             # 2階, 1F
    r')$',
    re.IGNORECASE
)


def parse_listing_command(text: str):
    """出品データ修正コマンドを解析して (field, value) を返す"""
    n = normalize_keyword(text)
    for jp, field in LISTING_COMMANDS.items():
        for sep in ("：", ":"):
            prefix = f"{jp}{sep}"
            if n.startswith(prefix):
                return field, n[len(prefix):].strip()
    return None, None


def get_item_from_monday(management_number: str) -> dict:
    """monday.comから管理番号に対応するアイテムデータを取得する（新ボード→旧ボードの順に検索）"""
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items {
                    name
                    column_values { id text }
                }
            }
        }
    }
    """
    result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
    items = (result.get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    for item in items:
        cols = {cv["id"]: cv["text"] for cv in item.get("column_values", [])}
        if cols.get("kanri_bango") == management_number:
            return {"monday_name": item["name"], "is_old_board": False, **cols}
    # 新ボードになければ旧ボードを検索
    return get_item_from_old_boards(management_number)


def get_item_from_old_boards(management_number: str) -> dict:
    """旧Monday.comボード（2枚）から管理番号に対応するアイテムを検索する"""
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items {
                    id
                    name
                    column_values { id text }
                }
            }
        }
    }
    """
    for board_id in MONDAY_OLD_BOARD_IDS:
        try:
            result = monday_graphql(query, {"board_id": board_id})
            items = (result.get("data", {}).get("boards", [{}])[0]
                     .get("items_page", {}).get("items", []))
            for item in items:
                cols = {cv["id"]: cv["text"] for cv in item.get("column_values", [])}
                # アイテム名または全列の値から管理番号を検索
                found = (management_number in item.get("name", "") or
                         any(v == management_number for v in cols.values()))
                if found:
                    print(f"[旧ボード発見] board={board_id} item={item['name']}")
                    return {
                        "monday_name":     item["name"],
                        "monday_item_id":  item["id"],
                        "monday_board_id": board_id,
                        "is_old_board":    True,
                        **cols,
                    }
        except Exception as e:
            print(f"[旧ボード検索エラー board={board_id}] {e}")
    return {}


# サイズ別最低出品金額（これ以下は個別出品しない→まとめ売りorスクラップ）
# 赤字ラインの1.5倍。人件費2倍補正済み。営業利益目標80万/月ベース
MIN_LISTING_PRICE = {
    80:  2200,   # 小型（60〜80）
    140: 4500,   # 中型（100〜140）
    200: 7500,   # 大型（160〜200）
    999: 12000,  # 超大型（220〜）
}


def get_min_listing_price(size: int) -> int:
    """サイズ（三辺合計cm）から最低出品金額を返す"""
    for max_size, price in sorted(MIN_LISTING_PRICE.items()):
        if size <= max_size:
            return price
    return MIN_LISTING_PRICE[999]


# アカウント別タイトル・説明文ルール
LISTING_RULES = {
    "ヤフオクヴィンテージ": {
        "brand_tag": "古道具と器のライノトリ",
        "title_style": (
            "古道具専門店のタイトルスタイルで作成。\n"
            "構造: [商品名]◇[タグ1]｜[タグ2]｜...｜古道具と器のライノトリ\n"
            "・商品名は素材＋品名を簡潔に（例:「南部鉄器 岩鋳 鉄瓶」）\n"
            "・◇の後に検索用タグを｜区切りで列挙（寸法｜在銘｜素材｜用途｜時代）\n"
            "・末尾は必ず「古道具と器のライノトリ」で締める\n"
            "・ペルソナ：30歳女性。暮らし・インテリアとしての魅力が伝わるタグ選び"
        ),
        "desc_style": (
            "古道具店の商品説明スタイルで作成。\n"
            "・簡潔で品のある文体。余計な装飾語は不要\n"
            "・構成：商品概要→状態詳細→サイズ→用途提案\n"
            "・経年変化はポジティブに表現（味わい・風合い）\n"
            "・インテリアとしての活用提案を一言添える\n"
            "・ペルソナ：30歳女性の暮らしに馴染む提案を意識"
        ),
        "price_style": (
            "値ごろ感のあるスタート価格。入札1〜3件で早く売り切る想定。\n"
            "予想販売価格の80〜100%程度。\n"
            "サイズ別最低金額: 60-80=¥2,200 / 100-140=¥4,500 / 160-200=¥7,500 / 220+=¥12,000\n"
            "安価スタートで入札数を集める戦略は取らない。商品相応の価格で設定する。"
        ),
    },
    "ヤフオク現行": {
        "brand_tag": "",
        "title_style": (
            "中古品販売のタイトルスタイルで作成。\n"
            "構造: [商品名]◇[タグ1]｜[タグ2]｜...\n"
            "・メーカー名＋品名＋型番を先頭に。検索されやすさ最優先\n"
            "・◇の後に状態｜付属品｜動作確認結果を｜区切りで列挙\n"
            "・スペック重視。感性的な表現より正確な情報"
        ),
        "desc_style": (
            "中古品の商品説明スタイルで作成。\n"
            "・メーカー・型番・スペックを明記\n"
            "・動作確認結果を具体的に\n"
            "・傷や汚れは場所と程度を正直に記載\n"
            "・付属品の有無を明記"
        ),
        "price_style": (
            "値ごろ感のあるスタート価格。入札1〜3件で早く売り切る想定。\n"
            "予想販売価格の80〜100%程度。\n"
            "サイズ別最低金額: 60-80=¥2,200 / 100-140=¥4,500 / 160-200=¥7,500 / 220+=¥12,000\n"
            "安価スタートで入札数を集める戦略は取らない。商品相応の価格で設定する。"
        ),
    },
    "ヤフオクまとめ": {
        "brand_tag": "",
        "title_style": (
            "まとめ売りのタイトルスタイルで作成。\n"
            "構造: [点数]点まとめ [代表商品名]◇[タグ1]｜[タグ2]｜...\n"
            "・点数を先頭に明記\n"
            "・代表的な商品名で内容が想像できるように\n"
            "・「まとめ」「セット」「大量」等のキーワードを含める"
        ),
        "desc_style": (
            "まとめ売りの商品説明スタイルで作成。\n"
            "・全体の点数と内訳を箇条書き\n"
            "・代表的な商品の状態を記載\n"
            "・1点あたりの単価のお得感を伝える"
        ),
        "price_style": "まとめ売りとしてお得感のあるスタート価格。1点あたり単価が安く見える設定。",
    },
}
# デフォルト（上記以外のチャンネル）
LISTING_RULES_DEFAULT = {
    "brand_tag": "",
    "title_style": (
        "ヤフオク出品タイトルを作成。\n"
        "構造: [商品名]◇[タグ1]｜[タグ2]｜...\n"
        "・商品名＋メーカー＋型番を先頭に\n"
        "・◇の後に検索用タグを｜区切りで列挙"
    ),
    "desc_style": (
        "ヤフオク商品説明文を作成。\n"
        "・商品の特徴・状態・付属品を簡潔に記載"
    ),
    "price_style": (
        "値ごろ感のあるスタート価格。入札1〜3件で早く売り切る想定。\n"
        "サイズ別最低金額: 60-80=¥2,200 / 100-140=¥4,500 / 160-200=¥7,500 / 220+=¥12,000"
    ),
}


def generate_listing_content(management_number: str, item_data: dict, max_title_len: int = 65) -> dict:
    """Claudeでヤフオク出品タイトル・説明文・価格を生成する"""
    import re
    client = get_anthropic_client()
    if not client:
        return {}

    item_name = item_data.get("item_name", "") or item_data.get("monday_name", "")
    maker = item_data.get("maker", "")
    model_number = item_data.get("model_number", "")
    condition = item_data.get("condition", "")
    channel = item_data.get("hantei_channel", "")
    price = item_data.get("yosou_kakaku", "")
    period = item_data.get("zaiko_kikan", "")
    kw = item_data.get("internal_keyword", "")

    # アカウント別ルールを取得
    rules = LISTING_RULES.get(channel, LISTING_RULES_DEFAULT)
    brand_tag = rules["brand_tag"]

    # ブランドタグ分の文字数を確保
    if brand_tag:
        tag_suffix = f"｜{brand_tag}"
        effective_title_len = max_title_len - len(tag_suffix)
    else:
        tag_suffix = ""
        effective_title_len = max_title_len

    prompt = (
        f"あなたはヤフオク出品のプロです。以下の商品情報をもとに出品データを作成してください。\n\n"
        f"【商品情報】\n"
        f"アイテム名：{item_name}\n"
        f"メーカー/ブランド：{maker}\n"
        f"品番/型式：{model_number}\n"
        f"商品状態：{condition}\n"
        f"販売チャンネル：{channel}\n"
        f"予想販売価格：{price}\n"
        f"内部KW：{kw}\n\n"
        f"【タイトルのルール】\n"
        f"{rules['title_style']}\n"
        f"・タイトル本文は{effective_title_len}文字以内（末尾にシステムが自動付与するタグがあるため）\n"
        f"・区切り記号は ◇（本文と詳細の間）と ｜（全角パイプ、詳細タグ間）のみ使用\n"
        f"・/（スラッシュ）や_（アンダーバー）は使わない\n\n"
        f"【説明文のルール】\n"
        f"{rules['desc_style']}\n"
        f"・600〜1000文字程度\n"
        f"・サイズは「未計測」と記載\n\n"
        f"【価格のルール】\n"
        f"{rules['price_style']}\n\n"
        f"以下のJSON形式のみで返してください（前置き不要）：\n"
        f'{{"title":"タイトル本文（{effective_title_len}文字以内）",'
        f'"description":"商品説明文",'
        f'"start_price":開始価格の数字}}'
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            result = json.loads(m.group(0))
            # ブランドタグを自動付与
            if brand_tag and result.get("title"):
                result["title"] = result["title"][:effective_title_len] + tag_suffix
            return result
    except Exception as e:
        print(f"[出品コンテンツ生成エラー] {e}")
    return {}


def _post_image_list(channel_id: str, thread_ts: str, management_number: str) -> None:
    """出品用の商品画像一覧をSlackスレッドに表示する（テプラ除外）"""
    images = list_drive_images(management_number, exclude_tepura=True)
    if not images:
        post_to_slack(channel_id, thread_ts,
            "📷 商品画像がありません。\n\n"
            "このスレッドに写真を投稿すると追加できます。",
            bot_role="shuppinon")
        return

    lines = [
        "━━━━━━━━━━━━━━━━",
        f"📷 *出品画像（{len(images)}枚）*",
        "━━━━━━━━━━━━━━━━",
        "",
    ]
    for i, img in enumerate(images, 1):
        name = img.get("name", "")
        link = img.get("webViewLink", "")
        lines.append(f"　[{i}] {name}" + (f"  <{link}|表示>" if link else ""))
    lines.extend([
        "",
        "─────────────────────",
        "*画像コマンド：*",
        "　写真を投稿 → 追加撮影",
        "　`画像削除 3` → 3枚目を削除",
        "　`順番入替 2 4` → 2枚目と4枚目を入替",
        "　`撮り直し 2` + 写真 → 2枚目を差替",
        "　`画像` → 一覧を再表示",
    ])
    post_to_slack(channel_id, thread_ts, "\n".join(lines), bot_role="shuppinon")


def post_listing_summary(channel_id: str, thread_ts: str, session: dict, mention_user: str = "") -> None:
    """出品データをSlackに整形して表示する"""
    mn = session["management_number"]
    start = session.get("start_price", 0)
    size = session.get("size", "")
    text = (
        "━━━━━━━━━━━━━━━━\n"
        "📦 *出品データ確認*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{mn}*\n\n"
        f"📋 タイトル\n"
        f"　{session.get('title', '（未設定）')}\n\n"
        f"📊 状態\n"
        f"　{session.get('condition', '（未確認）')}\n\n"
        f"💰 開始価格\n"
        f"　¥{start:,}\n\n"
        f"📐 梱包サイズ\n"
        f"　{size + 'サイズ' if size else '（推定中）'}\n\n"
        f"📝 説明文\n"
        f"{session.get('description', '（未生成）')}\n\n"
        "─────────────────────\n"
        "*修正する場合はコマンドで入力：*\n\n"
        "　`タイトル：新しいタイトル`\n"
        "　`開始価格：5000`\n"
        "　`説明文：新しい説明文`\n"
        "　`サイズ：120`\n\n"
        "─────────────────────\n"
        "✅ *次のステップ*\n\n"
        "　*Step 1:* ヤフオク/eBayのページを作成したら\n"
        "　　→ `ページ作成完了` と入力\n\n"
        "　*Step 2:* 棚に収納したら\n"
        "　　→ ロケーション番号を入力（例：`A-12`）"
    )
    post_to_slack(channel_id, thread_ts, text, mention_user=mention_user, bot_role="shuppinon")


def execute_listing(session: dict, location: str, channel_id: str, thread_ts: str, user_id: str) -> None:
    """出品を実行する（スプレッドシート記録 + Monday.com更新）"""
    import re
    management_number = session["management_number"]

    # ページ作成時間を計算（ページ作成完了〜ロケーション入力までの分数）
    page_creation_minutes = 0
    if session.get("page_created_time"):
        page_creation_minutes = max(0, int((datetime.now() - session["page_created_time"]).total_seconds() / 60))

    # スプレッドシートに出品データを記録
    try:
        send_to_spreadsheet({
            "action":                "shuppinon_listing",
            "kanri_bango":           management_number,
            "title":                 session.get("title", ""),
            "description":           session.get("description", ""),
            "condition":             session.get("condition", ""),
            "start_price":           str(session.get("start_price", "")),
            "buyout_price":          str(session.get("buyout_price", "")),
            "size":                  session.get("size", ""),
            "location":              location,
            "staff_id":              get_staff_code(user_id),
            "timestamp":             datetime.now().strftime("%Y/%m/%d %H:%M"),
            "page_creation_minutes": page_creation_minutes,
        })
    except Exception as e:
        print(f"[スプレッドシート出品記録エラー] {e}")

    # Monday.comステータスを「出品中」に更新
    try:
        shuppinon_jikan = 0
        if session.get("start_time"):
            shuppinon_jikan = max(0, int((datetime.now() - session["start_time"]).total_seconds() / 60))
        monday_cols = {
            "status": {"label": "出品待ち"},
            "shuppinon_tantosha": get_staff_code(user_id),
            "shuppinon_date": {"date": datetime.now().strftime("%Y-%m-%d")},
            "location": location,
        }
        if session.get("start_price"):
            monday_cols["kaishi_kakaku"] = session["start_price"]
        if shuppinon_jikan > 0:
            monday_cols["shuppinon_jikan"] = shuppinon_jikan
        update_monday_columns(management_number, monday_cols)
    except Exception as e:
        print(f"[Monday.com出品中更新エラー] {e}")

    # TODO: ヤフオク自動出品（オークタウンAPI確認後に実装予定）
    start = session.get("start_price", 0)
    post_to_slack(channel_id, thread_ts,
        "━━━━━━━━━━━━━━━━\n"
        "✅ *出品登録完了*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{management_number}*\n\n"
        f"📍 保管場所\n"
        f"　*{location}*\n\n"
        f"📋 タイトル\n"
        f"　{session.get('title', '')}\n\n"
        f"💰 開始価格\n"
        f"　¥{start:,}\n\n"
        "🔜 ヤフオクAPI連携は4/1以降に追加予定です",
        mention_user=user_id, bot_role="shuppinon")


def handle_shuppinon_channel(event: dict) -> None:
    """出品チャンネルのイベントを処理する"""
    import re
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_id = event.get("user", "")
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]
    text = normalize_keyword(event.get("text", ""))
    is_new_post = not event.get("thread_ts")

    # ── 新規投稿（テプラ写真 or テキストで管理番号）──────
    if is_new_post:
        import re as _re
        text_mn = _re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
        if not image_urls and not text_mn:
            print(f"[出品CH無視] 管理番号なし・画像なし channel={channel_id} text={text[:30]!r}")
            return
        if text_mn and not image_urls:
            management_number = text_mn.group(0)
        elif image_urls:
            post_to_slack(channel_id, current_ts,
                "🔍 管理番号を読み取り中...", mention_user=user_id, bot_role="shuppinon")
            management_number = extract_management_number_from_image(image_urls[0])
        if not management_number:
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *読み取りエラー*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "管理番号を確認できませんでした。\n\n"
                "もう一度管理番号を送信してください。",
                bot_role="shuppinon")
            return

        # Monday.comからデータ取得
        item_data = get_item_from_monday(management_number)
        if not item_data:
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *該当なし*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"*{management_number}* は確認できません。\n\n"
                "管理番号を確認して再送信してください。",
                bot_role="shuppinon")
            return

        # 担当マーク判定（タイトル先頭に付与）
        staff_name = get_staff_code(user_id)
        staff_mark = STAFF_LISTING_MARKS.get(staff_name, "")
        mark_prefix = f"{staff_mark} " if staff_mark else ""
        max_title_len = 65 - len(mark_prefix)

        # Claudeで出品コンテンツ生成
        post_to_slack(channel_id, current_ts, "⏳ 出品データを生成中...", bot_role="shuppinon")
        listing = generate_listing_content(management_number, item_data, max_title_len=max_title_len)

        # 梱包サイズを内部KWから推定（例: /S80/ → 80）
        kw = item_data.get("internal_keyword", "")
        size_m = re.search(r'/[A-Z]+(\d+)/', kw)
        size = size_m.group(1) if size_m else ""

        # タイトルにマークを付与
        raw_title = listing.get("title", management_number)
        title_with_mark = mark_prefix + raw_title[:max_title_len]

        session = {
            "management_number": management_number,
            "title":       title_with_mark,
            "description": listing.get("description", ""),
            "condition":   item_data.get("condition", ""),
            "start_price": listing.get("start_price", 0),
            "buyout_price": listing.get("buyout_price", 0),
            "size":        size,
            "item_data":   item_data,
            "start_time":  datetime.now(),
            "page_created": False,
            "page_created_time": None,
        }
        listing_sessions[current_ts] = session
        post_listing_summary(channel_id, current_ts, session, mention_user=user_id)

        # 商品画像をDriveから取得して表示
        _post_image_list(channel_id, current_ts, management_number)
        return

    # ── スレッド内（修正コマンド or ロケーション番号）──
    # 削除確認待ちの処理
    if handle_delete_step2(channel_id, thread_ts, user_id, text):
        return

    session = listing_sessions.get(thread_ts)
    if not session:
        # 削除コマンド（セッションなし）
        if text == "削除":
            handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["shuppinon"], "shuppinon")
        else:
            print(f"[出品CH無視] スレッド内・セッションなし channel={channel_id} text={text[:30]!r}")
        return

    management_number = session["management_number"]

    # ── 画像管理コマンド ──
    # 画像一覧表示
    if text == "画像":
        _post_image_list(channel_id, thread_ts, management_number)
        return

    # 画像削除（例: 画像削除 3）
    m_del = re.match(r'^画像削除\s*(\d+)$', text)
    if m_del:
        idx = int(m_del.group(1))
        images = list_drive_images(management_number)
        if 1 <= idx <= len(images):
            target = images[idx - 1]
            if delete_drive_file(target["id"]):
                post_to_slack(channel_id, thread_ts,
                    f"🗑️ {idx}枚目（{target['name']}）を削除しました。",
                    bot_role="shuppinon")
                _post_image_list(channel_id, thread_ts, management_number)
            else:
                post_to_slack(channel_id, thread_ts, "⚠️ 削除に失敗しました。", bot_role="shuppinon")
        else:
            post_to_slack(channel_id, thread_ts, f"⚠️ {idx}枚目は存在しません。", bot_role="shuppinon")
        return

    # 順番入替（例: 順番入替 2 4）
    m_swap = re.match(r'^順番入替\s*(\d+)\s+(\d+)$', text)
    if m_swap:
        a, b = int(m_swap.group(1)), int(m_swap.group(2))
        images = list_drive_images(management_number)
        if 1 <= a <= len(images) and 1 <= b <= len(images) and a != b:
            service = get_drive_service()
            if service:
                try:
                    name_a = images[a - 1]["name"]
                    name_b = images[b - 1]["name"]
                    service.files().update(fileId=images[a - 1]["id"], body={"name": name_b}, supportsAllDrives=True).execute()
                    service.files().update(fileId=images[b - 1]["id"], body={"name": name_a}, supportsAllDrives=True).execute()
                    post_to_slack(channel_id, thread_ts,
                        f"🔄 {a}枚目と{b}枚目を入れ替えました。",
                        bot_role="shuppinon")
                    _post_image_list(channel_id, thread_ts, management_number)
                except Exception as e:
                    print(f"[出品CH] 順番入替エラー: {e}")
                    post_to_slack(channel_id, thread_ts, "⚠️ 入れ替えに失敗しました。", bot_role="shuppinon")
        else:
            post_to_slack(channel_id, thread_ts, "⚠️ 番号が正しくありません。", bot_role="shuppinon")
        return

    # 撮り直し（例: 撮り直し 2 + 写真投稿）
    m_replace = re.match(r'^撮り直し\s*(\d+)$', text)
    if m_replace and image_urls:
        idx = int(m_replace.group(1))
        images = list_drive_images(management_number)
        if 1 <= idx <= len(images):
            if replace_drive_file(images[idx - 1]["id"], image_urls[0]):
                post_to_slack(channel_id, thread_ts,
                    f"📷 {idx}枚目を差し替えました。",
                    bot_role="shuppinon")
                _post_image_list(channel_id, thread_ts, management_number)
            else:
                post_to_slack(channel_id, thread_ts, "⚠️ 差し替えに失敗しました。", bot_role="shuppinon")
        else:
            post_to_slack(channel_id, thread_ts, f"⚠️ {idx}枚目は存在しません。", bot_role="shuppinon")
        return

    # 撮影（追加撮影。「撮影」+ 写真投稿）
    if text == "撮影" and image_urls:
        uploaded = upload_shuppinon_image(management_number, image_urls)
        if uploaded:
            post_to_slack(channel_id, thread_ts,
                f"📷 {len(uploaded)}枚を追加しました。",
                bot_role="shuppinon")
            _post_image_list(channel_id, thread_ts, management_number)
        else:
            post_to_slack(channel_id, thread_ts, "⚠️ アップロードに失敗しました。", bot_role="shuppinon")
        return

    # スレッド内で写真だけ投稿（テキストなし）→ 追加撮影として扱う
    if not text and image_urls:
        uploaded = upload_shuppinon_image(management_number, image_urls)
        if uploaded:
            post_to_slack(channel_id, thread_ts,
                f"📷 {len(uploaded)}枚を追加しました。",
                bot_role="shuppinon")
            _post_image_list(channel_id, thread_ts, management_number)
        return

    # キャンセル・中断
    if text in CANCEL_WORDS:
        log_work_activity(CHANNEL_NAMES["shuppinon"], management_number,
                          get_staff_code(user_id), "キャンセル", session.get("start_time"))
        del listing_sessions[thread_ts]
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "⏹️ *出品作業キャンセル*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号\n"
            f"　*{management_number}*\n\n"
            "出品作業をキャンセルしました。",
            mention_user=user_id, bot_role="shuppinon")
        return

    # 削除コマンド
    if text == "削除":
        handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["shuppinon"], "shuppinon")
        return

    # ページ作成完了コマンド
    if text == "ページ作成完了":
        session["page_created"] = True
        session["page_created_time"] = datetime.now()
        listing_sessions[thread_ts] = session
        try:
            update_monday_columns(management_number, {
                "status": {"label": "ページ作成完了"},
            })
        except Exception as e:
            print(f"[Monday.comページ作成完了更新エラー] {e}")
        try:
            send_to_spreadsheet({
                "action":      "shuppinon_page_complete",
                "kanri_bango": management_number,
                "staff_id":    get_staff_code(user_id),
                "timestamp":   datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        except Exception as e:
            print(f"[スプレッドシートページ作成完了エラー] {e}")
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "🖥️ *ページ作成完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号\n"
            f"　*{management_number}*\n\n"
            "ページ作成を記録しました！\n\n"
            "次は商品を棚に収納して\n"
            "ロケーション番号を入力してください。\n"
            "例：`A-12`",
            mention_user=user_id, bot_role="shuppinon")
        return

    # 修正コマンドの判定
    field, value = parse_listing_command(text)
    if field:
        if field == "start_price":
            try:
                session["start_price"] = int(re.sub(r'[^\d]', '', value))
            except Exception:
                pass
        elif field == "buyout_price":
            try:
                session["buyout_price"] = int(re.sub(r'[^\d]', '', value))
            except Exception:
                pass
        else:
            session[field] = value
        listing_sessions[thread_ts] = session
        post_listing_summary(channel_id, thread_ts, session, mention_user=user_id)
        return

    # ロケーション番号（バリデーション付き）→ 出品確定
    if text:
        if LOCATION_PATTERN.match(text):
            execute_listing(session, text, channel_id, thread_ts, user_id)
            log_work_activity(CHANNEL_NAMES["shuppinon"], session["management_number"],
                              get_staff_code(user_id), "完了", session.get("start_time"))
            del listing_sessions[thread_ts]
        else:
            post_to_slack(channel_id, thread_ts,
                "⚠️ ロケーション番号の形式を確認してください。\n\n"
                "入力例：\n"
                "　`A-12`　`B3`　`倉庫外`　`2階`\n\n"
                "修正コマンド：\n"
                "　`タイトル：` `開始価格：` `説明文：` `サイズ：`",
                bot_role="shuppinon")


# ── 梱包出荷チャンネル（黒田官兵衛）────────────────────

konpo_sessions = {}

CARRIER_MAP = {
    "1": "佐川急便",
    "2": "アートデリバリー",
    "3": "西濃運輸",
    "4": "直接引き取り",
    "5": "後日発送",
}

CARRIER_MENU = (
    "運送会社を番号で選んでください：\n"
    "1️⃣ 佐川急便\n"
    "2️⃣ アートデリバリー\n"
    "3️⃣ 西濃運輸\n"
    "4️⃣ 直接引き取り\n"
    "5️⃣ 後日発送"
)


def extract_tracking_number_from_image(image_url: str, carrier: str) -> str:
    """送り状ラベル写真から追跡番号をOCR抽出する"""
    import httpx as _httpx, base64 as _b64
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    try:
        resp = _httpx.get(image_url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        image_b64 = _b64.standard_b64encode(resp.content).decode()
    except Exception as e:
        print(f"[送り状画像取得エラー] {e}")
        return ""
    try:
        _client = get_anthropic_client()
        if not _client:
            return ""
        result = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": (
                        f"この{carrier}の送り状ラベルから追跡番号（伝票番号）のみを抽出してください。"
                        "数字のみで答えてください。見つからない場合は「なし」と答えてください。"
                    )},
                ],
            }],
        )
        answer = result.content[0].text.strip()
        return "" if answer == "なし" else answer
    except Exception as e:
        print(f"[追跡番号OCRエラー] {e}")
        return ""


def _notify_tsuhan_community(management_number: str, item_name: str,
                             carrier: str, tracking_number: str,
                             monday_board_id: str) -> None:
    """通販業務_共有コミュニティに出荷完了通知を投稿してピン留めする"""
    token = get_slack_token()
    if not token:
        return
    board_url = f"https://monday.com/boards/{monday_board_id}"
    tracking_line = f"\n📮 追跡番号：*{tracking_number}*" if tracking_number else ""
    text = (
        "<!channel>\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🚚 *出荷手配が完了しました*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号：*{management_number}*\n"
        f"📋 アイテム名：{item_name}\n"
        f"🏢 運送会社：{carrier}"
        f"{tracking_line}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "✅ *対応をお願いします*\n"
        f"Monday.com のステータスを *「出荷待ち」* に変更してください。\n\n"
        f"<{board_url}|📋 Monday.com ボードを開く>\n"
        "━━━━━━━━━━━━━━━━"
    )
    try:
        url = "https://slack.com/api/chat.postMessage"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        resp = httpx.post(url, headers=headers, json={
            "channel": TSUHAN_COMMUNITY_CHANNEL_ID,
            "text": text,
            "username": "黒田官兵衛",
        }, timeout=10)
        result = resp.json()
        if not result.get("ok"):
            print(f"[通販コミュニティ通知エラー] {result.get('error')}")
            return
        # ピン留め
        msg_ts = result.get("ts")
        if msg_ts:
            httpx.post("https://slack.com/api/pins.add", headers=headers, json={
                "channel": TSUHAN_COMMUNITY_CHANNEL_ID,
                "timestamp": msg_ts,
            }, timeout=10)
            print(f"[通販コミュニティ通知] 投稿・ピン留め完了 ts={msg_ts}")
    except Exception as e:
        print(f"[通販コミュニティ通知例外] {e}")


def _finish_shipping(channel_id, thread_ts, user_id, management_number, carrier, tracking_number,
                     is_old_board: bool = False, monday_board_id: str = "", item_name: str = ""):
    """出荷手配完了の共通処理"""
    tracking_line = f"\n📮 追跡番号\n　*{tracking_number}*" if tracking_number else ""
    post_to_slack(channel_id, thread_ts,
        "━━━━━━━━━━━━━━━━\n"
        "🚚 *出荷手配完了*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{management_number}*\n\n"
        f"🏢 運送会社\n"
        f"　{carrier}"
        f"{tracking_line}",
        mention_user=user_id, bot_role="konpo")
    if is_old_board:
        # 旧ボード品はMonday.com列構造が異なるため更新をスキップ → 通販コミュニティに通知
        print(f"[旧ボード品] Monday.com更新スキップ: {management_number}")
        _notify_tsuhan_community(management_number, item_name, carrier, tracking_number,
                                 monday_board_id or MONDAY_BOARD_ID)
    else:
        try:
            monday_cols = {
                "status": {"label": "出荷待ち"},
                "carrier": carrier,
                "shukka_date": {"date": datetime.now().strftime("%Y-%m-%d")},
            }
            if tracking_number:
                monday_cols["tracking_number"] = tracking_number
            update_monday_columns(management_number, monday_cols)
        except Exception as e:
            print(f"[Monday.com出荷済み更新エラー] {e}")
    try:
        send_to_spreadsheet({
            "action":          "shipping_update",
            "kanri_bango":     management_number,
            "carrier":         carrier,
            "tracking_number": tracking_number,
            "staff_id":        get_staff_code(user_id),
            "timestamp":       datetime.now().strftime("%Y/%m/%d %H:%M"),
        })
    except Exception as e:
        print(f"[スプレッドシート出荷更新エラー] {e}")


def handle_konpo_channel(event: dict) -> None:
    """梱包出荷チャンネルのイベントを処理する"""
    import re as _re
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_id = event.get("user", "")
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]
    text = normalize_keyword(event.get("text", ""))
    is_new_post = not event.get("thread_ts")

    # ── 新規投稿 ──────────────────────────────────────────
    if is_new_post:
        # 後日発送の送り状後入力: 「管理番号 運送会社 追跡番号」
        delayed_m = _re.match(r'(\d{4}(?:[VGME]\d{4}|-\d{4}|[A-Z]{2}\d{3}))\s+(佐川|アート|西濃)\S*\s+(\S+)', text)
        if delayed_m:
            mn, carrier_kw, tracking = delayed_m.group(1), delayed_m.group(2), delayed_m.group(3)
            carrier_name = {"佐川": "佐川急便", "アート": "アートデリバリー", "西濃": "西濃運輸"}.get(carrier_kw, carrier_kw)
            delayed_item = get_item_from_monday(mn)
            _finish_shipping(channel_id, current_ts, user_id, mn, carrier_name, tracking,
                             is_old_board=delayed_item.get("is_old_board", False),
                             monday_board_id=delayed_item.get("monday_board_id", ""),
                             item_name=delayed_item.get("monday_name", ""))
            return

        # 通常の梱包開始
        text_mn = _re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4}|[A-Z]{2}\d{3})', text)
        if not text_mn and not image_urls:
            print(f"[梱包CH無視] 管理番号なし・画像なし channel={channel_id} text={text[:30]!r}")
            return
        if text_mn:
            management_number = text_mn.group(0)
        else:
            post_to_slack(channel_id, current_ts, "🔍 管理番号を読み取り中...", bot_role="konpo")
            management_number = extract_management_number_from_image(image_urls[0])
            if not management_number:
                post_to_slack(channel_id, current_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "⚠️ *読み取りエラー*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    "管理番号を確認できませんでした。\n\n"
                    "もう一度送信してください。",
                    bot_role="konpo")
                return

        item_data = get_item_from_monday(management_number)
        if not item_data:
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *該当なし*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"*{management_number}* は確認できません。\n\n"
                "管理番号を確認して再送信してください。",
                bot_role="konpo")
            return

        kw = item_data.get("internal_keyword", "")
        size_m = _re.search(r'/[A-Z]+(\d+)/', kw)
        size = size_m.group(1) if size_m else "不明"
        is_old_board = item_data.get("is_old_board", False)

        konpo_sessions[current_ts] = {
            "management_number": management_number,
            "item_name":         item_data.get("monday_name", ""),
            "size":              size,
            "packed":            False,
            "carrier":           None,
            "waiting_label":     False,
            "start_time":        datetime.now(),
            "is_old_board":      is_old_board,
            "monday_board_id":   item_data.get("monday_board_id", MONDAY_BOARD_ID),
        }

        if is_old_board:
            # 旧ボード品：アイテム名と棚番を表示（サイズ・チャンネル等は手動確認）
            shelf = ""
            for col_id, col_val in item_data.items():
                if col_id in ("monday_name", "is_old_board", "monday_item_id", "monday_board_id") or not col_val:
                    continue
                if col_val == management_number:
                    continue
                import re as _re
                if _re.match(r'^([A-Z][A-Za-z\d\s横奥]*|\d{1,2}[階F]?|倉庫[外奥]?)$', col_val.strip()):
                    shelf = col_val
                    break
            shelf_line = f"📍 棚番\n　{shelf}\n\n" if shelf else ""
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "📦 *梱包情報確認（旧ボード）*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"🔖 管理番号\n"
                f"　*{management_number}*\n\n"
                f"📋 アイテム名\n"
                f"　{item_data.get('monday_name', '---')}\n\n"
                f"{shelf_line}"
                "⚠️ サイズ・チャンネル等は手動で確認してください。\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "梱包が完了したら `梱包完了` と入力してください。",
                mention_user=user_id, bot_role="konpo")
        else:
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "📦 *梱包情報確認*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"🔖 管理番号\n"
                f"　*{management_number}*\n\n"
                f"📐 梱包サイズ\n"
                f"　{size}サイズ\n\n"
                f"📺 判定チャンネル\n"
                f"　{item_data.get('hantei_channel', '')}\n\n"
                f"💰 予想販売価格\n"
                f"　{item_data.get('yosou_kakaku', '')}\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "梱包が完了したら `梱包完了` と入力してください。",
                mention_user=user_id, bot_role="konpo")
        return

    # ── スレッド内 ────────────────────────────────────────
    # 削除確認待ちの処理
    if handle_delete_step2(channel_id, thread_ts, user_id, text):
        return

    session = konpo_sessions.get(thread_ts)
    if not session:
        # 削除コマンド（セッションなし）
        if text == "削除":
            handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["konpo"], "konpo")
        else:
            print(f"[梱包CH無視] スレッド内・セッションなし channel={channel_id} text={text[:30]!r}")
        return
    management_number = session["management_number"]

    # キャンセル・中断
    if text in CANCEL_WORDS:
        log_work_activity(CHANNEL_NAMES["konpo"], management_number,
                          get_staff_code(user_id), "キャンセル", session.get("start_time"))
        del konpo_sessions[thread_ts]
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "⏹️ *梱包作業キャンセル*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号\n"
            f"　*{management_number}*\n\n"
            "梱包作業をキャンセルしました。",
            mention_user=user_id, bot_role="konpo")
        return

    # 削除コマンド
    if text == "削除":
        handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["konpo"], "konpo")
        return

    # ① 梱包完了 → 運送会社選択へ
    if text in ("梱包完了", "梱包") and not session["packed"]:
        session["packed"] = True
        konpo_sessions[thread_ts] = session
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "✅ *梱包完了確認*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"{CARRIER_MENU}",
            mention_user=user_id, bot_role="konpo")
        try:
            update_monday_columns(management_number, {
                "status": {"label": "梱包作業"},
                "konpo_tantosha": get_staff_code(user_id),
                "konpo_date": {"date": datetime.now().strftime("%Y-%m-%d")},
            })
        except Exception as e:
            print(f"[Monday.com梱包済み更新エラー] {e}")
        return

    # ② 運送会社選択（1〜5）
    if session["packed"] and not session["carrier"] and text in CARRIER_MAP:
        carrier = CARRIER_MAP[text]
        session["carrier"] = carrier
        konpo_sessions[thread_ts] = session

        if text == "4":  # 直接引き取り
            _finish_shipping(channel_id, thread_ts, user_id, management_number, carrier, "",
                             is_old_board=session.get("is_old_board", False),
                             monday_board_id=session.get("monday_board_id", ""),
                             item_name=session.get("item_name", ""))
            log_work_activity(CHANNEL_NAMES["konpo"], management_number,
                              get_staff_code(user_id), "完了", session.get("start_time"))
            del konpo_sessions[thread_ts]
        elif text == "5":  # 後日発送
            post_to_slack(channel_id, thread_ts,
                f"📋 *{management_number}* を「梱包済み（発送待ち）」として保留しました。\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📮 *後日発送時の入力方法*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "このチャンネルに新規メッセージで投稿してください。\n\n"
                "*入力形式*\n"
                "`管理番号 運送会社 伝票番号`\n\n"
                "*サンプル*\n"
                "```\n"
                "2603-0001 佐川 123456789012\n"
                "2603-0002 アート 0987654321\n"
                "2603-0003 西濃 111222333444\n"
                "```\n\n"
                "*運送会社の入力方法*\n"
                "• 佐川急便 → `佐川`\n"
                "• アートデリバリー → `アート`\n"
                "• 西濃運輸 → `西濃`\n\n"
                "⚠️ *注意事項*\n"
                "• スペースで区切ってください（全角スペース不可）\n"
                "• 伝票番号は数字のみ（ハイフン不要）\n"
                "• 管理番号・運送会社・伝票番号の順番を守ってください",
                mention_user=user_id, bot_role="konpo")
            del konpo_sessions[thread_ts]
        else:  # 佐川・アート・西濃
            session["waiting_label"] = True
            konpo_sessions[thread_ts] = session
            post_to_slack(channel_id, thread_ts,
                f"📸 *{carrier}* の\n"
                "送り状ラベルの写真を送ってください。",
                mention_user=user_id, bot_role="konpo")
        return

    # ③ 送り状ラベル写真 → OCRで追跡番号抽出
    if session.get("waiting_label") and image_urls:
        carrier = session["carrier"]
        post_to_slack(channel_id, thread_ts, "🔍 追跡番号を読み取り中...", bot_role="konpo")
        tracking_number = extract_tracking_number_from_image(image_urls[0], carrier)
        if not tracking_number:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *読み取りエラー*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "追跡番号を読み取れませんでした。\n\n"
                "もう一度写真を送ってください。",
                mention_user=user_id, bot_role="konpo")
            return
        _finish_shipping(channel_id, thread_ts, user_id, management_number, carrier, tracking_number,
                         is_old_board=session.get("is_old_board", False),
                         monday_board_id=session.get("monday_board_id", ""),
                         item_name=session.get("item_name", ""))
        log_work_activity(CHANNEL_NAMES["konpo"], management_number,
                          get_staff_code(user_id), "完了", session.get("start_time"))
        del konpo_sessions[thread_ts]


# ── 現場査定チャンネル（渋沢栄一）────────────────────────

# 古物台帳フローのセッション管理
# key: "{channel_id}_{user_id}"
# value: {"step": 1〜3, "price": int, "item_name": str, "staff_id": str, "timestamp": str, "id_info": dict}
kaitori_sessions = {}


def _extract_id_info(image_url: str) -> dict:
    """身分証の写真からClaudeで情報を抽出する（古物台帳記載用）"""
    img_data, img_type = fetch_image_as_base64(image_url)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    import anthropic as _anthropic
    import json as _json
    import re as _re
    client = _anthropic.Anthropic(api_key=anthropic_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="""身分証明書の画像から以下の情報をJSON形式のみで返してください。
読み取れない項目は「読取不可」としてください。
{
  "doc_type": "運転免許証 または マイナンバーカード または パスポート",
  "name": "氏名（姓名）",
  "address": "住所",
  "birthdate": "生年月日（YYYY/MM/DD形式）",
  "id_number": "証明書番号（免許証番号など）"
}
※マイナンバー（12桁の個人番号）は絶対に記録しないこと。
※JSON以外のテキストは一切出力しないこと。""",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "この身分証から情報を抽出してください。"},
                {"type": "image", "source": {"type": "base64", "media_type": img_type, "data": img_data}},
            ]
        }],
    )
    text = response.content[0].text
    m = _re.search(r'\{.*\}', text, _re.DOTALL)
    if m:
        try:
            return _json.loads(m.group(0))
        except Exception:
            pass
    return {}


def _handle_kaitori_flow(event: dict, channel_id: str, current_ts: str,
                         user_id: str, text: str, image_urls: list) -> bool:
    """古物台帳フロー（買取確定〜身分証確認〜台帳登録）を処理する。
    フロー処理した場合はTrueを返す。"""
    import re as _re
    session_key = f"{channel_id}_{user_id}"

    # ── Step 0: 「買取確定 ¥3000」でフロー開始 ──
    m = _re.search(r'買取確定\s*[¥￥]?\s*([\d,]+)', text or "")
    if m:
        price = int(m.group(1).replace(",", ""))
        kaitori_sessions[session_key] = {
            "step": 1,
            "price": price,
            "staff_id": get_staff_code(user_id),
            "timestamp": datetime.now().strftime("%Y/%m/%d %H:%M"),
        }
        post_to_slack(channel_id, current_ts,
            f"買取価格 *¥{price:,}* で記録いたします。\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📦 *品物の名称・特徴を教えてください*\n\n"
            "記入例：\n"
            "`パナソニック 洗濯機 NA-F60B14 中古 動作OK`\n"
            "━━━━━━━━━━━━━━━━",
            mention_user=user_id, bot_role="genba")
        return True

    # セッションがなければフロー外
    session = kaitori_sessions.get(session_key)
    if not session:
        return False

    step = session["step"]

    # ── Step 1: 品物名を受け取る ──
    if step == 1 and text and not image_urls:
        session["item_name"] = text
        session["step"] = 2
        kaitori_sessions[session_key] = session
        post_to_slack(channel_id, current_ts,
            "承りました。\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "🪪 *相手方の身分証をお送りください*\n\n"
            "古物営業法に基づく確認が必要です。\n\n"
            "📸 対応書類：\n"
            "　・運転免許証\n"
            "　・マイナンバーカード（番号面は不要）\n"
            "　・パスポート\n\n"
            "氏名・住所・生年月日・証明書番号が\n"
            "確認できる面の写真を送信してください。\n"
            "━━━━━━━━━━━━━━━━",
            mention_user=user_id, bot_role="genba")
        return True

    # ── Step 2: 身分証写真を受け取る ──
    if step == 2 and image_urls:
        post_to_slack(channel_id, current_ts,
            "🔍 身分証の情報を読み取っております...\n"
            "しばしお待ちを。",
            bot_role="genba")
        try:
            id_info = _extract_id_info(image_urls[0])
            session["id_info"] = id_info
            session["step"] = 3
            kaitori_sessions[session_key] = session
            post_to_slack(channel_id, current_ts,
                "読み取り完了でございます。\n"
                "内容をご確認ください。\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "📋 *古物台帳　記載内容確認*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"📅 取引日時　：{session['timestamp']}\n\n"
                f"📦 品　　物　：{session['item_name']}\n\n"
                f"💴 買取価格　：¥{session['price']:,}\n\n"
                f"👤 氏　　名　：{id_info.get('name', '読取不可')}\n\n"
                f"🏠 住　　所　：{id_info.get('address', '読取不可')}\n\n"
                f"🎂 生年月日　：{id_info.get('birthdate', '読取不可')}\n\n"
                f"🪪 証明書番号：{id_info.get('id_number', '読取不可')}\n\n"
                f"📋 確認書類　：{id_info.get('doc_type', '運転免許証')}\n\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "✅ 正しければ `登録` と送信してください。\n"
                "✏️ 修正がある場合は\n"
                "　`修正 氏名：正しい名前`\n"
                "　のように送信してください。",
                mention_user=user_id, bot_role="genba")
        except Exception as e:
            print(f"[身分証読取エラー] {e}")
            post_to_slack(channel_id, current_ts,
                "⚠️ 身分証の読み取りに失敗いたしました。\n\n"
                "もう一度、鮮明な写真をお送りください。",
                mention_user=user_id, bot_role="genba")
        return True

    # ── Step 3: 登録確認 or 修正 ──
    if step == 3:
        n = normalize_keyword(text or "")
        if n == "登録":
            id_info = session.get("id_info", {})
            try:
                send_to_spreadsheet({
                    "action":     "kobutsu_daichou",
                    "timestamp":  session["timestamp"],
                    "item_name":  session["item_name"],
                    "price":      session["price"],
                    "staff_id":   session["staff_id"],
                    "name":       id_info.get("name", ""),
                    "address":    id_info.get("address", ""),
                    "birthdate":  id_info.get("birthdate", ""),
                    "id_number":  id_info.get("id_number", ""),
                    "doc_type":   id_info.get("doc_type", "運転免許証"),
                })
                del kaitori_sessions[session_key]
                post_to_slack(channel_id, current_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "✅ *古物台帳への記録が完了いたしました*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    "道徳と算盤、両面から\n"
                    "適切な取引でありました。\n\n"
                    "スプレッドシートの\n"
                    "「古物台帳」シートをご確認ください。",
                    mention_user=user_id, bot_role="genba")
            except Exception as e:
                post_to_slack(channel_id, current_ts,
                    f"⚠️ 記録に失敗いたしました。\n\n"
                    f"もう一度 `登録` と送信してください。\n"
                    f"エラー：{e}",
                    mention_user=user_id, bot_role="genba")
            return True

        # 修正コマンド処理
        import re as _re2
        fix = _re2.match(r'修正\s+(.+?)[:：](.+)', text or "")
        if fix:
            field_name = fix.group(1).strip()
            new_value = fix.group(2).strip()
            field_map = {
                "氏名": "name", "住所": "address",
                "生年月日": "birthdate", "証明書番号": "id_number",
                "確認書類": "doc_type",
            }
            field_key = field_map.get(field_name)
            if field_key:
                session["id_info"][field_key] = new_value
                kaitori_sessions[session_key] = session
            id_info = session["id_info"]
            post_to_slack(channel_id, current_ts,
                f"✏️ *{field_name}* を修正しました。\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "📋 *修正後の内容*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"👤 氏　　名　：{id_info.get('name', '読取不可')}\n\n"
                f"🏠 住　　所　：{id_info.get('address', '読取不可')}\n\n"
                f"🎂 生年月日　：{id_info.get('birthdate', '読取不可')}\n\n"
                f"🪪 証明書番号：{id_info.get('id_number', '読取不可')}\n\n"
                f"📋 確認書類　：{id_info.get('doc_type', '運転免許証')}\n\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "✅ 正しければ `登録` と送信してください。",
                mention_user=user_id, bot_role="genba")
            return True

    return False

GENBA_SYSTEM_PROMPT = """あなたは「渋沢栄一」。道徳と経済の両立を説いた「論語と算盤」の著者。
現場で出会う品物・廃棄物・情報を算盤（そろばん）で正しく評価し、最適な判断を下す。
語り口は丁寧で温かく、「〜であります」「算盤に合う」「これは有望でありましょう」などを自然に使う。
AIらしい無機質な言い回しは避けること。

【あなたの役割】
スタッフが現場で出会ったものをSlackに投稿すると、以下の3つのいずれかで応答する。

━━━━━━━━━━━━━━━━
【判定タイプ1】買取査定
スタッフが商品・品物の写真やテキストを送った場合

【チャンネル別目標粗利率】
- eBayシングル（海外専用・競合少）: 8%
- ヤフオクヴィンテージ（古道具・希少品）: 8%
- ヤフオク現行（中古品一般）: 25%
- eBayまとめ: 20%
- ヤフオクまとめ・ロット販売: タダ引き推奨（0円）
- スクラップ・廃棄: 買取不可（処分費が発生）

【買取上限価格の計算式】
買取上限 = 予想売値 × (1 - 粗利率) - 送料概算 - 出品手数料 - 保管コスト概算

【プラットフォーム手数料】
- ヤフオク: 落札額の10%
- eBay: 落札額の約13%

【出力フォーマット（買取査定）】
━━━━━━━━━━━━━━━━
🏷️ 買取査定
━━━━━━━━━━━━━━━━
📦 品物：[商品名]
📊 状態：[状態]
🎯 推奨売先：[チャンネル名]

💰 予想売値：¥[下限]〜¥[上限]
📊 コスト内訳
　└ 送料概算：¥[数値]
　└ 出品手数料：¥[数値]
　└ 保管コスト：¥[数値]
　└ 合計コスト：¥[数値]

💴 *買取上限価格：¥[数値]*
🤝 交渉推奨価格：¥[数値]（上限より少し余裕を持たせた価格）

📝 根拠：[簡潔な説明]
⚠️ 注意：[特記事項があれば。なければ省略]
━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━
【判定タイプ2】廃棄・処分判断
「廃棄」「処分」「捨てる」「どうする」などのキーワードがある場合、または明らかに廃棄物の場合

【出力フォーマット（廃棄判断）】
━━━━━━━━━━━━━━━━
♻️ 廃棄・処分判断
━━━━━━━━━━━━━━━━
📦 品物：[品物名]
🔍 推奨処分方法：[方法]
💰 処分コスト概算：¥[下限]〜¥[上限]

📋 手順：
1. [手順1]
2. [手順2]

⚠️ 注意：[法的・安全上の注意事項]
━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━
【判定タイプ3】知識・情報のインプット
「メモ」「情報」「覚えておいて」「相場」「業者」などのキーワードがある場合

この場合は内容を要約して「承りました」と返すだけでよい。
━━━━━━━━━━━━━━━━

【あなたの人格補足】
算盤が合わない（採算が取れない）買取は正直に「これは算盤に合いませぬ」と伝える。
廃棄物でも資源価値がある場合は必ず指摘する。
スタッフの判断を支え、現場で素早く動けるよう簡潔に答える。"""


def handle_genba_channel(event: dict) -> None:
    """現場査定チャンネル（渋沢の算盤_現場の力）のイベントを処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_id = event.get("user", "")
    text = event.get("text", "")
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]

    # テキストも画像もない場合はスキップ
    if not text and not image_urls:
        return

    # ── 古物台帳フローを最優先で処理 ──
    if _handle_kaitori_flow(event, channel_id, current_ts, user_id, text, image_urls):
        return

    # 知識インプット判定（「メモ」「情報」「覚えておいて」「相場」などのキーワード）
    memo_keywords = ["メモ", "情報", "覚えておいて", "相場", "業者", "単価", "注意", "ポイント", "コツ"]
    is_memo = any(kw in text for kw in memo_keywords)

    if is_memo and not image_urls:
        # 知識をスプレッドシートに保存
        try:
            send_to_spreadsheet({
                "action":    "genba_memo",
                "staff_id":  get_staff_code(user_id),
                "message":   text,
                "timestamp": datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        except Exception as e:
            print(f"[現場メモ保存エラー] {e}")
        post_to_slack(channel_id, current_ts,
            BOT_PERSONA["genba"]["memo_saved"],
            mention_user=user_id, bot_role="genba")
        return

    # 買取査定 or 廃棄判断 → Claudeに投げる
    post_to_slack(channel_id, current_ts,
        BOT_PERSONA["genba"]["thinking"],
        bot_role="genba")

    try:
        messages = []
        # 画像がある場合は画像を含める
        if image_urls:
            content = []
            if text:
                content.append({"type": "text", "text": text})
            for url in image_urls[:3]:  # 最大3枚
                try:
                    img_data, img_type = fetch_image_as_base64(url)
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": img_type, "data": img_data},
                    })
                except Exception as e:
                    print(f"[画像取得エラー] {e}")
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": text})

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY が設定されていません")
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=anthropic_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=GENBA_SYSTEM_PROMPT,
            messages=messages,
        )
        result_text = response.content[0].text
        post_to_slack(channel_id, current_ts, result_text,
            mention_user=user_id, bot_role="genba")

        # スプレッドシートに査定記録を保存
        try:
            send_to_spreadsheet({
                "action":    "genba_satei",
                "staff_id":  get_staff_code(user_id),
                "input":     text[:200] if text else "（画像のみ）",
                "result":    result_text[:500],
                "timestamp": datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        except Exception as e:
            print(f"[現場査定記録エラー] {e}")

    except Exception as e:
        print(f"[現場査定エラー] {e}")
        post_to_slack(channel_id, current_ts,
            BOT_PERSONA["genba"]["error"],
            mention_user=user_id, bot_role="genba")


# ── ステータス確認チャンネル（松本）────────────────────

def handle_status_channel(event: dict) -> None:
    """ステータス確認チャンネルのイベントを処理する"""
    import re as _re
    from datetime import date
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_id = event.get("user", "")
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]
    text = normalize_keyword(event.get("text", ""))

    text_mn = _re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
    if not text_mn and not image_urls:
        print(f"[ステータスCH無視] 管理番号なし・画像なし channel={channel_id} text={text[:30]!r}")
        return

    if text_mn:
        management_number = text_mn.group(0)
    else:
        management_number = extract_management_number_from_image(image_urls[0])
        if not management_number:
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *読み取りエラー*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "管理番号を確認できませんでした。\n\n"
                "もう一度送信してください。",
                bot_role="status")
            return

    item_data = get_item_from_monday(management_number)
    if not item_data:
        post_to_slack(channel_id, current_ts,
            "━━━━━━━━━━━━━━━━\n"
            "⚠️ *該当なし*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"*{management_number}* は確認できません。\n\n"
            "管理番号を確認して再送信してください。",
            bot_role="status")
        return

    # 登録からの経過日数（管理番号のYYMMから計算）
    days_elapsed = ""
    try:
        yymm = management_number[:4]
        reg_date = date(int("20" + yymm[:2]), int(yymm[2:4]), 1)
        days_elapsed = (date.today() - reg_date).days
    except Exception:
        pass

    status = item_data.get("status", "不明")
    reply = (
        "━━━━━━━━━━━━━━━━\n"
        "📊 *ステータス確認*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{management_number}*\n\n"
        f"📌 現在のステータス\n"
        f"　*{status}*\n\n"
        f"📺 判定チャンネル\n"
        f"　{item_data.get('hantei_channel', '不明')}\n\n"
        f"💰 予想販売価格\n"
        f"　{item_data.get('yosou_kakaku', '不明')}\n\n"
        f"📅 在庫予測期間\n"
        f"　{item_data.get('zaiko_kikan', '不明')}\n\n"
        f"⭐ スコア\n"
        f"　{item_data.get('score', '不明')} 点"
    )
    if days_elapsed:
        reply += f"\n\n🕐 登録からの経過\n　約 {days_elapsed} 日"
    reply += "\n━━━━━━━━━━━━━━━━"

    post_to_slack(channel_id, current_ts, reply, mention_user=user_id, bot_role="status")


# ── 出退勤チャンネル ──────────────────────────────────────

def get_staff_break_minutes(staff_id: str) -> int:
    """スタッフマスターから標準休憩時間（分）を取得する。取得失敗時は60分を返す"""
    try:
        resp = httpx.get(GAS_URL, params={"type": "staff"}, timeout=10)
        data = resp.json()
        if data.get("ok"):
            for row in data.get("data", []):
                if row.get("SlackユーザーID") == staff_id or row.get("名前") == staff_id:
                    val = row.get("標準休憩時間（分）", 60)
                    return int(val) if val else 60
    except Exception as e:
        print(f"[休憩時間取得エラー] {e}")
    return 60


def handle_attendance_channel(event: dict) -> None:
    """出退勤チャンネル - 9:00~16:00 形式で自己申告"""
    import re as _re
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    user_id = event.get("user", "")
    text = normalize_keyword(event.get("text", ""))
    today = datetime.now().strftime("%Y/%m/%d")

    # ── 代筆モード：「北瀬孝 9:00~17:00」形式を検出 ──────────
    # Slackを使えないスタッフの勤怠を別のスタッフが代理入力する
    DAIHITSU_STAFF = ["北瀬孝"]  # 代筆対象スタッフ名リスト
    proxy_name = None
    proxy_match = None
    for name in DAIHITSU_STAFF:
        pm = _re.match(
            rf'{_re.escape(name)}\s+(\d{{1,2}}):(\d{{2}})[~\-～](\d{{1,2}}):(\d{{2}})',
            text
        )
        if pm:
            proxy_name = name
            proxy_match = pm
            break

    if proxy_name and proxy_match:
        # 代筆として処理
        daihitsu_by = get_staff_code(user_id)  # 代筆した人
        sh2, sm2 = int(proxy_match.group(1)), int(proxy_match.group(2))
        eh2, em2 = int(proxy_match.group(3)), int(proxy_match.group(4))
        total2 = (eh2 * 60 + em2) - (sh2 * 60 + sm2)
        if total2 <= 0:
            post_to_slack(channel_id, current_ts,
                "⚠️ 終了時刻が開始時刻より前になっています。確認してください。",
                bot_role="kintaro")
            return
        break2 = get_staff_break_minutes(proxy_name)
        net2 = max(0, total2 - break2) / 60
        try:
            send_to_spreadsheet({
                "action":        "attendance",
                "staff_id":      proxy_name,
                "type":          "勤務申告（代筆）",
                "date":          today,
                "start_time":    f"{sh2:02d}:{sm2:02d}",
                "end_time":      f"{eh2:02d}:{em2:02d}",
                "total_minutes": str(total2),
                "break_minutes": str(break2),
                "net_hours":     f"{net2:.2f}",
                "completed_count": "0",
                "daihitsu_by":   daihitsu_by,
            })
        except Exception as e:
            print(f"[代筆勤務記録エラー] {e}")
        post_to_slack(channel_id, current_ts,
            "━━━━━━━━━━━━━━━━\n"
            "📝 *代筆勤務記録完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"👤 {proxy_name}（代筆：{daihitsu_by}）\n\n"
            f"🕐 勤務時間\n"
            f"　{sh2:02d}:{sm2:02d} 〜 {eh2:02d}:{em2:02d}\n\n"
            f"☕ 休憩　{break2}分\n\n"
            f"⏱️ 実働時間　{net2:.1f}時間\n\n"
            f"記録しました。お疲れさまです。",
            bot_role="kintaro")
        return

    staff_id = get_staff_code(user_id)

    # "9:00~16:00" / "9:00-16:00" / "9:00～16:00" のパース
    m = _re.match(r'(\d{1,2}):(\d{2})[~\-～](\d{1,2}):(\d{2})', text)
    if not m:
        post_to_slack(channel_id, current_ts,
            "入力形式：`9:00~16:00`\n（開始時刻〜終了時刻）\n小さな記録の積み重ねが、大きな実りとなります。",
            bot_role="kintaro")
        return

    sh, sm, eh, em = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    total_minutes = (eh * 60 + em) - (sh * 60 + sm)

    if total_minutes <= 0:
        post_to_slack(channel_id, current_ts,
            "⚠️ 終了時刻が開始時刻より前になっています。\n焦らず、もう一度ご確認ください。",
            bot_role="kintaro")
        return

    # スタッフマスターから標準休憩時間を取得
    break_minutes = get_staff_break_minutes(staff_id)
    net_minutes = max(0, total_minutes - break_minutes)
    net_hours = net_minutes / 60

    # 本日の作業サマリー
    stats = daily_stats.get(staff_id, {"完了": 0, "キャンセル": 0, "削除": 0})
    summary_lines = []
    if stats["完了"] > 0:
        summary_lines.append(f"✅ 完了：{stats['完了']}件")
    if stats["キャンセル"] > 0:
        summary_lines.append(f"⏹️ キャンセル：{stats['キャンセル']}件")
    if stats["削除"] > 0:
        summary_lines.append(f"🗑️ 削除：{stats['削除']}件")
    summary_text = "\n".join(summary_lines) if summary_lines else "本日の作業記録なし"

    try:
        send_to_spreadsheet({
            "action":          "attendance",
            "staff_id":        staff_id,
            "type":            "勤務申告",
            "date":            today,
            "start_time":      f"{sh:02d}:{sm:02d}",
            "end_time":        f"{eh:02d}:{em:02d}",
            "total_minutes":   str(total_minutes),
            "break_minutes":   str(break_minutes),
            "net_hours":       f"{net_hours:.2f}",
            "completed_count": str(stats.get("完了", 0)),
        })
    except Exception as e:
        print(f"[勤務記録エラー] {e}")

    # daily_statsをリセット
    if staff_id in daily_stats:
        del daily_stats[staff_id]

    post_to_slack(channel_id, current_ts,
        "今日もよく働かれました。\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🌙 *勤務記録完了*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"👤 {staff_id}\n\n"
        f"🕐 勤務時間\n"
        f"　{sh:02d}:{sm:02d} 〜 {eh:02d}:{em:02d}\n\n"
        f"☕ 休憩\n"
        f"　{break_minutes}分\n\n"
        f"⏱️ 実働時間\n"
        f"　{net_hours:.1f}時間\n\n"
        "─────────────────\n"
        f"📊 *本日の作業実績*\n\n"
        f"{summary_text}\n\n"
        "積小為大。今日の積み重ねが明日の実りとなります。",
        bot_role="kintaro")
    return


# ── 勤怠連絡チャンネル（サイレント記録）──────────────────

def handle_kintai_channel(event: dict) -> None:
    """勤怠連絡チャンネルのメッセージをスプレッドシートにサイレント記録する"""
    user_id = event.get("user", "")
    text = event.get("text", "")
    if not text:
        return
    staff_id = get_staff_code(user_id)
    try:
        send_to_spreadsheet({
            "action":    "kintai_renraku",
            "staff_id":  staff_id,
            "message":   text,
            "timestamp": datetime.now().strftime("%Y/%m/%d %H:%M"),
        })
    except Exception as e:
        print(f"[勤怠連絡記録エラー] {e}")


@app.route("/debug", methods=["GET"])
def debug():
    """環境変数の設定状況を確認するエンドポイント"""
    return jsonify({
        "ANTHROPIC_API_KEY": "設定済み" if os.environ.get("ANTHROPIC_API_KEY") else "未設定",
        "SLACK_BOT_TOKEN": "設定済み" if os.environ.get("SLACK_BOT_TOKEN") else "未設定",
        "MONDAY_API_TOKEN": "設定済み" if (os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN")) else "未設定",
        "SATSUEI_CHANNEL_ID": os.environ.get("SATSUEI_CHANNEL_ID", "未設定"),
        "SHUPPINON_CHANNEL_ID": os.environ.get("SHUPPINON_CHANNEL_ID", "未設定"),
        "KONPO_CHANNEL_ID": os.environ.get("KONPO_CHANNEL_ID", "未設定"),
        "STATUS_CHANNEL_ID": os.environ.get("STATUS_CHANNEL_ID", "未設定"),
        "ATTENDANCE_CHANNEL_ID": os.environ.get("ATTENDANCE_CHANNEL_ID", "未設定"),
        "GENBA_CHANNEL_ID": os.environ.get("GENBA_CHANNEL_ID", "未設定"),
        "KINTAI_CHANNEL_ID": os.environ.get("KINTAI_CHANNEL_ID", "未設定"),
        "env_keys_count": len(os.environ),
    })


@app.route("/env-keys", methods=["GET"])
def env_keys():
    """全環境変数のキー名一覧を表示（値は非表示）"""
    keys = sorted(os.environ.keys())
    return jsonify({"keys": keys, "count": len(keys)})


@app.route("/monday-setup", methods=["GET"])
def monday_setup():
    """monday.comボードにカラムを作成する（初回のみ実行）"""
    columns = [
        # 既存カラム（作成済みの場合はスキップされる）
        ("管理番号",           "text",    "kanri_bango"),
        ("判定チャンネル",     "text",    "hantei_channel"),
        ("確信度",             "text",    "kakushin_do"),
        ("分荷担当者",         "text",    "toshosha"),
        ("予想販売価格",       "numbers", "yosou_kakaku"),
        ("在庫予測期間",       "text",    "zaiko_kikan"),
        ("スコア",             "numbers", "score"),
        ("分荷作業時間(分)",   "numbers", "sakugyou_jikan"),
        ("内部KW",             "text",    "internal_keyword"),
        # 商品情報
        ("アイテム名",         "text",    "item_name"),
        ("ブランド/メーカー",  "text",    "maker"),
        ("品番/型式",          "text",    "model_number"),
        ("状態",               "text",    "condition"),
        ("カテゴリ",           "text",    "category"),
        # 査定・仕入れ
        ("査定担当者",         "text",    "satei_tantosha"),
        ("査定日",             "date",    "satei_date"),
        ("仕入れ原価",         "numbers", "shiire_genka"),
        # 分荷判定
        ("分荷日",             "date",    "bunka_date"),
        ("在庫期限日",         "date",    "deadline_date"),
        # 撮影
        ("撮影担当",           "text",    "satsuei_tantosha"),
        ("撮影完了日",         "date",    "satsuei_date"),
        ("撮影時間(分)",       "numbers", "satsuei_jikan"),
        ("写真枚数",           "numbers", "photo_count"),
        ("Drive写真URL",       "text",    "drive_url"),
        # 出品
        ("出品担当",           "text",    "shuppinon_tantosha"),
        ("出品日",             "date",    "shuppinon_date"),
        ("出品作業時間(分)",   "numbers", "shuppinon_jikan"),
        ("出品プラットフォーム","text",   "platform"),
        ("出品アカウント",     "text",    "shuppinon_account"),
        ("開始価格",           "numbers", "kaishi_kakaku"),
        ("目標価格",           "numbers", "mokuhyo_kakaku"),
        ("保管ロケーション",   "text",    "location"),
        # 梱包・出荷
        ("梱包担当",           "text",    "konpo_tantosha"),
        ("梱包完了日",         "date",    "konpo_date"),
        ("梱包時間(分)",       "numbers", "konpo_jikan"),
        ("梱包材コスト",       "numbers", "konpo_cost"),
        ("運送会社",           "text",    "carrier"),
        ("追跡番号",           "text",    "tracking_number"),
        ("出荷日",             "date",    "shukka_date"),
        ("発送コスト",         "numbers", "hasso_cost"),
        # 販売結果
        ("落札日",             "date",    "rakusatsu_date"),
        ("落札価格",           "numbers", "rakusatsu_kakaku"),
        ("入札数",             "numbers", "nyusatsu_count"),
        ("アクセス数",         "numbers", "access_count"),
        ("在庫日数",           "numbers", "zaiko_days"),
        # 原価・利益（数式はMonday.com側で設定）
        ("プラットフォーム手数料", "numbers", "platform_fee"),
        ("合計原価",           "numbers", "total_genka"),
        ("総労務時間(分)",     "numbers", "total_rodo_jikan"),
        ("総労務費",           "numbers", "total_rodohi"),
        ("粗利益",             "numbers", "arari"),
        ("純利益",             "numbers", "junri"),
        ("ROI(%)",             "numbers", "roi"),
        ("利益率(%)",          "numbers", "rieki_ritsu"),
        # メモ
        ("メモ",               "text",    "memo"),
    ]
    _monday_setup_log.clear()
    _monday_setup_log.append("started")

    def _run():
        query = """
        mutation ($board_id: ID!, $title: String!, $col_type: ColumnType!, $col_id: String!) {
            create_column(board_id: $board_id, title: $title, column_type: $col_type, id: $col_id) {
                id title
            }
        }
        """
        for title, col_type, col_id in columns:
            try:
                monday_graphql(query, {
                    "board_id": MONDAY_BOARD_ID,
                    "title": title,
                    "col_type": col_type,
                    "col_id": col_id,
                })
                _monday_setup_log.append(f"OK: {title}")
            except Exception as e:
                _monday_setup_log.append(f"SKIP: {title} ({e})")
        _monday_setup_log.append("done")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "カラム作成をバックグラウンドで開始しました。/monday-setup-status で進捗確認できます。"})


@app.route("/monday-setup-status", methods=["GET"])
def monday_setup_status():
    """monday-setup バックグラウンド処理の進捗確認"""
    done = "done" in _monday_setup_log
    return jsonify({
        "done": done,
        "total": len(_monday_setup_log),
        "log": _monday_setup_log,
    })


@app.route("/slack/events", methods=["POST"])
def slack_events():
    """Slack Events APIのエンドポイント"""
    data = request.get_json(force=True)

    # URL検証チャレンジへの応答（初回設定時のみ）
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    # イベント処理
    event = data.get("event", {})
    event_id = data.get("event_id", "")

    # ボット自身の発言・重複イベントを無視
    # file_shareサブタイプは画像投稿なので許可する
    subtype = event.get("subtype", "")
    if event.get("bot_id") or event.get("bot_profile") or event_id in processed_events:
        return jsonify({"ok": True})
    if subtype and subtype != "file_share":
        return jsonify({"ok": True})

    processed_events.add(event_id)
    # メモリ節約のため古いイベントIDを削除
    if len(processed_events) > 1000:
        processed_events.clear()

    # メッセージイベントのみ処理
    if event.get("type") == "message":
        # バックグラウンドで処理（Slackの3秒タイムアウトを回避）
        thread = threading.Thread(target=process_slack_message, args=(event,))
        thread.daemon = True
        thread.start()

    return jsonify({"ok": True})


@app.route("/test-drive", methods=["GET"])
def test_drive():
    """Google Drive接続テスト"""
    import base64
    result = {"GOOGLE_SERVICE_ACCOUNT_JSON": "未設定", "GOOGLE_DRIVE_FOLDER_ID": "未設定", "drive_service": "NG", "folder_access": "NG", "error": ""}
    json_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    result["GOOGLE_SERVICE_ACCOUNT_JSON"] = "設定済み" if json_b64 else "未設定"
    result["GOOGLE_DRIVE_FOLDER_ID"] = folder_id if folder_id else "未設定"
    if not json_b64 or not folder_id:
        return jsonify(result)
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds_dict = json.loads(base64.b64decode(json_b64).decode())
        result["service_account_email"] = creds_dict.get("client_email", "不明")
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/drive"])
        service = build("drive", "v3", credentials=creds)
        result["drive_service"] = "OK"
        folder = service.files().get(fileId=folder_id, fields="id,name,permissions", supportsAllDrives=True).execute()
        result["folder_access"] = "OK"
        result["folder_name"] = folder.get("name", "不明")
    except Exception as e:
        result["error"] = str(e)
    return jsonify(result)


@app.route("/webhook", methods=["POST"])
def webhook():
    """MakeからのWebhookを受け取るエンドポイント（既存）"""
    data = request.get_json(force=True)

    channel_id = data.get("channel_id")
    thread_ts = data.get("thread_ts")
    user_message = data.get("user_message")
    image_url = data.get("image_url")

    if not all([channel_id, thread_ts, user_message]):
        return jsonify({"error": "channel_id, thread_ts, user_message は必須です"}), 400

    try:
        image_urls = [image_url] if image_url else []
        judgment = call_claude(user_message, image_urls)
        post_to_slack(channel_id, thread_ts, judgment)
        return jsonify({"ok": True, "judgment": judgment}), 200

    except Exception as e:
        error_msg = f"判定処理でエラーが発生しました: {e}"
        try:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *処理エラー*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"{error_msg}")
        except Exception:
            pass
        return jsonify({"ok": False, "error": error_msg}), 500


@app.route("/health", methods=["GET"])
def health_check():
    """全サービスの生死確認エンドポイント。Make.comから定期的に呼び出す。"""
    results = {}
    alerts = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 1. Slack API ──────────────────────────────────────────
    try:
        token = get_slack_token()
        if not token:
            raise RuntimeError("SLACK_BOT_TOKEN 未設定")
        r = httpx.get("https://slack.com/api/auth.test",
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
        data = r.json()
        if data.get("ok"):
            results["slack"] = "OK"
        else:
            raise RuntimeError(data.get("error", "unknown"))
    except Exception as e:
        results["slack"] = f"ERROR: {e}"
        alerts.append(f"🚨 Slack APIに接続できません\n→ Railwayの環境変数 SLACK_BOT_TOKEN が正しく設定されているか確認してください\n→ 人間の対応が必要です")

    # ── 2. Monday.com API ─────────────────────────────────────
    try:
        r = monday_graphql("query { me { id name } }")
        if r.get("data", {}).get("me"):
            results["monday"] = "OK"
        else:
            raise RuntimeError(str(r.get("errors", "unknown")))
    except Exception as e:
        results["monday"] = f"ERROR: {e}"
        alerts.append(f"🚨 Monday.comに接続できません\n→ Railwayの環境変数 MONDAY_TOKEN が正しく設定されているか確認してください\n→ 人間の対応が必要です")

    # ── 3. Anthropic API ステータスページ確認 ─────────────────
    try:
        r = httpx.get("https://status.claude.com/api/v2/status.json", timeout=10, follow_redirects=True)
        data = r.json()
        indicator = data.get("status", {}).get("indicator", "unknown")
        description = data.get("status", {}).get("description", "")
        if indicator == "none":
            results["anthropic"] = "OK"
        else:
            results["anthropic"] = f"WARN: {indicator} - {description}"
            alerts.append(f"⚠️ Claude AI（Anthropic）で障害が発生しています\n→ AI判定が一時的に使えない可能性があります\n→ 詳細: https://status.anthropic.com")
    except Exception as e:
        results["anthropic"] = f"ERROR: {e}"
        alerts.append(f"⚠️ Claude AIのステータス確認ができませんでした\n→ しばらく待ってから再確認してください")

    # ── 4. Slack ステータスページ確認 ─────────────────────────
    try:
        r = httpx.get("https://status.slack.com/api/v2.0.0/current", timeout=10, follow_redirects=True)
        data = r.json()
        status = data.get("status", "unknown")
        if status == "ok":
            results["slack_status"] = "OK"
        else:
            results["slack_status"] = f"WARN: {status}"
            alerts.append(f"⚠️ Slackで障害が発生しています\n→ メッセージが届かない・遅延する可能性があります\n→ 詳細: https://status.slack.com")
    except Exception as e:
        results["slack_status"] = f"ERROR: {e}"

    # ── 5. Google Drive API（設定済みの場合のみ）─────────────
    try:
        svc = get_drive_service()
        folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
        if svc:
            svc.files().list(pageSize=1, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            results["google_drive"] = "OK"
            # フォルダアクセス確認（共有ドライブのメンバー権限が切れていないかチェック）
            if folder_id:
                try:
                    folder = svc.files().get(fileId=folder_id, fields="id,name", supportsAllDrives=True).execute()
                    results["google_drive_folder"] = f"OK: {folder.get('name', '名前不明')}"
                except Exception as fe:
                    results["google_drive_folder"] = f"NG: {fe}"
                    alerts.append(f"🚨 Google Drive 写真フォルダにアクセスできません\n→ 撮影チャンネルの画像自動保存が止まっています\n→ 対応手順：\n　1. Google Drive「TKB｜A｜自社在庫」フォルダを開く\n　2. 右クリック→「共有」\n　3. bunka-bot-drive@ordinal-gear-489903-a5.iam.gserviceaccount.com を「編集者」で追加\n→ 人間の対応が必要です")
            else:
                results["google_drive_folder"] = "SKIP（GOOGLE_DRIVE_FOLDER_ID未設定）"
        else:
            results["google_drive"] = "SKIP（未設定）"
            results["google_drive_folder"] = "SKIP（未設定）"
    except Exception as e:
        results["google_drive"] = f"ERROR: {e}"
        results["google_drive_folder"] = "SKIP"
        alerts.append(f"🚨 Google Driveに接続できません\n→ Railwayの環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が正しく設定されているか確認してください\n→ 人間の対応が必要です")

    # ── 6. Bot直近24時間の処理件数確認 ────────────────────────
    total_ops = sum(v.get("完了", 0) + v.get("キャンセル", 0) + v.get("削除", 0)
                    for v in daily_stats.values())
    results["bot_24h_ops"] = total_ops
    if total_ops == 0:
        results["bot_activity"] = "WARN: 直近で処理件数が0件"
    else:
        results["bot_activity"] = f"OK: {total_ops}件処理済み"

    # ── Slack通知（異常がある場合のみ） ──────────────────────
    if alerts:
        alert_channel = os.environ.get("ALERT_CHANNEL_ID", "")
        if alert_channel:
            alert_text = (
                "━━━━━━━━━━━━━━━━\n"
                "🔍 *ヘルスチェック異常検知*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"確認日時: {now}\n\n"
                + "\n\n".join(alerts) +
                "\n\n━━━━━━━━━━━━━━━━"
            )
            try:
                httpx.post("https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {get_slack_token()}",
                             "Content-Type": "application/json"},
                    json={"channel": alert_channel, "text": alert_text},
                    timeout=10)
            except Exception as e:
                print(f"[ヘルスチェック通知エラー] {e}")

    print(f"[ヘルスチェック] {now} 結果: {results}")
    return jsonify({
        "ok": len(alerts) == 0,
        "timestamp": now,
        "results": results,
        "alerts": alerts
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
