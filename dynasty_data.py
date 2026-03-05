"""
Fetches dynasty player values from three sources:
  - KTC (KeepTradeCut)    — scraped from keeptradecut.com/dynasty-rankings
  - Dynasty Daddy         — public API (dynastydaddy.gg)
  - FantasyCalc           — public API (fantasycalc.com)

Each source is normalized to a 0–9999 scale (top player = 9999), then averaged
into a single combined score. Players missing from one source are averaged from
the remaining two. All 3 sources are fetched in parallel.

Results are cached for 15 minutes to avoid hammering APIs on every request.
"""
import re
import json
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ── API endpoints ─────────────────────────────────────────────────────────────

KTC_URL = "https://keeptradecut.com/dynasty-rankings"
DD_URL  = "https://api.dynastydaddy.gg/api/1.0/players/values?format=SF"
FC_URL  = "https://api.fantasycalc.com/values/current?isDynasty=true&numQbs=2"

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
# Picks are not available in Dynasty Daddy / FantasyCalc at pick-slot granularity,
# so these remain KTC-calibrated and are intentionally not merged into the combined score.
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


def _normalize_source(raw: dict, value_key: str) -> dict:
    """
    Normalize player values within a source dict to the 0–9999 scale.
    The player with the highest value in the source maps to 9999.
    Returns a new dict with an added 'norm' key on each entry.
    """
    if not raw:
        return {}
    max_val = max((p.get(value_key, 0) for p in raw.values()), default=0)
    if max_val == 0:
        return raw
    return {
        k: {**v, "norm": round(v.get(value_key, 0) / max_val * 9999)}
        for k, v in raw.items()
    }


# Public alias so other modules can normalize names for ranking lookups
normalize_name = _normalize

# ── Per-source fetchers (called in parallel) ──────────────────────────────────

def _fetch_ktc() -> dict:
    """
    Scrape KTC Superflex dynasty values from the JS variable on the page.
    Returns {norm_name: {ktc, rank, pos_rank, age, position}}
    """
    r = requests.get(KTC_URL, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    players = _extract_js_array(r.text, "playersArray")
    if not players:
        return {}
    result = {}
    for p in players:
        sv  = p.get("superflexValues", {})
        key = _normalize(p["playerName"])
        result[key] = {
            "name":     p["playerName"],
            "ktc":      sv.get("value", 0),
            "rank":     sv.get("rank", 999),
            "pos_rank": sv.get("positionalRank", 999),
            "age":      round(p.get("age", 0), 1),
            "position": p.get("position", ""),
        }
    return result


def _fetch_dynasty_daddy() -> dict:
    """
    Fetch Dynasty Daddy SF values from the public API.
    Returns {norm_name: {dd, rank, pos_rank, age, position}}
    """
    r = requests.get(DD_URL, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    result     = {}
    pos_counts = {}
    for rank_idx, p in enumerate(data, 1):
        name = p.get("playerName") or p.get("name", "")
        if not name:
            continue
        pos = p.get("position", "")
        pos_counts[pos] = pos_counts.get(pos, 0) + 1
        key = _normalize(name)
        result[key] = {
            "name":     name,
            "dd":       p.get("value", 0),
            "rank":     rank_idx,
            "pos_rank": pos_counts[pos],
            "age":      float(p.get("age") or 0),
            "position": pos,
        }
    return result


def _fetch_fantasycalc() -> dict:
    """
    Fetch FantasyCalc 2QB/SF dynasty values from the public API.
    Returns {norm_name: {fc, rank, pos_rank, age, position}}
    """
    r = requests.get(FC_URL, headers=_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = {}
    for rank_idx, entry in enumerate(data, 1):
        player = entry.get("player", {})
        name   = player.get("name", "")
        if not name:
            continue
        key = _normalize(name)
        result[key] = {
            "name":     name,
            "fc":       entry.get("value", 0),
            "rank":     rank_idx,
            "pos_rank": entry.get("positionRank", 999),
            "age":      float(player.get("age") or 0),
            "position": player.get("position", ""),
        }
    return result


# ── Combined rankings (main public entry point) ───────────────────────────────

def fetch_all_rankings() -> dict:
    """
    Fetch KTC, Dynasty Daddy, and FantasyCalc in parallel.
    Normalize each source to 0–9999, then average across available sources.

    Returns dict keyed by normalized player name:
      {
        "combined":      int,   # averaged normalized value (0-9999)
        "ktc":           int|None,
        "dd":            int|None,
        "fc":            int|None,
        "sources_count": int,   # how many sources contributed (1-3)
        "rank":          int,   # overall rank by combined score
        "pos_rank":      int,   # positional rank by combined score
        "age":           float,
        "position":      str,
      }
    """
    global _cache
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    # Fetch all three sources in parallel
    source_fns = {
        "ktc": _fetch_ktc,
        "dd":  _fetch_dynasty_daddy,
        "fc":  _fetch_fantasycalc,
    }
    raw: dict[str, dict] = {"ktc": {}, "dd": {}, "fc": {}}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fn): name for name, fn in source_fns.items()}
        for future in as_completed(futures):
            src = futures[future]
            try:
                raw[src] = future.result()
            except Exception:
                raw[src] = {}  # source unavailable — skip gracefully

    # Normalize each source to 0-9999
    ktc_norm = _normalize_source(raw["ktc"], "ktc")
    dd_norm  = _normalize_source(raw["dd"],  "dd")
    fc_norm  = _normalize_source(raw["fc"],  "fc")

    # Merge all player keys from all sources
    all_keys = set(ktc_norm) | set(dd_norm) | set(fc_norm)
    combined: dict = {}

    for key in all_keys:
        vals  = []
        ktc_v = dd_v = fc_v = None

        if key in ktc_norm and ktc_norm[key].get("norm", 0) > 0:
            ktc_v = ktc_norm[key]["norm"]
            vals.append(ktc_v)
        if key in dd_norm and dd_norm[key].get("norm", 0) > 0:
            dd_v = dd_norm[key]["norm"]
            vals.append(dd_v)
        if key in fc_norm and fc_norm[key].get("norm", 0) > 0:
            fc_v = fc_norm[key]["norm"]
            vals.append(fc_v)

        if not vals:
            continue

        # Prefer metadata from KTC, fall back to DD, then FC
        meta = ktc_norm.get(key) or dd_norm.get(key) or fc_norm.get(key) or {}
        combined[key] = {
            "name":          meta.get("name", key.title()),
            "combined":      round(sum(vals) / len(vals)),
            "ktc":           ktc_v,
            "dd":            dd_v,
            "fc":            fc_v,
            "sources_count": len(vals),
            "rank":          999,
            "pos_rank":      999,
            "age":           meta.get("age", 0),
            "position":      meta.get("position", ""),
        }

    # Rank all players by combined score
    sorted_players = sorted(
        combined.items(), key=lambda x: x[1]["combined"], reverse=True
    )
    pos_counters: dict[str, int] = {}
    for new_rank, (key, info) in enumerate(sorted_players, 1):
        combined[key]["rank"] = new_rank
        pos = info["position"]
        if pos:
            pos_counters[pos] = pos_counters.get(pos, 0) + 1
            combined[key]["pos_rank"] = pos_counters[pos]

    _cache["data"] = combined
    _cache["ts"]   = now
    return combined


def fetch_rankings() -> dict:
    """
    Backward-compatible wrapper — returns combined rankings.
    The 'ktc' key in each entry reflects the normalized KTC component
    (not the raw KTC value). Use 'combined' for trade math.
    """
    return fetch_all_rankings()


# ── Annotation helpers ────────────────────────────────────────────────────────

def annotate_player(player: dict, rankings: dict) -> str:
    """
    Returns 'Name (VAL:5890 #32 QB | age 26 · KTC/DD/FC)' with source indicators,
    or 'Name (POS)' if the player isn't in any ranking source.
    """
    name = player["name"]
    pos  = player["position"]
    key  = _normalize(name)
    info = rankings.get(key)

    if not info:
        return f"{name} ({pos})"

    val = info.get("combined") or info.get("ktc") or 0
    if not val:
        return f"{name} ({pos})"

    age_str = f" | age {info['age']:.0f}" if info.get("age") else ""

    # Source indicator: shows which of the 3 sources contributed
    src_parts = []
    if info.get("ktc"):  src_parts.append("KTC")
    if info.get("dd"):   src_parts.append("DD")
    if info.get("fc"):   src_parts.append("FC")
    src_str = f" [{'/'.join(src_parts)}]" if len(src_parts) < 3 else ""

    return (
        f"{name} (VAL:{val} #{info['rank']} {info['position']}"
        f"{age_str}{src_str})"
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
    Top-40 combined dynasty values to inject into the Claude prompt.
    Shows combined score + per-source breakdown for transparency.
    """
    if not rankings:
        return "(Rankings unavailable — falling back to positional reasoning)"

    top = sorted(
        rankings.items(),
        key=lambda x: x[1].get("combined", 0),
        reverse=True
    )[:40]

    # Detect which sources actually loaded
    has_ktc = any(v.get("ktc") for _, v in top)
    has_dd  = any(v.get("dd")  for _, v in top)
    has_fc  = any(v.get("fc")  for _, v in top)
    sources = [s for s, ok in [("KTC", has_ktc), ("DynastyDaddy", has_dd), ("FantasyCalc", has_fc)] if ok]
    header  = f"TOP 40 COMBINED DYNASTY VALUES ({' + '.join(sources)}, normalized 0-9999):"

    lines = [header]
    for name_key, info in top:
        age_str = f", age {info['age']:.0f}" if info.get("age") else ""
        ktc_str = f" KTC:{info['ktc']}"        if info.get("ktc") else " KTC:—"
        dd_str  = f" DD:{info['dd']}"          if info.get("dd")  else " DD:—"
        fc_str  = f" FC:{info['fc']}"          if info.get("fc")  else " FC:—"
        lines.append(
            f"  VAL:{info['combined']:>5}  #{info['rank']:>3} {info['position']:<3}  "
            f"{name_key.title()}{age_str} |{ktc_str}{dd_str}{fc_str}"
        )
    return "\n".join(lines)
