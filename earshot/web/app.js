/* earshot web UI — vanilla SPA bound to the /v1 API + SSE.
   No build step, no external dependencies (rpi/requirements/web-ui). */
"use strict";

// ---- tiny DOM helper ------------------------------------------------------
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2).toLowerCase(), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.appendChild(typeof kid === "object" ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

// ---- API ------------------------------------------------------------------
async function getJSON(url, headers) {
  const r = await fetch(url, { headers: headers || {} });
  if (!r.ok) throw await apiError(r);
  return r.json();
}
async function send(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw await apiError(r);
  return r.status === 204 ? null : r.json();
}
async function apiError(r) {
  let code = "error", message = r.statusText;
  try { const j = await r.json(); if (j.error) { code = j.error.code; message = j.error.message; } } catch (_) {}
  const e = new Error(message); e.code = code; e.status = r.status; return e;
}
const api = {
  status: () => getJSON("/v1/status"),
  sessions: () => getJSON("/v1/sessions"),
  session: (id) => getJSON("/v1/sessions/" + id),
  transcript: (id) => getJSON("/v1/sessions/" + id + "/transcript", { Accept: "application/json" }),
  speakers: (id) => getJSON("/v1/sessions/" + id + "/speakers"),
  service: () => getJSON("/v1/service"),
  jobs: () => getJSON("/v1/jobs"),
  startRec: () => send("POST", "/v1/recording"),
  stopRec: () => send("DELETE", "/v1/recording"),
  upload: (file, meta) => {
    const fd = new FormData();
    fd.append("audio", file);
    if (meta && meta.name) fd.append("name", meta.name);
    if (meta && meta.occurred_at) fd.append("occurred_at", meta.occurred_at);
    return fetch("/v1/sessions", { method: "POST", body: fd }).then(async (r) => {
      if (!r.ok) throw await apiError(r);
      return r.json();
    });
  },
  enqueue: (id, kind, opts) => send("POST", "/v1/sessions/" + id + "/jobs", Object.assign({ kind }, opts || {})),
  bulk: (kind, target) => send("POST", "/v1/jobs", { kind, target }),
  cancelJob: (id) => send("DELETE", "/v1/jobs/" + id),
  patchSession: (id, fields) => send("PATCH", "/v1/sessions/" + id, fields),
  rename: (id, name) => send("PATCH", "/v1/sessions/" + id, { name }),
  del: (id) => send("DELETE", "/v1/sessions/" + id),
  setSpeaker: (id, label, name) => send("PUT", "/v1/sessions/" + id + "/speakers/" + encodeURIComponent(label), { name }),
  putService: (url) => send("PUT", "/v1/service", { url }),
  clearService: () => send("DELETE", "/v1/service"),
};

// ---- meta / formatting ----------------------------------------------------
const STATUS_META = {
  recording: { label: "Recording", color: "#ef4444" },
  pending: { label: "Audio only", color: "#c78a3d" },
  queued: { label: "Queued", color: "#FFB300" },
  processing: { label: "Processing", color: "#FFB300" },
  transcribed: { label: "Transcribed", color: "var(--color-primary)" },
  diarized: { label: "Transcribed with Speakers", color: "#9b59b6" },
  failed: { label: "Failed", color: "var(--color-error)" },
};
const DEVICE_META = {
  booting: { label: "Booting", rgb: "255,255,255", pulse: true },
  idle: { label: "Ready", rgb: "34,197,94", pulse: false },
  recording: { label: "Recording", rgb: "239,68,68", pulse: true },
  finalizing: { label: "Finalizing", rgb: "255,179,0", pulse: true },
  processing: { label: "Processing", rgb: "255,179,0", pulse: true },
  disk_full: { label: "Disk full", rgb: "255,128,0", pulse: true },
};
const SPEAKER_PALETTE = ["var(--color-primary)", "var(--color-accent)", "#9b59b6", "#27ae60", "#1abc9c", "#e67e22"];

const pad = (n) => String(n).padStart(2, "0");
function fmtClock(sec) { sec = Math.max(0, Math.round(sec || 0)); const H = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60; return H ? `${H}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`; }
function fmtDur(sec) { sec = Math.round(sec || 0); const H = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60; if (H) return `${H}h ${m}m`; if (m) return `${m}m ${s}s`; return `${s}s`; }
function fmtSize(bytes) { return ((bytes || 0) / 1e6).toFixed(1) + " MB"; }
function titleOf(s) { return (s.name || "").trim() || s.id; }
function splitOccurred(value) {
  if (!value) return { date: "", time: "" };
  const parts = String(value).split("T");
  return { date: parts[0] || "", time: (parts[1] || "").slice(0, 5) };
}
function composeOccurred(date, time) { return date ? (time ? `${date}T${time}` : date) : null; }
function fmtDateTime(iso) {
  if (!iso) return "Date unknown";
  const d = new Date(iso); if (Number.isNaN(d.getTime())) return iso;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} · ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtOccurred(value) {
  const p = splitOccurred(value); if (!p.date) return "";
  const [y, m, d] = p.date.split("-").map(Number);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  let out = Number.isFinite(y) && Number.isFinite(m) && Number.isFinite(d) ? `${months[m - 1]} ${d}, ${y}` : p.date;
  if (p.time) {
    const [hh, mm] = p.time.split(":").map(Number);
    if (Number.isFinite(hh) && Number.isFinite(mm)) {
      const ap = hh < 12 ? "AM" : "PM", h12 = ((hh + 11) % 12) + 1;
      out += ` · ${h12}:${pad(mm)} ${ap}`;
    }
  }
  return out;
}
function sessionSubtitle(s) {
  const date = fmtOccurred(s.occurred_at) || fmtDateTime(s.created_at);
  return (s.name || "").trim() ? `${s.id} · ${date}` : date;
}
function badge(state) { const m = STATUS_META[state] || STATUS_META.pending; return h("span", { class: "badge", style: { background: m.color } }, m.label); }
function jobForSession(id) {
  return (S.jobs || []).find((j) => j.session_id === id && (j.state === "queued" || j.state === "running")) || null;
}
function sessionDisplayState(s) {
  const j = s.job || jobForSession(s.id);
  if (j && j.state === "queued") return "queued";
  if (j && j.state === "running") return "processing";
  return s.state;
}
function activeJob(job) { return job && (job.state === "queued" || job.state === "running"); }

// ---- state ----------------------------------------------------------------
const S = {
  status: null, sessions: [], jobs: [], service: null,
  detail: null, segments: [], speakers: [],
  route: { name: "list" },
  modal: null, // {type:'delete'|'transcribe'|'upload', id, diarize?}
  lastStructFp: null,
  sampleIdx: {},        // "id\0label" → which voice sample is selected
  activeTurnStart: null, // start of the transcript turn the selected sample was cut from
  scrollToTurn: false,   // one-shot: scroll to that turn on the next render only
};
// The parts of status that change the *layout* (vs. a ticking counter).
function structFingerprint(st) {
  if (!st) return "";
  const p = st.processing;
  return st.state + "|" + (p ? p.session_id + ":" + p.kind + ":" + p.route + ":" + (p.progress == null ? "?" : "n") : "none") + "|" + (st.disk && st.disk.blocked);
}
function updateLiveCounters() {
  const st = S.status; if (!st) return;
  const set = (id, txt) => { const n = document.getElementById(id); if (n != null && txt != null) n.textContent = txt; };
  const width = (id, pct) => { const n = document.getElementById(id); if (n) n.style.width = pct + "%"; };
  if (st.recording) { set("live-clock", fmtClock(st.recording.elapsed)); set("live-row-dur", fmtClock(st.recording.elapsed)); }
  if (st.disk) { width("live-disk", st.disk.used_percent); set("live-disk-label", st.disk.used_percent + "% used"); }
  if (st.processing) {
    set("live-progress-text", progressText(st.processing));
    if (st.processing.progress != null) width("live-progress", st.processing.progress * 100);
  }
}
let toastTimer = null;
function toast(msg) {
  const root = document.getElementById("toast-root");
  root.innerHTML = "";
  root.appendChild(h("div", { class: "toast", role: "status" }, msg));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (root.innerHTML = ""), 2600);
}
function fail(e) { toast(e && e.message ? e.message : "Something went wrong"); }

// ---- theme ----------------------------------------------------------------
function currentTheme() {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced) return forced;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme() {
  const t = localStorage.getItem("earshot-theme");
  if (t) document.documentElement.setAttribute("data-theme", t);
  document.getElementById("theme-toggle").textContent = currentTheme() === "dark" ? "☀️" : "🌙";
}
document.getElementById("theme-toggle").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  localStorage.setItem("earshot-theme", next);
  applyTheme();
});

// ---- header ---------------------------------------------------------------
function renderHeader() {
  const st = S.status;
  const dev = DEVICE_META[st && st.state] || DEVICE_META.idle;
  const led = document.getElementById("led");
  led.style.background = `rgb(${dev.rgb})`;
  led.style.boxShadow = `0 0 8px rgba(${dev.rgb},.7), 0 0 0 3px rgba(${dev.rgb},.18)`;
  led.style.animation = dev.pulse ? "ledpulse 1.4s ease-in-out infinite" : "none";
  let label = dev.label;
  if (st && st.state === "recording" && st.recording) label = "Recording · " + fmtClock(st.recording.elapsed);
  document.getElementById("device-label").textContent = label;
  document.title = "Earshot Hub — " + dev.label;
  document.getElementById("nav-sessions").classList.toggle("active", S.route.name !== "settings");
  document.getElementById("nav-settings").classList.toggle("active", S.route.name === "settings");
}

// ---- routing --------------------------------------------------------------
function parseRoute() {
  const hash = location.hash.replace(/^#/, "");
  if (hash === "/settings") return { name: "settings" };
  const m = /^\/s\/(rec-\d+)$/.exec(hash);
  if (m) return { name: "detail", id: m[1] };
  return { name: "list" };
}
async function onRoute() {
  S.route = parseRoute();
  if (S.route.name === "detail") await loadDetail(S.route.id);
  if (S.route.name === "settings" || S.service == null) { try { S.service = await api.service(); } catch (_) {} }
  renderView();
  renderHeader();
}
window.addEventListener("hashchange", onRoute);
document.getElementById("brand").addEventListener("click", () => (location.hash = "#/"));

// ---- data loads -----------------------------------------------------------
async function refreshSessions() { try { S.sessions = (await api.sessions()).sessions; } catch (e) { fail(e); } }
async function refreshJobs() { try { S.jobs = (await api.jobs()).jobs; } catch (e) { fail(e); } }
async function loadDetail(id) {
  // Sample choices belong to the session being viewed; moving to another one
  // starts over rather than carrying a stale index and highlight across.
  if (!S.detail || S.detail.id !== id) { S.sampleIdx = {}; S.activeTurnStart = null; }
  try {
    S.detail = await api.session(id);
    S.segments = S.detail.has_transcript ? await api.transcript(id) : [];
    S.speakers = S.detail.diarized ? (await api.speakers(id)).speakers : (S.detail.speakers || []);
  } catch (e) { S.detail = null; fail(e); }
}

// ---- render dispatch (focus-preserving) -----------------------------------
function renderView() {
  const active = document.activeElement;
  const focusKey = active && active.dataset ? active.dataset.focus : null;
  const caret = active && active.selectionStart != null ? active.selectionStart : null;

  const root = document.getElementById("view");
  root.innerHTML = "";
  let node;
  if (S.route.name === "settings") node = viewSettings();
  else if (S.route.name === "detail") node = viewDetail();
  else node = viewList();
  root.appendChild(node);
  renderModal();
  S.lastStructFp = structFingerprint(S.status);  // full render is now current

  if (focusKey) {
    const next = root.querySelector(`[data-focus="${focusKey}"]`);
    if (next) { next.focus(); if (caret != null && next.setSelectionRange) try { next.setSelectionRange(caret, caret); } catch (_) {} }
  }

  // One-shot: only a deliberate sample action scrolls. renderView also runs on
  // every SSE jobs/sessions change, and scrolling on those would yank the page
  // out from under someone mid-type whenever a background job ticks.
  if (S.scrollToTurn) {
    S.scrollToTurn = false;
    const turn = root.querySelector('[data-turn="active"]');
    if (turn) turn.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

// ---- LIST view ------------------------------------------------------------
function recordHero() {
  const st = S.status || { state: "idle", disk: { used_percent: 0, blocked: false } };
  const recording = st.state === "recording";
  const finalizing = st.state === "finalizing";
  const diskFull = st.state === "disk_full" || (st.disk && st.disk.blocked);

  const btn = h("button", {
    class: "rec-btn" + (recording ? " recording" : ""),
    "aria-label": recording ? "Stop recording" : "Start recording",
    disabled: finalizing || (diskFull && !recording),
    onclick: toggleRecord,
  }, h("span", { class: "dot", "aria-hidden": "true" }));
  const wrap = h("div", { class: "rec-btn-wrap" }, recording ? h("span", { class: "rec-ring", "aria-hidden": "true" }) : null, btn);

  const eyebrow = recording ? "Recording" : finalizing ? "Finalizing" : diskFull ? "Blocked" : "Recorder";
  const headline = recording ? fmtClock(st.recording ? st.recording.elapsed : 0)
    : finalizing ? "Encoding…" : diskFull ? "Disk full" : "Ready to record";
  const sub = recording ? "Press stop to encode the chunks into a single session.m4a."
    : diskFull ? "New recordings are blocked until usage drops below the threshold."
    : "Press the record control, or the button on the device.";

  const pct = st.disk ? st.disk.used_percent : 0;
  const diskColor = pct >= 90 ? "var(--color-warning)" : "var(--color-primary)";
  const disk = h("div", { style: { minWidth: "190px" } },
    h("div", { class: "mono", style: { display: "flex", justifyContent: "space-between", fontSize: "12px", color: "var(--color-text-secondary)", marginBottom: "7px" } },
      h("span", {}, "Disk"), h("span", { id: "live-disk-label" }, `${pct}% used`)),
    h("div", { class: "disk-bar" }, h("div", { id: "live-disk", style: { width: pct + "%", background: diskColor } })),
    h("div", { class: "mono muted", style: { fontSize: "12px", marginTop: "7px" } }, diskFull ? "blocked at threshold" : "recording allowed"));

  const idle = !recording && !finalizing && !diskFull;
  const uploadBtn = h("button", {
    class: "btn primary ghost", style: { marginTop: "16px" },
    title: idle ? "Create a session from an existing audio file" : "Available when the device is idle",
    disabled: !idle, onclick: openUpload,
  }, "⬆ Upload a file");

  return h("section", { class: "card hero" }, wrap,
    h("div", { style: { flex: "1", minWidth: "220px" } },
      h("div", { class: "mono muted", style: { fontSize: "12px", textTransform: "uppercase", letterSpacing: ".08em", marginBottom: "6px" } }, eyebrow),
      h("div", { id: recording ? "live-clock" : null, class: "serif", style: { fontWeight: "700", fontSize: "30px", lineHeight: "1.1" } }, headline),
      h("div", { class: "secondary", style: { fontSize: "15px", marginTop: "8px", maxWidth: "420px", lineHeight: "1.5" } }, sub),
      uploadBtn),
    disk);
}

function viewList() {
  const wrap = h("div", { class: "view" });
  wrap.appendChild(recordHero());

  const pending = S.sessions.filter((s) => s.state === "pending" && !jobForSession(s.id));
  const canDiarize = S.service && S.service.reachable && S.service.capabilities && S.service.capabilities.diarize;
  const undiarized = S.sessions.filter((s) => (s.state === "pending" || s.state === "transcribed") && !jobForSession(s.id));
  const showBulk = pending.length || (canDiarize && undiarized.length);
  const head = h("div", { style: { display: "flex", alignItems: "baseline", justifyContent: "space-between", margin: "40px 0 16px", gap: "16px", flexWrap: "wrap" } },
    h("div", {}, h("h2", { class: "title" }, "Sessions"),
      h("div", { class: "mono muted", style: { fontSize: "13px", marginTop: "4px" } }, `${S.sessions.length} on device`)),
    showBulk ? h("button", { class: "btn primary ghost", onclick: () => openTranscribeModal("__all__") }, "Transcribe all") : null);
  wrap.appendChild(head);

  if (S.sessions.length === 0) {
    wrap.appendChild(h("div", { class: "empty" },
      h("div", { style: { fontSize: "40px", marginBottom: "8px", opacity: ".5" } }, "🎙️"),
      h("div", { class: "serif", style: { fontWeight: "700", fontSize: "22px" } }, "No recordings yet"),
      h("div", { class: "secondary", style: { maxWidth: "380px", margin: "10px auto 0", lineHeight: "1.6" } },
        "Press the button on the device — or the record control above — to capture your first session. You can also upload an existing audio file."),
      h("button", { class: "btn primary ghost", style: { marginTop: "18px" }, onclick: openUpload }, "⬆ Upload a file")));
    return wrap;
  }

  const rows = h("div", { class: "rows" });
  for (const s of S.sessions) {
    const displayState = sessionDisplayState(s);
    const m = STATUS_META[displayState] || STATUS_META.pending;
    rows.appendChild(h("div", { class: "card srow", role: "link", tabindex: "0",
      "aria-label": `Open ${titleOf(s)}`,
      onclick: () => (location.hash = "#/s/" + s.id),
      onkeydown: (e) => { if (e.key === "Enter") location.hash = "#/s/" + s.id; } },
      h("span", { class: "dot", style: { background: m.color } }),
      h("div", { style: { minWidth: "0", flex: "1" } },
        h("div", { style: { fontWeight: "600", fontSize: "15px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, titleOf(s)),
        h("div", { class: "mono muted", style: { fontSize: "12px", marginTop: "3px" } }, sessionSubtitle(s))),
      h("div", { id: s.state === "recording" ? "live-row-dur" : null, class: "mono secondary", style: { fontSize: "13px", width: "96px", textAlign: "right" } },
        s.state === "recording" ? fmtClock(S.status && S.status.recording ? S.status.recording.elapsed : 0) : fmtDur(s.duration)),
      h("div", { class: "mono muted", style: { fontSize: "13px", width: "82px", textAlign: "right" } }, fmtSize(s.size)),
      h("div", { style: { width: "200px", display: "flex", justifyContent: "flex-end" } }, badge(displayState)),
      h("span", { class: "muted", "aria-hidden": "true" }, "›")));
  }
  wrap.appendChild(rows);
  return wrap;
}

// ---- DETAIL view ----------------------------------------------------------
function processingFor(id) {
  const p = S.status && S.status.processing;
  return p && p.session_id === id ? p : null;
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function viewDetail() {
  const wrap = h("div", { class: "view" });
  const d = S.detail;
  if (!d) { wrap.appendChild(h("div", { class: "secondary" }, "Session not found.")); return wrap; }

  wrap.appendChild(h("a", { href: "#/", style: { cursor: "pointer", display: "inline-flex", gap: "6px", fontSize: "14px", fontWeight: "600", color: "var(--color-text-secondary)", textDecoration: "none", marginBottom: "20px" } }, "← All sessions"));

  const onRename = debounce((v) => api.patchSession(d.id, { name: v.trim() || null }).catch(fail), 500);
  const onOccurred = debounce((date, time) => api.patchSession(d.id, { occurred_at: composeOccurred(date, time) }).catch(fail), 500);
  const occ = splitOccurred(d.occurred_at);
  const nameInput = h("input", { class: "name-input", "data-focus": "name", value: d.name || "", placeholder: d.id,
    "aria-label": "Session name", title: "Name this session — the ID stays its identity",
    oninput: (e) => onRename(e.target.value) });
  const dateInput = h("input", { class: "field", "data-focus": "occurred-date", type: "date", value: occ.date,
    style: { width: "auto", padding: "6px 9px", fontSize: "13px" },
    "aria-label": "Session date", title: "When this conversation actually happened — optional",
    oninput: (e) => { const time = document.getElementById("occurred-time"); onOccurred(e.target.value, time ? time.value : ""); } });
  const timeInput = h("input", { id: "occurred-time", class: "field", "data-focus": "occurred-time", type: "time", value: occ.time,
    style: { width: "auto", padding: "6px 9px", fontSize: "13px" },
    "aria-label": "Session time", title: "Optional time",
    oninput: (e) => { const date = document.querySelector('[data-focus="occurred-date"]'); onOccurred(date ? date.value : "", e.target.value); } });
  wrap.appendChild(h("div", { style: { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "16px", flexWrap: "wrap" } },
    h("div", {},
      h("div", { style: { display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" } }, nameInput, badge(d.state)),
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap", marginTop: "10px" } },
        h("span", { class: "mono muted", style: { fontSize: "12px" } }, "Date"), dateInput, timeInput,
        d.occurred_at ? h("button", { class: "btn", style: { padding: "6px 12px" }, onclick: () => api.patchSession(d.id, { occurred_at: null }).then(() => loadDetail(d.id).then(renderView)).catch(fail) }, "Clear") :
          h("span", { class: "mono muted", style: { fontSize: "12px" } }, "optional · user-set")),
      h("div", { class: "mono muted", style: { fontSize: "13px", marginTop: "8px" } },
        `${d.id}${d.occurred_at ? " · Date: " + fmtOccurred(d.occurred_at) : ""} · ${fmtDur(d.duration)} · ${fmtSize(d.size)} · session.m4a`)),
    h("div", { style: { display: "flex", gap: "9px", flexWrap: "wrap" } },
      d.state !== "recording" ? h("a", { class: "btn", href: `/v1/sessions/${d.id}/audio?download`, "aria-label": "Download audio" }, "↓ Download") : null,
      h("button", { class: "btn danger", onclick: () => (S.modal = { type: "delete", id: d.id }, renderModal()) }, "Delete"))));

  // audio player (native, reliable + accessible)
  if (d.state !== "recording") {
    wrap.appendChild(h("section", { class: "card", style: { padding: "18px 20px", marginTop: "24px" } },
      h("audio", { controls: true, preload: "none", src: `/v1/sessions/${d.id}/audio`, style: { width: "100%" }, "aria-label": "Session audio" })));
  }

  const proc = processingFor(d.id);
  const job = d.job;
  const active = activeJob(job);
  const running = job && job.state === "running";
  const queued = job && job.state === "queued";

  if (d.state === "failed" && job && job.state === "failed") {
    wrap.appendChild(failedBanner(d, job));
  } else if (queued) {
    wrap.appendChild(queuedCard(job));
  } else if (running) {
    wrap.appendChild(progressCard(proc ? Object.assign({}, job, proc, { id: job.id }) : job));
  } else if (d.state === "pending") {
    wrap.appendChild(transcribeCTA(d));
  }

  if (d.has_transcript && !active) {
    wrap.appendChild(transcriptSection(d));
  }
  return wrap;
}

function failedBanner(d, job) {
  return h("div", { style: { marginTop: "22px", border: "1px solid var(--color-error)", borderRadius: "var(--radius-lg)", background: "rgba(211,47,47,.06)", padding: "18px 20px" } },
    h("div", { style: { fontWeight: "700", color: "var(--color-error)" } }, "Transcription failed"),
    h("div", { class: "secondary", style: { fontSize: "14px", marginTop: "4px", lineHeight: "1.5" } }, `${job.last_error || "unknown error"} — ${job.attempts} attempt(s)`),
    h("div", { class: "mono muted", style: { fontSize: "12px", marginTop: "8px" } }, "session.m4a retained · Retry enqueues a fresh job"),
    h("button", { class: "btn primary", style: { marginTop: "14px" }, onclick: () => api.enqueue(d.id, "transcribe").then(() => toast("Retrying transcription")).catch(fail) }, "↻ Retry transcription"));
}

function progressText(proc) {
  if (proc.progress != null) return `${Math.round(proc.progress * 100)}%`;
  if (proc.route === "service") return "Processing…";
  return proc.stage || "Processing…";
}

function queuedCard(job) {
  const kind = job.kind === "diarize" ? "diarization" : "transcription";
  const queued = (S.jobs || []).filter((j) => j.state === "queued").sort((a, b) => a.id - b.id);
  const pos = queued.findIndex((j) => j.id === job.id) + 1;
  const line = pos > 0 ? `Position ${pos} in the queue · starts when the device is free` : "Waiting in the queue · starts when the device is free";
  return h("div", { class: "card", style: { marginTop: "22px", padding: "22px 24px", display: "flex", alignItems: "center", gap: "16px", flexWrap: "wrap" } },
    h("span", { style: { width: "12px", height: "12px", borderRadius: "50%", background: "#FFB300", boxShadow: "0 0 0 4px rgba(255,179,0,.2)", flexShrink: "0" } }),
    h("div", { style: { flex: "1", minWidth: "200px" } },
      h("div", { style: { fontWeight: "600" } }, `Queued for ${kind}`),
      h("div", { class: "mono muted", style: { fontSize: "12px", marginTop: "4px" } }, line)),
    h("button", { class: "btn danger", onclick: () => cancelJob(job.id) }, "Cancel job"));
}

function progressCard(proc) {
  const diar = proc.kind === "diarize";
  const pctText = progressText(proc);
  const bar = proc.progress != null ? h("div", { style: { height: "8px", borderRadius: "99px", background: "var(--color-surface-hover)", overflow: "hidden", marginTop: "14px", border: "1px solid var(--color-border)" } },
    h("div", { id: "live-progress", style: { height: "100%", width: (proc.progress * 100) + "%", background: "#FFB300", transition: "width .3s" } })) :
    h("div", { class: "indet", style: { marginTop: "14px" } }, h("div"));
  return h("div", { class: "card", style: { marginTop: "22px", padding: "22px 24px" } },
    h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "10px", fontWeight: "600" } },
        h("span", { style: { width: "12px", height: "12px", borderRadius: "50%", background: "#FFB300", boxShadow: "0 0 0 4px rgba(255,179,0,.2)", animation: "ledpulse 1.8s ease-in-out infinite" } }),
        diar ? "Diarizing… " : "Transcribing… ", h("span", { id: "live-progress-text", class: "mono secondary", style: { fontWeight: "500" } }, pctText)),
      h("button", { class: "btn danger", style: { padding: "7px 14px" }, onclick: () => cancelJob(proc.id) }, "Cancel")),
    bar,
    h("div", { class: "mono muted", style: { fontSize: "12px", marginTop: "10px" } },
      proc.route === "service" ? "On the processing service · progress not reported · recording is unaffected" : "faster-whisper on the Pi · a new recording cancels this"));
}

function cancelJob(jobId) {
  if (!jobId) return;
  api.cancelJob(jobId).then(async () => {
    toast("Job cancelled · session returned to pending");
    await refreshSessions();
    await refreshJobs();
    if (S.route.name === "detail") await loadDetail(S.route.id);
    renderView();
  }).catch(fail);
}

function transcribeCTA(d) {
  return h("div", { style: { marginTop: "22px", border: "1px dashed var(--color-border)", borderRadius: "var(--radius-lg)", background: "var(--color-surface)", padding: "24px", textAlign: "center" } },
    h("div", { class: "serif", style: { fontWeight: "700", fontSize: "20px", marginBottom: "16px" } }, "Audio only"),
    h("button", { class: "btn primary", onclick: () => openTranscribeModal(d.id) }, "Transcribe"),
    h("div", { class: "muted", style: { fontSize: "13px", marginTop: "14px", maxWidth: "470px", margin: "14px auto 0", lineHeight: "1.5" } },
      "Transcription runs on this device unless a processing service is configured."));
}

function openTranscribeModal(id) {
  const canDiarize = S.service && S.service.reachable && S.service.capabilities && S.service.capabilities.diarize;
  if (!canDiarize && id !== "__all__") {
    api.enqueue(id, "transcribe").then(() => toast("Transcribing")).catch(fail);
    return;
  }
  if (!canDiarize && id === "__all__") {
    api.bulk("transcribe", "pending").then((r) => toast(`Queued ${r.jobs.length} transcription job(s)`)).catch(fail);
    return;
  }
  S.modal = { type: "transcribe", id, diarize: false };
  renderModal();
}

function transcriptSection(d) {
  const diarized = !!d.diarized;
  const canDiarize = S.service && S.service.reachable && S.service.capabilities && S.service.capabilities.diarize;
  const head = h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "16px", flexWrap: "wrap", marginBottom: "18px" } },
    h("div", { style: { display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" } },
      h("h2", { class: "serif", style: { fontWeight: "700", fontSize: "24px", margin: "0" } }, "Transcript"),
      h("span", { class: "badge", style: { background: diarized ? "#9b59b6" : "var(--color-primary)" } }, diarized ? "Diarized" : "Transcribed")),
    h("div", { style: { display: "flex", gap: "9px", flexWrap: "wrap" } },
      (!diarized && canDiarize) ? h("button", { class: "btn primary", style: { background: "#9b59b6", borderColor: "#9b59b6" }, onclick: () => api.enqueue(d.id, "diarize").then(() => toast("Diarizing on the service")).catch(fail) }, "Diarize") : null,
      diarized ? h("button", { class: "btn", title: "Re-transcribe without speaker labels — reversible any time", onclick: () => api.enqueue(d.id, "transcribe").then(() => toast("Re-transcribing · diarization removed")).catch(fail) }, "↻ Re-transcribe") : null));

  const section = h("section", { style: { marginTop: "28px" } }, head);
  section.appendChild(diarized ? diarizedBody(d) : plainBody());
  return section;
}

function plainBody() {
  const panel = h("div", { class: "card panel", style: { maxWidth: "800px" } },
    h("div", { class: "mono muted", style: { fontSize: "12px", borderBottom: "1px solid var(--color-border)", paddingBottom: "14px", marginBottom: "18px" } }, "transcript.md · text only"));
  for (const seg of S.segments) {
    panel.appendChild(h("div", { class: "seg" },
      h("span", { class: "t" }, fmtClock(seg.start)),
      h("span", { class: "text" }, seg.text)));
  }
  return panel;
}

function diarizedBody(d) {
  const labels = [];
  for (const seg of S.segments) if (seg.speaker && !labels.includes(seg.speaker)) labels.push(seg.speaker);
  const colorOf = {}; labels.forEach((l, i) => (colorOf[l] = SPEAKER_PALETTE[i % SPEAKER_PALETTE.length]));
  const nameOf = {}; (S.speakers || []).forEach((s) => (nameOf[s.label] = s.name));

  // naming sidebar
  const side = h("div", { class: "card", style: { padding: "22px", position: "sticky", top: "88px", maxHeight: "calc(100vh - 104px)", overflowY: "auto" } },
    h("div", { class: "serif", style: { fontWeight: "700", fontSize: "18px" } }, "Name the speakers"),
    h("div", { class: "secondary", style: { fontSize: "13px", lineHeight: "1.55", margin: "6px 0 16px" } }, "Each voice offers a few things it actually said — read along, play the clearest, then type who it is. Names replace the Speaker N labels throughout."));
  const list = h("div", { style: { display: "flex", flexDirection: "column", gap: "14px" } });
  (S.speakers || []).forEach((sp, i) => {
    const color = colorOf[sp.label] || SPEAKER_PALETTE[i % SPEAKER_PALETTE.length];
    // The device chooses the candidates (GET /speakers); `n` indexes what it sent.
    const samples = sp.samples || [];
    const idx = Math.min(S.sampleIdx[sampleKey(d.id, sp.label)] || 0, Math.max(0, samples.length - 1));
    const cur = samples[idx] || null;
    const many = samples.length > 1;
    const active = cur != null && isActiveTurn(cur.start);

    const card = h("div", { class: "spk-card" + (active ? " active" : ""), style: { borderColor: active ? color : null, boxShadow: active ? `0 0 0 3px color-mix(in srgb, ${color} 12%, transparent)` : null },
      onclick: () => cur && activateSample(cur.start, false) },
      h("div", { style: { display: "flex", alignItems: "center", gap: "9px" } },
        h("span", { style: { width: "22px", height: "22px", borderRadius: "50%", background: color, color: "#fff", display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: "700", fontSize: "11px", flexShrink: "0" } }, (sp.name || "").trim() ? sp.name.trim()[0].toUpperCase() : String(i + 1)),
        h("span", { class: "mono muted", style: { fontSize: "12px", flex: "1" } }, `${sp.label} · ${sp.segments} turn${sp.segments === 1 ? "" : "s"}`),
        h("span", { class: "mono muted", style: { fontSize: "11px" } }, many ? `Clip ${idx + 1} of ${samples.length}` : (cur ? "Voice sample" : "No sample"))));

    if (cur) {
      const clipSec = Math.min(cur.end - cur.start, 6);
      card.appendChild(h("div", { style: { display: "flex", alignItems: "center", gap: "10px", marginTop: "11px" } },
        h("button", { class: "btn", style: { padding: "6px 11px" }, "aria-label": `Play clip ${idx + 1} of ${sp.label}`,
          onclick: (e) => { e.stopPropagation(); playSample(d.id, sp.label, idx, cur.start); } }, "▶"),
        h("div", { class: "mono muted", style: { fontSize: "11px", flex: "1", minWidth: "0" } },
          `Highlighted below · ${fmtClock(cur.start)} · ${clipSec.toFixed(1).replace(/\.0$/, "")}s clip`),
        // stopPropagation so the card's activate handler can't clobber the step.
        many ? h("div", { style: { display: "flex", gap: "6px", flexShrink: "0" } },
          h("button", { class: "btn", style: { padding: "6px 10px" }, "aria-label": `Previous clip for ${sp.label}`,
            onclick: (e) => { e.stopPropagation(); stepSample(d.id, sp.label, samples, -1); } }, "‹"),
          h("button", { class: "btn", style: { padding: "6px 10px" }, "aria-label": `Next clip for ${sp.label}`,
            onclick: (e) => { e.stopPropagation(); stepSample(d.id, sp.label, samples, 1); } }, "›")) : null));
    }

    card.appendChild(h("input", { class: "spk-input", "data-focus": "spk-" + sp.label, value: sp.name || "", placeholder: "Assign a name…",
      "aria-label": `Name for ${sp.label}`, onfocus: () => cur && activateSample(cur.start, false),
      oninput: (e) => updateSpeakerNameLive(d.id, sp.label, e.target.value) }));
    list.appendChild(card);
  });
  side.appendChild(list);

  // transcript body
  const body = h("div", { class: "card panel" },
    h("div", { class: "mono muted", style: { fontSize: "12px", borderBottom: "1px solid var(--color-border)", paddingBottom: "14px", marginBottom: "18px" } }, "transcript.md · diarized on the processing service"));
  for (const seg of S.segments) {
    const color = colorOf[seg.speaker] || "var(--color-text)";
    const who = (nameOf[seg.speaker] && nameOf[seg.speaker].trim()) ? nameOf[seg.speaker] : seg.speaker;
    // The turn the selected voice sample was cut from — reading it while the clip
    // plays is easier than naming a voice from a clip in the abstract.
    const active = isActiveTurn(seg.start);
    body.appendChild(h("div", Object.assign({ class: "spk-turn" + (active ? " active" : "") },
      active ? { "data-turn": "active", style: { borderLeftColor: color, background: `color-mix(in srgb, ${color} 10%, transparent)` } } : {}),
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px", marginBottom: "5px" } },
        h("span", { style: { width: "8px", height: "8px", borderRadius: "50%", background: color } }),
        h("span", { style: { fontWeight: "700", fontSize: "14px", color: color } }, who),
        h("span", { class: "mono muted", style: { fontSize: "12px" } }, fmtClock(seg.start))),
      h("div", { style: { fontSize: "16px", lineHeight: "1.7", paddingLeft: "16px" } }, seg.text)));
  }
  return h("div", { class: "diar-grid", style: { display: "grid", gridTemplateColumns: "320px 1fr", gap: "24px", alignItems: "start" } }, side, body);
}

const speakerSaveTimers = new Map();
function updateSpeakerNameLive(id, label, value) {
  const name = value.trim() || null;
  const apply = (rows) => (rows || []).map((sp) => sp.label === label ? Object.assign({}, sp, { name }) : sp);
  S.speakers = apply(S.speakers);
  if (S.detail && S.detail.id === id) S.detail.speakers = apply(S.detail.speakers);
  // Match the prototype: typed names immediately replace `Speaker N` throughout
  // the visible transcript, while persistence is debounced to the device API.
  renderView();

  const key = id + "\0" + label;
  clearTimeout(speakerSaveTimers.get(key));
  speakerSaveTimers.set(key, setTimeout(() => {
    api.setSpeaker(id, label, name)
      .then((result) => {
        if (result && result.speakers && S.detail && S.detail.id === id) {
          S.speakers = result.speakers;
          S.detail.speakers = result.speakers;
        }
      })
      .catch(fail)
      .finally(() => speakerSaveTimers.delete(key));
  }, 500));
}

// ---- voice samples --------------------------------------------------------
// Sample starts come from the same transcript the segments do, so they match
// exactly; the epsilon only guards float formatting across the JSON round-trip.
function sampleKey(id, label) { return id + "\0" + label; }
function isActiveTurn(start) {
  return S.activeTurnStart != null && Math.abs(start - S.activeTurnStart) < 0.001;
}

// Light the turn and scroll to it. `force` re-runs the fade and the scroll for a
// clip that is already selected — playing or stepping should always re-orient you,
// but merely refocusing the name field you are typing in should not.
function activateSample(start, force) {
  if (isActiveTurn(start) && !force) return;
  S.activeTurnStart = start;
  S.scrollToTurn = true;
  renderView();
}

function stepSample(id, label, samples, delta) {
  const key = sampleKey(id, label);
  const cur = Math.min(S.sampleIdx[key] || 0, samples.length - 1);
  const next = (cur + delta + samples.length) % samples.length;
  S.sampleIdx[key] = next;
  stopSample();
  activateSample(samples[next].start, true);
}

let sampleAudio = null;
function stopSample() { if (sampleAudio) { sampleAudio.pause(); sampleAudio = null; } }
// The session player is a native <audio> with no JS handle, so reach for it directly.
function stopPlayback() {
  document.querySelectorAll("audio").forEach((el) => { if (!el.paused) el.pause(); });
}

function playSample(id, label, n, start) {
  stopSample();      // only one thing plays at a time
  stopPlayback();
  sampleAudio = new Audio(`/v1/sessions/${id}/speakers/${encodeURIComponent(label)}/sample?n=${n || 0}`);
  sampleAudio.play().catch(() => toast("Could not play sample"));
  activateSample(start, true);
}

// ---- SETTINGS view --------------------------------------------------------
function viewSettings() {
  const svc = S.service || { configured: false, url: null, reachable: false, capabilities: null };
  const connected = svc.configured && svc.reachable;
  const svcColor = connected ? "var(--color-success)" : (svc.configured ? "var(--color-error)" : "var(--color-muted)");
  const svcLabel = connected ? "Connected" : (svc.configured ? "Unreachable" : "Not set — using this device");
  const caps = connected ? `transcribe ✓ · diarize ${svc.capabilities && svc.capabilities.diarize ? "✓" : "✗"}`
    : (svc.configured ? "No response — transcription falls back to this device"
      : "Optional. Without one, transcription runs on the Pi and diarization is unavailable.");

  const draft = h("input", { class: "field", "data-focus": "svc", value: svc.url || "", placeholder: "http://homelab.local:9010",
    style: { flex: "1", minWidth: "260px" }, "aria-label": "Processing service URL" });
  const save = h("button", { class: "btn primary", onclick: () => { const u = draft.value.trim(); if (!u) return toast("Enter a service URL"); api.putService(u).then(async () => { S.service = await api.service(); toast("Service saved"); renderView(); }).catch(fail); } }, "Save");
  const clear = svc.configured ? h("button", { class: "btn danger", onclick: () => api.clearService().then(async () => { S.service = await api.service(); toast("Service cleared · transcribing locally"); renderView(); }).catch(fail) }, "Clear") : null;

  const routeLabel = connected && svc.capabilities && svc.capabilities.transcribe ? "on the processing service" : "on this device";

  return h("div", { class: "view", style: { maxWidth: "720px" } },
    h("h1", { class: "title" }, "Settings"),
    h("p", { class: "secondary", style: { fontSize: "15px", lineHeight: "1.6", margin: "6px 0 28px" } },
      "This device records and transcribes on its own. An optional processing service on your network can speed transcription and adds diarization."),
    h("section", { class: "card", style: { padding: "26px 28px" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "10px", marginBottom: "4px" } },
        h("h3", { class: "serif", style: { fontWeight: "700", fontSize: "20px", margin: "0" } }, "Processing service"),
        h("span", { class: "badge", style: { background: svcColor } }, svcLabel)),
      h("p", { class: "secondary", style: { fontSize: "14px", lineHeight: "1.6", margin: "6px 0 18px" } },
        "Stored in config.toml under [processing].service_url. Applied without a restart. Leave it empty and everything still works."),
      h("div", { style: { display: "flex", gap: "10px", flexWrap: "wrap", alignItems: "center" } }, draft, save, clear),
      h("div", { class: "mono muted", style: { fontSize: "12px", marginTop: "14px" } }, caps)),
    h("section", { class: "card", style: { padding: "26px 28px", marginTop: "16px" } },
      h("h3", { class: "serif", style: { fontWeight: "700", fontSize: "20px", margin: "0 0 16px" } }, "Device"),
      h("div", { class: "grid2 mono" },
        h("span", { class: "muted" }, "Web UI"), h("span", {}, "0.0.0.0:8080"),
        h("span", { class: "muted" }, "Access"), h("span", {}, "Trusted LAN · no login (v1)"),
        h("span", { class: "muted" }, "Capture"), h("span", {}, "16 kHz · 16-bit · mono (left mic)"),
        h("span", { class: "muted" }, "Stored as"), h("span", {}, "session.m4a · AAC-LC 32 kbps"),
        h("span", { class: "muted" }, "Transcribes"), h("span", {}, routeLabel)),
      h("div", { class: "muted", style: { fontSize: "13px", marginTop: "18px", lineHeight: "1.6", borderTop: "1px solid var(--color-border)", paddingTop: "16px" } },
        "Other settings live in config.toml and take effect after sudo systemctl restart earshot.")));
}

// ---- modals ---------------------------------------------------------------
function renderModal() {
  const root = document.getElementById("modal-root");
  root.innerHTML = "";
  if (!S.modal) return;
  if (S.modal.type === "delete") {
    root.appendChild(h("div", { class: "modal-bg", onclick: () => (S.modal = null, renderModal()) },
      h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": "Delete session", onclick: (e) => e.stopPropagation() },
        h("h3", { class: "serif", style: { fontWeight: "700", fontSize: "22px", margin: "0 0 8px" } }, "Delete session?"),
        h("p", { class: "secondary", style: { fontSize: "14px", lineHeight: "1.6", margin: "0 0 22px" } }, "This removes the session directory and everything in it — audio and transcript. This cannot be undone."),
        h("div", { style: { display: "flex", gap: "10px", justifyContent: "flex-end" } },
          h("button", { class: "btn", onclick: () => (S.modal = null, renderModal()) }, "Cancel"),
          h("button", { class: "btn primary", style: { background: "var(--color-error)", borderColor: "var(--color-error)" },
            onclick: () => { const id = S.modal.id; S.modal = null; api.del(id).then(() => { toast("Session deleted"); location.hash = "#/"; }).catch(fail); } }, "Delete")))));
  } else if (S.modal.type === "transcribe") {
    const m = S.modal;
    const id = m.id;
    const canDiarize = S.service && S.service.reachable && S.service.capabilities && S.service.capabilities.diarize;
    const close = () => (S.modal = null, renderModal());
    const submit = () => {
      const n = parseInt((document.getElementById("speaker-count-hint") || {}).value, 10);
      const opts = Number.isFinite(n) && n > 0 ? { num_speakers: n } : {};
      const diarize = !!m.diarize;
      close();
      const p = id === "__all__"
        ? api.bulk(diarize ? "diarize" : "transcribe", diarize ? "undiarized" : "pending")
        : api.enqueue(id, diarize ? "diarize" : "transcribe", diarize ? opts : {});
      p.then((r) => {
        const count = r && r.jobs ? ` (${r.jobs.length})` : "";
        toast(diarize ? `Diarizing queued${count}` : `Transcription queued${count}`);
      }).catch(fail);
    };
    root.appendChild(h("div", { class: "modal-bg", onclick: close },
      h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": "Transcribe options", onclick: (e) => e.stopPropagation() },
        h("h3", { class: "serif", style: { fontWeight: "700", fontSize: "22px", margin: "0 0 6px" } }, id === "__all__" ? "Transcribe all" : "Transcribe this session"),
        h("p", { class: "secondary", style: { fontSize: "14px", lineHeight: "1.6", margin: "0 0 20px" } },
          id === "__all__" ? "Runs for every pending session. Turn on speaker labels to diarize every not-yet-diarized session instead." : "Text with timestamps. Turn on speaker labels below if you need them."),
        h("div", { class: "card", style: { display: "flex", gap: "14px", alignItems: "flex-start", background: "var(--color-bg)", padding: "16px 18px", marginBottom: "12px" } },
          h("span", { style: { fontSize: "20px" } }, "▤"),
          h("span", {}, h("strong", {}, "Transcribe"), h("br"), h("span", { class: "secondary", style: { fontSize: "13px" } }, "Full text with timestamps, saved as transcript.md."))),
        canDiarize ? h("label", { style: { display: "flex", gap: "12px", alignItems: "flex-start", cursor: "pointer" } },
          h("input", { type: "checkbox", checked: !!m.diarize, onchange: (e) => { m.diarize = e.target.checked; renderModal(); } }),
          h("span", {}, h("strong", {}, "Also identify speakers"), h("br"), h("span", { class: "secondary", style: { fontSize: "13px" } }, "Diarizes on the processing service. You name the detected speakers afterward."))) : null,
        canDiarize && m.diarize ? h("div", { style: { display: "flex", alignItems: "center", gap: "10px", marginTop: "12px" } },
          h("label", { class: "secondary", style: { fontSize: "13px", fontWeight: "600" } }, "Speakers (optional)"),
          h("input", { id: "speaker-count-hint", class: "field", inputmode: "numeric", placeholder: "auto", style: { width: "76px", padding: "8px 11px" }, "aria-label": "Optional speaker count hint" }),
          h("span", { class: "muted", style: { fontSize: "12px", lineHeight: "1.4", flex: "1" } }, "Hint passed to the service.")) : null,
        h("div", { style: { display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "22px" } },
          h("button", { class: "btn", onclick: close }, "Cancel"),
          h("button", { class: "btn primary", onclick: submit }, m.diarize ? "Transcribe + diarize" : "Transcribe")))));
  } else if (S.modal.type === "upload") {
    root.appendChild(uploadModal(S.modal));
  }
}

// The upload dialog (rpi/requirements/web-ui/upload-audio.md). State lives on the
// modal object `m` so it survives re-renders; text fields are read at submit time
// (and captured before a file-change re-render) so typing never loses focus.
const MAX_UPLOAD_MB = 500;
function uploadModal(m) {
  const close = () => { if (m.uploading) return; S.modal = null; renderModal(); };
  const capture = () => {
    const n = document.getElementById("up-name"); if (n) m.name = n.value;
    const d = document.getElementById("up-date"); if (d) m.date = d.value;
    const t = document.getElementById("up-time"); if (t) m.time = t.value;
  };
  const onFile = (e) => {
    capture();
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    const mb = f.size / 1048576;
    m.file = f;
    if (mb > MAX_UPLOAD_MB) { m.error = `Too large — ${mb.toFixed(0)} MB is over the ${MAX_UPLOAD_MB} MB limit.`; }
    else { m.error = null; if (!m.name) m.name = f.name.replace(/\.[^.]+$/, ""); }
    renderModal();
  };
  const submit = () => {
    capture();
    if (!m.file || m.error || m.uploading) return;
    m.uploading = true; renderModal();
    api.upload(m.file, { name: (m.name || "").trim(), occurred_at: composeOccurred(m.date, m.time) })
      .then((d) => { S.modal = null; renderModal(); toast("Uploaded · encoded to session.m4a · pending"); location.hash = "#/s/" + d.id; })
      .catch((e) => { m.uploading = false; m.error = e.message || "Upload failed"; renderModal(); });
  };

  const body = [];
  if (m.uploading) {
    body.push(
      h("div", { class: "card", style: { padding: "22px 24px", background: "var(--color-bg)" } },
        h("div", { style: { display: "flex", alignItems: "center", gap: "10px", fontWeight: "600" } },
          h("span", { class: "spinner", "aria-hidden": "true" }), "Encoding session.m4a…"),
        h("div", { class: "indet", style: { marginTop: "14px" } }, h("div")),
        h("div", { class: "mono muted", style: { fontSize: "12px", marginTop: "12px" } },
          "Transcoding to AAC-LC · 16 kHz mono · the same encode a recording ends with.")));
  } else {
    body.push(h("input", { id: "up-file", type: "file", accept: "audio/*,video/*", style: { display: "none" }, onchange: onFile }));
    if (!m.file) {
      body.push(h("label", { for: "up-file", class: "empty", style: { display: "flex", flexDirection: "column", alignItems: "center", gap: "8px", padding: "30px 24px", cursor: "pointer", background: "var(--color-bg)" } },
        h("div", { style: { fontSize: "26px" } }, "⬆"),
        h("div", { style: { fontWeight: "700", fontSize: "15px" } }, "Choose an audio file"),
        h("div", { class: "muted", style: { fontSize: "13px" } }, `Any format ffmpeg can decode · up to ${MAX_UPLOAD_MB} MB`)));
    } else {
      body.push(h("div", { class: "card", style: { display: "flex", alignItems: "center", gap: "14px", padding: "14px 16px", background: "var(--color-bg)" } },
        h("div", { style: { minWidth: "0", flex: "1" } },
          h("div", { style: { fontWeight: "600", fontSize: "14px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, m.file.name),
          h("div", { class: "mono muted", style: { fontSize: "12px", marginTop: "2px" } }, fmtSize(m.file.size))),
        h("label", { for: "up-file", style: { fontSize: "13px", fontWeight: "600", color: "var(--color-primary)", cursor: "pointer", flexShrink: "0" } }, "Change")));
    }
    if (m.error) {
      body.push(h("div", { style: { color: "var(--color-error)", fontSize: "13px", lineHeight: "1.5", marginTop: "12px" } }, m.error));
    }
    body.push(h("div", { style: { marginTop: "20px", paddingTop: "18px", borderTop: "1px solid var(--color-border)", display: "flex", flexDirection: "column", gap: "14px" } },
      h("div", {},
        h("label", { class: "secondary", style: { display: "block", fontSize: "13px", fontWeight: "600", marginBottom: "7px" } }, "Name (optional)"),
        h("input", { id: "up-name", class: "field", value: m.name, placeholder: "Falls back to the session ID", "aria-label": "Session name" })),
      h("div", {},
        h("label", { class: "secondary", style: { display: "block", fontSize: "13px", fontWeight: "600", marginBottom: "7px" } }, "Date & time (optional)"),
        h("div", { style: { display: "flex", gap: "10px", flexWrap: "wrap" } },
          h("input", { id: "up-date", class: "field", type: "date", value: m.date, style: { width: "auto" }, "aria-label": "Date it happened" }),
          h("input", { id: "up-time", class: "field", type: "time", value: m.time, style: { width: "auto" }, "aria-label": "Time it happened" })),
        h("div", { class: "muted", style: { fontSize: "12px", marginTop: "8px", lineHeight: "1.5" } },
          "When the conversation actually happened. Worth setting — the device only knows when the file was uploaded."))));
    body.push(h("div", { style: { display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "22px" } },
      h("button", { class: "btn", onclick: close }, "Cancel"),
      h("button", { class: "btn primary", disabled: !m.file || !!m.error, onclick: submit }, "Upload")));
  }

  return h("div", { class: "modal-bg", onclick: close },
    h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": "Upload an audio file", onclick: (e) => e.stopPropagation() },
      h("h3", { class: "serif", style: { fontWeight: "700", fontSize: "22px", margin: "0 0 6px" } }, "Upload an audio file"),
      h("p", { class: "secondary", style: { fontSize: "14px", lineHeight: "1.6", margin: "0 0 20px" } },
        "Create a session from a recording made elsewhere — a phone memo, another recorder, an exported call. It's encoded to session.m4a on the device, then behaves exactly like a recorded session."),
      ...body));
}

// ---- record toggle --------------------------------------------------------
async function toggleRecord() {
  try {
    if (S.status && S.status.state === "recording") await api.stopRec();
    else await api.startRec();
  } catch (e) { fail(e); }
}

// Open the upload modal, unless the device is not idle. The encode holds Pi CPU,
// so an upload is refused while recording/finalizing and while a local job runs
// (rpi/requirements/web-ui/upload-audio.md); the server enforces this too.
function openUpload() {
  const st = S.status || { state: "idle" };
  if (st.state === "recording" || st.state === "finalizing") { toast("Upload is disabled while recording"); return; }
  if (st.state === "processing") { toast("Busy — try the upload once the device is idle"); return; }
  if (st.state === "disk_full" || (st.disk && st.disk.blocked)) { toast("Disk threshold reached — free space first"); return; }
  S.modal = { type: "upload", file: null, name: "", date: "", time: "", uploading: false, error: null };
  renderModal();
}

// ---- live updates (SSE) ---------------------------------------------------
function connectEvents() {
  const es = new EventSource("/v1/events");
  es.addEventListener("state", (e) => {
    S.status = JSON.parse(e.data);
    renderHeader();
    // Re-render only on a *structural* change (a state transition, or a job
    // appearing/disappearing). A plain elapsed/disk/progress tick just updates
    // its own nodes in place, so the view doesn't flicker while recording.
    const fp = structFingerprint(S.status);
    if (fp !== S.lastStructFp) { S.lastStructFp = fp; renderView(); }
    else updateLiveCounters();
  });
  es.addEventListener("sessions-changed", async () => {
    await refreshSessions();
    await refreshJobs();
    if (S.route.name === "detail") await loadDetail(S.route.id);
    renderView();
  });
  es.addEventListener("jobs-changed", async () => {
    await refreshJobs();
    if (S.route.name === "detail") await loadDetail(S.route.id);
    renderView();
  });
  es.onerror = () => {/* EventSource auto-reconnects; first event after is a fresh snapshot */};
}

// ---- boot -----------------------------------------------------------------
async function boot() {
  applyTheme();
  try { S.status = await api.status(); } catch (_) {}
  try { S.service = await api.service(); } catch (_) {}
  await refreshSessions();
  await refreshJobs();
  await onRoute();
  connectEvents();
}
boot();
