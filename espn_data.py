"""Shared ESPN Fantasy Football data fetching and parsing."""
import os
import requests
from picks_data import get_team_picks
from dynasty_data import annotate_player, enrich_pick_label

ESPN_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2025"
    "/segments/0/leagues/1879404067?view=mRoster&view=mTeam&view=mDraftDetail"
)

_espn_s2 = os.getenv("ESPN_S2", "")
_swid    = os.getenv("ESPN_SWID", "")
ESPN_COOKIE = f"espn_s2={_espn_s2}; SWID={_swid}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://fantasy.espn.com/",
    "Cookie": ESPN_COOKIE,
}

POSITIONS = {
    1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 7: "P", 16: "D/ST",
}

SLOT_LABELS = {
    0: "QB", 1: "QB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE",
    6: "TE", 7: "OP", 16: "D/ST", 17: "K", 20: "BE", 21: "IR",
    23: "FLEX", 24: "OP", 25: "RDP",
}


def fetch_league_data():
    resp = requests.get(ESPN_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_league(data):
    members = {}
    for m in data.get("members", []):
        first = m.get("firstName", "").strip()
        last = m.get("lastName", "").strip()
        display = m.get("displayName", "").strip()
        members[m["id"]] = f"{first} {last}".strip() or display or m["id"]

    teams = []
    for team in sorted(data.get("teams", []), key=lambda t: t["id"]):
        location = team.get("location", "")
        nickname = team.get("nickname", "")
        name = f"{location} {nickname}".strip() or f"Team {team['id']}"

        owner = members.get(team.get("primaryOwner", ""), "Unknown")
        record = team.get("record", {}).get("overall", {})

        roster = []
        for entry in team.get("roster", {}).get("entries", []):
            slot_id = entry.get("lineupSlotId", 20)
            player = entry.get("playerPoolEntry", {}).get("player", {})
            roster.append({
                "name": player.get("fullName", "Unknown"),
                "position": POSITIONS.get(player.get("defaultPositionId", 0), "???"),
                "slot": SLOT_LABELS.get(slot_id, "BE"),
                "slot_id": slot_id,
                "player_id": player.get("id", 0),
            })

        # Sort: starters → bench → IR
        def sort_key(p):
            if p["slot_id"] == 21:
                return (3, 0)
            if p["slot_id"] == 25:
                return (2, 0)
            if p["slot_id"] == 20:
                return (1, 0)
            return (0, p["slot_id"])

        roster.sort(key=sort_key)

        picks = get_team_picks(team["id"])
        teams.append({
            "id": team["id"],
            "name": name,
            "owner": owner,
            "wins": record.get("wins", 0),
            "losses": record.get("losses", 0),
            "roster": roster,
            "picks_holds": picks["holds"],
            "picks_traded_away": picks["traded_away"],
        })

    return teams


def build_league_prompt(teams, my_team_id, rankings=None):
    """Build the league summary string for Claude, annotated with live dynasty rankings."""
    rankings = rankings or {}
    lines = []

    for team in teams:
        starters = [p for p in team["roster"] if p["slot_id"] not in (20, 21, 25)]
        bench    = [p for p in team["roster"] if p["slot_id"] == 20]
        taxi     = [p for p in team["roster"] if p["slot_id"] == 25]
        ir       = [p for p in team["roster"] if p["slot_id"] == 21]

        marker = "  <<<< THIS IS MY TEAM >>>>" if team["id"] == my_team_id else ""
        lines.append(f"\n{'='*55}")
        lines.append(f"Team: {team['name']}  ({team['wins']}-{team['losses']}){marker}")
        lines.append(f"Owner: {team['owner']}")

        def fmt(players):
            return ", ".join(annotate_player(p, rankings) for p in players)

        lines.append(f"Starters: {fmt(starters)}")
        if bench:
            lines.append(f"Bench:    {fmt(bench)}")
        if taxi:
            lines.append(f"Taxi/RDP: {fmt(taxi)}")
        if ir:
            lines.append(f"IR:       {fmt(ir)}")

        holds  = [enrich_pick_label(p) for p in team.get("picks_holds", [])]
        traded = team.get("picks_traded_away", [])
        if holds:
            lines.append(f"Available Picks: {', '.join(holds)}")
        if traded:
            lines.append(f"Picks Traded Away: {', '.join(traded)}")

    return "\n".join(lines)
