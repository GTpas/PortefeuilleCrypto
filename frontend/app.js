/* ============================================
   ANTIGRAVITY COCKPIT — Frontend Logic
   ============================================ */

const API_URL = `http://${window.location.host}/api`;
const WS_URL = `ws://${window.location.host}/ws/live`;

let chart, candlestickSeries, volumeSeries, ws;
let currentSymbol = '';

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
}

// ── Historical Data ────────────────────────

async function loadHistorical(symbol) {
    try {
        const res = await fetch(`${API_URL}/historical/${encodeURIComponent(symbol)}`);
        const data = await res.json();
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

function connectWebSocket(symbol) {
    const badge = document.getElementById('ws-status');
    badge.textContent = 'Connecting…'; badge.className = 'status-badge';

    ws = new WebSocket(`${WS_URL}/${encodeURIComponent(symbol)}`);

    ws.onopen = () => { badge.textContent = 'Live'; badge.className = 'status-badge connected'; };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.type !== 'candle') return;
            const d = msg.data;
            if (!d || d.time == null || d.open == null || d.close == null) return;
            try {
                candlestickSeries.update({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close });
                volumeSeries.update({ time: d.time, value: d.value || 0, color: d.close >= d.open ? 'rgba(38,166,154,0.5)' : 'rgba(239,83,80,0.5)' });
            } catch (e) { /* chart update error, ignore silently */ }
            updateCurrentPrice(d.close, d.close >= d.open);
        } catch (e) { console.error('WS parse error:', e); }
    };

    ws.onclose = () => {
        badge.textContent = 'Offline'; badge.className = 'status-badge disconnected';
        setTimeout(() => { if (currentSymbol === symbol) connectWebSocket(symbol); }, 3000);
    };
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
            div.innerHTML = `
                <div>
                    <div class="wl-symbol">${item.symbol}</div>
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
            card.innerHTML = `
                <div class="signal-symbol">${sig.symbol}</div>
                <div class="signal-scores">
                    <span class="score-badge social">SOC ${sig.s_social >= 0 ? '+' : ''}${sig.s_social.toFixed(2)}</span>
                    <span class="score-badge market">MKT ${sig.s_market >= 0 ? '+' : ''}${sig.s_market.toFixed(2)}</span>
                    <span class="score-badge risk">RSK ${sig.s_risk.toFixed(2)}</span>
                    <span class="score-badge total">Σ ${sig.s_total >= 0 ? '+' : ''}${sig.s_total.toFixed(2)}</span>
                </div>
            `;
            container.appendChild(card);
        });
    } catch (e) { /* silent */ }
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

// ── Logging System ─────────────────────────

function setupLogging() {
    const logsContainer = document.getElementById('logs-container');
    const logsModal = document.getElementById('logs-modal');
    const logsBtn = document.getElementById('logs-btn');
    const closeBtn = document.getElementById('close-logs');

    function addLog(message, type = 'info') {
        const entry = document.createElement('div');
        entry.className = `log-entry ${type}`;
        const time = new Date().toLocaleTimeString();
        entry.innerHTML = `<span class="log-time">[${time}]</span> ${message}`;
        logsContainer.prepend(entry);
        // Keep max 200 entries
        while (logsContainer.children.length > 200) logsContainer.removeChild(logsContainer.lastChild);
    }

    const origError = console.error;
    console.error = function (...args) {
        addLog(args.map(a => typeof a === 'object' ? JSON.stringify(a) : a).join(' '), 'error');
        origError.apply(console, args);
    };

    window.onerror = (msg, url, line, col) => { addLog(`${msg} (${url}:${line}:${col})`, 'error'); return false; };

    logsBtn.addEventListener('click', () => logsModal.classList.remove('hidden'));
    closeBtn.addEventListener('click', () => logsModal.classList.add('hidden'));
    logsModal.addEventListener('click', (e) => { if (e.target === logsModal) logsModal.classList.add('hidden'); });
}

// ── Init ───────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    setupLogging();
    initChart();
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
});