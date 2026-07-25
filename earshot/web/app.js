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
  startRec: () => send("POST", "/v1/recording"),
  stopRec: () => send("DELETE", "/v1/recording"),
  enqueue: (id, kind, opts) => send("POST", "/v1/sessions/" + id + "/jobs", Object.assign({ kind }, opts || {})),
  bulk: (kind) => send("POST", "/v1/jobs", { kind, target: "pending" }),
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
  transcribing: { label: "Transcribing", color: "#FFB300" },
  diarizing: { label: "Diarizing", color: "#FFB300" },
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

// ---- state ----------------------------------------------------------------
const S = {
  status: null, sessions: [], service: null,
  detail: null, segments: [], speakers: [],
  route: { name: "list" },
  modal: null, // {type:'delete'|'transcribe', id}
  lastStructFp: null,
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
  document.title = "earshot hub — " + dev.label;
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
async function loadDetail(id) {
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

  return h("section", { class: "card hero" }, wrap,
    h("div", { style: { flex: "1", minWidth: "220px" } },
      h("div", { class: "mono muted", style: { fontSize: "12px", textTransform: "uppercase", letterSpacing: ".08em", marginBottom: "6px" } }, eyebrow),
      h("div", { id: recording ? "live-clock" : null, class: "serif", style: { fontWeight: "700", fontSize: "30px", lineHeight: "1.1" } }, headline),
      h("div", { class: "secondary", style: { fontSize: "15px", marginTop: "8px", maxWidth: "420px", lineHeight: "1.5" } }, sub)),
    disk);
}

function viewList() {
  const wrap = h("div", { class: "view" });
  wrap.appendChild(recordHero());

  const pending = S.sessions.filter((s) => s.state === "pending");
  const head = h("div", { style: { display: "flex", alignItems: "baseline", justifyContent: "space-between", margin: "40px 0 16px", gap: "16px", flexWrap: "wrap" } },
    h("div", {}, h("h2", { class: "title" }, "Sessions"),
      h("div", { class: "mono muted", style: { fontSize: "13px", marginTop: "4px" } }, `${S.sessions.length} on device`)),
    pending.length ? h("button", { class: "btn primary ghost", onclick: () => api.bulk("transcribe").then(() => toast("Transcribing all pending")).catch(fail) }, `Transcribe all pending (${pending.length})`) : null);
  wrap.appendChild(head);

  if (S.sessions.length === 0) {
    wrap.appendChild(h("div", { class: "empty" },
      h("div", { style: { fontSize: "40px", marginBottom: "8px", opacity: ".5" } }, "🎙️"),
      h("div", { class: "serif", style: { fontWeight: "700", fontSize: "22px" } }, "No recordings yet"),
      h("div", { class: "secondary", style: { maxWidth: "380px", margin: "10px auto 0", lineHeight: "1.6" } },
        "Press the button on the device — or the record control above — to capture your first session.")));
    return wrap;
  }

  const rows = h("div", { class: "rows" });
  for (const s of S.sessions) {
    const m = STATUS_META[s.state] || STATUS_META.pending;
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
      h("div", { style: { width: "200px", display: "flex", justifyContent: "flex-end" } }, badge(s.state)),
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
  const isTranscribing = proc && proc.kind === "transcribe";
  const isDiarizing = proc && proc.kind === "diarize";

  if (d.state === "failed" && job && job.state === "failed") {
    wrap.appendChild(failedBanner(d, job));
  } else if (isTranscribing || isDiarizing) {
    wrap.appendChild(progressCard(proc));
  } else if (d.state === "pending") {
    wrap.appendChild(transcribeCTA(d));
  }

  if (d.has_transcript && !isTranscribing && !isDiarizing) {
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

function progressCard(proc) {
  const diar = proc.kind === "diarize";
  const pctText = progressText(proc);
  const bar = proc.progress != null ? h("div", { style: { height: "8px", borderRadius: "99px", background: "var(--color-surface-hover)", overflow: "hidden", marginTop: "14px", border: "1px solid var(--color-border)" } },
    h("div", { id: "live-progress", style: { height: "100%", width: (proc.progress * 100) + "%", background: "#FFB300", transition: "width .3s" } })) : null;
  return h("div", { class: "card", style: { marginTop: "22px", padding: "22px 24px" } },
    h("div", { style: { display: "flex", alignItems: "center", gap: "10px", fontWeight: "600" } },
      h("span", { style: { width: "12px", height: "12px", borderRadius: "50%", background: "#FFB300", boxShadow: "0 0 0 4px rgba(255,179,0,.2)", animation: "ledpulse 1.8s ease-in-out infinite" } }),
      diar ? "Diarizing… " : "Transcribing… ", h("span", { id: "live-progress-text", class: "mono secondary", style: { fontWeight: "500" } }, pctText)),
    bar,
    h("div", { class: "mono muted", style: { fontSize: "12px", marginTop: "10px" } },
      proc.route === "service" ? "On the processing service · recording is unaffected" : "faster-whisper on the Pi · a new recording cancels this"));
}

function transcribeCTA(d) {
  const canDiarize = S.service && S.service.reachable && S.service.capabilities && S.service.capabilities.diarize;
  return h("div", { style: { marginTop: "22px", border: "1px dashed var(--color-border)", borderRadius: "var(--radius-lg)", background: "var(--color-surface)", padding: "24px", textAlign: "center" } },
    h("div", { class: "serif", style: { fontWeight: "700", fontSize: "20px", marginBottom: "16px" } }, "Audio only"),
    h("button", { class: "btn primary", onclick: () => { if (canDiarize) { S.modal = { type: "transcribe", id: d.id }; renderModal(); } else api.enqueue(d.id, "transcribe").then(() => toast("Transcribing")).catch(fail); } }, "Transcribe"),
    h("div", { class: "muted", style: { fontSize: "13px", marginTop: "14px", maxWidth: "470px", margin: "14px auto 0", lineHeight: "1.5" } },
      "Transcription runs on this device unless a processing service is configured."));
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
    h("div", { class: "secondary", style: { fontSize: "13px", lineHeight: "1.55", margin: "6px 0 16px" } }, "Play each voice, then type who it is. Names replace the Speaker N labels throughout."));
  const list = h("div", { style: { display: "flex", flexDirection: "column", gap: "14px" } });
  (S.speakers || []).forEach((sp, i) => {
    const color = colorOf[sp.label] || SPEAKER_PALETTE[i % SPEAKER_PALETTE.length];
    const onName = debounce((v) => api.setSpeaker(d.id, sp.label, v.trim() || null).catch(fail), 500);
    list.appendChild(h("div", { style: { border: "1px solid var(--color-border)", borderRadius: "var(--radius-md)", padding: "14px" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "9px" } },
        h("span", { style: { width: "22px", height: "22px", borderRadius: "50%", background: color, color: "#fff", display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: "700", fontSize: "11px" } }, (sp.name || "").trim() ? sp.name.trim()[0].toUpperCase() : String(i + 1)),
        h("span", { class: "mono muted", style: { fontSize: "12px", flex: "1" } }, `${sp.label} · ${sp.segments} turn${sp.segments === 1 ? "" : "s"}`)),
      h("button", { class: "btn", style: { marginTop: "10px", padding: "6px 12px" }, "aria-label": `Play a sample of ${sp.label}`,
        onclick: () => playSample(d.id, sp.label) }, "▶ Voice sample"),
      h("input", { class: "spk-input", "data-focus": "spk-" + sp.label, value: sp.name || "", placeholder: "Assign a name…",
        "aria-label": `Name for ${sp.label}`, oninput: (e) => onName(e.target.value) })));
  });
  side.appendChild(list);

  // transcript body
  const body = h("div", { class: "card panel" },
    h("div", { class: "mono muted", style: { fontSize: "12px", borderBottom: "1px solid var(--color-border)", paddingBottom: "14px", marginBottom: "18px" } }, "transcript.md · diarized on the processing service"));
  for (const seg of S.segments) {
    const color = colorOf[seg.speaker] || "var(--color-text)";
    const who = (nameOf[seg.speaker] && nameOf[seg.speaker].trim()) ? nameOf[seg.speaker] : seg.speaker;
    body.appendChild(h("div", { class: "spk-turn" },
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px", marginBottom: "5px" } },
        h("span", { style: { width: "8px", height: "8px", borderRadius: "50%", background: color } }),
        h("span", { style: { fontWeight: "700", fontSize: "14px", color: color } }, who),
        !nameOf[seg.speaker] ? h("span", { class: "mono muted", style: { fontSize: "11px", border: "1px solid var(--color-border)", borderRadius: "var(--radius-pill)", padding: "1px 8px" } }, "unnamed") : null,
        h("span", { class: "mono muted", style: { fontSize: "12px" } }, fmtClock(seg.start))),
      h("div", { style: { fontSize: "16px", lineHeight: "1.7", paddingLeft: "16px" } }, seg.text)));
  }
  return h("div", { class: "diar-grid", style: { display: "grid", gridTemplateColumns: "320px 1fr", gap: "24px", alignItems: "start" } }, side, body);
}

let sampleAudio = null;
function playSample(id, label) {
  if (sampleAudio) { sampleAudio.pause(); }
  sampleAudio = new Audio(`/v1/sessions/${id}/speakers/${encodeURIComponent(label)}/sample`);
  sampleAudio.play().catch(() => toast("Could not play sample"));
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
    const id = S.modal.id;
    root.appendChild(h("div", { class: "modal-bg", onclick: () => (S.modal = null, renderModal()) },
      h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": "Transcribe options", onclick: (e) => e.stopPropagation() },
        h("h3", { class: "serif", style: { fontWeight: "700", fontSize: "22px", margin: "0 0 6px" } }, "Transcribe this session"),
        h("p", { class: "secondary", style: { fontSize: "14px", lineHeight: "1.6", margin: "0 0 20px" } }, "Diarization needs the processing service; plain transcription does not."),
        h("button", { class: "btn", style: { width: "100%", justifyContent: "flex-start", padding: "16px 18px", marginBottom: "12px" },
          onclick: () => { S.modal = null; renderModal(); api.enqueue(id, "transcribe").then(() => toast("Transcribing")).catch(fail); } }, "Transcribe — text with timestamps"),
        h("label", { class: "mono muted", style: { display: "block", fontSize: "12px", margin: "0 0 8px" } }, "Speaker count hint (optional)"),
        h("input", { id: "speaker-count-hint", class: "field", type: "number", min: "1", step: "1", placeholder: "infer automatically", style: { width: "100%", marginBottom: "12px" }, "aria-label": "Optional speaker count hint" }),
        h("button", { class: "btn", style: { width: "100%", justifyContent: "flex-start", padding: "16px 18px" },
          onclick: () => { const n = parseInt(document.getElementById("speaker-count-hint").value, 10); const opts = Number.isFinite(n) && n > 0 ? { num_speakers: n } : {}; S.modal = null; renderModal(); api.enqueue(id, "diarize", opts).then(() => toast("Diarizing on the service")).catch(fail); } }, "Diarize — label speakers"),
        h("div", { style: { display: "flex", justifyContent: "flex-end", marginTop: "20px" } },
          h("button", { class: "btn", onclick: () => (S.modal = null, renderModal()) }, "Cancel")))));
  }
}

// ---- record toggle --------------------------------------------------------
async function toggleRecord() {
  try {
    if (S.status && S.status.state === "recording") await api.stopRec();
    else await api.startRec();
  } catch (e) { fail(e); }
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
    if (S.route.name === "detail") await loadDetail(S.route.id);
    renderView();
  });
  es.addEventListener("jobs-changed", async () => {
    if (S.route.name === "detail") { await loadDetail(S.route.id); renderView(); }
  });
  es.onerror = () => {/* EventSource auto-reconnects; first event after is a fresh snapshot */};
}

// ---- boot -----------------------------------------------------------------
async function boot() {
  applyTheme();
  try { S.status = await api.status(); } catch (_) {}
  try { S.service = await api.service(); } catch (_) {}
  await refreshSessions();
  await onRoute();
  connectEvents();
}
boot();
