#!/usr/bin/env python3
"""
Henter automatisk data fra Fantasy Premier League sitt offentlige API og
skriver ut data/live-data.json som nettsiden leser og viser på
"Live oppdatering"-siden. Kjøres av GitHub Actions (se
.github/workflows/sync-fpl.yml), men kan også kjøres manuelt lokalt:

    CLASSIC_LEAGUE_ID=123456 H2H_LEAGUE_ID=654321 python3 scripts/sync_fpl.py

Manedens Manager beregnes ved å summere hver managers rundepoeng (event
points) innenfor hver kalendermåned, basert på når rundens deadline var.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

API_BASE = "https://fantasy.premierleague.com/api"

# Disse to settes enten som miljøvariabler (i GitHub Actions-workflowen) eller
# direkte her som fallback. Numerisk liga-ID finner du i URL-en når du åpner
# ligaens "Standings"-side på fantasy.premierleague.com, f.eks.
# .../leagues/123456/standings/c -> ID-en er 123456.
CLASSIC_LEAGUE_ID = os.environ.get("CLASSIC_LEAGUE_ID", "1460798")
H2H_LEAGUE_ID = os.environ.get("H2H_LEAGUE_ID", "1461019")

MONTH_NAMES_NO = {
    1: "Januar", 2: "Februar", 3: "Mars", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}

# Sesongen regnes august -> mai
SEASON_MONTHS = [8, 9, 10, 11, 12, 1, 2, 3, 4, 5]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HviteGutterSync/1.0)"}


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (URLError, HTTPError) as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def get_event_month_map():
    """event_id -> (year, month) basert på deadline_time for runden."""
    data = fetch_json(f"{API_BASE}/bootstrap-static/")
    mapping = {}
    for ev in data.get("events", []):
        deadline = ev.get("deadline_time")
        if not deadline:
            continue
        dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        mapping[ev["id"]] = (dt.year, dt.month)
    return mapping


def get_classic_standings(league_id):
    data = fetch_json(f"{API_BASE}/leagues-classic/{league_id}/standings/")
    results = data.get("standings", {}).get("results", [])
    out = []
    for r in results:
        out.append({
            "rank": r.get("rank"),
            "manager": r.get("player_name"),
            "team": r.get("entry_name"),
            "entry": r.get("entry"),
            "points": r.get("total"),
        })
    return out


def get_h2h_standings(league_id):
    data = fetch_json(f"{API_BASE}/leagues-h2h/{league_id}/standings/")
    results = data.get("standings", {}).get("results", [])
    out = []
    for r in results:
        won = r.get("matches_won", 0)
        drawn = r.get("matches_drawn", 0)
        lost = r.get("matches_lost", 0)
        out.append({
            "rank": r.get("rank"),
            "manager": r.get("player_name"),
            "team": r.get("entry_name"),
            "entry": r.get("entry"),
            "record": f"{won}-{drawn}-{lost}",
            "points": r.get("total"),
        })
    return out


def get_monthly_manager(entries, event_month_map):
    """entries: liste av {entry, manager}. Returnerer liste med månedsvinner."""
    # entry_id -> manager navn
    id_to_name = {e["entry"]: e["manager"] for e in entries if e.get("entry")}

    # måned -> {entry_id: sum_points}
    month_totals = {}

    for entry_id in id_to_name:
        try:
            hist = fetch_json(f"{API_BASE}/entry/{entry_id}/history/")
        except Exception:
            continue
        for gw in hist.get("current", []):
            event_id = gw.get("event")
            pts = gw.get("points", 0)
            ym = event_month_map.get(event_id)
            if not ym:
                continue
            month_totals.setdefault(ym, {})
            month_totals[ym][entry_id] = month_totals[ym].get(entry_id, 0) + pts

    # Bygg resultatliste i sesong-rekkefølge (aug -> mai), kun for måneder vi har data for
    result = []
    seen_months = sorted(month_totals.keys())
    for ym in seen_months:
        totals = month_totals[ym]
        if not totals:
            continue
        winner_entry = max(totals, key=totals.get)
        result.append({
            "month": MONTH_NAMES_NO[ym[1]],
            "manager": id_to_name.get(winner_entry, "Ukjent"),
            "points": totals[winner_entry],
        })
    return result


def main():
    if CLASSIC_LEAGUE_ID.startswith("REPLACE_ME") or H2H_LEAGUE_ID.startswith("REPLACE_ME"):
        print("CLASSIC_LEAGUE_ID / H2H_LEAGUE_ID er ikke satt ennå — hopper over synk.", file=sys.stderr)
        # Skriv en tom/ventende fil så siden ikke feiler, men ikke overskriv ekte data.
        out_path = os.path.join(os.path.dirname(__file__), "..", "data", "live-data.json")
        if not os.path.exists(out_path):
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"updatedAt": None, "classic": [], "h2h": [], "monthlyManager": []}, f, ensure_ascii=False, indent=2)
        return

    classic = get_classic_standings(CLASSIC_LEAGUE_ID)
    h2h = get_h2h_standings(H2H_LEAGUE_ID)
    event_month_map = get_event_month_map()
    monthly = get_monthly_manager(classic, event_month_map)

    out = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "classic": classic,
        "h2h": h2h,
        "monthlyManager": monthly,
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "live-data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Skrev {out_path} — {len(classic)} i klassisk liga, {len(h2h)} i H2H, {len(monthly)} måneder.")


if __name__ == "__main__":
    main()
