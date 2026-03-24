"""
handlers/konpo.py - 梱包出荷チャンネル（黒田官兵衛）
"""

import os
import re
import httpx
import base64
from datetime import datetime

from config import (
    get_anthropic_client, get_slack_token, get_staff_code,
    MONDAY_BOARD_ID, TSUHAN_COMMUNITY_CHANNEL_ID,
    CHANNEL_NAMES, CANCEL_WORDS, CARRIER_MAP, CARRIER_MENU,
)
from services.slack import post_to_slack
from services.monday import get_item_from_monday, update_monday_columns
from services.spreadsheet import send_to_spreadsheet
from handlers.satsuei import extract_management_number_from_image
from utils.commands import normalize_keyword, handle_free_comment
from utils.work_activity import (
    log_work_activity, handle_delete_step1, handle_delete_step2,
)


# 梱包セッション: {thread_ts: {...}}
konpo_sessions = {}


def extract_tracking_number_from_image(image_url: str, carrier: str) -> str:
    """送り状ラベル写真から追跡番号をOCR抽出する"""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    try:
        resp = httpx.get(image_url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        image_b64 = base64.standard_b64encode(resp.content).decode()
    except Exception as e:
        print(f"[送り状画像取得エラー] {e}")
        return ""
    try:
        _client = get_anthropic_client()
        if not _client:
            return ""
        result = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": (
                        f"この{carrier}の送り状ラベルから追跡番号（伝票番号）のみを抽出してください。"
                        "数字のみで答えてください。見つからない場合は「なし」と答えてください。"
                    )},
                ],
            }],
        )
        answer = result.content[0].text.strip()
        return "" if answer == "なし" else answer
    except Exception as e:
        print(f"[追跡番号OCRエラー] {e}")
        return ""


def _notify_tsuhan_community(management_number: str, item_name: str,
                             carrier: str, tracking_number: str,
                             monday_board_id: str) -> None:
    """通販業務_共有コミュニティに出荷完了通知を投稿してピン留めする"""
    token = get_slack_token()
    if not token:
        return
    board_url = f"https://monday.com/boards/{monday_board_id}"
    tracking_line = f"\n📮 追跡番号：*{tracking_number}*" if tracking_number else ""
    text = (
        "<!channel>\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🚚 *出荷手配が完了しました*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号：*{management_number}*\n"
        f"📋 アイテム名：{item_name}\n"
        f"🏢 運送会社：{carrier}"
        f"{tracking_line}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "✅ *対応をお願いします*\n"
        f"Monday.com のステータスを *「出荷待ち」* に変更してください。\n\n"
        f"<{board_url}|📋 Monday.com ボードを開く>\n"
        "━━━━━━━━━━━━━━━━"
    )
    try:
        url = "https://slack.com/api/chat.postMessage"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        resp = httpx.post(url, headers=headers, json={
            "channel": TSUHAN_COMMUNITY_CHANNEL_ID,
            "text": text,
            "username": "黒田官兵衛",
        }, timeout=10)
        result = resp.json()
        if not result.get("ok"):
            print(f"[通販コミュニティ通知エラー] {result.get('error')}")
            return
        # ピン留め
        msg_ts = result.get("ts")
        if msg_ts:
            httpx.post("https://slack.com/api/pins.add", headers=headers, json={
                "channel": TSUHAN_COMMUNITY_CHANNEL_ID,
                "timestamp": msg_ts,
            }, timeout=10)
            print(f"[通販コミュニティ通知] 投稿・ピン留め完了 ts={msg_ts}")
    except Exception as e:
        print(f"[通販コミュニティ通知例外] {e}")


def _finish_shipping(channel_id, thread_ts, user_id, management_number, carrier, tracking_number,
                     is_old_board: bool = False, monday_board_id: str = "", item_name: str = ""):
    """出荷手配完了の共通処理"""
    tracking_line = f"\n📮 追跡番号\n　*{tracking_number}*" if tracking_number else ""
    post_to_slack(channel_id, thread_ts,
        "━━━━━━━━━━━━━━━━\n"
        "🚚 *出荷手配完了*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{management_number}*\n\n"
        f"🏢 運送会社\n"
        f"　{carrier}"
        f"{tracking_line}",
        mention_user=user_id, bot_role="konpo")
    if is_old_board:
        # 旧ボード品はMonday.com列構造が異なるため更新をスキップ → 通販コミュニティに通知
        print(f"[旧ボード品] Monday.com更新スキップ: {management_number}")
        _notify_tsuhan_community(management_number, item_name, carrier, tracking_number,
                                 monday_board_id or MONDAY_BOARD_ID)
    else:
        try:
            monday_cols = {
                "status": {"label": "出荷待ち"},
                "carrier": carrier,
                "shukka_date": {"date": datetime.now().strftime("%Y-%m-%d")},
            }
            if tracking_number:
                monday_cols["tracking_number"] = tracking_number
            update_monday_columns(management_number, monday_cols)
        except Exception as e:
            print(f"[Monday.com出荷済み更新エラー] {e}")
    try:
        send_to_spreadsheet({
            "action":          "shipping_update",
            "kanri_bango":     management_number,
            "carrier":         carrier,
            "tracking_number": tracking_number,
            "staff_id":        get_staff_code(user_id),
            "timestamp":       datetime.now().strftime("%Y/%m/%d %H:%M"),
        })
    except Exception as e:
        print(f"[スプレッドシート出荷更新エラー] {e}")


def handle_konpo_channel(event: dict) -> None:
    """梱包出荷チャンネルのイベントを処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_id = event.get("user", "")
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]
    text = normalize_keyword(event.get("text", ""))
    is_new_post = not event.get("thread_ts")

    # ── 新規投稿 ──────────────────────────────────────────
    if is_new_post:
        # 後日発送の送り状後入力: 「管理番号 運送会社 追跡番号」
        delayed_m = re.match(r'(\d{4}(?:[VGME]\d{4}|-\d{4}|[A-Z]{2}\d{3}))\s+(佐川|アート|西濃)\S*\s+(\S+)', text)
        if delayed_m:
            mn, carrier_kw, tracking = delayed_m.group(1), delayed_m.group(2), delayed_m.group(3)
            carrier_name = {"佐川": "佐川急便", "アート": "アートデリバリー", "西濃": "西濃運輸"}.get(carrier_kw, carrier_kw)
            delayed_item = get_item_from_monday(mn)
            _finish_shipping(channel_id, current_ts, user_id, mn, carrier_name, tracking,
                             is_old_board=delayed_item.get("is_old_board", False),
                             monday_board_id=delayed_item.get("monday_board_id", ""),
                             item_name=delayed_item.get("monday_name", ""))
            return

        # 通常の梱包開始
        text_mn = re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4}|[A-Z]{2}\d{3})', text)
        if not text_mn and not image_urls:
            print(f"[梱包CH無視] 管理番号なし・画像なし channel={channel_id} text={text[:30]!r}")
            return
        if text_mn:
            management_number = text_mn.group(0)
        else:
            post_to_slack(channel_id, current_ts, "🔍 管理番号を読み取り中...", bot_role="konpo")
            management_number = extract_management_number_from_image(image_urls[0])
            if not management_number:
                post_to_slack(channel_id, current_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "⚠️ *読み取りエラー*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    "管理番号を確認できませんでした。\n\n"
                    "もう一度送信してください。",
                    bot_role="konpo")
                return

        item_data = get_item_from_monday(management_number)
        if not item_data:
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *該当なし*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"*{management_number}* は確認できません。\n\n"
                "管理番号を確認して再送信してください。",
                bot_role="konpo")
            return

        kw = item_data.get("internal_keyword", "")
        size_m = re.search(r'/[A-Z]+(\d+)/', kw)
        size = size_m.group(1) if size_m else "不明"
        is_old_board = item_data.get("is_old_board", False)

        konpo_sessions[current_ts] = {
            "management_number": management_number,
            "item_name":         item_data.get("monday_name", ""),
            "size":              size,
            "packed":            False,
            "carrier":           None,
            "waiting_label":     False,
            "start_time":        datetime.now(),
            "is_old_board":      is_old_board,
            "monday_board_id":   item_data.get("monday_board_id", MONDAY_BOARD_ID),
        }

        if is_old_board:
            # 旧ボード品：アイテム名と棚番を表示（サイズ・チャンネル等は手動確認）
            shelf = ""
            for col_id, col_val in item_data.items():
                if col_id in ("monday_name", "is_old_board", "monday_item_id", "monday_board_id") or not col_val:
                    continue
                if col_val == management_number:
                    continue
                if re.match(r'^([A-Z][A-Za-z\d\s横奥]*|\d{1,2}[階F]?|倉庫[外奥]?)$', col_val.strip()):
                    shelf = col_val
                    break
            shelf_line = f"📍 棚番\n　{shelf}\n\n" if shelf else ""
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "📦 *梱包情報確認（旧ボード）*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"🔖 管理番号\n"
                f"　*{management_number}*\n\n"
                f"📋 アイテム名\n"
                f"　{item_data.get('monday_name', '---')}\n\n"
                f"{shelf_line}"
                "⚠️ サイズ・チャンネル等は手動で確認してください。\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "梱包が完了したら `梱包完了` と入力してください。",
                mention_user=user_id, bot_role="konpo")
        else:
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "📦 *梱包情報確認*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"🔖 管理番号\n"
                f"　*{management_number}*\n\n"
                f"📐 梱包サイズ\n"
                f"　{size}サイズ\n\n"
                f"📺 判定チャンネル\n"
                f"　{item_data.get('hantei_channel', '')}\n\n"
                f"💰 予想販売価格\n"
                f"　{item_data.get('yosou_kakaku', '')}\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "梱包が完了したら `梱包完了` と入力してください。",
                mention_user=user_id, bot_role="konpo")
        return

    # ── スレッド内 ────────────────────────────────────────
    # 削除確認待ちの処理
    if handle_delete_step2(channel_id, thread_ts, user_id, text):
        return

    session = konpo_sessions.get(thread_ts)
    if not session:
        # 削除コマンド（セッションなし）
        if text == "削除":
            handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["konpo"], "konpo")
        else:
            print(f"[梱包CH無視] スレッド内・セッションなし channel={channel_id} text={text[:30]!r}")
        return
    management_number = session["management_number"]

    # キャンセル・中断
    if text in CANCEL_WORDS:
        log_work_activity(CHANNEL_NAMES["konpo"], management_number,
                          get_staff_code(user_id), "キャンセル", session.get("start_time"))
        del konpo_sessions[thread_ts]
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "⏹️ *梱包作業キャンセル*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号\n"
            f"　*{management_number}*\n\n"
            "梱包作業をキャンセルしました。",
            mention_user=user_id, bot_role="konpo")
        return

    # 削除コマンド
    if text == "削除":
        handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["konpo"], "konpo")
        return

    # ① 梱包完了 → 運送会社選択へ
    if text in ("梱包完了", "梱包") and not session["packed"]:
        session["packed"] = True
        konpo_sessions[thread_ts] = session
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "✅ *梱包完了確認*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"{CARRIER_MENU}",
            mention_user=user_id, bot_role="konpo")
        try:
            update_monday_columns(management_number, {
                "status": {"label": "梱包作業"},
                "konpo_tantosha": get_staff_code(user_id),
                "konpo_date": {"date": datetime.now().strftime("%Y-%m-%d")},
            })
        except Exception as e:
            print(f"[Monday.com梱包済み更新エラー] {e}")
        return

    # ② 運送会社選択（1〜5）
    if session["packed"] and not session["carrier"] and text in CARRIER_MAP:
        carrier = CARRIER_MAP[text]
        session["carrier"] = carrier
        konpo_sessions[thread_ts] = session

        if text == "4":  # 直接引き取り
            _finish_shipping(channel_id, thread_ts, user_id, management_number, carrier, "",
                             is_old_board=session.get("is_old_board", False),
                             monday_board_id=session.get("monday_board_id", ""),
                             item_name=session.get("item_name", ""))
            log_work_activity(CHANNEL_NAMES["konpo"], management_number,
                              get_staff_code(user_id), "完了", session.get("start_time"))
            del konpo_sessions[thread_ts]
        elif text == "5":  # 後日発送
            post_to_slack(channel_id, thread_ts,
                f"📋 *{management_number}* を「梱包済み（発送待ち）」として保留しました。\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📮 *後日発送時の入力方法*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "このチャンネルに新規メッセージで投稿してください。\n\n"
                "*入力形式*\n"
                "`管理番号 運送会社 伝票番号`\n\n"
                "*サンプル*\n"
                "```\n"
                "2603-0001 佐川 123456789012\n"
                "2603-0002 アート 0987654321\n"
                "2603-0003 西濃 111222333444\n"
                "```\n\n"
                "*運送会社の入力方法*\n"
                "• 佐川急便 → `佐川`\n"
                "• アートデリバリー → `アート`\n"
                "• 西濃運輸 → `西濃`\n\n"
                "⚠️ *注意事項*\n"
                "• スペースで区切ってください（全角スペース不可）\n"
                "• 伝票番号は数字のみ（ハイフン不要）\n"
                "• 管理番号・運送会社・伝票番号の順番を守ってください",
                mention_user=user_id, bot_role="konpo")
            del konpo_sessions[thread_ts]
        else:  # 佐川・アート・西濃
            session["waiting_label"] = True
            konpo_sessions[thread_ts] = session
            post_to_slack(channel_id, thread_ts,
                f"📸 *{carrier}* の\n"
                "送り状ラベルの写真を送ってください。",
                mention_user=user_id, bot_role="konpo")
        return

    # ③ 送り状ラベル写真 → OCRで追跡番号抽出
    if session.get("waiting_label") and image_urls:
        carrier = session["carrier"]
        post_to_slack(channel_id, thread_ts, "🔍 追跡番号を読み取り中...", bot_role="konpo")
        tracking_number = extract_tracking_number_from_image(image_urls[0], carrier)
        if not tracking_number:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *読み取りエラー*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "追跡番号を読み取れませんでした。\n\n"
                "もう一度写真を送ってください。",
                mention_user=user_id, bot_role="konpo")
            return
        _finish_shipping(channel_id, thread_ts, user_id, management_number, carrier, tracking_number,
                         is_old_board=session.get("is_old_board", False),
                         monday_board_id=session.get("monday_board_id", ""),
                         item_name=session.get("item_name", ""))
        log_work_activity(CHANNEL_NAMES["konpo"], management_number,
                          get_staff_code(user_id), "完了", session.get("start_time"))
        del konpo_sessions[thread_ts]
        return

    # どのコマンドにもマッチしなかった場合 → フリーコメント
    handle_free_comment(channel_id, thread_ts, event)
