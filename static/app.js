/* Document Hub — frontend. Vanilla JS, no build step. */
"use strict";

const state = {
  me: null,
  taxonomy: { department: [], document_type: [] },
  surface: "manage",
  queue: "unclassified",
  fieldsMode: false,
  selected: new Set(),
  currentDoc: null,
  rowById: new Map(),   // doc_id → last-rendered table row, for instant drawer previews
  classify: null,       // { ids: [...], idx } while walking selected docs through the drawer
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
const labelFor = (cat, val) => ((state.taxonomy[cat] || []).find((x) => x.value === val) || {}).label || val || "—";
const locationOf = (d) => {
  const parts = [d.sp_site_name, d.sp_path].filter(Boolean);
  return parts.length ? parts.join(" · ") : "—";
};
// Enclosing-folder URL for a SharePoint file link: drop the last path segment of the item's
// webUrl (e.g. ".../Docs/2023/file.pdf" → ".../Docs/2023"). Query/fragment stripped first.
// Returns null when there's no usable parent (no url, or the file sits at the web root).
const parentFolderUrl = (webUrl) => {
  if (!webUrl) return null;
  try {
    const u = new URL(webUrl);
    u.search = ""; u.hash = "";
    const segs = u.pathname.split("/").filter(Boolean);
    if (segs.length < 2) return null;   // nothing above the file to open
    segs.pop();
    u.pathname = "/" + segs.join("/");
    return u.toString();
  } catch { return null; }
};

// Small shared UI utilities ─────────────────────────────────
// Fire a handler on Enter/Space so role="button"/tabindex targets are keyboard-operable.
const activate = (fn) => (e) => {
  if (e.currentTarget === e.target && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); fn(); }
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
// Disable a button and swap its label while an async action is in flight — kills double-submits.
async function withBusy(btn, label, fn) {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = label;
  try { await fn(); } finally { if (btn.isConnected) { btn.disabled = false; btn.textContent = orig; } }
}

// Overlay a11y: Escape to close, focus into the overlay on open, trap Tab while open, and
// restore focus to the element that triggered it on close. Used by the drawer + both modals.
const overlayState = new Map();  // element → { closer, prevFocus, keyHandler }
const focusablesIn = (root) =>
  $$('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', root)
    .filter((n) => n.getClientRects().length > 0);
function openOverlay(elm, closer) {
  if (overlayState.has(elm)) return;  // already tracked (e.g. drawer re-render) — keep original trigger
  const prevFocus = document.activeElement;
  const keyHandler = (e) => {
    if (e.key === "Escape") { e.preventDefault(); closer(); return; }
    if (e.key !== "Tab") return;
    const f = focusablesIn(elm);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  elm.addEventListener("keydown", keyHandler);
  overlayState.set(elm, { closer, prevFocus, keyHandler });
  (focusablesIn(elm)[0] || elm).focus();
}
function closeOverlay(elm) {
  const s = overlayState.get(elm);
  if (!s) return;
  elm.removeEventListener("keydown", s.keyHandler);
  overlayState.delete(elm);
  if (s.prevFocus && s.prevFocus.focus) s.prevFocus.focus();
}

// ─────────────────────────────────────────────── surfaces ──
function setSurface(name) {
  state.surface = name;
  localStorage.setItem("dochub.surface", name);
  $$(".surface-tab").forEach((b) => {
    const on = b.dataset.surface === name;
    b.classList.toggle("active", on); b.setAttribute("aria-selected", on ? "true" : "false");
  });
  $("#surface-manage").hidden = name !== "manage";
  $("#surface-explore").hidden = name !== "explore";
  const path = "/" + name;
  if (location.pathname !== path) history.replaceState({}, "", path);
  if (name === "manage") { setFieldsMode(false); loadManage(); }  // explicit nav lands on the table
  else loadExploreFilters();
}

// ─────────────────────────────────────────────── init ──
async function init() {
  $$(".surface-tab").forEach((b) => b.addEventListener("click", () => setSurface(b.dataset.surface)));
  wireUpload();
  wireQueueTabs();
  wireDrawer();
  wireExplore();
  wireSharePoint();
  wireFields();

  // Paint the shell (with loading placeholders) immediately so the page never sits blank
  // while the warehouse resumes. Identity + taxonomy load in parallel and fill in after.
  const start = location.pathname.startsWith("/explore")
    ? "explore"
    : location.pathname.startsWith("/manage")
    ? "manage"
    : localStorage.getItem("dochub.surface") || "manage";
  $("#spEntry").hidden = false;          // show the import affordance greyed until status lands
  $("#spImportBtn").disabled = true;
  setSurface(start);

  try {
    const [me, tax] = await Promise.all([api("/api/me"), api("/api/taxonomy")]);
    state.me = me; state.taxonomy = tax;
    $("#userChip").textContent = me.email + (me.is_admin ? " · admin" : "");
    // The "Modify fields" toggle lives in the queue header; reveal it once identity is known.
    if (me.is_admin && state.surface === "manage") $("#modifyFieldsBtn").hidden = false;
    if (state.surface === "explore") loadExploreFilters();  // refill filters now taxonomy is in
  } catch (e) { toast("Load failed: " + e.message, true); }
}

// ─────────────────────────────────────────────── MANAGE ──
// Data refresh only — repaints stats/table/sync without leaving the fields editor. Explicit
// navigation (surface switch, queue tab, "Done") is what returns to the table; a background
// import poll or upload must not evict an admin who's mid-edit in the fields panel.
async function loadManage() {
  if (state.me?.is_admin) $("#modifyFieldsBtn").hidden = false;
  loadStats();
  loadDocs();
  refreshSharePoint();
}
// Manage has two mutually-exclusive views: the document queue (table) and the extraction-
// fields editor. "Modify fields" swaps to the editor and collapses the table; picking a queue
// tab / stat card swaps back. Admin-only (the button is hidden otherwise).
function setFieldsMode(on) {
  state.fieldsMode = on;
  $("#docTableWrap").hidden = on;
  $("#fieldsPanel").hidden = !on;
  $("#modifyFieldsBtn").classList.toggle("active", on);
  if (on) {
    loadAccess();
    loadFieldDefs();
    $("#fieldsPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}
function skeletonStatCards() {
  $("#statRow").replaceChildren(
    ...["Needs classification", "Needs review", "Verified"].map((l) =>
      el("div", { class: "stat-card" },
        el("div", { class: "n skel skel-n" }, ""), el("div", { class: "l" }, l)))
  );
}
async function loadStats() {
  if (!$("#statRow").children.length) skeletonStatCards();
  try {
    const s = await api("/api/stats");
    const cards = [
      ["Needs classification", s.unclassified, "unclassified"],
      ["Needs review", s.by_status.needs_review || 0, "needs_review"],
      ["Verified", s.by_status.verified || 0, "verified"],
    ];
    $("#statRow").replaceChildren(
      ...cards.map(([l, n, q]) =>
        el("div", { class: "stat-card" + (state.queue === q ? " active" : ""),
          role: "button", tabindex: "0", "aria-pressed": state.queue === q ? "true" : "false",
          onclick: () => selectQueue(q), onkeydown: activate(() => selectQueue(q)) },
          el("div", { class: "n" }, String(n)), el("div", { class: "l" }, l))
      )
    );
  } catch (e) {
    // Don't leave the shimmer forever — show "—" cards the user can click to retry.
    $("#statRow").replaceChildren(
      ...["Needs classification", "Needs review", "Verified"].map((l) =>
        el("div", { class: "stat-card err", role: "button", tabindex: "0",
          title: "Couldn't load — click to retry", onclick: loadStats, onkeydown: activate(loadStats) },
          el("div", { class: "n" }, "—"), el("div", { class: "l" }, l)))
    );
  }
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
  if (state.fieldsMode) setFieldsMode(false);  // a queue tab / stat card returns to the table
  state.queue = q;
  state.selected.clear(); updateBulkBar();
  $$("#queueTabs .qtab").forEach((b) => {
    const on = b.dataset.queue === q;
    b.classList.toggle("active", on); b.setAttribute("aria-selected", on ? "true" : "false");
  });
  // keep the top stat cards' highlight in sync with the active queue
  $$("#statRow .stat-card").forEach((c, i) =>
    c.classList.toggle("active", ["unclassified", "needs_review", "verified"][i] === q));
  loadDocs();
}
function skeletonDocRows(n = 5) {
  const cell = (w) => el("td", {}, el("span", { class: "skel", style: `width:${w}` }, ""));
  $("#docRows").replaceChildren(...Array.from({ length: n }, () =>
    el("tr", { class: "skel-row" },
      el("td", { class: "col-check" }, el("span", { class: "skel", style: "width:16px" }, "")),
      cell("70%"), cell("55%"), cell("40%"), cell("50%"))));
}
async function loadDocs() {
  const q = state.queue;
  let path = "/api/documents";
  const params = new URLSearchParams();
  if (q === "unclassified") params.set("classification_status", "unclassified");
  else if (q !== "all") params.set("verification_status", q);
  if ([...params].length) path += "?" + params;
  const tb = $("#docRows");
  $("#docEmpty").hidden = true;
  if (!tb.children.length) skeletonDocRows();
  let rows;
  try {
    rows = await api(path);
  } catch (e) {
    // Surface the failure with a retry instead of leaving skeleton rows spinning forever.
    tb.replaceChildren(el("tr", {}, el("td", { class: "empty", colspan: "5" },
      "Couldn't load documents. ",
      el("button", { class: "link-btn", onclick: loadDocs }, "Retry"))));
    $("#docEmpty").hidden = true;
    return;
  }
  tb.replaceChildren();
  state.rowById.clear();
  $("#docEmpty").hidden = rows.length > 0;
  for (const d of rows) {
    state.rowById.set(d.doc_id, d);
    const cb = el("input", { type: "checkbox", "data-id": d.doc_id });
    const setSel = (on) => { on ? state.selected.add(d.doc_id) : state.selected.delete(d.doc_id); updateBulkBar(); };
    cb.onchange = () => setSel(cb.checked);
    // Toggle from anywhere in the cell, not just the tiny box, and never let a select-click
    // fall through to the row (which opens the doc) — that mis-fire is why selecting felt flaky.
    const checkCell = el("td", { class: "col-check", onclick: (e) => {
      e.stopPropagation();
      if (e.target !== cb) { cb.checked = !cb.checked; setSel(cb.checked); }
    } }, cb);
    tb.append(el("tr", { role: "button", tabindex: "0",
      onclick: () => openDoc(d.doc_id, d), onkeydown: activate(() => openDoc(d.doc_id, d)) },
      checkCell,
      el("td", {}, el("span", { class: "doc-name" }, d.original_filename || "(unnamed)")),
      el("td", {}, el("span", { class: "loc muted small", title: locationOf(d) },
        locationOf(d))),
      el("td", {}, d.document_type || "—"),
      el("td", {}, stageBadge(d))));
  }
  $("#selectAll").checked = false;
}
function updateBulkBar() {
  const n = state.selected.size;
  $("#bulkBar").hidden = n === 0;
  $("#bulkCount").textContent = `${n} selected`;
}
// One badge tells the doc's whole stage: classify → extract → review → verified.
function stageBadge(d) {
  if (d.classification_status !== "classified")
    return el("span", { class: "badge gray" }, "Awaiting classification");
  if (d.verification_status === "verified")
    return el("span", { class: "badge green" }, "Verified");
  if (d.extraction_status === "pending" || d.extraction_status === "processing")
    return el("span", { class: "badge amber" }, "Pending extraction");
  if (d.extraction_status === "failed")
    return el("span", { class: "badge red" }, "Extraction failed");
  return el("span", { class: "badge blue" }, "Review");
}

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
  // Refresh the site list when the local-upload panel is opened, so newly-imported sites appear.
  const local = $(".upload-local");
  if (local) local.addEventListener("toggle", () => { if (local.open) loadUploadSites(); });
  loadUploadSites();
}
// A local upload has no SharePoint home of its own, so it must be filed under a site the
// uploader can access — it then inherits that site's audience. Options come from the sites
// already visible to the user (site-scoped server-side).
async function loadUploadSites() {
  const sel = $("#uploadSite");
  if (!sel) return;
  try {
    const { sites } = await api("/api/sites");
    const chosen = sel.value;
    sel.replaceChildren(
      el("option", { value: "" }, "Choose a site…"),
      ...sites.map((s) => el("option", { value: s.id, "data-name": s.name || "" }, s.name || s.id)));
    if (chosen) sel.value = chosen;
  } catch { /* leave the placeholder; uploadFiles() prompts if no site is chosen */ }
}
async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const sel = $("#uploadSite");
  const siteId = sel ? sel.value : "";
  if (!siteId) {
    toast("Choose a site to file the upload under.", true);
    const local = $(".upload-local"); if (local) local.open = true;
    if (sel) sel.focus();
    return;
  }
  const siteName = sel.selectedOptions[0]?.dataset.name || "";
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  fd.append("sp_site_id", siteId);
  fd.append("sp_site_name", siteName);
  const box = $("#uploadResults");
  box.replaceChildren(el("div", { class: "up-item" }, `Uploading ${files.length} file(s)…`));
  // Optimistic: drop each file into the table right away with an "Uploading" status so the
  // user sees it land. loadManage() below replaces these with the authoritative rows.
  if (state.surface === "manage") {
    $("#docEmpty").hidden = true;
    const tb = $("#docRows");
    files.forEach((f) => tb.prepend(el("tr", { class: "uploading-row" },
      el("td", { class: "col-check" }, ""),
      el("td", {}, el("span", { class: "doc-name" }, f.name)),
      el("td", {}, el("span", { class: "muted small" }, "—")),
      el("td", {}, "—"),
      el("td", {}, el("span", { class: "badge blue" }, "Uploading…")))));
  }
  try {
    const res = await api("/api/upload", { method: "POST", body: fd });
    box.replaceChildren(...res.results.map((r) => {
      const isNew = r.status === "new";
      // For a duplicate, surface the existing doc's state so the reviewer sees the free
      // reuse (SPEC §9): an identical, already-verified doc means this upload inherits the
      // OCR/text, extracted fields, and confirmed values at zero cost.
      const reuse = r.existing_status === "verified"
        ? " · already verified — reused for free"
        : " · already extracted — reused";
      return el("div", { class: "up-item " + (isNew ? "new" : "dup") },
        el("span", { class: "tag" }, isNew ? "NEW" : "DUPLICATE"),
        el("span", {}, r.filename),
        isNew ? "" : el("span", { class: "muted" }, `— already stored as "${r.existing_name}"${reuse}`));
    }));
    toast(`${res.results.filter((r) => r.status === "new").length} new, ` +
      `${res.results.filter((r) => r.status === "duplicate").length} duplicate`);
    loadManage();
  } catch (e) { toast("Upload failed: " + e.message, true); box.replaceChildren(); }
}

// ─────────────────────────────────────────────── BULK CLASSIFY ──
function fillSelect(sel, cat, placeholder) {
  sel.replaceChildren(el("option", { value: "" }, placeholder));
  (state.taxonomy[cat] || []).forEach((o) => sel.append(el("option", { value: o.value }, o.label)));
}
function fillTypeSelect(sel) {
  sel.replaceChildren(el("option", { value: "" }, "— select —"));
  (state.taxonomy.document_type || [])
    .forEach((o) => sel.append(el("option", { value: o.value }, o.label)));
}
function mostCommon(vals) {
  const counts = new Map();
  vals.forEach((v) => counts.set(v, (counts.get(v) || 0) + 1));
  let best = null, n = 0;
  for (const [v, c] of counts) if (c > n) { best = v; n = c; }
  return best;
}
// "Classify selected…" opens a bulk panel in the detail drawer: pick one type/department for
// the whole batch and apply in one click. You can glance at any file (title in the list →
// first page in the left viewer) if you want to, but you don't have to open each one. The
// type defaults to the most common name-based guess across the selection.
function openClassify() {
  const ids = [...state.selected];
  if (!ids.length) return;
  state.classify = { ids, focus: null, drawList: null };
  renderBulkClassify();
}
function renderBulkClassify() {
  const ids = state.classify.ids;
  $("#drawer").hidden = false; $("#drawerScrim").hidden = false;
  openOverlay($("#drawer"), closeDrawer);
  $("#drawerTitle").textContent = `Classify ${ids.length} document${ids.length > 1 ? "s" : ""}`;
  $("#drawerSub").textContent = "Set a type and department for the batch — preview any file on the left if you want.";
  const body = $("#drawerBody"); body.replaceChildren();
  $("#reviewFrame").hidden = true; $("#reviewNoPrev").hidden = false;  // until a file is focused

  const guess = mostCommon(ids.map((id) => state.rowById.get(id)?.document_type).filter(Boolean));
  const typeSel = el("select", {}); fillTypeSelect(typeSel); if (guess) typeSel.value = guess;
  const deptSel = el("select", {}); fillSelect(deptSel, "department", "— none —");
  body.append(
    el("div", { class: "section-label" }, "Apply to the whole batch"),
    el("div", { class: "field-row" }, el("label", {}, "Document type"), typeSel),
    el("div", { class: "field-row" }, el("label", {}, "Department"), deptSel));
  if (guess)
    body.append(el("div", { class: "prov" }, "Type pre-filled from the most common file-name guess — change it to override the batch."));

  // Which fields the chosen type will extract — mirrors the single-doc classify panel.
  const preview = el("div", { class: "extract-preview" });
  body.append(el("div", { class: "section-label" }, "Fields to be extracted"), preview);
  const loadFieldsPreview = async () => {
    const dt = typeSel.value;
    if (!dt) { preview.replaceChildren(el("div", { class: "muted small" },
      "Pick a document type to see the fields that will be extracted.")); return; }
    preview.replaceChildren(el("div", { class: "muted small" }, "Loading fields…"));
    try { renderExtractPreview(preview, await api("/api/field-defs?document_type=" + encodeURIComponent(dt))); }
    catch (e) { preview.replaceChildren(el("div", { class: "muted small" }, "Could not load fields.")); }
  };
  typeSel.addEventListener("change", loadFieldsPreview); loadFieldsPreview();

  // The batch as a clickable list: click a row to load its first page on the left; ✕ drops a
  // doc from the batch (so an outlier you spot while glancing can be handled separately).
  body.append(el("div", { class: "section-label" }, "Documents in this batch"));
  const list = el("div", { class: "bulk-doc-list" });
  const drawList = () => {
    list.replaceChildren();
    state.classify.ids.forEach((id) => {
      const row = state.rowById.get(id) || { doc_id: id };
      list.append(el("div", { class: "bulk-doc" + (id === state.classify.focus ? " active" : "") },
        el("button", { class: "bulk-doc-main", onclick: () => focusBulkDoc(id) },
          el("span", { class: "doc-name" }, row.original_filename || id),
          el("span", { class: "muted small" }, locationOf(row) + " · " + (row.document_type || "unclassified"))),
        el("button", { class: "tag-x", title: "Remove from batch", onclick: () => dropBulkDoc(id) }, "✕")));
    });
  };
  state.classify.drawList = drawList; drawList();
  body.append(list);

  $("#drawerFoot").replaceChildren(
    el("button", { class: "btn primary",
      onclick: (e) => withBusy(e.currentTarget, "Classifying…",
        () => applyBulkClassify(typeSel.value || null, deptSel.value || null)) },
      "Classify & queue extraction"));

  focusBulkDoc(ids[0]);  // lazy-preview the first so the viewer isn't empty
}
function focusBulkDoc(id) {
  if (!state.classify) return;
  state.classify.focus = id;
  const row = state.rowById.get(id);
  if (row) { $("#drawerSub").textContent = "Previewing: " + (row.original_filename || id); loadPreview(row); }
  state.classify.drawList?.();
}
function dropBulkDoc(id) {
  if (!state.classify) return;
  state.classify.ids = state.classify.ids.filter((x) => x !== id);
  state.selected.delete(id); updateBulkBar();
  if (!state.classify.ids.length) { closeDrawer(); loadManage(); return; }
  if (state.classify.focus === id) { renderBulkClassify(); return; }  // refresh header count + list
  state.classify.drawList?.();
}
async function applyBulkClassify(dt, dept) {
  if (!dt && !dept) { toast("Pick at least a type or department", true); return; }
  const ids = state.classify.ids;
  try {
    await api("/api/documents/classify", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ doc_ids: ids, document_type: dt, department: dept }) });
    await api("/api/documents/enqueue", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ doc_ids: ids }) });
    toast(`Classified and queued ${ids.length} document${ids.length > 1 ? "s" : ""}`);
    state.classify = null; state.selected.clear(); updateBulkBar();
    closeDrawer(); loadManage();
  } catch (e) { toast("Failed: " + e.message, true); }
}

// ─────────────────────────────────────────────── DRAWER (detail/verify) ──
function wireDrawer() {
  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#drawerScrim").addEventListener("click", closeDrawer);
}
function closeDrawer() {
  closeOverlay($("#drawer"));
  $("#drawer").hidden = true; $("#drawerScrim").hidden = true;
  const frame = $("#reviewFrame"); frame.removeAttribute("srcdoc"); frame.src = "about:blank"; frame.dataset.src = "";  // free the viewer
  state.currentDoc = null;
  state.classify = null;   // closing mid-walkthrough ends it
}
// Open instantly with whatever the clicked row already knows (title + viewer), then fill the
// fields panel from the detail fetch. Avoids a multi-query wait before anything appears.
async function openDoc(docId, row, opts = {}) {
  const readOnly = !!opts.readOnly;   // Explore opens the drawer as read-only discovery
  $("#drawer").hidden = false; $("#drawerScrim").hidden = false;
  openOverlay($("#drawer"), closeDrawer);
  if (row) {
    $("#drawerTitle").textContent = row.original_filename || "Document";
    $("#drawerSub").textContent = `${locationOf(row)} · ${row.document_type || "unclassified"}`;
    loadPreview(row);
  }
  $("#drawerBody").replaceChildren(el("div", { class: "muted small" }, "Loading…"));
  $("#drawerFoot").replaceChildren();
  try {
    const data = await api("/api/documents/" + docId);
    if ($("#drawer").hidden) return;  // user closed it while loading
    state.currentDoc = data;
    renderDrawer(data, readOnly);
    loadPreview(data.document);  // refine with authoritative mime/derived
  } catch (e) { toast("Could not open: " + e.message, true); closeDrawer(); }
}
// Office docs the server can render to standalone HTML via /api/render.
const OFFICE_EXTS = [".docx", ".xlsx", ".xlsm"];
const isOfficeRenderable = (name) => {
  const n = (name || "").toLowerCase();
  return OFFICE_EXTS.some((e) => n.endsWith(e));
};
// Load the file into the left-hand viewer. PDFs (incl. the derived searchable PDF) and images
// embed via the download endpoint; Office docs (docx/xlsx/xlsm) fetch server-rendered HTML into
// the iframe's srcdoc; anything else falls back to the View/Download actions. Idempotent:
// re-calling with the same target (row → detail refine) won't reload the iframe.
function loadPreview(d) {
  const frame = $("#reviewFrame"), noprev = $("#reviewNoPrev");
  const mime = (d.mime_type || "").toLowerCase();
  const embeddable = !!d.derived_pdf_path || mime === "application/pdf" || mime.startsWith("image/");
  const office = !embeddable && isOfficeRenderable(d.original_filename);
  // Synthetic dataset keys keep row→detail refines idempotent while still reloading when the
  // preview mode changes (download-embed ↔ srcdoc-render ↔ none).
  const target = embeddable ? `dl:${d.doc_id}` : office ? `office:${d.doc_id}` : "";
  if (frame.dataset.src === target) return;
  frame.dataset.src = target;
  if (embeddable) {
    frame.hidden = false; noprev.hidden = true;
    frame.removeAttribute("srcdoc");
    frame.src = `/api/download?doc_id=${d.doc_id}&inline=1`;
  } else if (office) {
    frame.hidden = false; noprev.hidden = true;
    frame.src = "about:blank";
    frame.srcdoc = "<!doctype html><body style=\"font:15px system-ui,sans-serif;color:#cbd5e1;" +
      "background:#3a3f44;display:flex;align-items:center;justify-content:center;height:100vh;" +
      "margin:0\">Rendering preview…</body>";
    fetch(`/api/render?doc_id=${d.doc_id}`)
      .then((r) => { if (!r.ok) throw new Error("render"); return r.text(); })
      .then((html) => { if (frame.dataset.src === target) frame.srcdoc = html; })
      .catch(() => {
        if (frame.dataset.src !== target) return;
        frame.removeAttribute("srcdoc"); frame.src = "about:blank";
        frame.hidden = true; noprev.hidden = false;
      });
  } else {
    frame.hidden = true; noprev.hidden = false;
    frame.removeAttribute("srcdoc"); frame.src = "about:blank";
  }
}
function renderDrawer(data, readOnly = false) {
  const d = data.document;
  $("#drawerTitle").textContent = d.original_filename || "Document";
  $("#drawerSub").textContent =
    `${locationOf(d)} · ${d.document_type || "unclassified"}`;
  const body = $("#drawerBody"); body.replaceChildren();

  // The viewer embeds PDFs, images and the derived searchable PDF directly, and renders Office
  // docs (docx/xlsx/xlsm) to HTML — all are "viewable". When nothing can be shown inline, offer
  // the original for download rather than a "View file" that opens blank.
  const mime = (d.mime_type || "").toLowerCase();
  const embeddable = !!d.derived_pdf_path || mime === "application/pdf" || mime.startsWith("image/");
  const office = !embeddable && isOfficeRenderable(d.original_filename);
  const viewable = embeddable || office;
  const viewHref = office ? `/api/render?doc_id=${d.doc_id}` : `/api/download?doc_id=${d.doc_id}&inline=1`;
  const actions = el("div", { class: "field-actions" },
    viewable
      ? el("a", { class: "btn", href: viewHref, target: "_blank" }, "View file")
      : el("a", { class: "btn", href: `/api/download?doc_id=${d.doc_id}` }, "Download original"));
  if (viewable)  // keep a plain download alongside the inline view; redundant when not viewable
    actions.append(el("a", { class: "btn", href: `/api/download?doc_id=${d.doc_id}` }, "Download"));
  if (d.sp_web_url) {
    actions.append(el("a", { class: "btn", href: d.sp_web_url, target: "_blank" }, "Open in SharePoint"));
    const folder = parentFolderUrl(d.sp_web_url);
    if (folder)
      actions.append(el("a", { class: "btn", href: folder, target: "_blank" }, "Open folder"));
  }
  body.append(actions);

  // Explore is read-only discovery for everyone: no field editor, no Save/Verify — just the
  // file actions and read-only tags.
  if (readOnly) {
    body.append(el("div", { class: "section-label" }, "Tags"));
    const tags = data.tags || [];
    body.append(tags.length
      ? el("div", { class: "tag-chips" }, ...tags.map((t) => el("span", { class: "tag-chip" }, t)))
      : el("div", { class: "muted small" }, "No tags."));
    body.append(el("div", { class: "section-label" }, "Related documents"));
    const treeBox = el("div", { class: "rel-tree" });
    body.append(treeBox);
    renderRelationTree(treeBox, d.doc_id, true);
    $("#drawerFoot").replaceChildren();
    return;
  }

  // Unclassified docs get a classify-first panel (no extracted fields yet); once classified
  // they leave this queue and enter review with the full field editor.
  if (d.classification_status !== "classified") { renderClassify(body, data); return; }

  body.append(el("div", { class: "section-label" }, "Tags"));
  body.append(renderTags(d.doc_id, data.tags || []));

  const hasAi = data.fields.some((f) => f.proposed_value && !f.confirmed_value);
  body.append(el("div", { class: "section-label" }, "Extracted fields"));
  // One legend for the whole section rather than a note under every field: AI-suggested
  // values are tinted; confirmed/edited ones use the normal field styling.
  if (hasAi)
    body.append(el("div", { class: "ai-legend small" },
      el("span", { class: "ai-dot" }), "Tinted fields are AI-suggested — review and confirm."));
  if (d.extraction_status !== "done")
    body.append(el("div", { class: "muted small" },
      d.extraction_status === "failed" ? "Extraction failed — you can still enter fields manually."
      : "Extraction not finished yet. Values will appear once processing completes."));

  for (const f of data.fields) {
    const val = f.confirmed_value ?? f.proposed_value ?? "";
    const isAi = !!(f.proposed_value && !f.confirmed_value);
    const missing = f.required_for_verify && val === "";
    const row = el("div", { class: "field-row" + (f.required_for_verify ? " req" : "") +
      (isAi ? " ai" : "") + (missing ? " missing" : "") });
    row.append(el("label", {}, f.label));
    // Multi-value fields (topics, parties) come back as JSON arrays — render them as an
    // editable chip list rather than dumping raw JSON into a text box. Also normalise the
    // legacy [{name,role}] shape into "Name — role" strings for display.
    const list = asStringList(val, f.data_type);
    let input;
    if (f.options) {
      input = el("select", { "data-key": f.field_key });
      input.append(el("option", { value: "" }, "—"));
      f.options.forEach((o) => input.append(el("option", { value: o, ...(o === val ? { selected: "" } : {}) }, o)));
    } else if (list) {
      input = listEditor(f.field_key, list);
    } else if (f.data_type === "summary" || f.data_type === "long_text") {
      input = el("textarea", { "data-key": f.field_key,
        class: f.data_type === "summary" ? "summary-box" : "" }); input.value = val;
    } else {
      input = el("input", { type: f.data_type === "date" ? "date" : "text", "data-key": f.field_key });
      input.value = val;
    }
    // Remember the value we rendered so we only save fields the user actually changed —
    // otherwise a single edit would stamp every field 'human' and confirm all AI guesses.
    if (input.hasAttribute("data-key")) input.dataset.orig = input.value;
    row.append(input);
    // Per-field AI note removed in favour of the section legend + tint; keep the human marker.
    if (!isAi && f.source_provenance === "human" && f.confirmed_value)
      row.append(el("div", { class: "prov" }, "Edited by a person"));
    body.append(row);
  }

  body.append(el("div", { class: "section-label" }, "Related documents"));
  const treeBox = el("div", { class: "rel-tree" });
  body.append(treeBox);
  renderRelationTree(treeBox, d.doc_id, false);
  const needsParent = () => d.document_type === "Amendment" &&
    !data.links.some((l) => l.relationship === "amendment_of");
  const warn = el("div", { class: "prov", style: "color:var(--warn)" },
    "Amendments must be linked to a parent contract before they can be verified.");
  warn.hidden = !needsParent();
  body.append(renderLinkAdder(d.doc_id, d.document_type, (newLink) => {
    data.links.push(newLink); renderRelationTree(treeBox, d.doc_id, false); warn.hidden = !needsParent();
  }));
  body.append(warn);

  // footer actions
  const foot = $("#drawerFoot"); foot.replaceChildren(
    el("button", { class: "btn", onclick: (e) => withBusy(e.currentTarget, "Saving…", saveFields) }, "Save"),
    d.verification_status === "verified"
      ? el("button", { class: "btn", onclick: (e) => withBusy(e.currentTarget, "Working…", () => setVerify(false)) }, "Un-verify")
      : el("button", { class: "btn ok", onclick: (e) => withBusy(e.currentTarget, "Verifying…", () => setVerify(true)) }, "Save & verify"));
}
// Right-hand panel for an unclassified doc: pick type/department, then classify + queue
// extraction in one step. The viewer on the left lets the user read the doc while deciding.
function renderClassify(body, data) {
  const d = data.document;
  body.append(el("div", { class: "section-label" }, "Classify this document"));
  body.append(el("p", { class: "muted small" },
    "Choose a type to queue extraction and move this into review."));

  const typeSel = el("select", {}); fillTypeSelect(typeSel); typeSel.value = d.document_type || "";
  const deptSel = el("select", {}); fillSelect(deptSel, "department", "— none —"); deptSel.value = d.department || "";
  const typeRow = el("div", { class: "field-row" }, el("label", {}, "Document type"), typeSel);
  if (d.document_type)  // pre-filled from the name/path guess at ingest time
    typeRow.append(el("div", { class: "prov" }, "Suggested from the file name — confirm or change."));
  const deptRow = el("div", { class: "field-row" }, el("label", {}, "Department"), deptSel);
  body.append(typeRow, deptRow);

  body.append(el("div", { class: "section-label" }, "Tags"));
  body.append(renderTags(d.doc_id, data.tags || []));

  // Preview of what extraction will pull for the chosen type — updates as the type changes.
  const preview = el("div", { class: "extract-preview" });
  body.append(el("div", { class: "section-label" }, "Fields to be extracted"), preview);
  const loadPreview = async () => {
    const dt = typeSel.value;
    if (!dt) { preview.replaceChildren(el("div", { class: "muted small" },
      "Pick a document type to see the fields that will be extracted.")); return; }
    preview.replaceChildren(el("div", { class: "muted small" }, "Loading fields…"));
    try {
      const defs = await api("/api/field-defs?document_type=" + encodeURIComponent(dt));
      renderExtractPreview(preview, defs);
    } catch (e) { preview.replaceChildren(el("div", { class: "muted small" }, "Could not load fields.")); }
  };
  typeSel.addEventListener("change", loadPreview);
  loadPreview();

  $("#drawerFoot").replaceChildren(
    el("button", { class: "btn primary",
      onclick: (e) => withBusy(e.currentTarget, "Classifying…",
        () => classifyOne(d.doc_id, typeSel.value || null, deptSel.value || null)) },
      "Classify & queue extraction"));
}
function renderExtractPreview(container, defs) {
  container.replaceChildren();
  if (!defs.length) { container.append(el("div", { class: "muted small" }, "No fields defined.")); return; }
  defs.forEach((f) => {
    container.append(el("div", { class: "extract-field" },
      el("div", { class: "ef-head" },
        el("span", { class: "ef-label" }, f.label + (f.required_for_verify ? " *" : "")),
        el("span", { class: "ef-type" }, TYPE_LABELS[f.data_type] || f.data_type)),
      f.extraction_prompt_hint
        ? el("div", { class: "ef-hint muted small" }, f.extraction_prompt_hint) : null));
  });
}
async function classifyOne(id, dt, dept) {
  if (!dt && !dept) { toast("Pick at least a type or department", true); return; }
  try {
    await api("/api/documents/classify", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ doc_ids: [id], document_type: dt, department: dept }) });
    await api("/api/documents/enqueue", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ doc_ids: [id] }) });
    toast("Classified and queued for extraction");
    closeDrawer(); loadManage();
  } catch (e) { toast("Failed: " + e.message, true); }
}
function renderTags(docId, tags) {
  const wrap = el("div", { class: "tag-editor" });
  const chips = el("div", { class: "tag-chips" });
  const draw = () => {
    chips.replaceChildren(...tags.map((t) =>
      el("span", { class: "tag-chip" }, t,
        el("button", { class: "tag-x", title: "Remove", onclick: async () => {
          try {
            await api(`/api/documents/${docId}/tags/${encodeURIComponent(t)}`, { method: "DELETE" });
            tags = tags.filter((x) => x !== t); draw();
          } catch (e) { toast("Failed: " + e.message, true); }
        } }, "✕"))));
    if (!tags.length) chips.append(el("span", { class: "muted small" }, "No tags yet."));
  };
  draw();
  const input = el("input", { type: "text", placeholder: "Add a tag and press Enter",
    onkeydown: async (e) => {
      if (e.key !== "Enter") return;
      const v = input.value.trim();
      if (!v || tags.includes(v)) { input.value = ""; return; }
      try {
        await api(`/api/documents/${docId}/tags`, { method: "POST",
          headers: { "content-type": "application/json" }, body: JSON.stringify({ tag: v }) });
        tags.push(v); input.value = ""; draw();
      } catch (e2) { toast("Failed: " + e2.message, true); }
    } });
  wrap.append(chips, input);
  return wrap;
}
// Turn a stored field value into a list of display strings, or null if it isn't list-shaped.
// `multi` fields are always lists (empty when blank); other types only when the value is a
// JSON array (e.g. a legacy [{name,role}] parties value we want to show cleanly).
function asStringList(val, dataType) {
  if (val === "" || val == null) return dataType === "multi" ? [] : null;
  let arr = val;
  if (typeof val === "string") {
    const s = val.trim();
    if (!s.startsWith("[")) return dataType === "multi" ? [val] : null;
    try { arr = JSON.parse(s); } catch { return null; }
  }
  if (!Array.isArray(arr)) return null;
  return arr.map(itemToStr).filter((x) => x !== "");
}
function itemToStr(item) {
  if (item == null) return "";
  if (typeof item === "object") {
    if (item.name && item.role) return `${item.name} — ${item.role}`;
    if (item.name) return String(item.name);
    return Object.values(item).filter(Boolean).map(String).join(" — ");
  }
  return String(item);
}
// Editable chip list backed by a hidden input[data-key] holding the JSON array, so the
// normal collectFieldValues()/save path stores it as a JSON string like everything else.
function listEditor(key, items) {
  const wrap = el("div", { class: "list-editor" });
  const hidden = el("input", { type: "hidden", "data-key": key });
  const chips = el("div", { class: "tag-chips" });
  const sync = () => (hidden.value = JSON.stringify(items));
  const draw = () => {
    chips.replaceChildren(...items.map((t, i) =>
      el("span", { class: "tag-chip" }, t,
        el("button", { class: "tag-x", title: "Remove", type: "button",
          onclick: () => { items.splice(i, 1); draw(); sync(); } }, "✕"))));
    if (!items.length) chips.append(el("span", { class: "muted small" }, "None."));
  };
  const input = el("input", { type: "text", placeholder: "Add and press Enter",
    onkeydown: (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const v = input.value.trim();
      if (!v || items.includes(v)) { input.value = ""; return; }
      items.push(v); input.value = ""; draw(); sync();
    } });
  draw(); sync();
  hidden.dataset.orig = hidden.value;  // baseline for dirty-tracking (see collectFieldValues)
  wrap.append(hidden, chips, input);
  return wrap;
}
// Inline "link this document to another" control. amendment_of / attachment_of point the
// current doc *up* to a parent (current = child); other relationships treat current as parent.
function renderLinkAdder(docId, dtype, onLinked) {
  const wrap = el("div", { class: "link-adder" });
  const rel = el("select", { class: "link-rel-sel" });
  const opts = dtype === "Amendment"
    ? [["amendment_of", "Amendment of (parent contract)"], ["related", "Related to"]]
    : [["related", "Related to"], ["attachment_of", "Attachment of"],
       ["amendment_of", "Amendment of"], ["supersedes", "Supersedes"]];
  opts.forEach(([v, l]) => rel.append(el("option", { value: v }, l)));
  const search = el("input", { type: "text", placeholder: "Find a document by name…" });
  const results = el("div", { class: "link-results", hidden: true });
  let cache = null;
  const ensure = async () => (cache ||= await api("/api/documents"));
  const doAdd = async (target) => {
    const relationship = rel.value;
    const [parent, child] = (relationship === "amendment_of" || relationship === "attachment_of")
      ? [target.doc_id, docId] : [docId, target.doc_id];
    try {
      await api("/api/links", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ parent_doc_id: parent, child_doc_id: child, relationship }) });
      toast("Linked");
      search.value = ""; results.hidden = true;
      onLinked({ relationship, original_filename: target.original_filename,
        document_type: target.document_type });
    } catch (e) { toast("Failed: " + e.message, true); }
  };
  let t;
  search.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = search.value.trim().toLowerCase();
      if (!q) { results.hidden = true; return; }
      let docs;
      try { docs = await ensure(); } catch { return; }
      const hits = docs.filter((x) => x.doc_id !== docId &&
        (x.original_filename || "").toLowerCase().includes(q)).slice(0, 8);
      results.replaceChildren(...hits.map((x) => el("div", { class: "link-result",
        onclick: () => doAdd(x) },
        el("span", { class: "doc-name small" }, x.original_filename || "(unnamed)"),
        el("span", { class: "muted small" }, " " + (x.document_type || "—")))));
      if (!hits.length) results.replaceChildren(el("div", { class: "muted small link-result" }, "No matches."));
      results.hidden = false;
    }, 250);
  });
  wrap.append(el("div", { class: "link-adder-row" }, rel, search), results);
  return wrap;
}
// Fetch the relation graph for a doc and render it as an indented tree rooted at that doc.
// Each edge is labelled with its relationship and direction; non-root nodes open in the drawer
// (preserving read-only mode when called from Explore). A lone root → "no related" empty state.
async function renderRelationTree(container, docId, readOnly) {
  container.replaceChildren(el("div", { class: "muted small" }, "Loading related documents…"));
  let tree;
  try { tree = await api(`/api/documents/${docId}/tree`); }
  catch { container.replaceChildren(el("div", { class: "muted small" }, "Could not load related documents.")); return; }
  const nodes = new Map((tree.nodes || []).map((n) => [n.doc_id, n]));
  const edges = tree.edges || [];
  if (!edges.length) { container.replaceChildren(el("div", { class: "muted small" }, "No related documents.")); return; }
  // Relationship phrasing depends on which way we're traversing the edge. FWD = label seen from
  // the parent looking at the child; REV = label seen from the child looking at the parent.
  const FWD = { amendment_of: "Amended by", attachment_of: "Has attachment",
    supersedes: "Supersedes", related: "Related to" };
  const REV = { amendment_of: "Amendment of", attachment_of: "Attachment of",
    supersedes: "Superseded by", related: "Related to" };
  const adj = new Map();
  const push = (from, other, label) => { (adj.get(from) || adj.set(from, []).get(from)).push({ other, label }); };
  edges.forEach((e) => {
    push(e.parent_doc_id, e.child_doc_id, FWD[e.relationship] || e.relationship);
    push(e.child_doc_id, e.parent_doc_id, REV[e.relationship] || e.relationship);
  });
  const root = tree.root != null ? tree.root : docId;
  const seen = new Set([root]);
  const childrenOf = (id) => {
    const out = [];
    for (const { other, label } of (adj.get(id) || [])) {
      if (seen.has(other)) continue;
      seen.add(other); out.push({ id: other, label });
    }
    return out;
  };
  const renderNode = (id, edgeLabel) => {
    const n = nodes.get(id) || { doc_id: id, original_filename: id };
    const isRoot = id === root;
    const label = el(
      isRoot ? "div" : "button",
      isRoot ? { class: "rel-self" }
             : { class: "rel-link", type: "button",
                 onclick: () => openDoc(id, { original_filename: n.original_filename, document_type: n.document_type }, { readOnly }) },
      edgeLabel ? el("span", { class: "rel-edge" }, edgeLabel + ": ") : null,
      el("span", { class: "rel-name" }, n.original_filename || id),
      n.document_type ? el("span", { class: "rel-type muted small" }, " (" + n.document_type + ")") : null);
    const nodeEl = el("div", { class: "rel-node" }, label);
    const kids = childrenOf(id);
    if (kids.length) {
      const wrap = el("div", { class: "rel-children" });
      kids.forEach((k) => wrap.append(renderNode(k.id, k.label)));
      nodeEl.append(wrap);
    }
    return nodeEl;
  };
  container.replaceChildren(renderNode(root, null));
}
// Only the fields whose value changed from what we rendered — so saving one field doesn't
// mark the rest as human-edited (verify accepts unedited AI proposals server-side).
function collectFieldValues() {
  const values = {};
  $$("#drawerBody [data-key]").forEach((i) => {
    if (i.value !== (i.dataset.orig ?? "")) values[i.dataset.key] = i.value;
  });
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
    if (e.body?.error === "missing_required") {
      // Server returns raw field_keys; show the human labels from the loaded field defs.
      const labelOf = (k) => (state.currentDoc?.fields || []).find((f) => f.field_key === k)?.label || k;
      toast("Fill required fields: " + (e.body.fields || []).map(labelOf).join(", "), true);
    }
    else if (e.body?.error === "amendment_needs_parent")
      toast("Link this amendment to a parent contract first", true);
    else toast("Failed: " + e.message, true);
  }
}

// ─────────────────────────────────────────────── EXPLORE ──
// limit/offset drive "Load more"; selected mirrors Manage's state.selected (prep for the
// plains-nexus hand-off). Read-only discovery for everyone — the drawer opens read-only.
const exploreState = { limit: 50, offset: 0, total: 0, selected: new Set(), tab: "search" };

async function loadExploreFilters() {
  fillSelectKeep($("#filterType"), "document_type", "All types");
  fillSelectKeep($("#filterDept"), "department", "All departments");
  fillSelectKeep($("#obligType"), "document_type", "All types");
  await loadTagFilter();
  if (exploreState.tab === "search" && !$("#searchResults").children.length) runSearch();
}
// Explore has three sub-tabs: Search (default), Chat, Obligations. Only one panel shows at once.
function setExploreTab(name) {
  exploreState.tab = name;
  $$(".etab").forEach((b) => {
    const on = b.dataset.exploreTab === name;
    b.classList.toggle("active", on); b.setAttribute("aria-selected", on ? "true" : "false");
  });
  $("#explore-search").hidden = name !== "search";
  $("#explore-chat").hidden = name !== "chat";
  $("#explore-obligations").hidden = name !== "obligations";
  if (name === "search" && !$("#searchResults").children.length) runSearch();
  else if (name === "chat") $("#chatInput").focus();
  else if (name === "obligations") loadObligations();
}
async function loadTagFilter() {
  const sel = $("#filterTag"), cur = sel.value;
  try {
    const rows = await api("/api/tags");
    sel.replaceChildren(el("option", { value: "" }, "All tags"));
    (rows || []).forEach((r) => sel.append(el("option", { value: r.tag }, r.tag)));
    sel.value = cur;
  } catch { /* non-fatal */ }
}
function fillSelectKeep(sel, cat, placeholder) {
  const cur = sel.value;
  sel.replaceChildren(el("option", { value: "" }, placeholder));
  (state.taxonomy[cat] || []).forEach((o) => sel.append(el("option", { value: o.value }, o.label)));
  sel.value = cur;
}
function wireExplore() {
  const search = () => runSearch();
  const debounced = debounce(search, 300);   // don't fire a search per keystroke/rapid change
  $("#searchBtn").addEventListener("click", search);
  $("#searchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
  $("#searchInput").addEventListener("input", debounced);
  $("#filterTag").addEventListener("change", debounced);
  $("#filterType").addEventListener("change", debounced);
  $("#filterDept").addEventListener("change", debounced);
  $("#filterPath").addEventListener("input", debounced);
  $("#sortSel").addEventListener("change", debounced);
  $("#loadMoreBtn").addEventListener("click", () => runSearch(true));
  $$(".etab").forEach((b) => b.addEventListener("click", () => setExploreTab(b.dataset.exploreTab)));
  wireChat();
  const obligDebounced = debounce(loadObligations, 300);
  $("#obligFrom").addEventListener("change", obligDebounced);
  $("#obligTo").addEventListener("change", obligDebounced);
  $("#obligType").addEventListener("change", obligDebounced);
}
function searchParams() {
  const params = new URLSearchParams();
  if ($("#searchInput").value) params.set("q", $("#searchInput").value);
  if ($("#filterTag").value) params.set("tag", $("#filterTag").value);
  if ($("#filterType").value) params.set("document_type", $("#filterType").value);
  if ($("#filterDept").value) params.set("department", $("#filterDept").value);
  if ($("#filterPath").value) params.set("path", $("#filterPath").value.trim());
  params.set("sort", $("#sortSel").value || "newest");
  params.set("limit", String(exploreState.limit));
  params.set("offset", String(exploreState.offset));
  return params;
}
// append=false → fresh search (reset offset + clear grid/selection); append=true → next page.
async function runSearch(append = false) {
  if (!append) exploreState.offset = 0;
  const grid = $("#searchResults");
  if (!append) { grid.replaceChildren(); exploreState.selected.clear(); updateExploreBulkBar(); }
  try {
    const data = await api("/api/search?" + searchParams());
    const rows = data.rows || [];
    exploreState.total = data.total || 0;
    if (!append && !rows.length) {
      grid.replaceChildren(el("div", { class: "empty" }, "No matching documents."));
      $("#searchCount").textContent = "0 results";
      $("#loadMoreBtn").hidden = true;
      return;
    }
    rows.forEach((d) => grid.append(resultCard(d)));
    exploreState.offset += rows.length;
    $("#searchCount").textContent =
      `${exploreState.total} result${exploreState.total === 1 ? "" : "s"}`;
    $("#loadMoreBtn").hidden = grid.querySelectorAll(".result-card").length >= exploreState.total;
  } catch (e) {
    if (append) { toast("Load failed: " + e.message, true); return; }
    grid.replaceChildren(el("div", { class: "empty" },
      "Search failed. ", el("button", { class: "link-btn", onclick: () => runSearch() }, "Retry")));
    $("#searchCount").textContent = "";
    $("#loadMoreBtn").hidden = true;
  }
}
function resultCard(d) {
  const cb = el("input", { type: "checkbox", class: "rc-check",
    ...(exploreState.selected.has(d.doc_id) ? { checked: "" } : {}),
    onclick: (e) => {
      e.stopPropagation();
      e.target.checked ? exploreState.selected.add(d.doc_id) : exploreState.selected.delete(d.doc_id);
      updateExploreBulkBar();
    } });
  const open = () => openDoc(d.doc_id, d, { readOnly: true });
  return el("div", { class: "result-card", role: "button", tabindex: "0",
      onclick: open, onkeydown: activate(open) },
    el("label", { class: "rc-check-wrap", title: "Select", onclick: (e) => e.stopPropagation() }, cb),
    el("div", { class: "rc-title" }, d.title || d.original_filename),
    el("div", { class: "rc-meta" },
      el("span", { class: "badge gray" }, d.document_type || "—"),
      d.sp_site_name ? el("span", { class: "badge blue" }, d.sp_site_name) : null),
    d.sp_path ? el("div", { class: "rc-path muted small", title: d.sp_path }, d.sp_path) : null,
    el("div", { class: "rc-sum" }, d.summary || d.original_filename));
}
function updateExploreBulkBar() {
  const n = exploreState.selected.size;
  $("#exploreBulkBar").hidden = n === 0;
  $("#exploreBulkCount").textContent = `${n} selected`;
  // "Open in plains-nexus" stays disabled until the hand-off contract lands (separate item).
}

// ─────────────────────────────────────────────── EXPLORE · CHAT ──
// Corpus chat over SSE. Client keeps a {role,content} history for multi-turn; each POST sends
// the new question plus the prior turns. The stream frames are: default (a JSON token string to
// append), `event: citations` (chips), `event: done`, `event: error`.
const chatState = { history: [], streaming: false };

function wireChat() {
  $("#chatSend").addEventListener("click", sendChat);
  $("#chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
}
function chatBubble(role, text) {
  const empty = $("#chatMessages .chat-empty");
  if (empty) empty.remove();
  const bubble = el("div", { class: "chat-bubble" }, text || "");
  const msg = el("div", { class: "chat-msg " + role }, bubble);
  $("#chatMessages").append(msg);
  scrollChat();
  return msg;
}
function scrollChat() { const m = $("#chatMessages"); m.scrollTop = m.scrollHeight; }
function renderCitations(msg, cites) {
  if (!cites || !cites.length) return;
  let box = msg.querySelector(".chat-citations");
  if (!box) { box = el("div", { class: "chat-citations" }); msg.append(box); }
  box.replaceChildren(...cites.map((c) =>
    el("button", { class: "cite-chip", type: "button",
      onclick: () => openDoc(c.doc_id, {}, { readOnly: true }) },
      (c.filename || "document") + (c.page != null ? " p." + c.page : ""))));
  scrollChat();
}
// Parse one SSE frame ("event:"/"data:" lines) and act on it. Returns a small result object so
// the reader loop can accumulate the assistant text and know when the stream is finished.
function handleChatFrame(frame, bubble, msg) {
  let event = "message";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  const data = dataLines.join("\n");
  if (event === "done") return { done: true };
  if (event === "error") {
    if (!bubble.textContent) { bubble.textContent = "Chat error."; bubble.classList.add("chat-err"); }
    toast("Chat is unavailable", true);
    return { done: true, error: true };
  }
  if (event === "citations") {
    try { renderCitations(msg, JSON.parse(data)); } catch {}
    return {};
  }
  try {
    const token = JSON.parse(data);
    if (typeof token === "string") { bubble.textContent += token; scrollChat(); return { token }; }
  } catch {}
  return {};
}
async function sendChat() {
  if (chatState.streaming) return;
  const input = $("#chatInput"), sendBtn = $("#chatSend");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  chatBubble("user", q);
  const prior = chatState.history.slice();   // turns before this question
  chatState.history.push({ role: "user", content: q });
  const msg = chatBubble("assistant", "");
  const bubble = msg.querySelector(".chat-bubble");
  bubble.classList.add("streaming");
  chatState.streaming = true; sendBtn.disabled = true; input.disabled = true;
  let answer = "";
  const finish = () => {
    chatState.streaming = false; sendBtn.disabled = false; input.disabled = false;
    bubble.classList.remove("streaming"); input.focus();
  };
  try {
    const resp = await fetch("/api/chat", { method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: q, history: prior }) });
    if (!resp.ok || !resp.body) {
      // 503 chat_unavailable comes back as JSON, not a stream.
      bubble.textContent = resp.status === 503 ? "Chat is unavailable." : "Something went wrong.";
      bubble.classList.add("chat-err");
      toast("Chat is unavailable", true);
      chatState.history.pop();   // drop the unanswered turn
      finish(); return;
    }
    const reader = resp.body.getReader(), decoder = new TextDecoder();
    let buffer = "", stop = false;
    while (!stop) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
        if (!frame.trim()) continue;
        const r = handleChatFrame(frame, bubble, msg);
        if (r.token) answer += r.token;
        if (r.done) stop = true;
      }
    }
    if (answer) chatState.history.push({ role: "assistant", content: answer });
    else chatState.history.pop();   // no answer produced — don't leave a dangling user turn
  } catch (e) {
    if (!bubble.textContent) { bubble.textContent = "Chat failed."; bubble.classList.add("chat-err"); }
    toast("Chat is unavailable", true);
    chatState.history.pop();
  } finally { finish(); }
}

// ─────────────────────────────────────────────── EXPLORE · OBLIGATIONS ──
// Read-only calendar of dated obligations (expirations, renewals…) grouped by month. Backend
// returns rows sorted by date asc; default range is today..+1yr.
const pad2 = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
function initObligDates() {
  const from = $("#obligFrom"), to = $("#obligTo");
  if (!from.value) from.value = ymd(new Date());
  if (!to.value) { const d = new Date(); d.setFullYear(d.getFullYear() + 1); to.value = ymd(d); }
}
function fmtObligDate(s) {
  const d = new Date(s + "T00:00:00");
  return isNaN(d) ? (s || "—") : d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
function obligMonth(s) {
  const d = new Date(s + "T00:00:00");
  return isNaN(d) ? "Undated" : d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}
function obligRow(r) {
  const open = () => openDoc(r.doc_id, { original_filename: r.original_filename,
    document_type: r.document_type, sp_web_url: r.sp_web_url, sp_site_name: r.sp_site_name },
    { readOnly: true });
  return el("div", { class: "oblig-item", role: "button", tabindex: "0",
      onclick: open, onkeydown: activate(open) },
    el("div", { class: "oblig-date" }, fmtObligDate(r.date)),
    el("div", { class: "oblig-main" },
      el("div", { class: "oblig-title doc-name" }, r.original_filename || "(unnamed)"),
      el("div", { class: "oblig-meta muted small" },
        [r.field_label, r.document_type].filter(Boolean).join(" · ") || "—")));
}
async function loadObligations() {
  initObligDates();
  const list = $("#obligList");
  list.replaceChildren(el("div", { class: "muted small" }, "Loading obligations…"));
  const params = new URLSearchParams();
  if ($("#obligFrom").value) params.set("from", $("#obligFrom").value);
  if ($("#obligTo").value) params.set("to", $("#obligTo").value);
  if ($("#obligType").value) params.set("document_type", $("#obligType").value);
  let rows;
  try { rows = await api("/api/obligations?" + params); }
  catch (e) {
    $("#obligCount").textContent = "";
    list.replaceChildren(el("div", { class: "empty" },
      "Could not load obligations. ", el("button", { class: "link-btn", onclick: loadObligations }, "Retry")));
    return;
  }
  rows = rows || [];
  $("#obligCount").textContent = `${rows.length} upcoming`;
  if (!rows.length) {
    list.replaceChildren(el("div", { class: "empty" }, "No upcoming obligations in this range."));
    return;
  }
  list.replaceChildren();
  let curMonth = null, section = null;
  rows.forEach((r) => {
    const m = obligMonth(r.date);
    if (m !== curMonth) {
      curMonth = m;
      section = el("div", { class: "oblig-month" }, el("h3", { class: "oblig-month-head" }, m));
      list.append(section);
    }
    section.append(obligRow(r));
  });
}

// ─────────────────────────────────────────────── SHAREPOINT ──
const spState = {
  status: null,
  view: "sites",          // sites | drives | items
  site: null,             // {id, name}
  drive: null,            // {id, name}
  path: [],               // breadcrumb of {id, name} folders inside the drive
  selected: new Map(),    // "driveId:itemId" → full selection entry (may span drives)
  visible: [],            // entries currently listed (for Select all)
};
// Composite key so selections don't collide across drives; null id === whole library (root).
const selKey = (driveId, id) => `${driveId}:${id ?? "__root__"}`;
function toggleSel(entry, on) {
  const key = selKey(entry.drive_id, entry.id);
  if (on) spState.selected.set(key, entry); else spState.selected.delete(key);
  updateSpSelCount();
}
function selectAllVisible() {
  const vis = spState.visible || [];
  const allSel = vis.length && vis.every((e) => spState.selected.has(selKey(e.drive_id, e.id)));
  vis.forEach((e) => {
    const key = selKey(e.drive_id, e.id);
    if (allSel) spState.selected.delete(key); else spState.selected.set(key, e);
  });
  updateSpSelCount();
  spState.view === "drives" ? loadSpDrives() : loadSpItems();  // reflect new checkbox states
}

function wireSharePoint() {
  $("#spImportBtn").addEventListener("click", openSharePoint);
  $("#spClose").addEventListener("click", closeSharePoint);
  $("#spCancel").addEventListener("click", closeSharePoint);
  $("#spConnectBtn").addEventListener("click", connectSharePoint);
  $("#spImport").addEventListener("click", doSharePointImport);
  $("#spSelectAll").addEventListener("click", selectAllVisible);
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
    const ok = !!(st.configured && st.can_import);
    $("#spEntry").hidden = !ok;
    $("#spImportBtn").disabled = !ok;
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
    const meta = [s.document_type, s.user_email].filter(Boolean).join(" · ");
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
  spState.selected.clear(); spState.visible = [];
  spState.view = "sites"; spState.site = null; spState.drive = null; spState.path = [];
  $("#spScrim").hidden = false;
  openOverlay($("#spScrim"), closeSharePoint);
  $("#spAutoSync").checked = true;   // default ON — imports stay in sync unless opted out
  updateSpSelCount();
  if (spState.status?.connected) showSpBrowser();
  else { $("#spConnect").hidden = false; $("#spBrowser").hidden = true; $("#spFoot").hidden = true; }
}
function closeSharePoint() { closeOverlay($("#spScrim")); $("#spScrim").hidden = true; }

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
  $("#spImport").disabled = n === 0;
  const vis = spState.visible || [];
  const allSel = vis.length && vis.every((e) => spState.selected.has(selKey(e.drive_id, e.id)));
  $("#spSelectAll").textContent = allSel ? "Select none" : "Select all";
  // Auto-sync tracks a single file/folder, so it's only offered when exactly one is selected.
  const auto = $("#spAutoSync"), note = $("#spAutoSyncNote");
  if (auto) {
    const single = n === 1;
    auto.disabled = !single;
    if (!single) auto.checked = false;
    if (note) note.hidden = single;
  }
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
  spState.view = "sites"; renderCrumbs(); spState.visible = [];
  $("#spSearch").hidden = false; $("#spSelectAll").hidden = true; spListBusy();
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
  $("#spSearch").hidden = true; $("#spSelectAll").hidden = false; renderCrumbs(); spListBusy();
  try {
    const { drives } = await api("/api/sharepoint/drives?site_id=" + encodeURIComponent(spState.site.id));
    const list = $("#spList"); list.replaceChildren();
    if (!drives.length) { spState.visible = []; list.append(el("div", { class: "empty" }, "No document libraries.")); return; }
    // A whole library imports as a folder rooted at the drive (id === null → root walk).
    spState.visible = drives.map((d) => ({ id: null, name: d.name, is_folder: true,
      drive_id: d.id, drive_name: d.name, child_count: null }));
    drives.forEach((d) => {
      const entry = { id: null, name: d.name, is_folder: true, drive_id: d.id,
        drive_name: d.name, child_count: null };
      const cb = el("input", { type: "checkbox",
        ...(spState.selected.has(selKey(d.id, null)) ? { checked: "" } : {}),
        onclick: (e) => { e.stopPropagation(); toggleSel(entry, e.target.checked); } });
      list.append(el("div", { class: "sp-row folder" },
        cb,
        el("span", { class: "sp-ic" }, "🗂"),
        el("span", { class: "sp-nm", onclick: () => {
          spState.drive = { id: d.id, name: d.name }; spState.path = []; loadSpItems();
        } }, d.name),
        el("span", { class: "sp-meta muted small" }, "library")));
    });
    updateSpSelCount();
  } catch (e) { handleSpErr(e); }
}

async function loadSpItems() {
  spState.view = "items"; $("#spSelectAll").hidden = false; renderCrumbs(); spListBusy();
  const parent = spState.path.length ? spState.path[spState.path.length - 1].id : "";
  const dId = spState.drive.id, dName = spState.drive.name;
  try {
    const { items } = await api(`/api/sharepoint/items?drive_id=${encodeURIComponent(dId)}` +
      (parent ? `&item_id=${encodeURIComponent(parent)}` : ""));
    const list = $("#spList"); list.replaceChildren();
    // Carry each item's metadata so directly-selected files land with their path/link/mime.
    const entryOf = (it) => ({ id: it.id, name: it.name, is_folder: it.is_folder,
      drive_id: dId, drive_name: dName, child_count: it.child_count,
      mime: it.mime, size: it.size, path: it.path, web_url: it.web_url, modified: it.modified });
    spState.visible = items.map(entryOf);
    if (!items.length) { updateSpSelCount(); list.append(el("div", { class: "empty" }, "Empty folder.")); return; }
    items.forEach((it) => {
      const entry = entryOf(it);
      const cb = el("input", { type: "checkbox",
        ...(spState.selected.has(selKey(dId, it.id)) ? { checked: "" } : {}),
        onclick: (e) => { e.stopPropagation(); toggleSel(entry, e.target.checked); } });
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
    updateSpSelCount();
  } catch (e) { handleSpErr(e); }
}

const fmtSize = (n) => !n ? "" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";

async function doSharePointImport() {
  const entries = [...spState.selected.values()];
  if (!entries.length) return;
  // Auto-sync only applies to a single item; the checkbox is disabled/unchecked otherwise
  // (see updateSpSelCount), so a multi-file import just proceeds without it — no error.
  const autosync = $("#spAutoSync").checked && entries.length === 1;
  const folderSel = entries.filter((e) => e.is_folder);
  // Folders import recursively; we only know immediate child counts client-side, so warn.
  if (folderSel.length) {
    const fileCount = entries.length - folderSel.length;
    const known = folderSel.reduce((s, f) => s + (f.child_count || 0), 0);
    const atLeast = fileCount + known;
    if (!confirm(
      `Importing ${fileCount} file(s) and ${folderSel.length} folder(s).\n\n` +
      `Folders are imported recursively — that's at least ~${atLeast} file(s), and the true ` +
      `total may be much larger (subfolders aren't counted here). This runs in the background. Continue?`))
      return;
  }
  const dt = null, dept = null;  // classify later in the Manage queue, not at import time
  const btn = $("#spImport"); btn.disabled = true; btn.textContent = "Importing…";
  try {
    if (autosync) {
      const f = entries[0];  // a single file or folder — folder_id carries either item id
      await api("/api/sharepoint/syncs", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ site_id: spState.site.id, site_name: spState.site.name,
          drive_id: f.drive_id, drive_name: f.drive_name,
          folder_id: f.id, folder_name: f.name, document_type: dt, department: dept }) });
    }
    // Each import job is single-drive, so group the selection by drive → one job per library.
    const byDrive = new Map();
    for (const e of entries) {
      if (!byDrive.has(e.drive_id)) byDrive.set(e.drive_id, { drive_name: e.drive_name, sels: [] });
      byDrive.get(e.drive_id).sels.push({ id: e.id, name: e.name, is_folder: e.is_folder,
        mime: e.mime, path: e.path, web_url: e.web_url, modified: e.modified });
    }
    let firstReq = null, inlineNew = 0, inlineDup = 0, anyQueued = false;
    for (const [driveId, g] of byDrive) {
      const res = await api("/api/sharepoint/import", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ drive_id: driveId, selections: g.sels, source_id: "sp_import",
          site_id: spState.site.id, site_name: spState.site.name, drive_name: g.drive_name,
          document_type: dt, department: dept }) });
      if (res.queued === false) { inlineNew += res.imported || 0; inlineDup += res.duplicates || 0; }
      else { anyQueued = true; firstReq = firstReq || res.request_id; }
    }
    closeSharePoint();
    if (anyQueued) {
      toast(`Import queued${byDrive.size > 1 ? ` (${byDrive.size} libraries)` : ""}` +
        `${autosync ? " · auto-sync on" : ""} — processing in the background…`);
      setImportProgress("Import queued — processing in the background…");
      pollImportJob(firstReq);
    } else {
      toast(`Imported ${inlineNew} new, ${inlineDup} already stored` + (autosync ? " · auto-sync on" : ""));
      loadManage();
    }
  } catch (e) {
    if (e.status === 401) handleSpErr(e);
    else toast("Import failed: " + e.message, true);
  } finally { btn.disabled = false; btn.textContent = "Import selected"; }
}

// A single persistent progress line (in the sync panel area) instead of a toast per poll tick.
// Pass null to hide it. `state` is "" (in-progress), "done", or "error".
function setImportProgress(text, status = "") {
  const p = $("#importProgress");
  if (!p) return;
  if (text == null) { p.hidden = true; p.replaceChildren(); return; }
  p.hidden = false;
  p.className = "panel import-progress" + (status ? " " + status : "");
  p.replaceChildren(el("span", {}, text));
}

// Poll a queued import until it finishes. Update the single progress line each tick; only
// reload the Manage table once, on completion, rather than on every poll.
async function pollImportJob(reqId, tries = 0) {
  if (!reqId) { loadManage(); return; }
  try {
    const st = await api(`/api/sharepoint/import/${reqId}`);
    const done = st.imported || 0, dup = st.duplicates || 0, errs = st.errors || 0;
    if (st.status === "done") {
      setImportProgress(`Import complete: ${done} new, ${dup} already stored${errs ? `, ${errs} failed` : ""}`, "done");
      loadManage(); return;
    }
    if (st.status === "error") {
      setImportProgress("Import failed: " + (st.last_error || "unknown error"), "error");
      loadManage(); return;
    }
    if (st.status === "processing") {
      setImportProgress(st.total_files
        ? `Importing… ${done + dup + errs}/${st.total_files}`
        : `Importing… ${done + dup + errs} so far`);
    }
  } catch (e) { /* transient; keep polling */ }
  // Back off from 2s toward 10s; give up surfacing progress after ~10 min (work continues server-side).
  if (tries < 120) setTimeout(() => pollImportJob(reqId, tries + 1), Math.min(2000 + tries * 500, 10000));
}

// ─────────────────────────────────────────────── FIELDS (admin) ──
const fieldState = { docTypes: [], editing: null };
const accessState = { grants: [], me: null };
const TYPE_LABELS = { text: "Text", long_text: "Long text", date: "Date", currency: "Currency",
  number: "Number", picklist: "Picklist", multi: "Multi-value", summary: "Summary" };

function wireFields() {
  $("#modifyFieldsBtn").addEventListener("click", () => setFieldsMode(true));
  $("#fieldsDoneBtn").addEventListener("click", () => setFieldsMode(false));
  $("#fieldAddBtn").addEventListener("click", () => openFieldModal(null));
  $("#accessGrantBtn").addEventListener("click", grantAccess);
  $("#accessEmail").addEventListener("keydown", (e) => { if (e.key === "Enter") grantAccess(); });
  $("#ffCancel").addEventListener("click", closeFieldModal);
  $("#ffSave").addEventListener("click", saveFieldDef);
}
function closeFieldModal() { closeOverlay($("#fieldScrim")); $("#fieldScrim").hidden = true; }

// ─────────────────────────────────────────────── APP ACCESS (admin) ──
// Manage who's an app admin (and grant Full / Read). Site grants are auto-mirrored from
// SharePoint and are not shown here. See /api/admin/access.
const ACCESS_LABELS = { ADMIN: "Admin", FULL: "Full", READ: "Read" };

async function loadAccess() {
  const list = $("#accessList");
  list.replaceChildren(el("div", { class: "muted small" }, "Loading access…"));
  try {
    const r = await api("/api/admin/access");
    accessState.me = r.me;
    accessState.grants = r.grants || [];
    renderAccess();
  } catch (e) {
    list.replaceChildren(el("div", { class: "muted small" },
      e.status === 403 ? "Admin access required." : "Could not load access: " + e.message));
  }
}

function renderAccess() {
  const list = $("#accessList");
  if (!accessState.grants.length) {
    list.replaceChildren(el("div", { class: "muted small" }, "No admin, full, or read grants yet."));
    return;
  }
  list.replaceChildren(...accessState.grants.map(accessRow));
}

function accessRow(g) {
  const isSelf = g.email === accessState.me;
  const sel = el("select", { class: "access-select" },
    ...Object.entries(ACCESS_LABELS).map(([v, l]) => el("option", { value: v }, l)));
  sel.value = g.access_type;
  const controls = [sel,
    el("button", { class: "btn small danger", onclick: () => setAccess(g.email, "NONE") }, "Revoke")];
  sel.addEventListener("change", () => setAccess(g.email, sel.value));
  return el("div", { class: "access-row" },
    el("div", { class: "access-who" },
      el("span", {}, g.email),
      isSelf ? el("span", { class: "access-you" }, "you") : null),
    el("div", { class: "access-actions" }, ...controls));
}

async function grantAccess() {
  const email = $("#accessEmail").value.trim().toLowerCase();
  const access_type = $("#accessType").value;
  if (!email || !email.includes("@")) { toast("Enter a valid email", true); return; }
  await setAccess(email, access_type);
  $("#accessEmail").value = "";
}

async function setAccess(email, access_type) {
  try {
    await api("/api/admin/access", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ email, access_type }),
    });
    toast(access_type === "NONE"
      ? `Removed access for ${email}`
      : `${email} is now ${ACCESS_LABELS[access_type]}`);
    loadAccess();
  } catch (e) {
    toast(e.status === 409 ? "Can't remove the last admin" : "Failed: " + e.message, true);
    loadAccess();  // resync the dropdown if a change was rejected
  }
}

async function loadFieldDefs() {
  const list = $("#fieldDefsList");
  list.replaceChildren(el("div", { class: "muted small" }, "Loading fields…"));
  try {
    const { fields, doc_types } = await api("/api/field-defs/all");
    fieldState.docTypes = doc_types || [];
    // Group by applies_to; "common" first, then each doc type in taxonomy order.
    const groups = new Map([["common", []]]);
    (doc_types || []).forEach((t) => groups.set(t, []));
    fields.forEach((f) => { if (!groups.has(f.applies_to)) groups.set(f.applies_to, []); groups.get(f.applies_to).push(f); });
    list.replaceChildren();
    for (const [applies, defs] of groups) {
      const title = applies === "common" ? "Common (all documents)" : applies;
      const grp = el("div", { class: "fielddefs-group" }, el("h3", {}, title));
      if (!defs.length) grp.append(el("div", { class: "muted small" }, "No specific fields — uses the common fields."));
      defs.forEach((f) => grp.append(fieldDefRow(f)));
      list.append(grp);
    }
  } catch (e) {
    list.replaceChildren(el("div", { class: "muted small" },
      e.status === 403 ? "Admin access required." : "Could not load fields: " + e.message));
  }
}

function fieldDefRow(f) {
  const meta = [`key: ${f.field_key}`, TYPE_LABELS[f.data_type] || f.data_type,
    f.required_for_verify ? "required" : null].filter(Boolean).join(" · ");
  return el("div", { class: "fielddef-row" },
    el("div", { class: "fielddef-main" },
      el("div", { class: "fielddef-name" }, f.label + (f.required_for_verify ? " *" : "")),
      el("div", { class: "fielddef-meta" }, meta),
      f.extraction_prompt_hint ? el("div", { class: "fielddef-hint" }, f.extraction_prompt_hint) : null),
    el("div", { class: "fielddef-actions" },
      el("button", { class: "btn small", onclick: () => openFieldModal(f) }, "Edit"),
      el("button", { class: "btn small danger", onclick: () => deleteFieldDef(f) }, "Remove")));
}

function openFieldModal(f) {
  fieldState.editing = f;
  $("#fieldModalTitle").textContent = f ? "Edit field" : "Add field";
  const applies = $("#ffApplies");
  applies.replaceChildren(el("option", { value: "common" }, "Common (all documents)"));
  fieldState.docTypes.forEach((t) => applies.append(el("option", { value: t }, t)));
  applies.value = f ? f.applies_to : "common";
  $("#ffKey").value = f ? f.field_key : "";
  $("#ffKey").disabled = !!f;  // key is the identity; don't rename in place
  $("#ffLabel").value = f ? f.label : "";
  $("#ffType").value = f ? f.data_type : "text";
  $("#ffHint").value = f ? (f.extraction_prompt_hint || "") : "";
  $("#ffOptions").value = f ? (f.picklist_source || "") : "";
  $("#ffRequired").checked = f ? !!f.required_for_verify : false;
  $("#fieldScrim").hidden = false;
  openOverlay($("#fieldScrim"), closeFieldModal);
}

async function saveFieldDef() {
  const key = $("#ffKey").value.trim();
  const label = $("#ffLabel").value.trim();
  if (!label) { toast("Label is required", true); return; }
  const body = {
    label, applies_to: $("#ffApplies").value, data_type: $("#ffType").value,
    extraction_prompt_hint: $("#ffHint").value.trim() || null,
    picklist_source: $("#ffType").value === "picklist" ? ($("#ffOptions").value.trim() || null) : null,
    required_for_verify: $("#ffRequired").checked,
  };
  try {
    if (fieldState.editing) {
      await api("/api/field-defs/" + encodeURIComponent(fieldState.editing.field_key),
        { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    } else {
      if (!key) { toast("Field key is required", true); return; }
      body.field_key = key;
      await api("/api/field-defs", { method: "POST",
        headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    }
    closeFieldModal();
    toast("Field saved");
    loadFieldDefs();
  } catch (e) { toast("Failed: " + e.message, true); }
}

async function deleteFieldDef(f) {
  if (!confirm(`Remove the "${f.label}" field? Documents already extracted keep their values.`)) return;
  try {
    await api("/api/field-defs/" + encodeURIComponent(f.field_key), { method: "DELETE" });
    toast("Field removed"); loadFieldDefs();
  } catch (e) { toast("Failed: " + e.message, true); }
}

document.addEventListener("DOMContentLoaded", init);
