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
            // Honest freshness: if the latest candle stopped advancing, drop LIVE
            // and show STALE rather than implying a live feed.
            updateFreshnessBadge(msg.stale === true, msg.data_age_ms);
        } catch (e) { console.error('WS parse error:', e); }
    };

    ws.onclose = () => {
        badge.textContent = 'Offline'; badge.className = 'status-badge disconnected';
        setTimeout(() => { if (currentSymbol === symbol) connectWebSocket(symbol); }, 3000);
    };
}

function updateFreshnessBadge(isStale, ageMs) {
    const badge = document.getElementById('ws-status');
    if (!badge) return;
    // Only override when the socket is up (don't stomp Offline/Connecting).
    if (badge.textContent === 'Offline' || badge.textContent === 'Connecting…') return;
    if (isStale) {
        const secs = (typeof ageMs === 'number') ? ` ${Math.round(ageMs / 1000)}s` : '';
        badge.textContent = 'STALE' + secs;
        badge.className = 'status-badge stale';
    } else {
        badge.textContent = 'Live';
        badge.className = 'status-badge connected';
    }
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

        const setMicro = (id, value, goodThresh, badThresh, format, invert = false) => {
            const el = document.getElementById(id);
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
        };

        setMicro('micro-spread', d.spread_bps, 3, 10, v => v.toFixed(1) + ' bps', true);
        setMicro('micro-depth', d.depth_usd_10bps, 5000, 1000, v => '$' + v.toLocaleString('en-US', {maximumFractionDigits: 0}));
        setMicro('micro-imbalance', d.book_imbalance, 0.1, -0.1, v => (v >= 0 ? '+' : '') + v.toFixed(3));
        setMicro('micro-pressure', d.trade_pressure, 0.1, -0.1, v => (v >= 0 ? '+' : '') + v.toFixed(3));
        setMicro('micro-relvol', d.relative_volume, 1.5, 0.5, v => v.toFixed(1) + 'x');
        setMicro('micro-slippage', d.slippage_bps_est, 5, 20, v => v.toFixed(1) + ' bps', true);
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
    setInterval(() => updateMicrostructure(), 5000);
});