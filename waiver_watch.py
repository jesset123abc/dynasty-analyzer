"""
Daily waiver-wire value radar.

Scans the league's free agents for stash-and-flip opportunities (the JK Dobbins /
Rico Dowdle archetype): players with a realistic path to an NFL starting job —
especially RB/QB — or genuine breakout signals. When something real surfaces,
Claude writes a short scouting email and it goes out via Gmail. Quiet days send
nothing.

Run:
    python waiver_watch.py             # normal daily run (emails only on real flags)
    python waiver_watch.py --dry-run   # print instead of email
    python waiver_watch.py --force     # ignore the already-flagged memory
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from espn_data import fetch_league_data, parse_league  # noqa: E402
from dynasty_data import fetch_all_rankings, normalize_name  # noqa: E402
from nfl_tools import _fetch_sleeper, fetch_sleeper_projections, fetch_sleeper_stats, _build_sleeper_name_map  # noqa: E402

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "waiver_seen.json")
TRENDING_URL = "https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=200"
RESCORE_DAYS = 7          # re-flag a player after this many days if still hot
RESCORE_JUMP = 1.3        # ...or sooner if their score jumps 30%+
MIN_SCORE = 55            # flag threshold (0-100ish scale)


def _load_seen() -> dict:
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_seen(seen: dict) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=1)


def gather_candidates() -> tuple[list[dict], dict]:
    """Score every unrostered QB/RB/WR/TE for starting-path + breakout signals."""
    teams = parse_league(fetch_league_data())
    owned = {normalize_name(p["name"]) for t in teams for p in t["roster"]}

    players = _fetch_sleeper()
    projections = fetch_sleeper_projections()
    stats_2025 = fetch_sleeper_stats()
    try:
        rankings = fetch_all_rankings()
    except Exception:
        rankings = {}

    # Map each NFL team's depth-chart STARTER per position, to spot ambiguous rooms
    # (rookie/aging/injury-flagged starter = a room a vet can steal work in — the
    # "Dobbins spelling RJ Harvey" pattern).
    starters: dict[tuple, dict] = {}
    for sp in players.values():
        if isinstance(sp, dict) and sp.get("active") and sp.get("team") and sp.get("depth_chart_order") == 1:
            starters[(sp["team"], sp.get("position"))] = sp

    def room_uncertainty(team: str, pos: str) -> tuple[float, str]:
        s = starters.get((team, pos))
        if not s:
            return 12, f"no established {pos}1 on {team}"
        reasons = []
        pts = 0.0
        if (s.get("years_exp") or 9) <= 1:
            pts += 10; reasons.append(f"{pos}1 {s.get('full_name')} is a rookie/soph")
        if (s.get("age") or 0) >= 29:
            pts += 8; reasons.append(f"{pos}1 {s.get('full_name')} is {s.get('age')}")
        if s.get("injury_status") in ("IR", "Out", "PUP", "Questionable", "Doubtful"):
            pts += 10; reasons.append(f"{pos}1 {s.get('full_name')} is {s.get('injury_status')}")
        return pts, "; ".join(reasons)

    trending: dict[str, int] = {}
    try:
        for row in requests.get(TRENDING_URL, timeout=15).json():
            trending[str(row.get("player_id"))] = int(row.get("count", 0))
    except Exception:
        pass
    max_trend = max(trending.values()) if trending else 1

    candidates = []
    for pid, p in players.items():
        if not isinstance(p, dict) or not p.get("active"):
            continue
        pos = p.get("position")
        if pos not in ("QB", "RB", "WR", "TE") or not p.get("team"):
            continue
        name = p.get("full_name") or ""
        nkey = normalize_name(name)
        if not name or nkey in owned:
            continue

        age = p.get("age") or 0
        depth = p.get("depth_chart_order") or 9
        proj = 0.0
        prow = projections.get(pid) or {}
        proj = float(prow.get("pts_half_ppr") or prow.get("pts_ppr") or 0)
        trend = trending.get(str(pid), 0)
        ktc = (rankings.get(nkey) or {}).get("combined", 0)

        # --- opportunity score ---
        score = 0.0
        tags = []
        if depth == 1:
            score += 40          # already listed as the starter and nobody owns him
        elif depth == 2:
            score += 22          # one injury / camp battle from a job
        elif depth == 3:
            score += 8
        score += min(proj / 6.0, 25)                    # 2026 projection weight
        score += (trend / max_trend) * 25 if trend else 0  # market heat
        if age and age <= 24:
            score += 8           # breakout window
        elif age and age >= 29:
            score -= 8           # aging vet, not a flip asset
        if (p.get("years_exp") or 0) <= 1:
            score += 4           # rookie/soph breakout profile

        # Ambiguous-room signal: backups behind shaky starters get a real path bump
        if depth in (2, 3):
            bump, why = room_uncertainty(p.get("team"), pos)
            if bump:
                score += bump
                tags.append(f"ambiguous room: {why}" if why else "unsettled depth chart")

        # Injury-comeback pedigree: real prior pedigree, lost 2025 to injury, healthy now
        srow = stats_2025.get(pid) or {}
        gms_2025 = srow.get("gms_active") or srow.get("gp") or 0
        pedigree = (p.get("search_rank") or 99999) < 400 or ktc > 1500
        healthy_now = p.get("injury_status") in (None, "", "Questionable")
        if pedigree and healthy_now and (p.get("years_exp") or 0) >= 1 and gms_2025 <= 6:
            score += 14
            tags.append(f"injury-return: only {gms_2025} games in 2025, healthy now")

        if pos in ("RB", "QB"):
            score *= 1.3         # Jesse's priority positions
        if p.get("injury_status") in ("IR", "Out", "PUP"):
            score *= 0.6

        if score >= MIN_SCORE - 15:  # keep a wide net; threshold applied after sort
            candidates.append({
                "name": name, "pos": pos, "team": p.get("team"),
                "age": age, "depth": depth, "depth_pos": p.get("depth_chart_position"),
                "proj_2026": round(proj, 1), "trending_adds_24h": trend,
                "dynasty_value": ktc, "years_exp": p.get("years_exp"),
                "injury": p.get("injury_status") or "healthy",
                "games_2025": gms_2025,
                "signals": tags,
                "score": round(score, 1),
            })

    candidates.sort(key=lambda c: -c["score"])
    return candidates, {"owned_count": len(owned)}


def pick_new_flags(candidates: list[dict], force: bool = False) -> list[dict]:
    seen = _load_seen()
    now = datetime.now()
    flags = []
    for c in candidates:
        if c["score"] < MIN_SCORE:
            continue
        prev = seen.get(c["name"])
        if prev and not force:
            fresh = now - datetime.fromisoformat(prev["date"]) < timedelta(days=RESCORE_DAYS)
            hotter = c["score"] >= prev["score"] * RESCORE_JUMP
            if fresh and not hotter:
                continue
        flags.append(c)
        seen[c["name"]] = {"date": now.isoformat(), "score": c["score"]}
        if len(flags) >= 5:
            break
    _save_seen(seen)
    return flags


def write_email(flags: list[dict], context: list[dict]) -> str:
    import anthropic
    client = anthropic.Anthropic()
    watchlist = "\n".join(json.dumps(c) for c in context[:15])
    flagged = "\n".join(json.dumps(c) for c in flags)
    prompt = f"""You are Jesse's dynasty fantasy football waiver-wire scout. His league: 10-team Superflex, 0.5 PPR.
His strategy this year: tanking 2026 for early 2027 picks, so he wants CHEAP STASH-AND-FLIP assets — unrostered
players who could win an NFL starting job (especially RB and QB) or genuinely break out, hold them while they
appreciate, then flip to contenders for picks. Past wins with this play: JK Dobbins, Rico Dowdle (flipped for
what became a 1st), Harold Fannin (had him early, dropped too soon — regrets it). The archetype he wants scouted
hardest: veterans or young players coming off injury, and/or heading into CONTRACT YEARS, who land in rooms with
positional uncertainty — e.g. a proven vet spelling a rookie starter (Dobbins spelling RJ Harvey). Candidates
carry a `signals` field with mechanical flags (ambiguous room, injury-return); layer your own knowledge of
contract situations, coaching changes, and camp news on top of them.

Today's flagged free agents (scored on depth-chart path, ambiguous rooms, injury-return pedigree, 2026
projections, 24h add-trend heat, age):
{flagged}

Wider watchlist for context (not flagged today):
{watchlist}

If you can search the web, verify the top 2-3 with current news (camp reports, contract status, depth-chart
moves) before writing. Write a SHORT email (plain text, no markdown syntax). For each flagged player: 2-3 sharp
sentences — why the starting-job path or breakout is real, the contract/injury angle if there is one, what the
flip window looks like, and a clear verdict (ADD NOW / SPECULATIVE STASH / MONITOR). Rank them. If someone is a
Fannin-type "don't drop this one early" hold, say so. End with one line on anyone from the watchlist worth
watching this week. No fluff, no preamble."""
    def _extract(msg):
        # Web-search runs interleave narration text blocks between tool calls —
        # the finished email is the LAST text block only.
        texts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        text = (texts[-1] if texts else "").strip()
        if text.lower().startswith("subject:"):  # we set our own subject header
            text = text.split("\n", 1)[1].strip() if "\n" in text else text
        return text

    last = None
    for model in ("claude-opus-4-7", "claude-sonnet-4-6"):
        # Prefer web-search-augmented scouting; fall back to knowledge-only.
        try:
            msg = client.messages.create(
                model=model, max_tokens=1400,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
                messages=[{"role": "user", "content": prompt}],
            )
            return _extract(msg)
        except Exception as e:
            last = e
        try:
            msg = client.messages.create(model=model, max_tokens=900, messages=[{"role": "user", "content": prompt}])
            return _extract(msg)
        except Exception as e:
            last = e
    raise last


def send_email(body: str, flags: list[dict]) -> None:
    user, pw = os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASS"]
    to = os.environ.get("WAIVER_EMAIL_TO", user)
    names = ", ".join(f"{f['name']} ({f['pos']})" for f in flags[:3])
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Waiver Radar — {names}"
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def main() -> None:
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    candidates, meta = gather_candidates()
    flags = pick_new_flags(candidates, force=force)
    print(f"[waiver_watch] {datetime.now():%Y-%m-%d %H:%M} scanned (owned={meta['owned_count']}), "
          f"candidates={len(candidates)}, new flags={len(flags)}")
    if not flags:
        print("[waiver_watch] no real opportunities today — no email.")
        return
    body = write_email(flags, candidates)
    if dry:
        print("\n--- EMAIL (dry run) ---\n" + body)
        return
    send_email(body, flags)
    print(f"[waiver_watch] emailed {len(flags)} flags: " + ", ".join(f["name"] for f in flags))


if __name__ == "__main__":
    main()
