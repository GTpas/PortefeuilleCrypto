# Frontend — Cockpit

Cockpit temps réel servi **directement par l'API** sur `http://localhost:8000/` (mount `StaticFiles`). **Pas de process frontend séparé** — ne jamais lancer un `http.server` sur 8000 (conflit avec l'API).

- `frontend/index.html` — structure + libs CDN.
- `frontend/app.js` — toute la logique (vanilla JS, ~1600 lignes).
- `frontend/style.css` — **design system v3** (dark premium), grille cockpit robuste.

> **Vanilla only** : pas de React/Vite/TypeScript/build. Toute refonte reste HTML/CSS/JS pur, **sans casser les IDs/`data-*`** consommés par `app.js`, les WebSockets, ni l'anti-freeze chart.

## Libs externes (CDN)
`lightweight-charts` 4.1.1 (bougies + volume), `chart.js` (waterfall du drill-down), `marked.js` (markdown des docs in-app), Inter (Google Fonts).

## Design system (`style.css` v3)
- **Tokens couleurs** : surfaces (`--bg-app/main/panel/card/elev`), borders, texte (`--text-primary/secondary/muted`), sémantiques (`--up/--down/--warn/--blue/--info/--purple/--accent` + variantes `*-soft`). `--up/--down` **identiques** aux couleurs de série du chart (cohérence prix↔bougies).
- **Variables de layout** : `--header-h`, `--portfolio-h`, `--macro-h`, `--activity-h`, `--left-w`, `--right-w`, `--panel-gap`, `--radius-card`, `--shadow-card`.
- **Utilitaires** : `.metric-card` / `.metric-label` / `.metric-value`, `.data-chip`, `.state` (badges), `.skeleton` (shimmer de chargement), `.truncate`, `.scroll-panel`.
- **Layout robuste** : `.app-container` = grid `auto / minmax(0,1fr) / var(--activity-h)` en `100dvh`, `overflow:hidden`. Wrapper **`.top-stack`** = header + KPI portfolio + macro (jamais coupés). `.cockpit-grid` = 3 colonnes `minmax()`. Chaque panneau scrollable : parent `overflow:hidden` + `min-height:0`, scroll uniquement sur `.scroll-panel`.

## Zones (5)
1. **Top App Bar** (`.app-bar`) : logo + cluster statut (Universe / Ops / Live-Stale-Offline, **texte** pas couleur seule) + cluster tools (🔬 Source, 🏦 DeFi, 📅 Report, 📋 Logs, 🖥 Ops) + `#toggle-right` (drawer).
- **Barre portefeuille** (cartes KPI) : Total Value, Cash, P&L, Exposure, Positions, Drawdown, statut bot.
- **Barre macro** (`#macro-bar`) : contexte marché global **données réelles uniquement** — Total Mkt Cap, 24h Volume, Dominance BTC/ETH, var. mcap 24h (CoinGecko), DeFi TVL (DefiLlama), Fear & Greed (alternative.me) + sources live. Cellule indisponible = `n/a` (jamais fabriquée), valeur périmée = atténuée. Masquée si `global_context_enabled=false`.
- **Watchlist** : filtres `trending / volume / gainers / losers / core / favorites` + recherche (debounced).
- **Chart** : candlestick + volume, sélecteur de range 1J/7J/1M/1An, badges `source` et `chart-status`.
- **Carte stats** : prix, 24h %, volume, rank, score tendance.
- **Signals & sentiment** : cartes SOC / MKT / RSK / Σ par symbole.
- **Microstructure** : spread, depth, imbalance, trade pressure, relative volume, slippage.
- **Activity feed** : décisions horodatées (ring buffer).
- **Modales** : Logs, Drill-down (waterfall des facteurs + **Source Evidence** structurée), Timeline, **🖥 Ops / Terminals**, **🏦 DeFi** (top protocoles par TVL — DefiLlama, table rang/nom/catégorie/chaînes/TVL/24h/7j + breakdown par catégorie, **données réelles uniquement** : `n/a`/vide honnête si hub off), **📅 Report** (rapport conseil quotidien — résumé + KPIs, distribution des ratings, top BUY/SELL/à-surveiller, table **filtrable signal+rating / triable / recherchable** des 300, détail au clic avec prédiction & ratios ; disclaimer « pas un conseil financier » visible ; `setupDailyReport()`/`fetchDailyReport()`/`renderReport()`, données réelles uniquement, `N/A` honnête), **🔬 Live Source Debug**, Docs.
- **Source Evidence** (Drill-down) : `renderDecisionSourceEvidence()` rend le bloc `source_evidence` de `/api/decision/{id}` — badge global `complete/partial/missing`, warnings, et une carte par groupe **Market / Risk / Social** (statut `available/stale/unavailable`, provider, exchange, table source, âge, métriques `name/value/contribution/explanation` reliées aux facteurs persistés ; social : auteur/source/texte/relevance/horodatage des vraies lignes, sinon `Social evidence unavailable` + raison). **Jamais de mock comme réel** ; fallback rétro-compatible sur l'ancien champ `evidence` si `source_evidence` absent ; aucun crash sur `null`/groupes/métriques vides.

- **Signal « why »** : chaque carte signal porte une ligne d'explication dérivée de `explainReason(reason_code, s_total)` (le `reason_code` **persisté** est servi par `/api/signals`) — BUY/HOLD/REDUCE/EXIT et le risk gate forçant éventuel. Jamais fabriqué : absente si `reason_code` est nul.

## Panneau Decision Intelligence (droite) — V2
- **Selected Market** + **Signals & Sentiment** (cartes SOC/MKT/RSK/Σ + ligne « why »).
- **Risk Gates** (`updateRiskGates`/`renderRiskGates`) : pour le symbole sélectionné, lit la **dernière décision** (`/api/signals/{sym}?limit=1` → `reason_code`/`s_risk`) et le décompte risque **persisté** (`/api/decision/{id}`, factors `category='risk'`). Affiche `tradeable`/`forced HOLD` + le gate forçant (`risk_gate:*`, libellé via `RISK_GATE_LABELS`) + facteurs réels. Symbole non-core ⇒ « no bot decision » honnête (jamais de matrice fabriquée).
- **Microstructure** (spread/depth/imbalance/trade pressure/rel. vol/slippage).
- **Source Quality** (`updateSourceQuality`) : `/api/market/source` → une puce par flux (Price/Chart/Universe/Social/Macro/DeFi) = `live`/`mock`/`stale`/`unavailable`/`disabled` + source + âge connu. **Social** honnête (`mock` vs `not configured`).

## Activity feed à onglets (bas) — V2
`setupFeedTabs`/`refreshActivity` ⇒ **Trades** (`/api/trades/recent`) · **Decisions** (`/api/signals` + « why ») · **Errors** (`/api/system/logs` filtré WARN/ERROR/CRITICAL) · **Incidents** (`window.OPS_URL`+`/api/ops/incidents`). États vides/erreur **honnêtes** : un non-array de l'Ops API ⇒ « Incidents unavailable » (jamais un faux « ✓ »). Lignes cliquables = `role=button`/`tabindex`/clavier ; ouvrent le drill-down.

## Thème clair/sombre — V2
Opt-in `html[data-theme="light"]` (persisté `localStorage:ag_theme`, bouton header `🌙/☀️`, `aria-pressed`). `applyChartTheme()` synchronise texte/grille/bordures Lightweight-Charts (les séries up/down restent les couleurs canoniques). Sémantiques clair **vérifiées WCAG AA** (≥4.5:1 sur la surface la plus faible `#E8ECF2` : up `#0A7048`, down `#CC1F38`, warn `#9A5B00`). Les boîtes de logs Ops (fond `#070A0F` permanent) gardent un texte clair stable même en thème clair.

## Responsive & drawers
- **≥1100px** : 3 colonnes (watchlist · chart · Decision Intelligence). `--left-w`/`--right-w` se compactent à 1440/1280px ; labels des tool-buttons en icône seule ≤1280px.
- **≤1100px** : panneau **droit** = drawer off-canvas (`#toggle-right`/`#close-right`/`#drawer-backdrop`, `Esc`/backdrop). Grille 2 colonnes.
- **≤880px** : chart pleine largeur ; panneau **gauche** (Market Explorer) = drawer off-canvas (`#toggle-left`, `☰`). **≤560px** : header/typo compactés.
- Drawers pilotés par `setupDrawers()` (`body.left-open`/`right-open`), CSS possède les media queries ; backdrop partagé, **focus déplacé dans le panneau ouvert puis restitué au déclencheur**, `aria-expanded` sur les toggles.
- Le chart **ne reçoit jamais** `height/width=0` (garde `ResizeObserver`) → pas de collapse pendant une transition de layout/drawer.

## Accessibilité
- `:focus-visible` partout (boutons, inputs, selects, lignes watchlist & feed).
- Boutons-icônes : `aria-label` ; lignes watchlist **et** feed `role="button"` + `tabindex=0` + clavier `Enter`/`Espace`.
- Onglets (watchlist & feed) : `aria-pressed` (état sélectionné non couleur-seule). Toggles drawer : `aria-expanded`. Thème : `aria-pressed`.
- États **live/stale/offline/unavailable** distingués par **texte** (badge), pas par la couleur seule.
- `prefers-reduced-motion` : coupe pulse header, shimmer skeleton et animations de modales.
- Contraste relevé (`--text-secondary`/`--text-muted`), prix coloré sans clignotement agressif.

## Endpoints consommés
REST : `/api/binance/config`, `/api/watchlist`, `/api/market/universe?limit=300`, `/api/market/global`, `/api/market/defi?limit=50`, `/api/market/source`, `/api/market/symbol/{symbol}/klines?range=…`, `/api/historical/{symbol}`, `POST /api/market/active-symbol`, `/api/portfolio`, `/api/signals`, `/api/signals/{symbol}`, `/api/decision/{id}`, `/api/market-features/{symbol}`, `/api/trades/recent`, `/api/system/logs`, `/api/binance/debug/{symbol}`, `/api/reports/daily/latest`, `POST /api/reports/daily/generate`.
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
