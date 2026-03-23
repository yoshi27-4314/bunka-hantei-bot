"""
utils/checklist.py - 動作確認チェックリスト
"""

import re
import httpx

from config import CONDITION_MAP, get_slack_token
from services.slack import post_to_slack


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
