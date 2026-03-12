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
        or "eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjYzMTAzNTEzOSwiYWFpIjoxMSwidWlkIjoyMjMyNzQ0MywiaWFkIjoiMjAyNi0wMy0xMFQwNzo1MjoyOS4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6OTA4Mjg2MSwicmduIjoidXNlMSJ9.FiX9oJA5CP-g7vb5Fa0RmhOl685wy3WYovd_Xhw1YdM"
    )


MONDAY_BOARD_ID = "18403611418"
MONDAY_API_URL = "https://api.monday.com/v2"


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


def generate_management_number() -> str:
    """管理番号を生成する（例：20250312-103045）"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def extract_judgment(response_text: str) -> dict:
    """Claude応答から判定結果を抽出する"""
    result = {"first_channel": "", "first_confidence": "", "second_channel": ""}
    confidence_count = 0
    for line in response_text.split("\n"):
        line = line.strip()
        if line.startswith("第一候補："):
            result["first_channel"] = line.replace("第一候補：", "").strip()
        elif line.startswith("第二候補："):
            result["second_channel"] = line.replace("第二候補：", "").strip()
        elif line.startswith("確信度："):
            confidence_count += 1
            if confidence_count == 1:
                result["first_confidence"] = line.replace("確信度：", "").strip()
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

【出力フォーマット】
第一候補：[チャンネル名]
理由：[50字以内]
確信度：[高／中／低]

第二候補：[チャンネル名]
理由：[50字以内]
確信度：[高／中／低]

追加確認：[判断に必要な情報が不足している場合のみ質問]"""


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


def call_claude(user_message: str, image_url: str | None = None, history: list[dict] | None = None) -> str:
    """Claude APIを呼び出して分荷判定を返す"""
    # 現在のメッセージのコンテンツを組み立て
    current_content = []
    if image_url:
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
            current_content.append({
                "type": "text",
                "text": f"添付画像も参考にして判定してください。\n\n{user_message}",
            })
        except Exception as e:
            current_content.append({
                "type": "text",
                "text": f"※画像の取得に失敗しました（{e}）。テキスト情報のみで判定します。\n\n{user_message}",
            })
    else:
        current_content.append({"type": "text", "text": user_message})

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


def post_to_slack(channel_id: str, thread_ts: str, text: str) -> None:
    """Slackの指定スレッドにメッセージを返信する"""
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

    # 添付画像のURLを取得
    image_url = None
    files = event.get("files", [])
    if files:
        image_url = files[0].get("url_private")

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
        judgment_text = call_claude(user_message, image_url, history)
        print(f"[Claude応答] {judgment_text[:50]}")

        # 最初のメッセージ（スレッド履歴なし）のみmonday.comに登録
        if not history:
            management_number = generate_management_number()
            judgment_data = extract_judgment(judgment_text)
            user_id = event.get("user", "不明")
            item_name = user_message[:50] if user_message else "商品名未入力"

            reply_text = f"【管理番号：{management_number}】\n\n{judgment_text}"

            try:
                register_to_monday(management_number, item_name, judgment_data, user_id)
                print("[Monday.com登録完了]")
            except Exception as me:
                print(f"[Monday.com登録エラー] {me}")
                reply_text += f"\n\n※monday.com登録失敗: {me}"
        else:
            reply_text = judgment_text

        post_to_slack(channel_id, thread_ts, reply_text)
        print("[Slack返信完了]")
    except Exception as e:
        print(f"[エラー] {e}")
        try:
            post_to_slack(channel_id, thread_ts, f":warning: エラーが発生しました: {e}")
        except Exception as e2:
            print(f"[Slack送信エラー] {e2}")


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

    # ボット自身の発言・重複イベント・サブタイプありを無視
    if event.get("bot_id") or event.get("bot_profile") or event.get("subtype") or event_id in processed_events:
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
        judgment = call_claude(user_message, image_url)
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
