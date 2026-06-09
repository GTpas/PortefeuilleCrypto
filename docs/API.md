# API — FastAPI (`api/main.py`, port 8000)

Sert l'API REST **+** le WebSocket live **+** héberge les hubs temps réel (Binance Spot Tier 3, Universe Tier 1) dans son `lifespan` **+** sert le cockpit statique sur `/`.

- CORS : ouvert (`*`) — usage **local**.
- Toutes les réponses d'erreur applicatives renvoient `{"error": "..."}` (le code reste 200) ; les exceptions DB sont capturées par route.
- Instrumentation : middleware `api_request_duration_ms{method,route,status}` (labellisé par **template** de route, cardinalité bornée).
- Lectures DB d'affichage **pinnées** `exchange_code = DISPLAY_EXCHANGE` (binance).

## Convention `{symbol:path}`

Les symboles canoniques contiennent un `/` (ex. `BTC/USDT`) → routes en `:path`. Appel : `/api/market-features/BTC/USDT`.

## Endpoints REST

### Référentiel & portefeuille
| Méthode | Route | Rôle | Lit |
|---|---|---|---|
| GET | `/api/symbols` | Liste `ACTIVE_SYMBOLS` | config |
| GET | `/api/portfolio` | État portefeuille (cash, valeur, positions, PnL) | `paper_portfolio`/`paper_position` (via engine) |
| GET | `/api/portfolio/history?limit=500` | Historique valeur (courbe PnL) | `portfolio_state` |
| GET | `/api/trades/recent?limit=50` | Derniers paper trades | `paper_trade` |

### Signaux & décisions
| Méthode | Route | Rôle | Lit |
|---|---|---|---|
| GET | `/api/watchlist` | Symboles core triés par `S_total` (prix = hub, fallback DB) | `decision_snapshot` + `signal_quality_audit` + `ohlcv_1s` |
| GET | `/api/signals` | Derniers scores par symbole core | `decision_snapshot` (+ audit) |
| GET | `/api/signals/{symbol:path}?limit=50` | Historique décisions d'un symbole | `decision_snapshot` |
| GET | `/api/decision/{decision_id}` | Drill-down complet (snapshot + facteurs + audit + **evidence réelle**) | `decision_snapshot`/`decision_factor`/`signal_quality_audit`/`decision_evidence_link`/`raw_content` |
| GET | `/api/factors/{decision_id}` | Facteurs d'une décision (tri par contribution) | `decision_factor` |
| GET | `/api/sources/{symbol:path}?limit=50` | Evidence sociale **réelle** d'un actif (mock filtré) | `content_entity`/`raw_content`/`tracked_*` |

> Le filtre anti-mock `COALESCE(ts.name,'') NOT ILIKE 'mock%'` s'applique à `/api/decision`, `/api/sources` → un contenu simulé n'atteint jamais l'evidence.

#### `source_evidence` (champ de `/api/decision/{decision_id}`)
En plus des champs existants (`snapshot`, `factors`, `quality_audit`, `evidence` — **inchangés, rétro-compatibles**), la réponse porte un bloc **`source_evidence`** structuré et traçable, assemblé par `api/decision_evidence.assemble_source_evidence()` (fonction **pure**, testée offline) à partir des données **déjà persistées** (aucune décision recalculée, aucune source fabriquée) :
```jsonc
"source_evidence": {
  "status": "complete|partial|missing",
  "decision_id": 483, "symbol": "ETH/USDT", "exchange_code": "binance",
  "generated_at": "…Z",
  "quality": { "quality_grade", "has_sufficient_market", "has_sufficient_social", "degradation_reasons" },
  "freshness": { "market_data_age_ms", "social_data_age_ms", "status": "available|stale|unavailable" },
  "groups": [
    { "type": "market", "label": "Market Evidence", "status": "available|stale|unavailable",
      "provider": "internal_market_features", "exchange_code": "binance",
      "source_table": "market_feature_1s", "age_ms": 340,
      "metrics": [ { "name": "spread_bps", "value": 2.308, "score_contribution": 0.9066, "explanation": "…" } ],
      "items": [] },
    { "type": "risk",   "provider": "internal_risk_engine", "source_table": "decision_factor / portfolio_state", … },
    { "type": "social", "status": "unavailable", "provider": null, "reason": "social_data_unavailable",
      "metrics": [], "items": [ /* author/source/text/relevance/published_at quand dispo */ ] }
  ],
  "warnings": ["Social evidence unavailable. …"]
}
```
Règles : facteurs groupés par `factor_category` (market/risk/social) ; `score_contribution`/`value`/`explanation` viennent de `decision_factor` (jamais réinventés ; explication manquante → phrase neutre) ; fraîcheur depuis `signal_quality_audit.{market,social}_data_age_ms` (seuils `SOURCE_EVIDENCE_AVAILABLE_MS`/`SOURCE_EVIDENCE_STALE_MS`) ; **social piloté par les vraies lignes** `decision_evidence_link` (mock filtré), jamais par le placeholder `social_unavailable` ; un groupe avec métriques persistées n'est jamais `unavailable` (au pire `stale`). Échec d'assemblage ⇒ `source_evidence: null` (le reste de la réponse reste servi).

### Marché & microstructure
| Méthode | Route | Rôle | Lit |
|---|---|---|---|
| GET | `/api/market-features/{symbol:path}` | Dernière microstructure (spread/depth/imbalance/pressure/relvol/slippage) | `market_feature_1s` (pinné binance) |
| GET | `/api/social-history/{symbol:path}?limit=100` | Historique signal social (charting) | `social_signal_1m` |
| GET | `/api/historical/{symbol:path}?limit=1800` | OHLCV 1s historique (pinné binance) | `ohlcv_1s` |

### Santé, logs, docs in-app
| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/health` | DB up/down + fraîcheur OHLCV/symbole + bloc `binance_live` (hub) + bloc `universe` + bloc `global_context` (macro) + bloc `defi_protocols` + `social_source` honnête |
| GET | `/api/system/logs?limit=100` | Logs backend (`system_log`) |
| GET | `/api/docs/signals-sentiments` | Doc markdown in-app du moteur Signals & Sentiments |
| GET | `/metrics` | Exposition Prometheus (route explicite, pas un mount) |

### Couche Binance Spot live (Tier 3, hub in-process)
| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/binance/config` | Source prix/chart, intervalle live, `chart_ranges`, `range_intervals`, `frontend_limits`, `universe_enabled`, connecté |
| GET | `/api/binance/debug/{symbol:path}` | Raw (trade/aggTrade/ticker/book/kline) **vs** prix affiché, event_time, latency, staleness |
| GET | `/api/binance/klines/{symbol:path}` | Vraies klines Binance (intervalle live du hub) |

### Univers (Tier 1, léger) & ranges
| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/market/universe?limit=300` | Top tendances (rows légers, `is_core` marqué). Vide + statut honnête si hub off. |
| GET | `/api/market/trending?limit=300` | Alias de `/api/market/universe` |
| GET | `/api/market/universe/debug` | **Pourquoi le compte ≠ 300** : tickers bruts, eligible, exclusions par raison, `final_universe_count`, latences, `last_error` |
| GET | `/api/market/source` | Quelle donnée est réelle / mock / non configurée (prix, chart, univers, social, **global**, **defi_protocols**) |
| GET | `/api/market/symbol/{symbol:path}/snapshot` | Plein détail si Tier 3, sinon row léger, sinon `unavailable` |
| GET | `/api/market/symbol/{symbol:path}/klines?range=1D` | Klines REST réelles pour le range (1D/7D/1M/1Y, alias 1J/7J/1An) |
| POST | `/api/market/active-symbol` | Body `{symbol, range}` → sélectionne le Tier 3 + range, **un seul reconnect**, renvoie klines fraîches |

### Contexte marché global (macro tier, hub in-process)
| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/market/global` | Macro **données réelles uniquement** : 3 blocs `market` (CoinGecko `/global` : total mcap, volume 24h, dominance BTC/ETH, var. mcap 24h), `defi` (DefiLlama `/v2/chains` : TVL DeFi total + top chains), `sentiment` (alternative.me `/fng/` : Fear & Greed). Chaque bloc porte `real`/`stale`/`error`/`age_ms`. Source jamais répondue ⇒ `real=false` + valeurs nulles (jamais fabriquées). `enabled:false` si `ENABLE_GLOBAL_CONTEXT=False`. |
| GET | `/api/market/defi?limit=50` | **Top protocoles DeFi par TVL** (DefiLlama `/protocols`) **données réelles uniquement** : `protocols` (rang, nom, catégorie, chaînes, `tvl_usd`, `change_1d/7d`, `mcap_usd`), `categories` (TVL par catégorie), `total_tracked_tvl_usd`. Catégories `CEX`/`Chain` **exclues** (réserves d'exchange ≠ DeFi). Porte `real`/`stale`/`error`/`age_ms` ; vide + `connected:false` si hub off/sans donnée ; échec REST transitoire ⇒ dernier bon snapshot conservé. |

## WebSocket

### `WS /ws/live/{symbol:path}`
Pousse l'état live du symbole, throttlé à `BROADCAST_THROTTLE_MS` (500 ms).
- **Chemin préféré** : hub Binance Spot. Si le symbole n'est pas suivi, il est **promu** (`set_active_symbol`) pour obtenir les streams plein détail.
- **Payload `type:"live"`** : `displayed_price`, `price_source`, `feed_status` (`live|stale|nodata`), `data_age_ms`, `latency_ms`, `event_time`, `raw`, `ticker`, `micro` (spread/depth/imbalance/slippage/pressure/relvol), `candle` + champs chart (`chart_source`, `chart_status`, `candle_age_ms`, `kline_event_count`).
- **`type:"nodata"`** : hub up mais aucun event Binance réel encore (jamais un prix fabriqué).
- **Fallback DB** : si le symbole est évincé du Tier 3, dégradation propre vers `ohlcv_1s` (pinné binance) avec `chart_status` honnête.

> Ops API/WS (`:8050`, `/api/ops/*`, `/ws/ops`) est servie par le **supervisor**, pas par cette API — voir [DEPLOYMENT.md](DEPLOYMENT.md#supervisor).

## Rapport conseil quotidien (advisory tier — `reports/`)

Rapport quotidien sur les ~300 cryptos (réel uniquement, prédictions prudentes). Détail : [daily_crypto_report.md](daily_crypto_report.md). Fichiers `reports/*.json|.md` = source de vérité ; index DB best-effort.

| Méthode / Route | Rôle |
|---|---|
| `GET /api/reports/daily/latest` | Dernier rapport généré (JSON complet) ; `{available:false}` si aucun |
| `GET /api/reports/daily/{date}` | Rapport d'une date `YYYY-MM-DD` (`{available:false}` si absent/format invalide) |
| `GET /api/reports/daily/history?limit=` | Liste des rapports (récent → ancien, lignes d'index slim) |
| `POST /api/reports/daily/generate` | Génère **maintenant** depuis les hubs in-process (test/admin) → résumé slim |
| `GET /api/reports/daily/latest/assets/{symbol}` | Analyse détaillée d'une crypto du dernier rapport |

- `GET /api/binance/config` expose `daily_report_enabled` ; `GET /api/health` ajoute un bloc `daily_report` (dernière date + statut).
- Real-data-only : horizons `1h/7j/30j` et `market_cap` renvoyés `null` (N/A) ; `signal ∈ {BUY,HOLD,SELL,AVOID}` ; `prediction.up_probability ∈ [0.15, 0.85]` (jamais une certitude).

## Ajouter un endpoint (checklist)
1. Implémenter dans `api/main.py` (gérer l'erreur, pinner `DISPLAY_EXCHANGE` si lecture DB d'affichage).
2. Ne **jamais** renvoyer de valeur fabriquée ⇒ `unavailable`/`error` explicite si la donnée réelle manque.
3. Ajouter la ligne dans ce fichier (`docs/API.md`).
4. Si le frontend le consomme, documenter dans [FRONTEND.md](FRONTEND.md).
5. Note dans `docs/CHANGELOG_TECH.md`.
