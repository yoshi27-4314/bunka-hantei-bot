"""
utils/unified_commands.py - 全チャンネル共通の統一コマンド（修正・キャンセル・削除）

「修正」と入力 → Botが「何を修正しますか？」と選択肢を表示
→ 番号で選択 → 新しい値を入力 → 更新完了

チャンネルごとの選択肢はハンドラ側で定義する。
"""

from services.slack import post_to_slack


# 統一コマンドのセッション管理
# キー: f"{channel_id}:{thread_ts}"
# 値: {"command": "修正", "options": [...], "selected": None, "bot_role": "satsuei"}
_command_sessions = {}


# 統一コマンドとして認識するワード
MODIFY_WORDS = ("修正", "変更")
CANCEL_WORDS_UNIFIED = ("キャンセル", "中止", "中断")
DELETE_WORDS = ("削除",)


def _session_key(channel_id: str, thread_ts: str) -> str:
    return f"{channel_id}:{thread_ts}"


def is_unified_command(text: str) -> str:
    """統一コマンドかどうか判定する。該当すればコマンド種別を返す。"""
    if text in MODIFY_WORDS:
        return "修正"
    if text in CANCEL_WORDS_UNIFIED:
        return "キャンセル"
    if text in DELETE_WORDS:
        return "削除"
    return ""


def show_options(channel_id: str, thread_ts: str, command: str,
                 options: list, bot_role: str, user_id: str = "") -> None:
    """統一コマンドの選択肢を表示してセッションに記録する。

    options: [{"label": "サイズ", "key": "size"}, ...]
    """
    key = _session_key(channel_id, thread_ts)
    _command_sessions[key] = {
        "command": command,
        "options": options,
        "selected": None,
        "bot_role": bot_role,
    }

    if command == "修正":
        header = "📝 *何を修正しますか？*"
    elif command == "キャンセル":
        header = "⏹️ *何をキャンセルしますか？*"
    elif command == "削除":
        header = "🗑️ *何を削除しますか？*"
    else:
        header = f"*{command}*"

    lines = [
        "━━━━━━━━━━━━━━━━",
        header,
        "━━━━━━━━━━━━━━━━\n",
    ]
    for i, opt in enumerate(options, 1):
        lines.append(f"　{i}. {opt['label']}")
    lines.append("\n番号で選んでください。")
    lines.append("`やめる` で戻れます。")

    post_to_slack(channel_id, thread_ts, "\n".join(lines),
                  mention_user=user_id, bot_role=bot_role)


def get_pending_selection(channel_id: str, thread_ts: str, text: str):
    """番号入力を待っているセッションがあれば、選択結果を返す。

    Returns:
        (command, selected_option) — 選択が確定した場合
        ("cancel", None) — 「やめる」が入力された場合
        (None, None) — 該当なし
    """
    key = _session_key(channel_id, thread_ts)
    session = _command_sessions.get(key)
    if not session:
        return None, None

    # 「やめる」で選択をキャンセル
    if text in ("やめる", "戻る"):
        del _command_sessions[key]
        return "cancel_menu", None

    # 番号入力
    try:
        idx = int(text)
    except (ValueError, TypeError):
        return None, None

    options = session["options"]
    if 1 <= idx <= len(options):
        selected = options[idx - 1]
        del _command_sessions[key]
        return session["command"], selected

    return None, None


def has_pending_session(channel_id: str, thread_ts: str) -> bool:
    """選択待ちセッションがあるか確認する。"""
    return _session_key(channel_id, thread_ts) in _command_sessions


def clear_session(channel_id: str, thread_ts: str) -> None:
    """セッションをクリアする。"""
    _command_sessions.pop(_session_key(channel_id, thread_ts), None)
