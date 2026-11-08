"use strict";
/* Cortex UI: vanilla JS, no build step, no network calls beyond this machine. */

const $ = (s, el = document) => el.querySelector(s);
const view = $("#view"), crumbs = $("#crumbs"), actions = $("#topActions");
const state = { tree: [], titles: new Map(), current: null, dirty: false };

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
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 2200);
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

/* ── sidebar ───────────────────────────────────────── */
async function loadSidebar() {
  const [tree, tags, stats] = await Promise.all([get("/api/tree"), get("/api/tags"), get("/api/stats")]);
  state.tree = tree;
  const root = { dirs: {}, files: [] };
  for (const n of tree) {
    const parts = n.path.split("/");
    let node = root;
    for (const d of parts.slice(0, -1)) node = node.dirs[d] ||= { dirs: {}, files: [] };
    node.files.push(n);
  }
  const count = (node) => node.files.length + Object.values(node.dirs).reduce((a, d) => a + count(d), 0);
  const open = new Set(JSON.parse(localStorageGet("cortex.open") || "[]"));
  const render = (node, prefix) =>
    Object.keys(node.dirs).sort((a, b) => a.localeCompare(b)).map((d) => {
      const key = prefix + d;
      return `<details data-key="${esc(key)}" ${open.has(key) ? "open" : ""}><summary>${esc(d)}<span class="count">${count(node.dirs[d])}</span></summary><div class="children">${render(node.dirs[d], key + "/")}</div></details>`;
    }).join("") +
    node.files.map((f) => `<a href="#/note/${enc(f.path)}" data-path="${esc(f.path)}" title="${esc(f.path)}">${esc(f.title)}</a>`).join("");
  $("#tree").innerHTML = render(root, "") || `<div class="empty" style="padding:8px">No notes yet</div>`;
  $("#tree").querySelectorAll("details").forEach((d) =>
    d.addEventListener("toggle", () => {
      d.open ? open.add(d.dataset.key) : open.delete(d.dataset.key);
      localStorageSet("cortex.open", JSON.stringify([...open]));
    }));
  $("#tagList").innerHTML = tags.slice(0, 30).map((t) => `<a class="tag" href="#/tag/${encodeURIComponent(t.tag)}">#${esc(t.tag)}</a>`).join("") || `<span style="color:var(--faint);font-size:12px;padding:0 2px">No tags yet</span>`;
  const s = stats.semantic;
  $("#sideFoot").innerHTML = `<div>${esc(stats.vault)}</div><div class="sem ${s.on ? "on" : ""}" title="${esc(s.hint || "")}">${s.on
    ? `● Meaning search on (${s.indexed}/${s.total})` : "○ Keyword search" + (s.hint ? ` · ${esc(s.hint)}` : "")}</div>`;
  $("#taskCount").textContent = stats.open_tasks || "";
  $("#convCount").textContent = stats.conversations ? stats.conversations.toLocaleString() : "";
  markActive();
  return stats;
}
function markActive() {
  document.querySelectorAll(".tree a").forEach((a) => a.classList.toggle("active", a.dataset.path === state.current));
  const v = location.hash.split("/")[1] || "";
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", (a.dataset.view === "home" && v === "") || a.dataset.view === v));
}
function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

/* ── views ─────────────────────────────────────────── */
function activityRow(a) {
  const isNote = a.target && a.target.endsWith(".md") && a.action !== "deleted";
  const target = a.target ? (isNote ? `<a class="target" href="#/note/${enc(a.target)}">${esc(a.target)}</a>` : `<span class="target">${esc(a.target)}</span>`) : "";
  const main = a.note ? esc(a.note) : `${esc(a.action)} ${target}`;
  const sub = a.note && target ? `<div class="act-note">${target}</div>` : "";
  return `<div class="row">${agentChip(a.agent)}<div class="t"><div class="act-text">${main}</div>${sub}</div><span class="when" title="${esc(a.ts)}">${ago(a.ts)}</span></div>`;
}

async function viewHome() {
  setTop("<b>Home</b>", `<button class="btn primary" id="homeNew">New note</button>`);
  $("#homeNew").onclick = () => newNote();
  const [stats, recent, acts] = await Promise.all([get("/api/stats"), get("/api/recent?limit=12"), get("/api/activity?limit=12")]);
  const agents = new Set(acts.map((a) => a.agent).filter((a) => a !== "you" && a !== "human"));
  const hour = new Date().getHours();
  const greet = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  view.innerHTML = `
    <h1 class="hello">${greet}.</h1>
    <p class="sub">Your brain is shared by every AI you connect. Here's what's been happening.</p>
    <div class="stats">
      <div class="stat"><div class="n">${stats.notes.toLocaleString()}</div><div class="l">notes</div></div>
      <a class="stat" href="#/tasks"><div class="n">${stats.open_tasks.toLocaleString()}</div><div class="l">open tasks</div></a>
      <div class="stat"><div class="n">${stats.links.toLocaleString()}</div><div class="l">links</div></div>
      <div class="stat"><div class="n">${agents.size}</div><div class="l">AIs active recently</div></div>
    </div>
    <div class="cols">
      <div>
        <h3 class="panel-title">Recently changed</h3>
        <div class="list">${recent.map((r) => `<a class="row" href="#/note/${enc(r.path)}"><div class="t"><div>${esc(r.title)}</div><div class="p">${esc(r.path)}</div></div><span class="when">${ago(r.modified)}</span></a>`).join("") || `<div class="empty"><b>No notes yet</b>Press New note, or ask an AI to save something.</div>`}</div>
      </div>
      <div>
        <h3 class="panel-title">AI activity <a href="#/activity">See all</a></h3>
        <div class="list">${acts.map(activityRow).join("") || `<div class="empty"><b>Quiet so far</b>Run <code>python run.py connect</code>, then ask an AI to remember something.</div>`}</div>
      </div>
    </div>`;
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

async function viewNote(path) {
  let n;
  try { n = await get("/api/note?path=" + encodeURIComponent(path)); }
  catch (e) { view.innerHTML = `<div class="empty"><b>Note not found</b>${esc(path)}</div>`; setTop(esc(path), ""); return; }
  state.current = n.path;
  markActive();
  const parts = n.path.split("/");
  setTop(parts.slice(0, -1).map(esc).join(" / ") + (parts.length > 1 ? " / " : "") + `<b>${esc(n.title)}</b>`,
    `${n.versions ? `<button class="btn" id="histBtn" title="Earlier versions">History (${n.versions})</button>` : ""}<button class="btn" id="renBtn">Rename</button><button class="btn primary" id="editBtn">Edit</button>`);
  $("#editBtn").onclick = () => (location.hash = "#/edit/" + enc(n.path));
  $("#renBtn").onclick = () => renameNote(n.path);
  if ($("#histBtn")) $("#histBtn").onclick = () => (location.hash = "#/history/" + enc(n.path));
  const body = markdown(n.content);
  const hasH1 = /^<h1>/.test(body.trim());
  const links = n.links.filter((l) => l.path);
  view.innerHTML = `
    <div class="note-wrap">
      <article>
        <div class="note-meta">${n.tags.map((t) => `<a class="tag" href="#/tag/${encodeURIComponent(t)}">#${esc(t)}</a>`).join("")}<span>Edited ${ago(n.modified)}</span></div>
        <div class="md">${hasH1 ? "" : `<h1>${esc(n.title)}</h1>`}${body}</div>
      </article>
      <aside class="rail">
        <h4>Linked from (${n.backlinks.length})</h4>
        ${n.backlinks.map((b) => `<a href="#/note/${enc(b)}">${esc(titleOf(b))}</a>`).join("") || `<div class="none">Nothing links here yet</div>`}
        <h4>Links to (${links.length})</h4>
        ${links.map((l) => `<a href="#/note/${enc(l.path)}">${esc(titleOf(l.path))}</a>`).join("") || `<div class="none">No outgoing links</div>`}
        <h4>Related, not linked</h4><div id="related"><div class="none">Looking…</div></div>
        <h4>File</h4><div class="none" style="word-break:break-all">${esc(n.path)}</div>
      </aside>
    </div>`;
  view.querySelectorAll("input[data-task]").forEach((cb) => cb.addEventListener("change", async () => {
    try {
      await post("/api/task/toggle", { path: n.path, line: -1, text: cb.dataset.task });
      toast(cb.checked ? "Done ✓" : "Reopened");
      loadSidebar();
    } catch (e) { cb.checked = !cb.checked; toast(e.message, true); }
  }));
  get("/api/related?limit=5&path=" + encodeURIComponent(n.path)).then((rel) => {
    const box = $("#related");
    if (box) box.innerHTML = rel.map((h) => `<a href="#/note/${enc(h.path)}" title="${esc(h.snippet)}">${esc(h.title)}</a>`).join("") || `<div class="none">Nothing similar</div>`;
  }).catch(() => {});
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

async function viewTasks(status = "open") {
  setTop("<b>Tasks</b>", "");
  const tasks = await get("/api/tasks?status=" + status);
  const today = new Date().toISOString().slice(0, 10);
  const groups = status === "open" ? [
    ["Overdue", tasks.filter((t) => t.overdue)],
    ["Today", tasks.filter((t) => t.due === today)],
    ["Upcoming", tasks.filter((t) => t.due && t.due > today)],
    ["No date", tasks.filter((t) => !t.due)],
  ] : [["Completed", tasks]];
  const row = (t) => `<div class="row task-row ${t.overdue ? "overdue" : ""}">
      <input type="checkbox" ${t.done ? "checked" : ""} data-path="${esc(t.path)}" data-line="${t.line}" data-text="${esc(t.text)}" aria-label="Mark done">
      <div class="t"><div>${inline(t.text.replace(/\s*(📅\s*|due:\s*|@)\d{4}-\d{2}-\d{2}/, ""))}</div><a class="p" href="#/note/${enc(t.path)}">${esc(t.title)}</a></div>
      ${t.due ? `<span class="due">${t.due === today ? "today" : esc(t.due)}</span>` : ""}</div>`;
  view.innerHTML = `
    <h1 class="hello">Tasks</h1>
    <p class="sub">Every checkbox in your brain, from any note, by you or any AI.</p>
    <form class="task-add" id="taskForm" autocomplete="off">
      <input id="taskText" placeholder="Add a task…" aria-label="New task" required>
      <input id="taskDue" type="date" aria-label="Due date">
      <button class="btn primary">Add</button>
    </form>
    <div class="filters">
      <button class="chip ${status === "open" ? "on" : ""}" data-s="open">Open</button>
      <button class="chip ${status === "done" ? "on" : ""}" data-s="done">Completed</button>
    </div>
    ${groups.filter(([, l]) => l.length).map(([name, list]) => `<h3 class="panel-title">${name} <span>${list.length}</span></h3><div class="list" style="margin-bottom:22px">${list.map(row).join("")}</div>`).join("")
      || `<div class="list"><div class="empty"><b>${status === "open" ? "Nothing to do" : "Nothing completed yet"}</b>${status === "open" ? "Add one above, or write <code>- [ ] something</code> in any note." : ""}</div></div>`}`;
  view.querySelectorAll(".chip").forEach((c) => (c.onclick = () => viewTasks(c.dataset.s)));
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
}

const SOURCE_COLORS = { "Claude Code": "--claude", "Claude.ai": "--claude", Codex: "--codex", "Gemini CLI": "--gemini", Cursor: "--cursor", ChatGPT: "--codex" };
const convSource = (path) => path.split("/")[1] || "";
function sourceChip(name) {
  const v = SOURCE_COLORS[name];
  return `<span class="agent" style="--c: ${v ? `var(${v})` : "var(--you)"}">${esc(name)}</span>`;
}

async function viewConversations(query = "") {
  setTop("<b>Conversations</b>", `<button class="btn" id="importNow">Import now</button>`);
  const [st, list] = await Promise.all([get("/api/import/status"),
    get(query ? "/api/conversations?q=" + encodeURIComponent(query) : "/api/conversations?limit=300")]);
  const names = { "claude-code": "Claude Code", codex: "Codex", gemini: "Gemini CLI", cursor: "Cursor", chatgpt: "ChatGPT", "claude-ai": "Claude.ai" };
  const counts = Object.entries(st.conversations).map(([k, n]) => `${sourceChip(names[k] || k)} <b>${n}</b>`).join(" &nbsp; ");
  const found = Object.entries(st.detected).filter(([, v]) => v).map(([k]) => names[k]);
  view.innerHTML = `
    <h1 class="hello">Everything you've said to any AI</h1>
    <p class="sub">Every conversation from your AI apps, in full and searchable by you and by every connected AI. New ones come in automatically every few minutes while Cortex is open, and whenever an AI starts.</p>
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

async function viewHealth() {
  setTop("<b>Health</b>", `<button class="btn" id="recheck">Check again</button>`);
  view.innerHTML = `<div class="empty">Checking your brain…</div>`;
  const h = await get("/api/health");
  $("#recheck").onclick = viewHealth;
  const note = (p) => `<a href="#/note/${enc(p)}">${esc(titleOf(p))}</a>`;
  const sections = [
    ["Sync conflicts", "Two devices edited the same note at once, so the sync app kept both. Merge the copy into the original, then delete the copy.", (h.sync_conflicts || []).map((c) => `${note(c.note)}${c.original ? ` <span class="p">copy of</span> ${note(c.original)}` : ""}`)],
    ["Broken links", "A [[link]] points to a note that doesn't exist.", h.broken_links.map((b) => `${note(b.note)} → <code>[[${esc(b.link)}]]</code> <a class="fix" href="#/new/${enc(b.link)}">create it</a>`)],
    ["Possible duplicates", "Notes with the same name or title. Merge them so AIs don't split facts.", h.duplicates.map((d) => d.map(note).join(" · "))],
    ["Stale projects", "Marked active but untouched for 60+ days.", h.stale.map((s) => `${note(s.note)} <span class="p">${s.days} days</span>`)],
    ["Overdue tasks", "", h.overdue_tasks.map((t) => `${esc(t.text)} <span class="p">due ${esc(t.due)} · ${note(t.path)}</span>`)],
    ["Empty notes", "", h.empty.map(note)],
    ["Orphans", "No links in or out. Link them so they surface in context.", h.orphans.map(note)],
    ["Very long notes", "Long notes cost AIs more tokens. Consider splitting.", h.too_long.map((t) => `${note(t.note)} <span class="p">${t.words} words</span>`)],
  ];
  const color = h.score >= 85 ? "var(--codex)" : h.score >= 60 ? "#d9a514" : "var(--danger)";
  view.innerHTML = `
    <h1 class="hello">Brain health</h1>
    <p class="sub">Problems that make AIs give worse answers, and how to fix them. Tip: ask any AI to run <code>/mcp__cortex__tidy</code>.</p>
    <div class="score" style="--sc:${color}"><span class="n">${h.score}</span><span class="l">/ 100 · ${h.notes} notes</span></div>
    ${sections.map(([title, why, items]) => `<h3 class="panel-title">${title} <span>${items.length}</span></h3>
      ${why && items.length ? `<p class="why">${why}</p>` : ""}
      <div class="list" style="margin-bottom:22px">${items.slice(0, 50).map((i) => `<div class="row"><div class="t">${i}</div></div>`).join("") || `<div class="row ok"><div class="t">✓ None</div></div>`}</div>`).join("")}`;
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

/* graph: small force layout on canvas, fine for a few thousand notes */
let graphStop = null;
async function viewGraph() {
  setTop("<b>Graph</b>", "");
  view.innerHTML = `<div class="graph-box"><canvas id="cv"></canvas><div class="graph-tip">Drag to pan · scroll to zoom · click a dot to open</div></div>`;
  const g = await get("/api/graph");
  const cv = $("#cv"), ctx = cv.getContext("2d");
  const css = getComputedStyle(document.documentElement);
  const col = (v) => css.getPropertyValue(v).trim();
  const idx = new Map(g.nodes.map((n, i) => [n.id, i]));
  const deg = new Array(g.nodes.length).fill(0);
  const edges = g.edges.map((e) => [idx.get(e.source), idx.get(e.target)]).filter(([a, b]) => a !== undefined && b !== undefined);
  edges.forEach(([a, b]) => { deg[a]++; deg[b]++; });
  const N = g.nodes.length, R = Math.sqrt(N) * 30 + 50;
  const pos = g.nodes.map((_, i) => ({ x: Math.cos(i * 2.4) * R * Math.sqrt(i / N), y: Math.sin(i * 2.4) * R * Math.sqrt(i / N), vx: 0, vy: 0 }));
  let cam = { x: 0, y: 0, k: 1 }, hover = -1, alpha = 1;
  const resize = () => { const r = cv.getBoundingClientRect(), d = devicePixelRatio || 1; cv.width = r.width * d; cv.height = r.height * d; ctx.setTransform(d, 0, 0, d, 0, 0); };
  resize();
  const step = () => {
    if (alpha < 0.005) return;
    for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
      let dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y, d2 = dx * dx + dy * dy + 0.01;
      if (d2 > 90000) continue;
      const f = (900 / d2) * alpha; pos[i].vx += dx * f; pos[i].vy += dy * f; pos[j].vx -= dx * f; pos[j].vy -= dy * f;
    }
    for (const [a, b] of edges) {
      const dx = pos[b].x - pos[a].x, dy = pos[b].y - pos[a].y, d = Math.sqrt(dx * dx + dy * dy) || 1, f = (d - 60) * 0.02 * alpha;
      pos[a].vx += (dx / d) * f; pos[a].vy += (dy / d) * f; pos[b].vx -= (dx / d) * f; pos[b].vy -= (dy / d) * f;
    }
    for (const p of pos) { p.vx -= p.x * 0.012 * alpha; p.vy -= p.y * 0.012 * alpha; p.x += p.vx; p.y += p.vy; p.vx *= 0.6; p.vy *= 0.6; }
    alpha *= 0.985;
    if (autofit && N) fit();
  };
  let autofit = true;
  const fit = () => {
    const r = cv.getBoundingClientRect();
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const p of pos) { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y); }
    cam.x = (x0 + x1) / 2; cam.y = (y0 + y1) / 2;
    cam.k = Math.min(2, Math.max(0.2, Math.min(r.width / (x1 - x0 + 160), r.height / (y1 - y0 + 160))));
  };
  const toScreen = (p) => { const r = cv.getBoundingClientRect(); return [(p.x - cam.x) * cam.k + r.width / 2, (p.y - cam.y) * cam.k + r.height / 2]; };
  const draw = () => {
    const r = cv.getBoundingClientRect();
    ctx.clearRect(0, 0, r.width, r.height);
    ctx.strokeStyle = col("--line"); ctx.lineWidth = 1;
    ctx.beginPath();
    for (const [a, b] of edges) { const [x1, y1] = toScreen(pos[a]), [x2, y2] = toScreen(pos[b]); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); }
    ctx.stroke();
    const near = hover >= 0 ? new Set(edges.filter(([a, b]) => a === hover || b === hover).flat()) : null;
    for (let i = 0; i < N; i++) {
      const [x, y] = toScreen(pos[i]), rad = (3 + Math.sqrt(deg[i]) * 1.6) * Math.min(1.6, Math.max(0.7, cam.k));
      ctx.fillStyle = i === hover ? col("--accent") : near && near.has(i) ? col("--accent") : near ? col("--line") : col("--faint");
      ctx.beginPath(); ctx.arc(x, y, rad, 0, 6.283); ctx.fill();
      if (N <= 40 || cam.k > 1.2 || i === hover || deg[i] > 4 || (near && near.has(i))) {
        ctx.fillStyle = i === hover ? col("--text") : col("--muted"); ctx.font = "12px system-ui"; ctx.textAlign = "center";
        ctx.fillText(g.nodes[i].title.slice(0, 32), x, y - rad - 5);
      }
    }
  };
  let raf;
  const loop = () => { step(); draw(); raf = requestAnimationFrame(loop); };
  loop();
  graphStop = () => cancelAnimationFrame(raf);
  const pick = (e) => {
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = -1, bd = 144;
    pos.forEach((p, i) => { const [x, y] = toScreen(p), d = (x - mx) ** 2 + (y - my) ** 2; if (d < bd) { bd = d; best = i; } });
    return best;
  };
  let drag = null, moved = false;
  cv.onmousedown = (e) => { autofit = false; drag = { x: e.clientX, y: e.clientY, cx: cam.x, cy: cam.y }; moved = false; cv.style.cursor = "grabbing"; };
  window.onmouseup = () => { drag = null; cv.style.cursor = "grab"; };
  cv.onmousemove = (e) => {
    if (drag) { const dx = e.clientX - drag.x, dy = e.clientY - drag.y; if (Math.abs(dx) + Math.abs(dy) > 3) moved = true; cam.x = drag.cx - dx / cam.k; cam.y = drag.cy - dy / cam.k; }
    else hover = pick(e);
  };
  cv.onclick = (e) => { if (moved) return; const i = pick(e); if (i >= 0) location.hash = "#/note/" + enc(g.nodes[i].id); };
  cv.onwheel = (e) => { e.preventDefault(); autofit = false; cam.k = Math.min(4, Math.max(0.2, cam.k * (e.deltaY < 0 ? 1.12 : 0.89))); };
  window.addEventListener("resize", resize);
  if (!N) view.querySelector(".graph-tip").textContent = "No notes yet. Links between notes show up here.";
}

function setTop(c, a) { crumbs.innerHTML = c; actions.innerHTML = a; }

/* ── router ────────────────────────────────────────── */
let lastHash = location.hash;
async function route() {
  if (state.dirty && !confirmLeave()) { history.replaceState(null, "", lastHash); return; }
  state.dirty = false;
  lastHash = location.hash;
  if (graphStop) { graphStop(); graphStop = null; }
  view.onkeydown = null;
  $("#app").classList.remove("menu-open");
  const h = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = h.split("/");
  const arg = rest.length ? dec(rest.join("/")) : "";
  state.current = null;
  try {
    if (name === "note") await viewNote(arg);
    else if (name === "edit") await viewEdit(arg);
    else if (name === "new") await viewEdit(arg ? `Inbox/${arg}.md` : "", true, arg);
    else if (name === "activity") await viewActivity();
    else if (name === "graph") await viewGraph();
    else if (name === "tasks") await viewTasks();
    else if (name === "conversations") await viewConversations();
    else if (name === "research") await viewResearch();
    else if (name === "health") await viewHealth();
    else if (name === "sync") await viewSync();
    else if (name === "history") await viewHistory(arg);
    else if (name === "tag") await viewTag(arg);
    else await viewHome();
  } catch (e) { view.innerHTML = `<div class="empty"><b>Something went wrong</b>${esc(e.message)}</div>`; }
  markActive();
  $("#main").scrollTop = 0;
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
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); pal.hidden ? openPalette() : closePalette(); }
});
$("#searchBtn").onclick = openPalette;
$("#newBtn").onclick = () => newNote();
$("#menuBtn").onclick = () => $("#app").classList.toggle("menu-open");
$("#captureForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const inp = $("#captureInput"), text = inp.value.trim();
  if (!text) return;
  try {
    const r = await post("/api/capture", { text });
    inp.value = "";
    toast("Saved to " + r.path);
    await loadSidebar();
    if (!location.hash || location.hash === "#/") viewHome();
  } catch (err) { toast(err.message, true); }
});

function applyTheme(t) { document.documentElement.dataset.theme = t; localStorageSet("cortex.theme", t); }
applyTheme(localStorageGet("cortex.theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
$("#themeBtn").onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

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
      if (v === "" || v === "activity") route();
      if (acts[0].agent !== "you") toast(`${acts[0].agent} ${acts[0].action} ${acts[0].target || ""}`.trim());
    }
    lastSig = sig;
  } catch {}
}
setInterval(poll, 4000);

loadSidebar().then(route).then(poll);
