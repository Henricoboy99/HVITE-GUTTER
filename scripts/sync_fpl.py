#!/usr/bin/env python3
"""
Henter automatisk data fra Fantasy Premier League sitt offentlige API og
skriver ut data/live-data.json som nettsiden leser og viser på
"Live oppdatering"-siden. Kjøres av GitHub Actions (se
.github/workflows/sync-fpl.yml), men kan også kjøres manuelt lokalt:

    CLASSIC_LEAGUE_ID=123456 H2H_LEAGUE_ID=654321 python3 scripts/sync_fpl.py

Manedens Manager beregnes ved å summere hver managers rundepoeng (event
points) innenfor hver kalendermåned, basert på når rundens deadline var.

For hver runde som har startet (deadline passert) beregnes i tillegg, per
deltaker: poeng i runden, kaptein, evt. brukt chip og hvilken spiller som
scoret mest for dem den runden. Dette caches per runde i data/live-data.json
– ferdigspilte runder beregnes kun én gang og gjenbrukes deretter, mens
runden som pågår nå alltid regnes på nytt. Det gjør at nettsiden kan la
brukeren bla tilbake og se f.eks. runde 7, i tillegg til gjeldende runde.
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

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "live-data.json")

# Antall av hver chip-type en manager har totalt i løpet av sesongen. FPL har
# siden 2023/24-sesongen gitt to av hver (én per sesonghalvdel). Justér disse
# tallene her hvis Premier League endrer reglene for en senere sesong.
CHIP_ALLOWANCE = {"wildcard": 2, "freehit": 2, "bboost": 2, "3xc": 2}
# code = forkortelsen som vises i "Chips igjen"-kolonnen.
CHIP_META = {
    "wildcard": {"code": "WC", "label": "Wildcard", "activeText": "WILDCARD AKTIVERT"},
    "freehit": {"code": "FH", "label": "Free Hit", "activeText": "FREE HIT AKTIVERT"},
    "bboost": {"code": "BB", "label": "Bench Boost", "activeText": "BENCH BOOST AKTIVERT"},
    "3xc": {"code": "TC", "label": "Trippel Captain", "activeText": "TRIPPEL CAPTAIN AKTIVERT"},
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


def load_previous_output():
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def get_bootstrap():
    return fetch_json(f"{API_BASE}/bootstrap-static/")


def get_event_month_map(bootstrap):
    """event_id -> (year, month) basert på deadline_time for runden, samt
    event_id -> om runden er ferdigspilt (finished)."""
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


def get_started_events(bootstrap):
    """Alle runder hvor deadline har passert (dvs. runden er i gang eller
    ferdig), i stigende rekkefølge. Returnerer liste av (event_id, finished)."""
    now = datetime.now(timezone.utc)
    out = []
    for ev in bootstrap.get("events", []):
        deadline = ev.get("deadline_time")
        if not deadline:
            continue
        dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        if dt <= now:
            out.append((ev["id"], bool(ev.get("finished"))))
    out.sort(key=lambda x: x[0])
    return out


def get_player_names(bootstrap):
    return {p["id"]: p.get("web_name", "Ukjent") for p in bootstrap.get("elements", [])}


def get_live_points_map(event_id):
    """element (spiller-id) -> rå poeng i den gitte runden."""
    try:
        data = fetch_json(f"{API_BASE}/event/{event_id}/live/")
    except Exception:
        return {}
    return {el["id"]: el.get("stats", {}).get("total_points", 0) for el in data.get("elements", [])}


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
    try:
        return fetch_json(f"{API_BASE}/entry/{entry_id}/history/")
    except Exception:
        return None


def get_monthly_manager(histories, id_to_name, event_month_map, event_finished_map):
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
    for ym in sorted(month_totals.keys()):
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


def get_chips_left_string(hist):
    """"WC 2 · FH 2 · BB 1 · TC 2" – kun chips med > 0 igjen tas med."""
    used = Counter()
    if hist:
        for c in hist.get("chips", []):
            name = c.get("name")
            if name in CHIP_ALLOWANCE:
                used[name] += 1
    parts = []
    for key in ("wildcard", "freehit", "bboost", "3xc"):
        left = CHIP_ALLOWANCE[key] - used.get(key, 0)
        if left > 0:
            parts.append(f"{CHIP_META[key]['code']} {left}")
    return " · ".join(parts) if parts else "Ingen igjen"


def compute_round(event_id, finished, entries, player_names):
    """Regner ut kaptein / stjernespiller / chip / rundepoeng for alle
    deltakere i én gitt runde. Returnerer {"finished":.., "entries": {...}}."""
    live_points_map = get_live_points_map(event_id)
    entries_out = {}

    for e in entries:
        entry_id = e.get("entry")
        if not entry_id:
            continue
        try:
            picks_data = fetch_json(f"{API_BASE}/entry/{entry_id}/event/{event_id}/picks/")
        except Exception:
            continue
        if not picks_data:
            continue

        picks = picks_data.get("picks", [])
        active_chip = picks_data.get("active_chip")

        captain_pick = next((p for p in picks if p.get("is_captain")), None)
        captain = player_names.get(captain_pick.get("element"), "Ukjent") if captain_pick else None

        starters = [p for p in picks if (p.get("multiplier") or 0) > 0]
        top_scorer = None
        top_scorer_points = None
        if starters:
            best = max(starters, key=lambda p: live_points_map.get(p["element"], 0))
            top_scorer = player_names.get(best["element"], "Ukjent")
            top_scorer_points = live_points_map.get(best["element"], 0)

        hist_entry = picks_data.get("entry_history", {}) or {}
        round_points = hist_entry.get("points", 0) - hist_entry.get("event_transfers_cost", 0)

        entries_out[str(entry_id)] = {
            "roundPoints": round_points,
            "captain": captain,
            "chipUsed": active_chip,
            "topScorer": top_scorer,
            "topScorerPoints": top_scorer_points,
        }

    return {"finished": finished, "entries": entries_out}


def main():
    if CLASSIC_LEAGUE_ID.startswith("REPLACE_ME") or H2H_LEAGUE_ID.startswith("REPLACE_ME"):
        print("CLASSIC_LEAGUE_ID / H2H_LEAGUE_ID er ikke satt ennå — hopper over synk.", file=sys.stderr)
        if not os.path.exists(OUT_PATH):
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump({"updatedAt": None, "classic": [], "h2h": [], "monthlyManager": [], "currentEvent": None, "rounds": {}}, f, ensure_ascii=False, indent=2)
        return

    prev = load_previous_output() or {}
    prev_rounds = prev.get("rounds", {}) if isinstance(prev.get("rounds"), dict) else {}

    bootstrap = get_bootstrap()
    classic = get_classic_standings(CLASSIC_LEAGUE_ID)
    h2h = get_h2h_standings(H2H_LEAGUE_ID)
    event_month_map, event_finished_map = get_event_month_map(bootstrap)
    player_names = get_player_names(bootstrap)

    id_to_name = {e["entry"]: e["manager"] for e in classic if e.get("entry")}

    # Historikk (for Månedens Manager og chips-oversikt) – hentes én gang per deltaker
    histories = {}
    for e in classic:
        entry_id = e.get("entry")
        if entry_id:
            histories[entry_id] = get_entry_history(entry_id)

    monthly = get_monthly_manager(histories, id_to_name, event_month_map, event_finished_map)

    # Legg "chips igjen" (statisk, ikke rundeavhengig) rett på hver rad i ligatabellen
    for row in classic:
        row["chipsLeft"] = get_chips_left_string(histories.get(row.get("entry")))

    # Runder: gjenbruk ferdigspilte runder fra forrige kjøring, regn kun ut nye
    # eller fortsatt-pågående runder på nytt.
    started_events = get_started_events(bootstrap)
    rounds = {}
    for event_id, finished in started_events:
        key = str(event_id)
        cached = prev_rounds.get(key)
        if cached and cached.get("finished"):
            rounds[key] = cached
        else:
            print(f"Beregner runde {event_id} (finished={finished}) …", file=sys.stderr)
            rounds[key] = compute_round(event_id, finished, classic, player_names)

    current_event_id = started_events[-1][0] if started_events else None

    out = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "classic": classic,
        "h2h": h2h,
        "monthlyManager": monthly,
        "currentEvent": current_event_id,
        "rounds": rounds,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Skrev {OUT_PATH} — {len(classic)} i klassisk liga, {len(h2h)} i H2H, {len(monthly)} måneder, {len(rounds)} runder (gjeldende: {current_event_id}).")


if __name__ == "__main__":
    main()
