"""Team Awards: a few fun superlatives per team for a season.

Two sources:
- Weekly lineups (2019 on): who started, who sat, and what they scored.
- Stints: each spell a player spent on a team. Built from adds, drops and
  trades, with the weekly lineups filling the gaps that ESPN's hidden past
  trades leave: a player who turns up on a new team with no recorded move
  changed teams that week. Times from lineups alone are only good to the week.
"""
import os
from collections import defaultdict
from datetime import datetime, timezone

from espn_lineups import NOT_STARTING, season_start, week_start

DAY = 86_400_000
HALF_WEEK = 7 * DAY // 2
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}


def _day(ms):
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return f"{d:%b} {d.day}"


def _month_year(ms):
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return f"{d:%b} {d.year}"


def _pts(x):
    """1,152 or 12.8: whole numbers without a decimal, thousands separated."""
    return f"{round(x):,}" if abs(x - round(x)) < 0.05 else f"{x:,.1f}"


# ---------- stints ----------

def _moves(conn):
    """{player_id: [(time, from_team, to_team, how)]}; members' typed-in players are matched by name."""
    ids_by_name = {}
    for r in conn.execute("SELECT id, name FROM players"):
        ids_by_name.setdefault(r["name"].strip().lower(), set()).add(r["id"])
    moves = defaultdict(list)
    for r in conn.execute("""
            SELECT i.player_id, i.player_name, i.from_team_id, i.to_team_id, i.action, t.date_ms
            FROM transaction_items i JOIN transactions t ON t.id = i.transaction_id
            WHERE i.asset_type = 'player' ORDER BY t.date_ms, t.id, i.id"""):
        pid = r["player_id"]
        if pid is None:
            match = ids_by_name.get((r["player_name"] or "").strip().lower(), set())
            if len(match) != 1:
                continue
            pid = next(iter(match))
        src, dst = r["from_team_id"], r["to_team_id"]
        how = "trade" if src and dst else ("add" if dst else "drop")
        moves[pid].append((r["date_ms"], src, dst, how))
    return moves


def _sightings(conn):
    """{player_id: [(time, team)]}: mid-week of every lineup he was in."""
    seen = defaultdict(list)
    for r in conn.execute("SELECT season, week, team_id, player_id FROM lineups ORDER BY season, week"):
        seen[r["player_id"]].append((week_start(r["season"], r["week"]) + HALF_WEEK, r["team_id"]))
    return seen


def build_stints(moves, sightings, rostered_now, now):
    """[{team, start, end, how_in, how_out, start_known}] for one player.

    start None: he was already there when our data begins. how_in: add, trade,
    unrecorded (turned up from another team with no recorded move) or earlier.
    """
    events = [(t, 0, ("move", src, dst, how)) for t, src, dst, how in moves]
    events += [(t, 1, ("seen", team)) for t, team in sightings]
    events.sort(key=lambda e: (e[0], e[1]))
    out = []
    cur = None  # the open stint

    def close(at, how_out):
        nonlocal cur
        cur["end"], cur["how_out"] = max(at, cur["start"] or at), how_out
        out.append(cur)
        cur = None

    for t, _, ev in events:
        if ev[0] == "move":
            _, src, dst, how = ev
            if src is not None:
                if cur and cur["team"] == src:
                    close(t, how)
                elif cur is None:  # he was on src before our data shows it
                    out.append({"team": src, "start": None, "end": t, "how_in": "earlier", "how_out": how,
                                "start_known": False, "last_seen": None})
                else:  # records disagree; trust the move
                    close(t, "unrecorded")
            if dst is not None:
                if cur:
                    close(t, "unrecorded")
                cur = {"team": dst, "start": t, "end": None, "how_in": how, "how_out": None,
                       "start_known": True, "last_seen": None}
        else:
            team = ev[1]
            if cur and cur["team"] == team:
                cur["last_seen"] = t
                continue
            # A lineup only says he was on the team at some point that week: if a stint we already
            # know of covers part of it (added or dropped mid-week), it's that one, not a new stint.
            if any(s["team"] == team and s["end"] >= t - HALF_WEEK and (s["start"] or -float("inf")) <= t + HALF_WEEK
                   for s in out[-3:]):
                continue
            came_from_team = cur is not None
            if cur:
                close(min(t - HALF_WEEK, (cur["last_seen"] or cur["start"] or t) + HALF_WEEK), "unrecorded")
            cur = {"team": team, "start": t - HALF_WEEK, "end": None,
                   "how_in": "unrecorded" if came_from_team else "earlier", "how_out": None,
                   "start_known": False, "last_seen": t}
    if cur:
        if cur["team"] in rostered_now:
            cur["end"], cur["how_out"] = now, None
        else:  # gone without a recorded move (e.g. a past season's roster); last seen that week
            cur["end"], cur["how_out"] = (cur["last_seen"] or cur["start"] or now) + HALF_WEEK, "unrecorded"
        out.append(cur)
    return out


def all_stints(conn, now):
    this_season = season_start(int(os.environ.get("ESPN_YEAR") or datetime.now(timezone.utc).year))
    moves, seen = _moves(conn), _sightings(conn)
    rostered = defaultdict(set)
    for r in conn.execute("SELECT team_id, player_id FROM roster_entries"):
        rostered[r["player_id"]].add(r["team_id"])
    stints = {}
    for pid in set(moves) | set(seen) | set(rostered):
        s = build_stints(moves.get(pid, []), seen.get(pid, []), rostered.get(pid, set()), now)
        if not s and rostered.get(pid):
            # On a roster now with no history at all (say, a rookie drafted this summer, since drafts
            # aren't imported): count him from this season, not from before our records.
            s = [{"team": t, "start": this_season, "end": now, "how_in": "earlier", "how_out": None, "start_known": False}
                 for t in rostered[pid]]
        stints[pid] = s
    return stints


# ---------- awards ----------

def season_window(conn, season, now):
    last_week = conn.scalar("SELECT MAX(week) AS w FROM lineups WHERE season = ?", (season,)) or 17
    start = season_start(season)
    return start, min(now, week_start(season, last_week + 1))


def player_names(conn):
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM players")}
    for r in conn.execute("SELECT DISTINCT player_id, player_name FROM lineups"):
        names.setdefault(r["player_id"], r["player_name"])
    return names


def seasons_available(conn):
    rows = conn.execute("""SELECT DISTINCT season FROM lineups
                           UNION SELECT DISTINCT season FROM transactions WHERE season IS NOT NULL""")
    return sorted({r["season"] for r in rows}, reverse=True)


def in_season(ms):
    """False for the summer (Feb 1 to kickoff), when this league adds its rookie picks as free agents."""
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    feb1 = int(datetime(d.year, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return not (feb1 <= ms < season_start(d.year))


def first_season(conn):
    return min(seasons_available(conn) or [datetime.now(timezone.utc).year])


def _plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def compute(conn, season, now):
    """{team_id: [award]} for one season, or for all of them combined when season is None.

    award = {key, icon, title, player_id, player, value, detail}.
    """
    all_time = season is None
    names = player_names(conn)
    awards = defaultdict(list)

    def give(team, key, icon, title, pid, value, detail=""):
        awards[team].append({"key": key, "icon": icon, "title": title, "player_id": pid,
                             "player": names.get(pid, f"Player #{pid}"), "value": value, "detail": detail})

    if all_time:
        start, end = season_start(first_season(conn)), now
        rows = list(conn.execute("SELECT * FROM lineups"))
    else:
        start, end = season_window(conn, season, now)
        rows = list(conn.execute("SELECT * FROM lineups WHERE season = ?", (season,)))
    stints = all_stints(conn, now)
    stints_of = defaultdict(list)  # (team, player) -> his stints there, oldest first
    for pid, ss in stints.items():
        for st in ss:
            stints_of[(st["team"], pid)].append(st)

    # --- from lineups: per player and team, and per stint ---
    stat = defaultdict(lambda: {"starts": 0, "start_pts": 0.0, "bench": 0, "bench_pts": 0.0,
                                "best": (None, None, None), "pos": None, "seasons": set()})
    stint_pts = defaultdict(float)  # id(stint) -> points as a starter during it
    for r in rows:
        key = (r["team_id"], r["player_id"])
        s = stat[key]
        s["pos"] = r["position"]
        s["seasons"].add(r["season"])
        pts = r["points"] or 0
        if r["slot"] in NOT_STARTING:
            if r["slot"] == "BE":
                s["bench"] += 1
                s["bench_pts"] += pts
            continue
        s["starts"] += 1
        s["start_pts"] += pts
        if s["best"][0] is None or pts > s["best"][0]:
            s["best"] = (pts, r["season"], r["week"])
        t = week_start(r["season"], r["week"]) + HALF_WEEK
        for st in stints_of.get(key, []):  # a lineup belongs to the stint around that week
            if (st["start"] if st["start"] is not None else -float("inf")) - HALF_WEEK <= t <= st["end"] + HALF_WEEK:
                stint_pts[id(st)] += pts
                break
    by_team = defaultdict(dict)
    for (team, pid), s in stat.items():
        by_team[team][pid] = s

    # --- stints, clipped to the season (or to all of our records) ---
    days = defaultdict(lambda: defaultdict(float))   # team -> player -> days in scope
    stint_count = defaultdict(int)                   # (team, player) -> stints in scope
    tenure_start = {}                                # (team, player) -> (start, known) of the stint at the end
    finished = defaultdict(list)                     # team -> stints that began and ended in scope
    scored = defaultdict(list)                       # team -> (player, stint) with points in scope
    for pid, ss in stints.items():
        for st in ss:
            s0, s1 = st["start"] if st["start"] is not None else -float("inf"), st["end"]
            overlap = min(s1, end) - max(s0, start)
            if overlap <= 0 and not (start <= s0 <= end):
                continue
            key = (st["team"], pid)
            days[st["team"]][pid] += max(overlap, 0) / DAY
            stint_count[key] += 1
            if s1 >= end or st["how_out"] is None:
                tenure_start[key] = (st["start"], st["start_known"])
            if start <= s0 and s1 < end and st["how_out"] in ("drop", "trade"):
                finished[st["team"]].append((pid, st))
            if stint_pts.get(id(st)):
                scored[st["team"]].append((pid, st))

    def when(ms):
        return f"{_day(ms)}, {datetime.fromtimestamp(ms / 1000, timezone.utc).year}" if all_time else _day(ms)

    for team in set(by_team) | set(days):
        players = by_team.get(team, {})
        if players:
            pid, s = max(players.items(), key=lambda kv: kv[1]["start_pts"])
            if s["starts"]:
                detail = _plural(s["starts"], "start")
                if all_time:
                    detail += f" over {_plural(len(s['seasons']), 'season')}"
                give(team, "mvp", "🏆", "MVP", pid, f"{_pts(s['start_pts'])} pts as a starter", detail)
            pid, s = max(players.items(), key=lambda kv: kv[1]["best"][0] if kv[1]["best"][0] is not None else -1e9)
            if s["best"][0] is not None:
                give(team, "best_game", "💥", "Best game", pid, f"{_pts(s['best'][0])} pts",
                     f"Week {s['best'][2]}" + (f", {s['best'][1]}" if all_time else ""))
            pid, s = max(players.items(), key=lambda kv: kv[1]["bench_pts"])
            if s["bench_pts"] > 0:
                give(team, "bench_points", "😬", "Should've started him", pid,
                     f"{_pts(s['bench_pts'])} pts on the bench", f"{_plural(s['bench'], 'week')} benched")
            pid, s = max(players.items(), key=lambda kv: (kv[1]["bench"], -kv[1]["starts"]))
            if s["bench"] > 0:
                give(team, "bench_weeks", "🪑", "Bench warmer", pid, f"{_plural(s['bench'], 'week')} on the bench",
                     _plural(s["starts"], "start"))
            min_starts = 8 if all_time else 3
            busts = [(pid, s) for pid, s in players.items() if s["starts"] >= min_starts and s["pos"] in SKILL_POSITIONS]
            if busts:
                pid, s = min(busts, key=lambda kv: kv[1]["start_pts"] / kv[1]["starts"])
                give(team, "bust", "📉", "Bust", pid, f"{_pts(s['start_pts'] / s['starts'])} pts per start",
                     _plural(s["starts"], "start"))

        # Pickups and trades count the points from that stint only. Pickups are in-season adds:
        # this league adds its rookie draft picks as free agents each summer.
        stints_here = scored.get(team, [])
        pickups = [(pid, st) for pid, st in stints_here if st["how_in"] == "add" and in_season(st["start"])]
        if pickups:
            pid, st = max(pickups, key=lambda x: stint_pts[id(x[1])])
            give(team, "waiver_gem", "💎", "Waiver wire gem", pid, f"{_pts(stint_pts[id(st)])} pts as a starter",
                 f"Picked up {when(st['start'])}")
        trades = [(pid, st) for pid, st in stints_here if st["how_in"] in ("trade", "unrecorded")]
        if trades:
            pid, st = max(trades, key=lambda x: stint_pts[id(x[1])])
            # "unrecorded": turned up from another team with no recorded move, so only the week is known
            give(team, "trade_prize", "🤝", "Trade prize", pid, f"{_pts(stint_pts[id(st)])} pts as a starter",
                 f"Acquired in a trade{'' if st['how_in'] == 'trade' else ' around'} {_month_year(st['start'])}")

        team_days = days.get(team, {})
        # Without lineups (2018) only players who moved have stints that season, so "most" would mislead.
        if team_days and players:
            def tenure(pid):
                """When his stint at the end began: earlier wins a tie; gone before the end loses it."""
                if (team, pid) not in tenure_start:
                    return float("inf")
                s, _ = tenure_start[(team, pid)]
                return s if s is not None else -float("inf")
            longest = max(team_days, key=lambda pid: (round(team_days[pid]), -tenure(pid),
                                                      players.get(pid, {}).get("start_pts", 0)))
            d = round(team_days[longest])
            since, known = tenure_start.get((team, longest), (None, False))
            detail = ""
            if (team, longest) in tenure_start:
                detail = f"On the team since {_month_year(since)}" if since is not None and known else \
                    f"On the team since at least {_month_year(max(since, start))}" if since is not None else \
                    "On the team since before our records start"
            if all_time:
                n = stint_count[(team, longest)]
                if n > 1:
                    detail = f"Across {n} stints" + (f"; {detail[0].lower()}{detail[1:]}" if detail else "")
                value = _plural(d, "day")
            else:
                value = f"All {d} days this season" if d >= round((end - start) / DAY) else f"{_plural(d, 'day')} this season"
            give(team, "most_days", "🧱", "Most days rostered", longest, value, detail)

        if finished.get(team):
            pid, st = min(finished[team], key=lambda x: (x[1]["end"] - x[1]["start"], -x[1]["end"]))
            length = (st["end"] - st["start"]) / DAY
            value = "Under a day" if length < 1 else _plural(round(length), "day")
            came = {"add": "Added", "trade": "Traded for", "unrecorded": "Arrived"}.get(st["how_in"], "Arrived")
            went = {"drop": "dropped", "trade": "traded away"}.get(st["how_out"], "gone")
            give(team, "fewest_days", "☕", "Fewest days rostered", pid, value,
                 f"{came} {_day(st['start'])}, {went} {when(st['end'])}")

    order = ["mvp", "best_game", "bench_points", "bench_weeks", "most_days", "fewest_days", "waiver_gem", "trade_prize", "bust"]
    return {team: sorted(a, key=lambda x: order.index(x["key"])) for team, a in awards.items()}
