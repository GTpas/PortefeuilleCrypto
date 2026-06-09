# Changelog technique

> Journal des **décisions techniques significatives** (pas un changelog produit). Une ligne par changement notable : quoi, pourquoi, fichiers/impact. **À mettre à jour à chaque modification technique** (voir [CONTRIBUTING.md](../CONTRIBUTING.md)).

Format : `## [date] — titre court` puis **Quoi / Pourquoi / Impact**.

---

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
