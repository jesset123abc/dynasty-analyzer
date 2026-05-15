"""
Fetches dynasty player values from KeepTradeCut (KTC) + FantasyCalc (FC).

Two sources are triangulated:
- KTC Superflex (scraped from JS on keeptradecut.com/dynasty-rankings)
- FantasyCalc Superflex 10-team 0.5 PPR (free JSON API)

Both are normalized to 0-9999 and averaged into a "combined" value.

Also exposes:
- KTC rookie-only rankings (live), used to refresh ROOKIES_2026 values
- KTC pick market values (Early/Mid/Late 1st-4th), used as live fallback
  for hardcoded slot-specific PICK_VALUES.

Results are cached for 15 minutes.
"""
import re
import json
import time
import unicodedata

import requests

# ── Endpoints ─────────────────────────────────────────────────────────────────

KTC_URL = "https://keeptradecut.com/dynasty-rankings"
KTC_ROOKIE_URL = "https://keeptradecut.com/dynasty-rankings/rookie-rankings"
FANTASYCALC_URL = (
    "https://api.fantasycalc.com/values/current"
    "?isDynasty=true&numQbs=2&numTeams=10&ppr=0.5"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}

# ── 15-minute caches ──────────────────────────────────────────────────────────

_cache: dict = {"data": None, "ts": 0.0}
_rookie_cache: dict = {"data": None, "ts": 0.0}
_picks_cache: dict = {"data": None, "ts": 0.0}
CACHE_TTL = 900  # seconds


# ---------------------------------------------------------------------------
# Draft pick values — Superflex format, March 2026
# Picks are not available at pick-slot granularity on KTC,
# so these are manually calibrated from KTC pick value ranges.
# ---------------------------------------------------------------------------

PICK_VALUES = {
    # 2026 1st round — exact slots confirmed from 2025 final standings (worst → best)
    # Jesse 4-10, Brad 5-9, Alex 5-9, Alexa 6-8, Driscoll 6-8, Berkowitz 6-8,
    # Denton 8-6, Lubin 9-5, Schueler 10-4, Patrick 11-3
    "Jesse's 2026 1st":    {"ktc": 7100, "note": "1.01 — Jeremiyah Love (Jesse's own pick)"},
    "Brad's 2026 1st":     {"ktc": 5900, "note": "1.02 — Brad's pick"},
    "Alex's 2026 1st":     {"ktc": 5400, "note": "1.03 — Alex's pick (held by Jesse)"},
    "Alexa's 2026 1st":    {"ktc": 4800, "note": "1.04 — Alexa's pick"},
    "Driscoll's 2026 1st": {"ktc": 4600, "note": "1.05 — Driscoll's pick"},
    "Berkowitz's 2026 1st":{"ktc": 4400, "note": "1.06 — Berkowitz's pick"},
    "Denton's 2026 1st":   {"ktc": 3900, "note": "1.07 — Denton's pick"},
    "Lubin's 2026 1st":    {"ktc": 3500, "note": "1.08 — Lubin's pick"},
    "Schueler's 2026 1st": {"ktc": 3200, "note": "1.09 — Schueler's pick"},
    "Patrick's 2026 1st":  {"ktc": 3000, "note": "1.10 — Patrick's pick (held by Jesse)"},
    # 2026 2nd round
    "Jesse's 2026 2nd":    {"ktc": 2100, "note": "2026 2nd (held by Patrick)"},
    "Schueler's 2026 2nd": {"ktc": 1900, "note": "2026 2nd (held by Berkowitz)"},
    "Driscoll's 2026 2nd": {"ktc": 1700, "note": "2026 2nd (held by Berkowitz)"},
    "Alex's 2026 2nd":     {"ktc": 1800, "note": "2026 2nd (held by Driscoll)"},
    "Patrick's 2026 2nd":  {"ktc": 1300, "note": "2026 2nd (held by Alex)"},
    "Alex's 2026 3rd":     {"ktc": 1400, "note": "2026 3rd (held by Patrick)"},
    "Patrick's 2026 3rd":  {"ktc":  800, "note": "2026 3rd"},
    "Patrick's 2026 4th":  {"ktc":  350, "note": "2026 4th (held by Alex)"},
    "Alex's 2026 4th":     {"ktc":  400, "note": "2026 4th (held by Alexa)"},
    "Alexa's 2026 3rd":    {"ktc":  700, "note": "2026 3rd (held by Brad)"},
    "Brad's 2026 2nd":     {"ktc": 1900, "note": "2026 2nd"},
    "Brad's 2026 3rd":     {"ktc":  750, "note": "2026 3rd"},
    "Brad's 2026 4th":     {"ktc":  320, "note": "2026 4th"},
    "Lubin's 2026 2nd":    {"ktc": 1500, "note": "2026 2nd"},
    "Lubin's 2026 3rd":    {"ktc":  620, "note": "2026 3rd"},
    "Lubin's 2026 4th":    {"ktc":  280, "note": "2026 4th"},
    "Schueler's 2026 3rd": {"ktc":  700, "note": "2026 3rd"},
    "Schueler's 2026 4th": {"ktc":  300, "note": "2026 4th"},
    "Denton's 2026 2nd":   {"ktc": 1600, "note": "2026 2nd"},
    "Denton's 2026 3rd":   {"ktc":  660, "note": "2026 3rd"},
    "Denton's 2026 4th":   {"ktc":  280, "note": "2026 4th"},
    "Berkowitz's 2026 2nd":{"ktc": 1700, "note": "2026 2nd"},
    "Berkowitz's 2026 3rd":{"ktc":  700, "note": "2026 3rd"},
    "Berkowitz's 2026 4th":{"ktc":  290, "note": "2026 4th"},
    "Alexa's 2026 2nd":    {"ktc": 1600, "note": "2026 2nd"},
    "Alexa's 2026 4th":    {"ktc":  310, "note": "2026 4th"},
    # 2027 picks — slot TBD
    "Jesse's 2027 1st":    {"ktc": 4800, "note": "2027 1st (slot TBD)"},
    "Alex's 2027 1st":     {"ktc": 4200, "note": "2027 1st (slot TBD)"},
    "Brad's 2027 1st":     {"ktc": 4200, "note": "2027 1st (slot TBD)"},
    "Driscoll's 2027 1st": {"ktc": 4500, "note": "2027 1st (held by Jesse, slot TBD)"},
    "Lubin's 2027 1st":    {"ktc": 4000, "note": "2027 1st (slot TBD)"},
    "Schueler's 2027 1st": {"ktc": 4000, "note": "2027 1st (slot TBD)"},
    "Denton's 2027 1st":   {"ktc": 4100, "note": "2027 1st (slot TBD)"},
    "Berkowitz's 2027 1st":{"ktc": 4100, "note": "2027 1st (slot TBD)"},
    "Alexa's 2027 1st":    {"ktc": 4200, "note": "2027 1st (slot TBD)"},
    "Patrick's 2027 1st":  {"ktc": 3900, "note": "2027 1st (slot TBD)"},
    "Jesse's 2027 2nd":    {"ktc": 1400, "note": "2027 2nd"},
    "Jesse's 2027 3rd":    {"ktc":  550, "note": "2027 3rd"},
    "Jesse's 2027 4th":    {"ktc":  220, "note": "2027 4th"},
    "Brad's 2027 2nd":     {"ktc": 1300, "note": "2027 2nd"},
    "Brad's 2027 4th":     {"ktc":  200, "note": "2027 4th"},
    "Alexa's 2027 2nd":    {"ktc": 1300, "note": "2027 2nd"},
    "Alexa's 2027 3rd":    {"ktc":  520, "note": "2027 3rd"},
    "Alexa's 2027 4th":    {"ktc":  200, "note": "2027 4th"},
    "Brad's 2027 3rd":     {"ktc":  510, "note": "2027 3rd (held by Alexa)"},
    # 2027 later rounds — generic KTC estimates for missing teams
    "Patrick's 2027 2nd":  {"ktc": 1100, "note": "2027 2nd"},
    "Patrick's 2027 3rd":  {"ktc":  450, "note": "2027 3rd"},
    "Patrick's 2027 4th":  {"ktc":  180, "note": "2027 4th"},
    "Alex's 2027 2nd":     {"ktc": 1200, "note": "2027 2nd"},
    "Alex's 2027 3rd":     {"ktc":  500, "note": "2027 3rd"},
    "Alex's 2027 4th":     {"ktc":  200, "note": "2027 4th"},
    "Lubin's 2027 2nd":    {"ktc": 1150, "note": "2027 2nd"},
    "Lubin's 2027 3rd":    {"ktc":  470, "note": "2027 3rd"},
    "Lubin's 2027 4th":    {"ktc":  190, "note": "2027 4th"},
    "Schueler's 2027 2nd": {"ktc": 1150, "note": "2027 2nd"},
    "Schueler's 2027 3rd": {"ktc":  470, "note": "2027 3rd"},
    "Schueler's 2027 4th": {"ktc":  190, "note": "2027 4th"},
    "Denton's 2027 2nd":   {"ktc": 1200, "note": "2027 2nd"},
    "Denton's 2027 3rd":   {"ktc":  490, "note": "2027 3rd"},
    "Denton's 2027 4th":   {"ktc":  200, "note": "2027 4th"},
    "Berkowitz's 2027 2nd":{"ktc": 1200, "note": "2027 2nd"},
    "Berkowitz's 2027 3rd":{"ktc":  490, "note": "2027 3rd"},
    "Berkowitz's 2027 4th":{"ktc":  200, "note": "2027 4th"},
    "Driscoll's 2027 2nd": {"ktc": 1250, "note": "2027 2nd"},
    "Driscoll's 2027 3rd": {"ktc":  510, "note": "2027 3rd"},
    "Driscoll's 2027 4th": {"ktc":  200, "note": "2027 4th"},
    # Missing 2026 3rd/4th
    "Jesse's 2026 3rd":    {"ktc":  750, "note": "2026 3rd (held by Alex)"},
    "Jesse's 2026 4th":    {"ktc":  320, "note": "2026 4th (held by Alex)"},
    "Driscoll's 2026 3rd": {"ktc":  680, "note": "2026 3rd"},
    "Driscoll's 2026 4th": {"ktc":  290, "note": "2026 4th"},
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Strip accents and name suffixes for fuzzy cross-source matching."""
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = re.sub(r"\s+(Jr\.?|Sr\.?|III|II|IV|V)$", "", name, flags=re.IGNORECASE)
    return name.lower().strip()


def _extract_js_array(html: str, var_name: str):
    """Extract a JS variable array from inline script via bracket counting."""
    marker = f"var {var_name} = ["
    start  = html.find(marker)
    if start == -1:
        return None
    start += len(marker) - 1
    depth  = 0
    for i, ch in enumerate(html[start:], start):
        if   ch == "[": depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return json.loads(html[start:i + 1])
    return None


# Public alias so other modules can normalize names for ranking lookups
normalize_name = _normalize


# ── KTC fetcher (main) ────────────────────────────────────────────────────────

def _fetch_ktc() -> tuple[dict, dict]:
    """
    Scrape KTC Superflex dynasty values. Returns (players, picks).

    players: {norm_name: {name, ktc, rank, pos_rank, age, position}}
    picks: {pick_label: {ktc, position='RDP'}} — e.g. "2026 Early 1st" -> {ktc: 5690}
    """
    r = requests.get(KTC_URL, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    entries = _extract_js_array(r.text, "playersArray")
    if not entries:
        return {}, {}

    max_val = max((p.get("superflexValues", {}).get("value", 0) for p in entries), default=1) or 1

    players: dict = {}
    picks: dict = {}
    for p in entries:
        sv = p.get("superflexValues", {})
        raw_val = sv.get("value", 0)
        norm_val = round(raw_val / max_val * 9999)
        pos = p.get("position", "")
        if pos == "RDP":
            picks[p["playerName"]] = {"ktc": norm_val, "position": pos}
            continue
        key = _normalize(p["playerName"])
        players[key] = {
            "name":     p["playerName"],
            "ktc":      norm_val,
            "ktc_rank": sv.get("rank", 999),
            "ktc_pos_rank": sv.get("positionalRank", 999),
            "age":      round(p.get("age", 0), 1),
            "position": pos,
        }
    return players, picks


# ── FantasyCalc fetcher ───────────────────────────────────────────────────────

def _fetch_fantasycalc() -> dict:
    """
    Fetch FantasyCalc Superflex 10-team 0.5-PPR dynasty values.
    Returns {norm_name: {fc, fc_rank, fc_pos_rank, fc_trend_30d, sleeper_id}}
    """
    try:
        r = requests.get(FANTASYCALC_URL, timeout=15)
        r.raise_for_status()
        rows = r.json()
    except Exception:
        return {}
    if not rows:
        return {}

    max_val = max((row.get("value", 0) for row in rows), default=1) or 1
    result: dict = {}
    for row in rows:
        player = row.get("player", {})
        name = player.get("name", "")
        if not name:
            continue
        norm_val = round(row.get("value", 0) / max_val * 9999)
        result[_normalize(name)] = {
            "fc":           norm_val,
            "fc_rank":      row.get("overallRank", 999),
            "fc_pos_rank":  row.get("positionRank", 999),
            "fc_trend_30d": row.get("trend30Day", 0),
            "sleeper_id":   player.get("sleeperId"),
        }
    return result


# ── KTC rookies (separate page) ───────────────────────────────────────────────

def _fetch_ktc_rookies() -> dict:
    """
    Scrape KTC rookie-only Superflex values from the rookies page.
    Returns {norm_name: {rookie_ktc, rookie_ktc_rank, position, age, nfl_team}}
    """
    global _rookie_cache
    now = time.time()
    if _rookie_cache["data"] and (now - _rookie_cache["ts"]) < CACHE_TTL:
        return _rookie_cache["data"]

    try:
        r = requests.get(KTC_ROOKIE_URL, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        entries = _extract_js_array(r.text, "playersArray")
    except Exception:
        entries = None
    if not entries:
        _rookie_cache = {"data": {}, "ts": now}
        return {}

    max_val = max((p.get("superflexValues", {}).get("value", 0) for p in entries), default=1) or 1

    result: dict = {}
    for p in entries:
        sv = p.get("superflexValues", {})
        norm_val = round(sv.get("value", 0) / max_val * 9999)
        result[_normalize(p["playerName"])] = {
            "name":            p["playerName"],
            "rookie_ktc":      norm_val,
            "rookie_ktc_rank": sv.get("rank", 999),
            "position":        p.get("position", ""),
            "age":             round(p.get("age", 0), 1),
            "nfl_team":        p.get("team", ""),
        }
    _rookie_cache = {"data": result, "ts": now}
    return result


def fetch_rookie_rankings() -> dict:
    """Public accessor for KTC rookie rankings (15-min cached)."""
    return _fetch_ktc_rookies()


def fetch_pick_market() -> dict:
    """
    Public accessor for the live KTC pick market (Early/Mid/Late 1st-4th).
    Pulls from the main rankings page (picks live alongside players, position='RDP').
    """
    global _picks_cache
    now = time.time()
    if _picks_cache["data"] and (now - _picks_cache["ts"]) < CACHE_TTL:
        return _picks_cache["data"]
    _, picks = _fetch_ktc()
    _picks_cache = {"data": picks, "ts": now}
    return picks


# ── Combined rankings (main public entry point) ──────────────────────────────

def fetch_all_rankings() -> dict:
    """
    Fetch KTC + FantasyCalc Superflex dynasty values and average them.

    Both sources normalize to 0-9999. "combined" is the average of available
    source values (KTC-only if FC misses, vice versa).

    Returns dict keyed by normalized player name:
      {
        "name":         str,
        "combined":     int,    # avg of available sources (0-9999)
        "ktc":          int,    # KTC value or 0
        "fc":           int,    # FantasyCalc value or 0
        "sources":      list,   # which sources had this player ['ktc', 'fc']
        "rank":         int,    # overall rank by combined
        "pos_rank":     int,    # positional rank by combined
        "ktc_rank":     int,    # rank within KTC only
        "fc_rank":      int,    # rank within FantasyCalc only
        "fc_trend_30d": int,    # FC 30-day trend (signed)
        "age":          float,
        "position":     str,
        "sleeper_id":   str|None,
      }
    """
    global _cache
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    ktc_players, _picks = _fetch_ktc()
    fc_players = _fetch_fantasycalc()

    # Union of keys across sources
    all_keys = set(ktc_players.keys()) | set(fc_players.keys())

    result: dict = {}
    for key in all_keys:
        k = ktc_players.get(key, {})
        f = fc_players.get(key, {})

        # Average available source values (skip zeros)
        vals = [v for v in (k.get("ktc", 0), f.get("fc", 0)) if v > 0]
        combined = round(sum(vals) / len(vals)) if vals else 0
        sources = []
        if k.get("ktc", 0) > 0: sources.append("ktc")
        if f.get("fc", 0) > 0:  sources.append("fc")

        result[key] = {
            "name":         k.get("name") or key.title(),
            "combined":     combined,
            "ktc":          k.get("ktc", 0),
            "fc":           f.get("fc", 0),
            "sources":      sources,
            "ktc_rank":     k.get("ktc_rank", 999),
            "fc_rank":      f.get("fc_rank", 999),
            "fc_trend_30d": f.get("fc_trend_30d", 0),
            "age":          k.get("age") or 0,
            "position":     k.get("position", ""),
            "sleeper_id":   f.get("sleeper_id"),
        }

    # Re-rank by combined score and assign positional ranks
    sorted_players = sorted(result.items(), key=lambda x: x[1]["combined"], reverse=True)
    pos_counters: dict[str, int] = {}
    for new_rank, (key, info) in enumerate(sorted_players, 1):
        result[key]["rank"] = new_rank
        pos = info["position"]
        if pos:
            pos_counters[pos] = pos_counters.get(pos, 0) + 1
            result[key]["pos_rank"] = pos_counters[pos]

    _cache["data"] = result
    _cache["ts"] = now
    return result


def fetch_rankings() -> dict:
    """Backward-compatible wrapper — returns KTC rankings."""
    return fetch_all_rankings()


# ── Annotation helpers ────────────────────────────────────────────────────────

def annotate_player(player: dict, rankings: dict) -> str:
    """
    Returns 'Name (VAL:5890 #32 QB | age 26)' or 'Name (POS)' if unranked.
    """
    name = player["name"]
    pos  = player["position"]
    key  = _normalize(name)
    info = rankings.get(key)

    if not info:
        return f"{name} ({pos})"

    val = info.get("combined", 0)
    if not val:
        return f"{name} ({pos})"

    age_str = f" | age {info['age']:.0f}" if info.get("age") else ""

    return (
        f"{name} (VAL:{val} #{info['rank']} {info['position']}"
        f"{age_str})"
    )


def enrich_pick_label(pick: str) -> str:
    """Appends KTC value and slot context to a pick label."""
    base = pick.split(" [")[0].split(" (KTC")[0].split(" (VAL")[0].strip()
    pv = PICK_VALUES.get(base)
    if pv:
        return f"{base} (KTC:{pv['ktc']} — {pv['note']})"
    return base


def build_rankings_summary(rankings: dict, limit: int = 40) -> str:
    """
    Top-N dynasty values to inject into the Claude prompt.
    Shows the combined value plus the per-source breakdown (KTC, FC) so the
    model can see source agreement/disagreement at a glance.
    """
    if not rankings:
        return "(Rankings unavailable — falling back to positional reasoning)"

    top = sorted(
        rankings.items(),
        key=lambda x: x[1].get("combined", 0),
        reverse=True,
    )[:limit]

    header = (
        f"TOP {limit} DYNASTY VALUES — combined avg of KTC Superflex + FantasyCalc "
        "SF 10-team 0.5PPR (each 0-9999). KTC/FC columns show source disagreement; "
        "trend30d is FC 30-day movement (positive = rising)."
    )

    lines = [header]
    for name_key, info in top:
        age_str = f", age {info['age']:.0f}" if info.get("age") else ""
        ktc = info.get("ktc", 0)
        fc = info.get("fc", 0)
        trend = info.get("fc_trend_30d", 0)
        trend_str = f" trend30d:{trend:+d}" if trend else ""
        sources = info.get("sources", [])
        src_warn = "" if len(sources) == 2 else f" [src:{','.join(sources) or 'none'}]"
        lines.append(
            f"  VAL:{info['combined']:>5}  #{info.get('rank', 999):>3} {info['position']:<3}  "
            f"{name_key.title()}{age_str}  "
            f"(KTC:{ktc} FC:{fc}{trend_str}{src_warn})"
        )
    return "\n".join(lines)
