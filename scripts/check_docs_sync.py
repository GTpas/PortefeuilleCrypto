#!/usr/bin/env python3
"""Garde-fou « documentation obligatoire ».

Détecte les changements sur des chemins de code « importants » et vérifie qu'au
moins un fichier de documentation a aussi été modifié dans le même lot. Sinon,
échoue (ou avertit) avec un message indiquant quelle doc mettre à jour.

Stdlib uniquement, multiplateforme. Utilisé par :
  - le hook pre-commit  : `python scripts/check_docs_sync.py --staged`
  - la GitHub Action     : `python scripts/check_docs_sync.py --base origin/<base>`

Modes de sélection des fichiers changés :
  --staged           git diff --cached  (fichiers indexés — pre-commit)
  --base <ref>       git diff <ref>...HEAD  (depuis le merge-base — CI sur PR)
  (défaut)           git diff HEAD  (modifications du working tree)

Options :
  --warn             n'échoue pas (exit 0), affiche seulement un avertissement
  -h / --help        aide

Exit codes : 0 = OK (ou rien à vérifier, ou --warn) ; 1 = doc manquante ; 2 = erreur git.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

# Préfixes de chemins dont une modification DOIT s'accompagner d'une mise à jour de doc.
# (séparateurs '/' — on normalise les '\' Windows avant comparaison)
CODE_PATHS: tuple[str, ...] = (
    "api/",
    "workers/",
    "collectors/",
    "market/",
    "signal_engine/",
    "social/",
    "paper_execution/",
    "models/",
    "db/migrations/",
    "db/writer.py",
    "frontend/",
    "config.py",
    "metrics.py",
    "docker-compose.yml",
)

# Modifier l'un de ces chemins « compte » comme avoir documenté.
DOC_PATHS: tuple[str, ...] = (
    "docs/",
    "CLAUDE.md",
    "README.md",
    "CONTRIBUTING.md",
)

# Indices : quel code → quelle doc cibler en priorité (pour le message d'aide).
SUGGESTIONS: tuple[tuple[str, str], ...] = (
    ("api/", "docs/API.md"),
    ("db/migrations/", "docs/DATABASE.md"),
    ("db/writer.py", "docs/DATABASE.md"),
    ("workers/", "docs/WORKERS.md"),
    ("collectors/", "docs/WORKERS.md (chemin d'ingestion) / docs/ARCHITECTURE.md"),
    ("signal_engine/", "docs/ARCHITECTURE.md (chaîne décision) + CLAUDE.md"),
    ("paper_execution/", "docs/ARCHITECTURE.md + docs/DATABASE.md (paper_*)"),
    ("market/", "docs/ARCHITECTURE.md + docs/API.md (hubs/endpoints)"),
    ("social/", "docs/ARCHITECTURE.md (chemin social) + docs/DATABASE.md"),
    ("frontend/", "docs/FRONTEND.md"),
    ("config.py", "docs/DEPLOYMENT.md (variables d'env)"),
    ("docker-compose.yml", "docs/DEPLOYMENT.md"),
    ("metrics.py", "docs/PERFORMANCE.md + docs/WORKERS.md (métriques)"),
)


def _run_git(args: list[str]) -> list[str]:
    """Retourne la liste de fichiers ; lève en cas d'erreur git."""
    out = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def changed_files(staged: bool, base: str | None) -> list[str]:
    if staged:
        return _run_git(["diff", "--cached", "--name-only"])
    if base:
        # Trois points = diff depuis le merge-base (changements de la branche).
        return _run_git(["diff", "--name-only", f"{base}...HEAD"])
    return _run_git(["diff", "--name-only", "HEAD"])


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    p = _norm(path)
    for pre in prefixes:
        if pre.endswith("/"):
            if p.startswith(pre):
                return True
        elif p == pre:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vérifie la synchro doc <-> code.")
    parser.add_argument("--staged", action="store_true", help="fichiers indexés (pre-commit)")
    parser.add_argument("--base", metavar="REF", default=None, help="diff depuis <ref>...HEAD (CI)")
    parser.add_argument("--warn", action="store_true", help="avertir sans échouer (exit 0)")
    ns = parser.parse_args(argv)

    # Sortie robuste sous Windows (console cp1252) — évite UnicodeEncodeError
    # si un message contient un caractère hors page de code par défaut.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        files = changed_files(ns.staged, ns.base)
    except FileNotFoundError:
        print("check_docs_sync: git introuvable — vérification ignorée.")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"check_docs_sync: erreur git — vérification ignorée.\n{e.stderr}", file=sys.stderr)
        return 0 if ns.warn else 2

    code_changed = sorted({f for f in files if _matches(f, CODE_PATHS)})
    doc_changed = [f for f in files if _matches(f, DOC_PATHS)]

    if not code_changed:
        return 0  # rien de « important » → rien à exiger
    if doc_changed:
        return 0  # code ET doc touchés → OK

    # Code touché, aucune doc → message d'aide ciblé.
    print("=" * 72)
    print("  DOCUMENTATION MANQUANTE — du code a changé sans mise à jour de doc")
    print("=" * 72)
    print("\nFichiers de code modifiés :")
    for f in code_changed:
        print(f"  - {_norm(f)}")

    targets: list[str] = []
    for pre, doc in SUGGESTIONS:
        if any(_matches(f, (pre,)) for f in code_changed) and doc not in targets:
            targets.append(doc)
    print("\nMettre à jour au moins un fichier de doc (cibles suggérées) :")
    for t in targets:
        print(f"  -> {t}")
    print("  -> docs/CHANGELOG_TECH.md  (note pour tout changement significatif)")
    print("  -> CLAUDE.md               (si architecture / endpoints / workers / DB / Docker / scripts changent)")
    print("\nRègle : voir CONTRIBUTING.md (« Documentation obligatoire »).")
    print("Contourner ponctuellement : `git commit --no-verify` (déconseillé).")
    print("=" * 72)

    if ns.warn:
        print("(--warn) avertissement uniquement — commit autorisé.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
