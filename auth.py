"""League member accounts: password hashing, sessions and sign-in throttling.

The commissioner creates every account with a temporary password; members
must choose their own password the first time they sign in.
"""
import secrets
import threading
import time

from flask import session
from werkzeug.security import check_password_hash, generate_password_hash

from db import now_ms

MIN_PASSWORD_LENGTH = 8

# Sign-in throttling: after this many failures in the window, wait it out.
MAX_FAILURES = 8
FAILURE_WINDOW_S = 15 * 60
_failures = {}
_failures_lock = threading.Lock()

USER_FIELDS = ("id", "username", "display_name", "team_id", "is_commissioner", "active",
               "must_change_password", "created_at", "last_login_at")


def public_user(u):
    out = {k: u[k] for k in USER_FIELDS}
    for k in ("is_commissioner", "active", "must_change_password"):
        out[k] = bool(out[k])
    return out


def normalize_username(name):
    return (name or "").strip().lower()


TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to read out or text


def temp_password():
    return "-".join("".join(secrets.choice(TEMP_ALPHABET) for _ in range(4)) for _ in range(3))


def password_problem(password, username=""):
    """Return a reason the password is unacceptable, or None."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if normalize_username(password) == normalize_username(username):
        return "Your password can't be your username."
    return None


def create_user(conn, username, display_name, password=None, team_id=None, is_commissioner=False):
    """Create an account. Returns (user_id, password); the password is a generated temporary one if not given."""
    username = normalize_username(username)
    if not username or not all(c.isalnum() or c in "._-" for c in username):
        raise ValueError("Usernames can use letters, numbers, dots, dashes and underscores.")
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise ValueError("That username is taken.")
    password = password or temp_password()
    problem = password_problem(password, username)
    if problem:
        raise ValueError(problem)
    uid = conn.insert("""
        INSERT INTO users (username, display_name, password_hash, team_id, is_commissioner,
                           must_change_password, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)""",
                      (username, (display_name or "").strip() or username, generate_password_hash(password),
                       team_id, 1 if is_commissioner else 0, now_ms()))
    return uid, password


def set_password(conn, user_id, password, must_change):
    """Change a password and sign the account out of every other session."""
    conn.execute("""UPDATE users SET password_hash = ?, must_change_password = ?, auth_version = auth_version + 1
                    WHERE id = ?""", (generate_password_hash(password), 1 if must_change else 0, user_id))


def _too_many_failures(key):
    cutoff = time.time() - FAILURE_WINDOW_S
    with _failures_lock:
        recent = [t for t in _failures.get(key, []) if t > cutoff]
        _failures[key] = recent
        return len(recent) >= MAX_FAILURES


def _record_failure(key):
    with _failures_lock:
        _failures.setdefault(key, []).append(time.time())


def reset_throttle():
    with _failures_lock:
        _failures.clear()


def check_login(conn, username, password, ip):
    """Return (user, error). Failures are throttled per username and per address."""
    username = normalize_username(username)
    keys = [("user", username), ("ip", ip)]
    if any(_too_many_failures(k) for k in keys):
        return None, "Too many failed sign-ins. Try again in 15 minutes."
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password or ""):
        for k in keys:
            _record_failure(k)
        return None, "Wrong username or password."
    if not user["active"]:
        return None, "This account has been turned off. Ask the commissioner."
    return user, None


def start_session(user):
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["av"] = user["auth_version"]


def session_user(conn):
    """The signed-in user, or None. Turned-off accounts and old sessions are rejected."""
    uid = session.get("uid")
    if uid is None:
        return None
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not user or not user["active"] or user["auth_version"] != session.get("av"):
        session.clear()
        return None
    return user
