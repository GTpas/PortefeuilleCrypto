"""
Daily report — persistence (files are the source of truth, DB index is a mirror)
================================================================================

The report artifacts (full JSON + readable Markdown) are written to disk under
``DAILY_REPORT_DIR`` — that is the authoritative store, consistent with the
project's data policy ("éviter de charger PostgreSQL avec de gros blobs
textuels"). A lightweight sidecar ``_index.json`` keeps the history listing fast.

A small index row (and optional per-asset scores) is **best-effort** mirrored
into Postgres so the report is queryable / future-backtestable, but the feature
never depends on the DB: every read works straight from the files. The DB schema
is the canonical migration ``008_daily_report_schema.sql``; ``ensure_schema`` runs
the same idempotent DDL at runtime so the feature also works on an already-running
database where the init-time migration was not replayed.

Pure filesystem functions (sync) are unit-tested offline; the asyncpg helpers are
thin and guarded by the callers (try/except → log + degrade).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FNAME_RE = re.compile(r"^daily_crypto_report_(\d{4}-\d{2}-\d{2})\.json$")
_INDEX_FILE = "_index.json"


def _is_date(s: str) -> bool:
    return bool(s and _DATE_RE.match(s))


class ReportStore:
    """Filesystem store for daily reports (+ a sidecar history index)."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir

    # — paths —
    def _ensure_dir(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)

    def json_path(self, report_date: str) -> str:
        return os.path.join(self.base_dir, f"daily_crypto_report_{report_date}.json")

    def md_path(self, report_date: str) -> str:
        return os.path.join(self.base_dir, f"daily_crypto_report_{report_date}.md")

    # — write —
    def save(self, report: dict, markdown: str) -> dict:
        """Persist JSON + Markdown for ``report['report_date']``; update the index.
        Returns a slim index entry (also what ``history`` lists)."""
        report_date = report.get("report_date") or report.get("generated_at", "")[:10]
        if not _is_date(report_date):
            raise ValueError(f"invalid report_date: {report_date!r}")
        self._ensure_dir()
        jpath = self.json_path(report_date)
        mpath = self.md_path(report_date)
        # Atomic-ish write (tmp + replace) so a reader never sees a half-written file.
        self._write_atomic(jpath, json.dumps(report, ensure_ascii=False, indent=2))
        self._write_atomic(mpath, markdown)
        entry = self._entry_from_report(report, jpath, mpath)
        self._update_index(entry)
        return entry

    @staticmethod
    def _write_atomic(path: str, content: str) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)

    @staticmethod
    def _entry_from_report(report: dict, jpath: str, mpath: str) -> dict:
        counts = report.get("signal_counts", {})
        return {
            "report_date": report.get("report_date") or report.get("generated_at", "")[:10],
            "generated_at": report.get("generated_at"),
            "market_regime": report.get("market_regime"),
            "universe_size": report.get("universe_size", 0),
            "signal_counts": counts,
            "summary": report.get("summary", ""),
            "status": report.get("status", "ok"),
            "json_path": jpath,
            "markdown_path": mpath,
        }

    # — read —
    def load(self, report_date: str) -> Optional[dict]:
        if not _is_date(report_date):
            return None
        path = self.json_path(report_date)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # corrupt/partial file → honest None
            logger.warning("[reports] failed to read %s: %s", path, e)
            return None

    def load_markdown(self, report_date: str) -> Optional[str]:
        path = self.md_path(report_date)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return None

    def dates(self) -> list[str]:
        """All report dates present on disk, newest first."""
        if not os.path.isdir(self.base_dir):
            return []
        out = []
        for name in os.listdir(self.base_dir):
            m = _FNAME_RE.match(name)
            if m:
                out.append(m.group(1))
        return sorted(out, reverse=True)

    def latest_date(self) -> Optional[str]:
        ds = self.dates()
        return ds[0] if ds else None

    def latest(self) -> Optional[dict]:
        d = self.latest_date()
        return self.load(d) if d else None

    def history(self, limit: int = 60) -> list[dict]:
        """Slim index entries (newest first). Prefers the sidecar index; falls
        back to scanning report headers if the sidecar is missing/stale."""
        idx = self._read_index()
        present = set(self.dates())
        entries = [e for e in idx if e.get("report_date") in present]
        known = {e["report_date"] for e in entries}
        # Backfill any on-disk report missing from the sidecar.
        for d in present - known:
            rep = self.load(d)
            if rep:
                entries.append(self._entry_from_report(rep, self.json_path(d), self.md_path(d)))
        entries.sort(key=lambda e: e.get("report_date", ""), reverse=True)
        return entries[: max(0, limit)]

    # — sidecar index —
    def _index_path(self) -> str:
        return os.path.join(self.base_dir, _INDEX_FILE)

    def _read_index(self) -> list[dict]:
        path = self._index_path()
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _update_index(self, entry: dict) -> None:
        idx = [e for e in self._read_index() if e.get("report_date") != entry["report_date"]]
        idx.append(entry)
        idx.sort(key=lambda e: e.get("report_date", ""), reverse=True)
        try:
            self._write_atomic(self._index_path(), json.dumps(idx, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.warning("[reports] failed to update index: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# Best-effort Postgres mirror (canonical schema = migration 008)
# ──────────────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS daily_crypto_report (
    report_date     DATE PRIMARY KEY,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_regime   TEXT,
    universe_size   INTEGER NOT NULL DEFAULT 0,
    buy_count       INTEGER NOT NULL DEFAULT 0,
    hold_count      INTEGER NOT NULL DEFAULT 0,
    sell_count      INTEGER NOT NULL DEFAULT 0,
    avoid_count     INTEGER NOT NULL DEFAULT 0,
    summary         TEXT,
    json_path       TEXT,
    markdown_path   TEXT,
    status          TEXT NOT NULL DEFAULT 'ok',
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_crypto_asset_score (
    report_date         DATE NOT NULL,
    symbol              TEXT NOT NULL,
    rank                INTEGER,
    signal              TEXT,
    rating              TEXT,
    opportunity_score   NUMERIC(6,2),
    risk_score          NUMERIC(6,2),
    confidence_score    NUMERIC(6,2),
    price               NUMERIC(38,18),
    change_24h          NUMERIC(12,4),
    metrics_json        JSONB,
    prediction_json     JSONB,
    explanation         TEXT,
    source_evidence_json JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_daily_asset_score_date
    ON daily_crypto_asset_score (report_date, opportunity_score DESC);
"""


async def ensure_schema(conn) -> None:
    """Idempotently create the index tables (so the feature works even on an
    existing DB where migration 008 was not replayed)."""
    await conn.execute(_DDL)


async def upsert_index(conn, report: dict, *, status: str = "ok",
                       error_message: Optional[str] = None) -> None:
    """Best-effort upsert of one index row. Caller guards exceptions."""
    counts = report.get("signal_counts", {})
    await conn.execute(
        """
        INSERT INTO daily_crypto_report
        (report_date, generated_at, market_regime, universe_size,
         buy_count, hold_count, sell_count, avoid_count,
         summary, json_path, markdown_path, status, error_message)
        VALUES ($1::date, $2::timestamptz, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        ON CONFLICT (report_date) DO UPDATE SET
            generated_at = EXCLUDED.generated_at,
            market_regime = EXCLUDED.market_regime,
            universe_size = EXCLUDED.universe_size,
            buy_count = EXCLUDED.buy_count,
            hold_count = EXCLUDED.hold_count,
            sell_count = EXCLUDED.sell_count,
            avoid_count = EXCLUDED.avoid_count,
            summary = EXCLUDED.summary,
            json_path = EXCLUDED.json_path,
            markdown_path = EXCLUDED.markdown_path,
            status = EXCLUDED.status,
            error_message = EXCLUDED.error_message
        """,
        report.get("report_date"), report.get("generated_at"),
        report.get("market_regime"), int(report.get("universe_size", 0)),
        int(counts.get("BUY", 0)), int(counts.get("HOLD", 0)),
        int(counts.get("SELL", 0)), int(counts.get("AVOID", 0)),
        report.get("summary", ""), report.get("_json_path"),
        report.get("_markdown_path"), status, error_message,
    )


async def upsert_asset_scores(conn, report: dict) -> int:
    """Best-effort replace of per-asset rows for this report_date (future backtest).
    Caller guards exceptions. Returns the number of rows written."""
    report_date = report.get("report_date")
    assets = report.get("assets", [])
    if not report_date or not assets:
        return 0
    async with conn.transaction():
        await conn.execute("DELETE FROM daily_crypto_asset_score WHERE report_date = $1::date",
                           report_date)
        rows = [
            (report_date, a.get("symbol"), a.get("rank"), a.get("signal"), a.get("rating"),
             a.get("opportunity_score"), a.get("risk_score"), a.get("confidence_score"),
             a.get("price"), a.get("change_24h"),
             json.dumps(a.get("metrics", {})), json.dumps(a.get("prediction", {})),
             a.get("explanation_simple"), json.dumps(a.get("source_evidence", [])))
            for a in assets
        ]
        await conn.executemany(
            """
            INSERT INTO daily_crypto_asset_score
            (report_date, symbol, rank, signal, rating, opportunity_score, risk_score,
             confidence_score, price, change_24h, metrics_json, prediction_json,
             explanation, source_evidence_json)
            VALUES ($1::date,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12::jsonb,$13,$14::jsonb)
            """,
            rows,
        )
    return len(assets)
