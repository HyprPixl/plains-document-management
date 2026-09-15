/* Document Hub — frontend. Vanilla JS, no build step. */
"use strict";

const state = {
  me: null,
  taxonomy: { business_unit: [], department: [], document_type: [] },
  surface: "manage",
  queue: "unclassified",
  selected: new Set(),
  currentDoc: null,
};

// ─────────────────────────────────────────────── helpers ──
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid.nodeType ? kid : document.createTextNode(kid));
  return n;
};
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.error || msg; } catch {}
    const e = new Error(msg); e.status = r.status;
    try { e.body = await r.clone().json(); } catch {}
    throw e;
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}
let toastT;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (isErr ? " err" : ""); t.hidden = false;
  clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), 3200);
}
const labelFor = (cat, val) => (state.taxonomy[cat].find((x) => x.value === val) || {}).label || val || "—";

// ─────────────────────────────────────────────── surfaces ──
function setSurface(name) {
  state.surface = name;
  localStorage.setItem("dochub.surface", name);
  $$(".surface-tab").forEach((b) => b.classList.toggle("active", b.dataset.surface === name));
  $("#surface-manage").hidden = name !== "manage";
  $("#surface-explore").hidden = name !== "explore";
  const path = "/" + name;
  if (location.pathname !== path) history.replaceState({}, "", path);
  if (name === "manage") loadManage();
  else loadExploreFilters();
}

// ─────────────────────────────────────────────── init ──
async function init() {
  $$(".surface-tab").forEach((b) => b.addEventListener("click", () => setSurface(b.dataset.surface)));
  wireUpload();
  wireQueueTabs();
  wireDrawer();
  wireClassifyModal();
  wireExplore();
  wireSharePoint();

  state.me = await api("/api/me");
  $("#userChip").textContent = state.me.email + (state.me.is_admin ? " · admin" : "");
  state.taxonomy = await api("/api/taxonomy");

  const start = location.pathname.startsWith("/explore")
    ? "explore"
    : location.pathname.startsWith("/manage")
    ? "manage"
    : localStorage.getItem("dochub.surface") || "manage";
  setSurface(start);
}

// ─────────────────────────────────────────────── MANAGE ──
async function loadManage() {
  loadStats();
  loadDocs();
  refreshSharePoint();
}
async function loadStats() {
  try {
    const s = await api("/api/stats");
    const cards = [
      ["Needs classification", s.unclassified, "unclassified"],
      ["Needs review", s.by_status.needs_review || 0, "needs_review"],
      ["Verified", s.by_status.verified || 0, "verified"],
    ];
    $("#statRow").replaceChildren(
      ...cards.map(([l, n, q]) =>
        el("div", { class: "stat-card", onclick: () => selectQueue(q) },
          el("div", { class: "n" }, String(n)), el("div", { class: "l" }, l))
      )
    );
  } catch (e) { /* non-fatal */ }
}
function wireQueueTabs() {
  $$("#queueTabs .qtab").forEach((b) =>
    b.addEventListener("click", () => selectQueue(b.dataset.queue)));
  $("#selectAll").addEventListener("change", (e) => {
    $$("#docRows input[type=checkbox]").forEach((cb) => {
      cb.checked = e.target.checked;
      cb.checked ? state.selected.add(cb.dataset.id) : state.selected.delete(cb.dataset.id);
    });
    updateBulkBar();
  });
  $("#bulkClassifyBtn").addEventListener("click", openClassify);
}
function selectQueue(q) {
  state.queue = q;
  state.selected.clear(); updateBulkBar();
  $$("#queueTabs .qtab").forEach((b) => b.classList.toggle("active", b.dataset.queue === q));
  loadDocs();
}
async function loadDocs() {
  const q = state.queue;
  let path = "/api/documents";
  const params = new URLSearchParams();
  if (q === "unclassified") params.set("classification_status", "unclassified");
  else if (q !== "all") params.set("verification_status", q);
  if ([...params].length) path += "?" + params;
  const rows = await api(path);
  const tb = $("#docRows"); tb.replaceChildren();
  $("#docEmpty").hidden = rows.length > 0;
  for (const d of rows) {
    const cb = el("input", { type: "checkbox", "data-id": d.doc_id,
      onclick: (e) => { e.stopPropagation();
        e.target.checked ? state.selected.add(d.doc_id) : state.selected.delete(d.doc_id);
        updateBulkBar(); } });
    tb.append(el("tr", { onclick: () => openDoc(d.doc_id) },
      el("td", { class: "col-check" }, cb),
      el("td", {}, el("span", { class: "doc-name" }, d.original_filename || "(unnamed)")),
      el("td", {}, labelFor("business_unit", d.business_unit)),
      el("td", {}, d.document_type || "—"),
      el("td", {}, extractionBadge(d.extraction_status)),
      el("td", {}, statusBadge(d.verification_status))));
  }
  $("#selectAll").checked = false;
}
function updateBulkBar() {
  const n = state.selected.size;
  $("#bulkBar").hidden = n === 0;
  $("#bulkCount").textContent = `${n} selected`;
}
const statusBadge = (s) => {
  const map = { verified: ["green", "Verified"], needs_review: ["amber", "Needs review"], failed: ["red", "Failed"] };
  const [c, t] = map[s] || ["gray", s || "—"];
  return el("span", { class: "badge " + c }, t);
};
const extractionBadge = (s) => {
  const map = { done: ["green", "Extracted"], processing: ["blue", "Processing"],
    pending: ["amber", "Queued"], failed: ["red", "Failed"] };
  const [c, t] = map[s] || ["gray", s || "—"];
  return el("span", { class: "badge " + c }, t);
};

// ─────────────────────────────────────────────── UPLOAD ──
function wireUpload() {
  const drop = $("#uploadDrop"), input = $("#fileInput");
  $("#browseBtn").addEventListener("click", (e) => { e.stopPropagation(); input.click(); });
  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => uploadFiles(input.files));
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));
}
async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  const box = $("#uploadResults");
  box.replaceChildren(el("div", { class: "up-item" }, `Uploading ${files.length} file(s)…`));
  try {
    const res = await api("/api/upload", { method: "POST", body: fd });
    box.replaceChildren(...res.results.map((r) => {
      const isNew = r.status === "new";
      return el("div", { class: "up-item " + (isNew ? "new" : "dup") },
        el("span", { class: "tag" }, isNew ? "NEW" : "DUPLICATE"),
        el("span", {}, r.filename),
        isNew ? "" : el("span", { class: "muted" }, `— already stored as "${r.existing_name}"`));
    }));
    toast(`${res.results.filter((r) => r.status === "new").length} new, ` +
      `${res.results.filter((r) => r.status === "duplicate").length} duplicate`);
    loadManage();
  } catch (e) { toast("Upload failed: " + e.message, true); box.replaceChildren(); }
}

// ─────────────────────────────────────────────── CLASSIFY MODAL ──
function wireClassifyModal() {
  $("#classifyCancel").addEventListener("click", () => ($("#classifyScrim").hidden = true));
  $("#classifyApply").addEventListener("click", applyClassify);
  $("#clBU").addEventListener("change", () => fillTypeSelect($("#clType"), $("#clBU").value));
}
function fillSelect(sel, cat, placeholder) {
  sel.replaceChildren(el("option", { value: "" }, placeholder));
  state.taxonomy[cat].forEach((o) => sel.append(el("option", { value: o.value }, o.label)));
}
function fillTypeSelect(sel, bu) {
  sel.replaceChildren(el("option", { value: "" }, "— select —"));
  state.taxonomy.document_type
    .filter((o) => !o.business_unit || o.business_unit === bu)
    .forEach((o) => sel.append(el("option", { value: o.value }, o.label)));
}
function openClassify() {
  fillSelect($("#clBU"), "business_unit", "— select —");
  fillTypeSelect($("#clType"), "");
  fillSelect($("#clDept"), "department", "— none —");
  $("#classifyCount").textContent = `${state.selected.size} document(s) selected`;
  $("#classifyScrim").hidden = false;
}
async function applyClassify() {
  const body = {
    doc_ids: [...state.selected],
    business_unit: $("#clBU").value || null,
    document_type: $("#clType").value || null,
    department: $("#clDept").value || null,
  };
  if (!body.business_unit && !body.document_type) { toast("Pick at least a business unit or type", true); return; }
  try {
    await api("/api/documents/classify", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    await api("/api/documents/enqueue", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ doc_ids: body.doc_ids }) });
    $("#classifyScrim").hidden = true;
    state.selected.clear(); updateBulkBar();
    toast("Classified and queued for extraction");
    loadManage();
  } catch (e) { toast("Failed: " + e.message, true); }
}

// ─────────────────────────────────────────────── DRAWER (detail/verify) ──
function wireDrawer() {
  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#drawerScrim").addEventListener("click", closeDrawer);
}
function closeDrawer() { $("#drawer").hidden = true; $("#drawerScrim").hidden = true; state.currentDoc = null; }
async function openDoc(docId) {
  try {
    const data = await api("/api/documents/" + docId);
    state.currentDoc = data;
    renderDrawer(data);
    $("#drawer").hidden = false; $("#drawerScrim").hidden = false;
  } catch (e) { toast("Could not open: " + e.message, true); }
}
function renderDrawer(data) {
  const d = data.document;
  $("#drawerTitle").textContent = d.original_filename || "Document";
  $("#drawerSub").textContent =
    `${labelFor("business_unit", d.business_unit)} · ${d.document_type || "unclassified"}`;
  const body = $("#drawerBody"); body.replaceChildren();

  body.append(el("div", { class: "field-actions" },
    el("a", { class: "btn", href: `/api/download?doc_id=${d.doc_id}&inline=1`, target: "_blank" }, "View file"),
    el("a", { class: "btn", href: `/api/download?doc_id=${d.doc_id}` }, "Download")));

  body.append(el("div", { class: "section-label" }, "Extracted fields"));
  if (d.extraction_status !== "done")
    body.append(el("div", { class: "muted small" },
      d.extraction_status === "failed" ? "Extraction failed — you can still enter fields manually."
      : "Extraction not finished yet. Values will appear once processing completes."));

  for (const f of data.fields) {
    const val = f.confirmed_value ?? f.proposed_value ?? "";
    const row = el("div", { class: "field-row" + (f.required_for_verify ? " req" : "") });
    row.append(el("label", {}, f.label));
    let input;
    if (f.options) {
      input = el("select", { "data-key": f.field_key });
      input.append(el("option", { value: "" }, "—"));
      f.options.forEach((o) => input.append(el("option", { value: o, ...(o === val ? { selected: "" } : {}) }, o)));
    } else if (f.data_type === "long_text") {
      input = el("textarea", { "data-key": f.field_key, rows: "3" }); input.value = val;
    } else {
      input = el("input", { type: f.data_type === "date" ? "date" : "text", "data-key": f.field_key });
      input.value = val;
    }
    row.append(input);
    if (f.proposed_value && !f.confirmed_value)
      row.append(el("div", { class: "prov" }, "AI-suggested — review and confirm"));
    else if (f.source_provenance === "human")
      row.append(el("div", { class: "prov" }, "Edited by a person"));
    body.append(row);
  }

  body.append(el("div", { class: "section-label" }, "Related documents"));
  const links = el("div", { class: "links-list" });
  if (!data.links.length) links.append(el("div", { class: "muted small" }, "No linked documents."));
  data.links.forEach((l) => links.append(el("div", { class: "link-item" },
    `${l.relationship}: ${l.original_filename} (${l.document_type || "—"})`)));
  body.append(links);
  if (d.document_type === "Amendment" && !data.links.some((l) => l.relationship === "amendment_of"))
    body.append(el("div", { class: "prov", style: "color:var(--warn)" },
      "Amendments must be linked to a parent contract before they can be verified."));

  // footer actions
  const foot = $("#drawerFoot"); foot.replaceChildren(
    el("button", { class: "btn", onclick: saveFields }, "Save"),
    d.verification_status === "verified"
      ? el("button", { class: "btn", onclick: () => setVerify(false) }, "Un-verify")
      : el("button", { class: "btn ok", onclick: () => setVerify(true) }, "Save & verify"));
}
function collectFieldValues() {
  const values = {};
  $$("#drawerBody [data-key]").forEach((i) => (values[i.dataset.key] = i.value));
  return values;
}
async function saveFields() {
  const id = state.currentDoc.document.doc_id;
  await api(`/api/documents/${id}/fields`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ values: collectFieldValues() }) });
  toast("Saved");
}
async function setVerify(on) {
  const id = state.currentDoc.document.doc_id;
  try {
    if (on) await saveFields();
    await api(`/api/documents/${id}/${on ? "verify" : "unverify"}`, { method: "POST" });
    toast(on ? "Verified" : "Moved back to review");
    closeDrawer(); loadManage();
  } catch (e) {
    if (e.body?.error === "missing_required")
      toast("Fill required fields: " + (e.body.fields || []).join(", "), true);
    else if (e.body?.error === "amendment_needs_parent")
      toast("Link this amendment to a parent contract first", true);
    else toast("Failed: " + e.message, true);
  }
}

// ─────────────────────────────────────────────── EXPLORE ──
function loadExploreFilters() {
  fillSelectKeep($("#filterBU"), "business_unit", "All business units");
  fillSelectKeep($("#filterType"), "document_type", "All types");
  if (!$("#searchResults").children.length) runSearch();
}
function fillSelectKeep(sel, cat, placeholder) {
  const cur = sel.value;
  sel.replaceChildren(el("option", { value: "" }, placeholder));
  state.taxonomy[cat].forEach((o) => sel.append(el("option", { value: o.value }, o.label)));
  sel.value = cur;
}
function wireExplore() {
  $("#searchBtn").addEventListener("click", runSearch);
  $("#searchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
  $("#filterBU").addEventListener("change", runSearch);
  $("#filterType").addEventListener("change", runSearch);
}
async function runSearch() {
  const params = new URLSearchParams();
  if ($("#searchInput").value) params.set("q", $("#searchInput").value);
  if ($("#filterBU").value) params.set("business_unit", $("#filterBU").value);
  if ($("#filterType").value) params.set("document_type", $("#filterType").value);
  const rows = await api("/api/search?" + params);
  const grid = $("#searchResults"); grid.replaceChildren();
  if (!rows.length) { grid.append(el("div", { class: "empty" }, "No matching documents.")); return; }
  for (const d of rows) {
    grid.append(el("div", { class: "result-card", onclick: () => openDoc(d.doc_id) },
      el("div", { class: "rc-title" }, d.title || d.original_filename),
      el("div", { class: "rc-meta" },
        el("span", { class: "badge blue" }, labelFor("business_unit", d.business_unit)),
        el("span", { class: "badge gray" }, d.document_type || "—")),
      el("div", { class: "rc-sum" }, d.summary || d.original_filename)));
  }
}

// ─────────────────────────────────────────────── SHAREPOINT ──
const spState = {
  status: null,
  view: "sites",          // sites | drives | items
  site: null,             // {id, name}
  drive: null,            // {id, name}
  path: [],               // breadcrumb of {id, name} folders inside the drive
  selected: new Map(),    // id → {id, name, is_folder}
};

function wireSharePoint() {
  $("#spImportBtn").addEventListener("click", openSharePoint);
  $("#spClose").addEventListener("click", closeSharePoint);
  $("#spCancel").addEventListener("click", closeSharePoint);
  $("#spConnectBtn").addEventListener("click", connectSharePoint);
  $("#spImport").addEventListener("click", doSharePointImport);
  $("#spBU").addEventListener("change", () => fillTypeSelect($("#spType"), $("#spBU").value));
  let searchT;
  $("#spSearch").addEventListener("input", () => {
    clearTimeout(searchT);
    searchT = setTimeout(() => { if (spState.view === "sites") loadSpSites($("#spSearch").value); }, 350);
  });
  window.addEventListener("message", (e) => {
    if (e.data && e.data.sharepoint === "connected") {
      if (e.data.error) { toast("SharePoint: " + e.data.error, true); return; }
      toast("Connected to SharePoint");
      refreshSharePoint();
      if (!$("#spScrim").hidden) showSpBrowser();
    }
  });
}

async function refreshSharePoint() {
  try {
    const st = await api("/api/sharepoint/status");
    spState.status = st;
    $("#spEntry").hidden = !(st.configured && st.can_import);
    await loadSyncs(st);
  } catch { /* non-fatal */ }
}

async function loadSyncs(st) {
  const panel = $("#syncPanel"), list = $("#syncList");
  let syncs = [];
  try { syncs = (await api("/api/sharepoint/syncs")).syncs || []; } catch { }
  if (!syncs.length) { panel.hidden = true; return; }
  panel.hidden = false;
  list.replaceChildren(...syncs.map((s) => {
    const dead = s.token_status === "needs_reauth";
    const meta = [labelFor("business_unit", s.business_unit), s.document_type, s.user_email]
      .filter(Boolean).join(" · ");
    const last = s.last_synced ? new Date(s.last_synced * 1000).toLocaleString() : "not yet";
    return el("div", { class: "sync-item" + (dead ? " dead" : "") },
      el("div", { class: "sync-main" },
        el("div", { class: "sync-name" }, `${s.site_name || "?"} / ${s.drive_name || ""} / ${s.folder_name || "root"}`),
        el("div", { class: "muted small" }, `${meta || "—"} · last synced ${last}`),
        dead ? el("div", { class: "sync-warn" },
          "Connection lapsed — reconnect to resume syncing." + (s.last_error ? ` (${s.last_error})` : "")) : null),
      el("div", { class: "sync-actions" },
        dead && (st?.connected)
          ? el("button", { class: "btn small", onclick: () => reconnectSync(s.id) }, "Reconnect")
          : (dead ? el("button", { class: "btn small", onclick: connectSharePoint }, "Connect") : null),
        el("button", { class: "btn small danger", onclick: () => removeSync(s.id, s.folder_name) }, "Remove")));
  }));
}

async function reconnectSync(id) {
  try {
    await api(`/api/sharepoint/syncs/${id}/reconnect`, { method: "POST" });
    toast("Sync reconnected"); refreshSharePoint();
  } catch (e) {
    if (e.status === 401) { toast("Connect to SharePoint first", true); connectSharePoint(); }
    else toast("Failed: " + e.message, true);
  }
}
async function removeSync(id, name) {
  if (!confirm(`Stop auto-syncing "${name || "this folder"}"? Already-imported documents stay.`)) return;
  await api(`/api/sharepoint/syncs/${id}`, { method: "DELETE" });
  toast("Auto-sync removed"); refreshSharePoint();
}

function openSharePoint() {
  spState.selected.clear();
  spState.view = "sites"; spState.site = null; spState.drive = null; spState.path = [];
  $("#spScrim").hidden = false;
  fillSelect($("#spBU"), "business_unit", "— none —");
  fillTypeSelect($("#spType"), "");
  fillSelect($("#spDept"), "department", "— none —");
  $("#spAutoSync").checked = false;
  if (spState.status?.connected) showSpBrowser();
  else { $("#spConnect").hidden = false; $("#spBrowser").hidden = true; $("#spFoot").hidden = true; }
}
function closeSharePoint() { $("#spScrim").hidden = true; }

async function connectSharePoint() {
  try {
    const r = await api("/api/sharepoint/login?return_to=/manage");
    window.open(r.authorize_url, "sp_oauth", "width=520,height=640");
  } catch (e) { toast("Could not start sign-in: " + e.message, true); }
}

function showSpBrowser() {
  $("#spConnect").hidden = true; $("#spBrowser").hidden = false; $("#spFoot").hidden = false;
  loadSpSites("");
}

function renderCrumbs() {
  const c = $("#spCrumbs"); c.replaceChildren();
  const crumb = (label, fn) => el("button", { class: "crumb", onclick: fn }, label);
  c.append(crumb("Sites", () => { spState.view = "sites"; spState.site = null; spState.drive = null; spState.path = []; loadSpSites(""); }));
  if (spState.site) {
    c.append(el("span", { class: "sep" }, "›"),
      crumb(spState.site.name, () => { spState.view = "drives"; spState.drive = null; spState.path = []; loadSpDrives(); }));
  }
  if (spState.drive) {
    c.append(el("span", { class: "sep" }, "›"),
      crumb(spState.drive.name, () => { spState.path = []; loadSpItems(); }));
    spState.path.forEach((f, i) => {
      c.append(el("span", { class: "sep" }, "›"),
        crumb(f.name, () => { spState.path = spState.path.slice(0, i + 1); loadSpItems(); }));
    });
  }
}

function updateSpSelCount() {
  const n = spState.selected.size;
  $("#spSelCount").textContent = n ? `${n} selected` : "Nothing selected";
  $("#spImport").disabled = n === 0 || !spState.drive;
}

function spListBusy() { $("#spList").replaceChildren(el("div", { class: "muted small sp-busy" }, "Loading…")); }
function handleSpErr(e) {
  if (e.status === 401) {
    toast("Please reconnect to SharePoint", true);
    $("#spConnect").hidden = false; $("#spBrowser").hidden = true; $("#spFoot").hidden = true;
    refreshSharePoint();
  } else toast("SharePoint error: " + e.message, true);
}

async function loadSpSites(q) {
  spState.view = "sites"; renderCrumbs();
  $("#spSearch").hidden = false; spListBusy();
  try {
    const { sites } = await api("/api/sharepoint/sites?q=" + encodeURIComponent(q || ""));
    const list = $("#spList"); list.replaceChildren();
    if (!sites.length) { list.append(el("div", { class: "empty" }, "No sites found.")); return; }
    sites.forEach((s) => list.append(el("div", { class: "sp-row folder", onclick: () => {
      spState.site = { id: s.id, name: s.name }; loadSpDrives();
    } }, el("span", { class: "sp-ic" }, "🏛"), el("span", { class: "sp-nm" }, s.name))));
  } catch (e) { handleSpErr(e); }
}

async function loadSpDrives() {
  spState.view = "drives"; spState.drive = null; spState.path = [];
  $("#spSearch").hidden = true; renderCrumbs(); spListBusy();
  try {
    const { drives } = await api("/api/sharepoint/drives?site_id=" + encodeURIComponent(spState.site.id));
    const list = $("#spList"); list.replaceChildren();
    if (!drives.length) { list.append(el("div", { class: "empty" }, "No document libraries.")); return; }
    drives.forEach((d) => list.append(el("div", { class: "sp-row folder", onclick: () => {
      spState.drive = { id: d.id, name: d.name }; spState.path = []; loadSpItems();
    } }, el("span", { class: "sp-ic" }, "🗂"), el("span", { class: "sp-nm" }, d.name))));
  } catch (e) { handleSpErr(e); }
}

async function loadSpItems() {
  spState.view = "items"; renderCrumbs(); spListBusy();
  const parent = spState.path.length ? spState.path[spState.path.length - 1].id : "";
  try {
    const { items } = await api(`/api/sharepoint/items?drive_id=${encodeURIComponent(spState.drive.id)}` +
      (parent ? `&item_id=${encodeURIComponent(parent)}` : ""));
    const list = $("#spList"); list.replaceChildren();
    if (!items.length) { list.append(el("div", { class: "empty" }, "Empty folder.")); return; }
    items.forEach((it) => {
      const checked = spState.selected.has(it.id);
      const cb = el("input", { type: "checkbox", ...(checked ? { checked: "" } : {}),
        onclick: (e) => { e.stopPropagation();
          if (e.target.checked) spState.selected.set(it.id, { id: it.id, name: it.name, is_folder: it.is_folder });
          else spState.selected.delete(it.id);
          updateSpSelCount(); } });
      const row = el("div", { class: "sp-row " + (it.is_folder ? "folder" : "file") },
        cb,
        el("span", { class: "sp-ic" }, it.is_folder ? "📁" : "📄"),
        el("span", { class: "sp-nm", onclick: () => {
          if (it.is_folder) { spState.path.push({ id: it.id, name: it.name }); loadSpItems(); }
        } }, it.name),
        el("span", { class: "sp-meta muted small" },
          it.is_folder ? (it.child_count != null ? `${it.child_count} items` : "folder") : fmtSize(it.size)));
      list.append(row);
    });
  } catch (e) { handleSpErr(e); }
}

const fmtSize = (n) => !n ? "" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";

async function doSharePointImport() {
  const selections = [...spState.selected.values()];
  if (!selections.length) return;
  const autosync = $("#spAutoSync").checked;
  const folderSel = selections.filter((s) => s.is_folder);
  if (autosync && folderSel.length !== 1) {
    toast("Auto-sync needs exactly one folder selected", true); return;
  }
  const bu = $("#spBU").value || null, dt = $("#spType").value || null, dept = $("#spDept").value || null;
  const btn = $("#spImport"); btn.disabled = true; btn.textContent = "Importing…";
  try {
    if (autosync) {
      const f = folderSel[0];
      await api("/api/sharepoint/syncs", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ site_id: spState.site.id, site_name: spState.site.name,
          drive_id: spState.drive.id, drive_name: spState.drive.name,
          folder_id: f.id, folder_name: f.name, business_unit: bu, document_type: dt, department: dept }) });
    }
    const res = await api("/api/sharepoint/import", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ drive_id: spState.drive.id, selections, source_id: "sp_import",
        business_unit: bu, document_type: dt, department: dept }) });
    toast(`Imported ${res.imported} new, ${res.duplicates} already stored${autosync ? " · auto-sync on" : ""}`);
    closeSharePoint(); loadManage();
  } catch (e) {
    if (e.status === 401) handleSpErr(e);
    else toast("Import failed: " + e.message, true);
  } finally { btn.disabled = false; btn.textContent = "Import selected"; }
}

document.addEventListener("DOMContentLoaded", init);
