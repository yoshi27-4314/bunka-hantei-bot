"""
scripts/backup_to_sheets.py - Monday.com全ボードをGoogleスプレッドシートにバックアップ

全ボードのアイテムを取得し、1つのスプレッドシートに各シートとして書き出す。
Make.comまたはcronで定期実行する想定。
"""

import os
import sys
import json
import time
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MONDAY_API_URL = "https://api.monday.com/v2"

# バックアップ対象ボード
BACKUP_BOARDS = {
    "取引先マスタ":        18405636938,
    "連絡先マスタ":        18405637012,
    "CMタスク管理":        18405995548,
    "TBタスク管理":        18405995566,
    "AIXタスク管理":       18405995588,
    "経営管理タスク":      18405995604,
    "アスカラタスク管理":  18405995636,
    "TB商品管理":          18404143384,
}


def monday_graphql(token: str, query: str, variables: dict = None) -> dict:
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "API-Version": "2023-10",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = httpx.post(MONDAY_API_URL, headers=headers, json=payload, timeout=60)
    return response.json()


def fetch_board_items(token: str, board_id: int) -> list:
    """ボードの全アイテムとカラム値を取得する"""
    all_items = []
    cursor = None
    page = 0

    while True:
        page += 1
        if cursor:
            query = """
            query ($cursor: String!) {
                next_items_page(cursor: $cursor, limit: 500) {
                    cursor
                    items {
                        id
                        name
                        group { title }
                        column_values { id title text }
                    }
                }
            }
            """
            result = monday_graphql(token, query, {"cursor": cursor})
            data = result.get("data", {}).get("next_items_page", {})
        else:
            query = """
            query ($board_id: [ID!]!) {
                boards(ids: $board_id) {
                    items_page(limit: 500) {
                        cursor
                        items {
                            id
                            name
                            group { title }
                            column_values { id title text }
                        }
                    }
                }
            }
            """
            result = monday_graphql(token, query, {"board_id": [board_id]})
            boards = result.get("data", {}).get("boards", [])
            data = boards[0].get("items_page", {}) if boards else {}

        items = data.get("items", [])
        all_items.extend(items)
        cursor = data.get("cursor")
        print(f"    ページ{page}: {len(items)}件取得（累計{len(all_items)}件）")

        if not cursor or not items:
            break
        time.sleep(0.5)

    return all_items


def items_to_rows(items: list) -> tuple:
    """Monday.comのアイテムをヘッダーと行データに変換する"""
    if not items:
        return [], []

    # ヘッダーを収集
    col_titles = []
    col_ids = []
    for cv in items[0].get("column_values", []):
        col_titles.append(cv["title"])
        col_ids.append(cv["id"])

    headers = ["ID", "アイテム名", "グループ"] + col_titles

    rows = []
    for item in items:
        row = [
            item["id"],
            item["name"],
            item.get("group", {}).get("title", ""),
        ]
        cv_map = {cv["id"]: cv.get("text", "") for cv in item.get("column_values", [])}
        for cid in col_ids:
            row.append(cv_map.get(cid, ""))
        rows.append(row)

    return headers, rows


def send_to_gas(gas_url: str, sheet_name: str, headers: list, rows: list) -> bool:
    """GAS WebアプリにデータをPOSTしてスプレッドシートに書き込む"""
    payload = {
        "action": "backup_sheet",
        "sheet_name": sheet_name,
        "headers": headers,
        "rows": rows,
    }
    try:
        response = httpx.post(gas_url, json=payload, timeout=120, follow_redirects=True)
        result = response.json() if response.status_code == 200 else {}
        if result.get("ok"):
            print(f"  ✅ {sheet_name}: {len(rows)}件書き込み完了")
            return True
        else:
            print(f"  ❌ {sheet_name}: GASエラー {result}")
            return False
    except Exception as e:
        print(f"  ❌ {sheet_name}: 通信エラー {e}")
        return False


def backup_all(token: str, gas_url: str):
    """全ボードをバックアップする"""
    print("=" * 50)
    print("Monday.com → スプレッドシート バックアップ")
    print("=" * 50)

    for sheet_name, board_id in BACKUP_BOARDS.items():
        print(f"\n📋 {sheet_name} (Board: {board_id})")
        items = fetch_board_items(token, board_id)
        if not items:
            print(f"  ⏭️ アイテムなし。スキップ")
            continue
        headers, rows = items_to_rows(items)
        send_to_gas(gas_url, sheet_name, headers, rows)
        time.sleep(1)

    print("\n" + "=" * 50)
    print("バックアップ完了！")
    print("=" * 50)


if __name__ == "__main__":
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    gas_url = os.environ.get("GAS_URL", "")
    if not token:
        print("❌ MONDAY_TOKEN が設定されていません")
        sys.exit(1)
    if not gas_url:
        print("❌ GAS_URL が設定されていません")
        sys.exit(1)
    backup_all(token, gas_url)
