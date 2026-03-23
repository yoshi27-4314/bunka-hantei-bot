"""
handlers/kintai.py - 勤怠連絡チャンネル（サイレント記録）
"""

from datetime import datetime

from config import get_staff_code
from services.spreadsheet import send_to_spreadsheet


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
