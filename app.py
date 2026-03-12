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


def get_monthly_sequence() -> int:
    """今月の管理番号通し番号をmonday.comのアイテム数から取得する"""
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
        return int(datetime.now().strftime("%S%f")[:3]) + 1


def generate_management_number() -> str:
    """管理番号を生成する（例：2602001）西暦下2桁+月2桁+月次通し番号3桁"""
    yymm = datetime.now().strftime("%y%m")
    seq = get_monthly_sequence()
    return f"{yymm}{seq:03d}"


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
    }
    score_count = 0
    for line in response_text.split("\n"):
        line = line.strip()
        if line.startswith("【第一候補】"):
            result["first_channel"] = line.replace("【第一候補】", "").strip()
        elif line.startswith("【第二候補】"):
            result["second_channel"] = line.replace("【第二候補】", "").strip()
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
    return result


def register_to_monday(management_number: str, item_name: str, judgment: dict, user_id: str) -> None:
    """monday.comにアイテムを登録する"""
    column_values = json.dumps({
        "kanri_bango": management_number,
        "hantei_channel": judgment.get("first_channel", ""),
        "kakushin_do": judgment.get("first_confidence", ""),
        "toshosha": user_id,
    }, ensure_ascii=False)

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
- 販売価格点（35点満点）：予想販売価格の高さ
- 売りやすさ点（35点満点）：需要・回転率・競合状況
- サイズ効率点（30点満点）：作業効率・発送サイズの小ささ（小さいほど高得点）

【販売期待値】
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
📅 在庫予測期間：[期間]
📊 販売期待値：[1/2/3]（[買い手市場/市場拮抗/売り手市場]）

⭐ 総合スコア：[合計]点
　└ 販売価格点：[点]/35
　└ 売りやすさ点：[点]/35
　└ サイズ効率点：[点]/30

🏷️ 推定内部KW：/[発送コード][サイズ]/[価格コード]/[期待値]

─────────────────────────

【第二候補】[チャンネル名]
理由：[50字以内]

💰 予想販売価格：¥[下限]〜¥[上限]
📅 在庫予測期間：[期間]
📊 販売期待値：[1/2/3]（[買い手市場/市場拮抗/売り手市場]）

⭐ 総合スコア：[合計]点
　└ 販売価格点：[点]/35
　└ 売りやすさ点：[点]/35
　└ サイズ効率点：[点]/30

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
        max_tokens=1024,
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
        # 在庫期間
        period = re.search(r'在庫予測期間：(.+)', text)
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
        break
    return result


def get_confirmed_kanri_bango(channel_id: str, thread_ts: str) -> str:
    """スレッド内のBot確定メッセージから管理番号を取得する"""
    import re
    token = get_slack_token()
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    response = httpx.get(url, headers=headers, params={"channel": channel_id, "ts": thread_ts}, timeout=10)
    for msg in response.json().get("messages", []):
        if not (msg.get("bot_id") or msg.get("bot_profile")):
            continue
        match = re.search(r'管理番号：\*?(\w+)\*?', msg.get("text", ""))
        if match:
            return match.group(1)
    return ""


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


def send_to_spreadsheet(payload: dict) -> None:
    """GAS経由でGoogleスプレッドシートにデータを転記する"""
    response = httpx.post(GAS_URL, json=payload, timeout=15, follow_redirects=True)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"GAS error: {result.get('error')}")
    print("[スプレッドシート転記完了]")


def post_to_slack(channel_id: str, thread_ts: str, text: str, mention_user: str = "") -> None:
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

    # 添付画像のURLを取得（複数対応）
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]

    # テキストなしで画像のみの場合はデフォルトメッセージを使用
    if not user_message and image_urls:
        user_message = "添付画像の商品を分荷判定してください。"

    # ── コマンド判定（スレッド返信のみ） ──────────────────
    if event.get("thread_ts") and user_message:
        cmd_type, cmd_option = parse_command(user_message)
        if cmd_type:
            try:
                _handle_command(cmd_type, cmd_option, channel_id, thread_ts, event)
            except Exception as e:
                print(f"[コマンド処理エラー] {e}")
                post_to_slack(channel_id, thread_ts, f":warning: コマンド処理エラー: {e}")
            return  # コマンドならAI判定はしない

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


def _handle_command(cmd_type: str, cmd_option: str, channel_id: str, thread_ts: str, event: dict) -> None:
    """コマンド（確定/再判定/保留）を処理する"""
    user_id = event.get("user", "不明")

    # 通販対象チャンネル（管理番号・monday.com登録対象）
    TSUHAN_CHANNELS = {
        "eBayシングル", "eBayまとめ",
        "ヤフオクヴィンテージ", "ヤフオク現行", "ヤフオクまとめ",
        "ロット販売",
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

        # 通販対象チャンネルのみ管理番号発行
        management_number = ""
        if kakutei_channel in TSUHAN_CHANNELS:
            management_number = generate_management_number()

        # スプレッドシートに転記（全チャンネル共通）
        payload = {
            "kanri_bango":      management_number,
            "kakutei_channel":  kakutei_channel,
            "first_channel":    judgment.get("first_channel", ""),
            "second_channel":   judgment.get("second_channel", ""),
            "item_name":        judgment.get("item_name", ""),
            "maker":            judgment.get("maker", ""),
            "model_number":     judgment.get("model_number", ""),
            "condition":        judgment.get("condition", ""),
            "predicted_price":  judgment.get("predicted_price", ""),
            "inventory_period": judgment.get("inventory_period", ""),
            "score":            judgment.get("first_score", ""),
            "internal_keyword": judgment.get("internal_keyword", ""),
            "staff_id":         user_id,
            "timestamp":        datetime.now().strftime("%Y/%m/%d %H:%M"),
        }
        send_to_spreadsheet(payload)

        # 通販対象のみmonday.comに登録
        if management_number:
            try:
                item_name = judgment.get("first_channel", "商品")
                register_to_monday(management_number, item_name, judgment, user_id)
                print("[Monday.com登録完了]")
            except Exception as me:
                print(f"[Monday.com登録エラー] {me}")

        # Slack確定返信
        if management_number:
            reply = f"✅ *確定：{kakutei_channel}*\n🔖 管理番号：*{management_number}*\nスプレッドシート・monday.comに転記しました。"
        else:
            reply = f"✅ *確定：{kakutei_channel}*\nスプレッドシートに転記しました。"
        post_to_slack(channel_id, thread_ts, reply, mention_user=user_id)

    elif cmd_type == 'saihantei':
        post_to_slack(channel_id, thread_ts, "🔄 再判定します...")
        judgment_text = call_claude("添付の情報をもとに改めて分荷判定してください。", history=[])
        post_to_slack(channel_id, thread_ts, judgment_text)

    elif cmd_type == 'horyuu':
        post_to_slack(channel_id, thread_ts, "⏸️ 保留にしました。")

    elif cmd_type == 'cancel':
        kanri_bango = get_confirmed_kanri_bango(channel_id, thread_ts)
        if kanri_bango:
            # 確定済み → スプレッドシートにキャンセル行追記・Monday.comステータス更新
            cancel_payload = {
                "kanri_bango":     kanri_bango,
                "kakutei_channel": "キャンセル",
                "first_channel":   "",
                "second_channel":  "",
                "predicted_price": "",
                "inventory_period": "",
                "score":           "",
                "internal_keyword": "",
                "staff_id":        user_id,
                "timestamp":       datetime.now().strftime("%Y/%m/%d %H:%M"),
            }
            send_to_spreadsheet(cancel_payload)
            try:
                cancel_monday_item(kanri_bango)
            except Exception as e:
                print(f"[Monday.comキャンセルエラー] {e}")
            post_to_slack(channel_id, thread_ts,
                f"🗑️ 管理番号 *{kanri_bango}* をキャンセルしました。\n作業時間は実績としてカウントされます。",
                mention_user=user_id)
        else:
            # 確定前キャンセル → 記録なし
            post_to_slack(channel_id, thread_ts, "🗑️ 判定を取り消しました。記録はされません。",
                mention_user=user_id)


@app.route("/debug", methods=["GET"])
def debug():
    """環境変数の設定状況を確認するエンドポイント"""
    return jsonify({
        "ANTHROPIC_API_KEY": "設定済み" if os.environ.get("ANTHROPIC_API_KEY") else "未設定",
        "SLACK_BOT_TOKEN": "設定済み" if os.environ.get("SLACK_BOT_TOKEN") else "未設定",
        "MONDAY_API_TOKEN": "設定済み" if (os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN")) else "未設定",
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
