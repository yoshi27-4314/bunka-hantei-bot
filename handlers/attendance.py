"""
handlers/attendance.py - 出退勤チャンネル
"""

import re
import httpx
from datetime import datetime

from config import get_gas_url, get_staff_code
from services.slack import post_to_slack
from services.spreadsheet import send_to_spreadsheet
from utils.commands import normalize_keyword
from utils.work_activity import daily_stats


def get_staff_break_minutes(staff_id: str) -> int:
    """スタッフマスターから標準休憩時間（分）を取得する。取得失敗時は60分を返す"""
    try:
        resp = httpx.get(get_gas_url(), params={"type": "staff"}, timeout=10)
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
        pm = re.match(
            rf'{re.escape(name)}\s+(\d{{1,2}}):(\d{{2}})[~\-～](\d{{1,2}}):(\d{{2}})',
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
    m = re.match(r'(\d{1,2}):(\d{2})[~\-～](\d{1,2}):(\d{2})', text)
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
