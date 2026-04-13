/**
 * fetch_data.js — Fetch OHLCV data from TradingView for futures + portfolio stocks/ETFs.
 *
 * Connects via WebSocket, fetches candles for each symbol, saves to ~/shared/tv_cache/.
 * - Futures (6): daily (500 candles) + 1h (50 candles) — volatile, need intraday
 * - Stocks/ETFs (60): daily (500 candles) only — positions are long-hold
 *
 * Usage: node fetch_data.js
 * Requires: SESSION and SIGNATURE in .env (or environment)
 *
 * Author: Claude Code
 * Created: 2026-03-21
 * Updated: 2026-04-13 — added 60 portfolio symbols (D1)
 */

require('dotenv').config();
const TradingView = require('@mathieuc/tradingview');
const fs = require('fs');
const path = require('path');

// ── Config ──────────────────────────────────────────────────
const CACHE_DIR = path.join(process.env.HOME, 'shared', 'tv_cache');
const TIMEOUT_MS = 30000; // 30s per symbol/timeframe fetch

// ── Futures — daily + 1h (volatile instruments, need intraday) ──
const FUTURES = [
  { tv: 'NYMEX:CL1!',    file: 'cl',  name: 'Crude Oil' },
  { tv: 'CME_MINI:ES1!', file: 'es',  name: 'E-mini S&P 500' },
  { tv: 'COMEX:GC1!',    file: 'gc',  name: 'Gold' },
  { tv: 'COMEX:SI1!',    file: 'si',  name: 'Silver' },
  { tv: 'CME_MINI:NQ1!', file: 'nq',  name: 'E-mini Nasdaq' },
  { tv: 'CBOE:VIX',      file: 'vix', name: 'VIX' },
];

// ── Portfolio stocks/ETFs — daily only (long-hold positions) ──
// Verified via TradingView searchMarketV3 on 2026-04-13
const PORTFOLIO = [
  // Stocks
  { tv: 'NYSE:AI',       file: 'ai',    name: 'C3.ai' },
  { tv: 'NASDAQ:AMD',    file: 'amd',   name: 'Advanced Micro Devices' },
  { tv: 'AMEX:ARMP',     file: 'armp',  name: 'Armata Pharmaceuticals' },
  { tv: 'NYSE:B',        file: 'b',     name: 'Barrick Mining' },
  { tv: 'NYSE:BB',       file: 'bb',    name: 'BlackBerry' },
  { tv: 'NYSE:CF',       file: 'cf',    name: 'CF Industries' },
  { tv: 'NYSE:CMP',      file: 'cmp',   name: 'Compass Minerals' },
  { tv: 'NYSE:DELL',     file: 'dell',  name: 'Dell Technologies' },
  { tv: 'NASDAQ:DPRO',   file: 'dpro',  name: 'Draganfly' },
  { tv: 'NYSE:FIGS',     file: 'figs',  name: 'FIGS' },
  { tv: 'NASDAQ:FROG',   file: 'frog',  name: 'JFrog' },
  { tv: 'NYSE:GME',      file: 'gme',   name: 'GameStop' },
  { tv: 'NASDAQ:HFFG',   file: 'hffg',  name: 'HF Foods Group' },
  { tv: 'NASDAQ:INTC',   file: 'intc',  name: 'Intel' },
  { tv: 'NYSE:IPI',      file: 'ipi',   name: 'Intrepid Potash' },
  { tv: 'NASDAQ:MARA',   file: 'mara',  name: 'MARA Holdings' },
  { tv: 'NYSE:MOS',      file: 'mos',   name: 'Mosaic Company' },
  { tv: 'NASDAQ:MRNA',   file: 'mrna',  name: 'Moderna' },
  { tv: 'NASDAQ:NNE',    file: 'nne',   name: 'Nano Nuclear Energy' },
  { tv: 'NASDAQ:NVDA',   file: 'nvda',  name: 'NVIDIA' },
  { tv: 'NYSE:OKLO',     file: 'oklo',  name: 'Oklo' },
  { tv: 'NYSE:OPTU',     file: 'optu',  name: 'Optimum Communications' },
  { tv: 'NYSE:PATH',     file: 'path',  name: 'UiPath' },
  { tv: 'NASDAQ:PBYI',   file: 'pbyi',  name: 'Puma Biotechnology' },
  { tv: 'NYSE:PFE',      file: 'pfe',   name: 'Pfizer' },
  { tv: 'AMEX:PHGE',     file: 'phge',  name: 'BiomX' },
  { tv: 'NASDAQ:PLTR',   file: 'pltr',  name: 'Palantir Technologies' },
  { tv: 'NASDAQ:PLUG',   file: 'plug',  name: 'Plug Power' },
  { tv: 'NASDAQ:PYPL',   file: 'pypl',  name: 'PayPal' },
  { tv: 'NASDAQ:SEDG',   file: 'sedg',  name: 'SolarEdge Technologies' },
  { tv: 'NYSE:SNAP',     file: 'snap',  name: 'Snap' },
  { tv: 'NASDAQ:TASK',   file: 'task_us', name: 'TaskUs' },
  { tv: 'NYSE:TEVA',     file: 'teva',  name: 'Teva Pharmaceutical' },
  { tv: 'NASDAQ:TSLA',   file: 'tsla',  name: 'Tesla' },
  { tv: 'NYSE:U',        file: 'u',     name: 'Unity Software' },
  { tv: 'AMEX:UAVS',     file: 'uavs',  name: 'AgEagle Aerial Systems' },
  // ETFs
  { tv: 'AMEX:BATT',     file: 'batt',  name: 'Amplify Lithium & Battery ETF' },
  { tv: 'AMEX:CANE',     file: 'cane',  name: 'Teucrium Sugar Fund' },
  { tv: 'AMEX:CORN',     file: 'corn',  name: 'Teucrium Corn Fund' },
  { tv: 'AMEX:CPER',     file: 'cper',  name: 'United States Copper ETF' },
  { tv: 'AMEX:DBA',      file: 'dba',   name: 'Invesco DB Agriculture Fund' },
  { tv: 'AMEX:DBB',      file: 'dbb',   name: 'Invesco DB Base Metals Fund' },
  { tv: 'AMEX:DBC',      file: 'dbc',   name: 'Invesco DB Commodity Index' },
  { tv: 'AMEX:DBE',      file: 'dbe',   name: 'Invesco DB Energy Fund' },
  { tv: 'AMEX:DBO',      file: 'dbo',   name: 'Invesco DB Oil Fund' },
  { tv: 'AMEX:EFZ',      file: 'efz',   name: 'ProShares Short MSCI EAFE' },
  { tv: 'NASDAQ:FTAG',   file: 'ftag',  name: 'First Trust Global Agriculture ETF' },
  { tv: 'AMEX:GSG',      file: 'gsg',   name: 'iShares S&P GSCI Commodity ETF' },
  { tv: 'AMEX:IWM',      file: 'iwm',   name: 'iShares Russell 2000 ETF' },
  { tv: 'NASDAQ:KROP',   file: 'krop',  name: 'Global X AgTech & Food Innovation ETF' },
  { tv: 'AMEX:LIT',      file: 'lit',   name: 'Global X Lithium & Battery ETF' },
  { tv: 'AMEX:MSOS',     file: 'msos',  name: 'AdvisorShares Pure US Cannabis ETF' },
  { tv: 'CBOE:OILK',     file: 'oilk',  name: 'ProShares K-1 Free Crude Oil ETF' },
  { tv: 'AMEX:SLV',      file: 'slv',   name: 'iShares Silver Trust' },
  { tv: 'AMEX:SOYB',     file: 'soyb',  name: 'Teucrium Soybean Fund' },
  { tv: 'AMEX:TAGS',     file: 'tags',  name: 'Teucrium Agricultural Fund' },
  { tv: 'AMEX:UNG',      file: 'ung',   name: 'United States Natural Gas Fund' },
  { tv: 'AMEX:URNM',     file: 'urnm',  name: 'Sprott Uranium Miners ETF' },
  { tv: 'AMEX:VEGI',     file: 'vegi',  name: 'iShares MSCI Agriculture Producers ETF' },
  { tv: 'AMEX:WEAT',     file: 'weat',  name: 'Teucrium Wheat Fund' },
];

// Futures get daily + 1h; portfolio stocks get daily only
const FUTURES_TIMEFRAMES = [
  { tf: 'D', candles: 500, suffix: 'daily' },
  { tf: '60', candles: 50, suffix: '1h' },
];
const PORTFOLIO_TIMEFRAMES = [
  { tf: 'D', candles: 500, suffix: 'daily' },
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
  const totalSymbols = FUTURES.length + PORTFOLIO.length;
  const totalFetches = FUTURES.length * FUTURES_TIMEFRAMES.length
                     + PORTFOLIO.length * PORTFOLIO_TIMEFRAMES.length;
  console.log(`TradingView Data Fetcher — ${new Date().toISOString()}`);
  console.log(`Futures: ${FUTURES.length}, Portfolio: ${PORTFOLIO.length}, Total fetches: ${totalFetches}`);
  console.log('');

  const client = new TradingView.Client({
    token: process.env.SESSION,
    signature: process.env.SIGNATURE,
  });

  let successCount = 0;
  let errorCount = 0;
  const errors = [];

  // Fetch futures (daily + 1h)
  console.log('── Futures (daily + 1h) ──');
  for (const symbol of FUTURES) {
    for (const { tf, candles, suffix } of FUTURES_TIMEFRAMES) {
      const label = `${symbol.tv} (${suffix})`;
      try {
        process.stdout.write(`  ${label}...`);
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
        console.log(` ${data.length} candles`);
        successCount++;
      } catch (err) {
        console.log(` ERROR: ${err.message}`);
        errors.push(label);
        errorCount++;
      }
    }
  }

  // Fetch portfolio stocks/ETFs (daily only)
  console.log('');
  console.log('── Portfolio stocks/ETFs (daily) ──');
  for (const symbol of PORTFOLIO) {
    for (const { tf, candles, suffix } of PORTFOLIO_TIMEFRAMES) {
      const label = `${symbol.tv} (${suffix})`;
      try {
        process.stdout.write(`  ${label}...`);
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
        console.log(` ${data.length} candles`);
        successCount++;
      } catch (err) {
        console.log(` ERROR: ${err.message}`);
        errors.push(label);
        errorCount++;
      }
    }
  }

  client.end();

  console.log('');
  console.log(`Done: ${successCount}/${totalFetches} saved, ${errorCount} errors`);
  if (errors.length > 0) {
    console.log(`Failed: ${errors.join(', ')}`);
  }
  console.log(`Cache: ${CACHE_DIR}`);
  process.exit(errorCount > 0 ? 1 : 0);
})();
