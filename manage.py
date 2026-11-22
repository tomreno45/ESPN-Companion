"""Account admin from the command line, for when you can't sign in to the app.

    python manage.py create-user USERNAME "Display Name" [--commissioner] [--team TEAM_ID]
    python manage.py reset-password USERNAME
    python manage.py list-users

Uses the same database settings as the app (DATABASE_URL or DATABASE_PATH from .env).
"""
import argparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import auth
import db


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user")
    create.add_argument("username")
    create.add_argument("display_name")
    create.add_argument("--commissioner", action="store_true")
    create.add_argument("--team", type=int, help="ESPN team id this member manages")
    reset = sub.add_parser("reset-password")
    reset.add_argument("username")
    sub.add_parser("list-users")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    if args.command == "create-user":
        uid, password = auth.create_user(conn, args.username, args.display_name,
                                         team_id=args.team, is_commissioner=args.commissioner)
        user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        db.audit(conn, None, "user.create", f"Created an account for {user['display_name']} ({user['username']}) from the command line")
        conn.commit()
        print(f"Created {user['username']}. Temporary password: {password}")
    elif args.command == "reset-password":
        user = conn.execute("SELECT * FROM users WHERE username = ?", (auth.normalize_username(args.username),)).fetchone()
        if not user:
            raise SystemExit(f"No account named {args.username}.")
        password = auth.temp_password()
        auth.set_password(conn, user["id"], password, must_change=True)
        db.audit(conn, None, "user.reset_password", f"Reset {user['display_name']}'s password from the command line")
        conn.commit()
        print(f"New temporary password for {user['username']}: {password}")
    else:
        for u in conn.execute("SELECT * FROM users ORDER BY username"):
            flags = [f for f, on in (("commissioner", u["is_commissioner"]), ("off", not u["active"]),
                                     ("must change password", u["must_change_password"])) if on]
            print(f"{u['username']:<20} {u['display_name']:<24} {', '.join(flags)}")
    conn.close()


if __name__ == "__main__":
    main()
