"""
services/spreadsheet.py - GAS経由のGoogleスプレッドシート転記
"""

import httpx

from config import GAS_URL


def send_to_spreadsheet(payload: dict) -> None:
    """GAS経由でGoogleスプレッドシートにデータを転記する"""
    response = httpx.post(GAS_URL, json=payload, timeout=30, follow_redirects=True)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"GAS error: {result.get('error')}")
    print("[スプレッドシート転記完了]")
