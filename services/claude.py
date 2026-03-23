"""
services/claude.py - Claude API呼び出し（画像取得・分荷判定メイン）
"""

import base64
import httpx

from config import get_anthropic_client, get_slack_token
from prompts import SYSTEM_PROMPT


def fetch_image_as_base64(image_url: str) -> tuple[str, str]:
    """画像URLをダウンロードしてbase64エンコードとメディアタイプを返す"""
    headers = {"Authorization": f"Bearer {get_slack_token()}"}
    response = httpx.get(image_url, headers=headers, timeout=30, follow_redirects=True)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if content_type not in supported:
        content_type = "image/jpeg"

    image_data = base64.standard_b64encode(response.content).decode("utf-8")
    return image_data, content_type


def call_claude(user_message: str, image_urls: list[str] | None = None, history: list[dict] | None = None) -> str:
    """Claude APIを呼び出して分荷判定を返す"""
    # 現在のメッセージのコンテンツを組み立て
    current_content = []

    # 画像を全て追加（複数対応）
    failed_images = []
    for image_url in (image_urls or []):
        try:
            image_data, media_type = fetch_image_as_base64(image_url)
            current_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data,
                },
            })
        except Exception as e:
            failed_images.append(str(e))

    # テキストを追加
    text = user_message
    if image_urls:
        text = f"添付画像も参考にして判定してください。\n\n{user_message}"
    if failed_images:
        text += f"\n\n※一部画像の取得に失敗しました: {', '.join(failed_images)}"
    current_content.append({"type": "text", "text": text})

    # 会話履歴 + 現在のメッセージを組み立て
    messages = list(history) if history else []
    messages.append({"role": "user", "content": current_content})

    client = get_anthropic_client()
    if not client:
        raise RuntimeError("ANTHROPIC_API_KEY が設定されていません")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return response.content[0].text
