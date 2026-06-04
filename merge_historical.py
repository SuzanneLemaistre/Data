"""
Fusion des données historiques SPOT dans spot_france.csv

Usage :
    python merge_historical.py historique.csv spot_france.csv

Le script :
  - lit le CSV historique (format EPEX, colonnes MTU en CET/CEST)
  - convertit les horodatages en UTC (ISO 8601)
  - fusionne dans spot_france.csv sans créer de doublons
  - trie le fichier final par date croissante

Dépendances : bibliothèque standard Python uniquement (3.9+)
"""

import csv
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

CET = ZoneInfo("Europe/Paris")  # gère automatiquement CET/CEST

INPUT_COLS = {
    "mtu":   "MTU (CET/CEST)",
    "price": "Day-ahead Price (EUR/MWh)",
}

OUTPUT_HEADERS = ["date_heure_debut", "date_heure_fin", "prix_eur_mwh", "volume_mw"]


def parse_mtu(mtu: str):
    """
    Parse '01/01/2026 00:00:00 - 01/01/2026 00:15:00' (CET/CEST)
    → (start_utc_iso, end_utc_iso)
    """
    parts = mtu.strip().split(" - ")
    if len(parts) != 2:
        return None, None

    fmt = "%d/%m/%Y %H:%M:%S"
    try:
        start_cet = datetime.strptime(parts[0].strip(), fmt).replace(tzinfo=CET)
        end_cet   = datetime.strptime(parts[1].strip(), fmt).replace(tzinfo=CET)
    except ValueError:
        return None, None

    # Convertir en UTC et formater
    start_utc = start_cet.astimezone(ZoneInfo("UTC"))
    end_utc   = end_cet.astimezone(ZoneInfo("UTC"))

    return (
        start_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        end_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    )


def load_historical(path: Path) -> list[dict]:
    # Essayer plusieurs encodages courants
    encoding_used = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            with open(path, newline="", encoding=encoding) as f:
                f.read()
            encoding_used = encoding
            break
        except UnicodeDecodeError:
            continue

    print(f"  Encodage détecté : {encoding_used}")

    # Détecter le séparateur (virgule ou point-virgule)
    with open(path, newline="", encoding=encoding_used) as f:
        sample = f.read(2048)
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    print(f"  Séparateur détecté : '{delimiter}'")

    # Le fichier est double-encodé : chaque ligne est enveloppée dans des
    # guillemets externes et les guillemets internes sont doublés ("" → ").
    # On désencapsule chaque ligne avant de parser.
    import io
    clean_lines = []
    with open(path, newline="", encoding=encoding_used) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('"') and line.endswith('"'):
                line = line[1:-1]          # supprimer les guillemets externes
            line = line.replace('""', '"') # désescaper les guillemets internes
            clean_lines.append(line)

    rows = []
    reader = csv.DictReader(io.StringIO("\n".join(clean_lines)), delimiter=delimiter)
    headers = reader.fieldnames or []
    print(f"  Headers : {headers}")

    # Trouver les colonnes MTU et prix de façon flexible
    mtu_col   = next((h for h in headers if "MTU" in h.upper()), None)
    price_col = next((h for h in headers if "DAY" in h.upper() and "PRICE" in h.upper()), None)

    if not mtu_col:
        print(f"  ❌ Colonne MTU introuvable.")
        return []
    if not price_col:
        print(f"  ❌ Colonne prix Day-ahead introuvable.")
        return []

    print(f"  Colonne MTU  : '{mtu_col}'")
    print(f"  Colonne Prix : '{price_col}'")

    for i, row in enumerate(reader, 1):
        mtu   = row.get(mtu_col, "").strip()
        price = row.get(price_col, "").strip()

        if not mtu or not price:
            continue

        start, end = parse_mtu(mtu)
        if not start:
            if i <= 3:
                print(f"  ⚠ Ligne {i} ignorée (MTU non parseable) : {mtu!r}")
            continue

        rows.append({
            "date_heure_debut": start,
            "date_heure_fin":   end,
            "prix_eur_mwh":     price,
            "volume_mw":        "",
        })

    return rows


def load_existing(path: Path) -> tuple[list[dict], set]:
    if not path.exists() or path.stat().st_size == 0:
        return [], set()
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    keys = {(r["date_heure_debut"], r["date_heure_fin"]) for r in rows}
    return rows, keys


def merge_and_save(existing: list[dict], new_rows: list[dict],
                   existing_keys: set, output: Path) -> int:
    added = 0
    for r in new_rows:
        key = (r["date_heure_debut"], r["date_heure_fin"])
        if key not in existing_keys:
            existing.append(r)
            existing_keys.add(key)
            added += 1

    # Tri par date de début
    existing.sort(key=lambda r: r["date_heure_debut"])

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(existing)

    return added


def main():
    if len(sys.argv) != 3:
        print("Usage : python merge_historical.py <historique.csv> <spot_france.csv>")
        sys.exit(1)

    hist_path   = Path(sys.argv[1])
    target_path = Path(sys.argv[2])

    if not hist_path.exists():
        print(f"Erreur : fichier introuvable → {hist_path}")
        sys.exit(1)

    print(f"Lecture de {hist_path} …")
    new_rows = load_historical(hist_path)
    print(f"  {len(new_rows)} lignes lues.")

    print(f"Chargement de {target_path} …")
    existing, existing_keys = load_existing(target_path)
    print(f"  {len(existing)} lignes existantes.")

    print("Fusion …")
    added = merge_and_save(existing, new_rows, existing_keys, target_path)

    print(f"\n✅ Terminé — {added} nouvelles lignes ajoutées ({len(new_rows) - added} doublons ignorés).")
    print(f"   Fichier final : {len(existing)} lignes dans {target_path}")


if __name__ == "__main__":
    main()
