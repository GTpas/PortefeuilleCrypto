"""
Outcome Evaluator Worker  (ex-post decision quality + actor credibility)
------------------------------------------------------------------------
Closes the learning loop the schema was built for but nothing filled:

  1. ``outcome_eval`` — for every matured ``decision_snapshot``, compare the
     price at decision time vs the price after each horizon (1h/4h/24h…),
     compute the realized return and whether the proposed action was correct.
  2. ``source_influence_snapshot`` + ``tracked_actor.influence_score`` —
     re-derive each actor's credibility from the accuracy of the decisions
     their content was used as evidence for (decision_evidence_link).

Prices come from ``ohlcv_1s`` on the decision's own exchange. The worker is
read-mostly (it only writes the two eval tables and one dimension column) and
idempotent: a decision/horizon pair is evaluated at most once (NOT EXISTS
guard), so it is safe to leave running.

Pure helpers (``return_pct``, ``classify_correct``, ``horizon_pg_interval``)
are module-level and unit-tested offline in ``tests/test_outcome_eval.py``.
"""

import asyncio
import logging
import os
import sys
import time
from typing import Optional

import asyncpg

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from metrics import (
    start_metrics_server, outcome_evals_written_total, outcome_eval_accuracy,
    actor_influence_updates_total, worker_last_success_ts, worker_events_failed_total,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("OutcomeEvaluator")

# Horizon label → PostgreSQL interval literal.
_PG_INTERVAL = {
    "15m": "15 minutes",
    "1h": "1 hour",
    "4h": "4 hours",
    "24h": "24 hours",
    "3d": "3 days",
}

# Bayesian prior so a handful of lucky/unlucky calls don't swing credibility.
_PRIOR_WEIGHT = 5.0
_PRIOR_ACCURACY = 0.5
# Minimum evidence-linked, evaluated decisions before we touch an actor's score.
_MIN_ACTOR_SAMPLES = 3


def horizon_pg_interval(label: str) -> Optional[str]:
    """Map a horizon label to a PG interval literal (None if unknown)."""
    return _PG_INTERVAL.get(label)


def return_pct(price_at_decision: float, price_at_horizon: float) -> Optional[float]:
    """Percentage return between the two prices (None if base price invalid)."""
    if not price_at_decision or price_at_decision <= 0:
        return None
    return (price_at_horizon - price_at_decision) / price_at_decision * 100.0


def classify_correct(action: str, ret_pct: Optional[float], hold_band_pct: float) -> Optional[bool]:
    """
    Was the proposed action vindicated by the realized return?

    - buy / reinforce → correct if price rose
    - exit / reduce   → correct if price fell (we avoided/limited a loss)
    - hold            → correct if the move stayed within ±hold_band_pct
    - anything else   → unknown (None)
    """
    if ret_pct is None:
        return None
    a = (action or "").lower()
    if a in ("buy", "reinforce"):
        return ret_pct > 0
    if a in ("exit", "reduce", "sell"):
        return ret_pct < 0
    if a == "hold":
        return abs(ret_pct) <= hold_band_pct
    return None


async def _price_near(conn: asyncpg.Connection, symbol: str, exchange_code: str,
                      target, tolerance_s: int) -> Optional[float]:
    """
    Latest ohlcv_1s close at/just before ``target`` for this symbol+exchange,
    accepted only if it is within ``tolerance_s`` of the target (else None →
    treated as "no usable price yet").
    """
    row = await conn.fetchrow(
        """
        SELECT close, bucket_start
        FROM ohlcv_1s
        WHERE symbol = $1 AND exchange_code = $2 AND bucket_start <= $3
        ORDER BY bucket_start DESC
        LIMIT 1
        """,
        symbol, exchange_code, target,
    )
    if not row or row["close"] is None:
        return None
    gap = (target - row["bucket_start"]).total_seconds()
    if gap > tolerance_s:
        return None
    return float(row["close"])


async def evaluate_horizon(pool: asyncpg.Pool, horizon: str, batch: int = 500) -> int:
    """Evaluate all matured, not-yet-scored decisions for one horizon."""
    pg_interval = horizon_pg_interval(horizon)
    if pg_interval is None:
        logger.warning("Unknown horizon '%s' — skipped.", horizon)
        return 0

    written = 0
    correct = 0
    scored = 0
    async with pool.acquire() as conn:
        # Matured decisions (older than the horizon) without an eval for it.
        # Bounded to the last 7 days so we never rescan ancient/retention-dropped
        # rows forever.
        candidates = await conn.fetch(
            f"""
            SELECT ds.id, ds.ts_eval, ds.symbol, ds.exchange_code, ds.action_proposed
            FROM decision_snapshot ds
            WHERE ds.ts_eval <= now() - INTERVAL '{pg_interval}'
              AND ds.ts_eval >= now() - INTERVAL '7 days'
              AND NOT EXISTS (
                  SELECT 1 FROM outcome_eval oe
                  WHERE oe.decision_snapshot_id = ds.id AND oe.horizon = $1
              )
            ORDER BY ds.ts_eval ASC
            LIMIT $2
            """,
            horizon, batch,
        )

        for c in candidates:
            target = c["ts_eval"] + (await conn.fetchval(f"SELECT INTERVAL '{pg_interval}'"))
            p0 = await _price_near(conn, c["symbol"], c["exchange_code"],
                                   c["ts_eval"], settings.OUTCOME_PRICE_TOLERANCE_S)
            p1 = await _price_near(conn, c["symbol"], c["exchange_code"],
                                   target, settings.OUTCOME_PRICE_TOLERANCE_S)
            if p0 is None or p1 is None:
                # No usable price yet — leave it for a later cycle.
                continue

            ret = return_pct(p0, p1)
            was_correct = classify_correct(c["action_proposed"], ret, settings.OUTCOME_HOLD_BAND_PCT)

            await conn.execute(
                """
                INSERT INTO outcome_eval
                (decision_snapshot_id, symbol, horizon,
                 price_at_decision, price_at_horizon, return_pct, was_correct)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                c["id"], c["symbol"], horizon,
                p0, p1, round(ret, 4) if ret is not None else None, was_correct,
            )
            written += 1
            scored += 1
            if was_correct:
                correct += 1

    if written:
        outcome_evals_written_total.labels(horizon=horizon).inc(written)
        logger.info("Horizon %s: %d outcomes written (%d/%d correct).",
                    horizon, written, correct, scored)
    if scored:
        outcome_eval_accuracy.labels(horizon=horizon).set(correct / scored)
    return written


async def recompute_actor_influence(pool: asyncpg.Pool) -> int:
    """
    Re-derive actor credibility from the accuracy of decisions their content was
    cited as evidence for, then write source_influence_snapshot + update
    tracked_actor.influence_score. Uses a Bayesian prior so small samples stay
    near the 0.5 baseline. Returns the number of actors updated.
    """
    updated = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rc.actor_id,
                   COUNT(*)                                                   AS total,
                   AVG(CASE WHEN oe.was_correct THEN 1.0 ELSE 0.0 END)        AS accuracy,
                   COALESCE(AVG(oe.return_pct) FILTER (
                       WHERE ds.action_proposed IN ('buy', 'reinforce')), 0)  AS lift
            FROM outcome_eval oe
            JOIN decision_snapshot ds      ON ds.id = oe.decision_snapshot_id
            JOIN decision_evidence_link del ON del.decision_snapshot_id = ds.id
            JOIN raw_content rc            ON rc.id = del.raw_content_id
            WHERE rc.actor_id IS NOT NULL
              AND oe.was_correct IS NOT NULL
              AND oe.ts_eval >= now() - INTERVAL '30 days'
            GROUP BY rc.actor_id
            HAVING COUNT(*) >= $1
            """,
            _MIN_ACTOR_SAMPLES,
        )

        for r in rows:
            total = int(r["total"])
            accuracy = float(r["accuracy"] or 0.0)
            lift = float(r["lift"] or 0.0)
            # Shrink toward the prior; converges to raw accuracy as samples grow.
            influence = (accuracy * total + _PRIOR_ACCURACY * _PRIOR_WEIGHT) / (total + _PRIOR_WEIGHT)
            influence = round(max(0.0, min(1.0, influence)), 2)

            async with conn.transaction():
                await conn.execute(
                    "UPDATE tracked_actor SET influence_score = $1 WHERE id = $2",
                    influence, r["actor_id"],
                )
                await conn.execute(
                    """
                    INSERT INTO source_influence_snapshot
                    (actor_id, influence_score, historical_lift, accuracy_rate, total_mentions)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    r["actor_id"], influence, round(lift, 4), round(accuracy, 2), total,
                )
            updated += 1

    if updated:
        actor_influence_updates_total.inc(updated)
        logger.info("Actor credibility recomputed for %d actor(s).", updated)
    return updated


async def run_outcome_evaluator():
    logger.info("Starting Outcome Evaluator Worker...")
    start_metrics_server(settings.METRICS_PORT_OUTCOME, settings.METRICS_ENABLED)
    pool = await asyncpg.create_pool(settings.DATABASE_URL)

    cycle = 0
    try:
        while True:
            cycle += 1
            try:
                for horizon in settings.OUTCOME_HORIZONS:
                    await evaluate_horizon(pool, horizon)
                # Credibility is cheaper to recompute occasionally, not every cycle.
                if cycle % 5 == 1:
                    await recompute_actor_influence(pool)
                worker_last_success_ts.labels(worker="outcome_evaluator").set(time.time())
            except Exception as e:
                worker_events_failed_total.labels(worker="outcome_evaluator").inc()
                logger.error("Outcome evaluation cycle error: %s", e)

            await asyncio.sleep(settings.OUTCOME_EVAL_INTERVAL_S)
    except asyncio.CancelledError:
        logger.info("Outcome evaluator stopped.")
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(run_outcome_evaluator())
    except KeyboardInterrupt:
        logger.info("Outcome evaluator stopped by user.")
