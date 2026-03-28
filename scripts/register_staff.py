"""
scripts/register_staff.py - スタッフマスタボードにスタッフ情報を登録
"""

import json
import time
import os
import httpx

MONDAY_API_URL = "https://api.monday.com/v2"
STAFF_BOARD_ID = 18405637105  # 統括 > スタッフマスタ


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


def create_column_safe(token, board_id, title, col_type, col_id):
    query = """
    mutation ($board_id: ID!, $title: String!, $col_type: ColumnType!, $col_id: String!) {
        create_column(board_id: $board_id, title: $title, column_type: $col_type, id: $col_id) { id }
    }
    """
    result = monday_graphql(token, query, {
        "board_id": board_id, "title": title, "col_type": col_type, "col_id": col_id
    })
    if "errors" not in result:
        print(f"  ✅ カラム: {title}")
    time.sleep(0.3)


def create_item(token, board_id, group_id, name, col_vals):
    query = """
    mutation ($board_id: ID!, $group_id: String!, $name: String!, $col_vals: JSON!) {
        create_item(board_id: $board_id, group_id: $group_id, item_name: $name, column_values: $col_vals) { id }
    }
    """
    result = monday_graphql(token, query, {
        "board_id": board_id, "group_id": group_id,
        "name": name, "col_vals": json.dumps(col_vals, ensure_ascii=False)
    })
    if "errors" in result:
        print(f"  ❌ {name}: {str(result['errors'])[:80]}")
        return 0
    item_id = int(result["data"]["create_item"]["id"])
    print(f"  ✅ {name} (ID: {item_id})")
    return item_id


def register_staff(token: str):
    print("=" * 50)
    print("スタッフマスタ登録")
    print("=" * 50)

    # カラム作成
    print("\n📋 カラム作成")
    columns = [
        ("所属（雇用）", "text", "staff_dept"),
        ("実務担当", "text", "staff_actual"),
        ("役割", "text", "staff_role"),
        ("Slack UserID", "text", "staff_slack"),
        ("Monday.comアカウント", "text", "staff_monday"),
        ("メールアドレス", "email", "staff_email"),
        ("電話番号", "phone", "staff_phone"),
        ("備考", "long_text", "staff_note"),
    ]
    for title, col_type, col_id in columns:
        create_column_safe(token, STAFF_BOARD_ID, title, col_type, col_id)

    # スタッフデータ
    staff_list = [
        {
            "name": "浅野儀頼",
            "dept": "クリアメンテ",
            "actual": "全事業部",
            "role": "代表取締役CEO",
            "slack": "U0AL10Q1HQC",
            "monday": "登録済み",
            "note": "全業務の最終判断者",
        },
        {
            "name": "三島圭織",
            "dept": "クリアメンテ",
            "actual": "TB/CM/アスカラ全般",
            "role": "総務・経理・営業・広報・雑務",
            "slack": "U0AMXQ8JH6V",
            "monday": "登録済み",
            "note": "自転車のみ。事務作業未経験。HP/SNS作業中（4月2週目完了予定）",
        },
        {
            "name": "林和人",
            "dept": "テイクバック",
            "actual": "テイクバック",
            "role": "出品・撮影・分荷",
            "slack": "U0ALQ4BJNSV",
            "monday": "登録済み",
            "note": "",
        },
        {
            "name": "横山優",
            "dept": "テイクバック",
            "actual": "テイクバック",
            "role": "撮影・分荷",
            "slack": "U0ALHCGD3U7",
            "monday": "登録済み",
            "note": "",
        },
        {
            "name": "平野光雄",
            "dept": "クリアメンテ",
            "actual": "テイクバック",
            "role": "現場作業",
            "slack": "U0AL4R1EMMZ",
            "monday": "未登録",
            "note": "雇用はCM。経営不安のためTB業務に移行",
        },
        {
            "name": "桃井侑菜",
            "dept": "テイクバック",
            "actual": "テイクバック",
            "role": "出品・撮影",
            "slack": "U0ALKDQEC2F",
            "monday": "未登録",
            "note": "",
        },
        {
            "name": "伊藤佐和子",
            "dept": "テイクバック",
            "actual": "テイクバック",
            "role": "出品・撮影",
            "slack": "U0ALV7C2EHJ",
            "monday": "未登録",
            "note": "",
        },
        {
            "name": "奥村亜優李",
            "dept": "テイクバック",
            "actual": "テイクバック",
            "role": "出品・撮影",
            "slack": "U0AM4HG1PRP",
            "monday": "未登録",
            "note": "",
        },
        {
            "name": "松本豊彦",
            "dept": "クリアメンテ",
            "actual": "テイクバック",
            "role": "現場作業",
            "slack": "未登録",
            "monday": "未登録",
            "note": "雇用はCM。経営不安のためTB業務に移行。Slack UserID未取得",
        },
        {
            "name": "北瀬孝",
            "dept": "クリアメンテ",
            "actual": "テイクバック",
            "role": "現場作業",
            "slack": "未登録",
            "monday": "未登録",
            "note": "雇用はCM。経営不安のためTB業務に移行。Slack UserID未取得",
        },
        {
            "name": "白木雄介",
            "dept": "テイクバック",
            "actual": "テイクバック",
            "role": "",
            "slack": "未登録",
            "monday": "未登録",
            "note": "Slack UserID未取得",
        },
    ]

    print("\n📋 スタッフ登録")
    for s in staff_list:
        col_vals = {
            "staff_dept": s["dept"],
            "staff_actual": s["actual"],
            "staff_role": s["role"],
            "staff_slack": s["slack"],
            "staff_monday": s["monday"],
        }
        if s["note"]:
            col_vals["staff_note"] = {"text": s["note"]}
        create_item(token, STAFF_BOARD_ID, "topics", s["name"], col_vals)
        time.sleep(0.5)

    print("\n" + "=" * 50)
    print("スタッフマスタ登録完了！")
    print("=" * 50)


if __name__ == "__main__":
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("❌ MONDAY_TOKEN が設定されていません")
        exit(1)
    register_staff(token)
