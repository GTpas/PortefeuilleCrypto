# Changelog technique

> Journal des **décisions techniques significatives** (pas un changelog produit). Une ligne par changement notable : quoi, pourquoi, fichiers/impact. **À mettre à jour à chaque modification technique** (voir [CONTRIBUTING.md](../CONTRIBUTING.md)).

Format : `## [date] — titre court` puis **Quoi / Pourquoi / Impact**.

---

## [2026-06-09] — CLI de diagnostic univers + bench latence d'affichage
- **Quoi** : `scripts/diagnose_universe.py` (CLI stdlib) wrappe `GET /api/market/universe/debug` et répond « combien sur N + pourquoi le reste manque » : demandé/éligible/chargé, **partition des rejets** (not_spot/inactive/stable/leverage/low_volume) + exemples, **cause dominante** (`none`/`capped_by_limit`/`low_volume_floor`/`rest_unavailable`/`dominated_by_*`/`insufficient_market`) et recommandation ; flags `--limit`/`--base-url`/`--json`/`--strict`. `scripts/benchmark_snapshot_api.py` mesure la latence client (p50/p95/p99) de `/api/market/universe` + klines. Logique pure (`summarize`, `percentile`, `summarize_latencies`) testée offline (`tests/test_diagnostics.py`, +11 → **192 tests**).
- **Pourquoi** : le prompt « patch prioritaire » demandait explicitement un diagnostic des cryptos manquantes et des commandes de validation de latence. Le « 66/300 » était déjà corrigé (PR5, `MIN_QUOTE_VOLUME` 500K) et la donnée de diagnostic existait derrière `/api/market/universe/debug`, mais sans **outil CLI** pour la consommer. Wrappers fins, **zéro impact** sur les hot paths (aucune écriture, aucune DB).
- **Impact** : nouveaux scripts uniquement ; aucun endpoint/contrat modifié. Docs : RUNBOOK (commandes de validation), TROUBLESHOOTING (diagnostic en une commande).

## [2026-06-09] — Contexte marché global (macro tier : total mcap / dominance / DeFi TVL / Fear & Greed)
- **Quoi** : nouveau hub in-process **display-only** `market/global_context.py` (`GlobalContextHub`) hébergé par l'API (`lifespan`, gated `ENABLE_GLOBAL_CONTEXT`), qui poll en tâche de fond 3 sources **gratuites, sans clé, ToS-safe** : CoinGecko `/global` (total market cap, volume 24h, dominance BTC/ETH, var. mcap 24h), DefiLlama `/v2/chains` (TVL DeFi total + top chains), alternative.me `/fng/` (Fear & Greed). Parsers **purs et testés** (`parse_coingecko_global`/`parse_defillama_chains`/`parse_fng`/`fng_band`). Endpoint `GET /api/market/global`, blocs ajoutés à `/api/market/source` et `/api/health` (`global_context`), flag `global_context_enabled` dans `/api/binance/config`. Métriques `/metrics` : `global_context_refresh_total{source}`, `global_context_refresh_errors_total{source}`, `global_context_refresh_latency_ms{source}`, `global_total_market_cap_usd`, `global_btc_dominance_pct`, `global_defi_tvl_usd`, `global_fear_greed_index`. Front : **barre macro** (`#macro-bar`) sous la barre portefeuille + `fetchGlobalContext()` (poll 30 s).
- **Pourquoi** : 1ʳᵉ tranche du « rapport crypto expert » (deep-research) — donner au cockpit le **backdrop macro** que les tiers Binance-only n'ont pas, en restant simple/local et sans secret. Choix de la tranche la plus additive et sans clé API.
- **Impact** : **données réelles uniquement** — chaque source porte `real`/`stale`/`error`/`age_ms` ; une source jamais répondue ⇒ `real=false` + valeurs nulles (UI `n/a`), une défaillance transitoire conserve la dernière bonne valeur (jamais blanchie). `ENABLE_COINGECKO` **repurposé** (worker fantôme → sous-toggle macro CoinGecko, défaut True). Nouveaux env `ENABLE_GLOBAL_CONTEXT`/`ENABLE_DEFILLAMA`/`ENABLE_FEAR_GREED`/`GLOBAL_CONTEXT_*`/`COINGECKO_API_*`/`DEFILLAMA_API_BASE`/`FEAR_GREED_API_BASE`. Pas de worker séparé (tourne dans l'API), pas de migration. Tests : +`test_global_context` (13) → **181 tests**.

## [2026-06-09] — Observabilité aggregator + 1ʳᵉ source sociale réelle (RSS) + évaluation ex-post + CI pytest
- **Quoi** :
  1. **Aggregator instrumenté** (`workers/aggregator.py`) : serveur Prometheus (port 9105) + `aggregator_lag_ms` (fraîcheur/lag de la source = âge du `trade_tick` le plus récent), `aggregator_cycles_total`, `aggregator_rows_upserted_total`, `aggregator_cycle_latency_ms`, `worker_last_success_ts`/`worker_events_failed_total{worker="aggregator"}`. C'était le seul worker sans métriques.
  2. **Vraie source sociale** : `social/rss_collector.py` (`RSSCollector` derrière `BaseSocialCollector`) — poll de flux **RSS/Atom publics** de news crypto (ToS-safe). Parsing pur `parse_feed()` (RSS 2.0 + Atom, strip HTML, dates RFC-822/ISO), politesse `RSS_POLL_SECONDS`. Câblé dans `workers/social_ingestor.py` derrière `ENABLE_RSS_SOCIAL`. Source `rss_news` → **réelle** (jamais filtrée comme mock).
  3. **Évaluation ex-post** : `workers/outcome_evaluator.py` remplit `outcome_eval` (return + `was_correct` par horizon, prix depuis `ohlcv_1s`, idempotent) et `source_influence_snapshot` + MAJ `tracked_actor.influence_score` (prior bayésien). Activé enfin le backtest + la crédibilité acteurs. Enregistré dans `dev_supervisor.build_specs()` (port métriques 9106).
  4. **CI pytest** : `.github/workflows/ci.yml` (`pytest -q`, offline) à côté de `docs-check`.
- **Pourquoi** : combler les TODO d'observabilité (aggregator aveugle), de données réelles (social = mock only) et de boucle d'apprentissage (tables d'éval vides), et garantir la non-régression des tests en CI.
- **Impact** : nouveaux env `ENABLE_RSS_SOCIAL`/`RSS_*`, `ENABLE_OUTCOME_EVAL`/`OUTCOME_*`, `METRICS_PORT_AGGREGATOR/OUTCOME` ; nouveau worker dans l'ordre supervisé (… → antigravity_bot → **outcome_evaluator** → api) ; ports Prometheus 9101–9106. Tests : +`test_rss_collector` (8) +`test_outcome_eval` (7) → **168 tests**. Pas de migration (tables 007 réutilisées).

## [2026-06-09] — Documentation complète + règle « doc obligatoire »
- **Quoi** : création de `docs/` (ARCHITECTURE, API, DATABASE, WORKERS, FRONTEND, DEPLOYMENT, RUNBOOK, TROUBLESHOOTING, PERFORMANCE, CHANGELOG_TECH), `CONTRIBUTING.md`, `.github/pull_request_template.md`, `scripts/check_docs_sync.py`, `.github/workflows/docs-check.yml`, hook `.githooks/pre-commit` ; enrichissement de `CLAUDE.md` (carte projet, instructions permanentes, checklist commit) et du `README.md`.
- **Pourquoi** : rendre le projet reprenable rapidement et empêcher la doc de devenir obsolète (contrôle automatisé de synchro doc↔code).
- **Impact** : aucun changement de comportement runtime. Le contrôle `check_docs_sync` peut bloquer une PR qui touche le code sans toucher la doc.
- **Correction de doc** : il n'existe pas de table `paper_order` — les ordres simulés vivent dans `paper_trade`.

---

## Historique reconstruit (PR1 → PR5)

Reconstitué depuis `CLAUDE.md` (sections « Politique IA / décision » et migrations 001→007). Antérieur à la tenue de ce journal.

### PR5 — Fix « UNIVERSE 66 » + métriques unavailable explicites
- **Quoi** : `MIN_QUOTE_VOLUME` 5M→500K ; `rejection_reason()`/`REJECT_REASONS` (partition pure des rejets) ; `GET /api/market/universe/debug` ; métriques `universe_*` ; micro indisponible → `unavail` + raison au survol (plus de `n/a` muet).
- **Pourquoi** : l'univers plafonnait à ~66 (plancher liquidité trop haut) ; rendre tout écart au top-300 chiffrable.
- **Impact** : univers se remplit à 300 ; 132 tests.

### PR4 — Univers 300 + ranges chart + mémoire
- **Quoi** : `market/universe.py` (Tier 1, un seul `!ticker@arr`) ; ranges 1J/7J/1M/1An (`range_to_interval`) ; sélection dynamique Tier 3 (`set_active_symbol`, éviction) ; bornes mémoire front/back ; durcissement `docker-compose.yml` + `.dockerignore`.
- **Pourquoi** : passer de 3 à ~300 cryptos sans freeze ni explosion mémoire.
- **Impact** : nouveaux endpoints `/api/market/*` ; tests `test_market_universe`, `test_deploy_config`.

### PR3 — Couche Binance Spot temps réel (prix cockpit = prix Binance)
- **Quoi** : hub in-process `market/binance_spot.py` (streams combinés trade/aggTrade/ticker/bookTicker/kline/depth, init REST, carnet avec détection de trou/resync) ; `PRICE_SOURCE`, `CANDLE_SOURCE`, `CANDLE_INTERVAL` ; feed CHART distinct du feed PRIX ; **fix chart figé** (temps régressif avalé) ; panneau 🔬 Source + `/api/binance/debug`.
- **Pourquoi** : le prix affiché venait du chemin DB (retard de plusieurs s, mélange d'exchanges).
- **Impact** : prix collé à Binance UI, explicitement sourcé ; tests `test_binance_spot`, `test_chart_live`.

### PR2 — Données réelles uniquement + supervisor/Ops
- **Quoi** : règle anti-mock (social mock gated `ENABLE_MOCK_SOCIAL=False`, filtre evidence `ILIKE 'mock%'`, `social_available`) ; `workers/process_supervisor.py` + `scripts/dev_supervisor.py` (Ops API :8050, `/ws/ops`, incidents) ; panneau cockpit 🖥 Ops.
- **Pourquoi** : ne jamais présenter du mock comme réel ; lancement/supervision en une commande.
- **Impact** : pipeline de décision honnête sur la dispo des données ; tests `test_social_availability`, `test_process_supervisor`, `test_ops_api`, `test_launch_scripts`.

### PR1 — Moteur de décision réel + observabilité
- **Quoi** : suppression de tout `random.uniform` ; `signal_engine/{market_features,social_engine,risk_engine,scorer}` réels ; seuils symétriques (`S_total ∈ [-1,+1]`, neutre = HOLD) ; gate de fraîcheur `MAX_DATA_AGE_S` ; source unique microstructure ; `metrics.py` (Prometheus, no-op si absent) + ports workers 9101-9104 + `/metrics`.
- **Pourquoi** : décisions explicables et déterministes ; observabilité réelle.
- **Impact** : `decision_snapshot`/`decision_factor`/`signal_quality_audit` ; tests `test_scorer_thresholds`, `test_engine_decimal`.

### Socle initial — ingestion + stockage
- **Quoi** : collecteurs Binance/Kraken/Coinbase + normalisation canonique + writer batch idempotent + DLQ ; schéma TimescaleDB (hypertables, agrégats continus, compression, rétention) migrations 001→007 ; paper trading (`paper_execution/engine.py`).
- **Impact** : base du système.
