/**
 * 分荷判定Bot システム仕様書・作業ログ セットアップスクリプト
 *
 * 【使い方】
 * 1. システム仕様書スプレッドシートを開く
 * 2. 拡張機能 → Apps Script を開く
 * 3. このスクリプト全体を貼り付けて保存
 * 4. 「setupAll」関数を選択して「実行」
 * 5. 権限の承認ダイアログが出たら許可する
 */

// ── スプレッドシートID ──────────────────────────────────
const SPEC_SHEET_ID = "1Sty7dE9tOsYOLoCJmXtoFnj4S0cQmbgG_WogxxSthHg";
const LOG_SHEET_ID  = "1-jspSk-pi9Epm0Z5GoyppVfCw8mXSLhJ3B-yBPoRy8U";

/**
 * 【実行手順】タイムアウト対策のため4回に分けて実行してください
 * 1回目: setup_step1 → 基本情報・環境変数・スタッフマップ・販売チャンネル
 * 2回目: setup_step2 → 管理番号・コマンド仕様・スコアリング・分荷判定フロー
 * 3回目: setup_step3 → DBカラム定義・Mondayカラム・Botキャラクター・実装済み/未実装
 * 4回目: setup_step4 → 作業ログシート
 */

function setup_step1() {
  const ss = SpreadsheetApp.openById(SPEC_SHEET_ID);
  _deleteAndInit(ss);
  createBasicInfoSheet(ss);
  createEnvVarsSheet(ss);
  createStaffMapSheet(ss);
  createChannelsSheet(ss);
  SpreadsheetApp.flush();
  SpreadsheetApp.getUi().alert("✅ STEP1完了（4シート作成）\n次は setup_step2 を実行してください。");
}

function setup_step2() {
  const ss = SpreadsheetApp.openById(SPEC_SHEET_ID);
  createKanriNumberSheet(ss);
  createCommandSheet(ss);
  createScoringSheet(ss);
  createFlowSheet(ss);
  SpreadsheetApp.flush();
  SpreadsheetApp.getUi().alert("✅ STEP2完了（4シート作成）\n次は setup_step3 を実行してください。");
}

function setup_step3() {
  const ss = SpreadsheetApp.openById(SPEC_SHEET_ID);
  createDbColumnsSheet(ss);
  createMondayColumnsSheet(ss);
  createBotCharSheet(ss);
  createImplSheet(ss);
  // 一時シートを削除
  try { ss.deleteSheet(ss.getSheetByName("_temp")); } catch(e) {}
  ss.setActiveSheet(ss.getSheetByName("基本情報"));
  SpreadsheetApp.flush();
  SpreadsheetApp.getUi().alert("✅ STEP3完了（4シート作成）\n次は setup_step4 を実行してください。");
}

function setup_step4() {
  setupLogSheet();
  SpreadsheetApp.getUi().alert("✅ STEP4完了！\n\n全セットアップが完了しました。");
}

function _deleteAndInit(ss) {
  // 一時シートを作成してから既存を削除
  const existing = ss.getSheets();
  const tempSheet = ss.insertSheet("_temp");
  existing.forEach(s => {
    try { ss.deleteSheet(s); } catch(e) {}
  });
}

// ── ① 基本情報 ──────────────────────────────────────────
function createBasicInfoSheet(ss) {
  const sh = ss.insertSheet("基本情報");
  const data = [
    ["項目", "値", "備考"],
    ["システム名", "分荷判定Bot", "AI分荷判定システム"],
    ["バージョン", "2026年3月版", ""],
    ["会社名", "アスカラ / 株式会社テイクバック", "TakeBack事業部"],
    ["", "", ""],
    ["【URL・エンドポイント】", "", ""],
    ["本番サーバーURL", "https://web-production-e7e9d.up.railway.app", "Railway"],
    ["GitHub", "https://github.com/yoshi27-4314/bunka-hantei-bot", ""],
    ["Slack Events エンドポイント", "/slack/events", "POST"],
    ["デバッグエンドポイント", "/debug", "GET - 環境変数確認"],
    ["Mondayセットアップ", "/monday-setup", "GET - 初回のみ"],
    ["", "", ""],
    ["【連携サービス】", "", ""],
    ["Monday.com ボードID", "18403611418", "在庫管理ボード"],
    ["GAS WebアプリURL", "https://script.google.com/macros/s/AKfycbwYn4XOS7vbUSgUW23OpXGSCGDxje9GwsKtWvgOFLMRsSKCCn6Zq3dGm9IC8u_N2DmU/exec", "スプレッドシートDB"],
    ["スプレッドシートID（DB）", "1CWG9MVrsw9gJwp31lCrUs9KB0a1zptZY1cPO47ZNmVU", "全出品データ"],
    ["DriveルートフォルダID", "GOOGLE_DRIVE_FOLDER_ID（環境変数）", "商品写真保存先"],
    ["", "", ""],
    ["【AIモデル】", "", ""],
    ["分荷判定メイン", "claude-sonnet-4-20250514", "max_tokens: 2048"],
    ["現場査定", "claude-sonnet-4-20250514", "max_tokens: 1024"],
    ["身分証情報抽出", "claude-sonnet-4-20250514", "max_tokens: 512"],
    ["管理番号OCR（テプラ）", "claude-haiku-4-5-20251001", "max_tokens: 50"],
    ["追跡番号OCR（送り状）", "claude-haiku-4-5-20251001", "max_tokens: 100"],
    ["出品コンテンツ生成", "claude-haiku-4-5-20251001", "max_tokens: 600"],
    ["GAS（yahooauction_sheet.gs）", "claude-opus-4-6", "max_tokens: 2000"],
  ];
  sh.getRange(1, 1, data.length, 3).setValues(data);
  formatHeader(sh, 1, 3);
  formatSection(sh, [6, 13, 18]);
  sh.setColumnWidth(1, 220);
  sh.setColumnWidth(2, 420);
  sh.setColumnWidth(3, 180);
}

// ── ② 環境変数 ──────────────────────────────────────────
function createEnvVarsSheet(ss) {
  const sh = ss.insertSheet("環境変数");
  const data = [
    ["変数名", "用途", "必須", "未設定時の動作"],
    ["ANTHROPIC_API_KEY", "Claude API認証", "必須", "全AI機能が停止"],
    ["SLACK_BOT_TOKEN", "Slack Bot認証（xoxb-）", "必須", "Slack送受信不可"],
    ["MONDAY_TOKEN または MONDAY_API_TOKEN", "Monday.com GraphQL API", "必須", "在庫管理機能が停止"],
    ["SATSUEI_CHANNEL_ID", "撮影確認チャンネルID（白洲次郎）", "推奨", "撮影チャンネル機能なし"],
    ["SHUPPINON_CHANNEL_ID", "出品保管チャンネルID（岩崎弥太郎）", "推奨", "出品チャンネル機能なし"],
    ["KONPO_CHANNEL_ID", "梱包出荷チャンネルID（黒田官兵衛）", "推奨", "梱包チャンネル機能なし"],
    ["STATUS_CHANNEL_ID", "ステータス確認チャンネルID（ステータス松本）", "推奨", "ステータスチャンネル機能なし"],
    ["ATTENDANCE_CHANNEL_ID", "出退勤チャンネルID（二宮金次郎）", "推奨", "出退勤機能なし"],
    ["GENBA_CHANNEL_ID", "現場査定チャンネルID（渋沢栄一）", "推奨", "現場査定機能なし"],
    ["KINTAI_CHANNEL_ID", "勤怠連絡チャンネルID（サイレント記録）", "任意", "勤怠サイレント記録なし"],
    ["GOOGLE_SERVICE_ACCOUNT_JSON", "Google Drive API認証（base64エンコード）", "推奨", "Drive写真保存スキップ"],
    ["GOOGLE_DRIVE_FOLDER_ID", "DriveのルートフォルダID", "推奨", "Drive写真保存スキップ"],
  ];
  sh.getRange(1, 1, data.length, 4).setValues(data);
  formatHeader(sh, 1, 4);
  sh.setColumnWidth(1, 280);
  sh.setColumnWidth(2, 300);
  sh.setColumnWidth(3, 60);
  sh.setColumnWidth(4, 200);
  // 必須行に色付け
  for (let i = 2; i <= 4; i++) {
    sh.getRange(i, 3).setBackground("#fde8e8");
  }
}

// ── ③ スタッフマップ ───────────────────────────────────
function createStaffMapSheet(ss) {
  const sh = ss.insertSheet("スタッフマップ");
  const data = [
    ["Slack UserID", "スタッフコード（app.py STAFF_MAP）", "氏名", "備考"],
    ["U0AL10Q1HQC", "浅野儀頼", "浅野儀頼", "管理者・高額案件通知先・Botが反応しない特別設定"],
    ["U0ALQ4BJNSV", "林和人", "林和人", "確定済み"],
    ["U0AL4R1EMMZ", "平野光雄", "平野光雄", "確定済み"],
    ["U0ALKDQEC2F", "桃井侑菜", "桃井侑菜", "確定済み"],
    ["U0ALV7C2EHJ", "伊藤佐和子", "伊藤佐和子", "確定済み"],
    ["U0AM4HG1PRP", "奥村亜優李", "奥村亜優李", "確定済み"],
    ["未取得", "横山優", "横山優", "SlackアカウントのメンバーIDを取得してSTAFF_MAPに追加要"],
    ["未取得", "三島圭織", "三島圭織", "同上"],
    ["未取得", "松本豊彦", "松本豊彦", "同上"],
    ["未取得（代筆対応）", "北瀬孝", "北瀬孝", "Slack不使用。他スタッフが「北瀬孝 9:00~17:00」形式で代筆申告"],
    ["未取得", "白木雄介", "白木雄介", "同上"],
  ];
  sh.getRange(1, 1, data.length, 4).setValues(data);
  formatHeader(sh, 1, 4);
  for (let i = 8; i <= 12; i++) {
    sh.getRange(i, 1).setBackground("#fff3cd");
  }
  sh.setColumnWidth(1, 180);
  sh.setColumnWidth(2, 100);
  sh.setColumnWidth(3, 120);
  sh.setColumnWidth(4, 320);
}

// ── ④ 販売チャンネル ───────────────────────────────────
function createChannelsSheet(ss) {
  const sh = ss.insertSheet("販売チャンネル");
  const data = [
    ["チャンネル名", "区分", "管理番号", "目標粗利率", "特徴・向いている商品", "注意点"],
    ["eBayシングル", "通販", "発行（Eコード）", "8%以上", "海外需要のある希少品・ヴィンテージ。国内で安い商品が高額になることも", "手数料約13%。英語対応必要"],
    ["eBayまとめ", "通販", "発行（Eコード）", "20%前後", "単品では安い部品・同種類の複数品。まとめで付加価値", "組み合わせ判断力が必要"],
    ["ヤフオクヴィンテージ", "通販", "発行（Vコード）※旧形式", "8%以上", "昭和レトロ・アンティーク・コレクターズアイテム", "在庫期間が長くなりやすい"],
    ["ヤフオク現行", "通販", "発行（Gコード）※旧形式", "25%前後", "現行モデルの家電・家具・日用品。需要安定・回転早い", "競合多い。適正価格把握が重要"],
    ["ヤフオクまとめ", "通販", "発行（Mコード）※旧形式", "タダ引き推奨", "単品では安いが、まとめで処分できる商品", "個別利益より在庫回転を優先"],
    ["ロット販売", "非通販", "なし", "タダ引き推奨", "業者向け一括売却", "大量処理可能。単品価値低"],
    ["社内利用", "非通販", "なし", "—", "事業内で使用する備品・消耗品", "表記ゆれ：自社使用・自社利用も可"],
    ["スクラップ", "非通販", "なし", "—", "金属・材料として素材価値のみある商品", "スクラップ業者への売却価格を把握"],
    ["廃棄", "非通販", "なし", "マイナス", "販売・利用が難しく処分が必要な商品", "廃棄費用が発生する"],
  ];
  sh.getRange(1, 1, data.length, 6).setValues(data);
  formatHeader(sh, 1, 6);
  // 通販行を青く
  for (let i = 2; i <= 6; i++) {
    sh.getRange(i, 2).setBackground("#dbeafe");
  }
  // 非通販行をグレー
  for (let i = 7; i <= 10; i++) {
    sh.getRange(i, 2).setBackground("#f3f4f6");
  }
  sh.setColumnWidth(1, 160);
  sh.setColumnWidth(2, 70);
  sh.setColumnWidth(3, 160);
  sh.setColumnWidth(4, 100);
  sh.setColumnWidth(5, 280);
  sh.setColumnWidth(6, 200);

  // 管理番号フォーマット説明を追加
  sh.getRange(12, 1).setValue("【管理番号フォーマット（現行）】").setFontWeight("bold");
  sh.getRange(13, 1, 4, 2).setValues([
    ["形式", "YYMM-連番4桁（例：2603-0001）"],
    ["YYMM", "西暦下2桁 + 月2桁（2026年3月 → 2603）"],
    ["連番", "その月の通し番号（Monday.comのアイテム数から自動採番）"],
    ["旧形式", "2603V0001（区分コードV/G/M/E付き）→ 廃止。新規発行しない。既存番号は参照可能"],
  ]);
}

// ── ⑤ 管理番号 ─────────────────────────────────────────
function createKanriNumberSheet(ss) {
  const sh = ss.insertSheet("管理番号");
  const data = [
    ["項目", "内容"],
    ["現行フォーマット", "YYMM-[連番4桁]　例：2603-0001"],
    ["YYMM", "西暦下2桁 + 月2桁（2026年3月 → 2603）"],
    ["連番", "その月の全チャンネル通し番号。Monday.comのアイテム数から取得"],
    ["採番方法", "get_monthly_sequence() → Monday.comのkanri_bangoカラムをスキャンしてyymm始まりの件数+1"],
    ["重複防止", "_issued_numbers セットで同プロセス内の発行済み番号を管理。衝突時はseq+1して再試行"],
    ["スレッドセーフ", "_management_number_lock（threading.Lock）で同時発行をブロック"],
    ["", ""],
    ["旧フォーマット（廃止）", "YYMM[区分1文字][連番4桁]　例：2603V0001"],
    ["旧フォーマットの区分コード", "V=ヤフオクヴィンテージ / G=ヤフオク現行 / M=ヤフオクまとめ / E=eBay系"],
    ["旧フォーマットの扱い", "新規発行しない。既存の旧番号はシステムで参照・表示可能"],
    ["", ""],
    ["発行対象チャンネル", "eBayシングル・eBayまとめ・ヤフオクヴィンテージ・ヤフオク現行・ヤフオクまとめ"],
    ["発行なし", "ロット販売・社内利用・スクラップ・廃棄（スプレッドシートのみ記録）"],
  ];
  sh.getRange(1, 1, data.length, 2).setValues(data);
  formatHeader(sh, 1, 2);
  sh.getRange(9, 1).setBackground("#fde8e8").setFontColor("#888888");
  sh.setColumnWidth(1, 220);
  sh.setColumnWidth(2, 450);
}

// ── ⑥ コマンド仕様 ────────────────────────────────────
function createCommandSheet(ss) {
  const sh = ss.insertSheet("コマンド仕様");
  const data = [
    ["コマンド（入力）", "内部タイプ", "オプション", "動作", "使用条件"],
    ["第一 / 第1", "kakutei", "'1'", "AI第一候補チャンネルで確定", "スレッド内のみ"],
    ["第二 / 第2", "kakutei", "'2'", "AI第二候補チャンネルで確定", "スレッド内のみ"],
    ["確定/チャンネル名", "kakutei", "チャンネル名", "指定チャンネルで確定", "スレッド内のみ"],
    ["チャンネル名をそのまま入力", "kakutei", "チャンネル名", "そのチャンネルで確定", "スレッド内のみ"],
    ["再判定", "saihantei", "なし", "スレッド履歴込みでAIに再判定", "スレッド内のみ"],
    ["保留", "horyuu", "なし", "保留メッセージを返す（記録なし）", "スレッド内のみ"],
    ["キャンセル / 取消 / 取り消し / 削除 / テスト", "cancel", "なし", "確定取消・Mondayをキャンセルに更新", "スレッド内のみ"],
    ["在庫検索 [キーワード]", "zaiko_search", "キーワード", "Mondayのキーワード検索", "どのチャンネルでも可"],
    ["検索 [キーワード]", "zaiko_search", "キーワード", "在庫検索の短縮形", "どのチャンネルでも可"],
    ["", "", "", "", ""],
    ["【管理者専用コマンド（浅野のみ）】", "", "", "", ""],
    ["浅野です [メッセージ]", "admin_announce", "メッセージ", "「📢浅野からのお知らせ」として整形して再投稿", "浅野のUserIDのみ"],
    ["", "", "", "", ""],
    ["【スタッフ向け相談トリガー】", "", "", "", ""],
    ["@浅野 + 相談（を含む文）", "consultation", "なし", "そのスレッドをボット無反応にする。浅野に通知", "どのチャンネルでも可"],
    ["", "", "", "", ""],
    ["【表記ゆれ自動補正】", "", "", "", ""],
    ["全角数字→半角", "normalize_keyword()", "", "０→0 など", ""],
    ["自社使用 / 自社利用", "normalize_channel()", "", "→ 社内利用 に統一", ""],
  ];
  sh.getRange(1, 1, data.length, 5).setValues(data);
  formatHeader(sh, 1, 5);
  formatSection(sh, [12, 15, 18]);
  sh.setColumnWidth(1, 240);
  sh.setColumnWidth(2, 120);
  sh.setColumnWidth(3, 100);
  sh.setColumnWidth(4, 300);
  sh.setColumnWidth(5, 150);
}

// ── ⑦ スコアリング ────────────────────────────────────
function createScoringSheet(ss) {
  const sh = ss.insertSheet("スコアリング");
  const data = [
    ["評価項目", "配点", "評価基準・詳細"],
    ["収益期待スコア", "30点", "予想販売価格の高さ（市場相場・オークション実績ベース）・価格の安定性・まとめ/セット売りによる付加価値可能性"],
    ["在庫回転スコア", "25点", "〜1週間:25点 / 〜1ヶ月:18点 / 〜3ヶ月:10点 / 3ヶ月超:3点　＋季節適合性・市場需要強度"],
    ["保管・物流コスト効率スコア", "25点", "保管コスト（サイズ×在庫期間）・発送コスト効率・梱包材コスト（精密機器・陶器等は減点）"],
    ["作業コスト効率スコア", "20点", "梱包難易度（割れ物・精密品・異形・超大型：大幅減点）・撮影難易度・問い合わせ対応の複雑さ"],
    ["合計", "100点", ""],
    ["", "", ""],
    ["確信度区分", "スコア範囲", "意味"],
    ["高", "75点以上", "AIが自信を持って判定できている"],
    ["中", "50〜74点", "複数チャンネルが候補。第一・第二を比較して判断"],
    ["低", "50点未満", "判定が難しい。先輩・管理者に確認推奨"],
    ["", "", ""],
    ["【内部キーワード体系】", "", ""],
    ["形式", "/[発送コード][サイズ]/[価格コード]/[期待値]", "例：/S140/4S/2"],
    ["発送コード", "S=佐川 / Y=ヤマト / SU=西濃 / AD=アートデリバリー / DC=直接引取", ""],
    ["サイズ（三辺合計cm）", "60 / 80 / 100 / 120 / 140 / 160 / 170 / 200", ""],
    ["価格コード", "J=×10 / H=×100 / S=×1,000 / M=×10,000（例：4S=4,000円）", "なるべくシンプルな表記を優先（40Hより4S）"],
    ["期待値", "1=買い手市場（売りにくい）/ 2=市場拮抗 / 3=売り手市場（売りやすい）", ""],
  ];
  sh.getRange(1, 1, data.length, 3).setValues(data);
  formatHeader(sh, 1, 3);
  formatSection(sh, [8, 13]);
  sh.setColumnWidth(1, 220);
  sh.setColumnWidth(2, 120);
  sh.setColumnWidth(3, 450);
}

// ── ⑧ 分荷判定フロー ──────────────────────────────────
function createFlowSheet(ss) {
  const sh = ss.insertSheet("分荷判定フロー");
  const data = [
    ["ステップ", "誰が", "アクション", "Bot（北大路魯山人）の動作", "分岐条件"],
    ["1", "スタッフ", "商品写真またはテキストを分荷判定チャンネルに投稿", "画像・テキストから商品を特定。「〇〇の商品と見たが合っておるか？」と確認", "新規メッセージ"],
    ["2", "スタッフ", "スレッドに「はい」または訂正情報を返信", "肯定→ステップ4へ / 訂正→再特定してステップ1に戻る", "「はい/OK」→4 / 否定→再試行"],
    ["3", "Bot", "（再特定）", "新情報で商品を再特定し確認を取る", "スタッフの訂正内容に基づく"],
    ["4", "Bot", "査定を実行", "市場相場・状態・ブランドを分析。通販候補→ステップ5へ / 非通販確定→ステップ6へ", "通販チャンネル候補か判断"],
    ["5", "Bot", "状態確認を依頼", "「動作・外観の確認をお願いしたい」と状態ランク（S/A/B/C/D）の入力を依頼", "通販チャンネル候補のとき"],
    ["6", "スタッフ", "状態ランクを入力", "例：「B 電源OK 外観に小傷あり」", "先頭1文字がS/A/B/C/Dのいずれか"],
    ["7", "Bot", "判定結果を出力", "第一候補・第二候補・スコア・予想価格・在庫期間・内部KWを出力", ""],
    ["8", "スタッフ", "確定コマンドを入力（スレッド内）", "管理番号発行・DB転記・Monday登録・動作確認チェックリストを表示", "「第一」「第二」「チャンネル名」など"],
    ["", "", "", "", ""],
    ["【確定後の自動処理】", "", "", "", ""],
    ["", "管理番号発行", "通販チャンネルのみ。YYMM-連番4桁形式", "", ""],
    ["", "スプレッドシート転記", "全チャンネル共通。GAS WebアプリにPOST", "", ""],
    ["", "Monday.com登録", "通販チャンネルのみ", "", ""],
    ["", "高額案件通知", "予想販売価格¥30,000以上 → 浅野（YA）に自動メンション", "", ""],
    ["", "作業時間記録", "投稿〜確定コマンドまでの経過時間（分）を自動計測", "", ""],
  ];
  sh.getRange(1, 1, data.length, 5).setValues(data);
  formatHeader(sh, 1, 5);
  formatSection(sh, [11]);
  sh.setColumnWidth(1, 70);
  sh.setColumnWidth(2, 80);
  sh.setColumnWidth(3, 240);
  sh.setColumnWidth(4, 320);
  sh.setColumnWidth(5, 200);
}

// ── ⑨ DBカラム定義 ────────────────────────────────────
function createDbColumnsSheet(ss) {
  const sh = ss.insertSheet("DBカラム定義");
  const data = [
    ["フィールド名（GAS送信キー）", "内容", "送信タイミング", "備考"],
    ["kanri_bango", "管理番号", "確定時", "通販のみ発行。非通販は空文字"],
    ["kakutei_channel", "確定チャンネル名", "確定時", ""],
    ["first_channel", "AI第一候補チャンネル", "確定時", ""],
    ["second_channel", "AI第二候補チャンネル", "確定時", ""],
    ["item_name", "アイテム名", "確定時", "AIが特定"],
    ["maker", "メーカー/ブランド", "確定時", ""],
    ["model_number", "品番/型式", "確定時", ""],
    ["condition", "状態ランク", "確定時", "例：B（中古美品）"],
    ["predicted_price", "予想販売価格", "確定時", "例：¥3,000〜¥5,000"],
    ["start_price", "推奨スタート価格（数値）", "確定時", ""],
    ["target_price", "推奨目標価格（数値）", "確定時", ""],
    ["inventory_period", "予測在庫期間", "確定時", ""],
    ["inventory_deadline", "推奨在庫期限", "確定時", ""],
    ["score", "総合スコア（数値）", "確定時", ""],
    ["storage_cost", "保管コスト概算（数値）", "確定時", ""],
    ["packing_cost", "梱包・発送コスト概算（数値）", "確定時", ""],
    ["expected_roi", "期待ROI（%）", "確定時", ""],
    ["internal_keyword", "推定内部KW", "確定時", "例：/S140/4S/2"],
    ["staff_id", "スタッフコード", "確定時", "UserID→スタッフコードに変換"],
    ["sakugyou_jikan", "分荷作業時間（分）", "確定時", "投稿〜確定コマンドまでの経過時間"],
    ["timestamp", "確定日時", "確定時", "YYYY/MM/DD HH:MM"],
    ["", "", "", ""],
    ["【キャンセル時のフィールド】", "", "", ""],
    ["kanri_bango", "管理番号（なければ---）", "キャンセル時", ""],
    ["kakutei_channel", "「キャンセル（元チャンネル名）」", "キャンセル時", ""],
    ["staff_id", "ユーザーID", "キャンセル時", ""],
    ["timestamp", "日時", "キャンセル時", ""],
    ["", "", "", ""],
    ["【その他アクション（actionフィールドで識別）】", "", "", ""],
    ["checklist_update", "動作確認チェックリスト完了時", "撮影確認チャンネル", "kanri_bango / condition / checklist_comment"],
    ["satsuei_update", "撮影完了（「完了」入力）時", "撮影確認チャンネル", "kanri_bango / drive_folder_url"],
    ["work_activity", "作業ログ（完了・キャンセル・削除）", "全チャンネル", "channel / kanri_bango / operation / duration_seconds"],
    ["shuppinon_listing", "出品登録（ロケーション番号入力）時", "出品保管チャンネル", "kanri_bango / title / description / condition / start_price / size / location"],
    ["shipping_update", "出荷手配完了時", "梱包出荷チャンネル", "kanri_bango / carrier / tracking_number"],
    ["kobutsu_daichou", "古物台帳登録（「登録」入力）時", "現場査定チャンネル", "timestamp / item_name / price / name / address / birthdate / id_number / doc_type"],
    ["genba_memo", "知識インプット時", "現場査定チャンネル", "staff_id / message"],
    ["genba_satei", "現場査定結果時", "現場査定チャンネル", "staff_id / input / result"],
    ["attendance", "出退勤申告時", "出退勤チャンネル", "staff_id / date / start_time / end_time / total_minutes / break_minutes / net_hours / completed_count"],
    ["kintai_renraku", "勤怠連絡チャンネル投稿時", "勤怠連絡チャンネル", "staff_id / message"],
  ];
  sh.getRange(1, 1, data.length, 4).setValues(data);
  formatHeader(sh, 1, 4);
  formatSection(sh, [24, 30]);
  sh.setColumnWidth(1, 220);
  sh.setColumnWidth(2, 240);
  sh.setColumnWidth(3, 160);
  sh.setColumnWidth(4, 320);
}

// ── ⑩ Mondayカラム ────────────────────────────────────
function createMondayColumnsSheet(ss) {
  const sh = ss.insertSheet("Mondayカラム");
  const data = [
    ["カラムID", "型", "内容", "備考"],
    ["kanri_bango", "text", "管理番号", ""],
    ["hantei_channel", "text", "判定チャンネル（確定チャンネル）", ""],
    ["kakushin_do", "text", "確信度（高/中/低）", "キャンセル時は「キャンセル」"],
    ["toshosha", "text", "投稿者スタッフコード", ""],
    ["zaiko_kikan", "text", "在庫予測期間", ""],
    ["status", "status", "ステータスラベル", "査定待ち→動作確認済み→撮影済み→出品中→梱包済み→出荷済み / キャンセル / 要確認"],
    ["yosou_kakaku", "numbers", "予想販売価格（数値のみ）", ""],
    ["score", "numbers", "総合スコア", ""],
    ["sakugyou_jikan", "numbers", "分荷作業時間（分）", ""],
    ["internal_keyword", "text", "推定内部キーワード", "例：/S140/4S/2"],
    ["", "", "", ""],
    ["ボードID", "18403611418", "", ""],
    ["ステータス遷移", "査定待ち（確定時）→ 動作確認済み → 撮影済み → 出品中 → 梱包済み → 出荷済み", "", ""],
  ];
  sh.getRange(1, 1, data.length, 4).setValues(data);
  formatHeader(sh, 1, 4);
  sh.setColumnWidth(1, 160);
  sh.setColumnWidth(2, 80);
  sh.setColumnWidth(3, 240);
  sh.setColumnWidth(4, 320);
}

// ── ⑪ Botキャラクター ─────────────────────────────────
function createBotCharSheet(ss) {
  const sh = ss.insertSheet("Botキャラクター");
  const data = [
    ["キー", "表示名", "Slackチャンネル名", "担当業務", "キャラクター・口調"],
    ["bunika", "北大路魯山人", "分荷判定（デフォルト）", "分荷判定メイン", "料理・陶芸・書を極めた美食家。「ふむ」「よかろう」「〜じゃな」。温かみのある職人語り"],
    ["satsuei", "白洲次郎", "白洲次郎（撮影確認）", "撮影・Drive保存", "短く的確。「〜だ」「悪くない」。歯切れよい"],
    ["shuppinon", "岩崎弥太郎", "岩崎弥太郎（出品保管）", "出品データ生成・ロケーション管理", "「〜じゃ！」「ようやった！」豪快で前向き"],
    ["konpo", "黒田官兵衛", "黒田官兵衛（梱包出荷）", "梱包・追跡番号OCR・出荷完了", "「承知した」「案ずるな」冷静で頼もしい"],
    ["status", "ステータス松本", "ステータス松本（ステータス確認）", "在庫ステータス・進捗確認", "「〜やで」「ありがとさん」関西ロック。熱くて人情味"],
    ["attendance", "（Bot名なし）", "二宮金次郎（出退勤）", "勤務時間申告・実働計算・代筆対応", "返信あり。勤務記録・実働時間計算をGASに転記"],
    ["genba", "渋沢栄一", "渋沢栄一（現場査定）", "買取査定・廃棄判断・古物台帳・知識インプット", "「〜であります」「算盤に合う」丁寧で実業家らしい"],
    ["kintai", "（サイレント）", "サイレント記録（勤怠連絡）", "欠勤・遅刻等の連絡を記録のみ", "返信なし。投稿を全てGASに転記するのみ"],
  ];
  sh.getRange(1, 1, data.length, 5).setValues(data);
  formatHeader(sh, 1, 5);
  sh.setColumnWidth(1, 100);
  sh.setColumnWidth(2, 160);
  sh.setColumnWidth(3, 220);
  sh.setColumnWidth(4, 200);
  sh.setColumnWidth(5, 340);
}

// ── ⑫ 実装済み/未実装 ─────────────────────────────────
function createImplSheet(ss) {
  const sh = ss.insertSheet("実装済み/未実装");
  const data = [
    ["区分", "機能", "実装状況", "備考"],
    ["実装済み", "分荷判定フロー（8ステップ）", "✅ 完了", "北大路魯山人チャンネル"],
    ["実装済み", "管理番号自動発行（YYMM-連番形式）", "✅ 完了", "重複防止・スレッドセーフ"],
    ["実装済み", "スプレッドシート自動転記", "✅ 完了", "GAS Webアプリ経由"],
    ["実装済み", "Monday.com在庫登録", "✅ 完了", "通販チャンネルのみ"],
    ["実装済み", "動作確認チェックリスト", "✅ 完了", "確定後に自動表示"],
    ["実装済み", "撮影Drive保存", "✅ 完了", "白洲次郎チャンネル"],
    ["実装済み", "出品データ生成（タイトル・説明文）", "✅ 完了", "岩崎弥太郎チャンネル"],
    ["実装済み", "梱包出荷フロー（追跡番号OCR）", "✅ 完了", "黒田官兵衛チャンネル"],
    ["実装済み", "古物台帳フロー（身分証Vision OCR）", "✅ 完了", "渋沢栄一チャンネル"],
    ["実装済み", "出退勤申告・実働計算", "✅ 完了", "二宮金次郎チャンネル"],
    ["実装済み", "在庫検索コマンド（全チャンネル共通）", "✅ 完了", "Monday.comキーワード検索"],
    ["実装済み", "ステータス確認チャンネル", "✅ 完了", "ステータス松本チャンネル"],
    ["実装済み", "高額案件（¥30,000以上）自動メンション", "✅ 完了", "浅野宛"],
    ["実装済み", "再判定コマンド（スレッド履歴付き）", "✅ 完了", ""],
    ["実装済み", "管理者専用：浅野のメッセージを無視", "✅ 完了", "ADMIN_USER_IDで判定"],
    ["実装済み", "管理者専用：「浅野です」お知らせコマンド", "✅ 完了", "整形して再投稿"],
    ["実装済み", "相談モード（@浅野+相談でスレッド無反応化）", "✅ 完了", ""],
    ["実装済み", "勤怠連絡サイレント記録", "✅ 完了", "返信なしでGAS転記"],
    ["実装済み", "現場査定・廃棄判断・知識インプット", "✅ 完了", "渋沢栄一チャンネル"],
    ["", "", "", ""],
    ["未実装", "ヤフオクAPI自動出品", "⬜ 未実装", "オークタウンAPI確認後・TODO コメントあり"],
    ["未実装", "オークファン連携（相場価格リアルタイム取得）", "⬜ 未実装", "将来対応予定"],
    ["未実装", "販売完了履歴シート（Sheet2）自動転記", "⬜ 未実装", "Monday.com販売完了時"],
    ["未実装", "eBay自動出品", "⬜ 未実装", "将来対応予定"],
    ["未実装", "Shopify連携", "⬜ 未実装", "将来対応予定"],
    ["未実装", "管理会計・インセンティブ自動計算", "⬜ 未実装", "将来対応予定"],
    ["", "", "", ""],
    ["スタッフ対応待ち", "横山優(YY) UserID取得", "⬜ 待機", "SlackプロフィールのメンバーIDをSTAFF_MAPに追加"],
    ["スタッフ対応待ち", "三島圭織(KM) UserID取得", "⬜ 待機", "同上"],
    ["スタッフ対応待ち", "松本豊彦(TM) UserID取得", "⬜ 待機", "同上"],
    ["スタッフ対応待ち", "北瀬孝(TK) UserID取得", "⬜ 待機", "同上"],
    ["スタッフ対応待ち", "白木雄介(YS) UserID取得", "⬜ 待機", "同上"],
  ];
  sh.getRange(1, 1, data.length, 4).setValues(data);
  formatHeader(sh, 1, 4);
  for (let i = 2; i <= 20; i++) {
    sh.getRange(i, 3).setBackground("#d4edda").setFontColor("#155724");
  }
  for (let i = 22; i <= 27; i++) {
    sh.getRange(i, 3).setBackground("#fff3cd").setFontColor("#856404");
  }
  sh.setColumnWidth(1, 140);
  sh.setColumnWidth(2, 280);
  sh.setColumnWidth(3, 100);
  sh.setColumnWidth(4, 300);
}

// ════════════════════════════════════════════════════════
// 作業ログシートセットアップ
// ════════════════════════════════════════════════════════
function setupLogSheet() {
  const ss = SpreadsheetApp.openById(LOG_SHEET_ID);
  let sh = ss.getSheetByName("作業ログ");
  if (!sh) sh = ss.getSheets()[0];
  sh.setName("作業ログ");
  sh.clearContents();

  const header = [["日時", "作業者", "変更ファイル", "変更内容", "Gitコミット", "備考"]];
  sh.getRange(1, 1, 1, 6).setValues(header);
  formatHeader(sh, 1, 6);

  // 初回ログを記録
  const firstLog = [
    [new Date(), "浅野儀頼", "app.py / CLAUDE.md / マニュアル",
     "初期セットアップ：管理番号フォーマット統一・管理者専用機能追加・相談モード実装・旧区分コード削除",
     "e5bcd0e", "Google Drive共有スプレッドシートに仕様書・ログ整備"]
  ];
  sh.getRange(2, 1, 1, 6).setValues(firstLog);
  sh.getRange(2, 1).setNumberFormat("yyyy/mm/dd hh:mm");

  sh.setColumnWidth(1, 140);
  sh.setColumnWidth(2, 80);
  sh.setColumnWidth(3, 200);
  sh.setColumnWidth(4, 380);
  sh.setColumnWidth(5, 100);
  sh.setColumnWidth(6, 200);

  // 書式：日時列
  sh.getRange("A:A").setNumberFormat("yyyy/mm/dd hh:mm");
}

// ════════════════════════════════════════════════════════
// 共通フォーマット関数
// ════════════════════════════════════════════════════════
function formatHeader(sh, row, cols) {
  const range = sh.getRange(row, 1, 1, cols);
  range.setBackground("#1a3a2a").setFontColor("#ffffff").setFontWeight("bold").setFontSize(10);
  sh.setFrozenRows(1);
}

function formatSection(sh, rows) {
  rows.forEach(r => {
    const maxCol = sh.getLastColumn() || 5;
    sh.getRange(r, 1, 1, maxCol).setBackground("#f0f5f2").setFontWeight("bold");
  });
}
