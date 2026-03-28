"""
scripts/scan_shared_drives.py - 共有ドライブの全フォルダ・ファイル構造を取得してスプレッドシートに書き出す
"""

import os
import sys
import json
import base64
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SHARED_DRIVES = {
    "ファーストエイトグループ": "0AI37ylzbUb03Uk9PVA",
    "株式会社クリアメンテ": "0AJU5lZiRWPpPUk9PVA",
    "株式会社テイクバック": "0AML2BP9kkAcmUk9PVA",
}


def get_drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    json_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not json_b64:
        return None
    creds_dict = json.loads(base64.b64decode(json_b64).decode())
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)


def list_all_files(service, drive_id, parent_id=None, path="", depth=0, results=None):
    """再帰的に全ファイル・フォルダを取得"""
    if results is None:
        results = []
    if depth > 10:
        return results

    query = f"'{parent_id or drive_id}' in parents and trashed=false"
    page_token = None

    while True:
        try:
            response = service.files().list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime)",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="drive",
                driveId=drive_id,
            ).execute()
        except Exception as e:
            print(f"  ❌ エラー: {e}")
            break

        files = response.get("files", [])
        for f in files:
            is_folder = f["mimeType"] == "application/vnd.google-apps.folder"
            full_path = f"{path}/{f['name']}" if path else f['name']
            results.append({
                "path": full_path,
                "name": f["name"],
                "type": "フォルダ" if is_folder else _get_file_type(f["mimeType"]),
                "size": f.get("size", ""),
                "created": (f.get("createdTime") or "")[:10],
                "modified": (f.get("modifiedTime") or "")[:10],
                "depth": depth,
                "id": f["id"],
            })

            if is_folder:
                time.sleep(0.2)
                list_all_files(service, drive_id, f["id"], full_path, depth + 1, results)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


def _get_file_type(mime_type):
    type_map = {
        "application/vnd.google-apps.spreadsheet": "スプレッドシート",
        "application/vnd.google-apps.document": "ドキュメント",
        "application/vnd.google-apps.presentation": "スライド",
        "application/vnd.google-apps.form": "フォーム",
        "application/pdf": "PDF",
        "image/jpeg": "画像(JPG)",
        "image/png": "画像(PNG)",
        "audio/mpeg": "音声(MP3)",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
    }
    return type_map.get(mime_type, mime_type.split("/")[-1])


def scan_all_drives():
    print("=" * 50)
    print("共有ドライブ 全構造スキャン")
    print("=" * 50)

    service = get_drive_service()
    if not service:
        print("❌ Google Drive認証に失敗")
        return {}

    all_results = {}
    for drive_name, drive_id in SHARED_DRIVES.items():
        print(f"\n📋 {drive_name} (ID: {drive_id})")
        files = list_all_files(service, drive_id)
        all_results[drive_name] = files
        print(f"  ✅ {len(files)}件のファイル・フォルダを検出")

    # GASに送信してスプレッドシートに書き出す
    import httpx
    gas_url = os.environ.get("GAS_URL", "")
    if gas_url:
        for drive_name, files in all_results.items():
            sheet_name = f"ドライブ構造_{drive_name}"
            headers = ["パス", "名前", "種類", "サイズ", "作成日", "更新日", "階層", "ID"]
            rows = [[f["path"], f["name"], f["type"], f["size"], f["created"], f["modified"], f["depth"], f["id"]] for f in files]
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
                    print(f"  ✅ スプレッドシート書き出し完了: {sheet_name}（{len(rows)}件）")
                else:
                    print(f"  ❌ GASエラー: {result}")
            except Exception as e:
                print(f"  ❌ 通信エラー: {e}")
            time.sleep(1)

    print("\n" + "=" * 50)
    print("全ドライブスキャン完了！")
    print("=" * 50)

    return all_results


if __name__ == "__main__":
    scan_all_drives()
