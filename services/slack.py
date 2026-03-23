"""
services/slack.py - Slack API通信（DM送信・メッセージ投稿・チャンネル別Bot role判定）
"""

import os
import httpx

from config import get_slack_token, BOT_NAMES, BOT_PERSONA


def send_dm(user_id: str, text: str) -> bool:
    """指定ユーザーにDMを送信する"""
    token = get_slack_token()
    if not token:
        return False
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    # DMチャンネルを開く
    open_resp = httpx.post("https://slack.com/api/conversations.open",
        headers=headers, json={"users": user_id}, timeout=10)
    open_data = open_resp.json()
    if not open_data.get("ok"):
        print(f"[DM] conversations.open失敗: {open_data.get('error')}")
        return False
    dm_channel = open_data["channel"]["id"]
    # DMを送信
    msg_resp = httpx.post("https://slack.com/api/chat.postMessage",
        headers=headers, json={"channel": dm_channel, "text": text}, timeout=10)
    msg_data = msg_resp.json()
    if not msg_data.get("ok"):
        print(f"[DM] chat.postMessage失敗: {msg_data.get('error')}")
        return False
    print(f"[DM] {user_id} に送信完了")
    return True


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
