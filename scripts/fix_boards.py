"""
scripts/fix_boards.py - ボードの修正（間違ったアイテムの削除・正しい状態への修正）
"""

import json
import time
import os
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


def get_all_items(token, board_id):
    """ボードの全アイテムを取得"""
    query = """
    query ($board_id: [ID!]!) {
        boards(ids: $board_id) {
            groups { id title }
            items_page(limit: 500) {
                items { id name group { id title } }
            }
        }
    }
    """
    result = monday_graphql(token, query, {"board_id": [board_id]})
    boards = result.get("data", {}).get("boards", [])
    if boards:
        return boards[0]
    return {}


def delete_item(token, item_id):
    query = """
    mutation ($item_id: ID!) {
        delete_item(item_id: $item_id) { id }
    }
    """
    result = monday_graphql(token, query, {"item_id": item_id})
    return "errors" not in result


def delete_group(token, board_id, group_id):
    query = """
    mutation ($board_id: ID!, $group_id: String!) {
        delete_group(board_id: $board_id, group_id: $group_id) { id }
    }
    """
    result = monday_graphql(token, query, {"board_id": board_id, "group_id": group_id})
    return "errors" not in result


def fix_asano_board(token):
    """よしさんの全体タスク管理ボードからスキルマップ項目を削除"""
    ASANO_BOARD_ID = 18405995746
    print("=" * 50)
    print("全体タスク管理ボード修正")
    print("=" * 50)

    board_data = get_all_items(token, ASANO_BOARD_ID)
    items = board_data.get("items_page", {}).get("items", [])
    groups = board_data.get("groups", [])

    print(f"\n現在のアイテム数: {len(items)}")
    print(f"現在のグループ数: {len(groups)}")

    # スキルマップのグループ（【TB】【CM】【アスカラ】【全社】で始まるもの）を特定
    skill_groups = []
    task_groups = []
    for g in groups:
        title = g["title"]
        if any(title.startswith(prefix) for prefix in ["【TB】", "【CM】", "【アスカラ】", "【全社】"]):
            skill_groups.append(g)
        else:
            task_groups.append(g)

    print(f"\nスキルマップのグループ: {len(skill_groups)}個")
    for g in skill_groups:
        print(f"  削除対象: {g['title']}")

    print(f"\nタスク管理のグループ（残す）: {len(task_groups)}個")
    for g in task_groups:
        print(f"  残す: {g['title']}")

    # スキルマップグループに属するアイテムを削除
    skill_group_ids = {g["id"] for g in skill_groups}
    delete_count = 0
    for item in items:
        if item.get("group", {}).get("id") in skill_group_ids:
            if delete_item(token, item["id"]):
                delete_count += 1
            time.sleep(0.3)

    print(f"\n✅ {delete_count}件のスキルマップ項目を削除")

    # 空になったスキルマップグループも削除
    for g in skill_groups:
        if delete_group(token, ASANO_BOARD_ID, g["id"]):
            print(f"  ✅ グループ削除: {g['title']}")
        time.sleep(0.3)

    print("\n" + "=" * 50)
    print("修正完了！")
    print("=" * 50)


def verify_all_boards(token):
    """全ボードの状態を確認してレポートを出す"""
    boards = {
        "CMタスク管理": 18405995548,
        "TBタスク管理": 18405995566,
        "AIXタスク管理": 18405995588,
        "経営管理タスク": 18405995604,
        "アスカラタスク管理": 18405995636,
        "全体タスク管理（よしさん）": 18405995746,
    }

    print("=" * 50)
    print("全ボード状態レポート")
    print("=" * 50)

    for name, bid in boards.items():
        board_data = get_all_items(token, bid)
        items = board_data.get("items_page", {}).get("items", [])
        groups = board_data.get("groups", [])
        print(f"\n📋 {name} (ID: {bid})")
        print(f"  グループ: {len(groups)}個")
        for g in groups:
            group_items = [i for i in items if i.get("group", {}).get("id") == g["id"]]
            print(f"    {g['title']}: {len(group_items)}件")


def verify_all_boards_json(token):
    """全ボードの状態をJSON形式で返す（API検証用）"""
    boards = {
        "CMタスク管理": 18405995548,
        "TBタスク管理": 18405995566,
        "AIXタスク管理": 18405995588,
        "経営管理タスク": 18405995604,
        "アスカラタスク管理": 18405995636,
        "全体タスク管理": 18405995746,
        "取引先": 18405636938,
        "連絡先": 18405637012,
        "スタッフマスタ": 18405637105,
        "CM案件管理": 18405637284,
    }

    result = {}
    for name, bid in boards.items():
        board_data = get_all_items(token, bid)
        items = board_data.get("items_page", {}).get("items", [])
        groups = board_data.get("groups", [])
        group_detail = {}
        for g in groups:
            group_items = [i["name"] for i in items if i.get("group", {}).get("id") == g["id"]]
            group_detail[g["title"]] = {"count": len(group_items), "items": group_items[:5]}
        result[name] = {
            "board_id": bid,
            "total_items": len(items),
            "total_groups": len(groups),
            "groups": group_detail,
        }
    return result


if __name__ == "__main__":
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("❌ MONDAY_TOKEN が設定されていません")
        exit(1)

    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        verify_all_boards(token)
    else:
        fix_asano_board(token)
        print("\n")
        verify_all_boards(token)
