# PortefeuilleCrypto

## Mission
PortefeuilleCrypto est un système local de paper trading crypto piloté par:
- données de marché temps réel
- actualité crypto
- signaux sociaux
- moteur de décision explicable
- cockpit de supervision temps réel

Le projet doit rester simple à lancer sur une machine locale via Docker, observable, et modulaire.

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
- **Données social/news = MOCK uniquement** : `social/mock_collector.py` est la seule source. Les moteurs sont réels mais tournent sur des données fabriquées. Tables `tracked_site` / `tracked_asset_source_map` créées mais inutilisées. → brancher de vraies sources (X, Reddit, RSS, annonces exchange).
- **Observabilité Prometheus absente** : `prometheus-client` est dans `requirements.txt` mais **zéro instrumentation** dans le code. `market_data_age_ms`/`social_data_age_ms` écrits à 0.
- **`docker-compose.yml` = infra only** (db + redis). Aucun service applicatif (workers/api/frontend) → compose full-stack manquant.
- **Profondeur de carnet = heuristique** et incohérente : `market_features.py` utilise `(bid·q+ask·q)·5.0`, `antigravity_bot.py` utilise `bid·q·10`. Deux sources de vérité. Flag `ENABLE_L2_BOOK=False`.
- **Pas de boucle d'évaluation ex-post** : tables `outcome_eval` / `source_influence_snapshot` créées mais aucun worker ne les remplit → pas de backtest ni d'apprentissage de crédibilité des acteurs.
- **Aucun test** malgré `pytest` dans les dépendances.
- **Garde-fou staleness manquant** : le scorer/bot utilisent le dernier `bbo_tick` sans vérifier son âge → risque de décision sur données périmées.

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

### Ports
| Service | Port |
|---|---|
| API / cockpit | 8000 |
| Ops supervisor (API + WS) | 8050 |
| Prometheus workers | 9101–9104 |
| PostgreSQL/TimescaleDB | 5432 |
| Redis | 6379 |

### Tests
`pytest -q` (offline, pas de DB requise) : `test_scorer_thresholds`, `test_social_availability` (garde-fous anti-mock), `test_process_supervisor` (capture stdout/stderr + crash + traceback sur **vrais** subprocess), `test_engine_decimal`.

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
| `api` | `python -m uvicorn api.main:app --host 127.0.0.1 --port 8000` | long-running | non | oui |

Notes de vérité (ne pas s'en écarter) :
- **Pas de process frontend séparé.** Le cockpit est servi par l'API elle-même (`api/main.py` monte `StaticFiles` sur `/` au port 8000). Ne **jamais** lancer `python -m http.server 8000` — cela entrerait en conflit avec l'API sur le même port.
- **`--reload` est volontairement omis** sous supervision pour que le PID suivi soit le vrai serveur, pas le process reloader parent.
- **`social_ingestor`** tourne mais ne produit de la donnée *réelle* que si une vraie source est branchée ; avec `ENABLE_MOCK_SOCIAL=False` (défaut) il n'émet rien de réel (voir règle anti-mock PR2).
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
