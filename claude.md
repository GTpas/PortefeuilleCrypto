# PortefeuilleCrypto

## Mission
PortefeuilleCrypto est un système local de paper trading crypto piloté par:
- données de marché temps réel
- actualité crypto
- signaux sociaux
- moteur de décision explicable
- cockpit de supervision temps réel

Le projet doit rester simple à lancer sur une machine locale via Docker, observable, et modulaire.

## 📚 Carte du projet & documentation (lire en premier)

> `CLAUDE.md` = index + règles permanentes (chargé à chaque session). Le détail vit dans [`docs/`](docs/). Pour un humain qui découvre : [`README.md`](README.md). Pour contribuer : [`CONTRIBUTING.md`](CONTRIBUTING.md).

| Doc | Quand l'ouvrir |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | vue d'ensemble, **flux de données**, tiers, rôle des dossiers |
| [docs/API.md](docs/API.md) | endpoints REST + WebSocket |
| [docs/DATABASE.md](docs/DATABASE.md) | tables, hypertables, migrations, rétention |
| [docs/WORKERS.md](docs/WORKERS.md) | rôle/entrées/sorties/cadence/métriques de chaque worker |
| [docs/FRONTEND.md](docs/FRONTEND.md) | cockpit, panneaux, anti-freeze chart, bornes mémoire |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, **variables d'env**, supervisor, ports |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | lancer, vérifier, lire les logs, **rollback** |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | problèmes fréquents + fixes |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | bornes mémoire, latence, vigilance perf & **sécurité** |
| [docs/CHANGELOG_TECH.md](docs/CHANGELOG_TECH.md) | **historique des décisions techniques** |

### Architecture en une vue
Deux chemins **séparés volontairement** (deux connexions Binance) :
- **Persistance** (bot + historique) : `collectors → ingestor → db.writer → trade_tick/bbo_tick → aggregator → ohlcv_1s → agrégats continus`. Features : `feature_worker → market_feature_1s`. Décision : `antigravity_bot → scorer (market+social+risk) → decision_* → paper_trade`.
- **Affichage** (cockpit, « collé à Binance UI ») : hubs in-process dans l'API — `binance_spot.py` (Tier 3 plein détail) + `universe.py` (Tier 1 ≤300, un seul `!ticker@arr`) + `global_context.py` (tier macro : total mcap / dominance / DeFi TVL / Fear & Greed) + `defi.py` (tier DeFi : top protocoles par TVL, DefiLlama) → `/ws/live`, `/api/market/*`, `/api/binance/*`. Sources macro/DeFi gratuites sans clé. Lectures DB d'affichage **pinnées** `DISPLAY_EXCHANGE`.
- **Ops** : `dev_supervisor → process_supervisor` (Ops API :8050, `/ws/ops`) → panneau 🖥 Ops.
- **Rapport conseil (advisory tier, display/report-only)** : `reports/` (scoring pur + generator JSON/Markdown + store fichiers) + `workers/report_worker.py` (génération minuit) → rapport quotidien sur les ~300 cryptos (`/api/reports/daily/*`, modale 📅 Report). Real-data-only ; ne nourrit ni le bot ni la persistance.

Schéma complet : [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#2-schéma-des-flux-de-données).

### Rôle des dossiers (résumé)
`collectors/` (WS par exchange) · `models/` (canonical + event_uid) · `db/` (writer batch+DLQ, migrations) · `workers/` (ingestor, aggregator, feature_worker, social_ingestor, antigravity_bot, outcome_evaluator, report_worker, bootstrap, process_supervisor) · `signal_engine/` (market_features, social_engine, risk_engine, scorer) · `paper_execution/` (engine) · `market/` (hubs binance_spot, universe, global_context, defi) · `reports/` (scoring pur, generator JSON/Markdown, store fichiers+index DB — daily report) · `social/` (base, analyzer, mock, rss) · `api/` (FastAPI + WS + hubs + cockpit statique) · `frontend/` (cockpit) · `scripts/` (supervisor + .ps1) · `tests/` (offline) · `docs/`.

### Repères rapides
- **Workers** → [docs/WORKERS.md](docs/WORKERS.md). Lancement : `python -m workers.<nom>` (`PYTHONPATH=.`). Ordre supervisé : docker→bootstrap→ingestor→aggregator→feature_worker→social_ingestor→antigravity_bot→outcome_evaluator→report_worker→api.
- **Endpoints** → [docs/API.md](docs/API.md). Principaux : `/api/health`, `/api/watchlist`, `/api/signals`, `/api/decision/{id}`, `/api/market/universe`, `/api/binance/debug/{symbol}`, `/api/reports/daily/latest`, `WS /ws/live/{symbol}`. Ops sur :8050.
- **Tables** → [docs/DATABASE.md](docs/DATABASE.md). Cœur : `trade_tick`, `bbo_tick`, `ohlcv_1s`, `market_feature_1s`, `decision_snapshot`/`decision_factor`/`signal_quality_audit`, `paper_portfolio`/`paper_position`/`paper_trade`, `portfolio_state`. ⚠️ pas de table `paper_order` (ordres = `paper_trade`).
- **Variables d'env** → [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Toutes dans `config.py`/`.env`. Jamais de secret en dur.
- **Debug** → [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) : 1) `GET /api/health` 2) 🖥 Ops + `/api/ops/incidents` 3) 🔬 Source pour tout écart prix/chart.
- **Rollback** → [docs/RUNBOOK.md](docs/RUNBOOK.md) : `git revert` ; DLQ + `drop_chunks` pour l'ingestion ; migration inverse (jamais éditer une migration appliquée).
- **Conventions de nommage** → [CONTRIBUTING.md](CONTRIBUTING.md) : branches `type/sujet`, commits `type: …`, migrations `NNN_desc.sql` idempotentes, symboles canoniques `BASE/QUOTE`.
- **Perf & sécurité** → [docs/PERFORMANCE.md](docs/PERFORMANCE.md) : bornes mémoire front/back explicites ; binds `127.0.0.1` + CORS `*` = **local only**, ne pas exposer sans durcir.
- **TODO techniques connus** : voir « ce qui n'est PAS encore final » ci-dessous + `tracked_site`/`tracked_asset_source_map` inutilisées. *(Résolus le 2026-06-09 : aggregator instrumenté port 9105 ; `outcome_eval`/`source_influence_snapshot` remplies par `workers/outcome_evaluator.py` ; 1ʳᵉ vraie source sociale RSS via `ENABLE_RSS_SOCIAL` ; CI pytest `.github/workflows/ci.yml`.)*
- **Historique des décisions** → [docs/CHANGELOG_TECH.md](docs/CHANGELOG_TECH.md) + sections PR1→PR5 plus bas.

## 🤖 Instructions permanentes pour Claude (documentation)

À **chaque** intervention sur le projet (complète la « Politique Claude » plus bas, orientée doc) :
- **Lire `CLAUDE.md` avant de modifier le code** (puis le `docs/` concerné).
- **Mettre à jour `CLAUDE.md`** si l'architecture, les endpoints, les workers, la base, Docker, les scripts ou les commandes changent.
- **Mettre à jour `/docs`** si un composant documenté est modifié (API→`docs/API.md`, DB→`docs/DATABASE.md`, worker→`docs/WORKERS.md`, Docker/env→`docs/DEPLOYMENT.md`, frontend→`docs/FRONTEND.md`, perf→`docs/PERFORMANCE.md`, bug récurrent→`docs/TROUBLESHOOTING.md`).
- **Ne jamais laisser un changement technique sans documentation.**
- **Ajouter une note dans `docs/CHANGELOG_TECH.md`** pour toute modification significative.
- **Ne pas supprimer une documentation existante sans la remplacer.**
- **Signaler clairement les zones incertaines** (« à vérifier ») ou les TODO.
- Le script `scripts/check_docs_sync.py` (hook pre-commit + GitHub Action `docs-check`) signale un changement de code sans mise à jour de doc.

## ✅ Documentation à maintenir à chaque commit

Avant chaque commit :
- [ ] Le code fonctionne localement.
- [ ] Les commandes modifiées sont documentées.
- [ ] Les nouveaux endpoints sont ajoutés dans `docs/API.md`.
- [ ] Les nouvelles tables ou migrations sont ajoutées dans `docs/DATABASE.md`.
- [ ] Les nouveaux workers sont ajoutés dans `docs/WORKERS.md`.
- [ ] Les changements Docker/env sont ajoutés dans `docs/DEPLOYMENT.md`.
- [ ] Les problèmes connus sont ajoutés dans `docs/TROUBLESHOOTING.md`.
- [ ] Le résumé technique est ajouté dans `docs/CHANGELOG_TECH.md`.
- [ ] `CLAUDE.md` reste cohérent avec l'état actuel du projet.

## État actuel à respecter
*(Mis à jour le 2026-06-08 après audit du code réel — migrations 001→007.)*

Le dépôt contient déjà, et **fonctionnel / non-aléatoire** :
- collecteurs marché Binance / Kraken / Coinbase + normalisation canonique des ticks + writer batch idempotent (`ON CONFLICT`) + DLQ
- stockage PostgreSQL / TimescaleDB : hypertables, agrégats continus (OHLCV 1m/5m, `market_feature_1m`), compression et rétention (migrations 003/004/007)
- paper trading avec règles de risque réelles (`paper_execution/engine.py`: 8 positions, 20 %/pos, 10 % cash mini, frais 10 bps, slippage dynamique, rejet > 40 bps)
- **moteur de décision RÉEL et explicable** — `random.uniform` a été supprimé :
  - `signal_engine/market_features.py` : momentum multi-TF, book imbalance, trade pressure, volume relatif, spread, vol réalisée
  - `signal_engine/social_engine.py` : 8 sous-métriques (mention velocity z, sentiment, auteurs uniques, engagement, cross-source, novelty, influence acteur, bot risk)
  - `signal_engine/risk_engine.py` : concentration, liquidité, vol, corrélation BTC, drawdown + **no-trade gates**
  - `scorer.py` : `S_total = 0.45·S_social + 0.45·S_market + 0.10·(2·S_risk−1)`, journalisé dans `decision_snapshot` / `decision_factor` / `decision_evidence_link` / `signal_quality_audit`
- workers : `ingestor`, `aggregator`, `feature_worker`, `social_ingestor`, `antigravity_bot`
- API FastAPI riche (détail décision, facteurs, sources, market-features, social-history, logs système, WS live) + interface cockpit

Ce qui n’est PAS encore final (vraies priorités) :
- **Vraie source sociale = RSS news uniquement** (depuis 2026-06-09, `social/rss_collector.py` derrière `ENABLE_RSS_SOCIAL`, OFF par défaut). `social/mock_collector.py` reste dispo en dev (`ENABLE_MOCK_SOCIAL`, tagué mock). Tables `tracked_site` / `tracked_asset_source_map` créées mais toujours inutilisées. → étendre aux sources gated (X, Reddit, annonces exchange) si besoin.
- **Profondeur de carnet = heuristique** et incohérente côté chemin DB : voir `market_features.py`. Flag `ENABLE_L2_BOOK=False`.
- **Backtest naissant** : `workers/outcome_evaluator.py` remplit `outcome_eval` + `source_influence_snapshot` (return/horizon + crédibilité acteurs). La crédibilité n'a de contenu réel que si une vraie source sociale + evidence links existent.
- **Garde-fou staleness** : géré côté risk_engine (gate `data_stale`) ; le chemin DB du bot reste sur le dernier `bbo_tick`.

Résolu le 2026-06-09 (ne plus lister comme TODO) :
- ~~Observabilité Prometheus absente~~ → instrumentation présente (PR1) ; **aggregator** désormais instrumenté (port 9105, `aggregator_lag_ms` & co).
- ~~Pas de boucle d'évaluation ex-post~~ → `workers/outcome_evaluator.py`.
- ~~Aucun test~~ → suite `pytest` (168 tests) + CI `.github/workflows/ci.yml`.
- `docker-compose.yml` = infra only **est voulu** (workers/api sous `dev_supervisor`, cf. plus bas).

## Priorités techniques
Quand tu travailles sur ce projet, priorité à:
1. consolider l’architecture existante
2. brancher les vraies sources news/social
3. remplacer toute logique aléatoire dans le moteur final
4. améliorer traçabilité, métriques, DLQ et monitoring
5. rendre toute décision achat/vente explicable
6. garder le projet exécutable localement

## Règles d’implémentation
- Toujours analyser les fichiers existants avant d’ajouter de nouvelles briques.
- Préférer les petites modifications cohérentes aux gros refactors abstraits.
- Préférer modifier l’existant plutôt que créer de nouveaux fichiers.
- Si de nouveaux fichiers sont nécessaires, les nommer clairement et limiter leur nombre.
- Supprimer les fichiers temporaires à la fin d’une tâche.
- Utiliser .env pour secrets et paramètres runtime.
- Ne jamais coder d’identifiants ou de secrets en dur.
- Toute fonctionnalité importante doit être accompagnée d’une voie d’observabilité: logs, métriques, statut API ou affichage UI.
- Toute intégration externe doit être documentée et découplée derrière une interface claire.

## Politique données
- TimescaleDB = séries temporelles, signaux structurés, exécution, features normalisées.
- Éviter de charger PostgreSQL avec de gros blobs textuels si un stockage local compressé convient mieux.
- Toujours préciser rétention, compression, indexation et cardinalité attendue.
- Conserver une séparation stricte entre données brutes, données normalisées, features et décisions.

## Politique IA / décision
Le moteur final doit être hybride et explicable.
Il doit séparer:
- ingestion
- extraction / normalisation
- features
- scoring
- politique risque
- exécution
- visualisation

Le score final doit être décomposable en sous-scores.
Chaque trade doit pouvoir être expliqué par:
- contexte marché
- contexte news/social
- métriques de liquidité
- règles risque
- motif de décision

Aucune logique aléatoire ne doit rester dans la chaîne décisionnelle finale.

### État du moteur de décision (implémenté PR1)
- **Seuils de décision** centralisés dans `signal_engine/scorer.py` (constantes `REINFORCE/BUY/REDUCE/EXIT_THRESHOLD`). `S_total ∈ [-1, +1]`, seuils **symétriques autour de 0** : `reinforce ≥ +0.60`, `buy ≥ +0.30`, `hold ∈ (-0.30, +0.30)`, `reduce ≤ -0.30`, `exit ≤ -0.60`. **Un score neutre = HOLD** (correction du bug de liquidation sur signal neutre). Mapping testé dans `tests/test_scorer_thresholds.py`.
- **Gate de fraîcheur** : `signal_engine/risk_engine.py` bloque le trading (`data_stale` / `data_unavailable`, `tradeable=False`) si le dernier quote dépasse `settings.MAX_DATA_AGE_S` (défaut 30 s). L'âge est porté par `market_features['data_age_ms']` et social par `social_engine` ; tous deux journalisés dans `signal_quality_audit.{market,social}_data_age_ms`.
- **Source unique de microstructure** : `signal_engine/market_features.compute_features()` fournit `spread_bps` + `depth_usd_10bps`. `evaluate_symbol()` les renvoie dans `result["features"]` ; `workers/antigravity_bot.py` les consomme pour l'exécution (plus de double heuristique de profondeur).
- **Toute décision force HOLD si un risk gate est actif**, quel que soit `S_total`.

### Observabilité (implémenté PR1)
- Module central `metrics.py` (Prometheus, dégrade en no-op si indisponible). Workers exposés via `start_metrics_server` sur des ports dédiés (`config.METRICS_PORT_*` : ingestor 9101, feature 9102, social 9103, bot 9104). L'API expose `/metrics`.
- Métriques clés : `market_events_total`, `queue_depth`, `db_write_latency_ms`, `rows_written_total`, `dlq_total`, `social_posts_collected_total`, `ai_decisions_total`, `paper_orders_total`, `worker_last_success_ts`.

### Données réelles uniquement (PR2 — RÈGLE ABSOLUE)
**Ne jamais afficher une donnée comme réelle si elle provient d'un mock, d'un random ou d'un placeholder.** Si la donnée réelle n'existe pas, l'UI affiche explicitement `unavailable` / `n/a` / "no real social feed configured" / "No real source evidence available" — jamais une valeur fabriquée.
- **Social = mock uniquement aujourd'hui** : `social/mock_collector.py` est la seule source. Gated derrière `ENABLE_MOCK_SOCIAL` (défaut **False**, opt-in dev). Marqueur mock fiable = `tracked_source.name ILIKE 'mock%'` (le flag `{"mock":true}` du payload est perdu à l'écriture). L'API filtre le mock de l'evidence/sources ; `signal_quality_audit.has_sufficient_social` porte la disponibilité réelle → frontend affiche `SOC n/a` si faux.
- **`signal_engine/scorer.py`** distingue `social: real|unavailable|fallback`, expose `data_quality` + `missing_features`. Une absence de social ⇒ `s_social` **neutre 0.0** (jamais le score baissier fantôme produit par `normalize(0,0,4)=-1`).
- **Fraîcheur marché** : WS `/ws/live` porte `data_age_ms`/`stale` ; `GET /api/health` renvoie le statut DB + l'âge OHLCV par symbole. L'UI retire le badge LIVE → **STALE** quand le prix se fige.

### Couche Binance Spot temps réel (PR3 — prix cockpit = prix Binance)
**Problème corrigé** : le prix affiché venait de `Binance aggTrade → trade_tick → aggregator (toutes les 2 s) → ohlcv_1s → /ws/live (poll DB 1 s)` — soit **plusieurs secondes de retard** sur Binance UI, et les requêtes OHLCV ne filtraient **pas** `exchange_code` (le même `ohlcv_1s` contient binance + kraken + coinbase ; Coinbase = BTC-**USD** ≠ BTC/USDT) → le prix pouvait silencieusement venir d'un autre marché.

**Solution** : un **hub Binance Spot in-process** (`market/binance_spot.py`) hébergé par l'API (démarré dans `lifespan` si `ENABLE_BINANCE_SPOT=True`, défaut). Il maintient en mémoire le dernier état de chaque stream et le sert directement au cockpit → le prix colle à Binance UI à la latence réseau près, **explicitement sourcé** et **horodaté**.

- **Streams Spot combinés** (`wss://stream.binance.com:9443/stream?streams=…`, jamais `fstream`/futures) : `<sym>@trade`, `<sym>@aggTrade`, `<sym>@ticker`, `<sym>@bookTicker`, `<sym>@kline_<interval>`, `<sym>@depth@100ms`.
- **Init REST avant WS** : `/api/v3/klines` (historique graphique), `/api/v3/depth` (snapshot carnet), `/api/v3/ticker/24hr` (stats 24h initiales). Carnet maintenu selon la procédure Binance (snapshot `lastUpdateId` → drop des events couverts → application ordonnée → **détection de trou** sur les update IDs → resync).
- **`PRICE_SOURCE`** choisit la valeur *affichée* : `trade` (défaut, le plus frais), `aggTrade`, `ticker_last` (= header 24h Binance, champ `c`), `book_mid` (`(bid+ask)/2`), `kline_close`. Logique pure et testée (`SymbolState.price_for`).
- **`CANDLE_SOURCE=binance_kline`** (défaut) + **`CANDLE_INTERVAL`** (`1s|1m|5m|15m|1h|4h|1d`) : le graphique trace les **vraies bougies Binance**. Pour comparer avec Binance UI en `1D`, mettre `CANDLE_INTERVAL=1d` ; en intraday `1m`/`1s`. L'intervalle/source est affiché dans le header du graphique (`source-badge`).
- **Microstructure live** : `spread`/`spread_bps` (depuis `bookTicker`, dispo même sans carnet complet), `depth_usd_10bps`/`imbalance`/`slippage_bps_est` (depuis le carnet local) poussés sur `/ws/live` ; `trade_pressure`/`relative_volume` restent sur le poll DB.
- **Statut LIVE honnête** (porté par le serveur, champ `feed_status`) : `live` (event Binance réel < `BINANCE_LIVE_MAX_AGE_MS`, défaut 3000 ms), `stale` (connecté mais figé), `nodata` (WS up mais aucun event réel encore), `mock` (jamais pour le prix Binance). Le badge distingue socket connecté / data reçue / data fraîche / source réelle-vs-mock.
- **Feed CHART distinct du feed PRIX (fix « chart figé »)** : le prix vient de `@trade`, les bougies de `@kline`. Si seules les klines s'arrêtent, le prix continue de bouger pendant que le graphe gèle — c'était masqué. Désormais : (1) le snapshot porte `chart_source`/`chart_status` (`live|stale|nodata`)/`candle_age_ms`/`kline_event_count`/`candle_count` (fraîcheur kline, séparée de `feed_status`), gouverné par **`CHART_LIVE_MAX_AGE_MS`** (défaut 6000 ms, plus large que le prix car une kline ≥1m ne pousse que ~toutes les 2 s) ; (2) un **2ᵉ badge `chart-status`** dans le header du graphe affiche `CHART LIVE` / `CHART STALE Ns` / `NO CANDLES` / `CHART LIVE (derived)`. **Cause racine corrigée côté front** : `candlestickSeries.update()` était appelé dans un `try/catch` muet ; Lightweight-Charts **lève** si le `time` est antérieur au dernier point (kline 1m `:00` après un backfill OHLCV 1s `:56`) → l'erreur avalée figeait toutes les updates suivantes. Le store de bougies (`chartApplyCandle`) ne passe **jamais** un temps régressif à `update()` (append / update-last / rebase), force le temps en **secondes** (`toChartTime`), backfill via klines Binance (retry pendant le warm-up du hub, plus de fallback d'intervalle incohérent), et logge chaque bougie (`?debug=1` ou `window.CHART_DEBUG=true`).
- **Panneau debug `🔬 Source`** (header) + `GET /api/binance/debug/{symbol}` : compare `raw_trade_price`, `raw_agg_trade_price`, `raw_ticker_last`, `raw_book_bid/ask`, `book_mid`, `raw_kline_close`, `displayed_price`, `price_source`, `event_time`, `local_receive_time`, `latency_ms`, `staleness_ms` → on voit immédiatement pourquoi Binance UI et le cockpit peuvent différer selon la source.
- **Quelle source choisir** : header Binance 24h → `ticker_last` ; dernier prix temps réel (carnet de trades) → `trade` ; prix « milieu de marché » → `book_mid`. Le graphique suit `CANDLE_INTERVAL` ; pour matcher exactement l'écran Binance, aligner l'intervalle.
- **Endpoints** : `GET /api/binance/config` (source/intervalle/connecté), `GET /api/binance/debug/{symbol}` (raw vs affiché), `GET /api/binance/klines/{symbol}` (bougies Binance pour le graphe). `GET /api/health` expose un bloc `binance_live` (connecté + statut par symbole), distinct de la fraîcheur DB.
- **Observabilité** : `binance_live_connected`, `binance_live_events_total{stream}`, `binance_live_staleness_ms{symbol}`, `binance_live_latency_ms`, `binance_book_resync_total{symbol}` (exposés via `/metrics`).
- **Séparation des chemins** : le hub est **display-only** ; l'`ingestor` reste le chemin de **persistance** (trade_tick/bbo_tick → aggregator → ohlcv_1s) pour le bot et l'historique. Deux connexions Binance publiques distinctes, c'est voulu. Le hub ne fabrique jamais de valeur : pas d'event réel ⇒ `nodata`.
- **Config** : `ENABLE_BINANCE_SPOT`, `PRICE_SOURCE`, `CANDLE_SOURCE`, `CANDLE_INTERVAL`, `BINANCE_WS_BASE`, `BINANCE_REST_BASE`, `BINANCE_DEPTH_LIMIT`, `BINANCE_LIVE_MAX_AGE_MS`, `CHART_LIVE_MAX_AGE_MS` (voir `.env`). Tests offline : `tests/test_binance_spot.py` (parsing trade/aggTrade/ticker/bookTicker/kline/depth, spread/mid/bps, sélection `PRICE_SOURCE`, statut LIVE/STALE/NODATA, application + trou du carnet) ; `tests/test_chart_live.py` (conversion kline ms→s, `classify_candle_update` append/update/older, statut CHART nodata/live/stale, isolation kline par symbole, source jamais mock).

### Univers marché 300 cryptos + ranges chart + mémoire (PR4)
**But** : passer de 3 symboles à un univers de ~300 cryptos tendances **sans freeze et sans exploser la mémoire**, ajouter les ranges 1J/7J/1M/1An, et ne jamais présenter de mock comme réel.

**Architecture en 3 niveaux (display-only, séparée de la persistance/bot)** :
- **Tier 1 — univers léger (≤300)** : `market/universe.py` → `BinanceUniverseHub`. **UNE** seule WS all-market `!ticker@arr` + un refresh REST `/api/v3/ticker/24hr` (toutes les `TRENDING_REFRESH_SECONDS`) alimentent un classement **borné en mémoire**. Pas de trade/kline/depth pour les 300. `exchangeInfo` filtre aux paires **SPOT TRADING** du quote `QUOTE_ASSET`. Exclusions : stablecoins/fiat (`EXCLUDE_STABLES`), leverage tokens UP/DOWN/BULL/BEAR/3L/3S (`EXCLUDE_LEVERAGE`, garde anti-faux-positif type `JUP`), volume mini (`MIN_QUOTE_VOLUME`). **Score tendance** pur et testé : `0.45·log(quote_vol) + 0.20·log(trades) + 0.20·|chg%| + 0.10·range + 0.05·spread_quality − pénalité_staleness`.
- **Tier 2 — watchlist active** : la fenêtre visible de l'univers (recherche/filtres/favoris) ; servie depuis l'état Tier 1, aucun flux supplémentaire.
- **Tier 3 — symbole sélectionné** : le hub plein détail `BinanceSpotHub` (trade/aggTrade/ticker/bookTicker/kline/depth). **Sélection dynamique** : `set_active_symbol()` ajoute le symbole choisi et **évince** le plus ancien slot non-core (borne `BACKEND_ACTIVE_SYMBOL_LIMIT`, le core `ACTIVE_SYMBOLS` n'est jamais évincé) puis **reconnecte** la WS combinée (sans backoff). Cache klines borné par `MAX_CANDLES_BACKEND`.

**Ranges chart (1J/7J/1M/1An)** : mapping **pur et testé** `range_to_interval()` + `klines_limit_for_range()` (capé à 1000, la limite REST Binance ; aliases FR 1J/7J/1An). Changer de range : `POST /api/market/active-symbol {symbol, range}` → le hub bascule l'intervalle kline (`set_range` → reconnect, cache vidé pour ne pas mélanger les intervalles) et renvoie les **klines REST fraîches** au bon intervalle ; le front rebase proprement. Le front **ignore** une bougie live dont l'intervalle ≠ intervalle attendu (protège le chart pendant la bascule). Conversion ms→s systématique (`Math.floor(t/1000)`), réutilise le `chartStore` anti-freeze de PR3.

**Mémoire front (bornes servies par `/api/binance/config.frontend_limits`)** : watchlist **windowed** (≤`MAX_VISIBLE_SYMBOLS` lignes DOM, jamais 300), recherche **debounced**, re-render **throttlé** (`UI_UPDATE_THROTTLE_MS`), candles capées (`MAX_CANDLES_PER_SYMBOL`, trim sur `setData`), ring buffers logs/events (`MAX_LOG_BUFFER`/`MAX_EVENT_BUFFER`), `series.update()` (jamais `setData()` au tick), chart jamais recréé. Favoris en `localStorage`.

**Mémoire back** : univers borné à `BACKEND_MAX_SYMBOLS` (on ne garde en RAM que les membres du top-N, les autres `!ticker@arr` sont ignorés) ; `/ws/live` throttlé par `BROADCAST_THROTTLE_MS` ; depth/order-book seulement pour Tier 3 (`ENABLE_DEPTH_ONLY_FOR_SELECTED`).

**Docker** : `docker-compose.yml` durci — `mem_limit`/`memswap_limit` (db 1g, redis 320m), `logging.options.max-size`/`max-file` (rotation), redis `--maxmemory 256mb --maxmemory-policy allkeys-lru --save ""`, healthchecks conservés. `.dockerignore` créé (exclut `venv`, `__pycache__`, `.git`, `.pytest_cache`, `node_modules`, `.env`, `logs/`, rapports).

**Endpoints ajoutés** (port 8000) :
- `GET /api/market/universe?limit=300` · `GET /api/market/trending?limit=300` — top tendances (rows légers, `is_core` marqué). Vide + statut honnête si hub off.
- `GET /api/market/source` — quelle donnée est réelle / mock / non configurée (prix, chart, univers, social).
- `GET /api/market/symbol/{symbol}/snapshot` — plein détail si Tier 3, sinon row léger, sinon `unavailable`.
- `GET /api/market/symbol/{symbol}/klines?range=1D` — klines REST réelles pour le range.
- `POST /api/market/active-symbol` `{symbol, range}` — sélectionne le symbole Tier 3 + range, renvoie klines fraîches.
- `GET /api/binance/config` enrichi : `chart_ranges`, `range_default`, `range_intervals`, `frontend_limits`, `universe_enabled`. `GET /api/health` ajoute un bloc `universe`.

**Données réelles uniquement** : univers/prix/chart = Binance Spot, **explicitement sourcés** ; pas de feed → `Universe n/a` / `core only` / rows `light`, jamais une valeur fabriquée. Social reste mock-only (cf. règle PR2).

**Config** (voir `.env`) : `ENABLE_MARKET_UNIVERSE`, `UNIVERSE_LIMIT`, `QUOTE_ASSET`, `MIN_QUOTE_VOLUME`, `EXCLUDE_STABLES`, `EXCLUDE_LEVERAGE`, `TRENDING_REFRESH_SECONDS`, `UNIVERSE_STALE_MS`, `BACKEND_MAX_SYMBOLS`, `BACKEND_ACTIVE_SYMBOL_LIMIT`, `MAX_CANDLES_BACKEND`, `MAX_MARKET_EVENTS`, `BROADCAST_THROTTLE_MS`, `SNAPSHOT_INTERVAL_SECONDS`, `ENABLE_DEPTH_ONLY_FOR_SELECTED`, `CHART_RANGE_DEFAULT`, `CHART_INTERVAL_{1D,7D,1M,1Y}`, `MAX_CANDLES_PER_SYMBOL`, `MAX_VISIBLE_SYMBOLS`, `MAX_EVENT_BUFFER`, `MAX_LOG_BUFFER`, `UI_UPDATE_THROTTLE_MS`.

**Tests offline ajoutés** : `tests/test_market_universe.py` (stable/leverage exclusion + garde JUP, canonicalisation, parsing REST/`!ticker@arr`, score tendance ordering, filtres min-volume/spot, ranking sort/cap/rank), `tests/test_deploy_config.py` (`.dockerignore`, durcissement compose, settings présents) ; `tests/test_binance_spot.py` étendu (mapping ranges, `set_active_symbol` add/evict, `set_chart_interval`/`set_range`, cache klines borné).

**Diagnostic** : chart figé → badge `CHART STALE`/`NO CANDLES` + `🔬 Source` (intervalle, `chart_status`, âges) ; écart avec Binance → `🔬 Source` (raw vs displayed, `PRICE_SOURCE`) ; univers vide → badge header `Universe n/a` + `GET /api/market/source` ; mémoire back → `GET /api/health.universe.tracked` (≤ `BACKEND_MAX_SYMBOLS`) ; mémoire Docker → `docker stats` (limites `mem_limit`).

### Fix « UNIVERSE 66 » + métriques unavailable explicites (PR5)
**Cause racine du « 66 »** : `MIN_QUOTE_VOLUME` valait **5 000 000** → seulement ~70 paires USDT au-dessus du floor (~66 après exclusions stables/leverage/spot). Le top-N=300 ne pouvait pas se remplir. **Fix** : floor abaissé à **500 000** (`config.py` + `.env`) → ~305 paires éligibles → l'univers se remplit bien à **300**. Le floor reste un garde-fou de liquidité ; le monter rétrécit l'univers (visible dans le debug). **Aucun cap silencieux à 66** : si Binance renvoie < 300 éligibles, la raison est chiffrée dans le debug.
- **Source unique de filtrage** : `market/universe.rejection_reason()` (pur, testé) attribue chaque rejet à la **première** raison échouée parmi `REJECT_REASONS = (not_spot, inactive, stable, leverage, low_volume)` → les compteurs debug partitionnent l'ensemble rejeté (jamais de double comptage). `passes_filters()` en dérive.
- **Endpoint debug** : `GET /api/market/universe/debug` expose `raw_binance_tickers_count`, `exchange_info_symbols_count`, `eligible_symbols_count`, `excluded_{stable,leverage,low_volume,not_spot,inactive}_count`, `quote_mismatch_count`, `rejected_examples` (≤5 symboles/raison), `capped_by_limit`, `final_universe_count`, `build_latency_ms`, `cache_age_ms`, `last_rest_refresh_ms`, `last_ws_update_ms`, `last_error`, `requested_limit`, `visible_limit`. Un count < 300 est immédiatement attribuable.
- **Refresh non bloquant + swap atomique** : `_refresh()` tourne déjà en tâche de fond (`asyncio.create_task`) ; il **construit** le nouveau snapshot puis publie `_tickers`/`_universe_set`/`_ranked` ensemble. Sur échec REST transitoire → **dernier snapshot conservé** (jamais blanchi), `universe_refresh_errors_total` + `last_error` renseignés.
- **Métriques** (`/metrics`) : `universe_refresh_total`, `universe_refresh_errors_total`, `universe_refresh_latency_ms`, `universe_symbols_loaded`, `universe_symbols_eligible`, `universe_cache_age_ms`.
- **Métriques selected-market : n/a → unavailable/stale explicite** (`frontend/app.js`, `setMicroUnavail`) : plus de `n/a` muet sur le panneau micro. Chaque cellule indisponible affiche `unavail` + **raison au survol** : `spread/imbalance` → « sélectionne le symbole pour streamer son bookTicker » ; `depth/slippage` → « pas de carnet live (L2 réservé au symbole sélectionné) » ; `trade pressure` → « pas de feature row trades agrégés » ; `relative volume` → « historique 24h insuffisant ». Spread/depth/imbalance/slippage sont **réels** quand le hub Tier 3 streame le symbole ; sinon honnêtement indisponibles. Aucun mock.
- **Rows univers** enrichies (additif, non cassant) : `native_symbol`, `updated_at`. Frontend déjà **windowed** (`MAX_VISIBLE_SYMBOLS`, défaut 60 lignes DOM), recherche debounced sur le snapshot complet, re-render throttlé — inchangé.
- **Tests** (`tests/test_market_universe.py`, +12) : `rejection_reason` partition, `stablecoin_filter_keeps_valid_altcoins` (garde JUP), `universe_loads_300_when_eligible`, `does_not_cap_to_66`, `returns_all_eligible_when_fewer_than_limit`, `debug_counts_rejected_reasons`, `cache_returns_previous_snapshot_on_refresh_failure`. **132 tests** au total.
- **Diagnostic « pas 300 »** : `GET /api/market/universe/debug` → si `excluded_low_volume_count` est élevé, baisser `MIN_QUOTE_VOLUME` ; si `eligible_symbols_count` ≥ 300 mais `final_universe_count` < 300, vérifier `UNIVERSE_LIMIT`/`BACKEND_MAX_SYMBOLS` ; si `raw_binance_tickers_count` faible/0, REST Binance KO (`last_error`).

### Contexte marché global — macro tier (PR6)
**But** : 1ʳᵉ tranche du « rapport crypto expert » (deep-research) — donner au cockpit le **backdrop macro** que les tiers Binance-only n'ont pas, en restant local/simple et **sans clé API**.

**Hub macro in-process, display-only** (`market/global_context.py` → `GlobalContextHub`, hébergé par l'API dans `lifespan` si `ENABLE_GLOBAL_CONTEXT`, défaut True). **Quatrième tier d'affichage**, distinct de la persistance/bot : il ne nourrit jamais le scorer ni la DB. Une tâche de fond re-poll toutes les `GLOBAL_CONTEXT_REFRESH_SECONDS` (60) trois sources **gratuites, sans clé, ToS-safe**, chacune derrière son sous-toggle :
- **CoinGecko** `/api/v3/global` (`ENABLE_COINGECKO`) → total market cap, volume 24h, dominance BTC/ETH, var. mcap 24h.
- **DefiLlama** `/v2/chains` (`ENABLE_DEFILLAMA`) → TVL DeFi total (somme des TVL par chaîne) + top chains.
- **alternative.me** `/fng/` (`ENABLE_FEAR_GREED`) → indice Fear & Greed [0,100] + classification.

**Données réelles uniquement** (règle absolue PR2) : parsers **purs et testés** (`parse_coingecko_global`/`parse_defillama_chains`/`parse_fng`/`fng_band`) renvoient `None` si la réponse est inutilisable → le hub ne publie **jamais** un zéro/vide comme une lecture. Chaque source porte `real`/`stale`/`error`/`age_ms` : jamais répondue ⇒ `real=false` + valeurs nulles (UI `n/a`) ; défaillance transitoire ⇒ **dernière bonne valeur conservée** (jamais blanchie). `ENABLE_COINGECKO` est **repurposé** (ancien worker fantôme jamais câblé → sous-toggle macro CoinGecko, défaut True).

- **Endpoints** : `GET /api/market/global` (3 blocs `market`/`defi`/`sentiment`). Blocs ajoutés à `GET /api/market/source` (`global`) et `GET /api/health` (`global_context`). `GET /api/binance/config` expose `global_context_enabled`.
- **Front** : **barre macro** (`#macro-bar`, sous la barre portefeuille) — Total Mkt Cap, 24h Volume, Dominance BTC/ETH, var. mcap 24h, DeFi TVL, Fear & Greed + sources live ; `fetchGlobalContext()` poll 30 s ; cellule indisponible = `n/a` (jamais fabriquée), valeur périmée atténuée ; barre masquée si désactivé.
- **Observabilité** (`/metrics`) : `global_context_refresh_total{source}`, `global_context_refresh_errors_total{source}`, `global_context_refresh_latency_ms{source}`, `global_total_market_cap_usd`, `global_btc_dominance_pct`, `global_defi_tvl_usd`, `global_fear_greed_index`.
- **Config** (voir `.env`) : `ENABLE_GLOBAL_CONTEXT`, `ENABLE_COINGECKO`, `ENABLE_DEFILLAMA`, `ENABLE_FEAR_GREED`, `GLOBAL_CONTEXT_REFRESH_SECONDS`, `GLOBAL_CONTEXT_HTTP_TIMEOUT`, `GLOBAL_CONTEXT_STALE_MS`, `COINGECKO_API_BASE`, `COINGECKO_API_KEY` (Demo optionnelle), `DEFILLAMA_API_BASE`, `FEAR_GREED_API_BASE`. Tests offline : `tests/test_global_context.py` (parsers + honnêteté real-data-only : snapshot vide, dernière valeur conservée sur erreur, staleness, source désactivée).

### DeFi par protocole — ranked-list tier (PR7)
**But** : 2ᵉ tranche du « rapport crypto expert » (deep-research) — passer du **macro DeFi-TVL** (PR6) au **niveau protocole** : top protocoles DeFi par TVL, sans nouvelle dépendance ni clé API.

**Hub ranked-list in-process, display-only** (`market/defi.py` → `DefiHub`, hébergé par l'API dans `lifespan` si `ENABLE_DEFI_PROTOCOLS`, défaut True). **Tier liste classée** (comme l'univers Binance), distinct du macro tier (scalaires) : une tâche de fond re-poll DefiLlama `/protocols` (gratuit, sans clé) toutes les `DEFI_PROTOCOLS_REFRESH_SECONDS` (120) et publie en **swap atomique** la liste top-N par TVL + un breakdown TVL par catégorie + un total suivi.

**⚠️ Bruit vs vrai DeFi** : `/protocols` renvoie ~7,6k entrées **dominées en TVL par des CEX** (réserves Binance/OKX/Bitfinex) et des rows `Chain` — ce ne sont **pas** des protocoles DeFi. Catégories `CEX`/`Chain` **exclues par défaut** (`DEFI_EXCLUDE_CATEGORIES`) → le panneau ranke du vrai DeFi (Lido, Aave V3, EigenLayer, Morpho…). Plancher `DEFI_PROTOCOLS_MIN_TVL` (1M) = garde-bruit.

**Données réelles uniquement** (règle PR2) : helpers **purs et testés** (`is_defi_protocol`/`protocol_row`/`rank_protocols`/`category_breakdown`/`total_tracked_tvl`). Pas de donnée ⇒ liste vide + `connected:false` (jamais un protocole fabriqué) ; échec REST transitoire ⇒ **dernier bon snapshot conservé** (jamais blanchi). Mémoire bornée par `DEFI_PROTOCOLS_LIMIT` (seul le top-N est retenu, pas les 7,6k).

- **Endpoints** : `GET /api/market/defi?limit=50` (`protocols`/`categories`/`total_tracked_tvl_usd` + `real`/`stale`/`error`/`age_ms`). Blocs ajoutés à `GET /api/market/source` (`defi_protocols`) et `GET /api/health` (`defi_protocols`). `GET /api/binance/config` expose `defi_protocols_enabled`.
- **Front** : **modale 🏦 DeFi** (bouton header) — table rang/nom/catégorie/chaînes/TVL/24h/7j + breakdown catégories ; statut `DefiLlama · live/stale` ou vide honnête.
- **Observabilité** (`/metrics`) : `defi_protocols_refresh_total`, `defi_protocols_refresh_errors_total`, `defi_protocols_refresh_latency_ms`, `defi_protocols_loaded`, `defi_tracked_tvl_usd`.
- **Config** (voir `.env`) : `ENABLE_DEFI_PROTOCOLS`, `DEFI_PROTOCOLS_LIMIT`, `DEFI_PROTOCOLS_MIN_TVL`, `DEFI_PROTOCOLS_REFRESH_SECONDS`, `DEFI_PROTOCOLS_STALE_MS`, `DEFI_EXCLUDE_CATEGORIES` (réutilise `DEFILLAMA_API_BASE`/`GLOBAL_CONTEXT_HTTP_TIMEOUT`). Tests : `tests/test_defi.py` (exclusion CEX/Chain, plancher TVL/sort/cap/rank, breakdown catégories, honnêteté hub vide).
- **Tranches deep-research suivantes** (non faites) : on-chain (Etherscan/Glassnode, **nécessite clés** → secrets), extension news/sentiment RSS, agrégateurs prix (CoinGecko markets).

### Daily Crypto Intelligence Report — advisory tier (PR8)
**But** : un **rapport conseil crypto quotidien** (généré automatiquement à minuit) sur les **~300 cryptos** de l'univers, **compréhensible par un débutant** mais crédible financièrement. Détail complet : [docs/daily_crypto_report.md](docs/daily_crypto_report.md).

**Module isolé `reports/`** (logique pure vs I/O, comme le reste du repo), **display/report-only** (ne nourrit jamais le bot ni la persistance marché) :
- **`reports/scoring.py`** — formules **pures** centralisées (**source unique de vérité** des chiffres) : ratios (momentum, volume-confirmation via percentile+VWAP, liquidité, force vs BTC, qualité de tendance, volatilité, drawdown, market-context), **Opportunity Score** `0–100` (poids : momentum 25 / volume 20 / liquidité 20 / force-BTC 15 / trend 10 / macro 5 / confiance 5), **Risk Score** `0–100` (vol 30 / drawdown 25 / spread 15 / illiquidité 20 / données manquantes 10), **Confidence** (plafonnée car seul l'horizon 24h est réel), **rating A+→E**, **signal BUY/HOLD/SELL/AVOID** (ordre AVOID→BUY→SELL→HOLD), **prédiction** `up_probability` **bornée [0.15, 0.85]** (jamais 0/100 %).
- **`reports/generator.py`** — `build_daily_report(rows, global_context, …)` → JSON structuré + `render_markdown()` → Markdown FR (résumé exécutif, classement, prédictions, ratios, source evidence, disclaimer « pas un conseil financier »).
- **`reports/store.py`** — **fichiers = source de vérité** (`reports/daily_crypto_report_YYYY-MM-DD.json|.md`, gitignored) + **index DB best-effort** (`daily_crypto_report` + `daily_crypto_asset_score`, migration **008**, aussi créé au runtime via `ensure_schema`).

**Real data only** (règle PR2) : le ticker Binance 24h fournit prix/%24h/volume/trades/spread/high-low/open/VWAP ; **1h/7j/30j et market cap = `N/A`** (réduisent la confiance, jamais fabriqués). Tier macro (`global_context`) fournit régime/Fear&Greed/mcap 24h.

**Worker** `workers/report_worker.py` (supervisé) : prochain minuit dans `DAILY_REPORT_TIMEZONE` (fallback UTC), lit les tiers live via l'API locale, génère, persiste. Relance manuelle : `python -m workers.report_worker --once` ou `POST /api/reports/daily/generate` (génère depuis les hubs in-process). Métriques port **9107**.

**Endpoints** (port 8000) : `GET /api/reports/daily/latest`, `GET /api/reports/daily/{date}`, `GET /api/reports/daily/history`, `POST /api/reports/daily/generate`, `GET /api/reports/daily/latest/assets/{symbol}`. `GET /api/binance/config.daily_report_enabled` + bloc `daily_report` dans `GET /api/health`.

**Front** : bouton header **📅 Report** → modale « Rapport Crypto Quotidien » (résumé + KPIs, distribution des ratings, top BUY/SELL/à-surveiller, table **filtrable signal+rating / triable / recherchable** des 300, détail au clic avec prédiction & ratios). Disclaimer visible.

**Config** (`.env`) : `ENABLE_DAILY_REPORT`, `DAILY_REPORT_HOUR/MINUTE/TIMEZONE`, `DAILY_REPORT_DIR`, `DAILY_REPORT_UNIVERSE_LIMIT`, `DAILY_REPORT_TOP_N`, `DAILY_REPORT_HISTORY_LIMIT`, `DAILY_REPORT_API_BASE`, `DAILY_REPORT_HTTP_TIMEOUT`, `DAILY_REPORT_PERSIST_DB`. Tests : `tests/test_daily_report.py`. **Amélioration future** : la table `daily_crypto_asset_score` prépare le backtest **prédiction-vs-réalisé** J+1/J+7 ; enrichissement 1h/7j/30j + market cap via CoinGecko markets ; export PDF/email/Telegram.
- **Donnée additionnelle** : `market/universe.py` `to_row()` expose désormais `open`/`high`/`low`/`weighted_avg_price`/`volatility_range` (réels Binance 24h, additif non cassant) — consommés par le rapport.

### Supervisor & Ops / Terminals (PR2)
Lancement unique du stack local (ne dépend plus de multiples terminaux Windows) :
```
python scripts/dev_supervisor.py
```
- **`workers/process_supervisor.py`** : cœur réutilisable et testable. Possède le cycle de vie des process enfants, capture stdout/stderr ligne à ligne, classe le niveau (INFO/WARNING/ERROR/CRITICAL), **accumule les tracebacks Python**, auto-restart avec **backoff exponentiel** borné par un budget glissant (`OPS_MAX_RESTARTS` / `OPS_RESTART_WINDOW_S`) → état `degraded` + **incident structuré** (format Phase 9) au-delà.
- **`scripts/dev_supervisor.py`** : construit la liste des process **uniquement à partir des fichiers réellement présents** (jamais de worker supposé) : `docker compose up -d` (oneshot) → `workers.bootstrap` (oneshot) → `ingestor`/`aggregator`/`feature_worker`/`social_ingestor`/`antigravity_bot` → `uvicorn api.main:app` (:8000). Sert l'**Ops API** sur `:8050` (`config.OPS_HOST/OPS_PORT`).
- **Endpoints Ops** (port 8050) : `GET /api/ops/status|processes|events|incidents|health`, `POST /api/ops/process/{start,stop,restart}` (body `{"name": ...}`), `POST /api/ops/frontend-error`, `WS /ws/ops` (flux temps réel : `log` / `status` / `incident`). Aucun shell brut — uniquement ces actions contrôlées.
- **Cockpit** : panneau **🖥 Ops** (header) — statut/PID/uptime/restarts/dernier log/dernier traceback par process, boutons start/stop/restart, logs temps réel filtrables (process + niveau), badge `Ops n/m` en header. Base configurable via `window.OPS_URL` (défaut `http://<host>:8050`).
- **Incidents** : persistés dans `logs/ops_incidents.jsonl` + diffusés sur `/ws/ops`. Brancher un webhook/Claude réel = `ProcessSupervisor.on_incident` (point d'extension, jamais d'incident fabriqué).

### Lancement local (runbook)
**En un clic depuis VS Code** : `Terminal → Run Task…` puis choisir :
- **Start Dev Supervisor** — lance le supervisor (docker + bootstrap + workers + API) dans un terminal dédié. Commande exécutée : `$env:PYTHONPATH="."; python .\scripts\dev_supervisor.py`.
- **Start Full Stack** — ouvre le supervisor dans une fenêtre dédiée **et** le cockpit dans le navigateur (ne relance rien que le supervisor possède déjà).
- **Stop Full Stack** — stoppe supervisor + workers + uvicorn puis `docker compose down`.
- **Run tests (offline)** — `pytest -q`.

**En PowerShell** (depuis la racine du repo) :
```powershell
# script robuste (venv auto, URLs, garde-fous)
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_dev_supervisor.ps1
# ou la commande directe :
$env:PYTHONPATH="."; python .\scripts\dev_supervisor.py
# full stack (nouvelle fenêtre + cockpit) :
.\scripts\start_all.ps1
# arrêt :
.\scripts\stop_all.ps1
```

**Vérifier que tout fonctionne** :
- Cockpit : http://localhost:8000/ — badge header `Ops n/m` doit passer au vert.
- Ops API : http://localhost:8050/api/ops/health (statut, running/total).
- Panneau **🖥 Ops** : process `running`, logs temps réel qui défilent.
- Marché : badge passe `Connecting… → Waiting data → Live` ; **jamais `Live` sans bougie réelle** (`No data`/`Waiting data`/`STALE` sinon).

**Problèmes fréquents** :
- **`Jeton inattendu « python »` / token error** : tu as écrit `$env:PYTHONPATH="." python ...`. ✅ Correct = **`$env:PYTHONPATH="."; python .\scripts\dev_supervisor.py`** (point-virgule entre l'affectation et la commande — PowerShell n'autorise pas deux commandes adjacentes).
- **Port 8050 déjà occupé** : un supervisor tourne déjà → `Stop Full Stack` (ou `.\scripts\stop_all.ps1`) avant de relancer.
- **`Ops API unavailable`** dans le panneau : le supervisor n'est pas lancé → `Start Dev Supervisor`. URL ajustable via `window.OPS_URL` (console navigateur) si tu changes `OPS_PORT`.
- **Python/venv absent** : `python -m venv venv ; .\venv\Scripts\pip install -r requirements.txt`.
- **`.ps1` bloqué par l'ExecutionPolicy** : les tâches/commandes utilisent `-ExecutionPolicy Bypass` ; en manuel, lance via `powershell -ExecutionPolicy Bypass -File ...`.

### Ports
| Service | Port |
|---|---|
| API / cockpit | 8000 |
| Ops supervisor (API + WS) | 8050 |
| Prometheus workers | 9101–9107 |
| PostgreSQL/TimescaleDB | 5432 |
| Redis | 6379 |

### Tests
`pytest -q` (offline, pas de DB requise) : `test_scorer_thresholds`, `test_social_availability` (garde-fous anti-mock), `test_process_supervisor` (capture stdout/stderr + crash + traceback sur **vrais** subprocess), `test_ops_api` (logique Ops + routes), `test_launch_scripts` (tasks.json + scripts PowerShell valides), `test_engine_decimal`, `test_binance_spot` (hub temps réel + ranges/active-symbol/cache borné), `test_chart_live` (feed graphique : anti-freeze, statut CHART, isolation par symbole), `test_market_universe` (univers 300 : exclusions, score tendance, ranking), `test_deploy_config` (.dockerignore + durcissement compose + settings), `test_rss_collector` (parsing RSS 2.0/Atom réel, strip HTML, dates, anti-mock, politesse), `test_outcome_eval` (return/correctness/horizon — logique pure du backtest), `test_global_context` (macro tier : parsers CoinGecko/DefiLlama/Fear&Greed + honnêteté real-data-only), `test_defi` (tier DeFi : exclusion CEX/Chain, plancher TVL/ranking, breakdown catégories, honnêteté hub vide + `total=null` sur set vide + dernier snapshot sur échec), `test_decision_evidence` (assemblage `source_evidence` réel par groupe market/risk/social), `test_daily_report` (rapport conseil quotidien : ratios bornés/directionnels, bandes de rating, signaux BUY/HOLD/SELL/AVOID aux seuils, robustesse données manquantes, prudence des prédictions ∈[0.15,0.85], assemblage JSON + Markdown, univers simulé 300 + perf, round-trip store, scheduler). **237 tests** au total. CI : `.github/workflows/ci.yml` (`pytest -q`) en plus de `docs-check`.

## Gestion automatique des terminaux par Claude

### Objectif
Claude doit pouvoir lancer, superviser et gérer **automatiquement** tous les processus du stack local, sans demander à l'utilisateur d'ouvrir plusieurs terminaux. Une **commande unique** démarre et supervise l'ensemble :

```bash
python scripts/dev_supervisor.py
```

Le superviseur capture les logs stdout/stderr, détecte les tracebacks Python, classe les niveaux, relance les process autorisés avec backoff, expose un état temps réel (HTTP + WS) et persiste les incidents. Implémentation réelle :
- **`workers/process_supervisor.py`** — cœur réutilisable et testé (cycle de vie, capture des flux, classification, incidents). Source unique de vérité de l'état des process.
- **`scripts/dev_supervisor.py`** — entrypoint : construit la liste des process **uniquement à partir des fichiers réellement présents sur disque** (`_exists(...)`, jamais de worker supposé) et sert l'**Ops API** sur `:8050`.

### Processus lancés automatiquement
Liste **réelle**, vérifiée par rapport au disque (`scripts/dev_supervisor.build_specs()`). Tous les fichiers existent. Les process sont lancés dans cet ordre ; les `oneshot` sont attendus jusqu'à complétion avant la suite.

| Process | Commande | Type | Optionnel | Auto-restart |
|---|---|---|---|---|
| `docker` | `docker compose up -d` | oneshot | oui (si compose présent) | non |
| `bootstrap` | `python -m workers.bootstrap` | oneshot | non | non |
| `ingestor` | `python -m workers.ingestor` | long-running | non | oui |
| `aggregator` | `python -m workers.aggregator` | long-running | non | oui |
| `feature_worker` | `python -m workers.feature_worker` | long-running | non | oui |
| `social_ingestor` | `python -m workers.social_ingestor` | long-running | non | oui |
| `antigravity_bot` | `python -m workers.antigravity_bot` | long-running | non | oui |
| `outcome_evaluator` | `python -m workers.outcome_evaluator` | long-running | non | oui |
| `report_worker` | `python -m workers.report_worker` | long-running | non | oui |
| `api` | `python -m uvicorn api.main:app --host 127.0.0.1 --port 8000` | long-running | non | oui |

Notes de vérité (ne pas s'en écarter) :
- **Pas de process frontend séparé.** Le cockpit est servi par l'API elle-même (`api/main.py` monte `StaticFiles` sur `/` au port 8000). Ne **jamais** lancer `python -m http.server 8000` — cela entrerait en conflit avec l'API sur le même port.
- **`--reload` est volontairement omis** sous supervision pour que le PID suivi soit le vrai serveur, pas le process reloader parent.
- **`social_ingestor`** tourne mais ne produit de la donnée *réelle* que si une vraie source est branchée : activer **`ENABLE_RSS_SOCIAL=True`** (flux RSS news publics, vraie source) ; sinon avec `ENABLE_RSS_SOCIAL=False` **et** `ENABLE_MOCK_SOCIAL=False` (défauts) il reste idle, aucun signal réel (voir règle anti-mock PR2).
- **`outcome_evaluator`** : évaluation ex-post (read-mostly) ; remplit `outcome_eval` + `source_influence_snapshot` et met à jour `tracked_actor.influence_score`. Gated `ENABLE_OUTCOME_EVAL` (défaut True).
- Règle absolue : **ne pas inventer de worker** et **ne pas documenter une commande dont le fichier n'existe pas**. Si un fichier est absent, le superviseur le saute silencieusement (`optional`) ou ne l'ajoute pas.

### Capture des logs & erreurs
`process_supervisor` lit stdout/stderr **ligne par ligne** et classe chaque ligne en `DEBUG/INFO/WARNING/ERROR/CRITICAL` (`detect_level`) :
- token de niveau explicite prioritaire ; sinon, une ligne stderr ressemblant à une exception → `ERROR`.
- **Tracebacks Python accumulés** : du header `Traceback (most recent call last):` jusqu'à la ligne de résumé d'exception (`TypeError`, `ValueError`, `ConnectionError`, `asyncpg.*Error`, `*Exception`, `*Timeout`…), flushés en un seul `last_traceback` structuré.
- Chaque process garde `last_log`, `last_log_level`, `last_traceback`, `recent_logs` (200 dernières lignes), `restarts`, `pid`, `uptime`.

### Incidents (format réel)
Au-delà des logs, le superviseur émet un **incident structuré** (crash, autorestart, spawn échoué, crash-loop). Il est **persisté** dans `logs/ops_incidents.jsonl`, diffusé sur `/ws/ops`, et passé au hook `ProcessSupervisor.on_incident` (point d'extension webhook/Claude — jamais d'incident fabriqué). Schéma réel émis par `_raise_incident` :

```json
{
  "incident_id": "inc-<process>-<seq>-<ts>",
  "severity": "warning | error | critical",
  "process": "antigravity_bot",
  "symbol": null,
  "started_at": 0,
  "last_seen_at": 0,
  "error_type": "TypeError",
  "exit_code": 1,
  "traceback": "...",
  "recent_logs": ["...20 dernières lignes..."],
  "health_status": {"status": "crashed", "restarts": 2},
  "market_data_freshness": null,
  "suspected_root_cause": "...",
  "recommended_action": "..."
}
```

Quand Claude résume un incident à l'utilisateur, il en dérive une vue courte (`process`, `severity`, `error_type`, `message`, `impact`, `recommended_fix`) **sans jamais masquer l'erreur ni la fabriquer** — la source reste l'incident persisté ci-dessus.

### Règles de redémarrage (valeurs réelles, configurables via `.env`)
- Auto-restart d'un process crashé **si `autorestart=True`**.
- **Budget glissant** : max `OPS_MAX_RESTARTS` (défaut **5**) crashs dans une fenêtre `OPS_RESTART_WINDOW_S` (défaut **120 s**).
- **Backoff exponentiel** avant relance : `delay = min(30 s, 1 s × 2^restarts)` → 1, 2, 4, 8, 16, puis plafond 30 s. *(Ce ne sont pas des paliers fixes 2/5/10 s.)*
- Au-delà du budget → statut **`degraded`** + incident **critical**, l'auto-restart s'arrête (pas de boucle infinie).
- Statuts possibles d'un process : `pending | starting | running | stopped | crashed | degraded | completed`.
- **Ne jamais masquer une erreur** : elle est journalisée, diffusée sur le WS et affichée dans le cockpit.
- **Ne jamais faire de modification destructive** (suppression de données, reset DB) sans validation explicite de l'utilisateur.

### Ops API & WebSocket (port 8050)
Aucun shell brut n'est exposé — uniquement ces actions contrôlées (`config.OPS_HOST`/`OPS_PORT`) :
- `GET /api/ops/status` · `GET /api/ops/health` · `GET /api/ops/processes`
- `GET /api/ops/events?limit&level&process` · `GET /api/ops/incidents?limit`
- `POST /api/ops/process/{start|stop|restart}` — body `{"name": "<process>"}`
- `POST /api/ops/frontend-error` — funnel des erreurs JS du cockpit vers le superviseur
- `WS /ws/ops` — flux temps réel ; types d'événements : `snapshot` (état initial), `log`, `status`, `incident`

Chaîne temps réel :
```text
process stdout/stderr → ProcessSupervisor → Ops API (:8050) → /ws/ops → panneau cockpit « Ops / Terminals » → résumé d'incident Claude
```

### Cockpit — panneau « 🖥 Ops / Terminals »
Servi par le cockpit (header), il lit l'Ops API (`window.OPS_URL`, défaut `http://<host>:8050`) et affiche par process : **nom · statut · PID · uptime · nombre de restarts · dernier log · dernier traceback**, des boutons **start / stop / restart**, des logs temps réel **filtrables (process + niveau)**, et un badge `Ops n/m` en header.

### Règle Claude (obligatoire)
- Claude **ne doit pas** demander à l'utilisateur d'ouvrir manuellement 4–5 terminaux : un superviseur existe (`python scripts/dev_supervisor.py`).
- Privilégier dans l'ordre : (1) la **commande unique** de lancement ; (2) la **supervision centralisée** ; (3) la **capture automatique des logs** ; (4) une **remontée claire des erreurs/incidents** ; (5) cette documentation maintenue à jour.
- Toute action sur les process passe par l'Ops API contrôlée — jamais par un shell brut non supervisé.

## Politique sources
Sources prioritaires:
1. code existant
2. schéma interne
3. docs officielles des APIs
4. seulement ensuite hypothèses

Si une source n’est pas officiellement supportée ou pose un risque légal / ToS, le dire explicitement et proposer une alternative sûre.

## Politique UI
Le cockpit doit permettre de voir en direct:
- état des collecteurs
- fraîcheur des flux
- erreurs / DLQ
- features et scores par actif
- décisions du bot
- portefeuille paper trading
- exécutions et coût estimé
- performance et santé système

## Politique Claude
Quand tu réalises une tâche:
- commence par comprendre l’existant
- résume l’écart à corriger
- propose le plus petit changement crédible
- implémente proprement
- valide la cohérence de bout en bout
- rends un résumé bref, concret, orienté fichiers modifiés et risques

Évite:
- les longues introductions
- les abstractions inutiles
- les hypothèses non vérifiées
- les changements non traçables
- les effets de bord cachés

## Définition de done
Une tâche est considérée terminée seulement si:
- le changement est cohérent avec l’architecture du dépôt
- les données circulent proprement
- la fonctionnalité est observable
- les risques sont signalés
- le système reste maintenable localement

## Règle de fin de conversation
**À la fin de chaque conversation ayant produit des modifications de fichiers** :
1. `git add` sur tous les fichiers modifiés/créés liés à la tâche (pas de `git add -A` global).
2. `git commit -m "feat/fix/chore: <résumé concis en une ligne>"` — message en français ou anglais, cohérent avec l’historique du repo.
3. Si un remote est configuré : `git push origin main` — voir **## Git workflow obligatoire** (push immédiat, sans confirmation, jamais de `force push`).
4. Signaler les fichiers volontairement exclus du commit (secrets, temporaires, artefacts de build).

## Git workflow obligatoire

Après chaque modification réelle du projet, Claude doit automatiquement faire un commit et un push.

Procédure obligatoire après chaque changement :

1. Vérifier les fichiers modifiés :

```bash
git status
```

2. Ajouter uniquement les fichiers liés au changement effectué :

```bash
git add <fichiers_modifiés_liés_au_changement>
```

3. Créer un commit clair et ciblé :

```bash
git commit -m "type: description courte du changement"
```

4. Pousser immédiatement sur le remote :

```bash
git push origin main
```

Règles importantes :

- Ne jamais faire de commit global aveugle avec `git add .` sauf si tous les fichiers modifiés sont explicitement liés au changement.
- Ne jamais inclure les fichiers temporaires, rapports locaux, fichiers non liés ou fichiers générés inutilement.
- Ne jamais faire de `force push`.
- Si le push est rejeté, arrêter l’action et afficher l’erreur.
- Si des fichiers non liés sont déjà modifiés avant l’intervention, les laisser hors commit.
- Chaque commit doit correspondre à un changement logique clair.
- Après chaque push, confirmer :
  - le hash du commit ;
  - les fichiers inclus ;
  - que le push est passé ;
  - que le repo distant est synchronisé.
