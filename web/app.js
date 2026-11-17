/* CLIP AUTO Studio — vanilla JS, no build step. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const main = $("#main");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!r.ok) { const t = await r.text(); throw new Error(t.slice(0, 200)); }
  return r.json();
};
const toast = (msg, err) => {
  const t = document.createElement("div"); t.className = "toast" + (err ? " err" : ""); t.textContent = msg;
  $("#toasts").append(t); setTimeout(() => t.remove(), 3800);
};
const fmt = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0"); };
const media = (p, bust) => p ? `/media?path=${encodeURIComponent(p)}${bust ? "&t=" + bust : ""}` : "";
const platformOf = l => /twitch\.tv/.test(l) ? "Twitch" : /youtu/.test(l) ? "YouTube" : /kick\.com/.test(l) ? "Kick" : /^[a-z]:\\|^\//i.test(l) ? "Local file" : "Link";
const SIG = { chat_rate: ["Chat speed", "#d7ff3a"], chat_keywords: ["Chat words", "#ff4f8b"], streamer_loudness: ["Loudness", "#5aa8ff"], speech_energy: ["Speech", "#ffb23a"], youtube_heatmap: ["Replays", "#b78cff"] };
const STAGES = [["download", "Download"], ["transcribe", "Transcribe"], ["detect", "Find moments"], ["selection", "Review"], ["render", "Render"]];

const S = { jobs: [], sys: null, campaigns: [], route: "new", arg: null, focus: 0, clipFilter: "all", draft: {} };

/* ---------------- polling ---------------- */
async function refresh() {
  try {
    S.jobs = await api("/api/jobs");
    const running = S.jobs.filter(j => ["RUNNING", "QUEUED"].includes(j.status)).length;
    const review = S.jobs.filter(j => j.status === "AWAITING_SELECTION").length;
    const pending = S.jobs.reduce((n, j) => n + (j.clips || []).filter(c => (c.status || "PENDING") === "PENDING").length, 0);
    $("#b-queue").textContent = running || ""; $("#b-review").textContent = review || ""; $("#b-clips").textContent = pending || "";
    if (S.route === "queue") renderQueue();
    if (S.route === "clips") updateClipsLive();
  } catch (e) { /* server restarting */ }
}
async function refreshSys() {
  try {
    const s = S.sys = await api("/api/system");
    $("#sys-gpu").textContent = s.gpu.ok ? s.gpu.name.replace(/NVIDIA GeForce |Laptop GPU/g, "").trim() : "not found";
    $("#sys-vram").style.width = s.gpu.ok ? (100 * s.gpu.mem_used / s.gpu.mem_total) + "%" : "0";
    $("#sys-llm").innerHTML = !s.llm.enabled ? '<span class="muted">off</span>' : s.ollama.running ? `<span style="color:var(--ok)">${esc(s.llm.model)}</span>` : '<span style="color:var(--warn)">Ollama offline</span>';
    $("#sys-whisper").textContent = s.whisper_model;
  } catch (e) {}
}

/* ---------------- router ---------------- */
function route() {
  const [r, arg] = (location.hash.slice(1) || "new").split("/");
  S.route = r; S.arg = arg || null; S.focus = 0;
  $$("#nav a").forEach(a => a.classList.toggle("on", a.dataset.page === r));
  ({ new: renderNew, queue: renderQueue, review: renderReview, clips: renderClips, live: renderLive, campaigns: renderCampaigns, settings: renderSettings }[r] || renderNew)();
}
window.addEventListener("hashchange", route);

/* ---------------- NEW RUN ---------------- */
async function renderNew() {
  S.campaigns = await api("/api/campaigns").catch(() => []);
  const sys = S.sys || { presets: ["classic_yellow"] };
  const d = S.draft;
  main.innerHTML = `
  <div class="head"><div><h1>New run</h1><p>Paste VOD or stream links from campaigns you've joined. One per line.</p></div></div>
  <div class="grid2">
    <div class="card col" style="gap:14px">
      <div class="col"><span class="lbl">Links</span>
        <textarea id="links" placeholder="https://www.twitch.tv/videos/123456789&#10;https://www.youtube.com/watch?v=...&#10;https://kick.com/video/...">${esc(d.links || "")}</textarea>
        <div class="links-meta" id="linkmeta"></div></div>
      <div class="formgrid">
        <div class="col"><span class="lbl">Campaign</span><select id="camp"><option value="">No campaign</option>
          ${S.campaigns.map(c => `<option value="${esc(c.id)}" ${d.camp === c.id ? "selected" : ""}>${esc(c.name || c.id)}${c.streamer ? " · " + esc(c.streamer) : ""}</option>`).join("")}</select></div>
        <div class="col"><span class="lbl">Caption style</span><select id="preset"><option value="">Campaign default</option>
          ${sys.presets.map(p => `<option ${d.preset === p ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></div>
        <div class="col"><span class="lbl">How many clips</span>
          <div class="seg acc" id="count">${["Auto", "3", "5", "8", "12"].map(v => `<button data-v="${v}" class="${(d.count || "5") === v ? "on" : ""}">${v}</button>`).join("")}</div></div>
        <div class="col"><span class="lbl">Sensitivity</span>
          <div class="seg acc" id="sens">${[["2.5", "Picky"], ["2.0", "Normal"], ["1.5", "Loose"]].map(([v, l]) => `<button data-v="${v}" class="${(d.sens || "2.0") === v ? "on" : ""}">${l}</button>`).join("")}</div></div>
        <div class="col"><span class="lbl">Video quality</span><select id="quality">${["1080p", "720p", "480p"].map(q => `<option ${d.quality === q ? "selected" : ""}>${q}</option>`).join("")}</select></div>
        <div class="col"><span class="lbl">Options</span>
          <label class="row small"><span class="switch"><input type="checkbox" id="review" ${d.review === false ? "" : "checked"}><span></span></span>Review moments before rendering</label>
          <label class="row small"><span class="switch"><input type="checkbox" id="cpu" ${d.cpu ? "checked" : ""}><span></span></span>CPU only (if GPU is busy)</label></div>
      </div>
      <div class="row"><button class="btn pri big" id="go">Start clipping →</button><span class="muted small" id="gohint"></span></div>
    </div>
    <div class="col" style="gap:14px">
      <div class="card"><span class="lbl">Campaign rules</span><div id="rules" class="rules" style="margin-top:8px">Pick a campaign to see its rules here.</div></div>
      <div class="card small muted">
        <span class="lbl">How a run works</span>
        <ol style="padding-left:18px;margin-top:8px;line-height:1.9">
          <li>Downloads <b style="color:var(--tx)">audio only</b> + chat (a 4h stream is a few hundred MB)</li>
          <li>Transcribes the whole stream on your GPU</li>
          <li>Builds a hype curve from chat speed, chat words, loudness, speech and replays</li>
          <li>${S.sys && S.sys.llm.enabled ? "AI judge picks the best ones and writes hooks" : "Optional: turn on the AI judge in Settings"}</li>
          <li>You review, then it downloads <b style="color:var(--tx)">only those minutes</b> and renders</li>
        </ol></div>
    </div>
  </div>`;
  const links = $("#links"), meta = $("#linkmeta");
  const upd = () => {
    const ls = links.value.split("\n").map(s => s.trim()).filter(Boolean);
    meta.innerHTML = ls.map(l => `<span class="chip ${platformOf(l) === "Link" ? "warn" : "ac"}">${platformOf(l)}</span>`).join("") || '<span class="muted small">No links yet</span>';
    $("#go").textContent = ls.length > 1 ? `Start ${ls.length} runs →` : "Start clipping →";
    d.links = links.value;
  };
  links.oninput = upd; upd();
  const showRules = () => {
    const c = S.campaigns.find(x => x.id === $("#camp").value); d.camp = $("#camp").value;
    $("#rules").innerHTML = c ? `<b style="color:var(--tx)">${esc(c.name)}</b>${c.streamer ? " · " + esc(c.streamer) : ""}\nLength: ${c.min_clip_length || 15}–${c.max_clip_length || 60}s\nPlatforms: ${esc((c.allowed_upload_platforms || []).join(", ") || "any")}\nHashtags: ${esc((c.required_hashtags || []).join(" ") || "none")}\n${c.required_disclosure ? "Disclosure: " + esc(c.required_disclosure) + "\n" : ""}${c.rules_note ? "\n" + esc(c.rules_note) : ""}` : "Pick a campaign to see its rules here.";
  };
  $("#camp").onchange = showRules; showRules();
  $$("#count button, #sens button").forEach(b => b.onclick = () => { $$("button", b.parentNode).forEach(x => x.classList.remove("on")); b.classList.add("on"); d[b.parentNode.id === "count" ? "count" : "sens"] = b.dataset.v; });
  $("#preset").onchange = e => d.preset = e.target.value; $("#quality").onchange = e => d.quality = e.target.value;
  $("#review").onchange = e => d.review = e.target.checked; $("#cpu").onchange = e => d.cpu = e.target.checked;
  $("#go").onclick = async () => {
    const ls = links.value.split("\n").map(s => s.trim()).filter(Boolean);
    if (!ls.length) return toast("Paste at least one link", true);
    const count = $("#count .on").dataset.v;
    try {
      await api("/api/jobs", { method: "POST", body: { links: ls, campaign_id: $("#camp").value || null, quality: $("#quality").value, top: count === "Auto" ? null : +count, sensitivity: +$("#sens .on").dataset.v, review: $("#review").checked, preset: $("#preset").value || null, cpu: $("#cpu").checked } });
      d.links = ""; toast(`Queued ${ls.length} run${ls.length > 1 ? "s" : ""}`); location.hash = "#queue";
    } catch (e) { toast(e.message, true); }
  };
}

/* ---------------- QUEUE ---------------- */
function statusChip(j) {
  const m = { RUNNING: "ac", QUEUED: "", PAUSED: "warn", AWAITING_SELECTION: "hot", COMPLETED: "ok", FAILED: "bad", CANCELLED: "bad", INTERRUPTED: "warn" };
  const label = j.status === "AWAITING_SELECTION" ? "Needs review" : j.status.toLowerCase();
  return `<span class="chip ${m[j.status] || ""}">${label}</span>`;
}
function renderQueue() {
  const jobs = S.jobs;
  const html = `
  <div class="head"><div><h1>Queue</h1><p>Runs go one at a time. You can close this tab; work continues.</p></div><span class="sp"></span>
    <button class="btn ghost" id="clr">Clear finished</button><a class="btn pri" href="#new">＋ New run</a></div>
  ${jobs.length ? jobs.map(j => {
    const st = j.stages || {};
    const camp = j.campaign ? `<span class="chip">${esc(j.campaign.name || j.campaign.id)}</span>` : "";
    const act = [];
    if (j.status === "AWAITING_SELECTION") act.push(`<a class="btn pri sm" href="#review/${j.id}">Review ${(j.detected_moments || []).length} moments</a>`);
    if (j.status === "COMPLETED" && (j.clips || []).length) act.push(`<a class="btn pri sm" href="#clips/${j.id}">Open ${(j.clips || []).length} clips</a>`);
    if (["RUNNING", "QUEUED"].includes(j.status)) act.push(`<button class="btn sm" data-a="pause" data-id="${j.id}">Pause</button>`);
    if (["PAUSED", "INTERRUPTED", "FAILED"].includes(j.status)) act.push(`<button class="btn sm" data-a="resume" data-id="${j.id}">Resume</button>`);
    if (["RUNNING", "QUEUED", "PAUSED", "AWAITING_SELECTION"].includes(j.status)) act.push(`<button class="btn sm ghost danger" data-a="cancel" data-id="${j.id}">Cancel</button>`);
    else act.push(`<button class="btn sm ghost danger" data-a="delete" data-id="${j.id}">Delete</button>`);
    return `<div class="card job">
      <div><div class="row">${statusChip(j)}<span class="chip">${platformOf(j.vod_input)}</span>${camp}<span class="muted small mono">#${j.id} · ${esc(j.created_at || "")}</span></div>
        <div class="ttl" style="margin-top:8px" title="${esc(j.vod_input)}">${esc(j.vod_input)}</div></div>
      <div class="row">${act.join("")}</div>
      <div class="steps">${STAGES.map(([k, l]) => `<div class="step ${st[k] === "DONE" ? "done" : st[k] === "RUNNING" || (j.current_stage === k && j.status === "RUNNING") ? "run" : ""}">${l}</div>`).join("")}</div>
      <div class="detail">${j.status === "RUNNING" ? `<b style="color:var(--ac)">${j.progress || 0}%</b> · ` : ""}${esc(j.error || j.stage_details || j.status_message || "")}${j.eta_seconds && j.status === "RUNNING" ? ` · ~${fmt(j.eta_seconds)} left` : ""}</div>
    </div>`;
  }).join("") : `<div class="empty"><b>Nothing queued</b>Start a run and it will show up here.</div>`}`;
  main.innerHTML = html;
  $("#clr").onclick = async () => { await api("/api/jobs-clear-finished", { method: "POST" }); refresh(); };
  $$("[data-a]").forEach(b => b.onclick = async () => {
    const { a, id } = b.dataset;
    if (a === "delete" && !confirm("Delete this run and its clips?")) return;
    try { a === "delete" ? await api(`/api/jobs/${id}`, { method: "DELETE" }) : await api(`/api/jobs/${id}/do/${a}`, { method: "POST" }); refresh(); } catch (e) { toast(e.message, true); }
  });
}

/* ---------------- REVIEW ---------------- */
async function renderReview() {
  if (!S.arg) {
    const waiting = S.jobs.filter(j => j.status === "AWAITING_SELECTION");
    if (waiting.length === 1) { location.hash = "#review/" + waiting[0].id; return; }
    main.innerHTML = `<div class="head"><div><h1>Review</h1><p>Runs waiting for you to pick moments.</p></div></div>` +
      (waiting.length ? waiting.map(j => `<a class="card row" href="#review/${j.id}" style="margin-bottom:10px"><span class="chip hot">${(j.detected_moments || []).length} moments</span><span class="ttl">${esc(j.vod_input)}</span></a>`).join("") : `<div class="empty"><b>Nothing to review</b>When a run finds moments, they land here.</div>`);
    return;
  }
  const job = S.jobs.find(j => j.id === S.arg);
  if (!job) { main.innerHTML = `<div class="empty"><b>Run not found</b></div>`; return; }
  const moments = (job.detected_moments || []).map(m => ({ ...m }));
  const sel = new Set(job.selected_indices || moments.map((m, i) => i).filter(i => i < 8));
  const curve = await api(`/api/jobs/${job.id}/curve`).catch(() => ({ composite: [] }));
  const audio = new Audio(); let playing = -1;
  const score = m => (m.blended_score ?? m.score ?? 0);
  const maxScore = Math.max(1, ...moments.map(score));

  main.innerHTML = `
  <div class="head"><div><h1>Pick the moments</h1><p class="ttl">${esc(job.vod_input)}</p></div><span class="sp"></span>
    <span class="muted small"><span class="kbd">J</span>/<span class="kbd">K</span> move · <span class="kbd">X</span> select · <span class="kbd">P</span> play</span></div>
  <div class="curve" id="curve"><canvas></canvas></div>
  <div class="legend">${Object.entries(SIG).filter(([k]) => (curve.signals || {})[k]).map(([k, [l, c]]) => `<span><i style="background:${c}"></i>${l}</span>`).join("")}<span><i style="background:#fff"></i>Combined</span><span class="muted">Shaded = detected moments. Click one to jump.</span></div>
  <div class="moments" id="moms"></div>
  <div class="sticky"><b id="selcount"></b><span class="muted small">Selected moments get downloaded (just those minutes) and rendered.</span><span style="flex:1"></span>
    <button class="btn ghost" id="selall">Select all</button><button class="btn ghost" id="selnone">None</button><button class="btn pri big" id="render">Render selected →</button></div>`;

  const drawCurve = () => {
    const cv = $("#curve canvas"), box = $("#curve"); const dpr = devicePixelRatio || 1;
    cv.width = box.clientWidth * dpr; cv.height = box.clientHeight * dpr; const g = cv.getContext("2d"); g.scale(dpr, dpr);
    const W = box.clientWidth, H = box.clientHeight, comp = curve.composite || [], step = curve.step || 5;
    const dur = (comp.length * step) || Math.max(...moments.map(m => m.end), 60);
    const all = [comp, ...Object.values(curve.signals || {})].flat(); const mx = Math.max(3, ...all);
    const X = t => t / dur * W, Y = v => H - 8 - (Math.min(v, mx) / mx) * (H - 20);
    moments.forEach((m, i) => { const x0 = X(m.start), w = Math.max(8, X(m.end) - x0); g.fillStyle = sel.has(i) ? "#d7ff3a30" : "#ffffff10"; g.fillRect(x0 - (w - (X(m.end) - x0)) / 2, 0, w, H);
      if (S.focus === i) { g.strokeStyle = "#d7ff3a"; g.lineWidth = 1; g.strokeRect(x0 - (w - (X(m.end) - x0)) / 2 + .5, .5, w - 1, H - 1); }
      g.fillStyle = sel.has(i) ? "#d7ff3a" : "#666"; g.font = "bold 10px Inter, sans-serif"; g.fillText(i + 1, X(m.start) + 3, 12); });
    const line = (arr, color, w) => { if (!arr || !arr.length) return; g.beginPath(); arr.forEach((v, k) => { const x = X(k * step), y = Y(v); k ? g.lineTo(x, y) : g.moveTo(x, y); }); g.strokeStyle = color; g.lineWidth = w; g.stroke(); };
    Object.entries(curve.signals || {}).forEach(([k, arr]) => { g.globalAlpha = .35; line(arr, (SIG[k] || ["", "#888"])[1], 1); }); g.globalAlpha = 1;
    line(comp, "#ffffff", 1.6);
    if (!comp.length) { g.fillStyle = "#666"; g.font = "12px Inter"; g.fillText("Hype curve not available for this run (older run or no signals).", 12, H / 2); }
    box.onclick = e => { const t = (e.offsetX / W) * dur; const i = moments.findIndex(m => t >= m.start - 5 && t <= m.end + 5); if (i >= 0) { S.focus = i; drawMoms(); $$(".mom")[i].scrollIntoView({ block: "center", behavior: "smooth" }); } };
  };

  const drawMoms = () => {
    $("#moms").innerHTML = moments.map((m, i) => {
      const bd = m.breakdown || {}; const sc = score(m);
      return `<div class="mom ${sel.has(i) ? "sel" : ""} ${S.focus === i ? "focus" : ""}" data-i="${i}">
        <div class="num" data-tog="${i}">${i + 1}</div>
        <div style="min-width:0">
          <div class="row"><span class="tm">${fmt(m.start)} → ${fmt(m.end)}</span><span class="chip">${Math.round(m.end - m.start)}s</span>
            ${m.llm_score != null ? `<span class="chip ac">AI ${m.llm_score}/10</span>` : ""}
            <button class="btn sm" data-play="${i}">${playing === i ? "■ Stop" : "▶ Listen"}</button>
            <span class="nudge"><span class="muted small">start</span><button class="btn sm ghost" data-n="${i},start,-5">−5</button><button class="btn sm ghost" data-n="${i},start,5">+5</button>
            <span class="muted small" style="margin-left:6px">end</span><button class="btn sm ghost" data-n="${i},end,-5">−5</button><button class="btn sm ghost" data-n="${i},end,5">+5</button></span></div>
          <div class="pv">${esc(m.preview || "")}</div>
          ${m.llm_reason ? `<div class="why"><b>AI:</b> ${esc(m.llm_reason)}</div>` : ""}
          <div class="col" style="margin-top:10px"><span class="lbl">Hook title (top of clip, first 2.5s)</span>
            <input type="text" data-hook="${i}" value="${esc(m.hook_title || "")}" placeholder="Leave empty to auto-pick from the transcript" maxlength="60"></div>
        </div>
        <div class="sigs"><div class="row" style="justify-content:space-between;margin-bottom:8px"><span class="lbl">Why it was picked</span><span class="score" style="color:${sc / maxScore > .66 ? "var(--ac)" : "var(--tx)"}">${sc.toFixed(1)}</span></div>
          ${Object.entries(SIG).map(([k, [l, c]]) => bd[k] == null ? "" : `<div class="sig"><span>${l}</span><div class="t"><div style="width:${Math.min(100, bd[k] / 6 * 100)}%;background:${c}"></div></div><span class="v">${(+bd[k]).toFixed(1)}</span></div>`).join("")}
        </div></div>`;
    }).join("") || `<div class="empty"><b>No moments found</b>Try "Loose" sensitivity on a new run.</div>`;
    $("#selcount").textContent = `${sel.size} of ${moments.length} selected`;
    $("#render").disabled = !sel.size;
    $$("[data-tog]").forEach(b => b.onclick = () => toggle(+b.dataset.tog));
    $$("[data-play]").forEach(b => b.onclick = () => play(+b.dataset.play));
    $$("[data-hook]").forEach(inp => inp.oninput = () => moments[+inp.dataset.hook].hook_title = inp.value);
    $$("[data-n]").forEach(b => b.onclick = () => { const [i, k, d] = b.dataset.n.split(","); const m = moments[+i]; m[k] = Math.max(0, +m[k] + +d); if (m.end - m.start < 5) m[k] = k === "start" ? m.end - 5 : m.start + 5; drawMoms(); drawCurve(); });
    $$(".mom").forEach(el => el.onmousedown = e => { if (!e.target.closest("button,input")) { S.focus = +el.dataset.i; $$(".mom").forEach(x => x.classList.toggle("focus", x === el)); } });
  };
  const toggle = i => { sel.has(i) ? sel.delete(i) : sel.add(i); drawMoms(); drawCurve(); };
  const play = i => {
    const m = moments[i];
    if (playing === i) { audio.pause(); playing = -1; return drawMoms(); }
    if (!m.audio_path) return toast("No audio for this run", true);
    if (!audio.src.includes(encodeURIComponent(m.audio_path))) audio.src = media(m.audio_path);
    audio.currentTime = m.start; audio.play().catch(e => toast("Can't play audio: " + e.message, true)); playing = i; drawMoms();
  };
  audio.ontimeupdate = () => { if (playing >= 0 && audio.currentTime >= moments[playing].end) { audio.pause(); playing = -1; drawMoms(); } };
  $("#selall").onclick = () => { moments.forEach((m, i) => sel.add(i)); drawMoms(); drawCurve(); };
  $("#selnone").onclick = () => { sel.clear(); drawMoms(); drawCurve(); };
  $("#render").onclick = async () => {
    audio.pause();
    try { await api(`/api/jobs/${job.id}/confirm`, { method: "POST", body: { selected: [...sel].sort((a, b) => a - b), moments } }); toast(`Rendering ${sel.size} clips`); location.hash = "#queue"; } catch (e) { toast(e.message, true); }
  };
  S.keys = e => {
    if (e.target.matches("input,textarea,select")) return;
    if (e.key === "j") { S.focus = Math.min(moments.length - 1, S.focus + 1); drawMoms(); drawCurve(); $$(".mom")[S.focus]?.scrollIntoView({ block: "center" }); }
    if (e.key === "k") { S.focus = Math.max(0, S.focus - 1); drawMoms(); drawCurve(); $$(".mom")[S.focus]?.scrollIntoView({ block: "center" }); }
    if (e.key === "x") toggle(S.focus);
    if (e.key === "p") play(S.focus);
  };
  drawMoms(); drawCurve(); window.onresize = drawCurve;
}

/* ---------------- CLIPS ---------------- */
function clipJobs() { return S.jobs.filter(j => (j.clips || []).length); }
function renderClips() {
  const jobs = clipJobs();
  if (!S.arg && jobs.length) { location.hash = "#clips/" + jobs[0].id; return; }
  const job = jobs.find(j => j.id === S.arg);
  if (!job) { main.innerHTML = `<div class="head"><div><h1>Clips</h1></div></div><div class="empty"><b>No clips yet</b>Finished runs show their clips here.</div>`; return; }
  const kept = job.clips.filter(c => c.status === "KEPT").length;
  main.innerHTML = `
  <div class="head"><div><h1>Clips</h1><p class="ttl">${esc(job.vod_input)}</p></div><span class="sp"></span>
    <select id="jobpick" style="width:auto;max-width:280px">${jobs.map(j => `<option value="${j.id}" ${j.id === job.id ? "selected" : ""}>#${j.id} · ${j.clips.length} clips · ${esc((j.campaign || {}).name || platformOf(j.vod_input))}</option>`).join("")}</select>
    <div class="seg" id="filt">${["all", "pending", "kept", "rejected"].map(f => `<button data-f="${f}" class="${S.clipFilter === f ? "on" : ""}">${f}</button>`).join("")}</div>
    <button class="btn pri" id="export" ${kept ? "" : "disabled"}>Export ${kept} kept</button></div>
  <p class="muted small" style="margin:-8px 0 16px"><span class="kbd">J</span>/<span class="kbd">K</span> move · <span class="kbd">Space</span> play · <span class="kbd">Y</span> keep · <span class="kbd">N</span> reject · <span class="kbd">1</span><span class="kbd">2</span><span class="kbd">3</span> layout</p>
  <div class="clips" id="cliplist"></div>`;
  $("#jobpick").onchange = e => location.hash = "#clips/" + e.target.value;
  $$("#filt button").forEach(b => b.onclick = () => { S.clipFilter = b.dataset.f; renderClips(); });
  $("#export").onclick = async () => { try { const r = await api(`/api/jobs/${job.id}/export`, { method: "POST" }); toast(`Exported ${r.exported} clips + captions`); api("/api/open-folder", { method: "POST", body: { path: r.folder } }); } catch (e) { toast(e.message, true); } };
  drawClips(job);
}
function visibleClips(job) {
  return job.clips.map((c, i) => ({ c, i })).filter(({ c }) => { const s = (c.status || "PENDING").toLowerCase(); return S.clipFilter === "all" || s === S.clipFilter || (S.clipFilter === "rejected" && s === "rejected"); });
}
function drawClips(job) {
  const list = visibleClips(job);
  $("#cliplist").innerHTML = list.length ? list.map(({ c, i }, n) => {
    const qc = c.qc || {}; const qcCls = { PASS: "ok", WARN: "warn", FAIL: "bad" }[qc.status] || "";
    const p = c.platforms || {}; const lay = (c.layout || "gameplay").toLowerCase();
    return `<div class="clip ${c.status === "KEPT" ? "kept" : ""} ${c.status === "REJECTED" ? "rej" : ""} ${S.focus === n ? "focus" : ""}" data-i="${i}" data-n="${n}">
      <div class="phone"><video src="${media(c.path, c.rendered_at || "")}#t=0.1" controls preload="metadata" playsinline></video>
        <div class="ov">${qc.status ? `<span class="chip ${qcCls}" title="${esc((qc.flags || []).join("\n"))}">QC ${qc.status}</span>` : ""}${c.is_flagged ? `<span class="chip bad" title="${esc((c.flagged_words || []).join(", "))}">Risk words</span>` : ""}${qc.possible_music ? `<span class="chip warn">Music?</span>` : ""}</div>
        ${c.busy ? `<div class="busy"><div class="spin"></div>Re-rendering…</div>` : ""}</div>
      <div class="body">
        <div class="row" style="justify-content:space-between"><span class="mono small muted">${fmt(c.start_seconds || 0)} · ${Math.round(c.duration || 0)}s</span><span class="chip">${(+(c.hype_score || 0)).toFixed(1)} hype</span></div>
        ${(qc.flags || []).length ? `<div class="small" style="color:var(--warn)">${(qc.flags || []).map(esc).join("<br>")}</div>` : ""}
        ${c.error ? `<div class="small" style="color:var(--bad)">${esc(c.error)}</div>` : ""}
        <div class="col"><span class="lbl">Hook title</span><input type="text" data-hook value="${esc(c.hook_title || "")}" maxlength="60" placeholder="No hook"></div>
        <div class="row" style="justify-content:space-between"><div class="seg" data-lay>${[["split", "Split"], ["gameplay", "Gameplay"], ["face", "Face"]].map(([v, l]) => `<button data-v="${v}" class="${lay === v ? "on" : ""}">${l}</button>`).join("")}</div>
          <button class="btn sm" data-rr ${c.busy ? "disabled" : ""}>Re-render</button></div>
        <div class="acts"><button class="btn keep ${c.status === "KEPT" ? "on" : ""}" data-st="KEPT">✓ Keep</button><button class="btn rej ${c.status === "REJECTED" ? "on" : ""}" data-st="REJECTED">✕ Reject</button></div>
        <div class="col"><div class="tabs" data-tabs>${[["youtube", "Shorts"], ["tiktok", "TikTok"], ["reels", "Reels"], ["x", "X"]].map(([k, l], t) => `<button class="btn sm ${t ? "ghost" : ""}" data-tab="${k}">${l}</button>`).join("")}</div>
          <div class="capbox" data-cap>${esc(capText(p, "youtube"))}</div>
          <button class="btn sm" data-copy>Copy caption</button></div>
      </div></div>`;
  }).join("") : `<div class="empty"><b>No clips in this filter</b></div>`;

  $$(".clip").forEach(el => {
    const i = +el.dataset.i, c = job.clips[i]; let tab = "youtube";
    $$("[data-lay] button", el).forEach(b => b.onclick = () => { $$("[data-lay] button", el).forEach(x => x.classList.toggle("on", x === b)); });
    $("[data-rr]", el).onclick = () => rerender(job, i, el);
    $$("[data-st]", el).forEach(b => b.onclick = () => setStatus(job, i, c.status === b.dataset.st ? "PENDING" : b.dataset.st));
    $$("[data-tab]", el).forEach(b => b.onclick = () => { tab = b.dataset.tab; $$("[data-tab]", el).forEach(x => x.classList.toggle("ghost", x !== b)); $("[data-cap]", el).textContent = capText(c.platforms || {}, tab); });
    $("[data-copy]", el).onclick = async () => { await navigator.clipboard.writeText($("[data-cap]", el).textContent); toast("Caption copied"); };
    el.onmousedown = e => { if (!e.target.closest("button,input,video")) { S.focus = +el.dataset.n; $$(".clip").forEach(x => x.classList.toggle("focus", x === el)); } };
  });
  S.keys = e => {
    if (e.target.matches("input,textarea,select")) return;
    const els = $$(".clip"); if (!els.length) return; const el = els[S.focus] || els[0];
    if (e.key === "j" || e.key === "k") { S.focus = Math.max(0, Math.min(els.length - 1, S.focus + (e.key === "j" ? 1 : -1))); els.forEach((x, n) => x.classList.toggle("focus", n === S.focus)); els[S.focus].scrollIntoView({ block: "center", behavior: "smooth" }); }
    if (e.key === " ") { e.preventDefault(); const v = $("video", el); v.paused ? v.play() : v.pause(); }
    if (e.key === "y") $('[data-st="KEPT"]', el).click();
    if (e.key === "n") $('[data-st="REJECTED"]', el).click();
    if ("123".includes(e.key)) { $$("[data-lay] button", el)[+e.key - 1].click(); }
  };
}
function capText(p, tab) {
  if (tab === "youtube") return [p.youtube_title, p.youtube_description].filter(Boolean).join("\n\n");
  return { tiktok: p.tiktok_caption, reels: p.reels_caption, x: p.x_tweet }[tab] || "";
}
async function setStatus(job, i, st) {
  try { await api(`/api/jobs/${job.id}/clips/${i}/status`, { method: "POST", body: { status: st } }); job.clips[i].status = st; renderClipsKeepScroll(); refresh(); } catch (e) { toast(e.message, true); }
}
async function rerender(job, i, el) {
  const layout = $("[data-lay] .on", el).dataset.v, hook = $("[data-hook]", el).value;
  try { await api(`/api/jobs/${job.id}/clips/${i}/rerender`, { method: "POST", body: { layout, hook_title: hook } }); job.clips[i].busy = true; renderClipsKeepScroll(); toast("Re-rendering in the background"); } catch (e) { toast(e.message, true); }
}
function renderClipsKeepScroll() { const y = main.scrollTop; renderClips(); main.scrollTop = y; }
let lastClipSig = "";
function updateClipsLive() {
  const job = S.jobs.find(j => j.id === S.arg); if (!job) return;
  const sig = job.clips.map(c => `${c.busy}|${c.rendered_at}|${c.status}`).join(",");
  if (lastClipSig && sig !== lastClipSig && !document.activeElement.matches("input")) renderClipsKeepScroll();
  lastClipSig = sig;
}

/* ---------------- LIVE ---------------- */
async function renderLive() {
  const [st, camps] = await Promise.all([api("/api/live"), api("/api/campaigns").catch(() => [])]);
  $("#b-live").textContent = st.sessions.filter(s => s.running).length || "";
  main.innerHTML = `
  <div class="head"><div><h1>Live</h1><p>Records a live stream and clips it while it's happening. A clip lands here a few minutes after the moment.</p></div></div>
  <div class="card row" style="margin-bottom:18px">
    <input type="text" id="lurl" placeholder="https://www.twitch.tv/streamer" style="flex:2;min-width:260px">
    <select id="lcamp" style="flex:1;min-width:160px"><option value="">No campaign</option>${camps.map(c => `<option value="${esc(c.id)}">${esc(c.name || c.id)}</option>`).join("")}</select>
    <button class="btn pri" id="lgo">● Start watching</button></div>
  ${st.sessions.map(s => `<div class="card row" style="margin-bottom:10px">${s.running ? '<span class="chip hot">● LIVE</span>' : '<span class="chip">stopped</span>'}<b class="ttl">${esc(s.url)}</b><span class="muted small">since ${s.started}</span><span style="flex:1"></span><button class="btn sm danger" data-stop="${s.id}">Stop</button></div>`).join("")}
  <div class="clips" style="margin-top:18px">${st.clips.length ? st.clips.map(c => `<div class="clip"><div class="phone"><video src="${media(c.path)}#t=0.1" controls preload="metadata"></video></div>
    <div class="body"><b>${esc(c.hook_title || "")}</b><span class="muted small mono">${esc(c.timestamp || "")} · ${Math.round(c.duration || 0)}s · ${esc(c.layout || "")}</span>
    <div class="capbox">${esc(capText(c.platforms || {}, "tiktok"))}</div></div></div>`).join("") : `<div class="empty" style="grid-column:1/-1"><b>No live clips yet</b>Start watching a stream above. Works best on Twitch (it reads chat too).</div>`}</div>`;
  $("#lgo").onclick = async () => { const url = $("#lurl").value.trim(); if (!url) return toast("Paste a stream link", true); try { await api("/api/live", { method: "POST", body: { url, campaign_id: $("#lcamp").value || null } }); toast("Watching " + url); renderLive(); } catch (e) { toast(e.message, true); } };
  $$("[data-stop]").forEach(b => b.onclick = async () => { await api("/api/live/" + b.dataset.stop, { method: "DELETE" }); renderLive(); });
}

/* ---------------- CAMPAIGNS ---------------- */
async function renderCampaigns() {
  S.campaigns = await api("/api/campaigns").catch(() => []);
  const cur = S.campaigns.find(c => c.id === S.arg) || (S.arg === "new" ? {} : S.campaigns[0] || {});
  const list = v => (v || []).join(", ");
  const ov = cur.overlay || {};
  main.innerHTML = `
  <div class="head"><div><h1>Campaigns</h1><p>Each campaign's rules get applied to every clip automatically.</p></div><span class="sp"></span><a class="btn pri" href="#campaigns/new">＋ New campaign</a></div>
  <div class="grid2" style="grid-template-columns:280px 1fr">
    <div class="camp-list">${S.campaigns.map(c => `<a href="#campaigns/${c.id}" class="${c.id === cur.id ? "on" : ""}"><b>${esc(c.name || c.id)}</b><small>${esc(c.streamer || "")} ${c.platform ? "· " + esc(c.platform) : ""}</small></a>`).join("") || '<div class="empty">No campaigns yet</div>'}</div>
    <div class="card"><div class="formgrid">
      <div class="col"><span class="lbl">Campaign name</span><input type="text" id="c-name" value="${esc(cur.name)}" placeholder="e.g. Streamer GTA 6 clipping"></div>
      <div class="col"><span class="lbl">Streamer</span><input type="text" id="c-streamer" value="${esc(cur.streamer)}"></div>
      <div class="col"><span class="lbl">Platform</span><select id="c-platform">${["twitch", "youtube", "kick"].map(p => `<option ${cur.platform === p ? "selected" : ""}>${p}</option>`).join("")}</select></div>
      <div class="col"><span class="lbl">Caption style</span><select id="c-preset">${((S.sys || {}).presets || ["classic_yellow"]).map(p => `<option ${cur.preset === p ? "selected" : ""}>${p}</option>`).join("")}</select></div>
      <div class="col"><span class="lbl">Min length (s)</span><input type="number" id="c-min" value="${cur.min_clip_length ?? 15}"></div>
      <div class="col"><span class="lbl">Max length (s)</span><input type="number" id="c-max" value="${cur.max_clip_length ?? 60}"></div>
      <div class="col"><span class="lbl">Required hashtags</span><input type="text" id="c-tags" value="${esc(list(cur.required_hashtags))}" placeholder="#ad, #streamer"></div>
      <div class="col"><span class="lbl">Allowed platforms</span><input type="text" id="c-plats" value="${esc(list(cur.allowed_upload_platforms))}" placeholder="tiktok, youtube, instagram, x"></div>
      <div class="col" style="grid-column:1/-1"><span class="lbl">Required disclosure</span><input type="text" id="c-disc" value="${esc(cur.required_disclosure)}"></div>
      <div class="col" style="grid-column:1/-1"><span class="lbl">Rules from the campaign brief</span><textarea id="c-rules" style="font-family:inherit;min-height:90px">${esc(cur.rules_note)}</textarea></div>
      <div class="col" style="grid-column:1/-1"><span class="lbl">Campaign overlay image (PNG path, optional)</span><input type="text" id="c-ov" value="${esc(ov.image_path)}" placeholder="C:\\path\\to\\campaign_sticker.png"></div>
      <div class="col"><span class="lbl">Overlay X</span><input type="text" id="c-ox" value="${esc(ov.x ?? "(W-w)/2")}"></div>
      <div class="col"><span class="lbl">Overlay Y / width</span><div class="row"><input type="text" id="c-oy" value="${esc(ov.y ?? 120)}" style="flex:1"><input type="text" id="c-ow" value="${esc(ov.width ?? 300)}" style="flex:1"></div></div>
    </div><hr><div class="row"><button class="btn pri" id="c-save">Save campaign</button>${cur.id ? `<button class="btn danger" id="c-del">Delete</button>` : ""}</div></div>
  </div>`;
  const csv = id => $(id).value.split(",").map(s => s.trim()).filter(Boolean);
  $("#c-save").onclick = async () => {
    if (!$("#c-name").value.trim()) return toast("Give it a name", true);
    const body = { id: cur.id || null, name: $("#c-name").value.trim(), streamer: $("#c-streamer").value.trim(), platform: $("#c-platform").value, preset: $("#c-preset").value,
      min_clip_length: +$("#c-min").value || 15, max_clip_length: +$("#c-max").value || 60, required_hashtags: csv("#c-tags"), allowed_upload_platforms: csv("#c-plats"),
      required_disclosure: $("#c-disc").value.trim(), rules_note: $("#c-rules").value, risk_words: cur.risk_words,
      overlay: $("#c-ov").value.trim() ? { image_path: $("#c-ov").value.trim(), x: $("#c-ox").value, y: $("#c-oy").value, width: +$("#c-ow").value || -1 } : null };
    try { const r = await api("/api/campaigns", { method: "POST", body }); toast("Saved"); location.hash = "#campaigns/" + r.id; renderCampaigns(); } catch (e) { toast(e.message, true); }
  };
  if ($("#c-del")) $("#c-del").onclick = async () => { if (!confirm("Delete this campaign?")) return; await api("/api/campaigns/" + cur.id, { method: "DELETE" }); location.hash = "#campaigns"; };
}

/* ---------------- SETTINGS ---------------- */
async function renderSettings() {
  const s = S.sys = await api("/api/system");
  const models = s.ollama.models || [];
  main.innerHTML = `
  <div class="head"><div><h1>Settings</h1><p>Saved to config/user_overrides.yaml. Your main config file is never rewritten.</p></div></div>
  <div class="grid2">
    <div class="card col" style="gap:16px">
      <div class="row" style="justify-content:space-between"><div><b>AI judge</b><div class="muted small">A local model (Ollama) scores moments harshly and writes hook titles. Free, runs on your GPU after Whisper unloads.</div></div>
        <label class="switch"><input type="checkbox" id="s-llm" ${s.llm.enabled ? "checked" : ""}><span></span></label></div>
      <div class="col"><span class="lbl">Model</span>
        ${models.length ? `<select id="s-model">${models.map(m => `<option ${m === s.llm.model ? "selected" : ""}>${esc(m)}</option>`).join("")}</select>` : `<input type="text" id="s-model" value="${esc(s.llm.model)}">`}
        <span class="small ${s.ollama.running ? "" : "muted"}" style="${s.ollama.running ? "color:var(--ok)" : ""}">${s.ollama.running ? `Ollama running · ${models.length} model(s)` : "Ollama isn't running. Install it, then run: ollama pull " + esc(s.llm.model)}</span></div>
      <hr>
      <div class="col"><span class="lbl">Whisper model</span><select id="s-whisper">${["large-v3", "large-v3-turbo", "distil-large-v3", "medium"].map(m => `<option ${m === s.whisper_model ? "selected" : ""}>${m}</option>`).join("")}</select>
        <span class="muted small">large-v3 = most accurate. large-v3-turbo = much faster, nearly as good. Good pick for launch day.</span></div>
    </div>
    <div class="card col" style="gap:16px">
      <div class="row" style="justify-content:space-between"><div><b>Progress bar</b><div class="muted small">Thin bar along the bottom that fills as the clip plays. Keeps people watching to the end.</div></div>
        <label class="switch"><input type="checkbox" id="s-bar" ${s.polish.progress_bar ? "checked" : ""}><span></span></label></div>
      <div class="col"><span class="lbl">Gameplay layout crop</span><div class="seg acc" id="s-asp">${[["16:9", "Full frame"], ["4:3", "Slight zoom"], ["1:1", "Big zoom"]].map(([v, l]) => `<button data-v="${v}" class="${s.gameplay_aspect === v ? "on" : ""}">${l}</button>`).join("")}</div>
        <span class="muted small">How much of the 16:9 game frame to keep in the gameplay layout. More zoom = bigger on a phone.</span></div>
      <hr><button class="btn pri" id="s-save">Save settings</button>
    </div>
  </div>`;
  $$("#s-asp button").forEach(b => b.onclick = () => { $$("#s-asp button").forEach(x => x.classList.toggle("on", x === b)); });
  $("#s-save").onclick = async () => {
    try { await api("/api/settings", { method: "POST", body: { llm_enabled: $("#s-llm").checked, llm_model: $("#s-model").value, whisper_model: $("#s-whisper").value, progress_bar: $("#s-bar").checked, gameplay_aspect: $("#s-asp .on").dataset.v } }); toast("Settings saved"); refreshSys(); } catch (e) { toast(e.message, true); }
  };
}

/* ---------------- boot ---------------- */
document.addEventListener("keydown", e => S.keys && ["review", "clips"].includes(S.route) && S.keys(e));
(async () => { await Promise.all([refresh(), refreshSys()]); route(); setInterval(refresh, 2000); setInterval(refreshSys, 10000); })();
