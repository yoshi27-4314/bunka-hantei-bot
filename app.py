"""
AI分荷判定Bot - Step 1
Slack Events API → Claude API → Slack返信
"""

import os
import base64
import threading
import httpx
from flask import Flask, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

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
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    response = httpx.get(image_url, headers=headers, timeout=30, follow_redirects=True)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if content_type not in supported:
        content_type = "image/jpeg"

    image_data = base64.standard_b64encode(response.content).decode("utf-8")
    return image_data, content_type


def call_claude(user_message: str, image_url: str | None = None) -> str:
    """Claude APIを呼び出して分荷判定を返す"""
    content = []

    if image_url:
        try:
            image_data, media_type = fetch_image_as_base64(image_url)
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data,
                },
            })
            content.append({
                "type": "text",
                "text": f"添付画像も参考にして判定してください。\n\n{user_message}",
            })
        except Exception as e:
            content.append({
                "type": "text",
                "text": f"※画像の取得に失敗しました（{e}）。テキスト情報のみで判定します。\n\n{user_message}",
            })
    else:
        content.append({"type": "text", "text": user_message})

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def post_to_slack(channel_id: str, thread_ts: str, text: str) -> None:
    """Slackの指定スレッドにメッセージを返信する"""
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
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
    thread_ts = event.get("thread_ts") or event.get("ts")
    user_message = event.get("text", "")

    print(f"[処理開始] channel={channel_id} ts={thread_ts} message={user_message[:30]}")
    print(f"[ENV確認] ANTHROPIC_API_KEY={'設定済み' if ANTHROPIC_API_KEY else '未設定'} SLACK_BOT_TOKEN={'設定済み' if SLACK_BOT_TOKEN else '未設定'}")

    # 添付画像のURLを取得
    image_url = None
    files = event.get("files", [])
    if files:
        image_url = files[0].get("url_private")

    try:
        print("[Claude API呼び出し中...]")
        judgment = call_claude(user_message, image_url)
        print(f"[Claude応答] {judgment[:50]}")
        post_to_slack(channel_id, thread_ts, judgment)
        print("[Slack返信完了]")
    except Exception as e:
        print(f"[エラー] {e}")
        try:
            post_to_slack(channel_id, thread_ts, f":warning: エラーが発生しました: {e}")
        except Exception as e2:
            print(f"[Slack送信エラー] {e2}")


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
