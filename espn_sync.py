"""Pull rosters and transaction history from ESPN into the local database.

Based on the reference main.py: rosters come from league.teams[*].roster and
transactions from league.recent_activity(). Unlike main.py (size=5), this
pages through the whole activity feed with an offset and stores every
transaction, keyed so that re-syncing never duplicates or overwrites rows.
"""
import hashlib
import os

from db import now_ms

PAGE_SIZE = 25

SLOT_ORDER = ["QB", "RB", "WR", "TE", "RB/WR/TE", "OP", "D/ST", "K", "BE", "IR"]


def make_league(year=None):
    from espn_api.football import League

    league_id = int(os.environ["ESPN_LEAGUE_ID"])
    year = int(year or os.environ.get("ESPN_YEAR", 2026))
    return League(
        league_id=league_id,
        year=year,
        espn_s2=os.environ.get("ESPN_S2") or None,
        swid=os.environ.get("ESPN_SWID") or None,
        debug=False,
    )


def _player_fields(player):
    """espn_api sometimes hands back a bare player id when it can't resolve one."""
    if isinstance(player, int):
        return player, f"Player #{player}", None, None
    if isinstance(player, str):
        return None, player, None, None
    if player is None:
        return None, "Unknown", None, None
    return (getattr(player, "playerId", None), getattr(player, "name", "Unknown"),
            getattr(player, "position", None), getattr(player, "proTeam", None))


def _team_id(team):
    return getattr(team, "team_id", None) if team else None


def upsert_team(conn, team):
    owners = getattr(team, "owners", None) or []
    owner_names = ", ".join(
        " ".join(filter(None, [o.get("firstName"), o.get("lastName")])) or o.get("displayName", "")
        if isinstance(o, dict) else str(o)
        for o in owners
    )
    conn.execute("""
        INSERT INTO teams (id, name, abbrev, owners, logo_url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name, abbrev=excluded.abbrev,
            owners=excluded.owners, logo_url=excluded.logo_url, updated_at=excluded.updated_at
    """, (team.team_id, team.team_name, getattr(team, "team_abbrev", None), owner_names,
          getattr(team, "logo_url", None), now_ms()))


def upsert_player(conn, player):
    pid, name, pos, pro = _player_fields(player)
    if pid is None:
        return None
    conn.execute("""
        INSERT INTO players (id, name, position, pro_team) VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name,
            position=COALESCE(excluded.position, players.position),
            pro_team=COALESCE(excluded.pro_team, players.pro_team)
    """, (pid, name, pos, pro))
    return pid


def update_record(conn, team):
    """Store the team's current-season record and standing (used to project draft order)."""
    conn.execute("UPDATE teams SET wins = ?, losses = ?, ties = ?, points_for = ?, standing = ? WHERE id = ?",
                 (getattr(team, "wins", None), getattr(team, "losses", None), getattr(team, "ties", None),
                  getattr(team, "points_for", None), getattr(team, "standing", None) or None, team.team_id))


def store_player_pool(conn, league):
    """Add every NFL player ESPN knows (it sends the list with the league) that isn't stored yet,
    so a pick can be marked used on a rookie nobody has rostered. Returns how many were new."""
    pool = {k: v for k, v in (getattr(league, "player_map", None) or {}).items() if isinstance(k, int) and v}
    have = {r["id"] for r in conn.execute("SELECT id FROM players")}
    new = [(pid, name) for pid, name in pool.items() if pid not in have]
    for pid, name in new:
        conn.execute("INSERT INTO players (id, name) VALUES (?, ?) ON CONFLICT (id) DO NOTHING", (pid, name))
    return len(new)


def sync_rosters(conn, league):
    conn.execute("DELETE FROM roster_entries")
    count = 0
    for team in league.teams:
        upsert_team(conn, team)
        update_record(conn, team)
        for p in team.roster:
            pid = upsert_player(conn, p)
            if pid is None:
                continue
            conn.execute("""
                INSERT INTO roster_entries (team_id, player_id, lineup_slot, acquisition_type)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (team_id, player_id) DO UPDATE SET lineup_slot=excluded.lineup_slot,
                    acquisition_type=excluded.acquisition_type
            """, (team.team_id, pid, getattr(p, "lineupSlot", None), getattr(p, "acquisitionType", None)))
            count += 1
    ids = [team.team_id for team in league.teams]
    conn.execute("UPDATE teams SET current = 0")
    conn.execute(f"UPDATE teams SET current = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
    return count


def parse_activity(activity):
    """Turn one espn_api Activity into (kind, items).

    Handles both espn_api trade formats: newer versions emit TRADE_SENT (from)
    and TRADE_RECEIVED (to) per player; older ones emit a single TRADED row
    whose team is the receiving team.
    """
    items = []
    trade_by_player = {}
    trade_teams = set()
    kinds = set()

    for action in activity.actions:
        team, verb, player = action[0], action[1], action[2]
        bid = action[3] if len(action) > 3 else 0
        pid, name, _, _ = _player_fields(player)
        tid = _team_id(team)

        if verb in ("TRADE_SENT", "TRADE_RECEIVED", "TRADED"):
            kinds.add("TRADE")
            if tid is not None:
                trade_teams.add(tid)
            key = pid if pid is not None else name
            item = trade_by_player.get(key)
            if item is None:
                item = {"asset_type": "player", "player": player, "player_id": pid,
                        "player_name": name, "from_team_id": None, "to_team_id": None,
                        "action": "TRADED", "bid_amount": 0}
                trade_by_player[key] = item
                items.append(item)
            if verb == "TRADE_SENT":
                item["from_team_id"] = tid
            else:
                item["to_team_id"] = tid
            continue

        item = {"asset_type": "player", "player": player, "player_id": pid,
                "player_name": name, "from_team_id": None, "to_team_id": None,
                "action": verb, "bid_amount": bid or 0}
        if verb in ("FA ADDED", "WAIVER ADDED"):
            item["to_team_id"] = tid
            kinds.add("WAIVER" if verb == "WAIVER ADDED" else "ADD")
        elif verb == "DROPPED":
            item["from_team_id"] = tid
            kinds.add("DROP")
        else:
            item["to_team_id"] = tid
            kinds.add("OTHER")
        items.append(item)

    # Old TRADED format has no sender; in a two-team trade it's the other team.
    if len(trade_teams) == 2:
        for item in trade_by_player.values():
            if item["from_team_id"] is None and item["to_team_id"] in trade_teams:
                item["from_team_id"] = next(t for t in trade_teams if t != item["to_team_id"])

    for k in ("TRADE", "WAIVER", "ADD", "DROP", "OTHER"):
        if k in kinds:
            return k, items
    return "OTHER", items


def activity_key(activity, items):
    sig = sorted(f"{i['action']}|{i['player_id'] or i['player_name']}|{i['from_team_id']}|{i['to_team_id']}"
                 for i in items)
    digest = hashlib.sha1("\n".join(sig).encode()).hexdigest()[:16]
    return f"{activity.date}:{digest}"


def store_activity(conn, activity, season):
    """Insert one activity. Returns True if it was new."""
    kind, items = parse_activity(activity)
    key = activity_key(activity, items)
    if conn.execute("SELECT 1 FROM transactions WHERE espn_key = ?", (key,)).fetchone():
        return False

    for action in activity.actions:
        tid = _team_id(action[0])
        if tid is not None and not conn.execute("SELECT 1 FROM teams WHERE id = ?", (tid,)).fetchone():
            upsert_team(conn, action[0])

    txn_id = conn.insert("""
        INSERT INTO transactions (espn_key, season, date_ms, kind, source, created_at)
        VALUES (?, ?, ?, ?, 'espn', ?)
    """, (key, season, int(activity.date), kind, now_ms()))
    for item in items:
        upsert_player(conn, item["player"])
        conn.execute("""
            INSERT INTO transaction_items (transaction_id, asset_type, player_id, player_name,
                from_team_id, to_team_id, action, bid_amount, source)
            VALUES (?, 'player', ?, ?, ?, ?, ?, ?, 'espn')
        """, (txn_id, item["player_id"], item["player_name"], item["from_team_id"],
              item["to_team_id"], item["action"], item["bid_amount"]))
    return True


def sync_transactions(conn, league, full=False, max_pages=200):
    """Page through ESPN activity, newest first.

    Incremental mode stops at the first page where every transaction is
    already stored; full mode walks the whole feed.
    """
    added = 0
    offset = 0
    for _ in range(max_pages):
        page = league.recent_activity(size=PAGE_SIZE, offset=offset)
        if not page:
            break
        new_here = sum(store_activity(conn, a, league.year) for a in page)
        added += new_here
        conn.commit()
        if len(page) < PAGE_SIZE or (not full and new_here == 0):
            break
        offset += PAGE_SIZE
    return added


def run_sync(conn, year=None, full=False, league_factory=None, user=None, actor_name=None, missing_ok=False):
    """Sync one season. `user` is the signed-in member who started it, if any.

    The current season comes from the activity feed (rosters too); earlier
    seasons from the transaction log, since ESPN drops their feed. With
    missing_ok, a season ESPN has no league for leaves no trace in the sync history.
    """
    import espn_log
    import espn_lineups

    started = now_ms()
    run_id = conn.insert("""INSERT INTO sync_runs (started_at, season, status, user_id, actor_name)
                            VALUES (?, ?, 'running', ?, ?)""",
                         (started, year, user["id"] if user else None,
                          user["display_name"] if user else (actor_name or "System")))
    conn.commit()
    try:
        league = (league_factory or make_league)(year)
        current_year = int(os.environ.get("ESPN_YEAR", league.year))
        if league.year == current_year:
            roster_count = sync_rosters(conn, league)
            espn_log.store_team_names(conn, league)
            store_player_pool(conn, league)
            espn_log.fetch_draft_standings(conn, league_factory or make_league, current_year)
            added = sync_transactions(conn, league, full=full)
            weeks = espn_lineups.sync_lineups(conn, league)
            msg = f"{roster_count} rostered players, {added} new transactions" + (f", {weeks} weeks of lineups" if weeks else "")
        else:
            msg = espn_log.import_season(conn, league)["message"]
            weeks = espn_lineups.sync_lineups(conn, league, finished=True)
            if weeks:
                msg += f", {weeks} weeks of lineups"
        conn.execute("UPDATE sync_runs SET finished_at=?, status='ok', message=?, season=? WHERE id=?",
                     (now_ms(), msg, league.year, run_id))
        conn.commit()
        return {"ok": True, "message": msg, "season": league.year}
    except Exception as e:  # surface ESPN/auth errors in the UI
        conn.rollback()
        if missing_ok and type(e).__name__ == "ESPNInvalidLeague":
            conn.execute("DELETE FROM sync_runs WHERE id = ?", (run_id,))
            conn.commit()
            return {"ok": False, "missing": True, "message": f"ESPN has no league data for {year}."}
        conn.execute("UPDATE sync_runs SET finished_at=?, status='error', message=? WHERE id=?",
                     (now_ms(), f"{type(e).__name__}: {e}", run_id))
        conn.commit()
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}
