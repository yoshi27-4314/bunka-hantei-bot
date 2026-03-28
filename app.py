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
    ASANO_USER_ID, ADMIN_USER_ID, CONDITION_MAP, STAFF_MAP,
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
from handlers.help import handle_help
from handlers.voice import (
    handle_voice_command, handle_voice_management, submit_voice,
    get_daily_summary, send_to_gchat, VOICE_CHANNEL_ID, SHANAI_CHANNEL_ID,
)

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
    print(f"[500エラー] {e}\n{traceback.format_exc()}")
    return jsonify({"error": "internal server error"}), 500


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
        print("[エラー] SLACK_SIGNING_SECRET が未設定です。リクエストを拒否します")
        return False

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

    # ── ヘルプはチャンネルに関わらず最優先で処理 ──────────
    if user_message:
        bot_role = get_bot_role_for_channel(channel_id)
        if handle_help(user_message, channel_id, thread_ts, user_id, bot_role):
            return

    # ── #新しい声チャンネルでの管理コマンド ──────────
    if user_message and handle_voice_management(user_message, channel_id, thread_ts, user_id):
        return

    # ── 新しい声コマンド（DMまたは任意チャンネル）──────────
    if user_message and handle_voice_command(user_message, user_id):
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
        "env_keys_count": len(os.environ),
    })


@app.route("/env-keys", methods=["GET"])
def env_keys():
    """全環境変数のキー名一覧を表示（要認証・値は非表示）"""
    if not verify_admin_token(request):
        return jsonify({"error": "認証が必要です"}), 403
    keys = sorted(os.environ.keys())
    return jsonify({"keys": keys, "count": len(keys)})






@app.route("/monday-setup", methods=["GET"])
def monday_setup():
    """monday.comボードにカラムを作成する（初回のみ実行）"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
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
        ("メイン写真",         "file",    "main_photo"),
        ("保管完了日",         "date",    "hokan_date"),
        ("撮影→保管(分)",     "numbers", "hokan_leadtime"),
        ("商品サイズ",         "text",    "product_size"),
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


@app.route("/monday-columns", methods=["GET"])
def monday_columns():
    """Monday.comボードの全カラムIDと名前を表示"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            columns { id title type }
        }
    }
    """
    result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
    columns = result.get("data", {}).get("boards", [{}])[0].get("columns", [])
    return jsonify({"columns": columns, "total": len(columns)})


@app.route("/monday-setup-status", methods=["GET"])
def monday_setup_status():
    """monday-setup バックグラウンド処理の進捗確認"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    done = "done" in _monday_setup_log
    return jsonify({
        "done": done,
        "total": len(_monday_setup_log),
        "log": _monday_setup_log,
    })


_task_board_log: list = []

@app.route("/setup-task-boards", methods=["GET"])
def setup_task_boards_endpoint():
    """タスク管理ボードを一括作成する"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    from scripts.setup_task_boards import setup_task_boards
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        return jsonify({"error": "MONDAY_TOKEN未設定"}), 500
    _task_board_log.clear()
    _task_board_log.append("started")
    def _run():
        try:
            result = setup_task_boards(token)
            _task_board_log.append(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            _task_board_log.append(f"error: {e}")
        _task_board_log.append("done")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "タスクボード作成をバックグラウンドで開始しました。/setup-task-boards-status で確認できます。"})


_register_tasks_log: list = []

@app.route("/register-tasks", methods=["GET"])
def register_tasks_endpoint():
    """タスクを一括登録する"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    from scripts.register_tasks import register_all_tasks
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        return jsonify({"error": "MONDAY_TOKEN未設定"}), 500
    _register_tasks_log.clear()
    _register_tasks_log.append("started")
    def _run():
        try:
            register_all_tasks(token)
        except Exception as e:
            _register_tasks_log.append(f"error: {e}")
        _register_tasks_log.append("done")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "タスク登録をバックグラウンドで開始しました。/register-tasks-status で確認できます。"})


@app.route("/register-tasks-status", methods=["GET"])
def register_tasks_status():
    """タスク登録の進捗確認"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    done = "done" in _register_tasks_log
    return jsonify({"done": done, "total": len(_register_tasks_log), "log": _register_tasks_log})


_backup_log: list = []

@app.route("/backup-monday", methods=["GET"])
def backup_monday_endpoint():
    """Monday.com全ボードをスプレッドシートにバックアップ"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    from scripts.backup_to_sheets import backup_all
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    gas_url = os.environ.get("GAS_URL", "")
    if not token or not gas_url:
        return jsonify({"error": "MONDAY_TOKEN or GAS_URL 未設定"}), 500
    _backup_log.clear()
    _backup_log.append("started")
    def _run():
        try:
            backup_all(token, gas_url)
        except Exception as e:
            _backup_log.append(f"error: {e}")
        _backup_log.append("done")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "バックアップをバックグラウンドで開始しました。/backup-monday-status で確認できます。"})


_migration_log: list = []

@app.route("/migrate-board-data", methods=["GET"])
def migrate_board_data_endpoint():
    """boardデータをMonday.comに移行（取引先498件＋案件811件）"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        return jsonify({"error": "MONDAY_TOKEN未設定"}), 500
    _migration_log.clear()
    _migration_log.append("started")
    def _run():
        try:
            from scripts.migrate_to_monday import run_migration
            run_migration(token)
        except Exception as e:
            _migration_log.append(f"error: {e}")
        _migration_log.append("done")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "board移行をバックグラウンドで開始しました（取引先498件＋案件811件）。/migrate-board-status で確認できます。"})


_staff_log: list = []

@app.route("/register-staff", methods=["GET"])
def register_staff_endpoint():
    """スタッフマスタを登録"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    from scripts.register_staff import register_staff
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        return jsonify({"error": "MONDAY_TOKEN未設定"}), 500
    _staff_log.clear()
    _staff_log.append("started")
    def _run():
        try:
            register_staff(token)
        except Exception as e:
            _staff_log.append(f"error: {e}")
        _staff_log.append("done")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "スタッフマスタ登録を開始しました。"})


@app.route("/update-staff-tags", methods=["GET"])
def update_staff_tags_endpoint():
    """スタッフマスタに業務タグ・権限を追加"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    from scripts.update_staff_tags import update_staff_tags
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        return jsonify({"error": "MONDAY_TOKEN未設定"}), 500
    def _run():
        try:
            update_staff_tags(token)
        except Exception as e:
            print(f"[スタッフタグ更新エラー] {e}")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "スタッフマスタのタグ・権限更新を開始しました。"})


@app.route("/fix-boards", methods=["GET"])
def fix_boards_endpoint():
    """ボードの修正と全ボード状態確認"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    from scripts.fix_boards import fix_asano_board, verify_all_boards
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        return jsonify({"error": "MONDAY_TOKEN未設定"}), 500
    _fix_log = []
    def _run():
        try:
            fix_asano_board(token)
            verify_all_boards(token)
        except Exception as e:
            print(f"[ボード修正エラー] {e}")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "全体タスク管理ボードのスキルマップ項目を削除し、全ボードの状態を確認します。"})


@app.route("/scan-drives", methods=["GET"])
def scan_drives_endpoint():
    """共有ドライブの全構造をスキャンしてスプレッドシートに書き出す"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    from scripts.scan_shared_drives import scan_all_drives
    def _run():
        try:
            scan_all_drives()
        except Exception as e:
            print(f"[ドライブスキャンエラー] {e}")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "共有ドライブのスキャンを開始しました。完了後、分荷判定DBに3シート追加されます。"})


@app.route("/register-skill-map", methods=["GET"])
def register_skill_map_endpoint():
    """スキルマップを個人ボードに登録"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    from scripts.register_skill_map import register_skill_map
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        return jsonify({"error": "MONDAY_TOKEN未設定"}), 500
    def _run():
        try:
            register_skill_map(token)
        except Exception as e:
            print(f"[スキルマップ登録エラー] {e}")
    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "スキルマップ登録を開始しました。約160項目×人数分のため数分かかります。"})


@app.route("/migrate-board-status", methods=["GET"])
def migrate_board_status():
    """board移行の進捗確認"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    done = "done" in _migration_log
    return jsonify({"done": done, "total": len(_migration_log), "log": _migration_log})


@app.route("/backup-monday-status", methods=["GET"])
def backup_monday_status():
    """バックアップの進捗確認"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    done = "done" in _backup_log
    return jsonify({"done": done, "total": len(_backup_log), "log": _backup_log})


@app.route("/setup-task-boards-status", methods=["GET"])
def setup_task_boards_status():
    """タスクボード作成の進捗確認"""
    if not verify_admin_token(request):
        return jsonify({"error": "Unauthorized"}), 403
    done = "done" in _task_board_log
    return jsonify({"done": done, "total": len(_task_board_log), "log": _task_board_log})


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


# ============================================================
# 新しい声 Webフォーム
# ============================================================

# スタッフ一覧をHTMLのoptionに変換
def _staff_options_html():
    names = sorted(set(STAFF_MAP.values()))
    return "\n".join(f'<option value="{n}">{n}</option>' for n in names)

@app.route("/voice", methods=["GET"])
def voice_form():
    """新しい声 投稿フォーム（Google Chatユーザー向け）"""
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    provided_token = request.args.get("token", "")
    if not webhook_secret or not hmac.compare_digest(provided_token, webhook_secret):
        print("[Voice GET] 認証失敗：不正なリクエストを拒否しました")
        return jsonify({"error": "認証が必要です"}), 403

    token = provided_token
    return f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>新しい声 | TakeBack</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: "Hiragino Kaku Gothic Pro", "Meiryo", sans-serif;
         background: #1a1a2e; color: #eee; min-height: 100vh;
         display: flex; justify-content: center; align-items: center; padding: 20px; }}
  .card {{ background: #16213e; border-radius: 16px; padding: 32px;
           max-width: 480px; width: 100%; box-shadow: 0 8px 32px rgba(0,0,0,0.3); }}
  h1 {{ font-size: 22px; text-align: center; margin-bottom: 8px; color: #fdab3d; }}
  .subtitle {{ text-align: center; font-size: 13px; color: #888; margin-bottom: 24px; }}
  label {{ display: block; font-size: 14px; margin-bottom: 6px; color: #ccc; }}
  select, textarea {{ width: 100%; padding: 12px; border: 1px solid #333;
                      border-radius: 8px; background: #0f3460; color: #eee;
                      font-size: 16px; margin-bottom: 16px; }}
  textarea {{ height: 120px; resize: vertical; }}
  button {{ width: 100%; padding: 14px; background: #fdab3d; color: #1a1a2e;
            border: none; border-radius: 8px; font-size: 16px; font-weight: bold;
            cursor: pointer; }}
  button:hover {{ background: #e89a2e; }}
  .result {{ text-align: center; padding: 20px; }}
  .result.ok {{ color: #00c875; }}
  .result.ng {{ color: #e2445c; }}
  .points {{ font-size: 12px; color: #888; margin-top: 16px; text-align: center; }}
</style>
</head>
<body>
<div class="card">
  <h1>💡 新しい声</h1>
  <div class="subtitle">要望・アイデア・相談を投稿できます</div>
  <form method="POST" action="/voice">
    <input type="hidden" name="_token" value="{token}">
    <label for="name">あなたの名前</label>
    <select id="name" name="name" required>
      <option value="">選んでください</option>
      {_staff_options_html()}
    </select>
    <label for="content">内容</label>
    <textarea id="content" name="content" placeholder="思いついたこと、改善してほしいこと、相談したいことを書いてください" required></textarea>
    <button type="submit">送信する</button>
  </form>
  <div class="points">投稿するだけで +1ポイント（1pt = 100円）</div>
</div>
</body>
</html>''', 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/voice", methods=["POST"])
def voice_submit():
    """新しい声 Webフォームからの投稿を処理する"""
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    provided_token = request.form.get("_token", "")
    if not webhook_secret or not hmac.compare_digest(provided_token, webhook_secret):
        print("[Voice POST] 認証失敗：不正なリクエストを拒否しました")
        return jsonify({"error": "認証が必要です"}), 403

    name = request.form.get("name", "").strip()
    content = request.form.get("content", "").strip()
    if not name or not content:
        return '<div class="card result ng">名前と内容を入力してください。<br><a href="/voice" style="color:#fdab3d;">戻る</a></div>', 400

    try:
        result = submit_voice(name, content)
        category = result.get("category", "")
        html = (
            '<!DOCTYPE html>'
            '<html lang="ja"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">'
            '<title>送信完了 | 新しい声</title>'
            '<style>'
            '* { margin: 0; padding: 0; box-sizing: border-box; }'
            'body { font-family: "Hiragino Kaku Gothic Pro", "Meiryo", sans-serif;'
            '       background: #1a1a2e; color: #eee; min-height: 100vh;'
            '       display: flex; justify-content: center; align-items: center; padding: 20px; }'
            '.card { background: #16213e; border-radius: 16px; padding: 32px;'
            '        max-width: 480px; width: 100%; box-shadow: 0 8px 32px rgba(0,0,0,0.3); text-align: center; }'
            'h1 { font-size: 22px; color: #00c875; margin-bottom: 16px; }'
            'p { font-size: 14px; color: #ccc; margin-bottom: 8px; }'
            'a { color: #fdab3d; text-decoration: none; }'
            '.pt { font-size: 18px; color: #fdab3d; font-weight: bold; margin: 16px 0; }'
            '</style></head>'
            '<body><div class="card">'
            '<h1>✅ 送信完了</h1>'
            f'<p>カテゴリ：<strong>{category}</strong></p>'
            '<div class="pt">+1 ポイント獲得</div>'
            '<p>浅野が確認して対応します。</p>'
            '<p style="margin-top:20px;"><a href="/voice">もう1件送る</a></p>'
            '</div></body></html>'
        )
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        print(f"[新しい声Webフォームエラー] {e}")
        return ('<div style="text-align:center;padding:40px;color:#e2445c;">'
                '送信に失敗しました。もう一度お試しください。<br>'
                '<a href="/voice" style="color:#fdab3d;">戻る</a></div>'), 500


@app.route("/voice/daily-summary", methods=["POST"])
def voice_daily_summary():
    """日次サマリーをSlack #社内連絡 + Google Chatに送信する（Make.comから毎朝8:55に呼び出す）"""
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    provided_secret = request.headers.get("X-Webhook-Secret", "")
    if not webhook_secret or not hmac.compare_digest(provided_secret, webhook_secret):
        print("[Voice Daily Summary] 認証失敗：不正なリクエストを拒否しました")
        return jsonify({"error": "認証が必要です"}), 403

    summary = get_daily_summary()
    if not summary:
        return jsonify({"ok": True, "msg": "本日の活動なし"})

    errors = []
    # Slack #社内連絡に送信
    try:
        token = get_slack_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        httpx.post("https://slack.com/api/chat.postMessage",
            headers=headers,
            json={"channel": SHANAI_CHANNEL_ID, "text": summary},
            timeout=10)
    except Exception as e:
        errors.append(f"Slack: {e}")

    # Google Chat（全スペース）に送信
    try:
        send_to_gchat(summary)
    except Exception as e:
        errors.append(f"Google Chat: {e}")

    if errors:
        return jsonify({"ok": False, "errors": errors})
    return jsonify({"ok": True, "msg": "Slack + Google Chat 送信完了"})


# ヘルスチェック: 前回のアラート内容（同じ障害の連続通知を防ぐ）
_previous_health_alerts: set = set()

@app.route("/health", methods=["GET"])
def health_check():
    """全サービスの生死確認エンドポイント。Make.comから定期的に呼び出す。
    同じ障害が続いている場合は重複通知しない。復旧時に復旧通知を送る。"""
    global _previous_health_alerts
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

    # ── Slack通知（ステータスが変わったときだけ）──
    current_alerts = set(alerts)
    new_alerts = current_alerts - _previous_health_alerts
    recovered = _previous_health_alerts - current_alerts
    alert_channel = os.environ.get("ALERT_CHANNEL_ID", "")

    if alert_channel:
        # 新しい障害が発生した場合のみ通知
        if new_alerts:
            alert_text = (
                "━━━━━━━━━━━━━━━━\n"
                "🔍 *ヘルスチェック異常検知*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"確認日時: {now}\n\n"
                + "\n\n".join(new_alerts) +
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

        # 障害が復旧した場合に復旧通知
        if recovered:
            recovery_text = (
                "━━━━━━━━━━━━━━━━\n"
                "✅ *ヘルスチェック復旧*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"確認日時: {now}\n\n"
                "以下の問題が解消しました：\n\n"
                + "\n\n".join(f"✅ {a.split(chr(10))[0]}" for a in recovered) +
                "\n\n━━━━━━━━━━━━━━━━"
            )
            try:
                httpx.post("https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {get_slack_token()}",
                             "Content-Type": "application/json"},
                    json={"channel": alert_channel, "text": recovery_text},
                    timeout=10)
            except Exception as e:
                print(f"[ヘルスチェック復旧通知エラー] {e}")

    _previous_health_alerts = current_alerts

    print(f"[ヘルスチェック] {now} 結果: {results}")
    return jsonify({
        "ok": len(alerts) == 0,
        "timestamp": now,
        "results": results,
        "alerts": alerts
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
