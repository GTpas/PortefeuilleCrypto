/* ============================================
   ANTIGRAVITY COCKPIT — Frontend Logic
   Enhanced with explainable decision views,
   microstructure display, timeline, tooltips,
   and evidence drilldown.
   ============================================ */

const API_URL = `http://${window.location.host}/api`;
const WS_URL = `ws://${window.location.host}/ws/live`;

let chart, candlestickSeries, volumeSeries, ws;
let currentSymbol = '';

// Binance live-layer config (price/candle source). Fetched once on load so the
// cockpit knows whether to draw Binance klines and which source the big number is.
let liveConfig = { enabled: false, price_source: 'trade', candle_source: 'derived_trades', candle_interval: '1m', max_age_ms: 3000 };
let lastLiveSnap = null;  // most recent /ws/live "live" payload, shown in the 🔬 Source panel

async function loadLiveConfig() {
    try {
        const res = await fetch(`${API_URL}/binance/config`);
        const cfg = await res.json();
        if (cfg && !cfg.error) liveConfig = cfg;
    } catch (e) { /* keep defaults; chart falls back to derived OHLCV */ }
    updateChartSourceBadge();
}

function updateChartSourceBadge() {
    const el = document.getElementById('chart-source-badge');
    if (!el) return;
    if (liveConfig.enabled) {
        el.textContent = `${liveConfig.price_source} · ${liveConfig.candle_interval}`;
        el.className = 'source-badge';
        el.title = `Displayed price = Binance Spot ${liveConfig.price_source} · chart ${liveConfig.candle_source} ${liveConfig.candle_interval}`;
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
        layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#8B94A5', fontFamily: 'Inter' },
        grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.03)' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: 'rgba(255,255,255,0.1)' },
        timeScale: { borderColor: 'rgba(255,255,255,0.1)', timeVisible: true, secondsVisible: true },
    });

    candlestickSeries = chart.addCandlestickSeries({
        upColor: '#26A69A', downColor: '#EF5350', borderVisible: false,
        wickUpColor: '#26A69A', wickDownColor: '#EF5350',
    });
    volumeSeries = chart.addHistogramSeries({
        color: '#26a69a', priceFormat: { type: 'volume' }, priceScaleId: '',
        scaleMargins: { top: 0.8, bottom: 0 },
    });

    new ResizeObserver(entries => {
        if (!entries.length || entries[0].target !== el) return;
        const r = entries[0].contentRect;
        chart.applyOptions({ height: r.height, width: r.width });
    }).observe(el);
}

// ── Symbols ────────────────────────────────

async function loadSymbols() {
    try {
        const res = await fetch(`${API_URL}/symbols`);
        const data = await res.json();
        const sel = document.getElementById('symbol-select');
        sel.innerHTML = '';
        data.symbols.forEach(s => {
            const o = document.createElement('option');
            o.value = s; o.textContent = s;
            sel.appendChild(o);
        });
        if (data.symbols.length > 0) {
            sel.value = data.symbols[0];
            switchSymbol(data.symbols[0]);
        }
        sel.addEventListener('change', e => switchSymbol(e.target.value));
    } catch (e) { console.error('Symbol load error:', e); }
}

// ── Switch Symbol ──────────────────────────

async function switchSymbol(symbol) {
    if (ws) ws.close();
    currentSymbol = symbol;
    document.getElementById('current-symbol').textContent = symbol;
    candlestickSeries.setData([]);
    volumeSeries.setData([]);

    // Highlight active watchlist item
    document.querySelectorAll('.watchlist-item').forEach(el => {
        el.classList.toggle('active', el.dataset.symbol === symbol);
    });

    await loadHistorical(symbol);
    connectWebSocket(symbol);
    updateMicrostructure(symbol);
}

// ── Historical Data ────────────────────────

async function loadHistorical(symbol) {
    try {
        let data;
        // Prefer real Binance klines (matches Binance UI at the configured interval).
        if (liveConfig.enabled && liveConfig.candle_source === 'binance_kline') {
            const kres = await fetch(`${API_URL}/binance/klines/${encodeURIComponent(symbol)}`);
            const kjson = await kres.json();
            data = (kjson && kjson.candles) ? kjson.candles : [];
            if (!data.length) {
                // hub not warmed up yet → fall back to derived OHLCV so the chart isn't blank
                const res = await fetch(`${API_URL}/historical/${encodeURIComponent(symbol)}`);
                data = await res.json();
            }
        } else {
            const res = await fetch(`${API_URL}/historical/${encodeURIComponent(symbol)}`);
            data = await res.json();
        }
        if (!data || !data.length) return;

        const seen = new Set(), unique = [];
        data.forEach(d => { if (!seen.has(d.time)) { seen.add(d.time); unique.push(d); } });
        unique.sort((a, b) => a.time - b.time);

        try {
            candlestickSeries.setData(unique.map(d => ({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close })));
            volumeSeries.setData(unique.map(d => ({ time: d.time, value: d.value || 0, color: d.close >= d.open ? 'rgba(38,166,154,0.5)' : 'rgba(239,83,80,0.5)' })));
        } catch (e) { console.error('SetData error:', e); }

        const last = data[data.length - 1];
        updateCurrentPrice(last.close, last.close >= last.open);
    } catch (e) { console.error('Historical error:', e); }
}

// ── WebSocket ──────────────────────────────

// Market feed state — keeps WS-connectivity SEPARATE from data-received so the
// badge never shows "Live" without real candles. States:
//   offline      → socket down
//   connecting   → socket opening
//   waiting      → socket open, no data yet (or none for a while)
//   nodata       → server: WS connected but NO real Binance event received yet
//   live         → fresh real Binance event flowing (< BINANCE_LIVE_MAX_AGE_MS)
//   stale        → real data exists but stopped advancing (> max age)
//   mock         → data came from a mock/simulated source (never for Binance price)
// The badge separates four facts the user asked for: socket connected, Binance
// data received, data fresh, and source real-vs-mock.
const WAIT_DATA_MS = 6000;            // socket open but silent this long → "Waiting data"
const feedState = { connected: false, gotData: false, lastMsgAt: 0, symbol: '' };
let feedWatchdog = null;

function setFeedBadge(state, ageMs) {
    const badge = document.getElementById('ws-status');
    if (!badge) return;
    const map = {
        offline:    ['Offline',      'disconnected'],
        connecting: ['Connecting…',  ''],
        waiting:    ['Waiting data', 'stale'],
        nodata:     ['No data',      'disconnected'],
        live:       ['Live',         'connected'],
        stale:      ['STALE' + (typeof ageMs === 'number' ? ` ${Math.round(ageMs / 1000)}s` : ''), 'stale'],
        mock:       ['MOCK',         'mock'],
    };
    const [text, cls] = map[state] || map.offline;
    badge.textContent = text;
    badge.className = 'status-badge ' + cls;
    badge.dataset.state = state;
}

// Map the server's authoritative feed_status onto a badge state.
function badgeFromFeedStatus(feedStatus) {
    return ({ live: 'live', stale: 'stale', nodata: 'nodata', mock: 'mock' })[feedStatus] || 'waiting';
}

function connectWebSocket(symbol) {
    feedState.connected = false; feedState.gotData = false; feedState.symbol = symbol;
    setFeedBadge('connecting');

    ws = new WebSocket(`${WS_URL}/${encodeURIComponent(symbol)}`);

    ws.onopen = () => {
        feedState.connected = true;
        feedState.lastMsgAt = Date.now();
        // Connected but no data confirmed yet — explicitly NOT "Live".
        setFeedBadge('waiting');
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            feedState.lastMsgAt = Date.now();

            if (msg.type === 'nodata') {
                feedState.gotData = false;
                setFeedBadge('nodata');
                updateCurrentPriceUnavailable();
                return;
            }

            // ── Binance Spot live payload (real-time, source-pinned) ──
            if (msg.type === 'live') {
                lastLiveSnap = msg;
                const price = msg.displayed_price;
                if (price == null) { setFeedBadge('nodata'); updateCurrentPriceUnavailable(); return; }
                feedState.gotData = true;
                // Direction from the in-progress candle if present, else 24h change sign.
                const isUp = msg.candle ? (msg.candle.close >= msg.candle.open)
                          : (msg.ticker ? msg.ticker.price_change >= 0 : true);
                updateCurrentPrice(price, isUp);
                if (msg.candle && msg.candle.time != null) {
                    try {
                        const c = msg.candle;
                        candlestickSeries.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
                        volumeSeries.update({ time: c.time, value: c.value || 0, color: c.close >= c.open ? 'rgba(38,166,154,0.5)' : 'rgba(239,83,80,0.5)' });
                    } catch (e) { /* chart update error, ignore */ }
                }
                if (msg.micro) applyLiveMicrostructure(msg.micro);
                if (document.getElementById('live-debug-modal') && !document.getElementById('live-debug-modal').classList.contains('hidden')) {
                    renderLiveDebug(msg);
                }
                setFeedBadge(badgeFromFeedStatus(msg.feed_status), msg.data_age_ms);
                return;
            }

            // ── Fallback: DB-derived candle ──
            if (msg.type !== 'candle') return;
            const d = msg.data;
            if (!d || d.time == null || d.open == null || d.close == null) return;
            feedState.gotData = true;
            try {
                candlestickSeries.update({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close });
                volumeSeries.update({ time: d.time, value: d.value || 0, color: d.close >= d.open ? 'rgba(38,166,154,0.5)' : 'rgba(239,83,80,0.5)' });
            } catch (e) { /* chart update error, ignore silently */ }
            updateCurrentPrice(d.close, d.close >= d.open);
            // Honest freshness: stale candle → STALE, never a misleading LIVE.
            setFeedBadge(msg.stale === true ? 'stale' : 'live', msg.data_age_ms);
        } catch (e) { console.error('WS parse error:', e); }
    };

    ws.onclose = () => {
        feedState.connected = false;
        setFeedBadge('offline');
        setTimeout(() => { if (currentSymbol === symbol) connectWebSocket(symbol); }, 3000);
    };
}

// Watchdog: socket open but silent for too long → "Waiting data" (never leave a
// stale "Live"). Runs independently of message cadence.
function startFeedWatchdog() {
    if (feedWatchdog) clearInterval(feedWatchdog);
    feedWatchdog = setInterval(() => {
        if (!feedState.connected) return;
        const silentFor = Date.now() - feedState.lastMsgAt;
        const badge = document.getElementById('ws-status');
        if (silentFor > WAIT_DATA_MS && badge && badge.dataset.state !== 'nodata') {
            setFeedBadge('waiting');
        }
    }, 2000);
}

function updateCurrentPriceUnavailable() {
    const el = document.getElementById('current-price');
    if (el) { el.textContent = '--'; el.className = 'current-price'; }
}

function updateCurrentPrice(price, isUp) {
    const el = document.getElementById('current-price');
    el.textContent = Number(price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    el.className = 'current-price ' + (isUp ? 'price-up' : 'price-down');
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

// ── Watchlist ──────────────────────────────

async function updateWatchlist() {
    try {
        const res = await fetch(`${API_URL}/watchlist`);
        const data = await res.json();
        if (!data || data.error) return;

        const container = document.getElementById('watchlist-container');
        container.innerHTML = '';

        data.forEach(item => {
            const div = document.createElement('div');
            div.className = 'watchlist-item' + (item.symbol === currentSymbol ? ' active' : '');
            div.dataset.symbol = item.symbol;

            const scoreClass = item.s_total >= 0.5 ? 'bullish' : item.s_total <= -0.2 ? 'bearish' : 'neutral';
            const actionClass = item.action_proposed || 'hold';
            const qualityClass = item.quality_grade || 'unavailable';

            div.innerHTML = `
                <div>
                    <div class="wl-symbol">${item.symbol} <span class="wl-quality ${qualityClass}"></span></div>
                    <span class="wl-action ${actionClass}">${actionClass}</span>
                </div>
                <div class="wl-details">
                    <div class="wl-price">${item.price ? Number(item.price).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '--'}</div>
                    <span class="wl-score ${scoreClass}">${item.s_total >= 0 ? '+' : ''}${item.s_total.toFixed(2)}</span>
                </div>
            `;
            div.addEventListener('click', () => {
                document.getElementById('symbol-select').value = item.symbol;
                switchSymbol(item.symbol);
            });
            container.appendChild(div);
        });
    } catch (e) { /* silent */ }
}

// ── Signals Panel ──────────────────────────

async function updateSignals() {
    try {
        const res = await fetch(`${API_URL}/signals`);
        const data = await res.json();
        if (!data || data.error) return;

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

            card.innerHTML = `
                <div class="signal-symbol">
                    ${sig.symbol}
                    <span class="signal-action-badge ${actionClass}">${actionClass}</span>
                </div>
                <div class="signal-scores">
                    ${socBadge}
                    <span class="score-badge market" data-tooltip="Market confirmation: momentum, volume, microstructure">MKT ${sig.s_market >= 0 ? '+' : ''}${sig.s_market.toFixed(2)}</span>
                    <span class="score-badge risk" data-tooltip="Risk gate: liquidity, spread, concentration, drawdown">RSK ${sig.s_risk.toFixed(2)}</span>
                    <span class="score-badge total" data-tooltip="Composite: 0.45×SOC + 0.45×MKT + 0.10×(2×RSK-1)">Σ ${sig.s_total >= 0 ? '+' : ''}${sig.s_total.toFixed(2)}</span>
                </div>
                <div class="signal-meta">
                    <span>Confidence: ${confidencePct}</span>
                    <span>Quality: ${sig.quality_grade || 'N/A'}</span>
                </div>
            `;

            // Click to show signal history
            card.addEventListener('click', () => openSignalDetail(sig.symbol));
            container.appendChild(card);
        });
    } catch (e) { /* silent */ }
}

// ── Microstructure ─────────────────────────

// Shared microstructure cell setter (threshold colouring). value == null → "n/a"
// so a metric not yet wired to a real source is never shown as a fabricated 0.
function setMicro(id, value, goodThresh, badThresh, format, invert = false) {
    const el = document.getElementById(id);
    if (!el) return;
    if (value == null || Number.isNaN(value)) { el.textContent = 'n/a'; el.className = 'micro-value'; return; }
    el.textContent = format(value);
    let cls = 'micro-value';
    if (invert) {
        if (value <= goodThresh) cls += ' good';
        else if (value >= badThresh) cls += ' bad';
        else cls += ' warn';
    } else {
        if (value >= goodThresh) cls += ' good';
        else if (value <= badThresh) cls += ' bad';
        else cls += ' warn';
    }
    el.className = cls;
}

// Real-time microstructure from the Binance hub (spread/depth/imbalance/slippage).
// Pushed on every /ws/live tick so these match Binance; trade_pressure and
// relative_volume stay on the slower DB poll (the hub doesn't compute them).
function applyLiveMicrostructure(m) {
    if (m.spread_bps != null) setMicro('micro-spread', m.spread_bps, 3, 10, v => v.toFixed(1) + ' bps', true);
    setMicro('micro-depth', m.depth_usd_10bps, 5000, 1000, v => '$' + v.toLocaleString('en-US', {maximumFractionDigits: 0}));
    setMicro('micro-imbalance', m.imbalance, 0.1, -0.1, v => (v >= 0 ? '+' : '') + v.toFixed(3));
    setMicro('micro-slippage', m.slippage_bps_est, 5, 20, v => v.toFixed(1) + ' bps', true);
}

async function updateMicrostructure(symbol) {
    if (!symbol) symbol = currentSymbol;
    if (!symbol) return;

    try {
        const res = await fetch(`${API_URL}/market-features/${encodeURIComponent(symbol)}`);
        const d = await res.json();
        if (!d || d.error) {
            // Clear
            ['micro-spread','micro-depth','micro-imbalance','micro-pressure','micro-relvol','micro-slippage'].forEach(id => {
                document.getElementById(id).textContent = '--';
                document.getElementById(id).className = 'micro-value';
            });
            return;
        }

        // When the live hub is feeding spread/depth/imbalance/slippage in real time,
        // don't clobber them with the slower DB values; only set trade_pressure /
        // relative_volume here (and seed the rest if no live tick has arrived yet).
        const liveActive = liveConfig.enabled && lastLiveSnap && lastLiveSnap.symbol === symbol;
        if (!liveActive) {
            setMicro('micro-spread', d.spread_bps, 3, 10, v => v.toFixed(1) + ' bps', true);
            setMicro('micro-depth', d.depth_usd_10bps, 5000, 1000, v => '$' + v.toLocaleString('en-US', {maximumFractionDigits: 0}));
            setMicro('micro-imbalance', d.book_imbalance, 0.1, -0.1, v => (v >= 0 ? '+' : '') + v.toFixed(3));
            setMicro('micro-slippage', d.slippage_bps_est, 5, 20, v => v.toFixed(1) + ' bps', true);
        }
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
    // Use the latest streamed snapshot if we have one; else pull a one-off.
    if (lastLiveSnap && lastLiveSnap.symbol === currentSymbol) {
        renderLiveDebug(lastLiveSnap);
    } else {
        try {
            const res = await fetch(`${API_URL}/binance/debug/${encodeURIComponent(currentSymbol)}`);
            const snap = await res.json();
            renderLiveDebug(snap);
        } catch (e) { /* leave whatever is there */ }
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
        trade: 'last @trade price (p)',
        aggTrade: 'last @aggTrade price (p)',
        ticker_last: '@ticker last price (c)',
        book_mid: '(@bookTicker bid + ask) / 2',
        kline_close: 'in-progress @kline close',
    };
    if (formula) formula.textContent = formulas[src] || src;

    summary.innerHTML = `
        <span class="ld-kv">source <b>${src}</b></span>
        <span class="ld-kv">status <b>${snap.feed_status}</b></span>
        <span class="ld-kv">latency <b>${fmtNum(snap.latency_ms, 0)} ms</b></span>
        <span class="ld-kv">staleness <b>${fmtNum(snap.data_age_ms, 0)} ms</b></span>
        <span class="ld-kv">event <b>${snap.event_time ? new Date(snap.event_time).toLocaleTimeString() : 'n/a'}</b></span>
        <span class="ld-kv">recv <b>${snap.local_receive_time ? new Date(snap.local_receive_time).toLocaleTimeString() : 'n/a'}</b></span>
    `;

    const r = snap.raw || {};
    const t = snap.ticker || {};
    const m = snap.micro || {};
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
    ];
    rows.innerHTML = defs.map(([label, val, key]) => {
        const active = (key === src) || (key === '__displayed__');
        const dp = (label.includes('%') || label.includes('imbalance') || label.includes('bps')) ? 3 : 2;
        return `<tr class="${active ? 'ld-active' : ''}"><td>${label}</td><td>${fmtNum(val, dp)}</td></tr>`;
    }).join('');
}

// ── Activity Feed ──────────────────────────

async function updateActivity() {
    try {
        const res = await fetch(`${API_URL}/trades/recent`);
        const data = await res.json();
        if (!data || data.error) return;

        const container = document.getElementById('activity-container');
        container.innerHTML = '';

        data.forEach(trade => {
            const div = document.createElement('div');
            div.className = 'feed-item';
            if (trade.decision_snapshot_id) {
                div.addEventListener('click', () => openDrilldown(trade.decision_snapshot_id, trade.symbol));
            }
            const time = new Date(trade.executed_at).toLocaleTimeString();
            const actionClass = trade.side === 'buy' ? 'buy' : 'sell';
            div.innerHTML = `
                <span class="feed-time">${time}</span>
                <span class="feed-action ${actionClass}">${trade.side}</span>
                <span class="feed-detail">${trade.symbol} — ${Number(trade.qty).toFixed(6)} @ $${Number(trade.price).toLocaleString('en-US', {minimumFractionDigits: 2})}</span>
                <span class="feed-score">slip: ${Number(trade.slippage_bps).toFixed(1)}bps | ${trade.reason || ''}</span>
            `;
            container.appendChild(div);
        });

        if (!data.length) {
            container.innerHTML = '<div class="feed-item"><span class="feed-detail" style="color:var(--text-muted)">No trades yet — bot is evaluating market conditions…</span></div>';
        }
    } catch (e) { /* silent */ }
}

// ── Drilldown Modal (Enhanced) ─────────────

let waterfallChartInstance = null;

async function openDrilldown(decisionId, symbol) {
    const modal = document.getElementById('drilldown-modal');
    document.getElementById('drilldown-title').textContent = `Decision Drill-down: ${symbol} (#${decisionId})`;

    try {
        const res = await fetch(`${API_URL}/decision/${decisionId}`);
        const decision = await res.json();

        if (decision.error) {
            console.error('Decision error:', decision.error);
            return;
        }

        // ── Score Summary ──
        const scoresEl = document.getElementById('drilldown-scores');
        const snap = decision.snapshot;
        const actionColor = {buy:'var(--up-color)', reinforce:'#64B5F6', reduce:'var(--warn-color)', exit:'var(--down-color)', hold:'var(--text-muted)'}[snap.action_proposed] || 'var(--text-primary)';

        // Only show s_social as a real number when a real social feed produced it;
        // otherwise it is a neutral placeholder and must read "n/a".
        const socReal = decision.social_available === true;
        const socHtml = socReal
            ? `<span class="dd-score-value" style="color:#CE93D8">${snap.s_social >= 0 ? '+' : ''}${snap.s_social.toFixed(2)}</span>`
            : `<span class="dd-score-value unavailable" title="No real social feed configured">n/a</span>`;

        scoresEl.innerHTML = `
            <div class="dd-score-item"><span class="dd-score-label">SOC</span>${socHtml}</div>
            <div class="dd-score-item"><span class="dd-score-label">MKT</span><span class="dd-score-value" style="color:var(--accent-color)">${snap.s_market >= 0 ? '+' : ''}${snap.s_market.toFixed(2)}</span></div>
            <div class="dd-score-item"><span class="dd-score-label">RSK</span><span class="dd-score-value" style="color:var(--warn-color)">${snap.s_risk.toFixed(2)}</span></div>
            <div class="dd-score-item"><span class="dd-score-label">Σ Total</span><span class="dd-score-value">${snap.s_total >= 0 ? '+' : ''}${snap.s_total.toFixed(2)}</span></div>
            <div class="dd-score-item"><span class="dd-score-label">Action</span><span class="dd-score-value" style="color:${actionColor};text-transform:uppercase">${snap.action_proposed}</span></div>
            <div class="dd-score-item"><span class="dd-score-label">Confidence</span><span class="dd-score-value">${snap.confidence_score ? (snap.confidence_score * 100).toFixed(0) + '%' : '--'}</span></div>
        `;

        // ── Quality Badge ──
        const qualityEl = document.getElementById('drilldown-quality');
        const qa = decision.quality_audit;
        if (qa) {
            const reasons = qa.degradation_reasons && qa.degradation_reasons.length
                ? ` — ${qa.degradation_reasons.join(', ')}` : '';
            qualityEl.innerHTML = `
                <span class="quality-badge ${qa.quality_grade}">${qa.quality_grade}</span>
                <span style="font-size:0.75rem;color:var(--text-muted)">Social: ${qa.has_sufficient_social ? '✓' : '✗'} | Market: ${qa.has_sufficient_market ? '✓' : '✗'}${reasons}</span>
            `;
        } else {
            qualityEl.innerHTML = '<span class="quality-badge unavailable">No audit data</span>';
        }

        // ── Factors ──
        const factors = decision.factors;
        const container = document.getElementById('factors-container');
        container.innerHTML = '';

        const labels = [];
        const data = [];
        const backgroundColors = [];

        let cumulative = 0;

        factors.forEach(f => {
            const div = document.createElement('div');
            div.className = 'factor-item';
            const isPos = f.contribution >= 0;
            div.innerHTML = `
                <div class="factor-header">
                    <span class="factor-name"><span class="factor-category ${f.category}">${f.category}</span> ${f.name}</span>
                    <span class="factor-contrib ${isPos ? 'positive' : 'negative'}">${isPos ? '+' : ''}${f.contribution.toFixed(4)}</span>
                </div>
                <div class="factor-exp">Val: ${f.value.toFixed(3)} — ${f.explanation}</div>
            `;
            container.appendChild(div);

            labels.push(f.name);
            data.push(f.contribution);
            backgroundColors.push(isPos ? 'rgba(38, 166, 154, 0.8)' : 'rgba(239, 83, 80, 0.8)');
            cumulative += f.contribution;
        });

        // Final sum bar
        labels.push('S_total');
        data.push(snap.s_total);
        backgroundColors.push('#00E5FF');

        // Render Waterfall Chart
        const ctx = document.getElementById('waterfallChart').getContext('2d');
        if (waterfallChartInstance) waterfallChartInstance.destroy();

        waterfallChartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Score Contribution',
                    data: data,
                    backgroundColor: backgroundColors,
                    borderWidth: 0,
                    borderRadius: 2,
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { display: false },
                    title: { display: true, text: 'Factor Contributions to S_total', color: '#8B94A5', font: { family: 'Inter' } }
                },
                scales: {
                    y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8B94A5', font: { family: 'Inter' } } },
                    x: { grid: { display: false }, ticks: { color: '#8B94A5', maxRotation: 45, minRotation: 45, font: { size: 10, family: 'Inter' } } }
                }
            }
        });

        // ── Evidence ──
        const evidenceContainer = document.getElementById('evidence-container');
        evidenceContainer.innerHTML = '';

        if (decision.evidence && decision.evidence.length > 0) {
            decision.evidence.forEach(e => {
                const div = document.createElement('div');
                div.className = 'evidence-item';
                const time = new Date(e.published_at).toLocaleTimeString();
                div.innerHTML = `
                    <div class="evidence-header">
                        <span><span class="evidence-author">${e.author_handle || 'Unknown'}</span> via ${e.source_name || 'unknown'}</span>
                        <span>${time}</span>
                    </div>
                    <div class="evidence-text">${escapeHtml(e.text || '')}</div>
                `;
                evidenceContainer.appendChild(div);
            });
        } else {
            evidenceContainer.innerHTML = '<div style="color:var(--text-muted);font-size:0.8rem;padding:0.5rem;">No real source evidence available for this decision.</div>';
        }

        modal.classList.remove('hidden');
    } catch (e) {
        console.error("Drilldown error:", e);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

document.getElementById('close-drilldown').addEventListener('click', () => {
    document.getElementById('drilldown-modal').classList.add('hidden');
});

// ── Signal Detail (click on signal card) ───

async function openSignalDetail(symbol) {
    try {
        const res = await fetch(`${API_URL}/signals/${encodeURIComponent(symbol)}?limit=10`);
        const history = await res.json();
        if (!history || !history.length) return;

        // Open drilldown for the latest decision
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
        // Fetch recent decisions for all symbols
        const allDecisions = [];
        for (const symbol of await getSymbolsList()) {
            const res = await fetch(`${API_URL}/signals/${encodeURIComponent(symbol)}?limit=20`);
            const data = await res.json();
            if (Array.isArray(data)) {
                data.forEach(d => { d.symbol = symbol; allDecisions.push(d); });
            }
        }

        // Sort by time descending
        allDecisions.sort((a, b) => new Date(b.ts_eval) - new Date(a.ts_eval));

        container.innerHTML = '';

        if (!allDecisions.length) {
            container.innerHTML = '<div style="color:var(--text-muted);padding:1rem;">No decisions recorded yet.</div>';
            return;
        }

        allDecisions.slice(0, 50).forEach((d, i) => {
            const div = document.createElement('div');
            div.className = 'timeline-item';
            div.style.cursor = 'pointer';
            div.addEventListener('click', () => {
                modal.classList.add('hidden');
                openDrilldown(d.id, d.symbol);
            });

            const action = d.action_proposed || 'hold';
            const time = new Date(d.ts_eval).toLocaleString();
            // Derive the "why" from the REAL reason_code stored with the decision,
            // not from invented thresholds. reason_code is one of:
            // s_total_reinforce|s_total_buy|s_total_reduce|s_total_exit|hold_neutral|risk_gate:<gate>
            const reasonText = explainReason(d.reason_code, d.s_total);

            div.innerHTML = `
                <div class="timeline-marker">
                    <div class="timeline-dot ${action}"></div>
                    ${i < allDecisions.length - 1 ? '<div class="timeline-line"></div>' : ''}
                </div>
                <div class="timeline-body">
                    <div class="timeline-action" style="color:${getActionColor(action)}">${action} — ${d.symbol}</div>
                    <div class="timeline-reason">${reasonText}</div>
                    <div class="timeline-scores">
                        <span class="score-badge social" style="font-size:0.6rem">SOC ${d.s_social >= 0 ? '+' : ''}${d.s_social.toFixed(2)}</span>
                        <span class="score-badge market" style="font-size:0.6rem">MKT ${d.s_market >= 0 ? '+' : ''}${d.s_market.toFixed(2)}</span>
                        <span class="score-badge risk" style="font-size:0.6rem">RSK ${d.s_risk.toFixed(2)}</span>
                        <span class="score-badge total" style="font-size:0.6rem">Σ ${d.s_total >= 0 ? '+' : ''}${d.s_total.toFixed(2)}</span>
                    </div>
                    <div class="timeline-time">${time}${d.quality_grade ? ' — Quality: ' + d.quality_grade : ''}</div>
                </div>
            `;
            container.appendChild(div);
        });
    } catch (e) {
        console.error('Timeline error:', e);
        container.innerHTML = '<div style="color:var(--down-color);padding:1rem;">Failed to load timeline.</div>';
    }
}

function getActionColor(action) {
    const colors = { buy: '#26A69A', reinforce: '#64B5F6', reduce: '#FFA726', exit: '#EF5350', hold: '#8B94A5' };
    return colors[action] || '#8B94A5';
}

// Translate the engine's real reason_code into a human sentence. No invented
// thresholds — text mirrors signal_engine/scorer.py exactly.
function explainReason(reasonCode, sTotal) {
    const s = (typeof sTotal === 'number') ? `${sTotal >= 0 ? '+' : ''}${sTotal.toFixed(2)}` : '--';
    if (!reasonCode) return `S_total ${s}`;
    if (reasonCode.startsWith('risk_gate:')) {
        return `Forced HOLD by risk gate: ${reasonCode.slice('risk_gate:'.length)} (S_total ${s} overridden)`;
    }
    const map = {
        s_total_reinforce: `Reinforce — S_total ${s} ≥ +0.60`,
        s_total_buy: `Buy — S_total ${s} ≥ +0.30`,
        s_total_reduce: `Reduce — S_total ${s} ≤ −0.30`,
        s_total_exit: `Exit — S_total ${s} ≤ −0.60`,
        hold_neutral: `Hold — S_total ${s} in neutral band (−0.30, +0.30)`,
    };
    return map[reasonCode] || `${reasonCode} (S_total ${s})`;
}

async function getSymbolsList() {
    try {
        const res = await fetch(`${API_URL}/symbols`);
        const data = await res.json();
        return data.symbols || [];
    } catch (e) { return []; }
}

document.getElementById('timeline-btn').addEventListener('click', openTimeline);
document.getElementById('close-timeline').addEventListener('click', () => {
    document.getElementById('timeline-modal').classList.add('hidden');
});

// ── Docs Modal ─────────────────────────────

document.getElementById('docs-btn').addEventListener('click', async () => {
    try {
        const res = await fetch(`${API_URL}/docs/signals-sentiments`);
        const data = await res.json();
        document.getElementById('docs-container').innerHTML = marked.parse(data.content);
        document.getElementById('docs-modal').classList.remove('hidden');
    } catch(e) { console.error("Docs load error:", e); }
});

document.getElementById('close-docs').addEventListener('click', () => {
    document.getElementById('docs-modal').classList.add('hidden');
});

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
            if (log.metadata && log.metadata.screenshot_path) {
                imgHtml = `<img src="${log.metadata.screenshot_path}" class="sys-log-screenshot" alt="Screenshot" onclick="window.open(this.src, '_blank')">`;
            }

            entry.innerHTML = `
                <div class="sys-log-header">
                    <span class="sys-log-component">[${log.component}]</span>
                    <span class="sys-log-time">${time}</span>
                </div>
                <div class="sys-log-message">${log.message}</div>
                ${imgHtml}
            `;
            logsContainer.appendChild(entry);
        });
    } catch (e) {
        console.error("Failed to fetch system logs:", e);
    }
}

function setupLogging() {
    const logsModal = document.getElementById('logs-modal');
    const logsBtn = document.getElementById('logs-btn');
    const closeBtn = document.getElementById('close-logs');

    logsBtn.addEventListener('click', () => {
        logsModal.classList.remove('hidden');
        updateSystemLogs();
    });

    closeBtn.addEventListener('click', () => logsModal.classList.add('hidden'));
    logsModal.addEventListener('click', (e) => { if (e.target === logsModal) logsModal.classList.add('hidden'); });

    setInterval(() => {
        if (!logsModal.classList.contains('hidden')) {
            updateSystemLogs();
        }
    }, 5000);
}

// ── Ops / Terminals ────────────────────────
// Talks to the dev supervisor (scripts/dev_supervisor.py), default :8050.
// All data is real process state — never placeholders.

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
    } catch (e) {
        renderOpsHeaderBadge(null);
        if (opsState.open) showOpsUnavailable();
        return null;
    }
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
        if (!opsState.knownProcs.has(p.name)) {
            opsState.knownProcs.add(p.name);
            const o = document.createElement('option'); o.value = p.name; o.textContent = p.name; sel.appendChild(o);
        }
        const card = document.createElement('div');
        card.className = 'ops-proc ' + p.status;
        const uptime = p.uptime_s != null ? fmtUptime(p.uptime_s) : '—';
        const tb = p.last_traceback ? `<pre class="ops-tb">${escapeHtml(p.last_traceback)}</pre>` : '';
        card.innerHTML = `
            <div class="ops-proc-head">
                <span class="ops-proc-name">${p.name}</span>
                <span class="ops-proc-status ${p.status}">${p.status}</span>
            </div>
            <div class="ops-proc-meta">
                <span>PID ${p.pid ?? '—'}</span>
                <span>up ${uptime}</span>
                <span>restarts ${p.restarts}</span>
                ${p.exit_code != null ? `<span>exit ${p.exit_code}</span>` : ''}
            </div>
            <div class="ops-proc-lastlog ${(p.last_log_level || 'INFO').toLowerCase()}">${escapeHtml(p.last_log || '')}</div>
            ${tb}
            <div class="ops-proc-actions">
                <button class="log-btn" data-act="start" data-proc="${p.name}">Start</button>
                <button class="log-btn" data-act="restart" data-proc="${p.name}">Restart</button>
                <button class="log-btn" data-act="stop" data-proc="${p.name}">Stop</button>
            </div>
        `;
        card.querySelectorAll('button[data-act]').forEach(b => {
            b.addEventListener('click', () => controlOpsProcess(b.dataset.proc, b.dataset.act));
        });
        c.appendChild(card);
    });
}

function fmtUptime(s) {
    s = Math.floor(s);
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
}

async function controlOpsProcess(name, action) {
    try {
        await fetch(`${OPS_BASE}/api/ops/process/${action}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        setTimeout(loadOpsStatus, 400);
    } catch (e) { console.error('Ops control error:', e); }
}

function connectOpsWS() {
    if (opsState.ws) { try { opsState.ws.close(); } catch (e) {} }
    const conn = document.getElementById('ops-conn');
    let ws;
    try { ws = new WebSocket(OPS_WS_URL); } catch (e) { if (conn) { conn.textContent = 'stream offline'; conn.className = 'status-badge disconnected'; } return; }
    opsState.ws = ws;
    ws.onopen = () => { if (conn) { conn.textContent = 'streaming'; conn.className = 'status-badge connected'; } };
    ws.onclose = () => {
        if (conn) { conn.textContent = 'stream offline'; conn.className = 'status-badge disconnected'; }
        if (opsState.open) setTimeout(connectOpsWS, 3000);
    };
    ws.onmessage = (ev) => {
        let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === 'snapshot' && msg.status) {
            renderOpsHeaderBadge(msg.status);
            renderOpsProcesses(msg.status.processes || []);
        } else if (msg.type === 'status') {
            loadOpsStatus();
        } else if (msg.type === 'log') {
            pushOpsLog(msg);
        } else if (msg.type === 'incident') {
            pushOpsLog({ process: msg.process, level: 'CRITICAL', stream: 'incident',
                message: `INCIDENT [${msg.severity}] ${msg.incident.suspected_root_cause}` });
            loadOpsStatus();
        }
    };
}

function pushOpsLog(evt) {
    opsState.logs.push(evt);
    if (opsState.logs.length > 1000) opsState.logs.shift();
    if (opsState.open) appendOpsLogLine(evt);
}

function logPassesFilter(evt) {
    if (opsState.filterProcess && evt.process !== opsState.filterProcess) return false;
    if (opsState.filterLevel) {
        const r = LEVEL_RANK[(evt.level || 'INFO').toUpperCase()] ?? 1;
        if (r < LEVEL_RANK[opsState.filterLevel]) return false;
    }
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
    while (box.childElementCount > 600) box.removeChild(box.firstChild);
    if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderOpsLogs() {
    const box = document.getElementById('ops-logs');
    if (!box) return;
    box.innerHTML = '';
    opsState.logs.filter(logPassesFilter).slice(-600).forEach(appendOpsLogLine);
    box.scrollTop = box.scrollHeight;
}

function setupOps() {
    const modal = document.getElementById('ops-modal');
    const btn = document.getElementById('ops-btn');
    if (!modal || !btn) return;

    btn.addEventListener('click', () => {
        opsState.open = true;
        modal.classList.remove('hidden');
        loadOpsStatus();
        renderOpsLogs();
        connectOpsWS();
    });
    document.getElementById('close-ops').addEventListener('click', () => {
        opsState.open = false;
        modal.classList.add('hidden');
        if (opsState.ws) { try { opsState.ws.close(); } catch (e) {} opsState.ws = null; }
    });
    document.getElementById('ops-filter-process').addEventListener('change', e => { opsState.filterProcess = e.target.value; renderOpsLogs(); });
    document.getElementById('ops-filter-level').addEventListener('change', e => { opsState.filterLevel = e.target.value; renderOpsLogs(); });
    document.getElementById('ops-clear-logs').addEventListener('click', () => { opsState.logs = []; renderOpsLogs(); });

    // Header badge reflects supervisor health even when the panel is closed.
    loadOpsStatus();
    setInterval(loadOpsStatus, 5000);

    // Phase 6/10: report uncaught frontend errors to the Ops pipeline (best-effort).
    setupFrontendErrorReporter();
}

let _opsReportInFlight = false;
function reportFrontendError(message, stack) {
    if (_opsReportInFlight) return;
    _opsReportInFlight = true;
    fetch(`${OPS_BASE}/api/ops/frontend-error`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, stack, url: window.location.href, ts: Date.now() }),
    }).catch(() => {}).finally(() => { _opsReportInFlight = false; });
}

function setupFrontendErrorReporter() {
    window.addEventListener('error', (e) => {
        reportFrontendError(e.message || 'window.error', e.error && e.error.stack);
    });
    window.addEventListener('unhandledrejection', (e) => {
        const r = e.reason || {};
        reportFrontendError('unhandledrejection: ' + (r.message || String(r)), r.stack);
    });
}

// ── Init ───────────────────────────────────

// ── Live source debug modal wiring ─────────
(function setupLiveDebug() {
    const btn = document.getElementById('live-debug-btn');
    const close = document.getElementById('close-live-debug');
    const modal = document.getElementById('live-debug-modal');
    if (btn) btn.addEventListener('click', openLiveDebug);
    if (close) close.addEventListener('click', () => modal.classList.add('hidden'));
    if (modal) modal.addEventListener('click', e => { if (e.target === modal) modal.classList.add('hidden'); });
})();

document.addEventListener('DOMContentLoaded', async () => {
    setupLogging();
    setupOps();
    startFeedWatchdog();
    initChart();
    await loadLiveConfig();   // know the price/candle source before loading the chart
    loadSymbols();

    // Periodic updates
    updatePortfolio();
    updateWatchlist();
    updateSignals();
    updateActivity();

    setInterval(updatePortfolio, 5000);
    setInterval(updateWatchlist, 10000);
    setInterval(updateSignals, 10000);
    setInterval(updateActivity, 8000);
    setInterval(() => updateMicrostructure(), 5000);
});