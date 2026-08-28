"""
Chargement de la série aFRR marginal price, désormais stockée en partitions
journalières compressées (data/afrr_marginal_price/AAAA-MM-JJ.csv.gz).

Exemples :

    from load_afrr import load_afrr

    df = load_afrr("2026-08-01", "2026-08-27")   # une plage
    df = load_afrr()                              # tout l'historique (lourd)

En ligne de commande, pour un aperçu :

    python load_afrr.py 2026-08-01 2026-08-27

Dépendances : pip install pandas
"""

import sys
from pathlib import Path

import pandas as pd

AFRR_DIR = Path(__file__).resolve().parent / "data" / "afrr_marginal_price"

# Les prix indisponibles sont renvoyés par RTE sous la forme du littéral
# "Invalid" : on les convertit en NaN plutôt qu'en colonne texte.
NUMERIC_COLS = [
    "upward_afrr_marginal_price", "downward_afrr_marginal_price",
    "pdemand", "afrr_for_france", "afrr_in_france",
]


def partitions(start: str | None = None, end: str | None = None) -> list[Path]:
    """Partitions dont la date est dans [start, end], bornes incluses."""
    files = sorted(AFRR_DIR.glob("*.csv.gz"))
    if start:
        files = [p for p in files if p.name[:10] >= start]
    if end:
        files = [p for p in files if p.name[:10] <= end]
    return files


def load_afrr(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    files = partitions(start, end)
    if not files:
        raise FileNotFoundError(
            f"Aucune partition trouvée dans {AFRR_DIR} pour la plage {start} → {end}."
        )

    df = pd.concat((pd.read_csv(p) for p in files), ignore_index=True)

    df["date_heure_debut"] = pd.to_datetime(df["date_heure_debut"], utc=True, format="mixed")
    df["date_heure_debut"] = df["date_heure_debut"].dt.tz_convert("Europe/Paris")
    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # Le champ booléen a changé de casse côté RTE ("False" puis "false").
    for col in ("prorata_mode", "picasso_connection"):
        df[col] = df[col].astype(str).str.lower().map({"true": True, "false": False})

    return df.sort_values("date_heure_debut").reset_index(drop=True)


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else None
    end   = sys.argv[2] if len(sys.argv) > 2 else None
    df = load_afrr(start, end)
    print(f"{len(df):,} lignes  |  {df['date_heure_debut'].min()} → {df['date_heure_debut'].max()}")
    print(df.head())
    print(df[NUMERIC_COLS].describe())
