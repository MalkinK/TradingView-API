#!/usr/bin/env node
/**
 * fetch_symbol.js — гибкий OHLCV-пуллер: ЛЮБОЙ символ, ЛЮБОЙ таймфрейм, полная история.
 * Обёртка над уже установленной @mathieuc/tradingview (WebSocket, TV Ultimate токен).
 * Не хардкодит watchlist (в отличие от fetch_data.js) — символ/ТФ/кол-во приходят аргументами.
 *
 * Usage: node fetch_symbol.js <EXCH:TICKER> [timeframe] [count] [--csv] [--bars]
 *   EXCH:TICKER  напр. NASDAQ:RCAT
 *   timeframe    1 5 15 30 60 120 240 D W M   (по умолч. D)
 *   count        сколько баров (по умолч. 300)
 *   --csv        сохранить полный CSV в ~/shared/trading/csv/<TICKER>_<TF>_<date>.csv
 *   --bars       вывести ВСЕ бары в JSON (по умолч. только сводка + последние 30)
 * Вывод: JSON-сводка (range, first, last, ATH/ATL, recent[30]) в stdout.
 *
 * Author: Пчёлка (Claude Code) 2026-07-02 — под задачу «полная история/любой ТФ» для Shadow Grab.
 * Requires: SESSION + SIGNATURE в .env этого каталога.
 */
require('dotenv').config({ path: require('path').join(__dirname, '.env') });
const TradingView = require('@mathieuc/tradingview');
const fs = require('fs');
const path = require('path');

const argv = process.argv.slice(2);
const flags = argv.filter(a => a.startsWith('--'));
const pos = argv.filter(a => !a.startsWith('--'));
const symbol = pos[0];
const tf = pos[1] || 'D';
const count = parseInt(pos[2], 10) || 300;
const wantCsv = flags.includes('--csv');
const wantBars = flags.includes('--bars');
const TIMEOUT_MS = 30000;

if (!symbol) {
  console.error('Usage: node fetch_symbol.js <EXCH:TICKER> [tf D/W/M/60...] [count] [--csv] [--bars]');
  process.exit(1);
}
if (!process.env.SESSION || !process.env.SIGNATURE) {
  console.error('ERROR: SESSION/SIGNATURE не заданы в .env'); process.exit(1);
}

const client = new TradingView.Client({ token: process.env.SESSION, signature: process.env.SIGNATURE });

function fetchOHLCV() {
  return new Promise((resolve, reject) => {
    const chart = new client.Session.Chart();
    const timer = setTimeout(() => { chart.delete(); reject(new Error('timeout 30s')); }, TIMEOUT_MS);
    chart.onError((...e) => { clearTimeout(timer); chart.delete(); reject(new Error('chart error: ' + e.join(' '))); });
    chart.setMarket(symbol, { timeframe: tf, range: count });
    chart.onUpdate(() => {
      if (!chart.periods || !chart.periods.length) return; // ждём непустой апдейт
      clearTimeout(timer);
      const bars = chart.periods
        .map(p => ({ time: p.time, open: p.open, high: p.max, low: p.min, close: p.close, volume: p.volume }))
        .sort((a, b) => a.time - b.time);
      chart.delete();
      resolve(bars);
    });
  });
}

const d = t => new Date(t * 1000).toISOString().slice(0, 10);

(async () => {
  try {
    const bars = await fetchOHLCV();
    client.end();
    if (!bars.length) { console.error('НЕТ данных'); process.exit(1); }
    const first = bars[0], last = bars[bars.length - 1];
    let ath = bars[0], atl = bars[0];
    for (const b of bars) { if (b.high > ath.high) ath = b; if (b.low < atl.low) atl = b; }

    let csvPath = null;
    if (wantCsv) {
      const dir = path.join(process.env.HOME, 'shared', 'trading', 'csv');
      fs.mkdirSync(dir, { recursive: true });
      const tick = symbol.replace(/[^A-Za-z0-9]/g, '_');
      csvPath = path.join(dir, `${tick}_${tf}_${d(last.time)}.csv`);
      const rows = ['time,date,open,high,low,close,volume',
        ...bars.map(b => `${b.time},${d(b.time)},${b.open},${b.high},${b.low},${b.close},${b.volume}`)];
      fs.writeFileSync(csvPath, rows.join('\n'));
    }

    const out = {
      symbol, timeframe: tf, count: bars.length,
      range: `${d(first.time)} → ${d(last.time)}`,
      first: { date: d(first.time), o: first.open, h: first.high, l: first.low, c: first.close, v: first.volume },
      last:  { date: d(last.time),  o: last.open,  h: last.high,  l: last.low,  c: last.close,  v: last.volume },
      ath: { price: ath.high, date: d(ath.time) },
      atl: { price: atl.low,  date: d(atl.time) },
      csv: csvPath,
      recent30: bars.slice(-30).map(b => ({ date: d(b.time), o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume })),
    };
    if (wantBars) out.bars = bars;
    console.log(JSON.stringify(out, null, 1));
    process.exit(0);
  } catch (e) {
    console.error('FETCH ERROR:', e.message); try { client.end(); } catch (_) {} process.exit(1);
  }
})();
