# Performance & points de vigilance

Le système vise un fonctionnement **local stable** : pas de fuite mémoire, pas de freeze UI, latence prix « collée à Binance UI ».

## Bornes mémoire (garde-fous explicites)

### Backend
| Mécanisme | Borne | Variable |
|---|---|---|
| Files ingestor (trade/bbo) | back-pressure, drop+warning au plein | `MAX_MARKET_EVENTS` (50000) |
| Univers en RAM | top-N seulement, autres `!ticker@arr` ignorés | `BACKEND_MAX_SYMBOLS` (300) |
| Symboles Tier 3 | éviction du plus ancien non-core | `BACKEND_ACTIVE_SYMBOL_LIMIT` (20) |
| Cache klines/symbole (hub) | trim | `MAX_CANDLES_BACKEND` (1500) |
| Snapshots `/ws/live` | throttle | `BROADCAST_THROTTLE_MS` (500 ms) |
| Carnet L2 | Tier 3 uniquement | `ENABLE_DEPTH_ONLY_FOR_SELECTED` |

### Frontend
| Mécanisme | Borne | Variable |
|---|---|---|
| Lignes watchlist DOM | windowed (jamais 300) | `MAX_VISIBLE_SYMBOLS` (60) |
| Bougies/symbole | trim | `MAX_CANDLES_PER_SYMBOL` (1500) |
| Ring buffers events/logs | bornés | `MAX_EVENT_BUFFER` (200) / `MAX_LOG_BUFFER` (600) |
| Re-renders lourds | throttle | `UI_UPDATE_THROTTLE_MS` (400) |
| Updates chart | `series.update()` (jamais `setData()` au tick), chart jamais recréé | — |

### Docker
`mem_limit`/`memswap_limit` (db 1g, redis 320m), rotation des logs JSON, redis LRU 256mb. Surveiller : `docker stats`.

## Latence

- **Affichage** : hub in-process → latence réseau Binance (sous-seconde). Le chemin DB (`aggregator` ~2 s + poll) **n'est pas** la source du prix affiché.
- **Décision** : bot ~15 s ; features ~1 s ; agrégation ~2 s.
- **Métriques de latence** (Prometheus histos) : `binance_live_latency_ms`, `db_write_latency_ms`, `market_ingest_lag_ms`, `model_score_latency_ms`, `api_request_duration_ms`.

## DB / TimescaleDB
- **Hypertables** + chunks dimensionnés (1j ticks, 7j ohlcv).
- **Compression** des chunks froids (segmentby `exchange_code, symbol`) → I/O réduit.
- **Rétention** automatique (trade 90j, bbo 30j, ohlcv 365j, features 90j…).
- **Agrégats continus** (`ohlcv_1m/5m`, `market_feature_1m`) pré-calculés → requêtes de charting légères.
- **Idempotence** (`ON CONFLICT DO NOTHING`) → réécriture sûre sans doublon.
- **Set-based reads** : `/api/watchlist` et `/api/signals` utilisent `DISTINCT ON` (une requête) au lieu d'un fan-out N+1.
- **Cardinalité métriques** : `api_request_duration_ms` labellisé par **template** de route (pas l'URL brute) → cardinalité bornée même à 300 symboles.

## Pièges de performance connus
- **`aggregator` non instrumenté** : pas de métrique → surveiller via la fraîcheur `ohlcv_1s` (amélioration : instrumenter).
- **Backlog d'agrégation** : si `trade_tick` s'accumule, le scan `time_bucket` (look-back 10 s) coûte plus cher — garder l'aggregator vivant.
- **Concurrence features** : `FEATURE_MAX_CONCURRENCY` (8) dimensionné pour le **core** `ACTIVE_SYMBOLS`, pas l'univers 300 ; ne pas étendre le feature worker à l'univers sans relever cette borne et mesurer.
- **WS multi-onglets** : chaque onglet ouvre un `/ws/live` ; le throttle borne le coût, mais multiplier les onglets multiplie les snapshots.
- **`ENABLE_L2_BOOK`** : volumineux — laisser `False` sauf besoin.

## Vigilance sécurité (local-first)
- **Secrets** : uniquement via `.env` (gitignored), jamais en dur. `DATABASE_URL`/identifiants db par défaut = **dev local seulement**.
- **CORS** ouvert (`*`) et binds `127.0.0.1` : adapté au local ; **ne pas exposer** API/Ops sur un réseau public sans authentification ni durcissement CORS.
- **Ops API** : n'expose **aucun shell brut**, seulement start/stop/restart de process connus.
- **ToS exchanges** : streams **publics** Binance Spot uniquement (jamais fstream/futures par défaut) ; respecter les limites de débit REST (timeouts + fallbacks).
- **Données réelles uniquement** : invariant produit *et* sécurité (ne jamais laisser une valeur fabriquée passer pour réelle).

> Modifier une borne mémoire / un chemin chaud ⇒ mettre à jour ce fichier + `docs/CHANGELOG_TECH.md`.
