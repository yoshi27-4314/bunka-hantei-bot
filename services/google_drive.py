"""
services/google_drive.py - Google Drive API操作（フォルダ作成・画像アップロード・一覧・削除・上書き）
"""

import os
import re
import json
import base64
import httpx

from config import get_slack_token


def get_drive_service():
    """Google Drive APIサービスを返す。認証情報未設定の場合はNone"""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    json_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not json_b64:
        print("[Google Drive] GOOGLE_SERVICE_ACCOUNT_JSON 未設定・スキップ")
        return None
    try:
        creds_dict = json.loads(base64.b64decode(json_b64).decode())
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"[Google Drive認証エラー] {e}")
        return None


def get_or_create_drive_folder(service, parent_id: str, folder_name: str) -> str:
    """指定フォルダ内のサブフォルダを取得または作成してIDを返す"""
    query = (
        f"name='{folder_name}' and '{parent_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return folder["id"]


def upload_images_to_drive(management_number: str, image_urls: list, is_tepura: bool = False) -> str:
    """画像をGoogle Driveの管理番号フォルダにアップロードし、フォルダURLを返す"""
    import io
    from googleapiclient.http import MediaIoBaseUpload

    service = get_drive_service()
    root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    if not service or not root_folder_id:
        return ""

    # フォルダ構成: TakeBack商品画像/YYMM/管理番号/
    yymm = management_number[:4]
    yymm_id = get_or_create_drive_folder(service, root_folder_id, yymm)
    item_id = get_or_create_drive_folder(service, yymm_id, management_number)

    # 既存ファイル数から採番開始番号を決定
    existing = service.files().list(
        q=f"'{item_id}' in parents and trashed=false",
        fields="files(name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute().get("files", [])
    next_num = len(existing) + 1

    token = get_slack_token()
    headers = {"Authorization": f"Bearer {token}"}
    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}

    for i, url in enumerate(image_urls):
        try:
            resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            ext = ext_map.get(content_type, "jpg")
            filename = f"01_テプラ.{ext}" if is_tepura else f"{next_num + i:02d}_商品.{ext}"
            media = MediaIoBaseUpload(io.BytesIO(resp.content), mimetype=content_type)
            service.files().create(
                body={"name": filename, "parents": [item_id]},
                media_body=media, fields="id",
                supportsAllDrives=True
            ).execute()
            print(f"[Drive] {filename} アップロード完了")
        except Exception as e:
            print(f"[Drive] アップロードエラー: {e}")

    return f"https://drive.google.com/drive/folders/{item_id}"


def get_drive_folder_id(management_number: str):
    """管理番号からDriveフォルダIDを取得する。存在しない場合はNone"""
    service = get_drive_service()
    root_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    if not service or not root_folder_id:
        return None, None
    yymm = management_number[:4]
    try:
        # YYMMフォルダを検索
        query = f"name='{yymm}' and '{root_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        yymm_files = results.get("files", [])
        if not yymm_files:
            return None, service
        # 管理番号フォルダを検索
        query = f"name='{management_number}' and '{yymm_files[0]['id']}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        item_files = results.get("files", [])
        if not item_files:
            return None, service
        return item_files[0]["id"], service
    except Exception as e:
        print(f"[Drive] フォルダ検索エラー: {e}")
        return None, service


def list_drive_images(management_number: str, exclude_tepura: bool = True) -> list:
    """管理番号のDriveフォルダ内の画像一覧を返す。テプラ除外オプション付き"""
    folder_id, service = get_drive_folder_id(management_number)
    if not folder_id or not service:
        return []
    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType contains 'image/'",
            fields="files(id, name, webViewLink, webContentLink)",
            orderBy="name",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        files = results.get("files", [])
        if exclude_tepura:
            files = [f for f in files if "テプラ" not in f.get("name", "")]
        return files
    except Exception as e:
        print(f"[Drive] 画像一覧取得エラー: {e}")
        return []


def download_first_product_image(management_number: str) -> tuple:
    """管理番号フォルダの1枚目の商品画像（テプラ以外）をダウンロードして(bytes, filename)を返す"""
    images = list_drive_images(management_number, exclude_tepura=True)
    if not images:
        return None, None
    first_image = images[0]
    file_id = first_image["id"]
    filename = first_image.get("name", "main.jpg")
    service = get_drive_service()
    if not service:
        return None, None
    try:
        content = service.files().get_media(
            fileId=file_id, supportsAllDrives=True
        ).execute()
        print(f"[Drive] メイン写真ダウンロード: {management_number}/{filename} ({len(content)}bytes)")
        return content, filename
    except Exception as e:
        print(f"[Drive] メイン写真ダウンロードエラー: {e}")
        return None, None


def delete_drive_file(file_id: str) -> bool:
    """Driveファイルをゴミ箱に移動（削除）"""
    service = get_drive_service()
    if not service:
        return False
    try:
        service.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
        return True
    except Exception as e:
        print(f"[Drive] ファイル削除エラー: {e}")
        return False


def upload_shuppinon_image(management_number: str, image_urls: list) -> list:
    """出品チャンネルから追加撮影した画像をDriveにアップロード（sp01_出品追加.jpg形式）"""
    import io
    from googleapiclient.http import MediaIoBaseUpload

    folder_id, service = get_drive_folder_id(management_number)
    if not folder_id or not service:
        return []

    # 既存のsp付きファイル数を取得して採番
    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false and name contains 'sp'",
            fields="files(name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        existing_sp = [f for f in results.get("files", []) if re.match(r'^sp\d+_', f["name"])]
        next_num = len(existing_sp) + 1
    except Exception:
        next_num = 1

    token = get_slack_token()
    headers = {"Authorization": f"Bearer {token}"}
    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
    uploaded = []

    for i, url in enumerate(image_urls):
        try:
            resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            ext = ext_map.get(content_type, "jpg")
            filename = f"sp{next_num + i:02d}_出品追加.{ext}"
            media = MediaIoBaseUpload(io.BytesIO(resp.content), mimetype=content_type)
            result = service.files().create(
                body={"name": filename, "parents": [folder_id]},
                media_body=media, fields="id,name",
                supportsAllDrives=True
            ).execute()
            uploaded.append(result)
            print(f"[Drive] {filename} 出品追加アップロード完了")
        except Exception as e:
            print(f"[Drive] 出品追加アップロードエラー: {e}")

    return uploaded


def replace_drive_file(file_id: str, image_url: str) -> bool:
    """既存のDriveファイルを新画像で上書き"""
    import io
    from googleapiclient.http import MediaIoBaseUpload

    service = get_drive_service()
    if not service:
        return False

    token = get_slack_token()
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = httpx.get(image_url, headers=headers, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        media = MediaIoBaseUpload(io.BytesIO(resp.content), mimetype=content_type)
        service.files().update(
            fileId=file_id, media_body=media,
            supportsAllDrives=True
        ).execute()
        return True
    except Exception as e:
        print(f"[Drive] ファイル上書きエラー: {e}")
        return False
