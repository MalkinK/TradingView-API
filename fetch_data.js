/**
 * fetch_data.js — Fetch OHLCV data from TradingView for key futures symbols.
 *
 * Connects via WebSocket, fetches daily (100 candles) and 1h (50 candles)
 * for each symbol, saves to ~/shared/tv_cache/<symbol>_<timeframe>.json.
 *
 * Usage: node fetch_data.js
 * Requires: SESSION and SIGNATURE in .env
 *
 * Author: Claude Code
 * Created: 2026-03-21
 */

require('dotenv').config();
const TradingView = require('@mathieuc/tradingview');
const fs = require('fs');
const path = require('path');

// ── Config ──────────────────────────────────────────────────
const CACHE_DIR = path.join(process.env.HOME, 'shared', 'tv_cache');
const TIMEOUT_MS = 30000; // 30s per symbol/timeframe fetch

const SYMBOLS = [
  { tv: 'NYMEX:CL1!', file: 'cl', name: 'Crude Oil' },
  { tv: 'CME_MINI:ES1!', file: 'es', name: 'E-mini S&P 500' },
  { tv: 'COMEX:GC1!', file: 'gc', name: 'Gold' },
  { tv: 'COMEX:SI1!', file: 'si', name: 'Silver' },
  { tv: 'CME_MINI:NQ1!', file: 'nq', name: 'E-mini Nasdaq' },
  { tv: 'CBOE:VIX', file: 'vix', name: 'VIX' },
];

const TIMEFRAMES = [
  { tf: 'D', candles: 100, suffix: 'daily' },
  { tf: '60', candles: 50, suffix: '1h' },
];

// ── Validation ──────────────────────────────────────────────
if (!process.env.SESSION || !process.env.SIGNATURE) {
  console.error('ERROR: Set SESSION and SIGNATURE in .env file');
  console.error('See: CC_TRADINGVIEW_API_SETUP.md Step 2');
  process.exit(1);
}

fs.mkdirSync(CACHE_DIR, { recursive: true });

// ── Fetch one symbol + timeframe ────────────────────────────
function fetchOHLCV(client, symbol, timeframe, numCandles) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chart.delete();
      reject(new Error(`Timeout fetching ${symbol.tv} ${timeframe}`));
    }, TIMEOUT_MS);

    const chart = new client.Session.Chart();

    chart.onError((...err) => {
      clearTimeout(timer);
      chart.delete();
      reject(new Error(`Chart error ${symbol.tv}: ${err.join(' ')}`));
    });

    chart.setMarket(symbol.tv, {
      timeframe: timeframe,
      range: numCandles,
    });

    chart.onUpdate(() => {
      clearTimeout(timer);
      try {
        if (!chart.periods || chart.periods.length === 0) {
          chart.delete();
          reject(new Error(`No data for ${symbol.tv} ${timeframe}`));
          return;
        }

        const candles = chart.periods.map(p => ({
          time: p.time,
          open: p.open,
          high: p.max,
          low: p.min,
          close: p.close,
          volume: p.volume,
        }));

        chart.delete();
        resolve(candles);
      } catch (e) {
        chart.delete();
        reject(e);
      }
    });
  });
}

// ── Main ────────────────────────────────────────────────────
(async () => {
  console.log(`TradingView Data Fetcher — ${new Date().toISOString()}`);
  console.log(`Symbols: ${SYMBOLS.length}, Timeframes: ${TIMEFRAMES.length}`);
  console.log('');

  const client = new TradingView.Client({
    token: process.env.SESSION,
    signature: process.env.SIGNATURE,
  });

  let successCount = 0;
  let errorCount = 0;

  for (const symbol of SYMBOLS) {
    for (const { tf, candles, suffix } of TIMEFRAMES) {
      const label = `${symbol.tv} (${suffix})`;
      try {
        process.stdout.write(`  Fetching ${label}...`);
        const data = await fetchOHLCV(client, symbol, tf, candles);

        const output = {
          symbol: symbol.tv,
          name: symbol.name,
          timeframe: tf === 'D' ? 'D' : `${tf}min`,
          fetched_at: new Date().toISOString(),
          candle_count: data.length,
          candles: data,
        };

        const filePath = path.join(CACHE_DIR, `${symbol.file}_${suffix}.json`);
        fs.writeFileSync(filePath, JSON.stringify(output, null, 2));
        console.log(` ${data.length} candles → ${symbol.file}_${suffix}.json`);
        successCount++;
      } catch (err) {
        console.log(` ERROR: ${err.message}`);
        errorCount++;
      }
    }
  }

  client.end();

  console.log('');
  console.log(`Done: ${successCount} files saved, ${errorCount} errors`);
  console.log(`Cache: ${CACHE_DIR}`);
  process.exit(errorCount > 0 ? 1 : 0);
})();
