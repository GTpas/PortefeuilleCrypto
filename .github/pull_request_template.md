<!-- PortefeuilleCrypto / Antigravity Crypto Cockpit — modèle de PR -->

## Résumé
<!-- Quoi et pourquoi, en 1-3 phrases. Orienté fichiers modifiés + risques. -->

## Type
- [ ] feat — nouvelle fonctionnalité
- [ ] fix — correction de bug
- [ ] chore / refactor / docs

## Changements
<!-- Liste concise des modifications principales (par fichier/zone). -->

## Checklist documentation (obligatoire)
> Toute modification technique doit mettre à jour la documentation. Voir [CONTRIBUTING.md](../CONTRIBUTING.md).

- [ ] `CLAUDE.md` reste cohérent (architecture / endpoints / workers / DB / Docker / scripts / commandes).
- [ ] Nouveaux/​modifiés **endpoints** → `docs/API.md`.
- [ ] Nouvelles **tables / migrations** → `docs/DATABASE.md`.
- [ ] Nouveaux/​modifiés **workers** → `docs/WORKERS.md`.
- [ ] Changements **Docker / variables d'env** → `docs/DEPLOYMENT.md`.
- [ ] Changements **frontend** (panneaux / appels API) → `docs/FRONTEND.md`.
- [ ] Nouveaux **problèmes connus** → `docs/TROUBLESHOOTING.md`.
- [ ] Impact **performance / mémoire** → `docs/PERFORMANCE.md`.
- [ ] Note ajoutée dans `docs/CHANGELOG_TECH.md`.

## Tests
- [ ] `pytest -q` passe (offline).
- [ ] Vérifié localement (cockpit / API / workers selon le changement) — voir `docs/RUNBOOK.md`.

## Données réelles uniquement
- [ ] Aucune donnée mock/random/placeholder présentée comme réelle (sinon `unavailable`/`n/a` explicite).
- [ ] Aucune logique aléatoire ajoutée dans la chaîne décisionnelle.

## Risques / points de vigilance
<!-- Effets de bord, migrations, compat, perf, sécurité. -->

## Rollback
<!-- Comment annuler ce changement si besoin (revert, migration inverse…). -->
