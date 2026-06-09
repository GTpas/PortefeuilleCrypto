# Daily Crypto Intelligence Report

> **Avertissement / Not financial advice.** Ce rapport est généré automatiquement à
> partir de données de marché **réelles** (Binance Spot 24h + contexte macro). Il a une
> vocation **informative et pédagogique**. **Ce n'est PAS un conseil financier
> personnalisé.** Les prédictions sont **indicatives**, exprimées en **probabilités** et
> **scénarios**, et n'ont **aucune valeur de certitude**. Le marché crypto est volatil :
> ne risquez que ce que vous pouvez vous permettre de perdre.

## 1. Objectif

Produire, **chaque jour automatiquement** (par défaut à minuit), une analyse complète et
**compréhensible par un débutant** mais crédible financièrement, sur les **~300
cryptomonnaies** déjà suivies par le cockpit (univers Binance Spot). Pour chaque crypto :
classement global, prédiction indicative court terme, signal **BUY / HOLD / SELL / AVOID**,
justification claire, ratios explicables, rating **A+→E**, explication pédagogique, et une
section **source evidence** basée sur les vraies données disponibles.

## 2. Périmètre & limites (réel uniquement)

Le rapport applique la **règle absolue anti-mock** du projet : aucune donnée n'est
fabriquée. Quand une donnée n'existe pas, elle est affichée **`N/A`** et **réduit le score
de confiance** — jamais une valeur inventée.

| Donnée | Disponible ? | Source |
|---|---|---|
| Prix, variation **24h**, volume 24h, nb trades 24h | ✅ réel | Binance `!ticker@arr` (universe) |
| High/Low/Open 24h, **VWAP** 24h, spread (bid/ask) | ✅ réel | Binance ticker 24h |
| Variation **1h / 7j / 30j** | ❌ N/A | non fournie par le ticker 24h |
| **Market cap** par actif | ❌ N/A | nécessite un agrégateur (CoinGecko markets) |
| Profondeur L2 (depth) par actif | ❌ N/A | réservée au symbole sélectionné (Tier 3) |
| Régime de marché, Fear&Greed, mcap 24h | ✅ réel | tier macro `global_context` |

> Ces absences sont **assumées** : le rapport reste honnête et la confiance est abaissée en
> conséquence. Une montée en richesse (1h/7j/30j, market cap) via CoinGecko markets est une
> amélioration future (cf. §9).

## 3. Architecture ajoutée

Module isolé `reports/` (logique pure vs I/O, comme le reste du repo) :

- **`reports/scoring.py`** — formules **pures** (ratios, scores, rating, signal, prédiction,
  régime). Zéro I/O. **Source unique de vérité** des chiffres → le worker et l'API ne
  peuvent pas diverger.
- **`reports/generator.py`** — `build_daily_report(rows, global_context, …)` → dict JSON
  structuré + `render_markdown(report)` → Markdown français. Pur (données en entrée → sortie).
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
**Final Opportunity Score** `0–100` :

| Composante | Poids |
|---|---|
| momentum | 25 % |
| volume confirmation | 20 % |
| liquidity | 20 % |
| relative strength vs BTC | 15 % |
| trend quality | 10 % |
| market context | 5 % |
| source confidence | 5 % |

**Risk Score** `0–100` : volatilité 30 %, drawdown 25 %, spread 15 %, faible liquidité 20 %,
données manquantes 10 %.

**Confidence Score** `0–100` : complétude des données 35 %, liquidité 30 %, fraîcheur 20 %,
couverture d'horizon 15 % (**plafonnée** car seul l'horizon 24h est réel → on ne prétend
jamais à une confiance maximale).

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
   **ou** risk ≥ 80. (Mouvement fragile/manipulable ou données insuffisantes.)
2. **BUY** — opportunity ≥ 75 **et** risk ≤ 45 **et** confiance ≥ 65.
3. **SELL** — variation 24h ≤ −3 % **et** momentum < 0.40 **et** risk ≥ 55.
4. **HOLD** — sinon (pas de confirmation suffisante).

Chaque signal porte une **justification** courte (FR) et une **explication simple** pour
débutant, dérivées des facteurs dominants (momentum, liquidité, volume, risque).

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
  - `POST /api/reports/daily/generate` — génère **maintenant** (test/admin), depuis les hubs.
  - `GET /api/reports/daily/latest/assets/{symbol}` — analyse détaillée d'une crypto.
- **Formats** : **JSON** structuré (`reports/daily_crypto_report_YYYY-MM-DD.json`, pour
  l'API/front) + **Markdown** lisible (`reports/daily_crypto_report_YYYY-MM-DD.md`). Un PDF
  est une amélioration future (non prioritaire).
- **Frontend** : bouton header **📅 Report** → modale « Rapport Crypto Quotidien » : résumé
  + KPIs, distribution des ratings, top BUY/SELL/À-surveiller, table **filtrable (signal,
  rating) / triable / recherchable** des 300, détail d'une crypto au clic.

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
`DAILY_REPORT_HTTP_TIMEOUT`, `DAILY_REPORT_PERSIST_DB`.

## 13. Tests

`tests/test_daily_report.py` (offline) : bornes/direction des ratios, bandes de rating,
signaux BUY/HOLD/SELL/AVOID aux seuils, robustesse données manquantes (pas de crash, N/A
honnête), prudence des prédictions (proba strictement dans `[0.15,0.85]`, jamais de
certitude), assemblage JSON + rendu Markdown, **univers simulé de 300 cryptos + perf**
(< 3 s), round-trip du store + historique, helpers de planification du worker.

## 14. Améliorations futures

- Backtest **prédiction vs réalité** J+1/J+7 (la table `daily_crypto_asset_score` est déjà prête).
- Enrichissement **1h/7j/30j + market cap** via CoinGecko `/coins/markets`.
- Modèle ML supervisé en remplacement transparent de `opportunity_score`/`up_probability`.
- Export **PDF**, envoi **email/Telegram/Discord**, page front avancée, tracking de
  performance des recommandations.
