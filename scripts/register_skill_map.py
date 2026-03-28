"""
scripts/register_skill_map.py - スキルマップ（評価制度）を個人ボードに一括登録
"""

import json
import time
import os
import httpx

MONDAY_API_URL = "https://api.monday.com/v2"

# 4段階評価のステータスラベル
SKILL_LEVEL_LABELS = {
    "labels": {
        "0": "未経験",
        "1": "練習中",
        "2": "一人でOK",
        "3": "指導可",
    },
    "label_colors": {
        "0": {"color": "#c4c4c4", "border": "#b0b0b0"},
        "1": {"color": "#fdab3d", "border": "#e99729"},
        "2": {"color": "#00c875", "border": "#00b461"},
        "3": {"color": "#0086c0", "border": "#0073a8"},
    },
}


def monday_graphql(token: str, query: str, variables: dict = None) -> dict:
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "API-Version": "2023-10",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = httpx.post(MONDAY_API_URL, headers=headers, json=payload, timeout=30)
    return response.json()


def create_column_safe(token, board_id, title, col_type, col_id, defaults=None):
    query = """
    mutation ($board_id: ID!, $title: String!, $col_type: ColumnType!, $col_id: String!, $defaults: JSON) {
        create_column(board_id: $board_id, title: $title, column_type: $col_type, id: $col_id, defaults: $defaults) { id }
    }
    """
    variables = {"board_id": board_id, "title": title, "col_type": col_type, "col_id": col_id}
    if defaults:
        variables["defaults"] = json.dumps(defaults)
    result = monday_graphql(token, query, variables)
    if "errors" not in result:
        print(f"    ✅ カラム: {title}")
    time.sleep(0.3)


def create_group(token, board_id, group_name):
    query = """
    mutation ($board_id: ID!, $group_name: String!) {
        create_group(board_id: $board_id, group_name: $group_name) { id }
    }
    """
    result = monday_graphql(token, query, {"board_id": board_id, "group_name": group_name})
    if "errors" in result:
        return ""
    gid = result["data"]["create_group"]["id"]
    print(f"    📁 {group_name}")
    return gid


def create_item(token, board_id, group_id, name, col_vals=None):
    query = """
    mutation ($board_id: ID!, $group_id: String!, $name: String!, $col_vals: JSON!) {
        create_item(board_id: $board_id, group_id: $group_id, item_name: $name, column_values: $col_vals) { id }
    }
    """
    cv = json.dumps(col_vals or {}, ensure_ascii=False)
    result = monday_graphql(token, query, {
        "board_id": board_id, "group_id": group_id, "name": name, "col_vals": cv
    })
    if "errors" in result:
        print(f"      ❌ {name}")
        return 0
    return int(result["data"]["create_item"]["id"])


# ============================================================
# スキルマップ定義
# ============================================================

TB_SKILLS = [
    ("【TB】現地調査", [
        "現場状況の把握",
        "写真記録",
        "お客様対応",
        "必要な道具の判断",
        "必要人数の判断",
        "日程調整",
        "業務範囲の確認",
        "工程管理・確認",
    ]),
    ("【TB】見積作成", [
        "作業内容の洗い出し",
        "原価計算（人件費・処分費・車両費）",
        "利益を考えた価格設定",
        "外部業者・CMとの協業打合せ",
        "外部業者・CMへの見積依頼",
        "見積書の作成",
        "お客様への見積説明",
    ]),
    ("【TB】不用品回収・片付け", [
        "搬出作業（安全・効率）",
        "仕分け判断（販売/スクラップ/廃棄）",
        "車両への積み込み",
        "車両運転（軽トラ/1トン車/2トン車）",
        "出荷業務（スクラップ・廃棄物）",
        "現場の養生・原状復旧",
        "お客様への完了報告",
    ]),
    ("【TB】分荷判定", [
        "商品の特定（メーカー・型番）",
        "状態報告の正確さ",
        "AI判定結果の理解",
        "テプラ作成・貼付",
    ]),
    ("【TB】スクラップ分別・出荷", [
        "素材の分別判断（鉄/非鉄/ステンレス等）",
        "適切な分解",
        "計量・記録",
        "出荷先の選定・手配",
        "等級・金額交渉",
        "取引先での精算業務",
        "積込み",
        "伝票処理",
    ]),
    ("【TB】廃棄物分別・出荷", [
        "廃棄物の分別判断",
        "適切な分解",
        "適正処理先の選定",
        "マニフェスト管理",
        "粗大ごみの数量確認",
        "粗大ごみの手配",
        "粗大ごみのシール貼り",
        "積込み",
        "出荷手配・伝票処理",
    ]),
    ("【TB】撮影・サイズ測定・保管", [
        "写真の品質（ピント・明るさ・角度）",
        "サイズ測定の正確さ",
        "棚入れ・ロケーション管理",
        "Slack Botの操作",
    ]),
    ("【TB】出品", [
        "出品ページ作成（タイトル・説明文）",
        "価格設定（相場理解）",
        "カテゴリ・配送設定",
        "出品画像の選定・編集",
    ]),
    ("【TB】梱包・出荷", [
        "梱包作業（破損防止）",
        "梱包材の選定",
        "送り状作成",
        "運送会社の手配",
        "Slack Botの操作",
    ]),
    ("【TB】請求", [
        "請求内容の確認・整理",
        "請求書の作成",
        "入金確認",
        "未入金の督促対応",
    ]),
    ("【TB】商品知識", [
        "ブランド・メーカーの理解",
        "相場観（売れる価格帯の感覚）",
        "商品状態の目利き",
        "販売チャンネルの特徴理解（9チャンネル）",
        "分荷判定の基準・ルール理解",
        "販売チャンネルの新規開拓",
        "スクラップ買取先の新規開拓",
        "廃棄物処理先の新規開拓",
    ]),
    ("【TB】作業スピード", [
        "外作業（回収・片付け・搬出）",
        "分荷判定",
        "分解作業",
        "スクラップ分別・積込み",
        "廃棄物分別・積込み",
        "撮影・サイズ測定・保管",
        "出品ページ作成",
        "梱包・出荷",
    ]),
    ("【TB】道具・作業場所の管理", [
        "道具の管理・手入れ",
        "道具の在庫確認・発注",
        "作業場所の整理整頓",
        "作業場所の清掃",
    ]),
]

CM_SKILLS = [
    ("【CM】現地調査", [
        "現場状況の把握",
        "写真記録",
        "お客様対応",
        "必要な工事内容の判断",
        "工事方法の判断",
        "必要な道具の判断",
        "協力業者の選定",
        "協力業者との打合せ・見積依頼",
        "工程の認識・管理",
        "日程調整",
    ]),
    ("【CM】見積作成", [
        "工事内容の洗い出し",
        "原価計算（材料費・人件費・外注費）",
        "利益を考えた価格設定",
        "見積書の作成",
        "お客様への見積説明",
    ]),
    ("【CM】協力業者打合せ", [
        "適切な業者の選定",
        "新規業者の開拓",
        "工事内容の説明・伝達",
        "見積内容の精査",
        "価格交渉",
        "スケジュール調整",
    ]),
    ("【CM】各種申請・手続き", [
        "建設リサイクル届出",
        "アスベスト調査・事前報告",
        "道路使用許可申請",
        "道路占有許可申請",
        "その他行政手続き",
    ]),
    ("【CM】工事施工・管理", [
        "施主への工事説明",
        "工事前近隣挨拶",
        "施工品質の管理",
        "安全管理",
        "工程管理・進捗確認",
        "協力業者への指示・連携",
        "現場でのトラブル対応",
        "近隣対応",
    ]),
    ("【CM】完了報告", [
        "完了写真の撮影・記録",
        "施主への完了報告・確認",
        "手直し対応",
    ]),
    ("【CM】請求", [
        "請求内容の確認・整理",
        "請求書の作成",
        "入金確認",
        "未入金の督促対応",
    ]),
    ("【CM】道具・作業場所の管理", [
        "工具・機材の管理・手入れ",
        "工具・機材の在庫確認・発注",
        "作業場所の整理整頓",
        "作業場所の清掃",
    ]),
]

AS_SKILLS = [
    ("【アスカラ】リード獲得", [
        "問い合わせ対応",
        "紹介ルートの開拓・維持",
        "HP・SNS経由の反響対応",
    ]),
    ("【アスカラ】相談受付", [
        "お客様のヒアリング",
        "困りごとの整理・言語化",
        "対応可否の判断",
        "安心感・居心地のよい対応",
    ]),
    ("【アスカラ】各事業の説明", [
        "テイクバック事業の説明",
        "クリアメンテ事業の説明",
        "AIX事業の説明",
        "お客様に合わせた提案力",
    ]),
    ("【アスカラ】案件振り分け", [
        "適切な事業部への振り分け判断",
        "外部業者への紹介判断",
        "担当者へのスムーズな引き継ぎ",
    ]),
    ("【アスカラ】フォローアップ", [
        "案件の進捗確認",
        "お客様への経過報告",
        "完了後のアフターフォロー",
        "リピート獲得実績",
        "紹介獲得実績",
    ]),
    ("【アスカラ】HP管理", [
        "HP作成・更新",
        "コンテンツの更新・追加",
        "アクセス状況の把握",
        "問い合わせ導線の改善",
    ]),
    ("【アスカラ】SNS管理・発信", [
        "SNS作成・更新",
        "投稿の企画・作成",
        "反応・フォロワーの分析",
        "ブランドイメージの統一",
    ]),
    ("【アスカラ】道具・作業場所の管理", [
        "事務用品の管理・発注",
        "オフィスの整理整頓",
        "オフィスの清掃",
    ]),
]

COMMON_SKILLS = [
    ("【全社】経営判断", [
        "事業方針の策定",
        "投資判断",
        "人事判断",
        "リスク管理",
    ]),
    ("【全社】総務", [
        "備品管理・発注",
        "社内環境整備",
        "書類管理・ファイリング",
        "郵便・宅配の対応",
    ]),
    ("【全社】経理", [
        "入出金管理",
        "帳簿記帳",
        "請求書・領収書の管理",
        "給与計算",
        "税務対応",
    ]),
    ("【全社】広報", [
        "プレスリリース・お知らせ作成",
        "メディア対応",
        "会社案内・パンフレット管理",
    ]),
    ("【全社】ブランディング", [
        "ブランドコンセプトの理解",
        "デザイン・トーンの統一",
        "対外的な印象管理",
        "名刺の作成・管理・発注",
    ]),
    ("【全社】電話・来客対応", [
        "電話の受け答え",
        "来客対応・案内",
        "適切な担当者への取次ぎ",
    ]),
    ("【全社】ツール操作", [
        "Monday.comの操作",
        "Slackの操作",
        "Google Chatの操作",
        "TimeTreeの操作",
        "Google Workspace（Drive/カレンダー等）",
    ]),
]

# スタッフごとに登録するスキルカテゴリ
STAFF_SKILL_SETS = {
    "浅野儀頼": [TB_SKILLS, CM_SKILLS, AS_SKILLS, COMMON_SKILLS],
    "三島圭織": [AS_SKILLS, COMMON_SKILLS],
    "林和人": [TB_SKILLS, COMMON_SKILLS],
    "横山優": [TB_SKILLS, COMMON_SKILLS],
    "平野光雄": [TB_SKILLS, COMMON_SKILLS],
    "桃井侑菜": [TB_SKILLS, COMMON_SKILLS],
    "伊藤佐和子": [TB_SKILLS, COMMON_SKILLS],
    "奥村亜優李": [TB_SKILLS, COMMON_SKILLS],
    "松本豊彦": [TB_SKILLS, COMMON_SKILLS],
    "北瀬孝": [TB_SKILLS, COMMON_SKILLS],
    "白木雄介": [TB_SKILLS, COMMON_SKILLS],
}


def get_private_boards(token):
    """個人プライベートボードのID一覧を取得する"""
    # setup_task_boardsで作成されたボードを名前で検索
    board_map = {}
    staff_names = list(STAFF_SKILL_SETS.keys())

    for name in staff_names:
        board_name = f"{name}専用"
        query = """
        query ($name: String!) {
            boards(limit: 5, board_kind: private) {
                id
                name
            }
        }
        """
        # 全プライベートボードを取得して名前で検索
        all_query = """
        query {
            boards(limit: 100, board_kind: private) {
                id
                name
            }
        }
        """
        result = monday_graphql(token, all_query)
        boards = result.get("data", {}).get("boards", [])
        for b in boards:
            if b["name"] == board_name:
                board_map[name] = int(b["id"])
                break

        if name not in board_map:
            # よしさんの全体タスク管理ボードも検索
            if name == "浅野儀頼":
                for b in boards:
                    if "全体タスク管理" in b["name"]:
                        board_map[name] = int(b["id"])
                        break

    return board_map


def register_skill_map(token: str):
    print("=" * 50)
    print("スキルマップ（評価制度）一括登録")
    print("=" * 50)

    # 個人ボードを検索
    print("\n📋 個人ボード検索中...")
    board_map = get_private_boards(token)
    print(f"  見つかったボード: {len(board_map)}件")
    for name, bid in board_map.items():
        print(f"    {name}: {bid}")

    for name, skill_sets in STAFF_SKILL_SETS.items():
        board_id = board_map.get(name)
        if not board_id:
            print(f"\n⏭️ {name}: ボードが見つかりません。スキップ")
            continue

        print(f"\n📋 {name}のスキルマップ登録")

        # 評価レベルカラムを追加
        create_column_safe(token, board_id, "評価レベル", "status", "skill_level", SKILL_LEVEL_LABELS)

        for skill_category in skill_sets:
            for group_name, items in skill_category:
                gid = create_group(token, board_id, group_name)
                if not gid:
                    continue
                time.sleep(0.3)
                for item_name in items:
                    create_item(token, board_id, gid, item_name, {"skill_level": {"index": 0}})
                    time.sleep(0.3)

    print("\n" + "=" * 50)
    print("スキルマップ登録完了！")
    print("=" * 50)


if __name__ == "__main__":
    token = os.environ.get("MONDAY_TOKEN") or os.environ.get("MONDAY_API_TOKEN", "")
    if not token:
        print("❌ MONDAY_TOKEN が設定されていません")
        exit(1)
    register_skill_map(token)
