"""
Fetches dynasty player values from KeepTradeCut (KTC).

Values are scraped from keeptradecut.com/dynasty-rankings (Superflex format)
and normalized to a 0–9999 scale (top player = 9999).

Results are cached for 15 minutes to avoid hammering the site on every request.
"""
import re
import json
import time
import unicodedata

import requests

# ── API endpoint ──────────────────────────────────────────────────────────────

KTC_URL = "https://keeptradecut.com/dynasty-rankings"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}

# ── 15-minute cache ───────────────────────────────────────────────────────────

_cache: dict = {"data": None, "ts": 0.0}
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


# ── KTC fetcher ───────────────────────────────────────────────────────────────

def _fetch_ktc() -> dict:
    """
    Scrape KTC Superflex dynasty values from the JS variable on the page.
    Returns {norm_name: {name, combined, ktc, rank, pos_rank, age, position}}
    """
    r = requests.get(KTC_URL, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    players = _extract_js_array(r.text, "playersArray")
    if not players:
        return {}

    # Find max value for normalization to 0-9999
    max_val = max((p.get("superflexValues", {}).get("value", 0) for p in players), default=1)
    if max_val == 0:
        max_val = 1

    result = {}
    for p in players:
        sv  = p.get("superflexValues", {})
        raw_val = sv.get("value", 0)
        key = _normalize(p["playerName"])
        norm_val = round(raw_val / max_val * 9999)
        result[key] = {
            "name":     p["playerName"],
            "combined": norm_val,
            "ktc":      norm_val,
            "rank":     sv.get("rank", 999),
            "pos_rank": sv.get("positionalRank", 999),
            "age":      round(p.get("age", 0), 1),
            "position": p.get("position", ""),
        }
    return result


# ── Combined rankings (main public entry point) ──────────────────────────────

def fetch_all_rankings() -> dict:
    """
    Fetch KTC dynasty values, normalize to 0-9999 scale.

    Returns dict keyed by normalized player name:
      {
        "combined":      int,   # normalized value (0-9999)
        "ktc":           int,   # same as combined (KTC-only)
        "rank":          int,   # overall rank by value
        "pos_rank":      int,   # positional rank by value
        "age":           float,
        "position":      str,
      }
    """
    global _cache
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    result = _fetch_ktc()

    # Re-rank by combined score (should already be ranked but ensure consistency)
    sorted_players = sorted(
        result.items(), key=lambda x: x[1]["combined"], reverse=True
    )
    pos_counters: dict[str, int] = {}
    for new_rank, (key, info) in enumerate(sorted_players, 1):
        result[key]["rank"] = new_rank
        pos = info["position"]
        if pos:
            pos_counters[pos] = pos_counters.get(pos, 0) + 1
            result[key]["pos_rank"] = pos_counters[pos]

    _cache["data"] = result
    _cache["ts"]   = now
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


def build_rankings_summary(rankings: dict) -> str:
    """
    Top-40 dynasty values to inject into the Claude prompt.
    """
    if not rankings:
        return "(Rankings unavailable — falling back to positional reasoning)"

    top = sorted(
        rankings.items(),
        key=lambda x: x[1].get("combined", 0),
        reverse=True
    )[:40]

    header = "TOP 40 DYNASTY VALUES (KTC Superflex, normalized 0-9999):"

    lines = [header]
    for name_key, info in top:
        age_str = f", age {info['age']:.0f}" if info.get("age") else ""
        lines.append(
            f"  VAL:{info['combined']:>5}  #{info['rank']:>3} {info['position']:<3}  "
            f"{name_key.title()}{age_str}"
        )
    return "\n".join(lines)
