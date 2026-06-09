# Dépannage (Troubleshooting)

> Réflexe : 1) `GET /api/health`, 2) panneau **🖥 Ops** + `GET /api/ops/incidents`, 3) modale **🔬 Source** pour tout écart prix/chart.

## Lancement / supervisor

| Symptôme | Cause probable | Fix |
|---|---|---|
| `Jeton inattendu « python »` | `$env:PYTHONPATH="." python ...` sans `;` | `$env:PYTHONPATH="."; python .\scripts\dev_supervisor.py` |
| `Port 8050 déjà occupé` | Un supervisor tourne déjà | `Stop Full Stack` / `.\scripts\stop_all.ps1` avant de relancer |
| `Ops API unavailable` (panneau) | Supervisor non lancé | `Start Dev Supervisor` ; ajuster `window.OPS_URL` si `OPS_PORT` changé |
| Port 8000 occupé / conflit | Un `http.server` lancé à la main | **Ne jamais** lancer `http.server` sur 8000 ; l'API sert déjà le cockpit |
| `.ps1` bloqué (ExecutionPolicy) | Politique restrictive | lancer via `powershell -ExecutionPolicy Bypass -File ...` |
| Python/venv absent | venv non créé | `python -m venv venv ; .\venv\Scripts\pip install -r requirements.txt` |

## Base de données

| Symptôme | Cause | Fix |
|---|---|---|
| `db_status: down` dans `/api/health` | Conteneur db non démarré / pas healthy | `docker compose ps` ; `docker compose up -d` ; `docker compose logs db` |
| Tables manquantes | Migrations non jouées (volume pré-existant) | Rejouer manuellement (voir [DATABASE.md](DATABASE.md#migrations)) ou `docker compose down -v` (destructif) |
| `asyncpg`/`psycopg2` connexion refusée | mauvaise `DATABASE_URL` / db pas prête | vérifier `.env` ; attendre le healthcheck `pg_isready` |
| DLQ qui grossit (`dead_letter_event`) | erreurs d'écriture batch | inspecter `error_class`/`error_message` ; voir rollback dans [RUNBOOK.md](RUNBOOK.md) |

## Ingestion / fraîcheur

| Symptôme | Cause | Fix |
|---|---|---|
| Symbole `stale`/`no_data` dans `/api/health` | Ingestor/aggregator arrêté, ou WS exchange coupé | Ops → statut ingestor/aggregator ; `market_events_total` doit monter |
| `queue_depth` élevé / events droppés | Back-pressure (DB lente, files pleines `MAX_MARKET_EVENTS`) | vérifier `db_write_latency_ms` ; réduire les symboles ; augmenter `MAX_MARKET_EVENTS` |
| Pas de bougies `ohlcv_1s` | aggregator off, ou pas de trades | vérifier l'aggregator et `trade_tick` |
| Décision bloquée `data_stale` | dernier quote > `MAX_DATA_AGE_S` | comportement attendu (gate de fraîcheur) ; rétablir le flux |

## Cockpit — prix / chart

| Symptôme | Cause | Fix |
|---|---|---|
| Badge jamais `Live` | hub up mais aucun event Binance (`nodata`) | géo-blocage / WS bloqué ; tester `GET /api/binance/debug/{symbol}` ; vérifier `binance_live.connected` |
| **Chart figé** (prix bouge) | feed kline arrêté, ou (historiquement) temps régressif avalé | badge `CHART STALE`/`NO CANDLES` + 🔬 Source (`chart_status`, `candle_age_ms`) ; cf. [FRONTEND.md](FRONTEND.md) |
| Prix ≠ Binance UI | mauvaise `PRICE_SOURCE` / intervalle | 🔬 Source : `raw` vs `displayed` ; pour matcher le header 24h → `PRICE_SOURCE=ticker_last` ; aligner `CANDLE_INTERVAL`/range |
| Mauvais marché affiché | lecture DB non pinnée | déjà corrigé (pin `DISPLAY_EXCHANGE`) ; vérifier qu'une nouvelle requête filtre bien `exchange_code` |

## Univers (Tier 1)

| Symptôme | Cause | Fix |
|---|---|---|
| `Universe n/a` / `core only` | hub off ou REST Binance KO | `GET /api/market/universe/debug` → `last_error` ; `ENABLE_MARKET_UNIVERSE=True` |
| Compte < 300 (ex. « 66 ») | `MIN_QUOTE_VOLUME` trop haut | `debug.excluded_low_volume_count` élevé → baisser `MIN_QUOTE_VOLUME` (défaut 500K) |
| `eligible ≥ 300` mais `final < 300` | cap | vérifier `UNIVERSE_LIMIT` / `BACKEND_MAX_SYMBOLS` |
| `raw_binance_tickers_count` ~0 | REST Binance indisponible | réseau / fallbacks `BINANCE_REST_FALLBACKS` |

> **Diagnostic en une commande** : `python scripts/diagnose_universe.py --limit 300` — wrappe `GET /api/market/universe/debug` et imprime demandé/éligible/chargé + la **partition des rejets** (stable/leverage/volume/not-spot/inactive) avec exemples, une **cause dominante** (`capped_by_limit` / `low_volume_floor` / `rest_unavailable` / `dominated_by_*`) et une recommandation. `--json` pour le brut, `--strict` pour `exit 1` si chargé < demandé. Latence des endpoints d'affichage : `python scripts/benchmark_snapshot_api.py --runs 30` (p50/p95/p99 sur `/api/market/universe` + klines).

## Social

| Symptôme | Cause | Fix |
|---|---|---|
| `SOC n/a` partout | mock off (défaut) et aucune vraie source | comportement **attendu** (anti-mock). Activer `ENABLE_MOCK_SOCIAL=True` **en dev** pour la pipeline |
| social_ingestor « ne fait rien » | idle sans collecteur | normal si `ENABLE_MOCK_SOCIAL=False` |

## Mémoire

| Symptôme | Regarder |
|---|---|
| RAM Docker | `docker stats` (limites `mem_limit` db 1g / redis 320m) |
| RAM back | `GET /api/health.universe.tracked` ≤ `BACKEND_MAX_SYMBOLS` |
| RAM front (onglet lourd) | bornes `frontend_limits` ; watchlist windowed (≤ `MAX_VISIBLE_SYMBOLS`) |

## Diagnostic général
- **Incident structuré** : `GET /api/ops/incidents` ou `logs/ops_incidents.jsonl` (`error_type`, `traceback`, `suspected_root_cause`, `recommended_action`).
- **Crash-loop** : process `degraded` après `OPS_MAX_RESTARTS` dans la fenêtre → corriger la cause racine avant `restart`.
- **Tests** : `pytest -q` (offline) pour valider la logique pure sans DB.

> Nouvelle classe de problème récurrente ⇒ l'ajouter ici + `docs/CHANGELOG_TECH.md`.
