"""
utils/slack_thread.py - Slackスレッドからの情報取得ユーティリティ
"""

import re
import httpx

from config import get_slack_token


def fetch_thread_messages(channel_id: str, thread_ts: str, current_ts: str) -> list[dict]:
    """Slackスレッドの会話履歴を取得してClaude用のmessagesリストに変換する"""
    token = get_slack_token()
    if not token:
        return []
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"channel": channel_id, "ts": thread_ts}
    response = httpx.get(url, headers=headers, params=params, timeout=20)
    data = response.json()
    if not data.get("ok"):
        print(f"[スレッド履歴取得エラー] {data.get('error')}")
        return []

    messages = []
    for msg in data.get("messages", []):
        # 現在処理中のメッセージは除外（後で追加する）
        if msg.get("ts") == current_ts:
            continue
        text = msg.get("text", "").strip()
        if not text:
            continue
        role = "assistant" if (msg.get("bot_id") or msg.get("bot_profile")) else "user"
        # 直前と同じroleの場合はテキストを結合（Claudeは交互要求のため）
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += f"\n{text}"
        else:
            messages.append({"role": role, "content": text})

    # userから始まらないと Claude APIがエラーになるので調整
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    return messages


def get_judgment_from_thread(channel_id: str, thread_ts: str) -> dict:
    """スレッド内のBot判定メッセージから判定データを抽出する"""
    token = get_slack_token()
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"channel": channel_id, "ts": thread_ts}
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=20)
        data = response.json()
    except Exception as e:
        print(f"[判定データ取得エラー] API呼び出し失敗: {e}")
        return {}

    if not data.get("ok"):
        print(f"[判定データ取得エラー] Slack API応答: {data.get('error', '不明')}")
        return {}

    messages = data.get("messages", [])
    print(f"[判定データ取得] スレッド内メッセージ数: {len(messages)}")

    bot_messages_found = 0
    result = {}
    for msg in messages:
        is_bot = bool(msg.get("bot_id") or msg.get("bot_profile"))
        if not is_bot:
            continue
        bot_messages_found += 1
        text = msg.get("text", "")
        if "分荷判定結果" not in text:
            print(f"[判定データ取得] Botメッセージ(判定結果なし): {text[:80]}...")
            continue
        print(f"[判定データ取得] 判定結果メッセージ発見: {text[:120]}...")
        mn = re.search(r'管理番号\n　\*?(\d{4}(?:[VGME]\d{4}|-\d{4}))\*?', text)
        if mn:
            result["kanri_bango"] = mn.group(1)
        kw = re.search(r'推定内部KW：(/\S+)', text)
        if kw:
            result["internal_keyword"] = kw.group(1)
        # 新フォーマット：AI自動判定チャンネル（▶ or :arrow_forward: + 太字*対応）
        auto_ch = re.search(r'(?:▶|:arrow_forward:)?\s*\*?判定：(.+?)\*?\s*$', text, re.MULTILINE)
        if auto_ch:
            result["auto_channel"] = auto_ch.group(1).strip()
        # 浅野承認待ちフラグ
        if '承認待ち' in text or '承認が必要' in text:
            result["needs_approval"] = True
        # 旧フォーマット互換：第一候補/第二候補
        ch1 = re.search(r'【第一候補】(.+)', text)
        if ch1:
            result["first_channel"] = ch1.group(1).strip()
        ch2 = re.search(r'【第二候補】(.+)', text)
        if ch2:
            result["second_channel"] = ch2.group(1).strip()
        # スコア（最初に出てくるもの）
        score = re.search(r'総合スコア：(\d+)点', text)
        if score:
            result["first_score"] = score.group(1)
        # 予想価格
        price = re.search(r'予想販売価格：(¥[\d,]+〜¥[\d,]+)', text)
        if price:
            result["predicted_price"] = price.group(1)
        # 在庫期間（新フォーマット対応）
        period = re.search(r'予測在庫期間：(.+)', text)
        if period:
            result["inventory_period"] = period.group(1).strip()
        # 商品情報（絵文字なしで安定マッチ）
        item = re.search(r'アイテム名：(.+)', text)
        if item:
            result["item_name"] = item.group(1).strip()
        maker = re.search(r'メーカー/ブランド：(.+)', text)
        if maker:
            result["maker"] = maker.group(1).strip()
        model = re.search(r'品番/型式：(.+)', text)
        if model:
            result["model_number"] = model.group(1).strip()
        cond = re.search(r'状態ランク：([SABCD])（(.+?)）', text)
        if cond:
            result["condition"] = f"{cond.group(1)}（{cond.group(2)}）"
        # 新フィールド
        sp = re.search(r'推奨スタート価格：¥([\d,]+)', text)
        if sp:
            result["start_price"] = sp.group(1).replace(",", "")
        tp = re.search(r'推奨目標価格：¥([\d,]+)', text)
        if tp:
            result["target_price"] = tp.group(1).replace(",", "")
        deadline = re.search(r'推奨在庫期限：(.+)', text)
        if deadline:
            result["inventory_deadline"] = deadline.group(1).strip()
        sc = re.search(r'保管コスト概算：¥([\d,]+)', text)
        if sc:
            result["storage_cost"] = sc.group(1).replace(",", "")
        pc = re.search(r'梱包・発送コスト概算：¥([\d,]+)', text)
        if pc:
            result["packing_cost"] = pc.group(1).replace(",", "")
        roi = re.search(r'期待ROI：約([\d.]+)%', text)
        if roi:
            result["expected_roi"] = roi.group(1)
        # breakしない → 全メッセージを走査し、最新の判定（再判定含む）で上書きされる

    if not bot_messages_found:
        print(f"[判定データ取得] Botメッセージが1件も見つかりません")
    print(f"[判定データ取得] 抽出結果: auto_channel={result.get('auto_channel')}, first_channel={result.get('first_channel')}, keys={list(result.keys())}")
    return result


def get_confirmation_from_thread(channel_id: str, thread_ts: str) -> dict:
    """スレッド内のBot確定メッセージから管理番号と確定チャンネルを取得する。
    管理番号なしの確定（社内利用・スクラップ・廃棄・ロット販売）も検出する。
    戻り値: {"kanri_bango": str, "kakutei_channel": str}
    """
    token = get_slack_token()
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    response = httpx.get(url, headers=headers, params={"channel": channel_id, "ts": thread_ts}, timeout=10)
    for msg in response.json().get("messages", []):
        if not (msg.get("bot_id") or msg.get("bot_profile")):
            continue
        text = msg.get("text", "")
        # 確定メッセージかどうか判定（管理番号あり・なし両方）
        if "確定完了" not in text:
            continue
        kanri_bango = ""
        kakutei_channel = ""
        m_kanri = re.search(r'管理番号\n　\*?(\d{4}(?:[VGME]\d{4}|-\d{4}))\*?', text)
        if m_kanri:
            kanri_bango = m_kanri.group(1)
        m_channel = re.search(r'確定チャンネル\n　\*?(.+?)\*?(?:\n|$)', text)
        if m_channel:
            kakutei_channel = m_channel.group(1).strip()
        if kakutei_channel:
            return {"kanri_bango": kanri_bango, "kakutei_channel": kakutei_channel}
    return {"kanri_bango": "", "kakutei_channel": ""}


def get_confirmed_kanri_bango(channel_id: str, thread_ts: str) -> str:
    """後方互換用。get_confirmation_from_thread に委譲する"""
    return get_confirmation_from_thread(channel_id, thread_ts)["kanri_bango"]


def get_matome_pending_from_thread(channel_id: str, thread_ts: str):
    """スレッド内にまとめ売り選択待ちメッセージがあればチャンネル名を返す"""
    token = get_slack_token()
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"channel": channel_id, "ts": thread_ts}
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=20)
        data = response.json()
    except Exception as e:
        print(f"[まとめ選択待ち確認エラー] {e}")
        return None
    for msg in data.get("messages", []):
        if not (msg.get("bot_id") or msg.get("bot_profile")):
            continue
        m = re.search(r'\[まとめ選択待ち:([^\]]+)\]', msg.get("text", ""))
        if m:
            return m.group(1)
    return None


def has_bot_interaction(channel_id: str, thread_ts: str) -> bool:
    """スレッド内にBotメッセージが1件以上あるか確認する"""
    token = get_slack_token()
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"channel": channel_id, "ts": thread_ts}
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=10)
        data = response.json()
        for msg in data.get("messages", []):
            if msg.get("bot_id") or msg.get("bot_profile"):
                return True
    except Exception as e:
        print(f"[Bot応答確認エラー] {e}")
    return False
