"""
scripts/fix_drive_urls.py - Monday.comのdrive_urlが空の商品に、Google DriveのフォルダURLを一括書き込み

使い方:
  cd 分荷判定Bot
  python scripts/fix_drive_urls.py

動作:
  1. Monday.comからdrive_urlが空の全商品を取得
  2. 各商品の管理番号でGoogle Driveのフォルダを検索
  3. フォルダがあればURLをMonday.comに書き込み
  4. 結果をレポート表示
"""

import os
import sys
import json
import time

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.monday import monday_graphql, update_monday_columns
from services.google_drive import get_drive_folder_id
from config import MONDAY_BOARD_ID


def get_items_without_drive_url():
    """Monday.comからdrive_urlが空のアイテムを全件取得"""
    query = """
    query ($board_id: ID!) {
        boards(ids: [$board_id]) {
            items_page(limit: 500) {
                items {
                    id
                    name
                    column_values(ids: ["kanri_bango", "drive_url"]) {
                        id
                        text
                    }
                }
            }
        }
    }
    """
    result = monday_graphql(query, {"board_id": MONDAY_BOARD_ID})
    items = (result.get("data", {}).get("boards", [{}])[0]
             .get("items_page", {}).get("items", []))

    missing = []
    for item in items:
        cols = {c["id"]: c["text"] for c in item.get("column_values", [])}
        kanri = cols.get("kanri_bango", "")
        drive_url = cols.get("drive_url", "")
        if kanri and not drive_url:
            missing.append({
                "item_id": item["id"],
                "name": item["name"],
                "kanri_bango": kanri,
            })
    return missing


def fix_drive_urls():
    """drive_urlが空の商品にGoogle DriveのURLを一括書き込み"""
    print("=" * 60)
    print("Monday.com drive_url 一括修復ツール")
    print("=" * 60)

    # Step 1: drive_urlが空の商品を取得
    print("\n[1/3] Monday.comからdrive_urlが空の商品を検索中...")
    missing_items = get_items_without_drive_url()
    print(f"  → {len(missing_items)}件見つかりました")

    if not missing_items:
        print("\n✅ 全商品にdrive_urlが設定済みです。修復不要です。")
        return

    # Step 2: Google Driveでフォルダを検索して書き込み
    print(f"\n[2/3] Google Driveのフォルダを検索してMonday.comに書き込み中...")
    success = 0
    not_found = 0
    errors = 0

    for i, item in enumerate(missing_items, 1):
        kanri = item["kanri_bango"]
        print(f"  [{i}/{len(missing_items)}] {kanri} ({item['name']})... ", end="")

        try:
            folder_id, service = get_drive_folder_id(kanri)
            if folder_id:
                folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
                update_monday_columns(kanri, {"drive_url": folder_url})
                print(f"✅ 書き込み完了")
                success += 1
            else:
                print(f"⬜ Driveにフォルダなし")
                not_found += 1
        except Exception as e:
            print(f"❌ エラー: {e}")
            errors += 1

        # Monday.com API制限を考慮して少し待つ
        if i % 10 == 0:
            time.sleep(1)

    # Step 3: 結果レポート
    print(f"\n[3/3] 結果レポート")
    print("=" * 60)
    print(f"  対象商品数:     {len(missing_items)}件")
    print(f"  ✅ 書き込み成功: {success}件")
    print(f"  ⬜ Driveにフォルダなし: {not_found}件")
    print(f"  ❌ エラー:       {errors}件")
    print("=" * 60)

    if success > 0:
        print(f"\n✅ {success}件のdrive_urlを修復しました！")
    if not_found > 0:
        print(f"\n⚠️ {not_found}件はDriveにフォルダがありませんでした。")
        print("  （撮影がまだの商品、またはDriveに別の名前で保存されている可能性があります）")


if __name__ == "__main__":
    fix_drive_urls()
