"""Every player the league has seen, for matching names typed in by members.

Rosters only hold today's players, so past ones come from transactions and weekly lineups.
"""
import re
import unicodedata

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(name):
    """'Kyle Pitts Sr.' and 'kyle pitts' compare equal; so do De'Von and Devon."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9 ]+", "", s.replace("-", " ")).split()
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words)


def directory(conn):
    """{player_id: {id, name, position, pro_team, team_id}}; team_id is his team today, if any."""
    out = {}
    for r in conn.execute("""SELECT player_id AS id, player_name AS name, position, pro_team FROM lineups
                             WHERE player_id IS NOT NULL ORDER BY season, week"""):
        out[r["id"]] = {**r, "team_id": None}  # later weeks overwrite: his latest name and team
    for r in conn.execute("""SELECT DISTINCT player_id AS id, player_name AS name FROM transaction_items
                             WHERE asset_type = 'player' AND player_id IS NOT NULL AND player_name IS NOT NULL"""):
        out.setdefault(r["id"], {**r, "position": None, "pro_team": None, "team_id": None})
    for r in conn.execute("SELECT id, name, position, pro_team FROM players"):
        out[r["id"]] = {**out.get(r["id"], {}), **{k: v for k, v in r.items() if v is not None}, "team_id": None}
    for r in conn.execute("SELECT team_id, player_id FROM roster_entries"):
        if r["player_id"] in out:
            out[r["player_id"]]["team_id"] = r["team_id"]
    return out


def find(players, name):
    """The players whose name matches, ignoring case, punctuation and suffixes."""
    key = norm(name)
    return [p for p in players.values() if key and norm(p["name"]) == key]


def search(players, q, limit=20):
    """Names containing every word typed: whole-word matches first, then words starting with it, then rostered players."""
    words = norm(q).split()
    if not words:
        return []
    hits = [p for p in players.values() if all(w in norm(p["name"]) for w in words)]

    def rank(p):
        parts = norm(p["name"]).split()
        fit = 0 if words[-1] in parts else 1 if any(x.startswith(words[-1]) for x in parts) else 2
        return fit, p["team_id"] is None, p["name"]
    hits.sort(key=rank)
    return hits[:limit]
