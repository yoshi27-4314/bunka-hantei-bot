"""
migrate_to_monday.py - boardの顧客・案件データをMonday.comに一括移行

対象:
  - clients.json → 統括WS 取引先ボード (18405636938)
  - projects.json → CM WS 案件管理ボード (18405637284)
"""

import json
import time
import os
import sys
import httpx

MONDAY_API_URL = "https://api.monday.com/v2"

# Monday.com ボードID
TORIHIKI_BOARD_ID = 18405636938   # 統括 > 取引先
ANKEN_BOARD_ID = 18405637284      # CM > 案件管理


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


def create_column_safe(token: str, board_id: int, title: str, col_type: str, col_id: str):
    """カラムを作成（既存ならスキップ）"""
    query = """
    mutation ($board_id: ID!, $title: String!, $col_type: ColumnType!, $col_id: String!) {
        create_column(board_id: $board_id, title: $title, column_type: $col_type, id: $col_id) { id }
    }
    """
    result = monday_graphql(token, query, {
        "board_id": board_id, "title": title, "col_type": col_type, "col_id": col_id
    })
    if "errors" not in result:
        print(f"  ✅ カラム作成: {title}")
    time.sleep(0.3)


def create_item(token: str, board_id: int, group_id: str, name: str, col_vals: dict) -> int:
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
    return int(result["data"]["create_item"]["id"])


def setup_torihiki_columns(token: str):
    """取引先ボードのカラムを作成"""
    print("📋 取引先ボード カラム作成")
    cols = [
        ("表示名", "text", "cli_disp"),
        ("敬称", "text", "cli_title"),
        ("郵便番号", "text", "cli_zip"),
        ("都道府県", "text", "cli_pref"),
        ("住所1", "text", "cli_addr1"),
        ("住所2", "text", "cli_addr2"),
        ("電話番号", "text", "cli_tel"),
        ("FAX", "text", "cli_fax"),
        ("支払条件", "text", "cli_payment"),
        ("法人番号", "text", "cli_corp_no"),
        ("board_ID", "text", "cli_board_id"),
        ("登録日", "date", "cli_created"),
        ("更新日", "date", "cli_updated"),
    ]
    for title, col_type, col_id in cols:
        create_column_safe(token, TORIHIKI_BOARD_ID, title, col_type, col_id)


def setup_anken_columns(token: str):
    """案件管理ボードのカラムを作成"""
    print("📋 案件管理ボード カラム作成")
    cols = [
        ("案件番号", "text", "prj_no"),
        ("取引先名", "text", "prj_client"),
        ("担当者名", "text", "prj_contact"),
        ("営業担当", "text", "prj_user"),
        ("税抜合計", "numbers", "prj_total"),
        ("税額", "numbers", "prj_tax"),
        ("見積日", "date", "prj_est_date"),
        ("請求日", "text", "prj_inv_date"),
        ("受注状況", "text", "prj_order_st"),
        ("進行状況", "text", "prj_deliv_st"),
        ("工事種別", "text", "prj_type"),
        ("board_ID", "text", "prj_board_id"),
        ("登録日", "date", "prj_created"),
        ("更新日", "date", "prj_updated"),
    ]
    for title, col_type, col_id in cols:
        create_column_safe(token, ANKEN_BOARD_ID, title, col_type, col_id)


def migrate_clients(token: str, data_path: str):
    """取引先データをMonday.comに移行"""
    clients = json.load(open(data_path, encoding="utf-8"))
    print(f"\n📋 取引先移行: {len(clients)}件")

    id_map = {}  # board_id → monday_item_id
    for i, c in enumerate(clients):
        name = c.get("name", "") or c.get("name_disp", "")
        if not name:
            continue

        col_vals = {
            "cli_disp": c.get("name_disp", "") or "",
            "cli_title": c.get("title", "") or "",
            "cli_zip": c.get("zip", "") or "",
            "cli_pref": c.get("pref", "") or "",
            "cli_addr1": c.get("address1", "") or "",
            "cli_addr2": c.get("address2", "") or "",
            "cli_tel": c.get("tel", "") or "",
            "cli_fax": c.get("fax", "") or "",
            "cli_payment": c.get("payment_term_name", "") or "",
            "cli_corp_no": c.get("company_number", "") or "",
            "cli_board_id": str(c.get("id", "")),
        }
        # 日付カラム
        created = (c.get("created_at") or "")[:10]
        updated = (c.get("updated_at") or "")[:10]
        if created:
            col_vals["cli_created"] = {"date": created}
        if updated:
            col_vals["cli_updated"] = {"date": updated}

        item_id = create_item(token, TORIHIKI_BOARD_ID, "topics", name, col_vals)
        if item_id:
            id_map[str(c["id"])] = item_id

        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(clients)} 完了")
        time.sleep(0.4)  # API制限対策

    print(f"  ✅ 取引先移行完了: {len(id_map)}/{len(clients)}件")

    # ID対応表を保存
    with open(os.path.join(os.path.dirname(data_path), "torihiki_id_map.json"), "w") as f:
        json.dump(id_map, f, ensure_ascii=False, indent=2)

    return id_map


def migrate_projects(token: str, data_path: str):
    """案件データをMonday.comに移行"""
    projects = json.load(open(data_path, encoding="utf-8"))
    print(f"\n📋 案件移行: {len(projects)}件")

    count = 0
    for i, p in enumerate(projects):
        name = p.get("name", "")
        if not name:
            continue

        client = p.get("client") or {}
        contact = p.get("contact") or {}
        user = p.get("user") or {}

        col_vals = {
            "prj_no": str(p.get("project_no", "")),
            "prj_client": client.get("name", "") or "",
            "prj_contact": f"{contact.get('last_name', '')} {contact.get('first_name', '')}".strip(),
            "prj_user": f"{user.get('last_name', '')} {user.get('first_name', '')}".strip(),
            "prj_order_st": p.get("order_status_name", "") or "",
            "prj_deliv_st": p.get("delivery_status_name", "") or "",
            "prj_type": p.get("project_type2_name", "") or "",
            "prj_board_id": str(p.get("id", "")),
        }

        # 数値カラム
        total = p.get("total")
        if total:
            try:
                col_vals["prj_total"] = float(total)
            except (ValueError, TypeError):
                pass
        tax = p.get("tax")
        if tax:
            try:
                col_vals["prj_tax"] = float(tax)
            except (ValueError, TypeError):
                pass

        # 日付カラム
        est_date = p.get("estimate_date", "")
        if est_date:
            col_vals["prj_est_date"] = {"date": est_date[:10]}

        inv_dates = p.get("invoice_dates") or []
        if inv_dates:
            col_vals["prj_inv_date"] = ", ".join(inv_dates)

        created = (p.get("created_at") or "")[:10]
        updated = (p.get("updated_at") or "")[:10]
        if created:
            col_vals["prj_created"] = {"date": created}
        if updated:
            col_vals["prj_updated"] = {"date": updated}

        item_id = create_item(token, ANKEN_BOARD_ID, "topics", name, col_vals)
        if item_id:
            count += 1

        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(projects)} 完了")
        time.sleep(0.4)

    print(f"  ✅ 案件移行完了: {count}/{len(projects)}件")


def run_migration(token: str):
    # migration_dataサブフォルダにデータがある
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migration_data")

    print("=" * 50)
    print("board → Monday.com データ移行")
    print("=" * 50)

    # カラム作成
    setup_torihiki_columns(token)
    setup_anken_columns(token)

    # データ移行
    migrate_clients(token, os.path.join(data_dir, "clients.json"))
    migrate_projects(token, os.path.join(data_dir, "projects.json"))

    print("\n" + "=" * 50)
    print("全データ移行完了！")
    print("=" * 50)


if __name__ == "__main__":
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("❌ MONDAY_TOKEN が設定されていません")
        sys.exit(1)
    run_migration(token)
