# Contribuer — PortefeuilleCrypto / Antigravity Crypto Cockpit

Guide pour développer sur le projet. Pour l'orientation technique : [CLAUDE.md](CLAUDE.md) + [docs/](docs/).

## Principes
- **Comprendre l'existant avant de modifier.** Préférer la **plus petite modification crédible** ; modifier l'existant plutôt que créer.
- **Données réelles uniquement** : ne jamais présenter mock/random/placeholder comme réel → `unavailable`/`n/a` explicite.
- **Aucune logique aléatoire** dans la chaîne décisionnelle.
- **Observabilité** : toute fonctionnalité importante a une voie d'observation (log, métrique, statut API, ou affichage UI).
- **Pas de secret en dur** : tout passe par `.env` (gitignored).

## Mettre en place l'environnement
```powershell
python -m venv venv
.\venv\Scripts\activate            # Windows
pip install -r requirements.txt
git config core.hooksPath .githooks   # active le hook pre-commit (rappel doc)
```

## Workflow de développement
1. **Brancher** depuis `main` :
   ```bash
   git checkout main && git pull
   git checkout -b <type>/<sujet-court>      # ex. feat/social-reddit, fix/chart-freeze
   ```
   Préfixes : `feat/`, `fix/`, `chore/`, `refactor/`, `docs/`.
2. **Développer** par petits changements logiques et traçables.
3. **Tester** avant chaque commit (voir ci-dessous).
4. **Documenter** le changement (voir « Documentation obligatoire »).
5. **Commit** ciblé, puis **PR**.

## Tester avant commit
```bash
pytest -q                                 # offline, pas de DB requise
python scripts/check_docs_sync.py --staged   # synchro doc <-> code (rappel)
```
Pour un test fonctionnel (cockpit/API/workers), voir [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Conventions de nommage
- **Branches** : `<type>/<sujet-court>` en kebab-case.
- **Commits** : `type: description courte` (`feat: …`, `fix: …`, `chore: …`, `docs: …`), une ligne, FR ou EN, cohérent avec l'historique. Un commit = un changement logique.
- **Migrations** : `db/migrations/NNN_description.sql`, numérotation croissante, **idempotent** (`IF NOT EXISTS`, gardes `DO $$`). Ne jamais éditer une migration déjà appliquée → en écrire une nouvelle.
- **Workers** : `python -m workers.<nom>` ; ajouter dans `scripts/dev_supervisor.build_specs()` **uniquement si le fichier existe**.
- **Symboles** : canonique `BASE/QUOTE` (ex. `BTC/USDT`) ; `native_symbol` pour le format exchange.
- **Métriques** : `snake_case`, suffixe `_total` (Counter) / `_ms` (durées) ; labels à **faible cardinalité**.

## Documentation obligatoire
**Toute modification technique met à jour la documentation.** Un contrôle automatique (`scripts/check_docs_sync.py`) vérifie qu'un changement sur `api/`, `workers/`, `collectors/`, `market/`, `signal_engine/`, `social/`, `paper_execution/`, `models/`, `db/migrations/`, `db/writer.py`, `frontend/`, `config.py`, `metrics.py` ou `docker-compose.yml` s'accompagne d'une mise à jour de doc (`docs/`, `CLAUDE.md`, `README.md`, `CONTRIBUTING.md`).

- **Local** : hook `pre-commit` (avertit) — activer via `git config core.hooksPath .githooks`.
- **CI** : GitHub Action `docs-check` (bloquante sur PR).

Cibles selon la zone touchée :

| Tu changes… | Mets à jour… |
|---|---|
| un endpoint | `docs/API.md` |
| une table / migration | `docs/DATABASE.md` |
| un worker | `docs/WORKERS.md` |
| Docker / variable d'env | `docs/DEPLOYMENT.md` |
| le frontend | `docs/FRONTEND.md` |
| une borne mémoire / chemin chaud | `docs/PERFORMANCE.md` |
| un problème récurrent découvert | `docs/TROUBLESHOOTING.md` |
| **tout changement significatif** | `docs/CHANGELOG_TECH.md` (+ `CLAUDE.md` si structurel) |

Règles : **ne jamais** laisser un changement technique sans doc ; **ne pas supprimer** une doc sans la remplacer ; signaler les zones incertaines avec « à vérifier ».

## Ouvrir une PR
1. Pousser la branche : `git push -u origin <branche>`.
2. Ouvrir la PR (le modèle [`.github/pull_request_template.md`](.github/pull_request_template.md) se charge — remplir la checklist doc).
3. Cible : `main`.

### Checklist avant merge
- [ ] `pytest -q` vert.
- [ ] Action `docs-check` verte (ou doc mise à jour).
- [ ] Vérifié localement selon le changement.
- [ ] Aucune donnée mock présentée comme réelle ; aucune logique aléatoire.
- [ ] Risques et rollback décrits dans la PR.
- [ ] Pas de secret/fichier temporaire/rapport local commité.

## Git (rappel des règles du projet)
- `git add` **ciblé** sur les fichiers du changement (jamais `git add -A` aveugle).
- Laisser hors commit les fichiers non liés déjà modifiés.
- **Jamais** de `force push`. Si un push est rejeté, s'arrêter et afficher l'erreur.
- Voir aussi `CLAUDE.md` → « Git workflow obligatoire ».
