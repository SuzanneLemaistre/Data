"""
Collecte quotidienne de données marché via l'API ouverte RTE.
APIs couvertes :
  - Wholesale Market v3          → data/spot_france.csv
  - Balancing Energy v5          → data/imbalance_data.csv
                                   data/prices.csv
                                   data/standard_rr_data.csv
                                   data/standard_afrr_data.csv
                                   data/afrr_marginal_price/AAAA-MM-JJ.csv.gz
                                   data/standard_mfrr_data.csv
  - Balancing Imbalances Account → data/coefficient_k.csv  (mensuel)

La série aFRR marginal price est au pas de 4 s (~21 600 lignes / 1,7 Mo par
jour). Elle est donc stockée en partitions journalières compressées, et non
dans un fichier unique : un fichier unique dépasse la limite de 100 Mo de
GitHub au bout de deux mois et bloque le push.

Variable d'environnement optionnelle :
  DAYS_BACK   nombre de jours à recollecter (défaut 7). À augmenter pour
              rattraper un trou de données (ex : DAYS_BACK=40).

Dépendances : pip install requests
"""

import os
import csv
import gzip
import requests
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

PARIS = ZoneInfo("Europe/Paris")

# ── Credentials (une seule application RTE) ───────────────────────────────────

CLIENT_ID     = os.environ["RTE_CLIENT_ID"]
CLIENT_SECRET = os.environ["RTE_CLIENT_SECRET"]

TOKEN_URL = "https://digital.iservices.rte-france.com/token/oauth/"

# ── URLs des APIs ─────────────────────────────────────────────────────────────

WM_BASE  = "https://digital.iservices.rte-france.com/open_api/wholesale_market/v3"
BE_BASE  = "https://digital.iservices.rte-france.com/open_api/balancing_energy/v5"
BIA_BASE = "https://digital.iservices.rte-france.com/open_api/balancing_imbalances_account/v1"

# Fenêtre maximale acceptée par requête (les APIs RTE refusent les plages
# trop larges) : on découpe la période demandée en tranches de cette taille.
MAX_WINDOW_DAYS = 7

# ── Authentification ──────────────────────────────────────────────────────────

def get_token() -> str:
    last_err = None
    for attempt in range(4):
        try:
            resp = requests.post(TOKEN_URL, auth=(CLIENT_ID, CLIENT_SECRET), timeout=30)
            resp.raise_for_status()
            return resp.json()["access_token"]
        except Exception as e:
            last_err = e
            wait = 2 ** attempt * 10  # 10s, 20s, 40s, 80s
            print(f"  [Auth] tentative {attempt+1}/4 echouee ({e}), retry dans {wait}s")
            import time; time.sleep(wait)
    raise last_err

def get_with_retry(url, headers, params, retries=3):
    """GET avec retries sur les erreurs 5xx (pannes transitoires RTE)."""
    import time
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} Server Error", response=resp)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 15)  # 15s, 30s
    raise last_err

def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_dt(d: date) -> str:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

def by_windows(start: date, end: date, max_days: int = MAX_WINDOW_DAYS):
    """Découpe [start, end) en tranches de max_days jours au maximum."""
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=max_days), end)
        yield cursor, stop
        cursor = stop

def fetch_windowed(fetch_one, token, start, end, max_days=MAX_WINDOW_DAYS):
    """Appelle fetch_one(token, a, b) sur chaque tranche et concatène."""
    rows = []
    for a, b in by_windows(start, end, max_days):
        rows.extend(fetch_one(token, a, b))
    return rows

def append_csv(path: Path, headers: list[str], rows: list[dict], dedup_keys: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if path.exists() and path.stat().st_size > 0:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add(tuple(row.get(k, "") for k in dedup_keys))

    new_rows = []
    seen = existing
    for r in rows:
        key = tuple(r.get(k, "") for k in dedup_keys)
        if key not in seen:
            seen.add(key)
            new_rows.append(r)
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(new_rows)
    return len(new_rows)

# ── 1. Wholesale Market — prix et volumes spot ────────────────────────────────

def fetch_wholesale(token, start, end):
    url = f"{WM_BASE}/france_power_exchanges"
    resp = get_with_retry(url, auth_headers(token),
                          {"start_date": fmt_dt(start), "end_date": fmt_dt(end)})
    rows = []
    for block in resp.json().get("france_power_exchanges", []):
        for v in block.get("values", []):
            rows.append({
                "date_heure_debut": v.get("start_date", ""),
                "date_heure_fin":   v.get("end_date", ""),
                "prix_eur_mwh":     v.get("price", ""),
                "volume_mw":        v.get("value", ""),
            })
    return rows

HEADERS_WM = ["date_heure_debut", "date_heure_fin", "prix_eur_mwh", "volume_mw"]

# ── 2. Balancing Energy — imbalance_data ─────────────────────────────────────

def fetch_imbalance(token, start, end):
    url = f"{BE_BASE}/imbalance_data"
    resp = get_with_retry(url, auth_headers(token),
                          {"start_date": fmt_dt(start), "end_date": fmt_dt(end)})
    rows = []
    for block in resp.json().get("imbalance_data", []):
        for v in block.get("values", []):
            rows.append({
                "date_heure_debut":                    v.get("start_date", ""),
                "date_heure_fin":                      v.get("end_date", ""),
                "imbalance_mw":                        v.get("imbalance", ""),
                "system_trend":                        v.get("system_trend", ""),
                "positive_imbalance_settlement_price": v.get("positive_imbalance_settlement_price", ""),
                "negative_imbalance_settlement_price": v.get("negative_imbalance_settlement_price", ""),
            })
    return rows

HEADERS_IMBALANCE = [
    "date_heure_debut", "date_heure_fin",
    "imbalance_mw", "system_trend",
    "positive_imbalance_settlement_price", "negative_imbalance_settlement_price",
]

# ── 3. Balancing Energy — prices ──────────────────────────────────────────────

def fetch_prices(token, start, end):
    url = f"{BE_BASE}/prices"
    resp = get_with_retry(url, auth_headers(token),
                          {"start_date": fmt_dt(start), "end_date": fmt_dt(end)})
    rows = []
    for block in resp.json().get("prices", []):
        for v in block.get("values", []):
            rows.append({
                "date_heure_debut":                          v.get("start_date", ""),
                "date_heure_fin":                            v.get("end_date", ""),
                "upward_weighted_average_price":             v.get("upward_weighted_average_price", ""),
                "downward_weighted_average_price":           v.get("downward_weighted_average_price", ""),
                "upward_marginal_price":                     v.get("upward_marginal_price", ""),
                "downward_marginal_price":                   v.get("downward_marginal_price", ""),
                "terre_clearing_price":                      v.get("terre_clearing_price", ""),
                "upward_weighted_average_price_afrr_fr":     v.get("upward_weighted_average_price_afrr_activated_for_fr", ""),
                "downward_weighted_average_price_afrr_fr":   v.get("downward_weighted_average_price_afrr_activated_for_fr", ""),
            })
    return rows

HEADERS_PRICES = [
    "date_heure_debut", "date_heure_fin",
    "upward_weighted_average_price", "downward_weighted_average_price",
    "upward_marginal_price", "downward_marginal_price",
    "terre_clearing_price",
    "upward_weighted_average_price_afrr_fr", "downward_weighted_average_price_afrr_fr",
]

# ── 4. Balancing Energy — TERRE (standard_rr_data) ───────────────────────────

def fetch_terre(token, start, end):
    url = f"{BE_BASE}/standard_rr_data"
    resp = get_with_retry(url, auth_headers(token),
                          {"start_date": fmt_dt(start), "end_date": fmt_dt(end)})
    rows = []
    for v in resp.json().get("terre", {}).get("terre_mesures", []):
        rows.append({
            "date_heure_debut":        v.get("start_date", ""),
            "direction":               v.get("direction", ""),
            "total_requested_volume":  v.get("total_requested_volume", ""),
            "total_satisfied_need":    v.get("total_satisfied_need", ""),
            "activated_volume":        v.get("activated_volume", ""),
            "activated_volume_rsoint": v.get("activated_volume_rsoint", ""),
            "clearing_price":          v.get("clearing_price", ""),
        })
    return rows

HEADERS_TERRE = [
    "date_heure_debut", "direction",
    "total_requested_volume", "total_satisfied_need",
    "activated_volume", "activated_volume_rsoint", "clearing_price",
]

# ── 5. Balancing Energy — PICASSO (standard_afrr_data) ───────────────────────

def fetch_picasso(token, start, end):
    url = f"{BE_BASE}/standard_afrr_data"
    resp = get_with_retry(url, auth_headers(token),
                          {"start_date": fmt_dt(start), "end_date": fmt_dt(end)})
    rows = []
    for v in resp.json().get("picasso", {}).get("picasso_mesures", []):
        rows.append({
            "date_heure_debut":                      v.get("start_date", ""),
            "upward_afrr_requested_need":            v.get("upward_afrr_requested_need", ""),
            "downward_afrr_requested_need":          v.get("downward_afrr_requested_need", ""),
            "upward_afrr_activated_volume_for_fr":   v.get("upward_afrr_activated_volume_for_fr", ""),
            "downward_afrr_activated_volume_for_fr": v.get("downward_afrr_activated_volume_for_fr", ""),
            "upward_afrr_activated_volume_in_fr":    v.get("upward_afrr_activated_volume_in_fr", ""),
            "downward_afrr_activated_volume_in_fr":  v.get("downward_afrr_activated_volume_in_fr", ""),
        })
    return rows

HEADERS_PICASSO = [
    "date_heure_debut",
    "upward_afrr_requested_need", "downward_afrr_requested_need",
    "upward_afrr_activated_volume_for_fr", "downward_afrr_activated_volume_for_fr",
    "upward_afrr_activated_volume_in_fr", "downward_afrr_activated_volume_in_fr",
]

# ── 6. Balancing Energy — aFRR marginal price (partitions journalières) ──────

AFRR_DIR = Path("data/afrr_marginal_price")

def afrr_step_iso(day_str: str, step: str) -> str:
    """L'API renvoie le jour ('2026-06-11') et le pas en heure locale Paris
    ('01:59:56') séparément. On reconstruit un horodatage ISO avec offset
    (+01:00/+02:00) cohérent avec les autres séries."""
    try:
        dt = datetime.strptime(f"{day_str} {step}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=PARIS)
        return dt.isoformat()
    except ValueError:
        return f"{day_str}T{step}"

def fetch_afrr_price(token, start, end):
    """L'API afrr_marginal_price (donnees aux 4 s) n'accepte que des fenetres
    courtes : on boucle jour par jour."""
    return fetch_windowed(_fetch_afrr_price_day, token, start, end, max_days=1)

def _fetch_afrr_price_day(token, start, end):
    url = f"{BE_BASE}/afrr_marginal_price"
    resp = get_with_retry(url, auth_headers(token),
                          {"start_date": fmt_dt(start), "end_date": fmt_dt(end)})
    rows = []
    for day in resp.json().get("days", []):
        for v in day.get("datas", []):
            rows.append({
                "date_heure_debut":            afrr_step_iso(day.get("start_date", ""), v.get("step", "")),
                "prorata_mode":                v.get("prorata_mode", ""),
                "picasso_connection":          v.get("picasso_connection", ""),
                "upward_afrr_marginal_price":  v.get("upward_afrr_marginal_price", ""),
                "downward_afrr_marginal_price": v.get("downward_afrr_marginal_price", ""),
                "pdemand":                     v.get("pdemand", ""),
                "afrr_for_france":             v.get("afrr_for_france", ""),
                "afrr_in_france":              v.get("afrr_in_france", ""),
            })
    return rows

HEADERS_AFRR_PRICE = [
    "date_heure_debut",
    "prorata_mode", "picasso_connection",
    "upward_afrr_marginal_price", "downward_afrr_marginal_price",
    "pdemand", "afrr_for_france", "afrr_in_france",
]

def afrr_partition_path(day: str) -> Path:
    return AFRR_DIR / f"{day}.csv.gz"

def write_afrr_partitions(rows: list[dict]) -> int:
    """Range les lignes aFRR dans data/afrr_marginal_price/AAAA-MM-JJ.csv.gz,
    une partition par date locale. Chaque partition est fusionnée avec son
    contenu existant (dédup sur l'horodatage) puis réécrite triée.

    Les fenêtres de collecte étant calées sur minuit UTC, une requête couvre
    deux dates locales : la fusion garantit qu'une journée incomplète est
    complétée par la collecte suivante.
    """
    AFRR_DIR.mkdir(parents=True, exist_ok=True)

    by_day: dict[str, list[dict]] = {}
    for r in rows:
        day = r["date_heure_debut"][:10]
        if len(day) == 10:
            by_day.setdefault(day, []).append(r)

    added_total = 0
    for day, day_rows in sorted(by_day.items()):
        path = afrr_partition_path(day)
        merged: dict[str, dict] = {}
        if path.exists():
            with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    merged[row["date_heure_debut"]] = row
        before = len(merged)
        for r in day_rows:
            merged.setdefault(r["date_heure_debut"], r)
        added = len(merged) - before
        if added == 0 and before > 0:
            continue

        tmp = path.with_suffix(".gz.tmp")
        with gzip.open(tmp, "wt", newline="", encoding="utf-8", compresslevel=9) as f:
            w = csv.DictWriter(f, fieldnames=HEADERS_AFRR_PRICE, extrasaction="ignore")
            w.writeheader()
            for key in sorted(merged):
                w.writerow(merged[key])
        tmp.replace(path)
        added_total += added
        print(f"      {path.name} : {added} nouvelles lignes ({len(merged)} au total)")
    return added_total

# ── 7. Balancing Energy — MARI/mFRR (standard_mfrr_data) ─────────────────────

def fetch_mfrr(token, start, end):
    url = f"{BE_BASE}/standard_mfrr_data"
    resp = get_with_retry(url, auth_headers(token),
                          {"start_date": fmt_dt(start), "end_date": fmt_dt(end)})
    rows = []
    for v in resp.json().get("mari", {}).get("mari_mesures", []):
            rows.append({
                "date_heure_debut":              v.get("start_date", ""),
                "total_requested_volume_SA":     v.get("total_requested_volume_SA", ""),
                "total_requested_volume_DA":     v.get("total_requested_volume_DA", ""),
                "total_activated_volume_SA":     v.get("total_activated_volume_SA", ""),
                "total_activated_volume_DA":     v.get("total_activated_volume_DA", ""),
                "settlement_price_previous":     v.get("settlement_price_previous", ""),
                "settlement_price_current":      v.get("settlement_price_current", ""),
                "total_volume_submitted_offers": v.get("total_volume_submitted_offers", ""),
                "total_volume_filtered_offers":  v.get("total_volume_filtered_offers", ""),
            })
    return rows

HEADERS_MFRR = [
    "date_heure_debut",
    "total_requested_volume_SA", "total_requested_volume_DA",
    "total_activated_volume_SA", "total_activated_volume_DA",
    "settlement_price_previous", "settlement_price_current",
    "total_volume_submitted_offers", "total_volume_filtered_offers",
]

# ── 8. Balancing Imbalances Account — coefficient K (mensuel) ────────────────

def fetch_coefficient_k(token, month: str) -> list[dict]:
    """month : format YYYY-MM"""
    url = f"{BIA_BASE}/coefficient_k"
    resp = get_with_retry(url, auth_headers(token), {"application_month": month})
    k = resp.json().get("coefficient_k", "")
    return [{"mois": month, "coefficient_k": k}]

HEADERS_K = ["mois", "coefficient_k"]

# ── Orchestration ─────────────────────────────────────────────────────────────

def run(label: str, headers: list[str], csv_path: Path,
        dedup_keys: list[str], rows: list[dict]) -> None:
    if not rows:
        print(f"  [{label}] Aucune donnée retournée.")
        return
    added = append_csv(csv_path, headers, rows, dedup_keys)
    print(f"  [{label}] {added} nouvelles lignes -> {csv_path}")

def days_back() -> int:
    """Profondeur de recollecte. Fenêtre glissante de 7 jours par défaut :
    rattrape automatiquement les jours manqués si un run a échoué (la
    déduplication rend l'opération idempotente). Augmenter DAYS_BACK pour
    combler un trou plus ancien."""
    try:
        return max(1, int(os.environ.get("DAYS_BACK", "7")))
    except ValueError:
        return 7

def main():
    today = date.today()
    start = today - timedelta(days=days_back())

    print(f"\n[{datetime.now().isoformat()}] Collecte du {start} au {today} …\n")
    token = get_token()

    # 1. Wholesale Market
    try:
        rows = fetch_windowed(fetch_wholesale, token, start, today)
        run("Wholesale Market", HEADERS_WM,
            Path("data/spot_france.csv"), ["date_heure_debut", "date_heure_fin"], rows)
    except Exception as e:
        print(f"  [Wholesale Market] ERREUR : {e}")

    # 2. Imbalance data
    try:
        rows = fetch_windowed(fetch_imbalance, token, start, today)
        run("Imbalance Data", HEADERS_IMBALANCE,
            Path("data/imbalance_data.csv"), ["date_heure_debut", "date_heure_fin"], rows)
    except Exception as e:
        print(f"  [Imbalance Data] ERREUR : {e}")

    # 3. Balancing prices
    try:
        rows = fetch_windowed(fetch_prices, token, start, today)
        run("Balancing Prices", HEADERS_PRICES,
            Path("data/prices.csv"), ["date_heure_debut", "date_heure_fin"], rows)
    except Exception as e:
        print(f"  [Balancing Prices] ERREUR : {e}")

    # 4. TERRE
    try:
        rows = fetch_windowed(fetch_terre, token, start, today)
        run("TERRE (RR)", HEADERS_TERRE,
            Path("data/standard_rr_data.csv"), ["date_heure_debut", "direction"], rows)
    except Exception as e:
        print(f"  [TERRE] ERREUR : {e}")

    # 5. PICASSO (aFRR)
    try:
        rows = fetch_windowed(fetch_picasso, token, start, today)
        run("PICASSO (aFRR)", HEADERS_PICASSO,
            Path("data/standard_afrr_data.csv"), ["date_heure_debut"], rows)
    except Exception as e:
        print(f"  [PICASSO] ERREUR : {e}")

    # 6. aFRR marginal price (partitions journalières compressées)
    try:
        rows = fetch_afrr_price(token, start, today)
        if rows:
            added = write_afrr_partitions(rows)
            print(f"  [aFRR Marginal Price] {added} nouvelles lignes -> {AFRR_DIR}/")
        else:
            print("  [aFRR Marginal Price] Aucune donnée retournée.")
    except Exception as e:
        print(f"  [aFRR Marginal Price] ERREUR : {e}")

    # 7. MARI / mFRR
    try:
        rows = fetch_windowed(fetch_mfrr, token, start, today)
        run("MARI (mFRR)", HEADERS_MFRR,
            Path("data/standard_mfrr_data.csv"), ["date_heure_debut"], rows)
    except Exception as e:
        print(f"  [MARI/mFRR] ERREUR : {e}")

    # 8. Coefficient K (mensuel — collecté le 1er du mois)
    if today.day == 1:
        prev_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        try:
            rows = fetch_coefficient_k(token, prev_month)
            run("Coefficient K", HEADERS_K,
                Path("data/coefficient_k.csv"), ["mois"], rows)
        except Exception as e:
            print(f"  [Coefficient K] ERREUR : {e}")
    else:
        print(f"  [Coefficient K] Pas de collecte (lancé le 1er du mois uniquement).")

    print("\nTerminé.")

if __name__ == "__main__":
    main()
