// ============================================================
// 分荷判定Bot - ヤフオク出品管理スプレッドシート GAS
// ============================================================

// ====== 設定 ======
const CONFIG = {
  ANTHROPIC_API_KEY: PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY'),
  DB_SPREADSHEET_ID: '1CWG9MVrsw9gJwp31lCrUs9KB0a1zptZY1cPO47ZNmVU', // スプレッドシート①
  DRIVE_ROOT_FOLDER_ID: '', // 商品画像ルートフォルダID（要設定）
  AUCTION_DURATION_DAYS: 4,
  AUCTION_END_HOUR: 22,
  AUTO_RELIST: 3,
  SHIPPING_FROM: '岐阜県',
};

// ====== 出品担当マーク（タイトル先頭に付与） ======
// Slack UserID → ヤフオクタイトル識別マーク
// 担当者が増えたら設定マスタシートで管理（下記はフォールバック用）
const LISTING_MARKS = {
  // Slack UserID: [マーク, 担当者名]  ← UserID確定後に更新
  'KH': ['〇', '林和人'],
  'YY': ['▽', '横山優'],
  'TK': ['◇', '鶴岡'],
  // 追加例: 'UXXXXXXXX': ['△', '新担当者名'],
};

// ====== シート名 ======
const SH = {
  MAIN:     '出品管理',
  SAGAWA:   '送料_佐川',
  SEINO:    '送料_西濃ミニ便',
  AD:       '送料_アートデリバリー',
  TEMPLATE: '説明文テンプレート',
  SETTINGS: '設定マスタ',
};

// ====== 佐川運賃表（岐阜発・契約価格）======
// 社内コスト計算用。商品ページには表示しない。
// [サイズ, 関西, 東海, 関東, 信越, 北陸, 中国, 四国, 北九州, 南九州, 南東北, 北東北, 北海道]
const SAGAWA_RATES = [
  [60,  550, 540, 570, 570, 550, 570, 570, 580, 580, 570, 590, 600],
  [80,  600, 590, 620, 610, 600, 620, 620, 640, 660, 630, 680, 710],
  [100, 640, 620, 690, 680, 650, 710, 690, 740, 770, 730, 810, 870],
  [140, 970, 880,1080,1060, 990,1130,1090,1240,1340,1190,1410,1620],
  [160,1230,1100,1400,1370,1270,1470,1410,1620,1780,1570,1900,2200],
  [170,1750,1560,2050,2000,1820,2160,2060,2420,2670,2330,2880,3390],
  [180,2140,1910,2490,2420,2230,2620,2510,2940,3230,2820,3490,4100],
  [200,2720,2400,3180,3100,2830,3360,3220,3790,4170,3630,4510,5330],
  [220,3280,2890,3850,3760,3410,4090,3900,4610,5110,4430,5510,6540],
  [240,4450,3890,5250,5030,4620,5570,5310,6150,6990,6030,7220,9000],
  [260,5610,4910,6650,6150,5830,6960,6730,7600,8830,7660,8930,11490],
];
const SAGAWA_REGIONS = ['関西','東海','関東','信越','北陸','中国','四国','北九州','南九州','南東北','北東北','北海道'];

// ====== 佐川運賃表（岐阜発・定価）======
// 商品ページ（ヤフオク説明文）への表示用。
// 出典: 佐川急便公式 東海発 宅配料金表（通常配達）
// [サイズ, 北海道, 北東北, 南東北, 関東, 信越, 中部(東海), 北陸, 関西, 中国, 四国, 北九州, 南九州, 沖縄]
const SAGAWA_PUBLIC_RATES = [
  [60,  1570, 1180, 1040,  910,  910,  910,  910,  910, 1040, 1180, 1180, 1180,  1914],
  [80,  1840, 1470, 1340, 1220, 1220, 1220, 1220, 1220, 1340, 1470, 1470, 1470,  3080],
  [100, 2130, 1740, 1630, 1520, 1520, 1520, 1520, 1520, 1630, 1740, 1740, 1740,  5016],
  [140, 2830, 2440, 2310, 2180, 2180, 2180, 2180, 2180, 2310, 2440, 2440, 2440,  7260],
  [160, 3090, 2700, 2570, 2440, 2440, 2440, 2440, 2440, 2570, 2700, 2700, 2700,  9493],
  [170, 4770, 3770, 3420, 3360, 2890, 2600, 2770, 2770, 3130, 3130, 3360, 3710, 14333],
  [180, 5360, 4130, 3770, 3660, 3130, 2890, 2950, 2950, 3420, 3420, 3660, 4130, 16753],
  [200, 6720, 5130, 4600, 4480, 3720, 3480, 3480, 3480, 4130, 4130, 4420, 5070, 21593],
  [220, 8070, 6070, 5420, 5240, 4360, 4070, 4070, 4070, 4840, 4840, 5240, 5950, 26433],
  [240,10780, 7950, 7070, 6830, 5540, 5240, 5240, 5240, 6240, 6240, 6770, 7830, 36113],
  [260,13480, 9830, 8710, 8420, 6770, 6420, 6420, 6420, 7660, 7660, 8360, 9720, 45793],
];
const SAGAWA_PUBLIC_REGIONS = ['北海道','北東北','南東北','関東','信越','中部','北陸','関西','中国','四国','北九州','南九州','沖縄'];

// 都道府県 → 佐川地帯マッピング
const PREF_TO_SAGAWA_REGION = {
  '北海道': '北海道',
  '青森': '北東北', '岩手': '北東北', '秋田': '北東北',
  '宮城': '南東北', '山形': '南東北', '福島': '南東北',
  '茨城': '関東', '栃木': '関東', '群馬': '関東',
  '埼玉': '関東', '千葉': '関東', '東京': '関東',
  '神奈川': '関東', '山梨': '関東',
  '新潟': '信越', '長野': '信越',
  '富山': '北陸', '石川': '北陸', '福井': '北陸',
  '岐阜': '東海', '静岡': '東海', '愛知': '東海', '三重': '東海',
  '滋賀': '関西', '京都': '関西', '大阪': '関西',
  '兵庫': '関西', '奈良': '関西', '和歌山': '関西',
  '鳥取': '中国', '島根': '中国', '岡山': '中国',
  '広島': '中国', '山口': '中国',
  '徳島': '四国', '香川': '四国', '愛媛': '四国', '高知': '四国',
  '福岡': '北九州', '佐賀': '北九州', '長崎': '北九州', '大分': '北九州',
  '熊本': '南九州', '宮崎': '南九州', '鹿児島': '南九州',
};

// ====== 西濃ミニ便（岐阜発・契約率80%） ======
// [サイズ名, 最大重量kg, 最大cm, 北海道, 北東北, 南東北, 北関東, 南関東, 甲信越, 北陸, 中部, 近畿, 中国, 四国, 北九州, 南九州]
const SEINO_MINI_RATES = [
  ['P', 2,  60, 1340, 950, 820, 680, 680, 680, 680, 680, 680, 820, 950, 950,1070],
  ['S', 5,  70, 1570,1160,1030, 880, 880, 880, 880, 880, 880,1030,1160,1160,1290],
  ['M',10, 100, 1790,1370,1230,1070,1070,1070,1070,1070,1070,1230,1370,1370,1500],
  ['L',20, 130, 2080,1600,1450,1270,1270,1270,1270,1270,1270,1450,1600,1600,1750],
];
const SEINO_CONTRACT_RATE = 0.80;
const SEINO_REGIONS = ['北海道','北東北','南東北','北関東','南関東','甲信越','北陸','中部','近畿','中国','四国','北九州','南九州'];

// ====== 免責・注意事項文（共通） ======
const DISCLAIMER = `※ご入札前に必ずお読み下さい。

・基本的に、商品は一点ものとなり、中古でアンティーク・ヴィンテージ品を数多く扱っております。
　動作保証から以上は給を要することによりいますし、その状態の善しあしは問いません。
・未使用やデッドストック品も含む今後の動作の保証はできません。

・専門的な知識のない部品も数多くあり、正確な動作・状態・作製年代などを保証できません。
　機材の詳細な機能や機能、変錆の若色や清澄、箇こつまましても正当な判断で判断できません。
以上をご確認の上、全品ノークレームノーリターンでお願いします。
また、神経質な方は入札ご遠慮ください。

・お取引市場内容以上は翌日より1週間以内でのご連絡にしていただいております。
・同梱をご希望の場合、着荷サイズが変更となることが御座いますのでお問合せをいただく場合が御座います。
　梱包後の3辺の合計が160cm/60kgを超える場合、重量が25kgを超える場合は同梱できません。
　梱包後に配送料が変わることが御座いますのでご了承下さい。
※同梱希望の方は、当方からの送料確認後をお待ちいただきますようお願い致します。
　発送確認後が落札されてしまう場合があります。

・入金確認後おおよそ1週間以内に発送させていただいております。
　1週間を超えた場合は購入者へのないものとお断りさせていただく場合、当相補的の取消キャンセルし、落荷を請請する場合があります。
・委托储関について、ご入金確認後1週間以内の発送とさせていただいております。
※大型道域や、市況によっては配置業者で業護できる場合もございますのでご了承ください。

◆営業日：月・火・水・木・金 10:00〜16:00
　　　　　（祝日・日曜・祝日・その他特殊時間）

入金確認遅延対応に基準を知っておりますので
即日発送や翌日引受なども早急なお届けには対応できない場合も御座います。
等にご理解いただければいただけたらいいです。

又以祝日等特別期間以上はかねかため遅滞が見滞れる場合が御座いますのでご了解下さい。

ご覧いただきありがとうございました。`;

// ============================================================
// セットアップ：スプレッドシートを初期化
// ============================================================
function setupSpreadsheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  // 既存シートを削除して再作成（初回のみ使用）
  const existing = ss.getSheets().map(s => s.getName());

  _setupMainSheet(ss, existing);
  _setupSagawaSheet(ss, existing);
  _setupSeinoSheet(ss, existing);
  _setupADSheet(ss, existing);
  _setupTemplateSheet(ss, existing);
  _setupSettingsSheet(ss, existing);

  SpreadsheetApp.getUi().alert('セットアップ完了しました。');
}

function _getOrCreateSheet(ss, name, existing) {
  if (existing.includes(name)) return ss.getSheetByName(name);
  return ss.insertSheet(name);
}

// ====== Sheet1: 出品管理 ======
function _setupMainSheet(ss, existing) {
  const sh = _getOrCreateSheet(ss, SH.MAIN, existing);
  sh.clearContents();

  const headers = [
    // 管理情報
    '管理番号', 'アカウント区分', 'アイテム名', 'メーカー/ブランド', '品番/型式',
    '状態', '内部KW', '担当者', '分荷確定日時',
    // 発送情報
    '発送会社', '発送サイズ', '発送重量目安(kg)',
    // 出品情報
    '出品タイトル(65文字以内)', 'カテゴリID', '開始価格',
    // 説明文（長文なので別列）
    '説明文',
    // 画像
    '画像フォルダURL', '画像1URL', '画像2URL', '画像3URL', '画像4URL', '画像5URL',
    // オークション設定（固定）
    '出品期間(日)', '終了時刻', '自動再出品回数',
    // ステータス
    '出品ステータス', '出品日時', '終了予定日時', '落札価格', '落札者ID', '在庫日数(分荷〜落札)',
    // 地域別送料（自動計算）
    '送料_北海道', '送料_東北', '送料_関東', '送料_信越', '送料_北陸',
    '送料_東海', '送料_関西', '送料_中国', '送料_四国', '送料_北九州', '送料_南九州',
    '沖縄',
  ];

  sh.getRange(1, 1, 1, headers.length).setValues([headers]);
  sh.getRange(1, 1, 1, headers.length)
    .setBackground('#1a73e8').setFontColor('white').setFontWeight('bold');

  // 列幅設定
  sh.setColumnWidth(1, 110);   // 管理番号
  sh.setColumnWidth(13, 300);  // タイトル
  sh.setColumnWidth(16, 400);  // 説明文
  sh.setColumnWidth(17, 200);  // フォルダURL
  sh.setFrozenRows(1);

  // デフォルト値（固定列）
  // 新規行追加時はappendRowで対応
}

// ====== Sheet2: 送料_佐川 ======
function _setupSagawaSheet(ss, existing) {
  const sh = _getOrCreateSheet(ss, SH.SAGAWA, existing);
  sh.clearContents();

  const weights = [2, 5, 10, 20, 30, 50, 50, 50, 50, 50, 50];

  // 契約価格テーブル
  sh.getRange(1, 1).setValue('【契約価格】社内コスト計算用').setFontWeight('bold').setBackground('#e8a000').setFontColor('white');
  const contractHeaders = ['サイズ', '重量上限(kg)', ...SAGAWA_REGIONS, '沖縄'];
  sh.getRange(2, 1, 1, contractHeaders.length).setValues([contractHeaders]);
  sh.getRange(2, 1, 1, contractHeaders.length).setBackground('#fce8b2').setFontWeight('bold');
  const contractRows = SAGAWA_RATES.map((r, i) => [r[0], weights[i], ...r.slice(1), '要問合せ']);
  sh.getRange(3, 1, contractRows.length, contractRows[0].length).setValues(contractRows);

  // 定価テーブル（2行空けて）
  const pubStartRow = 3 + contractRows.length + 2;
  sh.getRange(pubStartRow, 1).setValue('【定価（公式料金）】商品ページ表示用').setFontWeight('bold').setBackground('#1a73e8').setFontColor('white');
  const pubHeaders = ['サイズ', '重量上限(kg)', ...SAGAWA_PUBLIC_REGIONS];
  sh.getRange(pubStartRow + 1, 1, 1, pubHeaders.length).setValues([pubHeaders]);
  sh.getRange(pubStartRow + 1, 1, 1, pubHeaders.length).setBackground('#c9daf8').setFontWeight('bold');
  const pubRows = SAGAWA_PUBLIC_RATES.map((r, i) => [r[0], weights[i], ...r.slice(1)]);
  sh.getRange(pubStartRow + 2, 1, pubRows.length, pubRows[0].length).setValues(pubRows);

  sh.setFrozenRows(2);
}

// ====== Sheet3: 送料_西濃ミニ便 ======
function _setupSeinoSheet(ss, existing) {
  const sh = _getOrCreateSheet(ss, SH.SEINO, existing);
  sh.clearContents();

  sh.getRange(1, 1).setValue('※契約率80%適用済み金額');
  const headers = ['サイズ区分', '最大重量(kg)', '最大辺長(cm)', ...SEINO_REGIONS, '沖縄'];
  sh.getRange(2, 1, 1, headers.length).setValues([headers]);
  sh.getRange(2, 1, 1, headers.length)
    .setBackground('#0f9d58').setFontColor('white').setFontWeight('bold');

  const rows = SEINO_MINI_RATES.map(r => [
    r[0], r[1], r[2],
    ...r.slice(3).map(v => Math.ceil(v * SEINO_CONTRACT_RATE)),
    '別途',
  ]);
  sh.getRange(3, 1, rows.length, rows[0].length).setValues(rows);
  sh.setFrozenRows(2);
}

// ====== Sheet4: 送料_アートデリバリー ======
function _setupADSheet(ss, existing) {
  const sh = _getOrCreateSheet(ss, SH.AD, existing);
  sh.clearContents();

  sh.getRange(1,1).setValue('アートデリバリー料金表（手入力）');
  sh.getRange(1,1).setFontWeight('bold').setBackground('#db4437').setFontColor('white');
  const headers = ['品名/サイズ目安', '関西', '東海', '関東', '東北', '九州', '北海道', '備考'];
  sh.getRange(2, 1, 1, headers.length).setValues([headers]);
  sh.getRange(2, 1, 1, headers.length).setFontWeight('bold').setBackground('#f4c7c3');
  sh.getRange(3, 1).setValue('（実績ベースで追記してください）');
}

// ====== Sheet5: 説明文テンプレート ======
function _setupTemplateSheet(ss, existing) {
  const sh = _getOrCreateSheet(ss, SH.TEMPLATE, existing);
  sh.clearContents();

  const vintageTemplate = `◇算◇

《商品説明》
{アイテム名}です。
{商品詳細}

・アイソンの鈴です。
・全体的に通常が確認でありますが残っております。
・鍋に茶みがございます。

・ヴィンテージ品でインテリアとしておしゃれです。

・経年の汚れ、傷、擦れ、傷み、くすみなどお調べください。
・超認な商品としの状判断をしておりません。
・古いものですので※全な動作や他貨を保証できません。
・メンテナンス・作業や必要な場合深刻,州様でお願いします。
※その他不明な点などがございましたら、当方がわかる範囲にてお答えいたします。

◇◆◆◆◆◆◆◆

《サイズ詳細》
高さ：約{高さ}cm
幅：約{幅}cm
奥行：約{奥行}cm

{送料テーブル}

{免責文}`;

  const currentTemplate = `{内部KW}

【簡易説明】
・{メーカー} {アイテム名} {品番}です。

メーカー
・{メーカー}

品名
・{アイテム名}

型番
・{品番}

商品詳細
{商品詳細}

数量
・1台

サイズ / ㎝
・幅未計測
・奥行未計測
・高さ未計測

重量
・未計測

※サイズ及び重量については簡易計測の為正確ではございませんのでご了承願います。

【注意事項】
・付属部品の欠品等の有無は確認しておりません。
・ジャンク品（部品取り、要整備品）

【状態】
・外箱なし
{状態詳細}

【発送方法】
・発送は岐阜県からの、宅配便にての発送になります。
＝送料は（令和6年2月2日より送料を改訂しました。）

{送料テーブル}

【取引詳細・その他】
・平日16:00以及および十日祝はご回答・返答・送返送状等が出来ませんのでご連絡が遅くなります。
・ご入札をお考えの方で初回な方がある方は、入札前に直確認よりご連絡ください。
・複数をお買い求めになる方は日数を少しく広めてください。入托状態はお願いします。
・写真写に写っているものが全てとなります。
・動作の未保証のため入托状态を確定できません。
・箇品に関して適度(ご)神経質な方のご入托はお控えください。

{免責文}`;

  sh.getRange('A1').setValue('=== ビンテージ用テンプレート ===');
  sh.getRange('A2').setValue(vintageTemplate);
  sh.getRange('A4').setValue('=== 現行品用テンプレート ===');
  sh.getRange('A5').setValue(currentTemplate);
  sh.getRange('A7').setValue('=== まとめ売り用テンプレート ===');
  sh.getRange('A8').setValue('（まとめ売りは個別カスタマイズのため現行品テンプレート準用）');

  sh.getRange('A1').setFontWeight('bold').setBackground('#9fc5e8');
  sh.getRange('A4').setFontWeight('bold').setBackground('#b6d7a8');
  sh.getRange('A7').setFontWeight('bold').setBackground('#ffe599');
  sh.setColumnWidth(1, 600);
  sh.setRowHeight(2, 400);
  sh.setRowHeight(5, 400);
}

// ====== Sheet6: 設定マスタ ======
function _setupSettingsSheet(ss, existing) {
  const sh = _getOrCreateSheet(ss, SH.SETTINGS, existing);
  sh.clearContents();

  const data = [
    ['=== オークション設定 ==='],
    ['出品期間(日)', CONFIG.AUCTION_DURATION_DAYS],
    ['終了時刻', CONFIG.AUCTION_END_HOUR + ':00'],
    ['自動再出品回数', CONFIG.AUTO_RELIST],
    ['出品形式', 'オークション'],
    ['即決価格', 'なし'],
    [''],
    ['=== 発送設定 ==='],
    ['発送元', CONFIG.SHIPPING_FROM],
    ['発送までの日数', '7日程度'],
    [''],
    ['=== アカウント区分 ==='],
    ['V', 'ヤフオクビンテージ'],
    ['G', 'ヤフオク現行品'],
    ['M', 'ヤフオクまとめ売り'],
    ['E', 'eBay'],
    [''],
    ['=== 発送会社コード ==='],
    ['S', '佐川急便'],
    ['Y', 'ヤマト運輸'],
    ['SU', '西濃運輸'],
    ['AD', 'アートデリバリー'],
    ['DC', '購入者直接引取り'],
    [''],
    ['=== 出品ステータス ==='],
    ['未出品', ''],
    ['出品中', ''],
    ['終了（不落札）', ''],
    ['落札済み', ''],
    ['キャンセル', ''],
    [''],
    ['=== 出品担当マーク（タイトル先頭） ==='],
    ['担当者コード/SlackID', 'マーク', '担当者名'],
    ['KH', '〇', '林和人'],
    ['YY', '▽', '横山優'],
    ['TK', '◇', '鶴岡'],
    ['（追加行）', '', ''],
  ];

  sh.getRange(1, 1, data.length, 2).setValues(data.map(r => r.length === 1 ? [r[0], ''] : r));
  sh.getRange('A1').setFontWeight('bold').setBackground('#666666').setFontColor('white');
  sh.getRange('A8').setFontWeight('bold').setBackground('#666666').setFontColor('white');
  sh.getRange('A12').setFontWeight('bold').setBackground('#666666').setFontColor('white');
  sh.getRange('A16').setFontWeight('bold').setBackground('#666666').setFontColor('white');
  sh.getRange('A21').setFontWeight('bold').setBackground('#666666').setFontColor('white');
  sh.getRange('A26').setFontWeight('bold').setBackground('#666666').setFontColor('white');
  sh.getRange('A32').setFontWeight('bold').setBackground('#666666').setFontColor('white');
  // 出品担当マーク ヘッダー行
  sh.getRange('A33').setFontWeight('bold').setBackground('#d9d2e9');
  sh.getRange('B33').setFontWeight('bold').setBackground('#d9d2e9');
  sh.getRange('C33').setFontWeight('bold').setBackground('#d9d2e9');
}

// ============================================================
// 送料計算ユーティリティ
// ============================================================

/**
 * 佐川送料取得（岐阜発）
 * @param {number} size - サイズ（60/80/100/140/160/170/180/200/220/240/260）
 * @param {string} region - 地域名（SAGAWA_REGIONS内の値）
 * @returns {number} 送料（円）
 */
function getSagawaRate(size, region) {
  const row = SAGAWA_RATES.find(r => r[0] >= size);
  if (!row) return -1;
  const colIdx = SAGAWA_REGIONS.indexOf(region);
  if (colIdx < 0) return -1;
  return row[colIdx + 1];
}

/**
 * 都道府県名から佐川地帯を取得
 */
function prefToSagawaRegion(pref) {
  return PREF_TO_SAGAWA_REGION[pref] || null;
}

/**
 * 内部KWからサイズを抽出
 * 例: /S140/4S/2 → 140
 */
function extractSizeFromKW(kw) {
  const m = kw.match(/[SYAD]+(\d+)/);
  return m ? parseInt(m[1]) : null;
}

/**
 * 内部KWから発送会社を抽出
 * 例: /S140/4S/2 → S
 */
function extractCarrierFromKW(kw) {
  const m = kw.match(/\/(S|Y|SU|AD|DC)(\d+)/);
  return m ? m[1] : null;
}

/**
 * 佐川の地域別送料テーブル文字列を生成（説明文埋め込み用・定価表示）
 * SAGAWA_PUBLIC_RATES を使用（ヤフオク商品ページ表示用）
 * @param {number} size
 * @returns {string}
 */
function buildSagawaShippingTable(size) {
  const row = SAGAWA_PUBLIC_RATES.find(r => r[0] >= size);
  if (!row) return '（送料要確認）';

  // インデックス: [北海道, 北東北, 南東北, 関東, 信越, 中部, 北陸, 関西, 中国, 四国, 北九州, 南九州, 沖縄]
  const r = row;
  return `【送料（佐川急便・岐阜県発）】
〈北海道〉　${r[1].toLocaleString()}円
〈青森・秋田・岩手〉　${r[2].toLocaleString()}円
〈宮城・山形・福島〉　${r[3].toLocaleString()}円
〈茨城・栃木・群馬・埼玉・千葉・東京・神奈川・山梨〉　${r[4].toLocaleString()}円
〈長野・新潟〉　${r[5].toLocaleString()}円
〈静岡・愛知・岐阜・三重〉　${r[6].toLocaleString()}円
〈富山・石川・福井〉　${r[7].toLocaleString()}円
〈滋賀・京都・大阪・兵庫・和歌山・奈良〉　${r[8].toLocaleString()}円
〈岡山・広島・山口・鳥取・島根〉　${r[9].toLocaleString()}円
〈香川・徳島・高知・愛媛〉　${r[10].toLocaleString()}円
〈福岡・佐賀・長崎・大分〉　${r[11].toLocaleString()}円
〈熊本・宮崎・鹿児島〉　${r[12].toLocaleString()}円
〈沖縄〉　${r[13].toLocaleString()}円
※離島・一部地域は別途お見積りとなる場合があります。
・簡易梱包として、段ボール・エアキャップ・新聞紙・紙袋などを使用しております。`;
}

// ============================================================
// AI説明文生成
// ============================================================

/**
 * 選択行のAI説明文を生成してセルに書き込む
 * 出品管理シートで行を選択してから実行
 */
function generateDescription() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(SH.MAIN);
  const ui = SpreadsheetApp.getUi();
  const row = sh.getActiveRange().getRow();

  if (row <= 1) {
    ui.alert('説明文を生成する商品行を選択してください。');
    return;
  }

  const headers = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  const values = sh.getRange(row, 1, 1, sh.getLastColumn()).getValues()[0];
  const get = (name) => values[headers.indexOf(name)] || '';

  const kanriNo    = get('管理番号');
  const category   = get('アカウント区分'); // V/G/M
  const itemName   = get('アイテム名');
  const maker      = get('メーカー/ブランド');
  const modelNo    = get('品番/型式');
  const condition  = get('状態');
  const kw         = get('内部KW');
  const carrier    = get('発送会社') || extractCarrierFromKW(kw);
  const size       = parseInt(get('発送サイズ')) || extractSizeFromKW(kw);

  // 送料テーブル生成
  let shippingTable = '';
  if (carrier === 'S') {
    shippingTable = buildSagawaShippingTable(size);
  } else if (carrier === 'SU') {
    shippingTable = '西濃ミニ便で発送いたします。送料はサイズ・地域によって異なります。お気軽にお問合せください。';
  } else if (carrier === 'AD') {
    shippingTable = 'アートデリバリーにて発送いたします。送料は地域により異なりますので事前にお問合せください。';
  } else if (carrier === 'DC') {
    shippingTable = '直接引取りのみとなります。岐阜県よりお越しください。';
  }

  // テンプレート種別
  const templateType = category === 'V' ? 'ビンテージ・アンティーク' : (category === 'M' ? 'まとめ売り' : '現行品・中古');

  const prompt = `あなたはヤフオク出品の説明文を作成するアシスタントです。
以下の商品情報をもとに、日本語でヤフオク出品説明文を作成してください。

【商品情報】
- 管理番号: ${kanriNo}
- 種別: ${templateType}
- アイテム名: ${itemName}
- メーカー/ブランド: ${maker}
- 品番/型式: ${modelNo}
- 状態: ${condition}
- 内部KW: ${kw}

【送料情報】
${shippingTable}

【作成ルール】
1. 冒頭に内部KW「${kw}」を記載
2. 《商品説明》《サイズ詳細》《配送料》の3セクション構成
3. ${templateType}に合ったトーンで書く（ビンテージ：味わい・インテリア性を強調 / 現行品：スペック・状態を中心）
4. サイズは「未計測」と記載（後で担当者が入力）
5. 配送料セクションには以下の送料テーブルをそのまま挿入:
${shippingTable}
6. 末尾に以下の免責文をそのまま追加:
${DISCLAIMER}
7. 説明文のみを出力（前置き不要）`;

  try {
    const response = callClaudeAPI(prompt);
    const descCol = headers.indexOf('説明文') + 1;
    sh.getRange(row, descCol).setValue(response);
    ui.alert(`説明文を生成しました（行${row}: ${itemName}）`);
  } catch (e) {
    ui.alert('エラー: ' + e.message);
  }
}

/**
 * Claude API呼び出し
 */
function callClaudeAPI(prompt) {
  const apiKey = CONFIG.ANTHROPIC_API_KEY;
  if (!apiKey) throw new Error('ANTHROPIC_API_KEYがスクリプトプロパティに設定されていません。');

  const payload = {
    model: 'claude-opus-4-6',
    max_tokens: 2000,
    messages: [{ role: 'user', content: prompt }],
  };

  const options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
    },
    payload: JSON.stringify(payload),
  };

  const res = UrlFetchApp.fetch('https://api.anthropic.com/v1/messages', options);
  const json = JSON.parse(res.getContentText());
  return json.content[0].text;
}

// ============================================================
// 出品タイトル生成
// ============================================================
/**
 * 担当者コード(KH/YY等)またはSlack UserIDからタイトルマークを取得
 * 設定マスタシートを優先参照、なければLISTING_MARKSフォールバック
 */
function getListingMark(staffId) {
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sh = ss.getSheetByName(SH.SETTINGS);
    const data = sh.getDataRange().getValues();
    const markSection = data.findIndex(r => String(r[0]).includes('出品担当マーク'));
    if (markSection >= 0) {
      for (let i = markSection + 1; i < data.length; i++) {
        if (String(data[i][0]).startsWith('===')) break;
        if (String(data[i][0]) === staffId) return String(data[i][1]);
      }
    }
  } catch (e) { /* フォールバックへ */ }

  // フォールバック: LISTING_MARKS定数
  return LISTING_MARKS[staffId] ? LISTING_MARKS[staffId][0] : '';
}

function generateTitle() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(SH.MAIN);
  const ui = SpreadsheetApp.getUi();
  const row = sh.getActiveRange().getRow();
  if (row <= 1) { ui.alert('行を選択してください。'); return; }

  const headers = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  const values  = sh.getRange(row, 1, 1, sh.getLastColumn()).getValues()[0];
  const get = (name) => values[headers.indexOf(name)] || '';

  const category  = get('アカウント区分');
  const itemName  = get('アイテム名');
  const maker     = get('メーカー/ブランド');
  const modelNo   = get('品番/型式');
  const condition = get('状態');
  const staffId   = get('担当者');
  const mark      = getListingMark(staffId);
  const templateType = category === 'V' ? 'ビンテージ・アンティーク' : (category === 'M' ? 'まとめ売り' : '現行品・中古');

  // マーク＋半角スペース分（2文字）を除いた文字数制限
  const prefix = mark ? mark + ' ' : '';
  const maxLen = 65 - prefix.length;

  const prompt = `ヤフオクの出品タイトル（本文部分）と末尾検索タグを作成してください。

【商品情報】
種別: ${templateType}
アイテム名: ${itemName}
メーカー: ${maker}
品番: ${modelNo}
状態: ${condition}

【ルール】
1. 本文＋検索タグ合わせて${maxLen}文字以内（識別マークは含めない）
2. 本文は商品名・メーカー・品番・状態を含め検索されやすく
3. 検索タグは「 /キーワード」形式でタイトル末尾に付ける（例: /純正 /NISSAN /ヘッドライト）
4. 検索タグは商品に関連する検索ワード3〜6個
5. タイトル本文＋タグのみ出力（前置き・説明不要）`;

  try {
    const baseTitle = callClaudeAPI(prompt).trim().slice(0, maxLen);
    const fullTitle = prefix + baseTitle;
    const titleCol = headers.indexOf('出品タイトル(65文字以内)') + 1;
    sh.getRange(row, titleCol).setValue(fullTitle);
    ui.alert(`タイトル生成完了:\n${fullTitle}\n（${fullTitle.length}文字）`);
  } catch (e) {
    ui.alert('エラー: ' + e.message);
  }
}

// ============================================================
// 新規行追加（分荷DBから管理番号で引用）
// ============================================================
function addItemFromDB() {
  const ui = SpreadsheetApp.getUi();
  const response = ui.prompt('管理番号を入力してください', ui.ButtonSet.OK_CANCEL);
  if (response.getSelectedButton() !== ui.Button.OK) return;

  const kanriNo = response.getResponseText().trim();
  if (!kanriNo) return;

  // スプレッドシート①（分荷判定DB）から検索
  const dbSS = SpreadsheetApp.openById(CONFIG.DB_SPREADSHEET_ID);
  const dbSh = dbSS.getSheets()[0];
  const dbData = dbSh.getDataRange().getValues();
  const dbHeaders = dbData[0];
  const dbRow = dbData.find(r => String(r[dbHeaders.indexOf('管理番号')]) === kanriNo);

  if (!dbRow) {
    ui.alert(`管理番号 ${kanriNo} は分荷DBに見つかりませんでした。`);
    return;
  }

  const getDB = (name) => dbRow[dbHeaders.indexOf(name)] || '';
  const kw = getDB('推定内部KW');
  const carrier = extractCarrierFromKW(kw);
  const size = extractSizeFromKW(kw);

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(SH.MAIN);

  // 送料を一括計算（佐川のみ自動）
  let shippingRates = Array(11).fill('');
  if (carrier === 'S' && size) {
    const row = SAGAWA_RATES.find(r => r[0] >= size);
    if (row) {
      shippingRates = [row[12], row[10], row[9], row[8], row[5], row[2], row[1], row[6], row[7], row[8], row[9]];
    }
  }

  const newRow = [
    kanriNo,
    kanriNo.replace(/\d{4}/, '').charAt(0), // V/G/M抽出
    getDB('アイテム名'),
    getDB('メーカー/ブランド'),
    getDB('品番/型式'),
    getDB('状態'),
    kw,
    getDB('担当者SlackID'),
    getDB('日時'),
    // 発送情報
    carrier, size, '',
    // 出品情報（空欄・後で生成）
    '', '', getDB('予想販売価格'), '',
    // 画像（空欄）
    '', '', '', '', '', '',
    // オークション設定（固定）
    CONFIG.AUCTION_DURATION_DAYS,
    CONFIG.AUCTION_END_HOUR + ':00',
    CONFIG.AUTO_RELIST,
    // ステータス
    '未出品', '', '', '', '', '',
    // 送料
    ...shippingRates,
    '要問合せ', // 沖縄
  ];

  sh.appendRow(newRow);
  ui.alert(`管理番号 ${kanriNo} を出品管理に追加しました。`);
}

// ============================================================
// ヤフオク出品用CSV出力
// ============================================================

/**
 * 選択した行（複数可）をヤフオクCSV形式でDriveに保存
 */
function exportYahooCSV() {
  const ui = SpreadsheetApp.getUi();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(SH.MAIN);

  const allHeaders = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  const allData = sh.getDataRange().getValues();
  const rows = allData.slice(1).filter(r => r[allHeaders.indexOf('出品ステータス')] === '未出品');

  if (rows.length === 0) {
    ui.alert('出品ステータスが「未出品」の商品がありません。');
    return;
  }

  const get = (row, name) => row[allHeaders.indexOf(name)] || '';

  // ヤフオクCSV列順（公式フォーマット準拠）
  const csvHeaders = [
    'タイトル', '商品説明', 'カテゴリ番号', '開始価格',
    '数量', '出品期間', '自動再出品', '送料負担',
    '送料', '都道府県', '発送までの日数',
    '返品', '商品の状態',
    '画像1', '画像2', '画像3', '画像4', '画像5',
  ];

  const csvRows = rows.map(row => [
    get(row, '出品タイトル(65文字以内)'),
    get(row, '説明文'),
    get(row, 'カテゴリID'),
    get(row, '開始価格') || 1,
    1,
    CONFIG.AUCTION_DURATION_DAYS,
    CONFIG.AUTO_RELIST,
    '落札者',  // 送料負担
    '',        // 個別設定のため空
    CONFIG.SHIPPING_FROM,
    7,         // 発送までの日数
    '不可',
    '中古',
    get(row, '画像1URL'),
    get(row, '画像2URL'),
    get(row, '画像3URL'),
    get(row, '画像4URL'),
    get(row, '画像5URL'),
  ]);

  const csvContent = [csvHeaders, ...csvRows]
    .map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(','))
    .join('\n');

  // BOM付きUTF-8でDriveに保存
  const bom = '\uFEFF';
  const fileName = `ヤフオク出品_${Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd_HHmm')}.csv`;
  const blob = Utilities.newBlob(bom + csvContent, 'text/csv', fileName);
  const folder = DriveApp.getRootFolder();
  const file = folder.createFile(blob);

  ui.alert(`CSV出力完了: ${fileName}\nURL: ${file.getUrl()}\n\n対象: ${rows.length}件`);
}

// ============================================================
// カスタムメニュー
// ============================================================
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('📦 出品管理')
    .addItem('① DBから商品追加', 'addItemFromDB')
    .addSeparator()
    .addItem('② タイトル生成（AI）', 'generateTitle')
    .addItem('③ 説明文生成（AI）', 'generateDescription')
    .addSeparator()
    .addItem('④ ヤフオクCSV出力（未出品のみ）', 'exportYahooCSV')
    .addSeparator()
    .addItem('⚙️ スプレッドシート初期化', 'setupSpreadsheet')
    .addToUi();
}
