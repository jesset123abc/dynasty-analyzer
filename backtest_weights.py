"""
Source-weight backtest using 2025 Sleeper actuals as outcome data.

CAVEAT: A clean backtest would compare each source's pre-2025 rankings to
the 2025 outcome. We don't have historical snapshots, so this uses the
CURRENT rankings (May 2026, post-season) which may have absorbed 2025
results retroactively. Treat the output as directional, not definitive.

For a proper longitudinal backtest, snapshot fetch_all_rankings() to disk
at the start of each fantasy season and re-run this comparison against
that season's final actuals.

Method:
  1. Pull 2025 Sleeper half-PPR points per game.
  2. For each source (KTC, DS), rank all players who have both a source
     value and 2025 production.
  3. Compute Spearman rank correlation between source rank and actual
     2025 fpts/game rank.
  4. Convert correlations to recommended inverse-variance weights:
        w_i ∝ ρ_i² / (1 - ρ_i²)
     A source with perfect correlation gets infinite weight; ρ=0 gets zero.
     This is the optimal linear combination for variance-minimizing
     ensemble forecasts under standard assumptions.
"""
import math
from dynasty_data import normalize_name, fetch_all_rankings
from nfl_tools import fetch_sleeper_stats, _build_sleeper_name_map


def _spearman(ranks_a: list[float], ranks_b: list[float]) -> float:
    """Rank correlation. Inputs are aligned lists of player ranks."""
    n = len(ranks_a)
    if n < 3:
        return 0.0
    d2 = sum((ranks_a[i] - ranks_b[i]) ** 2 for i in range(n))
    rho = 1 - (6 * d2) / (n * (n ** 2 - 1))
    return max(-1.0, min(1.0, rho))


def _to_ranks(values: list[float]) -> list[float]:
    """Convert raw values to dense ranks (1 = highest), with ties averaged."""
    indexed = sorted(range(len(values)), key=lambda i: -values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def run_backtest(min_2025_games: int = 6) -> dict:
    """
    Run the source correlation backtest.

    min_2025_games: filter to players who played at least this many games
                    in 2025 (drops noisy small-sample tail).

    Returns dict with per-source correlations and recommended weights.
    """
    rankings = fetch_all_rankings()
    stats_data = fetch_sleeper_stats()
    name_map = _build_sleeper_name_map()

    # Build aligned arrays of (ktc_val, ds_val, fpts_pg_2025) for each player
    rows = []
    for nkey, info in rankings.items():
        pid = name_map.get(nkey)
        if not pid:
            continue
        s = stats_data.get(pid, {})
        gp = s.get("gp", 0) or 0
        if gp < min_2025_games:
            continue
        fpts_total = s.get("pts_half_ppr", 0) or 0
        if fpts_total <= 0:
            continue
        fpts_pg = fpts_total / gp
        ktc = info.get("ktc", 0)
        ds = info.get("ds", 0)
        rows.append({
            "name": info.get("name") or nkey.title(),
            "ktc":  ktc,
            "ds":   ds,
            "fpts_pg_2025": fpts_pg,
        })

    n = len(rows)
    if n < 10:
        return {"error": f"Only {n} players matched — not enough data"}

    # Filter to players with BOTH source values
    rows_both = [r for r in rows if r["ktc"] > 0 and r["ds"] > 0]
    n_both = len(rows_both)
    if n_both < 10:
        return {"error": f"Only {n_both} players have both sources — not enough data"}

    actual_ranks = _to_ranks([r["fpts_pg_2025"] for r in rows_both])
    ktc_ranks    = _to_ranks([r["ktc"]          for r in rows_both])
    ds_ranks     = _to_ranks([r["ds"]           for r in rows_both])

    rho_ktc = _spearman(ktc_ranks, actual_ranks)
    rho_ds  = _spearman(ds_ranks,  actual_ranks)

    # Inverse-variance weights from rho²/(1-rho²)
    def _w(rho):
        rho2 = rho * rho
        denom = max(1 - rho2, 0.001)
        return rho2 / denom

    w_ktc_raw = _w(rho_ktc)
    w_ds_raw  = _w(rho_ds)
    total = w_ktc_raw + w_ds_raw
    if total <= 0:
        rec_ktc, rec_ds = 0.5, 0.5
    else:
        rec_ktc = w_ktc_raw / total
        rec_ds  = w_ds_raw / total

    # Also compute RMSE-equivalent (lower correlation roughly = larger error)
    # For reporting context
    return {
        "n_players":         n_both,
        "min_games":         min_2025_games,
        "rho_ktc":           round(rho_ktc, 3),
        "rho_ds":            round(rho_ds, 3),
        "recommended_weights": {
            "ds":  round(rec_ds, 3),
            "ktc": round(rec_ktc, 3),
        },
        "current_weights": {
            "ds":  0.75,
            "ktc": 0.25,
        },
        "interpretation": (
            f"Spearman rank correlation with 2025 half-PPR fpts/game across "
            f"{n_both} qualifying players: KTC={rho_ktc:.3f}, DS={rho_ds:.3f}. "
            "Higher = better predictor of 2025 outcomes. Recommended weights "
            "use inverse-variance: w_i ∝ ρ²/(1-ρ²). Note: rankings are "
            "post-2025, so they may have absorbed those outcomes — treat "
            "as directional. For a clean test, snapshot rankings at season "
            "start and re-run against that season's actuals."
        ),
    }
