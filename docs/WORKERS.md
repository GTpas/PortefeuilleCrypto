# Workers

> Tous lancés et supervisés par `scripts/dev_supervisor.py` (voir [RUNBOOK.md](RUNBOOK.md)). Lancement standalone : `python -m workers.<nom>` avec `PYTHONPATH=.`.

Ordre de démarrage (le supervisor attend les `oneshot` avant la suite) :

```
docker (oneshot) → bootstrap (oneshot) → ingestor → aggregator → feature_worker → social_ingestor → antigravity_bot → outcome_evaluator → api
```

| Worker | Type | Auto-restart | Port métriques |
|---|---|---|---|
| `bootstrap` | oneshot | non | — |
| `ingestor` | long-running | oui | 9101 |
| `aggregator` | long-running | oui | 9105 |
| `feature_worker` | long-running | oui | 9102 |
| `social_ingestor` | long-running | oui | 9103 |
| `antigravity_bot` | long-running | oui | 9104 |
| `outcome_evaluator` | long-running | oui | 9106 |
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
- **Métriques** (port 9105) : `aggregator_cycles_total`, `aggregator_rows_upserted_total`, `aggregator_lag_ms` (âge du `trade_tick` le plus récent = **fraîcheur/lag de la source**), `aggregator_cycle_latency_ms`, `worker_last_success_ts{worker="aggregator"}`, `worker_events_failed_total{worker="aggregator"}`.
- **Risques** : exceptions loggées (compteur `worker_events_failed_total`), boucle continue ; un gros backlog non agrégé peut alourdir le scan `time_bucket` (visible via `aggregator_lag_ms` qui monte).
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
- **Rôle** : collecte → analyse → calcule le signal social par symbole.
- **Sources** : **RSS news réel** (`RSSCollector`, si `ENABLE_RSS_SOCIAL=True`) — voir `social/rss_collector.py` — **et/ou** `MockSocialCollector` (si `ENABLE_MOCK_SOCIAL=True`, dev only). Le RSS est une **vraie source** (jamais filtrée comme mock) ; le mock reste tagué mock. Puis `ContentAnalyzer`, `SocialEngine`. Config : `ENABLE_RSS_SOCIAL`/`RSS_FEEDS`/`RSS_POLL_SECONDS`/`RSS_HTTP_TIMEOUT`, `ENABLE_MOCK_SOCIAL` (défaut **False**), `ACTIVE_SYMBOLS`.
- **Sorties** : `raw_content`, `content_entity`, `social_signal_1m`, `social_signal_5m`.
- **Cadence** : boucle ~10 s si collecteur actif ; le RSS ne refetch qu'au plus toutes les `RSS_POLL_SECONDS` (politesse/ToS). Si aucun collecteur → **idle** (sommeils 30 s), aucun signal réel.
- **Métriques** : `social_posts_collected_total{source}`, `content_analyzed_total`, `worker_last_success_ts{worker="social_ingestor"}` (port 9103).
- **Risques** : avec `ENABLE_RSS_SOCIAL=False` **et** `ENABLE_MOCK_SOCIAL=False` → worker idle indéfiniment (comportement attendu). Analyse bornée à 200 items/cycle. RSS : flux indispo loggé en WARNING et sauté (gather `return_exceptions`), un item malformé n'éteint pas le flux.
- **Logs** : `ENABLE_RSS_SOCIAL is ON — polling N real RSS news feed(s)…` (INFO), `ENABLE_MOCK_SOCIAL is ON — running the SIMULATED social collector…` (WARNING), `No real social feed configured…` (WARNING).
- **Commande** : `python -m workers.social_ingestor`.

## `social/rss_collector.py` (vraie source — pas un worker autonome)
- **Rôle** : premier connecteur **réel** derrière `BaseSocialCollector`. Poll de flux **RSS 2.0 / Atom publics** de news crypto (publiés pour syndication → **ToS-safe**, contrairement au scraping X/Reddit).
- **Parsing** : `parse_feed(bytes, source_name)` est **pur** (sans I/O, testé offline) ; gère RSS 2.0 (`<item>`) et Atom (`<entry>`), strip HTML, dates RFC-822 et ISO-8601. Un item malformé est sauté, pas levé.
- **Sortie** : `SocialContent` réels (`source_name="rss_news"`, jamais `mock*`) → ingérés via `BaseSocialCollector.ingest` (dedup `content_hash`, DLQ). `author_handle` = byline ou titre du flux (garde `unique_authors` significatif). Pas d'engagement (RSS n'en expose pas → honnêtement vide).
- **Politesse** : `poll_seconds` (défaut `RSS_POLL_SECONDS`) — `collect()` renvoie `[]` entre deux fetches.
- **Tests** : `tests/test_rss_collector.py`.

## `workers/antigravity_bot.py`
- **Rôle** : évalue chaque symbole (scorer) et exécute des paper trades selon `S_total` + risk gates.
- **Entrées** : `bbo_tick` (dernier par symbole), `paper_portfolio`/`paper_position`, scores du `scorer` ; `raw_content` (evidence). Config : `ACTIVE_SYMBOLS`.
- **Sorties** : `decision_snapshot`, `decision_factor`, `decision_evidence_link`, `signal_quality_audit` ; `paper_trade` (+ MAJ portefeuille/positions) via `PaperExecutionEngine`.
- **Cadence** : boucle ~15 s. Consomme la microstructure unique (`spread_bps`/`depth_usd_10bps`) renvoyée par le scorer pour l'exécution (plus de double heuristique de profondeur).
- **Métriques** : `ai_decisions_total{action}`, `paper_orders_total{side}`, `worker_last_success_ts{worker="antigravity_bot"}` (port 9104).
- **Risques** : skip si pas de `bbo_tick` (warning) ; échec d'evidence link ne bloque pas le trade.
- **Logs** : `=== Starting evaluation cycle ===`, `[{symbol}] S_total: … → {action}`, `Trade blocked by risk gates: …` (INFO).
- **Commande** : `python -m workers.antigravity_bot`.

## `workers/outcome_evaluator.py`
- **Rôle** : boucle d'évaluation **ex-post** — note la qualité des décisions passées contre le prix réalisé et **re-dérive la crédibilité des acteurs**. Active enfin `outcome_eval` + `source_influence_snapshot` (tables créées en 007 mais jamais remplies).
- **Entrées** : `decision_snapshot` (décisions matures), `ohlcv_1s` (prix à la décision et à l'horizon, sur l'exchange de la décision), `decision_evidence_link`→`raw_content`→`tracked_actor` (pour la crédibilité). Config : `ENABLE_OUTCOME_EVAL`, `OUTCOME_EVAL_INTERVAL_S`, `OUTCOME_HORIZONS` (défaut `1h/4h/24h`), `OUTCOME_HOLD_BAND_PCT`, `OUTCOME_PRICE_TOLERANCE_S`.
- **Sorties** : `outcome_eval` (`return_pct`, `was_correct` par horizon) ; `source_influence_snapshot` + MAJ `tracked_actor.influence_score` (prior bayésien `0.5`/poids 5, ≥3 échantillons).
- **Logique correcte** (pure, testée) : buy/reinforce → correct si prix monte ; exit/reduce → correct si prix baisse ; hold → correct si `|return| ≤ OUTCOME_HOLD_BAND_PCT`. `return_pct`/`classify_correct`/`horizon_pg_interval` dans `tests/test_outcome_eval.py`.
- **Idempotence** : une paire (décision, horizon) n'est évaluée qu'une fois (`NOT EXISTS`) ; on n'évalue que les décisions matures (≥ horizon) et < 7 jours ; un prix introuvable dans la tolérance est laissé pour un cycle ultérieur.
- **Cadence** : boucle `OUTCOME_EVAL_INTERVAL_S` (60 s) ; crédibilité recalculée 1 cycle sur 5.
- **Métriques** (port 9106) : `outcome_evals_written_total{horizon}`, `outcome_eval_accuracy{horizon}`, `actor_influence_updates_total`, `worker_last_success_ts{worker="outcome_evaluator"}`, `worker_events_failed_total{worker="outcome_evaluator"}`.
- **Risques** : read-mostly (n'écrit que les 2 tables d'éval + 1 colonne dimension) ; sans vraie source sociale + evidence links, la crédibilité acteurs reste vide (attendu). Décisions trop vieilles pour avoir un prix → jamais résolues (sortent de la fenêtre 7 j).
- **Commande** : `python -m workers.outcome_evaluator`.

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
