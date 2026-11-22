"""League Records: the best and worst single weeks and seasons, from weekly lineups.

Only finished weeks count, so a week still being played can't set a low mark.
A team's score for a week is its starters' points added up.
"""
from collections import defaultdict

from espn_lineups import NOT_STARTING, week_start

DAY = 86_400_000
WEEK_DONE_AFTER = 6 * DAY  # Thursday kickoff to the Wednesday after Monday night
TOP = 10


def _ranked(rows, value, reverse=True):
    """The top TOP rows by value, each with a rank; equal values share one."""
    rows = sorted(rows, key=value, reverse=reverse)[:TOP]
    out, rank, last = [], 0, None
    for i, r in enumerate(rows, 1):
        if value(r) != last:
            rank, last = i, value(r)
        out.append({**r, "rank": rank})
    return out


def compute(conn, season, now):
    """{key: [record]} for one season, or for every season when season is None."""
    sql, params = "SELECT * FROM lineups", ()
    if season is not None:
        sql, params = sql + " WHERE season = ?", (season,)
    rows = [r for r in conn.execute(sql, params) if week_start(r["season"], r["week"]) + WEEK_DONE_AFTER <= now]

    then = {(r["season"], r["team_id"]): r["name"] for r in conn.execute("SELECT season, team_id, name FROM team_seasons")}

    def base(r):
        return {"season": r["season"], "team_id": r["team_id"], "then": then.get((r["season"], r["team_id"]))}

    starters, benched = [], []
    team_week = defaultdict(lambda: {"points": 0.0, "top": None})
    player_season = defaultdict(lambda: {"points": 0.0, "starts": 0})
    team_season = defaultdict(lambda: {"points": 0.0, "weeks": set()})
    for r in rows:
        pts = r["points"] or 0
        player = {**base(r), "week": r["week"], "player": r["player_name"], "position": r["position"], "pts": pts}
        if r["slot"] in NOT_STARTING:
            if r["slot"] == "BE":
                benched.append(player)
            continue
        starters.append(player)
        tw = team_week[(r["season"], r["week"], r["team_id"])]
        tw["points"] += pts
        if tw["top"] is None or pts > tw["top"]["pts"]:
            tw["top"] = player
        ps = player_season[(r["season"], r["team_id"], r["player_id"])]
        ps.update(player=r["player_name"], position=r["position"])
        ps["points"] += pts
        ps["starts"] += 1
        ts = team_season[(r["season"], r["team_id"])]
        ts["points"] += pts
        ts["weeks"].add(r["week"])

    team_weeks = [{"season": s, "week": w, "team_id": t, "then": then.get((s, t)), "pts": v["points"],
                   "top": v["top"]["player"], "top_points": v["top"]["pts"]}
                  for (s, w, t), v in team_week.items()]
    player_seasons = [{"season": s, "team_id": t, "then": then.get((s, t)), "pts": v["points"],
                       "player": v["player"], "position": v["position"], "starts": v["starts"]}
                      for (s, t, _), v in player_season.items()]
    # by points per week: early seasons had 16 weeks, later ones 17, and the current one is still going
    team_seasons = [{"season": s, "team_id": t, "then": then.get((s, t)), "pts": v["points"] / len(v["weeks"]),
                     "total": v["points"], "weeks": len(v["weeks"])}
                    for (s, t), v in team_season.items()]

    pts = lambda r: r["pts"]
    return {
        "player_week": _ranked(starters, pts),
        "team_week_high": _ranked(team_weeks, pts),
        "team_week_low": _ranked(team_weeks, pts, reverse=False),
        "bench_week": _ranked(benched, pts),
        "player_season": _ranked(player_seasons, pts),
        "team_season": _ranked(team_seasons, pts),
    }
