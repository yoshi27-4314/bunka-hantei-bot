"""
services/spreadsheet.py - GAS経由のGoogleスプレッドシート転記
"""

import httpx

from config import get_gas_url


def send_to_spreadsheet(payload: dict) -> None:
    """GAS経由でGoogleスプレッドシートにデータを転記する"""
    gas_url = get_gas_url()
    if not gas_url:
        raise RuntimeError("GAS_URL が未設定です")
    response = httpx.post(gas_url, json=payload, timeout=30, follow_redirects=True)
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"GAS error: {result.get('error')}")
    print("[スプレッドシート転記完了]")
