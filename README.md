# PortefeuilleCrypto — Antigravity Crypto Cockpit

Système **local** de paper trading crypto, temps réel, explicable et observable.

Il ingère les données de marché de Binance/Kraken/Coinbase, les stocke dans TimescaleDB, calcule des features de microstructure et un signal social, produit un **score de décision explicable** par actif, exécute des **trades simulés** (paper trading) sous contraintes de risque réelles, et affiche le tout dans un **cockpit temps réel** dont le prix « colle à Binance UI ».

> 🧭 **Pour intervenir vite (Claude / dev)** : [`CLAUDE.md`](CLAUDE.md) + [`docs/`](docs/).
> 🤝 **Pour contribuer** : [`CONTRIBUTING.md`](CONTRIBUTING.md).
> Ce README est l'entrée pour **découvrir** le projet.

## Ce que fait le projet
- **Ingestion temps réel** (WebSocket) : trades + best bid/ask des 3 exchanges, normalisés, écrits par batch idempotent (DLQ en cas d'échec).
- **Stockage TimescaleDB** : hypertables, bougies OHLCV (1s → agrégats 1m/5m), features de marché, compression + rétention.
- **Moteur de décision réel** (aucune logique aléatoire) : `S_total = 0.45·social + 0.45·marché + 0.10·(2·risque − 1)`, décomposable et journalisé.
- **Paper trading** sous règles de risque (8 positions max, 20 %/position, 10 % cash mini, frais 10 bps, slippage dynamique, rejet > 40 bps).
- **Cockpit** : prix/chart Binance live, univers ~300 cryptos tendances, watchlist, signaux, microstructure, portefeuille, logs, panneau Ops.
- **Données réelles uniquement** : jamais de mock présenté comme réel — sinon `unavailable`/`n/a` explicite.

Architecture détaillée : [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Prérequis
- Docker + Docker Compose
- Python 3.10+
- ≥ 4 Go RAM, SSD recommandé

## Installation
```powershell
# 1. Infra (PostgreSQL/TimescaleDB + Redis) — migrations jouées au 1er boot
docker compose up -d

# 2. Environnement Python
python -m venv venv
.\venv\Scripts\activate            # Windows  (Linux/Mac : source venv/bin/activate)
pip install -r requirements.txt
```
Configuration optionnelle via `.env` (voir [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)). Par défaut : DB locale, symboles `BTC/USDT`, `ETH/USDT`, `SOL/USDT`.

## Lancer la stack (recommandé : une seule commande)

Le **supervisor** démarre et surveille tout (docker + bootstrap + workers + API) :

```powershell
$env:PYTHONPATH="."; python .\scripts\dev_supervisor.py
```
Ou, sous VS Code : `Terminal → Run Task… → Start Dev Supervisor` (ou **Start Full Stack** pour ouvrir aussi le cockpit). Détails et dépannage : [docs/RUNBOOK.md](docs/RUNBOOK.md).

### Ouvrir le dashboard
- **Cockpit** : http://localhost:8000/
- **Santé API** : http://localhost:8000/api/health
- **Ops** : http://localhost:8050/api/ops/health

### Lancer l'API seule
```powershell
$env:PYTHONPATH="."; python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```
> ⚠️ Ne **pas** lancer un `http.server` sur 8000 : l'API sert déjà le cockpit (conflit de port).

### Lancer les workers manuellement (sans supervisor)
```powershell
$env:PYTHONPATH="."
python -m workers.bootstrap          # oneshot : peuple market_ref
python -m workers.ingestor           # collecte WS → trade_tick/bbo_tick
python -m workers.aggregator         # OHLCV 1s
python -m workers.feature_worker     # features de marché
python -m workers.social_ingestor    # social (idle si ENABLE_MOCK_SOCIAL=False)
python -m workers.antigravity_bot    # décisions + paper trades
```
Rôle de chaque worker : [docs/WORKERS.md](docs/WORKERS.md).

## Vérifier que l'ingestion fonctionne
1. `GET http://localhost:8000/api/health` → `db_status: up`, symboles `fresh`, `binance_live.connected: true`.
2. Les trades arrivent :
   ```bash
   docker exec -it crypto_timescaledb psql -U crypto_user -d crypto_market_data \
     -c "SELECT exchange_code, symbol, count(*), max(ts_event) FROM trade_tick GROUP BY 1,2;"
   ```
   `max(ts_event)` doit avancer.
3. Cockpit : badge marché passe `Connecting… → Waiting data → Live` (jamais `Live` sans bougie réelle).

## Lire les logs
- **Cockpit** → panneau **🖥 Ops / Terminals** (temps réel, filtrable).
- **API** → `GET /api/ops/events`, `GET /api/ops/incidents`, `GET /api/system/logs`.
- **Docker** → `docker compose logs -f db`.
- **Incidents** → `logs/ops_incidents.jsonl`.

## Erreurs courantes (extrait)
| Symptôme | Fix rapide |
|---|---|
| `Jeton inattendu « python »` (PowerShell) | utiliser le `;` : `$env:PYTHONPATH="."; python ...` |
| `Port 8050 déjà occupé` | un supervisor tourne déjà → `.\scripts\stop_all.ps1` |
| `db_status: down` | `docker compose up -d` ; attendre le healthcheck |
| Badge jamais `Live` | tester `GET /api/binance/debug/{symbol}` (géo-blocage / WS) |
| Univers vide / < 300 | `GET /api/market/universe/debug` (souvent `MIN_QUOTE_VOLUME`) |
| `SOC n/a` | normal : social mock désactivé par défaut (`ENABLE_MOCK_SOCIAL=False`) |

Catalogue complet : [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Tests
```bash
pytest -q        # offline, pas de DB requise
```

## Rollback / incident
Couper l'`ingestor`, inspecter `dead_letter_event`, au besoin `SELECT drop_chunks('trade_tick', newer_than => …)` puis relancer (réécriture sûre grâce à `event_uid` idempotent). Procédure : [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Ports
| Service | Port |
|---|---|
| API / cockpit | 8000 |
| Ops supervisor | 8050 |
| Prometheus workers | 9101–9104 |
| PostgreSQL/TimescaleDB | 5432 |
| Redis | 6379 |
