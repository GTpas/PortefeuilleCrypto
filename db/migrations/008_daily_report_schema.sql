-- ============================================================
-- 008 — Daily Crypto Intelligence Report (index + per-asset scores)
-- Idempotent: CREATE ... IF NOT EXISTS only.
--
-- The full report artifacts (JSON + Markdown) live on disk under DAILY_REPORT_DIR
-- (files are the source of truth — see CLAUDE.md data policy: no large text blobs
-- in Postgres). These tables are a lightweight, QUERYABLE mirror written
-- best-effort by the report worker / API:
--   * daily_crypto_report       — one index row per day (regime, counts, paths).
--   * daily_crypto_asset_score  — per-asset scores/prediction (enables the future
--                                 prediction-vs-realized backtest at J+1 / J+7).
-- The application also runs this same DDL at runtime (reports/store.ensure_schema)
-- so the feature works on an existing DB where this migration was not replayed.
-- ============================================================

CREATE TABLE IF NOT EXISTS daily_crypto_report (
    report_date     DATE PRIMARY KEY,                 -- one report per day (regenerate overwrites)
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_regime   TEXT,                             -- bullish | neutral | bearish
    universe_size   INTEGER NOT NULL DEFAULT 0,
    buy_count       INTEGER NOT NULL DEFAULT 0,
    hold_count      INTEGER NOT NULL DEFAULT 0,
    sell_count      INTEGER NOT NULL DEFAULT 0,
    avoid_count     INTEGER NOT NULL DEFAULT 0,
    summary         TEXT,
    json_path       TEXT,                             -- on-disk JSON artifact path
    markdown_path   TEXT,                             -- on-disk Markdown artifact path
    status          TEXT NOT NULL DEFAULT 'ok',       -- ok | partial | error
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_daily_crypto_report_date
    ON daily_crypto_report (report_date DESC);

CREATE TABLE IF NOT EXISTS daily_crypto_asset_score (
    report_date         DATE NOT NULL,
    symbol              TEXT NOT NULL,                 -- canonical e.g. 'BTC/USDT'
    rank                INTEGER,
    signal              TEXT,                          -- BUY | HOLD | SELL | AVOID
    rating              TEXT,                          -- A+ | A | B | C | D | E
    opportunity_score   NUMERIC(6,2),
    risk_score          NUMERIC(6,2),
    confidence_score    NUMERIC(6,2),
    price               NUMERIC(38,18),
    change_24h          NUMERIC(12,4),
    metrics_json        JSONB,                         -- the transparent ratios
    prediction_json     JSONB,                         -- prudent indicative prediction
    explanation         TEXT,                          -- beginner-friendly FR explanation
    source_evidence_json JSONB,                        -- real fields used (real-data-only)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_daily_asset_score_date
    ON daily_crypto_asset_score (report_date, opportunity_score DESC);
