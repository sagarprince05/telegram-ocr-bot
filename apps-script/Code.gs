/**
 * Expense Dashboard — Google Apps Script web app
 * Reads the bot's Google Sheet and serves an interactive dashboard.
 *
 * Setup: open your Google Sheet -> Extensions -> Apps Script.
 * Create this file (Code.gs) and an HTML file named "Index" (Index.html),
 * paste the contents, Save, then Deploy -> New deployment -> Web app.
 */

// The sheet the bot writes to.
var SHEET_ID = '1TJl89DGIspfcBbBnslM3fc1xZvQFW4y4x4NVNuF7PdQ';
var TAB_NAME = 'Sheet1';

function doGet() {
  // Inject the data into the page at render time so the dashboard does NOT
  // depend on google.script.run (which can hang with multi-account/cookie
  // setups). Reloading the page fetches fresh data.
  var t = HtmlService.createTemplateFromFile('Index');
  t.data = JSON.stringify(getData());
  return t.evaluate()
    .setTitle('Expense Dashboard')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

/**
 * Returns all expense rows as plain objects:
 *   { name, telegramId, category, date (yyyy-MM-dd), amount (number) }
 * Called from the client via google.script.run.
 */
function getData() {
  var ss = SpreadsheetApp.openById(SHEET_ID);
  var sh = ss.getSheetByName(TAB_NAME);
  if (!sh) {
    return { error: 'Tab "' + TAB_NAME + '" not found', rows: [] };
  }
  var tz = ss.getSpreadsheetTimeZone() || 'Asia/Kolkata';
  var values = sh.getDataRange().getValues();
  var rows = [];

  // Row layout: 0 Name | 1 Telegram ID | 2 Category | 3 Date & Time | 4 Amount | 5 Image
  for (var i = 1; i < values.length; i++) {
    var r = values[i];
    var name = r[0];
    var amountRaw = r[4];
    if (!name && (amountRaw === '' || amountRaw === null)) continue; // skip blanks

    rows.push({
      name: String(name || 'Unknown').trim(),
      telegramId: String(r[1] || '').trim(),
      category: String(r[2] || 'Other').trim() || 'Other',
      date: normalizeDate_(r[3], tz),
      amount: toNumber_(amountRaw)
    });
  }
  return { error: null, rows: rows };
}

/** Parse any amount cell into a plain number. */
function toNumber_(v) {
  if (typeof v === 'number') return v;
  var s = String(v || '').replace(/,/g, '').replace(/[^0-9.]/g, '');
  var n = parseFloat(s);
  return isNaN(n) ? 0 : n;
}

/** Return a yyyy-MM-dd string from a Date object or a date-like string. */
function normalizeDate_(cell, tz) {
  if (cell instanceof Date) {
    return Utilities.formatDate(cell, tz, 'yyyy-MM-dd');
  }
  var s = String(cell || '').trim();
  if (!s) return '';
  // Already ISO: 2026-07-16 or 2026-07-16 20:14
  var iso = s.match(/(\d{4})-(\d{2})-(\d{2})/);
  if (iso) return iso[1] + '-' + iso[2] + '-' + iso[3];
  // dd-mm-yyyy or dd/mm/yyyy
  var dmy = s.match(/(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})/);
  if (dmy) {
    var d = ('0' + dmy[1]).slice(-2);
    var m = ('0' + dmy[2]).slice(-2);
    return dmy[3] + '-' + m + '-' + d;
  }
  return '';
}
