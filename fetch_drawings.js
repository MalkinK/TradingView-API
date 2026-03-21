/**
 * fetch_drawings.js — Fetch Kristin's drawings/levels from TradingView chart.
 *
 * Extracts horizontal lines, trend lines, zones, and other drawing objects
 * from a TradingView layout, saves simplified JSON to ~/shared/tv_cache/drawings.json.
 *
 * Usage: node fetch_drawings.js [LAYOUT_ID]
 *   LAYOUT_ID from TradingView chart URL: /chart/LAYOUT_ID/
 *   Falls back to LAYOUT_ID in .env if not provided.
 *
 * Requires: SESSION, SIGNATURE, USER_ID in .env
 *
 * Author: Claude Code
 * Created: 2026-03-21
 */

require('dotenv').config();

// ── Cookie injection for charts-storage API ─────────────────
// TradingView added header validation to charts-storage.tradingview.com.
// The library's default requests fail with "Header validation failed".
// Fix: inject cookie + Origin headers on all charts-storage requests.
const axios = require('axios');
const _origAxiosGet = axios.get.bind(axios);
axios.get = function(url, config) {
  if (typeof url === 'string' && url.includes('charts-storage.tradingview.com')) {
    config = config || {};
    config.headers = config.headers || {};
    config.headers['Cookie'] = `sessionid=${process.env.SESSION}; sessionid_sign=${process.env.SIGNATURE}`;
    config.headers['Origin'] = 'https://www.tradingview.com';
    config.headers['Referer'] = 'https://www.tradingview.com/';
  }
  return _origAxiosGet(url, config);
};

const TradingView = require('@mathieuc/tradingview');
const fs = require('fs');
const path = require('path');

// ── Config ──────────────────────────────────────────────────
const CACHE_DIR = path.join(process.env.HOME, 'shared', 'tv_cache');
const OUTPUT_FILE = path.join(CACHE_DIR, 'drawings.json');

// ── Validation ──────────────────────────────────────────────
if (!process.env.SESSION || !process.env.SIGNATURE) {
  console.error('ERROR: Set SESSION and SIGNATURE in .env file');
  process.exit(1);
}

const USER_ID = parseInt(process.env.USER_ID, 10);
if (!USER_ID) {
  console.error('ERROR: Set USER_ID in .env (find it via TradingView profile)');
  process.exit(1);
}

const LAYOUT_ID = process.argv[2] || process.env.LAYOUT_ID;
if (!LAYOUT_ID) {
  console.error('Usage: node fetch_drawings.js <LAYOUT_ID>');
  console.error('  Or set LAYOUT_ID in .env');
  process.exit(1);
}

fs.mkdirSync(CACHE_DIR, { recursive: true });

// ── Drawing type labels for readability ─────────────────────
const DRAWING_TYPE_LABELS = {
  'LineToolHorzLine': 'Horizontal Line',
  'LineToolHorzRay': 'Horizontal Ray',
  'LineToolTrendLine': 'Trend Line',
  'LineToolRay': 'Ray',
  'LineToolExtended': 'Extended Line',
  'LineToolParallelChannel': 'Parallel Channel',
  'LineToolFibRetracement': 'Fib Retracement',
  'LineToolRectangle': 'Rectangle/Zone',
  'LineToolText': 'Text Note',
  'LineToolCallout': 'Callout',
  'LineToolPriceRange': 'Price Range',
  'LineToolDateRange': 'Date Range',
  'LineToolDateAndPriceRange': 'Date & Price Range',
  'LineToolRiskRewardLong': 'Risk/Reward Long',
  'LineToolRiskRewardShort': 'Risk/Reward Short',
  'LineToolBrush': 'Brush',
  'LineToolHighlighter': 'Highlighter',
  'LineToolArrow': 'Arrow',
  'LineToolCircle': 'Circle',
  'LineToolEllipse': 'Ellipse',
  'LineToolTriangle': 'Triangle',
  'LineToolPolyline': 'Polyline',
  'LineToolPath': 'Path',
  'LineToolVertLine': 'Vertical Line',
};

// ── Extract chartId and symbol from TradingView page ────────
// The library's getDrawings has two bugs:
//   1. Defaults chart_id to '_shared', but the real value is from charts[0].chartId
//      in the layout JSON (typically '1' for single-chart layouts).
//   2. Passes symbol='' which returns 0 results. Must omit or pass real symbol.
// See: https://github.com/Mathieu2301/TradingView-API/issues/79
async function getLayoutInfo(layoutId) {
  const cookie = `sessionid=${process.env.SESSION}; sessionid_sign=${process.env.SIGNATURE}`;
  try {
    const resp = await axios.get(`https://www.tradingview.com/chart/${layoutId}/`, {
      headers: {
        'Cookie': cookie,
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'text/html',
      },
    });
    const html = resp.data;

    // Extract the layout content JSON to find chartId and symbol
    const contentMatch = html.match(/initData\.content\s*=\s*({.*?});\s*\n/s);
    if (contentMatch) {
      const content = JSON.parse(contentMatch[1]);
      const chartId = content.charts && content.charts[0] ? String(content.charts[0].chartId) : null;

      // Find the main symbol from the MainSeries source
      let symbol = null;
      const symbolMatch = contentMatch[1].match(/"symbol":"([^"]+)"/);
      if (symbolMatch) symbol = symbolMatch[1];

      return { chartId, symbol, name: content.name };
    }
  } catch (e) {
    // Fall through to defaults
  }
  return { chartId: null, symbol: null, name: null };
}

// ── Fetch drawings directly (bypasses library's buggy defaults) ──
async function fetchDrawings(layoutId, chartId, creds) {
  const jwt = await TradingView.getChartToken(layoutId, creds);

  // Do NOT pass symbol parameter — empty string returns 0 results
  const params = { chart_id: chartId, jwt };
  const { data } = await axios.get(
    `https://charts-storage.tradingview.com/charts-storage/get/layout/${layoutId}/sources`,
    { params, validateStatus: (s) => s < 500 }
  );

  if (!data.payload) return [];
  return Object.values(data.payload.sources || {}).map((drawing) => ({
    ...drawing, ...drawing.state,
  }));
}

// ── Main ────────────────────────────────────────────────────
(async () => {
  console.log(`Fetching drawings from layout: ${LAYOUT_ID} (user: ${USER_ID})`);

  // Extract chartId and symbol from the layout page
  const info = await getLayoutInfo(LAYOUT_ID);
  console.log(`Layout: "${info.name || 'unknown'}", Chart ID: ${info.chartId || '?'}, Symbol: ${info.symbol || '?'}`);
  console.log('');

  const creds = {
    session: process.env.SESSION,
    signature: process.env.SIGNATURE,
    id: USER_ID,
  };

  // Try layout's chartId, then fallback values
  const chartIdsToTry = [
    info.chartId,
    '_shared',
    '0', '1', '2',
  ].filter(Boolean);

  // Deduplicate
  const uniqueIds = [...new Set(chartIdsToTry)];

  let drawings = [];
  let usedChartId = null;
  for (const chartId of uniqueIds) {
    try {
      drawings = await fetchDrawings(LAYOUT_ID, chartId, creds);
      if (drawings.length > 0) {
        usedChartId = chartId;
        break;
      }
    } catch (err) {
      // Try next chart_id
    }
  }

  if (!drawings || drawings.length === 0) {
    console.log('No drawings found in this layout.');
    console.log('');
    console.log('Possible causes:');
    console.log('  1. No lines/levels drawn on this chart yet');
    console.log('  2. Drawings not saved — press Ctrl+S in TradingView');
    console.log('  3. Drawings are on a different layout');
    process.exit(0);
  }

  console.log(`Found ${drawings.length} drawings (chart_id: ${usedChartId})`);

  // Simplify each drawing to essential fields
  // After library spread: d.type, d.points, d.zorder are top-level;
  // d.state contains style properties (linecolor, textcolor, etc.)
  const simplified = drawings.map(d => {
    const style = d.state || {};
    const result = {
      id: d.id,
      symbol: d.symbol,
      type: d.type,
      type_label: DRAWING_TYPE_LABELS[d.type] || d.type,
    };

    if (style.text) result.text = style.text;

    // Price points — at top level after spread (d.points, not d.state.points)
    const points = d.points || [];
    if (points.length > 0) {
      result.points = points.map(p => {
        const point = {};
        if (p.price !== undefined) point.price = p.price;
        if (p.time_t !== undefined) point.time = p.time_t;
        if (p.offset !== undefined) point.offset = p.offset;
        return point;
      });

      const prices = points
        .filter(p => p.price !== undefined)
        .map(p => p.price);
      if (prices.length > 0) {
        result.price_levels = [...new Set(prices)].sort((a, b) => a - b);
      }
    }

    // Fib levels (in style properties)
    if (style.levels) {
      result.fib_levels = style.levels
        .filter(l => l.visible !== false && l.coeff !== undefined)
        .map(l => ({ ratio: l.coeff, price: l.price }));
    }

    if (style.linecolor) result.color = style.linecolor;
    if (style.backgroundColor) result.background_color = style.backgroundColor;
    if (style.linewidth) result.line_width = style.linewidth;

    return result;
  });

  // Group by symbol
  const bySymbol = {};
  for (const d of simplified) {
    const sym = d.symbol || 'unknown';
    if (!bySymbol[sym]) bySymbol[sym] = [];
    bySymbol[sym].push(d);
  }

  const output = {
    layout_id: LAYOUT_ID,
    user_id: USER_ID,
    chart_id: usedChartId,
    layout_name: info.name,
    fetched_at: new Date().toISOString(),
    total_drawings: simplified.length,
    by_symbol: bySymbol,
    all_drawings: simplified,
  };

  fs.writeFileSync(OUTPUT_FILE, JSON.stringify(output, null, 2));

  // Print summary
  console.log(`Found ${simplified.length} drawings:`);
  console.log('');
  for (const [sym, items] of Object.entries(bySymbol)) {
    console.log(`  ${sym}: ${items.length} drawings`);
    for (const item of items) {
      const prices = item.price_levels
        ? ` @ ${item.price_levels.join(', ')}`
        : '';
      const text = item.text ? ` "${item.text}"` : '';
      console.log(`    - ${item.type_label}${text}${prices}`);
    }
  }
  console.log('');
  console.log(`Saved to: ${OUTPUT_FILE}`);
  process.exit(0);
})();
