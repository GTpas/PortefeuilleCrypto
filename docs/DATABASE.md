# Base de données — PostgreSQL / TimescaleDB

> Source de vérité : `db/migrations/001→007`. Montées **automatiquement au premier boot** du conteneur `db` (montées dans `/docker-entrypoint-initdb.d`). Pour rejouer sur une base existante, voir [§ Migrations](#migrations).

Toutes les migrations sont **idempotentes** (`CREATE ... IF NOT EXISTS`, gardes `DO $$`). Connexion par défaut : `postgresql://crypto_user:crypto_password@localhost:5432/crypto_market_data`.

## Séparation des données (politique)

`brut → normalisé → features → décisions`. TimescaleDB = séries temporelles + signaux structurés + exécution. Pas de gros blobs textuels en DB si un stockage compressé local suffit.

## Tables par domaine

### Référentiel (migration 001)
| Table | Rôle | PK / clés | Notes |
|---|---|---|---|
| `exchange_ref` | Exchanges supportés | PK `code` | Seed binance/kraken/coinbase. |
| `market_ref` | Métadonnées marchés (peuplé par `bootstrap`) | `(exchange_code, native_symbol)` + `(exchange_code, symbol)` uniques | `symbol` canonique `BASE/QUOTE`, `native_symbol` natif, precision, `meta` JSONB. |

### Données brutes de marché (migration 001 — hypertables)
| Table | Rôle | PK (idempotence) | Hypertable / rétention |
|---|---|---|---|
| `trade_tick` | Trades normalisés | `(ts_event, exchange_code, symbol, event_uid)` | chunk 1j ; rétention **90j** ; compression > 7j (segmentby exchange,symbol). |
| `bbo_tick` | Best bid/ask | `(ts_event, exchange_code, symbol, event_uid)` | chunk 1j ; rétention **30j** ; compression > 3j. |
| `ohlcv_1s` | Bougies 1s dérivées | `(bucket_start, exchange_code, symbol)` | chunk 7j ; rétention **365j** ; compression > 30j. `source` = `derived_trades`. |
| `ingestion_checkpoint` | Curseurs collecteurs | `(collector_name, shard_id)` | Reprise d'ingestion. |
| `dead_letter_event` | DLQ (écritures échouées) | `id` BIGSERIAL | `error_class`, `error_message`, `raw_payload`, `resolved`. |

Index clés : `(exchange_code, symbol, ts_event DESC)` sur trade/bbo ; GIN `payload` sur `trade_tick` ; `(exchange_code, symbol, bucket_start DESC)` sur `ohlcv_1s`.

### Agrégats continus (migrations 003 / 007)
| Vue matérialisée | Source | Bucket | Refresh |
|---|---|---|---|
| `ohlcv_1m` | `ohlcv_1s` | 1 min | start_offset 10m / end_offset 1m / sched 1m |
| `ohlcv_5m` | `ohlcv_1s` | 5 min | start_offset 30m / end_offset 5m / sched 5m |
| `market_feature_1m` | `market_feature_1s` | 1 min | start_offset 10m / end_offset 1m / sched 1m |

> Créées `WITH NO DATA` — se remplissent via les policies de refresh.

### Paper trading (migrations 002 / 007)
| Table | Rôle | Notes |
|---|---|---|
| `paper_portfolio` | Portefeuille(s) | Seed `Antigravity Default` (capital 10 000). `current_cash`, `total_value`. |
| `paper_position` | Positions ouvertes | unique `(portfolio_id, symbol, exchange_code)` ; `qty`, `average_entry_price`, `unrealized_pnl`. |
| `paper_trade` | **Trades exécutés** (table réelle des ordres) | `side` ∈ buy/sell, `slippage_bps`, `fees`, `signal_score`, `reason`, `decision_snapshot_id` (ajouté en 007). |
| `portfolio_state` | Historique valeur portefeuille (hypertable) | `total_value`, `current_cash`, `invested_value`, `num_positions`, `max_position_weight`, `drawdown_pct`, `exposure_pct`, `positions_snapshot` JSONB. |

> ⚠️ Il n'existe **pas** de table `paper_order` — les ordres simulés vivent dans `paper_trade`.

### Signaux & sentiments (migrations 005 / 007)
| Table | Rôle | Notes |
|---|---|---|
| `tracked_source` | Sources suivies (twitter/reddit/…/`mock_social`) | `reliability_score`. Marqueur mock = `name ILIKE 'mock%'`. |
| `tracked_actor` | Acteurs/auteurs | `influence_score`, `actor_type`. |
| `tracked_site` | Sites officiels/gouvernance (créée, **inutilisée**) | TODO : brancher. |
| `tracked_asset_source_map` | Mapping actif↔source (créée, **inutilisée**) | TODO : brancher. |
| `raw_content` | Contenu social brut ingéré | `content_hash` UNIQUE (dédup), `raw_payload` JSONB. |
| `content_entity` | Entités extraites (asset/actor/narrative/event) | `entity_value`, `content_type`, `entity_confidence`. |
| `social_signal_1m` | Signal social agrégé 1m (hypertable) | 8 sous-métriques + `source_breakdown` (colonnes ajoutées en 007). |
| `social_signal_5m` | Signal social agrégé 5m (hypertable) | rétention 180j. |

### Décisions & traçabilité (migrations 005 / 007 — hypertables)
| Table | Rôle | Notes |
|---|---|---|
| `decision_snapshot` | Snapshot d'évaluation par symbole | `s_social/s_market/s_risk/s_total`, `action_proposed`, `reason_code`, `quality_grade`, `confidence_score`. PK `(id, ts_eval)`. |
| `decision_factor` | Décomposition explicable | une ligne par facteur : `factor_category` (social/market/risk), `factor_name`, `factor_value`, `score_contribution`, `explanation`. |
| `decision_evidence_link` | Lien décision ↔ `raw_content` | `relevance_score`. Mock filtré côté API. |
| `signal_quality_audit` | Audit qualité/fraîcheur | `social_sources_count`, `market_data_age_ms`, `social_data_age_ms`, `has_sufficient_social/market`, `quality_grade` (full/partial/degraded/mock), `degradation_reasons[]`. |
| `signal_log` | Ancien log de signaux (migration 002) | Conservé ; remplacé par `decision_snapshot`. |

### Évaluation ex-post & crédibilité (migration 007 — créées, **non remplies**)
| Table | Rôle | État |
|---|---|---|
| `outcome_eval` | Retour ex-post (return par horizon 1h/4h/24h/3d, `was_correct`) | **TODO** : aucun worker ne la remplit → pas de backtest. |
| `source_influence_snapshot` | Crédibilité acteur dans le temps (`historical_lift`, `accuracy_rate`) | **TODO** : aucun worker ne la remplit. |

### Logs système (migration 006)
| Table | Rôle | Notes |
|---|---|---|
| `system_log` | Logs applicatifs (hypertable) | `component`, `level`, `message`, `metadata` JSONB. Rétention 30j. Lue par `/api/system/logs`. |

## Qui lit / écrit quoi (résumé)

| Producteur | Écrit |
|---|---|
| `ingestor` + `db/writer` | `trade_tick`, `bbo_tick`, `dead_letter_event` |
| `aggregator` | `ohlcv_1s` |
| `feature_worker` | `market_feature_1s`, `portfolio_state` |
| `social_ingestor` | `raw_content`, `content_entity`, `social_signal_1m`, `social_signal_5m` |
| `antigravity_bot` (via scorer/engine) | `decision_snapshot`, `decision_factor`, `decision_evidence_link`, `signal_quality_audit`, `paper_trade`, MAJ `paper_portfolio`/`paper_position` |
| `bootstrap` | `market_ref` |

| Consommateur | Lit (principales) |
|---|---|
| API cockpit | `decision_snapshot`, `decision_factor`, `signal_quality_audit`, `ohlcv_1s`, `market_feature_1s`, `social_signal_1m`, `paper_*`, `portfolio_state`, `system_log`, `content_entity`/`raw_content` |

## <a name="migrations"></a>Migrations

- **Convention** : `NNN_description.sql`, numérotation croissante, idempotent.
- **Premier boot** : exécutées dans l'ordre par l'image TimescaleDB (volume `./db/migrations:/docker-entrypoint-initdb.d`).
- **Base existante** (rejouer une nouvelle migration manuellement) :
  ```bash
  docker exec -i crypto_timescaledb psql -U crypto_user -d crypto_market_data < db/migrations/00X_xxx.sql
  ```
- **Idempotence** : sûr de rejouer ; les `CREATE ... IF NOT EXISTS` et `DO $$ ... END $$` évitent les erreurs.

> Ajouter une migration ⇒ mettre à jour ce fichier (table + rôle + rétention/compression) et `docs/CHANGELOG_TECH.md`.

## Pièges connus

- **Course inter-exchanges** : `ohlcv_1s` / `market_feature_1s` contiennent une ligne **par exchange** pour le même symbole. Toute lecture « latest » d'affichage **doit** filtrer `exchange_code = DISPLAY_EXCHANGE` (sinon Coinbase BTC-USD peut s'afficher sous un label Binance BTC/USDT). Déjà appliqué dans l'API.
- **Idempotence des ticks** : la dédup repose sur `event_uid`. Pour Kraken/Coinbase BBO, `event_uid` est basé sur l'horodatage courant (pas un id natif) → quasi-unique, pas strictement garanti unique (**à vérifier** si besoin de dédup stricte BBO multi-exchange).
