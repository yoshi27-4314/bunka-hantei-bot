"""
utils/commands.py - コマンド判定・正規化ユーティリティ
"""

import re

from config import VALID_CHANNELS, BOT_NAMES, get_anthropic_client
from services.slack import post_to_slack, get_bot_role_for_channel
from utils.slack_thread import has_bot_interaction


def handle_free_comment(channel_id: str, thread_ts: str, event: dict) -> bool:
    """スレッド内のフリーコメントを処理する。
    @メンションあり → 人同士の会話。Botは何もしない。
    @メンションなし → Botへの質問。AIで返事する。
    処理した場合True、処理不要の場合Falseを返す。
    """
    if not event.get("thread_ts"):
        return False

    user_id = event.get("user", "")
    if not user_id:
        return False

    if not has_bot_interaction(channel_id, thread_ts):
        return False

    text = event.get("text", "")

    # @メンションが含まれている → 人同士の会話。Botは何もしない
    if re.search(r'<@U[A-Z0-9]+>', text):
        return False

    # @メンションなし → Botへの質問としてAIで返事する
    bot_role = get_bot_role_for_channel(channel_id)
    bot_name = BOT_NAMES.get(bot_role, "北大路魯山人")
    try:
        client = get_anthropic_client()
        if not client:
            return False
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=(
                f"あなたは「{bot_name}」です。TakeBack事業部の業務サポートBotとして、"
                "スタッフからの質問に短く丁寧に答えてください。"
                "分からないことは「浅野さんに@メンションで聞いてください」と案内してください。"
            ),
            messages=[{"role": "user", "content": text}],
        )
        reply = response.content[0].text
        post_to_slack(channel_id, thread_ts, reply,
                      mention_user=user_id, bot_role=bot_role)
    except Exception as e:
        print(f"[フリーコメントAI応答エラー] {e}")
    return True


def normalize_keyword(text: str) -> str:
    """全角→半角・漢数字→数字・ヴ行→バ行に正規化してコマンド判定しやすくする"""
    text = text.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    text = text.replace('／', '/').replace('　', ' ')
    # ヴ行 → バ行（スマホで入力しやすい形に統一）
    for old, new in [('ヴァ', 'バ'), ('ヴィ', 'ビ'), ('ヴゥ', 'ブ'), ('ヴェ', 'ベ'), ('ヴォ', 'ボ'), ('ヴ', 'ブ')]:
        text = text.replace(old, new)
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
