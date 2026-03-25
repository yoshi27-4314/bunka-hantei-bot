"""
handlers/help.py - ヘルプ機能（全チャンネル共通）

「ヘルプ」→ メニュー表示
「ヘルプ ●●」→ キーワード検索（最大5件）
"""

import os
import re

from services.slack import post_to_slack

# ── ヘルプテキスト読み込み ──────────────────────────

# チャンネルID → ヘルプファイル名のマッピング
_CHANNEL_HELP_MAP = {
    "SATSUEI_CHANNEL_ID":  "商品撮影",
    "SHUPPINON_CHANNEL_ID": "出品保管",
    "KONPO_CHANNEL_ID":    "梱包出荷",
    "GENBA_CHANNEL_ID":    "現場査定",
    "STATUS_CHANNEL_ID":   "ステータス確認",
    "ATTENDANCE_CHANNEL_ID": "出退勤",
    "KINTAI_CHANNEL_ID":   "勤怠連絡",
}
# デフォルト（分荷判定チャンネル）
_DEFAULT_HELP = "分荷判定"

# ヘルプテキストのキャッシュ {ファイル名: {"sections": [...], "raw": str}}
_help_cache: dict = {}

# ヘルプファイルのディレクトリ
_HELP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "help")


def _load_help(name: str) -> dict:
    """ヘルプテキストを読み込み、セクションに分割してキャッシュする"""
    if name in _help_cache:
        return _help_cache[name]

    filepath = os.path.join(_HELP_DIR, f"{name}.txt")
    if not os.path.exists(filepath):
        _help_cache[name] = {"sections": [], "raw": ""}
        return _help_cache[name]

    with open(filepath, "r", encoding="utf-8") as f:
        raw = f.read()

    # 「# 数字　タイトル」のパターンでセクションを分割
    sections = []
    current_title = ""
    current_body = []

    for line in raw.split("\n"):
        # 大見出し: "# 1　タイトル" or "# 数字 タイトル"
        m = re.match(r'^#\s+(\d+)\s*[　\s]+(.+)', line)
        if m:
            if current_title:
                sections.append({
                    "num": len(sections) + 1,
                    "title": current_title,
                    "body": "\n".join(current_body).strip(),
                })
            current_title = m.group(2).strip()
            current_body = []
        else:
            current_body.append(line)

    # 最後のセクション
    if current_title:
        sections.append({
            "num": len(sections) + 1,
            "title": current_title,
            "body": "\n".join(current_body).strip(),
        })

    result = {"sections": sections, "raw": raw}
    _help_cache[name] = result
    return result


def _get_help_name(channel_id: str) -> str:
    """チャンネルIDからヘルプファイル名を返す"""
    for env_key, help_name in _CHANNEL_HELP_MAP.items():
        env_val = os.environ.get(env_key, "")
        if env_val and env_val == channel_id:
            return help_name
    return _DEFAULT_HELP


def _build_menu(help_name: str) -> str:
    """メニュー表示（大見出し一覧）を構築する"""
    data = _load_help(help_name)
    sections = data["sections"]

    if not sections:
        return (
            "━━━━━━━━━━━━━━━━\n"
            "📖 *ヘルプ*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "このチャンネルのヘルプはまだ準備中です。\n\n"
            "━━━━━━━━━━━━━━━━"
        )

    lines = [
        "━━━━━━━━━━━━━━━━",
        f"📖 *{help_name} ヘルプ*",
        "━━━━━━━━━━━━━━━━",
        "",
    ]
    for s in sections:
        lines.append(f"*{s['num']}*　{s['title']}")
    lines.append("")
    lines.append("─────────────────────────")
    lines.append("")
    lines.append("📝 *使い方：*")
    lines.append("　`ヘルプ 3` → 3番の内容を表示")
    lines.append("　`ヘルプ 確定` → キーワードで検索")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


def _build_section(help_name: str, num: int) -> str:
    """指定番号のセクション内容を返す"""
    data = _load_help(help_name)
    sections = data["sections"]

    if num < 1 or num > len(sections):
        return (
            f"⚠️ {num} 番のセクションはありません。\n\n"
            f"`ヘルプ` で目次を確認してください。"
        )

    s = sections[num - 1]
    # Slackの文字数制限を考慮して3000文字に制限
    body = s["body"]
    if len(body) > 3000:
        body = body[:3000] + "\n\n…（続きがあります）"

    return (
        "━━━━━━━━━━━━━━━━\n"
        f"📖 *{s['num']}. {s['title']}*\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"{body}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📝 `ヘルプ` で目次に戻る\n"
        "━━━━━━━━━━━━━━━━"
    )


def _search_help(help_name: str, keyword: str) -> str:
    """キーワードでヘルプを検索（最大5件）"""
    data = _load_help(help_name)
    sections = data["sections"]
    keyword_lower = keyword.lower()

    hits = []
    for s in sections:
        # タイトルと本文の両方を検索
        text = f"{s['title']} {s['body']}".lower()
        if keyword_lower in text:
            # マッチした行の前後を抜粋
            snippet = ""
            for line in s["body"].split("\n"):
                if keyword_lower in line.lower() and line.strip():
                    snippet = line.strip()[:100]
                    break
            hits.append({"num": s["num"], "title": s["title"], "snippet": snippet})

    if not hits:
        return (
            "━━━━━━━━━━━━━━━━\n"
            f"🔍 *「{keyword}」の検索結果*\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "該当する内容が見つかりませんでした。\n\n"
            "📝 `ヘルプ` で目次を確認してください。\n\n"
            "━━━━━━━━━━━━━━━━"
        )

    lines = [
        "━━━━━━━━━━━━━━━━",
        f"🔍 *「{keyword}」の検索結果　{len(hits[:5])}件*",
        "━━━━━━━━━━━━━━━━",
        "",
    ]
    for h in hits[:5]:
        lines.append(f"*{h['num']}*　{h['title']}")
        if h["snippet"]:
            lines.append(f"　　{h['snippet']}")
        lines.append("")
    lines.append("─────────────────────────")
    lines.append("")
    lines.append("📝 `ヘルプ 番号` で詳細を表示")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


def handle_help(text: str, channel_id: str, thread_ts: str,
                user_id: str, bot_role: str) -> bool:
    """ヘルプコマンドを処理する。処理した場合はTrueを返す。"""
    if not text:
        return False

    # 「ヘルプ」で始まるかチェック
    stripped = text.strip()
    if not re.match(r'^ヘルプ', stripped):
        return False

    help_name = _get_help_name(channel_id)

    # 「ヘルプ」のみ → メニュー表示
    arg = re.sub(r'^ヘルプ\s*', '', stripped).strip()
    if not arg:
        reply = _build_menu(help_name)
    elif re.match(r'^\d+$', arg):
        # 「ヘルプ 3」→ セクション番号指定
        reply = _build_section(help_name, int(arg))
    else:
        # 「ヘルプ 確定」→ キーワード検索
        reply = _search_help(help_name, arg)

    post_to_slack(channel_id, thread_ts, reply,
                  mention_user=user_id, bot_role=bot_role)
    return True
