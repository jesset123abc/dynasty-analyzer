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
    Maps fantasy-relevant players to their current NFL teams.
    """
    players_data = _fetch_sleeper()
    if not players_data:
        return ""

    # Build a quick lookup by normalized name
    nfl_lookup = {}
    for pid, p in players_data.items():
        full = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not full.strip():
            continue
        norm = normalize_name(full)
        team = p.get("team") or "FA"
        pos = p.get("position", "")
        status = p.get("status", "")
        injury = p.get("injury_status")
        nfl_lookup[norm] = {
            "team": team, "pos": pos, "status": status, "injury": injury
        }

    # Get all players from fantasy rosters
    lines = []
    seen = set()
    for team in fantasy_rosters:
        for player in team.get("roster", []):
            norm = normalize_name(player["name"])
            if norm in seen:
                continue
            seen.add(norm)
            nfl_info = nfl_lookup.get(norm)
            if nfl_info:
                injury_str = f" [{nfl_info['injury']}]" if nfl_info.get("injury") else ""
                val = rankings.get(norm, {}).get("combined", 0)
                if val > 1000:  # Only include relevant players
                    lines.append(
                        f"  {player['name']}({player['position']}) → {nfl_info['team']}{injury_str}"
                    )

    if not lines:
        return ""

    lines.sort()
    return "=== CURRENT NFL TEAMS (2026) ===\n" + "\n".join(lines)


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
    Blend KTC values with real production data to create enhanced rankings.

    Season mode:  40% KTC + 30% 2025 actual + 30% 2026 projected
    Dynasty mode: 70% KTC + 15% 2025 actual + 15% 2026 projected

    Returns a new rankings dict with blended 'combined' values.
    """
    # Build Sleeper name→id map and fetch projections
    name_map = _build_sleeper_name_map()
    proj_data = fetch_sleeper_projections()

    # Collect all production data points for normalization
    all_fpts_2025 = []
    all_proj_2026 = []
    player_production = {}  # norm_name -> {fpts_2025, proj_2026}

    for team in fantasy_rosters:
        for player in team.get("roster", []):
            norm = normalize_name(player["name"])
            fpts = player.get("fpts_2025", 0) or 0

            # Get 2026 projection — prefer ESPN (league-specific scoring), fall back to Sleeper
            proj_pts = player.get("fpts_2026_proj", 0) or 0
            if not proj_pts:
                pid = name_map.get(norm)
                if pid:
                    p = proj_data.get(pid, {})
                    proj_pts = p.get("pts_half_ppr", 0) or 0

            player_production[norm] = {
                "fpts_2025": fpts,
                "proj_2026": proj_pts,
            }
            if fpts > 0:
                all_fpts_2025.append(fpts)
            if proj_pts > 0:
                all_proj_2026.append(proj_pts)

    # Find max values for normalization to 0-9999
    max_fpts = max(all_fpts_2025) if all_fpts_2025 else 1
    max_proj = max(all_proj_2026) if all_proj_2026 else 1

    # Set blending weights by mode
    if mode == "season":
        w_ktc, w_actual, w_proj = 0.35, 0.15, 0.50
    else:  # dynasty
        w_ktc, w_actual, w_proj = 0.65, 0.15, 0.20

    # Build enhanced rankings
    enhanced = {}
    for key, info in base_rankings.items():
        entry = dict(info)
        ktc_val = info.get("combined", 0)

        prod = player_production.get(key)
        if prod:
            actual_norm = round(prod["fpts_2025"] / max_fpts * 9999) if max_fpts else 0
            proj_norm = round(prod["proj_2026"] / max_proj * 9999) if max_proj else 0

            blended = round(
                ktc_val * w_ktc
                + actual_norm * w_actual
                + proj_norm * w_proj
            )
            entry["combined"] = blended
            entry["ktc_raw"] = ktc_val
            entry["fpts_2025_norm"] = actual_norm
            entry["proj_2026_norm"] = proj_norm
        # else: keep original KTC-only value (player not on any roster)

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
