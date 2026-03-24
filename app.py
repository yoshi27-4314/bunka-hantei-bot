"""
AI分荷判定Bot - Step 1
Slack Events API → Claude API → Slack返信
"""

import os
import json
import base64
import re
import threading
import hashlib
import hmac
import time
import httpx
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from collections import OrderedDict

# 設定・定数
from config import (
    get_slack_token, MONDAY_BOARD_ID, PROCESSED_EVENTS_MAX,
    ASANO_USER_ID, ADMIN_USER_ID, CONDITION_MAP,
)

# サービスレイヤー（app.py で直接使うもののみ）
from services.slack import send_dm, post_to_slack, get_bot_role_for_channel
from services.claude import call_claude
from services.monday import monday_graphql
from services.google_drive import get_drive_service

# ユーティリティ
from utils.commands import normalize_keyword, parse_command, handle_free_comment
from utils.slack_thread import fetch_thread_messages, get_matome_pending_from_thread, get_judgment_from_thread
from utils.checklist import get_checklist_state
from utils.work_activity import daily_stats

# ハンドラーからインポート
from handlers.bunika import (
    _handle_zaiko_search, _complete_kakutei, _handle_matome_choice,
    _handle_command, _handle_checklist,
)
from handlers.satsuei import (
    extract_management_number_from_image, handle_satsuei_channel,
)
from handlers.shuppinon import handle_shuppinon_channel
from handlers.konpo import handle_konpo_channel
from handlers.genba import handle_genba_channel
from handlers.status import handle_status_channel
from handlers.attendance import handle_attendance_channel, get_staff_break_minutes
from handlers.kintai import handle_kintai_channel

load_dotenv()
_env_check = {k: ("設定済み" if v else "未設定") for k, v in {
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
    "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN"),
    "SLACK_SIGNING_SECRET": os.environ.get("SLACK_SIGNING_SECRET"),
    "GAS_URL": os.environ.get("GAS_URL"),  # 起動時チェック用（実際はget_gas_url()で遅延取得）
    "MONDAY_TOKEN": os.environ.get("MONDAY_TOKEN"),
    "GOOGLE_SERVICE_ACCOUNT_JSON": os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
}.items()}
print(f"[起動時ENV確認] {_env_check}")

app = Flask(__name__)


@app.errorhandler(500)
def handle_500(e):
    import traceback
    return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


def verify_admin_token(req):
    """管理用エンドポイントのアクセスを合言葉（トークン）で制限する"""
    admin_token = os.environ.get("ADMIN_API_TOKEN", "")
    if not admin_token:
        return False
    provided = req.args.get("token", "") or req.headers.get("Authorization", "").replace("Bearer ", "")
    return hmac.compare_digest(provided, admin_token)


def verify_slack_signature(req):
    """Slackからのリクエストが本物かどうかを署名で検証する"""
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        print("[警告] SLACK_SIGNING_SECRET が未設定です。署名検証をスキップします")
        return True

    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")

    if not timestamp or not signature:
        print("[署名検証] タイムスタンプまたは署名ヘッダーがありません")
        return False

    if abs(time.time() - int(timestamp)) > 300:
        print("[署名検証] タイムスタンプが古すぎます")
        return False

    body = req.get_data(as_text=True)
    sig_basestring = f"v0:{timestamp}:{body}"
    my_signature = "v0=" + hmac.new(
        signing_secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(my_signature, signature):
        print("[署名検証] 署名が一致しません")
        return False

    return True


_monday_setup_log: list = []

# 相談モードのスレッド管理（@浅野+「相談」でトリガー → そのスレッドでボットが無反応になる）
_consultation_threads: set[str] = set()

# 重複処理防止（同じメッセージを2回処理しない）
_processed_events_dict = OrderedDict()


def process_slack_message(event: dict) -> None:
    """Slackメッセージをバックグラウンドで処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_message = event.get("text", "")
    user_id = event.get("user", "")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    print(f"[処理開始] channel={channel_id} ts={thread_ts} user={user_id} message={user_message[:30]}")
    print(f"[ENV確認] ANTHROPIC_API_KEY={'設定済み' if anthropic_key else '未設定'} SLACK_BOT_TOKEN={'設定済み' if slack_token else '未設定'}")

    # ── 管理者（浅野）専用処理 ───────────────────────────────
    bot_role = get_bot_role_for_channel(channel_id)
    if user_id == ADMIN_USER_ID:
        if user_message and user_message.strip().startswith("浅野です"):
            announcement = user_message.strip()[len("浅野です"):].strip()
            if announcement:
                post_to_slack(channel_id, thread_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "📢 *浅野からのお知らせ*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    f"{announcement}\n\n"
                    "━━━━━━━━━━━━━━━━",
                    bot_role=bot_role)
            return

    # ── 相談モード：@浅野 + 「相談」でトリガー ────────────────
    admin_mention = f"<@{ADMIN_USER_ID}>"
    if user_message and admin_mention in user_message and "相談" in user_message:
        _consultation_threads.add(thread_ts)
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "💬 *浅野さんへの相談スレッド*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"<@{ADMIN_USER_ID}> に通知しました。\n"
            "このスレッドではボットは反応しません。\n"
            "自由にご相談ください。\n\n"
            "━━━━━━━━━━━━━━━━",
            bot_role=bot_role)
        return

    if thread_ts in _consultation_threads:
        return

    # ── 在庫検索はチャンネルに関わらず最優先で処理 ──────────
    if user_message:
        cmd_type, cmd_option = parse_command(user_message)
        if cmd_type == 'zaiko_search':
            try:
                _handle_zaiko_search(cmd_option, channel_id, thread_ts, event)
            except Exception as e:
                print(f"[在庫検索エラー] {e}")
                post_to_slack(channel_id, thread_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "⚠️ *在庫検索エラー*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    "検索中にエラーが発生しました。もう一度お試しください。")
            return

    # ── チャンネルルーティング ────────────────────────────
    satsuei_channel_id = os.environ.get("SATSUEI_CHANNEL_ID", "")
    if satsuei_channel_id and channel_id == satsuei_channel_id:
        handle_satsuei_channel(event)
        return

    shuppinon_channel_id = os.environ.get("SHUPPINON_CHANNEL_ID", "")
    if shuppinon_channel_id and channel_id == shuppinon_channel_id:
        handle_shuppinon_channel(event)
        return

    konpo_channel_id = os.environ.get("KONPO_CHANNEL_ID", "")
    if konpo_channel_id and channel_id == konpo_channel_id:
        handle_konpo_channel(event)
        return

    status_channel_id = os.environ.get("STATUS_CHANNEL_ID", "")
    if status_channel_id and channel_id == status_channel_id:
        handle_status_channel(event)
        return

    attendance_channel_id = os.environ.get("ATTENDANCE_CHANNEL_ID", "")
    if attendance_channel_id and channel_id == attendance_channel_id:
        handle_attendance_channel(event)
        return

    genba_channel_id = os.environ.get("GENBA_CHANNEL_ID", "")
    if genba_channel_id and channel_id == genba_channel_id:
        handle_genba_channel(event)
        return

    kintai_channel_id = os.environ.get("KINTAI_CHANNEL_ID", "")
    if kintai_channel_id and channel_id == kintai_channel_id:
        handle_kintai_channel(event)
        return

    # 添付画像のURLを取得（複数対応）
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]
    image_only_post = bool(image_urls) and not user_message.strip()

    if not user_message and image_urls:
        user_message = "添付画像の商品を分荷判定してください。"

    # ── まとめ売り選択待ちの確認（1/2/3 をコマンドより優先してキャッチ）──
    if event.get("thread_ts") and user_message and user_message.strip() in ('1', '2', '3'):
        try:
            matome_channel = get_matome_pending_from_thread(channel_id, thread_ts)
            if matome_channel:
                _handle_matome_choice(user_message.strip(), matome_channel, channel_id, thread_ts, event)
                return
        except Exception as e:
            print(f"[まとめ選択処理エラー] {e}")

    # ── コマンド判定（確定・再判定・保留・キャンセルはスレッド内のみ）──
    if user_message:
        cmd_type, cmd_option = parse_command(user_message)
        if event.get("thread_ts") and cmd_type:
            try:
                _handle_command(cmd_type, cmd_option, channel_id, thread_ts, event)
            except Exception as e:
                print(f"[コマンド処理エラー] {e}")
                post_to_slack(channel_id, thread_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "⚠️ *コマンド処理エラー*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    "処理中にエラーが発生しました。もう一度お試しください。")
            return

        # ── チェックリスト応答判定 ─────────────────────────
        checklist = get_checklist_state(channel_id, thread_ts)
        if checklist:
            if checklist["is_completed"]:
                if image_only_post:
                    return
            else:
                n = normalize_keyword(user_message)
                is_checklist_input = n and n[0].upper() in CONDITION_MAP
                if is_checklist_input:
                    try:
                        _handle_checklist(checklist, user_message, channel_id, thread_ts, event)
                    except Exception as e:
                        print(f"[チェックリスト処理エラー] {e}")
                        post_to_slack(channel_id, thread_ts,
                            "━━━━━━━━━━━━━━━━\n"
                            "⚠️ *チェックリスト処理エラー*\n"
                            "━━━━━━━━━━━━━━━━\n\n"
                            "処理中にエラーが発生しました。もう一度お試しください。")
                    return
                elif image_only_post:
                    post_to_slack(channel_id, thread_ts,
                        "写真を受け取りました。\n\n"
                        "状態ランク（S/A/B/C/D）を一言添えて返信してください。\n"
                        "例：`B 電源OK、外観に小傷あり`",
                        mention_user=event.get("user", ""))
                    return

    # ── フリーコメント（判定完了後のスレッドで浅野↔スタッフの連絡）──
    if event.get("thread_ts") and user_message and not image_urls:
        judgment = get_judgment_from_thread(channel_id, thread_ts)
        if judgment.get("auto_channel") or judgment.get("first_channel"):
            if handle_free_comment(channel_id, thread_ts, event):
                return

    # ── 通常のAI判定フロー ────────────────────────────────
    history = []
    if event.get("thread_ts"):
        try:
            history = fetch_thread_messages(channel_id, thread_ts, current_ts)
            print(f"[会話履歴] {len(history)}件")
        except Exception as e:
            print(f"[会話履歴取得エラー] {e}")

    try:
        print("[Claude API呼び出し中...]")
        judgment_text = call_claude(user_message, image_urls, history)
        print(f"[Claude応答] {judgment_text[:50]}")

        user_id = event.get("user", "")
        post_to_slack(channel_id, thread_ts, judgment_text, mention_user=user_id)
        print("[Slack返信完了]")

        if '確認待ち' in judgment_text or '承認' in judgment_text:
            item_match = re.search(r'アイテム名：(.+)', judgment_text)
            channel_match = re.search(r'▶\s*判定：(.+)', judgment_text)
            price_match = re.search(r'予想販売価格：(.+)', judgment_text)
            item_name = item_match.group(1).strip() if item_match else "不明"
            auto_channel = channel_match.group(1).strip() if channel_match else "不明"
            price_info = price_match.group(1).strip() if price_match else "不明"
            thread_link = f"https://app.slack.com/client/{channel_id}/{thread_ts}"
            dm_sent = send_dm(ASANO_USER_ID,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *承認待ち*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"商品：{item_name}\n"
                f"判定：{auto_channel}\n"
                f"価格：{price_info}\n"
                f"担当：<@{user_id}>\n\n"
                f"該当スレッドで `確定` または変更指示をお願いします。")
            if not dm_sent:
                post_to_slack(channel_id, thread_ts,
                    f"<@{ASANO_USER_ID}> 承認が必要です。\n"
                    f"商品：{item_name} / 判定：{auto_channel}\n"
                    "`確定` または変更指示をお願いします。")
    except Exception as e:
        print(f"[エラー] {e}")
        try:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *エラーが発生しました*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "処理中にエラーが発生しました。もう一度お試しください。")
        except Exception as e2:
            print(f"[Slack送信エラー] {e2}")


# ── Flask Routes ──────────────────────────────────────────


@app.route("/debug", methods=["GET"])
def debug():
    """環境変数の設定状況を確認するエンドポイント（要認証）"""
    if not verify_admin_token(request):
        return jsonify({"error": "認証が必要です"}), 403
    return jsonify({
        "ANTHROPIC_API_KEY": "設定済み" if os.environ.get("ANTHROPIC_API_KEY") else "未設定",
        "SLACK_BOT_TOKEN": "設定済み" if os.environ.get("SLACK_BOT_TOKEN") else "未設定",
        "MONDAY_API_TOKEN": "設定済み" if (os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN")) else "未設定",
        "SATSUEI_CHANNEL_ID": os.environ.get("SATSUEI_CHANNEL_ID", "未設定"),
        "SHUPPINON_CHANNEL_ID": os.environ.get("SHUPPINON_CHANNEL_ID", "未設定"),
        "KONPO_CHANNEL_ID": os.environ.get("KONPO_CHANNEL_ID", "未設定"),
        "STATUS_CHANNEL_ID": os.environ.get("STATUS_CHANNEL_ID", "未設定"),
        "ATTENDANCE_CHANNEL_ID": os.environ.get("ATTENDANCE_CHANNEL_ID", "未設定"),
        "GENBA_CHANNEL_ID": os.environ.get("GENBA_CHANNEL_ID", "未設定"),
        "KINTAI_CHANNEL_ID": os.environ.get("KINTAI_CHANNEL_ID", "未設定"),
        "env_keys_count": len(os.environ),
    })


@app.route("/env-keys", methods=["GET"])
def env_keys():
    """全環境変数のキー名一覧を表示（要認証・値は非表示）"""
    if not verify_admin_token(request):
        return jsonify({"error": "認証が必要です"}), 403
    keys = sorted(os.environ.keys())
    return jsonify({"keys": keys, "count": len(keys)})




_fix_drive_result = {"status": "not_started"}


def _fix_drive_urls_v2():
    """Drive側の全フォルダを走査してMonday.comに書き込む（v2: Drive→Monday方式）"""
    global _fix_drive_result
    _fix_drive_result = {"status": "running", "progress": "開始..."}
    try:
        from services.google_drive import get_drive_service
        from services.monday import update_monday_columns, monday_graphql

        service = get_drive_service()
        root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
        if not service or not root_folder_id:
            _fix_drive_result = {"status": "error", "error": "Drive認証またはフォルダID未設定"}
            return

        # Step 1: YYMMフォルダを全取得
        _fix_drive_result["progress"] = "Driveフォルダを検索中..."
        yymm_query = f"'{root_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        yymm_results = service.files().list(q=yymm_query, fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        yymm_folders = yymm_results.get("files", [])

        # Step 2: 各YYMMフォルダ内の管理番号フォルダを全取得
        all_drive_folders = {}
        for yymm in yymm_folders:
            item_query = f"'{yymm['id']}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            item_results = service.files().list(q=item_query, fields="files(id, name)", pageSize=1000, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            for f in item_results.get("files", []):
                all_drive_folders[f["name"]] = f["id"]

        _fix_drive_result["progress"] = f"Driveに{len(all_drive_folders)}フォルダ発見。Monday.comと照合中..."

        # Step 3: Monday.comの全アイテム取得（drive_url含む）
        query = """
        query ($board_id: ID!) {
            boards(ids: [$board_id]) {
                items_page(limit: 500) {
                    items {
                        id
                        name
                        column_values(ids: ["kanri_bango", "drive_url"]) { id text }
                    }
                }
            }
        }
        """
        result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
        items = (result.get("data", {}).get("boards", [{}])[0]
                 .get("items_page", {}).get("items", []))

        # Step 4: drive_urlが空のアイテムにDriveフォルダURLを書き込む
        success_list = []
        already_set = 0
        no_drive = 0
        error_list = []

        for item in items:
            cols = {c["id"]: c["text"] for c in item.get("column_values", [])}
            kanri = cols.get("kanri_bango", "")
            drive_url = cols.get("drive_url", "")

            if not kanri:
                continue
            if drive_url:
                already_set += 1
                continue

            folder_id = all_drive_folders.get(kanri)
            if not folder_id:
                no_drive += 1
                continue

            try:
                folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
                update_monday_columns(kanri, {"drive_url": folder_url})
                success_list.append({"kanri_bango": kanri, "name": item["name"]})
            except Exception as e:
                error_list.append({"kanri_bango": kanri, "error": str(e)})

        _fix_drive_result = {
            "status": "done",
            "drive_folders_total": len(all_drive_folders),
            "monday_items_total": len(items),
            "already_set": already_set,
            "fixed": len(success_list),
            "no_drive_folder": no_drive,
            "errors": len(error_list),
            "fixed_items": success_list,
            "error_items": error_list,
        }
    except Exception as e:
        import traceback
        _fix_drive_result = {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


@app.route("/fix-drive-v2-20260325", methods=["GET"])
def fix_drive_urls_v2():
    """drive_url一括修復v2（Drive→Monday方式）"""
    global _fix_drive_result
    if _fix_drive_result["status"] == "running":
        return jsonify({"message": "実行中です...", "progress": _fix_drive_result.get("progress", "")})
    if _fix_drive_result["status"] in ("done", "error"):
        return jsonify(_fix_drive_result)
    threading.Thread(target=_fix_drive_urls_v2, daemon=True).start()
    return jsonify({"message": "修復v2を開始しました。30秒後にこのURLに再度アクセスしてください。"})


@app.route("/monday-setup", methods=["GET"])
def monday_setup():
    """monday.comボードにカラムを作成する（初回のみ実行）"""
    columns = [
        ("管理番号",           "text",    "kanri_bango"),
        ("判定チャンネル",     "text",    "hantei_channel"),
        ("確信度",             "text",    "kakushin_do"),
        ("分荷担当者",         "text",    "toshosha"),
        ("予想販売価格",       "numbers", "yosou_kakaku"),
        ("在庫予測期間",       "text",    "zaiko_kikan"),
        ("スコア",             "numbers", "score"),
        ("分荷作業時間(分)",   "numbers", "sakugyou_jikan"),
        ("内部KW",             "text",    "internal_keyword"),
        ("アイテム名",         "text",    "item_name"),
        ("ブランド/メーカー",  "text",    "maker"),
        ("品番/型式",          "text",    "model_number"),
        ("状態",               "text",    "condition"),
        ("カテゴリ",           "text",    "category"),
        ("査定担当者",         "text",    "satei_tantosha"),
        ("査定日",             "date",    "satei_date"),
        ("仕入れ原価",         "numbers", "shiire_genka"),
        ("分荷日",             "date",    "bunka_date"),
        ("在庫期限日",         "date",    "deadline_date"),
        ("撮影担当",           "text",    "satsuei_tantosha"),
        ("撮影完了日",         "date",    "satsuei_date"),
        ("撮影時間(分)",       "numbers", "satsuei_jikan"),
        ("写真枚数",           "numbers", "photo_count"),
        ("Drive写真URL",       "text",    "drive_url"),
        ("出品担当",           "text",    "shuppinon_tantosha"),
        ("出品日",             "date",    "shuppinon_date"),
        ("出品作業時間(分)",   "numbers", "shuppinon_jikan"),
        ("出品プラットフォーム","text",   "platform"),
        ("出品アカウント",     "text",    "shuppinon_account"),
        ("開始価格",           "numbers", "kaishi_kakaku"),
        ("目標価格",           "numbers", "mokuhyo_kakaku"),
        ("保管ロケーション",   "text",    "location"),
        ("梱包担当",           "text",    "konpo_tantosha"),
        ("梱包完了日",         "date",    "konpo_date"),
        ("梱包時間(分)",       "numbers", "konpo_jikan"),
        ("梱包材コスト",       "numbers", "konpo_cost"),
        ("運送会社",           "text",    "carrier"),
        ("追跡番号",           "text",    "tracking_number"),
        ("出荷日",             "date",    "shukka_date"),
        ("発送コスト",         "numbers", "hasso_cost"),
        ("落札日",             "date",    "rakusatsu_date"),
        ("落札価格",           "numbers", "rakusatsu_kakaku"),
        ("入札数",             "numbers", "nyusatsu_count"),
        ("アクセス数",         "numbers", "access_count"),
        ("在庫日数",           "numbers", "zaiko_days"),
        ("プラットフォーム手数料", "numbers", "platform_fee"),
        ("合計原価",           "numbers", "total_genka"),
        ("総労務時間(分)",     "numbers", "total_rodo_jikan"),
        ("総労務費",           "numbers", "total_rodohi"),
        ("粗利益",             "numbers", "arari"),
        ("純利益",             "numbers", "junri"),
        ("ROI(%)",             "numbers", "roi"),
        ("利益率(%)",          "numbers", "rieki_ritsu"),
        ("メモ",               "text",    "memo"),
    ]
    _monday_setup_log.clear()
    _monday_setup_log.append("started")

    def _run():
        query = """
        mutation ($board_id: ID!, $title: String!, $col_type: ColumnType!, $col_id: String!) {
            create_column(board_id: $board_id, title: $title, column_type: $col_type, id: $col_id) {
                id title
            }
        }
        """
        for title, col_type, col_id in columns:
            try:
                monday_graphql(query, {
                    "board_id": MONDAY_BOARD_ID,
                    "title": title,
                    "col_type": col_type,
                    "col_id": col_id,
                })
                _monday_setup_log.append(f"OK: {title}")
            except Exception as e:
                _monday_setup_log.append(f"SKIP: {title} ({e})")
        _monday_setup_log.append("done")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "カラム作成をバックグラウンドで開始しました。/monday-setup-status で進捗確認できます。"})


@app.route("/monday-setup-status", methods=["GET"])
def monday_setup_status():
    """monday-setup バックグラウンド処理の進捗確認"""
    done = "done" in _monday_setup_log
    return jsonify({
        "done": done,
        "total": len(_monday_setup_log),
        "log": _monday_setup_log,
    })


@app.route("/slack/events", methods=["POST"])
def slack_events():
    """Slack Events APIのエンドポイント"""
    if request.headers.get("X-Slack-Retry-Num"):
        print(f"[Slackリトライ] リトライ#{request.headers.get('X-Slack-Retry-Num')}を無視")
        return jsonify({"ok": True})

    if not verify_slack_signature(request):
        print("[署名検証] 不正なリクエストを拒否しました")
        return jsonify({"error": "invalid signature"}), 403

    data = request.get_json(force=True)

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})
    event_id = data.get("event_id", "")

    subtype = event.get("subtype", "")
    if event.get("bot_id") or event.get("bot_profile") or event_id in _processed_events_dict:
        return jsonify({"ok": True})
    if subtype and subtype != "file_share":
        return jsonify({"ok": True})

    _processed_events_dict[event_id] = True
    while len(_processed_events_dict) > PROCESSED_EVENTS_MAX:
        _processed_events_dict.popitem(last=False)

    if event.get("type") == "message":
        thread = threading.Thread(target=process_slack_message, args=(event,))
        thread.daemon = True
        thread.start()

    return jsonify({"ok": True})


@app.route("/test-drive", methods=["GET"])
def test_drive():
    """Google Drive接続テスト"""
    result = {"GOOGLE_SERVICE_ACCOUNT_JSON": "未設定", "GOOGLE_DRIVE_FOLDER_ID": "未設定", "drive_service": "NG", "folder_access": "NG", "error": ""}
    json_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    result["GOOGLE_SERVICE_ACCOUNT_JSON"] = "設定済み" if json_b64 else "未設定"
    result["GOOGLE_DRIVE_FOLDER_ID"] = folder_id if folder_id else "未設定"
    if not json_b64 or not folder_id:
        return jsonify(result)
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds_dict = json.loads(base64.b64decode(json_b64).decode())
        result["service_account_email"] = creds_dict.get("client_email", "不明")
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/drive"])
        service = build("drive", "v3", credentials=creds)
        result["drive_service"] = "OK"
        folder = service.files().get(fileId=folder_id, fields="id,name,permissions", supportsAllDrives=True).execute()
        result["folder_access"] = "OK"
        result["folder_name"] = folder.get("name", "不明")
    except Exception as e:
        result["error"] = str(e)
    return jsonify(result)


@app.route("/webhook", methods=["POST"])
def webhook():
    """MakeからのWebhookを受け取るエンドポイント（要認証）"""
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    provided_secret = request.headers.get("X-Webhook-Secret", "")
    if not webhook_secret or not hmac.compare_digest(provided_secret, webhook_secret):
        print("[Webhook] 認証失敗：不正なリクエストを拒否しました")
        return jsonify({"error": "認証が必要です"}), 403

    data = request.get_json(force=True)
    channel_id = data.get("channel_id")
    thread_ts = data.get("thread_ts")
    user_message = data.get("user_message")
    image_url = data.get("image_url")

    if not all([channel_id, thread_ts, user_message]):
        return jsonify({"error": "channel_id, thread_ts, user_message は必須です"}), 400

    try:
        image_urls = [image_url] if image_url else []
        judgment = call_claude(user_message, image_urls)
        post_to_slack(channel_id, thread_ts, judgment)
        return jsonify({"ok": True, "judgment": judgment}), 200
    except Exception as e:
        print(f"[Webhook判定エラー] {e}")
        try:
            post_to_slack(channel_id, thread_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *処理エラー*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "判定処理中にエラーが発生しました。もう一度お試しください。")
        except Exception:
            pass
        return jsonify({"ok": False, "error": "internal error"}), 500


@app.route("/health", methods=["GET"])
def health_check():
    """全サービスの生死確認エンドポイント。Make.comから定期的に呼び出す。"""
    results = {}
    alerts = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 1. Slack API ──
    try:
        token = get_slack_token()
        if not token:
            raise RuntimeError("SLACK_BOT_TOKEN 未設定")
        r = httpx.get("https://slack.com/api/auth.test",
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
        data = r.json()
        if data.get("ok"):
            results["slack"] = "OK"
        else:
            raise RuntimeError(data.get("error", "unknown"))
    except Exception as e:
        results["slack"] = f"ERROR: {e}"
        alerts.append(f"🚨 Slack APIに接続できません\n→ Railwayの環境変数 SLACK_BOT_TOKEN が正しく設定されているか確認してください\n→ 人間の対応が必要です")

    # ── 2. Monday.com API ──
    try:
        r = monday_graphql("query { me { id name } }")
        if r.get("data", {}).get("me"):
            results["monday"] = "OK"
        else:
            raise RuntimeError(str(r.get("errors", "unknown")))
    except Exception as e:
        results["monday"] = f"ERROR: {e}"
        alerts.append(f"🚨 Monday.comに接続できません\n→ Railwayの環境変数 MONDAY_TOKEN が正しく設定されているか確認してください\n→ 人間の対応が必要です")

    # ── 3. Anthropic API ステータスページ確認 ──
    try:
        r = httpx.get("https://status.claude.com/api/v2/status.json", timeout=10, follow_redirects=True)
        data = r.json()
        indicator = data.get("status", {}).get("indicator", "unknown")
        description = data.get("status", {}).get("description", "")
        if indicator == "none":
            results["anthropic"] = "OK"
        else:
            results["anthropic"] = f"WARN: {indicator} - {description}"
            alerts.append(f"⚠️ Claude AI（Anthropic）で障害が発生しています\n→ AI判定が一時的に使えない可能性があります\n→ 詳細: https://status.anthropic.com")
    except Exception as e:
        results["anthropic"] = f"ERROR: {e}"
        alerts.append(f"⚠️ Claude AIのステータス確認ができませんでした\n→ しばらく待ってから再確認してください")

    # ── 4. Slack ステータスページ確認 ──
    try:
        r = httpx.get("https://status.slack.com/api/v2.0.0/current", timeout=10, follow_redirects=True)
        data = r.json()
        status = data.get("status", "unknown")
        if status == "ok":
            results["slack_status"] = "OK"
        else:
            results["slack_status"] = f"WARN: {status}"
            alerts.append(f"⚠️ Slackで障害が発生しています\n→ メッセージが届かない・遅延する可能性があります\n→ 詳細: https://status.slack.com")
    except Exception as e:
        results["slack_status"] = f"ERROR: {e}"

    # ── 5. Google Drive API ──
    try:
        svc = get_drive_service()
        folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
        if svc:
            svc.files().list(pageSize=1, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            results["google_drive"] = "OK"
            if folder_id:
                try:
                    folder = svc.files().get(fileId=folder_id, fields="id,name", supportsAllDrives=True).execute()
                    results["google_drive_folder"] = f"OK: {folder.get('name', '名前不明')}"
                except Exception as fe:
                    results["google_drive_folder"] = f"NG: {fe}"
                    alerts.append(f"🚨 Google Drive 写真フォルダにアクセスできません\n→ 撮影チャンネルの画像自動保存が止まっています\n→ 対応手順：\n　1. Google Drive「TKB｜A｜自社在庫」フォルダを開く\n　2. 右クリック→「共有」\n　3. bunka-bot-drive@ordinal-gear-489903-a5.iam.gserviceaccount.com を「編集者」で追加\n→ 人間の対応が必要です")
            else:
                results["google_drive_folder"] = "SKIP（GOOGLE_DRIVE_FOLDER_ID未設定）"
        else:
            results["google_drive"] = "SKIP（未設定）"
            results["google_drive_folder"] = "SKIP（未設定）"
    except Exception as e:
        results["google_drive"] = f"ERROR: {e}"
        results["google_drive_folder"] = "SKIP"
        alerts.append(f"🚨 Google Driveに接続できません\n→ Railwayの環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が正しく設定されているか確認してください\n→ 人間の対応が必要です")

    # ── 6. Bot直近24時間の処理件数確認 ──
    total_ops = sum(v.get("完了", 0) + v.get("キャンセル", 0) + v.get("削除", 0)
                    for v in daily_stats.values())
    results["bot_24h_ops"] = total_ops
    if total_ops == 0:
        results["bot_activity"] = "WARN: 直近で処理件数が0件"
    else:
        results["bot_activity"] = f"OK: {total_ops}件処理済み"

    # ── Slack通知（異常がある場合のみ）──
    if alerts:
        alert_channel = os.environ.get("ALERT_CHANNEL_ID", "")
        if alert_channel:
            alert_text = (
                "━━━━━━━━━━━━━━━━\n"
                "🔍 *ヘルスチェック異常検知*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"確認日時: {now}\n\n"
                + "\n\n".join(alerts) +
                "\n\n━━━━━━━━━━━━━━━━"
            )
            try:
                httpx.post("https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {get_slack_token()}",
                             "Content-Type": "application/json"},
                    json={"channel": alert_channel, "text": alert_text},
                    timeout=10)
            except Exception as e:
                print(f"[ヘルスチェック通知エラー] {e}")

    print(f"[ヘルスチェック] {now} 結果: {results}")
    return jsonify({
        "ok": len(alerts) == 0,
        "timestamp": now,
        "results": results,
        "alerts": alerts
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
