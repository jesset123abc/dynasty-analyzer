"""
Power Rankings engine for dynasty fantasy football.

Two modes:
  "season"  — Who wins in 2026? Pure roster strength, no future assets.
    50% Starter Quality (VOR), 30% Total Roster, 13% Positional Balance, 7% Depth
  "dynasty" — Who is set up best long-term?
    28% Starter Quality (VOR), 20% Total Roster, 18% Draft Capital,
    10% Positional Balance, 10% Youth, 14% Prospect Value

  Dynasty weights are calibrated so picks/capital matter as much as
  current starter strength — teams with stacked future picks (often
  rebuilders) aren't punished for short-term roster weakness.

All player values use the 0-9999 combined scale from dynasty_data.
"""
import json
import os

from dynasty_data import normalize_name, PICK_VALUES

# Owner name (any common variant) → ESPN team ID. Mirrors _OWNER_TEAM_IDS in app.py.
# draft_state.json entries can use first names, nicknames, or last names.
_OWNER_FIRST_TO_ID = {
    "patrick": 1, "stevenson": 1,
    "alexa": 2, "feldman": 2,
    "alex": 3, "wall": 3,
    "brad": 4, "bradley": 4, "komar": 4,
    "lubin": 5, "nathaniel": 5, "nate": 5,
    "schueler": 6, "john": 6,
    "denton": 7, "grant": 7,
    "jesse": 8, "gz": 8, "gztz": 8,
    "driscoll": 9, "sarah": 9,
    "berkowitz": 10, "jacob": 10, "jake": 10,
}
_DRAFT_STATE_FILE = os.path.join(os.path.dirname(__file__), "draft_state.json")
_PICKS_PER_ROUND = 10  # 10-team league
_ROUND_LABEL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}

# ── Position minimums for a healthy dynasty roster (Superflex) ───────────────
_POS_MINS = {"QB": 2, "RB": 3, "WR": 4, "TE": 1}
_POSITIONS = ["QB", "RB", "WR", "TE"]

# Superflex optimal lineup slots
_OPT_SLOTS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
_STARTER_SLOTS = {"QB": 2, "RB": 2, "WR": 3, "TE": 1}  # for balance calc
_EXPECTED_SHARE = {"QB": 0.28, "RB": 0.22, "WR": 0.35, "TE": 0.15}

# Prospect value: young players (<=24) get full value, 25 gets 60%, 26+ nothing
_PROSPECT_AGE_CUTOFF = 24
_PROSPECT_FADE = {25: 0.6, 26: 0.3}


def _player_value(player: dict, rankings: dict) -> int:
    key = normalize_name(player["name"])
    return rankings.get(key, {}).get("combined", 0)


def _player_age(player: dict, rankings: dict) -> float:
    key = normalize_name(player["name"])
    return rankings.get(key, {}).get("age", 0)


# Slot-based pick values by round — position 1 (worst team) to 10 (best team)
# Bottom 4 miss playoffs; any of them can win 1.01 via most points during playoffs.
# So bottom 4 share a blended value (avg of slots 1-4), reflecting equal 1.01 chance.
# Playoff teams (5-10) are ordered by finish (first out = 1.05, champ = 1.10).
_LOTTERY_SLOTS = 4  # bottom 4 are in the lottery
_RAW_SLOT_VALUES = {
    "1st": [7100, 5900, 5400, 4800, 4600, 4400, 3900, 3500, 3200, 3000],
    "2nd": [2100, 1900, 1800, 1700, 1700, 1600, 1600, 1500, 1400, 1300],
    "3rd": [800, 750, 720, 700, 700, 680, 660, 620, 550, 450],
    "4th": [350, 320, 310, 300, 290, 290, 280, 280, 220, 200],
}
# Compute blended lottery values: bottom 4 all get the average of slots 1-4
_SLOT_VALUES = {}
for rnd, vals in _RAW_SLOT_VALUES.items():
    lottery_avg = round(sum(vals[:_LOTTERY_SLOTS]) / _LOTTERY_SLOTS)
    _SLOT_VALUES[rnd] = [lottery_avg] * _LOTTERY_SLOTS + vals[_LOTTERY_SLOTS:]


def _used_picks_by_team(state_file: str = _DRAFT_STATE_FILE) -> dict:
    """Read draft_state.json and return {team_id: {round_str: count}} of picks used."""
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file) as f:
            ds = json.load(f)
    except Exception:
        return {}
    used = {}
    for entry in ds.get("draftLog", []):
        owner_first = (entry.get("teamOwner") or "").split()[0].lower()
        team_id = _OWNER_FIRST_TO_ID.get(owner_first)
        pick_num = entry.get("pick", 0)
        if not team_id or not pick_num:
            continue
        rd = (pick_num - 1) // _PICKS_PER_ROUND + 1
        round_str = _ROUND_LABEL.get(rd)
        if not round_str:
            continue
        used.setdefault(team_id, {})[round_str] = used.get(team_id, {}).get(round_str, 0) + 1
    return used


def _capital_picks(team: dict, used_by_team: dict, dynamic_values: dict | None) -> list:
    """Return picks_holds minus 2026 picks already used in the rookie draft.
    When N picks of a given round are used, drops the N highest-value picks
    of that round (drafts run in slot order, top-value picks go first)."""
    holds = team.get("picks_holds", [])
    used_counts = used_by_team.get(team["id"], {})
    if not used_counts:
        return list(holds)

    by_round_2026 = {"1st": [], "2nd": [], "3rd": [], "4th": []}
    other = []
    for pk in holds:
        if "2026" not in pk:
            other.append(pk)
            continue
        for rs in by_round_2026:
            if rs in pk:
                by_round_2026[rs].append(pk)
                break
        else:
            other.append(pk)

    result = list(other)
    for rs, picks in by_round_2026.items():
        n_used = used_counts.get(rs, 0)
        picks_sorted = sorted(picks, key=lambda p: _pick_value(p, dynamic_values), reverse=True)
        result.extend(picks_sorted[n_used:])
    return result


def _pick_value(pick_label: str, dynamic_values: dict | None = None) -> int:
    """Look up a pick's trade value.

    If dynamic_values is provided, use it for 2027+ picks. dynamic_values maps
    pick labels to KTC values, computed from projected season finish.
    """
    base = pick_label.split(" [")[0].split(" (KTC")[0].split(" (VAL")[0].strip()

    # Use dynamic values for future picks if available
    if dynamic_values and base in dynamic_values:
        return dynamic_values[base]

    pv = PICK_VALUES.get(base)
    if pv:
        return pv["ktc"]
    return 0


def _compute_dynamic_pick_values(teams: list, rankings: dict) -> dict:
    """Compute dynamic pick values for 2027+ picks based on season power rankings.

    Runs season rankings to project each team's finish, then values their
    future picks based on projected draft slot (worst team = slot 1 = highest value).
    Not a snake draft, so slot holds across all rounds.
    """
    # Compute season rankings to project finish order
    season_ranks = compute_power_rankings(teams, rankings, mode="season")

    # Map team_id to projected draft slot (1 = worst team = best pick)
    # Season rank #10 (worst) -> draft slot 1 (1.01), rank #1 (best) -> slot 10 (1.10)
    num_teams = len(season_ranks)
    team_slot = {}
    for sr in season_ranks:
        # Invert: best season rank (1) = worst draft slot (10)
        draft_slot = num_teams - sr["power_rank"] + 1  # rank 1 -> slot 10, rank 10 -> slot 1
        team_slot[sr["team_id"]] = draft_slot

    # Build owner name -> draft slot mapping
    # Pick labels use names like "Jesse's 2027 1st", "Driscoll's 2027 1st"
    # ESPN data uses full names like "Gz Tz", "Sarah Driscoll"
    # Map all known name variants to draft slot
    _NAME_TO_ID = {
        "patrick": 1, "stevenson": 1,
        "alexa": 2, "feldman": 2,
        "alex": 3, "wall": 3,
        "brad": 4, "bradley": 4, "komar": 4,
        "lubin": 5, "nathaniel": 5, "nate": 5,
        "schueler": 6, "john": 6,
        "denton": 7, "grant": 7,
        "jesse": 8, "gz": 8, "gztz": 8,
        "driscoll": 9, "sarah": 9,
        "berkowitz": 10, "jacob": 10, "jake": 10,
    }
    owner_slot = {}
    for name_variant, tid in _NAME_TO_ID.items():
        if tid in team_slot:
            owner_slot[name_variant] = team_slot[tid]

    # Generate dynamic values for all 2027+ picks
    dynamic = {}
    for team in teams:
        for pk_label in team.get("picks_holds", []):
            base = pk_label.split(" [")[0].split(" (KTC")[0].split(" (VAL")[0].strip()
            # Only apply to 2027+ picks
            if "2027" not in base and "2028" not in base:
                continue
            # Determine which round
            round_str = None
            for r in ("1st", "2nd", "3rd", "4th"):
                if r in base:
                    round_str = r
                    break
            if not round_str:
                continue
            # Determine whose pick this originally is (the team name in the label)
            # e.g., "Driscoll's 2027 1st" -> look up Driscoll's projected slot
            pick_owner = base.split("'s")[0].lower() if "'s" in base else ""
            slot = owner_slot.get(pick_owner)
            if slot and round_str in _SLOT_VALUES:
                slot_idx = min(slot - 1, len(_SLOT_VALUES[round_str]) - 1)
                dynamic[base] = _SLOT_VALUES[round_str][slot_idx]

    return dynamic


def _pos_players(roster: list, pos: str) -> list:
    return [p for p in roster if p["position"] == pos]


def _compute_replacement_values(all_teams: list, rankings: dict) -> dict:
    """
    Compute replacement-level value per position by looking at the Nth-best
    player at each position across the entire league.

    N is calibrated to the number of starters at that position across all
    10 teams in a Superflex lineup:
      - QB:  ~20 (1 QB starter + most OP slots = SF QB premium)
      - RB:  ~25 (2 starters + most FLEX slots → 2.5/team × 10)
      - WR:  ~35 (3 starters + some FLEX → 3.5/team × 10)
      - TE:  ~10 (1 starter × 10)

    Replacement value = "what you could realistically pluck off waivers at
    this position if your starter goes down." A starter's contribution is
    measured as Value Over Replacement (VOR) — much fairer across positions.
    """
    pos_players = {"QB": [], "RB": [], "WR": [], "TE": []}
    for team in all_teams:
        for p in team.get("roster", []):
            pos = p.get("position", "")
            if pos in pos_players:
                val = _player_value(p, rankings)
                if val > 0:
                    pos_players[pos].append(val)

    targets = {"QB": 20, "RB": 25, "WR": 35, "TE": 10}
    replacement = {}
    for pos, vals in pos_players.items():
        vals.sort(reverse=True)
        n = targets[pos]
        if len(vals) > n:
            replacement[pos] = vals[n]  # the (n+1)th-best value
        elif vals:
            replacement[pos] = vals[-1]
        else:
            replacement[pos] = 0
    return replacement


def _compute_optimal_starters(all_players: list, rankings: dict, replacement: dict | None = None) -> int:
    """
    Compute total Value Over Replacement (VOR) of the optimal starting lineup.
    Falls back to raw value sum if `replacement` is None (used by older
    callers that haven't been updated).
    """
    used = set()
    starter_score = 0
    pos_top = {}

    for pos in _POSITIONS:
        players = sorted(
            _pos_players(all_players, pos),
            key=lambda p: _player_value(p, rankings), reverse=True
        )
        slots = _OPT_SLOTS[pos]
        for p in players[:slots]:
            v = _player_value(p, rankings)
            if replacement is not None:
                starter_score += max(0, v - replacement.get(pos, 0))
            else:
                starter_score += v
            used.add(id(p))
        pos_top[pos] = players

    # FLEX slot: best remaining RB/WR/TE
    # Replacement for FLEX = average of RB/WR/TE replacements (player is
    # competing against the next-best at any of those positions)
    flex_repl = 0
    if replacement is not None:
        rb_r = replacement.get("RB", 0)
        wr_r = replacement.get("WR", 0)
        te_r = replacement.get("TE", 0)
        flex_repl = (rb_r + wr_r + te_r) / 3

    flex_candidates = [
        p for pos in ("RB", "WR", "TE") for p in pos_top[pos]
        if id(p) not in used
    ]
    flex_candidates.sort(key=lambda p: _player_value(p, rankings), reverse=True)
    if flex_candidates:
        v = _player_value(flex_candidates[0], rankings)
        if replacement is not None:
            starter_score += max(0, v - flex_repl)
        else:
            starter_score += v
        used.add(id(flex_candidates[0]))

    # OP/SF slot: best remaining player. In SF, this is usually a QB, so
    # replacement is QB replacement.
    op_repl = replacement.get("QB", 0) if replacement is not None else 0
    op_candidates = [p for p in all_players if id(p) not in used]
    op_candidates.sort(key=lambda p: _player_value(p, rankings), reverse=True)
    if op_candidates:
        v = _player_value(op_candidates[0], rankings)
        if replacement is not None:
            starter_score += max(0, v - op_repl)
        else:
            starter_score += v

    return starter_score


def _compute_prospect_value(all_players: list, rankings: dict) -> int:
    """Sum of dynasty value for young players (age <= 24), fading to 26."""
    total = 0
    for p in all_players:
        v = _player_value(p, rankings)
        a = _player_age(p, rankings)
        if v <= 0 or a <= 0:
            continue
        if a <= _PROSPECT_AGE_CUTOFF:
            total += v
        elif a in _PROSPECT_FADE:
            total += int(v * _PROSPECT_FADE[a])
        elif a < 27:
            # Smooth fade for fractional ages
            if a <= 25:
                total += int(v * 0.6)
            elif a <= 26:
                total += int(v * 0.3)
    return total


def _compute_depth_score(all_players: list, rankings: dict) -> int:
    """Count of roster players with meaningful dynasty value (>1500)."""
    return sum(1 for p in all_players if _player_value(p, rankings) > 1500)


def _age_penalty(weighted_age: float) -> float:
    """
    Tiered age penalty for season mode.
    27-28: 1 pt/yr, 28-30: 2 pt/yr, 30+: 3 pt/yr.
    Applied as a deduction from the final power score.
    """
    if weighted_age <= 27:
        return 0
    total = 0.0
    if weighted_age > 27:
        chunk = min(weighted_age, 28) - 27
        total += chunk * 1
    if weighted_age > 28:
        chunk = min(weighted_age, 30) - 28
        total += chunk * 2
    if weighted_age > 30:
        chunk = weighted_age - 30
        total += chunk * 3
    return round(total, 1)


def compute_power_rankings(
    teams: list, rankings: dict, mode: str = "dynasty",
    season_rankings: dict | None = None,
    apply_draft_state: bool = False,
) -> list[dict]:
    """
    Score and rank all teams.

    mode="season"  — 2026 competitiveness (starters + roster + balance + depth)
                     Filters out players with <50 projected 2026 FP.
                     Applies tiered age penalty to final score.
    mode="dynasty" — long-term outlook (adds capital, youth, prospect value)
                     Future pick values are dynamically weighted by projected
                     season finish (worse teams = higher pick value).

    season_rankings: optional separate rankings dict for season mode projections.
                     Used by dynasty mode to project draft order for future picks.
                     If None, uses `rankings` for season projections too.

    Returns list sorted by power_score (descending).
    """
    # For dynasty mode, compute dynamic pick values based on projected season finish
    dynamic_pick_vals = None
    if mode == "dynasty":
        sea_ranks = season_rankings if season_rankings else rankings
        dynamic_pick_vals = _compute_dynamic_pick_values(teams, sea_ranks)

    # Compute league-wide replacement values once — used for VOR scoring.
    # This properly weights starter contributions by positional scarcity:
    # a 7000-value QB in SF (where replacement is ~3000) contributes 4000;
    # a 7000-value WR (where replacement is ~2500) contributes 4500.
    replacement = _compute_replacement_values(teams, rankings)

    scored = []

    # Cap rosters to league-min size so low-value dart throws don't
    # inflate totals through volume. Only the top N players (by value) count.
    min_roster = min(len(t["roster"]) for t in teams) if teams else 25

    for team in teams:
        roster = team["roster"]

        # Season mode: filter out dead roster weight (players with <50 projected FP)
        if mode == "season":
            all_players = [
                p for p in roster
                if p.get("fpts_2026_proj", 100) >= 50  # keep if field missing (rookies etc.)
            ]
        else:
            all_players = roster

        # Trim to top N players by value — prevents below-replacement players
        # from inflating total/prospect/depth scores through roster volume
        all_players = sorted(
            all_players, key=lambda p: _player_value(p, rankings), reverse=True
        )[:min_roster]

        starter_value = _compute_optimal_starters(all_players, rankings, replacement)
        total_value = sum(_player_value(p, rankings) for p in all_players)
        # Count all picks team still holds. When apply_draft_state=True, also
        # subtract picks already used in the live rookie draft (their value
        # transitions to the rookie player on the roster). Caller-controlled so
        # compute_draft_impact's "before" baseline isn't polluted by saved state.
        used_by_team = _used_picks_by_team() if apply_draft_state else {}
        future_picks = _capital_picks(team, used_by_team, dynamic_pick_vals)
        draft_capital = sum(
            _pick_value(pk, dynamic_pick_vals) for pk in future_picks
        )
        prospect_value = _compute_prospect_value(all_players, rankings)
        depth_score = _compute_depth_score(all_players, rankings)

        # ── Positional Breakdown ─────────────────────────────
        pos_scores = {}
        for pos in _POSITIONS:
            players = _pos_players(all_players, pos)
            vals = sorted(
                [_player_value(p, rankings) for p in players], reverse=True
            )
            top_player = None
            top_val = 0
            for p in players:
                v = _player_value(p, rankings)
                if v > top_val:
                    top_val = v
                    top_player = p["name"]
            pos_scores[pos] = {
                "value": sum(vals),
                "count": len(players),
                "top_player": top_player,
                "top_value": top_val,
                "values": vals,
            }

        # ── Positional Balance Score ─────────────────────────
        pos_starter_vals = {}
        for pos in _POSITIONS:
            slots = _STARTER_SLOTS[pos]
            top_vals = pos_scores[pos]["values"][:slots]
            pos_starter_vals[pos] = sum(top_vals)

        total_pos_val = sum(pos_starter_vals.values()) or 1
        balance_score = 0
        for pos in _POSITIONS:
            actual_share = pos_starter_vals[pos] / total_pos_val if total_pos_val else 0
            expected = _EXPECTED_SHARE[pos]
            ratio = min(actual_share / expected, 1.5) if expected else 1.0
            balance_score += ratio * 1000

        for pos in _POSITIONS:
            count = pos_scores[pos]["count"]
            minimum = _POS_MINS[pos]
            if count < minimum:
                balance_score -= (minimum - count) * 500

        # ── Youth Score ──────────────────────────────────────
        age_vals = []
        for p in all_players:
            v = _player_value(p, rankings)
            a = _player_age(p, rankings)
            if v > 500 and a > 0:
                age_vals.append((a, v))

        if age_vals:
            total_weight = sum(v for _, v in age_vals)
            weighted_age = sum(a * v for a, v in age_vals) / total_weight if total_weight > 0 else 27
        else:
            weighted_age = 27

        youth_score = max(0, (30 - weighted_age) * 1000)

        scored.append({
            "team_id": team["id"],
            "team_name": team["name"],
            "owner": team["owner"],
            "record": f"{team['wins']}-{team['losses']}",
            "starter_value": starter_value,
            "total_value": total_value,
            "draft_capital": draft_capital,
            "prospect_value": prospect_value,
            "depth_score": depth_score,
            "pos_scores": pos_scores,
            "balance_score": balance_score,
            "youth_score": youth_score,
            "weighted_age": round(weighted_age, 1),
            "picks_count": len(team.get("picks_holds", [])),
        })

    if not scored:
        return []

    # ── Normalize each component to 0-100 (pct of max) ─────────────
    def _norm(vals):
        mx = max(vals) if vals else 1
        if mx == 0:
            mx = 1
        return [v / mx * 100 for v in vals]

    def _norm_abs(vals, ceiling):
        """Normalize against a fixed ceiling instead of league max.
        This prevents one team's changes from distorting everyone else's scores."""
        return [min(v / ceiling * 100, 100) for v in vals]

    starter_n = _norm([s["starter_value"] for s in scored])
    total_n = _norm([s["total_value"] for s in scored])
    # Capital uses absolute scaling — a fixed ceiling prevents the league leader
    # from distorting everyone else's scores when their capital changes (e.g. drafting).
    # Sized so the heaviest-capital team in the league lands ~90%, leaving headroom.
    _CAPITAL_CEILING = 30000
    capital_n = _norm_abs([s["draft_capital"] for s in scored], _CAPITAL_CEILING)
    balance_n = _norm([s["balance_score"] for s in scored])
    youth_n = _norm([s["youth_score"] for s in scored])
    prospect_n = _norm([s["prospect_value"] for s in scored])
    depth_n = _norm([s["depth_score"] for s in scored])

    # ── Apply weights based on mode ────────────────────────────────
    for i, s in enumerate(scored):
        if mode == "season":
            raw_score = round(
                starter_n[i] * 0.50
                + total_n[i] * 0.30
                + balance_n[i] * 0.13
                + depth_n[i] * 0.07,
                1,
            )
            penalty = _age_penalty(s["weighted_age"])
            s["power_score"] = round(max(0, raw_score - penalty), 1)
            s["age_penalty"] = penalty
            s["components"] = {
                "starters": round(starter_n[i], 1),
                "roster": round(total_n[i], 1),
                "balance": round(balance_n[i], 1),
                "depth": round(depth_n[i], 1),
            }
        else:  # dynasty
            s["power_score"] = round(
                starter_n[i] * 0.28
                + total_n[i] * 0.20
                + capital_n[i] * 0.18
                + balance_n[i] * 0.10
                + youth_n[i] * 0.10
                + prospect_n[i] * 0.14,
                1,
            )
            s["components"] = {
                "starters": round(starter_n[i], 1),
                "roster": round(total_n[i], 1),
                "capital": round(capital_n[i], 1),
                "balance": round(balance_n[i], 1),
                "youth": round(youth_n[i], 1),
                "prospects": round(prospect_n[i], 1),
            }

        s["mode"] = mode

    # Sort by power score descending
    scored.sort(key=lambda s: s["power_score"], reverse=True)

    # Assign ranks
    for rank, s in enumerate(scored, 1):
        s["power_rank"] = rank

    # ── League averages for needs/strengths ──────────────────
    league_avg = {}
    for pos in _POSITIONS:
        avg_val = sum(s["pos_scores"][pos]["value"] for s in scored) / len(scored)
        avg_cnt = sum(s["pos_scores"][pos]["count"] for s in scored) / len(scored)
        league_avg[pos] = {"value": avg_val, "count": avg_cnt}

    for s in scored:
        strengths = []
        weaknesses = []
        needs = []

        for pos in _POSITIONS:
            team_val = s["pos_scores"][pos]["value"]
            avg_val = league_avg[pos]["value"]
            team_cnt = s["pos_scores"][pos]["count"]

            if avg_val > 0:
                ratio = team_val / avg_val
                if ratio >= 1.3:
                    strengths.append(f"Elite {pos} room")
                elif ratio >= 1.1:
                    strengths.append(f"Strong {pos} depth")

                if ratio <= 0.7:
                    weaknesses.append(f"Weak at {pos}")
                    needs.append(pos)
                elif ratio <= 0.85:
                    weaknesses.append(f"Below average {pos}")
                    needs.append(pos)

            if team_cnt < _POS_MINS[pos]:
                weaknesses.append(f"Only {int(team_cnt)} {pos}(s)")
                if pos not in needs:
                    needs.append(pos)

        if s["weighted_age"] <= 24.5:
            strengths.append("Very young roster")
        elif s["weighted_age"] <= 25.5:
            strengths.append("Youth advantage")

        if s["weighted_age"] >= 28:
            weaknesses.append("Aging roster")
        elif s["weighted_age"] >= 27:
            weaknesses.append("Roster trending older")

        if s["draft_capital"] >= 15000:
            strengths.append("Loaded with draft capital")
        elif s["draft_capital"] >= 10000:
            strengths.append("Strong draft capital")

        if s["draft_capital"] <= 3000:
            weaknesses.append("Limited draft capital")

        if s["prospect_value"] >= 25000:
            strengths.append("Elite prospect pipeline")
        elif s["prospect_value"] >= 15000:
            strengths.append("Strong young core")

        s["strengths"] = strengths
        s["weaknesses"] = weaknesses
        s["needs"] = needs

    return scored
