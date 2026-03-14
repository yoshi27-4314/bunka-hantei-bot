"""
AI分荷判定Bot - Step 1
Slack Events API → Claude API → Slack返信
"""

import os
import json
import base64
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
        or "eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjYzMjExNTAzOCwiYWFpIjoxMSwidWlkIjoyMjMyNzQ0MywiaWFkIjoiMjAyNi0wMy0xMlQwNTowMzo1Mi4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6OTA4Mjg2MSwicmduIjoidXNlMSJ9.a_LqA3-PQBVnXoApomC0rjaYMoq57C7GJGj2lW1zWTA"
    )


MONDAY_BOARD_ID = "18403611418"
MONDAY_API_URL = "https://api.monday.com/v2"
GAS_URL = "https://script.google.com/macros/s/AKfycbwYn4XOS7vbUSgUW23OpXGSCGDxje9GwsKtWvgOFLMRsSKCCn6Zq3dGm9IC8u_N2DmU/exec"


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
    response = httpx.post(MONDAY_API_URL, headers=headers, json=payload, timeout=15)
    return response.json()


# 担当者Slack UserID → スタッフコード対応表
# UserIDはSlackプロフィール→「その他」→「メンバーIDをコピー」で取得
STAFF_MAP = {
    "U0AL10Q1HQC": "YA",  # 浅野儀頼
    "U0ALQ4BJNSV": "KH",  # 林和人
    "U0AL4R1EMMZ": "MH",  # 平野光雄
    # "UXXXXXXXX": "YY",  # 横山優
    # "UXXXXXXXX": "KM",  # 三島圭織
    # "UXXXXXXXX": "TM",  # 松本豊彦
    # "UXXXXXXXX": "TK",  # 北瀬孝
    # "UXXXXXXXX": "YM",  # 桃井侑菜
    # "UXXXXXXXX": "SI",  # 伊藤佐和子
    # "UXXXXXXXX": "YS",  # 白木雄介
}


def get_staff_code(user_id: str) -> str:
    """Slack UserIDからスタッフコードを返す。未登録の場合はUserIDをそのまま返す"""
    return STAFF_MAP.get(user_id, user_id)


# 確定チャンネル → アカウント区分 (V=ビンテージ / G=現行品 / M=まとめ売り / E=eBay)
CHANNEL_TO_ACCOUNT_TYPE = {
    "ヤフオクヴィンテージ": "V",
    "ヤフオク現行":         "G",
    "ヤフオクまとめ":       "M",
    "eBayシングル":         "E",
    "eBayまとめ":           "E",
}


def get_monthly_sequence(account_type: str) -> int:
    """今月・アカウント区分ごとの通し番号をmonday.comのアイテム数から取得する"""
    yymm = datetime.now().strftime("%y%m")
    prefix = f"{yymm}{account_type}"   # 例: 2603V
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
            if item.get("column_values", [{}])[0].get("text", "").startswith(prefix)
        )
        return count + 1
    except Exception as e:
        print(f"[通し番号取得エラー] {e}")
        return int(datetime.now().strftime("%S%f")[:4]) + 1


def generate_management_number(account_type: str) -> str:
    """管理番号を生成する（例：2603V0001）
    西暦下2桁 + 月2桁 + アカウント区分(V/G/M/E) + 月次通し番号4桁
    V=ヤフオクビンテージ / G=ヤフオク現行 / M=ヤフオクまとめ / E=eBay
    ※ロット販売・社内利用・スクラップ・廃棄は管理番号なし
    """
    yymm = datetime.now().strftime("%y%m")
    seq = get_monthly_sequence(account_type)
    return f"{yymm}{account_type}{seq:04d}"


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


def register_to_monday(management_number: str, item_name: str, judgment: dict, user_id: str, sakugyou_jikan: int = 0) -> None:
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
        "hantei_channel": judgment.get("first_channel", ""),
        "kakushin_do": judgment.get("first_confidence", ""),
        "toshosha": get_staff_code(user_id),
        "zaiko_kikan": judgment.get("inventory_period", ""),
        "status": {"label": "査定待ち"},
    }
    if price_num:
        col["yosou_kakaku"] = price_num
    if judgment.get("first_score"):
        col["score"] = judgment.get("first_score")
    if sakugyou_jikan > 0:
        col["sakugyou_jikan"] = sakugyou_jikan
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
スタッフが入力した商品情報をもとに、以下の9チャンネルのどこに振り分けるか判定してください。

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

【判定のポイント】
- 製造年代・ブランド・状態・市場相場を考慮する
- 判断に迷う場合は追加質問をする
- 判定理由を簡潔に説明する

【スコアリング基準（100点満点）】

▍収益期待スコア（30点）
・予想販売価格の高さ（市場相場・オークション実績ベース）
・価格の安定性・相場確信度
・まとめ/セット売りによる付加価値可能性

▍在庫回転スコア（25点）
・予測在庫期間（〜1週間:25点 / 〜1ヶ月:18点 / 〜3ヶ月:10点 / 3ヶ月超:3点）
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
※なるべくシンプルな表記を選ぶ（4Sより40Hより4Sが優先）

期待値：販売期待値（1/2/3）をそのまま使用

【商品情報の抽出】
入力・画像から以下を抽出してください（不明な場合は「不明」と記載）：
- アイテム名：商品の名称
- メーカー/ブランド：製造元・ブランド名
- 品番/型式：型番・モデル番号（分からない場合は空白）
- 状態：以下4つから最も近いものを1つ選ぶ
  　中古 / ジャンク・現状品 / 中古美品 / 新品・未使用品

【出力フォーマット】
━━━━━━━━━━━━━━━━
📦 分荷判定結果
━━━━━━━━━━━━━━━━

📋 アイテム名：[商品名]
🏭 メーカー/ブランド：[メーカー名]
🔢 品番/型式：[品番 or 不明]
📊 状態：[中古/ジャンク・現状品/中古美品/新品・未使用品]

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
追加確認：[判断に必要な情報が不足している場合のみ記載。不要な場合は省略]"""


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
    response = httpx.get(url, headers=headers, params=params, timeout=10)
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
        model="claude-sonnet-4-20250514",
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
    response = httpx.get(url, headers=headers, params=params, timeout=10)
    data = response.json()

    result = {}
    for msg in data.get("messages", []):
        if not (msg.get("bot_id") or msg.get("bot_profile")):
            continue
        text = msg.get("text", "")
        if "分荷判定結果" not in text:
            continue
        mn = re.search(r'管理番号：\*?(\w+)\*?', text)
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
        cond = re.search(r'状態：(中古|ジャンク[・\-]現状品|中古美品|新品[・\-]未使用品)', text)
        if cond:
            result["condition"] = cond.group(1).strip()
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
        if "確定：" not in text:
            continue
        kanri_bango = ""
        kakutei_channel = ""
        m_kanri = re.search(r'管理番号：\*?(\w+)\*?', text)
        if m_kanri:
            kanri_bango = m_kanri.group(1)
        m_channel = re.search(r'確定：\*?(.+?)\*?(?:\n|$)', text)
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
    col_vals = json.dumps({"kakushin_do": "キャンセル"}, ensure_ascii=False)
    monday_graphql(update_query, {"board_id": MONDAY_BOARD_ID, "item_id": item_id, "col_vals": col_vals})
    print(f"[Monday.com] {kanri_bango} をキャンセルに更新")


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
    "1": "新品",
    "2": "未使用",
    "3": "中古美品",
    "4": "中古",
    "5": "ジャンク（部品取り）",
}


def post_checklist(channel_id: str, thread_ts: str, management_number: str) -> None:
    """動作確認・現状確認チェックリストをスレッドに投稿する"""
    text = (
        f"📋 *{management_number}* の動作確認・現状確認をお願いします\n\n"
        "*【確認項目】*\n"
        "• 電源を入れて動作確認\n"
        "• ドア・引き出し・蓋など開閉確認\n"
        "• 外観・傷・汚れの確認\n"
        "• パーツ・付属品の欠品確認\n\n"
        "*【商品状態を番号で選んでください】*\n"
        "1️⃣ 新品\n"
        "2️⃣ 未使用\n"
        "3️⃣ 中古美品\n"
        "4️⃣ 中古\n"
        "5️⃣ ジャンク（部品取り）\n\n"
        "状態番号＋確認コメントを返信してください\n"
        "例：`3 電源OK、外観に小傷あり、パーツ全部揃ってます`\n"
        "※音声入力でも問題ありません"
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
            m = re.search(r'\*(\w+)\* の動作確認・現状確認をお願いします', text)
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
    response = httpx.post(GAS_URL, json=payload, timeout=15, follow_redirects=True)
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
}


def post_to_slack(channel_id: str, thread_ts: str, text: str, mention_user: str = "", bot_role: str = "bunika") -> None:
    """Slackの指定スレッドにメッセージを返信する"""
    if mention_user:
        text = f"<@{mention_user}> {text}"
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


def process_slack_message(event: dict) -> None:
    """Slackメッセージをバックグラウンドで処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_message = event.get("text", "")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    print(f"[処理開始] channel={channel_id} ts={thread_ts} message={user_message[:30]}")
    print(f"[ENV確認] ANTHROPIC_API_KEY={'設定済み' if anthropic_key else '未設定'} SLACK_BOT_TOKEN={'設定済み' if slack_token else '未設定'}")

    # ── 在庫検索はチャンネルに関わらず最優先で処理 ──────────
    if user_message:
        cmd_type, cmd_option = parse_command(user_message)
        if cmd_type == 'zaiko_search':
            try:
                _handle_zaiko_search(cmd_option, channel_id, thread_ts, event)
            except Exception as e:
                print(f"[在庫検索エラー] {e}")
                post_to_slack(channel_id, thread_ts, f":warning: 在庫検索エラー: {e}")
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

    kintai_channel_id = os.environ.get("KINTAI_CHANNEL_ID", "")
    if kintai_channel_id and channel_id == kintai_channel_id:
        handle_kintai_channel(event)
        return

    # 添付画像のURLを取得（複数対応）
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]

    # テキストなしで画像のみの場合はデフォルトメッセージを使用
    if not user_message and image_urls:
        user_message = "添付画像の商品を分荷判定してください。"

    # ── コマンド判定（確定・再判定・保留・キャンセルはスレッド内のみ） ──
    if user_message:
        cmd_type, cmd_option = parse_command(user_message)
        # 確定・再判定・保留・キャンネルはスレッド内のみ
        if event.get("thread_ts") and cmd_type:
            try:
                _handle_command(cmd_type, cmd_option, channel_id, thread_ts, event)
            except Exception as e:
                print(f"[コマンド処理エラー] {e}")
                post_to_slack(channel_id, thread_ts, f":warning: コマンド処理エラー: {e}")
            return  # コマンドならAI判定はしない

        # ── チェックリスト応答判定 ─────────────────────────
        checklist = get_checklist_state(channel_id, thread_ts)
        if checklist and not checklist["is_completed"]:
            n = normalize_keyword(user_message)
            # 先頭が1〜5の数字 → 状態番号＋コメントの回答とみなす
            is_checklist_input = n and n[0] in CONDITION_MAP
            if is_checklist_input:
                try:
                    _handle_checklist(checklist, user_message, channel_id, thread_ts, event)
                except Exception as e:
                    print(f"[チェックリスト処理エラー] {e}")
                    post_to_slack(channel_id, thread_ts, f":warning: チェックリスト処理エラー: {e}")
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
            post_to_slack(channel_id, thread_ts, f":warning: エラーが発生しました: {e}")
        except Exception as e2:
            print(f"[Slack送信エラー] {e2}")


def _handle_zaiko_search(keyword: str, channel_id: str, thread_ts: str, event: dict) -> None:
    """在庫検索コマンドを処理する"""
    user_id = event.get("user", "")
    results = search_inventory(keyword)
    if not results:
        post_to_slack(channel_id, thread_ts,
            f"🔍 「{keyword}」の在庫は見つかりませんでした。", mention_user=user_id)
        return

    lines = [f"🔍 *「{keyword}」の在庫 {len(results)}件*\n"]
    for i, r in enumerate(results[:10], 1):  # 最大10件
        kanri = r["kanri_bango"] or "番号なし"
        status = r["status"] or "不明"
        zaiko = r["zaiko_kikan"] or ""
        channel = r["channel"] or ""
        monday_url = f"https://monday.com/boards/{MONDAY_BOARD_ID}"
        line = f"*{i}. {r['name']}*\n"
        line += f"　管理番号: `{kanri}` ／ チャンネル: {channel}\n"
        line += f"　状態: {status}"
        if zaiko:
            line += f" ／ 在庫期間: {zaiko}"
        line += f"\n　<{monday_url}|Monday.comで詳細・画像確認>"
        lines.append(line)

    if len(results) > 10:
        lines.append(f"\n_...他 {len(results) - 10}件。Monday.comで全件確認できます。_")

    post_to_slack(channel_id, thread_ts, "\n".join(lines), mention_user=user_id)


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
            post_to_slack(channel_id, thread_ts, ":warning: 判定データが見つかりませんでした。")
            return

        # 確定チャンネルを決定（表記ゆれを正規化）
        if cmd_option == '1':
            kakutei_channel = normalize_channel(judgment.get("first_channel", ""))
        elif cmd_option == '2':
            kakutei_channel = normalize_channel(judgment.get("second_channel", ""))
        else:
            kakutei_channel = normalize_channel(cmd_option)  # 確定/○○ の場合

        # 通販対象チャンネルのみ管理番号発行（アカウント区分を自動判定）
        management_number = ""
        if kakutei_channel in TSUHAN_CHANNELS:
            account_type = CHANNEL_TO_ACCOUNT_TYPE.get(kakutei_channel, "G")
            management_number = generate_management_number(account_type)
            print(f"[管理番号発行] {management_number} (区分:{account_type} チャンネル:{kakutei_channel})")

        # 分荷作業時間を計算（投稿タイムスタンプ〜確定コマンドの経過時間・分）
        sakugyou_jikan = 0
        try:
            post_ts = float(thread_ts)
            confirm_ts = float(event.get("ts", thread_ts))
            sakugyou_jikan = max(0, int((confirm_ts - post_ts) / 60))
            print(f"[作業時間] {sakugyou_jikan}分")
        except Exception as e:
            print(f"[作業時間計算エラー] {e}")

        # スプレッドシートに転記（全チャンネル共通）
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

        # 通販対象のみmonday.comに登録
        if management_number:
            try:
                item_name = judgment.get("item_name") or judgment.get("first_channel", "商品")
                register_to_monday(management_number, item_name, judgment, user_id, sakugyou_jikan)
                print("[Monday.com登録完了]")
            except Exception as me:
                print(f"[Monday.com登録エラー] {me}")

        # Slack確定返信
        if management_number:
            reply = f"✅ *確定：{kakutei_channel}*\n🔖 管理番号：*{management_number}*\nスプレッドシート・monday.comに転記しました。"
        else:
            reply = f"✅ *確定：{kakutei_channel}*\nスプレッドシートに転記しました。"
        post_to_slack(channel_id, thread_ts, reply, mention_user=user_id)

        # 通販系チャンネルのみ動作確認チェックリストを表示
        if management_number:
            post_checklist(channel_id, thread_ts, management_number)

    elif cmd_type == 'saihantei':
        post_to_slack(channel_id, thread_ts, "🔄 再判定します...")
        judgment_text = call_claude("添付の情報をもとに改めて分荷判定してください。", history=[])
        post_to_slack(channel_id, thread_ts, judgment_text)

    elif cmd_type == 'horyuu':
        post_to_slack(channel_id, thread_ts, "⏸️ 保留にしました。")

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
            if kanri_bango:
                try:
                    cancel_monday_item(kanri_bango)
                except Exception as e:
                    print(f"[Monday.comキャンセルエラー] {e}")
                post_to_slack(channel_id, thread_ts,
                    f"🗑️ 管理番号 *{kanri_bango}* をキャンセルしました。\n作業時間は実績としてカウントされます。",
                    mention_user=user_id)
            else:
                post_to_slack(channel_id, thread_ts,
                    f"🗑️ *{confirmed_channel}* の確定をキャンセルしました。\nスプレッドシートに記録しました。",
                    mention_user=user_id)
        else:
            # 確定前キャンセル → 記録なし
            post_to_slack(channel_id, thread_ts, "🗑️ 判定を取り消しました。記録はされません。",
                mention_user=user_id)


def _handle_checklist(checklist: dict, raw_text: str, channel_id: str, thread_ts: str, event: dict) -> None:
    """チェックリスト応答（状態番号＋フリーコメント）を処理する"""
    user_id = event.get("user", "")
    management_number = checklist["management_number"]

    # 先頭の数字を状態番号として取得、残りをコメントとして扱う
    n = normalize_keyword(raw_text)
    condition_key = n[0]
    condition_label = CONDITION_MAP.get(condition_key, "")
    comment = n[1:].strip() if len(n) > 1 else ""

    reply = (
        f"✅ *動作確認完了* {management_number}\n"
        f"📊 状態：*{condition_label}*\n"
    )
    if comment:
        reply += f"💬 {comment}"

    post_to_slack(channel_id, thread_ts, reply, mention_user=user_id)

    # Monday.comのステータスと状態を更新
    try:
        update_monday_item_status(management_number, "動作確認済み")
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
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
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
        fields="files(name)"
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
                media_body=media, fields="id"
            ).execute()
            print(f"[Drive] {filename} アップロード完了")
        except Exception as e:
            print(f"[Drive] アップロードエラー: {e}")

    return f"https://drive.google.com/drive/folders/{item_id}"


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
                        "管理番号は「2603V0001」のような形式です（年月2桁+月2桁+英字1文字[V/G/M/E]+数字4桁）。"
                        "管理番号だけを返してください。見つからない場合は「不明」と返してください。"
                    )}
                ]
            }]
        )
        text = response.content[0].text.strip()
        m = re.search(r'\d{4}[VGME]\d{4}', text)
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
        m = re.search(r'管理番号\s*\*?(\d{4}[VGME]\d{4})\*?', msg.get("text", ""))
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
        text_mn = _re.search(r'\d{4}[VGME]\d{4}', text)
        if not image_urls and not text_mn:
            return
        if text_mn and not image_urls:
            management_number = text_mn.group(0)
        elif image_urls:
            management_number = extract_management_number_from_image(image_urls[0])
            if not management_number:
                post_to_slack(channel_id, current_ts,
                    "⚠️ テプラの管理番号を読み取れませんでした。\n"
                    "もう一度管理番号を送信してください。",
                    bot_role="satsuei")
                return
            # テプラ画像をDriveに保存
            upload_images_to_drive(management_number, [image_urls[0]], is_tepura=True)
        else:
            return
        post_to_slack(channel_id, current_ts,
            f"📸 管理番号 *{management_number}* を確認しました。\n"
            "商品写真をこのスレッドに投稿してください。\n"
            "※複数枚まとめてOKです。撮影完了後は `完了` と入力してください。",
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
        return

    # キャンセル・中断
    if text in CANCEL_WORDS:
        log_work_activity(CHANNEL_NAMES["satsuei"], management_number, get_staff_code(user_id), "キャンセル")
        post_to_slack(channel_id, thread_ts,
            f"⏹️ *{management_number}* の撮影作業をキャンセルしました。",
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
            f"📷 {len(image_urls)}枚を保存しました。\n"
            "追加写真を投稿するか `完了` と入力してください。",
            mention_user=user_id, bot_role="satsuei")

    # 完了コマンド
    if text == "完了":
        post_to_slack(channel_id, thread_ts,
            f"✅ *{management_number}* 撮影完了しました！",
            mention_user=user_id, bot_role="satsuei")
        log_work_activity(CHANNEL_NAMES["satsuei"], management_number, get_staff_code(user_id), "完了")
        try:
            update_monday_item_status(management_number, "撮影済み")
        except Exception as e:
            print(f"[Monday.com撮影済み更新エラー] {e}")
        try:
            send_to_spreadsheet({
                "action":           "satsuei_update",
                "kanri_bango":      management_number,
                "drive_folder_url": folder_url,
                "staff_id":         get_staff_code(user_id),
                "timestamp":        datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        except Exception as e:
            print(f"[スプレッドシート撮影済み更新エラー] {e}")


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
        "🗑️ 削除する管理番号を入力してください。\n"
        "（例：`2603G0001`）\n\n"
        "⚠️ 削除するとMonday.comのステータスが「要確認」に戻ります。",
        mention_user=user_id, bot_role=bot_role)


def handle_delete_step2(channel_id: str, thread_ts: str, user_id: str, text: str) -> bool:
    """削除確認：管理番号が一致したら削除を実行。処理した場合Trueを返す"""
    import re as _re
    pending = delete_confirm_sessions.get(thread_ts)
    if not pending:
        return False
    mn_m = _re.search(r'\d{4}[VGME]\d{4}', text)
    if not mn_m:
        return False
    management_number = mn_m.group(0)
    channel_name = pending["channel_name"]
    bot_role = pending["bot_role"]
    staff_id = pending["staff_id"]
    del delete_confirm_sessions[thread_ts]
    try:
        update_monday_item_status(management_number, "要確認")
    except Exception as e:
        print(f"[Monday.com削除更新エラー] {e}")
    log_work_activity(channel_name, management_number, staff_id, "削除")
    post_to_slack(channel_id, thread_ts,
        f"🗑️ *{management_number}* を削除しました。\n"
        "Monday.comのステータスを「要確認」に戻しました。",
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
    """monday.comから管理番号に対応するアイテムデータを取得する"""
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
            return {"monday_name": item["name"], **cols}
    return {}


def generate_listing_content(management_number: str, item_data: dict) -> dict:
    """Claudeでヤフオク出品タイトル・説明文・価格を生成する"""
    import re
    client = get_anthropic_client()
    if not client:
        return {}
    prompt = (
        f"以下の商品情報をもとに、ヤフオクの出品データをJSON形式で作成してください。\n\n"
        f"管理番号：{management_number}\n"
        f"販売チャンネル：{item_data.get('hantei_channel', '')}\n"
        f"予想販売価格：{item_data.get('yosou_kakaku', '')}\n"
        f"在庫予測期間：{item_data.get('zaiko_kikan', '')}\n"
        f"内部KW：{item_data.get('internal_keyword', '')}\n\n"
        "以下のJSON形式のみで返してください（説明文不要）：\n"
        '{"title":"出品タイトル（40文字以内）",'
        '"description":"商品説明文（200〜400文字）",'
        '"start_price":開始価格の数字}'
    )
    try:
        response = get_anthropic_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        print(f"[出品コンテンツ生成エラー] {e}")
    return {}


def post_listing_summary(channel_id: str, thread_ts: str, session: dict, mention_user: str = "") -> None:
    """出品データをSlackに整形して表示する"""
    mn = session["management_number"]
    start = session.get("start_price", 0)
    size = session.get("size", "")
    text = (
        f"📦 管理番号：*{mn}*\n"
        "─────────────────────\n"
        f"📋 タイトル：{session.get('title', '（未設定）')}\n\n"
        f"📊 状態：{session.get('condition', '（未確認）')}\n"
        f"💰 開始価格：¥{start:,}\n"
        f"📐 梱包サイズ：{size + 'サイズ' if size else '（推定中）'}\n\n"
        f"📝 説明文：\n{session.get('description', '（未生成）')}\n\n"
        "─────────────────────\n"
        "*修正する場合はコマンドで入力してください：*\n"
        "`タイトル：新しいタイトル`\n"
        "`開始価格：5000`\n"
        "`説明文：新しい説明文`\n"
        "`サイズ：120`\n\n"
        "✅ 準備ができたら *保管ロケーション番号* を入力してください（例：`A-12`）"
    )
    post_to_slack(channel_id, thread_ts, text, mention_user=mention_user, bot_role="shuppinon")


def execute_listing(session: dict, location: str, channel_id: str, thread_ts: str, user_id: str) -> None:
    """出品を実行する（スプレッドシート記録 + Monday.com更新）"""
    import re
    management_number = session["management_number"]

    # スプレッドシートに出品データを記録
    try:
        send_to_spreadsheet({
            "action":       "shuppinon_listing",
            "kanri_bango":  management_number,
            "title":        session.get("title", ""),
            "description":  session.get("description", ""),
            "condition":    session.get("condition", ""),
            "start_price":  str(session.get("start_price", "")),
            "buyout_price": str(session.get("buyout_price", "")),
            "size":         session.get("size", ""),
            "location":     location,
            "staff_id":     get_staff_code(user_id),
            "timestamp":    datetime.now().strftime("%Y/%m/%d %H:%M"),
        })
    except Exception as e:
        print(f"[スプレッドシート出品記録エラー] {e}")

    # Monday.comステータスを「出品中」に更新
    try:
        update_monday_item_status(management_number, "出品中")
    except Exception as e:
        print(f"[Monday.com出品中更新エラー] {e}")

    # TODO: ヤフオク自動出品（オークタウンAPI確認後に実装予定）
    start = session.get("start_price", 0)
    post_to_slack(channel_id, thread_ts,
        f"✅ *{management_number}* 出品処理を登録しました\n"
        f"📍 保管場所：*{location}*\n"
        f"📋 タイトル：{session.get('title', '')}\n"
        f"💰 開始価格：¥{start:,}\n\n"
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
        text_mn = _re.search(r'\d{4}[VGME]\d{4}', text)
        if not image_urls and not text_mn:
            return
        if text_mn and not image_urls:
            management_number = text_mn.group(0)
        elif image_urls:
            post_to_slack(channel_id, current_ts,
                "🔍 管理番号を読み取り中...", mention_user=user_id, bot_role="shuppinon")
            management_number = extract_management_number_from_image(image_urls[0])
        if not management_number:
            post_to_slack(channel_id, current_ts,
                "⚠️ 管理番号を確認できませんでした。\n"
                "もう一度管理番号を送信してください。",
                bot_role="shuppinon")
            return

        # Monday.comからデータ取得
        item_data = get_item_from_monday(management_number)
        if not item_data:
            post_to_slack(channel_id, current_ts,
                f"⚠️ *{management_number}* は確認できません。\n"
                "もう一度管理番号を確認して送信してください。",
                bot_role="shuppinon")
            return

        # Claudeで出品コンテンツ生成
        post_to_slack(channel_id, current_ts, "⏳ 出品データを生成中...", bot_role="shuppinon")
        listing = generate_listing_content(management_number, item_data)

        # 梱包サイズを内部KWから推定（例: /S80/ → 80）
        kw = item_data.get("internal_keyword", "")
        size_m = re.search(r'/[A-Z]+(\d+)/', kw)
        size = size_m.group(1) if size_m else ""

        session = {
            "management_number": management_number,
            "title":       listing.get("title", management_number),
            "description": listing.get("description", ""),
            "condition":   item_data.get("kakushin_do", ""),
            "start_price": listing.get("start_price", 0),
            "buyout_price": listing.get("buyout_price", 0),
            "size":        size,
            "item_data":   item_data,
            "start_time":  datetime.now(),
        }
        listing_sessions[current_ts] = session
        post_listing_summary(channel_id, current_ts, session, mention_user=user_id)
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
        return

    management_number = session["management_number"]

    # キャンセル・中断
    if text in CANCEL_WORDS:
        log_work_activity(CHANNEL_NAMES["shuppinon"], management_number,
                          get_staff_code(user_id), "キャンセル", session.get("start_time"))
        del listing_sessions[thread_ts]
        post_to_slack(channel_id, thread_ts,
            f"⏹️ *{management_number}* の出品作業をキャンセルしました。",
            mention_user=user_id, bot_role="shuppinon")
        return

    # 削除コマンド
    if text == "削除":
        handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["shuppinon"], "shuppinon")
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

    # ロケーション番号（修正コマンド以外のすべてのテキスト）→ 出品確定
    if text:
        execute_listing(session, text, channel_id, thread_ts, user_id)
        log_work_activity(CHANNEL_NAMES["shuppinon"], session["management_number"],
                          get_staff_code(user_id), "完了", session.get("start_time"))
        del listing_sessions[thread_ts]


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
        result = anthropic.Anthropic().messages.create(
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


def _finish_shipping(channel_id, thread_ts, user_id, management_number, carrier, tracking_number):
    """出荷手配完了の共通処理"""
    tracking_text = f"📮 追跡番号：*{tracking_number}*\n" if tracking_number else ""
    post_to_slack(channel_id, thread_ts,
        f"🚚 *{management_number}* 出荷手配完了！\n"
        f"🏢 運送会社：{carrier}\n"
        f"{tracking_text}",
        mention_user=user_id, bot_role="konpo")
    try:
        update_monday_item_status(management_number, "出荷済み")
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
        delayed_m = _re.match(r'(\d{4}[VGME]\d{4})\s+(佐川|アート|西濃)\S*\s+(\S+)', text)
        if delayed_m:
            mn, carrier_kw, tracking = delayed_m.group(1), delayed_m.group(2), delayed_m.group(3)
            carrier_name = {"佐川": "佐川急便", "アート": "アートデリバリー", "西濃": "西濃運輸"}.get(carrier_kw, carrier_kw)
            _finish_shipping(channel_id, current_ts, user_id, mn, carrier_name, tracking)
            return

        # 通常の梱包開始
        text_mn = _re.search(r'\d{4}[VGME]\d{4}', text)
        if not text_mn and not image_urls:
            return
        if text_mn:
            management_number = text_mn.group(0)
        else:
            post_to_slack(channel_id, current_ts, "🔍 管理番号を読み取り中...", bot_role="konpo")
            management_number = extract_management_number_from_image(image_urls[0])
            if not management_number:
                post_to_slack(channel_id, current_ts,
                    "⚠️ 管理番号を確認できませんでした。\nもう一度送信してください。",
                    bot_role="konpo")
                return

        item_data = get_item_from_monday(management_number)
        if not item_data:
            post_to_slack(channel_id, current_ts,
                f"⚠️ *{management_number}* は確認できません。\n"
                "もう一度管理番号を確認して送信してください。",
                bot_role="konpo")
            return

        kw = item_data.get("internal_keyword", "")
        size_m = _re.search(r'/[A-Z]+(\d+)/', kw)
        size = size_m.group(1) if size_m else "不明"

        konpo_sessions[current_ts] = {
            "management_number": management_number,
            "size": size,
            "packed": False,
            "carrier": None,
            "waiting_label": False,
            "start_time": datetime.now(),
        }
        post_to_slack(channel_id, current_ts,
            f"📦 *{management_number}* の梱包情報\n\n"
            f"📐 梱包サイズ：{size}サイズ\n"
            f"📋 判定チャンネル：{item_data.get('hantei_channel', '')}\n"
            f"💰 予想販売価格：{item_data.get('yosou_kakaku', '')}\n\n"
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
        return
    management_number = session["management_number"]

    # キャンセル・中断
    if text in CANCEL_WORDS:
        log_work_activity(CHANNEL_NAMES["konpo"], management_number,
                          get_staff_code(user_id), "キャンセル", session.get("start_time"))
        del konpo_sessions[thread_ts]
        post_to_slack(channel_id, thread_ts,
            f"⏹️ *{management_number}* の梱包作業をキャンセルしました。",
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
            f"✅ 梱包完了を確認しました。\n\n{CARRIER_MENU}",
            mention_user=user_id, bot_role="konpo")
        try:
            update_monday_item_status(management_number, "梱包済み")
        except Exception as e:
            print(f"[Monday.com梱包済み更新エラー] {e}")
        return

    # ② 運送会社選択（1〜5）
    if session["packed"] and not session["carrier"] and text in CARRIER_MAP:
        carrier = CARRIER_MAP[text]
        session["carrier"] = carrier
        konpo_sessions[thread_ts] = session

        if text == "4":  # 直接引き取り
            _finish_shipping(channel_id, thread_ts, user_id, management_number, carrier, "")
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
                "2603G0001 佐川 123456789012\n"
                "2603V0002 アート 0987654321\n"
                "2603M0003 西濃 111222333444\n"
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
                f"📸 {carrier}の送り状ラベルの写真を送ってください。",
                mention_user=user_id, bot_role="konpo")
        return

    # ③ 送り状ラベル写真 → OCRで追跡番号抽出
    if session.get("waiting_label") and image_urls:
        carrier = session["carrier"]
        post_to_slack(channel_id, thread_ts, "🔍 追跡番号を読み取り中...", bot_role="konpo")
        tracking_number = extract_tracking_number_from_image(image_urls[0], carrier)
        if not tracking_number:
            post_to_slack(channel_id, thread_ts,
                "⚠️ 追跡番号を読み取れませんでした。\nもう一度写真を送ってください。",
                mention_user=user_id, bot_role="konpo")
            return
        _finish_shipping(channel_id, thread_ts, user_id, management_number, carrier, tracking_number)
        log_work_activity(CHANNEL_NAMES["konpo"], management_number,
                          get_staff_code(user_id), "完了", session.get("start_time"))
        del konpo_sessions[thread_ts]


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

    text_mn = _re.search(r'\d{4}[VGME]\d{4}', text)
    if not text_mn and not image_urls:
        return

    if text_mn:
        management_number = text_mn.group(0)
    else:
        management_number = extract_management_number_from_image(image_urls[0])
        if not management_number:
            post_to_slack(channel_id, current_ts,
                "⚠️ 管理番号を確認できませんでした。\nもう一度送信してください。",
                bot_role="status")
            return

    item_data = get_item_from_monday(management_number)
    if not item_data:
        post_to_slack(channel_id, current_ts,
            f"⚠️ *{management_number}* は確認できません。\n"
            "もう一度管理番号を確認して送信してください。",
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
        f"📊 *{management_number}* のステータス\n\n"
        f"🏷️ 現在のステータス：*{status}*\n"
        f"📦 判定チャンネル：{item_data.get('hantei_channel', '不明')}\n"
        f"💰 予想販売価格：{item_data.get('yosou_kakaku', '不明')}\n"
        f"📅 在庫予測期間：{item_data.get('zaiko_kikan', '不明')}\n"
        f"⭐ スコア：{item_data.get('score', '不明')}点\n"
    )
    if days_elapsed:
        reply += f"🕐 登録からの経過：約{days_elapsed}日"

    post_to_slack(channel_id, current_ts, reply, mention_user=user_id, bot_role="status")


# ── 出退勤チャンネル ──────────────────────────────────────

def handle_attendance_channel(event: dict) -> None:
    """出退勤チャンネルのイベントを処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    user_id = event.get("user", "")
    text = normalize_keyword(event.get("text", ""))
    staff_id = get_staff_code(user_id)
    now = datetime.now()
    timestamp = now.strftime("%Y/%m/%d %H:%M")

    if text == "出勤":
        # 出勤記録
        try:
            send_to_spreadsheet({
                "action":    "attendance",
                "staff_id":  staff_id,
                "type":      "出勤",
                "timestamp": timestamp,
            })
        except Exception as e:
            print(f"[出勤記録エラー] {e}")
        post_to_slack(channel_id, current_ts,
            f"🌅 おはようございます！\n"
            f"*{staff_id}* の出勤を記録しました（{timestamp}）",
            bot_role="bunika")
        return

    if text == "退勤":
        # 本日の作業サマリー集計
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
                "action":    "attendance",
                "staff_id":  staff_id,
                "type":      "退勤",
                "timestamp": timestamp,
                "kanri_bango": str(stats.get("完了", 0)),  # 完了件数を流用
            })
        except Exception as e:
            print(f"[退勤記録エラー] {e}")

        # daily_statsをリセット
        if staff_id in daily_stats:
            del daily_stats[staff_id]

        post_to_slack(channel_id, current_ts,
            f"🌙 お疲れさまでした！\n"
            f"*{staff_id}* の退勤を記録しました（{timestamp}）\n\n"
            f"📊 *本日の作業実績*\n{summary_text}",
            bot_role="bunika")
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
        ("管理番号", "text", "kanri_bango"),
        ("判定チャンネル", "text", "hantei_channel"),
        ("確信度", "text", "kakushin_do"),
        ("投稿者", "text", "toshosha"),
        ("予想販売価格", "numbers", "yosou_kakaku"),
        ("在庫予測期間", "text", "zaiko_kikan"),
        ("スコア", "numbers", "score"),
        ("作業時間", "numbers", "sakugyou_jikan"),
        ("ステータス", "status", "status"),
    ]
    results = []
    for title, col_type, col_id in columns:
        query = """
        mutation ($board_id: ID!, $title: String!, $col_type: ColumnType!, $col_id: String!) {
            create_column(board_id: $board_id, title: $title, column_type: $col_type, id: $col_id) {
                id
                title
            }
        }
        """
        try:
            result = monday_graphql(query, {
                "board_id": MONDAY_BOARD_ID,
                "title": title,
                "col_type": col_type,
                "col_id": col_id,
            })
            results.append({"title": title, "result": result})
        except Exception as e:
            results.append({"title": title, "error": str(e)})
    return jsonify({"ok": True, "results": results})


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
            post_to_slack(channel_id, thread_ts, f":warning: {error_msg}")
        except Exception:
            pass
        return jsonify({"ok": False, "error": error_msg}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
