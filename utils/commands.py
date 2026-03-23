"""
utils/commands.py - コマンド判定・正規化ユーティリティ
"""

from config import VALID_CHANNELS


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
