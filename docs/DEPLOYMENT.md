# Déploiement & configuration (local)

Le projet est conçu pour tourner **localement** : Docker fournit **uniquement l'infra** (PostgreSQL/TimescaleDB + Redis) ; l'applicatif (workers + API + cockpit) tourne sous le **supervisor** Python.

## Prérequis
- Docker + Docker Compose
- Python 3.10+
- ≥ 4 Go RAM, SSD recommandé

## Docker (infra seulement)
`docker-compose.yml` ne contient **que** `db` et `redis`.

```bash
docker compose up -d        # démarre db + redis
docker compose ps           # statut + healthchecks
docker compose logs -f db   # logs DB
docker compose down         # arrêt (volume conservé)
docker compose down -v      # arrêt + SUPPRESSION des données (destructif)
```

Durcissement (pass mémoire/logs) :
- `db` : `mem_limit/memswap_limit 1g`, `shm_size 256m`, healthcheck `pg_isready`, logs `json-file` 10m×3.
- `redis` : `--save "" --appendonly no --maxmemory 256mb --maxmemory-policy allkeys-lru`, `mem_limit 320m`, logs 5m×2.
- Migrations `db/migrations` montées dans `/docker-entrypoint-initdb.d` → exécutées au **premier** boot.
- `.dockerignore` exclut `venv`, `__pycache__`, `.git`, `.pytest_cache`, `node_modules`, `.env`, `logs/`, rapports.

> Surveiller la RAM : `docker stats`. Limites = garde-fous, pas une cible.

## Lancement applicatif

Voir [RUNBOOK.md](RUNBOOK.md). En bref :
```powershell
# tout le stack (docker + bootstrap + workers + API) sous supervision
$env:PYTHONPATH="."; python .\scripts\dev_supervisor.py
```

## Variables d'environnement

Définies dans `.env` (à la racine) ou l'environnement. Source de vérité : `config.py` (`pydantic-settings`). **Jamais de secret en dur.**

### Base / ingestion
| Variable | Défaut | Rôle |
|---|---|---|
| `DATABASE_URL` | `postgresql://crypto_user:crypto_password@localhost:5432/crypto_market_data` | Connexion PostgreSQL |
| `REDIS_URL` | `redis://localhost:6379/0` | Cache/queue |
| `BATCH_MAX_ROWS` | 1000 | Lignes max/batch avant flush |
| `FLUSH_EVERY_SECONDS` | 0.25 | Délai max avant flush |
| `EXCHANGES` | binance,kraken,coinbase | Exchanges collectés |
| `ACTIVE_SYMBOLS` | BTC/USDT,ETH/USDT,SOL/USDT | Core persisté/tradé/features |
| `DISPLAY_EXCHANGE` | binance | **Exchange pinné** des lectures DB d'affichage |
| `FEATURE_MAX_CONCURRENCY` | 8 | Concurrence `compute_features` |
| `MAX_DATA_AGE_S` | 30 | Âge max d'un quote avant gate `data_stale` |

### Binance Spot live (Tier 3)
| Variable | Défaut | Rôle |
|---|---|---|
| `ENABLE_BINANCE_SPOT` | True | Active le hub temps réel |
| `PRICE_SOURCE` | trade | Prix affiché : `trade\|aggTrade\|ticker_last\|book_mid\|kline_close` |
| `CANDLE_SOURCE` | binance_kline | `binance_kline\|derived_trades` |
| `CANDLE_INTERVAL` | 1m | `1s\|1m\|5m\|15m\|1h\|4h\|1d` |
| `BINANCE_WS_BASE` | `wss://stream.binance.com:9443` | WS Spot (jamais fstream/futures) |
| `BINANCE_REST_BASE` | `https://api.binance.com` | REST Spot |
| `BINANCE_REST_TIMEOUT` | 6.0 | Timeout REST (s) |
| `BINANCE_REST_FALLBACKS` | api1,api2.binance.com | Bases REST de secours |
| `BINANCE_REST_MAX_SYNC_RETRIES` | 3 | Retries resync carnet |
| `BINANCE_DEPTH_LIMIT` | 100 | Profondeur snapshot REST |
| `BINANCE_LIVE_MAX_AGE_MS` | 3000 | Fenêtre LIVE prix |
| `CHART_LIVE_MAX_AGE_MS` | 6000 | Fenêtre CHART LIVE (kline) |

### Univers (Tier 1) & ranges
| Variable | Défaut | Rôle |
|---|---|---|
| `ENABLE_MARKET_UNIVERSE` | True | Active le hub univers léger |
| `UNIVERSE_LIMIT` | 300 | Cap dur du top-N |
| `QUOTE_ASSET` | USDT | Quote des paires de l'univers |
| `MIN_QUOTE_VOLUME` | 500000 | **Plancher liquidité** (5M→500K = fix « UNIVERSE 66 ») |
| `EXCLUDE_STABLES` | True | Exclut stablecoins/fiat |
| `EXCLUDE_LEVERAGE` | True | Exclut tokens à levier (UP/DOWN/BULL/BEAR/3L/3S) |
| `TRENDING_REFRESH_SECONDS` | 60 | Recalcul du classement |
| `UNIVERSE_STALE_MS` | 15000 | Seuil staleness d'une row |
| `CHART_RANGE_DEFAULT` | 1D | Range au chargement |
| `CHART_INTERVAL_1D/7D/1M/1Y` | 1m/15m/1h/1d | Mapping range→intervalle |

### Contexte marché global (macro tier — gratuit, sans clé)
| Variable | Défaut | Rôle |
|---|---|---|
| `ENABLE_GLOBAL_CONTEXT` | True | Active le hub macro in-process (display-only) |
| `ENABLE_COINGECKO` | True | Sous-source : CoinGecko `/global` (total mcap, volume 24h, dominance BTC/ETH) |
| `ENABLE_DEFILLAMA` | True | Sous-source : DefiLlama `/v2/chains` (TVL DeFi total) |
| `ENABLE_FEAR_GREED` | True | Sous-source : alternative.me Fear & Greed |
| `GLOBAL_CONTEXT_REFRESH_SECONDS` | 60 | Cadence de re-poll des sources macro |
| `GLOBAL_CONTEXT_HTTP_TIMEOUT` | 10.0 | Timeout HTTP par appel |
| `GLOBAL_CONTEXT_STALE_MS` | 300000 | Seuil staleness d'une valeur macro |
| `COINGECKO_API_BASE` / `COINGECKO_API_KEY` | api.coingecko.com/api/v3 / *(vide)* | Base REST + clé Demo optionnelle (`x-cg-demo-api-key`) |
| `DEFILLAMA_API_BASE` | api.llama.fi | Base REST DefiLlama (gratuit, sans clé) |
| `FEAR_GREED_API_BASE` | api.alternative.me | Base REST Fear & Greed (gratuit, sans clé) |

### Bornes mémoire (back & front)
| Variable | Défaut | Rôle |
|---|---|---|
| `BACKEND_MAX_SYMBOLS` | 300 | Symboles retenus en RAM (univers) |
| `BACKEND_ACTIVE_SYMBOL_LIMIT` | 20 | Symboles Tier 3 simultanés |
| `MAX_CANDLES_BACKEND` | 1500 | Klines/symbole dans le hub |
| `MAX_MARKET_EVENTS` | 50000 | Files trade/bbo de l'ingestor (back-pressure) |
| `BROADCAST_THROTTLE_MS` | 500 | Intervalle min des snapshots `/ws/live` |
| `SNAPSHOT_INTERVAL_SECONDS` | 3.0 | Intervalle snapshots univers |
| `ENABLE_DEPTH_ONLY_FOR_SELECTED` | True | Carnet L2 réservé au Tier 3 |
| `MAX_CANDLES_PER_SYMBOL` | 1500 | Front : bougies/symbole |
| `MAX_VISIBLE_SYMBOLS` | 60 | Front : lignes DOM watchlist |
| `MAX_EVENT_BUFFER` | 200 | Front : ring buffer events |
| `MAX_LOG_BUFFER` | 600 | Front : ring buffer logs |
| `UI_UPDATE_THROTTLE_MS` | 400 | Front : throttle re-render |

### Feature flags / social / ops / métriques / logs
| Variable | Défaut | Rôle |
|---|---|---|
| `ENABLE_L2_BOOK` | False | Ingestion carnet L2 complet (volumineux) |
| `ENABLE_COINGECKO` | True | Sous-source macro CoinGecko (voir « Contexte marché global ») |
| `ENABLE_DEX` | False | Ingestion DEX (Uniswap) |
| `ENABLE_MOCK_SOCIAL` | **False** | Collecteur social **simulé** (DEV ONLY — jamais présenté comme réel) |
| `ENABLE_RSS_SOCIAL` | False | Collecteur **RSS news réel** (flux publics, ToS-safe) — vraie source |
| `RSS_FEEDS` | CoinDesk/Cointelegraph/Decrypt/TheBlock | Liste de flux RSS/Atom (JSON) |
| `RSS_POLL_SECONDS` / `RSS_HTTP_TIMEOUT` | 120 / 10 | Intervalle mini de fetch (politesse) / timeout HTTP par flux |
| `ENABLE_OUTCOME_EVAL` | True | Worker d'évaluation ex-post (`outcome_eval` + crédibilité acteurs) |
| `OUTCOME_EVAL_INTERVAL_S` | 60 | Cadence de scan des décisions matures |
| `OUTCOME_HORIZONS` | `["1h","4h","24h"]` | Horizons d'évaluation (subset `15m\|1h\|4h\|24h\|3d`) |
| `OUTCOME_HOLD_BAND_PCT` | 0.5 | Bande (%) sous laquelle un HOLD est jugé correct |
| `OUTCOME_PRICE_TOLERANCE_S` | 300 | Écart max (s) entre l'horizon visé et le close OHLCV utilisé |
| `OPS_HOST` / `OPS_PORT` | 127.0.0.1 / 8050 | Bind Ops supervisor |
| `OPS_MAX_RESTARTS` / `OPS_RESTART_WINDOW_S` | 5 / 120 | Budget glissant de restart |
| `METRICS_ENABLED` | True | Exposition Prometheus |
| `METRICS_PORT_INGESTOR/FEATURE/SOCIAL/BOT` | 9101/9102/9103/9104 | Ports métriques workers |
| `METRICS_PORT_AGGREGATOR/OUTCOME` | 9105/9106 | Ports métriques aggregator / outcome_evaluator |
| `LOG_LEVEL` | INFO | Niveau de log |

## <a name="supervisor"></a>Supervisor & Ops API (port 8050)

`scripts/dev_supervisor.py` construit la liste des process **à partir des fichiers réellement présents** (`build_specs()`), les lance dans l'ordre (oneshots attendus), et sert l'Ops API.

Endpoints Ops (`config.OPS_HOST/OPS_PORT`) :
- `GET /api/ops/status` · `GET /api/ops/health` · `GET /api/ops/processes`
- `GET /api/ops/events?limit&level&process` · `GET /api/ops/incidents?limit`
- `POST /api/ops/process/{start|stop|restart}` — body `{"name": "<process>"}`
- `POST /api/ops/frontend-error` — funnel des erreurs JS du cockpit
- `WS /ws/ops` — flux temps réel (`snapshot`/`log`/`status`/`incident`)

Aucun shell brut n'est exposé — uniquement ces actions contrôlées. Incidents persistés dans `logs/ops_incidents.jsonl`.

## Ports
| Service | Port |
|---|---|
| API / cockpit | 8000 |
| Ops supervisor | 8050 |
| Prometheus workers | 9101–9106 |
| PostgreSQL/TimescaleDB | 5432 |
| Redis | 6379 |

> Modifier `docker-compose.yml` / `config.py` / `.env` ⇒ mettre à jour ce fichier + `docs/CHANGELOG_TECH.md`.
