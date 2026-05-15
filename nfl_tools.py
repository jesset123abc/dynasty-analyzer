"""
NFL roster data from Sleeper API, cached daily.

Provides current NFL team assignments for players so the advisor
knows real-world situations (trades, free agency, etc.).
"""
import os
import json
import time

import requests

from dynasty_data import normalize_name

SLEEPER_URL = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_STATS_URL = "https://api.sleeper.app/v1/stats/nfl/regular/2025"
SLEEPER_PROJ_URL = "https://api.sleeper.app/v1/projections/nfl/regular/2026"
CACHE_FILE = os.path.join(os.path.dirname(__file__), "sleeper_cache.json")
STATS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "sleeper_stats_cache.json")
PROJ_CACHE_FILE = os.path.join(os.path.dirname(__file__), "sleeper_proj_cache.json")
CACHE_TTL = 86400  # 24 hours

_sleeper_cache: dict = {"data": None, "ts": 0.0}
_stats_cache: dict = {"data": None, "ts": 0.0}
_proj_cache: dict = {"data": None, "ts": 0.0}


def _fetch_sleeper() -> dict:
    """Fetch all NFL players from Sleeper, cache to disk."""
    global _sleeper_cache
    now = time.time()

    # Check memory cache
    if _sleeper_cache["data"] and (now - _sleeper_cache["ts"]) < CACHE_TTL:
        return _sleeper_cache["data"]

    # Check disk cache
    try:
        if os.path.exists(CACHE_FILE):
            mtime = os.path.getmtime(CACHE_FILE)
            if (now - mtime) < CACHE_TTL:
                with open(CACHE_FILE, "r") as f:
                    data = json.load(f)
                _sleeper_cache = {"data": data, "ts": now}
                return data
    except Exception:
        pass

    # Fetch from API
    r = requests.get(SLEEPER_URL, timeout=30)
    r.raise_for_status()
    data = r.json()

    # Save to disk
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

    _sleeper_cache = {"data": data, "ts": now}
    return data


def get_player_nfl_team(player_name: str) -> dict | None:
    """Look up a player's current NFL team and status."""
    players = _fetch_sleeper()
    norm = normalize_name(player_name)

    for pid, p in players.items():
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if normalize_name(full) == norm:
            return {
                "name": full,
                "team": p.get("team") or "Free Agent",
                "position": p.get("position", ""),
                "status": p.get("status", ""),
                "injury_status": p.get("injury_status"),
                "age": p.get("age"),
                "years_exp": p.get("years_exp"),
            }
    return None


def get_nfl_team_roster(team_abbrev: str) -> list[dict]:
    """Get all skill-position players on an NFL team's roster."""
    players = _fetch_sleeper()
    abbrev = team_abbrev.upper()
    skill_positions = {"QB", "RB", "WR", "TE", "K"}

    roster = []
    for pid, p in players.items():
        if p.get("team") == abbrev and p.get("position") in skill_positions:
            full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            roster.append({
                "name": full,
                "position": p.get("position", ""),
                "status": p.get("status", ""),
                "age": p.get("age"),
            })

    roster.sort(key=lambda x: (
        ["QB", "RB", "WR", "TE", "K"].index(x["position"])
        if x["position"] in ["QB", "RB", "WR", "TE", "K"] else 99
    ))
    return roster


def build_nfl_context(fantasy_rosters: list, rankings: dict) -> str:
    """
    Build compact NFL context for the advisor system prompt.
    Maps fantasy-relevant players to their current NFL teams, depth-chart
    position, and structured injury data (body part + start date).
    """
    players_data = _fetch_sleeper()
    if not players_data:
        return ""

    nfl_lookup = {}
    for pid, p in players_data.items():
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not full:
            continue
        nfl_lookup[normalize_name(full)] = {
            "team":         p.get("team") or "FA",
            "pos":          p.get("position", ""),
            "status":       p.get("status", ""),
            "injury":       p.get("injury_status"),
            "injury_body":  p.get("injury_body_part"),
            "injury_start": p.get("injury_start_date"),
            "practice":     p.get("practice_participation"),
            "dc_pos":       p.get("depth_chart_position"),
            "dc_order":     p.get("depth_chart_order"),
        }

    lines = []
    seen = set()
    for team in fantasy_rosters:
        for player in team.get("roster", []):
            norm = normalize_name(player["name"])
            if norm in seen:
                continue
            seen.add(norm)
            nfl_info = nfl_lookup.get(norm)
            if not nfl_info:
                continue
            val = rankings.get(norm, {}).get("combined", 0)
            if val <= 1000:
                continue

            # Depth chart: e.g. "DC:RB1" or "DC:WR3"
            dc_str = ""
            if nfl_info.get("dc_pos") and nfl_info.get("dc_order"):
                dc_str = f" DC:{nfl_info['dc_pos']}{nfl_info['dc_order']}"

            # Injury detail: e.g. " [Q knee 2026-04-12, LP]"
            inj = nfl_info.get("injury")
            inj_str = ""
            if inj:
                parts = [inj]
                if nfl_info.get("injury_body"):
                    parts.append(nfl_info["injury_body"].lower())
                if nfl_info.get("injury_start"):
                    parts.append(nfl_info["injury_start"])
                if nfl_info.get("practice"):
                    parts.append(nfl_info["practice"])
                inj_str = f" [{' '.join(parts)}]"

            lines.append(
                f"  {player['name']}({player['position']}) → "
                f"{nfl_info['team']}{dc_str}{inj_str}"
            )

    if not lines:
        return ""
    lines.sort()
    return (
        "=== CURRENT NFL TEAMS / DEPTH CHART / INJURIES (2026) ===\n"
        "DC=depth chart slot (e.g. RB1, WR2). Practice codes: DNP/LP/FP. "
        "Injury date is ISO format.\n"
        + "\n".join(lines)
    )


# ── Sleeper stats & projections ───────────────────────────────────────────────

def _fetch_cached(url: str, cache_file: str, cache_dict: dict) -> dict:
    """Generic fetch-with-cache for Sleeper endpoints."""
    now = time.time()
    if cache_dict["data"] and (now - cache_dict["ts"]) < CACHE_TTL:
        return cache_dict["data"]
    try:
        if os.path.exists(cache_file):
            mtime = os.path.getmtime(cache_file)
            if (now - mtime) < CACHE_TTL:
                with open(cache_file, "r") as f:
                    data = json.load(f)
                cache_dict["data"] = data
                cache_dict["ts"] = now
                return data
    except Exception:
        pass

    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    try:
        with open(cache_file, "w") as f:
            json.dump(data, f)
    except Exception:
        pass
    cache_dict["data"] = data
    cache_dict["ts"] = now
    return data


def fetch_sleeper_stats() -> dict:
    """
    Fetch 2025 actual stats from Sleeper.
    Returns {sleeper_player_id: {pts_half_ppr, gp, pass_yd, rush_yd, rec_yd, ...}}
    """
    return _fetch_cached(SLEEPER_STATS_URL, STATS_CACHE_FILE, _stats_cache)


def fetch_sleeper_projections() -> dict:
    """
    Fetch 2026 projected stats from Sleeper.
    Returns {sleeper_player_id: {pts_half_ppr, pass_yd, rush_yd, rec_yd, ...}}
    """
    return _fetch_cached(SLEEPER_PROJ_URL, PROJ_CACHE_FILE, _proj_cache)


def _build_sleeper_name_map() -> dict:
    """Build {normalized_name: sleeper_id} mapping."""
    players = _fetch_sleeper()
    name_map = {}
    for pid, p in players.items():
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if full.strip():
            name_map[normalize_name(full)] = pid
    return name_map


def get_player_stats_and_projections(player_name: str) -> dict | None:
    """Get 2025 stats + 2026 projections for a specific player."""
    name_map = _build_sleeper_name_map()
    norm = normalize_name(player_name)
    pid = name_map.get(norm)
    if not pid:
        return None

    stats_data = fetch_sleeper_stats()
    proj_data = fetch_sleeper_projections()

    result = {"name": player_name}
    s = stats_data.get(pid, {})
    if s:
        result["stats_2025"] = {
            "pts_half_ppr": round(s.get("pts_half_ppr", 0), 1),
            "gp": s.get("gp", 0),
            "pass_yd": s.get("pass_yd", 0),
            "pass_td": s.get("pass_td", 0),
            "rush_yd": s.get("rush_yd", 0),
            "rush_td": s.get("rush_td", 0),
            "rec": s.get("rec", 0),
            "rec_yd": s.get("rec_yd", 0),
            "rec_td": s.get("rec_td", 0),
            "rec_tgt": s.get("rec_tgt", 0),
        }

    p = proj_data.get(pid, {})
    if p:
        result["proj_2026"] = {
            "pts_half_ppr": round(p.get("pts_half_ppr", 0), 1),
            "pass_yd": round(p.get("pass_yd", 0)),
            "pass_td": round(p.get("pass_td", 0), 1),
            "rush_yd": round(p.get("rush_yd", 0)),
            "rush_td": round(p.get("rush_td", 0), 1),
            "rec": round(p.get("rec", 0)),
            "rec_yd": round(p.get("rec_yd", 0)),
            "rec_td": round(p.get("rec_td", 0), 1),
        }

    return result if len(result) > 1 else None


def build_enhanced_rankings(
    base_rankings: dict,
    fantasy_rosters: list,
    mode: str = "dynasty",
) -> dict:
    """
    Blend market values with real production to create enhanced rankings.

    Both axes normalize to 0-9999. Per-game points are used (not season totals)
    so injury-shortened seasons don't unfairly tank a player's value.

    Season mode:  35% market + 15% 2025 per-game actual + 50% 2026 per-game proj
    Dynasty mode: 65% market + 15% 2025 per-game actual + 20% 2026 per-game proj

    "market" is the combined value coming in from base_rankings (i.e., the
    average of KTC + FantasyCalc that fetch_all_rankings already produced).

    Returns a new rankings dict with blended 'combined' values.
    """
    name_map = _build_sleeper_name_map()
    proj_data = fetch_sleeper_projections()

    # Collect per-game production data points for normalization
    all_fpg_2025 = []
    all_proj_pg_2026 = []
    player_production = {}  # norm_name -> {fpg_2025, proj_pg_2026}

    for team in fantasy_rosters:
        for player in team.get("roster", []):
            norm = normalize_name(player["name"])

            # 2025 per-game actuals — prefer ESPN's appliedAverage, else fpts/games
            fpg = player.get("fpts_2025_avg", 0) or 0
            if not fpg:
                total = player.get("fpts_2025", 0) or 0
                gp = player.get("games_played", 0) or 0
                fpg = round(total / gp, 1) if gp > 0 else 0

            # 2026 per-game proj — prefer ESPN avg, else Sleeper total/17
            proj_pg = player.get("fpts_2026_proj_avg", 0) or 0
            if not proj_pg:
                proj_total = player.get("fpts_2026_proj", 0) or 0
                if proj_total:
                    proj_pg = round(proj_total / 17, 1)  # full-season denom
                else:
                    pid = name_map.get(norm)
                    if pid:
                        p = proj_data.get(pid, {})
                        proj_total = p.get("pts_half_ppr", 0) or 0
                        if proj_total:
                            proj_pg = round(proj_total / 17, 1)

            player_production[norm] = {"fpg_2025": fpg, "proj_pg_2026": proj_pg}
            if fpg > 0:
                all_fpg_2025.append(fpg)
            if proj_pg > 0:
                all_proj_pg_2026.append(proj_pg)

    max_fpg = max(all_fpg_2025) if all_fpg_2025 else 1
    max_proj_pg = max(all_proj_pg_2026) if all_proj_pg_2026 else 1

    if mode == "season":
        w_market, w_actual, w_proj = 0.35, 0.15, 0.50
    else:  # dynasty
        w_market, w_actual, w_proj = 0.65, 0.15, 0.20

    enhanced = {}
    for key, info in base_rankings.items():
        entry = dict(info)
        market_val = info.get("combined", 0)

        prod = player_production.get(key)
        if prod:
            actual_norm = round(prod["fpg_2025"] / max_fpg * 9999) if max_fpg else 0
            proj_norm = round(prod["proj_pg_2026"] / max_proj_pg * 9999) if max_proj_pg else 0

            blended = round(
                market_val * w_market
                + actual_norm * w_actual
                + proj_norm * w_proj
            )
            entry["combined"] = blended
            entry["market_raw"] = market_val
            entry["fpg_2025_norm"] = actual_norm
            entry["proj_pg_2026_norm"] = proj_norm
            entry["fpg_2025"] = prod["fpg_2025"]
            entry["proj_pg_2026"] = prod["proj_pg_2026"]
        # else: keep original market value (player not on any roster)

        enhanced[key] = entry

    return enhanced


def build_production_context(fantasy_rosters: list) -> str:
    """
    Build compact production context: 2025 actual + 2026 projected fantasy points
    for all rostered players. Uses ESPN 2025 actuals from roster data + Sleeper projections.
    """
    # Build Sleeper name map and fetch projections
    name_map = _build_sleeper_name_map()
    proj_data = fetch_sleeper_projections()

    lines = []
    seen = set()
    for team in fantasy_rosters:
        for player in team.get("roster", []):
            norm = normalize_name(player["name"])
            if norm in seen:
                continue
            seen.add(norm)

            fpts = player.get("fpts_2025", 0)
            gp = player.get("games_played", 0)
            avg = player.get("fpts_2025_avg", 0)

            # Get 2026 projection — prefer ESPN, fall back to Sleeper
            proj_pts = player.get("fpts_2026_proj", 0) or 0
            proj_avg = player.get("fpts_2026_proj_avg", 0) or 0
            proj_src = "ESPN"
            if not proj_pts:
                pid = name_map.get(norm)
                if pid:
                    p = proj_data.get(pid, {})
                    proj_pts = round(p.get("pts_half_ppr", 0), 1)
                    proj_src = "Sleeper"

            # Only include players with meaningful data
            if fpts > 20 or proj_pts > 20:
                proj_str = f" Proj26:{proj_pts}({proj_src})" if proj_pts > 0 else ""
                gp_str = f" GP:{gp}" if gp > 0 else ""
                avg_str = f" Avg:{avg}" if avg > 0 else ""
                lines.append(
                    f"  {player['name']}({player['position']}) "
                    f"FP25:{fpts}{avg_str}{gp_str}{proj_str}"
                )

    if not lines:
        return ""

    lines.sort()
    return "=== PLAYER PRODUCTION (2025 actual + 2026 projected, half-PPR) ===\n" + "\n".join(lines)
