const API_URL = `http://${window.location.host}/api`;
const WS_URL = `ws://${window.location.host}/ws/live`;

let chart;
let candlestickSeries;
let volumeSeries;
let ws;
let currentSymbol = '';
let lastCandleTime = 0;

// Initialize Chart
function initChart() {
    const chartContainer = document.getElementById('tvchart');
    
    chart = LightweightCharts.createChart(chartContainer, {
        layout: {
            background: { type: 'solid', color: 'transparent' },
            textColor: '#8B94A5',
            fontFamily: 'Inter',
        },
        grid: {
            vertLines: { color: 'rgba(255, 255, 255, 0.03)' },
            horzLines: { color: 'rgba(255, 255, 255, 0.03)' },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
        },
        rightPriceScale: {
            borderColor: 'rgba(255, 255, 255, 0.1)',
        },
        timeScale: {
            borderColor: 'rgba(255, 255, 255, 0.1)',
            timeVisible: true,
            secondsVisible: true,
        },
    });

    candlestickSeries = chart.addCandlestickSeries({
        upColor: '#26A69A',
        downColor: '#EF5350',
        borderVisible: false,
        wickUpColor: '#26A69A',
        wickDownColor: '#EF5350',
    });

    volumeSeries = chart.addHistogramSeries({
        color: '#26a69a',
        priceFormat: { type: 'volume' },
        priceScaleId: '', 
        scaleMargins: {
            top: 0.8,
            bottom: 0,
        },
    });

    // Handle resize
    new ResizeObserver(entries => {
        if (entries.length === 0 || entries[0].target !== chartContainer) { return; }
        const newRect = entries[0].contentRect;
        chart.applyOptions({ height: newRect.height, width: newRect.width });
    }).observe(chartContainer);
}

// Fetch Symbols and Populate Dropdown
async function loadSymbols() {
    try {
        const response = await fetch(`${API_URL}/symbols`);
        const data = await response.json();
        
        const select = document.getElementById('symbol-select');
        select.innerHTML = '';
        
        data.symbols.forEach(sym => {
            const option = document.createElement('option');
            option.value = sym;
            option.textContent = sym;
            select.appendChild(option);
        });

        if (data.symbols.length > 0) {
            select.value = data.symbols[0];
            switchSymbol(data.symbols[0]);
        }

        select.addEventListener('change', (e) => switchSymbol(e.target.value));
    } catch (err) {
        console.error('Error loading symbols:', err);
    }
}

// Switch Symbol Logic
async function switchSymbol(symbol) {
    if (ws) {
        ws.close();
    }
    currentSymbol = symbol;
    document.getElementById('current-symbol').textContent = symbol;
    
    // Clear existing data
    candlestickSeries.setData([]);
    volumeSeries.setData([]);
    
    // Load historical
    await loadHistorical(symbol);
    
    // Connect Live WS
    connectWebSocket(symbol);
}

// Load Historical Data
async function loadHistorical(symbol) {
    try {
        const response = await fetch(`${API_URL}/historical/${encodeURIComponent(symbol)}`);
        const data = await response.json();
        
        if (data && data.length > 0) {
            try {
                // Filter duplicates by time (Lightweight charts requires strictly ascending time)
                const uniqueData = [];
                const seenTimes = new Set();
                data.forEach(d => {
                    if (!seenTimes.has(d.time)) {
                        seenTimes.add(d.time);
                        uniqueData.push(d);
                    }
                });
                uniqueData.sort((a, b) => a.time - b.time);

                candlestickSeries.setData(uniqueData.map(d => ({
                    time: d.time,
                    open: d.open,
                    high: d.high,
                    low: d.low,
                    close: d.close
                })));
                
                volumeSeries.setData(uniqueData.map(d => ({
                    time: d.time,
                    value: d.value || 0,
                    color: d.close >= d.open ? 'rgba(38, 166, 154, 0.5)' : 'rgba(239, 83, 80, 0.5)'
                })));
            } catch(e) {
                console.error("Historical SetData Error:", e);
            }
            
            updateCurrentPrice(data[data.length - 1].close, data[data.length - 1].close >= data[data.length - 1].open);
            lastCandleTime = data[data.length - 1].time;
        }
    } catch (err) {
        console.error('Error loading historical data:', err);
    }
}

// Connect WebSocket for Real-Time Updates
function connectWebSocket(symbol) {
    const statusBadge = document.getElementById('ws-status');
    statusBadge.textContent = 'Connecting...';
    statusBadge.className = 'status-badge';
    
    ws = new WebSocket(`${WS_URL}/${encodeURIComponent(symbol)}`);
    
    ws.onopen = () => {
        statusBadge.textContent = 'Live';
        statusBadge.className = 'status-badge connected';
    };
    
    ws.onmessage = (event) => {
        try {
            const message = JSON.parse(event.data);
            if (message.type === 'candle') {
                const d = message.data;
                if (!d || d.time == null || d.open == null || d.close == null) return;
                
                try {
                    candlestickSeries.update({
                        time: d.time,
                        open: d.open,
                        high: d.high,
                        low: d.low,
                        close: d.close
                    });
                    
                    volumeSeries.update({
                        time: d.time,
                        value: d.value || 0,
                        color: d.close >= d.open ? 'rgba(38, 166, 154, 0.5)' : 'rgba(239, 83, 80, 0.5)'
                    });
                } catch(err) {
                    console.error("Chart Update Error:", err, d);
                }
                
                updateCurrentPrice(d.close, d.close >= d.open);
            }
        } catch(e) {
            console.error("WS Message Error:", e, event.data);
        }
    };
    
    ws.onclose = () => {
        statusBadge.textContent = 'Disconnected';
        statusBadge.className = 'status-badge disconnected';
        // Auto reconnect after 3 seconds if not intentionally closed
        setTimeout(() => {
            if (currentSymbol === symbol) {
                connectWebSocket(symbol);
            }
        }, 3000);
    };
}

function updateCurrentPrice(price, isUp) {
    const priceEl = document.getElementById('current-price');
    priceEl.textContent = Number(price).toFixed(2);
    priceEl.className = 'current-price ' + (isUp ? 'price-up' : 'price-down');
}

// Error Logging System
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
    }

    // Intercept console.error
    const originalError = console.error;
    console.error = function(...args) {
        addLog(args.map(a => typeof a === 'object' ? JSON.stringify(a) : a).join(' '), 'error');
        originalError.apply(console, args);
    };

    // Intercept window errors
    window.onerror = function(msg, url, lineNo, columnNo, error) {
        addLog(`${msg} (at ${url}:${lineNo}:${columnNo})`, 'error');
        return false;
    };

    // Modal toggles
    logsBtn.addEventListener('click', () => logsModal.classList.remove('hidden'));
    closeBtn.addEventListener('click', () => logsModal.classList.add('hidden'));
}

// Initialization
document.addEventListener('DOMContentLoaded', () => {
    setupLogging();
    initChart();
    loadSymbols();
});
async function updatePortfolio() {
    try {
        const res = await fetch(API_URL + '/portfolio');
        const data = await res.json();
        if (data && !data.error) {
            document.getElementById('port-total').textContent = '$' + data.total_value.toFixed(2);
            document.getElementById('port-cash').textContent = '$' + data.current_cash.toFixed(2);
            document.getElementById('port-positions').textContent = data.positions.length + ' / 8';
        }
    } catch(e) {}
}
setInterval(updatePortfolio, 5000);
updatePortfolio();