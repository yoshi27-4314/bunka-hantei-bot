"""
handlers/shuppinon.py - 出品保管チャンネル（岩崎弥太郎）
"""

import re
import json
from datetime import datetime

from config import (
    get_anthropic_client, get_staff_code,
    CHANNEL_NAMES, CANCEL_WORDS, LOCATION_PATTERN,
    STAFF_LISTING_MARKS, LISTING_RULES, LISTING_RULES_DEFAULT,
)
from services.slack import post_to_slack
from services.monday import get_item_from_monday, update_monday_columns
from services.google_drive import (
    get_drive_service, list_drive_images, delete_drive_file,
    upload_shuppinon_image, replace_drive_file,
)
from services.spreadsheet import send_to_spreadsheet
from handlers.satsuei import extract_management_number_from_image
from utils.commands import normalize_keyword, handle_free_comment
from utils.work_activity import (
    log_work_activity, handle_delete_step1, handle_delete_step2,
)


# 出品データの一時保管（スレッドTS → 出品セッション）
listing_sessions = {}

LISTING_COMMANDS = {
    "タイトル":  "title",
    "開始価格":  "start_price",
    "説明文":    "description",
    "サイズ":    "size",
}


def parse_listing_command(text: str):
    """出品データ修正コマンドを解析して (field, value) を返す"""
    n = normalize_keyword(text)
    for jp, field in LISTING_COMMANDS.items():
        for sep in ("：", ":"):
            prefix = f"{jp}{sep}"
            if n.startswith(prefix):
                return field, n[len(prefix):].strip()
    return None, None


def generate_listing_content(management_number: str, item_data: dict, max_title_len: int = 65) -> dict:
    """Claudeでヤフオク出品タイトル・説明文・価格を生成する"""
    client = get_anthropic_client()
    if not client:
        return {}

    item_name = item_data.get("item_name", "") or item_data.get("monday_name", "")
    maker = item_data.get("maker", "")
    model_number = item_data.get("model_number", "")
    condition = item_data.get("condition", "")
    channel = item_data.get("hantei_channel", "")
    price = item_data.get("yosou_kakaku", "")
    period = item_data.get("zaiko_kikan", "")
    kw = item_data.get("internal_keyword", "")

    # アカウント別ルールを取得
    rules = LISTING_RULES.get(channel, LISTING_RULES_DEFAULT)
    brand_tag = rules["brand_tag"]

    # ブランドタグ分の文字数を確保
    if brand_tag:
        tag_suffix = f"｜{brand_tag}"
        effective_title_len = max_title_len - len(tag_suffix)
    else:
        tag_suffix = ""
        effective_title_len = max_title_len

    prompt = (
        f"あなたはヤフオク出品のプロです。以下の商品情報をもとに出品データを作成してください。\n\n"
        f"【商品情報】\n"
        f"アイテム名：{item_name}\n"
        f"メーカー/ブランド：{maker}\n"
        f"品番/型式：{model_number}\n"
        f"商品状態：{condition}\n"
        f"販売チャンネル：{channel}\n"
        f"予想販売価格：{price}\n"
        f"内部KW：{kw}\n\n"
        f"【タイトルのルール】\n"
        f"{rules['title_style']}\n"
        f"・タイトル本文は{effective_title_len}文字以内（末尾にシステムが自動付与するタグがあるため）\n"
        f"・区切り記号は ◇（本文と詳細の間）と ｜（全角パイプ、詳細タグ間）のみ使用\n"
        f"・/（スラッシュ）や_（アンダーバー）は使わない\n\n"
        f"【説明文のルール】\n"
        f"{rules['desc_style']}\n"
        f"・600〜1000文字程度\n"
        f"・サイズは「未計測」と記載\n\n"
        f"【価格のルール】\n"
        f"{rules['price_style']}\n\n"
        f"以下のJSON形式のみで返してください（前置き不要）：\n"
        f'{{"title":"タイトル本文（{effective_title_len}文字以内）",'
        f'"description":"商品説明文",'
        f'"start_price":開始価格の数字}}'
    )
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            result = json.loads(m.group(0))
            # ブランドタグを自動付与
            if brand_tag and result.get("title"):
                result["title"] = result["title"][:effective_title_len] + tag_suffix
            return result
    except Exception as e:
        print(f"[出品コンテンツ生成エラー] {e}")
    return {}


def _post_image_list(channel_id: str, thread_ts: str, management_number: str) -> None:
    """出品用の商品画像一覧をSlackスレッドに表示する（テプラ除外）"""
    images = list_drive_images(management_number, exclude_tepura=True)
    if not images:
        post_to_slack(channel_id, thread_ts,
            "📷 商品画像がありません。\n\n"
            "このスレッドに写真を投稿すると追加できます。",
            bot_role="shuppinon")
        return

    lines = [
        "━━━━━━━━━━━━━━━━",
        f"📷 *出品画像（{len(images)}枚）*",
        "━━━━━━━━━━━━━━━━",
        "",
    ]
    for i, img in enumerate(images, 1):
        name = img.get("name", "")
        link = img.get("webViewLink", "")
        lines.append(f"　[{i}] {name}" + (f"  <{link}|表示>" if link else ""))
    lines.extend([
        "",
        "─────────────────────",
        "*画像コマンド：*",
        "　写真を投稿 → 追加撮影",
        "　`画像削除 3` → 3枚目を削除",
        "　`順番入替 2 4` → 2枚目と4枚目を入替",
        "　`撮り直し 2` + 写真 → 2枚目を差替",
        "　`画像` → 一覧を再表示",
    ])
    post_to_slack(channel_id, thread_ts, "\n".join(lines), bot_role="shuppinon")


def post_listing_summary(channel_id: str, thread_ts: str, session: dict, mention_user: str = "") -> None:
    """出品データをSlackに整形して表示する"""
    mn = session["management_number"]
    start = session.get("start_price", 0)
    size = session.get("size", "")
    text = (
        "━━━━━━━━━━━━━━━━\n"
        "📦 *出品データ確認*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{mn}*\n\n"
        f"📋 タイトル\n"
        f"　{session.get('title', '（未設定）')}\n\n"
        f"📊 状態\n"
        f"　{session.get('condition', '（未確認）')}\n\n"
        f"💰 開始価格\n"
        f"　¥{start:,}\n\n"
        f"📐 梱包サイズ\n"
        f"　{size + 'サイズ' if size else '（推定中）'}\n\n"
        f"📝 説明文\n"
        f"{session.get('description', '（未生成）')}\n\n"
        "─────────────────────\n"
        "*修正する場合はコマンドで入力：*\n\n"
        "　`タイトル：新しいタイトル`\n"
        "　`開始価格：5000`\n"
        "　`説明文：新しい説明文`\n"
        "　`サイズ：120`\n\n"
        "─────────────────────\n"
        "✅ *次のステップ*\n\n"
        "　*Step 1:* ヤフオク/eBayのページを作成したら\n"
        "　　→ `ページ作成完了` と入力\n\n"
        "　*Step 2:* 棚に収納したら\n"
        "　　→ ロケーション番号を入力（例：`A-12`）"
    )
    post_to_slack(channel_id, thread_ts, text, mention_user=mention_user, bot_role="shuppinon")


def execute_listing(session: dict, location: str, channel_id: str, thread_ts: str, user_id: str) -> None:
    """出品を実行する（スプレッドシート記録 + Monday.com更新）"""
    management_number = session["management_number"]

    # ページ作成時間を計算（ページ作成完了〜ロケーション入力までの分数）
    page_creation_minutes = 0
    if session.get("page_created_time"):
        page_creation_minutes = max(0, int((datetime.now() - session["page_created_time"]).total_seconds() / 60))

    # スプレッドシートに出品データを記録
    try:
        send_to_spreadsheet({
            "action":                "shuppinon_listing",
            "kanri_bango":           management_number,
            "title":                 session.get("title", ""),
            "description":           session.get("description", ""),
            "condition":             session.get("condition", ""),
            "start_price":           str(session.get("start_price", "")),
            "buyout_price":          str(session.get("buyout_price", "")),
            "size":                  session.get("size", ""),
            "location":              location,
            "staff_id":              get_staff_code(user_id),
            "timestamp":             datetime.now().strftime("%Y/%m/%d %H:%M"),
            "page_creation_minutes": page_creation_minutes,
        })
    except Exception as e:
        print(f"[スプレッドシート出品記録エラー] {e}")

    # Monday.comステータスを「出品中」に更新
    try:
        shuppinon_jikan = 0
        if session.get("start_time"):
            shuppinon_jikan = max(0, int((datetime.now() - session["start_time"]).total_seconds() / 60))
        monday_cols = {
            "status": {"label": "出品待ち"},
            "shuppinon_tantosha": get_staff_code(user_id),
            "shuppinon_date": {"date": datetime.now().strftime("%Y-%m-%d")},
            "location": location,
        }
        if session.get("start_price"):
            monday_cols["kaishi_kakaku"] = session["start_price"]
        if shuppinon_jikan > 0:
            monday_cols["shuppinon_jikan"] = shuppinon_jikan
        update_monday_columns(management_number, monday_cols)
    except Exception as e:
        print(f"[Monday.com出品中更新エラー] {e}")

    # TODO: ヤフオク自動出品（オークタウンAPI確認後に実装予定）
    start = session.get("start_price", 0)
    post_to_slack(channel_id, thread_ts,
        "━━━━━━━━━━━━━━━━\n"
        "✅ *出品登録完了*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"🔖 管理番号\n"
        f"　*{management_number}*\n\n"
        f"📍 保管場所\n"
        f"　*{location}*\n\n"
        f"📋 タイトル\n"
        f"　{session.get('title', '')}\n\n"
        f"💰 開始価格\n"
        f"　¥{start:,}\n\n"
        "🔜 ヤフオクAPI連携は4/1以降に追加予定です",
        mention_user=user_id, bot_role="shuppinon")


def handle_shuppinon_channel(event: dict) -> None:
    """出品チャンネルのイベントを処理する"""
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
        text_mn = re.search(r'\d{4}(?:[VGME]\d{4}|-\d{4})', text)
        if not image_urls and not text_mn:
            print(f"[出品CH無視] 管理番号なし・画像なし channel={channel_id} text={text[:30]!r}")
            return
        management_number = ""
        if text_mn and not image_urls:
            management_number = text_mn.group(0)
        elif image_urls:
            post_to_slack(channel_id, current_ts,
                "🔍 管理番号を読み取り中...", mention_user=user_id, bot_role="shuppinon")
            management_number = extract_management_number_from_image(image_urls[0])
        if not management_number:
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *読み取りエラー*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "管理番号を確認できませんでした。\n\n"
                "もう一度管理番号を送信してください。",
                bot_role="shuppinon")
            return

        # Monday.comからデータ取得
        item_data = get_item_from_monday(management_number)
        if not item_data:
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚠️ *該当なし*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"*{management_number}* は確認できません。\n\n"
                "管理番号を確認して再送信してください。",
                bot_role="shuppinon")
            return

        # 担当マーク判定（タイトル先頭に付与）
        staff_name = get_staff_code(user_id)
        staff_mark = STAFF_LISTING_MARKS.get(staff_name, "")
        mark_prefix = f"{staff_mark} " if staff_mark else ""
        max_title_len = 65 - len(mark_prefix)

        # Claudeで出品コンテンツ生成
        post_to_slack(channel_id, current_ts, "⏳ 出品データを生成中...", bot_role="shuppinon")
        listing = generate_listing_content(management_number, item_data, max_title_len=max_title_len)

        # 梱包サイズを内部KWから推定（例: /S80/ → 80）
        kw = item_data.get("internal_keyword", "")
        size_m = re.search(r'/[A-Z]+(\d+)/', kw)
        size = size_m.group(1) if size_m else ""

        # タイトルにマークを付与
        raw_title = listing.get("title", management_number)
        title_with_mark = mark_prefix + raw_title[:max_title_len]

        session = {
            "management_number": management_number,
            "title":       title_with_mark,
            "description": listing.get("description", ""),
            "condition":   item_data.get("condition", ""),
            "start_price": listing.get("start_price", 0),
            "buyout_price": listing.get("buyout_price", 0),
            "size":        size,
            "item_data":   item_data,
            "start_time":  datetime.now(),
            "page_created": False,
            "page_created_time": None,
        }
        listing_sessions[current_ts] = session
        post_listing_summary(channel_id, current_ts, session, mention_user=user_id)

        # 商品画像をDriveから取得して表示
        _post_image_list(channel_id, current_ts, management_number)
        return

    # ── スレッド内（修正コマンド or ロケーション番号）──
    # 削除確認待ちの処理
    if handle_delete_step2(channel_id, thread_ts, user_id, text):
        return

    session = listing_sessions.get(thread_ts)
    if not session:
        # 削除コマンド（セッションなし）
        if text == "削除":
            handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["shuppinon"], "shuppinon")
        else:
            print(f"[出品CH無視] スレッド内・セッションなし channel={channel_id} text={text[:30]!r}")
        return

    management_number = session["management_number"]

    # ── 画像管理コマンド ──
    # 画像一覧表示
    if text == "画像":
        _post_image_list(channel_id, thread_ts, management_number)
        return

    # 画像削除（例: 画像削除 3）
    m_del = re.match(r'^画像削除\s*(\d+)$', text)
    if m_del:
        idx = int(m_del.group(1))
        images = list_drive_images(management_number)
        if 1 <= idx <= len(images):
            target = images[idx - 1]
            if delete_drive_file(target["id"]):
                post_to_slack(channel_id, thread_ts,
                    f"🗑️ {idx}枚目（{target['name']}）を削除しました。",
                    bot_role="shuppinon")
                _post_image_list(channel_id, thread_ts, management_number)
            else:
                post_to_slack(channel_id, thread_ts, "⚠️ 削除に失敗しました。", bot_role="shuppinon")
        else:
            post_to_slack(channel_id, thread_ts, f"⚠️ {idx}枚目は存在しません。", bot_role="shuppinon")
        return

    # 順番入替（例: 順番入替 2 4）
    m_swap = re.match(r'^順番入替\s*(\d+)\s+(\d+)$', text)
    if m_swap:
        a, b = int(m_swap.group(1)), int(m_swap.group(2))
        images = list_drive_images(management_number)
        if 1 <= a <= len(images) and 1 <= b <= len(images) and a != b:
            service = get_drive_service()
            if service:
                try:
                    name_a = images[a - 1]["name"]
                    name_b = images[b - 1]["name"]
                    service.files().update(fileId=images[a - 1]["id"], body={"name": name_b}, supportsAllDrives=True).execute()
                    service.files().update(fileId=images[b - 1]["id"], body={"name": name_a}, supportsAllDrives=True).execute()
                    post_to_slack(channel_id, thread_ts,
                        f"🔄 {a}枚目と{b}枚目を入れ替えました。",
                        bot_role="shuppinon")
                    _post_image_list(channel_id, thread_ts, management_number)
                except Exception as e:
                    print(f"[出品CH] 順番入替エラー: {e}")
                    post_to_slack(channel_id, thread_ts, "⚠️ 入れ替えに失敗しました。", bot_role="shuppinon")
        else:
            post_to_slack(channel_id, thread_ts, "⚠️ 番号が正しくありません。", bot_role="shuppinon")
        return

    # 撮り直し（例: 撮り直し 2 + 写真投稿）
    m_replace = re.match(r'^撮り直し\s*(\d+)$', text)
    if m_replace and image_urls:
        idx = int(m_replace.group(1))
        images = list_drive_images(management_number)
        if 1 <= idx <= len(images):
            if replace_drive_file(images[idx - 1]["id"], image_urls[0]):
                post_to_slack(channel_id, thread_ts,
                    f"📷 {idx}枚目を差し替えました。",
                    bot_role="shuppinon")
                _post_image_list(channel_id, thread_ts, management_number)
            else:
                post_to_slack(channel_id, thread_ts, "⚠️ 差し替えに失敗しました。", bot_role="shuppinon")
        else:
            post_to_slack(channel_id, thread_ts, f"⚠️ {idx}枚目は存在しません。", bot_role="shuppinon")
        return

    # 撮影（追加撮影。「撮影」+ 写真投稿）
    if text == "撮影" and image_urls:
        uploaded = upload_shuppinon_image(management_number, image_urls)
        if uploaded:
            post_to_slack(channel_id, thread_ts,
                f"📷 {len(uploaded)}枚を追加しました。",
                bot_role="shuppinon")
            _post_image_list(channel_id, thread_ts, management_number)
        else:
            post_to_slack(channel_id, thread_ts, "⚠️ アップロードに失敗しました。", bot_role="shuppinon")
        return

    # スレッド内で写真だけ投稿（テキストなし）→ 追加撮影として扱う
    if not text and image_urls:
        uploaded = upload_shuppinon_image(management_number, image_urls)
        if uploaded:
            post_to_slack(channel_id, thread_ts,
                f"📷 {len(uploaded)}枚を追加しました。",
                bot_role="shuppinon")
            _post_image_list(channel_id, thread_ts, management_number)
        return

    # キャンセル・中断
    if text in CANCEL_WORDS:
        log_work_activity(CHANNEL_NAMES["shuppinon"], management_number,
                          get_staff_code(user_id), "キャンセル", session.get("start_time"))
        del listing_sessions[thread_ts]
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "⏹️ *出品作業キャンセル*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号\n"
            f"　*{management_number}*\n\n"
            "出品作業をキャンセルしました。",
            mention_user=user_id, bot_role="shuppinon")
        return

    # 削除コマンド
    if text == "削除":
        handle_delete_step1(channel_id, thread_ts, user_id, CHANNEL_NAMES["shuppinon"], "shuppinon")
        return

    # ページ作成完了コマンド
    if text == "ページ作成完了":
        session["page_created"] = True
        session["page_created_time"] = datetime.now()
        listing_sessions[thread_ts] = session
        try:
            update_monday_columns(management_number, {
                "status": {"label": "ページ作成完了"},
            })
        except Exception as e:
            print(f"[Monday.comページ作成完了更新エラー] {e}")
        try:
            send_to_spreadsheet({
                "action":      "shuppinon_page_complete",
                "kanri_bango": management_number,
                "staff_id":    get_staff_code(user_id),
                "timestamp":   datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        except Exception as e:
            print(f"[スプレッドシートページ作成完了エラー] {e}")
        post_to_slack(channel_id, thread_ts,
            "━━━━━━━━━━━━━━━━\n"
            "🖥️ *ページ作成完了*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔖 管理番号\n"
            f"　*{management_number}*\n\n"
            "ページ作成を記録しました！\n\n"
            "次は商品を棚に収納して\n"
            "ロケーション番号を入力してください。\n"
            "例：`A-12`",
            mention_user=user_id, bot_role="shuppinon")
        return

    # 修正コマンドの判定
    field, value = parse_listing_command(text)
    if field:
        if field == "start_price":
            try:
                session["start_price"] = int(re.sub(r'[^\d]', '', value))
            except Exception:
                pass
        elif field == "buyout_price":
            try:
                session["buyout_price"] = int(re.sub(r'[^\d]', '', value))
            except Exception:
                pass
        else:
            session[field] = value
        listing_sessions[thread_ts] = session
        post_listing_summary(channel_id, thread_ts, session, mention_user=user_id)
        return

    # どのコマンドにもマッチしなかった場合 → フリーコメント
    if handle_free_comment(channel_id, thread_ts, event):
        return

    # ロケーション番号（バリデーション付き）→ 出品確定
    if text:
        if LOCATION_PATTERN.match(text):
            execute_listing(session, text, channel_id, thread_ts, user_id)
            log_work_activity(CHANNEL_NAMES["shuppinon"], session["management_number"],
                              get_staff_code(user_id), "完了", session.get("start_time"))
            del listing_sessions[thread_ts]
        else:
            post_to_slack(channel_id, thread_ts,
                "⚠️ 先頭に倉庫コードを付けてください。\n\n"
                "倉庫コード：\n"
                "　*A* = 厚見倉庫\n"
                "　*H* = 本荘倉庫\n"
                "　*Y* = 柳津倉庫\n\n"
                "入力例：\n"
                "　`A23`　`A25横`　`H5`　`Y12`　`A2階`\n\n"
                "修正コマンド：\n"
                "　`タイトル：` `開始価格：` `説明文：` `サイズ：`",
                bot_role="shuppinon")
