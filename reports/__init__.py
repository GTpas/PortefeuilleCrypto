"""
Daily Crypto Intelligence Report
================================

A display-only, real-data-only daily advisory report over the ~300-symbol
Binance Spot universe already tracked by the cockpit. It turns the live tiers
(universe 24h ticker + macro global-context) into a beginner-readable yet
financially-credible briefing: a global ranking, prudent indicative predictions,
explainable BUY/HOLD/SELL/AVOID signals, transparent ratios, and an A+→E rating.

Module split (mirrors the rest of the codebase: pure logic vs I/O):
  * ``scoring``   — PURE formulas (ratios, scores, rating, signal, prediction,
    market regime). Zero I/O, fully unit-tested. Single source of truth for the
    numbers, so they can never silently diverge between the worker and the API.
  * ``generator`` — assembles a structured JSON report + a French Markdown render
    from data passed in (universe rows + global context). Pure (data in → out).
  * ``store``     — persistence: writes the JSON + Markdown artifacts to disk
    (source of truth) and best-effort mirrors an index row into Postgres.

Real data only (CLAUDE.md PR2 rule): every number traces to a real Binance/macro
value. When an input is genuinely unavailable (e.g. 1h/7d/30d change, which the
Binance 24h ticker does not carry) the report shows ``N/A`` and lowers the
confidence score — it never fabricates a value. Predictions are always framed as
probabilities / scenarios, never certainties, and the report states it is **not**
personalized financial advice.
"""
