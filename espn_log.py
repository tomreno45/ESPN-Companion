"""Import past seasons from ESPN's transaction log (view=mTransactions2).

ESPN only keeps the activity feed that espn_sync reads for the current season.
For earlier seasons the transaction log still has every add, drop and waiver
claim, but trades only in part:

- The log is split by scoring period and only returns records for the period
  asked for, so periods 0-19 are fetched and merged by transaction id.
- Only EXECUTED records count; FAILED_*, CANCELED and PENDING are moves that
  never happened.
- A trade can show up as several records: one TRADE_ACCEPT per team, a
  TRADE_UPHOLD once the review period ends (2018 has two identical ones per
  trade), and the TRADE_PROPOSAL they point to. They are merged into one trade.
- What was traded lives on those records only sometimes. ESPN hides accepted
  proposals from managers whose team wasn't in the deal, so many past trades
  arrive as a dated placeholder that members fill in with Edit.
- Team ids of 0 or less are placeholders (2018 waivers say -2147483648); the
  teams always come from each item's fromTeamId/toTeamId instead.
"""
import hashlib
import json
import os
from collections import Counter
from types import SimpleNamespace as NS

import espn_sync
from db import now_ms

SCORING_PERIODS = range(0, 20)
ROSTER_MOVES = {"WAIVER", "FREEAGENT", "ROSTER"}
KEY_PREFIX = "log:"


def fetch_log(league):
    """Every transaction record for the league's season, keyed by id."""
    return fetch_records(league.espn_request)


def fetch_records(espn_request):
    records = {}
    for period in SCORING_PERIODS:
        data = espn_request.league_get(params={"view": "mTransactions2", "scoringPeriodId": period})
        for t in data.get("transactions") or []:
            records[t["id"]] = t
    return records


def _team(team_id):
    return team_id if team_id and team_id > 0 else None


def _when(t):
    return t.get("processDate") or t["proposedDate"]


def roster_move(t):
    """(kind, items) for an executed add/drop/waiver record, or None for lineup changes and failed claims."""
    if t["type"] not in ROSTER_MOVES or t.get("status") != "EXECUTED":
        return None
    items = []
    for i in t.get("items") or []:
        if i["type"] == "ADD":
            action = "WAIVER ADDED" if t["type"] == "WAIVER" else "FA ADDED"
            items.append({"player_id": i["playerId"], "from_team_id": None, "to_team_id": _team(i["toTeamId"]),
                          "action": action, "bid_amount": t.get("bidAmount") or 0})
        elif i["type"] == "DROP":
            items.append({"player_id": i["playerId"], "from_team_id": _team(i["fromTeamId"]), "to_team_id": None,
                          "action": "DROPPED", "bid_amount": 0})
    if not items:
        return None
    adds = any(i["action"] != "DROPPED" for i in items)
    kind = ("WAIVER" if t["type"] == "WAIVER" else "ADD") if adds else "DROP"
    return kind, items


def _signature(t):
    return ",".join(sorted(f"{i['type']}:{i['playerId']}:{i['fromTeamId']}:{i['toTeamId']}" for i in t.get("items") or []))


def _is_trade_evidence(t):
    # Accepts are sometimes missing a status (2019) and are PENDING when an uphold follows (2018).
    if t["type"] == "TRADE_UPHOLD":
        return t.get("status") == "EXECUTED"
    return t["type"] == "TRADE_ACCEPT" and t.get("status") in ("EXECUTED", None)


def trades(records):
    """Merge the accept/uphold/proposal records of each trade that went through."""
    groups = {}
    for t in records.values():
        if _is_trade_evidence(t):
            key = t.get("relatedTransactionId") or f"{t['proposedDate'] // 1000}:{_signature(t)}"
            groups.setdefault(key, []).append(t)
    vetoes = Counter(t.get("relatedTransactionId") for t in records.values() if t["type"] == "TRADE_VETO")

    out = []
    for key, recs in groups.items():
        proposal = records.get(recs[0].get("relatedTransactionId") or "")
        accepts = [r for r in recs if r["type"] == "TRADE_ACCEPT"]
        moved, dropped = {}, {}
        for r in recs + ([proposal] if proposal else []):
            for i in r.get("items") or []:
                if i["type"] == "TRADE":
                    moved[(i["playerId"], _team(i["fromTeamId"]), _team(i["toTeamId"]))] = None
                elif i["type"] == "DROP" and r is not proposal:
                    dropped[(i["playerId"], _team(i["fromTeamId"]))] = None
        items = [{"player_id": pid, "from_team_id": src, "to_team_id": dst, "action": "TRADED", "bid_amount": 0}
                 for pid, src, dst in moved]
        items += [{"player_id": pid, "from_team_id": src, "to_team_id": None, "action": "DROPPED", "bid_amount": 0}
                  for pid, src in dropped]
        out.append({
            "key": key,
            "date": min(r["proposedDate"] for r in (accepts or recs)),
            # The proposing team sometimes has an accept record of its own; it offered, it didn't accept.
            "offered_by": _team(proposal["teamId"]) if proposal else None,
            "accepted_by": sorted({_team(r["teamId"]) for r in accepts} - {None, _team(proposal["teamId"]) if proposal else None}),
            "items": items,
            "one_sided": bool(moved) and len({(src, dst) for _, src, dst in moved}) == 1,
            "vetoes": vetoes.get(recs[0].get("relatedTransactionId"), 0) if recs[0].get("relatedTransactionId") else 0,
        })
    return out


def trade_note(trade):
    """What ESPN does and doesn't tell us about an imported trade, shown under it.

    Team names are left out: the page shows who accepted (accepted_by) under today's names.
    """
    parts = []
    traded = [i for i in trade["items"] if i["action"] == "TRADED"]
    if not traded:
        parts.append("ESPN only shows what was traded to managers whose team was in the deal; use Edit to add the players and picks.")
    elif trade["one_sided"]:
        parts.append("ESPN may list only one side of this trade; use Edit to add anything missing.")
    if trade["vetoes"]:
        parts.append(f"{trade['vetoes']} veto vote{'s were' if trade['vetoes'] != 1 else ' was'} cast; ESPN still processed the trade.")
    return " ".join(parts) or None


def season_has_feed(conn, season):
    """True if this season came from the activity feed; the two sources can't be matched up, so never mix them."""
    return bool(conn.execute(
        "SELECT 1 FROM transactions WHERE season = ? AND source = 'espn' AND espn_key NOT LIKE ? LIMIT 1",
        (season, KEY_PREFIX + "%")).fetchone())


def store_team_names(conn, league):
    """Record each team's name, record and standing that season, and add teams that have since left the league."""
    names = {}
    for team in league.teams:
        names[team.team_id] = team.team_name
        if not conn.execute("SELECT 1 FROM teams WHERE id = ?", (team.team_id,)).fetchone():
            conn.execute("INSERT INTO teams (id, name, abbrev, current, updated_at) VALUES (?, ?, ?, 0, ?)",
                         (team.team_id, team.team_name, getattr(team, "team_abbrev", None), now_ms()))
        conn.execute("""INSERT INTO team_seasons (season, team_id, name, abbrev, wins, losses, ties, points_for, standing, final_standing)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (season, team_id) DO UPDATE SET name = excluded.name, abbrev = excluded.abbrev,
                            wins = excluded.wins, losses = excluded.losses, ties = excluded.ties, points_for = excluded.points_for,
                            standing = excluded.standing, final_standing = excluded.final_standing""",
                     (league.year, team.team_id, team.team_name, getattr(team, "team_abbrev", None),
                      getattr(team, "wins", None), getattr(team, "losses", None), getattr(team, "ties", None),
                      getattr(team, "points_for", None), getattr(team, "standing", None) or None,
                      getattr(team, "final_standing", None) or None))  # ESPN says 0 until the season is over
        # A team that has left the league goes by the last name it had.
        conn.execute("""UPDATE teams SET name = ?, abbrev = ? WHERE id = ? AND current = 0
                        AND NOT EXISTS (SELECT 1 FROM team_seasons WHERE team_id = ? AND season > ?)""",
                     (team.team_name, getattr(team, "team_abbrev", None), team.team_id, team.team_id, league.year))
    return names


def fetch_draft_standings(conn, make_league, current_year):
    """Fetch the final standings of past seasons that set a recorded draft's order. Returns the seasons fetched.

    A draft's order comes from the season before it, so only those seasons are needed.
    """
    needed = {r["season"] - 1 for r in conn.execute("SELECT DISTINCT season FROM draft_picks")}
    have = {r["season"] for r in conn.execute("""SELECT season FROM team_seasons GROUP BY season
                                                 HAVING COUNT(*) = COUNT(final_standing)""")}
    fetched = []
    for year in sorted(y for y in needed - have if y < current_year):
        try:
            store_team_names(conn, make_league(year))
        except Exception:  # a season ESPN no longer has; that draft's spots stay blank
            continue
        fetched.append(year)
    return fetched


def _ensure_team(conn, team_id):
    if team_id is not None and not conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
        conn.execute("INSERT INTO teams (id, name, current, updated_at) VALUES (?, ?, 0, ?)",
                     (team_id, f"Team {team_id}", now_ms()))


def trade_key(trade):
    return KEY_PREFIX + "trade:" + hashlib.sha1(trade["key"].encode()).hexdigest()[:16]


def _has_contents(conn, txn_id):
    return bool(conn.execute("SELECT 1 FROM transaction_items WHERE transaction_id = ? AND source = 'espn' AND action = 'TRADED'",
                             (txn_id,)).fetchone())


def _store(conn, key, season, date_ms, kind, items, player_names, espn_note=None, accepted_by=None, offered_by=None):
    accepted = json.dumps(accepted_by) if accepted_by else None
    row = conn.execute("SELECT id FROM transactions WHERE espn_key = ?", (key,)).fetchone()
    if row:
        # Already imported: refresh only what ESPN says about it, never items or members' notes. A login
        # that can't see a trade's contents (not in the deal) mustn't undo what a member recovered.
        if not _has_contents(conn, row["id"]) or any(i["action"] == "TRADED" for i in items):
            conn.execute("UPDATE transactions SET espn_note = ?, accepted_by = ? WHERE id = ?", (espn_note, accepted, row["id"]))
        conn.execute("UPDATE transactions SET offered_by = COALESCE(?, offered_by) WHERE id = ?", (offered_by, row["id"]))
        return False
    txn_id = conn.insert("""INSERT INTO transactions (espn_key, season, date_ms, kind, source, espn_note, accepted_by, offered_by, created_at)
                            VALUES (?, ?, ?, ?, 'espn', ?, ?, ?, ?)""",
                         (key, season, int(date_ms), kind, espn_note, accepted, offered_by, now_ms()))
    _insert_items(conn, txn_id, items, player_names)
    return True


def _insert_items(conn, txn_id, items, player_names):
    for it in items:
        _ensure_team(conn, it["from_team_id"])
        _ensure_team(conn, it["to_team_id"])
        name = player_names.get(it["player_id"])
        if name:
            espn_sync.upsert_player(conn, NS(playerId=it["player_id"], name=name))
        conn.execute("""INSERT INTO transaction_items (transaction_id, asset_type, player_id, player_name,
                            from_team_id, to_team_id, action, bid_amount, source)
                        VALUES (?, 'player', ?, ?, ?, ?, ?, ?, 'espn')""",
                     (txn_id, it["player_id"], name or f"Player #{it['player_id']}", it["from_team_id"],
                      it["to_team_id"], it["action"], it["bid_amount"]))


def import_season(conn, league):
    """Import one past season's adds, drops, waiver claims and trades. Safe to run again."""
    season = league.year
    if season_has_feed(conn, season):
        return {"skipped": True, "message": f"{season} already came from ESPN's activity feed"}
    store_team_names(conn, league)
    records = fetch_log(league)
    player_names = {k: v for k, v in (getattr(league, "player_map", None) or {}).items() if isinstance(k, int)}

    counts = Counter()
    for t in records.values():
        move = roster_move(t)
        if move and _store(conn, KEY_PREFIX + t["id"], season, _when(t), move[0], move[1], player_names):
            counts[move[0]] += 1
    for trade in trades(records):
        key = trade_key(trade)
        if _store(conn, key, season, trade["date"], "TRADE", trade["items"], player_names, trade_note(trade),
                  trade["accepted_by"], trade["offered_by"]):
            counts["TRADE"] += 1
            if not any(i["action"] == "TRADED" for i in trade["items"]):
                counts["placeholder"] += 1
    conn.commit()
    if not records:
        return {"skipped": False, "counts": {}, "message": f"ESPN has no transaction records for {season}"}
    parts = [f"{counts['TRADE']} trades" + (f" ({counts['placeholder']} need their players added)" if counts["placeholder"] else ""),
             f"{counts['WAIVER']} waiver claims", f"{counts['ADD']} free agent adds", f"{counts['DROP']} drops"]
    return {"skipped": False, "counts": dict(counts), "message": "Imported " + ", ".join(parts)}


# ---------- recovering trades with a member's own ESPN login ----------

def member_request(season, swid, espn_s2):
    """An ESPN client for one season using a member's cookies. Nothing keeps a reference to them afterwards."""
    from espn_api.requests.espn_requests import EspnFantasyRequests
    return EspnFantasyRequests("nfl", season, int(os.environ["ESPN_LEAGUE_ID"]), cookies={"espn_s2": espn_s2, "SWID": swid})


def recover_trades(conn, espn_request):
    """Fill in already-imported trades with what ESPN shows this login: the trades its team was in.

    Returns [(transaction_id, [items added])]. Items a member already typed in by hand
    (same player name) aren't added twice.
    """
    records = fetch_records(espn_request)
    pro_players = None
    recovered = []
    for trade in trades(records):
        if not any(i["action"] == "TRADED" for i in trade["items"]):
            continue
        row = conn.execute("SELECT id, offered_by FROM transactions WHERE espn_key = ?", (trade_key(trade),)).fetchone()
        if not row:
            continue
        existing = list(conn.execute("SELECT * FROM transaction_items WHERE transaction_id = ?", (row["id"],)))
        have = {(r["player_id"], r["from_team_id"], r["to_team_id"], r["action"]) for r in existing if r["source"] == "espn"}
        typed_in = {(r["player_name"] or "").strip().lower() for r in existing if r["source"] == "manual"}

        ids = [i["player_id"] for i in trade["items"]]
        names = {r["id"]: r["name"] for r in conn.execute(
            f"SELECT id, name FROM players WHERE id IN ({','.join('?' * len(ids))})", ids)}
        if any(pid not in names for pid in ids):
            if pro_players is None:
                pro_players = {p["id"]: p["fullName"] for p in espn_request.get_pro_players() or [] if "fullName" in p}
            names.update({pid: pro_players[pid] for pid in ids if pid in pro_players and pid not in names})

        new = [i for i in trade["items"]
               if (i["player_id"], i["from_team_id"], i["to_team_id"], i["action"]) not in have
               and (names.get(i["player_id"]) or "").lower() not in typed_in]
        if new:
            _insert_items(conn, row["id"], new, names)
        conn.execute("UPDATE transactions SET espn_note = ?, accepted_by = ?, offered_by = COALESCE(?, offered_by) WHERE id = ?",
                     (trade_note(trade), json.dumps(trade["accepted_by"]) if trade["accepted_by"] else None,
                      trade["offered_by"], row["id"]))
        if new or (trade["offered_by"] and not row["offered_by"]):
            recovered.append((row["id"], [dict(i, player_name=names.get(i["player_id"]) or f"Player #{i['player_id']}")
                                          for i in new]))
    return recovered
