# Architecture — PortefeuilleCrypto / Antigravity Crypto Cockpit

> Vue d'ensemble technique. Pour le détail par composant : [WORKERS.md](WORKERS.md), [API.md](API.md), [DATABASE.md](DATABASE.md), [FRONTEND.md](FRONTEND.md). Pour les règles de travail : [../CLAUDE.md](../CLAUDE.md).

## 1. Vue d'ensemble

Système **local** de paper trading crypto, temps réel, explicable et observable. Il sépare strictement :

`ingestion → normalisation → features → scoring → risque → exécution → visualisation`

Deux **chemins de données indépendants et volontairement séparés** :

| Chemin | Responsabilité | Source | Latence | Consommateurs |
|---|---|---|---|---|
| **Persistance** | Historique, features, décisions, backtest | WS Binance/Kraken/Coinbase → DB | secondes | bot, features, API (fallback), historique |
| **Affichage (display-only)** | Prix/chart/microstructure « collés à Binance UI » | Hubs in-process dans l'API | latence réseau | cockpit (`/ws/live`, `/api/market/*`, `/api/binance/*`) |

Ces deux chemins ouvrent **deux connexions Binance distinctes** — c'est intentionnel. Le hub d'affichage ne fabrique jamais de valeur : pas d'événement réel ⇒ statut `nodata`.

## 2. Schéma des flux de données

### 2.1 Chemin de persistance (bot + historique)

```
Binance  aggTrade / bookTicker  ┐
Kraken   trade / ticker         ├─► collectors/*  ──► models.canonical (TradeTick / BBOTick, event_uid idempotent)
Coinbase market_trades / ticker ┘                          │
                                                           ▼
                              workers/ingestor  (files async bornées MAX_MARKET_EVENTS, batch BATCH_MAX_ROWS / FLUSH_EVERY_SECONDS)
                                                           │
                                                           ▼
                              db/writer.DatabaseWriter.write_batch  (INSERT ... ON CONFLICT DO NOTHING, DLQ sur échec)
                                                           │
                                          ┌────────────────┴────────────────┐
                                          ▼                                 ▼
                                    trade_tick (hypertable)          bbo_tick (hypertable)
                                          │
                              workers/aggregator  (time_bucket 1s, boucle ~2s, look-back 10s)
                                          ▼
                                    ohlcv_1s (hypertable)
                                          │
                              continuous aggregates (TimescaleDB)
                                          ▼
                                  ohlcv_1m / ohlcv_5m
```

### 2.2 Chemin features

```
workers/feature_worker  (boucle 1s, concurrence FEATURE_MAX_CONCURRENCY)
   ├─ signal_engine/market_features.compute_features()  ← lit bbo_tick, trade_tick, ohlcv_1s
   │      → market_feature_1s (hypertable) → continuous aggregate market_feature_1m
   └─ snapshot_portfolio_state (toutes les 30 boucles) → portfolio_state (hypertable)
```

### 2.3 Chemin social (MOCK uniquement aujourd'hui)

```
workers/social_ingestor  (boucle 10s, SI ENABLE_MOCK_SOCIAL=True — défaut False)
   ├─ social/mock_collector.MockSocialCollector.collect()   → raw_content (tagué mock)
   ├─ social/content_analyzer.ContentAnalyzer.analyze_new_content()  → content_entity
   └─ signal_engine/social_engine.compute_social_score()    → social_signal_1m / social_signal_5m
```
> Sans vraie source branchée, le worker reste **idle** : aucun signal réel écrit. L'API filtre tout `tracked_source.name ILIKE 'mock%'` de l'evidence.

### 2.4 Chemin décision (bot)

```
workers/antigravity_bot  (boucle ~15s)
   └─ signal_engine/scorer.evaluate_symbol()
         ├─ market_features  → S_market (+ spread_bps, depth_usd_10bps : source unique microstructure)
         ├─ social_engine    → S_social (neutre 0.0 si indisponible — jamais le -1 fantôme)
         ├─ risk_engine       → S_risk + no-trade gates (+ gate de fraîcheur MAX_DATA_AGE_S)
         └─ S_total = 0.45·S_social + 0.45·S_market + 0.10·(2·S_risk − 1)
                 │  journalisé → decision_snapshot, decision_factor, decision_evidence_link, signal_quality_audit
                 ▼
         si tradeable && signal → paper_execution/engine.execute_trade()
                 → paper_trade (+ MAJ paper_portfolio / paper_position)
```

### 2.5 Chemin affichage (cockpit) — séparé de la persistance

```
api/main.py (FastAPI :8000, lifespan)
   ├─ market/binance_spot.BinanceSpotHub      (Tier 3 : trade/aggTrade/ticker/bookTicker/kline/depth)
   │     → /ws/live/{symbol}, /api/binance/*, /api/market/symbol/*  (prix/chart/microstructure live)
   ├─ market/universe.BinanceUniverseHub       (Tier 1 : un seul !ticker@arr → classement ≤300)
   │     → /api/market/universe, /api/market/trending, /api/market/universe/debug
   └─ lectures DB pinnées DISPLAY_EXCHANGE=binance
         → /api/watchlist, /api/signals, /api/decision, /api/market-features, /api/portfolio, /api/health, /api/historical

frontend/ (servi en statique sur /)  ← consomme tout ce qui précède
```

### 2.6 Chemin ops (supervision locale)

```
scripts/dev_supervisor.py  → workers/process_supervisor.ProcessSupervisor
   spawn ordonné : docker compose up -d (oneshot) → workers.bootstrap (oneshot)
                   → ingestor → aggregator → feature_worker → social_ingestor → antigravity_bot → uvicorn api.main (:8000)
   Ops API :8050  → /api/ops/*, /ws/ops  → panneau « 🖥 Ops » du cockpit
```

## 3. Architecture en 3 niveaux (Tiers) — affichage

| Tier | Quoi | Flux Binance | Borne |
|---|---|---|---|
| **Tier 1 — Univers** | ≤300 cryptos tendances (léger) | 1× `!ticker@arr` + REST 24h | `BACKEND_MAX_SYMBOLS` (300) |
| **Tier 2 — Watchlist** | fenêtre visible (recherche/filtres/favoris) | aucun flux additionnel (dérivé Tier 1) | `MAX_VISIBLE_SYMBOLS` (60 lignes DOM) |
| **Tier 3 — Sélection** | symbole sélectionné + core `ACTIVE_SYMBOLS` | streams plein détail | `BACKEND_ACTIVE_SYMBOL_LIMIT` (20) |

## 4. Rôle de chaque dossier

| Dossier | Rôle |
|---|---|
| `collectors/` | Collecteurs WS par exchange (Binance/Kraken/Coinbase) + base abstraite (reconnexion/backoff). |
| `models/` | `canonical.py` — dataclasses normalisées (`TradeTick`, `BBOTick`, `MarketRef`) + `event_uid` idempotent. |
| `db/` | `writer.py` (écriture batch idempotente + DLQ) et `migrations/` (schéma SQL 001→008, montées au 1ᵉʳ boot Docker). |
| `workers/` | Process long-running + oneshots : ingestor, aggregator, feature_worker, social_ingestor, antigravity_bot, outcome_evaluator, `report_worker` (rapport conseil quotidien), bootstrap, et `process_supervisor.py` (cœur de supervision). |
| `signal_engine/` | Chaîne décisionnelle réelle : `market_features`, `social_engine`, `risk_engine`, `scorer`. Aucune logique aléatoire. |
| `paper_execution/` | `engine.py` — moteur de paper trading (règles de risque, slippage, frais, MAJ portefeuille). |
| `market/` | Hubs in-process temps réel pour le cockpit : `binance_spot.py` (Tier 3), `universe.py` (Tier 1), `global_context.py` (macro), `defi.py` (DeFi). Display-only. |
| `reports/` | Rapport conseil quotidien (advisory tier) : `scoring.py` (formules pures), `generator.py` (JSON+Markdown), `store.py` (fichiers+index DB). Display/report-only. |
| `social/` | Collecte/analyse sociale : base abstraite, analyzer (entités/sentiment), `mock_collector` (DEV only). |
| `api/` | `main.py` — API FastAPI + WS + hubs hébergés + service statique du cockpit. |
| `frontend/` | Cockpit (HTML/CSS/JS vanilla + lightweight-charts). Servi par l'API sur `/`. |
| `scripts/` | `dev_supervisor.py` (entrypoint stack) + scripts PowerShell start/stop. |
| `tests/` | Tests offline `pytest` (pas de DB requise). |
| `docs/` | Cette documentation. |

## 5. Pile technique

- **Backend** : Python 3.10+, `asyncio`, `websockets`, FastAPI, `uvicorn`, `asyncpg` (API/workers async), `psycopg2-binary` (ingestor/aggregator/bootstrap sync), `ccxt` (bootstrap métadonnées), `pydantic-settings` (config).
- **Stockage** : PostgreSQL + **TimescaleDB** (hypertables, agrégats continus, compression, rétention).
- **Cache/queue** : Redis (infra présente ; usage applicatif limité).
- **Observabilité** : `prometheus-client` (dégrade en no-op si absent), `/metrics` exposé.
- **Frontend** : HTML/CSS/JS vanilla, `lightweight-charts` 4.1.1, `chart.js`, `marked.js` (CDN).
- **Infra** : Docker Compose (db + redis uniquement), supervision applicative locale via `dev_supervisor`.

## 6. Invariants à respecter (non négociables)

1. **Aucune donnée mock présentée comme réelle.** Absence de donnée réelle ⇒ `unavailable` / `n/a` / `nodata` explicite (jamais une valeur fabriquée).
2. **Aucune logique aléatoire** dans la chaîne décisionnelle finale.
3. **Pin d'exchange** (`DISPLAY_EXCHANGE=binance`) sur toute lecture DB d'affichage — sinon course inter-exchanges (Coinbase BTC-USD ≠ Binance BTC/USDT).
4. **Gate de fraîcheur** : pas de décision sur quote périmé (`MAX_DATA_AGE_S`).
5. **Source unique de microstructure** : `market_features.compute_features()` (spread/depth), consommée par le bot.
6. **Bornes mémoire** explicites front et back (voir [PERFORMANCE.md](PERFORMANCE.md)).
7. **Tout risk gate actif force HOLD**, quel que soit `S_total`.

## 7. Ports

| Service | Port |
|---|---|
| API / cockpit | 8000 |
| Ops supervisor (API + WS) | 8050 |
| Prometheus workers (ingestor/feature/social/bot) | 9101 / 9102 / 9103 / 9104 |
| PostgreSQL / TimescaleDB | 5432 |
| Redis | 6379 |
