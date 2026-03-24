"""
handlers/bunika.py - 分荷判定チャンネルのコマンド処理
"""

import re
from datetime import datetime

from config import (
    ASANO_USER_ID, MONDAY_BOARD_ID,
    BOT_PERSONA, MATOME_CHANNELS, TSUHAN_CHANNELS,
    CONDITION_MAP, get_staff_code,
)
from services.slack import send_dm, post_to_slack
from services.claude import call_claude
from services.monday import (
    generate_management_number, register_to_monday,
    cancel_monday_item, search_inventory,
    update_monday_columns,
)
from services.spreadsheet import send_to_spreadsheet
from utils.commands import normalize_keyword, normalize_channel
from utils.slack_thread import (
    fetch_thread_messages, get_judgment_from_thread,
    get_confirmation_from_thread,
)


def _handle_zaiko_search(keyword: str, channel_id: str, thread_ts: str, event: dict) -> None:
    """在庫検索コマンドを処理する"""
    user_id = event.get("user", "")
    results = search_inventory(keyword)
    persona = BOT_PERSONA["status"]
    if not results:
        msg = persona["search_none"].format(keyword=keyword)
        post_to_slack(channel_id, thread_ts, f"🔍 {msg}", mention_user=user_id, bot_role="status")
        return

    header = persona["search_found"].format(keyword=keyword, count=len(results))
    monday_url = f"https://monday.com/boards/{MONDAY_BOARD_ID}"
    lines = [
        f"{header}",
        "━━━━━━━━━━━━━━━━",
    ]
    for i, r in enumerate(results[:10], 1):
        kanri = r["kanri_bango"] or "番号なし"
        status = r["status"] or "不明"
        zaiko = r["zaiko_kikan"] or "─"
        channel = r["channel"] or "─"
        line = (
            f"*{i}. {r['name']}*\n\n"
            f"　🔖 管理番号　：`{kanri}`\n\n"
            f"　📺 チャンネル：{channel}\n\n"
            f"　📊 ステータス：{status}\n\n"
            f"　📅 在庫期間　：{zaiko}\n\n"
            f"　<{monday_url}|📷 Monday.comで詳細・画像を確認>\n\n"
            "─────────────────"
        )
        lines.append(line)

    if len(results) > 10:
        lines.append(f"_他 {len(results) - 10} 件はMonday.comで確認できます。_")

    post_to_slack(channel_id, thread_ts, "\n".join(lines), mention_user=user_id, bot_role="status")


def _complete_kakutei(kakutei_channel: str, judgment: dict, user_id: str,
                      channel_id: str, thread_ts: str, event: dict,
                      with_management_number: bool = True,
                      send_reply: bool = True) -> None:
    """分荷確定の共通処理（管理番号発行・スプレッドシート転記・Slack返信）"""
    # 管理番号発行（通販チャンネル かつ with_management_number=True の場合のみ）
    management_number = ""
    if with_management_number and kakutei_channel in TSUHAN_CHANNELS:
        management_number = generate_management_number()
        print(f"[管理番号発行] {management_number} (チャンネル:{kakutei_channel})")

    # 分荷作業時間を計算
    sakugyou_jikan = 0
    try:
        post_ts = float(thread_ts)
        confirm_ts = float(event.get("ts", thread_ts))
        sakugyou_jikan = max(0, int((confirm_ts - post_ts) / 60))
        print(f"[作業時間] {sakugyou_jikan}分")
    except Exception as e:
        print(f"[作業時間計算エラー] {e}")

    # スプレッドシートに転記
    payload = {
        "kanri_bango":         management_number,
        "kakutei_channel":     kakutei_channel,
        "first_channel":       judgment.get("first_channel", ""),
        "second_channel":      judgment.get("second_channel", ""),
        "item_name":           judgment.get("item_name", ""),
        "maker":               judgment.get("maker", ""),
        "model_number":        judgment.get("model_number", ""),
        "condition":           judgment.get("condition", ""),
        "predicted_price":     judgment.get("predicted_price", ""),
        "start_price":         judgment.get("start_price", ""),
        "target_price":        judgment.get("target_price", ""),
        "inventory_period":    judgment.get("inventory_period", ""),
        "inventory_deadline":  judgment.get("inventory_deadline", ""),
        "score":               judgment.get("first_score", ""),
        "storage_cost":        judgment.get("storage_cost", ""),
        "packing_cost":        judgment.get("packing_cost", ""),
        "expected_roi":        judgment.get("expected_roi", ""),
        "internal_keyword":    judgment.get("internal_keyword", ""),
        "staff_id":            get_staff_code(user_id),
        "sakugyou_jikan":      sakugyou_jikan,
        "timestamp":           datetime.now().strftime("%Y/%m/%d %H:%M"),
    }
    try:
        send_to_spreadsheet(payload)
    except Exception as se:
        print(f"[スプレッドシート転記エラー] {se}")

    # 通販チャンネル かつ 管理番号あり → Monday.com登録
    if management_number:
        try:
            item_name = judgment.get("item_name") or kakutei_channel
            register_to_monday(management_number, item_name, judgment, user_id, sakugyou_jikan, kakutei_channel=kakutei_channel)
            print("[Monday.com登録完了]")
        except Exception as me:
            print(f"[Monday.com登録エラー] {me}")

    # Slack確定返信（send_reply=Falseの場合は呼び出し元が返信を担当）
    if send_reply:
        persona = BOT_PERSONA["bunika"]
        if management_number:
            reply = persona["confirm"].format(channel=kakutei_channel, kanri=management_number)
        else:
            reply = persona["confirm_only"].format(channel=kakutei_channel)
        post_to_slack(channel_id, thread_ts, reply, mention_user=user_id)

    # 高額案件メンション（目標価格30,000円以上）
    try:
        target_price_val = int(str(judgment.get("target_price", "0")).replace(",", ""))
    except (ValueError, TypeError):
        target_price_val = 0
    if target_price_val >= 30000:
        post_to_slack(channel_id, thread_ts,
            f"<@{ASANO_USER_ID}> 高額案件の確定が入りました。\n"
            f"予想販売価格：¥{target_price_val:,}\n"
            f"チャンネル：{kakutei_channel}\n"
            f"担当：<@{user_id}>"
        )


def _handle_matome_choice(choice: str, kakutei_channel: str, channel_id: str,
                          thread_ts: str, event: dict) -> None:
    """まとめ売り選択（1/2/3）を処理する"""
    user_id = event.get("user", "")
    judgment = get_judgment_from_thread(channel_id, thread_ts)

    if choice == '1':
        # まとめ保管（管理番号なし）← デフォルト選択肢
        _complete_kakutei(kakutei_channel, judgment, user_id, channel_id, thread_ts, event, with_management_number=False, send_reply=False)
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "📦 *まとめ保管として記録しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "同カテゴリのまとめ対象商品と一緒に保管してください。\n"
            "まとめ販売が決まった時点で改めて管理番号を発行します。",
            mention_user=user_id
        )

    elif choice == '2':
        # 個別に管理番号を発行して通常確定
        _complete_kakutei(kakutei_channel, judgment, user_id, channel_id, thread_ts, event, with_management_number=True)

    elif choice == '3':
        # 保留・浅野に相談
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "⏸️ *保留にしました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"<@{ASANO_USER_ID}> 確定方法について相談があります。\n"
            f"担当：<@{user_id}>"
        )


def _handle_command(cmd_type: str, cmd_option: str, channel_id: str, thread_ts: str, event: dict) -> None:
    """コマンド（OK確定/相談/確定/再判定/保留）を処理する"""
    user_id = event.get("user", "不明")

    # ── 新フロー：「OK」でAI自動判定を確定 ──
    if cmd_type == 'ok_confirm':
        judgment = get_judgment_from_thread(channel_id, thread_ts)
        # 新フォーマット（auto_channel）を優先、なければ旧フォーマット（first_channel）
        kakutei_channel = judgment.get("auto_channel") or judgment.get("first_channel", "")
        if not kakutei_channel:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *判定データなし*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "判定データが見つかりませんでした。\n先に分荷判定を実行してください。")
            return
        kakutei_channel = normalize_channel(kakutei_channel)

        # 承認待ちの場合、スタッフのOKは受け付けない（浅野さんのみ）
        if judgment.get("needs_approval") and user_id != ASANO_USER_ID:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⏳ *承認待ちです*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "この商品は浅野の承認が必要です。\nしばらくお待ちください。",
                mention_user=user_id)
            return

        # まとめ売り系チャンネルは選択肢を表示
        MATOME_CHANNELS_LOCAL = {"eBayまとめ", "ヤフオクまとめ", "ロット販売"}
        if kakutei_channel in MATOME_CHANNELS_LOCAL:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                f"📦 *{kakutei_channel}*（まとめ売り）で判定されました\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "どちらで進めますか？\n\n"
                "1️⃣  まとめ保管する（管理番号なし）← *ほとんどの場合はこちら*\n\n"
                "2️⃣  個別に管理番号を発行して確定する\n\n"
                "3️⃣  保留にして浅野に相談する\n\n"
                f"`1` `2` `3` のいずれかを返信してください。\n\n"
                f"_[まとめ選択待ち:{kakutei_channel}]_",
                mention_user=user_id)
            return

        # 通常確定
        _complete_kakutei(kakutei_channel, judgment, user_id, channel_id, thread_ts, event)
        return

    # ── 新フロー：「相談」で浅野さんに通知 ──
    if cmd_type == 'soudan':
        judgment = get_judgment_from_thread(channel_id, thread_ts)
        item_name = judgment.get("item_name", "不明な商品")
        auto_channel = judgment.get("auto_channel") or judgment.get("first_channel", "不明")
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "💬 *相談リクエスト*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"<@{ASANO_USER_ID}> スタッフから判定について相談があります。\n\n"
            f"商品：{item_name}\n"
            f"AI判定：{auto_channel}\n"
            f"担当：<@{user_id}>\n\n"
            "確認後、このスレッドで `OK` または変更指示をお願いします。")
        return

    # 通販対象チャンネル（管理番号・monday.com登録対象）は TSUHAN_CHANNELS（config.py）を使用

    if cmd_type == 'kakutei':
        judgment = get_judgment_from_thread(channel_id, thread_ts)
        if not judgment.get("first_channel"):
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *判定データなし*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "判定データが見つかりませんでした。\n\n"
                "先に分荷判定を実行してください。")
            return

        # 確定チャンネルを決定（表記ゆれを正規化）
        if cmd_option == '1':
            kakutei_channel = normalize_channel(judgment.get("first_channel", ""))
        elif cmd_option == '2':
            kakutei_channel = normalize_channel(judgment.get("second_channel", ""))
        else:
            kakutei_channel = normalize_channel(cmd_option)  # 確定/○○ の場合

        # まとめ売り系チャンネルは選択肢を表示して一旦止める
        if kakutei_channel in MATOME_CHANNELS:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                f"📦 *{kakutei_channel}*（まとめ売り）が選択されました\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "どちらで進めますか？\n\n"
                "1️⃣  まとめ保管する（管理番号なし）← *ほとんどの場合はこちら*\n\n"
                "2️⃣  個別に管理番号を発行して確定する\n\n"
                "3️⃣  保留にして浅野に相談する\n\n"
                f"`1` `2` `3` のいずれかを返信してください。\n\n"
                f"_[まとめ選択待ち:{kakutei_channel}]_",
                mention_user=user_id
            )
            return

        # まとめ以外 → 通常確定処理
        _complete_kakutei(kakutei_channel, judgment, user_id, channel_id, thread_ts, event)

    elif cmd_type == 'saihantei':
        persona = BOT_PERSONA["bunika"]
        post_to_slack(channel_id, thread_ts, persona["saihantei"])
        try:
            history = fetch_thread_messages(channel_id, thread_ts, event.get("ts", ""))
        except Exception:
            history = []
        judgment_text = call_claude("添付の情報をもとに改めて分荷判定してください。", history=history)
        post_to_slack(channel_id, thread_ts, judgment_text)
        # 再判定でも承認待ちなら浅野にDM通知
        if '確認待ち' in judgment_text or '承認' in judgment_text:
            item_match = re.search(r'アイテム名：(.+)', judgment_text)
            channel_match = re.search(r'▶\s*判定：(.+)', judgment_text)
            item_name = item_match.group(1).strip() if item_match else "不明"
            auto_channel = channel_match.group(1).strip() if channel_match else "不明"
            dm_sent = send_dm(ASANO_USER_ID,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *再判定：承認待ち*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"商品：{item_name}\n"
                f"判定：{auto_channel}\n\n"
                "該当スレッドで `確定` または変更指示をお願いします。")
            if not dm_sent:
                post_to_slack(channel_id, thread_ts,
                    f"<@{ASANO_USER_ID}> 再判定で承認が必要です。\n"
                    f"商品：{item_name} / 判定：{auto_channel}\n"
                    "`確定` または変更指示をお願いします。")

    elif cmd_type == 'horyuu':
        persona = BOT_PERSONA["bunika"]
        post_to_slack(channel_id, thread_ts, persona["horyuu"])

    elif cmd_type == 'cancel':
        confirmation = get_confirmation_from_thread(channel_id, thread_ts)
        kanri_bango = confirmation["kanri_bango"]
        confirmed_channel = confirmation["kakutei_channel"]

        if confirmed_channel:
            # 確定済み（管理番号あり・なし両方）→ スプレッドシートにキャンセル行追記
            cancel_payload = {
                # 管理番号なしの場合は「---」を記入してキャンセル行と識別できるようにする
                "kanri_bango":      kanri_bango if kanri_bango else "---",
                "kakutei_channel":  f"キャンセル（{confirmed_channel}）",
                "first_channel":    "",
                "second_channel":   "",
                "predicted_price":  "",
                "inventory_period": "",
                "score":            "",
                "internal_keyword": "",
                "staff_id":         user_id,
                "timestamp":        datetime.now().strftime("%Y/%m/%d %H:%M"),
            }
            send_to_spreadsheet(cancel_payload)

            # 管理番号ありの場合のみMonday.comも更新
            persona = BOT_PERSONA["bunika"]
            if kanri_bango:
                try:
                    cancel_monday_item(kanri_bango)
                except Exception as e:
                    print(f"[Monday.comキャンセルエラー] {e}")
                post_to_slack(channel_id, thread_ts,
                    persona["cancel_kanri"].format(kanri=kanri_bango),
                    mention_user=user_id)
            else:
                post_to_slack(channel_id, thread_ts,
                    persona["cancel_only"].format(channel=confirmed_channel),
                    mention_user=user_id)
        else:
            # 確定前キャンセル → 記録なし
            persona = BOT_PERSONA["bunika"]
            post_to_slack(channel_id, thread_ts, persona["cancel_none"], mention_user=user_id)


def _handle_checklist(checklist: dict, raw_text: str, channel_id: str, thread_ts: str, event: dict) -> None:
    """チェックリスト応答（状態番号＋フリーコメント）を処理する"""
    user_id = event.get("user", "")
    management_number = checklist["management_number"]

    # 先頭のアルファベット（S/A/B/C/D）を状態ランクとして取得、残りをコメントとして扱う
    n = normalize_keyword(raw_text)
    condition_key = n[0].upper() if n else ""
    condition_label = CONDITION_MAP.get(condition_key, "")
    comment = n[1:].strip() if len(n) > 1 else ""

    reply = (
        "━━━━━━━━━━━━━━━━\n"
        "✅ *動作確認完了*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{management_number}*\n\n"
        f"📊 状態\n"
        f"　*{condition_label}*"
    )
    if comment:
        reply += f"\n\n💬 コメント\n　{comment}"
    reply += "\n━━━━━━━━━━━━━━━━"

    post_to_slack(channel_id, thread_ts, reply, mention_user=user_id)

    # Monday.comのステータス・状態を更新
    try:
        update_monday_columns(management_number, {
            "status": {"label": "分荷確定"},
            "condition": condition_label,
        })
    except Exception as e:
        print(f"[Monday.com動作確認更新エラー] {e}")

    # スプレッドシートに動作確認結果を記録
    try:
        send_to_spreadsheet({
            "action":           "checklist_update",
            "kanri_bango":      management_number,
            "condition":        condition_label,
            "checklist_comment": comment,
            "staff_id":         get_staff_code(user_id),
            "timestamp":        datetime.now().strftime("%Y/%m/%d %H:%M"),
        })
    except Exception as e:
        print(f"[スプレッドシート動作確認更新エラー] {e}")
