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
  if (highlight) {
    const el = document.getElementById(`card-${highlight}`);
    if (el) { el.classList.add("highlight"); el.scrollIntoView({ behavior: "smooth", block: "center" }); }
  }
}

function card(c) {
  const el = document.createElement("a");
  el.className = "card card-link";
  el.id = `card-${c.graph}`;
  el.href = `/code/${encodeURIComponent(c.graph)}`;
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
    <p class="card-meta">${esc(c.edition) || "&nbsp;"}</p>
    <dl class="stats">
      <div><dt>Pages</dt><dd>${fmtInt(c.pages)}</dd></div>
      <div><dt>Clauses</dt><dd>${fmtInt(n.clauses)}</dd></div>
      <div><dt>Tables</dt><dd>${fmtInt(n.tables)}</dd></div>
      <div><dt>Equations</dt><dd>${fmtInt(n.equations)}</dd></div>
      <div><dt title="High and medium severity items not yet approved">To review</dt><dd class="${n.to_review ? "warnv" : ""}">${fmtInt(n.to_review)}</dd></div>
    </dl>
    ${c.status.state === "partial" ? `<div class="bar" title="Pass 2 progress"><i style="width:${(c.status.pass2_share * 100).toFixed(1)}%"></i></div>` : ""}`;
  return el;
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
      location.href = `/ingest.html?job=${encodeURIComponent(data.job.id)}`;
    }
  } catch (err) {
    showNotice(esc(err.message), "bad");
  }
}

function initDrop() {
  const zone = $("#dropzone");
  $("#file").addEventListener("change", (e) => upload(e.target.files[0]));
  for (const ev of ["dragenter", "dragover"]) zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) zone.addEventListener(ev, () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); upload(e.dataTransfer.files[0]); });
}

document.addEventListener("DOMContentLoaded", () => {
  initDrop();
  loadLibrary(new URLSearchParams(location.search).get("open"));
});
