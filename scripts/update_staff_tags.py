"""
scripts/update_staff_tags.py - スタッフマスタに業務タグ・雇用区分・権限カラムを追加し、データを更新
"""

import json
import time
import os
import httpx

MONDAY_API_URL = "https://api.monday.com/v2"
STAFF_BOARD_ID = 18405637105


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


def create_column_safe(token, board_id, title, col_type, col_id, defaults=None):
    query = """
    mutation ($board_id: ID!, $title: String!, $col_type: ColumnType!, $col_id: String!, $defaults: JSON) {
        create_column(board_id: $board_id, title: $title, column_type: $col_type, id: $col_id, defaults: $defaults) { id }
    }
    """
    variables = {"board_id": board_id, "title": title, "col_type": col_type, "col_id": col_id}
    if defaults:
        variables["defaults"] = json.dumps(defaults)
    result = monday_graphql(token, query, variables)
    if "errors" not in result:
        print(f"  ✅ カラム: {title}")
    time.sleep(0.3)


def get_all_items(token, board_id):
    """ボードの全アイテムを取得"""
    query = """
    query ($board_id: [ID!]!) {
        boards(ids: $board_id) {
            items_page(limit: 500) {
                items { id name }
            }
        }
    }
    """
    result = monday_graphql(token, query, {"board_id": [board_id]})
    boards = result.get("data", {}).get("boards", [])
    if boards:
        return boards[0].get("items_page", {}).get("items", [])
    return []


def update_item(token, board_id, item_id, col_vals):
    query = """
    mutation ($board_id: ID!, $item_id: ID!, $col_vals: JSON!) {
        change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $col_vals) { id }
    }
    """
    result = monday_graphql(token, query, {
        "board_id": board_id, "item_id": item_id,
        "col_vals": json.dumps(col_vals, ensure_ascii=False)
    })
    if "errors" in result:
        print(f"  ❌ ID:{item_id} - {str(result['errors'])[:80]}")
        return False
    return True


# 雇用区分ラベル
EMPLOYMENT_LABELS = {
    "labels": {
        "0": "役員",
        "1": "正社員",
        "2": "パート",
        "3": "業務委託",
    },
    "label_colors": {
        "0": {"color": "#e2445c", "border": "#ce3048"},
        "1": {"color": "#0086c0", "border": "#0073a8"},
        "2": {"color": "#fdab3d", "border": "#e99729"},
        "3": {"color": "#c4c4c4", "border": "#b0b0b0"},
    },
}

# 権限レベルラベル
PERMISSION_LABELS = {
    "labels": {
        "0": "管理者",
        "1": "一般",
        "2": "閲覧のみ",
    },
    "label_colors": {
        "0": {"color": "#e2445c", "border": "#ce3048"},
        "1": {"color": "#0086c0", "border": "#0073a8"},
        "2": {"color": "#c4c4c4", "border": "#b0b0b0"},
    },
}

# スタッフごとのタグ・区分データ
STAFF_DATA = {
    "浅野儀頼": {
        "tags": "経営, TB, CM, アスカラ, AIX, 営業, 判断・承認",
        "employment": 0,  # 役員
        "permission": 0,  # 管理者
        "boards": "全ボード",
    },
    "三島圭織": {
        "tags": "総務, 経理, 広報, 営業, HP/SNS",
        "employment": 1,  # 正社員
        "permission": 1,  # 一般
        "boards": "CMタスク管理, アスカラタスク管理, 経営管理タスク",
    },
    "林和人": {
        "tags": "TB出品, 撮影, 分荷, 梱包",
        "employment": 1,
        "permission": 1,
        "boards": "TBタスク管理, TB商品管理",
    },
    "横山優": {
        "tags": "TB撮影, 分荷, サイズ測定, 保管",
        "employment": 1,
        "permission": 1,
        "boards": "TBタスク管理, TB商品管理",
    },
    "平野光雄": {
        "tags": "TB現場, 撮影, 分荷",
        "employment": 1,
        "permission": 1,
        "boards": "TBタスク管理",
    },
    "桃井侑菜": {
        "tags": "TB出品, 撮影",
        "employment": 2,  # パート
        "permission": 1,
        "boards": "TBタスク管理, TB商品管理",
    },
    "伊藤佐和子": {
        "tags": "TB出品, 撮影, サイズ測定, 保管",
        "employment": 2,
        "permission": 1,
        "boards": "TBタスク管理, TB商品管理",
    },
    "奥村亜優李": {
        "tags": "TB出品, 撮影",
        "employment": 2,
        "permission": 1,
        "boards": "TBタスク管理, TB商品管理",
    },
    "松本豊彦": {
        "tags": "TB現場",
        "employment": 1,
        "permission": 1,
        "boards": "TBタスク管理",
    },
    "北瀬孝": {
        "tags": "TB現場",
        "employment": 1,
        "permission": 1,
        "boards": "TBタスク管理",
    },
    "白木雄介": {
        "tags": "TB",
        "employment": 1,
        "permission": 1,
        "boards": "TBタスク管理",
    },
}


def update_staff_tags(token: str):
    print("=" * 50)
    print("スタッフマスタ 業務タグ・権限追加")
    print("=" * 50)

    # カラム追加
    print("\n📋 カラム追加")
    create_column_safe(token, STAFF_BOARD_ID, "業務タグ", "tags", "staff_tags")
    create_column_safe(token, STAFF_BOARD_ID, "雇用区分", "status", "staff_employ", EMPLOYMENT_LABELS)
    create_column_safe(token, STAFF_BOARD_ID, "権限レベル", "status", "staff_perm", PERMISSION_LABELS)
    create_column_safe(token, STAFF_BOARD_ID, "担当ボード", "text", "staff_boards")

    # 既存アイテムを取得
    print("\n📋 スタッフデータ更新")
    items = get_all_items(token, STAFF_BOARD_ID)
    print(f"  {len(items)}名のスタッフを検出")

    for item in items:
        name = item["name"]
        data = STAFF_DATA.get(name)
        if not data:
            print(f"  ⏭️ {name}: データなし（スキップ）")
            continue

        # タグはカンマ区切りテキストで入れる（tags型はAPI制限あり）
        col_vals = {
            "staff_tags": {"tags": [{"tag": t.strip()} for t in data["tags"].split(",")]},
            "staff_employ": {"index": data["employment"]},
            "staff_perm": {"index": data["permission"]},
            "staff_boards": data["boards"],
        }
        if update_item(token, STAFF_BOARD_ID, item["id"], col_vals):
            print(f"  ✅ {name}")
        time.sleep(0.5)

    print("\n" + "=" * 50)
    print("スタッフマスタ更新完了！")
    print("=" * 50)


if __name__ == "__main__":
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("❌ MONDAY_TOKEN が設定されていません")
        exit(1)
    update_staff_tags(token)
