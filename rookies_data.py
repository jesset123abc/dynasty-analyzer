"""
2026 NFL Draft dynasty rookie prospect board.
Live KTC Superflex values from keeptradecut.com/dynasty-rankings/rookie-rankings (March 2026).
Values marked est=True are estimates based on consensus rankings where KTC data was unavailable.
"""

ROOKIES_2026 = [
    # ── Live KTC values ───────────────────────────────────────────────────────
    {
        "rank": 1, "name": "Jeremiyah Love", "pos": "RB",
        "college": "Notre Dame", "ktc_est": 7098, "age": 21, "est": False,
        "notes": "Consensus 1.01. Dominant all-around RB — size, speed, and workhorse upside."
    },
    {
        "rank": 2, "name": "Fernando Mendoza", "pos": "QB",
        "college": "Indiana", "ktc_est": 5610, "age": 22, "est": False,
        "notes": "Heisman winner, National Champion. Best Superflex asset in class — Matt Ryan / Eli Manning comps."
    },
    {
        "rank": 3, "name": "Carnell Tate", "pos": "WR",
        "college": "Ohio State", "ktc_est": 5335, "age": 21, "est": False,
        "notes": "Elite route runner, projected top-10 NFL pick. Ja'Marr Chase / Malik Nabers ceiling."
    },
    {
        "rank": 4, "name": "Makai Lemon", "pos": "WR",
        "college": "USC", "ktc_est": 5182, "age": 21, "est": False,
        "notes": "Dynamic inside-out WR with elite YAC ability. PPR cheat code, WR2 floor as a rookie."
    },
    {
        "rank": 5, "name": "Jordyn Tyson", "pos": "WR",
        "college": "Arizona State", "ktc_est": 4830, "age": 21, "est": False,
        "notes": "Two-time All-Big 12. Projected top-15 NFL pick. Contends for WR1 off the board."
    },
    {
        "rank": 6, "name": "Kenyon Sadiq", "pos": "TE",
        "college": "Oregon", "ktc_est": 4135, "age": 22, "est": False,
        "notes": "Consensus TE1. Built like a lab experiment — mid/late 1st round projection with immediate upside."
    },
    {
        "rank": 7, "name": "KC Concepcion", "pos": "WR",
        "college": "Texas A&M", "ktc_est": 3815, "age": 21, "est": False,
        "notes": "Physically imposing WR with elite explosiveness. NFL draft darling."
    },
    {
        "rank": 8, "name": "Denzel Boston", "pos": "WR",
        "college": "Washington", "ktc_est": 3695, "age": 21, "est": False,
        "notes": "6'4 jump-ball WR who uses size to win contested catches. Big-play upside."
    },
    {
        "rank": 9, "name": "Jadarian Price", "pos": "RB",
        "college": "Notre Dame", "ktc_est": 3271, "age": 21, "est": False,
        "notes": "Love's backfield mate. 6.1 YPC in 28 games. Excellent pass-catcher — stacked landing spot upside."
    },
    {
        "rank": 10, "name": "Omar Cooper Jr.", "pos": "WR",
        "college": "Indiana", "ktc_est": 3197, "age": 22, "est": False,
        "notes": "Thrived with Mendoza at Indiana. Reliable slot with strong RAC and YAC profile."
    },
    {
        "rank": 11, "name": "Jonah Coleman", "pos": "RB",
        "college": "Washington", "ktc_est": 3154, "age": 21, "est": False,
        "notes": "Workhorse back — top pass-blocking grades in class. 5'9, 220. Durable, versatile."
    },
    {
        "rank": 12, "name": "Ty Simpson", "pos": "QB",
        "college": "Alabama", "ktc_est": 2903, "age": 22, "est": False,
        "notes": "Best ball placement in class. First-round lock. Attacks windows aggressively — Matt Stafford comp."
    },
    {
        "rank": 13, "name": "Nicholas Singleton", "pos": "RB",
        "college": "Penn State", "ktc_est": 2663, "age": 21, "est": False,
        "notes": "Explosive back who waited behind Kaytron Allen at PSU. Big-play ability, great landing spot value."
    },
    {
        "rank": 14, "name": "Elijah Sarratt", "pos": "WR",
        "college": "Indiana", "ktc_est": 2544, "age": 22, "est": False,
        "notes": "Another Indiana WR who thrived with Mendoza. Excellent RAC and YAC ability."
    },
    {
        "rank": 15, "name": "Emmett Johnson", "pos": "RB",
        "college": "Nebraska", "ktc_est": 2449, "age": 21, "est": False,
        "notes": "Big Ten RB of the Year. Dynamic all-purpose back with strong receiving chops."
    },
    {
        "rank": 16, "name": "Zachariah Branch", "pos": "WR",
        "college": "Georgia", "ktc_est": 2371, "age": 21, "est": False,
        "notes": "Elite speed with dangerous YAC (transferred USC→Georgia). Massive upside in right offense."
    },
    # ── Estimated KTC values (based on consensus rankings) ────────────────────
    {
        "rank": 17, "name": "Justice Haynes", "pos": "RB",
        "college": "Michigan", "ktc_est": 2300, "age": 21, "est": True,
        "notes": "Declared despite mid-season injury. Physical runner with good contact balance."
    },
    {
        "rank": 18, "name": "Chris Brazzell II", "pos": "WR",
        "college": "Tennessee", "ktc_est": 2100, "age": 22, "est": True,
        "notes": "Big WR with strong contested-catch ability and NFL-ready frame."
    },
    {
        "rank": 19, "name": "Kaytron Allen", "pos": "RB",
        "college": "Penn State", "ktc_est": 2000, "age": 21, "est": True,
        "notes": "Penn State all-time leading rusher. Powerful north-south back."
    },
    {
        "rank": 20, "name": "Eli Stowers", "pos": "TE",
        "college": "Vanderbilt", "ktc_est": 1900, "age": 23, "est": True,
        "notes": "TE2 in class. 6'4, 235. Physically ready to start. Reliable receiver."
    },
    {
        "rank": 21, "name": "Demond Claiborne", "pos": "RB",
        "college": "Wake Forest", "ktc_est": 1800, "age": 22, "est": True,
        "notes": "Undersized but dynamic. Consistent ACC producer with strong contact balance."
    },
    {
        "rank": 22, "name": "Chris Bell", "pos": "WR",
        "college": "Louisville", "ktc_est": 1700, "age": 21, "est": True,
        "notes": "Athletic WR with route-running finesse. Rising consensus darling."
    },
    {
        "rank": 23, "name": "Garrett Nussmeier", "pos": "QB",
        "college": "LSU", "ktc_est": 1650, "age": 22, "est": True,
        "notes": "QB3 in class — making case for QB2 behind Mendoza. Strong arm, downfield passer."
    },
    {
        "rank": 24, "name": "Ja'Kobi Lane", "pos": "WR",
        "college": "USC", "ktc_est": 1500, "age": 22, "est": True,
        "notes": "Slot WR with excellent hands and YAC. Developed well in USC's passing offense."
    },
    {
        "rank": 25, "name": "Duce Robinson", "pos": "WR",
        "college": "Florida State", "ktc_est": 1400, "age": 21, "est": True,
        "notes": "WR/TE hybrid with elite athleticism. Developing route runner — high ceiling."
    },
    {
        "rank": 26, "name": "Malachi Fields", "pos": "WR",
        "college": "Notre Dame", "ktc_est": 1300, "age": 22, "est": True,
        "notes": "Reliable WR from Notre Dame's loaded passing offense."
    },
    {
        "rank": 27, "name": "Michael Trigg", "pos": "TE",
        "college": "Baylor", "ktc_est": 1200, "age": 23, "est": True,
        "notes": "Strong pass-catching TE. Consistent in Baylor's offense."
    },
    {
        "rank": 28, "name": "Aaron Anderson", "pos": "WR",
        "college": "LSU", "ktc_est": 1100, "age": 21, "est": True,
        "notes": "Speed WR from LSU. Deep threat with big-play ability."
    },
    {
        "rank": 29, "name": "Hollywood Smothers", "pos": "RB",
        "college": "NC State", "ktc_est": 1000, "age": 21, "est": True,
        "notes": "Productive ACC back with good receiving ability."
    },
    {
        "rank": 30, "name": "Nyck Harbor", "pos": "WR",
        "college": "South Carolina", "ktc_est": 900, "age": 22, "est": True,
        "notes": "Athletic WR from South Carolina. Upside project."
    },
]
