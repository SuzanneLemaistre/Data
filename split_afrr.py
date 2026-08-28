"""
Migration ponctuelle : découpe data/afrr_marginal_price.csv (fichier unique,
devenu trop gros pour GitHub) en partitions journalières compressées
data/afrr_marginal_price/AAAA-MM-JJ.csv.gz

À lancer une seule fois, à la racine du dépôt :

    python split_afrr.py

Puis vérifier, supprimer l'ancien fichier et commiter :

    git rm --cached data/afrr_marginal_price.csv
    rm data/afrr_marginal_price.csv
    git add data/afrr_marginal_price/
    git commit -m "data: partitionnement journalier de la serie aFRR marginal price"

Le script travaille en flux (une journée en mémoire à la fois) : il tient
sans problème sur un fichier de plusieurs centaines de Mo.

Dépendances : bibliothèque standard Python uniquement (3.9+)
"""

import csv
import gzip
import shutil
import sys
import tempfile
from pathlib import Path

SOURCE = Path("data/afrr_marginal_price.csv")
DEST   = Path("data/afrr_marginal_price")

HEADERS = [
    "date_heure_debut",
    "prorata_mode", "picasso_connection",
    "upward_afrr_marginal_price", "downward_afrr_marginal_price",
    "pdemand", "afrr_for_france", "afrr_in_france",
]


def main():
    if not SOURCE.exists():
        print(f"Rien à faire : {SOURCE} n'existe pas (migration déjà effectuée ?).")
        return

    DEST.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="afrr_split_"))

    # Passe 1 — répartir les lignes brutes dans un fichier temporaire par date.
    print(f"Lecture de {SOURCE} ({SOURCE.stat().st_size / 1e6:.1f} Mo) …")
    handles: dict[str, object] = {}
    total = 0
    ignored = 0
    try:
        with open(SOURCE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            missing = [h for h in HEADERS if h not in (reader.fieldnames or [])]
            if missing:
                print(f"❌ Colonnes manquantes dans le fichier source : {missing}")
                sys.exit(1)
            for row in reader:
                day = (row.get("date_heure_debut") or "")[:10]
                if len(day) != 10 or day[4] != "-":
                    ignored += 1
                    continue
                h = handles.get(day)
                if h is None:
                    h = open(scratch / f"{day}.csv", "w", newline="", encoding="utf-8")
                    w = csv.DictWriter(h, fieldnames=HEADERS, extrasaction="ignore")
                    w.writeheader()
                    handles[day] = h
                csv.DictWriter(h, fieldnames=HEADERS, extrasaction="ignore").writerow(row)
                total += 1
                if total % 200_000 == 0:
                    print(f"  {total:,} lignes lues …")
    finally:
        for h in handles.values():
            h.close()

    print(f"  {total:,} lignes réparties sur {len(handles)} journées"
          + (f" ({ignored} lignes ignorées, horodatage illisible)" if ignored else ""))

    # Passe 2 — pour chaque journée : dédupliquer, trier, écrire en .csv.gz
    # (fusion avec une partition déjà présente le cas échéant).
    print(f"\nÉcriture des partitions dans {DEST}/ …")
    written = 0
    for day in sorted(handles):
        target = DEST / f"{day}.csv.gz"
        merged: dict[str, dict] = {}
        if target.exists():
            with gzip.open(target, "rt", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    merged[row["date_heure_debut"]] = row
        with open(scratch / f"{day}.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                merged.setdefault(row["date_heure_debut"], row)

        with gzip.open(target, "wt", newline="", encoding="utf-8", compresslevel=9) as f:
            w = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
            w.writeheader()
            for key in sorted(merged):
                w.writerow(merged[key])
        written += 1
        print(f"  {target.name}  {len(merged):>6} lignes  {target.stat().st_size / 1e6:.2f} Mo")

    shutil.rmtree(scratch, ignore_errors=True)

    biggest = max((p.stat().st_size for p in DEST.glob("*.csv.gz")), default=0)
    print(f"\n✅ {written} partitions écrites — la plus grosse fait "
          f"{biggest / 1e6:.2f} Mo (limite GitHub : 100 Mo).")
    print(f"\nÉtape suivante — supprimer l'ancien fichier monolithique :")
    print(f"    git rm --cached {SOURCE.as_posix()}")
    print(f"    rm {SOURCE.as_posix()}")
    print(f"    git add {DEST.as_posix()}/ .gitignore")
    print(f"    git commit -m \"data: partitionnement journalier de la serie aFRR marginal price\"")


if __name__ == "__main__":
    main()
