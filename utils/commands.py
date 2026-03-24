"""
utils/commands.py - コマンド判定・正規化ユーティリティ
"""

from config import VALID_CHANNELS, ASANO_USER_ID
from services.slack import post_to_slack, get_bot_role_for_channel
from utils.slack_thread import get_thread_starter, has_bot_interaction


def handle_free_comment(channel_id: str, thread_ts: str, event: dict) -> bool:
    """スレッド内のフリーコメントを処理する。
    Botが応答済みのスレッドで、浅野↔スタッフ間のメンション通知を行う。
    処理した場合True、処理不要の場合Falseを返す。
    """
    if not event.get("thread_ts"):
        return False

    user_id = event.get("user", "")
    if not user_id:
        return False

    if not has_bot_interaction(channel_id, thread_ts):
        return False

    bot_role = get_bot_role_for_channel(channel_id)
    starter = get_thread_starter(channel_id, thread_ts)

    if user_id == ASANO_USER_ID:
        # 浅野のコメント → スタッフに通知
        if starter and starter != ASANO_USER_ID:
            post_to_slack(channel_id, thread_ts,
                f"<@{starter}> 浅野から連絡があります。上のコメントを確認してください。",
                bot_role=bot_role)
            return True
    else:
        # スタッフのコメント → 浅野に通知
        post_to_slack(channel_id, thread_ts,
            f"<@{ASANO_USER_ID}> スタッフから連絡があります。上のコメントを確認してください。",
            bot_role=bot_role)
        return True

    return False


def normalize_keyword(text: str) -> str:
    """全角→半角・漢数字→数字に正規化してコマンド判定しやすくする"""
    text = text.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    text = text.replace('／', '/').replace('　', ' ')
    return text.strip()


def normalize_channel(channel: str) -> str:
    """チャンネル名の表記ゆれを統一する"""
    aliases = {
        '自社使用': '社内利用',
        '自社利用': '社内利用',
    }
    return aliases.get(channel.strip(), channel.strip())


def parse_command(text: str):
    """テキストがコマンドかどうか判定し (command_type, option) を返す。
    コマンドでなければ (None, None)"""
    n = normalize_keyword(text)
    # 新フロー：AI自動判定の承認
    if n in ('確定', 'ok', 'OK', 'ＯＫ'):
        return 'ok_confirm', None
    elif n in ('相談', 'そうだん'):
        return 'soudan', None
    # 旧フロー互換：第一/第二での確定も引き続き対応
    elif n in ('第一', '第1'):
        return 'kakutei', '1'
    elif n in ('第二', '第2'):
        return 'kakutei', '2'
    elif n.startswith('確定/') and len(n) > 3:
        return 'kakutei', n[3:].strip()
    # チャンネル名をそのまま入力した場合も確定として認識
    elif normalize_channel(n) in VALID_CHANNELS:
        return 'kakutei', normalize_channel(n)
    elif n == '再判定':
        return 'saihantei', None
    elif n == '保留':
        return 'horyuu', None
    elif n in ('削除', 'テスト', 'キャンセル', '取消', '取り消し'):
        return 'cancel', None
    # 在庫検索（スレッド外・どのチャンネルでも）
    elif n.startswith('在庫検索 ') and len(n) > 5:
        return 'zaiko_search', n[5:].strip()
    elif n.startswith('検索 ') and len(n) > 3:
        return 'zaiko_search', n[3:].strip()
    return None, None
