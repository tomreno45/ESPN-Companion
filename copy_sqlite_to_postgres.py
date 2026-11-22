"""Copy an existing dynasty.db (SQLite) into the Postgres database in DATABASE_URL.

    DATABASE_URL=postgresql://... python copy_sqlite_to_postgres.py dynasty.db

Run it once, against an empty Postgres database (before anyone starts editing
the hosted copy). It refuses to run if the target already has transactions or
draft picks, so it can't overwrite live data.
"""
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db

# Parents before children so foreign keys are satisfied.
TABLES = ["teams", "players", "roster_entries", "users", "transactions", "draft_picks",
          "transaction_items", "sync_runs", "audit_log", "team_seasons", "lineups"]
ID_TABLES = ["users", "transactions", "draft_picks", "transaction_items", "sync_runs", "audit_log"]


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    if not db.using_postgres():
        raise SystemExit("Set DATABASE_URL to the Postgres database to copy into.")
    src = db.connect(sys.argv[1])  # only read from; older files may lack newer tables and columns
    dst = db.connect()
    db.init_db(dst)
    if dst.scalar("SELECT COUNT(*) AS n FROM transactions") or dst.scalar("SELECT COUNT(*) AS n FROM draft_picks"):
        raise SystemExit("The Postgres database already has data; not copying over it.")

    for table in TABLES:
        cols = sorted(db.columns(src, table) & db.columns(dst, table))
        if not cols:
            print(f"{table}: not in the old file, skipped")
            continue
        rows = list(src.execute(f"SELECT {', '.join(cols)} FROM {table}"))
        if table == "users" and rows:
            # a commissioner created from COMMISSIONER_USERNAME on first start would clash
            dst.execute("DELETE FROM audit_log")
            dst.execute("DELETE FROM users")
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
        with dst.raw.cursor() as cur:  # one batch per table: row by row is slow over the internet
            cur.executemany(sql, [[r[c] for c in cols] for r in rows])
        print(f"{table}: {len(rows)} rows")

    for table in ID_TABLES:  # continue numbering after the copied ids
        dst.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
    dst.commit()
    print("Done.")


if __name__ == "__main__":
    main()
