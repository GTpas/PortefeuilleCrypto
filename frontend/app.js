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
            if (trade.decision_snapshot_id) {
                div.style.cursor = 'pointer';
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

// ── Modals & Drilldown ─────────────────────

let waterfallChartInstance = null;

async function openDrilldown(decisionId, symbol) {
    const modal = document.getElementById('drilldown-modal');
    document.getElementById('drilldown-title').textContent = `Decision Drill-down: ${symbol} (#${decisionId})`;
    
    // Fetch factors
    try {
        const res = await fetch(`${API_URL}/factors/${decisionId}`);
        const factors = await res.json();
        
        const container = document.getElementById('factors-container');
        container.innerHTML = '';
        
        const labels = ['Base S_total (0)'];
        const data = [0];
        const backgroundColors = ['#8B94A5'];
        
        let cumulative = 0;
        
        factors.forEach(f => {
            // UI List
            const div = document.createElement('div');
            div.className = 'factor-item';
            const isPos = f.contribution >= 0;
            div.innerHTML = `
                <div class="factor-header">
                    <span class="factor-name">[${f.category.toUpperCase()}] ${f.name}</span>
                    <span class="factor-contrib ${isPos ? 'positive' : 'negative'}">${isPos ? '+' : ''}${f.contribution.toFixed(4)}</span>
                </div>
                <div class="factor-exp">Val: ${f.value.toFixed(2)} — ${f.explanation}</div>
            `;
            container.appendChild(div);
            
            // Chart Data
            labels.push(f.name);
            data.push(f.contribution);
            backgroundColors.push(isPos ? 'rgba(38, 166, 154, 0.8)' : 'rgba(239, 83, 80, 0.8)');
            cumulative += f.contribution;
        });
        
        // Final sum bar
        labels.push('Final S_total');
        data.push(cumulative);
        backgroundColors.push('#00E5FF');
        
        // Render Chart.js Waterfall
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
                    borderWidth: 0
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { display: false },
                    title: { display: true, text: 'Factor Contributions to S_total', color: '#8B94A5' }
                },
                scales: {
                    y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8B94A5' } },
                    x: { grid: { display: false }, ticks: { color: '#8B94A5', maxRotation: 45, minRotation: 45 } }
                }
            }
        });
        
        modal.classList.remove('hidden');
    } catch (e) {
        console.error("Drilldown error:", e);
    }
}

document.getElementById('close-drilldown').addEventListener('click', () => {
    document.getElementById('drilldown-modal').classList.add('hidden');
});

// Docs Modal
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

let systemLogsInterval = null;

async function updateSystemLogs() {
    try {
        const res = await fetch(`${API_URL}/system/logs?limit=50`);
        const logs = await res.json();
        const logsContainer = document.getElementById('logs-container');
        
        // Save scroll pos if we want, or just rebuild (for simplicity we rebuild)
        logsContainer.innerHTML = '';
        
        logs.forEach(log => {
            const entry = document.createElement('div');
            entry.className = `sys-log-entry ${log.level}`;
            
            const time = new Date(log.ts_event).toLocaleTimeString();
            
            let imgHtml = '';
            if (log.metadata && log.metadata.screenshot_path) {
                // If the backend drops screenshots in frontend/screenshots/, 
                // they are served at /screenshots/
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
        updateSystemLogs(); // fetch immediately on open
    });
    
    closeBtn.addEventListener('click', () => logsModal.classList.add('hidden'));
    logsModal.addEventListener('click', (e) => { if (e.target === logsModal) logsModal.classList.add('hidden'); });
    
    // Poll logs every 5s if modal is open (optional), or just poll generally
    setInterval(() => {
        if (!logsModal.classList.contains('hidden')) {
            updateSystemLogs();
        }
    }, 5000);
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