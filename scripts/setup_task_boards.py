"""
scripts/setup_task_boards.py - Monday.com タスク管理ボード一括作成スクリプト

使い方:
  Railway環境で実行: python scripts/setup_task_boards.py
  または app.py に管理エンドポイントとして組み込み
"""

import os
import sys
import json
import time
import httpx

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def create_board(token: str, name: str, workspace_id: int, board_kind: str = "public") -> int:
    """ボードを作成してIDを返す"""
    query = """
    mutation ($name: String!, $kind: BoardKind!, $ws_id: ID!) {
        create_board(board_name: $name, board_kind: $kind, workspace_id: $ws_id) {
            id
        }
    }
    """
    result = monday_graphql(token, query, {
        "name": name, "kind": board_kind, "ws_id": workspace_id
    })
    if "errors" in result:
        print(f"  エラー: {result['errors']}")
        return 0
    board_id = int(result["data"]["create_board"]["id"])
    print(f"  ✅ ボード作成: {name} (ID: {board_id})")
    return board_id


def create_group(token: str, board_id: int, group_name: str) -> str:
    """グループを作成してIDを返す"""
    query = """
    mutation ($board_id: ID!, $group_name: String!) {
        create_group(board_id: $board_id, group_name: $group_name) {
            id
        }
    }
    """
    result = monday_graphql(token, query, {
        "board_id": board_id, "group_name": group_name
    })
    if "errors" in result:
        print(f"    エラー: {result['errors']}")
        return ""
    group_id = result["data"]["create_group"]["id"]
    print(f"    📁 グループ: {group_name}")
    return group_id


def create_column(token: str, board_id: int, title: str, col_type: str, col_id: str, defaults: dict = None) -> bool:
    """カラムを作成する"""
    query = """
    mutation ($board_id: ID!, $title: String!, $col_type: ColumnType!, $col_id: String!, $defaults: JSON) {
        create_column(board_id: $board_id, title: $title, column_type: $col_type, id: $col_id, defaults: $defaults) {
            id
        }
    }
    """
    variables = {
        "board_id": board_id, "title": title,
        "col_type": col_type, "col_id": col_id,
    }
    if defaults:
        variables["defaults"] = json.dumps(defaults)
    result = monday_graphql(token, query, variables)
    if "errors" in result:
        err = str(result["errors"])
        if "already exists" in err or "duplicate" in err.lower():
            print(f"    ⏭️ スキップ（既存）: {title}")
        else:
            print(f"    ❌ エラー: {title} - {err[:100]}")
        return False
    print(f"    ✅ カラム: {title}")
    return True


# ステータスラベルの設定
STATUS_LABELS = {
    "labels": {
        "0": "未着手",
        "1": "対応中",
        "2": "社外：相手待ち",
        "3": "社内：対応必要",
        "4": "完了",
    },
    "label_colors": {
        "0": {"color": "#c4c4c4", "border": "#b0b0b0"},  # グレー
        "1": {"color": "#0086c0", "border": "#0073a8"},  # 青
        "2": {"color": "#fdab3d", "border": "#e99729"},  # オレンジ
        "3": {"color": "#e2445c", "border": "#ce3048"},  # 赤
        "4": {"color": "#00c875", "border": "#00b461"},  # 緑
    },
}

PRIORITY_LABELS = {
    "labels": {
        "0": "高",
        "1": "中",
        "2": "低",
    },
    "label_colors": {
        "0": {"color": "#e2445c", "border": "#ce3048"},  # 赤
        "1": {"color": "#fdab3d", "border": "#e99729"},  # オレンジ
        "2": {"color": "#0086c0", "border": "#0073a8"},  # 青
    },
}


def setup_common_columns(token: str, board_id: int):
    """共通カラムを作成"""
    time.sleep(0.5)
    create_column(token, board_id, "担当者", "people", "task_person")
    time.sleep(0.3)
    create_column(token, board_id, "ステータス", "status", "task_status", STATUS_LABELS)
    time.sleep(0.3)
    create_column(token, board_id, "期限", "date", "task_deadline")
    time.sleep(0.3)
    create_column(token, board_id, "優先度", "status", "task_priority", PRIORITY_LABELS)
    time.sleep(0.3)
    create_column(token, board_id, "プロジェクト名", "text", "task_project")
    time.sleep(0.3)
    create_column(token, board_id, "顧客・案件", "text", "task_customer")
    time.sleep(0.3)
    create_column(token, board_id, "メモ", "long_text", "task_memo")
    time.sleep(0.3)


def setup_task_boards(token: str):
    """全タスクボードを一括作成"""
    print("=" * 50)
    print("Monday.com タスク管理ボード 一括作成")
    print("=" * 50)

    created_boards = {}

    # ── 事業部タスクボード ──

    # CM タスク管理（クリアメンテ WS: 14821104）
    print("\n📋 CMタスク管理")
    bid = create_board(token, "CMタスク管理", 14821104)
    if bid:
        created_boards["cm_task"] = bid
        setup_common_columns(token, bid)
        create_column(token, bid, "案件の種類", "dropdown", "cm_type")
        time.sleep(0.3)
        create_column(token, bid, "金額", "numbers", "cm_amount")
        time.sleep(0.3)
        for g in ["案件受付", "見積", "工事進行中", "完了・請求済み"]:
            create_group(token, bid, g)
            time.sleep(0.3)

    # TB タスク管理（テイクバック商品 WS: 既存 → TB案件WS: 14821113 に入れる）
    print("\n📋 TBタスク管理")
    bid = create_board(token, "TBタスク管理", 14821113)
    if bid:
        created_boards["tb_task"] = bid
        setup_common_columns(token, bid)
        create_column(token, bid, "管理番号", "text", "tb_kanri")
        time.sleep(0.3)
        create_column(token, bid, "工程", "status", "tb_process")
        time.sleep(0.3)
        for g in ["分荷・撮影待ち", "出品中", "売れた・梱包待ち", "発送完了"]:
            create_group(token, bid, g)
            time.sleep(0.3)

    # AIX タスク管理（AIX WS: 14821475）
    print("\n📋 AIXタスク管理")
    bid = create_board(token, "AIXタスク管理", 14821475)
    if bid:
        created_boards["aix_task"] = bid
        setup_common_columns(token, bid)
        create_column(token, bid, "種類", "dropdown", "aix_type")
        time.sleep(0.3)
        for g in ["開発タスク", "打ち合わせ予定", "提案中案件"]:
            create_group(token, bid, g)
            time.sleep(0.3)

    # 経営管理タスク（HQ WS: 14821118）
    print("\n📋 経営管理タスク")
    bid = create_board(token, "経営管理タスク", 14821118)
    if bid:
        created_boards["hq_task"] = bid
        setup_common_columns(token, bid)
        create_column(token, bid, "分類", "dropdown", "hq_category")
        time.sleep(0.3)
        for g in ["従業員・人材", "資金・財務", "新規事業", "外部連携・仕入先"]:
            create_group(token, bid, g)
            time.sleep(0.3)

    # アスカラ タスク管理（アスカラ WS: 14821101）
    print("\n📋 アスカラタスク管理")
    bid = create_board(token, "アスカラタスク管理", 14821101)
    if bid:
        created_boards["askara_task"] = bid
        setup_common_columns(token, bid)
        for g in ["リード対応", "案件振り分け", "フォローアップ"]:
            create_group(token, bid, g)
            time.sleep(0.3)

    # ── 個人プライベートボード ──
    staff_boards = [
        ("平野さん専用", 14821104),    # CM
        ("三島さん専用", 14821104),    # CM
        ("林さん専用", 14821113),      # TB
        ("横山さん専用", 14821113),    # TB
        ("桃井さん専用", 14821113),    # TB
        ("伊藤さん専用", 14821113),    # TB
    ]

    print("\n📋 個人プライベートボード")
    for name, ws_id in staff_boards:
        bid = create_board(token, name, ws_id, "private")
        if bid:
            created_boards[f"private_{name}"] = bid
            setup_common_columns(token, bid)
            # KPIグループ
            create_group(token, bid, "今月のタスク")
            time.sleep(0.3)
            create_group(token, bid, "KPI・目標")
            time.sleep(0.3)
            # KPIカラム
            create_column(token, bid, "目標", "numbers", "kpi_target")
            time.sleep(0.3)
            create_column(token, bid, "実績", "numbers", "kpi_actual")
            time.sleep(0.3)

    # ── よしさんの全体タスク管理ボード（WS: 13210571）──
    print("\n📋 よしさんの全体タスク管理")
    bid = create_board(token, "全体タスク管理", 13210571, "private")
    if bid:
        created_boards["asano_all"] = bid
        setup_common_columns(token, bid)
        create_column(token, bid, "事業部", "dropdown", "division")
        time.sleep(0.3)
        for g in ["全事業部タスク", "プライベート", "勉強・インプット", "異業種交流会"]:
            create_group(token, bid, g)
            time.sleep(0.3)

    # ── 結果出力 ──
    print("\n" + "=" * 50)
    print("作成完了！")
    print("=" * 50)
    for key, bid in created_boards.items():
        print(f"  {key}: {bid}")

    return created_boards


if __name__ == "__main__":
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("❌ MONDAY_TOKEN が設定されていません")
        sys.exit(1)
    setup_task_boards(token)
