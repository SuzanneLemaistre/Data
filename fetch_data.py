"""
Collecte quotidienne des prix et volumes spot France (EPEX / Nord Pool)
via l'API ouverte RTE : https://data.rte-france.com

Dépendances : pip install requests
"""

import os
import csv
import json
import requests
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

CLIENT_ID     = os.environ["RTE_CLIENT_ID"]
CLIENT_SECRET = os.environ["RTE_CLIENT_SECRET"]

OUTPUT_FILE   = Path("data/spot_france.csv")

TOKEN_URL     = "https://digital.iservices.rte-france.com/token/oauth/"
API_URL       = "https://digital.iservices.rte-france.com/open_api/wholesale_market/v2/france_power_exchanges"

CSV_HEADERS = [
    "date_heure_debut",
    "date_heure_fin",
    "valeur_eur_mwh",
    "unite",
]

# ── Authentification OAuth2 ───────────────────────────────────────────────────

def get_token() -> str:
    resp = requests.post(TOKEN_URL, auth=(CLIENT_ID, CLIENT_SECRET))
    resp.raise_for_status()
    return resp.json()["access_token"]

# ── Récupération des données ──────────────────────────────────────────────────

def fetch_prices(token: str, start: date, end: date) -> list[dict]:
    """Retourne les enregistrements de prix pour la période [start, end[."""
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt   = datetime(end.year,   end.month,   end.day,   tzinfo=timezone.utc)

    params = {
        "start_date": start_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "end_date":   end_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(API_URL, params=params, headers=headers)
    resp.raise_for_status()

    data = resp.json()
    records = []

    for exchange in data.get("france_power_exchanges", []):
        for value in exchange.get("values", []):
            records.append({
                "date_heure_debut": value.get("start_date", ""),
                "date_heure_fin":   value.get("end_date", ""),
                "valeur_eur_mwh":   value.get("value", ""),
                "unite":            exchange.get("unit", "EUR/MWh"),
            })

    return records

# ── Écriture CSV ──────────────────────────────────────────────────────────────

def append_to_csv(records: list[dict]) -> int:
    """Ajoute les nouvelles lignes au CSV (sans doublons). Retourne le nb de lignes ajoutées."""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Charger les horodatages déjà présents pour éviter les doublons
    existing = set()
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing.add((row["date_heure_debut"], row["date_heure_fin"]))

    new_records = [
        r for r in records
        if (r["date_heure_debut"], r["date_heure_fin"]) not in existing
    ]

    write_header = not OUTPUT_FILE.exists() or OUTPUT_FILE.stat().st_size == 0
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerows(new_records)

    return len(new_records)

# ── Point d'entrée ────────────────────────────────────────────────────────────

def main():
    today     = date.today()
    yesterday = today - timedelta(days=1)

    print(f"[{datetime.now().isoformat()}] Collecte du {yesterday} …")

    token   = get_token()
    records = fetch_prices(token, start=yesterday, end=today)

    if not records:
        print("Aucune donnée retournée par l'API pour cette période.")
        return

    added = append_to_csv(records)
    print(f"  → {added} nouvelles lignes ajoutées ({len(records) - added} doublons ignorés).")
    print(f"  → Fichier : {OUTPUT_FILE.resolve()}")

if __name__ == "__main__":
    main()
