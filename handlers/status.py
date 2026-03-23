"""
handlers/status.py - ステータス確認チャンネル（ステータス松本）
"""

import re
from datetime import date

from services.slack import post_to_slack
from services.monday import get_item_from_monday
from handlers.satsuei import extract_management_number_from_image
from utils.commands import normalize_keyword


def handle_status_channel(event: dict) -> None:
    """ステータス確認チャンネルのイベントを処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_id = event.get("user", "")
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]
    text = normalize_keyword(event.get("text", ""))

    text_mn = re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
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
