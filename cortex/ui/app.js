"use strict";
/* Cortex UI 2.0: vanilla JS, no build step, no network calls beyond this machine.
   Layout: icon rail · explorer (folders or categories) · tabbed main view · context panel. */

const $ = (s, el = document) => el.querySelector(s);
const view = $("#view"), crumbs = $("#crumbs"), actions = $("#topActions"), ctx = $("#context"), app = $("#app");
const state = { tree: [], meta: [], titles: new Map(), current: null, dirty: false, mode: "folders", tabs: [], nodes: new Map() };


/* ── api ───────────────────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-Cortex": "1", ...(opts.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.error || res.statusText); e.status = res.status; throw e; }
  return data;
}
const get = (p) => api(p);
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body) });

/* ── utils ─────────────────────────────────────────── */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = (p) => p.split("/").map(encodeURIComponent).join("/");
const dec = (p) => p.split("/").map(decodeURIComponent).join("/");
function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 604800) return `${Math.floor(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}
const AGENT_COLORS = { claude: "--claude", "claude-desktop": "--claude", codex: "--codex", gemini: "--gemini", cursor: "--cursor", you: "--you", human: "--you" };
function agentChip(a) {
  const v = AGENT_COLORS[a] || null;
  const style = v ? `--c: var(${v})` : `--c: hsl(${[...a].reduce((h, c) => h + c.charCodeAt(0) * 37, 0) % 360} 55% 50%)`;
  return `<span class="agent" style="${style}">${esc(a === "human" ? "you" : a)}</span>`;
}
let toastTimer;
/* toast(msg, err, action?) where action = {label, run}: e.g. an Undo button for a change just made */
function toast(msg, err = false, action = null) {
  const t = $("#toast"), b = $("#toastAct");
  $("#toastText").textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  b.hidden = !action;
  if (action) { b.textContent = action.label; b.onclick = () => { t.className = "toast"; action.run(); }; }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), action ? 6000 : 2400);
}
async function undoChange(id) {
  try {
    const r = await post("/api/undo", id ? { changeset: id } : {});
    const msg = r.reverted.length ? `Undid ${r.reverted.length} change${r.reverted.length === 1 ? "" : "s"} by ${r.actor === "human" ? "you" : r.actor}` : "Nothing could be undone";
    toast(r.skipped.length ? `${msg} · ${r.skipped.length} left alone (edited since)` : msg, false,
      r.changeset ? { label: "Redo", run: () => undoChange(r.changeset) } : null);
    await loadSidebar(); route(true);
  } catch (e) { toast(e.message, true); }
}

/* Only http(s) links may ever be clickable: a "javascript:" URL from a search result
   or an imported page would otherwise run code in the app when clicked. */
const safeUrl = (u) => { try { const x = new URL(u); return x.protocol === "https:" || x.protocol === "http:" ? x.href : "#"; } catch { return "#"; } };
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

/* ── markdown (safe: everything is escaped first) ─── */
function inline(s) {
  s = esc(s);
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = s.replace(/\[([\w-]+)::\s*([^\]]*)\]/g, (_, k, v) => `<span class="field">${k} · ${v}</span>`);
  s = s.replace(/\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]/g, (_, target, label) => {
    const name = target.split(/[#^]/)[0].trim();
    const path = resolveLink(name);
    const cls = path ? "wl" : "wl missing";
    const href = path ? `#/note/${enc(path)}` : `#/new/${enc(name)}`;
    return `<a class="${cls}" href="${href}">${label || target}</a>`;
  });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
    const u = url.replace(/&amp;/g, "&");
    if (/^(https?:|mailto:)/i.test(u) && safeUrl(u) !== "#") return `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    if (/^[\w./ %-]+\.md$/i.test(u)) return `<a class="wl" href="#/note/${enc(decodeURIComponent(u))}">${text}</a>`;
    return text;
  });
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>").replace(/(^|[^\w])_([^_\s][^_]*)_(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>").replace(/==([^=]+)==/g, "<mark>$1</mark>");
  s = s.replace(/(^|\s)#([A-Za-z][\w/-]*)/g, '$1<a class="tag" href="#/tag/$2">#$2</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}
function markdown(src) {
  src = src.replace(/^---\n[\s\S]*?\n---\n?/, "");
  const lines = src.replace(/\r/g, "").split("\n");
  let html = "", i = 0;
  const flushPara = (buf) => buf.length && (html += `<p>${buf.map(inline).join("<br>")}</p>`);
  let para = [];
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) {
      flushPara(para); para = [];
      const code = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      html += `<pre><code>${esc(code.join("\n"))}</code></pre>`; i++; continue;
    }
    let m;
    if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { flushPara(para); para = []; html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; i++; continue; }
    if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(l)) { flushPara(para); para = []; html += "<hr>"; i++; continue; }
    if (/^>\s?/.test(l)) {
      flushPara(para); para = [];
      const q = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) q.push(lines[i++].replace(/^>\s?/, ""));
      html += `<blockquote>${markdown(q.join("\n"))}</blockquote>`; continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
      flushPara(para); para = [];
      const ordered = /^\s*\d/.test(l);
      html += ordered ? "<ol>" : "<ul>";
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        let item = lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "");
        const task = item.match(/^\[([ xX])\]\s+(.*)$/);
        html += task
          ? `<li class="task"><input type="checkbox" data-task="${esc(task[2].trim())}" aria-label="Done" ${task[1] !== " " ? "checked" : ""}>${inline(task[2])}</li>`
          : `<li>${inline(item)}</li>`;
        i++;
      }
      html += ordered ? "</ol>" : "</ul>"; continue;
    }
    if (/^\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flushPara(para); para = [];
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      html += "<table><thead><tr>" + cells(l).map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>";
      i += 2;
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) html += "<tr>" + cells(lines[i++]).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>";
      html += "</tbody></table>"; continue;
    }
    if (!l.trim()) { flushPara(para); para = []; i++; continue; }
    para.push(l); i++;
  }
  flushPara(para);
  return html;
}
function resolveLink(name) {
  const n = name.toLowerCase();
  for (const t of state.tree) {
    const p = t.path.toLowerCase();
    if (p === n + ".md" || p === n || p.endsWith("/" + n + ".md") || t.title.toLowerCase() === n) return t.path;
  }
  return null;
}
/* ── explorer: folders or categories, rendered lazily (a big Conversations/ folder stays cheap) ── */
const CAT_VARS = ["--d1", "--d2", "--d3", "--d4", "--d5", "--d6", "--d7", "--d8", "--d9"];
const hue = (s) => [...String(s)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7);
const catColor = (c) => (c ? `var(${CAT_VARS[hue(c) % CAT_VARS.length]})` : "var(--faint)");
const icon = (id, cls = "") => `<svg class="${cls}"><use href="#i-${id}"/></svg>`;

async function loadSidebar() {
  const [meta, stats] = await Promise.all([get("/api/notes_meta"), get("/api/stats")]);
  state.meta = meta;
  state.tree = meta;
  drawTree();
  const s = stats.semantic;
  $("#sideFoot").innerHTML = `<div class="vault" title="${esc(stats.vault)}">${esc(stats.vault.split(/[\\/]/).pop())} · <span class="mono">${stats.notes.toLocaleString()}</span> notes</div>
    <div class="sem ${s.on ? "on" : ""}" title="${esc(s.hint || "")}">${s.on ? `Meaning search (${s.indexed}/${s.total})` : "Keyword search"}</div>`;
  $("#taskCount").textContent = stats.open_tasks ? (stats.open_tasks > 99 ? "99+" : stats.open_tasks) : "";
  markActive();
  return stats;
}

function treeModel() {
  const root = { dirs: {}, files: [], key: "" };
  if (state.mode === "categories") {
    for (const n of state.meta) {
      if (n.path.startsWith("Conversations/") || n.path.startsWith("Topics/")) continue;
      const c = n.category || "Uncategorized";
      (root.dirs[c] ||= { dirs: {}, files: [], key: "cat:" + c, cat: n.category || "" }).files.push(n);
    }
    for (const d of Object.values(root.dirs)) d.files.sort((a, b) => a.title.localeCompare(b.title));
    return root;
  }
  for (const n of state.meta) {
    const parts = n.path.split("/");
    let node = root, key = "";
    for (const d of parts.slice(0, -1)) { key += d + "/"; node = node.dirs[d] ||= { dirs: {}, files: [], key }; }
    node.files.push(n);
  }
  return root;
}
function drawTree() {
  const open = new Set(JSON.parse(localStorageGet("cortex.open") || "[]"));
  state.nodes = new Map();
  const count = (node) => node.files.length + Object.values(node.dirs).reduce((a, d) => a + count(d), 0);
  const fileRow = (f) => `<a href="#/note/${enc(f.path)}" data-path="${esc(f.path)}" title="${esc(f.path)}">${f.category && state.mode === "folders"
    ? `<i class="dot" style="--c:${catColor(f.category)}"></i>` : icon("file")}<span>${esc(f.title)}</span></a>`;
  const render = (node) => Object.keys(node.dirs).sort((a, b) => (a === "Uncategorized") - (b === "Uncategorized") || a.localeCompare(b)).map((d) => {
    const child = node.dirs[d];
    state.nodes.set(child.key, child);
    const isOpen = open.has(child.key);
    const lead = child.cat !== undefined ? `<i class="dot" style="--c:${catColor(child.cat)}"></i>` : "";
    return `<details data-key="${esc(child.key)}" ${isOpen ? "open" : ""}><summary>${icon("chev", "chev")}${lead}${esc(d)}<span class="count">${count(child)}</span></summary>
      <div class="children">${isOpen ? render(child) : ""}</div></details>`;
  }).join("") + node.files.slice(0, 400).map(fileRow).join("") + (node.files.length > 400 ? `<div class="p" style="padding:4px 8px">…and ${node.files.length - 400} more (search to find them)</div>` : "");
  const root = treeModel();
  $("#tree").innerHTML = render(root) || `<div class="empty">No notes yet. Tell Cortex something above.</div>`;
  state.open = open;
  state.render = render;
  markActive();
}
$("#tree").addEventListener("toggle", (e) => {
  const d = e.target;
  if (d.tagName !== "DETAILS" || !state.open) return;
  d.open ? state.open.add(d.dataset.key) : state.open.delete(d.dataset.key);
  localStorageSet("cortex.open", JSON.stringify([...state.open]));
  const box = d.querySelector(":scope > .children");
  if (d.open && !box.childElementCount) box.innerHTML = state.render(state.nodes.get(d.dataset.key));
  markActive();
}, true);
function markActive() {
  document.querySelectorAll(".tree a").forEach((a) => a.classList.toggle("active", a.dataset.path === state.current));
  const v = location.hash.split("/")[1] || "";
  document.querySelectorAll(".rail a[data-view]").forEach((a) => a.classList.toggle("active",
    (a.dataset.view === "home" && (v === "" || v === "note" || v === "edit" || v === "new" || v === "history" || v === "tag")) || a.dataset.view === v));
}
function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

/* ── tabs ──────────────────────────────────────────── */
const VIEW_NAMES = { "": "Home", graph: "Graph", tasks: "Tasks", topics: "Topics", conversations: "Conversations", changes: "Changes",
  activity: "Activity", research: "Research", health: "Health", sync: "Sync", settings: "Settings", tag: "Tag" };
try { state.tabs = JSON.parse(localStorageGet("cortex.tabs") || "[]").filter((t) => t && t.hash).slice(0, 10); } catch { state.tabs = []; }
function touchTab(title) {
  const hash = location.hash || "#/";
  if (/^#\/(edit|new)/.test(hash)) return drawTabs();
  let t = state.tabs.find((x) => x.hash === hash);
  if (!t) {
    t = { hash, title };
    state.tabs.push(t);
    while (state.tabs.length > 10) state.tabs.splice(state.tabs.findIndex((x) => x.hash !== hash), 1);
  }
  t.title = title;
  localStorageSet("cortex.tabs", JSON.stringify(state.tabs));
  drawTabs();
}
function drawTabs() {
  const cur = location.hash || "#/";
  $("#tabs").innerHTML = state.tabs.map((t, i) => `<div class="tab" role="tab" tabindex="0" data-i="${i}" aria-selected="${t.hash === cur}" title="${esc(t.title)}">
    <span>${esc(t.title)}</span><button aria-label="Close tab" data-close="${i}">${icon("x")}</button></div>`).join("");
}
$("#tabs").addEventListener("click", (e) => {
  const close = e.target.closest("[data-close]");
  if (close) {
    const i = +close.dataset.close, wasActive = state.tabs[i].hash === (location.hash || "#/");
    state.tabs.splice(i, 1);
    localStorageSet("cortex.tabs", JSON.stringify(state.tabs));
    if (wasActive) location.hash = state.tabs.length ? state.tabs[Math.max(0, i - 1)].hash : "#/";
    drawTabs();
    return;
  }
  const tab = e.target.closest(".tab");
  if (tab) location.hash = state.tabs[+tab.dataset.i].hash;
});
$("#tabs").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.classList.contains("tab")) e.target.click(); });

/* ── context panel ─────────────────────────────────── */
function setContext(html) {
  ctx.innerHTML = html || "";
  app.classList.toggle("has-context", !!html);
}

function activityRow(a) {
  const isNote = a.target && a.target.endsWith(".md") && a.action !== "deleted";
  const target = a.target ? (isNote ? `<a class="target" href="#/note/${enc(a.target)}">${esc(a.target)}</a>` : `<span class="target">${esc(a.target)}</span>`) : "";
  const main = a.note ? esc(a.note) : `${esc(a.action)} ${target}`;
  const sub = a.note && target ? `<div class="act-note">${target}</div>` : "";
  return `<div class="row">${agentChip(a.agent)}<div class="t"><div class="act-text">${main}</div>${sub}</div><span class="when" title="${esc(a.ts)}">${ago(a.ts)}</span></div>`;
}
function setupCard(st) {
  if (st.connected && st.import_history !== null && st.import_history !== undefined) return "";
  const apps = Object.entries(st.detected).filter(([, v]) => v).map(([k]) => ({ "claude-code": "Claude Code", codex: "Codex", gemini: "Gemini CLI", cursor: "Cursor" }[k]));
  return `<div class="setup">
    <h3>Finish setting up</h3>
    ${st.connected ? "" : `<div class="setup-step"><div><b>1. Connect your AI apps</b><p>Lets Claude Code, Codex, Gemini, Cursor and Claude Desktop use this brain. Cortex adds itself to each app's settings (a backup of each file is kept).</p><div id="connectOut"></div></div>
      <button class="btn primary" id="doConnect">Connect my AI apps</button></div>`}
    ${st.import_history === null || st.import_history === undefined ? `<div class="setup-step"><div><b>${st.connected ? "" : "2. "}Import your AI conversation history?</b>
      <p>Cortex can read the chat history your AI apps already keep on this PC${apps.length ? ` (found: ${apps.map(esc).join(", ")})` : ""}, remove passwords and keys, and save every conversation as searchable notes. Nothing is read until you say yes, and it stays on this computer unless you turn on cloud sync.</p></div>
      <div class="setup-btns"><button class="btn primary" id="importYes">Yes, import</button><button class="btn" id="importNo">No thanks</button></div></div>` : ""}
  </div>`;
}

function wireSetup() {
  const c = $("#doConnect");
  if (c) c.onclick = async () => {
    c.textContent = "Connecting…"; c.disabled = true;
    try {
      const res = await post("/api/connect", {});
      $("#connectOut").innerHTML = `<ul class="connect-res">${res.map((r) => `<li class="${r.ok ? "ok" : r.ok === false ? "bad" : ""}"><b>${esc(r.app)}</b>: ${esc(r.status)}</li>`).join("")}</ul><p class="p">Restart your AI apps so they pick up the brain.</p>`;
      c.remove();
    } catch (e) { toast(e.message, true); c.textContent = "Connect my AI apps"; c.disabled = false; }
  };
  const choose = async (yes) => {
    try {
      await post("/api/settings", { import_history: yes });
      if (yes) { toast("Importing your AI history…"); post("/api/import", {}).then((r) => { toast(`Imported ${r.new || 0} conversations`); loadSidebar(); }).catch(() => {}); }
      else toast("OK. You can turn it on later in Conversations");
      viewHome();
    } catch (e) { toast(e.message, true); }
  };
  if ($("#importYes")) { $("#importYes").onclick = () => choose(true); $("#importNo").onclick = () => choose(false); }
}

async function viewActivity(filter) {
  setTop("<b>AI activity</b>", "");
  const all = await get("/api/activity?limit=300");
  const agents = [...new Set(all.map((a) => a.agent))];
  const rows = filter ? all.filter((a) => a.agent === filter) : all;
  view.innerHTML = `
    <h1 class="hello">Shared logbook</h1>
    <p class="sub">Every change any AI (or you) makes to the brain, newest first.</p>
    <div class="filters">
      <button class="chip ${!filter ? "on" : ""}" data-a="">Everyone</button>
      ${agents.map((a) => `<button class="chip ${filter === a ? "on" : ""}" data-a="${esc(a)}">${esc(a === "human" ? "you" : a)}</button>`).join("")}
    </div>
    <div class="list">${rows.map(activityRow).join("") || `<div class="empty"><b>Nothing logged yet</b></div>`}</div>`;
  view.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => viewActivity(c.dataset.a || null)));
}
/* ── home ──────────────────────────────────────────── */
const who = (a) => (a === "human" ? "you" : a);
function csRow(c, withUndo = true) {
  const files = c.files.map((f) => f.path);
  const first = files[0] || "";
  const label = c.undo_of ? "Undo" : c.summary ? c.summary.replace(/^ingest /, "Filed a ") : (c.files.length === 1 ? (c.files[0].before ? "Changed" : "Created") : `Changed ${files.length} notes`);
  return `<div class="row cs-row ${c.undone ? "undone" : ""}">${agentChip(who(c.actor))}
    <div class="t"><div>${esc(label)} ${first ? `<a href="#/note/${enc(first)}">${esc(titleOf(first))}</a>` : ""}${files.length > 1 ? ` <span class="p" style="display:inline">+${files.length - 1}</span>` : ""}</div>
    <div class="cs-files">${esc(files.join(" · "))}</div></div>
    <span class="when" title="${esc(c.ts)}">${ago(c.ts)}</span>
    ${withUndo ? `<button class="btn" data-undo="${esc(c.id)}">${c.undone ? "Redo" : "Undo"}</button>` : ""}</div>`;
}
function wireUndo(root = view) {
  root.querySelectorAll("[data-undo]").forEach((b) => (b.onclick = async () => {
    const id = b.dataset.undo;
    if (b.textContent === "Redo") {  // redo = undo the undo that reverted it
      const sets = await get("/api/changes?limit=400");
      const u = sets.find((c) => c.undo_of === id && !c.undone);
      return undoChange(u ? u.id : id);
    }
    undoChange(id);
  }));
}

async function viewHome() {
  setTop("<b>Home</b>", `<button class="btn primary" id="homeNew">New note</button>`);
  $("#homeNew").onclick = () => newNote();
  const [stats, recent, acts, setup, changes, cats] = await Promise.all([get("/api/stats"), get("/api/recent?limit=10"),
    get("/api/activity?limit=10"), get("/api/setup"), get("/api/changes?limit=8"), get("/api/categories")]);
  const agents = new Set(acts.map((a) => a.agent).filter((a) => a !== "you" && a !== "human"));
  const hour = new Date().getHours();
  const greet = hour < 12 ? "Morning." : hour < 18 ? "Afternoon." : "Evening.";
  view.innerHTML = `
    <h1 class="hello">${greet}</h1>
    <p class="sub">Remembers. Locally. Every AI you connect reads and writes this one brain.</p>
    ${setupCard(setup)}
    <div class="stats">
      <div class="stat"><div class="n">${stats.notes.toLocaleString()}</div><div class="l">notes</div></div>
      <a class="stat" href="#/tasks"><div class="n">${stats.open_tasks.toLocaleString()}</div><div class="l">open tasks</div></a>
      <a class="stat" href="#/graph"><div class="n">${stats.links.toLocaleString()}</div><div class="l">links</div></a>
      <a class="stat" href="#/conversations"><div class="n">${stats.conversations.toLocaleString()}</div><div class="l">AI conversations</div></a>
      <a class="stat" href="#/activity"><div class="n">${agents.size}</div><div class="l">AIs active lately</div></a>
    </div>
    <div class="cols">
      <div>
        <h3 class="panel-title">Recently changed</h3>
        <div class="list">${recent.map((r) => `<a class="row" href="#/note/${enc(r.path)}"><div class="t"><div>${esc(r.title)}</div><div class="p">${esc(r.path)}</div></div><span class="when">${ago(r.modified)}</span></a>`).join("") || `<div class="empty"><b>No notes yet</b>Tell Cortex something in the box on the left.</div>`}</div>
        <h3 class="panel-title">Categories <a href="#/settings">Rules</a></h3>
        <div class="filters">${cats.map((c) => `<span class="pill"><i class="dot" style="--c:${catColor(c.category)};width:7px;height:7px;border-radius:50%;background:var(--c)"></i>${esc(c.category)} <span class="mono">${c.count}</span></span>`).join("") || `<span class="p">Notes get a category as they come in.</span>`}</div>
      </div>
      <div>
        <h3 class="panel-title">Latest changes <a href="#/changes">All, with undo</a></h3>
        <div class="list">${changes.map((c) => csRow(c)).join("") || `<div class="empty"><b>Nothing changed yet</b>Every change shows up here and can be undone.</div>`}</div>
        <h3 class="panel-title">AI activity <a href="#/activity">See all</a></h3>
        <div class="list">${acts.map(activityRow).join("") || `<div class="empty"><b>Quiet so far</b>Connect your AI apps, then ask one to remember something.</div>`}</div>
      </div>
    </div>`;
  wireSetup();
  wireUndo();
  setContext("");
  touchTab("Home");
}

/* ── note ──────────────────────────────────────────── */
async function viewNote(path) {
  let n;
  try { n = await get("/api/note?path=" + encodeURIComponent(path)); }
  catch (e) { view.innerHTML = `<div class="empty"><b>Note not found</b>${esc(path)}</div>`; setTop(esc(path), ""); setContext(""); return; }
  state.current = n.path;
  markActive();
  const parts = n.path.split("/");
  setTop(parts.slice(0, -1).map(esc).join(" / ") + (parts.length > 1 ? " / " : "") + `<b>${esc(n.title)}</b>`,
    `<a class="btn" href="#/graph/${enc(n.path)}">Local graph</a><button class="btn" id="renBtn">Rename</button><button class="btn primary" id="editBtn">Edit</button>`);
  $("#editBtn").onclick = () => (location.hash = "#/edit/" + enc(n.path));
  $("#renBtn").onclick = () => renameNote(n.path);
  const body = markdown(n.content);
  const hasH1 = /^<h1>/.test(body.trim());
  const links = n.links.filter((l) => l.path);
  const meta = state.meta.find((m) => m.path === n.path) || {};
  view.innerHTML = `
    <article>
      <div class="note-meta">${meta.category ? `<span class="pill"><i class="dot" style="--c:${catColor(meta.category)};width:7px;height:7px;border-radius:50%;background:var(--c)"></i>${esc(meta.category)}</span>` : ""}
        ${n.tags.map((t) => `<a class="tag" href="#/tag/${encodeURIComponent(t)}">#${esc(t)}</a>`).join("")}<span>Edited ${ago(n.modified)}</span></div>
      <div class="md">${hasH1 ? "" : `<h1>${esc(n.title)}</h1>`}${body}</div>
    </article>`;
  const heads = [...n.content.replace(/```[\s\S]*?```/g, "").matchAll(/^(#{1,4})\s+(.+)$/gm)].slice(0, 40);
  setContext(`
    ${heads.length > 1 ? `<h4>Outline</h4><div class="outline">${heads.map((h) => `<button class="ln" data-h="${esc(h[2])}" style="padding-left:${(h[1].length - 1) * 12 + 8}px">${esc(h[2].replace(/\[\[|\]\]/g, ""))}</button>`).join("")}</div>` : ""}
    <h4>Linked from <span class="mono">${n.backlinks.length}</span></h4>
    ${n.backlinks.map((b) => `<a class="ln" href="#/note/${enc(b)}">${esc(titleOf(b))}</a>`).join("") || `<div class="none">Nothing links here yet</div>`}
    <h4>Links to <span class="mono">${links.length}</span></h4>
    ${links.map((l) => `<a class="ln" href="#/note/${enc(l.path)}">${esc(titleOf(l.path))}</a>`).join("") || `<div class="none">No outgoing links</div>`}
    <h4>Related, not linked</h4><div id="related"><div class="none">Looking…</div></div>
    <h4>Versions <span class="mono">${n.versions}</span></h4>
    ${n.versions ? `<a class="btn" href="#/history/${enc(n.path)}">Browse and restore</a>` : `<div class="none">Each change through Cortex keeps the previous version.</div>`}
    <div id="noteChanges"></div>
    <h4>File</h4><div class="none mono" style="word-break:break-all;color:var(--faint)">${esc(n.path)}</div>`);
  ctx.querySelectorAll("[data-h]").forEach((a) => (a.onclick = () => {
    const h = [...view.querySelectorAll(".md h1, .md h2, .md h3, .md h4")].find((x) => x.textContent.trim() === a.dataset.h.replace(/\[\[|\]\]/g, "").trim());
    if (h) h.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  view.querySelectorAll("input[data-task]").forEach((cb) => cb.addEventListener("change", async () => {
    try {
      await post("/api/task/toggle", { path: n.path, line: -1, text: cb.dataset.task });
      toast(cb.checked ? "Done" : "Reopened");
      loadSidebar();
    } catch (e) { cb.checked = !cb.checked; toast(e.message, true); }
  }));
  get("/api/related?limit=5&path=" + encodeURIComponent(n.path)).then((rel) => {
    const box = $("#related");
    if (box) box.innerHTML = rel.map((h) => `<a class="ln" href="#/note/${enc(h.path)}" title="${esc(h.snippet)}">${esc(h.title)}</a>`).join("") || `<div class="none">Nothing similar</div>`;
  }).catch(() => {});
  get("/api/changes?limit=200").then((sets) => {
    const mine = sets.filter((c) => c.files.some((f) => f.path === n.path)).slice(0, 4);
    const box = $("#noteChanges");
    if (box && mine.length) {
      box.innerHTML = `<h4>Recent changes</h4>${mine.map((c) => `<div class="mini-cs ${c.undone ? "undone" : ""}">${agentChip(who(c.actor))}<span class="when">${ago(c.ts)}</span>
        <button class="btn" data-undo="${esc(c.id)}">${c.undone ? "Redo" : "Undo"}</button></div>`).join("")}`;
      wireUndo(box);
    }
  }).catch(() => {});
  touchTab(n.title);
}

/* ── tasks: a board ────────────────────────────────── */
async function viewTasks(status = "open") {
  setTop("<b>Tasks</b>", `<button class="chip ${status === "open" ? "on" : ""}" data-s="open">Open</button><button class="chip ${status === "done" ? "on" : ""}" data-s="done">Completed</button>`);
  const tasks = await get("/api/tasks?status=" + status);
  const today = new Date().toISOString().slice(0, 10);
  const groups = status === "open" ? [
    ["Overdue", tasks.filter((t) => t.overdue)],
    ["Today", tasks.filter((t) => t.due === today)],
    ["Upcoming", tasks.filter((t) => t.due && t.due > today)],
    ["Someday", tasks.filter((t) => !t.due)],
  ] : [["Completed", tasks]];
  const row = (t) => `<div class="row task-row ${t.overdue ? "overdue" : ""}">
      <input type="checkbox" ${t.done ? "checked" : ""} data-path="${esc(t.path)}" data-line="${t.line}" data-text="${esc(t.text)}" aria-label="Mark done">
      <div class="t"><div>${inline(t.text.replace(/\s*(📅\s*|due:\s*|@)\d{4}-\d{2}-\d{2}/, ""))}</div><a class="p" href="#/note/${enc(t.path)}">${esc(t.title)}</a></div>
      ${t.due ? `<span class="due">${t.due === today ? "today" : esc(t.due)}</span>` : ""}</div>`;
  view.innerHTML = `
    <h1 class="hello">Tasks</h1>
    <p class="sub">Every checkbox in the brain, from any note. Cortex also finds tasks in what you tell it: “remind me to call the bank tomorrow”, “jangan lupa bayar bil esok”.</p>
    <form class="task-add" id="taskForm" autocomplete="off">
      <input id="taskText" placeholder="Add a task…" aria-label="New task" required>
      <input id="taskDue" type="date" aria-label="Due date">
      <button class="btn primary">Add</button>
    </form>
    ${tasks.length ? `<div class="board">${groups.filter(([, l]) => l.length).map(([name, list]) => `<div class="col"><h3>${name} <span>${list.length}</span></h3><div class="list">${list.slice(0, 200).map(row).join("")}</div></div>`).join("")}</div>`
      : `<div class="list"><div class="empty"><b>${status === "open" ? "Nothing to do" : "Nothing completed yet"}</b>${status === "open" ? "Add one above, or write <code>- [ ] something</code> in any note." : ""}</div></div>`}`;
  actions.querySelectorAll(".chip").forEach((c) => (c.onclick = () => viewTasks(c.dataset.s)));
  view.querySelectorAll(".task-row input").forEach((cb) => cb.addEventListener("change", async () => {
    try {
      await post("/api/task/toggle", { path: cb.dataset.path, line: +cb.dataset.line, text: cb.dataset.text });
      cb.closest(".row").classList.add("leaving");
      setTimeout(() => viewTasks(status), 250);
      loadSidebar();
    } catch (e) { cb.checked = !cb.checked; toast(e.message, true); }
  }));
  $("#taskForm").onsubmit = async (e) => {
    e.preventDefault();
    try { await post("/api/task", { text: $("#taskText").value, due: $("#taskDue").value }); toast("Task added to today's note"); loadSidebar(); viewTasks("open"); }
    catch (err) { toast(err.message, true); }
  };
  setContext("");
  touchTab("Tasks");
}

/* ── topics ────────────────────────────────────────── */
async function viewTopics() {
  setTop("<b>Topics</b>", `<button class="btn" id="refreshTopics">Group new chats</button>`);
  const topics = await get("/api/topics");
  view.innerHTML = `
    <h1 class="hello">Topics</h1>
    <p class="sub">Your AI conversations, grouped by what they're about. Each topic is a note with a summary, the decisions and action items mentioned, and links to every chat. Grouping uses word statistics, not an AI.</p>
    ${topics.length ? `<div class="topic-grid">${topics.map((t) => `<a class="card topic" href="#/note/${enc(t.note)}">
      <h3>${esc(t.name)}</h3><div class="p"><span class="mono">${t.conversations}</span> conversation${t.conversations === 1 ? "" : "s"}</div>
      <div class="terms">${t.terms.slice(0, 6).map((w) => `<span class="pill">${esc(w)}</span>`).join("")}</div></a>`).join("")}</div>`
      : `<div class="list"><div class="empty"><b>No topics yet</b>Import your AI history (Chats), or let COre store conversations; they're grouped as they arrive.</div></div>`}`;
  $("#refreshTopics").onclick = async () => {
    $("#refreshTopics").textContent = "Grouping…";
    try { await post("/api/topics/refresh", {}); toast("Topics updated"); await loadSidebar(); viewTopics(); }
    catch (e) { toast(e.message, true); }
  };
  setContext("");
  touchTab("Topics");
}

/* ── changes: every change-set, undoable ──────────── */
async function viewChanges(actor = "") {
  setTop("<b>Changes</b>", `<button class="btn" id="undoLast">Undo latest</button>`);
  const all = await get("/api/changes?limit=300");
  const actors = [...new Set(all.map((c) => c.actor.split("/")[0]))];
  const rows = actor ? all.filter((c) => c.actor === actor || c.actor.startsWith(actor + "/")) : all;
  view.innerHTML = `
    <h1 class="hello">Changes</h1>
    <p class="sub">Every change made through Cortex, by you, COre or any AI, grouped the way it happened. Undo puts back exactly what was there. A note edited again since is left alone.</p>
    <div class="filters"><button class="chip ${!actor ? "on" : ""}" data-a="">Everyone</button>
      ${actors.map((a) => `<button class="chip ${actor === a ? "on" : ""}" data-a="${esc(a)}">${esc(who(a))}</button>`).join("")}</div>
    <div class="list">${rows.map((c) => csRow(c)).join("") || `<div class="empty"><b>No changes yet</b></div>`}</div>`;
  view.querySelectorAll(".chip").forEach((c) => (c.onclick = () => viewChanges(c.dataset.a)));
  $("#undoLast").onclick = () => undoChange(null);
  wireUndo();
  setContext("");
  touchTab("Changes");
}

/* ── settings: organizing rules ────────────────────── */
async function viewSettings() {
  setTop("<b>Settings</b>", `<button class="btn primary" id="saveRules">Save</button>`);
  const [rules, setup, capture, gw] = await Promise.all([get("/api/rules"), get("/api/setup"), get("/api/capture-rules"), get("/api/gateway")]);
  const st = rules.settings;
  const ruleRow = (r = { match: {} }) => `<tr>
    <td><input data-f="folder" value="${esc(r.match.folder || "")}" placeholder="Clients/"></td>
    <td><input data-f="keywords" value="${esc((r.match.keywords || []).join(", "))}" placeholder="acme, invoice"></td>
    <td><input data-f="regex" value="${esc(r.match.regex || "")}" placeholder="optional"></td>
    <td><input data-f="category" value="${esc(r.category || "")}" placeholder="Clients"></td>
    <td><input data-f="target" value="${esc(r.folder || "")}" placeholder="Clients"></td>
    <td><button class="icon-btn" data-del aria-label="Remove rule">${icon("x")}</button></td></tr>`;
  view.innerHTML = `
    <h1 class="hello">Settings</h1>
    <p class="sub">How Cortex organizes what comes in. Your rules win. After that, keyword lists (English and Malay) decide, and then how similar notes are already filed.</p>
    <h3 class="panel-title">Organizing</h3>
    <div class="list">
      <div class="set-row"><div><b>File long notes into folders</b><p>Without this, new long notes go to Inbox/ and Cortex only suggests a folder.</p></div>
        <label class="switch"><input type="checkbox" id="autoFile" ${st.auto_file ? "checked" : ""}><i></i></label></div>
      <div class="set-row"><div><b>Make notes for names that keep coming up</b><p>A person or project mentioned in ${st.stub_after} separate captures gets a short note in Entities/, so links have somewhere to go.</p></div>
        <label class="switch"><input type="checkbox" id="stubs" ${st.stubs ? "checked" : ""}><i></i></label></div>
      <div class="set-row"><div><b>Import my AI history</b><p>Read the chats your AI apps keep on this PC (passwords and keys removed). Off until you turn it on.</p></div>
        <label class="switch"><input type="checkbox" id="importHist" ${setup.import_history ? "checked" : ""}><i></i></label></div>
    </div>
    <h3 class="panel-title">Category rules <span>${rules.rules.length}</span></h3>
    <div class="card" style="padding:6px 10px">
      <table class="rules"><thead><tr><th>In folder</th><th>Any of these words</th><th>Pattern</th><th>Category</th><th>File into</th><th></th></tr></thead>
      <tbody id="ruleBody">${rules.rules.map(ruleRow).join("")}</tbody></table>
      <button class="btn" id="addRule" style="margin:8px 0 6px">Add rule</button>
    </div>
    <p class="why">Stored in <code>.cortex/rules.json</code> inside the brain folder, so every computer that shares the brain uses the same rules.</p>
    <h3 class="panel-title">Capture <span>${gw.queue.delivered} archived · ${gw.queue.pending} waiting · ${gw.queue.dead} failed</span></h3>
    <div class="list">${gw.adapters.filter((a) => a.provider !== "*").map((a) => {
      const c = capture.providers[a.provider] || {};
      return `<div class="set-row"><div><b>${esc({ "claude-code": "Claude Code", codex: "Codex", "gemini-cli": "Gemini CLI", cursor: "Cursor", chatgpt: "ChatGPT", "claude-ai": "Claude.ai", gemini: "Gemini (web)" }[a.provider] || a.provider)}</b>
        <span class="pill ${a.tested === "live" ? "ok" : ""}">${a.tested === "live" ? "tested live" : "sample-tested"}</span>
        <p>${esc(a.modes.join(", "))} · real time: ${esc(a.realtime)}</p>
        <p>Keep for <input class="ret" data-p="${esc(a.provider)}" type="number" min="1" placeholder="forever" value="${c.retention_days || ""}" style="width:90px;padding:3px 6px"> days</p></div>
        <label class="switch"><input type="checkbox" class="cap" data-p="${esc(a.provider)}" ${c.enabled === false ? "" : "checked"}><i></i></label></div>`;
    }).join("")}
      <div class="set-row"><div><b>Never capture these projects</b><p>One pattern per line, matched against the project folder, e.g. <code>*/client-*</code>.</p>
        <textarea class="field" id="capExclude" rows="2" style="width:100%;margin-top:6px">${esc(capture.projects.exclude.join("\n"))}</textarea></div><span></span></div>
      <div class="set-row"><div><b>Local apps and webhooks</b><p>${gw.rest_enabled ? "On: apps on this computer can send conversations to <code>POST /api/events</code> with the token." : "Off. Run <code>cortex gateway token</code> to let other apps on this computer send conversations."}</p></div><span></span></div>
    </div>
    <p class="why">Captured conversations are kept as they were said, with passwords and keys removed. Turning a provider off stops new capture; to delete what's stored, use <code>cortex gateway forget</code>.</p>
    <h3 class="panel-title">AI apps</h3>
    <div class="list"><div class="set-row"><div><b>Connect Claude Code, Codex, Gemini, Cursor and Claude Desktop</b><p>Cortex adds itself to each app's MCP settings and keeps a backup of each file.</p><div id="connectOut"></div></div>
      <button class="btn" id="doConnect">Connect</button></div></div>`;
  const body = $("#ruleBody");
  const wireDel = () => body.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => b.closest("tr").remove()));
  wireDel();
  $("#addRule").onclick = () => { body.insertAdjacentHTML("beforeend", ruleRow()); wireDel(); body.lastElementChild.querySelector("input").focus(); };
  $("#importHist").onchange = async (e) => { try { await post("/api/settings", { import_history: e.target.checked }); toast(e.target.checked ? "History import on" : "History import off"); } catch (err) { toast(err.message, true); } };
  $("#doConnect").onclick = async () => {
    $("#doConnect").disabled = true;
    try { const res = await post("/api/connect", {}); $("#connectOut").innerHTML = `<ul class="connect-res">${res.map((r) => `<li class="${r.ok ? "ok" : r.ok === false ? "bad" : ""}"><b>${esc(r.app)}</b>: ${esc(r.status)}</li>`).join("")}</ul>`; }
    catch (e) { toast(e.message, true); }
    $("#doConnect").disabled = false;
  };
  $("#saveRules").onclick = async () => {
    const list = [...body.querySelectorAll("tr")].map((tr) => {
      const v = (f) => tr.querySelector(`[data-f="${f}"]`).value.trim();
      return { match: { folder: v("folder"), keywords: v("keywords").split(",").map((s) => s.trim()).filter(Boolean), regex: v("regex") }, category: v("category"), folder: v("target") };
    }).filter((r) => r.category || r.match.folder || r.match.keywords.length || r.match.regex);
    try {
      await post("/api/rules", { rules: list, settings: { auto_file: $("#autoFile").checked, stubs: $("#stubs").checked, stub_after: st.stub_after } });
      const providers = {};
      view.querySelectorAll(".cap").forEach((c) => {
        const days = parseInt(view.querySelector(`.ret[data-p="${c.dataset.p}"]`).value, 10);
        providers[c.dataset.p] = { enabled: c.checked, ...(days > 0 ? { retention_days: days } : {}) };
      });
      await post("/api/capture-rules", { providers, projects: { exclude: $("#capExclude").value.split("\n").map((x) => x.trim()).filter(Boolean), include: capture.projects.include }, redact: capture.redact });
      toast("Saved. New and changed notes use these rules");
      await post("/api/reindex", {});
      loadSidebar();
    } catch (e) { toast(e.message, true); }
  };
  setContext("");
  touchTab("Settings");
}

/* ── health ────────────────────────────────────────── */
async function viewHealth() {
  setTop("<b>Health</b>", `<button class="btn" id="recheck">Check again</button>`);
  view.innerHTML = `<div class="empty">Checking your brain…</div>`;
  const h = await get("/api/health");
  $("#recheck").onclick = viewHealth;
  const note = (p) => `<a href="#/note/${enc(p)}">${esc(titleOf(p))}</a>`;
  const sections = [
    ["Sync conflicts", "Two devices edited the same note at once, so the sync app kept both. Merge the copy into the original, then delete the copy.", (h.sync_conflicts || []).map((c) => `${note(c.note)}${c.original ? ` <span class="p" style="display:inline">copy of</span> ${note(c.original)}` : ""}`)],
    ["Broken links", "A [[link]] points to a note that doesn't exist.", h.broken_links.map((b) => `${note(b.note)} → <code>[[${esc(b.link)}]]</code> <a class="fix" href="#/new/${enc(b.link)}">create it</a>`)],
    ["Possible duplicates", "Notes with the same name or title. Merge them so AIs don't split facts.", h.duplicates.map((d) => d.map(note).join(" · "))],
    ["Decisions that may have changed", "Two decisions in one note about the same thing that say different things. Keep the current one and mark the old one superseded.", (h.decisions_to_review || []).map((d) => `${note(d.note)}: <span class="p" style="display:inline">“${esc(d.earlier)}”</span> → “${esc(d.later)}”`)],
    ["Stale projects", "Marked active but untouched for 60+ days.", h.stale.map((s) => `${note(s.note)} <span class="p" style="display:inline">${s.days} days</span>`)],
    ["Overdue tasks", "", h.overdue_tasks.map((t) => `${esc(t.text)} <span class="p" style="display:inline">due ${esc(t.due)} · </span>${note(t.path)}`)],
    ["Empty notes", "", h.empty.map(note)],
    ["Orphans", "No links in or out. Link them so they surface in context.", h.orphans.map(note)],
    ["Very long notes", "Long notes cost AIs more tokens. Consider splitting.", h.too_long.map((t) => `${note(t.note)} <span class="p" style="display:inline">${t.words} words</span>`)],
  ];
  const color = h.score >= 85 ? "var(--ok)" : h.score >= 60 ? "var(--warn)" : "var(--danger)";
  view.innerHTML = `
    <h1 class="hello">Health</h1>
    <p class="sub">Problems that make AIs give worse answers, and how to fix them. Tip: ask any AI to run <code>/mcp__cortex__tidy</code>.</p>
    <div class="score" style="--sc:${color}"><span class="n">${h.score}</span><span class="l">/ 100 · ${h.notes} notes</span></div>
    ${sections.map(([title, why, items]) => `<h3 class="panel-title">${title} <span>${items.length}</span></h3>
      ${why && items.length ? `<p class="why">${why}</p>` : ""}
      <div class="list">${items.slice(0, 50).map((i) => `<div class="row"><div class="t">${i}</div></div>`).join("") || `<div class="row ok"><div class="t">None</div></div>`}</div>`).join("")}`;
  setContext("");
  touchTab("Health");
}

async function renameNote(path) {
  const to = prompt("New name or folder/name (links to this note will be updated):", path.replace(/\.md$/, ""));
  if (!to || to.trim() === path.replace(/\.md$/, "")) return;
  try {
    const r = await post("/api/move", { path, to: to.trim() });
    toast(`Renamed · fixed links in ${r.links_updated.length} note${r.links_updated.length === 1 ? "" : "s"}`);
    await loadSidebar();
    location.hash = "#/note/" + enc(r.path);
  } catch (e) { toast(e.message, true); }
}

async function viewHistory(path) {
  const versions = await get("/api/history?path=" + encodeURIComponent(path));
  setTop(`<a href="#/note/${enc(path)}">${esc(titleOf(path))}</a> / <b>History</b>`, "");
  if (!versions.length) { view.innerHTML = `<div class="empty"><b>No earlier versions</b>Versions are saved every time a note is changed through Cortex.</div>`; return; }
  view.innerHTML = `
    <h1 class="hello">History</h1>
    <p class="sub">Every change made through Cortex keeps the previous version (last 30). Pick one to preview and restore it.</p>
    <div class="hist">
      <div class="list">${versions.map((v, i) => `<button class="row hist-row" data-id="${esc(v.id)}" aria-pressed="${i === 0}">
        <div class="t"><div>${new Date(v.saved).toLocaleString()}</div><div class="p">before a change by ${esc(v.replaced_by === "human" ? "you" : v.replaced_by)}</div></div>
        <span class="when">${ago(v.saved)}</span></button>`).join("")}</div>
      <div class="hist-preview"><div class="hist-bar"><span id="histLabel"></span><button class="btn primary" id="restoreBtn">Restore this version</button></div><div class="md" id="histBody"></div></div>
    </div>`;
  let current = versions[0].id;
  const show = async (id) => {
    current = id;
    view.querySelectorAll(".hist-row").forEach((r) => r.setAttribute("aria-pressed", r.dataset.id === id));
    const v = await get(`/api/version?path=${encodeURIComponent(path)}&id=${encodeURIComponent(id)}`);
    $("#histBody").innerHTML = markdown(v.content);
    $("#histLabel").textContent = new Date(versions.find((x) => x.id === id).saved).toLocaleString();
  };
  view.querySelectorAll(".hist-row").forEach((r) => (r.onclick = () => show(r.dataset.id)));
  $("#restoreBtn").onclick = async () => {
    if (!confirm("Replace the current note with this version? (The current text is kept in history too.)")) return;
    try { await post("/api/restore", { path, id: current }); toast("Restored"); await loadSidebar(); location.hash = "#/note/" + enc(path); }
    catch (e) { toast(e.message, true); }
  };
  show(current);
}
const SOURCE_COLORS = { "Claude Code": "--claude", "Claude.ai": "--claude", Codex: "--codex", "Gemini CLI": "--gemini", Cursor: "--cursor", ChatGPT: "--codex" };
const convSource = (path) => path.split("/")[1] || "";
function sourceChip(name) {
  const v = SOURCE_COLORS[name];
  return `<span class="agent" style="--c: ${v ? `var(${v})` : "var(--you)"}">${esc(name)}</span>`;
}

async function viewConversations(query = "") {
  setTop("<b>Conversations</b>", `<button class="btn" id="importNow">Import now</button>`);
  const setup = await get("/api/setup");
  const [st, list] = await Promise.all([get("/api/import/status"),
    get(query ? "/api/conversations?q=" + encodeURIComponent(query) : "/api/conversations?limit=300")]);
  const names = { "claude-code": "Claude Code", codex: "Codex", gemini: "Gemini CLI", cursor: "Cursor", chatgpt: "ChatGPT", "claude-ai": "Claude.ai" };
  const counts = Object.entries(st.conversations).map(([k, n]) => `${sourceChip(names[k] || k)} <b>${n}</b>`).join(" &nbsp; ");
  const found = Object.entries(st.detected).filter(([, v]) => v).map(([k]) => names[k]);
  view.innerHTML = `
    <h1 class="hello">Everything you've said to any AI</h1>
    <p class="sub">Every conversation from your AI apps, in full and searchable by you and by every connected AI. New ones come in automatically every few minutes while Cortex is open, and whenever an AI starts.</p>
    ${setup.import_history === true ? "" : `<div class="sync-card warn" style="margin-bottom:14px"><b>Importing is off</b>
      <p>Cortex doesn't read your AI apps' chat history until you allow it.</p>
      <button class="btn primary" id="allowImport" style="margin-top:8px">Import my AI history</button></div>`}
    <div class="conv-head">
      <div>${counts || "No conversations imported yet."}</div>
      <div class="p">${found.length ? "Found on this computer: " + found.join(", ") : "No AI apps with saved history found on this computer yet."}${st.last_run ? " · last import " + ago(st.last_run) : ""}</div>
    </div>
    <form class="task-add" id="convSearch" autocomplete="off">
      <input id="convQ" placeholder="Search your past conversations… e.g. “what did I decide about the logo”" value="${esc(query)}">
      <button class="btn primary">Search</button>
    </form>
    <div class="list">${list.map((c) => `<a class="row" href="#/note/${enc(c.path)}">${sourceChip(convSource(c.path))}
      <div class="t"><div>${esc(c.title)}</div>${c.snippet ? `<div class="p">${esc(c.snippet).replace(/«/g, "<mark>").replace(/»/g, "</mark>")}</div>` : ""}</div>
      <span class="when">${c.modified ? ago(c.modified) : ""}</span></a>`).join("")
      || `<div class="empty"><b>${query ? "No conversation mentions that" : "Nothing yet"}</b>${query ? "" : "Press Import now, or use an AI app and come back."}</div>`}</div>
    <details class="export-box">
      <summary>Import from ChatGPT or Claude.ai (web)</summary>
      <p class="why">Web chats live on their servers, so export them first. <b>ChatGPT</b>: Settings → Data controls → Export data. <b>Claude.ai</b>: Settings → Privacy → Export data. You get a .zip by email. Paste the path to the downloaded file:</p>
      <form class="task-add" id="exportForm"><input id="exportPath" placeholder="C:\\Users\\you\\Downloads\\chatgpt-export.zip" required><button class="btn primary">Import file</button></form>
    </details>`;
  $("#convSearch").onsubmit = (e) => { e.preventDefault(); viewConversations($("#convQ").value.trim()); };
  if ($("#allowImport")) $("#allowImport").onclick = async () => {
    await post("/api/settings", { import_history: true });
    $("#allowImport").textContent = "Importing…";
    try { const r = await post("/api/import", {}); toast(`Imported ${r.new || 0} conversations`); } catch (e) { toast(e.message, true); }
    await loadSidebar(); viewConversations();
  };
  $("#importNow").onclick = async () => {
    $("#importNow").textContent = "Importing…";
    try { const r = await post("/api/import", {}); toast(r.skipped ? r.skipped : `${r.new} new, ${r.updated} updated`); await loadSidebar(); viewConversations(query); }
    catch (e) { toast(e.message, true); }
  };
  $("#exportForm").onsubmit = async (e) => {
    e.preventDefault();
    try { const r = await post("/api/import", { file: $("#exportPath").value }); toast(`Imported ${r.new} new, ${r.updated} updated`); await loadSidebar(); viewConversations(); }
    catch (err) { toast(err.message, true); }
  };
}

const RESEARCH_SOURCES = ["web", "news", "wikipedia", "hackernews", "reddit", "github", "stackoverflow", "arxiv", "bluesky", "mastodon", "lemmy", "x", "youtube"];
const PRESETS = { Everyday: ["web", "news", "wikipedia", "hackernews", "reddit"], Social: ["x", "reddit", "hackernews", "bluesky", "mastodon", "lemmy", "youtube", "news"],
  Tech: ["web", "github", "stackoverflow", "hackernews", "arxiv"], All: RESEARCH_SOURCES };
const LABEL = { hackernews: "Hacker News", stackoverflow: "Stack Overflow", x: "X / Twitter", youtube: "YouTube", github: "GitHub", arxiv: "arXiv" };
let lastResearch = null;

async function viewResearch() {
  setTop("<b>Research</b>", "");
  const chosen = new Set(JSON.parse(localStorageGet("cortex.sources") || "null") || PRESETS.Everyday);
  view.innerHTML = `
    <h1 class="hello">Research</h1>
    <p class="sub">Search the web and social media for free, with no API keys, and save what matters into your brain. Any AI can do this too: ask it to run <code>/mcp__cortex__research</code> or <code>/mcp__cortex__pulse</code>.</p>
    <form class="task-add" id="rForm" autocomplete="off"><input id="rQ" placeholder="What do you want to know?" required><button class="btn primary">Search</button></form>
    <div class="filters">${Object.keys(PRESETS).map((p) => `<button class="chip preset" data-p="${p}">${p}</button>`).join("")}<span class="sep"></span>
      ${RESEARCH_SOURCES.map((s) => `<button class="chip src ${chosen.has(s) ? "on" : ""}" data-s="${s}">${LABEL[s] || s[0].toUpperCase() + s.slice(1)}</button>`).join("")}</div>
    <form class="task-add" id="fForm" autocomplete="off"><input id="fUrl" placeholder="…or paste any link to read it: article, YouTube video, X post, Reddit thread" required><button class="btn">Read link</button></form>
    <div id="rOut"></div>`;
  const sync = () => { view.querySelectorAll(".chip.src").forEach((c) => c.classList.toggle("on", chosen.has(c.dataset.s))); localStorageSet("cortex.sources", JSON.stringify([...chosen])); };
  view.querySelectorAll(".chip.src").forEach((c) => (c.onclick = () => { chosen.has(c.dataset.s) ? chosen.delete(c.dataset.s) : chosen.add(c.dataset.s); sync(); }));
  view.querySelectorAll(".chip.preset").forEach((c) => (c.onclick = () => { chosen.clear(); PRESETS[c.dataset.p].forEach((s) => chosen.add(s)); sync(); }));
  $("#rForm").onsubmit = async (e) => {
    e.preventDefault();
    const query = $("#rQ").value.trim();
    $("#rOut").innerHTML = `<div class="empty">Searching ${chosen.size} sources…</div>`;
    try { lastResearch = await post("/api/research", { query, sources: [...chosen] }); drawResults(); }
    catch (err) { $("#rOut").innerHTML = `<div class="empty"><b>Search failed</b>${esc(err.message)}</div>`; }
  };
  $("#fForm").onsubmit = (e) => { e.preventDefault(); readLink($("#fUrl").value.trim()); };
  if (lastResearch) { $("#rQ").value = lastResearch.query; drawResults(); }
}

function drawResults() {
  const d = lastResearch, groups = {};
  d.results.forEach((r) => (groups[r.source] ||= []).push(r));
  $("#rOut").innerHTML = `
    <div class="r-bar"><span>${d.results.length} results for “${esc(d.query)}”</span>${d.results.length ? `<button class="btn" id="saveAll">Save all to brain</button>` : ""}</div>
    ${Object.entries(groups).map(([src, items]) => `<h3 class="panel-title">${LABEL[src] || src} <span>${items.length}</span></h3>
      <div class="list" style="margin-bottom:20px">${items.map((r, i) => `<div class="row r-row"><div class="t">
        <a href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener noreferrer" class="r-title">${esc(r.title || r.url)}</a>
        <div class="p">${[r.author, r.date, r.score != null ? r.score.toLocaleString() + " " + ({ github: "stars", hackernews: "points", reddit: "upvotes", stackoverflow: "votes", lemmy: "votes", x: "likes", bluesky: "likes", mastodon: "likes" }[r.source] || "points") : "", r.extra && r.extra.comments != null ? r.extra.comments + " comments" : ""].filter(Boolean).map(esc).join(" · ")}</div>
        ${r.snippet ? `<div class="r-snip">${esc(r.snippet.slice(0, 300))}</div>` : ""}</div>
        <button class="btn r-read" data-url="${esc(safeUrl(r.url))}">Read</button></div>`).join("")}</div>`).join("")}
    ${Object.keys(d.errors).length ? `<p class="why">Unavailable right now: ${Object.entries(d.errors).map(([k, v]) => `<b>${esc(LABEL[k] || k)}</b> (${esc(v)})`).join("; ")}</p>` : ""}`;
  view.querySelectorAll(".r-read").forEach((b) => (b.onclick = () => readLink(b.dataset.url)));
  if ($("#saveAll")) $("#saveAll").onclick = async () => {
    try { const r = await post("/api/research/save", { query: d.query, data: d }); toast("Saved to " + r.path); loadSidebar(); }
    catch (e) { toast(e.message, true); }
  };
}

async function readLink(url) {
  const out = $("#rOut");
  out.innerHTML = `<div class="empty">Reading ${esc(url)}…</div>`;
  try {
    const p = await post("/api/fetch", { url });
    const meta = ["author", "date", "likes", "reposts", "views", "score", "comments"].filter((k) => p[k] != null && p[k] !== "").map((k) => `${k}: ${esc(p[k])}`).join(" · ");
    out.innerHTML = `<div class="hist-preview reader">
      <div class="hist-bar"><span>${esc(p.kind)} · <a href="${esc(safeUrl(p.url))}" target="_blank" rel="noopener noreferrer">open original</a></span>
        <span>${lastResearch ? `<button class="btn" id="backBtn">Back to results</button> ` : ""}<button class="btn primary" id="savePage">Save to brain</button></span></div>
      <h2>${esc(p.title)}</h2>${meta ? `<p class="p">${meta}</p>` : ""}
      <div class="md">${markdown(p.text)}</div></div>`;
    $("#savePage").onclick = async () => { try { const r = await post("/api/research/save", { page: p }); toast("Saved to " + r.path); loadSidebar(); } catch (e) { toast(e.message, true); } };
    if ($("#backBtn")) $("#backBtn").onclick = drawResults;
  } catch (e) { out.innerHTML = `<div class="empty"><b>Couldn't read that link</b>${esc(e.message)}</div>`; }
}

async function viewSync() {
  setTop("<b>Sync &amp; backup</b>", "");
  const st = await get("/api/sync");
  const gd = st.detected.find((d) => d.provider === "Google Drive");
  const fmtBytes = (n) => n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB";
  const cloudCard = st.cloud
    ? `<div class="sync-card ok"><b>☁ In the cloud: ${esc(st.cloud.provider)}</b>
        <p>Your brain lives on this PC <i>and</i> in ${esc(st.cloud.provider)}. Cortex works on the local copy at full speed, even offline, and the sync app uploads every change. Any computer that opens this folder shares the same brain.</p></div>`
    : `<div class="sync-card warn"><b>Only on this computer</b>
        <p>${gd ? `Google Drive is installed at <code>${esc(gd.path)}</code>.` : "Install <a href=\"https://www.google.com/drive/download/\" target=\"_blank\" rel=\"noopener noreferrer\">Google Drive for desktop</a>, sign in, and choose <b>Mirror files</b> in its settings."}
        To keep the brain in the cloud too, close Cortex and run:</p><pre><code>python run.py cloud</code></pre>
        <p class="p">It copies everything into Google Drive, checks every file, switches over and reconnects your AIs. Your current folder stays untouched as a fallback.</p></div>`;
  view.innerHTML = `
    <h1 class="hello">Local + cloud</h1>
    <p class="sub">Notes sync through your cloud drive. The search index stays on each computer (a database inside a synced folder gets corrupted), and it rebuilds itself from the notes.</p>
    ${cloudCard}
    <div class="cols" style="margin-top:22px">
      <div>
        <h3 class="panel-title">Devices sharing this brain</h3>
        <div class="list">${st.devices.map((d) => `<div class="row"><div class="t"><div>${esc(d.device)}${d.this ? " <span class=\"p\">(this computer)</span>" : ""}</div></div><span class="when">${d.last_seen ? ago(d.last_seen) : ""}</span></div>`).join("")
          }</div>
        ${st.devices.length < 2 ? `<p class="why" style="margin-top:8px">Other computers appear here once they open the same brain folder from Google Drive.</p>` : ""}
        <p class="why" style="margin-top:10px">Brain folder: <code>${esc(st.vault)}</code><br>Search index (local, never synced): <code>${esc(st.local_index)}</code></p>
      </div>
      <div>
        <h3 class="panel-title">Backups <span>${st.backup_count}</span></h3>
        <div class="sync-card">
          ${st.backup_dir ? `<p>Daily snapshots go to <code>${esc(st.backup_dir)}</code>${st.last_backup ? ` · last one ${ago(st.last_backup)}` : ""}. Sync copies mistakes too (a deleted note disappears everywhere), so a dated snapshot lets you go back to any day.</p>`
            : `<p>Sync copies mistakes too. A deleted note disappears everywhere. Daily snapshots let you go back to any day. Choose a folder, ideally in Google Drive but <i>outside</i> the brain folder:</p>
               <input id="bdir" class="path-in" placeholder="${esc(gd ? gd.path + (gd.path.includes("\\") ? "\\" : "/") + "Cortex Backups" : "Full path to a backup folder")}">`}
          <button class="btn primary" id="backupNow" style="margin-top:10px">Back up now</button>
        </div>
        <div class="list" style="margin-top:12px">${st.backups.map((b) => `<div class="row"><div class="t"><div>${new Date(b.created).toLocaleString()}</div><div class="p">${esc(b.name)}</div></div><span class="when">${fmtBytes(b.bytes)}</span></div>`).join("")}</div>
        ${st.backups.length ? `<p class="why" style="margin-top:8px">To restore one: <code>python run.py restore "&lt;backup.zip&gt;" "&lt;new folder&gt;"</code>. It never overwrites your current brain.</p>` : ""}
      </div>
    </div>`;
  $("#backupNow").onclick = async () => {
    const dir = $("#bdir") ? $("#bdir").value.trim() || $("#bdir").placeholder : undefined;
    $("#backupNow").textContent = "Backing up…";
    try { const r = await post("/api/backup", dir ? { dir } : {}); toast(`Backed up ${r.files} files`); viewSync(); }
    catch (e) { toast(e.message, true); $("#backupNow").textContent = "Back up now"; }
  };
}

const titleOf = (p) => (state.tree.find((t) => t.path === p) || {}).title || p.split("/").pop().replace(/\.md$/, "");

async function viewEdit(path, isNew = false, suggested = "") {
  let content = "", realPath = path, mtime = null;
  if (!isNew) {
    try { const n = await get("/api/note?path=" + encodeURIComponent(path)); content = n.content; realPath = n.path; mtime = n.mtime; }
    catch { isNew = true; }
  }
  if (isNew) {
    const name = suggested || "Untitled";
    realPath = path || `Inbox/${name}.md`;
    content = `# ${name}\n\n`;
  }
  state.current = isNew ? null : realPath;
  state.dirty = false;
  markActive();
  setTop(isNew ? "<b>New note</b>" : `Editing <b>${esc(titleOf(realPath))}</b>`,
    `${isNew ? "" : `<button class="btn danger" id="delBtn">Delete</button>`}<button class="btn" id="cancelBtn">Cancel</button><button class="btn primary" id="saveBtn">Save</button>`);
  view.innerHTML = `
    <div class="editor">
      <input class="path" id="edPath" value="${esc(realPath)}" aria-label="File path" ${isNew ? "" : "readonly"}>
      <textarea id="edText" spellcheck="true" aria-label="Note content">${esc(content)}</textarea>
      <div class="hint">Markdown. Link notes with [[Note name]], tag with #tag. <kbd>Ctrl S</kbd> save · <kbd>Esc</kbd> cancel</div>
    </div>`;
  const ta = $("#edText");
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  ta.addEventListener("input", () => (state.dirty = true));
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Tab") { e.preventDefault(); document.execCommand("insertText", false, "  "); }
  });
  const save = async () => {
    const p = $("#edPath").value.trim();
    if (!p) return toast("Give the note a path", true);
    const send = async (force) => {
      const r = await post("/api/note", { path: p, content: ta.value, mode: isNew ? "create" : "overwrite", expect_mtime: force || isNew ? null : mtime });
      state.dirty = false;
      toast(r.similar ? `Saved · similar notes exist: ${r.similar.join(", ")}` : "Saved");
      await loadSidebar();
      location.hash = "#/note/" + enc(r.path);
    };
    try { await send(false); }
    catch (e) {
      if (e.status === 409) {
        if (confirm("An AI (or another window) changed this note while you were editing.\n\nOK = save yours anyway (theirs stays in History)\nCancel = keep editing")) {
          try { await send(true); } catch (e2) { toast(e2.message, true); }
        }
      } else toast(e.message, true);
    }
  };
  $("#saveBtn").onclick = save;
  $("#cancelBtn").onclick = () => { if (confirmLeave()) history.back(); };
  if ($("#delBtn")) $("#delBtn").onclick = async () => {
    if (!confirm(`Move "${titleOf(realPath)}" to the vault's .trash folder?`)) return;
    try { await api("/api/note?path=" + encodeURIComponent(realPath), { method: "DELETE" }); state.dirty = false; toast("Moved to trash"); await loadSidebar(); location.hash = "#/"; }
    catch (e) { toast(e.message, true); }
  };
  view.onkeydown = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
    if (e.key === "Escape") $("#cancelBtn").click();
  };
}
function confirmLeave() { return !state.dirty || confirm("Discard unsaved changes?"); }
window.newNote = () => { location.hash = "#/new"; };

async function viewTag(tag) {
  setTop(`Tag <b>#${esc(tag)}</b>`, "");
  const hits = await get(`/api/search?q=&tag=${encodeURIComponent(tag)}&limit=200`);
  view.innerHTML = `<h1 class="hello">#${esc(tag)}</h1><p class="sub">${hits.length} note${hits.length === 1 ? "" : "s"}</p>
    <div class="list">${hits.map((h) => `<a class="row" href="#/note/${enc(h.path)}"><div class="t"><div>${esc(h.title)}</div><div class="p">${esc(h.path)}</div></div></a>`).join("") || `<div class="empty">No notes with this tag</div>`}</div>`;
}
/* ── graph: force layout on canvas (grid-accelerated repulsion), metrics from the server ── */
let graphStop = null;
const METRIC_LABEL = { betweenness: "Betweenness", closeness: "Closeness", eigenvector: "Eigenvector", pagerank: "PageRank", degree: "Links" };
function graphOpts() {
  const d = { labels: true, arrows: true, orphans: true, color: "cluster", metric: "betweenness" };
  try { return { ...d, ...JSON.parse(localStorageGet("cortex.graph") || "{}") }; } catch { return d; }
}
async function viewGraph(focus = "") {
  const o = graphOpts(), my = state.nav;
  setTop(focus ? `<a href="#/graph">Graph</a> / <b>${esc(titleOf(focus))}</b>` : "<b>Graph</b>",
    focus ? `<a class="btn" href="#/note/${enc(focus)}">Open note</a><a class="btn" href="#/graph">Whole brain</a>` : "");
  view.classList.add("full");
  view.innerHTML = `<div class="graph-box"><canvas id="cv" aria-label="Graph of notes and links"></canvas>
    <div class="graph-card">
      <h4>Display</h4>
      <div class="opt">Labels <label class="switch"><input type="checkbox" data-o="labels" ${o.labels ? "checked" : ""}><i></i></label></div>
      <div class="opt">Arrows <label class="switch"><input type="checkbox" data-o="arrows" ${o.arrows ? "checked" : ""}><i></i></label></div>
      <div class="opt">Orphans <label class="switch"><input type="checkbox" data-o="orphans" ${o.orphans ? "checked" : ""}><i></i></label></div>
      <div class="opt">Colour <select data-o="color"><option value="cluster">Cluster</option><option value="category">Category</option><option value="folder">Folder</option></select></div>
      <div class="opt">Size <select data-o="metric">${Object.entries(METRIC_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div>
      <div class="legend"><div class="lname" id="legName"></div><div class="bar"></div><div class="ends"><span>low</span><span>high</span></div></div>
    </div>
    <input class="graph-search" id="gq" placeholder="Find a note…" aria-label="Find a note in the graph">
    <div class="graph-tip" id="gtip">Loading…</div></div>`;
  view.querySelector('[data-o="color"]').value = o.color;
  view.querySelector('[data-o="metric"]').value = o.metric;
  const g = await get(`/api/graph2?limit=3000&metric=${o.metric}` + (focus ? `&focus=${encodeURIComponent(focus)}&depth=2` : ""));
  if (my !== state.nav) return;
  const css = getComputedStyle(document.documentElement), col = (v) => css.getPropertyValue(v).trim();
  const palette = CAT_VARS.map(col), C = { line: col("--line"), edge: col("--edge"), accent: col("--accent"), hi: col("--accent-hi"),
    ink: col("--ink"), muted: col("--muted"), faint: col("--faint"), danger: col("--danger"), void: col("--void") };
  const all = g.nodes, idx = new Map(all.map((n, i) => [n.id, i]));
  const E = g.edges.map((e) => [idx.get(e.source), idx.get(e.target)]).filter(([a, b]) => a !== undefined && b !== undefined);
  const deg = new Array(all.length).fill(0);
  E.forEach(([a, b]) => { deg[a]++; deg[b]++; });
  const N = all.length;
  const X = new Float64Array(N), Y = new Float64Array(N), VX = new Float64Array(N), VY = new Float64Array(N), pinned = new Uint8Array(N);
  const nClusters = Math.max(1, ...all.map((n) => (n.cluster ?? 0) + 1));
  all.forEach((n, i) => {  // seed: clusters around a ring, so the layout settles fast and groups stay apart
    const c = Math.min(n.cluster ?? 0, 12), ang = (c / Math.min(nClusters, 13)) * 6.283, rr = 60 + Math.sqrt(N) * 9;
    X[i] = Math.cos(ang) * rr + Math.cos(i * 2.4) * 40 * Math.random(); Y[i] = Math.sin(ang) * rr + Math.sin(i * 2.4) * 40 * Math.random();
  });
  const cv = $("#cv"), c2 = cv.getContext("2d");
  let cam = { x: 0, y: 0, k: 1 }, hover = -1, alpha = 1, autofit = true, dirty = true, found = -1;
  const visible = (i) => o.orphans || deg[i] > 0 || i === found;
  const size = (i) => all[i].missing ? 3.5 : 3 + 10 * Math.sqrt(all[i][o.metric === "degree" ? "degree_s" : o.metric] || 0);
  const maxDeg = Math.max(1, ...all.map((n) => n.degree || 0));
  all.forEach((n) => (n.degree_s = (n.degree || 0) / maxDeg));
  const color = (i) => {
    const n = all[i];
    if (n.missing) return C.danger;
    if (o.color === "category") return n.category ? palette[hue(n.category) % palette.length] : C.faint;
    if (o.color === "folder") return n.folder ? palette[hue(n.folder) % palette.length] : C.faint;
    return palette[(n.cluster ?? 0) % palette.length];
  };
  const resize = () => { const r = cv.getBoundingClientRect(), d = devicePixelRatio || 1; cv.width = r.width * d; cv.height = r.height * d; c2.setTransform(d, 0, 0, d, 0, 0); dirty = true; };
  resize();
  const ro = new ResizeObserver(resize);  // the side panel opening changes the canvas size after the first layout
  ro.observe(cv);
  const CELL = 120;
  const step = () => {
    const grid = new Map();
    for (let i = 0; i < N; i++) { const k = `${Math.floor(X[i] / CELL)},${Math.floor(Y[i] / CELL)}`; let b = grid.get(k); if (!b) grid.set(k, (b = [])); b.push(i); }
    for (let i = 0; i < N; i++) {
      const cx = Math.floor(X[i] / CELL), cy = Math.floor(Y[i] / CELL);
      for (let gx = cx - 1; gx <= cx + 1; gx++) for (let gy = cy - 1; gy <= cy + 1; gy++) {
        const b = grid.get(`${gx},${gy}`); if (!b) continue;
        for (const j of b) {
          if (j <= i) continue;
          const dx = X[i] - X[j], dy = Y[i] - Y[j], d2 = dx * dx + dy * dy + 0.01;
          if (d2 > CELL * CELL) continue;
          const f = (700 / d2) * alpha;
          VX[i] += dx * f; VY[i] += dy * f; VX[j] -= dx * f; VY[j] -= dy * f;
        }
      }
    }
    for (const [a, b] of E) {
      const dx = X[b] - X[a], dy = Y[b] - Y[a], d = Math.sqrt(dx * dx + dy * dy) || 1, f = (d - 55) * 0.03 * alpha;
      VX[a] += (dx / d) * f; VY[a] += (dy / d) * f; VX[b] -= (dx / d) * f; VY[b] -= (dy / d) * f;
    }
    for (let i = 0; i < N; i++) {
      if (pinned[i]) { VX[i] = VY[i] = 0; continue; }
      const grav = deg[i] ? 0.008 : 0.02;  // orphans drift to a loose outer ring instead of flying off
      VX[i] -= X[i] * grav * alpha; VY[i] -= Y[i] * grav * alpha;
      X[i] += VX[i]; Y[i] += VY[i]; VX[i] *= 0.58; VY[i] *= 0.58;
    }
    alpha *= 0.982;
    if (autofit && N) fit();
    dirty = true;
  };
  const fit = () => {
    const r = cv.getBoundingClientRect();
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (let i = 0; i < N; i++) if (visible(i)) { x0 = Math.min(x0, X[i]); x1 = Math.max(x1, X[i]); y0 = Math.min(y0, Y[i]); y1 = Math.max(y1, Y[i]); }
    if (x0 === Infinity) return;
    cam.x = (x0 + x1) / 2; cam.y = (y0 + y1) / 2;
    cam.k = Math.min(2.2, Math.max(0.12, Math.min((r.width - 300) / (x1 - x0 + 80), r.height / (y1 - y0 + 120))));
  };
  const sx = (i) => (X[i] - cam.x) * cam.k + cv.clientWidth / 2 + 110, sy = (i) => (Y[i] - cam.y) * cam.k + cv.clientHeight / 2;
  const draw = () => {
    const w = cv.clientWidth, h = cv.clientHeight;
    c2.save(); c2.setTransform(1, 0, 0, 1, 0, 0); c2.clearRect(0, 0, cv.width, cv.height); c2.restore();
    const near = hover >= 0 ? new Set([hover, ...E.filter(([a, b]) => a === hover || b === hover).flat()]) : null;
    const zoomR = Math.min(1.7, Math.max(0.55, cam.k));
    c2.lineWidth = 1;
    for (const pass of [0, 1]) {
      c2.strokeStyle = pass ? C.accent : C.line;
      c2.fillStyle = pass ? C.accent : C.edge;
      c2.globalAlpha = pass ? 0.9 : (near ? 0.35 : 0.85);
      c2.beginPath();
      for (const [a, b] of E) {
        const hot = near && (a === hover || b === hover);
        if (!!pass !== !!hot || !visible(a) || !visible(b)) continue;
        c2.moveTo(sx(a), sy(a)); c2.lineTo(sx(b), sy(b));
      }
      c2.stroke();
      if (o.arrows && cam.k > 0.35) {
        for (const [a, b] of E) {
          const hot = near && (a === hover || b === hover);
          if (!!pass !== !!hot || !visible(a) || !visible(b)) continue;
          const x1 = sx(a), y1 = sy(a), x2 = sx(b), y2 = sy(b), d = Math.hypot(x2 - x1, y2 - y1);
          if (d < 18) continue;
          const ux = (x2 - x1) / d, uy = (y2 - y1) / d, r = size(b) * zoomR + 2, tx = x2 - ux * r, ty = y2 - uy * r, s = 4.5;
          c2.beginPath(); c2.moveTo(tx, ty); c2.lineTo(tx - ux * s - uy * s * 0.6, ty - uy * s + ux * s * 0.6); c2.lineTo(tx - ux * s + uy * s * 0.6, ty - uy * s - ux * s * 0.6); c2.fill();
        }
      }
    }
    c2.globalAlpha = 1;
    for (let i = 0; i < N; i++) {
      if (!visible(i)) continue;
      const x = sx(i), y = sy(i), r = size(i) * zoomR;
      if (x < -20 || y < -20 || x > w + 20 || y > h + 20) continue;
      c2.globalAlpha = near && !near.has(i) ? 0.25 : 1;
      c2.fillStyle = color(i);
      c2.beginPath(); c2.arc(x, y, r, 0, 6.283); c2.fill();
      if (i === hover || i === found) { c2.strokeStyle = C.hi; c2.lineWidth = 2; c2.stroke(); c2.lineWidth = 1; }
    }
    if (o.labels) {
      c2.font = "11px " + getComputedStyle(document.body).fontFamily;
      c2.textAlign = "center";
      for (let i = 0; i < N; i++) {
        if (!visible(i)) continue;
        const big = size(i) > 7, show = i === hover || i === found || (near && near.has(i)) || N <= 60 || cam.k > 1.1 || (big && cam.k > 0.45);
        if (!show) continue;
        c2.globalAlpha = near && !near.has(i) ? 0.2 : 1;
        c2.fillStyle = i === hover ? C.ink : all[i].missing ? C.danger : C.muted;
        c2.fillText(all[i].title.slice(0, 34), sx(i), sy(i) + size(i) * zoomR + 13);
      }
    }
    c2.globalAlpha = 1;
  };
  let raf;
  const loop = () => { if (alpha > 0.004) step(); if (dirty) { draw(); dirty = false; } raf = requestAnimationFrame(loop); };
  loop();
  graphStop = () => { cancelAnimationFrame(raf); ro.disconnect(); window.removeEventListener("resize", resize); };
  const pick = (e) => {
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = -1, bd = 196;
    for (let i = 0; i < N; i++) { if (!visible(i)) continue; const d = (sx(i) - mx) ** 2 + (sy(i) - my) ** 2; if (d < bd) { bd = d; best = i; } }
    return best;
  };
  let drag = null, moved = false;
  cv.onpointerdown = (e) => {
    autofit = false; moved = false;
    const i = pick(e);
    drag = i >= 0 ? { node: i } : { x: e.clientX, y: e.clientY, cx: cam.x, cy: cam.y };
    if (i >= 0) { pinned[i] = 1; alpha = Math.max(alpha, 0.3); }
    cv.setPointerCapture(e.pointerId);
    cv.style.cursor = "grabbing";
  };
  cv.onpointerup = (e) => { if (drag && drag.node !== undefined) pinned[drag.node] = 0; drag = null; cv.style.cursor = "grab"; try { cv.releasePointerCapture(e.pointerId); } catch {} };
  cv.onpointermove = (e) => {
    if (drag && drag.node !== undefined) {
      const r = cv.getBoundingClientRect();
      X[drag.node] = (e.clientX - r.left - cv.clientWidth / 2 - 110) / cam.k + cam.x; Y[drag.node] = (e.clientY - r.top - cv.clientHeight / 2) / cam.k + cam.y;
      moved = true; dirty = true;
    } else if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      cam.x = drag.cx - dx / cam.k; cam.y = drag.cy - dy / cam.k; dirty = true;
    } else { const h2 = pick(e); if (h2 !== hover) { hover = h2; dirty = true; cv.title = h2 >= 0 ? all[h2].title : ""; } }
  };
  cv.onclick = (e) => {
    if (moved) return;
    const i = pick(e);
    if (i < 0) return;
    const n = all[i];
    location.hash = n.missing ? "#/new/" + enc(n.title) : "#/note/" + enc(n.id);
  };
  cv.onwheel = (e) => { e.preventDefault(); autofit = false; cam.k = Math.min(5, Math.max(0.08, cam.k * (e.deltaY < 0 ? 1.12 : 0.89))); dirty = true; };
  window.addEventListener("resize", resize);
  view.querySelectorAll("[data-o]").forEach((el) => (el.onchange = () => {
    o[el.dataset.o] = el.type === "checkbox" ? el.checked : el.value;
    localStorageSet("cortex.graph", JSON.stringify(o));
    if (el.dataset.o === "orphans") { autofit = true; alpha = Math.max(alpha, 0.05); }
    $("#legName").textContent = "Size: " + METRIC_LABEL[o.metric];
    dirty = true;
    drawAnalysis();
  }));
  $("#legName").textContent = "Size: " + METRIC_LABEL[o.metric];
  $("#gq").oninput = () => {
    const q = $("#gq").value.trim().toLowerCase();
    found = q ? all.findIndex((n) => n.title.toLowerCase().includes(q)) : -1;
    if (found >= 0) { autofit = false; cam.x = X[found]; cam.y = Y[found]; cam.k = Math.max(cam.k, 1.2); }
    dirty = true;
  };
  $("#gtip").textContent = N ? `${g.stats.notes.toLocaleString()} notes · ${g.stats.links.toLocaleString()} links${g.exact ? "" : " · metrics estimated"} · drag to move · scroll to zoom · click to open`
    : "No notes yet. Links between notes show up here.";
  let page = 0;
  function drawAnalysis() {
    const m = o.metric, key = m === "degree" ? "degree" : m;
    const ranked = all.filter((n) => !n.missing).sort((a, b) => (b[key] || 0) - (a[key] || 0));
    const per = 20, pages = Math.max(1, Math.ceil(ranked.length / per));
    page = Math.min(page, pages - 1);
    const slice = ranked.slice(page * per, page * per + per);
    setContext(`
      <h4>${METRIC_LABEL[m]} ranking</h4>
      <div class="rank" style="counter-reset:r ${page * per}">${slice.map((n) => `<a href="#/note/${enc(n.id)}" title="${esc(n.id)}"><span>${esc(n.title)}</span><b>${m === "degree" ? n.degree : (n[m] || 0).toFixed(3)}</b></a>`).join("") || `<div class="none">No notes</div>`}</div>
      <div class="pager"><button class="btn" id="pgPrev" ${page ? "" : "disabled"}>Prev</button><span>Showing ${ranked.length ? page * per + 1 : 0}–${Math.min(ranked.length, page * per + per)} of ${ranked.length}</span><button class="btn" id="pgNext" ${page < pages - 1 ? "" : "disabled"}>Next</button></div>
      <div class="card analysis">
        <h4>${focus ? "Neighbourhood" : "Brain analysis"}</h4>
        <div class="stats" style="grid-template-columns:1fr 1fr">
          <div><div class="num">${g.stats.notes.toLocaleString()}</div><div class="p">notes</div></div>
          <div><div class="num">${g.stats.links.toLocaleString()}</div><div class="p">links</div></div>
          <div><div class="num">${g.stats.clusters}</div><div class="p">clusters</div></div>
          <div><div class="num">${g.orphans.length}${g.orphans.length >= 50 ? "+" : ""}</div><div class="p">orphans</div></div>
        </div>
        ${g.clusters.length ? `<h4>Clusters</h4>${g.clusters.slice(0, 8).map((c) => `<div class="cluster-row"><i class="dot" style="width:8px;height:8px;border-radius:50%;flex:none;background:${palette[c.id % palette.length]}"></i>
          <div style="min-width:0"><div><span class="mono">${c.size}</span> ${c.category ? esc(c.category) + " · " : ""}<a href="#/note/${enc(c.top[0])}">${esc(titleOf(c.top[0]))}</a></div><div class="p">${esc(c.keywords.join(", "))}</div></div></div>`).join("")}` : ""}
        ${g.missing.length ? `<h4>Links to nothing</h4><div class="p">${g.missing.slice(0, 12).map((m2) => `<a href="#/new/${enc(m2)}">${esc(m2)}</a>`).join(", ")}</div>` : ""}
        <p class="p" style="margin-top:10px">Computed from your links (${g.exact ? "exact" : "sampled"}), not by an AI.</p>
      </div>`);
    if ($("#pgPrev")) { $("#pgPrev").onclick = () => { page--; drawAnalysis(); }; $("#pgNext").onclick = () => { page++; drawAnalysis(); }; }
  }
  drawAnalysis();
  touchTab(focus ? "Graph · " + titleOf(focus) : "Graph");
}

function setTop(c, a) { crumbs.innerHTML = c; actions.innerHTML = a; }

/* ── router ────────────────────────────────────────── */
let lastHash = location.hash;
async function route(force) {
  if (force !== true && state.dirty && !confirmLeave()) { history.replaceState(null, "", lastHash); return; }
  state.dirty = false;
  lastHash = location.hash;
  state.nav = (state.nav || 0) + 1;  // a slower, older navigation must not draw over this one
  if (graphStop) { graphStop(); graphStop = null; }
  view.onkeydown = null;
  view.classList.remove("full");
  app.classList.remove("menu-open", "ctx-open");
  const h = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = h.split("/");
  const arg = rest.length ? dec(rest.join("/")) : "";
  state.current = null;
  try {
    if (name === "note") await viewNote(arg);
    else if (name === "edit") { setContext(""); await viewEdit(arg); }
    else if (name === "new") { setContext(""); await viewEdit(arg ? `Inbox/${arg}.md` : "", true, arg); }
    else if (name === "activity") { setContext(""); await viewActivity(); touchTab("Activity"); }
    else if (name === "graph") await viewGraph(arg);
    else if (name === "tasks") await viewTasks();
    else if (name === "topics") await viewTopics();
    else if (name === "changes") await viewChanges();
    else if (name === "settings") await viewSettings();
    else if (name === "conversations") { setContext(""); await viewConversations(); touchTab("Conversations"); }
    else if (name === "research") { setContext(""); await viewResearch(); touchTab("Research"); }
    else if (name === "health") await viewHealth();
    else if (name === "sync") { setContext(""); await viewSync(); touchTab("Sync"); }
    else if (name === "history") { setContext(""); await viewHistory(arg); }
    else if (name === "tag") { setContext(""); await viewTag(arg); touchTab("#" + arg); }
    else await viewHome();
  } catch (e) { view.innerHTML = `<div class="empty"><b>Something went wrong</b>${esc(e.message)}</div>`; }
  markActive();
  drawTabs();
  view.scrollTop = 0;
}
window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

/* ── command palette ───────────────────────────────── */
const pal = $("#palette"), palIn = $("#paletteInput"), palList = $("#paletteList");
let palItems = [], palSel = 0;
function openPalette() { pal.hidden = false; palIn.value = ""; palIn.focus(); runSearch(""); }
function closePalette() { pal.hidden = true; }
const runSearch = debounce(async (q) => {
  if (!q.trim()) {
    const recent = await get("/api/recent?limit=8");
    palItems = recent.map((r) => ({ path: r.path, title: r.title, snippet: "Recently edited " + ago(r.modified) }));
  } else {
    const hits = await get("/api/search?limit=12&q=" + encodeURIComponent(q));
    palItems = hits.map((h) => ({ path: h.path, title: h.title, snippet: h.snippet, meaning: h.via.length === 1 && h.via[0] === "meaning" }));
    if (!palItems.some((p) => p.title.toLowerCase() === q.trim().toLowerCase())) palItems.push({ create: q.trim() });
  }
  palSel = 0;
  drawPalette();
}, 90);
function drawPalette() {
  if (!palItems.length) { palList.innerHTML = `<div class="none">Type to search your notes</div>`; return; }
  palList.innerHTML = palItems.map((it, i) => it.create
    ? `<li class="create" role="option" data-i="${i}" aria-selected="${i === palSel}"><div class="pt">＋ Create “${esc(it.create)}”</div><div class="ps">New note in Inbox/</div></li>`
    : `<li role="option" data-i="${i}" aria-selected="${i === palSel}"><span class="pt">${esc(it.title)}</span><span class="pp">${esc(it.path)}</span>${it.meaning ? `<span class="pm">by meaning</span>` : ""}<div class="ps">${esc(it.snippet).replace(/«/g, "<mark>").replace(/»/g, "</mark>")}</div></li>`).join("");
  palList.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
}
function choose(i) {
  const it = palItems[i];
  if (!it) return;
  closePalette();
  location.hash = it.create ? "#/new/" + enc(it.create) : "#/note/" + enc(it.path);
}
palIn.addEventListener("input", () => runSearch(palIn.value));
palIn.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); palSel = Math.min(palItems.length - 1, palSel + 1); drawPalette(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); palSel = Math.max(0, palSel - 1); drawPalette(); }
  else if (e.key === "Enter") { e.preventDefault(); choose(palSel); }
  else if (e.key === "Escape") closePalette();
});
palList.addEventListener("click", (e) => { const li = e.target.closest("li"); if (li) choose(+li.dataset.i); });
pal.addEventListener("mousedown", (e) => { if (e.target === pal) closePalette(); });
/* ── global wiring ─────────────────────────────────── */
const typing = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
document.addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && k === "k") { e.preventDefault(); pal.hidden ? openPalette() : closePalette(); }
  else if ((e.ctrlKey || e.metaKey) && k === "z" && !e.shiftKey && !typing(document.activeElement) && pal.hidden) { e.preventDefault(); undoChange(null); }
  else if (e.key === "Escape" && app.classList.contains("ctx-open")) app.classList.remove("ctx-open");
});
$("#searchBtn").onclick = openPalette;
$("#newBtn").onclick = () => newNote();
$("#collapseBtn").onclick = () => { localStorageSet("cortex.open", "[]"); drawTree(); };
$("#menuBtn").onclick = () => app.classList.toggle("menu-open");
$("#ctxBtn").onclick = () => {
  if (matchMedia("(max-width: 1180px)").matches) app.classList.toggle("ctx-open");
  else app.classList.toggle("ctx-hidden");
};
document.querySelectorAll(".seg button").forEach((b) => (b.onclick = () => {
  state.mode = b.dataset.mode;
  localStorageSet("cortex.mode", state.mode);
  document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-selected", x === b));
  drawTree();
}));
state.mode = localStorageGet("cortex.mode") === "categories" ? "categories" : "folders";
document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-selected", x.dataset.mode === state.mode));

/* Tell Cortex anything: it decides what it is (task, decision, idea, note), files it, and says where. */
const cap = $("#captureInput");
cap.addEventListener("input", () => { cap.style.height = "auto"; cap.style.height = Math.min(160, cap.scrollHeight) + "px"; });
cap.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#captureForm").requestSubmit(); } });
$("#captureForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = cap.value.trim();
  if (!text) return;
  try {
    const r = await post("/api/ingest", { text });
    cap.value = ""; cap.style.height = "";
    if (r.action === "duplicate") { toast(`Already in ${titleOf(r.duplicate_of)}`, false, { label: "Open", run: () => (location.hash = "#/note/" + enc(r.duplicate_of)) }); return; }
    const bits = [`${r.type === "note" ? "Saved" : r.type[0].toUpperCase() + r.type.slice(1)} → ${titleOf(r.path)}`];
    if (r.tasks && r.tasks.length) bits.push(`${r.tasks.length} task${r.tasks.length === 1 ? "" : "s"}${r.tasks[0].due ? " (due " + r.tasks[0].due + ")" : ""}`);
    if (r.category) bits.push(r.category);
    if (r.links && r.links.length) bits.push(`linked ${r.links.length}`);
    toast(bits.join(" · "), false, r.changeset ? { label: "Undo", run: () => undoChange(r.changeset) } : null);
    await loadSidebar();
    const v = location.hash.split("/")[1] || "";
    if (v === "" || v === "tasks" || v === "changes" || state.current === r.path) route(true);
  } catch (err) { toast(err.message, true); }
});
document.documentElement.dataset.theme = "dark";  // dark only, like COre

/* live refresh: pick up notes written by AIs while the app is open */
let lastSig = "";
async function poll() {
  if (document.hidden || state.dirty) return;
  try {
    const acts = await get("/api/activity?limit=1");
    const sig = acts[0] ? acts[0].ts + acts[0].target + acts[0].action : "";
    if (lastSig && sig !== lastSig) {
      await loadSidebar();
      const v = location.hash.split("/")[1] || "";
      if (v === "" || v === "activity" || v === "changes") route(true);
      if (acts[0].agent !== "you") toast(`${acts[0].agent} ${acts[0].action} ${acts[0].target || ""}`.trim());
    }
    lastSig = sig;
  } catch {}
}
setInterval(poll, 4000);

loadSidebar().then(route).then(poll);
