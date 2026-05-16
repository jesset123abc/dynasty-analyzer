"""
Monte Carlo season simulation for the 10-team Superflex league.

For each simulation:
  1. Each player has a per-game point distribution (mean from projections,
     sigma derived from position-typical fantasy variance).
  2. Each week, each player is sampled for availability (injury chance) and
     their fantasy points if active.
  3. Optimal lineup is chosen from available players.
  4. Matchups are run; winners get +1 win.
  5. Top 6 teams make playoffs; single-elim bracket determines champion.

Aggregating across N simulations gives expected wins, win-count CIs,
playoff probability, and championship probability per team.

Per-game distribution choices:
  - mean = ESPN's fpts_2026_proj_avg (preferred) or DS projection / 17
  - sigma = position-specific (QB:7, RB:6, WR:6.5, TE:5)
  - injury rate per week from historical NFL data:
        QB:3%, RB:8%, WR:6%, TE:7%

Random seed is fixed (42) so identical inputs produce identical outputs —
deterministic for cache stability across requests within the rankings TTL.
"""
import random
from dynasty_data import normalize_name

INJURY_RATE = {"QB": 0.03, "RB": 0.08, "WR": 0.06, "TE": 0.07}
POS_SIGMA = {"QB": 7.0, "RB": 6.0, "WR": 6.5, "TE": 5.0}
_LINEUP_POSITIONS = ("QB", "RB", "WR", "TE")


def _player_mean_ppg(player: dict, rankings: dict) -> float:
    """Get a player's projected fantasy points per game for 2026.

    Priority:
      1. ESPN per-game projection (fpts_2026_proj_avg)
      2. ESPN season total / 17
      3. DraftSharks 1-year projection / 17
      4. Estimate from combined dynasty value (Allen ~22 ppg @ 9999)
    """
    val = player.get("fpts_2026_proj_avg", 0) or 0
    if val > 0:
        return float(val)
    total = player.get("fpts_2026_proj", 0) or 0
    if total > 0:
        return total / 17.0
    info = rankings.get(normalize_name(player.get("name", "")), {})
    ds_1yr = info.get("ds_proj_1yr", 0) or 0
    if ds_1yr > 0:
        return ds_1yr / 17.0
    combined = info.get("combined", 0) or 0
    if combined > 0:
        return max(2.0, combined / 454.0)  # Allen ~22 ppg at 9999
    return 2.0


def _build_player_dists(roster: list, rankings: dict) -> list:
    """One {mean, sigma, pos, injury_rate} per skill-position player."""
    out = []
    for p in roster:
        pos = p.get("position", "")
        if pos not in _LINEUP_POSITIONS:
            continue
        mean = _player_mean_ppg(p, rankings)
        if mean <= 0:
            continue
        out.append({
            "name":    p.get("name", ""),
            "pos":     pos,
            "mean":    mean,
            "sigma":   POS_SIGMA.get(pos, 6.0),
            "inj":     INJURY_RATE.get(pos, 0.07),
        })
    return out


def _sim_week_points(team_players: list, rng: random.Random) -> float:
    """Sample availability + points for each player, pick optimal lineup."""
    available = []
    for p in team_players:
        if rng.random() < p["inj"]:
            continue  # injured this week
        pts = rng.normalvariate(p["mean"], p["sigma"])
        if pts < 0:
            pts = 0.0
        available.append({"pos": p["pos"], "pts": pts})

    by_pos = {pos: [] for pos in _LINEUP_POSITIONS}
    for a in available:
        by_pos[a["pos"]].append(a)
    for pos in _LINEUP_POSITIONS:
        by_pos[pos].sort(key=lambda x: -x["pts"])

    used = set()
    total = 0.0

    def _take(pos: str) -> bool:
        for idx, p in enumerate(by_pos[pos]):
            if id(p) in used:
                continue
            used.add(id(p))
            nonlocal_add(p["pts"])
            return True
        return False

    # Use a list to allow closure mutation under "nonlocal" semantics
    box = [0.0]
    def nonlocal_add(v):
        box[0] += v

    # Required slots: 1 QB, 2 RB, 3 WR, 1 TE
    slot_plan = [("QB", 1), ("RB", 2), ("WR", 3), ("TE", 1)]
    for pos, n in slot_plan:
        for _ in range(n):
            _take(pos)

    # FLEX: best remaining RB/WR/TE
    flex_pool = [(p["pts"], p) for pos in ("RB", "WR", "TE") for p in by_pos[pos] if id(p) not in used]
    if flex_pool:
        flex_pool.sort(key=lambda x: -x[0])
        used.add(id(flex_pool[0][1]))
        nonlocal_add(flex_pool[0][0])

    # OP/SF: best remaining any position (usually a QB)
    op_pool = [(p["pts"], p) for pos in _LINEUP_POSITIONS for p in by_pos[pos] if id(p) not in used]
    if op_pool:
        op_pool.sort(key=lambda x: -x[0])
        nonlocal_add(op_pool[0][0])

    return box[0]


def _round_robin_schedule(team_ids: list, weeks: int, rng: random.Random) -> list:
    """Build `weeks` rounds of random pairings (each team plays once per week)."""
    schedule = []
    for _ in range(weeks):
        shuffled = list(team_ids)
        rng.shuffle(shuffled)
        pairs = []
        for i in range(0, len(shuffled) - 1, 2):
            pairs.append((shuffled[i], shuffled[i + 1]))
        schedule.append(pairs)
    return schedule


def simulate_season(
    teams: list,
    rankings: dict,
    num_sims: int = 1000,
    num_weeks: int = 14,            # regular-season weeks before playoffs
    num_playoff_teams: int = 6,
    seed: int = 42,
) -> list[dict]:
    """
    Run Monte Carlo. Returns list of dicts (one per team) with:
        team_id, expected_wins, wins_p05, wins_p95,
        playoff_pct, championship_pct
    """
    rng = random.Random(seed)
    team_ids = [t["id"] for t in teams]
    team_players = {t["id"]: _build_player_dists(t.get("roster", []), rankings) for t in teams}

    wins_history = {tid: [] for tid in team_ids}
    playoff_count = {tid: 0 for tid in team_ids}
    champ_count = {tid: 0 for tid in team_ids}

    for _ in range(num_sims):
        sim_wins = {tid: 0 for tid in team_ids}
        sim_pf = {tid: 0.0 for tid in team_ids}  # points for (tiebreaker)
        schedule = _round_robin_schedule(team_ids, num_weeks, rng)
        for week_pairs in schedule:
            for t1, t2 in week_pairs:
                p1 = _sim_week_points(team_players[t1], rng)
                p2 = _sim_week_points(team_players[t2], rng)
                sim_pf[t1] += p1
                sim_pf[t2] += p2
                if p1 > p2:
                    sim_wins[t1] += 1
                else:
                    sim_wins[t2] += 1

        for tid, w in sim_wins.items():
            wins_history[tid].append(w)

        # Playoff seeding: by wins, then by points-for (random tiebreak after)
        sorted_teams = sorted(
            team_ids,
            key=lambda tid: (-sim_wins[tid], -sim_pf[tid], rng.random()),
        )
        playoff_teams = sorted_teams[:num_playoff_teams]
        for tid in playoff_teams:
            playoff_count[tid] += 1

        # Single-elim bracket (3 rounds for 6-team field with byes for top 2)
        # Round 1: seeds 3-6 play (3v6, 4v5); top 2 bye
        # Round 2: top 2 play winners of round 1
        # Round 3: championship
        round1_pairs = [(playoff_teams[2], playoff_teams[5]),
                        (playoff_teams[3], playoff_teams[4])]
        winners_r1 = []
        for t1, t2 in round1_pairs:
            p1 = _sim_week_points(team_players[t1], rng)
            p2 = _sim_week_points(team_players[t2], rng)
            winners_r1.append(t1 if p1 > p2 else t2)
        # Semifinals: seed 1 vs lower r1 winner; seed 2 vs higher r1 winner
        sf_pairs = [(playoff_teams[0], winners_r1[1]),
                    (playoff_teams[1], winners_r1[0])]
        finalists = []
        for t1, t2 in sf_pairs:
            p1 = _sim_week_points(team_players[t1], rng)
            p2 = _sim_week_points(team_players[t2], rng)
            finalists.append(t1 if p1 > p2 else t2)
        # Final
        p1 = _sim_week_points(team_players[finalists[0]], rng)
        p2 = _sim_week_points(team_players[finalists[1]], rng)
        champion = finalists[0] if p1 > p2 else finalists[1]
        champ_count[champion] += 1

    # Aggregate per team
    results = []
    for tid in team_ids:
        wins = sorted(wins_history[tid])
        n = len(wins)
        if n == 0:
            continue
        results.append({
            "team_id":          tid,
            "expected_wins":    round(sum(wins) / n, 1),
            "wins_p05":         wins[max(0, int(n * 0.05))],
            "wins_p95":         wins[min(n - 1, int(n * 0.95))],
            "playoff_pct":      round(100 * playoff_count[tid] / num_sims, 1),
            "championship_pct": round(100 * champ_count[tid] / num_sims, 1),
        })
    return results
