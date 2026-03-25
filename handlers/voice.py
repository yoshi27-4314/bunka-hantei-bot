"""
handlers/voice.py - 新しい声ポイント制度

投稿方法:
  1. Slack DMで「新しい声 ○○」
  2. Webフォーム /voice から投稿

通知3層:
  1. DM（本人）— 全経過
  2. #新しい声（浅野専用）— 全件
  3. #社内連絡 — 毎朝8:55まとめ（動きある日のみ）

ポイント: 投稿+1 / 受理+1 / 採用+1 / 実装+3 / 優秀+15
1pt = 100円 / 四半期支払い（3/6/9/12月給与）
"""

import os
import json
import re
import httpx
from datetime import datetime

from config import get_anthropic_client, get_staff_code, STAFF_MAP, ASANO_USER_ID
from services.slack import post_to_slack, send_dm
from services.monday import monday_graphql

# 新しい声ボードID（環境変数で設定）
VOICE_BOARD_ID = os.environ.get("VOICE_BOARD_ID", "")

# チャンネルID
VOICE_CHANNEL_ID = "C0AMM3N5Z7F"       # #新しい声（浅野専用）
SHANAI_CHANNEL_ID = "C0ALAA21S57"       # #社内連絡（Slack）

# Google Chat Webhook URL（環境変数で設定）
GCHAT_WEBHOOKS = {
    "アスカラ｜現場作業": "GCHAT_WEBHOOK_ASKARA",
    "業務支援チーム｜総務・経理": "GCHAT_WEBHOOK_SOUMU",
}

# ポイント定義
POINTS = {
    "投稿": 1,
    "受理": 1,
    "採用": 1,
    "実装": 3,
    "優秀": 15,
    "見送り": 0,
    "却下": -1,
}

# ステータスラベル
STATUS_LABELS = {
    "受理": {"label": "受理", "color": "#0086c0"},
    "見送り": {"label": "見送り", "color": "#c4c4c4"},
    "却下": {"label": "却下", "color": "#e2445c"},
    "採用": {"label": "採用", "color": "#00c875"},
    "実装": {"label": "実装", "color": "#9cd326"},
    "優秀": {"label": "優秀", "color": "#fdab3d"},
}


def send_to_gchat(text: str) -> None:
    """全てのGoogle Chatスペースにメッセージを送信する"""
    for name, env_key in GCHAT_WEBHOOKS.items():
        url = os.environ.get(env_key, "")
        if not url:
            print(f"[Google Chat] {env_key} が未設定、スキップ: {name}")
            continue
        try:
            resp = httpx.post(url, json={"text": text}, timeout=10)
            if resp.status_code == 200:
                print(f"[Google Chat] {name} に送信完了")
            else:
                print(f"[Google Chat] {name} 送信エラー: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            print(f"[Google Chat] {name} 送信エラー: {e}")


def _classify_category(content: str) -> str:
    """AIでカテゴリを自動判別する（要望/アイデア/相談）"""
    try:
        client = get_anthropic_client()
        if not client:
            return "要望"
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            system='投稿内容を「要望」「アイデア」「相談」のいずれか1語のみで分類してください。それ以外の文字は出力しないこと。',
            messages=[{"role": "user", "content": content}],
        )
        category = response.content[0].text.strip()
        if category in ("要望", "アイデア", "相談"):
            return category
        return "要望"
    except Exception as e:
        print(f"[新しい声カテゴリ判別エラー] {e}")
        return "要望"


def _create_voice_item(staff_name: str, content: str, category: str) -> str:
    """Monday.comの気づきボードにアイテムを作成する。item_idを返す。"""
    if not VOICE_BOARD_ID:
        print("[新しい声] VOICE_BOARD_ID が未設定")
        return ""

    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    item_name = f"[{category}] {content[:30]}"

    column_values = json.dumps({
        "text_mkr1y71d": staff_name,       # 投稿者
        "text_mkr1y72d": content,           # 内容
        "text_mkr1y73d": category,          # カテゴリ
        "text_mkr1y74d": now,               # 投稿日時
        "text_mkr1y75d": "投稿",            # ステータス
        "numbers_mkr1z": POINTS["投稿"],    # ポイント
    })

    query = '''
    mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {
        create_item(
            board_id: $board_id,
            item_name: $item_name,
            column_values: $column_values
        ) {
            id
        }
    }
    '''
    try:
        result = monday_graphql(query, {
            "board_id": VOICE_BOARD_ID,
            "item_name": item_name,
            "column_values": column_values,
        })
        item_id = result.get("data", {}).get("create_item", {}).get("id", "")
        print(f"[新しい声] Monday.com登録完了 item_id={item_id}")
        return item_id
    except Exception as e:
        print(f"[新しい声] Monday.com登録エラー: {e}")
        return ""


def _notify_voice_channel(staff_name: str, content: str, category: str, anonymous: bool = True) -> None:
    """#新しい声チャンネル（浅野専用）に通知する"""
    display_name = "匿名スタッフ" if anonymous else staff_name
    text = (
        "━━━━━━━━━━━━━━━━\n"
        "💡 *新しい声が届きました*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"📝 カテゴリ：*{category}*\n\n"
        f"👤 投稿者：{display_name}\n\n"
        f"💬 内容：\n{content}\n\n"
        "─────────────────────────\n\n"
        "対応コマンド（スレッドで返信）：\n"
        "　`受理` — 受け付ける（+1pt）\n"
        "　`見送り` — 今回は見送り（±0pt）\n"
        "　`却下` — 却下する（-1pt）\n"
        "　`採用` — 採用する（+1pt）\n"
        "　`実装` — 実装完了（+3pt）\n"
        "　`優秀` — 優秀な提案（+15pt）\n\n"
        "━━━━━━━━━━━━━━━━"
    )
    try:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not token:
            return
        import httpx
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        resp = httpx.post("https://slack.com/api/chat.postMessage",
            headers=headers,
            json={"channel": VOICE_CHANNEL_ID, "text": text},
            timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print(f"[新しい声通知エラー] {data.get('error')}")
    except Exception as e:
        print(f"[新しい声通知エラー] {e}")


def submit_voice(staff_name: str, content: str, user_id: str = "") -> dict:
    """新しい声を投稿する（Slack/Webフォーム共通のエントリポイント）"""
    # カテゴリ自動判別
    category = _classify_category(content)

    # Monday.com登録
    item_id = _create_voice_item(staff_name, content, category)

    # #新しい声チャンネルに通知（浅野専用、投稿者名を表示）
    _notify_voice_channel(staff_name, content, category, anonymous=False)

    # 投稿者本人にDM通知（Slackユーザーの場合）
    if user_id:
        send_dm(user_id,
            "━━━━━━━━━━━━━━━━\n"
            "💡 *新しい声を受け付けました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"📝 カテゴリ：*{category}*\n"
            f"💬 内容：{content[:100]}\n"
            f"⭐ ポイント：+{POINTS['投稿']}pt（投稿）\n\n"
            "進捗があればDMでお知らせします。\n"
            "━━━━━━━━━━━━━━━━")

    return {"ok": True, "category": category, "item_id": item_id}


def handle_voice_command(text: str, user_id: str) -> bool:
    """Slack DMで「新しい声 ○○」を処理する。処理した場合はTrueを返す。"""
    if not text:
        return False

    m = re.match(r'^(?:新しい声|あたらしいこえ)\s+(.+)', text.strip(), re.DOTALL)
    if not m:
        return False

    content = m.group(1).strip()
    if not content:
        return False

    staff_name = get_staff_code(user_id)
    submit_voice(staff_name, content, user_id)
    return True


def handle_voice_management(text: str, channel_id: str, thread_ts: str,
                            user_id: str) -> bool:
    """#新しい声チャンネルで浅野がステータス変更する。
    スレッドで「受理」「見送り」「却下」「採用」「実装」「優秀」と返信。"""
    if channel_id != VOICE_CHANNEL_ID:
        return False
    if user_id != ASANO_USER_ID:
        return False

    normalized = text.strip()
    if normalized not in STATUS_LABELS:
        return False

    status = normalized
    points = POINTS.get(status, 0)

    # ステータス変更通知を#新しい声チャンネルのスレッドに投稿
    point_str = f"+{points}" if points >= 0 else str(points)
    reply = (
        "━━━━━━━━━━━━━━━━\n"
        f"📋 *ステータス変更：{status}*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"⭐ ポイント：{point_str}pt\n\n"
        "━━━━━━━━━━━━━━━━"
    )
    post_to_slack(channel_id, thread_ts, reply, bot_role="bunika")
    return True


def get_daily_summary() -> str:
    """本日の新しい声の活動サマリーを生成する（#社内連絡向け）。
    活動がなければ空文字を返す。"""
    if not VOICE_BOARD_ID:
        return ""

    today = datetime.now().strftime("%Y/%m/%d")
    query = '''
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 50) {
                items {
                    name
                    column_values {
                        id
                        text
                    }
                }
            }
        }
    }
    '''
    try:
        result = monday_graphql(query, {"board_id": VOICE_BOARD_ID})
        items = result.get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
    except Exception as e:
        print(f"[日次サマリーエラー] {e}")
        return ""

    # 今日の活動を抽出
    today_items = []
    for item in items:
        cols = {c["id"]: c["text"] for c in item.get("column_values", [])}
        timestamp = cols.get("text_mkr1y74d", "")
        if timestamp.startswith(today):
            today_items.append({
                "name": item["name"],
                "status": cols.get("text_mkr1y75d", "投稿"),
                "staff": cols.get("text_mkr1y71d", ""),
                "points": cols.get("numbers_mkr1z", "0"),
            })

    if not today_items:
        return ""

    lines = [
        "━━━━━━━━━━━━━━━━",
        "💡 *新しい声　本日のまとめ*",
        "━━━━━━━━━━━━━━━━",
        "",
    ]
    for item in today_items:
        status = item["status"]
        # 匿名ルール：投稿・受理は匿名、実装・優秀は実名
        if status in ("実装", "優秀"):
            lines.append(f"　• {status}：{item['staff']}さん — {item['name']}（+{item['points']}pt）")
        else:
            lines.append(f"　• {status}：{item['name']}")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")

    return "\n".join(lines)
