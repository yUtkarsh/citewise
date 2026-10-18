// Library page: one card per code graph, plus the upload drop zone.
"use strict";

const STAGE_TEXT = {
  queued: "Queued", profiling: "Building profile", profile_ready: "Waiting for your confirmation",
  pass1: "Pass 1 running", pass2: "Pass 2 running", done: "Done", failed: "Failed",
};

async function loadLibrary(highlight) {
  let data;
  try { data = await getJSON("/codes"); }
  catch (err) { $("#cards").innerHTML = `<p class="src-error">Could not load the library: ${esc(err.message)}</p>`; return; }
  $("#max-cost").textContent = fmtUsd(data.max_cost);
  renderJobs(data.jobs || []);
  const cards = $("#cards");
  cards.innerHTML = data.codes.length ? "" : `<p class="lede">No codes yet. Add a PDF above.</p>`;
  for (const code of data.codes) cards.append(card(code));
  data.codes.filter((c) => !c.error).forEach((c) => loadRecent(c.graph));
  if (highlight) {
    const el = document.getElementById(`card-${highlight}`);
    if (el) { el.classList.add("highlight"); el.scrollIntoView({ behavior: "smooth", block: "center" }); }
  }
}

function card(c) {
  const el = document.createElement("article");
  el.className = "card";
  el.id = `card-${c.graph}`;
  if (c.error) {
    el.innerHTML = `<h3>${esc(c.title)}</h3><p class="src-error">${esc(c.error)}</p>`;
    return el;
  }
  const n = c.counts;
  const status = c.job ? { state: "running", text: STAGE_TEXT[c.job.stage] || c.job.stage } : c.status;
  el.innerHTML = `
    <div class="card-top">
      <span class="status st-${esc(status.state)}">${esc(status.text)}</span>
      <span class="graph-id">${esc(c.graph)}</span>
    </div>
    <h3>${esc(c.title)}</h3>
    <p class="card-meta">${[c.edition, c.jurisdiction].filter(Boolean).map(esc).join(" · ") || "&nbsp;"}</p>
    <dl class="stats">
      <div><dt>Pages</dt><dd>${fmtInt(c.pages)}</dd></div>
      <div><dt>Clauses</dt><dd>${fmtInt(n.clauses)}</dd></div>
      <div><dt>Tables</dt><dd>${fmtInt(n.tables)}</dd></div>
      <div><dt>Equations</dt><dd>${fmtInt(n.equations)}</dd></div>
      <div><dt>Flagged</dt><dd class="${n.flagged ? "warnv" : ""}">${fmtInt(n.flagged)}</dd></div>
      <div><dt title="Symbol warnings">Sym. warnings</dt><dd class="${n.symbol_warnings ? "warnv" : ""}">${fmtInt(n.symbol_warnings)}</dd></div>
    </dl>
    ${c.status.state === "partial" ? `<div class="bar" title="Pass 2 progress"><i style="width:${(c.status.pass2_share * 100).toFixed(1)}%"></i></div>` : ""}
    <div class="recent" data-recent="${esc(c.graph)}"></div>
    <div class="card-actions">
      <a class="btn primary" href="chat.html?graph=${encodeURIComponent(c.graph)}">Open chat</a>
      <a class="btn" href="report.html?graph=${encodeURIComponent(c.graph)}">View report</a>
      ${c.job ? `<a class="btn" href="ingest.html?job=${encodeURIComponent(c.job.id)}">Progress</a>`
              : c.has_pdf ? `<button class="btn ghost" type="button" data-reingest="${esc(c.graph)}">Re-ingest</button>` : ""}
    </div>`;
  return el;
}

async function loadRecent(graph) {
  const box = document.querySelector(`[data-recent="${CSS.escape(graph)}"]`);
  if (!box) return;
  let data;
  try { data = await getJSON(`/codes/${encodeURIComponent(graph)}/sessions?limit=3`); } catch { return; }
  if (!data.sessions.length) return;
  box.innerHTML = `<div class="recent-h">Recent chats</div>` + data.sessions.map((s) => `
    <a class="recent-row" href="chat.html?graph=${encodeURIComponent(graph)}&session=${encodeURIComponent(s.id)}">
      <span class="recent-q">${esc(s.first_question || s.id)}</span>
      <span class="recent-meta">${s.questions} ${s.questions === 1 ? "question" : "questions"} · ${esc(ago(s.last))}</span>
    </a>`).join("");
}

function ago(t) {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString();
}

function renderJobs(jobs) {
  $("#jobs").hidden = !jobs.length;
  $("#job-list").innerHTML = jobs.map((j) => `
    <a class="job-row" href="ingest.html?job=${encodeURIComponent(j.id)}">
      <span class="status st-running">${esc(STAGE_TEXT[j.stage] || j.stage)}</span>
      <span class="job-file">${esc(j.file)}</span>
      <span class="graph-id">${esc(j.graph)}</span>
    </a>`).join("");
}

function showNotice(html, kind = "info") {
  const n = $("#notice");
  n.hidden = false;
  n.className = `notice ${kind}`;
  n.innerHTML = html;
}

async function upload(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
    showNotice("Only PDF files can be added.", "bad");
    return;
  }
  showNotice(`Uploading <strong>${esc(file.name)}</strong> (${(file.size / 1e6).toFixed(1)} MB) and checking it…`);
  try {
    const data = await getJSON(`/codes?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", headers: { "Content-Type": "application/pdf" }, body: file,
    });
    if (data.status === "exists") {
      showNotice(`${esc(data.message)} Opening its card.`);
      await loadLibrary(data.graph);
    } else {
      location.href = `ingest.html?job=${encodeURIComponent(data.job.id)}`;
    }
  } catch (err) {
    showNotice(esc(err.message), "bad");
  }
}

async function reingest(graph) {
  if (!confirm(`Re-ingest ${graph}? You will see its profile first; confirming there replaces the graph.`)) return;
  try {
    const data = await getJSON(`/codes/${encodeURIComponent(graph)}/reingest`, { method: "POST" });
    location.href = `ingest.html?job=${encodeURIComponent(data.job.id)}`;
  } catch (err) { showNotice(esc(err.message), "bad"); }
}

function initDrop() {
  const zone = $("#dropzone");
  $("#file").addEventListener("change", (e) => upload(e.target.files[0]));
  for (const ev of ["dragenter", "dragover"]) zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) zone.addEventListener(ev, () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); upload(e.dataTransfer.files[0]); });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-reingest]");
    if (b) reingest(b.dataset.reingest);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initDrop();
  loadLibrary(new URLSearchParams(location.search).get("open"));
});
