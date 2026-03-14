// ============================================================
// master_db.gs - マスターデータ管理 + WebApp GETエンドポイント
// 同じGASプロジェクトに追加して使用する
// ============================================================

const MASTER_SHEETS = {
  STAFF:       'スタッフマスター',
  HOURLY_RATE: 'アワーレートマスター',
  WAREHOUSE:   '倉庫コストマスター',
};

// ============================================================
// WebApp: GETエンドポイント（app.pyからマスターデータ取得用）
// デプロイ後のURLを app.py の MASTER_GAS_URL に設定する
// ============================================================
function doGet(e) {
  const type = (e && e.parameter && e.parameter.type) ? e.parameter.type : '';
  try {
    let data;
    switch (type) {
      case 'staff':       data = _getStaffMaster();      break;
      case 'hourly_rate': data = _getHourlyRateMaster(); break;
      case 'warehouse':   data = _getWarehouseMaster();  break;
      default:
        return _jsonRes({ ok: false, error: 'type must be: staff / hourly_rate / warehouse' });
    }
    return _jsonRes({ ok: true, type: type, data: data });
  } catch (err) {
    return _jsonRes({ ok: false, error: err.toString() });
  }
}

function _jsonRes(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ============================================================
// マスターデータ取得（内部関数）
// ============================================================
function _getMasterRows(sheetName) {
  const ss = SpreadsheetApp.openById(CONFIG.DB_SPREADSHEET_ID);
  const sh = ss.getSheetByName(sheetName);
  if (!sh) throw new Error(sheetName + ' シートが見つかりません');
  const rows = sh.getDataRange().getValues();
  const headers = rows[0];
  return rows.slice(1)
    .filter(r => r[0] !== '' && r[0] !== null)
    .map(r => {
      const obj = {};
      headers.forEach((h, i) => { if (h) obj[String(h)] = r[i]; });
      return obj;
    });
}

function _getStaffMaster()      { return _getMasterRows(MASTER_SHEETS.STAFF); }
function _getHourlyRateMaster() { return _getMasterRows(MASTER_SHEETS.HOURLY_RATE); }
function _getWarehouseMaster()  { return _getMasterRows(MASTER_SHEETS.WAREHOUSE); }

// ============================================================
// 初期化（初回のみ実行 → スプレッドシートメニューから呼び出す）
// ============================================================
function initMasterSheets() {
  const ss = SpreadsheetApp.openById(CONFIG.DB_SPREADSHEET_ID);
  _initStaffSheet(ss);
  _initHourlyRateSheet(ss);
  _initWarehouseSheet(ss);
  SpreadsheetApp.getUi().alert('マスターシート初期化完了！\n各シートにデータを入力してください。');
}

// ------ スタッフマスター ------
function _initStaffSheet(ss) {
  let sh = ss.getSheetByName(MASTER_SHEETS.STAFF);
  if (!sh) sh = ss.insertSheet(MASTER_SHEETS.STAFF);
  sh.clearContents();

  const headers = ['名前', 'SlackユーザーID', '時給（円）', '雇用区分', '査定係数', '標準休憩時間（分）', '備考'];
  const sample = [
    ['浅野儀頼', '', 0, '正社員', 1.0, 60, 'SlackID確定後に入力'],
    ['林和人',   '', 0, '正社員', 1.0, 60, 'SlackID確定後に入力'],
    ['横山優',   '', 0, 'パート',  0.9, 60, 'SlackID確定後に入力'],
    ['平野光雄', '', 0, 'パート',  0.9, 60, 'SlackID確定後に入力'],
    ['桃井',     '', 0, 'パート',  0.9,  0, '短時間勤務のため休憩なし'],
    ['北瀬',     '', 0, 'パート',  0.9, 60, 'SlackID確定後に入力'],
    ['伊藤',     '', 0, 'パート',  0.9,  0, '9:00-15:00 木曜休み・短時間のため休憩なし'],
  ];

  sh.getRange(1, 1, 1, headers.length).setValues([headers])
    .setBackground('#1a73e8').setFontColor('white').setFontWeight('bold');
  sh.getRange(2, 1, sample.length, headers.length).setValues(sample);
  sh.setColumnWidths(1, headers.length, 130);
  sh.setFrozenRows(1);
  sh.getRange('A1').setNote(
    '査定係数: 同じ件数・時間でも職位によって評価を重み付けする係数\n例: 正社員=1.0 / パート=0.9'
  );
}

// ------ アワーレートマスター ------
function _initHourlyRateSheet(ss) {
  let sh = ss.getSheetByName(MASTER_SHEETS.HOURLY_RATE);
  if (!sh) sh = ss.insertSheet(MASTER_SHEETS.HOURLY_RATE);
  sh.clearContents();

  const headers = ['作業分類', 'アワーレート（円/h）', '適用開始日', '備考'];
  const data = [
    ['分荷作業',   1500, '2026/03/01', '査定・判断業務'],
    ['撮影作業',   1200, '2026/03/01', '単純作業'],
    ['リスト作成', 1500, '2026/03/01', 'タイトル・説明文作成'],
    ['出品作業',   1500, '2026/03/01', 'プラットフォームへの登録'],
    ['梱包作業',   1200, '2026/03/01', ''],
    ['出荷作業',   1200, '2026/03/01', ''],
    ['状態確認',   1200, '2026/03/01', ''],
    ['問合せ対応', 1500, '2026/03/01', ''],
    ['その他',     1200, '2026/03/01', ''],
  ];

  sh.getRange(1, 1, 1, headers.length).setValues([headers])
    .setBackground('#0f9d58').setFontColor('white').setFontWeight('bold');
  sh.getRange(2, 1, data.length, headers.length).setValues(data);
  sh.setColumnWidths(1, headers.length, 160);
  sh.setFrozenRows(1);
  sh.getRange('A1').setNote(
    'アワーレート = 月間固定費（賃料・光熱費・設備費等）÷ 月間総稼働時間\n' +
    '例: 固定費30万円 ÷ 稼働200h = 1,500円/h\n' +
    '自動車整備工場の「工賃レート」に相当する会社の標準コスト単価'
  );
}

// ------ 倉庫コストマスター ------
function _initWarehouseSheet(ss) {
  let sh = ss.getSheetByName(MASTER_SHEETS.WAREHOUSE);
  if (!sh) sh = ss.insertSheet(MASTER_SHEETS.WAREHOUSE);
  sh.clearContents();

  const headers = ['倉庫名', '月額賃料（円）', '面積（m²）', '日額/m²（自動計算）', '備考'];
  const data = [
    ['メイン倉庫', 0, 0, '', '月額賃料と面積を入力すると日額が自動計算されます'],
  ];

  sh.getRange(1, 1, 1, headers.length).setValues([headers])
    .setBackground('#db4437').setFontColor('white').setFontWeight('bold');
  sh.getRange(2, 1, 1, headers.length).setValues(data);
  // D2に自動計算式
  sh.getRange('D2').setFormula('=IF(AND(B2>0,C2>0), B2/C2/30, "")');
  sh.getRange('D2').setNumberFormat('¥#,##0.00');
  sh.setColumnWidths(1, headers.length, 160);
  sh.setFrozenRows(1);
  sh.getRange('D1').setNote('= 月額賃料 ÷ 面積 ÷ 30日 で自動計算\n商品サイズ × この日額 × 予測在庫日数 = 保管原価');
}

// ============================================================
// カスタムメニューに追加（onOpen関数が既にある場合は手動で追記）
// ============================================================
// ※ 既存の onOpen() に以下を追加してください:
// .addItem('⚙️ マスターシート初期化', 'initMasterSheets')
