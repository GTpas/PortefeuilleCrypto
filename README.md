# Real-time Crypto Market Data Ingestion Pipeline

Ce projet implémente une chaîne d'ingestion robuste de données de marché cryptographiques en temps réel (Spot trades, best bid/ask, et bougies OHLCV 1s), comme spécifié dans le rapport de recherche approfondie.

L'architecture s'appuie sur :
- **Python (asyncio / websockets)** pour les collecteurs.
- **PostgreSQL avec TimescaleDB** pour le stockage temporel et l'agrégation continue (bien qu'ici effectuée via un script Python par souci de portabilité).
- **Docker Compose** pour la base de données.

## Prérequis

- Docker et Docker Compose
- Python 3.10+ (ou `uv`, ou `venv`)
- Au moins 4 Go de RAM et un SSD (80 Go NVMe recommandé en production)

## Installation et démarrage rapide

### 1. Démarrer la base de données (TimescaleDB)

Le fichier `docker-compose.yml` inclut une image TimescaleDB et mappe les scripts d'initialisation.

```bash
docker-compose up -d
```

*(Lors du premier lancement, TimescaleDB exécutera le script `db/migrations/001_initial_schema.sql` pour créer les tables et les hypertables.)*

### 2. Configurer l'environnement Python

```bash
python -m venv venv
# Sur Windows :
venv\Scripts\activate
# Sur Linux/Mac :
# source venv/bin/activate

pip install -r requirements.txt
```

### 3. Configurer l'application (optionnel)

Les variables d'environnement peuvent être écrites dans un fichier `.env` à la racine ou exportées. 
Par défaut, la connexion pointe vers `postgresql://crypto_user:crypto_password@localhost:5432/crypto_market_data` et s'abonne à `BTC/USDT`, `ETH/USDT`, `SOL/USDT`.
Voir `config.py` pour les valeurs possibles.

### 4. Bootstrap des marchés (CCXT)

Ce worker utilise l'API REST de CCXT pour télécharger les métadonnées de Binance, Kraken et Coinbase, et peuple les tables `exchange_ref` et `market_ref`.

```bash
set PYTHONPATH=.
python workers/bootstrap.py
```

### 5. Lancer l'ingestion live (WebSockets)

Le script principal se connecte aux WebSockets des exchanges configurés, gère la backpressure, et batch les écritures en DB.

```bash
set PYTHONPATH=.
python workers/ingestor.py
```

### 6. Lancer l'agrégateur OHLCV 1s

Un script séparé lit les trades récents et génère des bougies 1 seconde (en production, cela peut tourner en permanence ou être remplacé par une Materialized View TimescaleDB).

```bash
set PYTHONPATH=.
python workers/aggregator.py
```

## Limites connues et Travaux Futurs

- **Résilience avancée** : Le script de base gère les coupures via `backoff` et `websockets`, mais la DLQ (Dead Letter Queue) se contente pour l'instant d'enregistrer l'erreur entière en base si le batch entier échoue, sans le découper au niveau de la ligne en erreur.
- **Agrégation continue** : Dans une production massive, le worker `aggregator.py` devrait être remplacé par les vues matérialisées natives (Continuous Aggregates) de TimescaleDB.
- **Monitoring** : Il manque un conteneur Grafana/Prometheus (qui peut être ajouté dans le docker-compose) couplé aux métriques exposées par l'application (à implémenter via `prometheus_client`).

## Plan de Rollback / Incident

En cas de corruption ou si le lag d'ingestion s'accumule trop (ex: base de données hors ligne trop longtemps) :
1. Couper le worker `ingestor.py`.
2. Inspecter les tables `dead_letter_event` pour voir les échecs.
3. Si nécessaire, supprimer les chunks très récents via `SELECT drop_chunks('trade_tick', newer_than => ...);` et relancer l'ingestion depuis le flux live (qui écrasera sans risque grâce à l'`event_uid` idempotent).
