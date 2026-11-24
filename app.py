"""Dynasty league companion: ESPN rosters + transaction history + draft picks."""
import csv
import functools
import io
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, g, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import auth
import awards
import db
import espn_log
import espn_sync
import records

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # Railway sits behind one proxy
sync_lock = threading.Lock()

ONLY_TRADES = "Only trades can be edited. Waivers, free agent adds and drops come from ESPN as they are."
KIND_WORD = {"TRADE": "trade", "WAIVER": "waiver claim", "ADD": "free agent add", "DROP": "release", "OTHER": "transaction"}

# Reachable without signing in, or while a new password is still required.
OPEN_PATHS = {"/api/login", "/api/logout", "/api/me"}
PASSWORD_CHANGE_PATHS = OPEN_PATHS | {"/api/password"}


def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def bad_request(msg, status=400):
    return jsonify({"error": msg}), status


def body():
    return request.get_json(silent=True) or {}


def int_or_none(v):
    if v in (None, ""):
        return None
    return int(v)


# ---------- access control ----------

@app.before_request
def require_sign_in():
    if not request.path.startswith("/api/"):
        return None
    # Only accept JSON for changes; a cross-site form post can't send it.
    if request.method in ("POST", "PATCH", "PUT", "DELETE") and not request.is_json:
        return bad_request("Requests that change data must send JSON.", 415)
    g.user = auth.session_user(get_db())
    if request.path in OPEN_PATHS:
        return None
    if g.user is None:
        return jsonify({"error": "Sign in first.", "signed_out": True}), 401
    if g.user["must_change_password"] and request.path not in PASSWORD_CHANGE_PATHS:
        return jsonify({"error": "Choose a new password first.", "must_change_password": True}), 403
    return None


def commissioner_only(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user["is_commissioner"]:
            return bad_request("Only the commissioner can do that.", 403)
        return view(*args, **kwargs)
    return wrapped


@app.get("/healthz")
def healthz():
    get_db().execute("SELECT 1").fetchone()
    return jsonify({"ok": True})


# ---------- serializers ----------

def team_map(conn):
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM teams")}


def user_names(conn):
    return {r["id"]: r["display_name"] for r in conn.execute("SELECT id, display_name FROM users")}


def pick_label(p, teams):
    orig = teams.get(p["original_team_id"], {}).get("abbrev") or teams.get(p["original_team_id"], {}).get("name", "?")
    slot = f".{p['pick_in_round']:02d}" if p["pick_in_round"] else ""
    return f"{p['season']} Rd {p['round']}{slot} ({orig})"


def current_season():
    return int(os.environ.get("ESPN_YEAR", 2026))


def next_draft_season():
    """The draft after the current season, the only one current standings say anything about."""
    return current_season() + 1


def projected_slots(teams):
    """{team_id: slot in each round} for the next draft: worst record picks first.

    Uses ESPN's standings rank when every team has one (it applies the league's
    tiebreakers), otherwise win percentage and then points scored.
    """
    if not teams or any(t.get("wins") is None for t in teams.values()):
        return {}
    if all(t.get("standing") for t in teams.values()):
        best_first = sorted(teams.values(), key=lambda t: t["standing"])
    else:
        def win_pct(t):
            games = (t["wins"] or 0) + (t["losses"] or 0) + (t["ties"] or 0)
            return ((t["wins"] or 0) + 0.5 * (t["ties"] or 0)) / games if games else 0
        best_first = sorted(teams.values(), key=lambda t: (-win_pct(t), -(t["points_for"] or 0)))
    n = len(best_first)
    return {t["id"]: n - i for i, t in enumerate(best_first)}


def load_picks(conn, teams=None):
    teams = teams or team_map(conn)
    owners = db.pick_owners(conn)
    league = {tid: t for tid, t in teams.items() if t["current"]}  # teams in the league now
    projection = projected_slots(league)
    draft_season = next_draft_season()
    picks = []
    for p in conn.execute("SELECT * FROM draft_picks"):
        p["owner_team_id"] = owners.get(p["id"], p["original_team_id"])
        p["label"] = pick_label(p, teams)
        # A pick's spot comes from its original team; a slot set by hand beats the projection.
        slot, projected = p["pick_in_round"], False
        if slot is None and p["season"] == draft_season and p["original_team_id"] in projection:
            slot, projected = projection[p["original_team_id"]], True
        p["slot"] = slot
        p["slot_projected"] = projected
        p["overall"] = (p["round"] - 1) * len(league) + slot if slot else None
        picks.append(p)
    picks.sort(key=lambda p: (p["season"], p["round"], p["slot"] or 99, p["original_team_id"]))
    return picks


def load_transactions(conn, where="", params=(), limit=None):
    teams = team_map(conn)
    names = user_names(conn)
    picks = {p["id"]: p for p in load_picks(conn, teams)}
    sql = f"SELECT * FROM transactions {where} ORDER BY date_ms DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    txns = list(conn.execute(sql, params))
    if not txns:
        return []
    ids = [t["id"] for t in txns]
    items_by_txn = {i: [] for i in ids}
    q = f"SELECT * FROM transaction_items WHERE transaction_id IN ({','.join('?' * len(ids))}) ORDER BY id"
    for it in conn.execute(q, ids):
        if it["asset_type"] == "pick":
            pk = picks.get(it["pick_id"])
            it["label"] = pk["label"] if pk else "(deleted pick)"
        else:
            it["label"] = it["player_name"]
        it["created_by_name"] = names.get(it["created_by"])
        items_by_txn[it["transaction_id"]].append(it)
    seasons = sorted({t["season"] for t in txns if t["season"] is not None})
    season_names = {}
    if seasons:
        for r in conn.execute(f"SELECT * FROM team_seasons WHERE season IN ({','.join('?' * len(seasons))})", seasons):
            season_names.setdefault(r["season"], {})[r["team_id"]] = r["name"]
    for t in txns:
        t["team_names"] = season_names.get(t["season"], {})  # who was in the league that season
        t["accepted_by"] = json.loads(t["accepted_by"]) if t["accepted_by"] else []
        t["items"] = items_by_txn[t["id"]]
        t["team_ids"] = sorted({tid for it in t["items"] for tid in (it["from_team_id"], it["to_team_id"]) if tid})
        t["created_by_name"] = names.get(t["created_by"])
    return txns


# ---------- activity log wording ----------

def fmt_day(date_ms):
    d = datetime.fromtimestamp(date_ms / 1000, timezone.utc)
    return f"{d:%b} {d.day}, {d.year}"


def txn_ref(t):
    return f"the {fmt_day(t['date_ms'])} {KIND_WORD.get(t['kind'], 'transaction')}"


def item_text(it, teams):
    src = teams.get(it["from_team_id"], {}).get("name") if it["from_team_id"] else "free agency"
    dst = teams.get(it["to_team_id"], {}).get("name") if it["to_team_id"] else "released"
    return f"{it['label']} ({src} → {dst})"


def txn_snapshot(t):
    """What's kept in the activity log when a transaction or item disappears."""
    keep = ("id", "season", "date_ms", "kind", "source", "note", "created_by_name")
    out = {k: t[k] for k in keep}
    out["items"] = [{k: it[k] for k in ("label", "asset_type", "from_team_id", "to_team_id", "source", "created_by_name")}
                    for it in t["items"]]
    return out


# ---------- pages ----------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- sign in ----------

@app.post("/api/login")
def api_login():
    conn = get_db()
    data = body()
    user, error = auth.check_login(conn, data.get("username"), data.get("password"), request.remote_addr)
    if error:
        return bad_request(error, 429 if "Too many" in error else 401)
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (db.now_ms(), user["id"]))
    conn.commit()
    auth.start_session(user)
    return jsonify(auth.public_user(user))


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    if g.user is None:
        return jsonify({"error": "Not signed in.", "signed_out": True}), 401
    return jsonify(auth.public_user(g.user))


@app.post("/api/password")
def api_change_password():
    conn = get_db()
    data = body()
    user = g.user
    if user is None:
        return jsonify({"error": "Sign in first.", "signed_out": True}), 401
    new = data.get("new_password") or ""
    # Right after signing in with a temporary password there's no need to type it again.
    if not user["must_change_password"] and not check_password_hash(user["password_hash"], data.get("current_password") or ""):
        return bad_request("Your current password is wrong.")
    problem = auth.password_problem(new, user["username"])
    if problem:
        return bad_request(problem)
    if check_password_hash(user["password_hash"], new):
        return bad_request("Choose a password different from the current one.")
    auth.set_password(conn, user["id"], new, must_change=False)
    db.audit(conn, user, "user.password", "Changed their password")
    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    auth.start_session(user)  # other sessions for this account are now signed out
    return jsonify(auth.public_user(user))


# ---------- members ----------

@app.get("/api/members")
def api_members():
    """Everyone's name and team, for showing who made a change."""
    rows = get_db().execute("SELECT id, display_name, team_id, is_commissioner, active FROM users ORDER BY display_name")
    return jsonify([{**r, "is_commissioner": bool(r["is_commissioner"]), "active": bool(r["active"])} for r in rows])


@app.get("/api/users")
@commissioner_only
def api_users():
    rows = get_db().execute("SELECT * FROM users ORDER BY display_name")
    return jsonify([auth.public_user(u) for u in rows])


def check_team(conn, team_id):
    if team_id is not None and not conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
        raise ValueError("Unknown team.")
    return team_id


@app.post("/api/users")
@commissioner_only
def api_create_user():
    conn = get_db()
    data = body()
    try:
        team_id = check_team(conn, int_or_none(data.get("team_id")))
        uid, password = auth.create_user(conn, data.get("username"), data.get("display_name"),
                                         password=data.get("password") or None, team_id=team_id,
                                         is_commissioner=bool(data.get("is_commissioner")))
    except ValueError as e:
        return bad_request(str(e))
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    team = team_map(conn).get(team_id, {}).get("name")
    db.audit(conn, g.user, "user.create",
             f"Created an account for {user['display_name']} ({user['username']})" + (f", manager of {team}" if team else ""))
    conn.commit()
    return jsonify({"user": auth.public_user(user), "temp_password": password}), 201


@app.patch("/api/users/<int:user_id>")
@commissioner_only
def api_update_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return bad_request("Not found", 404)
    data = body()
    changes = {}
    try:
        if "display_name" in data and (data["display_name"] or "").strip():
            changes["display_name"] = data["display_name"].strip()
        if "team_id" in data:
            changes["team_id"] = check_team(conn, int_or_none(data["team_id"]))
        for flag in ("is_commissioner", "active"):
            if flag in data:
                changes[flag] = 1 if data[flag] else 0
    except ValueError as e:
        return bad_request(str(e))
    changes = {k: v for k, v in changes.items() if v != user[k]}
    if not changes:
        return jsonify(auth.public_user(user))
    if user_id == g.user["id"] and (changes.get("is_commissioner") == 0 or changes.get("active") == 0):
        return bad_request("You can't remove your own commissioner access or turn off your own account.")

    teams = team_map(conn)
    words = []
    if "display_name" in changes:
        words.append(f"renamed to {changes['display_name']}")
    if "team_id" in changes:
        words.append(f"team set to {teams.get(changes['team_id'], {}).get('name', 'none')}")
    if "is_commissioner" in changes:
        words.append("made commissioner" if changes["is_commissioner"] else "commissioner access removed")
    if "active" in changes:
        words.append("account turned back on" if changes["active"] else "account turned off")
    for k, v in changes.items():
        conn.execute(f"UPDATE users SET {k} = ? WHERE id = ?", (v, user_id))
    db.audit(conn, g.user, "user.update", f"Updated {user['display_name']}: " + ", ".join(words),
             details={"before": {k: user[k] for k in changes}, "after": changes})
    conn.commit()
    return jsonify(auth.public_user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()))


@app.post("/api/users/<int:user_id>/reset-password")
@commissioner_only
def api_reset_password(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return bad_request("Not found", 404)
    password = body().get("password") or auth.temp_password()
    problem = auth.password_problem(password, user["username"])
    if problem:
        return bad_request(problem)
    auth.set_password(conn, user_id, password, must_change=True)
    db.audit(conn, g.user, "user.reset_password", f"Reset {user['display_name']}'s password")
    conn.commit()
    if user_id == g.user["id"]:
        auth.start_session(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    return jsonify({"temp_password": password})


# ---------- activity log ----------

@app.get("/api/activity")
def api_activity():
    clauses, params = [], []
    for arg in ("transaction_id", "pick_id", "user_id"):
        if request.args.get(arg):
            clauses.append(f"{arg} = ?")
            params.append(int(request.args[arg]))
    if request.args.get("before"):
        clauses.append("id < ?")
        params.append(int(request.args["before"]))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = list(get_db().execute(f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT {limit}", params))
    for r in rows:
        r["details"] = json.loads(r["details"]) if r["details"] else None
    return jsonify(rows)


# ---------- team awards ----------

@app.get("/api/awards")
def api_awards():
    conn = get_db()
    seasons = awards.seasons_available(conn)
    asked = request.args.get("season") or ""
    try:
        season = None if asked == "all" else int(asked or (seasons[0] if seasons else current_season()))
    except ValueError:
        return bad_request("Unknown season.")
    teams = team_map(conn)
    result = awards.compute(conn, season, db.now_ms())  # season None: every season combined
    out = [{"team_id": tid, "awards": a} for tid, a in result.items() if tid in teams]
    out.sort(key=lambda t: teams[t["team_id"]]["name"].lower())
    has_lineups = bool(conn.scalar("SELECT 1 AS x FROM lineups LIMIT 1") if season is None else
                       conn.scalar("SELECT 1 AS x FROM lineups WHERE season = ? LIMIT 1", (season,)))
    season = season if season is not None else "all"
    return jsonify({"season": season, "seasons": seasons, "has_lineups": has_lineups, "teams": out})


# ---------- league records ----------

@app.get("/api/records")
def api_records():
    conn = get_db()
    seasons = [r["season"] for r in conn.execute("SELECT DISTINCT season FROM lineups ORDER BY season DESC")]
    asked = request.args.get("season") or "all"
    try:
        season = None if asked == "all" else int(asked)
    except ValueError:
        return bad_request("Unknown season.")
    teams = team_map(conn)
    result = records.compute(conn, season, db.now_ms())
    for rows in result.values():
        for r in rows:
            r["team"] = teams[r["team_id"]]["name"] if r["team_id"] in teams else f"Team {r['team_id']}"
    return jsonify({"season": season if season is not None else "all", "seasons": seasons, "records": result})


# ---------- teams / rosters ----------

@app.get("/api/teams")
def api_teams():
    conn = get_db()
    teams = team_map(conn)
    picks = load_picks(conn, teams)
    out = []
    for t in sorted(teams.values(), key=lambda t: t["name"].lower()):
        roster = list(conn.execute("""
            SELECT p.id, p.name, p.position, p.pro_team, r.lineup_slot, r.acquisition_type
            FROM roster_entries r JOIN players p ON p.id = r.player_id
            WHERE r.team_id = ?""", (t["id"],)))
        order = {s: i for i, s in enumerate(espn_sync.SLOT_ORDER)}
        roster.sort(key=lambda p: (order.get(p["lineup_slot"], 99), p["name"]))
        t = dict(t)
        t["roster"] = roster
        t["picks"] = [p for p in picks if p["owner_team_id"] == t["id"]]
        out.append(t)
    return jsonify(out)


# ---------- transactions ----------

@app.get("/api/transactions")
def api_transactions():
    clauses, params = [], []
    if request.args.get("kind"):
        clauses.append("kind = ?")
        params.append(request.args["kind"])
    if request.args.get("season"):
        clauses.append("season = ?")
        params.append(int(request.args["season"]))
    if request.args.get("team"):
        clauses.append("id IN (SELECT transaction_id FROM transaction_items WHERE from_team_id = ? OR to_team_id = ?)")
        params += [int(request.args["team"])] * 2
    if request.args.get("q"):
        clauses.append("(id IN (SELECT transaction_id FROM transaction_items WHERE LOWER(player_name) LIKE ?) OR LOWER(note) LIKE ?)")
        params += [f"%{request.args['q'].lower()}%"] * 2
    if request.args.get("has_picks") == "1":
        clauses.append("id IN (SELECT transaction_id FROM transaction_items WHERE asset_type = 'pick')")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return jsonify(load_transactions(get_db(), where, params, limit=request.args.get("limit", 500)))


def get_txn(conn, txn_id):
    rows = load_transactions(conn, "WHERE id = ?", (txn_id,))
    return rows[0] if rows else None


def parse_date_ms(value):
    if value in (None, ""):
        return db.now_ms()
    if isinstance(value, (int, float)):
        return int(value)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def add_item(conn, txn, data, user):
    """Add a player or pick movement to a transaction. Returns (item_id, warning)."""
    asset = data.get("asset_type", "pick")
    from_id, to_id = int_or_none(data.get("from_team_id")), int_or_none(data.get("to_team_id"))
    if from_id is None and to_id is None:
        raise ValueError("Pick a team the asset moves from or to.")
    if from_id is not None and from_id == to_id:
        raise ValueError("From and to teams must differ.")
    warning = None
    if asset == "pick":
        pick_id = int_or_none(data.get("pick_id"))
        if pick_id is None or not conn.execute("SELECT 1 FROM draft_picks WHERE id = ?", (pick_id,)).fetchone():
            raise ValueError("Unknown draft pick.")
        if to_id is None:
            raise ValueError("A traded pick needs a receiving team.")
        if conn.execute("SELECT 1 FROM transaction_items WHERE transaction_id = ? AND pick_id = ?",
                        (txn["id"], pick_id)).fetchone():
            raise ValueError("That pick is already part of this transaction.")
        owner = db.pick_owner_before(conn, pick_id, txn["date_ms"], txn["id"])
        if from_id is not None and owner != from_id:
            name = conn.execute("SELECT name FROM teams WHERE id = ?", (owner,)).fetchone()
            warning = f"At that date the pick belonged to {name['name'] if name else 'another team'}."
        item_id = conn.insert("""
            INSERT INTO transaction_items (transaction_id, asset_type, pick_id, from_team_id, to_team_id, action, source, created_by)
            VALUES (?, 'pick', ?, ?, ?, 'PICK TRADED', 'manual', ?)""", (txn["id"], pick_id, from_id, to_id, user["id"]))
    elif asset == "player":
        name = (data.get("player_name") or "").strip()
        if not name:
            raise ValueError("Enter a player name.")
        item_id = conn.insert("""
            INSERT INTO transaction_items (transaction_id, asset_type, player_name, player_id, from_team_id, to_team_id, action, source, created_by)
            VALUES (?, 'player', ?, ?, ?, ?, 'MANUAL', 'manual', ?)""",
                              (txn["id"], name, int_or_none(data.get("player_id")), from_id, to_id, user["id"]))
    else:
        raise ValueError("asset_type must be 'pick' or 'player'.")
    return item_id, warning


@app.post("/api/transactions")
def api_create_transaction():
    conn = get_db()
    data = body()
    kind = (data.get("kind") or "TRADE").upper()
    if kind != "TRADE":
        return bad_request(ONLY_TRADES)
    try:
        date_ms = parse_date_ms(data.get("date"))
    except ValueError:
        return bad_request("Invalid date.")
    items = data.get("items") or []
    if not items:
        return bad_request("Add at least one player or pick.")
    season = int_or_none(data.get("season")) or datetime.fromtimestamp(date_ms / 1000, timezone.utc).year
    txn_id = conn.insert("""INSERT INTO transactions (season, date_ms, kind, source, note, created_at, created_by)
                            VALUES (?, ?, ?, 'manual', ?, ?, ?)""",
                         (season, date_ms, kind, data.get("note"), db.now_ms(), g.user["id"]))
    txn = {"id": txn_id, "date_ms": date_ms}
    warnings = []
    try:
        for it in items:
            _, w = add_item(conn, txn, it, g.user)
            if w:
                warnings.append(w)
    except ValueError as e:
        conn.rollback()
        return bad_request(str(e))
    saved = get_txn(conn, txn_id)
    teams = team_map(conn)
    db.audit(conn, g.user, "trade.create",
             f"Recorded a {KIND_WORD[kind]} dated {fmt_day(date_ms)}: " + "; ".join(item_text(i, teams) for i in saved["items"]),
             details=txn_snapshot(saved), transaction_id=txn_id)
    conn.commit()
    return jsonify({"transaction": get_txn(conn, txn_id), "warnings": warnings}), 201


@app.patch("/api/transactions/<int:txn_id>")
def api_update_transaction(txn_id):
    conn = get_db()
    txn = get_txn(conn, txn_id)
    if not txn:
        return bad_request("Not found", 404)
    if txn["kind"] != "TRADE":
        return bad_request(ONLY_TRADES)
    data = body()
    changes = {}
    if "note" in data and (data["note"] or None) != (txn["note"] or None):
        changes["note"] = data["note"] or None
    if txn["source"] == "manual":
        if data.get("date"):
            try:
                date_ms = parse_date_ms(data["date"])
            except ValueError:
                return bad_request("Invalid date.")
            if date_ms != txn["date_ms"]:
                changes["date_ms"] = date_ms
    if changes:
        for k, v in changes.items():
            conn.execute(f"UPDATE transactions SET {k} = ? WHERE id = ?", (v, txn_id))
        words = {"note": "the note", "date_ms": "the date"}
        what = " and ".join(words[k] for k in changes)
        db.audit(conn, g.user, "trade.update", f"Changed {what} on {txn_ref(txn)}",
                 details={"before": {k: txn[k] for k in changes}, "after": changes}, transaction_id=txn_id)
        conn.commit()
    return jsonify(get_txn(conn, txn_id))


@app.delete("/api/transactions/<int:txn_id>")
def api_delete_transaction(txn_id):
    conn = get_db()
    txn = get_txn(conn, txn_id)
    if not txn:
        return bad_request("Not found", 404)
    if txn["source"] != "manual":
        return bad_request("ESPN transactions can't be deleted; they would come back on the next sync.")
    teams = team_map(conn)
    conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    db.audit(conn, g.user, "trade.delete",
             f"Deleted {txn_ref(txn)}" + (f" recorded by {txn['created_by_name']}" if txn["created_by_name"] else "")
             + ": " + "; ".join(item_text(i, teams) for i in txn["items"]),
             details=txn_snapshot(txn), transaction_id=txn_id)
    conn.commit()
    return jsonify({"ok": True})


@app.post("/api/transactions/<int:txn_id>/items")
def api_add_item(txn_id):
    conn = get_db()
    txn = get_txn(conn, txn_id)
    if not txn:
        return bad_request("Not found", 404)
    if txn["kind"] != "TRADE":
        return bad_request(ONLY_TRADES)
    try:
        item_id, warning = add_item(conn, txn, body(), g.user)
    except ValueError as e:
        return bad_request(str(e))
    saved = get_txn(conn, txn_id)
    item = next(i for i in saved["items"] if i["id"] == item_id)
    db.audit(conn, g.user, "item.add", f"Added {item_text(item, team_map(conn))} to {txn_ref(txn)}",
             details={"item": item}, transaction_id=txn_id, pick_id=item["pick_id"])
    conn.commit()
    return jsonify({"transaction": saved, "warnings": [warning] if warning else []}), 201


@app.delete("/api/items/<int:item_id>")
def api_delete_item(item_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM transaction_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return bad_request("Not found", 404)
    if row["source"] != "manual":
        return bad_request("Only items you added by hand can be removed.")
    txn = get_txn(conn, row["transaction_id"])
    item = next(i for i in txn["items"] if i["id"] == item_id)
    conn.execute("DELETE FROM transaction_items WHERE id = ?", (item_id,))
    db.audit(conn, g.user, "item.remove",
             f"Removed {item_text(item, team_map(conn))} from {txn_ref(txn)}"
             + (f" (added by {item['created_by_name']})" if item["created_by_name"] else ""),
             details={"item": item}, transaction_id=txn["id"], pick_id=item["pick_id"])
    conn.commit()
    return jsonify(get_txn(conn, row["transaction_id"]))


@app.get("/api/export/transactions.csv")
def api_export_csv():
    conn = get_db()
    teams = team_map(conn)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["transaction_id", "date_utc", "season", "type", "source", "asset", "from_team", "to_team", "added_by", "note"])
    for t in load_transactions(conn):
        when = datetime.fromtimestamp(t["date_ms"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
        for it in t["items"]:
            w.writerow([t["id"], when, t["season"], t["kind"], it["source"], it["label"],
                        teams.get(it["from_team_id"], {}).get("name", ""),
                        teams.get(it["to_team_id"], {}).get("name", ""), it["created_by_name"] or "", t["note"] or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=transactions.csv"})


# ---------- draft picks ----------

@app.get("/api/picks")
def api_picks():
    conn = get_db()
    teams = team_map(conn)
    picks = load_picks(conn, teams)
    history = {}
    for r in conn.execute("""
        SELECT i.pick_id, i.from_team_id, i.to_team_id, t.id AS transaction_id, t.date_ms
        FROM transaction_items i JOIN transactions t ON t.id = i.transaction_id
        WHERE i.asset_type = 'pick' ORDER BY t.date_ms, t.id"""):
        history.setdefault(r["pick_id"], []).append(r)
    for p in picks:
        p["history"] = history.get(p["id"], [])
    return jsonify(picks)


def get_pick(conn, pick_id):
    return next((p for p in load_picks(conn) if p["id"] == pick_id), None)


@app.post("/api/picks/generate")
@commissioner_only
def api_generate_picks():
    """Create every team's own picks for a range of seasons (skips ones that exist)."""
    conn = get_db()
    data = body()
    try:
        start, end, rounds = int(data["start_season"]), int(data.get("end_season") or data["start_season"]), int(data["rounds"])
    except (KeyError, ValueError, TypeError):
        return bad_request("start_season, end_season and rounds are required numbers.")
    if end < start or not (1 <= rounds <= 40) or end - start > 20:
        return bad_request("Check the season range and number of rounds.")
    team_ids = [r["id"] for r in conn.execute("SELECT id FROM teams WHERE current = 1")]
    if not team_ids:
        return bad_request("No teams yet. Sync with ESPN first.")
    created = 0
    for season in range(start, end + 1):
        for rnd in range(1, rounds + 1):
            for tid in team_ids:
                cur = conn.execute("""INSERT INTO draft_picks (season, round, original_team_id) VALUES (?, ?, ?)
                                      ON CONFLICT DO NOTHING""", (season, rnd, tid))
                created += cur.rowcount
    if created:
        seasons = str(start) if start == end else f"{start}–{end}"
        db.audit(conn, g.user, "picks.generate", f"Created {created} draft picks for {seasons} ({rounds} rounds)")
    conn.commit()
    return jsonify({"created": created})


@app.post("/api/picks")
@commissioner_only
def api_create_pick():
    conn = get_db()
    data = body()
    try:
        season, rnd, tid = int(data["season"]), int(data["round"]), int(data["original_team_id"])
        slot = int_or_none(data.get("pick_in_round"))
        check_team(conn, tid)
    except (KeyError, ValueError, TypeError):
        return bad_request("season, round and original_team_id are required.")
    pick_id = conn.insert("""INSERT INTO draft_picks (season, round, original_team_id, pick_in_round, note)
                             VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                          (season, rnd, tid, slot, data.get("note")))
    if pick_id is None:
        return bad_request("That pick already exists.")
    db.audit(conn, g.user, "pick.create", f"Created pick {get_pick(conn, pick_id)['label']}", pick_id=pick_id)
    conn.commit()
    return jsonify({"id": pick_id}), 201


@app.patch("/api/picks/<int:pick_id>")
def api_update_pick(pick_id):
    conn = get_db()
    pick = get_pick(conn, pick_id)
    if not pick:
        return bad_request("Not found", 404)
    data = body()
    fields = {k: data[k] for k in ("pick_in_round", "used_on", "note") if k in data}
    try:
        if "pick_in_round" in fields:
            fields["pick_in_round"] = int_or_none(fields["pick_in_round"])
    except ValueError:
        return bad_request("Pick number must be a number.")
    changes = {k: (v or None) for k, v in fields.items() if (v or None) != pick[k]}
    if changes:
        for k, v in changes.items():
            conn.execute(f"UPDATE draft_picks SET {k} = ? WHERE id = ?", (v, pick_id))
        words = {"pick_in_round": "pick number", "used_on": "used on", "note": "note"}
        what = "; ".join(f"{words[k]} {pick[k] or '(blank)'} → {v or '(blank)'}" for k, v in changes.items())
        db.audit(conn, g.user, "pick.update", f"Edited {pick['label']}: {what}",
                 details={"before": {k: pick[k] for k in changes}, "after": changes}, pick_id=pick_id)
        conn.commit()
    return jsonify({"ok": True})


@app.delete("/api/picks/<int:pick_id>")
def api_delete_pick(pick_id):
    conn = get_db()
    pick = get_pick(conn, pick_id)
    if not pick:
        return bad_request("Not found", 404)
    trades = conn.scalar("SELECT COUNT(*) AS n FROM transaction_items WHERE pick_id = ?", (pick_id,))
    conn.execute("DELETE FROM draft_picks WHERE id = ?", (pick_id,))
    db.audit(conn, g.user, "pick.delete",
             f"Deleted pick {pick['label']}" + (f", removing it from {trades} trade{'s' if trades != 1 else ''}" if trades else ""),
             details={k: pick[k] for k in ("season", "round", "original_team_id", "owner_team_id", "pick_in_round", "used_on", "note")},
             pick_id=pick_id)
    conn.commit()
    return jsonify({"ok": True})


# ---------- sync ----------

@app.get("/api/sync")
def api_sync_status():
    conn = get_db()
    runs = list(conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 10"))
    seasons = [r["season"] for r in conn.execute("SELECT DISTINCT season FROM transactions WHERE season IS NOT NULL ORDER BY season DESC")]
    return jsonify({
        "configured": bool(os.environ.get("ESPN_LEAGUE_ID")),
        "league_id": os.environ.get("ESPN_LEAGUE_ID"),
        "year": current_season(),
        "draft_season": next_draft_season(),
        "auto_sync_hours": auto_sync_hours(),
        "next_auto_sync": max(next_auto_sync_ms(conn), db.now_ms()) if auto_sync_hours() > 0 else None,
        "runs": runs,
        "seasons": seasons,
        "counts": {
            "teams": conn.scalar("SELECT COUNT(*) AS n FROM teams"),
            "transactions": conn.scalar("SELECT COUNT(*) AS n FROM transactions"),
            "picks": conn.scalar("SELECT COUNT(*) AS n FROM draft_picks"),
        },
    })


@app.post("/api/sync")
@commissioner_only
def api_sync():
    if not os.environ.get("ESPN_LEAGUE_ID"):
        return bad_request("ESPN_LEAGUE_ID is not set. Copy .env.example to .env and fill it in.")
    if not sync_lock.acquire(blocking=False):
        return bad_request("A sync is already running.", 409)
    try:
        data = body()
        if data.get("all_past"):
            result = import_past_seasons(get_db())
        else:
            result = espn_sync.run_sync(get_db(), year=int_or_none(data.get("year")), full=bool(data.get("full")), user=g.user)
    finally:
        sync_lock.release()
    return jsonify(result), (200 if result["ok"] else 502)


def import_past_seasons(conn):
    """Import every earlier season ESPN still has, newest first, stopping at the first it doesn't."""
    done, lines = [], []
    for year in range(current_season() - 1, current_season() - 40, -1):
        result = espn_sync.run_sync(conn, year=year, user=g.user, missing_ok=True)
        if result.get("missing"):
            break
        if not result["ok"]:
            return {"ok": False, "message": f"{year}: {result['message']}", "seasons": done}
        done.append(year)
        lines.append(f"{year}: {result['message']}")
    if not done:
        return {"ok": False, "message": "ESPN has no earlier seasons for this league.", "seasons": []}
    return {"ok": True, "message": f"Imported {done[-1]}–{done[0]}. " + " · ".join(lines), "seasons": done}


# ---------- recovering trades with a member's ESPN login ----------

SWID_PATTERN = re.compile(r"^\{?([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\}?$")


def trades_missing_contents(conn):
    """Imported past trades nobody has filled in yet: nothing on them but drops."""
    return list(conn.execute("""
        SELECT id, season, accepted_by FROM transactions t
        WHERE espn_key LIKE 'log:trade:%'
          AND NOT EXISTS (SELECT 1 FROM transaction_items i WHERE i.transaction_id = t.id AND i.action <> 'DROPPED')"""))


@app.get("/api/recover-trades")
def api_recover_status():
    missing = trades_missing_contents(get_db())
    team = g.user["team_id"]
    mine = [t for t in missing if team is not None and team in json.loads(t["accepted_by"] or "[]")]
    return jsonify({"missing": len(missing), "accepted_by_my_team": len(mine), "team_id": team,
                    "seasons": sorted({t["season"] for t in missing})})


@app.post("/api/recover-trades")
def api_recover_trades():
    """Fill in past trades from what ESPN shows this member. Their cookies are used for this request only."""
    data = body()
    swid_match = SWID_PATTERN.match((data.get("swid") or "").strip())
    espn_s2 = (data.get("espn_s2") or "").strip()
    if not swid_match or not espn_s2 or len(espn_s2) > 2000 or any(c.isspace() for c in espn_s2):
        return bad_request("Paste both cookies exactly as they appear: SWID looks like {XXXXXXXX-XXXX-...}, and espn_s2 is one long line.")
    if not os.environ.get("ESPN_LEAGUE_ID"):
        return bad_request("ESPN_LEAGUE_ID is not set.")
    swid = "{" + swid_match.group(1).upper() + "}"
    conn = get_db()
    seasons = [r["season"] for r in conn.execute(
        "SELECT DISTINCT season FROM transactions WHERE espn_key LIKE 'log:trade:%' ORDER BY season")]
    if not seasons:
        return bad_request("No past seasons have been imported yet. Ask the commissioner to run Import all past seasons.")
    if not sync_lock.acquire(blocking=False):
        return bad_request("A sync is running. Try again in a minute.", 409)
    try:
        recovered = []
        for season in seasons:
            recovered += espn_log.recover_trades(conn, espn_log.member_request(season, swid, espn_s2))
    except Exception as e:
        conn.rollback()
        if type(e).__name__ == "ESPNAccessDenied":
            return bad_request("ESPN didn't accept those cookies, or that ESPN account isn't in this league. "
                               "Copy them again while signed in to espn.com.")
        return bad_request(f"ESPN couldn't be reached ({type(e).__name__}). Try again later.", 502)
    finally:
        sync_lock.release()
        swid = espn_s2 = None  # nothing keeps the cookies past this request

    teams = team_map(conn)
    for txn_id, added in recovered:
        txn = get_txn(conn, txn_id)
        moved = "; ".join(item_text(dict(i, label=i["player_name"]), teams) for i in added)
        db.audit(conn, g.user, "trade.recover",
                 f"Filled in {txn_ref(txn)} from ESPN" + (f": {moved}" if moved else " (who offered it)"),
                 details={"items": added}, transaction_id=txn_id)
    conn.commit()
    n = len(recovered)
    seasons = sorted({get_txn(conn, t)["season"] for t, _ in recovered})
    span = "" if not seasons else f" from {seasons[0]}" if len(seasons) == 1 else f" from {seasons[0]}–{seasons[-1]}"
    message = (f"Success! Your ESPN login worked and filled in {n} trade{'s' if n != 1 else ''}{span}." if n
               else "Success! Your ESPN login worked. Every past trade ESPN shows your account was already filled in, "
                    "so there was nothing new to add.")
    return jsonify({"ok": True, "recovered": n, "transaction_ids": [t for t, _ in recovered], "seasons": seasons,
                    "missing": len(trades_missing_contents(conn)), "message": message})


def auto_sync_hours():
    try:
        return float(os.environ.get("AUTO_SYNC_HOURS") or 0)
    except ValueError:
        return 0


_auto_sync_started = False
AUTO_SYNC_CHECK_S = 15 * 60       # how often the background thread looks at the schedule
AUTO_SYNC_RETRY_MS = 3600 * 1000  # after a failed automatic sync (e.g. expired cookies), wait an hour


def next_auto_sync_ms(conn):
    """When the next automatic sync is due, or None if they're off.

    Counted from the last successful sync of the current season, manual or
    automatic, and kept in the database so restarts and redeploys don't reset it.
    """
    hours = auto_sync_hours()
    if hours <= 0:
        return None
    last_ok = conn.scalar("SELECT MAX(finished_at) AS t FROM sync_runs WHERE status = 'ok' AND season = ?",
                          (current_season(),)) or 0
    last_try = conn.scalar("SELECT MAX(started_at) AS t FROM sync_runs WHERE actor_name = 'Automatic sync'") or 0
    return max(last_ok + int(hours * 3600 * 1000), last_try + AUTO_SYNC_RETRY_MS)


def start_auto_sync():
    """Pull new ESPN activity every AUTO_SYNC_HOURS in a background thread."""
    global _auto_sync_started
    if _auto_sync_started or auto_sync_hours() <= 0 or not os.environ.get("ESPN_LEAGUE_ID"):
        return
    _auto_sync_started = True

    def loop():
        time.sleep(60)  # let the app finish starting before an overdue sync
        while True:
            try:
                conn = db.connect()
                try:
                    if db.now_ms() >= next_auto_sync_ms(conn) and sync_lock.acquire(blocking=False):
                        try:
                            espn_sync.run_sync(conn, actor_name="Automatic sync")
                        finally:
                            sync_lock.release()
                finally:
                    conn.close()
            except Exception as e:  # keep the loop alive if the database is briefly unreachable
                print(f"Automatic sync failed: {e}", file=sys.stderr)
            time.sleep(AUTO_SYNC_CHECK_S)

    threading.Thread(target=loop, name="auto-sync", daemon=True).start()


# ---------- startup ----------

def configure():
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        if db.using_postgres():
            raise RuntimeError("Set SECRET_KEY before running with a hosted database.")
        print("SECRET_KEY is not set; using a random one, so everyone is signed out on restart.", file=sys.stderr)
        secret = secrets.token_hex(32)
    app.secret_key = secret
    hosted = any(k in os.environ for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME"))
    if hosted and not db.using_postgres():
        # Railway's disk is wiped on every deploy, so a SQLite file there would lose everything
        url = db.database_url()
        got = ("is empty" if not url else f"is the unresolved text {url!r}" if "${{" in url
               else f"starts with {url.split(':')[0][:20]!r} ({len(url)} characters)")  # never the password
        names = sorted(k for k in os.environ if "DATABASE" in k or k.startswith("PG"))
        raise RuntimeError(f"DATABASE_URL {got}, not a postgresql:// URL. Database variables this service has: "
                           f"{', '.join(names) or 'none'}. On the app service's Variables, set DATABASE_URL to "
                           "${{Postgres.DATABASE_URL}}, using the Postgres service's exact name.")
    print(f"Using {'Postgres' if db.using_postgres() else 'the SQLite file ' + db.db_path()}.", file=sys.stderr)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES", "1" if hosted else "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )


def bootstrap_commissioner(conn):
    """On a fresh database, create the first commissioner from COMMISSIONER_USERNAME/PASSWORD."""
    username = os.environ.get("COMMISSIONER_USERNAME")
    if not username or conn.scalar("SELECT COUNT(*) AS n FROM users"):
        return
    given = os.environ.get("COMMISSIONER_PASSWORD") or None
    _, password = auth.create_user(conn, username, os.environ.get("COMMISSIONER_NAME") or username,
                                   password=given, is_commissioner=True)
    db.audit(conn, None, "user.create", f"Created the commissioner account {auth.normalize_username(username)}")
    conn.commit()
    shown = "the COMMISSIONER_PASSWORD you set" if given else f"temporary password {password}"
    print(f"Created commissioner account '{username}'. Sign in with {shown}, then choose a new password.",
          file=sys.stderr)


def create_app():
    configure()
    conn = db.connect()
    db.init_db(conn)
    bootstrap_commissioner(conn)
    conn.close()
    start_auto_sync()
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
