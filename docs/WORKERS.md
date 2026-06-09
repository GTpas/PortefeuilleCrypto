# Workers

> Tous lancés et supervisés par `scripts/dev_supervisor.py` (voir [RUNBOOK.md](RUNBOOK.md)). Lancement standalone : `python -m workers.<nom>` avec `PYTHONPATH=.`.

Ordre de démarrage (le supervisor attend les `oneshot` avant la suite) :

```
docker (oneshot) → bootstrap (oneshot) → ingestor → aggregator → feature_worker → social_ingestor → antigravity_bot → api
```

| Worker | Type | Auto-restart | Port métriques |
|---|---|---|---|
| `bootstrap` | oneshot | non | — |
| `ingestor` | long-running | oui | 9101 |
| `aggregator` | long-running | oui | — (aucune métrique) |
| `feature_worker` | long-running | oui | 9102 |
| `social_ingestor` | long-running | oui | 9103 |
| `antigravity_bot` | long-running | oui | 9104 |
| `api` (uvicorn) | long-running | oui | `/metrics` sur :8000 |

---

## `workers/ingestor.py`
- **Rôle** : collecte trades + BBO live des 3 exchanges et les écrit en DB par batch.
- **Entrées** : WS via `collectors/{binance,kraken,coinbase}` (callbacks `handle_trade`/`handle_bbo` → files async). Config : `ACTIVE_SYMBOLS`, `EXCHANGES`, `MAX_MARKET_EVENTS`, `BATCH_MAX_ROWS`, `FLUSH_EVERY_SECONDS`.
- **Sorties** : `trade_tick`, `bbo_tick` (via `db.writer.DatabaseWriter.write_batch`, `ON CONFLICT DO NOTHING`) ; DLQ → `dead_letter_event`.
- **Cadence** : boucle d'écriture continue ; flush quand buffer ≥ `BATCH_MAX_ROWS` (1000) **ou** délai ≥ `FLUSH_EVERY_SECONDS` (0,25 s).
- **Métriques** : `market_events_total{exchange,kind}`, `queue_depth{queue}`, `db_write_latency_ms`, `rows_written_total{table}`, `dlq_total{channel}`.
- **Risques** : files bornées à `MAX_MARKET_EVENTS` (50 000) → **back-pressure** : au-delà, événements **droppés avec warning** (pas de retry). Crash collecteur non géré en interne → supervisor relance.
- **Logs** : `Trade/BBO queue full, dropping event` (WARNING), `Ingestor stopped.` (INFO).
- **Commande** : `python -m workers.ingestor`.

## `workers/aggregator.py`
- **Rôle** : agrège les trades en bougies OHLCV 1s (`time_bucket`).
- **Entrées** : `trade_tick` (look-back ~10 s pour rattraper les événements tardifs).
- **Sorties** : `ohlcv_1s` (UPSERT idempotent).
- **Cadence** : boucle **synchrone**, sommeil ~2 s par cycle.
- **Métriques** : **aucune** (worker non instrumenté — *amélioration possible*).
- **Risques** : exceptions loggées, boucle continue ; un gros backlog non agrégé peut alourdir le scan `time_bucket`.
- **Logs** : `Aggregator error: …` (ERROR), `Aggregator stopped.` (INFO).
- **Commande** : `python -m workers.aggregator`.

## `workers/feature_worker.py`
- **Rôle** : calcule la microstructure marché par symbole actif + snapshot portefeuille.
- **Entrées** : `bbo_tick`, `trade_tick`, `ohlcv_1s` (via `MarketFeaturesCalculator`), `paper_portfolio`, `paper_position`. Config : `ACTIVE_SYMBOLS`, `EXCHANGES`, `FEATURE_MAX_CONCURRENCY`.
- **Sorties** : `market_feature_1s` (batch upsert) ; `portfolio_state` (toutes les 30 boucles).
- **Cadence** : boucle async ~1 s ; `compute_features()` concurrent borné par `FEATURE_MAX_CONCURRENCY` (8).
- **Métriques** : `rows_written_total{table="market_feature_1s"}`, `worker_last_success_ts{worker="feature_worker"}` (port 9102).
- **Risques** : erreur de calcul par paire isolée (feature non écrite) ; épuisement du pool bloque le calcul.
- **Logs** : `Feature worker cycle N …` (INFO ~60 s), `Feature computation error for {symbol}/{exchange}` (ERROR).
- **Commande** : `python -m workers.feature_worker`.
- **Portée** : volontairement limité au **core `ACTIVE_SYMBOLS`** (pas l'univers 300).

## `workers/social_ingestor.py`
- **Rôle** : collecte → analyse → calcule le signal social par symbole. **MOCK uniquement** aujourd'hui.
- **Entrées** : `MockSocialCollector` (si `ENABLE_MOCK_SOCIAL=True`), `ContentAnalyzer`, `SocialEngine`. Config : `ENABLE_MOCK_SOCIAL` (défaut **False**), `ACTIVE_SYMBOLS`.
- **Sorties** : `raw_content`, `content_entity`, `social_signal_1m`, `social_signal_5m`.
- **Cadence** : boucle ~10 s si collecteur actif ; sinon **idle** (sommeils 30 s), aucun signal réel.
- **Métriques** : `social_posts_collected_total{source}`, `content_analyzed_total`, `worker_last_success_ts{worker="social_ingestor"}` (port 9103).
- **Risques** : avec `ENABLE_MOCK_SOCIAL=False` et aucune vraie source → worker idle indéfiniment (comportement attendu). Analyse bornée à 200 items/cycle.
- **Logs** : `ENABLE_MOCK_SOCIAL is ON — running the SIMULATED social collector…` (WARNING), `No real social feed configured…` (WARNING).
- **Commande** : `python -m workers.social_ingestor`.

## `workers/antigravity_bot.py`
- **Rôle** : évalue chaque symbole (scorer) et exécute des paper trades selon `S_total` + risk gates.
- **Entrées** : `bbo_tick` (dernier par symbole), `paper_portfolio`/`paper_position`, scores du `scorer` ; `raw_content` (evidence). Config : `ACTIVE_SYMBOLS`.
- **Sorties** : `decision_snapshot`, `decision_factor`, `decision_evidence_link`, `signal_quality_audit` ; `paper_trade` (+ MAJ portefeuille/positions) via `PaperExecutionEngine`.
- **Cadence** : boucle ~15 s. Consomme la microstructure unique (`spread_bps`/`depth_usd_10bps`) renvoyée par le scorer pour l'exécution (plus de double heuristique de profondeur).
- **Métriques** : `ai_decisions_total{action}`, `paper_orders_total{side}`, `worker_last_success_ts{worker="antigravity_bot"}` (port 9104).
- **Risques** : skip si pas de `bbo_tick` (warning) ; échec d'evidence link ne bloque pas le trade.
- **Logs** : `=== Starting evaluation cycle ===`, `[{symbol}] S_total: … → {action}`, `Trade blocked by risk gates: …` (INFO).
- **Commande** : `python -m workers.antigravity_bot`.

## `workers/bootstrap.py`
- **Rôle** : oneshot — charge les métadonnées marché via CCXT et peuple `market_ref`.
- **Entrées** : CCXT (`load_markets()` binance/kraken/coinbase). Config : `EXCHANGES`.
- **Sorties** : `market_ref` (upsert par batch, page 500).
- **Cadence** : oneshot (pas de boucle).
- **Métriques** : aucune.
- **Risques** : échec d'un exchange loggé et sauté (pas de rollback des lignes déjà commitées) ; rate-limit CCXT via `enableRateLimit=True`.
- **Logs** : `Loading markets for {exchange} via CCXT…`, `{exchange}: N markets synchronized.` (INFO).
- **Commande** : `python -m workers.bootstrap`.

---

## `workers/process_supervisor.py` (cœur de supervision — pas un worker métier)
- **Rôle** : possède le cycle de vie des process enfants ; capture stdout/stderr ligne à ligne ; classe le niveau ; **accumule les tracebacks Python** ; auto-restart avec backoff borné ; émet des **incidents** structurés.
- **Classes** : `ProcessSupervisor` (procs + deques events/incidents + subscribers), `ManagedProcess` (état par process : `status`, `pid`, `started_at`, `restarts`, `recent_logs` (200), `last_log`, `last_log_level`, `last_traceback`).
- **Détection niveau** (`detect_level`) : token explicite prioritaire ; sinon, ligne stderr ressemblant à une exception → `ERROR`.
- **Backoff** : `delay = min(30 s, 1 s × 2^restarts)` → 1, 2, 4, 8, 16, plafond 30 s.
- **Budget glissant** : `OPS_MAX_RESTARTS` (5) crashs dans `OPS_RESTART_WINDOW_S` (120 s) → statut `degraded` + incident `critical`, arrêt de l'auto-restart.
- **Incident** : persisté `logs/ops_incidents.jsonl`, diffusé sur `/ws/ops`, passé au hook `ProcessSupervisor.on_incident` (point d'extension webhook/Claude — jamais fabriqué). Schéma : `incident_id`, `severity`, `process`, `error_type`, `exit_code`, `traceback`, `recent_logs`, `health_status`, `suspected_root_cause`, `recommended_action`.
- **Statuts process** : `pending | starting | running | stopped | crashed | degraded | completed`.
- Détails Ops API/WS : [DEPLOYMENT.md](DEPLOYMENT.md#supervisor) et [RUNBOOK.md](RUNBOOK.md).

> **Ajout d'un worker** ⇒ l'ajouter dans `scripts/dev_supervisor.build_specs()` (uniquement si le fichier existe), documenter ici (rôle/entrées/sorties/tables/métriques) et dans `docs/CHANGELOG_TECH.md`. **Ne jamais inventer un worker** ni documenter une commande dont le fichier n'existe pas.
