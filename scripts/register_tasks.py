"""
scripts/register_tasks.py - 初期タスクをMonday.comに一括登録する
"""

import os
import sys
import json
import time
import httpx

MONDAY_API_URL = "https://api.monday.com/v2"


def monday_graphql(token: str, query: str, variables: dict = None) -> dict:
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "API-Version": "2023-10",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = httpx.post(MONDAY_API_URL, headers=headers, json=payload, timeout=30)
    return response.json()


def create_item(token: str, board_id: int, group_id: str, item_name: str, column_values: dict) -> int:
    """アイテム（タスク）を作成する"""
    query = """
    mutation ($board_id: ID!, $group_id: String!, $item_name: String!, $col_vals: JSON!) {
        create_item(board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $col_vals) {
            id
        }
    }
    """
    col_vals = json.dumps(column_values, ensure_ascii=False)
    result = monday_graphql(token, query, {
        "board_id": board_id,
        "group_id": group_id,
        "item_name": item_name,
        "col_vals": col_vals,
    })
    if "errors" in result:
        print(f"  ❌ {item_name}: {result['errors']}")
        return 0
    item_id = int(result["data"]["create_item"]["id"])
    print(f"  ✅ {item_name} (ID: {item_id})")
    return item_id


def get_group_ids(token: str, board_id: int) -> dict:
    """ボードのグループID一覧を取得"""
    query = """
    query ($board_id: [ID!]!) {
        boards(ids: $board_id) {
            groups { id title }
        }
    }
    """
    result = monday_graphql(token, query, {"board_id": [board_id]})
    groups = {}
    for g in result.get("data", {}).get("boards", [{}])[0].get("groups", []):
        groups[g["title"]] = g["id"]
    return groups


# ステータスのインデックス: 0=未着手, 1=対応中, 2=社外：相手待ち, 3=社内：対応必要, 4=完了
# 優先度のインデックス: 0=高, 1=中, 2=低

def status_val(index: int) -> dict:
    return {"task_status": {"index": index}}

def priority_val(index: int) -> dict:
    return {"task_priority": {"index": index}}

def make_columns(status_idx: int, priority_idx: int, memo: str = "") -> dict:
    cols = {}
    cols["task_status"] = {"index": status_idx}
    cols["task_priority"] = {"index": priority_idx}
    if memo:
        cols["task_memo"] = {"text": memo}
    return cols


def register_all_tasks(token: str):
    """全タスクを一括登録"""
    print("=" * 50)
    print("Monday.com タスク一括登録")
    print("=" * 50)

    # ボードID（setup_task_boardsで作成済み）
    BOARDS = {
        "tb": 18405995566,
        "cm": 18405995548,
        "aix": 18405995588,
        "hq": 18405995604,
        "askara": 18405995636,
    }

    # ── テイクバック ──
    print("\n📋 テイクバック")
    groups = get_group_ids(token, BOARDS["tb"])
    # 最初のグループに入れる
    gid = list(groups.values())[0] if groups else "topics"
    tb_tasks = [
        ("林さん承認待ち商品「確定」再送", 0, 0, "管理番号未発行分"),
        ("撮影済み商品のサイズ・棚番号入力", 0, 0, "林・伊藤で対応。月曜指示済み"),
        ("オークファン連携", 0, 1, "4/1契約開始"),
        ("販売完了履歴シート自動転記", 0, 1, ""),
        ("出品CSV自動生成", 0, 1, ""),
        ("既存在庫の再分荷の仕組み作り", 0, 2, "実装後の作業はスタッフ"),
        ("ヤフオクAPI自動出品の設定", 0, 2, "設定は浅野。作業はスタッフ"),
    ]
    for name, status, priority, memo in tb_tasks:
        create_item(token, BOARDS["tb"], gid, name, make_columns(status, priority, memo))
        time.sleep(0.5)

    # ── クリアメンテ ──
    print("\n📋 クリアメンテ")
    groups = get_group_ids(token, BOARDS["cm"])
    gid = list(groups.values())[0] if groups else "topics"
    cm_tasks = [
        ("boardデータ移行（顧客498件・案件811件）", 0, 0, ""),
        ("CM案件管理ボードの運用開始", 0, 1, ""),
        ("見積・請求PDF生成の仕組み構築", 0, 1, "Monday.com + Claude AI"),
        ("board解約", 0, 1, "移行完了後"),
    ]
    for name, status, priority, memo in cm_tasks:
        create_item(token, BOARDS["cm"], gid, name, make_columns(status, priority, memo))
        time.sleep(0.5)

    # ── AIX・開発 ──
    print("\n📋 AIX・開発")
    groups = get_group_ids(token, BOARDS["aix"])
    gid = list(groups.values())[0] if groups else "topics"
    aix_tasks = [
        ("Monday.comタスク同期（Make.com設定）", 0, 0, ""),
        ("分荷判定Bot 統一コマンドのテスト", 0, 0, "スタッフに依頼"),
        ("OKABE GROUP タイヤホテル 商談フォロー", 2, 1, ""),
        ("Rork/RorkMaxでアプリ制作", 0, 1, ""),
    ]
    for name, status, priority, memo in aix_tasks:
        create_item(token, BOARDS["aix"], gid, name, make_columns(status, priority, memo))
        time.sleep(0.5)

    # ── 経営管理 ──
    print("\n📋 経営管理")
    groups = get_group_ids(token, BOARDS["hq"])
    gid = list(groups.values())[0] if groups else "topics"
    hq_tasks = [
        ("三島さん業務マニュアル作成", 0, 0, "4月中旬〜開始"),
        ("ツール統合整理（TimeTree/Gカレンダー/board/Monday.com）", 0, 1, ""),
        ("Make.com → n8n 移行判断", 0, 2, ""),
        ("管理会計・インセンティブ設計", 0, 2, ""),
        ("Slack UserID登録（松本・北瀬・白木）", 0, 0, "TBから移動"),
        ("Make.com日次サマリー設定", 0, 0, "毎朝8:55にPOST"),
        ("Slack DM権限追加", 0, 0, "im:write/im:read/message.im"),
    ]
    for name, status, priority, memo in hq_tasks:
        create_item(token, BOARDS["hq"], gid, name, make_columns(status, priority, memo))
        time.sleep(0.5)

    # ── アスカラ ──
    print("\n📋 アスカラ")
    groups = get_group_ids(token, BOARDS["askara"])
    gid = list(groups.values())[0] if groups else "topics"
    askara_tasks = [
        ("リード獲得の仕組みづくり", 0, 1, ""),
        ("HP作成・実装", 1, 0, "三島担当"),
    ]
    for name, status, priority, memo in askara_tasks:
        create_item(token, BOARDS["askara"], gid, name, make_columns(status, priority, memo))
        time.sleep(0.5)

    print("\n" + "=" * 50)
    print("全タスク登録完了！")
    print("=" * 50)


if __name__ == "__main__":
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("❌ MONDAY_TOKEN が設定されていません")
        sys.exit(1)
    register_all_tasks(token)
