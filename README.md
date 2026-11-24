# Dynasty Companion

A web app for an ESPN fantasy football dynasty league. It pulls rosters and
the full transaction history from ESPN, tracks every team's draft picks, and
lets league members attach draft picks to trades (ESPN doesn't know about picks
in dynasty deals). Everyone signs in, anyone can edit, and every change is
recorded with who made it.

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # then fill in the ESPN cookies, SECRET_KEY and COMMISSIONER_*
python app.py
```

Open http://127.0.0.1:5000 and sign in as the commissioner. Locally the data is
kept in a SQLite file, `dynasty.db`; set `DATABASE_URL` to use Postgres instead.

`ESPN_SWID` and `ESPN_S2` are the `SWID` and `espn_s2` cookies from espn.com
(browser dev tools, Application, Cookies, while logged in). They expire now and
then; when syncs start failing with an authorization error, copy fresh ones in.

## Host it on Railway

1. Put this folder in a GitHub repository (`.env` and `*.db` are gitignored, so
   your cookies and data stay out of it).
2. In Railway, create a project, add **PostgreSQL**, then add a service from the
   GitHub repo. `railway.json` sets the start command and health check.
3. On the app service, under **Variables**, add:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (a reference to the database)
   - `SECRET_KEY` = a long random string (`python -c "import secrets; print(secrets.token_hex(32))"`)
   - `ESPN_LEAGUE_ID`, `ESPN_YEAR`, `ESPN_SWID`, `ESPN_S2`
   - `COMMISSIONER_USERNAME`, `COMMISSIONER_NAME`, `COMMISSIONER_PASSWORD` (temporary)
   - `AUTO_SYNC_HOURS` = `24` to pull new ESPN activity once a day (counted from the last
     sync, so redeploys don't reset it; a failed one is retried an hour later)
4. Under **Settings > Networking**, generate a domain, open it and sign in.
   You'll be asked to choose a new password straight away.
5. To bring over the data from an existing `dynasty.db`, do it before anyone
   starts editing: copy the Postgres **public** connection URL from Railway and run
   ```bash
   DATABASE_URL="postgresql://..." python copy_sqlite_to_postgres.py dynasty.db
   ```
   It refuses to run if the hosted database already has transactions or picks.
   It also copies accounts if the old file had any; otherwise keep the
   commissioner created on first start.

Railway's Postgres has backups you can turn on in the database's settings; do,
since it holds the only copy of the draft pick history.

## Accounts

- The commissioner creates every account on the **Members** tab. The app shows a
  temporary password once; send it to the member, who must choose their own
  password the first time they sign in.
- **Reset password** gives a member a new temporary password and signs them out
  everywhere. **Edit** sets their name, team and commissioner access, or turns
  the account off (they can't sign in, but their name stays on everything they did).
- Members can't manage accounts; everything else is open to everyone signed in.
- Locked out? `python manage.py reset-password USERNAME` (with `DATABASE_URL` set
  to the hosted database) prints a new temporary password.
- After 8 wrong passwords in 15 minutes, sign-in for that username pauses for 15 minutes.

## Using it

1. **Sync** tab (commissioner only): "Sync new activity" pulls rosters, standings
   and any transactions since the last sync. "Pull full history" walks ESPN's
   whole activity feed for the season. With `AUTO_SYNC_HOURS` set, new activity is
   also pulled on a timer.
   ESPN keeps that feed only for the current season, so **"Import all past
   seasons"** reads ESPN's transaction log instead (`espn_log.py`), back to the
   league's first season (2018 here). It brings in every add, drop and waiver
   claim. Trades are partial: ESPN only shows a trade's contents to managers whose
   team was in it, so many past trades arrive as "Accepted by ..." placeholders for
   members to fill in with **Edit**. Running it again only adds what's missing.
6. **Recover my trades** tab: a member shares their own ESPN `SWID` and `espn_s2`
   cookies and the app fills in every past trade their team was in, since ESPN
   shows those to them. The easy way is the **Send ESPN login** bookmarklet on that
   tab: dragged to the bookmarks bar and clicked on fantasy.espn.com, it reads the
   cookies (ESPN doesn't mark them HttpOnly), opens this site and hands them over
   with `postMessage`, never through a URL. (In Edge, bookmarks are Favorites; the
   favorites bar shows with Ctrl+Shift+B.) Without bookmarks: **Open ESPN** on that
   tab opens the league with this site as its opener, and the member pastes a short
   snippet into the ESPN tab's console, which checks it's talking to this site before
   sending anything. Pasting the cookies by hand also works. The cookies are used for that one request and never
   saved or logged; they do sign in to the member's ESPN account, so members are
   trusting this site with them. Players a member already typed in aren't added
   twice, and a later "Import all past seasons" doesn't undo a recovery.
2. **Draft picks** tab: "Create picks" gives every team its own picks for a range
   of seasons. Each draft's order comes from the season before, fetched
   from ESPN: the teams that missed the playoffs pick first, worst regular-season record
   first, then the playoff teams by playoff finish, the champion last; for past
   seasons the picks go to that season's teams. The tab shows upcoming drafts unless you
   pick a season or "All seasons". Edit a pick to set its slot in the round, who it was used on, or a note.
   "Used on" must be a player in the system: the box suggests names as you type, from
   rosters, transactions, lineups and ESPN's full player list (saved on every sync, so
   a rookie nobody has added yet can be chosen). Names that match no one are flagged.
3. To show picks changing hands:
   - On the **Transactions** tab, press **Edit** on an ESPN trade and add the picks
     that were part of the deal.
   - For deals ESPN has no record of (pick-for-pick swaps), use **New trade**, or
     **Trade** next to a pick.
4. **Teams** tab shows each roster next to the picks the team currently owns.
   Gold chips are picks acquired from another team.
   **Team Awards** tab: per team and season, the MVP, best game, most points left
   on the bench, bench warmer, most and fewest days rostered, waiver wire gem (an
   in-season pickup; this league adds its rookies as free agents in the summer),
   trade prize and bust. Points come from weekly lineups, which ESPN has from 2019
   (`espn_lineups.py`, stored each sync). Days rostered come from adds, drops and
   trades, with lineups filling in for trades ESPN hides (`awards.py`).
   **League Records** tab: top-10 boards for the best player weeks, best and worst
   team weeks, biggest weeks left on the bench, best player seasons and best team
   seasons (points per week, since early seasons had 16 weeks), all-time or per
   season (`records.py`). Only finished weeks count.
   **Trades** (Transactions tab): each trade shows what every side got and the points
   those players went on to score as starters for their new team, until they left it.
   A traded pick shows the player it was used on (set "used on" when editing the pick)
   and his points for the team that made the pick. Members vote on who won, or a tie;
   click your vote again to take it back (`trades.py`).
5. **Activity** tab lists every change: who made it, when, and what it was.
   Edit dialogs for a trade or a pick show that item's own change history.

A pick's owner is worked out from its trade history: it starts with its original
team and moves with each trade it appears in, in date order. Deleting a manual
trade puts the pick back automatically.

## How data is kept safe

- Every ESPN transaction is stored with a key built from its timestamp and contents,
  so re-syncing never duplicates it.
- Anything added by hand (picks on trades, manual trades, pick details) is marked
  `manual` and is never changed by a sync.
- ESPN transactions can't be deleted in the app, since they would come back on the
  next sync; only items added by hand can be removed.
- Deletes are permanent, but the activity log keeps a full copy of what was
  deleted and who deleted it.

## Files

- `app.py`: Flask app and JSON API, including sign-in and the activity log
- `auth.py`: accounts, password hashing, sessions, sign-in throttling
- `db.py`: schema (SQLite or Postgres), pick-ownership logic, activity log writes
- `espn_sync.py`: ESPN pull, built on `espn_api`
- `espn_log.py`: past seasons from ESPN's transaction log
- `espn_lineups.py`: weekly lineups and points; `awards.py`: Team Awards; `records.py`: League Records;
  `trades.py`: trade points and votes;
  `players.py`: the player list behind name suggestions
- `wsgi.py`, `railway.json`, `.python-version`: hosting on Railway with gunicorn
- `manage.py`: create accounts or reset passwords from the command line
- `copy_sqlite_to_postgres.py`: one-time move of an existing `dynasty.db` to Postgres
- `static/`: the web interface (plain HTML, CSS and JavaScript)
- `tests/`: `pytest` tests against a fake ESPN league; set `TEST_DATABASE_URL` to
  a throwaway Postgres database to run them against Postgres too
