/* ============================================
   ANTIGRAVITY COCKPIT — Frontend Logic (v2)
   Adds: 300-symbol trending universe (light tier), searchable/filtered/
   virtualized watchlist, favorites, chart ranges (1J/7J/1M/1An), and
   bounded client-side memory (ring buffers + throttled re-renders).
   Real Binance Spot data only — mock is never presented as real.
   ============================================ */

const API_URL = `http://${window.location.host}/api`;
const WS_URL = `ws://${window.location.host}/ws/live`;

let chart, candlestickSeries, volumeSeries, ws;
let currentSymbol = '';
let currentRange = '1D';

// Frontend memory bounds — overwritten by /api/binance/config.frontend_limits.
const LIMITS = {
    maxCandles: 1500, maxVisible: 60, maxEvents: 200, maxLogs: 600,
    uiThrottleMs: 400, snapshotMs: 3000,
};

// Binance live-layer config (price/candle source + ranges + limits). Fetched once.
let liveConfig = {
    enabled: false, price_source: 'trade', candle_source: 'derived_trades',
    candle_interval: '1m', max_age_ms: 3000, chart_live_max_age_ms: 6000,
    chart_ranges: ['1D', '7D', '1M', '1Y'], range_default: '1D',
    range_intervals: { '1D': '1m', '7D': '15m', '1M': '1h', '1Y': '1d' },
    universe_enabled: false, universe_limit: 300, symbols: [], active_symbol: null,
};
let lastLiveSnap = null;  // most recent /ws/live "live" payload, shown in 🔬 Source

const VOL_UP = 'rgba(38,194,129,0.5)', VOL_DOWN = 'rgba(240,97,109,0.5)';
const CHART_STALE_MS_DEFAULT = 6000;

// Verbose chart logging: ?debug=1 in the URL, or window.CHART_DEBUG = true.
const CHART_DEBUG = (typeof window !== 'undefined') &&
    (window.CHART_DEBUG === true || /[?&]debug=1\b/.test(window.location.search));
function chartLog(...args) { if (CHART_DEBUG) console.log('[chart]', ...args); }

// ── Small utilities (throttle / debounce / formatting) ───────────────────────
function throttle(fn, ms) {
    let last = 0, timer = null, lastArgs;
    return (...args) => {
        lastArgs = args;
        const now = Date.now();
        const remaining = ms - (now - last);
        if (remaining <= 0) { last = now; fn(...lastArgs); }
        else if (!timer) { timer = setTimeout(() => { last = Date.now(); timer = null; fn(...lastArgs); }, remaining); }
    };
}
function debounce(fn, ms) {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
function fmtPrice(p) {
    if (p == null || !Number.isFinite(Number(p))) return '--';
    const a = Math.abs(Number(p));
    const dp = a >= 1000 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : a >= 0.0001 ? 7 : 8;
    return Number(p).toLocaleString('en-US', { minimumFractionDigits: Math.min(dp, 2), maximumFractionDigits: dp });
}
function fmtCompact(n) {
    if (n == null || !Number.isFinite(Number(n))) return '--';
    n = Number(n);
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (a >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (a >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toFixed(0);
}
function fmtPct(p) {
    if (p == null || !Number.isFinite(Number(p))) return '--';
    return (p >= 0 ? '+' : '') + Number(p).toFixed(2) + '%';
}
function chgClass(p) { return p == null ? 'flat' : (p > 0 ? 'up' : p < 0 ? 'down' : 'flat'); }

// ── Chart candle store (authoritative, prevents the "frozen chart" bug) ──────
// Lightweight-Charts THROWS when update() gets a time older than the last bar;
// the swallowed throw used to freeze every later update. This store guarantees we
// never feed update() a backwards time, tracks freshness, and logs every apply.
const chartStore = {
    symbol: '', interval: '', source: 'none',
    lastTime: null, lastCandle: null, lastClose: null,
    count: 0, klineEvents: 0, lastAppliedAt: 0,
    // Bounded local rings of the rendered bars so the LIVE append path can trim the
    // lightweight-charts series (whose update() is incremental and never shrinks).
    bars: [], volBars: [],
};
// Trim slack: rebuild the series only once it exceeds the cap by this margin, so we
// don't call setData() on every tick once full.
const CHART_TRIM_SLACK = 250;

function toChartTime(t) {
    let n = Number(t);
    if (!Number.isFinite(n)) return null;
    if (n > 1e12) n = Math.floor(n / 1000);   // ms → seconds
    return Math.floor(n);
}

function chartReset(symbol) {
    chartStore.symbol = symbol;
    chartStore.interval = liveConfig.candle_interval || '';
    chartStore.source = 'none';
    chartStore.lastTime = null;
    chartStore.lastCandle = null;
    chartStore.lastClose = null;
    chartStore.count = 0;
    chartStore.klineEvents = 0;
    chartStore.lastAppliedAt = 0;
    chartStore.bars = [];
    chartStore.volBars = [];
    try { candlestickSeries.setData([]); volumeSeries.setData([]); } catch (e) {}
    chartLog('reset', symbol);
}

// Replace the whole series from a backfill (REST klines or DB OHLCV). Trims to the
// configured max-candles bound so the chart never grows unbounded in memory.
function chartSetHistory(candles, source, interval) {
    const seen = new Map();
    for (const d of candles) {
        if (!d) continue;
        const t = toChartTime(d.time);
        if (t == null) continue;
        const o = Number(d.open), h = Number(d.high), l = Number(d.low), c = Number(d.close);
        if (![o, h, l, c].every(Number.isFinite)) continue;
        seen.set(t, { time: t, open: o, high: h, low: l, close: c, value: Number(d.value || 0) });
    }
    let unique = Array.from(seen.values()).sort((a, b) => a.time - b.time);
    if (unique.length > LIMITS.maxCandles) unique = unique.slice(unique.length - LIMITS.maxCandles);
    chartStore.bars = unique.map(d => ({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close }));
    chartStore.volBars = unique.map(d => ({ time: d.time, value: d.value, color: d.close >= d.open ? VOL_UP : VOL_DOWN }));
    candlestickSeries.setData(chartStore.bars);
    volumeSeries.setData(chartStore.volBars);

    const last = unique.length ? unique[unique.length - 1] : null;
    chartStore.source = source;
    chartStore.interval = interval || chartStore.interval;
    chartStore.lastTime = last ? last.time : null;
    chartStore.lastCandle = last;
    chartStore.lastClose = last ? last.close : null;
    chartStore.count = unique.length;
    chartStore.lastAppliedAt = last ? Date.now() : 0;
    chartLog(`history set: ${unique.length} candles, source=${source}, interval=${chartStore.interval}`);
}

// Apply one live candle WITHOUT ever calling update() with a backwards time.
function chartApplyCandle(raw, source) {
    if (!raw || raw.time == null) return false;
    const t = toChartTime(raw.time);
    const o = Number(raw.open), h = Number(raw.high), l = Number(raw.low), c = Number(raw.close);
    if (t == null || ![o, h, l, c].every(Number.isFinite)) { chartLog('skip non-numeric candle', raw); return false; }
    const vol = Number(raw.value || 0);
    const bar = { time: t, open: o, high: h, low: l, close: c };
    const volBar = { time: t, value: vol, color: c >= o ? VOL_UP : VOL_DOWN };

    if (chartStore.lastTime != null && t < chartStore.lastTime) {
        // Backwards time → would throw & freeze. Rebase onto the authoritative source.
        chartLog(`out-of-order candle t=${t} < lastTime=${chartStore.lastTime} — rebasing onto ${source}`);
        candlestickSeries.setData([bar]);
        volumeSeries.setData([volBar]);
        chartStore.bars = [bar];
        chartStore.volBars = [volBar];
        chartStore.count = 1;
    } else {
        const isNew = chartStore.lastTime == null || t > chartStore.lastTime;
        try { candlestickSeries.update(bar); volumeSeries.update(volBar); }
        catch (e) { console.warn('[chart] update() failed (kept series intact):', e.message || e, raw); return false; }
        if (isNew) {
            chartStore.bars.push(bar);
            chartStore.volBars.push(volBar);
            // Bound the live series: update() never shrinks it, so rebuild via setData
            // once we exceed the cap by the slack margin (cheap, amortized).
            if (chartStore.bars.length > LIMITS.maxCandles + CHART_TRIM_SLACK) {
                chartStore.bars = chartStore.bars.slice(-LIMITS.maxCandles);
                chartStore.volBars = chartStore.volBars.slice(-LIMITS.maxCandles);
                candlestickSeries.setData(chartStore.bars);
                volumeSeries.setData(chartStore.volBars);
            }
        } else if (chartStore.bars.length) {
            chartStore.bars[chartStore.bars.length - 1] = bar;
            chartStore.volBars[chartStore.volBars.length - 1] = volBar;
        }
        chartStore.count = chartStore.bars.length;
        chartLog(`${isNew ? 'append' : 'update'} t=${t} C=${c} n=${chartStore.count}`);
    }

    chartStore.lastTime = t;
    chartStore.lastCandle = bar;
    chartStore.lastClose = c;
    chartStore.source = source;
    chartStore.lastAppliedAt = Date.now();
    if (source === 'binance_kline') chartStore.klineEvents++;
    return true;
}

// ── Chart status badge (CHART LIVE / STALE / NO CANDLES / MOCK) ──────────────
function chartStaleThreshold() { return (liveConfig && liveConfig.chart_live_max_age_ms) || CHART_STALE_MS_DEFAULT; }

function setChartStatusBadge(state, ageMs) {
    const el = document.getElementById('chart-status');
    if (!el) return;
    const derived = chartStore.source === 'ohlcv_derived';
    const map = {
        nocandles: ['NO CANDLES', 'disconnected'],
        offline: ['CHART OFFLINE', 'disconnected'],
        live: [derived ? 'CHART LIVE (derived)' : 'CHART LIVE', 'connected'],
        stale: ['CHART STALE' + (typeof ageMs === 'number' ? ` ${Math.round(ageMs / 1000)}s` : ''), 'stale'],
        mock: ['MOCK', 'mock'],
    };
    const [text, cls] = map[state] || map.nocandles;
    el.textContent = text;
    el.className = 'status-badge ' + cls;
    const lastT = chartStore.lastTime ? new Date(chartStore.lastTime * 1000).toLocaleTimeString() : 'n/a';
    el.title = `chart source: ${chartStore.source} · interval ${chartStore.interval} · candles ${chartStore.count} · last bar ${lastT} · last close ${chartStore.lastClose ?? 'n/a'}`;
    el.dataset.state = state;
}

let chartWatchdog = null;
function recomputeChartStatus() {
    if (!chartStore.symbol) return;
    if (chartStore.count === 0 || chartStore.source === 'none') { setChartStatusBadge('nocandles'); return; }
    const age = Date.now() - chartStore.lastAppliedAt;
    setChartStatusBadge(age <= chartStaleThreshold() ? 'live' : 'stale', age);
}
function startChartWatchdog() {
    if (chartWatchdog) clearInterval(chartWatchdog);
    chartWatchdog = setInterval(recomputeChartStatus, 1000);
}

async function loadLiveConfig() {
    try {
        const res = await fetch(`${API_URL}/binance/config`);
        const cfg = await res.json();
        if (cfg && !cfg.error) liveConfig = Object.assign(liveConfig, cfg);
    } catch (e) { /* keep defaults */ }
    // Apply server-provided frontend memory bounds.
    const fl = liveConfig.frontend_limits || {};
    if (fl.max_candles_per_symbol) LIMITS.maxCandles = fl.max_candles_per_symbol;
    if (fl.max_visible_symbols) LIMITS.maxVisible = fl.max_visible_symbols;
    if (fl.max_event_buffer) LIMITS.maxEvents = fl.max_event_buffer;
    if (fl.max_log_buffer) LIMITS.maxLogs = fl.max_log_buffer;
    if (fl.ui_update_throttle_ms) LIMITS.uiThrottleMs = fl.ui_update_throttle_ms;
    if (fl.snapshot_interval_ms) LIMITS.snapshotMs = fl.snapshot_interval_ms;
    currentRange = liveConfig.range_default || '1D';
    updateChartSourceBadge();
    syncRangeButtons();
}

function updateChartSourceBadge() {
    const el = document.getElementById('chart-source-badge');
    if (!el) return;
    if (liveConfig.enabled) {
        el.textContent = `${liveConfig.price_source} · ${chartStore.interval || liveConfig.candle_interval}`;
        el.className = 'source-badge';
        el.title = `Displayed price = Binance Spot ${liveConfig.price_source} · chart ${liveConfig.candle_source} ${chartStore.interval || liveConfig.candle_interval}`;
    } else {
        el.textContent = 'derived · 1s';
        el.className = 'source-badge derived';
        el.title = 'Binance live hub disabled — price/candles derived from DB OHLCV (may lag)';
    }
}

// ── Chart ──────────────────────────────────
function initChart() {
    const el = document.getElementById('tvchart');
    chart = LightweightCharts.createChart(el, {
        layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#8C97A8', fontFamily: 'Inter' },
        grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.03)' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: 'rgba(255,255,255,0.1)' },
        timeScale: { borderColor: 'rgba(255,255,255,0.1)', timeVisible: true, secondsVisible: true },
    });
    candlestickSeries = chart.addCandlestickSeries({
        upColor: '#26C281', downColor: '#F0616D', borderVisible: false,
        wickUpColor: '#26C281', wickDownColor: '#F0616D',
    });
    volumeSeries = chart.addHistogramSeries({
        color: '#26C281', priceFormat: { type: 'volume' }, priceScaleId: '',
        scaleMargins: { top: 0.8, bottom: 0 },
    });
    new ResizeObserver(entries => {
        if (!entries.length || entries[0].target !== el) return;
        const r = entries[0].contentRect;
        // Never apply a zero/negative size. Lightweight-Charts collapses (and cannot
        // recover) if it receives height/width 0 during a drawer slide or layout
        // transition — skip until the container actually has a box.
        const w = Math.floor(r.width), h = Math.floor(r.height);
        if (w > 0 && h > 0) chart.applyOptions({ height: h, width: w });
    }).observe(el);
}

// ── Universe (Tier 1 — light, top trending) ─────────────────────────────────
const universe = { rows: [], bySymbol: new Map(), connected: false, source: 'unavailable', lastRefreshMs: 0, count: 0 };
const coreBySymbol = new Map();        // /api/watchlist overlay (s_total, action, quality, social)
const signalsBySymbol = new Map();     // /api/signals (right panel cards)
let activeFilter = 'trending';
let searchQuery = '';

// Favorites persisted in localStorage.
function loadFavorites() {
    try { return new Set(JSON.parse(localStorage.getItem('ag_favorites') || '[]')); }
    catch (e) { return new Set(); }
}
function saveFavorites() {
    try { localStorage.setItem('ag_favorites', JSON.stringify(Array.from(favorites))); } catch (e) {}
}
const favorites = loadFavorites();
function toggleFavorite(symbol) {
    if (favorites.has(symbol)) favorites.delete(symbol); else favorites.add(symbol);
    saveFavorites();
    renderWatchlistNow();
    syncFavCurrent();
}

async function updateCoreWatchlist() {
    try {
        const res = await fetch(`${API_URL}/watchlist`);
        const data = await res.json();
        if (!Array.isArray(data)) return;
        coreBySymbol.clear();
        data.forEach(item => coreBySymbol.set(item.symbol, item));
    } catch (e) { /* silent */ }
}

async function fetchUniverse() {
    let rows = [];
    try {
        const res = await fetch(`${API_URL}/market/universe?limit=${liveConfig.universe_limit || 300}`);
        const data = await res.json();
        if (data && Array.isArray(data.rows)) {
            rows = data.rows;
            universe.connected = !!data.connected;
            universe.source = data.connected ? 'binance_spot' : (rows.length ? 'binance_spot' : 'unavailable');
            universe.lastRefreshMs = data.last_refresh_ms || 0;
        }
    } catch (e) { /* network/hub down */ }

    // Fallback: if the universe hub gave nothing, show the bot-traded core so the
    // cockpit is never empty (clearly marked 'core only', never fabricated data).
    if (!rows.length) {
        rows = Array.from(coreBySymbol.values()).map(c => ({
            symbol: c.symbol, base: c.symbol.split('/')[0], quote: c.symbol.split('/')[1] || 'USDT',
            price: c.price, change_pct: null, quote_volume: null, num_trades: null,
            // trending_score is the [0,1] liquidity composite — NOT available in the
            // core-only fallback (s_total is a different [-1,+1] metric). Show n/a, and
            // keep s_total separately only as a stable sort key for the core list.
            spread_bps: null, trending_score: null, sort_key: c.s_total, rank: null, stale: false,
            source: 'core', is_core: true,
        }));
        universe.connected = false;
        universe.source = rows.length ? 'core_only' : 'unavailable';
    }

    universe.rows = rows;
    universe.count = rows.length;
    universe.bySymbol.clear();
    rows.forEach(r => universe.bySymbol.set(r.symbol, r));
    updateUniverseBadge();
    renderWatchlist();
    updateSelectedStats(currentSymbol);
}

function updateUniverseBadge() {
    const el = document.getElementById('universe-badge');
    if (!el) return;
    if (universe.source === 'unavailable') { el.textContent = 'Universe n/a'; el.className = 'status-badge disconnected'; return; }
    if (universe.source === 'core_only') { el.textContent = `Universe core (${universe.count})`; el.className = 'status-badge stale'; return; }
    el.textContent = `Universe ${universe.count}`;
    el.className = 'status-badge ' + (universe.connected ? 'connected' : 'stale');
    el.title = `Top trending Binance Spot pairs · ${universe.connected ? 'live' : 'seeded (REST)'}`;
}

function filteredUniverse() {
    const q = searchQuery.trim().toUpperCase();
    let rows = universe.rows;
    const byScore = (a, b) => ((b.trending_score ?? b.sort_key ?? 0) - (a.trending_score ?? a.sort_key ?? 0));
    if (q) {
        return rows.filter(r => r.symbol.toUpperCase().includes(q) || (r.base || '').toUpperCase().includes(q))
                   .slice().sort(byScore);
    }
    switch (activeFilter) {
        case 'volume': return rows.slice().sort((a, b) => (b.quote_volume || 0) - (a.quote_volume || 0));
        case 'gainers': return rows.filter(r => r.change_pct != null).slice().sort((a, b) => b.change_pct - a.change_pct);
        case 'losers': return rows.filter(r => r.change_pct != null).slice().sort((a, b) => a.change_pct - b.change_pct);
        case 'core': return rows.filter(r => r.is_core).slice().sort(byScore);
        case 'favorites': return rows.filter(r => favorites.has(r.symbol)).slice().sort(byScore);
        default: return rows.slice().sort((a, b) => (a.rank || 1e9) - (b.rank || 1e9));
    }
}

function renderWatchlistNow() {
    const container = document.getElementById('watchlist-container');
    if (!container) return;
    const all = filteredUniverse();
    const rows = all.slice(0, LIMITS.maxVisible);   // windowed render — never dump 300 DOM rows

    document.getElementById('wl-count').textContent = `${all.length} shown${all.length > rows.length ? ` (top ${rows.length})` : ''}`;
    const srcEl = document.getElementById('wl-source');
    if (srcEl) {
        const map = { binance_spot: ['binance spot · live', 'real'], core_only: ['core only', 'unavailable'], unavailable: ['no live feed', 'unavailable'] };
        const [txt, cls] = map[universe.source] || ['—', ''];
        srcEl.textContent = txt; srcEl.className = 'wl-source ' + cls;
    }

    if (!rows.length) {
        container.innerHTML = `<div class="wl-empty">${searchQuery ? 'No symbol matches “' + escapeHtml(searchQuery) + '”.' : (activeFilter === 'favorites' ? 'No favorites yet — tap ☆ on a symbol.' : 'No market data available.')}</div>`;
        return;
    }

    const frag = document.createDocumentFragment();
    rows.forEach(r => {
        const core = coreBySymbol.get(r.symbol);
        const div = document.createElement('div');
        div.className = 'watchlist-item' + (r.symbol === currentSymbol ? ' active' : '');
        div.dataset.symbol = r.symbol;
        div.setAttribute('role', 'button');
        div.tabIndex = 0;
        div.setAttribute('aria-label', `${r.symbol}${r.price != null ? ' ' + fmtPrice(r.price) : ''}`);
        const fav = favorites.has(r.symbol);
        const isUp = r.change_pct == null ? 'flat' : chgClass(r.change_pct);
        const rankBadge = r.rank ? `<span class="wl-rank">#${r.rank}</span>` : '';
        const quality = core && core.quality_grade ? core.quality_grade : null;
        const qualityDot = quality ? `<span class="wl-quality ${quality}" title="signal quality: ${quality}"></span>` : '';
        const sub = core && core.action_proposed
            ? `<span class="wl-action ${core.action_proposed}">${core.action_proposed}</span>`
            : (r.quote_volume != null ? `<span class="wl-vol">vol ${fmtCompact(r.quote_volume)}</span>` : `<span class="wl-vol">light</span>`);
        div.innerHTML = `
            <button class="wl-fav ${fav ? 'on' : ''}" data-fav="${r.symbol}" title="Toggle favorite">${fav ? '★' : '☆'}</button>
            <div class="wl-main">
                <div class="wl-symbol">${r.symbol}${qualityDot} ${rankBadge}</div>
                <div class="wl-sub">${sub}</div>
            </div>
            <div class="wl-right">
                <div class="wl-price">${fmtPrice(r.price)}</div>
                <div class="wl-chg ${isUp}">${r.change_pct == null ? '—' : fmtPct(r.change_pct)}</div>
            </div>`;
        div.addEventListener('click', () => switchSymbol(r.symbol));
        div.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); switchSymbol(r.symbol); }
        });
        const favBtn = div.querySelector('.wl-fav');
        favBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleFavorite(r.symbol); });
        frag.appendChild(div);
    });
    container.innerHTML = '';
    container.appendChild(frag);
}
const renderWatchlist = throttle(renderWatchlistNow, 350);

function setupWatchlistControls() {
    const search = document.getElementById('wl-search');
    if (search) {
        search.addEventListener('input', debounce(e => { searchQuery = e.target.value || ''; renderWatchlistNow(); }, 180));
    }
    const tabs = document.getElementById('wl-tabs');
    if (tabs) {
        tabs.querySelectorAll('button').forEach(b => {
            b.addEventListener('click', () => {
                activeFilter = b.dataset.filter;
                tabs.querySelectorAll('button').forEach(x => x.classList.toggle('active', x === b));
                renderWatchlistNow();
            });
        });
    }
}

// ── Selected-symbol stats card (right panel top) ─────────────────────────────
function updateSelectedStats(symbol) {
    const el = document.getElementById('selected-stats');
    if (!el || !symbol) return;
    const r = universe.bySymbol.get(symbol);
    const snap = (lastLiveSnap && lastLiveSnap.symbol === symbol) ? lastLiveSnap : null;
    const t = snap && snap.ticker ? snap.ticker : null;
    const price = snap && snap.displayed_price != null ? snap.displayed_price : (r ? r.price : null);
    const change = t ? t.price_change_pct : (r ? r.change_pct : null);
    const vol = t ? t.volume_quote : (r ? r.quote_volume : null);
    const trades = t ? t.num_trades : (r ? r.num_trades : null);
    const tier = (snap || (liveConfig.symbols || []).includes(symbol)) ? 'full' : 'light';
    if (price == null && !r) { el.innerHTML = '<div class="ss-empty">No live data for this symbol.</div>'; return; }
    el.innerHTML = `
        <div class="ss-head">
            <span class="ss-sym">${symbol}</span>
            <span class="ss-tier ${tier}">${tier === 'full' ? 'full detail' : 'light'}</span>
        </div>
        <div class="ss-grid">
            <div class="ss-cell"><span class="ss-label">Price</span><span class="ss-value">${fmtPrice(price)}</span></div>
            <div class="ss-cell"><span class="ss-label">24h</span><span class="ss-value ${chgClass(change)}">${fmtPct(change)}</span></div>
            <div class="ss-cell"><span class="ss-label">24h Vol</span><span class="ss-value">${vol != null ? '$' + fmtCompact(vol) : 'n/a'}</span></div>
            <div class="ss-cell"><span class="ss-label">Trades</span><span class="ss-value">${trades != null ? fmtCompact(trades) : 'n/a'}</span></div>
            <div class="ss-cell"><span class="ss-label">Rank</span><span class="ss-value">${r && r.rank ? '#' + r.rank : 'n/a'}</span></div>
            <div class="ss-cell"><span class="ss-label">Trend Score</span><span class="ss-value">${r && r.trending_score != null ? Number(r.trending_score).toFixed(3) : 'n/a'}</span></div>
        </div>`;
}

// ── Range controls (1J/7J/1M/1An) ────────────────────────────────────────────
function syncRangeButtons() {
    const wrap = document.getElementById('chart-ranges');
    if (!wrap) return;
    wrap.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.range === currentRange));
}
function setupRangeControls() {
    const wrap = document.getElementById('chart-ranges');
    if (!wrap) return;
    wrap.querySelectorAll('button').forEach(b => {
        b.addEventListener('click', () => setRange(b.dataset.range));
    });
}
async function setRange(range) {
    if (!range || range === currentRange) { syncRangeButtons(); return; }
    currentRange = range;
    syncRangeButtons();
    if (currentSymbol) await loadChart(currentSymbol, range, /*switching*/ false);
}

function syncFavCurrent() {
    const btn = document.getElementById('fav-current');
    if (!btn) return;
    const on = favorites.has(currentSymbol);
    btn.textContent = on ? '★' : '☆';
    btn.classList.toggle('on', on);
}
function setupFavCurrent() {
    const btn = document.getElementById('fav-current');
    if (btn) btn.addEventListener('click', () => { if (currentSymbol) toggleFavorite(currentSymbol); });
}

// ── Switch symbol / load chart for a range ───────────────────────────────────
async function switchSymbol(symbol) {
    if (!symbol) return;
    if (ws) { try { ws.close(); } catch (e) {} }
    currentSymbol = symbol;
    document.getElementById('current-symbol').textContent = symbol;
    // Seed the header with the last-known price/change from the universe row (real
    // Binance 24h ticker data) so the price isn't blank '--' during the brief
    // warm-up while the live feed connects — avoids a false 'disconnected' look.
    const seed = universe.bySymbol.get(symbol);
    if (seed && seed.price != null) {
        updateCurrentPrice(seed.price, (seed.change_pct ?? 0) >= 0);
        if (seed.change_pct != null) updateChange(seed.change_pct);
    }
    chartReset(symbol);
    setChartStatusBadge('nocandles');
    syncFavCurrent();
    document.querySelectorAll('.watchlist-item').forEach(el => {
        el.classList.toggle('active', el.dataset.symbol === symbol);
    });
    updateSelectedStats(symbol);
    await loadChart(symbol, currentRange, /*switching*/ true);
    // Guard the post-await race: if the user switched again during loadChart, don't
    // bind the live price/microstructure feed to this now-stale symbol.
    if (symbol !== currentSymbol) return;
    connectWebSocket(symbol);
    updateMicrostructure(symbol);
}

// Load the chart for a symbol+range: tell the hub which symbol/range is active
// (server returns fresh REST klines at the range's interval), then set history.
async function loadChart(symbol, range, switching) {
    try {
        let candles = [], source = 'none', interval = liveConfig.candle_interval;

        if (liveConfig.enabled) {
            try {
                const res = await fetch(`${API_URL}/market/active-symbol`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ symbol, range }),
                });
                const data = await res.json();
                if (data && data.ok && data.klines && Array.isArray(data.klines.candles)) {
                    candles = data.klines.candles;
                    interval = data.interval || data.klines.interval || interval;
                    if (candles.length) source = 'binance_kline';
                }
            } catch (e) { chartLog('active-symbol error', e); }
        }
        // Fallback: range klines endpoint, then DB OHLCV.
        if (!candles.length) {
            try {
                const r = await fetch(`${API_URL}/market/symbol/${encodeURIComponent(symbol)}/klines?range=${encodeURIComponent(range)}`);
                const j = await r.json();
                if (j && Array.isArray(j.candles) && j.candles.length) { candles = j.candles; interval = j.interval || interval; source = 'binance_kline'; }
            } catch (e) {}
        }
        if (!candles.length) {
            try {
                const res = await fetch(`${API_URL}/historical/${encodeURIComponent(symbol)}`);
                const data = await res.json();
                if (Array.isArray(data) && data.length) { candles = data; source = 'ohlcv_derived'; }
            } catch (e) {}
        }

        if (symbol !== currentSymbol) return;   // user switched while we were fetching

        liveConfig.candle_interval = interval;
        chartStore.interval = interval;
        updateChartSourceBadge();

        if (!candles.length) {
            chartStore.source = 'none'; chartStore.count = 0;
            setChartStatusBadge('nocandles');
            chartLog('no backfill candles for', symbol, range);
            return;
        }
        chartSetHistory(candles, source, interval);
        if (chartStore.lastCandle) updateCurrentPrice(chartStore.lastCandle.close, chartStore.lastCandle.close >= chartStore.lastCandle.open);
        recomputeChartStatus();
    } catch (e) { console.error('loadChart error:', e); }
}

// ── WebSocket (price + live candle feed for the selected symbol) ─────────────
const WAIT_DATA_MS = 6000;
const feedState = { connected: false, gotData: false, lastMsgAt: 0, openedAt: 0, symbol: '' };
let feedWatchdog = null;

function setFeedBadge(state, ageMs) {
    const badge = document.getElementById('ws-status');
    if (!badge) return;
    const map = {
        offline: ['Offline', 'disconnected'],
        connecting: ['Connecting…', ''],
        waiting: ['Waiting data', 'stale'],
        nodata: ['No data', 'disconnected'],
        live: ['Live', 'connected'],
        stale: ['STALE' + (typeof ageMs === 'number' ? ` ${Math.round(ageMs / 1000)}s` : ''), 'stale'],
        mock: ['MOCK', 'mock'],
    };
    const [text, cls] = map[state] || map.offline;
    badge.textContent = text;
    badge.className = 'status-badge ' + cls;
    badge.dataset.state = state;
}
function badgeFromFeedStatus(feedStatus) {
    return ({ live: 'live', stale: 'stale', nodata: 'nodata', mock: 'mock' })[feedStatus] || 'waiting';
}

function connectWebSocket(symbol) {
    feedState.connected = false; feedState.gotData = false; feedState.symbol = symbol;
    feedState.openedAt = Date.now();
    setFeedBadge('connecting');
    ws = new WebSocket(`${WS_URL}/${encodeURIComponent(symbol)}`);

    ws.onopen = () => { feedState.connected = true; feedState.lastMsgAt = Date.now(); setFeedBadge('waiting'); };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            feedState.lastMsgAt = Date.now();

            if (msg.type === 'nodata') {
                feedState.gotData = false;
                // During the post-switch warm-up the freshly-selected symbol has no
                // Binance event yet — show a neutral 'waiting' and KEEP the seeded
                // price instead of a red 'No data' flash on every symbol switch.
                // After the grace window, report unavailability honestly.
                if (Date.now() - feedState.openedAt < WAIT_DATA_MS) {
                    setFeedBadge('waiting');
                } else {
                    setFeedBadge('nodata');
                    updateCurrentPriceUnavailable();
                }
                return;
            }
            if (msg.type === 'live') {
                lastLiveSnap = msg;
                const price = msg.displayed_price;
                if (price == null) {
                    if (Date.now() - feedState.openedAt < WAIT_DATA_MS) { setFeedBadge('waiting'); }
                    else { setFeedBadge('nodata'); updateCurrentPriceUnavailable(); }
                    return;
                }
                feedState.gotData = true;
                const isUp = msg.candle ? (msg.candle.close >= msg.candle.open) : (msg.ticker ? msg.ticker.price_change >= 0 : true);
                updateCurrentPrice(price, isUp);
                if (msg.ticker) updateChange(msg.ticker.price_change_pct);
                // Only apply candles for the interval the chart currently expects —
                // protects the chart during a range switch (hub mid-reconnect).
                if (msg.candle && msg.candle.time != null &&
                    (!msg.candle.interval || msg.candle.interval === chartStore.interval)) {
                    chartApplyCandle(msg.candle, msg.chart_source || 'binance_kline');
                }
                if (msg.micro) applyLiveMicrostructure(msg.micro);
                if (msg.symbol === currentSymbol) updateSelectedStats(currentSymbol);
                if (liveDebugOpen()) renderLiveDebug(msg);
                setFeedBadge(badgeFromFeedStatus(msg.feed_status), msg.data_age_ms);
                recomputeChartStatus();
                return;
            }
            if (msg.type !== 'candle') return;
            const d = msg.data;
            if (!d || d.time == null || d.open == null || d.close == null) return;
            feedState.gotData = true;
            chartApplyCandle(d, msg.chart_source || 'ohlcv_derived');
            updateCurrentPrice(d.close, d.close >= d.open);
            setFeedBadge(msg.stale === true ? 'stale' : 'live', msg.data_age_ms);
            recomputeChartStatus();
        } catch (e) { console.error('WS parse error:', e); }
    };

    ws.onclose = () => {
        feedState.connected = false;
        // Only touch the badge if this socket is still the active symbol's — a stale
        // socket closing during a symbol switch must not flash the new symbol to Offline.
        if (currentSymbol === symbol) {
            setFeedBadge('offline');
            recomputeChartStatus();
            setTimeout(() => { if (currentSymbol === symbol) connectWebSocket(symbol); }, 3000);
        }
    };
}

function liveDebugOpen() {
    const m = document.getElementById('live-debug-modal');
    return !!(m && !m.classList.contains('hidden'));
}

function startFeedWatchdog() {
    if (feedWatchdog) clearInterval(feedWatchdog);
    feedWatchdog = setInterval(() => {
        if (!feedState.connected) return;
        const silentFor = Date.now() - feedState.lastMsgAt;
        const badge = document.getElementById('ws-status');
        if (silentFor > WAIT_DATA_MS && badge && badge.dataset.state !== 'nodata') setFeedBadge('waiting');
    }, 2000);
}

function updateCurrentPriceUnavailable() {
    const el = document.getElementById('current-price');
    if (el) { el.textContent = '--'; el.className = 'current-price'; }
}
function updateCurrentPrice(price, isUp) {
    const el = document.getElementById('current-price');
    el.textContent = fmtPrice(price);
    el.className = 'current-price ' + (isUp ? 'price-up' : 'price-down');
}
function updateChange(pct) {
    const el = document.getElementById('current-change');
    if (!el) return;
    el.textContent = fmtPct(pct);
    el.className = 'chg ' + chgClass(pct);
}

// ── Portfolio ──────────────────────────────
async function updatePortfolio() {
    try {
        const res = await fetch(`${API_URL}/portfolio`);
        const d = await res.json();
        if (!d || d.error) return;
        document.getElementById('port-total').textContent = '$' + Number(d.total_value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        document.getElementById('port-cash').textContent = '$' + Number(d.current_cash).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const pnl = d.total_value - d.initial_capital;
        const pnlEl = document.getElementById('port-pnl');
        pnlEl.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
        pnlEl.className = 'value ' + (pnl >= 0 ? 'up' : 'down');
        const exposure = d.initial_capital > 0 ? ((d.initial_capital - d.current_cash) / d.initial_capital * 100) : 0;
        document.getElementById('port-exposure').textContent = exposure.toFixed(1) + '%';
        document.getElementById('port-positions').textContent = d.positions.length + ' / 8';
        const drawdown = d.initial_capital > 0 ? Math.min(0, (d.total_value - d.initial_capital) / d.initial_capital * 100) : 0;
        const ddEl = document.getElementById('port-drawdown');
        ddEl.textContent = drawdown.toFixed(1) + '%';
        ddEl.className = 'value ' + (drawdown < -2 ? 'down' : '');
    } catch (e) { /* silent */ }
}

// ── Signals Panel (core symbols) ────────────
async function updateSignals() {
    try {
        const res = await fetch(`${API_URL}/signals`);
        const data = await res.json();
        if (!data || data.error) return;
        signalsBySymbol.clear();
        data.forEach(s => signalsBySymbol.set(s.symbol, s));

        const container = document.getElementById('signals-container');
        container.innerHTML = '';
        data.forEach(sig => {
            const card = document.createElement('div');
            card.className = 'signal-card';
            const actionClass = sig.action_proposed || 'hold';
            const confidencePct = sig.confidence_score ? (sig.confidence_score * 100).toFixed(0) + '%' : '--';
            const socBadge = sig.social_available
                ? `<span class="score-badge social" data-tooltip="Social signal: sentiment, velocity, credibility">SOC ${sig.s_social >= 0 ? '+' : ''}${sig.s_social.toFixed(2)}</span>`
                : `<span class="score-badge social unavailable" data-tooltip="No real social feed configured">SOC n/a</span>`;
            // "Why this action" — derived from the persisted reason_code (real decision
            // data). Explains BUY/HOLD/REDUCE/EXIT and surfaces any forcing risk gate.
            const why = sig.reason_code
                ? `<div class="signal-why" title="Decision reason (persisted reason_code)">${escapeHtml(explainReason(sig.reason_code, sig.s_total))}</div>`
                : '';
            card.innerHTML = `
                <div class="signal-symbol">${sig.symbol}<span class="signal-action-badge ${actionClass}">${actionClass}</span></div>
                <div class="signal-scores">
                    ${socBadge}
                    <span class="score-badge market" data-tooltip="Market confirmation: momentum, volume, microstructure">MKT ${sig.s_market >= 0 ? '+' : ''}${sig.s_market.toFixed(2)}</span>
                    <span class="score-badge risk" data-tooltip="Risk gate: liquidity, spread, concentration, drawdown">RSK ${sig.s_risk.toFixed(2)}</span>
                    <span class="score-badge total" data-tooltip="Composite: 0.45×SOC + 0.45×MKT + 0.10×(2×RSK-1)">Σ ${sig.s_total >= 0 ? '+' : ''}${sig.s_total.toFixed(2)}</span>
                </div>
                ${why}
                <div class="signal-meta"><span>Confidence: ${confidencePct}</span><span>Quality: ${sig.quality_grade || 'N/A'}</span></div>`;
            card.addEventListener('click', () => openSignalDetail(sig.symbol));
            container.appendChild(card);
        });
    } catch (e) { /* silent */ }
}

// ── Microstructure ─────────────────────────
// Explicit "this metric is genuinely not available right now, and here's why"
// — never a silent bare n/a. reason shows on hover and is announced via title.
function setMicroUnavail(id, label, reason) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = label;                 // e.g. 'unavail' / 'stale 12s' / 'L2 off'
    el.className = 'micro-value unavailable';
    el.title = reason || '';
}
function setMicro(id, value, goodThresh, badThresh, format, invert = false) {
    const el = document.getElementById(id);
    if (!el) return;
    if (value == null || Number.isNaN(value)) {
        setMicroUnavail(id, 'unavail', 'no real value for this metric on the selected symbol');
        return;
    }
    el.textContent = format(value);
    el.title = '';
    let cls = 'micro-value';
    if (invert) { if (value <= goodThresh) cls += ' good'; else if (value >= badThresh) cls += ' bad'; else cls += ' warn'; }
    else { if (value >= goodThresh) cls += ' good'; else if (value <= badThresh) cls += ' bad'; else cls += ' warn'; }
    el.className = cls;
}
function applyLiveMicrostructure(m) {
    if (m.spread_bps != null) setMicro('micro-spread', m.spread_bps, 3, 10, v => v.toFixed(1) + ' bps', true);
    setMicro('micro-depth', m.depth_usd_10bps, 5000, 1000, v => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }));
    setMicro('micro-imbalance', m.imbalance, 0.1, -0.1, v => (v >= 0 ? '+' : '') + v.toFixed(3));
    setMicro('micro-slippage', m.slippage_bps_est, 5, 20, v => v.toFixed(1) + ' bps', true);
    // Trade pressure + relative volume now stream live from the hub for the selected
    // symbol (computed from @trade / @kline / @ticker) — real values, no more 'unavail'.
    setMicro('micro-pressure', m.trade_pressure, 0.1, -0.1, v => (v >= 0 ? '+' : '') + v.toFixed(3));
    setMicro('micro-relvol', m.relative_volume, 1.5, 0.5, v => v.toFixed(1) + 'x');
}
async function updateMicrostructure(symbol) {
    if (!symbol) symbol = currentSymbol;
    if (!symbol) return;
    // When the live hub is streaming this symbol, the WS path (applyLiveMicro-
    // structure) owns ALL six cells with real-time values — skip the DB poll
    // entirely (it would flicker live values back to 'unavail'/DB on each tick).
    const liveActive = liveConfig.enabled && lastLiveSnap && lastLiveSnap.symbol === symbol;
    if (liveActive) return;
    try {
        const res = await fetch(`${API_URL}/market-features/${encodeURIComponent(symbol)}`);
        const d = await res.json();
        if (!d || d.error) {
            // Honest unavailability with a reason — not a silent n/a. The DB feature
            // row exists only for the core ACTIVE_SYMBOLS; for any other symbol that
            // isn't currently streaming, these are genuinely unavailable.
            setMicroUnavail('micro-spread', 'unavail', 'no live book — select this symbol to stream its bookTicker');
            setMicroUnavail('micro-depth', 'unavail', 'no live order book for this symbol (L2 only for the selected symbol)');
            setMicroUnavail('micro-imbalance', 'unavail', 'no live book/depth for this symbol');
            setMicroUnavail('micro-slippage', 'unavail', 'needs live depth — unavailable without the selected-symbol order book');
            setMicroUnavail('micro-pressure', 'unavail', 'select this symbol to stream its live trades (or no aggregated-trade feature row)');
            setMicroUnavail('micro-relvol', 'unavail', 'insufficient 24h volume history to compute relative volume');
            return;
        }
        setMicro('micro-spread', d.spread_bps, 3, 10, v => v.toFixed(1) + ' bps', true);
        setMicro('micro-depth', d.depth_usd_10bps, 5000, 1000, v => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }));
        setMicro('micro-imbalance', d.book_imbalance, 0.1, -0.1, v => (v >= 0 ? '+' : '') + v.toFixed(3));
        setMicro('micro-slippage', d.slippage_bps_est, 5, 20, v => v.toFixed(1) + ' bps', true);
        setMicro('micro-pressure', d.trade_pressure, 0.1, -0.1, v => (v >= 0 ? '+' : '') + v.toFixed(3));
        setMicro('micro-relvol', d.relative_volume, 1.5, 0.5, v => v.toFixed(1) + 'x');
    } catch (e) { /* silent */ }
}

// ── Live source debug (🔬 Source): raw Binance vs cockpit ──
function fmtNum(v, dp = 2) {
    if (v == null || Number.isNaN(v)) return 'n/a';
    return Number(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
async function openLiveDebug() {
    const modal = document.getElementById('live-debug-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    if (lastLiveSnap && lastLiveSnap.symbol === currentSymbol) renderLiveDebug(lastLiveSnap);
    else {
        try { const res = await fetch(`${API_URL}/binance/debug/${encodeURIComponent(currentSymbol)}`); renderLiveDebug(await res.json()); }
        catch (e) {}
    }
}
function renderLiveDebug(snap) {
    const summary = document.getElementById('live-debug-summary');
    const rows = document.getElementById('live-debug-rows');
    const formula = document.getElementById('ld-source-formula');
    if (!summary || !rows) return;
    if (!snap || snap.error || snap.displayed_price == null) {
        summary.innerHTML = `<span class="ld-kv">No live Binance data for <b>${currentSymbol}</b>. ${snap && snap.error ? snap.error : ''}</span>`;
        rows.innerHTML = '';
        if (formula) formula.textContent = '—';
        return;
    }
    const src = snap.price_source;
    const formulas = {
        trade: 'last @trade price (p)', aggTrade: 'last @aggTrade price (p)',
        ticker_last: '@ticker last price (c)', book_mid: '(@bookTicker bid + ask) / 2',
        kline_close: 'in-progress @kline close',
    };
    if (formula) formula.textContent = formulas[src] || src;
    summary.innerHTML = `
        <span class="ld-kv">source <b>${src}</b></span>
        <span class="ld-kv">status <b>${snap.feed_status}</b></span>
        <span class="ld-kv">latency <b>${fmtNum(snap.latency_ms, 0)} ms</b></span>
        <span class="ld-kv">staleness <b>${fmtNum(snap.data_age_ms, 0)} ms</b></span>
        <span class="ld-kv">event <b>${snap.event_time ? new Date(snap.event_time).toLocaleTimeString() : 'n/a'}</b></span>
        <span class="ld-kv">recv <b>${snap.local_receive_time ? new Date(snap.local_receive_time).toLocaleTimeString() : 'n/a'}</b></span>`;
    const r = snap.raw || {}, t = snap.ticker || {}, m = snap.micro || {}, candle = snap.candle || {};
    const klineTime = candle.time != null ? new Date(toChartTime(candle.time) * 1000).toLocaleTimeString() : 'n/a';
    const chartAge = chartStore.lastAppliedAt ? (Date.now() - chartStore.lastAppliedAt) : null;
    const defs = [
        ['raw_trade_price', r.trade_price, 'trade'],
        ['raw_agg_trade_price', r.agg_trade_price, 'aggTrade'],
        ['raw_ticker_last (c)', r.ticker_last, 'ticker_last'],
        ['raw_book_bid (b)', r.book_bid, null],
        ['raw_book_ask (a)', r.book_ask, null],
        ['book_mid', r.book_mid, 'book_mid'],
        ['raw_kline_close', r.kline_close, 'kline_close'],
        ['— displayed_price —', snap.displayed_price, '__displayed__'],
        ['spread', m.spread, null],
        ['spread_bps', m.spread_bps, null],
        ['depth_usd_10bps', m.depth_usd_10bps, null],
        ['imbalance', m.imbalance, null],
        ['24h change %', t.price_change_pct, null],
        ['24h high', t.high, null],
        ['24h low', t.low, null],
        ['24h vol (base)', t.volume_base, null],
        ['— chart feed —', null, '__hdr__'],
        ['chart_source', snap.chart_source || chartStore.source, '__str__'],
        ['chart_status (server)', snap.chart_status || 'n/a', '__str__'],
        ['candle interval', snap.candle_interval || candle.interval || chartStore.interval || 'n/a', '__str__'],
        ['last kline close', candle.close, null],
        ['last kline time', klineTime, '__str__'],
        ['candle_age_ms (server)', snap.candle_age_ms, null],
        ['kline_event_count (server)', snap.kline_event_count, null],
        ['server candle_count', snap.candle_count, null],
        ['chart displayed close', chartStore.lastClose, null],
        ['chart candles (local)', chartStore.count, null],
        ['chart applied age ms', chartAge, null],
    ];
    rows.innerHTML = defs.map(([label, val, key]) => {
        if (key === '__hdr__') return `<tr class="ld-hdr"><td colspan="2">${label}</td></tr>`;
        const active = (key === src) || (key === '__displayed__');
        let cell;
        if (key === '__str__') cell = escapeHtml(String(val));
        else { const dp = (label.includes('%') || label.includes('imbalance') || label.includes('bps')) ? 3 : 2; cell = fmtNum(val, dp); }
        return `<tr class="${active ? 'ld-active' : ''}"><td>${label}</td><td>${cell}</td></tr>`;
    }).join('');
}

// ── Activity Feed (bounded ring buffer) ─────
async function updateActivity() {
    try {
        const res = await fetch(`${API_URL}/trades/recent?limit=${LIMITS.maxEvents}`);
        const data = await res.json();
        if (!data || data.error) return;
        const container = document.getElementById('activity-container');
        container.innerHTML = '';
        data.slice(0, LIMITS.maxEvents).forEach(trade => {
            const div = document.createElement('div');
            div.className = 'feed-item';
            if (trade.decision_snapshot_id) div.addEventListener('click', () => openDrilldown(trade.decision_snapshot_id, trade.symbol));
            const time = new Date(trade.executed_at).toLocaleTimeString();
            const actionClass = trade.side === 'buy' ? 'buy' : 'sell';
            div.innerHTML = `
                <span class="feed-time">${time}</span>
                <span class="feed-action ${actionClass}">${trade.side}</span>
                <span class="feed-detail">${trade.symbol} — ${Number(trade.qty).toFixed(6)} @ $${Number(trade.price).toLocaleString('en-US', { minimumFractionDigits: 2 })}</span>
                <span class="feed-score">slip: ${Number(trade.slippage_bps).toFixed(1)}bps | ${trade.reason || ''}</span>`;
            container.appendChild(div);
        });
        if (!data.length) container.innerHTML = '<div class="feed-item"><span class="feed-detail" style="color:var(--text-muted)">No trades yet — bot is evaluating market conditions…</span></div>';
    } catch (e) { /* silent */ }
}

// ── Drilldown Modal ─────────────────────────
let waterfallChartInstance = null;
async function openDrilldown(decisionId, symbol) {
    const modal = document.getElementById('drilldown-modal');
    document.getElementById('drilldown-title').textContent = `Decision Drill-down: ${symbol} (#${decisionId})`;
    try {
        const res = await fetch(`${API_URL}/decision/${decisionId}`);
        const decision = await res.json();
        if (decision.error) { console.error('Decision error:', decision.error); return; }

        const scoresEl = document.getElementById('drilldown-scores');
        const snap = decision.snapshot;
        const actionColor = { buy: 'var(--up-color)', reinforce: 'var(--blue-color)', reduce: 'var(--warn-color)', exit: 'var(--down-color)', hold: 'var(--text-muted)' }[snap.action_proposed] || 'var(--text-primary)';
        const socReal = decision.social_available === true;
        const socHtml = socReal
            ? `<span class="dd-score-value" style="color:var(--purple-color)">${snap.s_social >= 0 ? '+' : ''}${snap.s_social.toFixed(2)}</span>`
            : `<span class="dd-score-value unavailable" title="No real social feed configured">n/a</span>`;
        scoresEl.innerHTML = `
            <div class="dd-score-item"><span class="dd-score-label">SOC</span>${socHtml}</div>
            <div class="dd-score-item"><span class="dd-score-label">MKT</span><span class="dd-score-value" style="color:var(--accent-color)">${snap.s_market >= 0 ? '+' : ''}${snap.s_market.toFixed(2)}</span></div>
            <div class="dd-score-item"><span class="dd-score-label">RSK</span><span class="dd-score-value" style="color:var(--warn-color)">${snap.s_risk.toFixed(2)}</span></div>
            <div class="dd-score-item"><span class="dd-score-label">Σ Total</span><span class="dd-score-value">${snap.s_total >= 0 ? '+' : ''}${snap.s_total.toFixed(2)}</span></div>
            <div class="dd-score-item"><span class="dd-score-label">Action</span><span class="dd-score-value" style="color:${actionColor};text-transform:uppercase">${snap.action_proposed}</span></div>
            <div class="dd-score-item"><span class="dd-score-label">Confidence</span><span class="dd-score-value">${snap.confidence_score ? (snap.confidence_score * 100).toFixed(0) + '%' : '--'}</span></div>`;

        const qualityEl = document.getElementById('drilldown-quality');
        const qa = decision.quality_audit;
        if (qa) {
            const reasons = qa.degradation_reasons && qa.degradation_reasons.length ? ` — ${qa.degradation_reasons.join(', ')}` : '';
            qualityEl.innerHTML = `<span class="quality-badge ${qa.quality_grade}">${qa.quality_grade}</span><span style="font-size:0.75rem;color:var(--text-muted)">Social: ${qa.has_sufficient_social ? '✓' : '✗'} | Market: ${qa.has_sufficient_market ? '✓' : '✗'}${reasons}</span>`;
        } else qualityEl.innerHTML = '<span class="quality-badge unavailable">No audit data</span>';

        const factors = decision.factors;
        const container = document.getElementById('factors-container');
        container.innerHTML = '';
        const labels = [], data = [], backgroundColors = [];
        factors.forEach(f => {
            const div = document.createElement('div');
            div.className = 'factor-item';
            const isPos = f.contribution >= 0;
            div.innerHTML = `
                <div class="factor-header">
                    <span class="factor-name"><span class="factor-category ${f.category}">${f.category}</span> ${f.name}</span>
                    <span class="factor-contrib ${isPos ? 'positive' : 'negative'}">${isPos ? '+' : ''}${f.contribution.toFixed(4)}</span>
                </div>
                <div class="factor-exp">Val: ${f.value.toFixed(3)} — ${f.explanation}</div>`;
            container.appendChild(div);
            labels.push(f.name); data.push(f.contribution);
            backgroundColors.push(isPos ? 'rgba(38, 194, 129, 0.8)' : 'rgba(240, 97, 109, 0.8)');
        });
        labels.push('S_total'); data.push(snap.s_total); backgroundColors.push('#00E5FF');

        const ctx = document.getElementById('waterfallChart').getContext('2d');
        if (waterfallChartInstance) waterfallChartInstance.destroy();
        waterfallChartInstance = new Chart(ctx, {
            type: 'bar',
            data: { labels, datasets: [{ label: 'Score Contribution', data, backgroundColor: backgroundColors, borderWidth: 0, borderRadius: 2 }] },
            options: {
                responsive: true,
                plugins: { legend: { display: false }, title: { display: true, text: 'Factor Contributions to S_total', color: '#8C97A8', font: { family: 'Inter' } } },
                scales: {
                    y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8C97A8', font: { family: 'Inter' } } },
                    x: { grid: { display: false }, ticks: { color: '#8C97A8', maxRotation: 45, minRotation: 45, font: { size: 10, family: 'Inter' } } }
                }
            }
        });

        renderDecisionSourceEvidence(
            document.getElementById('evidence-container'),
            decision.source_evidence,
            decision.evidence,
        );

        modal.classList.remove('hidden');
    } catch (e) { console.error("Drilldown error:", e); }
}

function escapeHtml(text) { const div = document.createElement('div'); div.textContent = text; return div.innerHTML; }

// ── Source Evidence (Decision Drill-down) ───────────────────
// Renders the backend `source_evidence` block: grouped, traceable, honest about
// stale/unavailable. Falls back to legacy social-only evidence if the new field
// is absent (backward compatible), and never crashes on null/empty shapes.
function evStatusClass(s) {
    if (s === 'available' || s === 'complete') return 'ev-ok';
    if (s === 'stale' || s === 'partial') return 'ev-warn';
    return 'ev-bad';
}
function fmtAgeMs(ms) {
    if (ms == null) return 'n/a';
    return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(0)}s`;
}
function renderDecisionSourceEvidence(container, ev, legacyEvidence) {
    if (!container) return;
    container.innerHTML = '';

    // Backward-compatible fallback: no structured evidence → legacy social list.
    if (!ev) {
        if (legacyEvidence && legacyEvidence.length) {
            legacyEvidence.forEach(e => {
                const div = document.createElement('div');
                div.className = 'evidence-item';
                const time = e.published_at ? new Date(e.published_at).toLocaleTimeString() : '';
                div.innerHTML = `<div class="evidence-header"><span><span class="evidence-author">${escapeHtml(e.author_handle || 'Unknown')}</span> via ${escapeHtml(e.source_name || 'unknown')}</span><span>${time}</span></div><div class="evidence-text">${escapeHtml(e.text || '')}</div>`;
                container.appendChild(div);
            });
            return;
        }
        container.innerHTML = '<div class="ev-empty">Evidence unavailable for this decision.</div>';
        return;
    }

    const head = document.createElement('div');
    head.className = 'ev-head';
    head.innerHTML = `<span class="ev-badge ${evStatusClass(ev.status)}">${escapeHtml(ev.status || 'unknown')}</span>`;
    container.appendChild(head);

    if (ev.status === 'missing') {
        const m = document.createElement('div');
        m.className = 'ev-empty';
        m.textContent = 'Evidence unavailable: no fresh market, risk or social source was found for this decision.';
        container.appendChild(m);
    }

    (ev.warnings || []).forEach(w => {
        const d = document.createElement('div');
        d.className = 'ev-warning';
        d.textContent = w;
        container.appendChild(d);
    });

    (ev.groups || []).forEach(g => {
        const card = document.createElement('div');
        card.className = 'ev-group';
        const meta = [
            g.provider ? `provider: ${g.provider}` : null,
            g.exchange_code ? `exchange: ${g.exchange_code}` : null,
            g.source_table ? `table: ${g.source_table}` : null,
            g.age_ms != null ? `age: ${fmtAgeMs(g.age_ms)}` : null,
        ].filter(Boolean).join(' · ');

        let html = `<div class="ev-group-head"><span class="ev-group-label">${escapeHtml(g.label || g.type || '')}</span><span class="ev-badge ${evStatusClass(g.status)}">${escapeHtml(g.status || '')}</span></div>`;
        if (meta) html += `<div class="ev-group-meta">${escapeHtml(meta)}</div>`;

        (g.metrics || []).forEach(mt => {
            const c = mt.score_contribution;
            const isPos = c >= 0;
            const contrib = c != null ? `${isPos ? '+' : ''}${Number(c).toFixed(4)}` : '';
            const val = mt.value != null ? Number(mt.value).toFixed(3) : 'n/a';
            html += `<div class="ev-metric"><div class="ev-metric-head"><span>${escapeHtml(mt.name || '')}</span><span class="${isPos ? 'positive' : 'negative'}">${contrib}</span></div><div class="ev-metric-exp">Val: ${val} — ${escapeHtml(mt.explanation || '')}</div></div>`;
        });

        (g.items || []).forEach(it => {
            const time = it.published_at ? new Date(it.published_at).toLocaleTimeString() : '';
            const rel = it.relevance_score != null ? ` · rel ${Number(it.relevance_score).toFixed(2)}` : '';
            html += `<div class="evidence-item"><div class="evidence-header"><span><span class="evidence-author">${escapeHtml(it.author_handle || 'Unknown')}</span> via ${escapeHtml(it.source_name || 'unknown')}${rel}</span><span>${time}</span></div><div class="evidence-text">${escapeHtml(it.text || '')}</div></div>`;
        });

        if (g.status === 'unavailable' && !(g.metrics || []).length && !(g.items || []).length) {
            const reason = g.reason ? ` — ${escapeHtml(g.reason)}` : '';
            const label = g.type === 'social' ? 'Social evidence unavailable' : 'Evidence unavailable';
            html += `<div class="ev-group-empty">${label}${reason}</div>`;
        }

        card.innerHTML = html;
        container.appendChild(card);
    });
}

document.getElementById('close-drilldown').addEventListener('click', () => document.getElementById('drilldown-modal').classList.add('hidden'));

async function openSignalDetail(symbol) {
    try {
        const res = await fetch(`${API_URL}/signals/${encodeURIComponent(symbol)}?limit=10`);
        const history = await res.json();
        if (!history || !history.length) return;
        openDrilldown(history[0].id, symbol);
    } catch (e) { console.error('Signal detail error:', e); }
}

// ── Timeline Modal ─────────────────────────
async function openTimeline() {
    const modal = document.getElementById('timeline-modal');
    const container = document.getElementById('timeline-container');
    container.innerHTML = '<div style="color:var(--text-muted);padding:1rem;">Loading decisions…</div>';
    modal.classList.remove('hidden');
    try {
        const allDecisions = [];
        for (const symbol of await getSymbolsList()) {
            const res = await fetch(`${API_URL}/signals/${encodeURIComponent(symbol)}?limit=20`);
            const data = await res.json();
            if (Array.isArray(data)) data.forEach(d => { d.symbol = symbol; allDecisions.push(d); });
        }
        allDecisions.sort((a, b) => new Date(b.ts_eval) - new Date(a.ts_eval));
        container.innerHTML = '';
        if (!allDecisions.length) { container.innerHTML = '<div style="color:var(--text-muted);padding:1rem;">No decisions recorded yet.</div>'; return; }
        allDecisions.slice(0, 50).forEach((d, i) => {
            const div = document.createElement('div');
            div.className = 'timeline-item';
            div.style.cursor = 'pointer';
            div.addEventListener('click', () => { modal.classList.add('hidden'); openDrilldown(d.id, d.symbol); });
            const action = d.action_proposed || 'hold';
            const time = new Date(d.ts_eval).toLocaleString();
            const reasonText = explainReason(d.reason_code, d.s_total);
            div.innerHTML = `
                <div class="timeline-marker"><div class="timeline-dot ${action}"></div>${i < allDecisions.length - 1 ? '<div class="timeline-line"></div>' : ''}</div>
                <div class="timeline-body">
                    <div class="timeline-action" style="color:${getActionColor(action)}">${action} — ${d.symbol}</div>
                    <div class="timeline-reason">${reasonText}</div>
                    <div class="timeline-scores">
                        ${d.social_available
                            ? `<span class="score-badge social" style="font-size:0.6rem">SOC ${d.s_social >= 0 ? '+' : ''}${d.s_social.toFixed(2)}</span>`
                            : `<span class="score-badge social unavailable" style="font-size:0.6rem" title="No real social feed configured">SOC n/a</span>`}
                        <span class="score-badge market" style="font-size:0.6rem">MKT ${d.s_market >= 0 ? '+' : ''}${d.s_market.toFixed(2)}</span>
                        <span class="score-badge risk" style="font-size:0.6rem">RSK ${d.s_risk.toFixed(2)}</span>
                        <span class="score-badge total" style="font-size:0.6rem">Σ ${d.s_total >= 0 ? '+' : ''}${d.s_total.toFixed(2)}</span>
                    </div>
                    <div class="timeline-time">${time}${d.quality_grade ? ' — Quality: ' + d.quality_grade : ''}</div>
                </div>`;
            container.appendChild(div);
        });
    } catch (e) { console.error('Timeline error:', e); container.innerHTML = '<div style="color:var(--down-color);padding:1rem;">Failed to load timeline.</div>'; }
}
function getActionColor(action) { return ({ buy: '#26C281', reinforce: '#5B9DF9', reduce: '#FFB02E', exit: '#F0616D', hold: '#8C97A8' })[action] || '#8C97A8'; }
function explainReason(reasonCode, sTotal) {
    const s = (typeof sTotal === 'number') ? `${sTotal >= 0 ? '+' : ''}${sTotal.toFixed(2)}` : '--';
    if (!reasonCode) return `S_total ${s}`;
    if (reasonCode.startsWith('risk_gate:')) return `Forced HOLD by risk gate: ${reasonCode.slice('risk_gate:'.length)} (S_total ${s} overridden)`;
    const map = {
        s_total_reinforce: `Reinforce — S_total ${s} ≥ +0.60`, s_total_buy: `Buy — S_total ${s} ≥ +0.30`,
        s_total_reduce: `Reduce — S_total ${s} ≤ −0.30`, s_total_exit: `Exit — S_total ${s} ≤ −0.60`,
        hold_neutral: `Hold — S_total ${s} in neutral band (−0.30, +0.30)`,
    };
    return map[reasonCode] || `${reasonCode} (S_total ${s})`;
}
async function getSymbolsList() {
    try { const res = await fetch(`${API_URL}/symbols`); const data = await res.json(); return data.symbols || []; }
    catch (e) { return []; }
}
document.getElementById('timeline-btn').addEventListener('click', openTimeline);
document.getElementById('close-timeline').addEventListener('click', () => document.getElementById('timeline-modal').classList.add('hidden'));

// ── Docs Modal ─────────────────────────────
document.getElementById('docs-btn').addEventListener('click', async () => {
    try {
        const res = await fetch(`${API_URL}/docs/signals-sentiments`);
        const data = await res.json();
        document.getElementById('docs-container').innerHTML = marked.parse(data.content);
        document.getElementById('docs-modal').classList.remove('hidden');
    } catch (e) { console.error("Docs load error:", e); }
});
document.getElementById('close-docs').addEventListener('click', () => document.getElementById('docs-modal').classList.add('hidden'));

// ── Logging System ─────────────────────────
async function updateSystemLogs() {
    try {
        const res = await fetch(`${API_URL}/system/logs?limit=50`);
        const logs = await res.json();
        const logsContainer = document.getElementById('logs-container');
        logsContainer.innerHTML = '';
        logs.forEach(log => {
            const entry = document.createElement('div');
            entry.className = `sys-log-entry ${log.level}`;
            const time = new Date(log.ts_event).toLocaleTimeString();
            let imgHtml = '';
            if (log.metadata && log.metadata.screenshot_path) imgHtml = `<img src="${log.metadata.screenshot_path}" class="sys-log-screenshot" alt="Screenshot" onclick="window.open(this.src, '_blank')">`;
            entry.innerHTML = `
                <div class="sys-log-header"><span class="sys-log-component">[${log.component}]</span><span class="sys-log-time">${time}</span></div>
                <div class="sys-log-message">${log.message}</div>${imgHtml}`;
            logsContainer.appendChild(entry);
        });
    } catch (e) { console.error("Failed to fetch system logs:", e); }
}
function setupLogging() {
    const logsModal = document.getElementById('logs-modal');
    const logsBtn = document.getElementById('logs-btn');
    const closeBtn = document.getElementById('close-logs');
    logsBtn.addEventListener('click', () => { logsModal.classList.remove('hidden'); updateSystemLogs(); });
    closeBtn.addEventListener('click', () => logsModal.classList.add('hidden'));
    logsModal.addEventListener('click', (e) => { if (e.target === logsModal) logsModal.classList.add('hidden'); });
    setInterval(() => { if (!logsModal.classList.contains('hidden')) updateSystemLogs(); }, 5000);
}

// ── Ops / Terminals ────────────────────────
const OPS_BASE = window.OPS_URL || (`http://${window.location.hostname}:${window.OPS_PORT || 8050}`);
const OPS_WS_URL = OPS_BASE.replace(/^http/, 'ws') + '/ws/ops';
const opsState = { ws: null, logs: [], filterProcess: '', filterLevel: '', open: false, knownProcs: new Set() };
const LEVEL_RANK = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 };

async function loadOpsStatus() {
    try {
        const res = await fetch(`${OPS_BASE}/api/ops/status`);
        const st = await res.json();
        renderOpsHeaderBadge(st);
        if (opsState.open) renderOpsProcesses(st.processes || []);
        return st;
    } catch (e) { renderOpsHeaderBadge(null); if (opsState.open) showOpsUnavailable(); return null; }
}
function showOpsUnavailable() {
    const c = document.getElementById('ops-processes');
    if (!c) return;
    c.innerHTML = `
        <div class="ops-unavailable">
            <div class="ops-unavailable-title">⚠ Ops API unavailable</div>
            <div>Pas de réponse de <code>${OPS_BASE}</code>.</div>
            <div>Lance le supervisor :</div>
            <pre>$env:PYTHONPATH="."; python .\\scripts\\dev_supervisor.py</pre>
            <div>ou via VS Code : <b>Terminal → Run Task… → Start Dev Supervisor</b>.</div>
            <div class="ops-unavailable-hint">URL configurable via <code>window.OPS_URL</code>.</div>
        </div>`;
    const conn = document.getElementById('ops-conn');
    if (conn) { conn.textContent = 'stream offline'; conn.className = 'status-badge disconnected'; }
}
function renderOpsHeaderBadge(st) {
    const badge = document.getElementById('ops-status');
    if (!badge) return;
    if (!st) { badge.textContent = 'Ops down'; badge.className = 'status-badge disconnected'; return; }
    const cls = st.status === 'ok' ? 'connected' : st.status === 'degraded' ? 'stale' : 'disconnected';
    badge.textContent = `Ops ${st.running}/${st.total}`;
    badge.className = 'status-badge ' + cls;
}
function renderOpsProcesses(procs) {
    const c = document.getElementById('ops-processes');
    if (!c) return;
    c.innerHTML = '';
    const sel = document.getElementById('ops-filter-process');
    procs.forEach(p => {
        if (!opsState.knownProcs.has(p.name)) { opsState.knownProcs.add(p.name); const o = document.createElement('option'); o.value = p.name; o.textContent = p.name; sel.appendChild(o); }
        const card = document.createElement('div');
        card.className = 'ops-proc ' + p.status;
        const uptime = p.uptime_s != null ? fmtUptime(p.uptime_s) : '—';
        const tb = p.last_traceback ? `<pre class="ops-tb">${escapeHtml(p.last_traceback)}</pre>` : '';
        card.innerHTML = `
            <div class="ops-proc-head"><span class="ops-proc-name">${p.name}</span><span class="ops-proc-status ${p.status}">${p.status}</span></div>
            <div class="ops-proc-meta"><span>PID ${p.pid ?? '—'}</span><span>up ${uptime}</span><span>restarts ${p.restarts}</span>${p.exit_code != null ? `<span>exit ${p.exit_code}</span>` : ''}</div>
            <div class="ops-proc-lastlog ${(p.last_log_level || 'INFO').toLowerCase()}">${escapeHtml(p.last_log || '')}</div>
            ${tb}
            <div class="ops-proc-actions">
                <button class="log-btn" data-act="start" data-proc="${p.name}">Start</button>
                <button class="log-btn" data-act="restart" data-proc="${p.name}">Restart</button>
                <button class="log-btn" data-act="stop" data-proc="${p.name}">Stop</button>
            </div>`;
        card.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', () => controlOpsProcess(b.dataset.proc, b.dataset.act)));
        c.appendChild(card);
    });
}
function fmtUptime(s) { s = Math.floor(s); if (s < 60) return s + 's'; if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's'; return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm'; }
async function controlOpsProcess(name, action) {
    try { await fetch(`${OPS_BASE}/api/ops/process/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); setTimeout(loadOpsStatus, 400); }
    catch (e) { console.error('Ops control error:', e); }
}
function connectOpsWS() {
    if (opsState.ws) { try { opsState.ws.close(); } catch (e) {} }
    const conn = document.getElementById('ops-conn');
    let wsx;
    try { wsx = new WebSocket(OPS_WS_URL); } catch (e) { if (conn) { conn.textContent = 'stream offline'; conn.className = 'status-badge disconnected'; } return; }
    opsState.ws = wsx;
    wsx.onopen = () => { if (conn) { conn.textContent = 'streaming'; conn.className = 'status-badge connected'; } };
    wsx.onclose = () => { if (conn) { conn.textContent = 'stream offline'; conn.className = 'status-badge disconnected'; } if (opsState.open) setTimeout(connectOpsWS, 3000); };
    wsx.onmessage = (ev) => {
        let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === 'snapshot' && msg.status) { renderOpsHeaderBadge(msg.status); renderOpsProcesses(msg.status.processes || []); }
        else if (msg.type === 'status') loadOpsStatus();
        else if (msg.type === 'log') pushOpsLog(msg);
        else if (msg.type === 'incident') { pushOpsLog({ process: msg.process, level: 'CRITICAL', stream: 'incident', message: `INCIDENT [${msg.severity}] ${msg.incident.suspected_root_cause}` }); loadOpsStatus(); }
    };
}
function pushOpsLog(evt) {
    opsState.logs.push(evt);
    if (opsState.logs.length > LIMITS.maxLogs) opsState.logs.shift();
    if (opsState.open) appendOpsLogLine(evt);
}
function logPassesFilter(evt) {
    if (opsState.filterProcess && evt.process !== opsState.filterProcess) return false;
    if (opsState.filterLevel) { const r = LEVEL_RANK[(evt.level || 'INFO').toUpperCase()] ?? 1; if (r < LEVEL_RANK[opsState.filterLevel]) return false; }
    return true;
}
function appendOpsLogLine(evt) {
    if (!logPassesFilter(evt)) return;
    const box = document.getElementById('ops-logs');
    if (!box) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const line = document.createElement('div');
    line.className = 'ops-log-line ' + (evt.level || 'INFO').toLowerCase();
    const t = new Date((evt.ts ? evt.ts * 1000 : Date.now())).toLocaleTimeString();
    line.innerHTML = `<span class="ops-log-t">${t}</span><span class="ops-log-p">${evt.process}</span><span class="ops-log-lvl">${evt.level || ''}</span><span class="ops-log-m">${escapeHtml(evt.message || '')}</span>`;
    box.appendChild(line);
    while (box.childElementCount > LIMITS.maxLogs) box.removeChild(box.firstChild);
    if (atBottom) box.scrollTop = box.scrollHeight;
}
function renderOpsLogs() {
    const box = document.getElementById('ops-logs');
    if (!box) return;
    box.innerHTML = '';
    opsState.logs.filter(logPassesFilter).slice(-LIMITS.maxLogs).forEach(appendOpsLogLine);
    box.scrollTop = box.scrollHeight;
}
function setupOps() {
    const modal = document.getElementById('ops-modal');
    const btn = document.getElementById('ops-btn');
    if (!modal || !btn) return;
    btn.addEventListener('click', () => { opsState.open = true; modal.classList.remove('hidden'); loadOpsStatus(); renderOpsLogs(); connectOpsWS(); });
    document.getElementById('close-ops').addEventListener('click', () => { opsState.open = false; modal.classList.add('hidden'); if (opsState.ws) { try { opsState.ws.close(); } catch (e) {} opsState.ws = null; } });
    document.getElementById('ops-filter-process').addEventListener('change', e => { opsState.filterProcess = e.target.value; renderOpsLogs(); });
    document.getElementById('ops-filter-level').addEventListener('change', e => { opsState.filterLevel = e.target.value; renderOpsLogs(); });
    document.getElementById('ops-clear-logs').addEventListener('click', () => { opsState.logs = []; renderOpsLogs(); });
    loadOpsStatus();
    setInterval(loadOpsStatus, 5000);
    setupFrontendErrorReporter();
}
let _opsReportInFlight = false;
function reportFrontendError(message, stack) {
    if (_opsReportInFlight) return;
    _opsReportInFlight = true;
    fetch(`${OPS_BASE}/api/ops/frontend-error`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, stack, url: window.location.href, ts: Date.now() }) }).catch(() => {}).finally(() => { _opsReportInFlight = false; });
}
function setupFrontendErrorReporter() {
    window.addEventListener('error', (e) => reportFrontendError(e.message || 'window.error', e.error && e.error.stack));
    window.addEventListener('unhandledrejection', (e) => { const r = e.reason || {}; reportFrontendError('unhandledrejection: ' + (r.message || String(r)), r.stack); });
}

// ── Right decision-intelligence panel drawer (narrow screens) ───────────────
// On wide screens the panel lives in the grid; below 1100px it becomes an
// off-canvas drawer toggled from the header (CSS owns the media query).
function setupRightDrawer() {
    const toggle = document.getElementById('toggle-right');
    const closeBtn = document.getElementById('close-right');
    const backdrop = document.getElementById('drawer-backdrop');
    const close = () => document.body.classList.remove('right-open');
    if (toggle) toggle.addEventListener('click', () => document.body.classList.toggle('right-open'));
    if (closeBtn) closeBtn.addEventListener('click', close);
    if (backdrop) backdrop.addEventListener('click', close);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
}

// ── Live source debug modal wiring ─────────
(function setupLiveDebug() {
    const btn = document.getElementById('live-debug-btn');
    const close = document.getElementById('close-live-debug');
    const modal = document.getElementById('live-debug-modal');
    if (btn) btn.addEventListener('click', openLiveDebug);
    if (close) close.addEventListener('click', () => modal.classList.add('hidden'));
    if (modal) modal.addEventListener('click', e => { if (e.target === modal) modal.classList.add('hidden'); });
})();

// ── Init ───────────────────────────────────
// ── Macro bar (global market context) ────────────────────────────────────────
// Real data only: each macro source carries real/stale flags. A source that has
// never answered renders "n/a" (never a fabricated number); a stale value renders
// dimmed. The whole bar hides when the global-context hub is disabled.
function fmtUsdCompact(n) {
    if (n == null || !Number.isFinite(Number(n))) return null;
    n = Number(n);
    const a = Math.abs(n);
    if (a >= 1e12) return '$' + (n / 1e12).toFixed(2) + 'T';
    if (a >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B';
    if (a >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M';
    return '$' + n.toFixed(0);
}

function setMacroCell(id, text, opts = {}) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = (text == null || text === '') ? 'n/a' : text;
    el.classList.toggle('na', text == null || text === '');
    el.classList.toggle('stale', !!opts.stale);
    el.classList.remove('up', 'down');
    if (opts.cls) el.classList.add(opts.cls);
}

async function fetchGlobalContext() {
    const bar = document.getElementById('macro-bar');
    if (!bar) return;
    if (liveConfig.global_context_enabled === false) { bar.style.display = 'none'; return; }

    let snap = null;
    try {
        const res = await fetch(`${API_URL}/market/global`);
        snap = await res.json();
    } catch (e) { /* keep last paint; cells stay as-is */ return; }
    if (!snap || snap.enabled === false) { bar.style.display = 'none'; return; }
    bar.style.display = '';

    const mkt = snap.market || {}, defi = snap.defi || {}, sent = snap.sentiment || {};
    const live = [];

    // CoinGecko macro
    if (mkt.real) {
        setMacroCell('macro-mcap', fmtUsdCompact(mkt.total_market_cap_usd), { stale: mkt.stale });
        setMacroCell('macro-vol', fmtUsdCompact(mkt.total_volume_usd), { stale: mkt.stale });
        setMacroCell('macro-btc-dom', mkt.btc_dominance != null ? mkt.btc_dominance.toFixed(1) + '%' : null, { stale: mkt.stale });
        setMacroCell('macro-eth-dom', mkt.eth_dominance != null ? mkt.eth_dominance.toFixed(1) + '%' : null, { stale: mkt.stale });
        const chg = mkt.market_cap_change_24h_pct;
        setMacroCell('macro-mcap-chg', chg != null ? fmtPct(chg) : null,
            { stale: mkt.stale, cls: chgClass(chg) });
        live.push('CoinGecko');
    } else {
        ['macro-mcap', 'macro-vol', 'macro-btc-dom', 'macro-eth-dom', 'macro-mcap-chg'].forEach(id => setMacroCell(id, null));
    }

    // DefiLlama TVL
    if (defi.real) {
        setMacroCell('macro-defi-tvl', fmtUsdCompact(defi.defi_tvl_usd), { stale: defi.stale });
        live.push('DefiLlama');
    } else {
        setMacroCell('macro-defi-tvl', null);
    }

    // Fear & Greed sentiment
    if (sent.real && sent.value != null) {
        const v = Math.round(sent.value);
        const cls = v < 45 ? 'down' : v > 55 ? 'up' : '';
        setMacroCell('macro-fng', `${v} · ${sent.classification || ''}`.trim(), { stale: sent.stale, cls });
        live.push('Fear&Greed');
    } else {
        setMacroCell('macro-fng', null);
    }

    const srcEl = document.getElementById('macro-source');
    if (srcEl) srcEl.textContent = live.length ? live.join(' · ') : 'no source';
}

// ── DeFi modal (top protocols by TVL — DefiLlama, real data only) ─────────────
function setupDefi() {
    const modal = document.getElementById('defi-modal');
    const btn = document.getElementById('defi-btn');
    const close = document.getElementById('close-defi');
    if (!modal || !btn) return;
    btn.addEventListener('click', () => { modal.classList.remove('hidden'); fetchDefi(); });
    if (close) close.addEventListener('click', () => modal.classList.add('hidden'));
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.classList.add('hidden'); });
}

async function fetchDefi() {
    const statusEl = document.getElementById('defi-status');
    const rowsEl = document.getElementById('defi-rows');
    const summaryEl = document.getElementById('defi-summary');
    const catsEl = document.getElementById('defi-categories');
    if (!rowsEl) return;

    let snap = null;
    try {
        const res = await fetch(`${API_URL}/market/defi?limit=50`);
        snap = await res.json();
    } catch (e) {
        if (statusEl) { statusEl.textContent = 'unavailable'; statusEl.className = 'status-badge disconnected'; }
        rowsEl.innerHTML = `<tr><td colspan="7" class="defi-empty">DeFi source unavailable (network)</td></tr>`;
        return;
    }

    const protocols = (snap && Array.isArray(snap.protocols)) ? snap.protocols : [];
    const connected = !!(snap && snap.connected);
    if (statusEl) {
        if (snap && snap.enabled === false) { statusEl.textContent = 'disabled'; statusEl.className = 'status-badge disconnected'; }
        else if (connected && protocols.length) {
            statusEl.textContent = snap.stale ? 'DefiLlama · stale' : 'DefiLlama · live';
            statusEl.className = `status-badge ${snap.stale ? 'disconnected' : 'connected'}`;
        } else { statusEl.textContent = 'waiting data'; statusEl.className = 'status-badge disconnected'; }
    }

    if (summaryEl) {
        // Real-data-only: require count>0 AND a non-null total — a 0/empty set is never
        // shown as a "$0 TVL" reading (the backend already publishes null in that case).
        if (connected && snap.count > 0 && snap.total_tracked_tvl_usd != null) {
            const ageS = snap.age_ms != null ? Math.round(snap.age_ms / 1000) + 's' : '—';
            summaryEl.textContent = `${snap.count} protocoles · TVL suivie ${fmtUsdCompact(snap.total_tracked_tvl_usd)} · maj ${ageS}`;
        } else summaryEl.textContent = snap && snap.enabled === false ? 'Hub DeFi désactivé.' : 'En attente de données DefiLlama…';
    }

    if (catsEl) {
        // Category names come from the external DefiLlama API → escape before innerHTML.
        const cats = (snap && Array.isArray(snap.categories)) ? snap.categories : [];
        catsEl.innerHTML = cats.map(c =>
            `<span class="defi-cat"><b>${escapeHtml(c.category)}</b> ${fmtUsdCompact(c.tvl_usd) ?? 'n/a'} <i>(${Number(c.count) || 0})</i></span>`
        ).join('');
    }

    if (!protocols.length) {
        rowsEl.innerHTML = `<tr><td colspan="7" class="defi-empty">${connected ? 'No protocols above the TVL floor' : 'No real DeFi data yet'}</td></tr>`;
        return;
    }
    // All string fields below (name, symbol, category, chains) are external API data —
    // escape every one before injecting into innerHTML (XSS guard). Numbers go through
    // our own formatters.
    rowsEl.innerHTML = protocols.map(p => {
        const chainList = Array.isArray(p.chains) ? p.chains : [];
        const first = chainList[0] ? escapeHtml(chainList[0]) : '—';
        const chains = (p.chains_count > 1) ? `${first} +${p.chains_count - 1}` : first;
        const sym = p.symbol ? ` <span class="defi-sym">${escapeHtml(p.symbol)}</span>` : '';
        return `<tr>
            <td class="defi-rank">${Number(p.rank) || ''}</td>
            <td class="defi-name">${escapeHtml(p.name)}${sym}</td>
            <td>${p.category ? escapeHtml(p.category) : '—'}</td>
            <td title="${escapeHtml(chainList.join(', '))}">${chains}</td>
            <td class="defi-tvl">${fmtUsdCompact(p.tvl_usd) ?? 'n/a'}</td>
            <td class="${chgClass(p.change_1d)}">${p.change_1d != null ? fmtPct(p.change_1d) : '—'}</td>
            <td class="${chgClass(p.change_7d)}">${p.change_7d != null ? fmtPct(p.change_7d) : '—'}</td>
        </tr>`;
    }).join('');
}

document.addEventListener('DOMContentLoaded', async () => {
    setupLogging();
    setupOps();
    setupDefi();
    startFeedWatchdog();
    startChartWatchdog();
    initChart();
    setupWatchlistControls();
    setupRangeControls();
    setupFavCurrent();
    setupRightDrawer();

    await loadLiveConfig();          // ranges, limits, sources (needed first)
    // Core overlay and the 300-row universe are independent — fetch them IN PARALLEL
    // so the universe no longer waits behind the watchlist round-trip. Combined with
    // the backend painting the universe from the single 24h call (exchangeInfo loads
    // off the critical path), the full list appears in seconds, not >10s.
    await Promise.all([updateCoreWatchlist(), fetchUniverse()]);

    const def = liveConfig.active_symbol || (liveConfig.symbols && liveConfig.symbols[0]) ||
        (universe.rows[0] && universe.rows[0].symbol);
    if (def) switchSymbol(def);

    updatePortfolio();
    updateSignals();
    updateActivity();
    fetchGlobalContext();

    // Periodic refreshes (throttled / bounded to keep memory + CPU low).
    setInterval(updatePortfolio, 5000);
    setInterval(updateCoreWatchlist, 10000);
    setInterval(fetchUniverse, Math.max(2000, LIMITS.snapshotMs));
    setInterval(updateSignals, 10000);
    setInterval(updateActivity, 8000);
    setInterval(() => updateMicrostructure(), 5000);
    // Macro context updates slowly (server polls ~every 60s); 30s client poll is ample.
    setInterval(fetchGlobalContext, 30000);
});
