#!/usr/bin/env python3
"""
Henter automatisk data fra Fantasy Premier League sitt offentlige API og
skriver ut data/live-data.json som nettsiden leser og viser på
"Live oppdatering"-siden. Kjøres av GitHub Actions (se
.github/workflows/sync-fpl.yml), men kan også kjøres manuelt lokalt:

    CLASSIC_LEAGUE_ID=123456 H2H_LEAGUE_ID=654321 python3 scripts/sync_fpl.py

Manedens Manager beregnes ved å summere hver managers rundepoeng (event
points) innenfor hver kalendermåned, basert på når rundens deadline var.

I tillegg henter skriptet, for hver deltaker, kaptein og eventuelt brukt chip
i inneværende runde, deltakerens beste spiller poengmessig den runden, og
hvor mange chips av hver type deltakeren har igjen for resten av sesongen.
"""

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

API_BASE = "https://fantasy.premierleague.com/api"

# Disse to settes enten som miljøvariabler (i GitHub Actions-workflowen) eller
# direkte her som fallback. Numerisk liga-ID finner du i URL-en når du åpner
# ligaens "Standings"-side på fantasy.premierleague.com, f.eks.
# .../leagues/123456/standings/c -> ID-en er 123456.
# Merk: bruker "or" i stedet for os.environ.get(..., default) fordi GitHub
# Actions setter miljøvariabelen til en TOM STRENG (ikke usatt) når
# ${{ vars.X }} ikke finnes i repoet — da hopper .get()-default over, og en
# tom liga-ID sendt til FPL sitt API gir en feil som stopper hele synken.
CLASSIC_LEAGUE_ID = os.environ.get("CLASSIC_LEAGUE_ID") or "1460798"
H2H_LEAGUE_ID = os.environ.get("H2H_LEAGUE_ID") or "1461019"

MONTH_NAMES_NO = {
    1: "Januar", 2: "Februar", 3: "Mars", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}

# Sesongen regnes august -> mai
SEASON_MONTHS = [8, 9, 10, 11, 12, 1, 2, 3, 4, 5]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HviteGutterSync/1.0)"}

# Antall av hver chip-type en manager har totalt i løpet av sesongen. FPL har
# siden 2023/24-sesongen gitt to av hver (én per sesonghalvdel). Justér disse
# tallene her hvis Premier League endrer reglene for en senere sesong.
CHIP_ALLOWANCE = {"wildcard": 2, "freehit": 2, "bboost": 2, "3xc": 2}
CHIP_LABELS = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Trippelkaptein",
}


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


def get_bootstrap():
    """Henter bootstrap-static én gang og returnerer rådataene, slik at vi
    slipper å hente den samme store filen flere ganger."""
    return fetch_json(f"{API_BASE}/bootstrap-static/")


def get_event_month_map(bootstrap):
    """event_id -> (year, month) basert på deadline_time for runden, samt
    event_id -> om runden er ferdigspilt (finished), slik at vi kan skille
    mellom avsluttede måneder og måneden som pågår akkurat nå."""
    mapping = {}
    finished = {}
    for ev in bootstrap.get("events", []):
        deadline = ev.get("deadline_time")
        if not deadline:
            continue
        dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        mapping[ev["id"]] = (dt.year, dt.month)
        finished[ev["id"]] = bool(ev.get("finished"))
    return mapping, finished


def get_current_event_id(bootstrap):
    """Finner runden som pågår akkurat nå (deadline passert, ikke ferdigspilt).
    Faller tilbake til neste kommende runde hvis ingen runde pågår akkurat nå
    (f.eks. midt i en gameweek-pause), og til None før sesongstart."""
    for ev in bootstrap.get("events", []):
        if ev.get("is_current"):
            return ev["id"]
    for ev in bootstrap.get("events", []):
        if ev.get("is_next"):
            return ev["id"]
    return None


def get_player_names(bootstrap):
    """element (spiller-id) -> visningsnavn, f.eks. "Haaland"."""
    return {p["id"]: p.get("web_name", "Ukjent") for p in bootstrap.get("elements", [])}


def get_live_points_map(event_id):
    """element (spiller-id) -> rå poeng i den gitte runden (ikke multiplisert
    med kapteinsbindet), hentet fra rundens "live"-endepunkt."""
    if not event_id:
        return {}
    try:
        data = fetch_json(f"{API_BASE}/event/{event_id}/live/")
    except Exception:
        return {}
    out = {}
    for el in data.get("elements", []):
        out[el["id"]] = el.get("stats", {}).get("total_points", 0)
    return out


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


def get_entry_history(entry_id):
    """Henter /entry/{id}/history/ – brukes både til Månedens Manager og til
    å regne ut hvilke chips en deltaker har brukt så langt i sesongen."""
    try:
        return fetch_json(f"{API_BASE}/entry/{entry_id}/history/")
    except Exception:
        return None


def get_monthly_manager(entries, histories, event_month_map, event_finished_map):
    """entries: liste av {entry, manager}. histories: entry_id -> historikk
    (fra get_entry_history). Returnerer liste med månedsvinner. Hver rad har
    også "inProgress": True hvis ikke alle rundene i den måneden er
    ferdigspilt ennå (dvs. det er en foreløpig ledelse, ikke en endelig vinner)."""
    id_to_name = {e["entry"]: e["manager"] for e in entries if e.get("entry")}

    month_totals = {}
    month_finished = {}

    for entry_id, hist in histories.items():
        if not hist:
            continue
        for gw in hist.get("current", []):
            event_id = gw.get("event")
            pts = gw.get("points", 0)
            ym = event_month_map.get(event_id)
            if not ym:
                continue
            month_totals.setdefault(ym, {})
            month_totals[ym][entry_id] = month_totals[ym].get(entry_id, 0) + pts
            month_finished.setdefault(ym, True)
            if not event_finished_map.get(event_id, False):
                month_finished[ym] = False

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
            "inProgress": not month_finished.get(ym, True),
        })
    return result


def get_chips_left(hist):
    """Regner ut hvor mange av hver chip-type en deltaker har igjen, basert
    på hvilke chips som allerede er brukt i historikken."""
    used = Counter()
    if hist:
        for c in hist.get("chips", []):
            name = c.get("name")
            if name in CHIP_ALLOWANCE:
                used[name] += 1
    remaining = []
    for key in ("wildcard", "freehit", "bboost", "3xc"):
        left = CHIP_ALLOWANCE[key] - used.get(key, 0)
        remaining.append({"chip": CHIP_LABELS[key], "left": max(left, 0)})
    return remaining


def format_chips_left(chips_left):
    parts = [f"{c['chip']} ({c['left']})" for c in chips_left if c["left"] > 0]
    return ", ".join(parts) if parts else "Ingen chips igjen"


def get_round_details(entries, histories, event_id, player_names, live_points_map):
    """entries: liste av {entry, manager}. Returnerer entry_id -> detaljer for
    inneværende runde: kaptein, toppscorer i eget lag, evt. brukt chip, og
    hvilke chips deltakeren har igjen."""
    details = {}
    for e in entries:
        entry_id = e.get("entry")
        if not entry_id:
            continue

        chips_left = get_chips_left(histories.get(entry_id))

        captain = None
        top_scorer = None
        top_scorer_points = None
        chip_used = None

        if event_id:
            try:
                picks_data = fetch_json(f"{API_BASE}/entry/{entry_id}/event/{event_id}/picks/")
            except Exception:
                picks_data = None

            if picks_data:
                active_chip = picks_data.get("active_chip")
                chip_used = CHIP_LABELS.get(active_chip) if active_chip else None

                picks = picks_data.get("picks", [])
                captain_pick = next((p for p in picks if p.get("is_captain")), None)
                if captain_pick:
                    captain = player_names.get(captain_pick.get("element"), "Ukjent")

                # "Beste spiller" = spilleren i laget (i spill, dvs. multiplier > 0)
                # med flest rå poeng i runden, uavhengig av kapteinsbindet.
                starters = [p for p in picks if (p.get("multiplier") or 0) > 0]
                if starters:
                    best = max(starters, key=lambda p: live_points_map.get(p["element"], 0))
                    top_scorer = player_names.get(best["element"], "Ukjent")
                    top_scorer_points = live_points_map.get(best["element"], 0)

        details[entry_id] = {
            "captain": captain,
            "topScorer": top_scorer,
            "topScorerPoints": top_scorer_points,
            "chipUsed": chip_used,
            "chipsLeft": format_chips_left(chips_left),
        }
    return details


def main():
    if CLASSIC_LEAGUE_ID.startswith("REPLACE_ME") or H2H_LEAGUE_ID.startswith("REPLACE_ME"):
        print("CLASSIC_LEAGUE_ID / H2H_LEAGUE_ID er ikke satt ennå — hopper over synk.", file=sys.stderr)
        out_path = os.path.join(os.path.dirname(__file__), "..", "data", "live-data.json")
        if not os.path.exists(out_path):
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"updatedAt": None, "classic": [], "h2h": [], "monthlyManager": [], "currentEvent": None}, f, ensure_ascii=False, indent=2)
        return

    bootstrap = get_bootstrap()
    classic = get_classic_standings(CLASSIC_LEAGUE_ID)
    h2h = get_h2h_standings(H2H_LEAGUE_ID)
    event_month_map, event_finished_map = get_event_month_map(bootstrap)
    current_event_id = get_current_event_id(bootstrap)
    player_names = get_player_names(bootstrap)
    live_points_map = get_live_points_map(current_event_id)

    # Hent historikk for hver deltaker i klassisk-ligaen én gang, og gjenbruk
    # den både til Månedens Manager og til chips-oversikten.
    histories = {}
    for e in classic:
        entry_id = e.get("entry")
        if entry_id:
            histories[entry_id] = get_entry_history(entry_id)

    monthly = get_monthly_manager(classic, histories, event_month_map, event_finished_map)
    round_details = get_round_details(classic, histories, current_event_id, player_names, live_points_map)

    # Berik ligatabellen med kaptein / toppscorer / chip-info per deltaker
    for row in classic:
        extra = round_details.get(row.get("entry"), {})
        row.update(extra)

    out = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "classic": classic,
        "h2h": h2h,
        "monthlyManager": monthly,
        "currentEvent": current_event_id,
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "live-data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Skrev {out_path} — {len(classic)} i klassisk liga, {len(h2h)} i H2H, {len(monthly)} måneder (runde {current_event_id}).")


if __name__ == "__main__":
    main()
