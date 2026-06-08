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
