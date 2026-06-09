# Frontend — Cockpit

Cockpit temps réel servi **directement par l'API** sur `http://localhost:8000/` (mount `StaticFiles`). **Pas de process frontend séparé** — ne jamais lancer un `http.server` sur 8000 (conflit avec l'API).

- `frontend/index.html` — structure + libs CDN.
- `frontend/app.js` — toute la logique (vanilla JS, ~1260 lignes).
- `frontend/style.css` — thème terminal sombre, grille cockpit.

## Libs externes (CDN)
`lightweight-charts` 4.1.1 (bougies + volume), `chart.js` (waterfall du drill-down), `marked.js` (markdown des docs in-app), Inter (Google Fonts).

## Panneaux
- **Header** : logo + contrôles, badges de statut.
- **Barre portefeuille** : Total Value, Cash, P&L, Exposure, Positions, Drawdown, statut bot.
- **Barre macro** (`#macro-bar`) : contexte marché global **données réelles uniquement** — Total Mkt Cap, 24h Volume, Dominance BTC/ETH, var. mcap 24h (CoinGecko), DeFi TVL (DefiLlama), Fear & Greed (alternative.me) + sources live. Cellule indisponible = `n/a` (jamais fabriquée), valeur périmée = atténuée. Masquée si `global_context_enabled=false`.
- **Watchlist** : filtres `trending / volume / gainers / losers / core / favorites` + recherche (debounced).
- **Chart** : candlestick + volume, sélecteur de range 1J/7J/1M/1An, badges `source` et `chart-status`.
- **Carte stats** : prix, 24h %, volume, rank, score tendance.
- **Signals & sentiment** : cartes SOC / MKT / RSK / Σ par symbole.
- **Microstructure** : spread, depth, imbalance, trade pressure, relative volume, slippage.
- **Activity feed** : décisions horodatées (ring buffer).
- **Modales** : Logs, Drill-down (waterfall des facteurs), Timeline, **🖥 Ops / Terminals**, **🔬 Live Source Debug**, Docs.

## Endpoints consommés
REST : `/api/binance/config`, `/api/watchlist`, `/api/market/universe?limit=300`, `/api/market/global`, `/api/market/symbol/{symbol}/klines?range=…`, `/api/historical/{symbol}`, `POST /api/market/active-symbol`, `/api/portfolio`, `/api/signals`, `/api/market-features/{symbol}`, `/api/binance/debug/{symbol}`.
WebSocket : `ws://<host>/ws/live/{symbol}`.
Ops (modale) : `window.OPS_URL` (défaut `http://<host>:8050`) → `/api/ops/*`, `/ws/ops`.

## Anti-freeze chart (point critique — cause racine corrigée en PR3)

Lightweight-Charts **lève** si `series.update()` reçoit un `time` antérieur au dernier point (ex. kline 1m `:00` après un backfill OHLCV 1s `:56`). Avant, l'erreur était avalée dans un `try/catch` muet → toutes les updates suivantes gelaient.

`chartStore` + `chartApplyCandle()` garantissent :
- **jamais de temps régressif** envoyé à `update()` (append / update-last / rebase) ;
- temps forcé en **secondes** (`Math.floor(t/1000)`) ;
- une bougie live dont l'intervalle ≠ intervalle attendu est **ignorée** (protège pendant un changement de range) ;
- trim à `maxCandles` via reconstruction `setData()` au-delà du cap + marge ;
- log par bougie si `?debug=1` ou `window.CHART_DEBUG=true`.

> **Ne jamais** appeler `setData()` au tick (uniquement `update()`), et **ne jamais recréer** le chart.

## Bornes mémoire (servies par `/api/binance/config.frontend_limits`)
| Borne | Défaut | Effet |
|---|---|---|
| `MAX_CANDLES_PER_SYMBOL` | 1500 | bougies max/symbole (trim) |
| `MAX_VISIBLE_SYMBOLS` | 60 | lignes DOM watchlist (windowed, **jamais 300**) |
| `MAX_EVENT_BUFFER` | 200 | ring buffer activity/decisions |
| `MAX_LOG_BUFFER` | 600 | ring buffer logs Ops |
| `UI_UPDATE_THROTTLE_MS` | 400 | throttle des re-renders lourds |

Recherche **debounced** sur le snapshot complet, render **throttlé** (~350 ms), favoris en `localStorage`.

## Statuts honnêtes (jamais de mock affiché comme réel)
- Prix : badge `Connecting… → Waiting data → Live` ; **jamais `Live` sans bougie réelle** (`No data`/`STALE` sinon).
- Chart : 2ᵉ badge `CHART LIVE / CHART STALE Ns / NO CANDLES / CHART LIVE (derived)` gouverné par `CHART_LIVE_MAX_AGE_MS`.
- Social : `SOC n/a` si `has_sufficient_social=false`.
- Univers vide : badge header `Universe n/a` / `core only`.
- Microstructure indisponible : cellule `unavail` + **raison au survol** (jamais `n/a` muet).

## Fonctions/stores clés (`app.js`)
`chartStore`, `chartApplyCandle()`, `chartSetHistory()`, `universe` (Map + rows), `filteredUniverse()`, `renderWatchlist()` (throttlé, windowed), `favorites` (Set localStorage), `throttle()`/`debounce()`, `chartWatchdog` (recalcule live/stale 1 s), `feedWatchdog` (détecte WS silencieux > 6 s). Erreurs JS remontées à l'Ops API via `POST /api/ops/frontend-error`.

## Diagnostic rapide
| Symptôme | Regarder |
|---|---|
| Chart figé | badge `CHART STALE`/`NO CANDLES` + modale 🔬 Source (intervalle, `chart_status`, âges) |
| Prix ≠ Binance UI | 🔬 Source (`raw` vs `displayed`, `PRICE_SOURCE`), aligner `CANDLE_INTERVAL`/range |
| Univers vide | badge `Universe n/a` + `GET /api/market/universe/debug` |

> Modifier un panneau / un appel d'API ⇒ mettre à jour ce fichier + `docs/CHANGELOG_TECH.md`.
