# Runbook — exploitation locale

## Démarrer le stack (recommandé : supervision unique)

**VS Code** : `Terminal → Run Task…` :
- **Start Dev Supervisor** — docker + bootstrap + workers + API dans un terminal dédié.
- **Start Full Stack** — supervisor + ouverture du cockpit dans le navigateur.
- **Stop Full Stack** — stoppe tout puis `docker compose down`.
- **Run tests (offline)** — `pytest -q`.

**PowerShell** (racine du repo) :
```powershell
# script robuste (venv auto, garde-fous)
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_dev_supervisor.ps1
# commande directe
$env:PYTHONPATH="."; python .\scripts\dev_supervisor.py
# full stack (nouvelle fenêtre + cockpit)
.\scripts\start_all.ps1
# arrêt
.\scripts\stop_all.ps1
```

> ⚠️ Le point-virgule est obligatoire : `$env:PYTHONPATH="."; python ...` (PowerShell n'autorise pas deux commandes adjacentes).

## Démarrage manuel (sans supervisor)

```powershell
docker compose up -d
$env:PYTHONPATH="."
python -m workers.bootstrap          # oneshot : peuple market_ref
python -m workers.ingestor           # terminal 1
python -m workers.aggregator         # terminal 2
python -m workers.feature_worker     # terminal 3
python -m workers.social_ingestor    # terminal 4 (idle si ENABLE_MOCK_SOCIAL=False)
python -m workers.antigravity_bot    # terminal 5
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000   # terminal 6
```
Le supervisor évite d'ouvrir 6 terminaux — préférez-le.

## Vérifier que tout fonctionne

1. **Cockpit** : http://localhost:8000/ — badge `Ops n/m` passe au vert.
2. **Ops** : http://localhost:8050/api/ops/health — `running/total`, statut.
3. **Santé API** : http://localhost:8000/api/health — `db_status: up`, symboles `fresh`, bloc `binance_live.connected: true`, bloc `universe.count`.
4. **Ingestion vivante** :
   ```bash
   docker exec -it crypto_timescaledb psql -U crypto_user -d crypto_market_data \
     -c "SELECT exchange_code, symbol, count(*), max(ts_event) FROM trade_tick GROUP BY 1,2;"
   ```
   Le `max(ts_event)` doit avancer.
5. **Bougies** :
   ```bash
   docker exec -it crypto_timescaledb psql -U crypto_user -d crypto_market_data \
     -c "SELECT symbol, max(bucket_start) FROM ohlcv_1s WHERE exchange_code='binance' GROUP BY 1;"
   ```
6. **Marché** : badge cockpit `Connecting… → Waiting data → Live`. **Jamais `Live` sans bougie réelle.**
7. **Univers (combien sur N + pourquoi le reste manque)** :
   ```bash
   python scripts/diagnose_universe.py --limit 300        # rapport lisible (cause + reco)
   python scripts/diagnose_universe.py --limit 300 --json # verdict JSON
   python scripts/diagnose_universe.py --limit 300 --strict  # exit 1 si chargé < demandé (CI/smoke)
   ```
   Tape `GET /api/market/universe/debug` (aucune DB). Si chargé < demandé, la **cause dominante** et la **partition des rejets** sont imprimées.
8. **Latence des endpoints d'affichage** :
   ```bash
   python scripts/benchmark_snapshot_api.py --runs 30                      # univers + klines
   python scripts/benchmark_snapshot_api.py --symbol BTC/USDT --range 1D   # p50/p95/p99
   ```

## Lire les logs
- **Cockpit** : panneau **🖥 Ops / Terminals** (logs temps réel filtrables par process + niveau).
- **API REST** : `GET /api/ops/events?process=ingestor&level=ERROR`, `GET /api/ops/incidents`.
- **DB applicative** : `GET /api/system/logs` (table `system_log`).
- **Docker** : `docker compose logs -f db`.
- **Incidents** : `logs/ops_incidents.jsonl`.

## Métriques (Prometheus)
- API : http://localhost:8000/metrics
- Workers : `:9101` ingestor, `:9102` feature, `:9103` social, `:9104` bot.
- Clés : `market_events_total`, `queue_depth`, `db_write_latency_ms`, `rows_written_total`, `dlq_total`, `ai_decisions_total`, `paper_orders_total`, `worker_last_success_ts`, `binance_live_connected`, `universe_symbols_loaded`.

## Procédure de rollback / incident d'ingestion

Si corruption ou lag d'ingestion qui s'accumule (DB hors-ligne trop longtemps) :
1. **Couper** l'`ingestor` (panneau Ops `stop` ou `POST /api/ops/process/stop {"name":"ingestor"}`).
2. **Inspecter la DLQ** :
   ```sql
   SELECT error_class, count(*) FROM dead_letter_event WHERE NOT resolved GROUP BY 1;
   SELECT * FROM dead_letter_event ORDER BY created_at DESC LIMIT 20;
   ```
3. Si nécessaire, **supprimer les chunks récents** puis relancer (le flux live réécrit sans risque grâce à `event_uid` idempotent) :
   ```sql
   SELECT drop_chunks('trade_tick', newer_than => now() - INTERVAL '1 hour');
   ```
4. **Relancer** l'ingestor.

## Rollback de code

```bash
git log --oneline -10
git revert <hash>          # annule un commit en créant un commit inverse (préféré)
# ou, si pas encore poussé :
git reset --hard <hash>    # destructif — uniquement local, jamais sur une branche partagée
```
> Pour une **migration** déjà appliquée : écrire une migration inverse (`NNN_revert_xxx.sql`) plutôt que d'éditer une migration existante.

## Réinitialiser la base (destructif — demander confirmation)
```bash
docker compose down -v && docker compose up -d   # rejoue 001→007 à neuf
```

## Problèmes fréquents
Voir [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
