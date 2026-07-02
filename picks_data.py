"""
Draft pick ownership for 2026–2027 (future tradeable picks only).
Source: league Google Sheet. Parsed March 2026.

Abbreviation key used in source sheet:
  own=still holds original pick, JB=Jacob Berkowitz, LGAS/PS=Patrick Stevenson,
  AW=Alex Wall, SCHU=Schueler, BK=Brad Komar, TL=Team Lubin, SD=Sarah Driscoll,
  AF=Alexa Feldman, DENT=Grant Denton, JT=Jesse/GzTz (Team 8)

ESPN team IDs (confirmed from API):
  1=Patrick Stevenson, 2=Alexa Feldman, 3=Alex Wall, 4=Bradley Komar,
  5=Nathaniel Lubin, 6=John Schueler, 7=Grant Denton, 8=Jesse/GzTz,
  9=Sarah Driscoll, 10=Jacob Berkowitz
"""

# Maps ESPN team_id -> picks they currently HOLD (available to trade)
# Includes both their own un-traded picks AND picks acquired from others.
TEAM_PICKS = {
    1: {  # Patrick Stevenson (LaGarrette Blount AllStars)
        "holds": [
            "Patrick's 2026 3rd",
            "Jesse's 2026 2nd",    # acquired from Jesse
            "Alex's 2026 3rd",     # acquired from Alex Wall
            "Patrick's 2027 1st",
            "Patrick's 2027 2nd",
            "Patrick's 2027 3rd",
            "Patrick's 2027 4th",
            "Jesse's 2027 2nd",    # acquired from Jesse (Omar Cooper Jr. pick trade)
        ],
        "traded_away": [
            "Patrick's 2026 1st -> Jesse (GzTz)",
            "Patrick's 2026 2nd -> Alex Wall",
            "Patrick's 2026 4th -> Alex Wall",
        ],
    },
    2: {  # Alexa Feldman
        "holds": [
            "Alexa's 2026 2nd",
            "Alexa's 2026 4th",
            "Alex's 2026 4th",     # acquired from Alex Wall
            "Brad's 2027 3rd",     # acquired from Brad Komar
            "Alexa's 2027 1st",
            "Alexa's 2027 3rd",
            "Alexa's 2027 4th",
        ],
        "traded_away": [
            "Alexa's 2026 1st -> Alex Wall",
            "Alexa's 2026 3rd -> Brad Komar",
            "Alexa's 2027 2nd -> Jesse (GzTz) (Carnell Tate / Tyler Shough trade)",
        ],
    },
    3: {  # Alex Wall
        "holds": [
            "Jesse's 2026 3rd",    # acquired from Jesse
            "Jesse's 2026 4th",    # acquired from Jesse
            "Alexa's 2026 1st",    # acquired from Alexa
            "Patrick's 2026 2nd",  # acquired from Patrick
            "Patrick's 2026 4th",  # acquired from Patrick
            "Alex's 2027 1st",
            "Alex's 2027 2nd",
            "Alex's 2027 3rd",
            "Alex's 2027 4th",
        ],
        "traded_away": [
            "Alex's 2026 1st -> Jesse (GzTz)",
            "Alex's 2026 2nd -> Sarah Driscoll",
            "Alex's 2026 3rd -> Patrick Stevenson",
            "Alex's 2026 4th -> Alexa Feldman",
        ],
    },
    4: {  # Bradley Komar
        "holds": [
            "Brad's 2026 1st",
            "Brad's 2026 2nd",
            "Brad's 2026 3rd",
            "Brad's 2026 4th",
            "Alexa's 2026 3rd",    # acquired from Alexa
            "Brad's 2027 1st",
            "Brad's 2027 2nd",
            "Brad's 2027 4th",
        ],
        "traded_away": [
            "Brad's 2027 3rd -> Alexa Feldman",
        ],
    },
    5: {  # Nathaniel Lubin
        "holds": [
            "Lubin's 2026 1st",
            "Lubin's 2026 2nd",
            "Lubin's 2026 3rd",
            "Lubin's 2026 4th",
            "Lubin's 2027 1st",
            "Lubin's 2027 2nd",
            "Lubin's 2027 3rd",
            "Lubin's 2027 4th",
        ],
        "traded_away": [],
    },
    6: {  # John Schueler
        "holds": [
            "Schueler's 2026 1st",
            "Schueler's 2026 3rd",
            "Schueler's 2026 4th",
            "Schueler's 2027 1st",
            "Schueler's 2027 2nd",
            "Schueler's 2027 3rd",
            "Schueler's 2027 4th",
        ],
        "traded_away": [
            "Schueler's 2026 2nd -> Jacob Berkowitz",
        ],
    },
    7: {  # Grant Denton
        "holds": [
            "Denton's 2026 1st",
            "Denton's 2026 2nd",
            "Denton's 2026 3rd",
            "Denton's 2026 4th",
            "Denton's 2027 1st",
            "Denton's 2027 2nd",
            "Denton's 2027 3rd",
            "Denton's 2027 4th",
        ],
        "traded_away": [],
    },
    8: {  # Jesse / Gz Tz
        "holds": [
            "Jesse's 2026 1st",
            "Patrick's 2026 1st",  # acquired from Patrick (JT=Jesse in sheet)
            "Alex's 2026 1st",     # acquired from Alex Wall (JT=Jesse in sheet)
            "Driscoll's 2027 1st", # acquired from Sarah Driscoll (JT=Jesse in sheet)
            "Jesse's 2027 1st",
            "Alexa's 2027 2nd",    # acquired from Alexa in Carnell Tate trade (w/ Tyler Shough)
            "Jesse's 2027 3rd",
            "Jesse's 2027 4th",
        ],
        "traded_away": [
            "Jesse's 2026 2nd -> Patrick Stevenson",
            "Jesse's 2026 3rd -> Alex Wall",
            "Jesse's 2026 4th -> Alex Wall",
            "Jesse's 2027 2nd -> Patrick Stevenson (for the pick used on Omar Cooper Jr.)",
        ],
    },
    9: {  # Sarah Driscoll
        "holds": [
            "Driscoll's 2026 1st",
            "Driscoll's 2026 3rd",
            "Driscoll's 2026 4th",
            "Alex's 2026 2nd",     # acquired from Alex Wall
            "Driscoll's 2027 2nd",
            "Driscoll's 2027 3rd",
            "Driscoll's 2027 4th",
        ],
        "traded_away": [
            "Driscoll's 2026 2nd -> Jacob Berkowitz",
            "Driscoll's 2027 1st -> Jesse (GzTz)",
        ],
    },
    10: {  # Jacob Berkowitz
        "holds": [
            "Berkowitz's 2026 1st",
            "Berkowitz's 2026 2nd",
            "Berkowitz's 2026 3rd",
            "Berkowitz's 2026 4th",
            "Driscoll's 2026 2nd",  # acquired from Sarah Driscoll
            "Schueler's 2026 2nd",  # acquired from John Schueler
            "Berkowitz's 2027 1st",
            "Berkowitz's 2027 2nd",
            "Berkowitz's 2027 3rd",
            "Berkowitz's 2027 4th",
        ],
        "traded_away": [],
    },
}


def get_team_picks(team_id):
    return TEAM_PICKS.get(team_id, {"holds": [], "traded_away": []})
