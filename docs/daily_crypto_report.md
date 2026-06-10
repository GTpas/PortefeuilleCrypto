# Daily Crypto Report — aide à la décision financière

> **Cadrage.** *Rapport d'aide à la décision financière — recommandations générées par
> modèle quantitatif*, à partir de données de marché **réelles** (Binance Spot 24h,
> CoinGecko, DefiLlama, alternative.me). Les crypto-actifs sont **volatils** : les
> recommandations dépendent de la **qualité et de la disponibilité des données** au moment
> de la génération, et les scénarios sont exprimés en **probabilités**, jamais en
> certitudes. Le rapport est **actionnable et assumé** (BUY/HOLD/SELL/AVOID, allocations,
> niveaux), sans prétendre au conseil personnalisé réglementé.

## 1. Objectif

Produire, **chaque jour automatiquement** (par défaut à minuit), un rapport
d'investissement professionnel sur les **~300 cryptomonnaies** suivies par le cockpit
(univers Binance Spot), enrichi du **top 1000 CoinGecko** (watchlist externe). Pour chaque
crypto : classement global, signal **BUY / HOLD / SELL / AVOID** + **action portefeuille**
(acheter / renforcer / conserver / surveiller / alléger / vendre / éviter), **conviction**
(forte/moyenne/faible), rating **A+→E**, **rationale décisionnelle** (métriques
déclencheuses, signaux contradictoires, risque principal), scénarios haussier/baissier,
**niveaux d'invalidation / TP / SL indicatifs** (dérivés des niveaux 24h réels), poids
recommandés par profil, et **source evidence** horodatée. Au niveau portefeuille : un
**positionnement recommandé** (offensif / équilibré / défensif / cash majoritaire) et des
**allocations modèles** pour 3 profils (prudent / équilibré / agressif).

## 2. Périmètre & limites (réel uniquement)

Le rapport applique la **règle absolue anti-mock** du projet : aucune donnée n'est
fabriquée. Quand une donnée n'existe pas, elle est affichée **`N/A`** et **réduit le score
de confiance** — jamais une valeur inventée.

| Donnée | Disponible ? | Source |
|---|---|---|
| Prix, variation **24h**, volume 24h, nb trades 24h | ✅ réel | Binance `!ticker@arr` (universe) |
| High/Low/Open 24h, **VWAP** 24h, spread (bid/ask) | ✅ réel | Binance ticker 24h |
| Variation **1h / 7j / 30j** (univers) | ❌ indisponible | non fournie par le ticker 24h |
| **Market cap** par actif de l'univers | ❌ indisponible | réservée à la watchlist externe |
| Market cap, rang, prix, 24h, volume **top 1000** | ✅ réel (best-effort) | CoinGecko `/coins/markets` (4×250, sans clé) |
| Profondeur L2 (depth) par actif | ❌ indisponible | réservée au symbole sélectionné (Tier 3) |
| Régime de marché, Fear&Greed, mcap 24h | ✅ réel | tier macro `global_context` |

> L'UI n'affiche jamais un `N/A` brut : chaque absence est rendue **« Donnée
> indisponible »** avec la **raison au survol**, et **dégrade le score de confiance**
> de l'actif. Trop de données manquantes ⇒ signal **AVOID**.

> Ces absences sont **assumées** : le rapport reste honnête et la confiance est abaissée en
> conséquence. Une montée en richesse (1h/7j/30j, market cap) via CoinGecko markets est une
> amélioration future (cf. §9).

## 3. Architecture ajoutée

Module isolé `reports/` (logique pure vs I/O, comme le reste du repo) :

- **`reports/scoring.py`** — formules **pures** (ratios, sous-scores 0–100, scores finaux,
  rating, signal, **conviction**, **signaux contradictoires**, prédiction, régime). Zéro
  I/O. **Source unique de vérité** des chiffres → le worker et l'API ne peuvent pas diverger.
- **`reports/portfolio_advisor.py`** — couche portefeuille **pure** : posture marché
  (offensif/équilibré/défensif/cash majoritaire), **allocations modèles** par profil
  (prudent/équilibré/agressif, somme = 100 %), tiers de taille (proxy percentile de volume
  24h réel — la market cap exacte n'est jamais fabriquée), **actions par actif** et **poids
  recommandés** plafonnés par des règles de sécurité codées : small cap illiquide jamais
  surpondérée (exclue pour le profil prudent), token très volatil plafonné à moitié,
  mid/small caps repassées en cash si la largeur de marché est négative.
- **`reports/top1000.py`** — watchlist externe **CoinGecko top 1000** : parsing +
  classification **purs** (suivies / nouvelles opportunités / exclues avec raison), fetch
  **best-effort** (lecture partielle conservée, statut `ok/partial/unavailable/disabled`,
  jamais une liste fabriquée).
- **`reports/generator.py`** — `build_daily_report(rows, global_context, …,
  previous_report, external_watchlist)` → dict JSON structuré (résumé exécutif,
  portefeuille modèle, recommandations par actif avec niveaux et evidence, `data_quality`,
  `changes_vs_previous`) + `render_markdown(report)` → Markdown français type rapport de
  gestion. Pur (données en entrée → sortie).
- **`reports/store.py`** — persistance : écrit le **JSON + Markdown sur disque** (source de
  vérité, conforme à la politique « pas de gros blobs en PG ») + miroir **best-effort** d'un
  index (et des scores par actif) dans Postgres.

Données : le **worker** lit les tiers live via l'**API locale** (mêmes endpoints réels que
le cockpit) ; l'**endpoint `POST /generate`** construit directement depuis les hubs
in-process. Les deux appellent le **même** `build_daily_report`. C'est **display/report-only** :
ça ne nourrit jamais le bot ni le chemin de persistance marché.

## 4. Formules de scoring (centralisées, auditables)

Tous les sous-ratios sont bornés dans `[0,1]` (ou un ratio clair pour la force relative) ;
les scores finaux dans `[0,100]`. Constantes nommées dans `reports/scoring.py`.

### Ratios
- **Momentum** `[0,1]` — `0.65·tanh(chg24h/8) + 0.35·position_dans_le_range_24h`. (24h
  uniquement ; 1h/7j/30j = N/A.)
- **Volume Confirmation** `[0,1]` — `0.6·percentile_volume_univers + 0.4·alignement_VWAP`
  (prix au-dessus de son VWAP = acheteurs aux commandes). *Limite assumée* : faute
  d'historique de volume par symbole, la conviction est approchée par le **percentile
  transversal** de volume.
- **Liquidity** `[0,1]` — `0.55·log(quote_vol) + 0.20·log(trades) + 0.25·spread_serré`.
- **Relative Strength vs BTC** — `(1+chg24h)/(1+chgBTC24h)`, centré sur 1.0 (N/A si BTC absent).
- **Trend Quality** `[0,1]` — pénalise un gros mouvement sans volume (pump à faible liquidité).
- **Volatility** `[0,1]` — `0.6·range_intraday + 0.4·|chg24h|`.
- **Drawdown** `[0,1]` — distance du prix sous le plus haut 24h.
- **Market Context** `[0,1]` — `0.4·Fear&Greed + 0.3·mcap24h + 0.3·largeur_marché` (commun).

### Scores finaux
**Final Opportunity Score** `0–100` (le Risk Score est calculé **d'abord** car il entre en
pénalité) :

| Composante | Poids |
|---|---|
| momentum | 25 % |
| trend quality | 20 % |
| volume confirmation | 15 % |
| relative strength vs BTC | 15 % |
| liquidity | 10 % |
| market regime | 10 % |
| **risk penalty** (− risk/100) | **− 5 %** |

**Risk Score** `0–100` : volatilité 30 %, drawdown 25 %, illiquidité 20 %, qualité de
données 15 % (champs manquants + staleness), microstructure/spread 10 %. *(Le
« concentration risk » du spec nécessite des données de détention on-chain qu'on n'a pas —
règle real-data-only — son slot est tenu par le risque de microstructure, proxy réel le
plus proche de la manipulabilité.)*

**Confidence Score** `0–100` : complétude des données 35 %, liquidité 30 %, fraîcheur 20 %,
couverture d'horizon 15 % (**plafonnée** car seul l'horizon 24h est réel → on ne prétend
jamais à une confiance maximale).

**Sous-scores exposés** (`scores` par actif, 0–100, `None` honnête si l'entrée manque) :
`momentum_score`, `trend_score`, `volume_score`, `liquidity_score`, `volatility_score`,
`drawdown_score`, `relative_strength_score`, `market_regime_score`, `risk_score`,
`opportunity_score`, `confidence_score` — consommés tels quels par l'UI (radar, tableaux),
qui ne recalcule jamais un chiffre.

## 5. Échelle de rating

Composite = `opportunity − 0.4·risk`, **plafonné par la confiance** (une confiance < 35 ne
peut pas obtenir un bon rating).

| Rating | Définition | Règle (composite / garde-fous) |
|---|---|---|
| **A+** | Opportunité très forte, risque contrôlé, données solides | composite ≥ 70, risk ≤ 40, conf ≥ 70 |
| **A**  | Opportunité forte, bon momentum, liquidité correcte | composite ≥ 58, risk ≤ 50, conf ≥ 60 |
| **B**  | Intéressante mais prudence nécessaire | composite ≥ 45 |
| **C**  | Signal neutre ou incertain | composite ≥ 32 |
| **D**  | Risque élevé ou tendance dégradée | composite ≥ 18 (ou confiance faible) |
| **E**  | À éviter : données faibles ou risque extrême | sinon |

## 6. Signal BUY / HOLD / SELL / AVOID

Ordre d'évaluation (le premier qui s'applique gagne) :

1. **AVOID** — `stale`, **ou** liquidity < 0.25, **ou** spread > 60 bps, **ou** confiance < 35,
   **ou** risk ≥ 80, **ou** **pump suspect** (|variation 24h| ≥ 18 % sans support
   volume/VWAP < 0.35). (Mouvement fragile/manipulable ou données insuffisantes.)
2. **BUY** — opportunity ≥ 75 **et** risk ≤ 60 **et** confiance ≥ 65 **et** momentum > 0.5
   **et** liquidity ≥ 0.35 (momentum positif + liquidité suffisante exigés).
3. **SELL** — variation 24h ≤ −3 % **et** momentum < 0.40 **et** (risk ≥ 55 **ou** rupture
   de tendance court terme : prix sous son VWAP 24h avec drawdown ≥ 0.5).
4. **HOLD** — sinon (opportunité 50–75, signaux contradictoires, ou tendance positive mais
   risque élevé → pas de confirmation suffisante).

Chaque signal porte : une **conviction** (forte/moyenne/faible — confiance + marge vs les
seuils), une **action portefeuille** (BUY forte → *renforcer*, BUY → *acheter*, SELL forte
→ *vendre*, SELL → *alléger*, AVOID → *éviter*, HOLD opportun → *surveiller*, sinon
*conserver*), une **rationale décisionnelle** (métriques déclencheuses chiffrées, risque
principal, signaux contradictoires), une **justification** courte et une **explication
simple** pour débutant.

### Niveaux indicatifs (réels uniquement)
Les niveaux d'**invalidation / take-profit / stop-loss** sont dérivés **exclusivement des
niveaux 24h réellement observés** (plus bas / plus haut / VWAP) — jamais de cible
fabriquée. Thèse haussière : invalidation = cassure du plus bas 24h, TP = zone du plus haut
24h. Thèse SELL : inversée (reprise du plus haut 24h = invalidation). Niveau manquant ⇒
« Donnée indisponible » + raison.

## 7. Prédiction (transparente, prudente)

`up_probability` = mélange **linéaire et explicite** de drivers directionnels (momentum,
force relative, confirmation volume, macro) moins une pénalité de risque, **bornée à
`[0.15, 0.85]`** → **jamais 0 % ni 100 %**. `down_probability = 1 − up`. Le rapport fournit
un **scénario central / haussier / baissier** et une **condition d'invalidation** (ex.
cassure du plus bas 24h), plus un **niveau de confiance** (élevé/modéré/faible). Horizons :
**24h réel** ; **7j / 30j = N/A** (raison affichée).

## 8. Génération à minuit, endpoints, formats

- **Worker** `workers/report_worker.py` (supervisé) : calcule le prochain minuit dans
  `DAILY_REPORT_TIMEZONE`, dort jusque-là (par tranches, robuste au drift d'horloge),
  génère, persiste, recommence. Relance manuelle : `python -m workers.report_worker --once`.
- **Endpoints API** (port 8000) :
  - `GET /api/reports/daily/latest` — dernier rapport (JSON complet) ou état honnête si aucun.
  - `GET /api/reports/daily/{date}` — rapport d'une date (`YYYY-MM-DD`).
  - `GET /api/reports/daily/history` — liste des rapports (récent → ancien).
  - `POST /api/reports/daily/generate` — génère **maintenant** (bouton « Générer »), depuis les hubs.
  - `GET /api/reports/daily/latest/assets/{symbol}` — analyse détaillée d'une crypto.
  - `GET /api/reports/daily/{date}/crypto/{symbol}` — idem pour une date (`latest` accepté).
  - `GET /api/reports/opportunities/top1000` — watchlist externe du dernier rapport.
  - `GET /api/reports/portfolio/model` — bloc portefeuille modèle du dernier rapport.
- **Diff quotidien** : le générateur reçoit le **rapport précédent** (date strictement
  antérieure) et produit `changes_vs_previous` : passages de signal (HOLD→BUY, BUY→SELL…),
  baisses de confiance ≥ 15 pts, entrées/sorties de l'univers.
- **Formats** : **JSON** structuré (`reports/daily_crypto_report_YYYY-MM-DD.json`, pour
  l'API/front) + **Markdown** lisible (`reports/daily_crypto_report_YYYY-MM-DD.md`). Un PDF
  est une amélioration future (non prioritaire).
- **Frontend** : bouton header **📅 Report** → modale à **7 onglets** : **Synthèse**
  (positionnement recommandé, conviction, régime, KPIs, distributions signaux/ratings en
  barres, top actions du jour, mini-diff), **Portefeuille** (3 cartes profil : barre
  d'allocation empilée, drawdown estimé, horizon, positions BUY suggérées),
  **Opportunités** (cartes BUY avec rationale + TP/SL/invalidation + watchlist top 1000),
  **Risques** (SELL/AVOID + carte momentum × risque), **Classement** (table filtrable /
  triable / recherchable avec action + conviction, détail au clic : **radar SVG** des
  sous-scores + evidence horodatée), **Sources** (qualité des données, statut par source,
  lacunes connues), **Historique** (diff complet + liste des rapports + frise du régime).

## 9. Stockage

Les **fichiers sont la source de vérité** (`DAILY_REPORT_DIR`, défaut `reports/`,
gitignored). Un **index DB best-effort** (migration `008_daily_report_schema.sql`, aussi
créé à l'exécution via `reports.store.ensure_schema`) rend le rapport **interrogeable** :

- `daily_crypto_report` — 1 ligne/jour (régime, compteurs de signaux, chemins, statut).
- `daily_crypto_asset_score` — scores/prédiction par actif (prépare le **backtest
  prédiction-vs-réalisé** J+1/J+7). Best-effort : si la DB est absente, le rapport
  fonctionne quand même (lecture depuis les fichiers).

## 10. Observabilité

Métriques `/metrics` (worker sur le port **9107**) : `daily_report_runs_total{trigger}`,
`daily_report_errors_total`, `daily_report_build_latency_ms`, `daily_report_assets`,
`daily_report_last_success_ts`, `daily_report_signal_counts{signal}`. `GET /api/health`
expose un bloc `daily_report` (dernière date + statut). `GET /api/binance/config` expose
`daily_report_enabled`.

## 11. Comment relancer / lire le rapport

```powershell
# 1) le stack tourne (cockpit + universe live)
$env:PYTHONPATH="."; python .\scripts\dev_supervisor.py
# 2) générer un rapport immédiatement (test) — soit via le worker :
$env:PYTHONPATH="."; python -m workers.report_worker --once
#    soit via l'API :
#    POST http://127.0.0.1:8000/api/reports/daily/generate
# 3) lire : http://localhost:8000/  → bouton 📅 Report
#    ou GET http://127.0.0.1:8000/api/reports/daily/latest
#    ou le fichier reports/daily_crypto_report_<date>.md
```

> Au tout premier lancement, l'univers met quelques secondes à se remplir : générer le
> rapport **après** que le badge `Universe 300` est vert donne un univers complet.

## 12. Configuration (`.env`)

`ENABLE_DAILY_REPORT`, `DAILY_REPORT_HOUR`, `DAILY_REPORT_MINUTE`, `DAILY_REPORT_TIMEZONE`
(IANA, fallback UTC si `tzdata` absent), `DAILY_REPORT_DIR`, `DAILY_REPORT_UNIVERSE_LIMIT`,
`DAILY_REPORT_TOP_N`, `DAILY_REPORT_HISTORY_LIMIT`, `DAILY_REPORT_API_BASE`,
`DAILY_REPORT_HTTP_TIMEOUT`, `DAILY_REPORT_PERSIST_DB`, `ENABLE_TOP1000_WATCHLIST`,
`TOP1000_PAGES`, `TOP1000_MIN_VOLUME_USD` (réutilise `COINGECKO_API_BASE` /
`COINGECKO_API_KEY` / `GLOBAL_CONTEXT_HTTP_TIMEOUT`).

## 13. Tests

Offline, sans réseau ni DB :
- `tests/test_daily_report.py` — bornes/direction des ratios, bandes de rating, signaux
  BUY/HOLD/SELL/AVOID aux seuils, conviction/action/rationale présents, **niveaux TP/SL/
  invalidation = niveaux 24h réels** (et jamais fabriqués si absents), bloc `data_quality`
  honnête, diff `changes_vs_previous` (transitions de signal, baisses de confiance),
  evidence horodatée avec raisons FR, robustesse données manquantes, prudence des
  prédictions (`[0.15,0.85]`), assemblage JSON + Markdown, univers simulé 300 + perf,
  round-trip store, scheduler.
- `tests/test_portfolio_advisor.py` — mapping posture, allocations = 100 % pour toutes
  postures×profils, règle largeur de marché négative, caps de sécurité (small cap
  illiquide, volatilité), verbes d'action, poids ≤ budget et ≤ caps, bloc assemblé.
- `tests/test_top1000.py` — parsing payload CoinGecko (rows réelles uniquement),
  classification suivies/nouvelles/exclues avec raisons, tri par volume + cap, statuts
  honnêtes `disabled`/`unavailable`.

## 14. Améliorations futures

- Backtest **prédiction vs réalité** J+1/J+7 (la table `daily_crypto_asset_score` est déjà prête).
- Enrichissement **1h/7j/30j** par actif de l'univers (croisement CoinGecko markets ↔
  paires Binance) pour lever le plafond de confiance d'horizon.
- Corrélations réelles entre actifs (matrice sur les retours) pour la contribution au
  risque portefeuille — aujourd'hui les caps sont des règles statiques prudentes.
- Modèle ML supervisé en remplacement transparent de `opportunity_score`/`up_probability`.
- Export **PDF**, envoi **email/Telegram/Discord**, tracking de performance des
  recommandations.
