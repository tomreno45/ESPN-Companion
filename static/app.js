const state = { awards: null, records: null, me: null, teams: [], picks: [], txns: [], sync: null, members: [], users: [], activity: [] };

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (data.signed_out && path !== "/api/me" && path !== "/api/login") showSignIn("You've been signed out. Sign in again.");
    else if (data.must_change_password) showPasswordForm(true);
    throw new Error(data.error || data.message || `Request failed (${res.status})`);
  }
  return data;
}

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), 4000);
}

const teamName = (id) => state.teams.find((t) => t.id === id)?.name ?? (id ? `Team ${id}` : "");
const memberName = (id) => state.members.find((m) => m.id === id)?.display_name ?? "";
const managersOf = (teamId) => state.members.filter((m) => m.team_id === teamId && m.active).map((m) => m.display_name);
const fmtDate = (ms) => new Date(ms).toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const currentTeams = () => state.teams.filter((t) => t.current);
const teamOptions = (selected, blank = "", teams = currentTeams()) =>
  `<option value="">${esc(blank)}</option>` +
  teams.map((t) => `<option value="${t.id}" ${t.id === selected ? "selected" : ""}>${esc(t.name)}</option>`).join("");
// Who offered an imported trade: from ESPN when it shows the proposal, otherwise the one other team
// in the deal once members have filled it in (in a two-team trade, the side that didn't accept offered).
function offeredBy(t) {
  if (t.offered_by) return t.offered_by;
  if ((t.accepted_by || []).length !== 1) return null;
  const others = new Set(t.items.flatMap((i) => [i.from_team_id, i.to_team_id]).filter((id) => id && id !== t.accepted_by[0]));
  return others.size === 1 ? [...others][0] : null;
}

// ESPN's note on an imported trade, naming the teams under today's names
const espnNote = (t) => {
  const accepted = (t.accepted_by || []).map(teamName).join(" and ");
  const offered = offeredBy(t);
  const who = offered && accepted ? `Offered by ${teamName(offered)}, accepted by ${accepted}.`
    : offered ? `Offered by ${teamName(offered)}.` : accepted ? `Accepted by ${accepted}.` : "";
  return [who, t.espn_note || ""].filter(Boolean).join(" ");
};
// the teams in the league in a transaction's season (under today's names), for filling in past trades
const seasonTeams = (t) => {
  const ids = Object.keys(t.team_names || {}).map(Number);
  return ids.length ? ids.map((id) => ({ id, name: teamName(id) })).sort((a, b) => a.name.localeCompare(b.name)) : currentTeams();
};

// ---------- loading ----------

async function loadTeams() { state.teams = await api("/api/teams"); }
async function loadPicks() { state.picks = await api("/api/picks"); }
async function loadSync() { state.sync = await api("/api/sync"); }
async function loadMembers() { state.members = await api("/api/members"); }
async function loadRecover() { state.recover = await api("/api/recover-trades"); }
async function loadUsers() { state.users = state.me?.is_commissioner ? await api("/api/users") : []; }

async function loadTxns() {
  const p = new URLSearchParams();
  if ($("#f-q").value) p.set("q", $("#f-q").value);
  if ($("#f-team").value) p.set("team", $("#f-team").value);
  if ($("#f-kind").value) p.set("kind", $("#f-kind").value);
  if ($("#f-season").value) p.set("season", $("#f-season").value);
  if ($("#f-picks").checked) p.set("has_picks", "1");
  const request = (loadTxns.latest = (loadTxns.latest || 0) + 1);
  const rows = await api(`/api/transactions?${p}`);
  if (request === loadTxns.latest) state.txns = rows;  // a slower, older filter change mustn't win
}

async function refreshAll() {
  await Promise.all([loadTeams(), loadPicks(), loadSync(), loadMembers(), loadUsers(), loadRecover()]);
  await loadTxns();
  fillFilters();
  renderAll();
  loadActivity().catch((err) => toast(err.message, true));
}

function renderAll() { renderTeams(); renderTxns(); renderPicks(); renderSync(); renderMembers(); renderRecover(); }

function fillFilters() {
  for (const sel of ["#f-team", "#p-team"]) {
    const el = $(sel), v = el.value;
    el.innerHTML = teamOptions(null, sel === "#f-team" ? "All teams" : "All owners");  // teams in the league now
    el.value = v;
  }
  const au = $("#a-user"), av = au.value;
  au.innerHTML = `<option value="">Everyone</option>` + state.members.map((m) => `<option value="${m.id}">${esc(m.display_name)}</option>`).join("");
  au.value = av;
  const mf = $("#member-form").team_id, mv = mf.value;
  mf.innerHTML = teamOptions(null, "Team (optional)");
  mf.value = mv;
  const seasons = state.sync.seasons;
  const el = $("#f-season"), v = el.value;
  el.innerHTML = `<option value="">All seasons</option>` + seasons.map((s) => `<option>${s}</option>`).join("");
  el.value = v;
  const ps = $("#p-season"), pv = ps.value;
  const pickSeasons = [...new Set(state.picks.map((p) => p.season))];
  ps.innerHTML = `<option value="">Upcoming drafts</option><option value="all">All seasons</option>`
    + pickSeasons.map((s) => `<option>${s}</option>`).join("");
  ps.value = pv;
  const gen = $("#gen-form");
  if (!gen.start_season.value) {
    const y = state.sync.year + 1;
    gen.start_season.value = y;
    gen.end_season.value = y + 2;
  }
  if (!$("#sync-year").value) $("#sync-year").value = state.sync.year;
}

// ---------- teams ----------

// "1.04", or "" when the pick's spot in the round isn't known or projected
const slotText = (p) => (p.slot ? `${p.round}.${String(p.slot).padStart(2, "0")}` : "");
const ordinal = (n) => n + (["th", "st", "nd", "rd"][(n % 100 - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th");
const recordText = (t) => (t.wins == null ? "" : `${t.wins}–${t.losses}${t.ties ? "–" + t.ties : ""}${t.standing ? " · " + ordinal(t.standing) : ""}`);

// ESPN's lineup slot names, as the league calls them
const SLOT_LABEL = { "RB/WR/TE": "FLEX" };

function picksSummary(picks) {
  if (!picks.length) return `<p class="muted small">No draft picks recorded.</p>`;
  const bySeason = {};
  for (const p of picks) (bySeason[p.season] ??= []).push(p);
  return Object.entries(bySeason).map(([season, ps]) => `
    <div class="pick-row"><span class="season">${season}</span>
      ${ps.map((p) => `<span class="chip ${p.original_team_id !== p.owner_team_id ? "acquired" : ""} ${p.used_on ? "used" : ""} ${p.slot_projected ? "projected" : ""}"
        title="${esc(p.original_team_id !== p.owner_team_id ? "Via " + teamName(p.original_team_id) : "Own pick")}${p.overall ? ` · ${p.slot_projected ? "projected " : ""}#${p.overall} overall` : ""}${p.used_on ? " · used on " + esc(p.used_on) : ""}">
        ${p.slot ? `${slotText(p)} <small>#${p.overall}</small>` : `R${p.round}`}${p.original_team_id !== p.owner_team_id ? " <small>" + esc(state.teams.find((t) => t.id === p.original_team_id)?.abbrev || "") + "</small>" : ""}
      </span>`).join("")}
    </div>`).join("");
}

function renderTeams() {
  const grid = $("#teams-grid");
  if (!currentTeams().length) {
    grid.innerHTML = `<div class="empty">No teams yet. ${state.me?.is_commissioner ? `Open the <a href="#" data-goto="sync">Sync</a> tab to pull your league from ESPN.` : "The commissioner hasn't pulled the league from ESPN yet."}</div>`;
    return;
  }
  const last = state.sync.runs.find((r) => r.status === "ok");
  $("#teams-meta").textContent = last ? `Last synced ${fmtDate(last.finished_at)}` : "";
  grid.innerHTML = currentTeams().map((t) => `
    <article class="card team">
      <header>
        ${t.logo_url ? `<img src="${esc(t.logo_url)}" alt="" onerror="this.remove()">` : ""}
        <div><h2>${esc(t.name)}${recordText(t) ? ` <span class="record muted">${recordText(t)}</span>` : ""}</h2><div class="muted small">${esc(t.owners || "")}${managersOf(t.id).length ? ` · <span title="League member account">${esc(managersOf(t.id).join(", "))}</span>` : ""}</div></div>
      </header>
      <h3>Draft picks</h3>
      ${picksSummary(t.picks)}
      <h3>Roster <span class="muted small">${t.roster.length}</span></h3>
      <table class="roster">
        ${t.roster.map((p) => `<tr class="slot-${esc((p.lineup_slot || "").replace(/\W/g, ""))}">
          <td class="slot">${esc(SLOT_LABEL[p.lineup_slot] ?? p.lineup_slot)}</td><td>${esc(p.name)}</td><td class="muted">${esc(p.position)}</td><td class="muted">${esc(p.pro_team || "")}</td></tr>`).join("")}
      </table>
    </article>`).join("");
}

// ---------- team awards ----------

async function loadAwards() {
  const season = $("#aw-season").value;
  state.awards = await api(`/api/awards${season ? `?season=${encodeURIComponent(season)}` : ""}`);
  renderAwards();
}

function renderAwards() {
  const a = state.awards;
  if (!a) return;
  const sel = $("#aw-season");
  const allTime = a.season === "all";
  sel.innerHTML = `<option value="all" ${allTime ? "selected" : ""}>All-time</option>`
    + a.seasons.map((s) => `<option ${s === a.season ? "selected" : ""}>${s}</option>`).join("");
  $("#awards-note").textContent = allTime
    ? `Every season combined, ${a.seasons[a.seasons.length - 1]}–${a.seasons[0]}, for each franchise under today's name. Points start in 2019, when ESPN's weekly lineups begin; trades and pickups count the points from that stint only.`
    : a.has_lineups
      ? "Points come from each week's lineups. In past seasons, a player who changed teams in a trade ESPN keeps hidden is only placed to the week."
      : "ESPN has no weekly lineups before 2019, so this season only has awards from roster moves.";
  const grid = $("#awards-grid");
  if (!a.teams.length) {
    grid.innerHTML = `<div class="empty">No awards for ${allTime ? "any season" : a.season} yet. They appear once lineups or roster moves are synced from ESPN.</div>`;
    return;
  }
  grid.innerHTML = a.teams.map((t) => `
    <article class="card team awards-card">
      <header><div><h2>${esc(teamName(t.team_id))}${a.season === state.sync?.year && recordText(state.teams.find((x) => x.id === t.team_id) || {}) ? ` <span class="record muted">${recordText(state.teams.find((x) => x.id === t.team_id))}</span>` : ""}</h2></div></header>
      <ul class="awards">${t.awards.map((w) => `
        <li class="award award-${w.key}">
          <span class="award-icon" aria-hidden="true">${w.icon}</span>
          <div class="award-body">
            <div class="award-title">${esc(w.title)}</div>
            <div><strong>${esc(w.player)}</strong> <span class="award-value">${esc(w.value)}</span></div>
            ${w.detail ? `<div class="muted small">${esc(w.detail)}</div>` : ""}
          </div>
        </li>`).join("")}</ul>
    </article>`).join("");
}

// ---------- league records ----------

const RECORD_BOARDS = [
  { key: "player_week", icon: "💥", title: "Best player weeks", sub: "As a starter" },
  { key: "team_week_high", icon: "🔥", title: "Best team weeks", sub: "Starters' points" },
  { key: "team_week_low", icon: "🧊", title: "Worst team weeks", sub: "Starters' points" },
  { key: "bench_week", icon: "😬", title: "Best weeks on the bench", sub: "Points nobody started" },
  { key: "player_season", icon: "🏆", title: "Best player seasons", sub: "Points as a starter, for one team" },
  { key: "team_season", icon: "📈", title: "Best team seasons", sub: "Starters' points per week" },
];

async function loadRecords() {
  const season = $("#rec-season").value;
  state.records = await api(`/api/records${season ? `?season=${encodeURIComponent(season)}` : ""}`);
  renderRecords();
}

function recordTeam(r) {
  const then = r.then && r.then.trim() !== r.team.trim() ? ` <span class="muted">(then ${esc(r.then)})</span>` : "";
  return `${esc(r.team)}${then}`;
}

function recordLine(key, r, allTime) {
  const when = (week) => (week ? `Week ${week}` : "") + (allTime ? `${week ? ", " : ""}${r.season}` : "");
  switch (key) {
    case "player_week":
    case "bench_week":
      return { main: `${esc(r.player)} <span class="muted small">${esc(r.position)}</span>`, sub: `${recordTeam(r)} · ${when(r.week)}` };
    case "team_week_high":
    case "team_week_low":
      return { main: recordTeam(r), sub: `${when(r.week)} · top scorer ${esc(r.top)}, ${fmtPts(r.top_points)}` };
    case "player_season":
      return { main: `${esc(r.player)} <span class="muted small">${esc(r.position)}</span>`,
               sub: `${recordTeam(r)}${allTime ? ` · ${r.season}` : ""} · ${r.starts} start${r.starts === 1 ? "" : "s"}` };
    default:
      return { main: recordTeam(r), sub: `${allTime ? `${r.season} · ` : ""}${fmtPts(r.total)} pts over ${r.weeks} week${r.weeks === 1 ? "" : "s"}` };
  }
}

function fmtPts(x) {
  return Math.abs(x - Math.round(x)) < 0.05 ? Math.round(x).toLocaleString() : x.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

function renderRecords() {
  const d = state.records;
  if (!d) return;
  const allTime = d.season === "all";
  $("#rec-season").innerHTML = `<option value="all" ${allTime ? "selected" : ""}>All-time</option>`
    + d.seasons.map((s) => `<option ${s === d.season ? "selected" : ""}>${s}</option>`).join("");
  $("#records-note").textContent = `${allTime && d.seasons.length ? `Every season from ${d.seasons[d.seasons.length - 1]}, when ESPN's weekly lineups begin. ` : ""}`
    + "Finished weeks only, so a week still being played can't set a record. Teams go by today's name.";
  const grid = $("#records-grid");
  if (!Object.values(d.records).some((rows) => rows.length)) {
    grid.innerHTML = `<div class="empty">No records for ${allTime ? "any season" : d.season} yet. They appear once weekly lineups are synced from ESPN.</div>`;
    return;
  }
  grid.innerHTML = RECORD_BOARDS.filter((b) => d.records[b.key]?.length).map((b) => `
    <article class="card records-card">
      <header><div><h2><span aria-hidden="true">${b.icon}</span> ${esc(b.title)}</h2><div class="muted small">${esc(b.sub)}</div></div></header>
      <ol class="records">${d.records[b.key].map((r) => {
        const line = recordLine(b.key, r, allTime);
        return `<li class="${r.rank === 1 ? "first" : ""}">
          <span class="rank">${r.rank}</span>
          <div class="record-body"><div class="record-main">${line.main}</div><div class="muted small">${line.sub}</div></div>
          <span class="record-pts">${fmtPts(r.pts)}</span>
        </li>`;
      }).join("")}</ol>
    </article>`).join("");
}

// ---------- transactions ----------

const KIND_LABEL = { TRADE: "Trade", WAIVER: "Waiver claim", ADD: "Free agent", DROP: "Release", OTHER: "Other" };

// ---------- trade grades and votes ----------

const fmtMonthYear = (ms) => new Date(ms).toLocaleDateString(undefined, { month: "short", year: "numeric" });
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function assetLine(a) {
  const pts = a.points == null ? "" : `<span class="asset-pts">${fmtPts(a.points)} pts</span>`;
  const bits = [];
  if (a.kind === "pick") {
    if (a.traded_on_to) bits.push(`Traded on to ${esc(teamName(a.traded_on_to))} before the draft`);
    else if (a.used_on) bits.push(`Used on <strong>${esc(a.used_on)}</strong>`);
  }
  if (a.points != null) bits.push(a.starts ? plural(a.starts, "start") : "Never started");
  if (a.left === "drop") bits.push(`dropped ${fmtMonthYear(a.left_at)}`);
  else if (a.left === "trade") bits.push(`traded away ${fmtMonthYear(a.left_at)}`);
  else if (a.left === "unrecorded") bits.push(`gone by ${fmtMonthYear(a.left_at)}`);
  else if (a.points != null && !a.traded_on_to) bits.push("still on the team");
  if (a.note) bits.push(esc(a.note));
  return `<li class="${a.kind}">${a.kind === "pick" ? "🎟️ " : ""}${esc(a.label)} ${pts}
    ${bits.length ? `<div class="muted small">${bits.join(" · ")}</div>` : ""}</li>`;
}

function describeTrade(t) {
  const released = t.items.filter((i) => i.to_team_id == null);
  return t.sides.map((s) => `
    <div class="side">
      <div class="side-team">${esc(teamName(s.team_id))} receives${s.points != null && s.assets.length
        ? ` <span class="side-pts" title="Points the players scored as starters for this team">${fmtPts(s.points)} pts</span>` : ""}</div>
      ${s.assets.length ? `<ul>${s.assets.map(assetLine).join("")}</ul>` : `<p class="muted small">Not filled in yet. Use Edit to add what they got.</p>`}
    </div>`).join("")
    + (released.length ? `<div class="side"><div class="side-team">Released</div><ul>${released.map((i) => `<li>${esc(i.label)}</li>`).join("")}</ul></div>` : "");
}

function voteBar(t) {
  if (!t.votes || t.sides.length < 2) return "";
  const { tally, mine } = t.votes;
  const total = Object.values(tally).reduce((n, v) => n + v.length, 0);
  const options = [...t.sides.map((s) => ({ choice: s.team_id, label: `${teamName(s.team_id)} won` })), { choice: 0, label: "Tie" }];
  const counts = options.map((o) => (tally[o.choice] || []).length);
  const top = Math.max(...counts);
  const leaders = options.filter((o, i) => counts[i] === top);
  const verdict = !total ? "No votes yet. Be the first."
    : leaders.length > 1 ? `Split decision (${plural(total, "vote")})`
    : `League says: ${leaders[0].choice === 0 ? "a tie" : esc(leaders[0].label)} (${top} of ${plural(total, "vote")})`;
  return `<div class="vote">
    <div class="vote-head"><strong>Who won?</strong> <span class="muted small">${verdict}</span></div>
    <div class="vote-options">${options.map((o, i) => `
      <button class="small vote-btn ${mine === o.choice ? "mine" : ""}" data-vote="${t.id}" data-choice="${o.choice}"
        aria-pressed="${mine === o.choice}" title="${esc((tally[o.choice] || []).join(", "))}">
        ${esc(o.label)} <span class="vote-count">${counts[i]}</span></button>`).join("")}</div>
    ${total ? `<div class="voters muted small">${options.filter((o) => tally[o.choice]?.length)
      .map((o) => `${esc(o.choice === 0 ? "Tie" : teamName(o.choice))}: ${esc(tally[o.choice].join(", "))}`).join(" · ")}</div>` : ""}
  </div>`;
}

async function castVote(txnId, choice) {
  const t = state.txns.find((x) => x.id === txnId);
  const again = t?.votes?.mine === choice;  // clicking your own vote takes it back
  t.votes = await api(`/api/transactions/${txnId}/vote`, again ? { method: "DELETE" } : { method: "PUT", body: { choice } });
  const card = $(`.txn[data-id="${txnId}"] .vote`);
  if (card) card.outerHTML = voteBar(t);
}

function describeTxn(t) {
  if (t.kind === "TRADE" && t.sides) return describeTrade(t);
  if (t.kind === "TRADE" || t.items.some((i) => i.asset_type === "pick")) {
    const byTeam = {};
    for (const it of t.items) {
      const key = it.to_team_id ?? "released";
      (byTeam[key] ??= []).push(it);
    }
    return Object.entries(byTeam).map(([tid, its]) => `
      <div class="side"><div class="side-team">${tid === "released" ? "Released" : esc(teamName(+tid)) + " receives"}</div>
        <ul>${its.map((i) => `<li class="${i.asset_type}">${i.asset_type === "pick" ? "🎟️ " : ""}${esc(i.label)}
          ${i.from_team_id && t.team_ids.length > 2 ? `<span class="muted small">from ${esc(teamName(i.from_team_id))}</span>` : ""}
          ${i.source === "manual" ? `<span class="tag" title="Added by hand">added${i.created_by_name ? " by " + esc(i.created_by_name) : ""}</span>` : ""}</li>`).join("")}</ul></div>`).join("");
  }
  return `<ul class="plain">${t.items.map((i) => {
    let verb;
    if (i.to_team_id && !i.from_team_id) verb = `${esc(teamName(i.to_team_id))} ${i.action === "WAIVER ADDED" ? "claimed" : "added"}`;
    else if (i.from_team_id && !i.to_team_id) verb = `${esc(teamName(i.from_team_id))} dropped`;
    else verb = `${esc(teamName(i.from_team_id))} → ${esc(teamName(i.to_team_id))}:`;
    return `<li>${verb} <strong>${esc(i.label)}</strong>${i.bid_amount ? ` <span class="muted small">($${i.bid_amount})</span>` : ""}</li>`;
  }).join("")}</ul>`;
}

function renderTxns() {
  const list = $("#txn-list");
  if (!state.txns.length) {
    list.innerHTML = `<div class="empty">No transactions match. Sync with ESPN or record a trade by hand.</div>`;
    return;
  }
  list.innerHTML = state.txns.map((t) => `
    <article class="card txn kind-${t.kind}" data-id="${t.id}">
      <div class="txn-head">
        <span class="badge">${KIND_LABEL[t.kind] || t.kind}</span>
        ${t.source === "manual" ? `<span class="badge manual">Manual</span>` : ""}
        <span class="muted">${fmtDate(t.date_ms)}</span>
        ${t.source === "manual" && t.created_by_name ? `<span class="muted small">recorded by ${esc(t.created_by_name)}</span>` : ""}
        ${t.kind === "TRADE" ? `<button class="small" data-edit="${t.id}">Edit</button>` : ""}
      </div>
      <div class="txn-body">${describeTxn(t)}</div>
      ${espnNote(t) ? `<p class="espn-note muted small">${esc(espnNote(t))}</p>` : ""}
      ${t.note ? `<p class="note">${esc(t.note)}</p>` : ""}
      ${t.kind === "TRADE" ? voteBar(t) : ""}
    </article>`).join("");
}

// pick dropdown, shows current owner so it's easy to find the right one
function pickOptions(selected) {
  return `<option value="">Choose a pick…</option>` + state.picks.map((p) =>
    `<option value="${p.id}" ${p.id === selected ? "selected" : ""}>${esc(p.label)} — owned by ${esc(teamName(p.owner_team_id))}</option>`).join("");
}

function itemRow(defaults = {}, teams = currentTeams()) {
  const type = defaults.asset_type || "pick";
  return `<div class="item-row">
    <select name="asset_type"><option value="pick" ${type === "pick" ? "selected" : ""}>Pick</option><option value="player" ${type === "player" ? "selected" : ""}>Player</option></select>
    <select name="pick_id" ${type === "pick" ? "" : "hidden"}>${pickOptions(defaults.pick_id)}</select>
    <input name="player_name" placeholder="Player name" ${type === "player" ? "" : "hidden"}>
    <label>from <select name="from_team_id">${teamOptions(defaults.from_team_id, "—", teams)}</select></label>
    <label>to <select name="to_team_id">${teamOptions(defaults.to_team_id, "—", teams)}</select></label>
    <button type="button" class="small ghost" data-remove-row>✕</button>
  </div>`;
}

function readRow(row) {
  const v = (n) => row.querySelector(`[name=${n}]`).value;
  return { asset_type: v("asset_type"), pick_id: v("pick_id"), player_name: v("player_name"), from_team_id: v("from_team_id"), to_team_id: v("to_team_id") };
}

function wireItemRows(root) {
  root.addEventListener("change", (e) => {
    const row = e.target.closest(".item-row");
    if (!row) return;
    if (e.target.name === "asset_type") {
      row.querySelector("[name=pick_id]").hidden = e.target.value !== "pick";
      row.querySelector("[name=player_name]").hidden = e.target.value !== "player";
    }
    if (e.target.name === "pick_id" && e.target.value) {
      const p = state.picks.find((p) => p.id === +e.target.value);
      if (p) {
        const from = row.querySelector("[name=from_team_id]"), to = row.querySelector("[name=to_team_id]");
        // keep the receiving side sensible: if it was the owner, flip it to the previous "from"
        if (+to.value === p.owner_team_id) to.value = +from.value !== p.owner_team_id ? from.value : "";
        from.value = p.owner_team_id;
      }
    }
  });
  root.addEventListener("click", (e) => {
    if (e.target.matches("[data-remove-row]")) e.target.closest(".item-row").remove();
  });
}

function openModal(html, onSubmit) {
  const dlg = $("#modal"), form = $("#modal-body");
  form.innerHTML = html;
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (e.submitter?.value === "cancel") return dlg.close();
    try {
      await onSubmit(e.submitter?.value, form);
    } catch (err) { toast(err.message, true); }
  };
  dlg.showModal();
  return form;
}

function showWarnings(res) {
  if (res.warnings?.length) toast(res.warnings.join(" "), true);
}

function openEditTxn(id) {
  const t = state.txns.find((x) => x.id === id);
  const form = openModal(`
    <h2>${KIND_LABEL[t.kind] || t.kind} · ${fmtDate(t.date_ms)}</h2>
    <p class="muted small">${t.source === "espn" ? "Imported from ESPN. You can add draft picks or players that were part of this deal; ESPN items stay as they are." : "Recorded by hand."}</p>
    ${espnNote(t) ? `<p class="espn-note small">${esc(espnNote(t))}</p>` : ""}
    <ul class="edit-items">${t.items.map((i) => `<li>
      <span>${i.asset_type === "pick" ? "🎟️ " : ""}<strong>${esc(i.label)}</strong>
      <span class="muted">${esc(teamName(i.from_team_id) || "FA")} → ${esc(teamName(i.to_team_id) || "released")}</span></span>
      ${i.source === "manual" ? `<button type="button" class="small ghost" data-del-item="${i.id}">Remove</button>` : `<span class="tag">ESPN</span>`}
    </li>`).join("")}</ul>
    <h3>Add to this transaction</h3>
    <div id="new-items">${itemRow({ to_team_id: t.team_ids[0], from_team_id: t.team_ids[1] }, seasonTeams(t))}</div>
    <button type="button" class="small" id="add-row">+ Another asset</button>
    <label class="block">Note <textarea name="note" rows="2">${esc(t.note || "")}</textarea></label>
    <div class="change-history" data-history-for="transaction_id=${t.id}"></div>
    <div class="actions">
      ${t.source === "manual" ? `<button value="delete" class="danger" formnovalidate>Delete transaction</button>` : ""}
      <span class="spacer"></span>
      <button value="cancel" formnovalidate>Cancel</button>
      <button value="save" class="primary">Save</button>
    </div>`, async (action, form) => {
    if (action === "delete") {
      if (!confirm("Delete this manual transaction? This can't be undone, but the activity log keeps a record of it.")) return;
      await api(`/api/transactions/${id}`, { method: "DELETE" });
    } else {
      await api(`/api/transactions/${id}`, { method: "PATCH", body: { note: form.note.value } });
      for (const row of $$(".item-row", form)) {
        const item = readRow(row);
        if ((item.asset_type === "pick" && !item.pick_id) || (item.asset_type === "player" && !item.player_name)) continue;
        showWarnings(await api(`/api/transactions/${id}/items`, { method: "POST", body: item }));
      }
    }
    $("#modal").close();
    await refreshAll();
  });
  wireItemRows(form);
  loadChangeHistory(form);
  $("#add-row", form).onclick = () => $("#new-items", form).insertAdjacentHTML("beforeend", itemRow({}, seasonTeams(t)));
  form.addEventListener("click", async (e) => {
    const del = e.target.dataset.delItem;
    if (!del) return;
    try {
      await api(`/api/items/${del}`, { method: "DELETE" });
      e.target.closest("li").remove();
      await refreshAll();
    } catch (err) { toast(err.message, true); }
  });
}

function openNewTrade(prefill = {}) {
  const today = new Date();
  const local = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const form = openModal(`
    <h2>Record a trade</h2>
    <p class="muted small">For deals ESPN doesn't know about, like pick-for-pick swaps. To attach picks to a trade ESPN already has, use Edit on that trade instead.</p>
    <label class="block">Date <input type="datetime-local" name="date" value="${local}" required></label>
    <div id="new-items">${itemRow(prefill)}${itemRow({ to_team_id: prefill.from_team_id, from_team_id: prefill.to_team_id })}</div>
    <button type="button" class="small" id="add-row">+ Another asset</button>
    <label class="block">Note <textarea name="note" rows="2"></textarea></label>
    <div class="actions"><span class="spacer"></span>
      <button value="cancel" formnovalidate>Cancel</button>
      <button value="save" class="primary">Save trade</button></div>`, async (_a, form) => {
    const items = $$(".item-row", form).map(readRow)
      .filter((i) => (i.asset_type === "pick" ? i.pick_id : i.player_name));
    const date = new Date(form.date.value).toISOString();
    const res = await api("/api/transactions", { method: "POST", body: { kind: "TRADE", date, note: form.note.value, items } });
    showWarnings(res);
    $("#modal").close();
    await refreshAll();
  });
  wireItemRows(form);
  $("#add-row", form).onclick = () => $("#new-items", form).insertAdjacentHTML("beforeend", itemRow({}));
}

// ---------- picks ----------

function renderPicks() {
  const season = $("#p-season").value, owner = $("#p-team").value, moved = $("#p-moved").checked;
  const rows = state.picks.filter((p) =>
    (season === "all" || (season ? p.season === +season : p.season >= state.sync.draft_season)) && (!owner || p.owner_team_id === +owner) && (!moved || p.owner_team_id !== p.original_team_id));
  const el = $("#picks-table");
  const hasStandings = state.teams.some((t) => t.wins != null);
  $("#picks-note").textContent = (hasStandings
    ? `Spots for the ${state.sync.draft_season} draft are projected from current standings until the season ends. `
    : `Sync the ${state.sync.year} season to project ${state.sync.draft_season} draft spots from the standings. `)
    + "Once a season is over, the teams that missed the playoffs pick first, worst regular-season record first; then the playoff teams by playoff finish, the champion last. Set a pick's number to override it.";
  if (!state.picks.length) {
    el.innerHTML = `<div class="empty">No picks yet. ${state.me?.is_commissioner ? "Use “Create picks” above to give every team its own picks for the seasons you track." : "The commissioner hasn't created them yet."}</div>`;
    return;
  }
  el.innerHTML = `<div class="table-wrap"><table class="picks cards pick-list">
    <thead><tr><th>Pick</th><th>Original team</th><th>Owner</th><th>Spot (overall)</th><th>Used on</th><th>Note</th><th>Moves</th><th></th></tr></thead>
    <tbody>${rows.map((p) => `<tr class="${p.owner_team_id !== p.original_team_id ? "acquired" : ""}">
      <td class="card-title"><strong>${p.season} R${p.round}</strong></td>
      <td data-label="Original team" class="orig">${esc(teamName(p.original_team_id))}</td>
      <td data-label="Owner" class="owner">${esc(teamName(p.owner_team_id))}</td>
      <td data-label="Spot" class="spot">${p.slot ? `${slotText(p)} <span class="muted">#${p.overall}</span>${p.slot_projected ? ` <span class="tag" title="From current standings">projected</span>` : ""}` : ""}</td>
      <td data-label="Used on" class="extra">${esc(p.used_on || "")}${p.used_on && !p.used_on_player_id
        ? ` <span class="tag warn" title="Doesn't match a player in the system. Edit the pick and choose from the suggestions.">no match</span>` : ""}</td>
      <td data-label="Note" class="extra muted small">${esc(p.note || "")}</td>
      <td data-label="Moves" class="extra">${p.history.length ? `<a href="#" data-history="${p.id}">${p.history.length}</a>` : ""}</td>
      <td class="row-actions"><button class="small" data-trade-pick="${p.id}">Trade</button>
        <button class="small ghost" data-edit-pick="${p.id}">Edit</button></td>
    </tr>`).join("")}</tbody></table></div>`;
}

function openEditPick(id) {
  const p = state.picks.find((x) => x.id === id);
  const suggested = new Map();  // name shown -> player id, for the names offered so far
  const form = openModal(`
    <h2>${esc(p.label)}</h2>
    <p class="muted small">Owned by ${esc(teamName(p.owner_team_id))}. Ownership changes come from trades, so use Trade to move it.</p>
    <label class="block">Pick number in round <input type="number" name="pick_in_round" min="1" value="${p.pick_in_round ?? ""}" placeholder="${p.slot_projected ? "projected " + p.slot : ""}"></label>
    <label class="block">Used on (player drafted) <input name="used_on" list="player-suggestions" autocomplete="off"
      value="${esc(p.used_on || "")}" placeholder="Start typing a name"></label>
    <datalist id="player-suggestions"></datalist>
    <p class="muted small" id="used-on-hint">${p.used_on && !p.used_on_player_id ? "⚠️ This name doesn't match a player in the system yet. Pick one from the suggestions." : ""}</p>
    <label class="block">Note <textarea name="note" rows="2">${esc(p.note || "")}</textarea></label>
    <div class="change-history" data-history-for="pick_id=${p.id}"></div>
    <div class="actions"><button value="delete" class="danger" formnovalidate>Delete pick</button><span class="spacer"></span>
      <button value="cancel" formnovalidate>Cancel</button><button value="save" class="primary">Save</button></div>`,
  async (action, form) => {
    if (action === "delete") {
      if (!confirm("Delete this pick? It will also be removed from any trades it appears in. This can't be undone.")) return;
      await api(`/api/picks/${id}`, { method: "DELETE" });
    } else {
      const chosen = suggested.get(form.used_on.value);
      await api(`/api/picks/${id}`, { method: "PATCH", body: { pick_in_round: form.pick_in_round.value, used_on: form.used_on.value,
        used_on_player_id: chosen ?? (form.used_on.value === (p.used_on || "") ? p.used_on_player_id : null), note: form.note.value } });
    }
    $("#modal").close();
    await refreshAll();
  });
  let timer;
  form.used_on.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const hits = await api(`/api/players?q=${encodeURIComponent(form.used_on.value)}`).catch(() => []);
      $("#player-suggestions").innerHTML = hits.map((h) => {
        suggested.set(h.name, h.id);
        return `<option value="${esc(h.name)}">${esc([h.position, h.pro_team, h.team].filter(Boolean).join(" · "))}</option>`;
      }).join("");
      $("#used-on-hint").textContent = "";
    }, 200);
  };
  loadChangeHistory(form);
}

function showPickHistory(id) {
  const p = state.picks.find((x) => x.id === id);
  openModal(`<h2>${esc(p.label)} history</h2>
    <ol class="history"><li>Starts with ${esc(teamName(p.original_team_id))}</li>
    ${p.history.map((h) => `<li>${fmtDate(h.date_ms)}: ${esc(teamName(h.from_team_id) || "?")} → <strong>${esc(teamName(h.to_team_id))}</strong></li>`).join("")}</ol>
    <div class="actions"><span class="spacer"></span><button value="cancel">Close</button></div>`, async () => $("#modal").close());
}

// ---------- sync ----------

function renderSync() {
  const s = state.sync;
  $("#sync-config").innerHTML = s.configured
    ? `League <strong>${esc(s.league_id)}</strong>, current season ${s.year}. ${s.counts.teams} teams, ${s.counts.transactions} transactions, ${s.counts.picks} draft picks stored.`
    : `ESPN isn't configured yet. Set the league id and cookies (see <code>.env.example</code>) and restart.`;
  if (s.auto_sync_hours) $("#sync-config").insertAdjacentHTML("beforeend",
    ` New activity is also pulled automatically every ${s.auto_sync_hours} hours; the next one is due around ${fmtDate(s.next_auto_sync)}.`);
  $("#sync-runs").innerHTML = s.runs.length ? `<table class="runs cards"><thead><tr><th>When</th><th>Season</th><th>Started by</th><th>Result</th></tr></thead><tbody>
    ${s.runs.map((r) => `<tr class="${r.status}"><td class="card-title">${fmtDate(r.started_at)}</td><td data-label="Season">${r.season ?? ""}</td><td data-label="Started by">${esc(r.actor_name || "")}</td><td data-label="Result">${esc(r.status)}: ${esc(r.message || "")}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">No syncs yet.</p>`;
}

async function runSync(full, allPast = false) {
  const btns = $$("#tab-sync button");
  btns.forEach((b) => (b.disabled = true));
  $("#sync-result").textContent = allPast ? "Importing every past season… this takes a few minutes."
    : full ? "Pulling full history… this can take a minute." : "Syncing…";
  try {
    const res = await api("/api/sync", { method: "POST", body: allPast ? { all_past: true } : { year: $("#sync-year").value, full } });
    $("#sync-result").textContent = `Done: ${res.message}`;
  } catch (err) {
    $("#sync-result").textContent = err.message;
  } finally {
    btns.forEach((b) => (b.disabled = false));
    await refreshAll();
  }
}

// ---------- wiring ----------

function showTab(name) {
  if (!$(`#tab-${name}`) || (["members", "sync"].includes(name) && !state.me?.is_commissioner)) name = "teams";
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $("#tabs button.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  if (name === "awards" && state.me) loadAwards().catch((err) => toast(err.message, true));
  if (name === "records" && state.me) loadRecords().catch((err) => toast(err.message, true));
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${name}`));
  history.replaceState(null, "", `#${name}`);
}

$("#tabs").onclick = (e) => e.target.dataset.tab && showTab(e.target.dataset.tab);
document.addEventListener("click", (e) => {
  const d = e.target.dataset;
  if (d.goto) { e.preventDefault(); showTab(d.goto); }
  if (d.edit) openEditTxn(+d.edit);
  const vote = e.target.closest("[data-vote]");  // the click may land on the count inside the button
  if (vote) castVote(+vote.dataset.vote, +vote.dataset.choice).catch((err) => toast(err.message, true));
  if (d.editPick) openEditPick(+d.editPick);
  if (d.history) { e.preventDefault(); showPickHistory(+d.history); }
  if (d.tradePick) {
    const p = state.picks.find((x) => x.id === +d.tradePick);
    openNewTrade({ asset_type: "pick", pick_id: p.id, from_team_id: p.owner_team_id });
  }
});

let qTimer;
$("#f-q").oninput = () => { clearTimeout(qTimer); qTimer = setTimeout(() => loadTxns().then(renderTxns), 250); };
for (const id of ["#f-team", "#f-kind", "#f-season", "#f-picks"]) $(id).onchange = () => loadTxns().then(renderTxns);
for (const id of ["#p-season", "#p-team", "#p-moved"]) $(id).onchange = renderPicks;
$("#new-trade").onclick = () => openNewTrade();
$("#sync-quick").onclick = () => runSync(false);
$("#sync-full").onclick = () => runSync(true);
$("#sync-past").onclick = () => runSync(false, true);
$("#gen-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const res = await api("/api/picks/generate", { method: "POST", body: { start_season: f.start_season.value, end_season: f.end_season.value, rounds: f.rounds.value } });
    toast(`Created ${res.created} picks.`);
    await refreshAll();
  } catch (err) { toast(err.message, true); }
};

// ---------- activity ----------

const ACTIVITY_PAGE = 100;
const lowerFirst = (s) => s.charAt(0).toLowerCase() + s.slice(1);
const fmtTime = (ms) => new Date(ms).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const fmtDay = (ms) => new Date(ms).toLocaleDateString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric" });
const activityLine = (e, when) => `<li><span class="time muted">${when}</span>
  <span><strong>${esc(e.actor_name)}</strong> ${esc(lowerFirst(e.summary))}</span></li>`;

async function loadActivity(more = false) {
  const p = new URLSearchParams({ limit: ACTIVITY_PAGE });
  if ($("#a-user").value) p.set("user_id", $("#a-user").value);
  if (more && state.activity.length) p.set("before", state.activity[state.activity.length - 1].id);
  const rows = await api(`/api/activity?${p}`);
  state.activity = more ? state.activity.concat(rows) : rows;
  $("#activity-more").hidden = rows.length < ACTIVITY_PAGE;
  renderActivity();
}

function renderActivity() {
  const el = $("#activity-list");
  if (!state.activity.length) {
    el.innerHTML = `<div class="empty">No changes recorded yet.</div>`;
    return;
  }
  let html = "", day = "";
  for (const e of state.activity) {
    const d = fmtDay(e.at);
    if (d !== day) {
      html += `${day ? "</ul>" : ""}<h3>${esc(d)}</h3><ul class="activity">`;
      day = d;
    }
    html += activityLine(e, fmtTime(e.at));
  }
  el.innerHTML = html + "</ul>";
}

// fills a modal's .change-history box with the log entries for one transaction or pick
async function loadChangeHistory(root) {
  const box = $(".change-history", root);
  if (!box) return;
  try {
    const rows = await api(`/api/activity?${box.dataset.historyFor}&limit=20`);
    box.innerHTML = rows.length
      ? `<h3>Change history</h3><ul class="activity compact">${rows.map((e) => activityLine(e, fmtDate(e.at))).join("")}</ul>`
      : "";
  } catch { box.innerHTML = ""; }
}

// ---------- recovering trades ----------

function renderRecover() {
  const r = state.recover;
  if (!r) return;
  const yours = r.team_id == null ? " Your account isn't linked to a team yet; the commissioner can set it on the Members tab."
    : r.accepted_by_my_team ? ` At least ${r.accepted_by_my_team} of them were accepted by ${esc(teamName(r.team_id))}.` : "";
  $("#recover-status").innerHTML = r.missing
    ? `<strong>${r.missing} past trade${r.missing === 1 ? "" : "s"}</strong> (${r.seasons[0]}–${r.seasons[r.seasons.length - 1]}) still need their players.${yours}`
    : "Every imported past trade has its players. Nothing to recover.";
}

// The "Send ESPN login" bookmarklet runs on an espn.com page: it reads the member's ESPN cookies, opens this
// site in a new tab, waits for that tab to say it's signed in and ready, then hands the cookies over with
// postMessage, addressed to this site only. They never go in an address bar or anywhere but this one request.
function bookmarkletCode(origin) {
  const src = `(function(){var o=${JSON.stringify(origin)};`
    + `if(!/(^|\\.)espn\\.com$/.test(location.hostname)){alert('Open fantasy.espn.com first, then use this bookmark.');return}`
    + `var c={};document.cookie.split('; ').forEach(function(s){var i=s.indexOf('=');c[s.slice(0,i)]=s.slice(i+1)});`
    + `if(!c.espn_s2||!c.SWID){alert('Sign in to ESPN first, then use this bookmark again.');return}`
    + `var w=window.open(o+'/#recover','dynasty-companion');`
    + `if(!w){alert('Allow pop-ups for espn.com, then use this bookmark again.');return}`
    + `function h(e){if(e.origin===o&&e.source===w&&e.data==='dynasty-ready'){`
    + `w.postMessage({type:'dynasty-espn-login',swid:c.SWID,espn_s2:c.espn_s2},o);removeEventListener('message',h)}}`
    + `addEventListener('message',h)})();`;
  return "javascript:" + encodeURIComponent(src);
}

// Same idea without a bookmark: the member opens ESPN from this page (so the ESPN tab's window.opener is
// this tab), then pastes this into that tab's console. It checks the opener really is this site before
// sending anything: "hello" goes only to this site's origin, and only a "ready" reply gets the cookies.
function consoleCode(origin) {
  return `(function(){var o=${JSON.stringify(origin)};
if(!/(^|\\.)espn\\.com$/.test(location.hostname)){alert('Run this in the ESPN tab.');return}
var c={};document.cookie.split('; ').forEach(function(s){var i=s.indexOf('=');c[s.slice(0,i)]=s.slice(i+1)});
if(!c.espn_s2||!c.SWID){alert('Sign in to ESPN first, then run this again.');return}
var p=window.opener,done=false;
if(!p){alert('Open ESPN with the Open ESPN button on the Recover my trades page, then run this again.');return}
function h(e){if(e.origin===o&&e.source===p&&e.data==='dynasty-ready'){done=true;removeEventListener('message',h);
p.postMessage({type:'dynasty-espn-login',swid:c.SWID,espn_s2:c.espn_s2},o);alert('Sent! Go back to the Dynasty Companion tab.')}}
addEventListener('message',h);p.postMessage('dynasty-hello',o);
setTimeout(function(){if(!done)alert("Couldn't reach Dynasty Companion. Open ESPN with the Open ESPN button on the Recover my trades page, then run this again.")},4000)})();`;
}

const ESPN_PAGE = /^https:\/\/([a-z0-9-]+\.)*espn\.com$/;

// Opened by the bookmarklet: once signed in, tell the ESPN tab we're ready (the message carries nothing).
function announceReadyForEspnLogin() {
  if (window.opener && location.hash === "#recover") window.opener.postMessage("dynasty-ready", "*");
}

window.addEventListener("message", (e) => {
  if (!ESPN_PAGE.test(e.origin) || !state.me) return;
  // the console code checking it's talking to this site; the reply carries nothing
  if (e.data === "dynasty-hello") return e.source?.postMessage("dynasty-ready", e.origin);
  if (e.data?.type !== "dynasty-espn-login") return;
  const f = $("#recover-form");
  f.swid.value = e.data.swid || "";
  f.espn_s2.value = e.data.espn_s2 || "";
  showTab("recover");
  toast("Got your ESPN login. Recovering your trades…");
  f.requestSubmit();
});

$("#espn-bookmarklet").href = bookmarkletCode(location.origin);
$("#console-code").value = consoleCode(location.origin);
$("#copy-console-code").onclick = () =>
  navigator.clipboard.writeText(consoleCode(location.origin))
    .then(() => toast("Copied. Paste it into the ESPN tab's console."),
      () => { $("#console-code").select(); toast("Couldn't copy automatically; the code is selected, press Ctrl+C.", true); });
$("#open-espn").onclick = () => {
  const league = state.sync?.league_id;
  // a plain window.open keeps window.opener, which is how the ESPN tab finds this one
  window.open(`https://fantasy.espn.com/football/league${league ? `?leagueId=${encodeURIComponent(league)}` : ""}`, "_blank");
};
$("#espn-bookmarklet").onclick = (e) => {
  e.preventDefault();
  toast("Drag this button to your bookmarks bar, then click it while you're on fantasy.espn.com.");
};
$("#copy-bookmarklet").onclick = () =>
  navigator.clipboard.writeText(bookmarkletCode(location.origin))
    .then(() => toast("Copied. Paste it as a bookmark's address."),
      () => toast("Couldn't copy; long-press the Send ESPN login button and copy its link instead.", true));

function showRecoverResult(kind, text) {
  const el = $("#recover-result");
  el.className = `result ${kind}`;
  el.textContent = (kind === "success" ? "✓ " : kind === "error" ? "✕ " : "") + text;
  el.hidden = false;
  el.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

$("#recover-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, button = $("button", f);
  button.disabled = true;
  const login = { swid: f.swid.value, espn_s2: f.espn_s2.value };
  f.reset();  // don't leave the cookies sitting in the page, whether or not ESPN takes them
  showRecoverResult("working", "Checking every past season with ESPN… this takes about a minute.");
  try {
    const res = await api("/api/recover-trades", { method: "POST", body: login });
    showRecoverResult("success", res.message + (res.recovered ? " They're on the Transactions tab, and the Activity tab credits you." : ""));
    toast(res.recovered ? `Success! Filled in ${res.recovered} trade${res.recovered === 1 ? "" : "s"}.` : "Success! Your ESPN login worked.");
    await refreshAll();
  } catch (err) {
    showRecoverResult("error", err.message);
    toast(err.message, true);
  } finally {
    button.disabled = false;
  }
};

// ---------- members (commissioner) ----------

function renderMembers() {
  if (!state.me?.is_commissioner) return;
  const status = (u) => (!u.active ? "Turned off" : u.must_change_password ? "Hasn't chosen a password yet" : "Active");
  $("#members-table").innerHTML = `<div class="table-wrap"><table class="picks cards">
    <thead><tr><th>Name</th><th>Username</th><th>Team</th><th>Role</th><th>Status</th><th>Last sign-in</th><th></th></tr></thead>
    <tbody>${state.users.map((u) => `<tr class="${u.active ? "" : "inactive"}">
      <td class="card-title"><strong>${esc(u.display_name)}</strong>${u.id === state.me.id ? ` <span class="muted small">(you)</span>` : ""}</td>
      <td data-label="Username">${esc(u.username)}</td>
      <td data-label="Team">${esc(teamName(u.team_id))}</td>
      <td data-label="Role">${u.is_commissioner ? "Commissioner" : "Member"}</td>
      <td data-label="Status">${status(u)}</td>
      <td data-label="Last sign-in">${u.last_login_at ? fmtDate(u.last_login_at) : "Never"}</td>
      <td class="row-actions"><button class="small" data-reset-user="${u.id}">Reset password</button>
        <button class="small ghost" data-edit-user="${u.id}">Edit</button></td>
    </tr>`).join("")}</tbody></table></div>`;
}

function showTempPassword(user, password, title) {
  openModal(`<h2>${esc(title)}</h2>
    <p>Give ${esc(user.display_name)} these details. They'll choose their own password when they sign in.</p>
    <div class="credentials">
      <div><span class="muted">Site</span> <code>${esc(location.origin)}</code></div>
      <div><span class="muted">Username</span> <code>${esc(user.username)}</code></div>
      <div><span class="muted">Temporary password</span> <code>${esc(password)}</code></div>
    </div>
    <p class="muted small">This password won't be shown again. If it gets lost, use Reset password.</p>
    <div class="actions"><button type="button" id="copy-creds">Copy</button><span class="spacer"></span>
      <button value="cancel" class="primary">Done</button></div>`, async () => $("#modal").close());
  $("#copy-creds").onclick = () =>
    navigator.clipboard.writeText(`Site: ${location.origin}\nUsername: ${user.username}\nTemporary password: ${password}`)
      .then(() => toast("Copied."), () => toast("Couldn't copy; select the text instead.", true));
}

function openEditUser(id) {
  const u = state.users.find((x) => x.id === id);
  const self = u.id === state.me.id;
  openModal(`<h2>${esc(u.display_name)}</h2>
    <label class="block">Name <input name="display_name" value="${esc(u.display_name)}" required></label>
    <label class="block">Team <select name="team_id">${teamOptions(u.team_id, "No team")}</select></label>
    <label class="check"><input type="checkbox" name="is_commissioner" ${u.is_commissioner ? "checked" : ""} ${self ? "disabled" : ""}> Commissioner (can manage accounts)</label>
    <label class="check"><input type="checkbox" name="active" ${u.active ? "checked" : ""} ${self ? "disabled" : ""}> Account on (turn off to stop them signing in)</label>
    ${self ? `<p class="muted small">You can't remove your own commissioner access or turn off your own account.</p>` : ""}
    <div class="actions"><span class="spacer"></span><button value="cancel" formnovalidate>Cancel</button>
      <button value="save" class="primary">Save</button></div>`, async (_a, form) => {
    const changes = { display_name: form.display_name.value, team_id: form.team_id.value };
    if (!self) Object.assign(changes, { is_commissioner: form.is_commissioner.checked, active: form.active.checked });
    await api(`/api/users/${id}`, { method: "PATCH", body: changes });
    $("#modal").close();
    await refreshAll();
  });
}

async function resetPassword(id) {
  const u = state.users.find((x) => x.id === id);
  if (!confirm(`Give ${u.display_name} a new temporary password? Their current password stops working and they're signed out.`)) return;
  try {
    const res = await api(`/api/users/${id}/reset-password`, { method: "POST", body: {} });
    showTempPassword(u, res.temp_password, "Password reset");
    await refreshAll();
  } catch (err) { toast(err.message, true); }
}

// ---------- signing in ----------

function formError(form, msg) {
  const p = $(".form-error", form);
  p.textContent = msg;
  p.hidden = !msg;
}

function showSignIn(message = "") {
  state.me = null;
  if ($("#modal").open) $("#modal").close();
  document.body.className = "signed-out";
  $("#password-form").hidden = true;
  const f = $("#login-form");
  f.hidden = false;
  formError(f, message);
  f.username.focus();
}

function showPasswordForm(required) {
  if ($("#modal").open) $("#modal").close();
  document.body.className = "signed-out";
  $("#login-form").hidden = true;
  const f = $("#password-form");
  f.reset();
  f.hidden = false;
  f.dataset.required = required ? "1" : "";
  formError(f, "");
  $("#current-password-field").hidden = required;
  $("#password-cancel").textContent = required ? "Sign out" : "Cancel";
  $("#password-intro").textContent = required
    ? `Welcome${state.me ? ", " + state.me.display_name : ""}. Replace your temporary password with one only you know.`
    : "Enter your current password, then the new one.";
  (required ? f.new_password : f.current_password).focus();
}

function showApp() {
  document.body.className = state.me.is_commissioner ? "signed-in commish" : "signed-in";
  $("#whoami").textContent = state.me.display_name;
}

async function enterApp() {
  showApp();
  showTab(location.hash.slice(1) || "teams");
  await refreshAll();
  announceReadyForEspnLogin();
}

async function signOut() {
  await api("/api/logout", { method: "POST", body: {} }).catch(() => {});
  showSignIn();
}

$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    state.me = await api("/api/login", { method: "POST", body: { username: f.username.value, password: f.password.value } });
    f.password.value = "";
    if (state.me.must_change_password) showPasswordForm(true);
    else await enterApp();
  } catch (err) { formError(f, err.message); }
};

$("#password-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.new_password.value !== f.confirm.value) return formError(f, "The two new passwords don't match.");
  try {
    state.me = await api("/api/password", { method: "POST", body: { current_password: f.current_password.value, new_password: f.new_password.value } });
    toast("Password saved.");
    await enterApp();
  } catch (err) { formError(f, err.message); }
};

$("#password-cancel").onclick = () => ($("#password-form").dataset.required ? signOut() : showApp());
$("#change-password").onclick = () => showPasswordForm(false);
$("#sign-out").onclick = signOut;

$("#aw-season").onchange = () => loadAwards().catch((err) => toast(err.message, true));
$("#rec-season").onchange = () => loadRecords().catch((err) => toast(err.message, true));
$("#a-user").onchange = () => loadActivity().catch((err) => toast(err.message, true));
$("#activity-more").onclick = () => loadActivity(true).catch((err) => toast(err.message, true));

$("#member-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const res = await api("/api/users", { method: "POST", body: {
      display_name: f.display_name.value, username: f.username.value, team_id: f.team_id.value, is_commissioner: f.is_commissioner.checked } });
    f.reset();
    showTempPassword(res.user, res.temp_password, "Account created");
    await refreshAll();
  } catch (err) { toast(err.message, true); }
};

document.addEventListener("click", (e) => {
  const d = e.target.dataset;
  if (d.editUser) openEditUser(+d.editUser);
  if (d.resetUser) resetPassword(+d.resetUser);
});

(async function boot() {
  try {
    state.me = await api("/api/me");
  } catch {
    return showSignIn();
  }
  if (state.me.must_change_password) showPasswordForm(true);
  else await enterApp();
})().catch((err) => toast(err.message, true));
