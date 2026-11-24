"""Tests using a fake espn_api League (no network).

They run against SQLite, and also against Postgres when TEST_DATABASE_URL
points at a database the tests may wipe, e.g.
    TEST_DATABASE_URL=postgresql://test@localhost:55432/dynasty_test pytest
"""
import os
import sys
from collections import Counter
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as app_module  # loads any local .env now, so each test's own settings below take precedence
import auth
import db
import espn_log
import espn_sync


def player(pid, name, pos="WR", slot="BE"):
    return NS(playerId=pid, name=name, position=pos, proTeam="KC", lineupSlot=slot, acquisitionType="ADD")


A = NS(team_id=1, team_name="Alpha", team_abbrev="ALP", owners=[{"firstName": "Tom", "lastName": "R"}], logo_url=None,
       wins=5, losses=2, ties=0, points_for=812.4, standing=1,
       roster=[player(10, "Patrick Mahomes", "QB", "QB"), player(11, "Josh Downs")])
B = NS(team_id=2, team_name="Bravo", team_abbrev="BRV", owners=[], logo_url=None,
       wins=3, losses=4, ties=0, points_for=701.0, standing=2,
       roster=[player(20, "Bijan Robinson", "RB", "RB")])


def activity(date, actions):
    return NS(date=date, actions=actions)


FEED = [
    # newer espn_api trade format
    activity(3000, [(A, "TRADE_SENT", player(11, "Josh Downs"), 0), (B, "TRADE_RECEIVED", player(11, "Josh Downs"), 0),
                    (B, "TRADE_SENT", player(21, "Some RB"), 0), (A, "TRADE_RECEIVED", player(21, "Some RB"), 0)]),
    activity(2000, [(A, "WAIVER ADDED", player(12, "Waiver Guy"), 7), (A, "DROPPED", player(13, "Cut Guy"), 0)]),
    activity(1000, [(B, "FA ADDED", player(22, "FA Guy"), 0)]),
    # old espn_api trade format: team = receiver
    activity(500, [(A, "TRADED", player(30, "Old A"), 0), (B, "TRADED", player(31, "Old B"), 0)]),
]


class FakeLeague:
    year = 2026
    teams = [A, B]

    def __init__(self, feed):
        self.feed = feed

    def recent_activity(self, size=25, msg_type=None, offset=0):
        return self.feed[offset:offset + size]


BACKENDS = ["sqlite"] + (["postgres"] if os.environ.get("TEST_DATABASE_URL") else [])


@pytest.fixture(params=BACKENDS)
def flask_app(request, tmp_path, monkeypatch):
    monkeypatch.setenv("ESPN_LEAGUE_ID", "1280240")
    monkeypatch.setenv("ESPN_YEAR", "2026")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    for var in ("AUTO_SYNC_HOURS", "COMMISSIONER_USERNAME", "COMMISSIONER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    if request.param == "postgres":
        monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
        conn = db.connect()
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        conn.close()
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(espn_sync, "make_league", lambda year=None: FakeLeague(FEED))
    auth.reset_throttle()
    return app_module.create_app()


def make_user(username, name, password="correct-horse", commissioner=False, must_change=False):
    conn = db.connect()
    uid, _ = auth.create_user(conn, username, name, password=password, is_commissioner=commissioner)
    if not must_change:
        conn.execute("UPDATE users SET must_change_password = 0 WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    return uid


def signed_in(flask_app, username, password="correct-horse"):
    c = flask_app.test_client()
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.json
    return c


@pytest.fixture
def client(flask_app):
    make_user("commish", "Commish", commissioner=True)
    return signed_in(flask_app, "commish")


def test_parse_trade_formats():
    kind, items = espn_sync.parse_activity(FEED[0])
    assert kind == "TRADE"
    moves = {i["player_name"]: (i["from_team_id"], i["to_team_id"]) for i in items}
    assert moves == {"Josh Downs": (1, 2), "Some RB": (2, 1)}
    kind, items = espn_sync.parse_activity(FEED[3])
    assert {i["player_name"]: (i["from_team_id"], i["to_team_id"]) for i in items} == {"Old A": (2, 1), "Old B": (1, 2)}
    kind, items = espn_sync.parse_activity(FEED[1])
    assert kind == "WAIVER" and items[0]["bid_amount"] == 7 and items[1]["from_team_id"] == 1


def test_pagination_and_resync_preserves_manual_edits(client):
    big_feed = [activity(10000 - i, [(A, "FA ADDED", player(100 + i, f"P{i}"), 0)]) for i in range(60)]
    conn = db.connect()
    added = espn_sync.sync_transactions(conn, FakeLeague(big_feed), full=True)
    conn.close()
    assert added == 60

    r = client.post("/api/sync", json={"full": True})
    assert r.status_code == 200, r.json
    assert client.get("/api/sync").json["counts"]["transactions"] == 64
    assert client.get("/api/sync").json["runs"][0]["actor_name"] == "Commish"

    client.post("/api/picks/generate", json={"start_season": 2027, "end_season": 2028, "rounds": 3})
    picks = client.get("/api/picks").json
    assert len(picks) == 2 * 2 * 3
    alpha_first = next(p for p in picks if p["season"] == 2027 and p["round"] == 1 and p["original_team_id"] == 1)

    trade = next(t for t in client.get("/api/transactions?kind=TRADE").json if t["date_ms"] == 3000)
    r = client.post(f"/api/transactions/{trade['id']}/items",
                    json={"asset_type": "pick", "pick_id": alpha_first["id"], "from_team_id": 1, "to_team_id": 2})
    assert r.status_code == 201 and r.json["warnings"] == []

    # resync must not duplicate or wipe the added pick
    assert client.post("/api/sync", json={"full": True}).status_code == 200
    assert client.get("/api/sync").json["counts"]["transactions"] == 64
    trade = next(t for t in client.get("/api/transactions?has_picks=1").json)
    assert any(i["asset_type"] == "pick" for i in trade["items"])

    owners = {p["id"]: p["owner_team_id"] for p in client.get("/api/picks").json}
    assert owners[alpha_first["id"]] == 2
    bravo = next(t for t in client.get("/api/teams").json if t["id"] == 2)
    assert alpha_first["id"] in [p["id"] for p in bravo["picks"]]
    assert [p["name"] for p in bravo["roster"]] == ["Bijan Robinson"]
    assert [t["date_ms"] for t in client.get("/api/transactions?q=josh").json] == [3000]


def test_manual_trade_and_ownership_chain(client):
    client.post("/api/sync", json={})
    client.post("/api/picks/generate", json={"start_season": 2027, "end_season": 2027, "rounds": 1})
    pick = next(p for p in client.get("/api/picks").json if p["original_team_id"] == 2)

    r = client.post("/api/transactions", json={"kind": "TRADE", "date": "2026-11-01T12:00", "note": "pick swap",
                                                "items": [{"asset_type": "pick", "pick_id": pick["id"], "from_team_id": 2, "to_team_id": 1}]})
    assert r.status_code == 201, r.json
    txn_id = r.json["transaction"]["id"]

    # Later trade sends it back; but claiming it came from the wrong team warns
    r = client.post("/api/transactions", json={"kind": "TRADE", "date": "2026-12-01T12:00",
                                                "items": [{"asset_type": "pick", "pick_id": pick["id"], "from_team_id": 2, "to_team_id": 1}]})
    assert r.json["warnings"]
    later = r.json["transaction"]["id"]
    assert client.delete(f"/api/transactions/{later}", json={}).status_code == 200

    assert {p["id"]: p["owner_team_id"] for p in client.get("/api/picks").json}[pick["id"]] == 1
    history = next(p for p in client.get("/api/picks").json if p["id"] == pick["id"])["history"]
    assert len(history) == 1

    # ESPN transactions can't be deleted, ESPN items can't be removed
    espn_txn = next(t for t in client.get("/api/transactions").json if t["source"] == "espn")
    assert client.delete(f"/api/transactions/{espn_txn['id']}", json={}).status_code == 400
    assert client.delete(f"/api/items/{espn_txn['items'][0]['id']}", json={}).status_code == 400

    # deleting the manual trade returns the pick to its original team
    client.delete(f"/api/transactions/{txn_id}", json={})
    assert {p["id"]: p["owner_team_id"] for p in client.get("/api/picks").json}[pick["id"]] == 2

    csv = client.get("/api/export/transactions.csv").data.decode()
    assert "Josh Downs" in csv


def test_only_trades_can_be_edited(client):
    client.post("/api/sync", json={})
    client.post("/api/picks/generate", json={"start_season": 2027, "end_season": 2027, "rounds": 1})
    pick = client.get("/api/picks").json[0]
    move = {"asset_type": "pick", "pick_id": pick["id"], "from_team_id": 1, "to_team_id": 2}
    for kind in ("WAIVER", "ADD"):
        txn = client.get(f"/api/transactions?kind={kind}").json[0]
        assert client.patch(f"/api/transactions/{txn['id']}", json={"note": "x"}).status_code == 400
        assert client.post(f"/api/transactions/{txn['id']}/items", json=move).status_code == 400
        assert client.get(f"/api/transactions?kind={kind}").json[0]["note"] is None
    assert client.post("/api/transactions", json={"kind": "WAIVER", "items": [move]}).status_code == 400
    assert client.get("/api/activity").json[0]["action"] == "picks.generate"  # nothing was recorded

    trade = client.get("/api/transactions?kind=TRADE").json[0]
    assert client.patch(f"/api/transactions/{trade['id']}", json={"note": "x"}).status_code == 200
    assert client.post(f"/api/transactions/{trade['id']}/items", json=move).status_code == 201


def test_projected_draft_spots_follow_standings(client):
    client.post("/api/sync", json={})
    client.post("/api/picks/generate", json={"start_season": 2027, "end_season": 2028, "rounds": 2})
    picks = client.get("/api/picks").json
    spots = {(p["season"], p["round"], p["original_team_id"]): (p["slot"], p["overall"], p["slot_projected"]) for p in picks}
    # Bravo has the worse record, so it picks first in every round of the next draft
    assert spots[(2027, 1, 2)] == (1, 1, True)
    assert spots[(2027, 1, 1)] == (2, 2, True)
    assert spots[(2027, 2, 2)] == (1, 3, True)
    assert spots[(2028, 1, 2)] == (None, None, False)  # too far out to project
    assert [(p["round"], p["slot"]) for p in picks if p["season"] == 2027] == [(1, 1), (1, 2), (2, 1), (2, 2)]

    # a spot set by hand replaces the projection
    alpha_first = next(p for p in picks if (p["season"], p["round"], p["original_team_id"]) == (2027, 1, 1))
    client.patch(f"/api/picks/{alpha_first['id']}", json={"pick_in_round": 1})
    p = next(p for p in client.get("/api/picks").json if p["id"] == alpha_first["id"])
    assert (p["slot"], p["overall"], p["slot_projected"]) == (1, 1, False)

    alpha = next(t for t in client.get("/api/teams").json if t["id"] == 1)
    assert (alpha["wins"], alpha["losses"], alpha["standing"]) == (5, 2, 1)


class ESPNInvalidLeague(Exception):
    """Stands in for espn_api's error for a season ESPN has no league for."""


def log_item(kind, pid, src, dst):
    return {"type": kind, "playerId": pid, "fromTeamId": src, "toTeamId": dst}


def log_record(id, kind, status="EXECUTED", team=1, date=1_660_000_000_000, items=(), related=None, **extra):
    rec = {"id": id, "type": kind, "teamId": team, "proposedDate": date, "items": list(items), "bidAmount": 0, **extra}
    if status is not None:
        rec["status"] = status
    if related:
        rec["relatedTransactionId"] = related
    return rec


PAST_LOG = {  # scoring period -> records; ESPN repeats some records across periods
    0: [
        # waiver claim with a drop; 2018-style placeholder team id on the record itself
        log_record("w1", "WAIVER", team=-2147483648, items=[log_item("ADD", 501, 0, 1), log_item("DROP", 502, 1, 0)],
                   processDate=1_660_000_100_000),
        log_record("w2", "WAIVER", status="FAILED_ROSTERLIMIT", items=[log_item("ADD", 503, 0, 1)]),
        log_record("f1", "FREEAGENT", team=3, items=[log_item("ADD", 504, 0, 3)]),
        log_record("f2", "FREEAGENT", team=3, items=[log_item("DROP", 505, 3, 0)]),  # drop-only
        log_record("r1", "ROSTER", items=[log_item("LINEUP", 501, 0, 0)]),  # lineup change, not a move
    ],
    1: [
        log_record("w1", "WAIVER", team=-2147483648, items=[log_item("ADD", 501, 0, 1), log_item("DROP", 502, 1, 0)],
                   processDate=1_660_000_100_000),
        # 2018 style: two identical upholds carry the trade, the pending accept is ignored
        log_record("u1", "TRADE_UPHOLD", date=1_661_000_000_100, items=[log_item("TRADE", 506, 1, 3), log_item("DROP", 507, 3, -1)]),
        log_record("u2", "TRADE_UPHOLD", date=1_661_000_000_400, items=[log_item("TRADE", 506, 1, 3), log_item("DROP", 507, 3, -1)]),
        log_record("a0", "TRADE_ACCEPT", status="PENDING", date=1_660_900_000_000, items=[log_item("TRADE", 506, 1, 3)]),
        # contents hidden: only the accept, pointing at a proposal ESPN doesn't show
        log_record("a1", "TRADE_ACCEPT", team=3, date=1_662_000_000_000, related="hidden"),
        # both teams' accepts plus the visible proposal: one trade
        log_record("p2", "TRADE_PROPOSAL", status="PENDING", date=1_662_900_000_000,
                   items=[log_item("TRADE", 508, 1, 3), log_item("TRADE", 509, 3, 1)]),
        log_record("a2", "TRADE_ACCEPT", team=1, date=1_663_000_000_000, related="p2"),
        log_record("a3", "TRADE_ACCEPT", team=3, date=1_663_000_000_000, related="p2", items=[log_item("DROP", 510, 3, 0)]),
    ],
    2: [
        # 2019 style: accept without a status, upheld after veto votes
        log_record("a4", "TRADE_ACCEPT", status=None, team=1, date=1_664_000_000_000, related="p4"),
        log_record("v1", "TRADE_VETO", team=3, date=1_664_000_100_000, related="p4"),
        log_record("v2", "TRADE_VETO", team=3, date=1_664_000_200_000, related="p4"),
        log_record("u4", "TRADE_UPHOLD", date=1_664_100_000_000, related="p4"),
        log_record("x1", "TRADE_PROPOSAL", status="CANCELED", items=[log_item("TRADE", 511, 1, 3)]),
    ],
}


class PastLeague:
    def __init__(self, year, log):
        self.year = year
        self.teams = [NS(team_id=1, team_name="Old Alpha", team_abbrev="OA"), NS(team_id=3, team_name="Gone Team", team_abbrev="GT")]
        self.player_map = {501: "Waiver Add", 502: "Waiver Drop", 504: "FA Add", 505: "FA Drop", 506: "Traded Star",
                           507: "Trade Cut", 508: "Alpha Sends", 509: "Gone Sends", 510: "Room Maker", "Waiver Add": 501}
        self.espn_request = NS(league_get=lambda params: {"transactions": log.get(params["scoringPeriodId"], [])})


def past_league_factory(year=None):
    if year in (None, 2026):
        return FakeLeague(FEED)
    if year == 2025:
        return PastLeague(2025, PAST_LOG)
    if year == 2024:
        return PastLeague(2024, {})
    raise ESPNInvalidLeague(f"League 1280240 does not exist")


def test_past_seasons_come_from_the_transaction_log(client, monkeypatch):
    monkeypatch.setattr(espn_sync, "make_league", past_league_factory)
    client.post("/api/sync", json={})
    r = client.post("/api/sync", json={"all_past": True})
    assert r.status_code == 200, r.json
    assert r.json["seasons"] == [2025, 2024]
    assert "4 trades (2 need their players added)" in r.json["message"]
    assert all(run["status"] == "ok" for run in client.get("/api/sync").json["runs"])  # 2023 left no error behind

    txns = client.get("/api/transactions?season=2025").json
    kinds = Counter(t["kind"] for t in txns)
    assert kinds == {"WAIVER": 1, "ADD": 1, "DROP": 1, "TRADE": 4}

    waiver = next(t for t in txns if t["kind"] == "WAIVER")
    assert waiver["date_ms"] == 1_660_000_100_000
    assert {(i["player_name"], i["from_team_id"], i["to_team_id"]) for i in waiver["items"]} == \
        {("Waiver Add", None, 1), ("Waiver Drop", 1, None)}
    assert waiver["team_names"] == {"1": "Old Alpha", "3": "Gone Team"}

    trades = sorted((t for t in txns if t["kind"] == "TRADE"), key=lambda t: t["date_ms"])
    upheld, hidden, visible, vetoed = trades
    assert {(i["player_name"], i["from_team_id"], i["to_team_id"]) for i in upheld["items"]} == \
        {("Traded Star", 1, 3), ("Trade Cut", 3, None)}
    assert "may list only one side" in upheld["espn_note"]
    assert hidden["items"] == [] and hidden["accepted_by"] == [3]
    assert hidden["espn_note"].startswith("ESPN only shows what was traded")
    assert visible["date_ms"] == 1_663_000_000_000
    assert {(i["player_name"], i["from_team_id"], i["to_team_id"]) for i in visible["items"]} == \
        {("Alpha Sends", 1, 3), ("Gone Sends", 3, 1), ("Room Maker", 3, None)}
    assert visible["espn_note"] is None
    assert (visible["offered_by"], visible["accepted_by"]) == (1, [3])  # the proposer's own accept doesn't count
    assert hidden["offered_by"] is None
    assert "2 veto votes were cast" in vetoed["espn_note"]

    # running it again adds nothing
    client.post("/api/sync", json={"all_past": True})
    assert len(client.get("/api/transactions?season=2025").json) == len(txns)

    # a team that has left the league keeps its history but isn't on the Teams tab or given picks
    teams = {t["id"]: t for t in client.get("/api/teams").json}
    assert teams[3]["current"] == 0 and teams[1]["current"] == 1 and teams[1]["name"] == "Alpha"
    client.post("/api/picks/generate", json={"start_season": 2027, "rounds": 1})
    assert {p["original_team_id"] for p in client.get("/api/picks").json} == {1, 2}

    # members fill in the hidden trade
    pick = client.get("/api/picks").json[0]
    r = client.post(f"/api/transactions/{hidden['id']}/items",
                    json={"asset_type": "player", "player_name": "Somebody", "from_team_id": 1, "to_team_id": 3})
    assert r.status_code == 201

    # the current season never mixes in the log
    conn = db.connect()
    assert espn_log.import_season(conn, PastLeague(2026, PAST_LOG))["skipped"]
    conn.close()


class ESPNAccessDenied(Exception):
    """Stands in for espn_api's error when ESPN rejects a login."""


# What a member of the hidden trade sees: the proposal behind accept "a1" is visible to them.
MEMBER_LOG = {**PAST_LOG, 3: [log_record("hidden", "TRADE_PROPOSAL", status="PENDING", team=1, date=1_661_900_000_000,
                                         items=[log_item("TRADE", 520, 1, 3), log_item("TRADE", 521, 3, 1)])]}
SWID = "{ABCDEF01-2345-6789-ABCD-EF0123456789}"
ESPN_S2 = "AEB-very-secret-espn-s2-cookie-value"


def test_members_recover_their_trades_with_their_own_login(flask_app, client, monkeypatch):
    monkeypatch.setattr(espn_sync, "make_league", past_league_factory)
    client.post("/api/sync", json={})
    client.post("/api/sync", json={"all_past": True})
    uid = make_user("dana", "Dana")
    client.patch(f"/api/users/{uid}", json={"team_id": 1})
    dana = signed_in(flask_app, "dana")
    status = dana.get("/api/recover-trades").json
    assert (status["missing"], status["seasons"]) == (2, [2025])  # the hidden trade and the vetoed one

    hidden = next(t for t in dana.get("/api/transactions?season=2025&kind=TRADE").json if t["date_ms"] == 1_662_000_000_000)
    # someone already typed in one of the players by hand
    dana.post(f"/api/transactions/{hidden['id']}/items",
              json={"asset_type": "player", "player_name": "hidden star", "from_team_id": 1, "to_team_id": 3})

    calls = []

    def member_request(season, swid, espn_s2):
        calls.append((season, swid, espn_s2))
        if espn_s2 == "rejected":
            raise ESPNAccessDenied("League 1280240 cannot be accessed with the provided credentials")
        return NS(league_get=lambda params: {"transactions": MEMBER_LOG.get(params["scoringPeriodId"], [])},
                  get_pro_players=lambda: [{"id": 520, "fullName": "Hidden Star"}, {"id": 521, "fullName": "Second Piece"}])
    monkeypatch.setattr(espn_log, "member_request", member_request)

    assert dana.post("/api/recover-trades", json={"swid": "nope", "espn_s2": ESPN_S2}).status_code == 400
    r = dana.post("/api/recover-trades", json={"swid": SWID, "espn_s2": "rejected"})
    assert r.status_code == 400 and "didn't accept those cookies" in r.json["error"]

    r = dana.post("/api/recover-trades", json={"swid": SWID.strip("{}").lower(), "espn_s2": ESPN_S2})
    assert r.status_code == 200, r.json
    assert r.json["recovered"] == 1 and r.json["missing"] == 1
    assert r.json["message"] == "Success! Your ESPN login worked and filled in 1 trade from 2025."
    again = dana.post("/api/recover-trades", json={"swid": SWID, "espn_s2": ESPN_S2}).json
    assert again["recovered"] == 0 and again["message"].startswith("Success! Your ESPN login worked.")
    assert calls[-1] == (2025, SWID, ESPN_S2)

    hidden = next(t for t in dana.get("/api/transactions?season=2025&kind=TRADE").json if t["id"] == hidden["id"])
    assert sorted((i["player_name"], i["source"]) for i in hidden["items"]) == [("Second Piece", "espn"), ("hidden star", "manual")]
    assert (hidden["offered_by"], hidden["accepted_by"], hidden["espn_note"]) == (1, [3], None)
    entry = dana.get(f"/api/activity?transaction_id={hidden['id']}").json[0]
    assert entry["action"] == "trade.recover" and entry["actor_name"] == "Dana" and "Second Piece" in entry["summary"]

    # the cookies are nowhere in the database
    conn = db.connect()
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")] if conn.kind == "sqlite" else \
        [r["table_name"] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")]
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}"):
            assert ESPN_S2 not in str(row) and SWID.strip("{}") not in str(row).upper(), table
    conn.close()

    # the commissioner's own re-import can't see that trade's players, and mustn't undo the recovery
    client.post("/api/sync", json={"all_past": True})
    again = next(t for t in client.get("/api/transactions?season=2025&kind=TRADE").json if t["id"] == hidden["id"])
    assert (again["offered_by"], again["espn_note"], len(again["items"])) == (1, None, 2)


def test_automatic_sync_schedule_survives_restarts(client, monkeypatch):
    import app as app_module
    conn = db.connect()
    assert app_module.next_auto_sync_ms(conn) is None  # off unless AUTO_SYNC_HOURS is set
    monkeypatch.setenv("AUTO_SYNC_HOURS", "24")
    assert app_module.next_auto_sync_ms(conn) <= db.now_ms()  # never synced: due now

    client.post("/api/sync", json={})
    last = conn.scalar("SELECT MAX(finished_at) AS t FROM sync_runs WHERE status = 'ok'")
    assert app_module.next_auto_sync_ms(conn) == last + 24 * 3600 * 1000
    status = client.get("/api/sync").json
    assert (status["auto_sync_hours"], status["next_auto_sync"]) == (24, last + 24 * 3600 * 1000)

    # a failed automatic attempt waits an hour before trying again rather than retrying every check
    monkeypatch.setenv("AUTO_SYNC_HOURS", "0.0001")
    monkeypatch.setattr(espn_sync, "make_league", lambda year=None: (_ for _ in ()).throw(RuntimeError("401")))
    espn_sync.run_sync(conn, actor_name="Automatic sync")
    tried = conn.scalar("SELECT MAX(started_at) AS t FROM sync_runs WHERE actor_name = 'Automatic sync'")
    assert app_module.next_auto_sync_ms(conn) == tried + app_module.AUTO_SYNC_RETRY_MS
    conn.close()


def test_sync_error_is_reported(client, monkeypatch):
    def boom(year=None):
        raise RuntimeError("401 not authorized")
    monkeypatch.setattr(espn_sync, "make_league", boom)
    r = client.post("/api/sync", json={})
    assert r.status_code == 502 and "401" in r.json["message"]
    assert client.get("/api/sync").json["runs"][0]["status"] == "error"


def test_everything_requires_sign_in(flask_app):
    anon = flask_app.test_client()
    for path in ("/api/teams", "/api/transactions", "/api/picks", "/api/activity", "/api/export/transactions.csv"):
        assert anon.get(path).status_code == 401, path
    assert anon.post("/api/sync", json={}).status_code == 401
    assert anon.get("/api/me").status_code == 401
    assert anon.get("/healthz").status_code == 200
    assert anon.get("/").status_code == 200
    # changes must be JSON, so another site can't submit a form on a member's behalf
    assert anon.post("/api/login", data={"username": "x", "password": "y"}).status_code == 415


def test_new_member_must_choose_password(flask_app, client):
    client.post("/api/sync", json={})
    r = client.post("/api/users", json={"username": "Dana", "display_name": "Dana", "team_id": 2})
    assert r.status_code == 201, r.json
    temp = r.json["temp_password"]
    assert r.json["user"]["username"] == "dana" and r.json["user"]["must_change_password"]

    dana = signed_in(flask_app, "DANA", temp)  # usernames aren't case sensitive
    other_device = signed_in(flask_app, "dana", temp)
    assert dana.get("/api/me").json["must_change_password"] is True
    assert dana.get("/api/teams").status_code == 403

    assert dana.post("/api/password", json={"new_password": "short"}).status_code == 400
    assert dana.post("/api/password", json={"new_password": temp}).status_code == 400
    r = dana.post("/api/password", json={"new_password": "a-better-password"})
    assert r.status_code == 200 and r.json["must_change_password"] is False
    assert dana.get("/api/teams").status_code == 200
    assert other_device.get("/api/teams").status_code == 401  # signed out by the password change

    # a normal password change needs the current password
    assert dana.post("/api/password", json={"current_password": "nope", "new_password": "another-one-123"}).status_code == 400
    assert dana.post("/api/password", json={"current_password": "a-better-password",
                                            "new_password": "another-one-123"}).status_code == 200

    # commissioner reset forces another change
    uid = r.json["id"]
    temp2 = client.post(f"/api/users/{uid}/reset-password", json={}).json["temp_password"]
    assert dana.get("/api/teams").status_code == 401
    assert signed_in(flask_app, "dana", temp2).get("/api/me").json["must_change_password"] is True


def test_members_edit_and_activity_log_shows_who(flask_app, client):
    client.post("/api/sync", json={})
    client.post("/api/picks/generate", json={"start_season": 2027, "end_season": 2027, "rounds": 1})
    make_user("dana", "Dana")
    dana = signed_in(flask_app, "dana")

    # members can't manage accounts or run syncs, but can edit league data
    assert dana.get("/api/users").status_code == 403
    assert dana.post("/api/sync", json={}).status_code == 403
    assert dana.post("/api/picks/generate", json={"start_season": 2030, "rounds": 1}).status_code == 403
    assert dana.post("/api/picks", json={"season": 2030, "round": 1, "original_team_id": 1}).status_code == 403
    assert dana.post("/api/users", json={"username": "x", "display_name": "X"}).status_code == 403

    pick = next(p for p in dana.get("/api/picks").json if p["original_team_id"] == 1)
    espn_trade = next(t for t in dana.get("/api/transactions?kind=TRADE").json if t["date_ms"] == 3000)
    r = dana.post(f"/api/transactions/{espn_trade['id']}/items",
                  json={"asset_type": "pick", "pick_id": pick["id"], "from_team_id": 1, "to_team_id": 2})
    assert r.status_code == 201
    added = next(i for i in r.json["transaction"]["items"] if i["asset_type"] == "pick")
    assert added["created_by_name"] == "Dana"

    r = dana.post("/api/transactions", json={"date": "2026-11-01T12:00", "items": [
        {"asset_type": "pick", "pick_id": pick["id"], "from_team_id": 2, "to_team_id": 1}]})
    manual = r.json["transaction"]
    assert manual["created_by_name"] == "Dana"
    assert dana.patch(f"/api/transactions/{manual['id']}", json={"note": "for a 3rd"}).status_code == 200
    assert dana.patch(f"/api/picks/{pick['id']}", json={"pick_in_round": 4, "used_on": "", "note": ""}).status_code == 200

    # someone else removes Dana's work; the log keeps what was there
    assert client.delete(f"/api/items/{added['id']}", json={}).status_code == 200
    assert client.delete(f"/api/transactions/{manual['id']}", json={}).status_code == 200

    log = client.get("/api/activity").json
    by_action = {e["action"]: e for e in log}
    assert by_action["item.add"]["actor_name"] == "Dana"
    assert "2027 Rd 1 (ALP) (Alpha → Bravo)" in by_action["item.add"]["summary"]
    assert by_action["trade.update"]["details"] == {"before": {"note": None}, "after": {"note": "for a 3rd"}}
    assert "pick number (blank) → 4" in by_action["pick.update"]["summary"]
    assert by_action["item.remove"]["actor_name"] == "Commish"
    assert "(added by Dana)" in by_action["item.remove"]["summary"]
    deleted = by_action["trade.delete"]
    assert deleted["actor_name"] == "Commish" and "recorded by Dana" in deleted["summary"]
    assert deleted["details"]["items"][0]["label"].startswith("2027 Rd 1")
    assert by_action["picks.generate"]["actor_name"] == "Commish"

    # filters
    assert {e["actor_name"] for e in client.get(f"/api/activity?user_id={by_action['item.add']['user_id']}").json} == {"Dana"}
    assert all(e["pick_id"] == pick["id"] for e in client.get(f"/api/activity?pick_id={pick['id']}").json)
    first_page = client.get("/api/activity?limit=2").json
    rest = client.get(f"/api/activity?before={first_page[-1]['id']}").json
    assert len(first_page) + len(rest) == len(log)


def test_turned_off_account_is_signed_out(flask_app, client):
    uid = make_user("dana", "Dana")
    dana = signed_in(flask_app, "dana")
    assert client.patch(f"/api/users/{uid}", json={"active": False}).status_code == 200
    assert dana.get("/api/teams").status_code == 401
    r = flask_app.test_client().post("/api/login", json={"username": "dana", "password": "correct-horse"})
    assert r.status_code == 401 and "turned off" in r.json["error"]
    assert "account turned off" in client.get("/api/activity").json[0]["summary"]


def test_commissioner_cannot_lock_themselves_out(client):
    me = client.get("/api/me").json
    assert client.patch(f"/api/users/{me['id']}", json={"is_commissioner": False}).status_code == 400
    assert client.patch(f"/api/users/{me['id']}", json={"active": False}).status_code == 400


def test_repeated_failed_sign_ins_are_throttled(flask_app):
    make_user("dana", "Dana")
    c = flask_app.test_client()
    for _ in range(auth.MAX_FAILURES):
        assert c.post("/api/login", json={"username": "dana", "password": "wrong"}).status_code == 401
    r = c.post("/api/login", json={"username": "dana", "password": "correct-horse"})
    assert r.status_code == 429


def test_first_commissioner_created_from_environment(flask_app, monkeypatch):
    monkeypatch.setenv("COMMISSIONER_USERNAME", "Boss")
    monkeypatch.setenv("COMMISSIONER_PASSWORD", "first-password")
    app_module.create_app()
    app_module.create_app()  # only once, on an empty users table
    c = signed_in(flask_app, "boss", "first-password")
    me = c.get("/api/me").json
    assert me["is_commissioner"] and me["must_change_password"]
    assert len(c.post("/api/password", json={"new_password": "my-own-password"}).json) and \
        len(c.get("/api/users").json) == 1


# ---------- team awards ----------

import awards
import espn_lineups

DAY = 86_400_000


def test_stints_from_moves_and_lineups():
    w = lambda week: espn_lineups.week_start(2025, week) + awards.HALF_WEEK  # mid-week of a 2025 week
    now = espn_lineups.week_start(2026, 1)

    # added, then dropped: exact dates
    [s] = awards.build_stints([(w(2), None, 1, "add"), (w(4), 1, None, "drop")], [(w(3), 1)], set(), now)
    assert (s["team"], s["start"], s["end"], s["how_in"], s["how_out"], s["start_known"]) == (1, w(2), w(4), "add", "drop", True)

    # only ever seen in lineups, then turns up on another team with no recorded move (a hidden trade)
    a, b = awards.build_stints([], [(w(1), 1), (w(2), 1), (w(5), 2)], {2}, now)
    assert (a["team"], a["how_in"], a["start_known"], a["how_out"]) == (1, "earlier", False, "unrecorded")
    assert a["end"] <= w(5) - awards.HALF_WEEK and a["end"] >= w(2)
    assert (b["team"], b["how_in"], b["end"], b["how_out"]) == (2, "unrecorded", now, None)  # still on 2's roster

    # first we hear of him is a drop: he'd been there since before our records
    [s] = awards.build_stints([(w(3), 1, None, "drop")], [], set(), now)
    assert (s["start"], s["end"], s["how_out"]) == (None, w(3), "drop")

    # traded: one stint ends, the next begins at the same moment
    a, b = awards.build_stints([(w(1), None, 1, "add"), (w(6), 1, 2, "trade")], [], {2}, now)
    assert (a["end"], b["start"], b["how_in"]) == (w(6), w(6), "trade")


def add_lineup(conn, week, team, pid, name, pos, slot, pts):
    conn.execute("""INSERT INTO lineups (season, week, team_id, player_id, player_name, position, slot, points)
                    VALUES (2025, ?, ?, ?, ?, ?, ?, ?)""", (week, team, pid, name, pos, slot, pts))


def test_team_awards(client):
    client.post("/api/sync", json={})
    conn = db.connect()
    wk = lambda week: espn_lineups.week_start(2025, week)
    for week in range(1, 5):
        add_lineup(conn, week, 1, 101, "Star QB", "QB", "QB", 20 + week)        # MVP; best game 24 in week 4
        add_lineup(conn, week, 1, 102, "Sad WR", "WR", "WR", 2)                 # bust: 2 per start
        add_lineup(conn, week, 1, 103, "Bench RB", "RB", "BE", 15)              # 60 bench points, 4 weeks
    add_lineup(conn, 2, 1, 104, "Pickup TE", "TE", "TE", 18)                    # waiver gem
    add_lineup(conn, 1, 2, 201, "Bravo Guy", "RB", "RB", 10)
    # Pickup TE: added in week 2, dropped two days later
    t = conn.insert("INSERT INTO transactions (season, date_ms, kind, source) VALUES (2025, ?, 'ADD', 'espn')", (wk(2) + DAY,))
    conn.execute("INSERT INTO transaction_items (transaction_id, asset_type, player_id, player_name, to_team_id, action) "
                 "VALUES (?, 'player', 104, 'Pickup TE', 1, 'FA ADDED')", (t,))
    t = conn.insert("INSERT INTO transactions (season, date_ms, kind, source) VALUES (2025, ?, 'DROP', 'espn')", (wk(2) + 3 * DAY,))
    conn.execute("INSERT INTO transaction_items (transaction_id, asset_type, player_id, player_name, from_team_id, action) "
                 "VALUES (?, 'player', 104, 'Pickup TE', 1, 'DROPPED')", (t,))
    conn.commit()
    conn.close()

    r = client.get("/api/awards?season=2025").json
    assert r["season"] == 2025 and 2025 in r["seasons"] and r["has_lineups"]
    alpha = {a["key"]: a for a in next(t for t in r["teams"] if t["team_id"] == 1)["awards"]}
    assert (alpha["mvp"]["player"], alpha["mvp"]["value"]) == ("Star QB", "90 pts as a starter")
    assert (alpha["best_game"]["player"], alpha["best_game"]["detail"]) == ("Star QB", "Week 4")
    assert (alpha["bench_points"]["player"], alpha["bench_points"]["value"]) == ("Bench RB", "60 pts on the bench")
    assert alpha["bench_weeks"]["value"] == "4 weeks on the bench"
    assert (alpha["bust"]["player"], alpha["bust"]["value"]) == ("Sad WR", "2 pts per start")
    assert alpha["waiver_gem"]["player"] == "Pickup TE"
    assert (alpha["fewest_days"]["player"], alpha["fewest_days"]["value"]) == ("Pickup TE", "2 days")
    assert alpha["fewest_days"]["detail"].startswith("Added Sep") and "dropped Sep" in alpha["fewest_days"]["detail"]
    assert alpha["most_days"]["player"] in ("Star QB", "Sad WR", "Bench RB")
    assert list(alpha) == ["mvp", "best_game", "bench_points", "bench_weeks", "most_days", "fewest_days", "waiver_gem", "bust"]

    # a season without lineups (2018-style) leaves out the awards that need them
    assert client.get("/api/awards?season=1999").json["teams"] == []
    assert client.get("/api/awards?season=nope").status_code == 400


def test_lineups_are_synced_each_week(client, monkeypatch):
    class Box:
        def __init__(self, pid, slot, pts):
            self.playerId, self.name, self.position, self.proTeam = pid, f"P{pid}", "WR", "KC"
            self.slot_position, self.points, self.projected_points = slot, pts, 10

    fetched = []

    def fetch_week(league, week):
        fetched.append(week)
        return [(1, Box(11, "WR", week * 1.5)), (2, Box(20, "BE", 3))]
    monkeypatch.setattr(espn_lineups, "fetch_week", fetch_week)
    league = FakeLeague(FEED)
    league.current_week, league.espn_request = 3, object()
    conn = db.connect()
    assert espn_lineups.sync_lineups(conn, league) == 3 and fetched == [1, 2, 3]
    assert espn_lineups.sync_lineups(conn, league) == 1 and fetched[-1] == 3  # the latest week again, as it may change
    assert espn_lineups.sync_lineups(conn, league, finished=True) == 0      # a complete past season isn't refetched
    rows = list(conn.execute("SELECT week, team_id, player_id, slot, points FROM lineups ORDER BY week, team_id"))
    assert len(rows) == 6 and rows[-2] == {"week": 3, "team_id": 1, "player_id": 11, "slot": "WR", "points": 4.5}
    league.year = 2018
    assert espn_lineups.sync_lineups(conn, league) == 0  # no box scores before 2019
    conn.close()


def test_all_time_awards_combine_seasons(client):
    client.post("/api/sync", json={})
    conn = db.connect()

    def lineup(season, week, team, pid, name, pos, slot, pts):
        conn.execute("""INSERT INTO lineups (season, week, team_id, player_id, player_name, position, slot, points)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (season, week, team, pid, name, pos, slot, pts))

    for season in (2024, 2025):
        for week in range(1, 6):
            lineup(season, week, 1, 101, "Star QB", "QB", "QB", 20)                  # 200 pts over 10 starts
            lineup(season, week, 1, 102, "Sad WR", "WR", "WR", 3)                    # bust once he has 8+ starts
    conn.execute("UPDATE lineups SET points = 41 WHERE season = 2024 AND week = 2 AND player_id = 101")
    # Traded Guy: on team 2 in 2024, then traded to team 1 for 2025; team 1 only gets his 2025 points
    for week in range(1, 6):
        lineup(2024, week, 2, 103, "Traded Guy", "RB", "RB", 30)
        lineup(2025, week, 1, 103, "Traded Guy", "RB", "RB", 10)
    t = conn.insert("INSERT INTO transactions (season, date_ms, kind, source) VALUES (2025, ?, 'TRADE', 'espn')",
                    (espn_lineups.season_start(2025) - 30 * DAY,))
    conn.execute("INSERT INTO transaction_items (transaction_id, asset_type, player_id, player_name, from_team_id, to_team_id, action) "
                 "VALUES (?, 'player', 103, 'Traded Guy', 2, 1, 'TRADED')", (t,))
    conn.commit()
    conn.close()

    r = client.get("/api/awards?season=all").json
    assert r["season"] == "all" and r["has_lineups"]
    alpha = {a["key"]: a for a in next(t for t in r["teams"] if t["team_id"] == 1)["awards"]}
    assert (alpha["mvp"]["player"], alpha["mvp"]["value"], alpha["mvp"]["detail"]) == \
        ("Star QB", "221 pts as a starter", "10 starts over 2 seasons")
    assert alpha["best_game"]["detail"] == "Week 2, 2024"
    assert (alpha["trade_prize"]["player"], alpha["trade_prize"]["value"]) == ("Traded Guy", "50 pts as a starter")
    assert (alpha["bust"]["player"], alpha["bust"]["detail"]) == ("Sad WR", "10 starts")
    assert alpha["most_days"]["value"].endswith("days") and "this season" not in alpha["most_days"]["value"]
    bravo = {a["key"]: a for a in next(t for t in r["teams"] if t["team_id"] == 2)["awards"]}
    assert (bravo["mvp"]["player"], bravo["mvp"]["value"]) == ("Traded Guy", "150 pts as a starter")
    assert "bust" not in bravo  # 5 starts isn't enough all-time

    # a single season still shows that season alone
    one = client.get("/api/awards?season=2025").json
    alpha_2025 = {a["key"]: a for a in next(t for t in one["teams"] if t["team_id"] == 1)["awards"]}
    assert alpha_2025["mvp"]["value"] == "100 pts as a starter" and alpha_2025["best_game"]["detail"] == "Week 1"


def test_league_records(client):
    client.post("/api/sync", json={})  # teams 1 (Alpha) and 2 (Bravo)
    conn = db.connect()
    conn.execute("INSERT INTO team_seasons (season, team_id, name, abbrev) VALUES (2024, 1, 'Old Alpha', 'OLD')")

    def lineup(season, week, team, pid, name, slot, pts):
        conn.execute("""INSERT INTO lineups (season, week, team_id, player_id, player_name, position, slot, points)
                        VALUES (?, ?, ?, ?, ?, 'WR', ?, ?)""", (season, week, team, pid, name, slot, pts))

    lineup(2024, 1, 1, 101, "Big Day", "WR", 50)
    lineup(2024, 1, 1, 102, "Sidekick", "WR", 10)
    lineup(2024, 1, 1, 103, "Sat Down", "BE", 40)
    lineup(2024, 2, 1, 101, "Big Day", "WR", 5)
    lineup(2024, 1, 2, 201, "Steady", "WR", 30)
    lineup(2024, 2, 2, 201, "Steady", "WR", 30)
    conn.commit()
    conn.close()

    r = client.get("/api/records").json
    assert r["season"] == "all" and r["seasons"] == [2024]
    rec = r["records"]
    assert [(x["rank"], x["player"], x["pts"]) for x in rec["player_week"][:2]] == [(1, "Big Day", 50), (2, "Steady", 30)]
    best = rec["team_week_high"][0]
    assert (best["team"], best["then"], best["week"], best["pts"], best["top"]) == ("Alpha", "Old Alpha", 1, 60, "Big Day")
    assert (rec["team_week_low"][0]["team"], rec["team_week_low"][0]["pts"]) == ("Alpha", 5)
    assert [(x["player"], x["pts"]) for x in rec["bench_week"]] == [("Sat Down", 40)]
    assert (rec["player_season"][0]["player"], rec["player_season"][0]["pts"], rec["player_season"][0]["starts"]) == ("Steady", 60, 2)
    ts = {(x["team"], x["season"]): x for x in rec["team_season"]}
    assert (ts[("Alpha", 2024)]["pts"], ts[("Alpha", 2024)]["total"]) == (32.5, 65)  # 65 points over 2 weeks

    one = client.get("/api/records?season=2024").json
    assert one["season"] == 2024 and all(x["season"] == 2024 for x in one["records"]["player_week"])
    assert client.get("/api/records?season=abc").status_code == 400

    # a week still being played can't set a record (Week 2 has just kicked off here)
    import records
    conn = db.connect()
    during = records.compute(conn, 2024, espn_lineups.week_start(2024, 2) + DAY)
    conn.close()
    assert (during["team_week_low"][0]["week"], during["team_week_low"][0]["pts"]) == (1, 30)


def test_hosted_app_refuses_to_run_without_postgres(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv("DATABASE_URL", "")  # an unresolved ${{Postgres.DATABASE_URL}} comes through empty
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        app_module.configure()


# ---------- trade grades and votes ----------

def test_trade_points_and_votes(client, flask_app):
    client.post("/api/sync", json={})  # teams 1 (Alpha) and 2 (Bravo)
    conn = db.connect()
    conn.execute("INSERT INTO players (id, name, position) VALUES (301, 'Big Back', 'RB'), (302, 'Rookie Star', 'WR')")
    pick_id = conn.insert("INSERT INTO draft_picks (season, round, original_team_id, used_on) VALUES (2025, 1, 1, 'Rookie Star')")

    def lineup(season, week, team, pid, slot, pts):
        conn.execute("""INSERT INTO lineups (season, week, team_id, player_id, player_name, position, slot, points)
                        VALUES (?, ?, ?, ?, 'x', 'RB', ?, ?)""", (season, week, team, pid, slot, pts))

    lineup(2024, 2, 2, 301, "RB", 50)   # before the trade, for Bravo: not Alpha's
    lineup(2024, 5, 1, 301, "RB", 20)
    lineup(2024, 6, 1, 301, "BE", 30)   # benched points don't count
    # this league adds its rookies as free agents the summer of the draft
    add = conn.insert("INSERT INTO transactions (season, date_ms, kind, source) VALUES (2025, ?, 'ADD', 'espn')",
                      (espn_lineups.season_start(2025) - 60 * DAY,))
    conn.execute("""INSERT INTO transaction_items (transaction_id, asset_type, player_id, player_name, to_team_id, action)
                    VALUES (?, 'player', 302, 'Rookie Star', 2, 'FA ADDED')""", (add,))
    lineup(2025, 1, 2, 302, "WR", 12)
    lineup(2025, 2, 2, 302, "BE", 9)
    conn.commit()
    conn.close()

    r = client.post("/api/transactions", json={"kind": "TRADE", "date": "2024-10-01T12:00", "season": 2024, "items": [
        {"asset_type": "player", "player_name": "Big Back", "player_id": 301, "from_team_id": 2, "to_team_id": 1},
        {"asset_type": "pick", "pick_id": pick_id, "from_team_id": 1, "to_team_id": 2}]})
    assert r.status_code == 201, r.json
    txn_id = r.json["transaction"]["id"]

    t = next(x for x in client.get("/api/transactions?kind=TRADE").json if x["id"] == txn_id)
    sides = {s["team_id"]: s for s in t["sides"]}
    big = sides[1]["assets"][0]
    assert (big["label"], big["points"], big["starts"]) == ("Big Back", 20, 1)
    rookie = sides[2]["assets"][0]
    assert (rookie["kind"], rookie["used_on"], rookie["points"], rookie["starts"]) == ("pick", "Rookie Star", 12, 1)
    assert (sides[1]["points"], sides[2]["points"]) == (20, 12)
    assert t["votes"] == {"tally": {}, "mine": None}

    vote = lambda c, choice: c.put(f"/api/transactions/{txn_id}/vote", json={"choice": choice})
    assert vote(client, 1).json == {"tally": {"1": ["Commish"]}, "mine": 1}
    make_user("tom", "Tom")
    tom = signed_in(flask_app, "tom")
    assert vote(tom, 0).json == {"tally": {"1": ["Commish"], "0": ["Tom"]}, "mine": 0}
    assert vote(tom, 2).json["tally"] == {"1": ["Commish"], "2": ["Tom"]}  # changing a vote replaces it
    assert vote(tom, 99).status_code == 400  # not a team in the trade
    assert vote(tom, "1").status_code == 400
    assert tom.delete(f"/api/transactions/{txn_id}/vote", json={}).json == {"tally": {"1": ["Commish"]}, "mine": None}
    assert client.put(f"/api/transactions/{add}/vote", json={"choice": 2}).status_code == 404  # not a trade
    mine = next(x for x in client.get("/api/transactions?kind=TRADE").json if x["id"] == txn_id)["votes"]
    assert mine == {"tally": {"1": ["Commish"]}, "mine": 1}

    # a pick traded on again before the draft counts for the team that used it, not this one
    conn = db.connect()
    later = conn.insert("INSERT INTO transactions (season, date_ms, kind, source) VALUES (2025, ?, 'TRADE', 'manual')",
                        (espn_lineups.season_start(2025) - 90 * DAY,))
    conn.execute("""INSERT INTO transaction_items (transaction_id, asset_type, pick_id, from_team_id, to_team_id, action, source)
                    VALUES (?, 'pick', ?, 2, 1, 'PICK TRADED', 'manual')""", (later, pick_id))
    conn.commit()
    conn.close()
    t = next(x for x in client.get("/api/transactions?kind=TRADE").json if x["id"] == txn_id)
    rookie = next(s for s in t["sides"] if s["team_id"] == 2)["assets"][0]
    assert (rookie["traded_on_to"], rookie["points"]) == (1, None)
