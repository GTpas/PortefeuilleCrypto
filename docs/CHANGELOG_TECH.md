# Changelog technique

> Journal des **décisions techniques significatives** (pas un changelog produit). Une ligne par changement notable : quoi, pourquoi, fichiers/impact. **À mettre à jour à chaque modification technique** (voir [CONTRIBUTING.md](../CONTRIBUTING.md)).

Format : `## [date] — titre court` puis **Quoi / Pourquoi / Impact**.

---

## [2026-06-10] — Daily Crypto Intelligence Report (advisory tier, PR8)
- **Quoi** : nouveau module **`reports/`** (display/report-only) + worker `workers/report_worker.py` générant un **rapport conseil crypto quotidien** (minuit, TZ configurable) sur les ~300 cryptos de l'univers : classement global, prédiction indicative prudente, signal **BUY/HOLD/SELL/AVOID**, ratios explicables, rating **A+→E**, explication pédagogique FR, source evidence réelle, historique.
  - `reports/scoring.py` — formules **pures** centralisées (source unique de vérité) : ratios (momentum, volume-confirmation via percentile+VWAP, liquidité, force vs BTC, qualité tendance, volatilité, drawdown, market-context), Opportunity/Risk/Confidence `0–100`, rating, signal (AVOID→BUY→SELL→HOLD), `up_probability` **bornée [0.15,0.85]**.
  - `reports/generator.py` — `build_daily_report()` (JSON) + `render_markdown()` (FR). `reports/store.py` — fichiers `reports/*.json|.md` = **source de vérité** + index DB best-effort.
  - API : `GET /api/reports/daily/{latest,history,{date},latest/assets/{symbol}}` + `POST /generate` ; `config.daily_report_enabled` ; bloc `health.daily_report`. Migration **008** (`daily_crypto_report` + `daily_crypto_asset_score`, aussi `ensure_schema` au runtime). Métriques port **9107**. Front : modale **📅 Report** (filtres signal/rating, tri, recherche, détail).
  - `market/universe.to_row()` expose `open/high/low/weighted_avg_price/volatility_range` (réels Binance 24h, additif non cassant) — consommés par le rapport.
- **Pourquoi** : 1ʳᵉ brique « conseil » lisible pour non-expert, crédible et **honnête** (real-data-only) ; prépare le backtest prédiction-vs-réalisé (table `daily_crypto_asset_score`).
- **Impact** : `reports/*`, `workers/report_worker.py`, `api/main.py`, `config.py`, `metrics.py`, `market/universe.py`, `db/migrations/008`, `scripts/dev_supervisor.py`, frontend, `.env`/`.gitignore`/`.dockerignore`. **Real data only** : 1h/7j/30j + market cap = `N/A` (confiance réduite, jamais fabriqué) ; prédictions = probabilités/scénarios, **pas un conseil financier**. Tests `tests/test_daily_report.py` (**237 tests** au total). Doc : `docs/daily_crypto_report.md`.

## [2026-06-10] — Refonte frontend cockpit (design system v3, layout robuste, a11y)
- **Quoi** : refonte **visuelle et structurelle** du cockpit, **vanilla** (pas de React/Vite/build), **sans casser** endpoints/WS/anti-freeze chart.
  1. **`frontend/style.css` réécrit en design system v3** : tokens couleurs (surfaces/borders/texte/sémantiques up-down-warn-info-social-accent + tints), variables de layout (`--header-h`/`--portfolio-h`/`--macro-h`/`--activity-h`/`--left-w`/`--right-w`/`--panel-gap`/`--radius-card`/`--shadow-card`), utilitaires (`.metric-card`/`.metric-label`/`.metric-value`/`.data-chip`/`.skeleton`/`.truncate`/`.scroll-panel`). **Tous les noms de classes consommés par `app.js` sont préservés** ; `--up/--down` gardés identiques aux couleurs de série du chart.
  2. **Layout robuste** : `.app-container` = grid `auto / minmax(0,1fr) / var(--activity-h)` en `100dvh`, `overflow:hidden`. Nouveau wrapper **`.top-stack`** (header + KPI portfolio + macro) → ces barres ne sont **plus coupées**. `.cockpit-grid` 3 colonnes `minmax()` ; chaque panneau `min-height:0` + scroll interne (`.scroll-panel`) → plus de chevauchement, l'activity feed a son propre scroll.
  3. **`frontend/index.html` restructuré** (IDs/`data-*` **inchangés**) : barre portfolio en **cartes KPI**, header en 3 clusters (statut live/stale/offline · tools · drawer), modales **déplacées hors** de `.app-container` (évite tout piège de containing-block), skeletons de chargement (watchlist/signals).
  4. **Panneau droit = drawer responsive** : ≤1100px le panneau « Decision Intelligence » devient off-canvas (bouton header `#toggle-right`, `#close-right`, backdrop, `Esc`) ; ≤880px layout 1 colonne (watchlist en haut, capée). CSS-driven, JS minimal (`setupRightDrawer`).
  5. **Accessibilité** : `:focus-visible` global, `aria-label` sur boutons-icônes, états live/stale/offline **textuels** (pas couleur seule), `prefers-reduced-motion` (coupe pulse/shimmer/anim), lignes watchlist `role="button"`/`tabindex`/clavier `Enter/Espace`, contraste texte relevé (`--text-muted`/`--text-secondary`).
  6. **Décision plus explicable** : carte signal enrichie d'une ligne **« why »** réelle (`explainReason(reason_code, s_total)` — `reason_code` déjà servi par `/api/signals`, jamais fabriqué).
  7. **Fix chart resize** : le `ResizeObserver` **n'applique plus jamais** `height/width = 0` (collapse irrécupérable de Lightweight-Charts pendant une transition drawer/layout).
- **Pourquoi** : le cockpit « terminal brut » coupait header/KPI/macro, chevauchait des zones et compressait le panneau droit ; demande d'une refonte produit moderne, accessible et crypto-native, **incrémentale** et compatible avec l'archi existante.
- **Impact** : **3 fichiers front** (`index.html`/`style.css`/`app.js`) + docs. **Aucun** endpoint/WS/worker/migration touché ; anti-freeze chart (`chartStore`/`chartApplyCandle`) **intact** ; bornes mémoire (`MAX_VISIBLE_SYMBOLS`/ring buffers/throttle) **conservées** ; règle anti-mock respectée (états honnêtes `n/a`/`unavail`/`stale`/`no data`). Aucun nouveau test (changement front pur) — `pytest` (216) inchangé. Docs : `FRONTEND.md` (zones, design system, drawer, a11y).

## [2026-06-09] — Source Evidence réelle dans le Decision Drill-down
- **Quoi** : nouveau module `api/decision_evidence.py` (`assemble_source_evidence` + `freshness_status` — **purs, testés offline**) qui construit un bloc **`source_evidence`** structuré et traçable, ajouté à la réponse de `GET /api/decision/{decision_id}` (champs existants `snapshot`/`factors`/`quality_audit`/`evidence` **inchangés** = rétro-compatible). Les facteurs persistés (`decision_factor`) sont **groupés par catégorie** (Market/Risk/Social) avec `value`/`score_contribution`/`explanation` d'origine ; la fraîcheur vient de `signal_quality_audit.{market,social}_data_age_ms` (requête étendue) ; le social est piloté par les **vraies** lignes `decision_evidence_link` (mock filtré `NOT ILIKE 'mock%'`), jamais par le placeholder `social_unavailable`. Front : `renderDecisionSourceEvidence()` remplace le message statique « No real source evidence available » par des cartes par groupe + badges `complete/partial/missing` + warnings (CSS `.ev-*`). Config : `SOURCE_EVIDENCE_AVAILABLE_MS=5000` / `SOURCE_EVIDENCE_STALE_MS=60000`.
- **Pourquoi** : le modal affichait un fallback générique trompeur même quand market/risk étaient disponibles. Rendre **chaque décision traçable** à partir des données déjà persistées, sans recalcul ni source fabriquée.
- **Impact** : **données réelles uniquement** — un groupe avec métriques persistées n'est jamais `unavailable` (au pire `stale` si fraîcheur inconnue) ; absence réelle ⇒ `unavailable` + raison ; échec d'assemblage ⇒ `source_evidence: null` (réponse toujours servie). Pas de migration. Front sans crash sur `null`/groupes/métriques vides + fallback legacy. Tests : +`test_decision_evidence` (10) → **216 tests**. Docs : API.md (format), FRONTEND.md (modal).

## [2026-06-09] — Tier DeFi par protocole (top protocoles par TVL — DefiLlama /protocols)
- **Quoi** : nouveau hub in-process **display-only** `market/defi.py` (`DefiHub`) hébergé par l'API (`lifespan`, gated `ENABLE_DEFI_PROTOCOLS`), qui re-poll DefiLlama `/protocols` (gratuit, sans clé) et publie (swap atomique) une **liste classée top-N par TVL** + un **breakdown TVL par catégorie** + un total suivi. Helpers **purs et testés** (`is_defi_protocol`/`protocol_row`/`rank_protocols`/`category_breakdown`/`total_tracked_tvl`). Endpoint `GET /api/market/defi?limit=50`, blocs ajoutés à `/api/market/source` et `/api/health` (`defi_protocols`), flag `defi_protocols_enabled` dans `/api/binance/config`. Métriques `/metrics` : `defi_protocols_refresh_total`, `defi_protocols_refresh_errors_total`, `defi_protocols_refresh_latency_ms`, `defi_protocols_loaded`, `defi_tracked_tvl_usd`. Front : **modale 🏦 DeFi** (bouton header) — table rang/nom/catégorie/chaînes/TVL/24h/7j + breakdown catégories + statut honnête.
- **Pourquoi** : 2ᵉ tranche du « rapport crypto expert » (deep-research), choisie par l'utilisateur — étendre l'intégration DefiLlama existante (macro TVL) vers le **niveau protocole** sans nouvelle dépendance ni clé. `/protocols` renvoie ~7,6k entrées **dominées en TVL par des CEX** (réserves Binance/OKX/Bitfinex) et des rows `Chain` : ce ne sont **pas** des protocoles DeFi → catégories `CEX`/`Chain` **exclues** (`DEFI_EXCLUDE_CATEGORIES`) pour ranker du vrai DeFi (Lido, Aave, EigenLayer…).
- **Impact** : **données réelles uniquement** — snapshot porte `real`/`stale`/`error`/`age_ms` ; pas de donnée ⇒ liste vide + `connected:false` (jamais un protocole fabriqué) ; échec REST transitoire ⇒ dernier bon snapshot conservé. Mémoire bornée par `DEFI_PROTOCOLS_LIMIT` (top-N retenu, pas les 7,6k). Nouveaux env `ENABLE_DEFI_PROTOCOLS`/`DEFI_PROTOCOLS_*`/`DEFI_EXCLUDE_CATEGORIES` (réutilise `DEFILLAMA_API_BASE`/`GLOBAL_CONTEXT_HTTP_TIMEOUT`). Pas de worker séparé (tourne dans l'API), pas de migration. Tests : +`test_defi` (14, dont la garde real-data-only post-review : set vide ⇒ `total_tracked_tvl_usd=null`, échec transitoire ⇒ dernier snapshot) → **206 tests**. Relu via review multi-agent adversarial (9 findings confirmés appliqués : XSS escaping des chaînes DefiLlama, `total=null` sur set vide, timeout dédié `DEFI_PROTOCOLS_HTTP_TIMEOUT`, pré-filtre 1 passe, User-Agent).

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
