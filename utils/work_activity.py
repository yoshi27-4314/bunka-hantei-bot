"""
utils/work_activity.py - 作業ログ・削除確認・日次統計
"""

import re
from datetime import datetime

from config import get_staff_code
from services.slack import post_to_slack
from services.spreadsheet import send_to_spreadsheet
from services.monday import update_monday_item_status


# 削除確認待ちセッション: {thread_ts: {"channel_id":..,"management_number":..,"channel_name":..,"staff_id":..}}
delete_confirm_sessions = {}

# 日次統計: {staff_id: {"完了": 0, "キャンセル": 0, "削除": 0}}
daily_stats = {}


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
    pending = delete_confirm_sessions.get(thread_ts)
    if not pending:
        return False
    mn_m = re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
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
