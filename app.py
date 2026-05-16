import os
import re
import json
import anthropic
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from dotenv import load_dotenv
from espn_data import fetch_league_data, parse_league, build_league_prompt
from dynasty_data import fetch_all_rankings, build_rankings_summary, build_draftsharks_notes_block, normalize_name, PICK_VALUES
from rookies_data import ROOKIES_2026
from power_rankings import compute_power_rankings
from trade_impact import compute_trade_impact, compute_draft_impact, find_partner_team_id, simulate_draft_state
from season_sim import simulate_season
from backtest_weights import run_backtest
from nfl_tools import build_nfl_context, build_production_context, build_enhanced_rankings
import memory_store

load_dotenv()

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

PRIMARY_MODEL = "claude-opus-4-7"
FALLBACK_MODEL = "claude-sonnet-4-6"
MODEL = PRIMARY_MODEL  # legacy alias


def _is_overload(exc: BaseException) -> bool:
    s = str(exc).lower()
    if "overload" in s:
        return True
    code = getattr(exc, "status_code", None)
    return code in (529, 503)


def _messages_create_with_fallback(**kwargs):
    """Call client.messages.create on Opus 4.7, retry on Sonnet 4.6 if overloaded."""
    try:
        return client.messages.create(model=PRIMARY_MODEL, **kwargs)
    except Exception as e:
        if _is_overload(e):
            return client.messages.create(model=FALLBACK_MODEL, **kwargs)
        raise

# ESPN owner first name → draft board display name
OWNER_DISPLAY = {
    "Gz": "Jesse", "Gztz": "Jesse",
    "Patrick": "Patrick", "Alexa": "Alexa",
    "Alex": "Alex", "Bradley": "Brad",
    "Nathaniel": "Lubin", "John": "Schueler",
    "Grant": "Denton", "Sarah": "Driscoll",
    "Jacob": "Jake",
}

# ── Server-side trade value validator ─────────────────────────────────────────
# Generic round-pick estimates (when we can't match a specific named pick)
_ROUND_EST = {"1st": 4000, "2nd": 1700, "3rd": 750, "4th": 350, "5th": 200}


def _rookie_draft_premium(pick_number: int) -> float:
    """15% premium for top 3 picks only. All others at face KTC value.
    Late-round filler is handled by the roster cap instead."""
    if pick_number and pick_number <= 3:
        return 1.10
    return 1.0


def _pick_val(asset: str) -> int | None:
    """Try to match a named pick from PICK_VALUES, then fall back to round estimates."""
    al = asset.lower()
    for name, data in PICK_VALUES.items():
        if name.lower() in al or all(w in al for w in name.lower().split()):
            return data["ktc"]
    # Generic: "2027 1st", "2026 2nd", etc.
    m = re.search(r"(1st|2nd|3rd|4th|5th)", al)
    if m:
        return _ROUND_EST.get(m.group(1), 0)
    return None

def _player_val(asset: str, rankings: dict) -> int | None:
    """Strip position annotation and look up player in combined rankings."""
    name = re.sub(r"\s*\([^)]*\)\s*$", "", asset).strip()
    val = rankings.get(normalize_name(name), {}).get("combined", 0)
    return val if val > 0 else None

def _asset_val(asset: str, rankings: dict) -> int:
    """Best-effort Combined Value for a single trade asset string."""
    v = _pick_val(asset)
    if v is not None:
        return v
    v = _player_val(asset, rankings)
    if v is not None:
        return v
    return 0

def _trade_ratio(trade: dict, rankings: dict) -> float | None:
    """
    Compute give/receive ratio from Claude's reported give_total/receive_total,
    cross-checked against our own asset lookup. Returns None if we can't determine.
    """
    # Prefer Claude's explicit totals when they seem plausible
    try:
        g = float(trade.get("give_total", 0))
        r = float(trade.get("receive_total", 0))
        if g > 0 and r > 0:
            # Cross-check with our own lookup (rough)
            our_g = sum(_asset_val(a, rankings) for a in trade.get("my_team_gives", []))
            our_r = sum(_asset_val(a, rankings) for a in trade.get("i_receive", []))
            # If Claude's totals are wildly different from ours, trust ours
            if our_g > 0 and our_r > 0:
                if abs(g - our_g) / our_g > 0.6 or abs(r - our_r) / our_r > 0.6:
                    g, r = our_g, our_r
            return r / g
    except (TypeError, ZeroDivisionError):
        pass
    # Fall back to our own lookup
    our_g = sum(_asset_val(a, rankings) for a in trade.get("my_team_gives", []))
    our_r = sum(_asset_val(a, rankings) for a in trade.get("i_receive", []))
    if our_g > 0 and our_r > 0:
        return our_r / our_g
    return None

# Mode-based ratio windows (generous tolerance — only catch clear violations)
# ratio = receive_total / give_total
_MODE_WINDOWS = {
    "fair":       (0.80, 1.25),   # close to 1.0; ratio >1.25 means Jesse wins too much
    "aggressive": (0.70, 1.08),   # Jesse gives more → ratio should be <1; >1.08 means Jesse wins
    "overpay":    (0.92, 1.40),   # Jesse receives more → ratio should be >1; <0.92 means Jesse loses
}

def _passes_ratio(trade: dict, style_key: str, rankings: dict) -> bool:
    """Return False if the trade clearly violates the mode's value window."""
    ratio = _trade_ratio(trade, rankings)
    if ratio is None:
        return True  # can't determine → allow through
    lo, hi = _MODE_WINDOWS.get(style_key, (0.0, 9.9))
    return lo <= ratio <= hi

_BAD_GRADES = {"F", "D", "D-", "D+", "C-"}

def _filter_trades(trades: list, style_key: str, rankings: dict) -> list:
    out = []
    for t in trades:
        if t.get("grade", "").upper().strip().rstrip("+").rstrip("-") in _BAD_GRADES:
            continue
        if not _passes_ratio(t, style_key, rankings):
            continue
        out.append(t)
    return out


def _enrich_trades_with_impact(trades: list, teams: list, rankings: dict, my_team_id: int) -> list:
    """Add power ranking impact data to each trade."""
    for t in trades:
        partner_name = t.get("trade_partner") or t.get("trade_partner_owner") or ""
        partner_id = find_partner_team_id(teams, partner_name)
        if not partner_id:
            continue
        give = t.get("my_team_gives", [])
        recv = t.get("i_receive", [])
        if not give or not recv:
            continue
        try:
            impact = compute_trade_impact(teams, rankings, my_team_id, partner_id, give, recv)
            if impact:
                t["impact"] = impact
        except Exception:
            pass
    return trades


# ── Trade style prompt blocks ──────────────────────────────────────────────────
# Injected into the prompt depending on the aggressiveness mode selected by user.
TRADE_STYLES = {
    "fair": {
        "label": "Fair & Balanced",
        "value_rule": (
            "TRADE BALANCE REQUIREMENT — FAIR & BALANCED (STRICT):\n"
            "Both sides must receive within 3% of each other's Combined Value. No exceptions.\n"
            "MATH CHECK (required for every trade before including it):\n"
            "  Jesse gives VAL:X → Jesse receives VAL:Y → ratio Y/X must be between 0.97 and 1.03.\n"
            "  If the ratio falls outside this window, DISCARD the trade and find a different one.\n"
            "Example: Jesse gives 4000, he must receive between 3880 and 4120. NOT 7000. NOT 2000.\n"
            "A ratio of 1.45 (145% of give) is COMPLETELY WRONG for this mode — it would be graded F.\n"
            "Find assets on each side whose total values are genuinely close. Both owners should feel the deal is even."
        ),
        "grade_scale": "A = within 1% (true dead-even), B+ = 1–2%, B = 2–3%, C = 3–5% (outside target, note it), F = >5% (disqualified — do not include this trade)",
        "task_note": "Find genuinely even swaps — no side wins. If you cannot find 6 that fit within 3%, propose fewer rather than violating the requirement.",
    },
    "aggressive": {
        "label": "Aggressive Buy",
        "value_rule": (
            "TRADE BALANCE REQUIREMENT — AGGRESSIVE BUY:\n"
            "Jesse is aggressively buying talent and willing to pay a slight premium to make deals happen.\n"
            "Jesse gives MORE than he receives. The other team wins the value battle — that is the point.\n"
            "MATH CHECK (required for every trade before including it):\n"
            "  Jesse gives VAL:X → Jesse receives VAL:Y → ratio Y/X must be between 0.85 and 0.95.\n"
            "  (Jesse overpays by 5–15%. The other team profits by 5–15%.)\n"
            "  If Y/X > 0.97, Jesse is getting too much — DISCARD the trade.\n"
            "  If Y/X < 0.80, Jesse is overpaying too much — DISCARD the trade.\n"
            "Example: Jesse gives 5000, he should receive between 4250 and 4750.\n"
            "Explain what Jesse WANTS from this deal (positional need, age, upside) and why he'd pay up to get it."
        ),
        "grade_scale": "A = Jesse overpays 8–12% (motivated buyer, great deal for other team), B+ = 5–8% or 12–15%, B = 3–5% (too close to fair), C = <3% overpay (wrong mode), F = Jesse wins value (this is backwards)",
        "task_note": "Jesse is the motivated buyer paying a slight premium. The other team clearly wins value — that's their incentive to deal. Explain what Jesse wants and why it's worth the slight overpay.",
    },
    "overpay": {
        "label": "Overpay Required",
        "value_rule": (
            "TRADE BALANCE REQUIREMENT — OVERPAY REQUIRED:\n"
            "Jesse's assets are premium and he requires the other team to overpay him to acquire them.\n"
            "Jesse receives MORE than he gives. Jesse wins the value battle — that is the point.\n"
            "MATH CHECK (required for every trade before including it):\n"
            "  Jesse gives VAL:X → Jesse receives VAL:Y → ratio Y/X must be between 1.05 and 1.15.\n"
            "  (Jesse profits by 5–15%. The other team overpays by 5–15%.)\n"
            "  If Y/X < 1.03, Jesse isn't getting enough — DISCARD the trade.\n"
            "  If Y/X > 1.20, the deal is too lopsided — no one accepts that — DISCARD.\n"
            "Example: Jesse gives 5000, he should receive between 5250 and 5750.\n"
            "Identify teams who NEED what Jesse has and explain specifically why they'd accept paying a small premium."
        ),
        "grade_scale": "A = Jesse profits 10–15% (strong but realistic premium), B+ = 7–10%, B = 5–7%, C = 3–5% (too close to fair), F = Jesse loses value or profits <3% (wrong mode)",
        "task_note": "Jesse's picks/players are valuable. Find the most motivated buyers who need what Jesse has and would accept paying a 5–15% premium. Be specific about why that owner would still say yes.",
    },
}


@app.route("/")
def index():
    try:
        data = fetch_league_data()
        teams = parse_league(data)
        my_team_id = int(request.args.get("team_id", 8))
        return render_template("index.html", teams=teams, my_team_id=my_team_id, error=None)
    except Exception as e:
        return render_template("index.html", teams=[], my_team_id=8, error=str(e))


# Monte Carlo season-sim cache. Sim takes ~1-2 seconds for 1000 runs;
# cache by team-roster signature for the rankings TTL window (15 min).
_SIM_CACHE: dict = {"key": None, "data": None, "ts": 0.0}
_SIM_TTL = 900  # seconds


def _season_sim_cached(teams: list, rankings: dict) -> dict:
    """Return {team_id: sim_result} dict, cached by roster signature."""
    import time as _t
    # Cheap signature: each team's id + roster name set hash
    sig = tuple((t["id"], len(t.get("roster", []))) for t in teams)
    now = _t.time()
    if _SIM_CACHE["key"] == sig and (now - _SIM_CACHE["ts"]) < _SIM_TTL and _SIM_CACHE["data"]:
        return _SIM_CACHE["data"]
    try:
        results = simulate_season(teams, rankings, num_sims=1000)
        by_id = {r["team_id"]: r for r in results}
        _SIM_CACHE["key"] = sig
        _SIM_CACHE["data"] = by_id
        _SIM_CACHE["ts"] = now
        return by_id
    except Exception:
        return {}


def _load_draft_state_for_sim(teams: list) -> tuple[list, list, dict]:
    """
    Read draft_state.json (written by the draft board) and convert it into the
    (draft_picks, trade_log) format that simulate_draft_state expects.
    Returns (draft_picks, trade_log, summary_dict).
    """
    summary = {"picks_count": 0, "trades_count": 0, "last_pick": None}
    try:
        state_path = os.path.join(os.path.dirname(__file__), STATE_FILE) \
            if not os.path.isabs(STATE_FILE) else STATE_FILE
        with open(state_path) as f:
            draft_state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return [], [], summary

    if not draft_state:
        return [], [], summary

    draft_picks = []
    for entry in draft_state.get("draftLog", []):
        owner = entry.get("teamOwner", "")
        tid = _owner_to_team_id(owner)
        if not tid:
            continue
        draft_picks.append({
            "pick": entry.get("pick"),
            "player_name": entry.get("playerName", ""),
            "pos": entry.get("pos", ""),
            "ktc_est": entry.get("ktcEst", 0),
            "team_id": tid,
        })

    trade_log = []
    for t in (draft_state.get("tradeLog") or []):
        to_id = _owner_to_team_id(t.get("to", ""))
        if to_id:
            trade_log.append({
                "gave": t.get("gave", ""),
                "from_team_id": 8,  # Jesse always the one trading from in this UI
                "to_team_id": to_id,
            })

    summary["picks_count"] = len(draft_picks)
    summary["trades_count"] = len(trade_log)
    if draft_picks:
        last = max(draft_picks, key=lambda p: p.get("pick") or 0)
        summary["last_pick"] = f"#{last.get('pick')} {last.get('player_name')} ({last.get('pos')})"

    return draft_picks, trade_log, summary


@app.route("/api/backtest-weights")
def backtest_weights_endpoint():
    """Run the source-weight backtest and return JSON results."""
    try:
        min_games = int(request.args.get("min_games", 6))
    except (TypeError, ValueError):
        min_games = 6
    try:
        result = run_backtest(min_2025_games=min_games)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/power-rankings")
def power_rankings_page():
    try:
        data = fetch_league_data()
        teams = parse_league(data)
        rankings = fetch_all_rankings()
        dynasty_rankings = build_enhanced_rankings(rankings, teams, mode="dynasty")
        season_rankings = build_enhanced_rankings(rankings, teams, mode="season")

        # Pull live draft state — if picks/trades exist, simulate them onto teams
        draft_picks, trade_log, draft_summary = _load_draft_state_for_sim(teams)
        if draft_picks or trade_log:
            sim_teams, sim_dynasty, sim_season = simulate_draft_state(
                teams, dynasty_rankings, season_rankings, draft_picks, trade_log or None,
            )
            # Use simulated state. apply_draft_state=False because we already
            # consumed picks in simulate_draft_state (avoid double-subtraction).
            teams_for_pr = sim_teams
            dynasty_for_pr = sim_dynasty
            season_for_pr = sim_season
            apply_state = False
        else:
            teams_for_pr = teams
            dynasty_for_pr = dynasty_rankings
            season_for_pr = season_rankings
            apply_state = True

        pr_dynasty = compute_power_rankings(teams_for_pr, dynasty_for_pr, mode="dynasty", season_rankings=season_for_pr, apply_draft_state=apply_state)
        pr_season = compute_power_rankings(teams_for_pr, season_for_pr, mode="season", apply_draft_state=apply_state)

        # Monte Carlo season simulation — attach to season-mode entries.
        # Runs ~1-2s for 1000 sims, cached by roster signature.
        sim_by_id = _season_sim_cached(teams_for_pr, season_for_pr)
        for entry in pr_season:
            sim = sim_by_id.get(entry["team_id"])
            if sim:
                entry["expected_wins"] = sim["expected_wins"]
                entry["wins_p05"] = sim["wins_p05"]
                entry["wins_p95"] = sim["wins_p95"]
                entry["playoff_pct"] = sim["playoff_pct"]
                entry["championship_pct"] = sim["championship_pct"]
        # Compute max positional values for bar scaling (use dynasty for both)
        max_pos = {}
        all_teams = pr_dynasty + pr_season
        for pos in ["QB", "RB", "WR", "TE"]:
            max_pos[pos] = max((t["pos_scores"][pos]["value"] for t in all_teams), default=1) or 1

        # Attach roster details, positional league ranks, and picks to each entry.
        # Use the simulated state so rosters/picks reflect the live draft board.
        teams_by_id = {t["id"]: t for t in teams_for_pr}
        for pr_list, rnk in [(pr_dynasty, dynasty_for_pr), (pr_season, season_for_pr)]:
            # Compute positional league ranks
            for pos in ["QB", "RB", "WR", "TE"]:
                sorted_by_pos = sorted(pr_list, key=lambda t: t["pos_scores"][pos]["value"], reverse=True)
                for rank, entry in enumerate(sorted_by_pos, 1):
                    entry.setdefault("pos_league_ranks", {})[pos] = rank

            for entry in pr_list:
                team = teams_by_id.get(entry["team_id"])
                if not team:
                    entry["roster_detail"] = []
                    entry["picks_detail"] = []
                    continue

                # Build roster detail with overall rank and pos rank from rankings
                roster_detail = []
                for p in team["roster"]:
                    nkey = normalize_name(p["name"])
                    r = rnk.get(nkey, {})
                    val = r.get("combined", 0)
                    age = r.get("age", 0)
                    pos = p.get("position", "")
                    ovr_rank = r.get("rank", 999)
                    pos_rank_str = ""
                    pr_val = r.get("pos_rank", 999)
                    if pos and pr_val and pr_val < 999:
                        pos_rank_str = f"{pos}{pr_val}"
                    roster_detail.append({
                        "name": p["name"],
                        "pos": pos,
                        "value": val,
                        "age": round(age, 1) if age and age > 0 else None,
                        "ovr_rank": ovr_rank if ovr_rank < 999 else None,
                        "pos_rank": pos_rank_str,
                    })
                roster_detail.sort(key=lambda x: x["value"], reverse=True)
                entry["roster_detail"] = roster_detail

                # Attach picks detail
                picks_detail = []
                for pk in team.get("picks_holds", []):
                    pv = PICK_VALUES.get(pk, 0)
                    pk_val = pv["ktc"] if isinstance(pv, dict) else (pv or 0)
                    picks_detail.append({"label": pk, "value": pk_val})
                picks_detail.sort(key=lambda x: x["value"], reverse=True)
                entry["picks_detail"] = picks_detail

        my_team_id = int(request.args.get("team_id", 8))
        return render_template(
            "power_rankings.html",
            dynasty_rankings=pr_dynasty,
            season_rankings=pr_season,
            max_pos_values=max_pos,
            my_team_id=my_team_id,
            teams=teams,
            draft_summary=draft_summary,
            error=None,
        )
    except Exception as e:
        return render_template(
            "power_rankings.html",
            dynasty_rankings=[],
            season_rankings=[],
            max_pos_values={"QB": 1, "RB": 1, "WR": 1, "TE": 1},
            my_team_id=8,
            teams=[],
            draft_summary={"picks_count": 0, "trades_count": 0, "last_pick": None},
            error=str(e),
        )


@app.route("/analyze", methods=["POST"])
def analyze():
    body        = request.get_json()
    my_team_id  = int(body.get("team_id", 0))
    style_key   = body.get("trade_style", "aggressive")
    target_pos  = body.get("target_pos", "").strip().upper()   # e.g. "QB", "WR", ""
    style       = TRADE_STYLES.get(style_key, TRADE_STYLES["aggressive"])

    try:
        data = fetch_league_data()
        teams = parse_league(data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch ESPN data: {e}"}), 500

    my_team = next((t for t in teams if t["id"] == my_team_id), None)
    if not my_team:
        return jsonify({"error": "Team not found"}), 400

    rankings = fetch_all_rankings()
    league_summary = build_league_prompt(teams, my_team_id, rankings)
    rankings_block = build_rankings_summary(rankings)
    ds_notes_block = build_draftsharks_notes_block(
        rankings,
        [p["name"] for t in teams for p in t.get("roster", [])],
    )

    prompt = f"""You are an expert dynasty fantasy football trade analyst. It is March 2026, immediately after the 2025 NFL season ended.

=== LEAGUE FORMAT ===
Superflex (OP slot), NO tight end premium, 0.5 PPR, 10 teams.
In Superflex leagues, QBs are scarce and extremely valuable — elite starters are nearly impossible to replace.

=== 2026 ROOKIE CLASS CONTEXT ===
The 2026 NFL Draft has NOT occurred yet (April 2026). Top dynasty prospects in this class:
- Jeremiyah Love (RB) — consensus #1 dynasty rookie, elite athleticism and backfield role
- Carnell Tate (WR) — elite dynasty WR prospect, likely top-3 pick
- Jordyn Tyson (WR) — elite dynasty WR prospect, likely top-5 pick
Pick 1.01 (Jesse's own) projects to land Love. Pick 1.03 (Alex's) projects to land Tate or Tyson.
These picks carry immense dynasty value and should only be traded for established players of equivalent caliber (top-15 dynasty overall).

=== HOW TO USE COMBINED VALUES ===
All player values use a Combined Dynasty Value (0–9999) — DraftSharks (75% weight) + KTC Superflex (25%), normalized to the same scale.
Pick values are KTC-calibrated.

{style["value_rule"]}

=== CURRENT DYNASTY RANKINGS (March 2026 — weighted DraftSharks (75%) + KTC Superflex (25%)) ===
{rankings_block}

{ds_notes_block}

=== FULL LEAGUE ROSTERS ===
Players are annotated: Name (VAL:combined-value #overall-rank POS | Age X). Picks annotated with KTC value and known 2026 draft slot.
{league_summary}

=== MY TEAM SITUATION ===
Team: {my_team['name']} | Owner: Jesse | 2025 Record: {my_team['wins']}-{my_team['losses']}

WHAT I HAVE GOING FOR ME:
- Three 2026 dynasty 1st-round picks: my own (1.01, top pick), Alex's (1.03), Patrick's (1.10)
- Strong 2027 pick stack as well
- 2026 draft plan is LOCKED: Jeremiyah Love at 1.01, top WR at 1.03, best available at 1.10
- Some solid young pieces: Quinshon Judkins (#61 RB, age 22), Luther Burden III (#46 WR, age 22), Chris Olave (#23 WR, age 25), Jordan Addison (#67 WR, age 24)

MY WEAKNESSES / GOALS:
- LONG-TERM QB VULNERABILITY: Brock Purdy is my only real Superflex starter. I'm open to trading 2027 1st round picks to acquire an established young QB from another team if the value is right — that would be PREFERABLE to using those picks on a rookie QB.
- Thin overall roster depth — many bench players are low-value fillers
- Strategy: use peripheral assets (late picks, redundant bench players, IR players) to improve the roster NOW while keeping my core 2026 draft plan intact

=== STRICT CONSTRAINTS — CLAUDE MUST FOLLOW THESE ===
1. Brock Purdy CAN be traded, but ONLY if: (a) the return is equal or better dynasty value, AND (b) the trade includes a viable replacement QB1 coming back to my team.
2. My 2026 1st picks (Jesse's 1.01 and Alex's 1.03) CAN be traded, but ONLY for elite dynasty value equivalent to what those picks would draft — think Jeremiyah Love, Carnell Tate, or Jordyn Tyson tier (top-15 dynasty overall). Do not trade them for anything less.
3. My 2027 1st round pick CAN be traded if it returns real value — especially for an established young QB. Do not trade it for minor upgrades.
4. ASSETS I CAN TRADE: Patrick's 2026 1st (1.10), 2026 2nd/3rd/4th picks, 2027 2nd/3rd/4th picks, and low-value bench/IR players (Najee Harris, James Conner, Deshaun Watson, Will Levis, etc.)
5. Every player mentioned in a trade MUST actually exist on that team's roster as shown above. Do not hallucinate players.

TASK ({style["label"].upper()}): Generate exactly 6 trade proposals. {style["task_note"]} Use peripheral assets (NOT my locked picks or Purdy).
{f"POSITIONAL FILTER — REQUIRED: Every trade Jesse receives must include at least one {target_pos}. Jesse specifically needs to address his {target_pos} situation. Do not propose trades that don't bring back a {target_pos}." if target_pos else "Prioritize acquiring young players (age 22-25) at positions where I am thin."}

For each trade, explicitly state the Combined Value Jesse gives vs. receives and confirm it meets the {style["label"]} balance requirement above.

Return ONLY a valid JSON array — no markdown, no explanation, no code fences:
[
  {{
    "trade_partner": "Team Name",
    "trade_partner_owner": "Owner Name",
    "my_team_gives": ["Player Name (POS) — VAL:XXXX", "202X Round Pick — VAL:XXXX"],
    "i_receive": ["Player Name (POS) — VAL:XXXX", "202X Round Pick — VAL:XXXX"],
    "give_total": 0,
    "receive_total": 0,
    "value_check": "Jesse gives: X (VAL total). Jesse receives: Y (VAL total). Ratio Y/X = Z. Meets [mode] requirement: yes/no.",
    "rationale": "2-3 sentences explaining why both sides benefit and why the other team accepts, referencing actual dynasty values and ages",
    "grade": "A",
    "dynasty_impact": "1-2 sentences on how this improves my roster without compromising my 2026 draft plan"
  }}
]

{style["grade_scale"]}"""

    try:
        message = _messages_create_with_fallback(
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        trades = json.loads(raw)
        trades = _filter_trades(trades, style_key, rankings)
        trades = _enrich_trades_with_impact(trades, teams, rankings, my_team_id)
        return jsonify({"trades": trades, "my_team": my_team["name"], "trade_style": style["label"]})

    except json.JSONDecodeError as e:
        # Try to salvage partial JSON
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start != -1 and end > start:
            try:
                trades = json.loads(raw[start:end])
                trades = _filter_trades(trades, style_key, rankings)
                trades = _enrich_trades_with_impact(trades, teams, rankings, my_team_id)
                return jsonify({"trades": trades, "my_team": my_team["name"], "trade_style": style["label"]})
            except Exception:
                pass
        return jsonify({"error": f"Could not parse AI response: {e}"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/draft-board")
def draft_board():
    try:
        data = fetch_league_data()
        teams = parse_league(data)
    except Exception:
        teams = []
    owners = [t["owner"].split()[0] for t in teams]

    # Enrich rookies with live combined values (DraftSharks + KTC)
    rankings = fetch_all_rankings()
    enriched = []
    for r in ROOKIES_2026:
        info = rankings.get(normalize_name(r["name"]))
        if info and info.get("combined", 0) > 0:
            entry = {
                **r,
                "dyn_value":   info["combined"],
                "ktc_live":    info.get("ktc"),
                "live":        True,
            }
        else:
            entry = {
                **r,
                "dyn_value":   r["ktc_est"],
                "ktc_live":    None,
                "live":        False,
            }
        enriched.append(entry)

    # Build team position counts for draft simulation
    team_pos_counts = {}
    for t in teams:
        first = t["owner"].split()[0]
        display = OWNER_DISPLAY.get(first, first)
        counts = {"QB": 0, "RB": 0, "WR": 0, "TE": 0}
        for p in t["roster"]:
            pos = p.get("position", "")
            if pos in counts:
                counts[pos] += 1
        team_pos_counts[display] = counts

    return render_template(
        "draft_board.html", rookies=enriched, owners=owners,
        team_pos_counts=team_pos_counts,
    )


@app.route("/recommend-pick", methods=["POST"])
def recommend_pick():
    body = request.get_json()
    pick_number = int(body.get("pick_number", 1))
    available = body.get("available_players", [])  # [{name, pos, ktc_est}]

    try:
        data = fetch_league_data()
        teams = parse_league(data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch ESPN data: {e}"}), 500

    my_team = next((t for t in teams if t["id"] == 8), None)
    if not my_team:
        return jsonify({"error": "Jesse's team not found"}), 400

    avail_str = "\n".join(
        f"  #{i+1}. {p['name']} ({p['pos']}) — Dynasty Value ~{p['ktc_est']}"
        for i, p in enumerate(available[:20])
    )

    rankings = fetch_all_rankings()
    ds_notes_block = build_draftsharks_notes_block(
        rankings,
        [p["name"] for p in available[:20]]
        + [p["name"] for p in my_team.get("roster", [])],
    )

    pick_context = {
        1:  "This is Jesse's 1.01 (first overall). Jeremiyah Love is the consensus #1 dynasty prospect (Combined Value ~7100). If Love is still available, this is essentially locked. Only discuss alternatives if Love is off the board.",
        3:  "This is Jesse's 1.03 (Alex's pick). Note: Fernando Mendoza (QB, Indiana, Combined Value ~5600) is the #2 dynasty asset in this class in Superflex leagues — higher than the WRs. Jesse's stated plan is to take a WR here, but Mendoza would directly solve his long-term QB vulnerability (Purdy is his only QB1). If Mendoza is available, flag this as a genuine decision point. Otherwise, the target is the top WR available — Carnell Tate (~5335) or Makai Lemon (~5182) or Jordyn Tyson (~4830).",
        10: "This is Jesse's 1.10 (Patrick's pick, last pick of round 1). With Love and a WR already locked in earlier picks, this is pure best-player-available. Consider: Jesse already has Purdy (QB), Judkins (RB), Burden/Olave/Addison (WRs). He could use another RB or WR with upside.",
    }

    prompt = f"""You are an expert dynasty fantasy football draft analyst. It is draft day 2026.

=== LEAGUE FORMAT ===
Superflex (OP slot), NO TE premium, 0.5 PPR, 10 teams.

=== JESSE'S CURRENT ROSTER (pre-draft) ===
QB1: Brock Purdy (Combined Value ~6000, age 26)
RBs: Quinshon Judkins (~2800, age 22)
WRs: Chris Olave (~3200, age 25), Luther Burden III (~2600, age 22), Jordan Addison (~2200, age 24)
(Thin roster — primarily a pick-stacking rebuild entering 2026 draft)

=== PICK CONTEXT ===
Jesse is making pick {pick_number}. {pick_context.get(pick_number, "Best player available.")}

=== AVAILABLE PLAYERS (top 20 remaining on board) ===
Values shown are pre-draft KTC estimates — use as relative tier guides.
{avail_str}

{ds_notes_block}

Give Jesse a clear, direct recommendation for this pick. Name the specific player he should take and explain why in 2-3 sentences, referencing dynasty values and his roster needs. If there's a close call between 2 players, address it.

Return ONLY a JSON object — no markdown, no code fences:
{{
  "pick": "Player Name (POS)",
  "reasoning": "2-3 sentences explaining why this is the right call for Jesse's dynasty",
  "alternatives": ["Alternative 1 (POS) — one-line reason", "Alternative 2 (POS) — one-line reason"]
}}"""

    try:
        message = _messages_create_with_fallback(
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _build_draft_context(draft_state):
    """Convert the frontend localStorage draft board into a prompt-ready context block."""
    if not draft_state:
        return ""
    log           = draft_state.get("draftLog", [])
    current_pick  = draft_state.get("currentPick", 1)
    drafted_names = draft_state.get("draftedNames", [])
    picks_held    = draft_state.get("picksHeld", {})
    trade_log     = draft_state.get("tradeLog", [])
    roster_trades = draft_state.get("rosterTrades", [])

    if not log and not trade_log and not roster_trades:
        return ""

    JESSE_SLOTS     = {1: "1.01 (own)", 3: "1.03 (Alex's)", 10: "1.10 (Patrick's)"}
    JESSE_SLOT_KEYS = {1: "1.01", 3: "1.03", 10: "1.10"}
    jesse_drafted   = [e for e in log if e.get("isJesse")]

    context_parts = []

    if log:
        lines = [f"=== LIVE DRAFT BOARD — {len(log)}/10 picks made, now at pick {current_pick} ==="]
        for e in log:
            slot   = JESSE_SLOTS.get(e["pick"], "")
            slot_s = f" [{slot}]" if slot else ""
            me_s   = " ← JESSE" if e.get("isJesse") else ""
            lines.append(f"  Pick {e['pick']}{slot_s}: {e['playerName']} ({e['pos']}) → {e['teamOwner']}{me_s}")

        lines.append("")

        # Pick status summary — respect picks_held for pending (traded picks omitted)
        made_parts, pending_parts = [], []
        for p in [1, 3, 10]:
            entry = next((x for x in log if x["pick"] == p and x.get("isJesse")), None)
            if entry:
                made_parts.append(f"{JESSE_SLOTS[p]} → {entry['playerName']} ({entry['pos']})")
            else:
                slot_key = JESSE_SLOT_KEYS[p]
                if picks_held.get(slot_key, True):   # default True = still holding
                    pending_parts.append(JESSE_SLOTS[p])
                # if False → traded away, skip from pending list
        if made_parts:
            lines.append("Jesse's picks MADE:    " + " | ".join(made_parts))
        if pending_parts:
            lines.append("Jesse's picks PENDING: " + ", ".join(pending_parts))

        # Updated roster & needs
        if jesse_drafted:
            pos_drafted = [e["pos"] for e in jesse_drafted]
            haul = ", ".join(f"{e['playerName']} ({e['pos']})" for e in jesse_drafted)
            lines.append(f"Jesse's draft haul so far: {haul}")
            needs = []
            if "QB" not in pos_drafted:
                needs.append("QB (long-term Purdy vulnerability still unaddressed)")
            if pos_drafted.count("RB") == 0:
                needs.append("RB depth")
            if pos_drafted.count("WR") < 1:
                needs.append("WR")
            if needs:
                lines.append(f"Jesse's remaining positional needs: {', '.join(needs)}")
            else:
                lines.append("Jesse has addressed all major positional needs in this draft.")
        else:
            lines.append("Jesse has not yet made any of his picks.")

        # Off-the-board list
        if drafted_names:
            lines.append(f"\nOFF THE BOARD — already drafted, cannot be a draft-day pickup target:")
            lines.append(f"  {', '.join(drafted_names)}")
            lines.append("These players CAN still be offered by the team that drafted them in a post-draft player trade.")

        context_parts.append("\n".join(lines))

    # In-draft pick trade overrides
    if trade_log:
        lines = ["\n=== IN-DRAFT PICK TRADES (overrides pre-draft holdings) ==="]
        for t in trade_log:
            gave_str = t['gave']
            if t.get('alsoGives'):
                gave_str += f" + {t['alsoGives']}"
            lines.append(f"  Jesse GAVE {gave_str} to {t.get('to', '?')} → received: {t.get('received', '?')}")
        still_held = [k for k, v in picks_held.items() if k != "acquired" and v is True]
        acquired   = picks_held.get("acquired", [])
        acq_labels = [p.get("label", "?") for p in acquired] if isinstance(acquired, list) else []
        all_held   = still_held + acq_labels
        lines.append("Jesse's current picks held: " + (", ".join(all_held) if all_held else "none"))
        context_parts.append("\n".join(lines))

    # Mid-draft player/roster trade overrides
    if roster_trades:
        lines = ["\n=== MID-DRAFT PLAYER TRADES (override ESPN rosters) ==="]
        for t in roster_trades:
            note = f" ({t['note']})" if t.get("note") else ""
            lines.append(f"  {t['player']}: {t['fromTeam']} → {t['toTeam']}{note}")
        lines.append("NOTE: Treat the above as current roster truth, superseding the ESPN data shown above.")
        context_parts.append("\n".join(lines))

    return "\n".join(context_parts)


@app.route("/draft-day", methods=["POST"])
def draft_day():
    body        = request.get_json()
    my_team_id  = int(body.get("team_id", 0))
    style_key   = body.get("trade_style", "fair")
    pick_slot   = body.get("pick_slot", "1.01")   # "1.01", "1.03", or "1.10"
    draft_state = body.get("draft_state")          # live board from localStorage
    style       = TRADE_STYLES.get(style_key, TRADE_STYLES["fair"])

    try:
        data = fetch_league_data()
        teams = parse_league(data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch ESPN data: {e}"}), 500

    my_team = next((t for t in teams if t["id"] == my_team_id), None)
    if not my_team:
        return jsonify({"error": "Team not found"}), 400

    rankings = fetch_all_rankings()
    league_summary = build_league_prompt(teams, my_team_id, rankings)
    rankings_block = build_rankings_summary(rankings)
    ds_notes_block = build_draftsharks_notes_block(
        rankings,
        [p["name"] for t in teams for p in t.get("roster", [])]
        + [r["name"] for r in ROOKIES_2026],
    )

    # ── Parse live draft board ─────────────────────────────────────────────────
    draft_ctx = _build_draft_context(draft_state)

    # Determine which of Jesse's picks are already made vs still pending
    _log = (draft_state or {}).get("draftLog", [])
    _jesse_made = {e["pick"] for e in _log if e.get("isJesse")}
    _jesse_haul = [e for e in _log if e.get("isJesse")]
    _pos_drafted = [e["pos"] for e in _jesse_haul]
    _drafted_names = set((draft_state or {}).get("draftedNames", []))

    # Build a dynamic "Jesse's roster situation NOW" string for each pick task
    def _needs_str():
        needs = []
        if "QB" not in _pos_drafted:
            needs.append("QB (long-term vulnerability still open)")
        if _pos_drafted.count("RB") == 0:
            needs.append("RB depth")
        if _pos_drafted.count("WR") < 1:
            needs.append("WR")
        haul = ", ".join(f"{e['playerName']} ({e['pos']})" for e in _jesse_haul) or "none yet"
        need_s = ", ".join(needs) if needs else "all major positions now addressed"
        return f"Jesse's draft haul so far: {haul}. Remaining needs: {need_s}."

    # ── Per-pick task prompts ──────────────────────────────────────────────────
    _needs_now = _needs_str()

    if pick_slot == "1.01":
        _101_status = "Jesse has ALREADY MADE this pick." if 1 in _jesse_made else "This pick has NOT been made yet."
        pick_task = f"""=== THE PICK BEING SOLD: 1.01 (Jeremiyah Love) ===
{_101_status}
Jesse is entertaining offers for the #1 overall pick — Jeremiyah Love, the consensus #1 dynasty prospect (Combined Value ~7101, elite RB).
This is the most valuable asset in the draft. Jesse only moves it if he gets a true franchise-level return.

FLOOR: Jesse receives a minimum of ~7101 Combined Value (or equivalent pick + player stack).
Jesse still holds 1.03 (Alex's, ~5400) and 1.10 (Patrick's, ~3000) — those are NOT being traded here.
{_needs_now}
Do NOT leave Jesse without a QB1 if Purdy is involved.

TASK ({style["label"].upper()}): Generate exactly 5 trade packages from 5 DIFFERENT teams, each showing what Jesse should demand to sell 1.01. {style["task_note"]}
Each scenario must come from a different team. Vary the package structures (star player, star + pick, multi-pick stack, etc.).
SANITY CHECK BEFORE INCLUDING EACH TRADE: Does the trading partner actually WANT Jeremiyah Love given their roster? Do they have an RB need and enough capital to give? Would they rationally give up what you're proposing?
Use FULL Combined Values from the rankings for every player on both sides — no backup discounts or arbitrary haircuts.
For each trade, calculate: Jesse gives 1.01 (~7101 VAL) → Jesse receives [assets] (VAL total). Ratio = receives/7101. Confirm it meets {style["label"]} requirement.
scenario_type for all trades: "Sell 1.01 — Trade Up Package" """

    elif pick_slot == "1.03":
        _103_status = "Jesse has ALREADY MADE this pick." if 3 in _jesse_made else "This pick has NOT been made yet."
        _101_info = f"Jesse already drafted {next((e['playerName'] for e in _jesse_haul if e['pick']==1), 'Love')} at 1.01." if 1 in _jesse_made else "Jesse still plans to draft Love at 1.01."
        pick_task = f"""=== THE PICK BEING SOLD: 1.03 (Alex's pick, held by Jesse) ===
{_103_status} {_101_info}
Jesse is entertaining offers for the 1.03 pick — projects to Carnell Tate or Jordyn Tyson, elite WR prospects (Combined Value ~5400).
Jesse keeps 1.01 (or has already used it) and 1.10 (Patrick's pick, ~3000) — neither is part of these deals.

FLOOR: Jesse receives a minimum of ~5400 Combined Value for 1.03.
{_needs_now}
Do NOT leave Jesse without a QB1 if Purdy is involved.

TASK ({style["label"].upper()}): Generate exactly 5 trade packages from 5 DIFFERENT teams, each showing what Jesse should demand to sell 1.03. {style["task_note"]}
Each scenario must come from a different team. Vary the package structures.
SANITY CHECK: Focus on teams that genuinely NEED a blue-chip WR or young playmaker AND have the capital (players/picks) to give. Do NOT include teams as buyers if they have no logical reason to want this pick.
SANITY CHECK: Only offer players from teams that can realistically part with them given their own roster needs as shown above.
Use FULL Combined Values from the rankings for every player on both sides — no backup discounts or arbitrary haircuts.
For each trade, calculate: Jesse gives 1.03 (~5400 VAL) → Jesse receives [assets] (VAL total). Ratio = receives/5400. Confirm it meets {style["label"]} requirement.
scenario_type for all trades: "Sell 1.03 — Trade Up Package" """

    else:  # 1.10
        _110_status = "Jesse has ALREADY MADE this pick." if 10 in _jesse_made else "This pick has NOT been made yet."
        _haul_str = ", ".join(f"{e['playerName']} ({e['pos']})" for e in _jesse_haul)
        _prior = f"Jesse has already drafted: {_haul_str}. " if _jesse_haul else ""
        pick_task = f"""=== THE PICK BEING SOLD: 1.10 (Patrick's pick, held by Jesse) ===
{_110_status} {_prior}
Jesse is entertaining offers for the 1.10 pick — Combined Value ~3000.
Jesse keeps 1.01 and 1.03 picks (or has already used them) — only 1.10 is being moved.
{_needs_now}

HOW TO BUILD THESE TRADES — READ THIS CAREFULLY:
1.10 alone is only worth ~3000. To land any meaningful return, Jesse PACKAGES additional peripheral assets on his side. This is the expectation, not the exception. Look at the MY TEAM rosters section above for Jesse's full inventory — use anything that's NOT his 1.01, 1.03, or Purdy.

PACKAGE INVENTORY Jesse can offer ALONGSIDE 1.10 (combine freely):
  - Jesse's 2026 2nd / 3rd / 4th round picks (look up exact values in the rankings/pick listings)
  - Jesse's 2027 2nd / 3rd / 4th round picks
  - Driscoll's 2027 1st (Jesse holds it — only trade as last resort; mention if used)
  - IR/depth players Jesse can flip: Conner, Dell, Najee Harris, Helm, Bigsby, Tracy Jr., Blue, etc.
  - Any peripheral player on Jesse's roster who isn't a clear hold

TRADE CONSTRUCTION RULES:
- Use FULL Combined Values from the rankings — no backup discounts, no haircuts.
- If a target player is worth 4900, Jesse adds peripherals (e.g. 1.10 + 2027 2nd + IR player) until totals match within the {style["label"]} window.
- You MUST find ways to package — DO NOT return fewer than the requested number of trades simply because 1.10-alone doesn't match. Add peripheral assets until the math works.
- VERIFY trade partner motivations: don't propose someone giving up a player they need; don't propose someone wanting 1.10 if their roster is full of late picks already.
- Do NOT leave Jesse without a QB1 if Purdy is involved.

TASK ({style["label"].upper()}): Generate exactly 5 trade scenarios from 5 DIFFERENT teams involving Jesse moving 1.10 plus peripheral assets. {style["task_note"]}
Each scenario must come from a different team.
For each trade, calculate: Jesse gives 1.10 + [peripheral assets] (VAL total) → Jesse receives [assets] (VAL total). Ratio = receives/gives. Confirm it meets {style["label"]} requirement using FULL Combined Values.
scenario_type for all trades: "Sell 1.10 — Package Deal" """

    draft_ctx_section = f"\n{draft_ctx}\n" if draft_ctx else ""

    prompt = f"""You are an expert dynasty fantasy football trade analyst. It is DRAFT DAY for the 2026 NFL Draft (late April 2026).

=== LEAGUE FORMAT ===
Superflex (OP slot), NO tight end premium, 0.5 PPR, 10 teams.
In Superflex leagues, QBs are scarce and extremely valuable.

=== VALUE RULES — READ CAREFULLY ===
Player values use a Combined Dynasty Value (0–9999) — DraftSharks (75%) + KTC Superflex (25%), each normalized.
Pick values are KTC-calibrated.

USE FULL COMBINED VALUES IN ALL TRADE MATH:
- Always state each player's full Combined Dynasty Value as shown in the rankings — do NOT apply a "backup discount" or any other arbitrary haircut.
- Picks trade close to their listed KTC value.
- Realism comes from PICKING THE RIGHT TRADE PARTNERS, not from artificially deflating player values. A team should NEVER trade away a position they are thin at. Before including a trade partner giving up a player, verify that player is genuinely surplus for them (check their roster above). If the partner needs that player, find someone else.
- A QB on a roster with multiple SF-quality QBs is still worth his full Combined Value; surplus only means he's more likely to be moved, not that his price drops.
{draft_ctx_section}
{style["value_rule"]}

=== CURRENT DYNASTY RANKINGS (weighted DraftSharks (75%) + KTC Superflex (25%)) ===
{rankings_block}

{ds_notes_block}

=== FULL LEAGUE ROSTERS ===
{league_summary}

=== MY TEAM ===
Team: {my_team['name']} | Owner: Jesse | Record: {my_team['wins']}-{my_team['losses']}
QB1: Brock Purdy | Picks: 1.01 (Love, ~7101), 1.03 (Alex's, ~5400), 1.10 (Patrick's, ~3000)

=== HARD CONSTRAINTS ===
- Every player mentioned MUST actually exist on that team's roster as shown above
- A trade partner should NEVER give away a player at a position they are short on (e.g., a team needing QB depth should not give up their backup QB)
- Trades must be ones BOTH sides would realistically accept
- Never leave Jesse without a starting QB1 in Superflex
- EXCLUDE any trade where the value ratio falls outside the {style["label"]} window — DO NOT include it, do not grade it F, simply omit it

{pick_task}

Return ONLY a valid JSON array — no markdown, no explanation, no code fences:
[
  {{
    "trade_partner": "Team Name",
    "trade_partner_owner": "Owner Name",
    "scenario_type": "...",
    "my_team_gives": ["asset — VAL:XXXX", "asset — VAL:XXXX"],
    "i_receive": ["asset — VAL:XXXX", "asset — VAL:XXXX"],
    "give_total": 0,
    "receive_total": 0,
    "value_check": "Jesse gives: X (VAL total). Jesse receives: Y (VAL total). Ratio Y/X = Z. Meets {style["label"]} requirement: yes.",
    "rationale": "2-3 sentences — why this team is a motivated buyer AND has surplus in what they're giving up, referencing actual values and roster context",
    "grade": "A",
    "dynasty_impact": "1-2 sentences on how this changes Jesse's dynasty trajectory given his current draft haul"
  }}
]

{style["grade_scale"]}
Only include trades that would receive A, B+, or B grades. Omit anything below B."""

    try:
        message = _messages_create_with_fallback(
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        trades = json.loads(raw)
        trades = _filter_trades(trades, style_key, rankings)
        trades = _enrich_trades_with_impact(trades, teams, rankings, my_team_id)

        return jsonify({
            "trades": trades,
            "my_team": my_team["name"],
            "mode": "draft_day",
            "pick_slot": pick_slot,
            "trade_style": style["label"],
        })

    except json.JSONDecodeError as e:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start != -1 and end > start:
            try:
                trades = json.loads(raw[start:end])
                trades = _filter_trades(trades, style_key, rankings)
                trades = _enrich_trades_with_impact(trades, teams, rankings, my_team_id)
                return jsonify({"trades": trades, "my_team": my_team["name"], "mode": "draft_day", "pick_slot": pick_slot, "trade_style": style["label"]})
            except Exception:
                pass
        return jsonify({"error": f"Could not parse AI response: {e}"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/evaluate-offer", methods=["POST"])
def evaluate_offer():
    body = request.get_json()
    my_team_id = int(body.get("team_id", 0))
    offer_text = body.get("offer_text", "").strip()

    if not offer_text:
        return jsonify({"error": "No offer text provided"}), 400

    try:
        data = fetch_league_data()
        teams = parse_league(data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch ESPN data: {e}"}), 500

    my_team = next((t for t in teams if t["id"] == my_team_id), None)
    if not my_team:
        return jsonify({"error": "Team not found"}), 400

    rankings = fetch_all_rankings()
    league_summary = build_league_prompt(teams, my_team_id, rankings)
    rankings_block = build_rankings_summary(rankings)
    ds_notes_block = build_draftsharks_notes_block(
        rankings,
        [p["name"] for t in teams for p in t.get("roster", [])],
    )

    prompt = f"""You are an expert dynasty fantasy football trade analyst evaluating an incoming offer on 2026 NFL draft day.

=== LEAGUE FORMAT ===
Superflex (OP slot), NO TE premium, 0.5 PPR, 10 teams.

=== DRAFT DAY CONTEXT ===
2026 NFL Draft is happening today. Jesse's picks and values:
- 1.01 = Jeremiyah Love (#1 dynasty prospect, KTC 7101)
- 1.03 (Alex's pick, held by Jesse) = Carnell Tate or Jordyn Tyson (KTC ~5400)
- 1.10 (Patrick's pick, held by Jesse) = KTC 3000
- Jesse's QB1: Brock Purdy — must not be left without a QB1 in Superflex

=== CURRENT DYNASTY RANKINGS (weighted DraftSharks (75%) + KTC Superflex (25%)) ===
{rankings_block}

{ds_notes_block}

=== FULL LEAGUE ROSTERS ===
{league_summary}

=== MY TEAM ===
Team: {my_team['name']} | Owner: Jesse

=== OFFER TO EVALUATE ===
{offer_text}

Evaluate this offer using Combined Dynasty Values (DraftSharks 75% + KTC 25%). For any player not in the rankings, estimate based on position/age and note it. For picks, use KTC values.

Return ONLY a single JSON object — no markdown, no code fences:
{{
  "offer_summary": "Brief one-line description of the offer",
  "trade_partner": "Team name of the trade partner",
  "jesse_gives": ["Player Name", "Pick Label"],
  "jesse_receives": ["Player Name", "Pick Label"],
  "jesse_gives_ktc": 0,
  "jesse_receives_ktc": 0,
  "ktc_breakdown": "Jesse gives: Asset1 (VAL X) + Asset2 (VAL Y) = Z. Jesse receives: Asset1 (VAL X) + Asset2 (VAL Y) = Z.",
  "verdict": "ACCEPT",
  "verdict_reason": "2-3 sentences evaluating the fairness and whether Jesse should pull the trigger",
  "counter_offer": "Specific counter-offer Jesse should make (e.g. 'Ask for X instead of Y'), or 'N/A' if verdict is ACCEPT",
  "grade": "A"
}}

verdict must be: ACCEPT (Combined Value within 10%), COUNTER (close but needs a sweetener), or DECLINE (too far off)
Grades: A = excellent value for Jesse, B+ = solid, B = fair, C+ = slight overpay, C = bad, D = terrible"""

    try:
        message = _messages_create_with_fallback(
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        evaluation = json.loads(raw)

        # Add trade impact if we can identify partner and assets
        give = evaluation.get("jesse_gives", [])
        recv = evaluation.get("jesse_receives", [])
        partner = evaluation.get("trade_partner", "")
        if give and recv and partner:
            partner_id = find_partner_team_id(teams, partner)
            if partner_id:
                try:
                    impact = compute_trade_impact(teams, rankings, my_team_id, partner_id, give, recv)
                    if impact:
                        evaluation["impact"] = impact
                except Exception:
                    pass

        return jsonify({"evaluation": evaluation, "my_team": my_team["name"]})

    except json.JSONDecodeError as e:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                evaluation = json.loads(raw[start:end])
                return jsonify({"evaluation": evaluation, "my_team": my_team["name"]})
            except Exception:
                pass
        return jsonify({"error": f"Could not parse AI response: {e}"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/advisor")
def advisor():
    try:
        data = fetch_league_data()
        teams = parse_league(data)
    except Exception:
        teams = []
    my_team_id = int(request.args.get("team_id", 8))
    return render_template("advisor.html", teams=teams, my_team_id=my_team_id)


def _build_advisor_buy_sell_block(rankings: dict, teams: list, my_team_id: int) -> str:
    """
    Identify largest BUY/SELL gaps between DraftSharks rank and the KTC+FC
    market rank for rostered players. Both axes are percentile-normalized so
    a positive 'gap' means DS values higher than market (BUY candidate).
    Uses market_rank (KTC+FC only, excludes DS) to keep the gap clean.
    """
    if not rankings:
        return ""

    # Regression-residual signal: fit DS = a*KTC + b across players above
    # KTC=1500, then flag players whose actual DS sits >1.5σ from the
    # regression line. This isolates real source disagreement from
    # systematic curve-shape differences between KTC and DS's 3D Value.
    signals = _compute_regression_signals(rankings, min_ktc=1500)
    if not signals:
        return ""

    buys, sells = [], []
    THRESHOLD = 1.5  # standard deviations
    for team in teams:
        is_mine = team["id"] == my_team_id
        first = team["owner"].split()[0]
        owner = OWNER_DISPLAY.get(first, first)
        for p in team["roster"]:
            if p.get("position") not in ("QB", "RB", "WR", "TE"):
                continue
            nkey = normalize_name(p["name"])
            sig = signals.get(nkey)
            if not sig:
                continue
            z = sig["z"]
            if abs(z) < THRESHOLD:
                continue
            r = rankings.get(nkey, {})
            (buys if z > 0 else sells).append({
                "name":    p["name"],
                "pos":     p["position"],
                "owner":   "Jesse" if is_mine else owner,
                "ds_val":  r.get("ds", 0),
                "ktc_val": r.get("ktc", 0),
                "z":       round(z, 2),
                "mine":    is_mine,
            })

    buys.sort(key=lambda x: -x["z"])
    sells.sort(key=lambda x: x["z"])

    def _fmt(rows, label):
        if not rows:
            return ""
        out = [f"  {label}:"]
        for r in rows[:12]:
            j = " <<JESSE" if r["mine"] else ""
            sign = "+" if r["z"] > 0 else ""
            out.append(
                f"    {r['name']}({r['pos']}) {r['owner']}: "
                f"DS:{r['ds_val']} vs KTC:{r['ktc_val']} "
                f"z={sign}{r['z']}σ{j}"
            )
        return "\n".join(out)

    parts = ["=== DRAFTSHARKS vs KTC — REGRESSION-RESIDUAL SIGNALS ==="]
    parts.append(
        "Both sources normalized to 0-9999 (KTC = superflex value; DS = "
        "3D Value composite). To isolate REAL source disagreement from "
        "systematic curve-shape differences, we fit DS = a*KTC + b across "
        "all players (KTC>=1500), compute the residual per player, and "
        "standardize to z-scores. |z|>1.5σ surfaces the meaningful "
        "disagreements. Positive z = DS values player higher than KTC "
        "predicts (BUY — analyst sees something market doesn't). "
        "Negative z = DS values lower than KTC predicts (SELL — market "
        "may be overrating relative to DS view)."
    )
    buys_block = _fmt(buys, "BUY candidates (DS > KTC-implied)")
    sells_block = _fmt(sells, "SELL candidates (DS < KTC-implied)")
    if buys_block:
        parts.append(buys_block)
    if sells_block:
        parts.append(sells_block)
    return "\n".join(parts) if (buys_block or sells_block) else ""


@app.route("/chat", methods=["POST"])
def chat():
    body        = request.get_json()
    messages    = body.get("messages", [])   # [{role, content}] full history
    team_id     = int(body.get("team_id", 8))
    app_context = body.get("app_context", "").strip()
    session_id  = (body.get("session_id") or "").strip() or "default"

    try:
        data  = fetch_league_data()
        teams = parse_league(data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch ESPN data: {e}"}), 500

    my_team = next((t for t in teams if t["id"] == team_id), None)
    if not my_team:
        return jsonify({"error": "Team not found"}), 400

    rankings = fetch_all_rankings()  # DS (75%) + KTC (25%) weighted

    # Build power rankings context (compact) — both modes
    def _format_pr(pr_list, label):
        lines = [f"  {label}:"]
        for t in pr_list:
            marker = " <<JESSE" if t["team_id"] == team_id else ""
            needs = ",".join(t.get("needs", []))
            strengths = ",".join(t.get("strengths", []))
            lines.append(
                f"  #{t['power_rank']} {t['team_name']}({t['owner']}) "
                f"S:{t['power_score']}|Start:{t['starter_value']}|Tot:{t['total_value']}|"
                f"Cap:{t['draft_capital']}|Age:{t['weighted_age']}|N:[{needs}]|Str:[{strengths}]{marker}"
            )
        return "\n".join(lines)

    # Enhanced rankings blend market values with per-game production
    dynasty_r = build_enhanced_rankings(rankings, teams, mode="dynasty")
    season_r = build_enhanced_rankings(rankings, teams, mode="season")
    rankings_block = build_rankings_summary(dynasty_r, limit=50)
    pr_dynasty = compute_power_rankings(teams, dynasty_r, mode="dynasty", season_rankings=season_r, apply_draft_state=True)
    pr_season = compute_power_rankings(teams, season_r, mode="season", apply_draft_state=True)
    power_rankings_block = _format_pr(pr_dynasty, "Dynasty (long-term)") + "\n" + _format_pr(pr_season, "2026 Season (win-now)")

    # BUY/SELL gap between DraftSharks rank and KTC+FC market rank
    buy_sell_block = _build_advisor_buy_sell_block(rankings, teams, team_id)

    # DraftSharks written analyst notes for rostered players + 2026 rookies
    ds_notes_block = build_draftsharks_notes_block(
        rankings,
        [p["name"] for t in teams for p in t.get("roster", [])]
        + [r["name"] for r in ROOKIES_2026],
    )

    # Build compact league rosters — starters + notable bench only
    roster_lines = []
    for team in teams:
        marker = " <<MY TEAM" if team["id"] == team_id else ""
        starters = [p for p in team["roster"] if p["slot_id"] not in (20, 21, 25)]
        bench    = [p for p in team["roster"] if p["slot_id"] == 20]
        # For non-Jesse teams, only show starters; for Jesse show all
        def fmt_p(p):
            info = rankings.get(normalize_name(p["name"]))
            val = info["combined"] if info and info.get("combined") else 0
            return f"{p['name']}({p.get('position','?')},V:{val})" if val else f"{p['name']}({p.get('position','?')})"
        s_str = ", ".join(fmt_p(p) for p in starters)
        roster_lines.append(f"{team['name']}({team['owner']},{team['wins']}-{team['losses']}){marker}")
        roster_lines.append(f"  Start: {s_str}")
        if team["id"] == team_id and bench:
            roster_lines.append(f"  Bench: {', '.join(fmt_p(p) for p in bench)}")
        elif bench:
            # Only show bench players with value > 1000
            notable = [p for p in bench if (rankings.get(normalize_name(p["name"]), {}).get("combined", 0) or 0) > 1000]
            if notable:
                roster_lines.append(f"  Key bench: {', '.join(fmt_p(p) for p in notable)}")
        holds = team.get("picks_holds", [])
        traded = team.get("picks_traded_away", [])
        if holds:
            roster_lines.append(f"  Picks: {', '.join(holds)}")
        if traded:
            roster_lines.append(f"  Traded away: {', '.join(traded)}")
    league_summary = "\n".join(roster_lines)

    # Build draft board context if available, including server-side impact computation
    draft_ctx = ""
    try:
        with open(STATE_FILE) as f:
            draft_state = json.load(f)
        if draft_state and draft_state.get("draftLog"):
            draft_lines = ["DRAFT BOARD:"]
            for entry in sorted(draft_state["draftLog"], key=lambda e: e.get("pick", 0)):
                pick_num = entry.get("pick", 0)
                rd = (pick_num - 1) // 10 + 1
                slot = (pick_num - 1) % 10 + 1
                j = "*" if entry.get("isJesse") else ""
                draft_lines.append(
                    f"  {rd}.{slot:02d}: {entry.get('playerName','?')}({entry.get('pos','?')})→{entry.get('teamOwner','?')}{j}"
                )
            remaining = 40 - len(draft_state["draftLog"])
            if remaining > 0:
                draft_lines.append(f"  ({remaining} remaining)")
            if draft_state.get("tradeLog"):
                draft_lines.append("  TRADES:")
                for t in draft_state["tradeLog"]:
                    draft_lines.append(f"    Jesse traded {t.get('gave','')} to {t.get('to','')} for {t.get('received','')}")

            # Compute post-draft power rankings impact server-side
            try:
                draft_picks = []
                for entry in draft_state["draftLog"]:
                    owner = entry.get("teamOwner", "")
                    tid = _owner_to_team_id(owner)
                    if tid:
                        draft_picks.append({
                            "pick": entry.get("pick"),
                            "player_name": entry.get("playerName", ""),
                            "pos": entry.get("pos", ""),
                            "team_id": tid,
                        })
                trade_log_for_impact = []
                for t in (draft_state.get("tradeLog") or []):
                    to_id = _owner_to_team_id(t.get("to", ""))
                    if to_id:
                        trade_log_for_impact.append({
                            "gave": t.get("gave", ""),
                            "from_team_id": 8,
                            "to_team_id": to_id,
                        })
                if draft_picks:
                    impact = compute_draft_impact(teams, rankings, draft_picks, trade_log_for_impact or None)
                    def _fmt_impact(changes, label):
                        lines = [f"\nPOST-DRAFT {label} (rookies added):"]
                        for tc in changes:
                            ch = ""
                            if tc["rank_change"] > 0:
                                ch = f" UP{tc['rank_change']}"
                            elif tc["rank_change"] < 0:
                                ch = f" DN{abs(tc['rank_change'])}"
                            j = " <<JESSE" if tc["team_id"] == team_id else ""
                            sc = f"+{tc['score_change']}" if tc["score_change"] >= 0 else str(tc["score_change"])
                            lines.append(
                                f"  #{tc['after_rank']} {tc['owner']} S:{tc['after_score']}(was {tc['before_score']},{sc}){ch}{j}"
                            )
                        return lines
                    if impact.get("dynasty_changes"):
                        draft_lines.extend(_fmt_impact(impact["dynasty_changes"], "DYNASTY RANKINGS"))
                    if impact.get("season_changes"):
                        draft_lines.extend(_fmt_impact(impact["season_changes"], "2026 SEASON RANKINGS"))
            except Exception:
                pass

            draft_ctx = "\n".join(draft_lines)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Compact rookie board — top 10
    rookie_lines = []
    for r in ROOKIES_2026[:10]:
        info = rankings.get(normalize_name(r["name"]))
        val  = info["combined"] if info and info.get("combined") else r["ktc_est"]
        rookie_lines.append(f"  #{r['rank']}. {r['name']}({r['pos']}→{r['nfl_team']}) V:{val} {r['notes']}")
    rookie_block = "\n".join(rookie_lines)

    # Load Jesse's strategy brief (editable without code changes)
    try:
        with open(os.path.join(os.path.dirname(__file__), "strategy_brief.md")) as f:
            strategy_brief = f.read()
    except FileNotFoundError:
        strategy_brief = ""

    # ── Build STATIC portion (will be marked cache_control: ephemeral) ──
    # This is everything that doesn't change between consecutive /chat calls
    # within a 5-minute window. Anthropic prompt caching gives ~90% input
    # discount on cache hits, so we keep this block as large and stable as
    # possible.
    try:
        nfl_ctx = build_nfl_context(teams, rankings) or ""
    except Exception:
        nfl_ctx = ""
    try:
        prod_ctx = build_production_context(teams) or ""
    except Exception:
        prod_ctx = ""

    static_sections = [
        "You are an expert dynasty fantasy football advisor for Jesse's 10-team Superflex league (0.5 PPR, NO TE premium). It is May 2026 — the 2026 NFL Draft has happened and rookies have landing spots. Be direct, reference specific players/values and landing-spot context, keep responses concise and actionable. Take Jesse's strategy brief seriously — agree when his reasoning is sound, push back specifically when the rankings/data contradict his convictions.",
        """=== DATA SOURCES (be honest about confidence) ===
Player values below are a weighted blend of TWO ranking sources with DraftSharks as the PRIMARY weight, then combined with production:
  - DraftSharks dynasty composite — PRIMARY (75% weight; 250 players, post-NFL-Draft, analyst-driven, 1/3/5/10yr projections)
  - KTC Superflex (25% weight; live, ~500 players, market-driven trade values)
Plus Sleeper 2025 per-game actuals + 2026 per-game projections (half-PPR).

(FantasyCalc was previously a third source but systematically under-valued QBs in Superflex — KTC and DS price the top of the QB market closer to parity with elite RBs/WRs, FC priced them at ~60% — so it was dropped.)

When a player isn't in DraftSharks (rank > 250), KTC alone is used. Each ranking row shows the source breakdown (KTC:X DS:Y) so source disagreement is visible. The "BUY/SELL gap" block below compares DS to KTC alone (DS is excluded from the "market" side of that comparison so the gap stays meaningful) — call out the methodology difference: KTC is market price, DraftSharks is analyst opinion with multi-year projections.""",
        f"=== DYNASTY RANKINGS (top 50, blended: 65% DS/KTC weighted + 15% 2025 per-game + 20% 2026 per-game proj) ===\n{rankings_block}",
        f"=== 2026 ROOKIES (post-draft, with NFL landing spots) ===\n{rookie_block}",
        f"=== POWER RANKINGS (Dynasty + 2026 Season) ===\n{power_rankings_block}",
        buy_sell_block,
        ds_notes_block,
        f"=== ROSTERS ===\n{league_summary}",
        f"=== JESSE'S STRATEGY BRIEF ===\n{strategy_brief}" if strategy_brief else "",
        nfl_ctx,
        prod_ctx,
        """=== SAVING MEMORIES ===
You have a save_memory tool. Call it AGGRESSIVELY (without asking) whenever Jesse:
- Expresses a strategic decision or stance ("I'm holding Conner until W10", "I won't trade Love")
- Declines or accepts a trade with reasoning
- Sets a target or plan ("I want to draft Stowers at 1.10")
- Shares a player conviction or thesis
- Mentions a roster move, cut, or upcoming decision
- States a rule for trade evaluation
Each memory should be ONE sentence, concrete and self-contained (no pronouns like "him" without context). Better to over-save and prune later than to miss. Don't tell Jesse you saved — just do it inline with your normal response. Don't save trivia, jokes, or short acknowledgements ("ok", "thanks").

=== USING WEB SEARCH ===
You have web_search available with up to 10 uses per turn. Use it AGGRESSIVELY — the data baked into this prompt is a snapshot; reality changes daily. Search proactively (don't wait to be told) whenever the user's question touches:
- Specific players: current injury status, snap %, target share, recent game logs, depth-chart position, recent news (last 14 days)
- Teams: coaching changes (HC/OC/DC hires/fires), scheme shifts, depth-chart battles, beat-writer reporting
- Rookies / 2nd-year players: training camp buzz, preseason usage, OTA reports, beat-writer rankings
- Trades / rumors / contract situations (extensions, holdouts, free agency moves)
- Anything where your training data could be stale (cutoff ~Jan 2026; today is May 2026)

When answering a player-specific question, search FIRST, then synthesize the dynasty-value implications using the rankings/rosters/production data in this prompt. Cite sources inline (e.g., "per ESPN") so the user can verify.

Do NOT use a search for: pure trade-math questions (use the rankings/values in this prompt), questions Jesse already pasted into chat, or generic fantasy strategy that doesn't reference current events.""",
    ]
    system_static = "\n\n".join(s for s in static_sections if s)

    # ── Build DYNAMIC portion (NOT cached — changes per request) ──
    dynamic_sections = []
    if draft_ctx:
        dynamic_sections.append(draft_ctx)
    if app_context:
        dynamic_sections.append(f"=== LIVE APP STATE ===\n{app_context}")
    try:
        prior_memories = memory_store.get_recent_memories(limit=100)
        memories_block = memory_store.format_memories_block(prior_memories)
        if memories_block:
            dynamic_sections.append(memories_block)
    except Exception:
        pass
    try:
        prior_messages = memory_store.get_recent_messages(limit=50)
        history_block = memory_store.format_messages_block(prior_messages, session_id)
        if history_block:
            dynamic_sections.append(history_block)
    except Exception:
        pass
    system_dynamic = "\n\n".join(dynamic_sections) if dynamic_sections else ""

    # System param as a list of content blocks — first block is marked
    # cache_control:ephemeral so Anthropic caches it for ~5 minutes.
    # On a cache hit, the cached portion costs ~10% of its normal input price.
    system_blocks = [
        {"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}}
    ]
    if system_dynamic:
        system_blocks.append({"type": "text", "text": system_dynamic})

    # Web search tool (server-side, Anthropic handles execution) + save_memory (client-side)
    tools = [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 10,
        },
        {
            "name": "save_memory",
            "description": "Save a fact, decision, or stance about Jesse's dynasty team that should be remembered in future conversations. Call this proactively when Jesse expresses a decision, declines/accepts a trade with reasoning, sets a target, or shares a conviction. One sentence per memory, concrete and self-contained.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "The fact/decision/stance to remember. One sentence, self-contained.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["trade", "roster", "strategy", "player", "draft", "other"],
                        "description": "Category for the memory.",
                    },
                },
                "required": ["note"],
            },
        },
    ]

    def generate():
        accumulated_text = []  # for persisting final assistant reply
        try:
            current_messages = list(messages)
            max_rounds = 6  # handle pause_turn + tool_use loops

            for _ in range(max_rounds):
                response = None
                last_overload = None
                for attempt_idx, attempt_model in enumerate([PRIMARY_MODEL, FALLBACK_MODEL]):
                    try:
                        with client.messages.stream(
                            model=attempt_model,
                            max_tokens=4096,
                            system=system_blocks,
                            messages=current_messages,
                            tools=tools,
                        ) as stream:
                            if attempt_idx > 0:
                                yield f"data: {json.dumps({'status': 'fallback', 'message': f'{PRIMARY_MODEL} overloaded — using {FALLBACK_MODEL}'})}\n\n"
                            for event in stream:
                                if hasattr(event, 'type'):
                                    if event.type == "content_block_start":
                                        block = getattr(event, 'content_block', None)
                                        if block and getattr(block, 'type', '') == "server_tool_use":
                                            yield f"data: {json.dumps({'status': 'searching'})}\n\n"
                                    elif event.type == "content_block_delta":
                                        delta = getattr(event, 'delta', None)
                                        if delta and getattr(delta, 'type', '') == "text_delta":
                                            accumulated_text.append(delta.text)
                                            yield f"data: {json.dumps({'text': delta.text})}\n\n"

                            response = stream.get_final_message()
                        break  # primary or fallback succeeded
                    except Exception as e:
                        if _is_overload(e) and attempt_idx == 0:
                            last_overload = e
                            continue
                        raise

                if response is None:
                    raise last_overload or RuntimeError("No model response")

                if response.stop_reason == "tool_use":
                    # Handle client-side tools (save_memory). server_tool_use (web_search)
                    # is executed by Anthropic and arrives as pause_turn, not tool_use.
                    tool_results = []
                    for block in response.content:
                        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == "save_memory":
                            inp = getattr(block, "input", {}) or {}
                            ok = memory_store.save_memory(
                                inp.get("note", ""),
                                inp.get("category"),
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": "saved" if ok else "memory store unavailable",
                            })
                    current_messages.append({"role": "assistant", "content": response.content})
                    current_messages.append({"role": "user", "content": tool_results})
                    continue
                elif response.stop_reason == "pause_turn":
                    # Continue the conversation for multi-search queries
                    current_messages.append({"role": "assistant", "content": response.content})
                    continue
                else:
                    break

            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            # Persist this exchange to Supabase (best-effort)
            try:
                last_user = next(
                    (m for m in reversed(messages) if m.get("role") == "user"),
                    None,
                )
                if last_user and isinstance(last_user.get("content"), str):
                    memory_store.append_message(session_id, "user", last_user["content"])
                final_text = "".join(accumulated_text).strip()
                if final_text:
                    memory_store.append_message(session_id, "assistant", final_text)
            except Exception:
                pass

    return Response(stream_with_context(generate()), content_type="text/event-stream")


@app.route("/rankings")
def rankings_page():
    rankings = fetch_all_rankings()
    players = []
    for norm_key, data in rankings.items():
        players.append({
            "name":          data.get("name", norm_key.title()),
            "position":      data.get("position", ""),
            "age":           data.get("age", 0),
            "combined":      data.get("combined", 0),
            "ktc":           data.get("ktc"),
            "rank":          data.get("rank", 999),
            "pos_rank":      data.get("pos_rank", 999),
        })
    players.sort(key=lambda x: x["rank"])
    picks = [
        {"name": k, "ktc": v["ktc"], "note": v["note"]}
        for k, v in PICK_VALUES.items()
    ]
    return render_template("rankings.html", players=players, picks=picks)


STATE_FILE = os.getenv("STATE_FILE", "draft_state.json")

@app.route("/api/draft-state", methods=["GET"])
def get_draft_state_api():
    try:
        with open(STATE_FILE) as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify(None)

@app.route("/api/draft-state", methods=["POST"])
def save_draft_state_api():
    data = request.get_json()
    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)
    return jsonify({"ok": True})


# ── Owner name → ESPN team ID mapping ─────────────────────────────────────────
_OWNER_TEAM_IDS = {
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

def _owner_to_team_id(owner_name: str) -> int | None:
    """Resolve an owner display name to ESPN team ID."""
    if not owner_name:
        return None
    low = owner_name.lower().strip()
    # Exact match
    if low in _OWNER_TEAM_IDS:
        return _OWNER_TEAM_IDS[low]
    # First-name match
    first = low.split()[0] if low else ""
    if first in _OWNER_TEAM_IDS:
        return _OWNER_TEAM_IDS[first]
    # Last-name match
    parts = low.split()
    if len(parts) > 1 and parts[-1] in _OWNER_TEAM_IDS:
        return _OWNER_TEAM_IDS[parts[-1]]
    return None


@app.route("/api/draft-impact", methods=["POST"])
def draft_impact_api():
    """
    Compute power ranking shifts from draft picks + in-draft trades.

    Expects JSON body:
    {
      "draft_log": [{"pick": 1, "playerName": "X", "pos": "RB", "teamOwner": "Jesse", "isJesse": true}],
      "trade_log": [{"gave": "1.01", "to": "Brad", "from_team_id": 8}],
      "roster_trades": [{"player": "X", "fromTeam": "Team A", "toTeam": "Team B"}]
    }
    """
    body = request.get_json() or {}

    try:
        data = fetch_league_data()
        teams = parse_league(data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch ESPN data: {e}"}), 500

    rankings = fetch_all_rankings()
    enhanced_dynasty = build_enhanced_rankings(rankings, teams, mode="dynasty")
    enhanced_season = build_enhanced_rankings(rankings, teams, mode="season")

    # Convert draft_log to format expected by compute_draft_impact
    raw_draft = body.get("draft_log", [])
    draft_picks = []
    for entry in raw_draft:
        owner = entry.get("teamOwner", "")
        team_id = _owner_to_team_id(owner)
        if not team_id:
            continue
        draft_picks.append({
            "pick": entry.get("pick"),
            "player_name": entry.get("playerName", ""),
            "pos": entry.get("pos", ""),
            "ktc_est": entry.get("ktcEst", 3000),
            "team_id": team_id,
        })

    # Convert trade_log
    raw_trades = body.get("trade_log", [])
    trade_log = []
    for entry in raw_trades:
        from_id = 8  # Jesse is always the one trading from in this UI
        to_owner = entry.get("to", "")
        to_id = _owner_to_team_id(to_owner)
        if to_id:
            trade_log.append({
                "gave": entry.get("gave", ""),
                "from_team_id": from_id,
                "to_team_id": to_id,
            })

    try:
        result = compute_draft_impact(
            teams, enhanced_dynasty, draft_picks, trade_log or None,
            season_rankings=enhanced_season,
        )
        response = {
            "team_changes": result.get("team_changes", result.get("dynasty_changes", [])),
            "season_changes": result.get("season_changes", []),
            "dynasty_changes": result.get("dynasty_changes", result.get("team_changes", [])),
        }
        return jsonify(response)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/draft-preview", methods=["POST"])
def draft_preview_api():
    """
    Pre-compute power ranking impact of drafting each available player.

    Expects JSON body:
    {
      "pick_number": 1,
      "team_id": 8,
      "available": [{"name": "...", "pos": "RB"}],
      "draft_log": [...],  // existing draft picks
      "trade_log": [...]   // existing pick trades
    }

    Returns: { "impacts": { "Player Name": { "dynasty_score": +1.2, "dynasty_rank": +1, "season_score": +0.8, "season_rank": 0 } } }
    """
    import copy as _copy

    body = request.get_json() or {}
    pick_number = body.get("pick_number", 1)
    team_id = int(body.get("team_id", 8))
    available = body.get("available", [])
    raw_draft = body.get("draft_log", [])
    raw_trades = body.get("trade_log", [])

    try:
        data = fetch_league_data()
        teams = parse_league(data)
    except Exception as e:
        return jsonify({"error": f"ESPN fetch failed: {e}"}), 500

    rankings = fetch_all_rankings()
    enhanced_dynasty = build_enhanced_rankings(rankings, teams, mode="dynasty")
    enhanced_season = build_enhanced_rankings(rankings, teams, mode="season")

    # Apply existing draft picks to get "before" state
    base_teams = _copy.deepcopy(teams)
    # Use copies of rankings so we can add rookie entries without polluting originals
    enhanced_dynasty = dict(enhanced_dynasty)
    enhanced_season = dict(enhanced_season)
    for entry in raw_draft:
        owner = entry.get("teamOwner", "")
        tid = _owner_to_team_id(owner)
        if not tid:
            continue
        team = next((t for t in base_teams if t["id"] == tid), None)
        if team:
            pname = entry.get("playerName", "")
            team["roster"].append({
                "name": pname,
                "position": entry.get("pos", ""),
                "slot": "BE", "slot_id": 20, "player_id": 0,
                "fpts_2026_proj": 100,  # assume rookies have projections
            })
            # Remove the used pick from the team's holds
            epick = entry.get("pick", 0)
            if epick:
                rd = (epick - 1) // 10 + 1
                round_labels = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
                round_str = round_labels.get(rd, f"{rd}th")
                for pl in list(team.get("picks_holds", [])):
                    if f"2026 {round_str}" in pl:
                        team["picks_holds"].remove(pl)
                        break
            # Ensure rookie has a ranking entry in BOTH dynasty and season rankings
            # 1st-round picks get a tiered premium boost reflecting scarcity.
            nkey = normalize_name(pname)
            base_ktc = entry.get("ktcEst", 3000)
            boosted_ktc = int(base_ktc * _rookie_draft_premium(epick))
            premium = _rookie_draft_premium(epick)
            for rdict in (enhanced_dynasty, enhanced_season):
                existing = rdict.get(nkey)
                if existing and existing.get("combined"):
                    rdict[nkey] = dict(existing)
                    if premium >= 1.0:
                        # Boost: use higher of existing or boosted
                        rdict[nkey]["combined"] = max(existing["combined"], boosted_ktc)
                    else:
                        # Penalty: cap down to penalized value
                        rdict[nkey]["combined"] = min(existing["combined"], boosted_ktc)
                    if not existing.get("age") or existing["age"] <= 0:
                        rdict[nkey]["age"] = 21
                else:
                    rdict[nkey] = {
                        "name": pname, "combined": boosted_ktc,
                        "position": entry.get("pos", ""), "age": 21,
                        "rank": 999, "pos_rank": 999,
                    }

    # Remove the current pick from base_teams too — it's being used regardless,
    # so both before/after should have the same capital baseline.
    my_base = next((t for t in base_teams if t["id"] == team_id), None)
    if my_base:
        rd = (pick_number - 1) // 10 + 1
        round_labels = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
        round_str = round_labels.get(rd, f"{rd}th")
        for pl in list(my_base.get("picks_holds", [])):
            if f"2026 {round_str}" in pl:
                my_base["picks_holds"].remove(pl)
                break

    # Compute "before" rankings (with existing picks applied, current pick removed)
    before_dyn = compute_power_rankings(base_teams, enhanced_dynasty, mode="dynasty", season_rankings=enhanced_season)
    before_sea = compute_power_rankings(base_teams, enhanced_season, mode="season")

    my_before_dyn = next((t for t in before_dyn if t["team_id"] == team_id), None)
    my_before_sea = next((t for t in before_sea if t["team_id"] == team_id), None)

    if not my_before_dyn or not my_before_sea:
        return jsonify({"impacts": {}})

    # For each available player, simulate drafting and compute impact
    impacts = {}
    for player in available[:25]:  # limit to top 25 for performance
        pname = player.get("name", "")
        ppos = player.get("pos", "")
        ktc_est = player.get("ktc_est", 1000)

        sim_teams = _copy.deepcopy(base_teams)
        sim_rankings_dyn = dict(enhanced_dynasty)
        sim_rankings_sea = dict(enhanced_season)

        # Add player to team
        my_sim = next((t for t in sim_teams if t["id"] == team_id), None)
        if not my_sim:
            continue
        my_sim["roster"].append({
            "name": pname, "position": ppos,
            "slot": "BE", "slot_id": 20, "player_id": 0,
            "fpts_2026_proj": 100,
        })

        # Ensure ranking entry — tiered draft premium/penalty + fix missing age
        premium = _rookie_draft_premium(pick_number)
        boosted_ktc = int(ktc_est * premium)
        nkey = normalize_name(pname)
        for rdict in (sim_rankings_dyn, sim_rankings_sea):
            existing = rdict.get(nkey)
            if existing and existing.get("combined"):
                rdict[nkey] = dict(existing)
                if premium >= 1.0:
                    rdict[nkey]["combined"] = max(existing["combined"], boosted_ktc)
                else:
                    rdict[nkey]["combined"] = min(existing["combined"], boosted_ktc)
                if not existing.get("age") or existing["age"] <= 0:
                    rdict[nkey]["age"] = 21
            else:
                rdict[nkey] = {
                    "name": pname, "combined": boosted_ktc,
                    "position": ppos, "age": 21,
                    "rank": 999, "pos_rank": 999,
                }

        # Pick already removed from baseline — no need to remove again

        after_dyn = compute_power_rankings(sim_teams, sim_rankings_dyn, mode="dynasty", season_rankings=sim_rankings_sea)
        after_sea = compute_power_rankings(sim_teams, sim_rankings_sea, mode="season")

        my_after_dyn = next((t for t in after_dyn if t["team_id"] == team_id), None)
        my_after_sea = next((t for t in after_sea if t["team_id"] == team_id), None)

        if my_after_dyn and my_after_sea:
            impacts[pname] = {
                "dynasty_score": round(my_after_dyn["power_score"] - my_before_dyn["power_score"], 1),
                "dynasty_before_rank": my_before_dyn["power_rank"],
                "dynasty_after_rank": my_after_dyn["power_rank"],
                "season_score": round(my_after_sea["power_score"] - my_before_sea["power_score"], 1),
                "season_before_rank": my_before_sea["power_rank"],
                "season_after_rank": my_after_sea["power_rank"],
            }

    # Compute the trade value of the current pick for the trade signal
    pick_trade_value = 0
    if my_base:
        rd = (pick_number - 1) // 10 + 1
        slot_num = ((pick_number - 1) % 10) + 1
        slot_label = f"{rd}.{slot_num:02d}"
        # Find the specific pick by matching slot label in the note field
        for pv_name, pv_data in PICK_VALUES.items():
            note = pv_data.get("note", "")
            if slot_label in note:
                pick_trade_value = pv_data.get("ktc", 0)
                break
        # Fallback: use average for the round if specific slot not found
        if not pick_trade_value:
            round_labels = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
            round_str = round_labels.get(rd, f"{rd}th")
            round_vals = [pv_data.get("ktc", 0) for pv_name, pv_data in PICK_VALUES.items()
                          if f"2026 {round_str}" in pv_name]
            if round_vals:
                pick_trade_value = sum(round_vals) // len(round_vals)

    # Best available player's value
    best_player_value = max((p.get("ktc_est", 0) for p in available[:25]), default=0)

    # Trade target recommendations — who to trade with and for what position
    trade_targets = []
    if pick_trade_value > 0 and my_before_dyn:
        my_needs = my_before_dyn.get("needs", [])
        # Also check which positions are weakest by looking at pos_scores
        pos_scores = my_before_dyn.get("pos_scores", {})
        weak_positions = []
        for pos in ["QB", "RB", "WR", "TE"]:
            ps = pos_scores.get(pos, {})
            count = ps.get("count", 0)
            top_val = ps.get("top_value", 0)
            # Weak if in needs, or if top player is low value, or thin depth
            if pos in my_needs:
                weak_positions.append((pos, 3))  # high priority
            elif count <= 1:
                weak_positions.append((pos, 2))
            elif top_val < 4000:
                weak_positions.append((pos, 1))
        weak_positions.sort(key=lambda x: -x[1])
        target_positions = [p for p, _ in weak_positions[:3]] or ["QB", "RB", "WR"]

        # Find trade partners: teams with surplus at positions we need
        value_floor = int(pick_trade_value * 0.6)
        value_ceiling = int(pick_trade_value * 1.3)
        for t_rank in before_dyn:
            if t_rank["team_id"] == team_id:
                continue
            t_team = next((t for t in base_teams if t["id"] == t_rank["team_id"]), None)
            if not t_team:
                continue
            owner_first = t_rank["owner"].split()[0]
            for pos in target_positions:
                # Find players on this team at this position in the value range
                candidates = []
                for p in t_team["roster"]:
                    if p["position"] != pos:
                        continue
                    pval = enhanced_dynasty.get(normalize_name(p["name"]), {}).get("combined", 0)
                    age = enhanced_dynasty.get(normalize_name(p["name"]), {}).get("age", 30)
                    if value_floor <= pval <= value_ceiling and age <= 28:
                        candidates.append({
                            "name": p["name"], "value": pval, "age": age,
                        })
                if candidates:
                    candidates.sort(key=lambda c: -c["value"])
                    best = candidates[0]
                    trade_targets.append({
                        "owner": owner_first,
                        "pos": pos,
                        "player": best["name"],
                        "player_value": best["value"],
                        "age": best["age"],
                    })

        # Sort by value match (closest to pick value = best fit)
        trade_targets.sort(key=lambda t: abs(t["player_value"] - pick_trade_value))
        trade_targets = trade_targets[:4]  # top 4 suggestions

    return jsonify({
        "impacts": impacts,
        "pick_trade_value": pick_trade_value,
        "best_available_value": best_player_value,
        "trade_targets": trade_targets,
    })


# ── Buy / Sell Targets ─────────────────────────────────────────────────────────
#
# Uses DraftSharks dynasty rankings (percentile) vs KTC market percentile.
# BUY: DraftSharks scores player higher than KTC → market is undervaluing
# SELL: KTC scores player higher than DraftSharks → market is overvaluing
import csv

_DRAFTSHARKS_FILE = os.path.join(os.path.dirname(__file__), "ofantasy_rankings.csv")


def _compute_regression_signals(rankings: dict, min_ktc: int = 1500) -> dict:
    """
    Fit DS = a*KTC + b across players with both source values above min_ktc,
    compute per-player residuals and standardized z-scores.

    z = (actual_DS - predicted_DS) / σ(residuals)

    This isolates player-specific source disagreement from systematic
    differences in the two sources' curves (scale, shape, methodology
    biases all get absorbed into the slope/intercept).

    min_ktc=1500 filters out the deep-bench regime where the regression
    line predicts negative DS values and produces meaningless residuals.

    Returns {norm_name: {"z": float, "residual": float, "predicted_ds": float}}
    """
    pairs = []
    for nkey, p in rankings.items():
        ktc = p.get("ktc", 0)
        ds = p.get("ds", 0)
        if ktc >= min_ktc and ds > 0:
            pairs.append((nkey, ktc, ds))

    if len(pairs) < 10:
        return {}

    xs = [p[1] for p in pairs]
    ys = [p[2] for p in pairs]
    n = len(pairs)

    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return {}
    a = num / den
    b = my - a * mx

    resids = [ys[i] - (a * xs[i] + b) for i in range(n)]
    mr = sum(resids) / n
    sigma = (sum((r - mr) ** 2 for r in resids) / (n - 1)) ** 0.5 if n > 1 else 1.0
    if sigma == 0:
        return {}

    return {
        pairs[i][0]: {
            "z":            resids[i] / sigma,
            "residual":     resids[i],
            "predicted_ds": a * xs[i] + b,
        }
        for i in range(n)
    }


def _rank_to_percentile(rank: float, total: int) -> float:
    """Convert rank (1=best) to percentile score (100=best, 0=worst)."""
    if total <= 1:
        return 100.0
    return round(100 * (1 - (rank - 1) / (total - 1)), 1)


def _load_expert_scores() -> dict:
    """Load DraftSharks dynasty rankings, normalize to 0-100 percentile.
    Returns {norm_name: {expert_score, pos, analysis}}.
    """
    raw = {}
    if not os.path.exists(_DRAFTSHARKS_FILE):
        return raw
    with open(_DRAFTSHARKS_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Player", "").strip()
            if not name:
                continue
            nkey = normalize_name(name)
            try:
                rank = int(row.get("Rank", 0))
            except (ValueError, TypeError):
                continue
            raw[nkey] = {
                "rank": rank,
                "pos": row.get("Fantsy Position", ""),
                "team": row.get("Team", ""),
                "analysis": row.get("DS Analysis", ""),
            }

    total = len(raw)
    result = {}
    for nkey, v in raw.items():
        result[nkey] = {
            "expert_score": _rank_to_percentile(v["rank"], total),
            "ds_rank": v["rank"],
            "pos": v["pos"],
            "analysis": v["analysis"],
        }
    return result


@app.route("/buy-sell")
def buy_sell_page():
    try:
        data = fetch_league_data()
        teams = parse_league(data)
        rankings = fetch_all_rankings()
        my_team_id = int(request.args.get("team_id", 8))
        # Default threshold = 1.5σ on standardized residuals.
        # |z|>1.5 ≈ top 13% of disagreement; |z|>2.0 ≈ top 5%.
        threshold = float(request.args.get("threshold", 1.5))

        # Fit DS = a*KTC + b across all eligible players, compute z-scores
        signals = _compute_regression_signals(rankings, min_ktc=1500)

        buy_signals = []
        sell_signals = []

        for team in teams:
            is_my_team = team["id"] == my_team_id
            first = team["owner"].split()[0]
            display_owner = OWNER_DISPLAY.get(first, first)

            for p in team["roster"]:
                nkey = normalize_name(p["name"])
                r = rankings.get(nkey, {})
                sig = signals.get(nkey)
                if not sig:
                    continue  # not in regression universe (deep bench or missing source)

                ktc_val = r.get("ktc", 0)
                ds_val = r.get("ds", 0)
                ds_rank = r.get("ds_rank", 0)
                pos = p.get("position", "")
                age = r.get("age", 0)
                z = sig["z"]

                if pos not in ("QB", "RB", "WR", "TE"):
                    continue

                # Looser threshold for own team so holds/sells always surface
                min_z = threshold * 0.6 if is_my_team else threshold
                if abs(z) < min_z:
                    continue

                source_str = f"DS #{ds_rank}" if ds_rank else ""
                z_rounded = round(z, 2)
                raw_gap = ds_val - ktc_val

                entry = {
                    "name": p["name"],
                    "pos": pos,
                    "age": round(age, 1) if age > 0 else None,
                    "team_name": team["name"],
                    "owner": display_owner,
                    "is_my_team": is_my_team,
                    "ktc_value": ktc_val,
                    "ktc_score": ktc_val,       # template field — shows KTC raw value
                    "expert_score": ds_val,     # template field — shows DS raw value
                    "gap": z_rounded,           # template field — now z-score (σ units)
                    "raw_gap": raw_gap,
                    "source_detail": source_str,
                    "analysis": r.get("ds_analysis", ""),
                }

                if z > 0:
                    # BUY: DS values significantly higher than the regression predicts
                    age_cutoffs = {"RB": 28, "WR": 30, "TE": 31, "QB": 32}
                    max_age = age_cutoffs.get(pos, 30)
                    if age and age >= max_age and not is_my_team:
                        continue

                    tags = []
                    if is_my_team:
                        tags.append("HOLD")
                    elif age and age < 25:
                        if abs(z) >= 2.5:
                            tags.append("BREAKOUT CANDIDATE")
                        else:
                            tags.append("YOUNG VALUE")
                    elif abs(z) >= 2.5:
                        tags.append("UNDERVALUED")
                    elif abs(z) >= 2.0:
                        tags.append("VALUE BUY")
                    else:
                        tags.append("BUY WATCH")
                    entry["tags"] = tags
                    entry["reason"] = (
                        f"DS:{ds_val} vs KTC:{ktc_val} — "
                        f"z=+{z_rounded}σ (DS {abs(round(sig['residual']))} above expected). "
                        f"{source_str}"
                    )
                    entry["sort_score"] = z
                    buy_signals.append(entry)

                elif z < 0:
                    # SELL: DS values significantly lower than the regression predicts
                    tags = []
                    if is_my_team:
                        tags.append("YOUR TEAM")
                    if age and age >= 28:
                        tags.append("AGING")
                    if abs(z) >= 2.5:
                        tags.append("OVERVALUED")
                    elif abs(z) >= 2.0:
                        tags.append("SELL HIGH")
                    else:
                        tags.append("WATCH")
                    entry["tags"] = tags
                    entry["reason"] = (
                        f"DS:{ds_val} vs KTC:{ktc_val} — "
                        f"z={z_rounded}σ (DS {abs(round(sig['residual']))} below expected). "
                        f"{source_str}"
                    )
                    entry["sort_score"] = abs(z)
                    sell_signals.append(entry)

        buy_signals.sort(key=lambda x: x["sort_score"], reverse=True)
        sell_signals.sort(key=lambda x: x["sort_score"], reverse=True)

        return render_template(
            "buy_sell.html",
            buy_signals=buy_signals,
            sell_signals=sell_signals,
            threshold=threshold,
            my_team_id=my_team_id,
            teams=teams,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return render_template(
            "buy_sell.html",
            buy_signals=[],
            sell_signals=[],
            threshold=8,
            my_team_id=8,
            teams=[],
            error=str(e),
        )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
