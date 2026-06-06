-- Paper Trading Schema (Antigravity Simulator)

CREATE TABLE IF NOT EXISTS paper_portfolio (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    initial_capital NUMERIC(38, 18) NOT NULL DEFAULT 10000.0,
    current_cash NUMERIC(38, 18) NOT NULL DEFAULT 10000.0,
    total_value NUMERIC(38, 18) NOT NULL DEFAULT 10000.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS paper_position (
    id SERIAL PRIMARY KEY,
    portfolio_id INTEGER NOT NULL REFERENCES paper_portfolio(id),
    symbol TEXT NOT NULL,
    exchange_code TEXT NOT NULL REFERENCES exchange_ref(code),
    qty NUMERIC(38, 18) NOT NULL DEFAULT 0.0,
    average_entry_price NUMERIC(38, 18) NOT NULL DEFAULT 0.0,
    unrealized_pnl NUMERIC(38, 18) NOT NULL DEFAULT 0.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(portfolio_id, symbol, exchange_code)
);

CREATE TABLE IF NOT EXISTS paper_trade (
    id SERIAL PRIMARY KEY,
    portfolio_id INTEGER NOT NULL REFERENCES paper_portfolio(id),
    symbol TEXT NOT NULL,
    exchange_code TEXT NOT NULL REFERENCES exchange_ref(code),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty NUMERIC(38, 18) NOT NULL,
    price NUMERIC(38, 18) NOT NULL,
    slippage_bps NUMERIC(38, 18) NOT NULL DEFAULT 0.0,
    fees NUMERIC(38, 18) NOT NULL DEFAULT 0.0,
    signal_score NUMERIC(10, 4),
    reason TEXT,
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signal_log (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange_code TEXT NOT NULL REFERENCES exchange_ref(code),
    ts_eval TIMESTAMPTZ NOT NULL DEFAULT now(),
    s_social NUMERIC(10, 4) NOT NULL,
    s_market NUMERIC(10, 4) NOT NULL,
    s_risk NUMERIC(10, 4) NOT NULL,
    s_total NUMERIC(10, 4) NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_paper_trade_executed_at
    ON paper_trade (executed_at DESC);

CREATE INDEX IF NOT EXISTS idx_signal_log_ts_eval
    ON signal_log (ts_eval DESC);

-- Initialize the default paper portfolio
INSERT INTO paper_portfolio (name, initial_capital, current_cash, total_value)
VALUES ('Antigravity Default', 10000.0, 10000.0, 10000.0)
ON CONFLICT (name) DO NOTHING;
