"""Storage for teams, rosters, transactions, draft picks, members and the activity log.

Runs on SQLite locally and on Postgres when DATABASE_URL is set (hosting).
All SQL is written once with ? placeholders and sticks to syntax both support.

ESPN data is re-synced into these tables, but anything entered by hand
(draft picks, pick movements added to trades, manual transactions) is stored
with source='manual' and is never touched by a sync.
"""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id          INTEGER PRIMARY KEY,          -- ESPN team id
    name        TEXT NOT NULL,
    abbrev      TEXT,
    owners      TEXT,
    logo_url    TEXT,
    updated_at  BIGINT,
    wins        INTEGER,                      -- current season record, for projected draft order
    losses      INTEGER,
    ties        INTEGER,
    points_for  REAL,
    standing    INTEGER,                      -- ESPN's current rank, 1 = first place
    current     INTEGER NOT NULL DEFAULT 1    -- 0 = only in past seasons
);

-- Team names change from season to season; old transactions show the name of the time.
CREATE TABLE IF NOT EXISTS team_seasons (
    season      INTEGER NOT NULL,
    team_id     INTEGER NOT NULL REFERENCES teams(id),
    name        TEXT NOT NULL,
    abbrev      TEXT,
    PRIMARY KEY (season, team_id)
);

CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY,          -- ESPN player id
    name        TEXT NOT NULL,
    position    TEXT,
    pro_team    TEXT
);

CREATE TABLE IF NOT EXISTS roster_entries (
    team_id          INTEGER NOT NULL REFERENCES teams(id),
    player_id        INTEGER NOT NULL REFERENCES players(id),
    lineup_slot      TEXT,
    acquisition_type TEXT,
    PRIMARY KEY (team_id, player_id)
);

CREATE TABLE IF NOT EXISTS users (
    id                    {id},
    username              TEXT NOT NULL UNIQUE, -- stored lowercase
    display_name          TEXT NOT NULL,
    password_hash         TEXT NOT NULL,
    team_id               INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    is_commissioner       INTEGER NOT NULL DEFAULT 0,
    active                INTEGER NOT NULL DEFAULT 1,
    must_change_password  INTEGER NOT NULL DEFAULT 1,
    auth_version          INTEGER NOT NULL DEFAULT 1, -- bumped to sign a user out everywhere
    created_at            BIGINT,
    last_login_at         BIGINT
);

CREATE TABLE IF NOT EXISTS transactions (
    id          {id},
    espn_key    TEXT UNIQUE,                  -- NULL for manual transactions
    season      INTEGER,
    date_ms     BIGINT NOT NULL,
    kind        TEXT NOT NULL,                -- TRADE, WAIVER, ADD, DROP, OTHER
    source      TEXT NOT NULL DEFAULT 'espn', -- espn | manual
    note        TEXT,
    espn_note   TEXT,                         -- what ESPN's log does and doesn't say about an imported trade
    accepted_by TEXT,                         -- JSON list of team ids that accepted an imported trade
    offered_by  INTEGER,                      -- team that proposed it, when ESPN shows the proposal
    created_at  BIGINT,
    created_by  INTEGER                       -- users.id, for manual transactions
);

CREATE TABLE IF NOT EXISTS draft_picks (
    id                {id},
    season            INTEGER NOT NULL,
    round             INTEGER NOT NULL,
    original_team_id  INTEGER NOT NULL REFERENCES teams(id),
    pick_in_round     INTEGER,
    used_on           TEXT,
    note              TEXT,
    UNIQUE (season, round, original_team_id)
);

-- One row per asset that moved: a player or a draft pick.
-- from_team_id NULL = came from free agency/waivers; to_team_id NULL = released.
CREATE TABLE IF NOT EXISTS transaction_items (
    id              {id},
    transaction_id  INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    asset_type      TEXT NOT NULL,            -- player | pick
    player_id       INTEGER,
    player_name     TEXT,
    pick_id         INTEGER REFERENCES draft_picks(id) ON DELETE CASCADE,
    from_team_id    INTEGER REFERENCES teams(id),
    to_team_id      INTEGER REFERENCES teams(id),
    action          TEXT,                     -- raw ESPN action, e.g. WAIVER ADDED
    bid_amount      INTEGER DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'espn',
    created_by      INTEGER                   -- users.id, for manual items
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          {id},
    started_at  BIGINT,
    finished_at BIGINT,
    season      INTEGER,
    status      TEXT,
    message     TEXT,
    user_id     INTEGER,
    actor_name  TEXT
);

-- Who changed what. No foreign keys: entries outlive the things they describe,
-- and summary/details keep a copy of anything that was deleted.
CREATE TABLE IF NOT EXISTS audit_log (
    id              {id},
    at              BIGINT NOT NULL,
    user_id         INTEGER,
    actor_name      TEXT NOT NULL,
    action          TEXT NOT NULL,            -- e.g. trade.create, item.remove, pick.update
    summary         TEXT NOT NULL,
    details         TEXT,                     -- JSON
    transaction_id  INTEGER,
    pick_id         INTEGER
);

-- Each week's lineup: the slot every rostered player was in and what he scored (2019 on).
CREATE TABLE IF NOT EXISTS lineups (
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    player_name TEXT,
    position    TEXT,
    pro_team    TEXT,
    slot        TEXT,                         -- QB, RB, WR, TE, RB/WR/TE, OP, D/ST, K, BE, IR
    points      REAL,
    projected   REAL,
    updated_at  BIGINT,
    PRIMARY KEY (season, week, team_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_items_txn  ON transaction_items(transaction_id);
CREATE INDEX IF NOT EXISTS idx_lineups_player ON lineups(player_id);
CREATE INDEX IF NOT EXISTS idx_items_pick ON transaction_items(pick_id);
CREATE INDEX IF NOT EXISTS idx_txn_date   ON transactions(date_ms);
CREATE INDEX IF NOT EXISTS idx_audit_at   ON audit_log(at);
CREATE INDEX IF NOT EXISTS idx_audit_txn  ON audit_log(transaction_id);
CREATE INDEX IF NOT EXISTS idx_audit_pick ON audit_log(pick_id);
"""

ID_COLUMN = {
    "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "postgres": "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
}

# Columns added after the first release; older databases get them on startup.
ADDED_COLUMNS = [
    ("teams", "wins", "INTEGER"),
    ("teams", "losses", "INTEGER"),
    ("teams", "ties", "INTEGER"),
    ("teams", "points_for", "REAL"),
    ("teams", "standing", "INTEGER"),
    ("teams", "current", "INTEGER NOT NULL DEFAULT 1"),
    ("transactions", "espn_note", "TEXT"),
    ("transactions", "accepted_by", "TEXT"),
    ("transactions", "offered_by", "INTEGER"),
    ("transactions", "created_by", "INTEGER"),
    ("transaction_items", "created_by", "INTEGER"),
    ("sync_runs", "user_id", "INTEGER"),
    ("sync_runs", "actor_name", "TEXT"),
]


def db_path():
    return os.environ.get("DATABASE_PATH", "dynasty.db")


def database_url():
    return os.environ.get("DATABASE_URL", "")


def using_postgres():
    return database_url().startswith(("postgres://", "postgresql://"))


class Connection:
    """Thin wrapper so the same SQL runs on sqlite3 and psycopg. Rows are dicts."""

    def __init__(self, raw, kind):
        self.raw = raw
        self.kind = kind

    def execute(self, sql, params=()):
        if self.kind == "postgres":
            sql = sql.replace("%", "%%").replace("?", "%s")  # a literal % (as in LIKE 'x%') isn't a placeholder
        return self.raw.execute(sql, tuple(params))

    def insert(self, sql, params=()):
        """Run an INSERT and return the new row's id (None if nothing was inserted)."""
        rows = self.execute(sql + " RETURNING id", params).fetchall()
        return rows[0]["id"] if rows else None

    def scalar(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        return next(iter(row.values())) if row else None

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def _dict_row(cursor, row):
    return {d[0]: v for d, v in zip(cursor.description, row)}


def connect(path=None):
    if path is None and using_postgres():
        import psycopg
        from psycopg.rows import dict_row
        return Connection(psycopg.connect(database_url(), row_factory=dict_row), "postgres")
    raw = sqlite3.connect(path or db_path())
    raw.row_factory = _dict_row
    raw.execute("PRAGMA foreign_keys = ON")
    return Connection(raw, "sqlite")


def columns(conn, table):
    if conn.kind == "sqlite":
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    return {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ?",
        (table,))}


def init_db(conn):
    schema = "\n".join(line.split("--")[0] for line in SCHEMA.splitlines())  # comments may contain ';'
    for stmt in schema.replace("{id}", ID_COLUMN[conn.kind]).split(";"):
        if stmt.strip():
            conn.execute(stmt)
    for table, col, decl in ADDED_COLUMNS:
        if col not in columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def now_ms():
    return int(time.time() * 1000)


def audit(conn, user, action, summary, details=None, transaction_id=None, pick_id=None):
    """Record a change in the activity log. Call before the caller's commit."""
    conn.execute("""
        INSERT INTO audit_log (at, user_id, actor_name, action, summary, details, transaction_id, pick_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (now_ms(), user["id"] if user else None, user["display_name"] if user else "System",
                  action, summary, json.dumps(details) if details is not None else None,
                  transaction_id, pick_id))


def pick_owners(conn):
    """Return {pick_id: owner_team_id}.

    A pick belongs to its original team until it shows up in a transaction;
    after that it belongs to whoever received it most recently.
    """
    owners = {r["id"]: r["original_team_id"]
              for r in conn.execute("SELECT id, original_team_id FROM draft_picks")}
    rows = conn.execute("""
        SELECT i.pick_id, i.to_team_id
        FROM transaction_items i JOIN transactions t ON t.id = i.transaction_id
        WHERE i.asset_type = 'pick' AND i.pick_id IS NOT NULL
        ORDER BY t.date_ms, t.id, i.id
    """)
    for r in rows:
        if r["to_team_id"] is not None:
            owners[r["pick_id"]] = r["to_team_id"]
    return owners


def pick_owner_before(conn, pick_id, date_ms, exclude_txn_id=None):
    """Who owned a pick just before a given moment (for validating trades)."""
    row = conn.execute("""
        SELECT i.to_team_id FROM transaction_items i
        JOIN transactions t ON t.id = i.transaction_id
        WHERE i.pick_id = ? AND t.date_ms < ? AND t.id <> ?
          AND i.to_team_id IS NOT NULL
        ORDER BY t.date_ms DESC, t.id DESC, i.id DESC LIMIT 1
    """, (pick_id, date_ms, -1 if exclude_txn_id is None else exclude_txn_id)).fetchone()
    if row:
        return row["to_team_id"]
    pick = conn.execute("SELECT original_team_id FROM draft_picks WHERE id = ?",
                        (pick_id,)).fetchone()
    return pick["original_team_id"] if pick else None
