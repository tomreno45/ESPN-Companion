"""Trade grades and votes: what each side of a trade went on to score, and who members think won.

A player counts the points he scored as a starter for the team that got him,
from the trade until he left that team (a stint, see awards.py). A traded pick
counts the player it was used on, for the team that made the pick.
"""
from collections import defaultdict
from datetime import datetime, timezone

from awards import HALF_WEEK, all_stints
from db import pick_owners
from espn_lineups import FIRST_SEASON_WITH_BOX_SCORES, NOT_STARTING, season_start, week_start

DAY = 86_400_000
TIE = 0  # a vote's choice: a team id, or 0 for "it was a tie"


def side_teams(t):
    """The teams in a trade: everyone who sent or got something, plus whoever ESPN says took part."""
    teams = {tid for it in t["items"] if it["from_team_id"] and it["to_team_id"]
             for tid in (it["from_team_id"], it["to_team_id"])}
    teams |= set(t.get("accepted_by") or [])
    if t.get("offered_by"):
        teams.add(t["offered_by"])
    return sorted(teams)


def _feb1(season):
    return int(datetime(season, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)


class _Lineups:
    """Starter points by (team, player), each as (mid-week time, points)."""

    def __init__(self, conn):
        self.rows = defaultdict(list)
        for r in conn.execute("SELECT season, week, team_id, player_id, slot, points FROM lineups"):
            if r["slot"] not in NOT_STARTING:
                self.rows[(r["team_id"], r["player_id"])].append(
                    (week_start(r["season"], r["week"]) + HALF_WEEK, r["points"] or 0))

    def during(self, team, pid, start, end):
        rows = [p for t, p in self.rows.get((team, pid), []) if start - HALF_WEEK <= t <= end + HALF_WEEK]
        return sum(rows), len(rows)


def _stint_result(st, lineups, team, pid):
    start = st["start"] if st["start"] is not None else -float("inf")
    points, starts = lineups.during(team, pid, start, st["end"])
    out = {"points": points, "starts": starts, "left": st["how_out"], "left_at": st["end"] if st["how_out"] else None}
    if st["end"] < season_start(FIRST_SEASON_WITH_BOX_SCORES):
        out.update(points=None, note="Before 2019, when ESPN's weekly lineups begin")
    elif start < season_start(FIRST_SEASON_WITH_BOX_SCORES):
        out["note"] = "Points from 2019 on, when ESPN's weekly lineups begin"
    return out


def grade(conn, trades, now):
    """Add t["sides"] to each trade: [{team_id, points, starts, assets: [...]}], one per team that got something."""
    if not trades:
        return
    stints = all_stints(conn, now)
    lineups = _Lineups(conn)
    ids_by_name = defaultdict(set)
    for r in conn.execute("SELECT id, name FROM players"):
        ids_by_name[r["name"].strip().lower()].add(r["id"])
    for r in conn.execute("SELECT DISTINCT player_id, player_name FROM lineups"):
        if r["player_name"]:
            ids_by_name[r["player_name"].strip().lower()].add(r["player_id"])

    def player_id(pid, name):
        if pid is not None:
            return pid
        match = ids_by_name.get((name or "").strip().lower(), set())
        return next(iter(match)) if len(match) == 1 else None

    owners = pick_owners(conn)
    picks = {p["id"]: p for p in conn.execute("SELECT * FROM draft_picks")}

    def player_asset(it, team, when):
        pid = player_id(it["player_id"], it["player_name"])
        out = {"kind": "player", "label": it["label"], "points": None, "starts": 0}
        if pid is None:
            return {**out, "note": "No lineups found for this name"}
        mine = [s for s in stints.get(pid, []) if s["team"] == team]
        # the stint the trade began, or failing that the one he was in at the time
        st = next((s for s in mine if s["start"] is not None and abs(s["start"] - when) <= 2 * DAY), None) or \
            next((s for s in mine if (s["start"] if s["start"] is not None else -float("inf")) - 7 * DAY <= when <= s["end"]), None)
        if st is None:
            if when < season_start(FIRST_SEASON_WITH_BOX_SCORES):
                return {**out, "note": "Before 2019, when ESPN's weekly lineups begin"}
            return {**out, "points": 0, "note": "Never in a lineup for this team"}
        return {**out, **_stint_result(st, lineups, team, pid)}

    def pick_asset(it, team):
        pick = picks.get(it["pick_id"])
        out = {"kind": "pick", "label": it["label"], "points": None, "starts": 0}
        if pick is None:
            return out
        owner = owners.get(pick["id"], pick["original_team_id"])
        if owner != team:
            return {**out, "traded_on_to": owner}
        if not pick["used_on"]:
            return {**out, "note": "No player recorded for this pick yet"}
        out["used_on"] = pick["used_on"]
        pid = player_id(None, pick["used_on"])
        if pid is None:
            return {**out, "note": "No lineups found for this name"}
        # rookies arrive the summer of the draft (this league adds them as free agents)
        st = next((s for s in stints.get(pid, []) if s["team"] == team and s["start"] is not None
                   and s["start"] >= _feb1(pick["season"])), None)
        if st is None:
            return {**out, "points": 0, "note": "Never in a lineup for this team"}
        return {**out, **_stint_result(st, lineups, team, pid)}

    for t in trades:
        got = defaultdict(list)
        for it in t["items"]:
            if it["to_team_id"] is None or (it["asset_type"] == "player" and it["from_team_id"] is None):
                continue  # dropped to make room, not part of the deal
            got[it["to_team_id"]].append(player_asset(it, it["to_team_id"], t["date_ms"]) if it["asset_type"] == "player"
                                         else pick_asset(it, it["to_team_id"]))
        t["sides"] = []
        for team in side_teams(t):
            assets = got.get(team, [])
            counted = [a for a in assets if a["points"] is not None]
            t["sides"].append({"team_id": team, "assets": assets,
                               "points": sum(a["points"] for a in counted) if counted else None,
                               "starts": sum(a["starts"] for a in counted)})


def votes(conn, txn_ids, user_id):
    """{txn_id: {"tally": {choice: [voter names]}, "mine": choice or None}}."""
    out = {tid: {"tally": {}, "mine": None} for tid in txn_ids}
    if not txn_ids:
        return out
    rows = conn.execute(f"""
        SELECT v.transaction_id, v.user_id, v.choice, u.display_name FROM trade_votes v JOIN users u ON u.id = v.user_id
        WHERE v.transaction_id IN ({','.join('?' * len(txn_ids))}) ORDER BY v.voted_at""", list(txn_ids))
    for r in rows:
        v = out[r["transaction_id"]]
        v["tally"].setdefault(r["choice"], []).append(r["display_name"])
        if r["user_id"] == user_id:
            v["mine"] = r["choice"]
    return out
