// ============================================================
// webhook.gs - 分荷判定DB に置くスクリプト
//
// 【役割】
//   app.py からの POST を受信し、
//   ・ログ系シート（分荷確定ログ・古物台帳・出退勤など）→ 分荷判定DB に書き込む
//   ・出品管理シート → 当月の出品管理スプレッドシートに書き込む
//
// 【デプロイ手順（初回のみ）】
//   1. 分荷判定DB を開く → 拡張機能 → Apps Script
//   2. このファイルを追加して保存
//   3. デプロイ → 新しいデプロイ → 種類: ウェブアプリ
//      実行ユーザー: 自分 / アクセス: 全員
//   4. 発行された URL を app.py の GAS_URL に設定
//   5. GASエディタで setCurrentListingSheet("スプレッドシートID") を実行して
//      当月の出品管理スプレッドシートを登録する
//
// 【月次切り替え手順】
//   新しい出品管理スプレッドシートを作成したら
//   GASエディタのコンソールで以下を実行する:
//     setCurrentListingSheet("新しいスプレッドシートID")
// ============================================================

// 分荷判定DB スプレッドシートID
const DB_SS_ID = '1CWG9MVrsw9gJwp31lCrUs9KB0a1zptZY1cPO47ZNmVU';

// Claude Code 作業ログ スプレッドシートID
const CLAUDE_LOG_SS_ID = '1-jspSk-pi9Epm0Z5GoyppVfCw8mXSLhJ3B-yBPoRy8U';

// 分荷判定DB のシート名
const DB_SH = {
  BUNIKA_LOG:  '分荷確定ログ',
  WORK_LOG:    '作業ログ',
  ATTENDANCE:  '出退勤記録',
  KINTAI:      '勤怠連絡',
  KOBUTSU:     '古物台帳',
  GENBA_SATEI: '現場査定記録',
  GENBA_MEMO:  '現場メモ',
};

// 出品管理スプレッドシートのメインシート名
const LISTING_MAIN_SH = '出品管理';

// オークション固定設定（yahooauction_sheet.gs の CONFIG と合わせる）
const WH_AUCTION = {
  DURATION_DAYS: 4,
  END_HOUR:      22,
  AUTO_RELIST:   3,
};

// 通販チャンネル → アカウント区分
const TSUHAN_MAP = {
  'eBayシングル':          'E',
  'eBayまとめ':            'E',
  'ヤフオクヴィンテージ':   'V',
  'ヤフオク現行':           'G',
  'ヤフオクまとめ':         'M',
};

// ============================================================
// メインエントリポイント
// ============================================================
function doPost(e) {
  try {
    const raw     = (e && e.postData) ? e.postData.contents : '{}';
    const payload = JSON.parse(raw);
    const action  = String(payload.action || '').trim();
    const isCancel = !action && String(payload.kakutei_channel || '').startsWith('キャンセル');

    let result;
    if (isCancel) {
      result = _handleCancel(payload);
    } else if (!action) {
      result = _handleBunikaKakutei(payload);
    } else {
      switch (action) {
        case 'checklist_update':  result = _handleChecklistUpdate(payload); break;
        case 'satsuei_update':    result = _handleSatsueiUpdate(payload);   break;
        case 'shuppinon_listing': result = _handleShuppinon(payload);       break;
        case 'shipping_update':   result = _handleShippingUpdate(payload);  break;
        case 'work_activity':     result = _handleWorkActivity(payload);    break;
        case 'attendance':        result = _handleAttendance(payload);      break;
        case 'kintai_renraku':    result = _handleKintai(payload);          break;
        case 'kobutsu_daichou':   result = _handleKobutsu(payload);         break;
        case 'genba_satei':       result = _handleGenbaSatei(payload);      break;
        case 'genba_memo':        result = _handleGenbaMemo(payload);       break;
        case 'claude_session_log': result = _handleClaudeSessionLog(payload); break;
        default:                  result = { ok: true, skipped: action };   break;
      }
    }

    return ContentService
      .createTextOutput(JSON.stringify(result || { ok: true }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    console.error('[doPost error]', err.toString());
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

// ============================================================
// 月次スプレッドシート切り替え（毎月1回、GASエディタから実行）
// ============================================================

/**
 * 当月の出品管理スプレッドシートIDを登録する
 * 例: setCurrentListingSheet("1bEhdEnjLoRVghd0mHwAmazW4Q2PUMMcVzhGB6ixCAyY")
 */
function setCurrentListingSheet(id) {
  PropertiesService.getScriptProperties().setProperty('LISTING_SS_ID', id);
  console.log('出品管理スプレッドシートを設定しました: ' + id);
}

/** 現在設定されているIDを確認する */
function getCurrentListingSheet() {
  const id = PropertiesService.getScriptProperties().getProperty('LISTING_SS_ID');
  console.log('現在の出品管理ID: ' + (id || '未設定'));
  return id;
}

// ============================================================
// ヘルパー: スプレッドシート取得
// ============================================================

/** 分荷判定DB（ログ系シート） */
function _getSS() {
  return SpreadsheetApp.openById(DB_SS_ID);
}

/** 当月の出品管理スプレッドシート */
function _getListingSS() {
  const id = PropertiesService.getScriptProperties().getProperty('LISTING_SS_ID');
  if (!id) throw new Error(
    '出品管理スプレッドシートが未設定です。\n' +
    'GASエディタで setCurrentListingSheet("スプレッドシートID") を実行してください。'
  );
  return SpreadsheetApp.openById(id);
}

/** 分荷判定DB のシートを取得。なければ作成する */
function _getDbSheetOrCreate(name) {
  const ss = _getSS();
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

/** 管理番号で出品管理シートの行番号（1-indexed）を返す。見つからなければ -1 */
function _findRowByKanri(sh, kanriNo) {
  if (!kanriNo) return -1;
  const data     = sh.getDataRange().getValues();
  const kanriCol = data[0].indexOf('管理番号');
  if (kanriCol < 0) return -1;
  for (let i = 1; i < data.length; i++) {
    if (String(data[i][kanriCol]) === String(kanriNo)) return i + 1;
  }
  return -1;
}

/** 指定行・列名のセルを更新する */
function _updateListingCell(kanriNo, colName, value) {
  const sh  = _getListingSS().getSheetByName(LISTING_MAIN_SH);
  if (!sh) return;
  const row = _findRowByKanri(sh, kanriNo);
  if (row < 0) return;
  const headers = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  const col     = headers.indexOf(colName);
  if (col >= 0) sh.getRange(row, col + 1).setValue(value);
}

/** DBシートにヘッダーがなければ作成する */
function _ensureDbHeader(sh, headers, bgColor) {
  if (sh.getLastRow() === 0) {
    sh.appendRow(headers);
    sh.getRange(1, 1, 1, headers.length)
      .setBackground(bgColor || '#1a3a2a')
      .setFontColor('#ffffff')
      .setFontWeight('bold');
    sh.setFrozenRows(1);
  }
}

// ============================================================
// ① 分荷確定（action なし）
// ============================================================
function _handleBunikaKakutei(payload) {
  // 全件 → 分荷確定ログ（DB）
  _appendBunikaLog(payload);

  // 通販チャンネルのみ → 出品管理シート（月次SS）に新規行を追加
  const kakuteiCh   = String(payload.kakutei_channel || '');
  const kanriNo     = String(payload.kanri_bango || '');
  const accountType = TSUHAN_MAP[kakuteiCh];

  if (!accountType || !kanriNo) {
    return { ok: true, msg: '非通販 or 管理番号なし。分荷確定ログのみ記録。' };
  }

  const ss      = _getListingSS();
  const sh      = ss.getSheetByName(LISTING_MAIN_SH);
  if (!sh) return { ok: false, error: `「${LISTING_MAIN_SH}」シートが出品管理スプレッドシートに見つかりません。setupSpreadsheet() を実行してください。` };

  const headers = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  const kw      = String(payload.internal_keyword || '');

  const cellMap = {
    '管理番号':                   kanriNo,
    'アカウント区分':              accountType,
    'アイテム名':                  String(payload.item_name    || ''),
    'メーカー/ブランド':           String(payload.maker        || ''),
    '品番/型式':                   String(payload.model_number || ''),
    '状態':                        String(payload.condition    || ''),
    '内部KW':                      kw,
    '担当者':                      String(payload.staff_id     || ''),
    '分荷確定日時':                String(payload.timestamp    || new Date().toLocaleString('ja-JP')),
    '発送会社':                    '',   // yahooauction_sheet.gs の addItemFromDB or 手動で補完
    '発送サイズ':                  '',
    '発送重量目安(kg)':            '',
    '保管ロケーション':             '',
    '出品タイトル(65文字以内)':    '',
    'カテゴリID':                  '',
    '開始価格':                    String(payload.start_price  || ''),
    '説明文':                      '',
    '画像フォルダURL':             '',
    '画像1URL': '', '画像2URL': '', '画像3URL': '', '画像4URL': '', '画像5URL': '',
    '出品期間(日)':                WH_AUCTION.DURATION_DAYS,
    '終了時刻':                    WH_AUCTION.END_HOUR + ':00',
    '自動再出品回数':               WH_AUCTION.AUTO_RELIST,
    '出品ステータス':               '未出品',
    '出品日時': '', '終了予定日時': '', '落札価格': '', '落札者ID': '', '在庫日数(分荷〜落札)': '',
    '送料_北海道': '', '送料_東北': '', '送料_関東': '', '送料_信越': '', '送料_北陸': '',
    '送料_東海':  '', '送料_関西': '', '送料_中国': '', '送料_四国': '',
    '送料_北九州': '', '送料_南九州': '',
    '沖縄': '要問合せ',
  };

  sh.appendRow(headers.map(h => (cellMap[h] !== undefined ? cellMap[h] : '')));
  return { ok: true, msg: `出品管理に追加: ${kanriNo}` };
}

/** 全確定を分荷確定ログ（DB）に記録 */
function _appendBunikaLog(payload) {
  const sh = _getDbSheetOrCreate(DB_SH.BUNIKA_LOG);
  _ensureDbHeader(sh, [
    '管理番号', '確定チャンネル', 'AI第一候補', 'AI第二候補',
    'アイテム名', 'メーカー', '品番/型式', '状態',
    '予想販売価格', 'スタート価格', '目標価格', '予測在庫期間', '推奨在庫期限',
    '総合スコア', '保管コスト', '梱包コスト', '期待ROI(%)', '内部KW',
    '担当者', '作業時間(分)', '確定日時',
  ], '#1a3a2a');

  sh.appendRow([
    payload.kanri_bango        || '',
    payload.kakutei_channel    || '',
    payload.first_channel      || '',
    payload.second_channel     || '',
    payload.item_name          || '',
    payload.maker              || '',
    payload.model_number       || '',
    payload.condition          || '',
    payload.predicted_price    || '',
    payload.start_price        || '',
    payload.target_price       || '',
    payload.inventory_period   || '',
    payload.inventory_deadline || '',
    payload.score              || '',
    payload.storage_cost       || '',
    payload.packing_cost       || '',
    payload.expected_roi       || '',
    payload.internal_keyword   || '',
    payload.staff_id           || '',
    payload.sakugyou_jikan     || '',
    payload.timestamp          || new Date().toLocaleString('ja-JP'),
  ]);
}

// ============================================================
// ② キャンセル
// ============================================================
function _handleCancel(payload) {
  _appendBunikaLog(payload);

  const kanriNo = String(payload.kanri_bango || '');
  if (kanriNo && kanriNo !== '---') {
    try {
      _updateListingCell(kanriNo, '出品ステータス', 'キャンセル');
    } catch (err) {
      // 出品管理SSが未設定でもキャンセルログは記録済みなのでエラーは無視
      console.warn('[キャンセル] 出品管理SS更新スキップ:', err.toString());
    }
  }
  return { ok: true, msg: `キャンセル: ${kanriNo}` };
}

// ============================================================
// ③ 動作確認チェックリスト完了
// ============================================================
function _handleChecklistUpdate(payload) {
  const kanriNo = String(payload.kanri_bango || '');
  if (!kanriNo) return { ok: true, skipped: 'no kanri_bango' };
  if (payload.condition) _updateListingCell(kanriNo, '状態', String(payload.condition));
  return { ok: true };
}

// ============================================================
// ④ 撮影完了（Drive 画像フォルダ URL 更新）
// ============================================================
function _handleSatsueiUpdate(payload) {
  const kanriNo = String(payload.kanri_bango || '');
  if (!kanriNo) return { ok: true, skipped: 'no kanri_bango' };
  if (payload.drive_folder_url) _updateListingCell(kanriNo, '画像フォルダURL', String(payload.drive_folder_url));
  _updateListingCell(kanriNo, '出品ステータス', '撮影済み');
  return { ok: true };
}

// ============================================================
// ⑤ 出品保管（タイトル・説明文・ロケーション番号登録）
// ============================================================
function _handleShuppinon(payload) {
  const kanriNo = String(payload.kanri_bango || '');
  if (!kanriNo) return { ok: true, skipped: 'no kanri_bango' };
  if (payload.title)       _updateListingCell(kanriNo, '出品タイトル(65文字以内)', String(payload.title));
  if (payload.description) _updateListingCell(kanriNo, '説明文',                   String(payload.description));
  if (payload.condition)   _updateListingCell(kanriNo, '状態',                     String(payload.condition));
  if (payload.start_price) _updateListingCell(kanriNo, '開始価格',                 String(payload.start_price));
  if (payload.size)        _updateListingCell(kanriNo, '発送サイズ',               String(payload.size));
  if (payload.location)    _updateListingCell(kanriNo, '保管ロケーション',          String(payload.location));
  _updateListingCell(kanriNo, '出品ステータス', '出品中');
  _updateListingCell(kanriNo, '出品日時',       new Date().toLocaleString('ja-JP'));
  return { ok: true };
}

// ============================================================
// ⑥ 出荷完了
// ============================================================
function _handleShippingUpdate(payload) {
  const kanriNo = String(payload.kanri_bango || '');
  if (!kanriNo) return { ok: true, skipped: 'no kanri_bango' };
  if (payload.carrier) _updateListingCell(kanriNo, '発送会社', String(payload.carrier));
  _updateListingCell(kanriNo, '出品ステータス', '出荷済み');
  return { ok: true };
}

// ============================================================
// ⑦ 作業ログ（DB）
// ============================================================
function _handleWorkActivity(payload) {
  const sh = _getDbSheetOrCreate(DB_SH.WORK_LOG);
  _ensureDbHeader(sh, ['日時', 'チャンネル', '管理番号', '担当者', '操作', '経過秒数'], '#334155');
  sh.appendRow([
    new Date().toLocaleString('ja-JP'),
    payload.channel          || '',
    payload.kanri_bango      || '',
    payload.staff_id         || '',
    payload.operation        || '',
    payload.duration_seconds || '',
  ]);
  return { ok: true };
}

// ============================================================
// ⑧ 出退勤（DB）
// ============================================================
function _handleAttendance(payload) {
  const sh = _getDbSheetOrCreate(DB_SH.ATTENDANCE);
  _ensureDbHeader(sh, ['日付', '担当者', '出勤時刻', '退勤時刻', '合計分', '休憩分', '実働時間(h)', '完了件数'], '#1a3a2a');
  sh.appendRow([
    payload.date            || '',
    payload.staff_id        || '',
    payload.start_time      || '',
    payload.end_time        || '',
    payload.total_minutes   || '',
    payload.break_minutes   || '',
    payload.net_hours       || '',
    payload.completed_count || '',
  ]);
  return { ok: true };
}

// ============================================================
// ⑨ 勤怠連絡（DB）
// ============================================================
function _handleKintai(payload) {
  const sh = _getDbSheetOrCreate(DB_SH.KINTAI);
  _ensureDbHeader(sh, ['日時', '担当者', 'メッセージ'], '#334155');
  sh.appendRow([
    new Date().toLocaleString('ja-JP'),
    payload.staff_id || '',
    payload.message  || '',
  ]);
  return { ok: true };
}

// ============================================================
// ⑩ 古物台帳（DB）
// ============================================================
function _handleKobutsu(payload) {
  const sh = _getDbSheetOrCreate(DB_SH.KOBUTSU);
  _ensureDbHeader(sh, ['日時', '品物名', '買取金額(円)', '氏名', '住所', '生年月日', '証明書番号', '確認書類'], '#7f1d1d');
  sh.appendRow([
    payload.timestamp  || new Date().toLocaleString('ja-JP'),
    payload.item_name  || '',
    payload.price      || '',
    payload.name       || '',
    payload.address    || '',
    payload.birthdate  || '',
    payload.id_number  || '',
    payload.doc_type   || '',
  ]);
  return { ok: true };
}

// ============================================================
// ⑪ 現場査定（DB）
// ============================================================
function _handleGenbaSatei(payload) {
  const sh = _getDbSheetOrCreate(DB_SH.GENBA_SATEI);
  _ensureDbHeader(sh, ['日時', '担当者', '入力内容', '査定結果'], '#1e3a5f');
  sh.appendRow([
    new Date().toLocaleString('ja-JP'),
    payload.staff_id || '',
    payload.input    || '',
    payload.result   || '',
  ]);
  return { ok: true };
}

// ============================================================
// ⑬ Claude Code 作業ログ（別SS）
// ============================================================
function _handleClaudeSessionLog(payload) {
  const ss = SpreadsheetApp.openById(CLAUDE_LOG_SS_ID);
  let sh = ss.getSheetByName('作業ログ');
  if (!sh) sh = ss.insertSheet('作業ログ');
  if (sh.getLastRow() === 0) {
    sh.appendRow(['日時', '作業者', '変更ファイル', '変更内容', 'Gitコミット', '備考']);
    sh.getRange(1, 1, 1, 6)
      .setBackground('#1a3a2a')
      .setFontColor('#ffffff')
      .setFontWeight('bold');
    sh.setFrozenRows(1);
    sh.setColumnWidth(1, 150);
    sh.setColumnWidth(3, 280);
    sh.setColumnWidth(4, 400);
  }
  sh.appendRow([
    payload.timestamp    || new Date().toLocaleString('ja-JP'),
    payload.author       || '浅野儀頼',
    payload.files        || '',
    payload.description  || '',
    payload.commit_hash  || '',
    payload.note         || '',
  ]);
  return { ok: true };
}

// ============================================================
// ⑫ 現場メモ（DB）
// ============================================================
function _handleGenbaMemo(payload) {
  const sh = _getDbSheetOrCreate(DB_SH.GENBA_MEMO);
  _ensureDbHeader(sh, ['日時', '担当者', 'メッセージ'], '#1e3a5f');
  sh.appendRow([
    new Date().toLocaleString('ja-JP'),
    payload.staff_id || '',
    payload.message  || '',
  ]);
  return { ok: true };
}
