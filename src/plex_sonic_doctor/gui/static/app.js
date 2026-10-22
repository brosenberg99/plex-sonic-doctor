"use strict";
// Plex Sonic Doctor GUI. Talks only to the local app; every call carries the session key from the link.

const KEY = new URLSearchParams(location.hash.slice(1)).get("t") || sessionStorage.getItem("sd-key") || "";
if (KEY) { sessionStorage.setItem("sd-key", KEY); history.replaceState(null, "", location.pathname); }
// Opening a fresh link (new key) in an already-open tab only changes the #part: start over with it.
window.addEventListener("hashchange", () => location.reload());

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const size = (b) => (b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB");
const pct = (m) => (m == null ? "" : Math.round(m * 100) + "%");
const num = (n) => (n ?? 0).toLocaleString();

async function api(path, body) {
  const opts = { headers: { "X-Sonic-Doctor": KEY } };
  if (body !== undefined) { opts.method = "POST"; opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (error ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), error ? 8000 : 3500);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

// ------------------------------------------------------------------ tabs and state

let STATE = null;
const views = { identical: { page: 1 }, versions: { page: 1 }, broken: { page: 1, mode: "albums" }, check: { page: 1 }, unanalysed: { page: 1 } };
let current = "overview";

function showTab(name) {
  current = name;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + name));
  if (views[name]) loadList(name);
  if (name === "moves") loadBatches();
  if (name === "settings") loadSettings();
  if (name === "setup") loadSetup();
  window.scrollTo(0, 0);
}
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-tab]");
  if (t && t.closest(".tabs")) return showTab(t.dataset.tab);
  const g = e.target.closest("[data-goto]");
  if (g) showTab(g.dataset.goto);
});

async function refreshState() {
  STATE = await api("/api/state");
  $("#server-name").textContent = STATE.server ? "· " + STATE.server : "";
  $("#not-configured").classList.toggle("hidden", STATE.configured);
  const tw = $("#tools-warning");
  tw.classList.toggle("hidden", STATE.tools_ok);
  tw.textContent = "Audio fingerprints aren't available: " + STATE.tools;
  const s = STATE.results && STATE.results.stats;
  $("#n-identical").textContent = s ? num(s.identical_groups + s.sonic_only_groups) : "";
  $("#n-versions").textContent = s ? num(s.version_songs) : "";
  $("#n-broken").textContent = s ? num(s.broken_files) : "";
  $("#n-check").textContent = s ? num(s.check_pairs ?? (s.verdicts["check by ear"] || 0)) : "";
  $("#n-unanalysed").textContent = s ? num(s.not_analysed) : "";
  renderCards(s);
  renderMarked(STATE.marked);
  renderJob(STATE.job);
}

function renderCards(s) {
  const el = $("#stat-cards");
  if (!s) { el.innerHTML = `<div class="card"><div class="big">No scan yet</div><div class="sub">Run your first scan below.</div></div>`; return; }
  const sev = s.broken_by_severity || {};
  el.innerHTML = `
    <div class="card" data-goto="identical"><div class="big">${num(s.identical_groups + s.sonic_only_groups)}</div>
      <div>groups of identical copies</div><div class="sub">${num(s.extra_copies)} extra copies, ${size(s.extra_bytes)}${s.sonic_only_groups ? ` · ${num(s.sonic_only_groups)} matched on sonic data only` : ""}</div></div>
    <div class="card" data-goto="versions"><div class="big">${num(s.version_songs)}</div>
      <div>songs with different versions</div><div class="sub">${num(s.versions)} versions in all</div></div>
    <div class="card" data-goto="broken"><div class="big">${num(s.broken_files)}</div>
      <div>possibly broken files</div><div class="sub">${num(sev["Broken"])} broken, ${num(sev["Likely broken"])} likely, ${num(sev["Check"])} to check${s.broken_fixed ? ` · ${num(s.broken_fixed)} fixed since the scan` : ""}</div></div>
    <div class="card" data-goto="unanalysed"><div class="big">${num(s.tracks)}</div><div>tracks</div>
      <div class="sub">${num(s.not_analysed)} without Plex's sonic analysis (see why) · last scan ${esc(STATE.results.created)}</div></div>`;
}

function renderMarked(m) {
  $("#marked-text").textContent = m.files ? `${num(m.files)} ${m.files === 1 ? "copy" : "copies"} marked for removal (${size(m.bytes)})` +
    (STATE && STATE.moved ? ` · ${num(STATE.moved)} already in the holding folder` : "") : "Nothing marked for removal yet.";
  $("#btn-review").disabled = !m.files;
}

// ------------------------------------------------------------------ background jobs

let polling = null, lastJobFinished = 0;
function renderJob(j) {
  const running = j && j.running;
  $("#job-chip").classList.toggle("hidden", !running);
  $("#btn-stop").classList.toggle("hidden", !running);
  ["#btn-scan", "#btn-quick", "#btn-damage", "#btn-plan"].forEach((s) => ($(s).disabled = running));
  if (!j || !j.kind) return;
  $("#job-panel").classList.remove("hidden");
  $("#job-kind").textContent = j.kind + (running ? "" : j.error ? ": stopped" : ": finished");
  $("#job-message").textContent = j.error ? j.error : j.message;
  $("#job-chip-text").textContent = j.kind + ": " + j.message;
  const frac = running ? j.fraction : 1;
  for (const id of ["#job-bar", "#job-chip-bar"]) {
    const bar = $(id);
    bar.parentElement.classList.toggle("indeterminate", running && frac < 0);
    bar.style.width = frac >= 0 ? Math.round(frac * 100) + "%" : "30%";
  }
  $("#job-log").textContent = (j.log || []).map((l) => l[0] + "  " + l[1]).join("\n");
  $("#job-log").scrollTop = 1e9;
  if (running && !polling) polling = setInterval(pollJob, 1000);
}
async function pollJob() {
  const j = await api("/api/job");
  renderJob(j);
  if (!j.running) {
    clearInterval(polling); polling = null;
    if (j.finished !== lastJobFinished) {
      lastJobFinished = j.finished;
      if (j.error) toast(j.kind + ": " + j.error, true); else toast(jobSummary(j));
      await refreshState();
      if (views[current]) loadList(current);
      if (current === "moves") { loadBatches(); $("#plan").classList.add("hidden"); }
      if (current === "setup") loadSetup();
    }
  }
}
function jobSummary(j) {
  const r = j.result || {}, pl = r.playlists || {};
  const parts = [];
  if (r.moved != null) parts.push(`Moved ${num(r.moved)} ${r.moved === 1 ? "file" : "files"}`);
  if (r.restored != null) parts.push(`Put back ${num(r.restored)} ${r.restored === 1 ? "file" : "files"}`);
  if (r.rechecked != null) parts.push(`Rechecked ${num(r.rechecked)} ${r.rechecked === 1 ? "album" : "albums"}` +
    (Object.keys(r.statuses || {}).length ? ": " + Object.entries(r.statuses).map(([st, n]) => `${num(n)} ${STATUS[st][0].toLowerCase()}`).join(", ") : ""));
  if (pl.updated) parts.push(`updated ${num(pl.updated)} playlist ${pl.updated === 1 ? "entry" : "entries"}`);
  if (pl.restored) parts.push(`restored ${num(pl.restored)} playlist ${pl.restored === 1 ? "entry" : "entries"}`);
  const problems = [...(r.failed || []), ...(pl.failed || []), ...(pl.pending || [])];
  if (pl.pending && pl.pending.length) parts.push(`${num(pl.pending.length)} playlist entries wait for a Plex rescan`);
  if (problems.length) console.warn(problems);
  return parts.length ? parts.join("; ") + "." + (problems.length ? ` ${num(problems.length)} problem(s): see the browser console.` : "")
    : j.kind + " finished.";
}
async function startJob(path, body) { await api(path, body || {}); await pollJob(); polling = polling || setInterval(pollJob, 1000); }

$("#btn-scan").onclick = guard(() => startJob("/api/scan", { quick: false }));
$("#btn-quick").onclick = guard(() => startJob("/api/scan", { quick: true }));
$("#btn-damage").onclick = guard(() => startJob("/api/damage", { folder: $("#damage-folder").value }));
$("#btn-stop").onclick = guard(() => api("/api/stop", {}));
$("#btn-open-reports").onclick = guard(() => api("/api/open-reports", {}));

// ------------------------------------------------------------------ lists

function filtersOf(name) {
  const f = {};
  $$(`.filters[data-for=${name}] [name]`).forEach((el) => (f[el.name] = el.type === "checkbox" ? (el.checked ? "1" : "") : el.value));
  return f;
}

// The filters and sorts every list shares, added to each tab's own filters. A group shows when
// one of its tracks passes them all, and sorts by its first track (the suggested keep, version 1).
const OWN_SORTS = {
  identical: [["", "group order"], ["space", "most space saved"], ["copies", "most copies"]],
  versions: [["", "group order"], ["versions", "most versions"]],
  tracks: [["", "severity"]],
  albums: [["artist", "artist"], ["album", "album"], ["broken", "most broken tracks"], ["status", "status"], ["folder", "folder"],
    ["source", "source"], ["year", "year"], ["added", "recently added"]],
  check: [["", "scan order"], ["match", "lowest match first"]],
  unanalysed: [["", "reason"]],
};
const TRACK_SORTS = [["artist", "artist"], ["album", "album"], ["title", "title"], ["folder", "folder"], ["source", "source"],
  ["year", "year"], ["added", "recently added"]];
function sortOptions(name) {
  const key = name === "broken" ? views.broken.mode : name;
  return (key === "albums" ? OWN_SORTS.albums : [...OWN_SORTS[key], ...TRACK_SORTS])
    .map(([v, label]) => `<option value="${v}">Sort by ${esc(label)}</option>`).join("");
}
$$(".filters").forEach((box) => {
  const name = box.dataset.for;
  box.insertAdjacentHTML("beforeend", `<div class="filters-more">
    <input name="artist" list="dl-${name}-artists" placeholder="Artist"><datalist id="dl-${name}-artists"></datalist>
    <input name="album" list="dl-${name}-albums" placeholder="Album"><datalist id="dl-${name}-albums"></datalist>
    <input name="folder" list="dl-${name}-folders" placeholder="Folder"><datalist id="dl-${name}-folders"></datalist>
    <select name="source"><option value="">Any source</option></select>
    <span class="years"><input type="number" name="year_from" placeholder="Year from" min="1000" max="2999">
      <input type="number" name="year_to" placeholder="Year to" min="1000" max="2999"></span>
    <select name="sort">${sortOptions(name)}</select>
    <label class="check inline"><input type="checkbox" name="desc"> Reverse</label>
    <button class="link small" data-clear-filters>Clear filters</button></div>`);
  let timer;
  const reload = () => { views[name].page = 1; loadList(name); };
  box.addEventListener("input", (e) => {   // typing waits a moment; dropdowns and ticks apply at once
    clearTimeout(timer);
    if (e.target.tagName === "SELECT" || e.target.type === "checkbox") reload(); else timer = setTimeout(reload, 300);
  });
  box.addEventListener("click", (e) => {
    if (!e.target.closest("[data-clear-filters]")) return;
    $$("[name]", box).forEach((el) => { if (el.type === "checkbox") el.checked = false; else if (el.tagName === "SELECT") el.selectedIndex = 0; else el.value = ""; });
    reload();
  });
});

// What the shared filters can offer on a tab (its artists, albums, folders, sources), once per scan.
async function loadFacets(name) {
  const stamp = STATE.results.created;
  if (views[name].facets === stamp) return;
  views[name].facets = stamp;
  const f = await api(`/api/facets?tab=${name}`);
  const opts = (vals) => vals.map((v) => `<option value="${esc(v)}">`).join("");
  $(`#dl-${name}-artists`).innerHTML = opts(f.artists || []);
  $(`#dl-${name}-albums`).innerHTML = opts(f.albums || []);
  $(`#dl-${name}-folders`).innerHTML = Object.entries(f.folders || {}).map(([v, n]) => `<option value="${esc(v)}" label="${num(n)} tracks">`).join("");
  const sel = $(`.filters[data-for=${name}] [name=source]`), chosen = sel.value;
  sel.innerHTML = `<option value="">Any source</option>` +
    Object.entries(f.sources || {}).map(([v, n]) => `<option value="${esc(v)}">${esc(v)} (${num(n)})</option>`).join("");
  sel.value = chosen;
  if (f.years && f.years.length) $$(`.filters[data-for=${name}] .years input`).forEach((el) => (el.title = `${f.years[0]}–${f.years[1]} on this tab`));
}

async function loadList(name) {
  const f = filtersOf(name);
  const qs = new URLSearchParams({ ...f, page: views[name].page }).toString();
  const list = $(`#${name}-list`);
  if (!STATE || !STATE.results) { list.innerHTML = `<div class="empty">No scan yet. Run one from the Overview tab.</div>`; return; }
  const byAlbum = name === "broken" && views.broken.mode === "albums";
  loadFacets(name).catch(() => {});
  const data = await api(`/api/${byAlbum ? "broken-albums" : name}?${qs}`);
  views[name].data = data;
  const word = byAlbum ? "albums" : { identical: "groups", versions: "songs", broken: "files", check: "pairs", unanalysed: "tracks" }[name];
  $(`#${name}-total`).textContent = `${num(data.total)} ${word}` + (data.pages > 1 ? ` · page ${data.page} of ${data.pages}` : "");
  list.innerHTML = data.items.length ? data.items.map(byAlbum ? RENDER.albums : RENDER[name]).join("") : `<div class="empty">Nothing matches these filters.</div>`;
  renderPager(name, data);
  if (name === "unanalysed") renderReasons();
  if (byAlbum) renderAlbumStatuses(data.status_counts);
}

// ------------------------------------------------------------------ possibly broken, by album

const STATUS = {
  todo: ["To redownload", "not rechecked yet"],
  fixed: ["Fixed", "every file plays all the way through"],
  replaced: ["Fixed, old copies left", "a fresh download plays, but the broken file is still beside it"],
  broken: ["Still broken", "a file still won't play"],
  missing: ["Tracks missing", "a broken file is gone and nothing replaced it"],
  skipped: ["Can't redownload", "marked as not available"],
};
function renderAlbumStatuses(counts) {
  const chosen = $(`.filters[data-for=broken] [name=status]`).value;
  const all = Object.values(counts || {}).reduce((a, b) => a + b, 0);
  const done = (counts.fixed || 0) + (counts.replaced || 0);
  $("#album-statuses").innerHTML = `<div class="progress-line"><b>${num(done)} of ${num(all)}</b> albums fixed` +
    `<div class="bar small"><div style="width:${all ? Math.round(100 * done / all) : 0}%"></div></div></div>` +
    Object.keys(STATUS).filter((st) => counts[st]).map((st) =>
      `<button data-status="${st}" class="${chosen === st ? "active" : ""}"><b>${num(counts[st])}</b>${esc(STATUS[st][0])}<span>${esc(STATUS[st][1])}</span></button>`).join("");
}
$("#album-statuses").onclick = (e) => {
  const b = e.target.closest("[data-status]");
  if (!b) return;
  const sel = $(`.filters[data-for=broken] [name=status]`);
  sel.value = sel.value === b.dataset.status ? "" : b.dataset.status;
  views.broken.page = 1;
  loadList("broken");
};
$("#broken-view").onclick = (e) => {
  const b = e.target.closest("[data-view]");
  if (!b || b.dataset.view === views.broken.mode) return;
  views.broken.mode = b.dataset.view;
  views.broken.page = 1;
  $$("#broken-view button").forEach((x) => x.classList.toggle("active", x === b));
  $("#tab-broken").classList.toggle("tracks", b.dataset.view === "tracks");
  $(".filters[data-for=broken] [name=sort]").innerHTML = sortOptions("broken");
  loadList("broken");
};
const fileName = (p) => esc(p.split(/[\\/]/).pop());
$("#broken-list").addEventListener("click", guard(async (e) => {
  const box = e.target.closest("[data-folder]");
  if (!box) return;
  const folder = box.dataset.folder;
  if (e.target.closest("[data-recheck]")) return startJob("/api/recheck", { folders: [folder] });
  if (e.target.closest("[data-open-folder]")) return api("/api/reveal", { k: +box.dataset.k, folder: true });
  const skip = e.target.closest("[data-skip]");
  if (skip) { await api("/api/album-skip", { folder, skipped: skip.dataset.skip === "1" }); return loadList("broken"); }
  if (e.target.closest("[data-mark-old]")) {
    const r = await api("/api/mark-old-copies", { folder });
    toast(r.changed ? `Marked ${num(r.changed)} old ${r.changed === 1 ? "copy" : "copies"} for removal.` : "Nothing to mark: the old copies are already marked, or are kept as copies elsewhere.");
    renderMarked(r.marked); loadList("broken");
  }
}));
$("#btn-recheck-list").onclick = guard(async () => {
  await loadList("broken");
  const total = views.broken.data ? views.broken.data.total : 0;
  if (!total) return toast("No albums match these filters.");
  if (!confirm(`Recheck the ${num(total)} albums these filters show? Every audio file in their folders is played all the way through, which takes a while for many albums. It can be stopped.`)) return;
  await startJob("/api/recheck", { filters: filtersOf("broken") });
});

// The reasons on the Not analysed tab, with counts; clicking one filters the list to it.
function renderReasons() {
  const s = STATE && STATE.results && STATE.results.stats;
  const counts = (s && s.not_analysed_reasons) || {};
  const chosen = $(`.filters[data-for=unanalysed] [name=reason]`).value;
  const help = {
    "problem file": "won't decode: not audio, encrypted or damaged",
    "blocked by another file in its album": "fine, but an album-mate is a problem file",
    "result thrown away by Plex": "mostly clips under 30 s; nothing to fix",
    "no problem found": "Plex may not have reached these yet",
  };
  $("#unanalysed-reasons").innerHTML = Object.keys(help).filter((r) => counts[r]).map((r) =>
    `<button data-reason="${esc(r)}" class="${chosen === r ? "active" : ""}"><b>${num(counts[r])}</b>${esc(r)}<span>${esc(help[r])}</span></button>`).join("");
}
$("#unanalysed-reasons").onclick = (e) => {
  const b = e.target.closest("[data-reason]");
  if (!b) return;
  const sel = $(`.filters[data-for=unanalysed] [name=reason]`);
  sel.value = sel.value === b.dataset.reason ? "" : b.dataset.reason;
  views.unanalysed.page = 1;
  loadList("unanalysed");
};

function renderPager(name, d) {
  const el = $(`.pager[data-for=${name}]`);
  if (d.pages <= 1) { el.innerHTML = ""; return; }
  el.innerHTML = `<button data-page="1" ${d.page === 1 ? "disabled" : ""}>First</button>
    <button data-page="${d.page - 1}" ${d.page === 1 ? "disabled" : ""}>Previous</button>
    <span>Page ${d.page} of ${d.pages}</span>
    <button data-page="${d.page + 1}" ${d.page === d.pages ? "disabled" : ""}>Next</button>
    <button data-page="${d.pages}" ${d.page === d.pages ? "disabled" : ""}>Last</button>`;
  el.onclick = (e) => { const p = e.target.dataset.page; if (p) { views[name].page = +p; loadList(name); window.scrollTo(0, 0); } };
}

function decisionSelect(t) {
  if (t.moved) return `<span class="tag">in holding folder</span>`;
  return `<select class="decision" data-file="${esc(t.file)}">
    <option value="" ${!t.decision ? "selected" : ""}>Undecided</option>
    <option value="keep" ${t.decision === "keep" ? "selected" : ""}>Keep</option>
    <option value="remove" ${t.decision === "remove" ? "selected" : ""}>Remove</option></select>`;
}
const rowClass = (t) => (t.moved ? "moved" : t.decision === "remove" ? "remove" : t.decision === "keep" ? "keep" : "");
const playButtons = (t) => `<button class="play" data-k="${t.k}" data-at="start" title="Play 30 s from the start">▶ start</button>` +
  (t.duration > 60 ? `<button class="play" data-k="${t.k}" data-at="middle" title="Play 30 s from the middle">▶ middle</button>` : "");
const where = (t) => `<div class="path">${esc(t.file)} <button class="reveal" data-k="${t.k}" title="Show in folder">📂</button></div>`;
const loud = (t) => (t.silent ? "silent" : t.loudness == null ? "" : t.loudness.toFixed(1) + " LU");
const fmt = (t) => `${esc(t.codec)} ${t.bitrate ? t.bitrate + "k" : ""}`;
const source = (src) => (src ? `<span class="tag src">${esc(src)}</span>` : "");
const nameCell = (t) => `<div class="title">${esc(t.title)}</div><div class="sub">${esc(t.artist)} — ${esc(t.album)}${t.year ? ` (${t.year})` : ""} ${source(t.source)}</div>${where(t)}`;

const RENDER = {
  identical: (g) => `<div class="group">
    <div class="group-head"><b>Group ${g.group}</b> <span>${g.copies.length} copies</span>
      <span class="tag">${esc(g.status)}</span>${g.note ? `<span class="tag warn">${esc(g.note)}</span>` : ""}
      ${g.other_versions ? `<span class="tag">this song also has other versions (${esc(g.other_versions)})</span>` : ""}</div>
    <table><tr><th>Decision</th><th>Listen</th><th>Track</th><th>Length</th><th>Format</th><th>Loudness</th><th>Match</th><th>Size</th></tr>
    ${g.copies.map((t, n) => `<tr class="${rowClass(t)}"><td>${decisionSelect(t)}${n === 0 ? `<div class="suggest">suggested keep</div>` : ""}</td>
      <td>${playButtons(t)}</td><td>${nameCell(t)}</td><td class="num">${t.length}</td><td class="num">${fmt(t)}<div class="sub">${esc(t.channels)}</div></td>
      <td class="num">${loud(t)}</td><td class="num">${n === 0 ? "" : t.sonic_only ? "sonic only" : pct(t.match)}</td><td class="num">${size(t.size)}</td></tr>`).join("")}
    </table></div>`,

  versions: (s) => `<div class="group">
    <div class="group-head"><b>${esc(s.versions[0].title)}</b> <span>${esc(s.versions[0].artist)}</span>
      <span class="tag">${s.versions.length} versions</span></div>
    <table><tr><th>Decision</th><th>Listen</th><th>Version</th><th>Length</th><th>Format</th><th>Loudness</th><th>How it differs from version 1</th></tr>
    ${s.versions.map((t) => `<tr class="${rowClass(t)}"><td>${decisionSelect(t)}</td><td>${playButtons(t)}</td>
      <td><div class="sub">Version ${t.number}${t.identical_copies ? ` · ${t.identical_copies} identical ${t.identical_copies === 1 ? "copy" : "copies"} (group ${t.identical_group})` : ""}</div>${nameCell(t)}</td>
      <td class="num">${t.length}</td><td class="num">${fmt(t)}<div class="sub">${esc(t.channels)}</div></td><td class="num">${loud(t)}</td>
      <td class="reasons">${esc(t.reasons.join("; "))}${t.match != null ? `<div class="sub">audio match ${pct(t.match)}</div>` : ""}</td></tr>`).join("")}
    </table></div>`,

  broken: (b) => `<div class="group"><table><tr class="${rowClass(b)}">
      <td style="width:130px"><span class="sev ${esc(b.severity.split(" ")[0])}">${esc(b.severity)}</span><div>${decisionSelect(b)}</div></td>
      <td>${nameCell(b)}<div class="sub">${esc(b.folder_flagged)}</div></td>
      <td style="width:38%">${b.problems.map((p) => `<div><b>${esc(p.problem)}</b>: <span class="sub">${esc(p.details)}</span></div>`).join("")}</td>
      <td class="num">${b.length}<div>${fmt(b)}</div></td><td>${playButtons(b)}</td></tr></table></div>`,

  unanalysed: (t) => `<div class="group"><table><tr class="${rowClass(t)}">
      <td style="width:130px"><span class="reason">${esc(t.reason)}</span>${t.removable ? `<div>${decisionSelect(t)}</div>` : ""}</td>
      <td>${nameCell(t)}</td>
      <td style="width:38%" class="sub">${esc(t.details)}</td>
      <td class="num">${t.length}<div>${fmt(t)}</div></td></tr></table></div>`,

  albums: (a) => `<div class="group album" data-folder="${esc(a.folder)}" data-k="${a.broken[0].k}">
    <div class="group-head"><b>${esc(a.album || "(no album)")}</b> <span>${esc(a.artist)}${a.year ? ` (${a.year})` : ""}</span> ${source(a.source)}
      <span class="status ${a.status}" title="${esc(STATUS[a.status][1])}">${esc(STATUS[a.status][0])}</span>
      ${a.checked ? `<span>rechecked ${esc(a.checked)}</span>` : ""}
      <span class="actions">
        ${a.status === "replaced" ? `<button class="small" data-mark-old>Mark the old copies for removal</button>` : ""}
        <button class="small" data-open-folder title="Show the album folder">📂 Folder</button>
        <button class="small primary" data-recheck title="Play every file in the folder all the way through">Recheck</button>
        <button class="link small" data-skip="${a.status === "skipped" ? 0 : 1}">${a.status === "skipped" ? "Can be redownloaded after all" : "Can't redownload"}</button>
      </span></div>
    <div class="album-body">
      <div><b>${num(a.broken.length)} of ${num(a.tracks)}</b> tracks broken: <span class="sub">${Object.entries(a.problems).map(([p, n]) => `${esc(p)}${n > 1 ? ` (${num(n)})` : ""}`).join(", ")}</span></div>
      <div class="path">${esc(a.folder)}</div>
      ${a.checked ? `<div class="found">${a.status === "fixed" && !a.found.length && !a.gone.length ? `<div class="ok">All ${num(a.checked_files)} files play all the way through.</div>` : ""}
        ${a.found.map((f) => `<div class="${f.problem ? "bad" : "ok"}">${f.new ? `<span class="tag">new since the scan</span> ` : ""}${fileName(f.file)}: ${f.problem ? `<b>${esc(f.problem)}</b> <span class="sub">${esc(f.details)}</span>` : "plays all the way through"}</div>`).join("")}
        ${a.gone.map((f) => `<div class="bad"><span class="tag">gone</span> ${fileName(f)}</div>`).join("")}</div>` : ""}
      <details><summary>Broken tracks (${num(a.broken.length)})</summary><table>
        ${a.broken.map((t) => `<tr class="${rowClass(t)}"><td style="width:130px">${t.fixed ? `<span class="tag ok">plays now</span>` : `<span class="sev ${esc(t.severity.split(" ")[0])}">${esc(t.severity)}</span>`}
          ${t.removable && !t.fixed ? `<div>${decisionSelect(t)}</div>` : ""}</td>
          <td><div class="title">${esc(t.title)}</div>${where(t)}</td>
          <td style="width:38%">${t.problems.map((p) => `<div><b>${esc(p.problem)}</b>: <span class="sub">${esc(p.details)}</span></div>`).join("")}</td>
          <td>${playButtons(t)}</td></tr>`).join("")}</table></details>
    </div></div>`,

  check: (p) => `<div class="group"><div class="group-head"><b>Audio match ${pct(p.match)}</b> <span>sonic distance ${p.sonic.toFixed(3)}</span></div>
    <table>${[p.a, p.b].map((t) => `<tr><td>${playButtons(t)}</td><td>${nameCell(t)}</td><td class="num">${t.length}</td><td class="num">${fmt(t)}</td></tr>`).join("")}</table></div>`,
};

// decisions: change one, update the row and the totals
document.addEventListener("change", guard(async (e) => {
  const sel = e.target.closest("select.decision");
  if (!sel) return;
  const r = await api("/api/decide", { files: [sel.dataset.file], decision: sel.value });
  $$(`select.decision`).filter((s) => s.dataset.file === sel.dataset.file).forEach((s) => {
    s.value = sel.value;
    s.closest("tr").className = sel.value === "remove" ? "remove" : sel.value === "keep" ? "keep" : "";
  });
  STATE.marked = r.marked; renderMarked(r.marked);
}));

$("#btn-mark-suggested").onclick = guard(async () => {
  await loadList("identical");          // so the count matches the filters right now
  const total = views.identical.data ? views.identical.data.total : 0;
  if (!total) return toast("No groups match these filters.");
  if (!confirm(`Mark every suggested copy (all but the first in each group) for removal, in the ${num(total)} groups these filters show?\nCopies you've already decided are left alone. Nothing is moved yet.`)) return;
  const r = await api("/api/bulk", { tab: "identical", action: "suggested", filters: filtersOf("identical") });
  toast(`Marked ${num(r.changed)} copies for removal.`);
  renderMarked(r.marked); loadList("identical");
});
const clear = (name) => guard(async () => {
  if (!confirm("Clear every decision in what these filters show?")) return;
  const r = await api("/api/bulk", { tab: name, action: "clear", filters: filtersOf(name) });
  toast(`Cleared ${num(r.changed)} decisions.`); renderMarked(r.marked); loadList(name);
});
$("#btn-clear-identical").onclick = clear("identical");
$("#btn-mark-problems").onclick = guard(async () => {
  const f = filtersOf("unanalysed");
  if (!confirm("Mark every problem file in what this list shows for removal?\nFiles you've already decided are left alone. Nothing is moved yet.")) return;
  const r = await api("/api/bulk", { tab: "unanalysed", action: "problems", filters: f });
  toast(`Marked ${num(r.changed)} problem ${r.changed === 1 ? "file" : "files"} for removal.`);
  renderMarked(r.marked); loadList("unanalysed");
});
$("#btn-clear-versions").onclick = clear("versions");

// listening and showing files
const player = $("#player");
let playingBtn = null;
document.addEventListener("click", guard(async (e) => {
  const b = e.target.closest(".play");
  if (b) {
    if (playingBtn === b && !player.paused) { player.pause(); b.classList.remove("playing"); playingBtn = null; return; }
    if (playingBtn) playingBtn.classList.remove("playing");
    playingBtn = b; b.classList.add("playing"); b.textContent = "… loading";
    player.src = `/api/audio?k=${b.dataset.k}&at=${b.dataset.at}&t=${encodeURIComponent(KEY)}`;
    try { await player.play(); } catch (err) { b.classList.remove("playing"); toast("Couldn't play this file.", true); }
    b.textContent = b.dataset.at === "start" ? "▶ start" : "▶ middle";
    return;
  }
  const r = e.target.closest(".reveal");
  if (r) await api("/api/reveal", { k: +r.dataset.k });
}));
player.addEventListener("ended", () => { if (playingBtn) playingBtn.classList.remove("playing"); playingBtn = null; });
player.addEventListener("error", () => { if (playingBtn) { playingBtn.classList.remove("playing"); playingBtn.textContent = playingBtn.dataset.at === "start" ? "▶ start" : "▶ middle"; } toast("Couldn't play this file (missing or damaged?).", true); });

// ------------------------------------------------------------------ move & undo

$("#opt-playlists").onchange = guard(async (e) => {
  await api("/api/settings", { update_playlists: e.target.checked });
  $("#plan").classList.add("hidden");
  toast(e.target.checked ? "Playlists and collections will be updated when you move." : "Playlists and collections won't be changed.");
});

$("#btn-plan").onclick = guard(async () => {
  const el = $("#plan");
  el.classList.remove("hidden");
  el.innerHTML = `<p class="hint">Working out the move${$("#opt-playlists").checked ? " and checking your playlists" : ""}…</p>`;
  const p = await api("/api/plan");
  const pe = p.playlist_edits || [];
  const plText = !p.update_playlists ? `<p class="hint">Playlists and collections won't be changed (the option above is off).</p>`
    : !pe.length ? `<p>None of these files are in a playlist or collection.</p>`
    : `<p><b>${num(pe.length)} playlist/collection ${pe.length === 1 ? "entry" : "entries"}</b> will be updated:</p>
       <div class="plan-list">${pe.map((x) => `<div>${esc(x.container)}: “${esc(x.old_title)}” → ${x.keeper_already_there
         ? "removed (the kept copy is already in this list)" : `“${esc(x.new_title)}” (the kept copy)`}</div>`).join("")}</div>`;
  el.innerHTML = `<p><b>${num(p.files)} files</b> (${size(p.bytes)}) would move to the holding folder.</p>
    ${p.problems.length ? `<p>Skipped, with the reason:</p><ul class="problems">${p.problems.slice(0, 50).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${p.items.length ? `<div class="plan-list">${p.items.map((i) => `<div>${esc(i.src)}<br>&nbsp;&nbsp;→ ${esc(i.dst)}</div>`).join("")}${p.files > p.items.length ? `<div>… and ${num(p.files - p.items.length)} more</div>` : ""}</div>` : ""}
    ${p.files ? plText : ""}
    <div class="row-buttons">${p.files ? `<button class="primary" id="btn-go-move">Move ${num(p.files)} files</button>` : ""}</div>`;
  const go = $("#btn-go-move");
  if (go) go.onclick = guard(async () => {
    const extra = p.update_playlists && pe.length ? ` and update ${num(pe.length)} playlist entries` : "";
    if (!confirm(`Move ${num(p.files)} files (${size(p.bytes)}) to the holding folder${extra}? Everything can be put back from this page.`)) return;
    await startJob("/api/move", {});
  });
});

const when = (batch) => { const m = /^(\d{4})-(\d\d)-(\d\d)_(\d\d)(\d\d)/.exec(batch); return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : batch; };

async function loadBatches() {
  const [{ batches }, settings] = await Promise.all([api("/api/batches"), api("/api/settings")]);
  $("#opt-playlists").checked = settings.update_playlists;
  const el = $("#batches");
  if (!batches.length) { el.innerHTML = `<p class="hint">Nothing has been moved yet.</p>`; return; }
  el.innerHTML = batches.map((b) => `<div class="batch" data-batch="${esc(b.batch)}">
      <div class="batch-head"><span class="when">Moved ${esc(when(b.batch))}</span>
        <span>${num(b.held)} ${b.held === 1 ? "file" : "files"} in the holding folder (${size(b.held_bytes)})</span>
        ${b.restored ? `<span class="sub">${num(b.restored)} put back</span>` : ""}
        ${b.playlist_edits ? `<span class="sub">${num(b.playlist_edits)} playlist ${b.playlist_edits === 1 ? "entry" : "entries"} updated</span>` : ""}
        ${b.held ? `<button data-undo-all>Put back…</button>` : ""}
        ${b.playlist_pending ? `<button data-restore-pl title="Files are back but some playlist entries aren't yet">Restore ${num(b.playlist_pending)} playlist ${b.playlist_pending === 1 ? "entry" : "entries"}</button>` : ""}</div>
      <div class="some hidden">
        <label>Only put back files whose path contains <input data-match placeholder="e.g. an artist or album name"></label>
      </div>
      <div class="undo-plan"></div></div>`).join("");
  el.onclick = guard(async (e) => {
    const box = e.target.closest(".batch");
    if (!box) return;
    const batch = box.dataset.batch;
    if (e.target.matches("[data-restore-pl]")) return startJob("/api/restore-playlists", { batch });
    if (!e.target.matches("[data-undo-all], [data-preview]")) return;
    box.querySelector(".some").classList.remove("hidden");
    const match = box.querySelector("[data-match]").value.trim();
    const p = await api("/api/undo-plan", { batch, match });
    box.querySelector(".undo-plan").innerHTML = `<p><b>${num(p.files)} ${p.files === 1 ? "file" : "files"}</b> would go back to where
      ${p.files === 1 ? "it" : "they"} came from${match ? ` (only paths containing “${esc(match)}”)` : ""}${p.playlist_entries ?
      `, and ${num(p.playlist_entries)} playlist ${p.playlist_entries === 1 ? "entry" : "entries"} would be restored` : ""}.</p>
      ${p.problems.length ? `<ul class="problems">${p.problems.slice(0, 30).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      <div class="plan-list">${p.items.map((i) => `<div>${esc(i.original)}</div>`).join("")}</div>
      <div class="row-buttons"><button data-preview>Update the list</button>
        ${p.files ? `<button class="primary" data-go>Put ${num(p.files)} ${p.files === 1 ? "file" : "files"} back</button>` : ""}</div>`;
    const go = box.querySelector("[data-go]");
    if (go) go.onclick = guard(async (ev) => { ev.stopPropagation(); await startJob("/api/undo", { batch, match }); });
  });
}
$("#btn-rescan").onclick = guard(async () => { await api("/api/rescan-plex", {}); toast("Asked Plex to rescan."); });

// ------------------------------------------------------------------ settings

async function loadSettings() {
  const s = await api("/api/settings");
  const f = $("#settings-form");
  for (const [k, v] of Object.entries(s.rules)) {
    const el = f.elements[k];
    if (el) el.type === "checkbox" ? (el.checked = v) : (el.value = v);
  }
  for (const k of ["holding_dir", "workers", "ffmpeg", "fpcalc"]) f.elements[k].value = s[k];
  $("#holding-default").textContent = s.default_holding || "a _duplicates_holding folder at the top of the music's drive";
}
async function saveSettings() {
  const f = $("#settings-form");
  const rules = {};
  for (const k of ["identical_max_length_diff", "identical_max_loudness_diff", "same_audio", "maybe_audio"]) rules[k] = parseFloat(f.elements[k].value);
  rules.split_on_title_words = f.elements.split_on_title_words.checked;
  await api("/api/settings", { rules, holding_dir: f.elements.holding_dir.value, workers: +f.elements.workers.value,
    ffmpeg: f.elements.ffmpeg.value, fpcalc: f.elements.fpcalc.value });
  $("#settings-saved").textContent = "Saved.";
  setTimeout(() => ($("#settings-saved").textContent = ""), 3000);
}
$("#settings-form").onsubmit = guard(async (e) => { e.preventDefault(); await saveSettings(); });
$("#btn-save-resort").onclick = guard(async () => { await saveSettings(); showTab("overview"); await startJob("/api/scan", { quick: true }); });

// ------------------------------------------------------------------ setup

const inline = (sel, e) => ($(sel).innerHTML = `<p class="notice warn">${esc(e.message)}</p>`);

async function loadSetup() {
  await refreshState();
  $("#tools-status").innerHTML = (STATE.tools_ok ? "✓ " : "⚠ ") + esc(STATE.tools);
  loadSonic().catch((e) => inline("#sonic-status", e));   // needs no server
  if (!(STATE.job && STATE.job.running && STATE.job.kind.startsWith("Measuring"))) showScale(STATE.job).catch(() => {});
  if (!STATE.configured) {
    $("#s1-done").textContent = "";
    $("#libraries").innerHTML = $("#paths-status").innerHTML = `<p class="hint">Connect first (step 1).</p>`;
    return;
  }
  $("#s1-done").textContent = "checking…";
  const st = await api("/api/setup/status");
  $("#s1-done").textContent = st.ok ? "✓ connected to " + st.server : "";
  if (!st.ok) {
    const msg = `<p class="notice warn">${esc(st.message)}</p>`;
    $("#libraries").innerHTML = $("#paths-status").innerHTML = msg;
    return;
  }
  try {
    const libs = await api("/api/setup/libraries");
    $("#libraries").innerHTML = libs.libraries.map((l) => `<label class="check"><input type="checkbox" value="${esc(l.key)}"
      ${!libs.chosen.length || libs.chosen.includes(l.key) ? "checked" : ""}> ${esc(l.title)} <span class="sub">${esc(l.folders.join(", "))}</span></label>`).join("") +
      `<button id="btn-libs">Save libraries</button>`;
    $("#btn-libs").onclick = guard(async () => {
      await api("/api/setup/libraries", { keys: $$("#libraries input:checked").map((i) => i.value) });
      toast("Libraries saved."); loadPaths().catch((e) => inline("#paths-status", e));
    });
  } catch (e) { inline("#libraries", e); }
  loadPaths().catch((e) => inline("#paths-status", e));
}
async function loadPaths() {
  const p = await api("/api/setup/paths");
  $("#s3-done").textContent = p.found >= 0.8 ? "✓ " + pct(p.found) + " of sample tracks found" : "";
  $("#paths-status").innerHTML = `<p>Plex sees your files like this: <code>${esc(p.example)}</code><br>
    This computer can open <b>${pct(p.found)}</b> of a sample.${p.maps.length ? " Mapping: " + p.maps.map((m) => `<code>${esc(m.plex)}</code> = <code>${esc(m.local)}</code>`).join(", ") : ""}</p>`;
}
async function loadSonic() {
  const s = await api("/api/setup/sonic");
  $("#s4-done").textContent = s.current ? "✓ found" : "";
  $("#sonic-status").innerHTML = s.current ? `<p>Using <code>${esc(s.current)}</code></p>` :
    s.found_here ? `<p>Found on this computer: <code>${esc(s.found_here)}</code> <button id="btn-use-found">Use it</button></p>` :
      `<p>Not set: the slower network method will be used.</p>`;
  const use = $("#btn-use-found");
  if (use) use.onclick = guard(async () => { await api("/api/setup/sonic", { path: s.found_here }); loadSonic(); });
}
$("#btn-pin").onclick = guard(async () => {
  const { code } = await api("/api/setup/pin", {});
  const box = $("#pin-box");
  box.classList.remove("hidden");
  box.innerHTML = `<p>Go to <a href="https://plex.tv/link" target="_blank" rel="noopener"><b>plex.tv/link</b></a> and enter this code:</p>
    <p style="font-size:30px;font-weight:700;letter-spacing:4px">${esc(code)}</p><p class="hint" id="pin-wait">Waiting for you to sign in…</p>`;
  const timer = setInterval(guard(async () => {
    const st = await api("/api/setup/pin");
    if (st.status === "waiting") return;
    clearInterval(timer);
    if (st.status !== "signed in") { $("#pin-wait").textContent = "Sign-in didn't complete (" + st.status + "). Try again."; return; }
    box.innerHTML = `<p>Signed in. Which server?</p>` + st.servers.map((s, i) =>
      `<button data-server="${i}">${esc(s.name)}${s.owned ? "" : " (shared with you)"}</button> `).join("");
    box.onclick = guard(async (e) => {
      const i = e.target.dataset.server;
      if (i === undefined) return;
      box.innerHTML = `<p class="hint">Connecting…</p>`;
      const r = await api("/api/setup/server", { index: +i });
      box.innerHTML = `<p>✓ Connected to <b>${esc(r.server)}</b>.</p>`;
      loadSetup();
    });
  }), 2000);
});
$("#btn-manual").onclick = guard(async () => {
  const r = await api("/api/setup/manual", { url: $("#man-url").value, token: $("#man-token").value });
  toast("Connected to " + r.server); loadSetup();
});
$("#btn-paths").onclick = guard(async () => {
  const r = await api("/api/setup/paths", { local: $("#local-folder").value });
  if (!r.ok) return toast(r.message, true);
  toast(`Found ${pct(r.found)} of the sample tracks.`); loadPaths();
});
$("#btn-sonic").onclick = guard(async () => {
  const r = await api("/api/setup/sonic", { path: $("#sonic-folder").value });
  if (!r.ok) return toast(r.message, true);
  toast("Found Plex's sonic analysis."); loadSonic();
});
$("#btn-sonic-network").onclick = guard(async () => { await api("/api/setup/sonic", { path: "" }); loadSonic(); });
$("#btn-measure").onclick = guard(async () => { $("#scale-status").textContent = "Measuring…"; await startJob("/api/setup/measure", {}); });

async function showScale(job) {
  if (job && job.kind.startsWith("Measuring") && !job.running && (job.error || !(job.result && job.result.scale))) {
    $("#scale-status").textContent = job.error ? "Couldn't measure: " + job.error
      : !STATE.sonic_dir ? "Measuring needs Plex's sonic analysis folder (step 4); using the default 1.08."
      : "Couldn't measure it (Plex returned too few similar tracks); using the default 1.08.";
    return;
  }
  const s = await api("/api/settings");
  $("#scale-status").textContent = s.scale_measured
    ? `✓ Measured on your library: ${s.plex_scale.toFixed(3)}`
    : `Not measured yet; using the default ${s.plex_scale.toFixed(2)}.`;
}

// ------------------------------------------------------------------ start

guard(async () => {
  if (!KEY) { document.body.innerHTML = `<main><div class="notice warn">Open the app from the link it printed when it started (it includes a private key).</div></main>`; return; }
  await refreshState();
  if (!STATE.configured) showTab("setup");
})();
