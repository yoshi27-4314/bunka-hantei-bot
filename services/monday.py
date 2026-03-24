"""
services/monday.py - Monday.com GraphQL API操作（管理番号生成・登録・検索・更新）
"""

import json
import re
import threading
import httpx
from datetime import datetime

from config import (
    get_monday_token, MONDAY_BOARD_ID, MONDAY_OLD_BOARD_IDS,
    MONDAY_API_URL, STAFF_MAP, get_staff_code,
)


def monday_graphql(query: str, variables: dict = None) -> dict:
    """monday.com GraphQL APIを呼び出す"""
    token = get_monday_token()
    if not token:
        raise RuntimeError("MONDAY_API_TOKEN が設定されていません")
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "API-Version": "2023-10",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = httpx.post(MONDAY_API_URL, headers=headers, json=payload, timeout=25)
    return response.json()


def get_monthly_sequence() -> int:
    """今月の管理番号通し番号をmonday.comのアイテム数から取得する（全チャンネル共通）"""
    yymm = datetime.now().strftime("%y%m")
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items {
                    column_values(ids: ["kanri_bango"]) {
                        text
                    }
                }
            }
        }
    }
    """
    try:
        result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
        items = (result.get("data", {})
                 .get("boards", [{}])[0]
                 .get("items_page", {})
                 .get("items", []))
        count = sum(
            1 for item in items
            if item.get("column_values", [{}])[0].get("text", "").startswith(yymm)
        )
        return count + 1
    except Exception as e:
        print(f"[通し番号取得エラー] {e}")
        return int(datetime.now().strftime("%S%f")[:4]) + 1


# 管理番号の重複発行防止
# Monday.comへの登録遅延・失敗で同じ番号が発行されるのを防ぐ
_management_number_lock = threading.Lock()
_issued_numbers: set[str] = set()  # このプロセスセッションで発行済みの管理番号


def generate_management_number() -> str:
    """管理番号を生成する（例：2603-0001）
    西暦下2桁 + 月2桁 + ハイフン + 月次通し番号4桁（チャンネル共通連番）
    ※ロット販売・社内利用・スクラップ・廃棄は管理番号なし
    重複防止: Lockで同時発行をブロック + セッション内発行済みセットで衝突回避
    """
    with _management_number_lock:
        yymm = datetime.now().strftime("%y%m")
        seq = get_monthly_sequence()
        # Monday.comにまだ反映されていない番号との衝突を検知してインクリメント
        while True:
            candidate = f"{yymm}-{seq:04d}"
            if candidate not in _issued_numbers:
                _issued_numbers.add(candidate)
                print(f"[管理番号発行] {candidate} (発行済みセット: {len(_issued_numbers)}件)")
                return candidate
            print(f"[管理番号衝突] {candidate} は発行済み → seq+1 して再試行")
            seq += 1


def extract_judgment(response_text: str) -> dict:
    """Claude応答から判定結果を抽出する"""
    result = {
        "first_channel": "",
        "first_confidence": "",
        "second_channel": "",
        "first_score": "",
        "second_score": "",
        "internal_keyword": "",
        "item_name": "",
        "maker": "",
        "model_number": "",
        "condition": "",
        "start_price": "",
        "target_price": "",
        "inventory_deadline": "",
        "storage_cost": "",
        "packing_cost": "",
        "expected_roi": "",
    }
    score_count = 0
    in_first = False
    for line in response_text.split("\n"):
        line = line.strip()
        if line.startswith("【第一候補】"):
            result["first_channel"] = line.replace("【第一候補】", "").strip()
            in_first = True
        elif line.startswith("【第二候補】"):
            result["second_channel"] = line.replace("【第二候補】", "").strip()
            in_first = False
        elif "総合スコア：" in line:
            score_count += 1
            match = re.search(r"(\d+)点", line)
            if match:
                score = int(match.group(1))
                if score_count == 1:
                    result["first_score"] = str(score)
                    result["first_confidence"] = "高" if score >= 75 else ("中" if score >= 50 else "低")
                else:
                    result["second_score"] = str(score)
        elif "推定内部KW：" in line:
            kw_match = re.search(r"(/\S+)", line)
            if kw_match:
                result["internal_keyword"] = kw_match.group(1)
        elif line.startswith("📋 アイテム名："):
            result["item_name"] = line.replace("📋 アイテム名：", "").strip()
        elif line.startswith("🏭 メーカー/ブランド："):
            result["maker"] = line.replace("🏭 メーカー/ブランド：", "").strip()
        elif line.startswith("🔢 品番/型式："):
            result["model_number"] = line.replace("🔢 品番/型式：", "").strip()
        elif line.startswith("📊 状態："):
            result["condition"] = line.replace("📊 状態：", "").strip()
        elif "推奨スタート価格：" in line and in_first:
            m = re.search(r"¥([\d,]+)", line)
            if m:
                result["start_price"] = m.group(1).replace(",", "")
        elif "推奨目標価格：" in line and in_first:
            m = re.search(r"¥([\d,]+)", line)
            if m:
                result["target_price"] = m.group(1).replace(",", "")
        elif "推奨在庫期限：" in line and in_first:
            result["inventory_deadline"] = line.split("推奨在庫期限：", 1)[1].strip()
        elif "保管コスト概算：" in line and in_first:
            m = re.search(r"¥([\d,]+)", line)
            if m:
                result["storage_cost"] = m.group(1).replace(",", "")
        elif "梱包・発送コスト概算：" in line and in_first:
            m = re.search(r"¥([\d,]+)", line)
            if m:
                result["packing_cost"] = m.group(1).replace(",", "")
        elif "期待ROI：" in line and in_first:
            m = re.search(r"約([\d.]+)%", line)
            if m:
                result["expected_roi"] = m.group(1)
    return result


def register_to_monday(management_number: str, item_name: str, judgment: dict, user_id: str, sakugyou_jikan: int = 0, kakutei_channel: str = "") -> None:
    """monday.comにアイテムを登録する"""
    # 予想販売価格から数値を抽出（例: "¥5,000〜¥8,000" → "5000"）
    price_str = judgment.get("predicted_price", "")
    price_num = ""
    if price_str:
        m = re.search(r'[\d,]+', price_str.replace("¥", "").replace(",", ""))
        if m:
            price_num = re.sub(r'[^\d]', '', m.group(0))

    col = {
        "kanri_bango": management_number,
        "hantei_channel": kakutei_channel or judgment.get("first_channel", ""),
        "toshosha": get_staff_code(user_id),
        "zaiko_kikan": judgment.get("inventory_period", ""),
        "status": {"label": "分荷確定"},
        "bunka_date": {"date": datetime.now().strftime("%Y-%m-%d")},
    }
    if price_num:
        col["yosou_kakaku"] = price_num
    if judgment.get("first_score"):
        col["score"] = judgment.get("first_score")
    if sakugyou_jikan > 0:
        col["sakugyou_jikan"] = sakugyou_jikan
    if judgment.get("internal_keyword"):
        col["internal_keyword"] = judgment.get("internal_keyword")
    if judgment.get("maker"):
        col["maker"] = judgment.get("maker")
    if judgment.get("model_number"):
        col["model_number"] = judgment.get("model_number")
    if judgment.get("condition"):
        col["condition"] = judgment.get("condition")
    if judgment.get("start_price"):
        col["kaishi_kakaku"] = judgment.get("start_price")
    if judgment.get("target_price"):
        col["mokuhyo_kakaku"] = judgment.get("target_price")
    column_values = json.dumps(col, ensure_ascii=False)

    query = """
    mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {
        create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) {
            id
        }
    }
    """
    result = monday_graphql(query, {
        "board_id": MONDAY_BOARD_ID,
        "item_name": item_name[:50],
        "column_values": column_values,
    })
    if "errors" in result:
        raise RuntimeError(f"Monday.com API error: {result['errors']}")
    item_id = result.get("data", {}).get("create_item", {}).get("id")
    print(f"[Monday.com] アイテム作成完了 ID={item_id}")


def cancel_monday_item(kanri_bango: str) -> None:
    """monday.comの該当アイテムのステータスをキャンセルに変更する"""
    # 管理番号でアイテムを検索
    search_query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items { id column_values(ids: ["kanri_bango"]) { text } }
            }
        }
    }
    """
    result = monday_graphql(search_query, {"board_id": MONDAY_BOARD_ID})
    items = (result.get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    item_id = next(
        (i["id"] for i in items
         if i.get("column_values", [{}])[0].get("text") == kanri_bango),
        None
    )
    if not item_id:
        print(f"[Monday.com] 管理番号 {kanri_bango} のアイテムが見つかりません")
        return
    # ステータスをキャンセルに更新
    update_query = """
    mutation ($board_id: ID!, $item_id: ID!, $col_vals: JSON!) {
        change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $col_vals) { id }
    }
    """
    col_vals = json.dumps({"status": {"label": "キャンセル"}}, ensure_ascii=False)
    monday_graphql(update_query, {"board_id": MONDAY_BOARD_ID, "item_id": item_id, "col_vals": col_vals})
    print(f"[Monday.com] {kanri_bango} をキャンセルに更新")


def _find_monday_item_id(management_number: str) -> str | None:
    """管理番号でMonday.comアイテムIDを検索する"""
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items { id column_values(ids: ["kanri_bango"]) { text } }
            }
        }
    }
    """
    result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
    items = (result.get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    return next(
        (i["id"] for i in items
         if i.get("column_values", [{}])[0].get("text") == management_number),
        None
    )


def update_monday_columns(management_number: str, col_updates: dict) -> None:
    """Monday.comの指定アイテムの複数カラムを一括更新する"""
    item_id = _find_monday_item_id(management_number)
    if not item_id:
        print(f"[Monday.com] 管理番号 {management_number} が見つかりません")
        return
    col_vals = json.dumps(col_updates, ensure_ascii=False)
    query = """
    mutation ($board_id: ID!, $item_id: ID!, $col_vals: JSON!) {
        change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $col_vals) { id }
    }
    """
    monday_graphql(query, {"board_id": MONDAY_BOARD_ID, "item_id": item_id, "col_vals": col_vals})
    print(f"[Monday.com] {management_number} を更新: {list(col_updates.keys())}")


def update_monday_item_status(management_number: str, status_label: str) -> None:
    """monday.comの該当アイテムのstatusを更新する"""
    search_query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items { id column_values(ids: ["kanri_bango"]) { text } }
            }
        }
    }
    """
    result = monday_graphql(search_query, {"board_id": MONDAY_BOARD_ID})
    items = (result.get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    item_id = next(
        (i["id"] for i in items
         if i.get("column_values", [{}])[0].get("text") == management_number),
        None
    )
    if not item_id:
        print(f"[Monday.com] 管理番号 {management_number} が見つかりません")
        return
    col_vals = json.dumps({"status": {"label": status_label}}, ensure_ascii=False)
    update_query = """
    mutation ($board_id: ID!, $item_id: ID!, $col_vals: JSON!) {
        change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $col_vals) { id }
    }
    """
    monday_graphql(update_query, {"board_id": MONDAY_BOARD_ID, "item_id": item_id, "col_vals": col_vals})
    print(f"[Monday.com] {management_number} のステータスを「{status_label}」に更新")


def search_inventory(keyword: str) -> list[dict]:
    """Monday.comのリスト作成ボードからキーワードで在庫検索する"""
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items {
                    id
                    name
                    column_values(ids: ["kanri_bango", "hantei_channel", "zaiko_kikan", "status"]) {
                        id
                        text
                    }
                }
            }
        }
    }
    """
    try:
        result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
        items = (result.get("data", {})
                 .get("boards", [{}])[0]
                 .get("items_page", {})
                 .get("items", []))
        kw_lower = keyword.lower()
        matched = []
        for item in items:
            name = item.get("name", "")
            col_map = {c["id"]: c["text"] for c in item.get("column_values", [])}
            kanri = col_map.get("kanri_bango", "")
            channel = col_map.get("hantei_channel", "")
            zaiko = col_map.get("zaiko_kikan", "")
            status = col_map.get("status", "")
            # アイテム名・管理番号・チャンネルにキーワードが含まれるものを返す
            search_target = f"{name} {kanri} {channel}".lower()
            if kw_lower in search_target:
                matched.append({
                    "id": item.get("id"),
                    "name": name,
                    "kanri_bango": kanri,
                    "channel": channel,
                    "zaiko_kikan": zaiko,
                    "status": status,
                })
        return matched
    except Exception as e:
        print(f"[在庫検索エラー] {e}")
        return []


def get_item_from_monday(management_number: str) -> dict:
    """monday.comから管理番号に対応するアイテムデータを取得する（新ボード→旧ボードの順に検索）"""
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items {
                    name
                    column_values { id text }
                }
            }
        }
    }
    """
    result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
    items = (result.get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))
    for item in items:
        cols = {cv["id"]: cv["text"] for cv in item.get("column_values", [])}
        if cols.get("kanri_bango") == management_number:
            return {"monday_name": item["name"], "is_old_board": False, **cols}
    # 新ボードになければ旧ボードを検索
    return get_item_from_old_boards(management_number)


def get_item_from_old_boards(management_number: str) -> dict:
    """旧Monday.comボード（2枚）から管理番号に対応するアイテムを検索する"""
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items {
                    id
                    name
                    column_values { id text }
                }
            }
        }
    }
    """
    for board_id in MONDAY_OLD_BOARD_IDS:
        try:
            result = monday_graphql(query, {"board_id": board_id})
            items = (result.get("data", {}).get("boards", [{}])[0]
                     .get("items_page", {}).get("items", []))
            for item in items:
                cols = {cv["id"]: cv["text"] for cv in item.get("column_values", [])}
                # アイテム名または全列の値から管理番号を検索
                found = (management_number in item.get("name", "") or
                         any(v == management_number for v in cols.values()))
                if found:
                    print(f"[旧ボード発見] board={board_id} item={item['name']}")
                    return {
                        "monday_name":     item["name"],
                        "monday_item_id":  item["id"],
                        "monday_board_id": board_id,
                        "is_old_board":    True,
                        **cols,
                    }
        except Exception as e:
            print(f"[旧ボード検索エラー board={board_id}] {e}")
    return {}
