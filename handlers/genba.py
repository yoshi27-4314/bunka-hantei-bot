"""
handlers/genba.py - 現場査定チャンネル（渋沢栄一）
"""

import re
import json
from datetime import datetime

from config import get_anthropic_client, get_staff_code, BOT_PERSONA
from prompts import GENBA_SYSTEM_PROMPT
from services.slack import post_to_slack
from services.claude import fetch_image_as_base64
from services.spreadsheet import send_to_spreadsheet
from utils.commands import normalize_keyword


# 古物台帳フローのセッション管理
# key: "{channel_id}_{user_id}"
# value: {"step": 1〜3, "price": int, "item_name": str, "staff_id": str, "timestamp": str, "id_info": dict}
kaitori_sessions = {}


def _extract_id_info(image_url: str) -> dict:
    """身分証の写真からClaudeで情報を抽出する（古物台帳記載用）"""
    img_data, img_type = fetch_image_as_base64(image_url)
    client = get_anthropic_client()
    if not client:
        return {}
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="""身分証明書の画像から以下の情報をJSON形式のみで返してください。
読み取れない項目は「読取不可」としてください。
{
  "doc_type": "運転免許証 または マイナンバーカード または パスポート",
  "name": "氏名（姓名）",
  "address": "住所",
  "birthdate": "生年月日（YYYY/MM/DD形式）",
  "id_number": "証明書番号（免許証番号など）"
}
※マイナンバー（12桁の個人番号）は絶対に記録しないこと。
※JSON以外のテキストは一切出力しないこと。""",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "この身分証から情報を抽出してください。"},
                {"type": "image", "source": {"type": "base64", "media_type": img_type, "data": img_data}},
            ]
        }],
    )
    text = response.content[0].text
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def _handle_scrap_calc(text: str, channel_id: str, current_ts: str, user_id: str) -> bool:
    """スクラップ計算コマンドを処理する。
    形式: スクラップ [素材] [単価]円 [重量]kg
    例: スクラップ 鉄 45円 120kg
    例: スクラップ 45円 120kg（素材省略可）
    処理した場合はTrueを返す。"""
    if not text:
        return False

    normalized = normalize_keyword(text)
    if "スクラップ" not in normalized:
        return False

    # 単価（円）と重量（kg）を全て normalized から抽出
    price_match = re.search(r'(\d+(?:\.\d+)?)\s*円', normalized)
    weight_match = re.search(r'(\d+(?:\.\d+)?)\s*kg', normalized, re.IGNORECASE)

    if not price_match or not weight_match:
        if price_match or weight_match:
            # 片方だけ入力されている → 使い方を案内
            post_to_slack(channel_id, current_ts,
                "━━━━━━━━━━━━━━━━\n"
                "⚖️ *スクラップ計算*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "単価と重量の両方が必要です。\n\n"
                "📝 入力例：\n"
                "`スクラップ 鉄 45円 120kg`\n\n"
                "━━━━━━━━━━━━━━━━",
                mention_user=user_id, bot_role="genba")
            return True
        # どちらもない → 通常の査定に回す
        return False

    unit_price = float(price_match.group(1))
    weight_kg = float(weight_match.group(1))
    total = unit_price * weight_kg

    # 素材名を抽出（Slackメンション・「スクラップ」・数値部分を除いた残り）
    cleaned = re.sub(r'<[^>]+>', '', normalized).strip()
    material = re.sub(r'スクラップ|[\d.]+\s*円|[\d.]+\s*kg', '', cleaned, flags=re.IGNORECASE).strip()
    material_str = material if material else "指定なし"

    reply = (
        "━━━━━━━━━━━━━━━━\n"
        "⚖️ *スクラップ計算*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"📦 素材：*{material_str}*\n\n"
        f"💴 単価：¥{unit_price:g}/kg\n\n"
        f"⚖️ 重量：{weight_kg:g}kg\n\n"
        "─────────────────────────\n\n"
        f"💰 *合計：¥{total:,.0f}*\n\n"
        "━━━━━━━━━━━━━━━━"
    )
    post_to_slack(channel_id, current_ts, reply, mention_user=user_id, bot_role="genba")
    return True


def _handle_kaitori_flow(event: dict, channel_id: str, current_ts: str,
                         user_id: str, text: str, image_urls: list) -> bool:
    """古物台帳フロー（買取確定〜身分証確認〜台帳登録）を処理する。
    フロー処理した場合はTrueを返す。"""
    session_key = f"{channel_id}_{user_id}"

    # ── Step 0: 「買取確定 ¥3000」でフロー開始 ──
    m = re.search(r'買取確定\s*[¥￥]?\s*([\d,]+)', text or "")
    if m:
        price = int(m.group(1).replace(",", ""))
        kaitori_sessions[session_key] = {
            "step": 1,
            "price": price,
            "staff_id": get_staff_code(user_id),
            "timestamp": datetime.now().strftime("%Y/%m/%d %H:%M"),
        }
        post_to_slack(channel_id, current_ts,
            f"買取価格 *¥{price:,}* で記録いたします。\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "📦 *品物の名称・特徴を教えてください*\n\n"
            "記入例：\n"
            "`パナソニック 洗濯機 NA-F60B14 中古 動作OK`\n"
            "━━━━━━━━━━━━━━━━",
            mention_user=user_id, bot_role="genba")
        return True

    # セッションがなければフロー外
    session = kaitori_sessions.get(session_key)
    if not session:
        return False

    step = session["step"]

    # ── Step 1: 品物名を受け取る ──
    if step == 1 and text and not image_urls:
        session["item_name"] = text
        session["step"] = 2
        kaitori_sessions[session_key] = session
        post_to_slack(channel_id, current_ts,
            "承りました。\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "🪪 *相手方の身分証をお送りください*\n\n"
            "古物営業法に基づく確認が必要です。\n\n"
            "📸 対応書類：\n"
            "　・運転免許証\n"
            "　・マイナンバーカード（番号面は不要）\n"
            "　・パスポート\n\n"
            "氏名・住所・生年月日・証明書番号が\n"
            "確認できる面の写真を送信してください。\n"
            "━━━━━━━━━━━━━━━━",
            mention_user=user_id, bot_role="genba")
        return True

    # ── Step 2: 身分証写真を受け取る ──
    if step == 2 and image_urls:
        post_to_slack(channel_id, current_ts,
            "🔍 身分証の情報を読み取っております...\n"
            "しばしお待ちを。",
            bot_role="genba")
        try:
            id_info = _extract_id_info(image_urls[0])
            session["id_info"] = id_info
            session["step"] = 3
            kaitori_sessions[session_key] = session
            post_to_slack(channel_id, current_ts,
                "読み取り完了でございます。\n"
                "内容をご確認ください。\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "📋 *古物台帳　記載内容確認*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"📅 取引日時　：{session['timestamp']}\n\n"
                f"📦 品　　物　：{session['item_name']}\n\n"
                f"💴 買取価格　：¥{session['price']:,}\n\n"
                f"👤 氏　　名　：{id_info.get('name', '読取不可')}\n\n"
                f"🏠 住　　所　：{id_info.get('address', '読取不可')}\n\n"
                f"🎂 生年月日　：{id_info.get('birthdate', '読取不可')}\n\n"
                f"🪪 証明書番号：{id_info.get('id_number', '読取不可')}\n\n"
                f"📋 確認書類　：{id_info.get('doc_type', '運転免許証')}\n\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "✅ 正しければ `登録` と送信してください。\n"
                "✏️ 修正がある場合は\n"
                "　`修正 氏名：正しい名前`\n"
                "　のように送信してください。",
                mention_user=user_id, bot_role="genba")
        except Exception as e:
            print(f"[身分証読取エラー] {e}")
            post_to_slack(channel_id, current_ts,
                "⚠️ 身分証の読み取りに失敗いたしました。\n\n"
                "もう一度、鮮明な写真をお送りください。",
                mention_user=user_id, bot_role="genba")
        return True

    # ── Step 3: 登録確認 or 修正 ──
    if step == 3:
        n = normalize_keyword(text or "")
        if n == "登録":
            id_info = session.get("id_info", {})
            try:
                send_to_spreadsheet({
                    "action":     "kobutsu_daichou",
                    "timestamp":  session["timestamp"],
                    "item_name":  session["item_name"],
                    "price":      session["price"],
                    "staff_id":   session["staff_id"],
                    "name":       id_info.get("name", ""),
                    "address":    id_info.get("address", ""),
                    "birthdate":  id_info.get("birthdate", ""),
                    "id_number":  id_info.get("id_number", ""),
                    "doc_type":   id_info.get("doc_type", "運転免許証"),
                })
                del kaitori_sessions[session_key]
                post_to_slack(channel_id, current_ts,
                    "━━━━━━━━━━━━━━━━\n"
                    "✅ *古物台帳への記録が完了いたしました*\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    "道徳と算盤、両面から\n"
                    "適切な取引でありました。\n\n"
                    "スプレッドシートの\n"
                    "「古物台帳」シートをご確認ください。",
                    mention_user=user_id, bot_role="genba")
            except Exception as e:
                print(f"[古物台帳登録エラー] {e}")
                post_to_slack(channel_id, current_ts,
                    "⚠️ 記録に失敗いたしました。\n\n"
                    "もう一度 `登録` と送信してください。",
                    mention_user=user_id, bot_role="genba")
            return True

        # 修正コマンド処理
        fix = re.match(r'修正\s+(.+?)[:：](.+)', text or "")
        if fix:
            field_name = fix.group(1).strip()
            new_value = fix.group(2).strip()
            field_map = {
                "氏名": "name", "住所": "address",
                "生年月日": "birthdate", "証明書番号": "id_number",
                "確認書類": "doc_type",
            }
            field_key = field_map.get(field_name)
            if field_key:
                session["id_info"][field_key] = new_value
                kaitori_sessions[session_key] = session
            id_info = session["id_info"]
            post_to_slack(channel_id, current_ts,
                f"✏️ *{field_name}* を修正しました。\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "📋 *修正後の内容*\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"👤 氏　　名　：{id_info.get('name', '読取不可')}\n\n"
                f"🏠 住　　所　：{id_info.get('address', '読取不可')}\n\n"
                f"🎂 生年月日　：{id_info.get('birthdate', '読取不可')}\n\n"
                f"🪪 証明書番号：{id_info.get('id_number', '読取不可')}\n\n"
                f"📋 確認書類　：{id_info.get('doc_type', '運転免許証')}\n\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "✅ 正しければ `登録` と送信してください。",
                mention_user=user_id, bot_role="genba")
            return True

    return False


def handle_genba_channel(event: dict) -> None:
    """現場査定チャンネル（渋沢の算盤_現場の力）のイベントを処理する"""
    channel_id = event.get("channel")
    current_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or current_ts
    user_id = event.get("user", "")
    text = event.get("text", "")
    files = event.get("files", [])
    image_urls = [f.get("url_private") for f in files if f.get("url_private")]

    # テキストも画像もない場合はスキップ
    if not text and not image_urls:
        return

    # ── スクラップ計算（例：「スクラップ 鉄 45円 120kg」）──
    if _handle_scrap_calc(text, channel_id, current_ts, user_id):
        return

    # ── 古物台帳フローを最優先で処理 ──
    if _handle_kaitori_flow(event, channel_id, current_ts, user_id, text, image_urls):
        return

    # 知識インプット判定（「メモ」「情報」「覚えておいて」「相場」などのキーワード）
    memo_keywords = ["メモ", "情報", "覚えておいて", "相場", "業者", "単価", "注意", "ポイント", "コツ"]
    is_memo = any(kw in text for kw in memo_keywords)

    if is_memo and not image_urls:
        # 知識をスプレッドシートに保存
        try:
            send_to_spreadsheet({
                "action":    "genba_memo",
                "staff_id":  get_staff_code(user_id),
                "message":   text,
                "timestamp": datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        except Exception as e:
            print(f"[現場メモ保存エラー] {e}")
        post_to_slack(channel_id, current_ts,
            BOT_PERSONA["genba"]["memo_saved"],
            mention_user=user_id, bot_role="genba")
        return

    # 買取査定 or 廃棄判断 → Claudeに投げる
    post_to_slack(channel_id, current_ts,
        BOT_PERSONA["genba"]["thinking"],
        bot_role="genba")

    try:
        messages = []
        # 画像がある場合は画像を含める
        if image_urls:
            content = []
            if text:
                content.append({"type": "text", "text": text})
            for url in image_urls[:3]:  # 最大3枚
                try:
                    img_data, img_type = fetch_image_as_base64(url)
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": img_type, "data": img_data},
                    })
                except Exception as e:
                    print(f"[画像取得エラー] {e}")
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": text})

        client = get_anthropic_client()
        if not client:
            raise RuntimeError("ANTHROPIC_API_KEY が設定されていません")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=GENBA_SYSTEM_PROMPT,
            messages=messages,
        )
        result_text = response.content[0].text
        post_to_slack(channel_id, current_ts, result_text,
            mention_user=user_id, bot_role="genba")

        # スプレッドシートに査定記録を保存
        try:
            send_to_spreadsheet({
                "action":    "genba_satei",
                "staff_id":  get_staff_code(user_id),
                "input":     text[:200] if text else "（画像のみ）",
                "result":    result_text[:500],
                "timestamp": datetime.now().strftime("%Y/%m/%d %H:%M"),
            })
        except Exception as e:
            print(f"[現場査定記録エラー] {e}")

    except Exception as e:
        print(f"[現場査定エラー] {e}")
        post_to_slack(channel_id, current_ts,
            BOT_PERSONA["genba"]["error"],
            mention_user=user_id, bot_role="genba")
