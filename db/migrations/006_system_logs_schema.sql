-- Ingestion System Logs Schema

CREATE TABLE IF NOT EXISTS system_log (
    id BIGSERIAL,
    ts_event TIMESTAMPTZ NOT NULL DEFAULT now(),
    component TEXT NOT NULL,         -- e.g., 'truth_social_scraper', 'metrics_recovery', 'screenshot_service'
    level TEXT NOT NULL,             -- 'INFO', 'WARN', 'ERROR', 'SUCCESS'
    message TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb, -- e.g., {"screenshot_path": "/screenshots/truth_123.jpg"}
    PRIMARY KEY (id, ts_event)
);

-- Make it a hypertable for efficient time-based queries and retention
SELECT create_hypertable('system_log', 'ts_event', if_not_exists => TRUE, chunk_time_interval => INTERVAL '7 days');

CREATE INDEX IF NOT EXISTS idx_system_log_component ON system_log (component, ts_event DESC);
CREATE INDEX IF NOT EXISTS idx_system_log_level ON system_log (level, ts_event DESC);

-- Add retention policy (keep logs for 30 days)
SELECT add_retention_policy('system_log', INTERVAL '30 days', if_not_exists => TRUE);
