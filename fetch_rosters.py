"""
Step 1: Fetch ESPN dynasty league rosters and print to terminal.
Run with: python fetch_rosters.py
"""
import requests
import json
import sys
import io

# Force UTF-8 output on Windows to handle special characters in player names
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ESPN_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2025/segments/0/leagues/1879404067"
    "?view=mRoster&view=mTeam&view=mDraftDetail"
)

COOKIES = {
    "espn_s2": (
        "AECmcgdA793f0P6O1MFlU1yJU495ZjEtk97YDBcnlCtGU73WulIqcAIsMglpgfqSZLVk1OMj0XYQ0idGN"
        "ZpYxjnhP9%2BiV3if8cqEgNM%2F6gFe3Nju4OmTOlGFPTE3rlasEjDjkYdzvW6YJ2PzjivOez910svHf"
        "qt6HdC9Yb%2FaGbbEzf9OAnIqlaF9gj5NoL7aIAm%2B1BcdFjrJTp3GTGOM%2FTB1yyz2pkmyp4Uo0Ag"
        "DEsYayvJr76cAd29f%2BVO4eETIVogM2BXsGE%2FDuJp3O%2BQs%2FVkR"
    ),
    "SWID": "{C8927212-4A01-4800-AC18-15CCD5C13E53}",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://fantasy.espn.com/",
    "Cookie": (
        "espn_s2=AECmcgdA793f0P6O1MFlU1yJU495ZjEtk97YDBcnlCtGU73WulIqcAIsMglpgfqSZLVk1OMj0XYQ0idGN"
        "ZpYxjnhP9%2BiV3if8cqEgNM%2F6gFe3Nju4OmTOlGFPTE3rlasEjDjkYdzvW6YJ2PzjivOez910svHf"
        "qt6HdC9Yb%2FaGbbEzf9OAnIqlaF9gj5NoL7aIAm%2B1BcdFjrJTp3GTGOM%2FTB1yyz2pkmyp4Uo0Ag"
        "DEsYayvJr76cAd29f%2BVO4eETIVogM2BXsGE%2FDuJp3O%2BQs%2FVkR"
        "; SWID={C8927212-4A01-4800-AC18-15CCD5C13E53}"
    ),
}

# defaultPositionId → position name
POSITIONS = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    7: "P",
    16: "D/ST",
}

# lineupSlotId → slot label
SLOT_LABELS = {
    0:  "QB",
    1:  "QB",
    2:  "RB",
    3:  "RB/WR",
    4:  "WR",
    5:  "WR/TE",
    6:  "TE",
    7:  "OP",
    16: "D/ST",
    17: "K",
    20: "BE",
    21: "IR",
    23: "FLEX",
    24: "OP",
    25: "RDP",
}


def fetch_data():
    print("Connecting to ESPN API...")
    resp = requests.get(ESPN_URL, cookies=COOKIES, headers=HEADERS, timeout=30)
    print(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def parse_members(data):
    members = {}
    for m in data.get("members", []):
        swid = m.get("id", "")
        first = m.get("firstName", "").strip()
        last = m.get("lastName", "").strip()
        display = m.get("displayName", "").strip()
        members[swid] = f"{first} {last}".strip() or display or swid
    return members


def print_rosters(data):
    members = parse_members(data)
    teams = sorted(data.get("teams", []), key=lambda t: t["id"])

    season = data.get("seasonId", "?")
    league_id = data.get("id", "?")

    print(f"\n{'='*62}")
    print(f"  ESPN Dynasty League | Season {season} | League ID: {league_id}")
    print(f"  Teams found: {len(teams)}")
    print(f"{'='*62}\n")

    for idx, team in enumerate(teams, 1):
        location = team.get("location", "")
        nickname = team.get("nickname", "")
        team_name = f"{location} {nickname}".strip() or f"Team {team['id']}"

        owner_id = team.get("primaryOwner", "")
        owner = members.get(owner_id, owner_id or "Unknown")

        record = team.get("record", {}).get("overall", {})
        wins = record.get("wins", 0)
        losses = record.get("losses", 0)
        ties = record.get("ties", 0)
        record_str = f"{wins}-{losses}" + (f"-{ties}" if ties else "")

        entries = team.get("roster", {}).get("entries", [])

        starters, bench, ir, rdp = [], [], [], []

        for entry in entries:
            slot_id = entry.get("lineupSlotId", 20)
            player = entry.get("playerPoolEntry", {}).get("player", {})
            name = player.get("fullName", "Unknown Player")
            pos = POSITIONS.get(player.get("defaultPositionId", 0), "???")
            slot = SLOT_LABELS.get(slot_id, f"Slot{slot_id}")

            line = f"    [{slot:<5}]  {name:<28}  ({pos})"

            if slot_id == 21:
                ir.append(line)
            elif slot_id == 25:
                rdp.append(line)
            elif slot_id == 20:
                bench.append(line)
            else:
                starters.append(line)

        print(f"{'─'*62}")
        print(f"  [{idx:>2}] {team_name}")
        print(f"       Owner: {owner}  |  Record: {record_str}")
        print(f"{'─'*62}")

        if starters:
            print(f"  STARTERS ({len(starters)}):")
            for p in starters:
                print(p)

        if bench:
            print(f"  BENCH ({len(bench)}):")
            for p in bench:
                print(p)

        if rdp:
            print(f"  ROOKIE/TAXI ({len(rdp)}):")
            for p in rdp:
                print(p)

        if ir:
            print(f"  INJURED RESERVE ({len(ir)}):")
            for p in ir:
                print(p)

        total = len(entries)
        print(f"  Total players on roster: {total}")
        print()

    print(f"\n✓ Successfully loaded {len(teams)} teams from ESPN.\n")


if __name__ == "__main__":
    try:
        data = fetch_data()
        print_rosters(data)
    except requests.HTTPError as e:
        print(f"\nHTTP Error: {e}")
        print("Check that your ESPN cookies are valid and the league ID is correct.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
