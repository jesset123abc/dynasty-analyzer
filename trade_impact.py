"""
Trade & Draft Impact Simulator — before/after power ranking projections.

- compute_trade_impact: simulates a trade, returns full league rankings before/after
- compute_draft_impact: simulates draft picks assigned to teams, returns league rankings shift
"""
import re
import copy
from dynasty_data import normalize_name, PICK_VALUES
from power_rankings import compute_power_rankings

# Generic round-pick estimates (fallback)
_ROUND_EST = {"1st": 4000, "2nd": 1700, "3rd": 750, "4th": 350, "5th": 200}


def _match_pick(asset: str) -> str | None:
    """Try to match an asset string to a named pick label in PICK_VALUES."""
    al = asset.lower()
    for name in PICK_VALUES:
        if name.lower() in al or all(w in al for w in name.lower().split()):
            return name
    return None


def _is_pick(asset: str) -> bool:
    al = asset.lower()
    return bool(
        _match_pick(asset)
        or "pick" in al
        or re.search(r"\b20\d{2}\b", al)
        or re.search(r"1st|2nd|3rd|4th|5th", al)
    )


def _find_player_on_roster(roster: list, asset: str) -> dict | None:
    """Find a player on a roster by name (fuzzy)."""
    name = re.sub(r"\s*\([^)]*\)\s*$", "", asset).strip()
    norm = normalize_name(name)
    for p in roster:
        if normalize_name(p["name"]) == norm:
            return p
    return None


def _league_summary(rankings_list: list) -> list[dict]:
    """Compact league rankings for JSON response."""
    return [
        {
            "team_id": t["team_id"],
            "team_name": t["team_name"],
            "owner": t["owner"],
            "power_rank": t["power_rank"],
            "power_score": t["power_score"],
            "starter_value": t["starter_value"],
            "total_value": t["total_value"],
            "draft_capital": t["draft_capital"],
            "weighted_age": t["weighted_age"],
            "needs": t.get("needs", []),
            "strengths": t.get("strengths", []),
            "weaknesses": t.get("weaknesses", []),
        }
        for t in rankings_list
    ]


def compute_trade_impact(
    teams: list,
    rankings: dict,
    my_team_id: int,
    partner_team_id: int,
    give_assets: list[str],
    receive_assets: list[str],
) -> dict | None:
    """
    Simulate a trade and return before/after power ranking impact
    for BOTH teams plus full league standings.
    """
    # Compute before rankings
    before_rankings = compute_power_rankings(teams, rankings)

    my_before = next((t for t in before_rankings if t["team_id"] == my_team_id), None)
    partner_before = next((t for t in before_rankings if t["team_id"] == partner_team_id), None)

    if not my_before or not partner_before:
        return None

    # Deep copy teams for simulation
    sim_teams = copy.deepcopy(teams)
    my_team = next((t for t in sim_teams if t["id"] == my_team_id), None)
    partner_team = next((t for t in sim_teams if t["id"] == partner_team_id), None)

    if not my_team or not partner_team:
        return None

    # Move assets: give (my_team -> partner)
    for asset in give_assets:
        if _is_pick(asset):
            pick_label = _match_pick(asset)
            if pick_label and pick_label in my_team.get("picks_holds", []):
                my_team["picks_holds"].remove(pick_label)
                partner_team.setdefault("picks_holds", []).append(pick_label)
        else:
            player = _find_player_on_roster(my_team["roster"], asset)
            if player:
                my_team["roster"].remove(player)
                player["slot_id"] = 20  # goes to bench
                player["slot"] = "BE"
                partner_team["roster"].append(player)

    # Move assets: receive (partner -> my_team)
    for asset in receive_assets:
        if _is_pick(asset):
            pick_label = _match_pick(asset)
            if pick_label and pick_label in partner_team.get("picks_holds", []):
                partner_team["picks_holds"].remove(pick_label)
                my_team.setdefault("picks_holds", []).append(pick_label)
        else:
            player = _find_player_on_roster(partner_team["roster"], asset)
            if player:
                partner_team["roster"].remove(player)
                player["slot_id"] = 20
                player["slot"] = "BE"
                my_team["roster"].append(player)

    # Recompute rankings after trade
    after_rankings = compute_power_rankings(sim_teams, rankings)

    my_after = next((t for t in after_rankings if t["team_id"] == my_team_id), None)
    partner_after = next((t for t in after_rankings if t["team_id"] == partner_team_id), None)

    if not my_after or not partner_after:
        return None

    # Positional value changes for my team
    pos_changes = {}
    for pos in ["QB", "RB", "WR", "TE"]:
        before_val = my_before["pos_scores"][pos]["value"]
        after_val = my_after["pos_scores"][pos]["value"]
        pos_changes[pos] = after_val - before_val

    # Partner positional changes
    partner_pos_changes = {}
    for pos in ["QB", "RB", "WR", "TE"]:
        before_val = partner_before["pos_scores"][pos]["value"]
        after_val = partner_after["pos_scores"][pos]["value"]
        partner_pos_changes[pos] = after_val - before_val

    # Needs analysis
    before_needs = set(my_before.get("needs", []))
    after_needs = set(my_after.get("needs", []))

    return {
        "before": {
            "my_rank": my_before["power_rank"],
            "my_score": my_before["power_score"],
            "partner_rank": partner_before["power_rank"],
            "partner_score": partner_before["power_score"],
            "partner_name": partner_before["team_name"],
            "partner_owner": partner_before["owner"],
        },
        "after": {
            "my_rank": my_after["power_rank"],
            "my_score": my_after["power_score"],
            "partner_rank": partner_after["power_rank"],
            "partner_score": partner_after["power_score"],
        },
        "my_rank_change": my_before["power_rank"] - my_after["power_rank"],
        "my_score_change": round(my_after["power_score"] - my_before["power_score"], 1),
        "partner_rank_change": partner_before["power_rank"] - partner_after["power_rank"],
        "partner_score_change": round(partner_after["power_score"] - partner_before["power_score"], 1),
        "pos_changes": pos_changes,
        "partner_pos_changes": partner_pos_changes,
        "resolved_needs": list(before_needs - after_needs),
        "new_needs": list(after_needs - before_needs),
        "after_strengths": my_after.get("strengths", []),
        "after_weaknesses": my_after.get("weaknesses", []),
        "league_before": _league_summary(before_rankings),
        "league_after": _league_summary(after_rankings),
    }


_SLOT_FLOOR = {
    1: 7000, 2: 5500, 3: 5000, 4: 4800, 5: 4500,
    6: 4200, 7: 4000, 8: 3500, 9: 3200, 10: 3000,
    11: 2200, 12: 2000, 13: 1800, 14: 1600, 15: 1400,
    16: 1200, 17: 1100, 18: 1000, 19: 900, 20: 800,
}


def simulate_draft_state(
    teams: list,
    rankings: dict,
    season_rankings: dict | None,
    draft_picks: list[dict],
    trade_log: list[dict] | None = None,
) -> tuple[list, dict, dict]:
    """
    Apply draft picks and in-draft pick trades to a deep-copied snapshot of
    teams + rankings. Used by both compute_draft_impact (for before/after deltas)
    and /power-rankings (to show live draft-aware standings).

    Returns (sim_teams, sim_rankings, sim_season_rankings).
    """
    if season_rankings is None:
        season_rankings = rankings

    sim_teams = copy.deepcopy(teams)

    # Apply in-draft pick trades (move pick ownership between teams)
    if trade_log:
        for trade in trade_log:
            gave_pick = trade.get("gave", "")
            from_id = trade.get("from_team_id")
            to_id = trade.get("to_team_id")
            if not from_id or not to_id:
                continue
            from_team = next((t for t in sim_teams if t["id"] == from_id), None)
            to_team = next((t for t in sim_teams if t["id"] == to_id), None)
            if not from_team or not to_team:
                continue
            pick_label = _match_pick(gave_pick)
            if pick_label and pick_label in from_team.get("picks_holds", []):
                from_team["picks_holds"].remove(pick_label)
                to_team.setdefault("picks_holds", []).append(pick_label)

    # Inject rookie values into rankings so they aren't invisible to the engine
    sim_rankings = dict(rankings)
    sim_season_rankings = dict(season_rankings)
    for pick in draft_picks:
        pname = pick.get("player_name", "")
        pnum = pick.get("pick", 0)
        ktc_est = pick.get("ktc_est", 0)
        nkey = normalize_name(pname)
        value = ktc_est if ktc_est else (_SLOT_FLOOR.get(pnum, 500) if pnum <= 20 else 300)
        premium = 1.10 if (pnum and pnum <= 3) else 1.0
        value = int(value * premium)
        for rdict in (sim_rankings, sim_season_rankings):
            existing = rdict.get(nkey)
            if existing and existing.get("combined"):
                rdict[nkey] = dict(existing)
                if premium >= 1.0:
                    rdict[nkey]["combined"] = max(existing["combined"], value)
                else:
                    rdict[nkey]["combined"] = min(existing["combined"], value)
                if not existing.get("age") or existing["age"] <= 0:
                    rdict[nkey]["age"] = 21
            else:
                rdict[nkey] = {
                    "name": pname, "combined": value,
                    "position": pick.get("pos", ""), "age": 21,
                    "rank": 999, "pos_rank": 999,
                }

    # Add drafted rookies to team rosters + consume picks.
    #
    # Mid-draft, ESPN rosters don't have the rookies yet → append them (original
    # behavior). Post-draft, ESPN rosters already contain them (and reflect later
    # trades AND cuts), so ESPN is the source of truth: appending would double-count
    # rostered rookies and resurrect waived ones, skewing the rankings. Heuristic:
    # if at least half the drafted rookies are already rostered somewhere, the
    # draft has been processed by ESPN → never append.
    rostered_names = {
        normalize_name(p.get("name", ""))
        for t in sim_teams
        for p in t.get("roster", [])
    }
    matched = sum(
        1 for pick in draft_picks
        if normalize_name(pick.get("player_name", "")) in rostered_names
    )
    espn_has_draft = draft_picks and matched >= len(draft_picks) / 2

    for pick in draft_picks:
        team_id = pick.get("team_id")
        player_name = pick.get("player_name", "")
        pos = pick.get("pos", "")
        if not team_id or not player_name:
            continue
        team = next((t for t in sim_teams if t["id"] == team_id), None)
        if not team:
            continue
        nkey = normalize_name(player_name)
        if not espn_has_draft and nkey not in rostered_names:
            rostered_names.add(nkey)
            team["roster"].append({
                "name": player_name,
                "position": pos,
                "slot": "BE",
                "slot_id": 20,
                "player_id": 0,
            })
        pick_num = pick.get("pick")
        if pick_num:
            rd = (pick_num - 1) // 10 + 1
            round_labels = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
            round_str = round_labels.get(rd, f"{rd}th")
            for pl in list(team.get("picks_holds", [])):
                if f"2026 {round_str}" in pl:
                    team["picks_holds"].remove(pl)
                    break

    return sim_teams, sim_rankings, sim_season_rankings


def compute_draft_impact(
    teams: list,
    rankings: dict,
    draft_picks: list[dict],
    trade_log: list[dict] | None = None,
    season_rankings: dict | None = None,
) -> dict:
    """
    Simulate draft picks and in-draft trades, return league power rankings shift.

    draft_picks: list of {pick: int, player_name: str, pos: str, team_id: int, ktc_est: int}
    trade_log: list of {gave: str, to_team_id: int, from_team_id: int} — pick trades during draft
    season_rankings: optional separate rankings dict for season mode (if None, uses rankings for both)

    Returns:
      before_rankings: full league rankings pre-draft
      after_rankings: full league rankings post-draft
      team_changes: [{team_id, team_name, owner, before_rank, after_rank, rank_change, score_change}]
    """
    if season_rankings is None:
        season_rankings = rankings

    # Before rankings (current state)
    before_rankings = compute_power_rankings(teams, rankings)

    sim_teams, sim_rankings, sim_season_rankings = simulate_draft_state(
        teams, rankings, season_rankings, draft_picks, trade_log
    )

    # Compute after rankings in both modes
    def _build_changes(before_list, after_list):
        changes = []
        for before in before_list:
            after = next((a for a in after_list if a["team_id"] == before["team_id"]), None)
            if not after:
                continue
            changes.append({
                "team_id": before["team_id"],
                "team_name": before["team_name"],
                "owner": before["owner"],
                "before_rank": before["power_rank"],
                "before_score": before["power_score"],
                "after_rank": after["power_rank"],
                "after_score": after["power_score"],
                "rank_change": before["power_rank"] - after["power_rank"],
                "score_change": round(after["power_score"] - before["power_score"], 1),
            })
        changes.sort(key=lambda t: t["after_rank"])
        return changes

    # Season rankings
    before_season = compute_power_rankings(teams, season_rankings, mode="season")
    after_season = compute_power_rankings(sim_teams, sim_season_rankings, mode="season")
    season_changes = _build_changes(before_season, after_season)

    # Dynasty rankings
    before_dynasty = compute_power_rankings(teams, rankings, mode="dynasty", season_rankings=season_rankings)
    after_dynasty = compute_power_rankings(sim_teams, sim_rankings, mode="dynasty", season_rankings=sim_season_rankings)
    dynasty_changes = _build_changes(before_dynasty, after_dynasty)

    # Default team_changes uses dynasty for backward compat
    return {
        "league_before": _league_summary(before_dynasty),
        "league_after": _league_summary(after_dynasty),
        "team_changes": dynasty_changes,
        "season_changes": season_changes,
        "dynasty_changes": dynasty_changes,
    }


def find_partner_team_id(teams: list, partner_name: str) -> int | None:
    """Find a team ID by team name or owner name (fuzzy match)."""
    if not partner_name:
        return None
    pn = partner_name.lower().strip()
    for t in teams:
        if pn in t["name"].lower() or pn in t["owner"].lower():
            return t["id"]
    # Try partial match
    for t in teams:
        name_words = t["name"].lower().split()
        owner_words = t["owner"].lower().split()
        if any(w in pn for w in name_words if len(w) > 2):
            return t["id"]
        if any(w in pn for w in owner_words if len(w) > 2):
            return t["id"]
    return None
