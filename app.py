import os
import re
import json
import anthropic
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from dotenv import load_dotenv
from espn_data import fetch_league_data, parse_league, build_league_prompt
from dynasty_data import fetch_all_rankings, build_rankings_summary, normalize_name, PICK_VALUES
from rookies_data import ROOKIES_2026

load_dotenv()

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-4-20250514"

# ── Server-side trade value validator ─────────────────────────────────────────
# Generic round-pick estimates (when we can't match a specific named pick)
_ROUND_EST = {"1st": 4000, "2nd": 1700, "3rd": 750, "4th": 350, "5th": 200}

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
        return render_template("index.html", teams=teams, error=None)
    except Exception as e:
        return render_template("index.html", teams=[], error=str(e))


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
All player values use a Combined Dynasty Value (0–9999) — the average of KTC, Dynasty Daddy, and FantasyCalc, each normalized to the same scale.
Pick values are KTC-calibrated.

{style["value_rule"]}

=== CURRENT DYNASTY RANKINGS (March 2026 — combined from KTC + Dynasty Daddy + FantasyCalc) ===
{rankings_block}

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
        message = client.messages.create(
            model=MODEL,
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
        return jsonify({"trades": trades, "my_team": my_team["name"], "trade_style": style["label"]})

    except json.JSONDecodeError as e:
        # Try to salvage partial JSON
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start != -1 and end > start:
            try:
                trades = json.loads(raw[start:end])
                trades = _filter_trades(trades, style_key, rankings)
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

    # Enrich rookies with live combined values (KTC + Dynasty Daddy + FantasyCalc)
    rankings = fetch_all_rankings()
    enriched = []
    for r in ROOKIES_2026:
        info = rankings.get(normalize_name(r["name"]))
        if info and info.get("combined", 0) > 0:
            entry = {
                **r,
                "dyn_value":   info["combined"],
                "ktc_live":    info.get("ktc"),
                "dd_live":     info.get("dd"),
                "fc_live":     info.get("fc"),
                "src_count":   info.get("sources_count", 1),
                "live":        True,
            }
        else:
            entry = {
                **r,
                "dyn_value":   r["ktc_est"],
                "ktc_live":    None,
                "dd_live":     None,
                "fc_live":     None,
                "src_count":   0,
                "live":        False,
            }
        enriched.append(entry)

    return render_template("draft_board.html", rookies=enriched, owners=owners)


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

Give Jesse a clear, direct recommendation for this pick. Name the specific player he should take and explain why in 2-3 sentences, referencing dynasty values and his roster needs. If there's a close call between 2 players, address it.

Return ONLY a JSON object — no markdown, no code fences:
{{
  "pick": "Player Name (POS)",
  "reasoning": "2-3 sentences explaining why this is the right call for Jesse's dynasty",
  "alternatives": ["Alternative 1 (POS) — one-line reason", "Alternative 2 (POS) — one-line reason"]
}}"""

    try:
        message = client.messages.create(
            model=MODEL,
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
For each trade, calculate: Jesse gives 1.03 (~5400 VAL) → Jesse receives [assets] (VAL total). Ratio = receives/5400. Confirm it meets {style["label"]} requirement.
scenario_type for all trades: "Sell 1.03 — Trade Up Package" """

    else:  # 1.10
        _110_status = "Jesse has ALREADY MADE this pick." if 10 in _jesse_made else "This pick has NOT been made yet."
        _haul_str = ", ".join(f"{e['playerName']} ({e['pos']})" for e in _jesse_haul)
        _prior = f"Jesse has already drafted: {_haul_str}. " if _jesse_haul else ""
        pick_task = f"""=== THE PICK BEING SOLD: 1.10 (Patrick's pick, held by Jesse) ===
{_110_status} {_prior}
Jesse is entertaining offers for the 1.10 pick — last pick of round 1, best player available (Combined Value ~3000).
Jesse keeps 1.01 and 1.03 picks (or has already used them) — only 1.10 is being moved.

FLOOR: Jesse receives a minimum of ~3000 Combined Value for 1.10.
{_needs_now}
WHAT IS REALISTIC FOR A 1.10 PICK (~3000 value)? This is a late 1st — treat it accordingly:
- A mid-tier starter (age 24–27) who is underperforming on their current team
- A 2026 2nd + a 2027 3rd
- A package of future mid-round picks
It does NOT get: top-40 dynasty players, starting QBs with significant upside, or elite young players. Those are worth far more.
SANITY CHECK BEFORE INCLUDING EACH TRADE:
1. Does the trading partner have enough surplus at the position(s) Jesse receives? (Don't have a team trade away their only QB if they need QB depth.)
2. Does the trading partner actually WANT a late 1st round pick — do they have a pick need or rebuild motivation?
3. Is the player Jesse receives truly surplus for that team — not a starter they depend on?
Do NOT leave Jesse without a QB1 if Purdy is involved.

TASK ({style["label"].upper()}): Generate exactly 5 trade scenarios from 5 DIFFERENT teams involving Jesse moving 1.10. {style["task_note"]}
Each scenario must come from a different team. Be creative but realistic.
For each trade, calculate: Jesse gives 1.10 + any additional assets (VAL total) → Jesse receives [assets] (VAL total). Ratio = receives/gives. Confirm it meets {style["label"]} requirement.
scenario_type for all trades: "Sell 1.10 — Package Deal" """

    draft_ctx_section = f"\n{draft_ctx}\n" if draft_ctx else ""

    prompt = f"""You are an expert dynasty fantasy football trade analyst. It is DRAFT DAY for the 2026 NFL Draft (late April 2026).

=== LEAGUE FORMAT ===
Superflex (OP slot), NO tight end premium, 0.5 PPR, 10 teams.
In Superflex leagues, QBs are scarce and extremely valuable.

=== VALUE RULES — READ CAREFULLY ===
Player values use a Combined Dynasty Value (0–9999) — averaged from KTC, Dynasty Daddy, and FantasyCalc (each normalized).
Pick values are KTC-calibrated.

CRITICAL — TRADE MARKET REALITY:
Combined Dynasty Values are reference points, NOT literal transaction prices. Real dynasty trades happen at negotiated discounts.
- BACKUP QBs and DEPTH players trade at 40–60% of their KTC rank — most teams won't pay full price for a backup they don't need.
- A team should NEVER trade away a position they are thin at. Before including a trade partner giving a player, verify that player is genuinely SURPLUS for them (check their roster above).
- PICKS trade close to face value since they are liquid assets.
- STARTERS at positions of need trade closer to full KTC value.
Use the Combined Value rankings as relative tier guides, but build trades around what owners would REALISTICALLY give up.
{draft_ctx_section}
{style["value_rule"]}

=== CURRENT DYNASTY RANKINGS (KTC + Dynasty Daddy + FantasyCalc combined) ===
{rankings_block}

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
        message = client.messages.create(
            model=MODEL,
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

    prompt = f"""You are an expert dynasty fantasy football trade analyst evaluating an incoming offer on 2026 NFL draft day.

=== LEAGUE FORMAT ===
Superflex (OP slot), NO TE premium, 0.5 PPR, 10 teams.

=== DRAFT DAY CONTEXT ===
2026 NFL Draft is happening today. Jesse's picks and values:
- 1.01 = Jeremiyah Love (#1 dynasty prospect, KTC 7101)
- 1.03 (Alex's pick, held by Jesse) = Carnell Tate or Jordyn Tyson (KTC ~5400)
- 1.10 (Patrick's pick, held by Jesse) = KTC 3000
- Jesse's QB1: Brock Purdy — must not be left without a QB1 in Superflex

=== CURRENT DYNASTY RANKINGS (KTC + Dynasty Daddy + FantasyCalc combined) ===
{rankings_block}

=== FULL LEAGUE ROSTERS ===
{league_summary}

=== MY TEAM ===
Team: {my_team['name']} | Owner: Jesse

=== OFFER TO EVALUATE ===
{offer_text}

Evaluate this offer using Combined Dynasty Values (averaged from KTC/DynastyDaddy/FantasyCalc). For any player not in the rankings, estimate based on position/age and note it. For picks, use KTC values.

Return ONLY a single JSON object — no markdown, no code fences:
{{
  "offer_summary": "Brief one-line description of the offer",
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
        message = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        evaluation = json.loads(raw)
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
    return render_template("advisor.html", teams=teams)


@app.route("/chat", methods=["POST"])
def chat():
    body        = request.get_json()
    messages    = body.get("messages", [])   # [{role, content}] full history
    team_id     = int(body.get("team_id", 8))
    app_context = body.get("app_context", "").strip()

    try:
        data  = fetch_league_data()
        teams = parse_league(data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch ESPN data: {e}"}), 500

    my_team = next((t for t in teams if t["id"] == team_id), None)
    if not my_team:
        return jsonify({"error": "Team not found"}), 400

    rankings       = fetch_all_rankings()
    league_summary = build_league_prompt(teams, team_id, rankings)
    rankings_block = build_rankings_summary(rankings)

    # Build a compact rookie board string for the advisor context
    rookie_lines = []
    for r in ROOKIES_2026[:20]:   # top 20 is enough context
        info = rankings.get(normalize_name(r["name"]))
        val  = info["combined"] if info and info.get("combined") else r["ktc_est"]
        rookie_lines.append(
            f"  #{r['rank']}. {r['name']} ({r['pos']}, {r['college']}) — VAL:{val} | {r['notes']}"
        )
    rookie_block = "\n".join(rookie_lines)

    system = f"""You are an expert dynasty fantasy football advisor. The user is Jesse, and you have full context on his league, roster, situation, and the 2026 rookie class. Answer conversationally, be direct, and reference specific players and values. Always use 2026 context — the 2025 NFL season has ended.

=== CURRENT DATE & CONTEXT ===
March 2026. The 2025 NFL season is OVER. The 2026 NFL Draft is in April 2026 and has NOT happened yet. Rookies below are prospects, not yet drafted.

=== LEAGUE FORMAT ===
Superflex (OP slot), NO TE premium, 0.5 PPR, 10 teams.

=== PLAYER VALUES ===
Combined Dynasty Value (0–9999) = average of KTC, Dynasty Daddy, and FantasyCalc, each normalized. Pick values are KTC-calibrated.

=== CURRENT DYNASTY RANKINGS (top 40 established players) ===
{rankings_block}

=== 2026 ROOKIE CLASS (not yet drafted — April 2026) ===
{rookie_block}

=== FULL LEAGUE ROSTERS (ESPN, post-2025 season) ===
{league_summary}

=== JESSE'S SITUATION ===
Team: {my_team['name']} | Record: {my_team['wins']}-{my_team['losses']}

KEY ASSETS:
- 2026 1st picks: 1.01 (~7100, projects to Jeremiyah Love), 1.03 (Alex's, ~5400, projects to Carnell Tate/Tyson), 1.10 (Patrick's, ~3000)
- Strong 2027 pick stack (Jesse's own 1st, plus Driscoll's 1st held by Jesse)
- Young core: Quinshon Judkins (RB, 22), Luther Burden III (WR, 22), Chris Olave (WR, 25), Jordan Addison (WR, 24)
- QB1: Brock Purdy — only real Superflex starter (long-term vulnerability)

CONSTRAINTS TO ALWAYS RESPECT:
- Never leave Jesse without a QB1 in any trade scenario
- 2026 1.01 and 1.03 require top-15 dynasty value equivalent to move
- 2027 1sts tradeable for real value, especially to solve the QB need

Be specific. Reference actual players and values. Keep responses focused and actionable."""

    if app_context:
        system += f"\n\n=== LIVE APP STATE (real-time from user's browser) ===\n{app_context}"

    def generate():
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=2048,
                system=system,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

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
            "dd":            data.get("dd"),
            "fc":            data.get("fc"),
            "sources_count": data.get("sources_count", 1),
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
