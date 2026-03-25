"""
handlers/satsuei.py - 撮影確認チャンネル（白洲次郎）
"""

import os
import re
from datetime import datetime

from config import get_anthropic_client, get_staff_code, CHANNEL_NAMES, CANCEL_WORDS
from services.slack import post_to_slack
from services.claude import fetch_image_as_base64
from services.monday import update_monday_columns, upload_file_to_monday
from services.google_drive import (
    get_drive_service, get_or_create_drive_folder, upload_images_to_drive,
    download_first_product_image,
)
from services.spreadsheet import send_to_spreadsheet
from utils.commands import normalize_keyword, handle_free_comment
from utils.work_activity import (
    log_work_activity, handle_delete_step1, handle_delete_step2,
)


def extract_management_number_from_image(image_url: str) -> str:
    """テプラ画像からClaude Visionで管理番号を読み取る"""
    try:
        image_data, media_type = fetch_image_as_base64(image_url)
        client = get_anthropic_client()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                    {"type": "text", "text": (
                        "この画像のテプラ（ラベル）に書かれた管理番号を読み取ってください。"
                        "管理番号は「2603-0001」または「2603G0001」のような形式です（年月4桁＋ハイフン＋4桁 または 年月4桁＋英字1文字＋4桁）。"
                        "管理番号だけを返してください。見つからない場合は「不明」と返してください。"
                    )}
                ]
            }]
        )
        text = response.content[0].text.strip()
        m = re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
        return m.group(0) if m else ""
    except Exception as e:
        print(f"[管理番号読取エラー] {e}")
        return ""


def _get_management_number_from_satsuei_thread(channel_id: str, thread_ts: str) -> str:
    """撮影スレッド内のBot確認メッセージから管理番号を取得する"""
    import httpx
    from config import get_slack_token
    token = get_slack_token()
    response = httpx.get(
        "https://slack.com/api/conversations.replies",
        headers={"Authorization": f"Bearer {token}"},
        params={"channel": channel_id, "ts": thread_ts}, timeout=10
    )
    for msg in response.json().get("messages", []):
        if not (msg.get("bot_id") or msg.get("bot_profile")):
            continue
        m = re.search(r'管理番号\s*\*?(\d{4}(?:[VGME]\d{4}|-\d{4}))\*?', msg.get("text", ""))
        if m:
            return m.group(1)
    return ""


def handle_satsuei_channel(event: dict) -> None:
    """撮影確認チャンネルのイベントを処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_id = event.get("user", "")
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]
    text = normalize_keyword(event.get("text", ""))
    is_new_post = not event.get("thread_ts")

    # ── 新規投稿（テプラ写真 or テキストで管理番号）──────
    if is_new_post:
        # テキストで管理番号が直接入力された場合
        text_mn = re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
        if not image_urls and not text_mn:
            print(f"[撮影CH無視] 管理番号なし・画像なし channel={channel_id} text={text[:30]!r}")
            return
        if text_mn and not image_urls:
            management_number = text_mn.group(0)
        elif image_urls:
            management_number = extract_management_number_from_image(image_urls[0])
            if not management_number:
                post_to_slack(channel_id, current_ts,
                    "⚠️ *読み取りエラー*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    "テプラの管理番号を\n"
                    "読み取れませんでした。\n\n"
                    "📌 *対処方法*\n"
                    "　① テプラをもう一度撮影して送る\n"
                    "　② または管理番号をテキストで入力\n"
                    "　　例） *2603-0001*",
                    bot_role="satsuei")
                return
            # テプラ画像をDriveに保存
            upload_images_to_drive(management_number, [image_urls[0]], is_tepura=True)
        else:
            return
        post_to_slack(channel_id, current_ts,
            "📸 *撮影セッション開始*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号　*{management_number}*\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 *作業手順*\n\n"
            "　① このスレッドに商品写真を投稿\n"
            "　　（原則3枚・最大5枚）\n\n"
            "　② Botの確認メッセージが届いたら\n"
            "　　写真をチェックする\n\n"
            "　③ 問題なければ `完了` と送信\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "💡 *使えるコマンド*\n\n"
            "　`完了` ／ 撮影完了・Driveに保存\n"
            "　`やり直し` ／ 写真を全削除して1枚目から撮り直す\n"
            "　`キャンセル` ／ 作業を中断する",
            bot_role="satsuei")
        return

    # ── スレッド内（商品写真 or 完了 or キャンセル/削除）──
    # 削除確認待ちの処理
    if handle_delete_step2(channel_id, thread_ts, user_id, text):
        return

    management_number = _get_management_number_from_satsuei_thread(channel_id, thread_ts)
    if not management_number:
        # 削除コマンド（セッションなし）
        if text == "削除":
            handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["satsuei"], "satsuei")
        else:
            print(f"[撮影CH無視] スレッド内・セッションなし channel={channel_id} text={text[:30]!r}")
        return

    # キャンセル・中断
    if text in CANCEL_WORDS:
        log_work_activity(CHANNEL_NAMES["satsuei"], management_number, get_staff_code(user_id), "キャンセル")
        post_to_slack(channel_id, thread_ts,
            "⏹️ *撮影作業を中断しました*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号　*{management_number}*\n\n"
            "作業を再開するときは\n"
            "もう一度管理番号を投稿してください。",
            mention_user=user_id, bot_role="satsuei")
        return

    # やり直しコマンド
    if text == "やり直し":
        deleted_count = 0
        try:
            svc = get_drive_service()
            root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
            if svc and root_folder_id:
                yymm_id = get_or_create_drive_folder(svc, root_folder_id, management_number[:4])
                item_id = get_or_create_drive_folder(svc, yymm_id, management_number)
                files_list = svc.files().list(
                    q=f"'{item_id}' in parents and trashed=false and not name contains '01_'",
                    fields="files(id,name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True
                ).execute().get("files", [])
                for f in files_list:
                    svc.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                    deleted_count += 1
        except Exception as e:
            print(f"[Drive やり直しエラー] {e}")
        post_to_slack(channel_id, thread_ts,
            "🔄 *写真をやり直します*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🗑️ 削除した写真　*{deleted_count}枚*\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📌 *1枚目から撮り直してください*\n\n"
            "このスレッドに\n"
            "新しい写真を投稿してください。\n\n"
            "　• テプラ画像は残してあります\n"
            "　• 商品写真のみ全て削除しました",
            mention_user=user_id, bot_role="satsuei")
        return

    # 削除コマンド
    if text == "削除":
        handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["satsuei"], "satsuei")
        return

    # 商品写真をDriveに保存
    folder_url = ""
    if image_urls:
        folder_url = upload_images_to_drive(management_number, image_urls, is_tepura=False)
        post_to_slack(channel_id, thread_ts,
            f"📷 *{len(image_urls)}枚* を受け取りました\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "🔍 *投稿した写真を確認してください*\n\n"
            "　□ ピントが合っているか\n"
            "　□ 明るさは適切か\n"
            "　□ 角度・アングルは揃っているか\n"
            "　□ 枚数は足りているか\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "✅ 問題なければ `完了` と送信\n"
            "📷 追加写真があればそのまま投稿\n"
            "🔄 撮り直す場合は `やり直し` と送信",
            mention_user=user_id, bot_role="satsuei")
        # 写真投稿は通常操作 → 浅野通知不要
        if text == "完了":
            pass  # 下の完了処理に進む
        else:
            return

    # 完了コマンド
    if text == "完了":
        # 完了メッセージと写真投稿が別メッセージの場合、folder_urlが空になるため取得し直す
        if not folder_url:
            try:
                _svc = get_drive_service()
                _root = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
                if _svc and _root:
                    _yymm_id = get_or_create_drive_folder(_svc, _root, management_number[:4])
                    _item_id = get_or_create_drive_folder(_svc, _yymm_id, management_number)
                    folder_url = f"https://drive.google.com/drive/folders/{_item_id}"
            except Exception as e:
                print(f"[DriveフォルダURL取得エラー] {e}")
        post_to_slack(channel_id, thread_ts,
            "✅ *撮影完了！お疲れ様でした*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号　*{management_number}*\n\n"
            "写真をDriveに保存しました。",
            mention_user=user_id, bot_role="satsuei")
        log_work_activity(CHANNEL_NAMES["satsuei"], management_number, get_staff_code(user_id), "完了")
        monday_updates = {
            "status": {"label": "撮影完了"},
            "satsuei_tantosha": get_staff_code(user_id),
            "satsuei_date": {"date": datetime.now().strftime("%Y-%m-%d")},
        }
        if folder_url:
            monday_updates["drive_url"] = folder_url
        try:
            update_monday_columns(management_number, monday_updates)
        except Exception as e:
            print(f"[Monday.com撮影完了更新エラー] {e}")
        # メイン写真をMonday.comにアップロード
        try:
            img_bytes, img_name = download_first_product_image(management_number)
            if img_bytes:
                upload_file_to_monday(management_number, "file_mm1rwrna", img_bytes, img_name)
        except Exception as e:
            print(f"[メイン写真アップロードエラー] {e}")
        try:
            send_to_spreadsheet({
                "action":           "satsuei_update",
                "kanri_bango":      management_number,
                "drive_folder_url": folder_url,
                "staff_id":         get_staff_code(user_id),
                "timestamp":        datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        except Exception as e:
            print(f"[スプレッドシート撮影完了更新エラー] {e}")
        return

    # どのコマンドにもマッチしなかった場合 → フリーコメント
    handle_free_comment(channel_id, thread_ts, event)
