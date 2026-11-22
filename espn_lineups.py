"""Weekly lineups: which slot every rostered player was in, and what he scored.

Read from ESPN's box score view, one request per scoring period, and parsed
with espn_api's BoxScore. ESPN only has box scores from 2019 on. Teams with no
matchup that week (playoff byes, eliminated teams) have no lineup for it.
"""
import json
from datetime import date, datetime, timedelta, timezone

from db import now_ms

FIRST_SEASON_WITH_BOX_SCORES = 2019
NOT_STARTING = {"BE", "IR"}


def season_start(season):
    """Kickoff of week 1: the Thursday after Labor Day (the first Monday of September)."""
    labor_day = date(season, 9, 1) + timedelta(days=(0 - date(season, 9, 1).weekday()) % 7)
    kickoff = labor_day + timedelta(days=3)
    return int(datetime(kickoff.year, kickoff.month, kickoff.day, tzinfo=timezone.utc).timestamp() * 1000)


def week_start(season, week):
    return season_start(season) + (week - 1) * 7 * 86_400_000


def fetch_week(league, week):
    """[(team_id, player)] for one scoring period, player being an espn_api BoxPlayer."""
    from espn_api.football.box_score import BoxScore

    period = next((int(k) for k, weeks in league.settings.matchup_periods.items() if week in weeks), week)
    data = league.espn_request.league_get(
        params={"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": week},
        headers={"x-fantasy-filter": json.dumps({"schedule": {"filterMatchupPeriodIds": {"value": [period]}}})})
    out = []
    for matchup in data.get("schedule") or []:
        try:
            box = BoxScore(matchup, {}, {}, week, league.year)
        except KeyError:  # a matchup without lineups yet
            continue
        for team, lineup in ((box.home_team, box.home_lineup), (box.away_team, box.away_lineup)):
            if team:
                out += [(team, p) for p in lineup]
    return out


def store_week(conn, season, week, rows):
    conn.execute("DELETE FROM lineups WHERE season = ? AND week = ?", (season, week))  # lineups change until the week ends
    for team_id, p in rows:
        if not getattr(p, "playerId", None):
            continue
        conn.execute("""INSERT INTO lineups (season, week, team_id, player_id, player_name, position, pro_team,
                            slot, points, projected, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (season, week, team_id, player_id) DO NOTHING""",
                     (season, week, team_id, p.playerId, p.name, getattr(p, "position", None), getattr(p, "proTeam", None),
                      p.slot_position, round(p.points or 0, 2), round(getattr(p, "projected_points", 0) or 0, 2), now_ms()))


def sync_lineups(conn, league, finished=False):
    """Store lineups from the last stored week (it may have changed) through ESPN's current week.

    For a finished season that's already complete, nothing is fetched. Returns
    the number of weeks fetched; seasons before 2019 have no box scores.
    """
    last_week = getattr(league, "current_week", 0) or 0
    if league.year < FIRST_SEASON_WITH_BOX_SCORES or last_week < 1 or not hasattr(league, "espn_request"):
        return 0
    stored = conn.scalar("SELECT MAX(week) AS w FROM lineups WHERE season = ?", (league.year,)) or 0
    if finished and stored >= last_week:
        return 0
    weeks = range(max(1, stored), last_week + 1)
    for week in weeks:
        store_week(conn, league.year, week, fetch_week(league, week))
    conn.commit()
    return len(weeks)
