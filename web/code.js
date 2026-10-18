// Code page (/code/{graph}): the code's header and counts, its actions, and every chat on it.
"use strict";

const graph = decodeURIComponent(location.pathname.replace(/^\/code\//, "").replace(/\/$/, ""));
const enc = encodeURIComponent(graph);
const STAGE_TEXT = {
  queued: "Queued", profiling: "Building profile", profile_ready: "Waiting for your confirmation",
  pass1: "Pass 1 running", pass2: "Pass 2 running", done: "Done", failed: "Failed",
};

function when(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  return `${d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}, `
    + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

async function loadHead() {
  let c;
  try { c = await getJSON(`/codes/${enc}`); }
  catch (err) { $("#code-head").innerHTML = `<p class="src-error">${esc(err.message)}</p>`; return; }
  document.title = `${(c.designation || c.title).slice(0, 40)} · CiteWise`;
  const n = c.counts;
  const status = c.job ? { state: "running", text: STAGE_TEXT[c.job.stage] || c.job.stage } : c.status;
  $("#code-head").innerHTML = `
    <div class="page-head code-head">
      <div>
        <span class="status st-${esc(status.state)}">${esc(status.text)}</span>
        <h1>${esc(c.title)}</h1>
        <p class="lede">${[c.edition, c.jurisdiction].filter(Boolean).map(esc).join(" · ")} · <span class="graph-id">${esc(c.graph)}</span></p>
      </div>
    </div>
    <div class="panel">
      <dl class="stats big">
        <div><dt>Pages</dt><dd>${fmtInt(c.pages)}</dd></div>
        <div><dt>Clauses</dt><dd>${fmtInt(n.clauses)}</dd></div>
        <div><dt>Tables</dt><dd>${fmtInt(n.tables)}</dd></div>
        <div><dt>Equations</dt><dd>${fmtInt(n.equations)}</dd></div>
        <div><dt title="High and medium severity items not yet approved">To review</dt><dd class="${n.to_review ? "warnv" : ""}">${fmtInt(n.to_review)}</dd></div>
        <div><dt>Notes (low)</dt><dd>${fmtInt(n.notes)}</dd></div>
        <div><dt>Symbol warnings</dt><dd>${fmtInt(n.symbol_warnings)}</dd></div>
        <div><dt>Coverage</dt><dd>${fmtPct(c.coverage)}</dd></div>
      </dl>
      <div class="card-actions">
        <a class="btn primary" href="/chat.html?graph=${enc}&new=1">New chat</a>
        <a class="btn" href="/report.html?graph=${enc}">View report</a>
        <a class="btn" href="/review.html?graph=${enc}">Review <span class="count">${fmtInt(n.to_review)}</span></a>
        ${c.job ? `<a class="btn" href="/ingest.html?job=${encodeURIComponent(c.job.id)}">Ingestion progress</a>`
                : c.has_pdf ? `<button class="btn ghost" type="button" id="reingest">Re-ingest</button>` : ""}
      </div>
    </div>`;
  const re = $("#reingest");
  if (re) re.addEventListener("click", reingest);
}

async function reingest() {
  if (!confirm(`Re-ingest ${graph}? You will see its profile first; confirming there replaces the graph.`)) return;
  try {
    const data = await getJSON(`/codes/${enc}/reingest`, { method: "POST" });
    location.href = `/ingest.html?job=${encodeURIComponent(data.job.id)}`;
  } catch (err) { notice(esc(err.message), "bad"); }
}

function notice(html, kind = "info") {
  const n = $("#notice");
  n.hidden = false;
  n.className = `notice ${kind}`;
  n.innerHTML = html;
}

async function loadChats() {
  const box = $("#chats");
  let data;
  try { data = await getJSON(`/codes/${enc}/sessions`); }
  catch (err) { box.innerHTML = `<p class="src-error">${esc(err.message)}</p>`; return; }
  const sessions = data.sessions;
  $("#chat-count").textContent = sessions.length ? `${sessions.length} ${sessions.length === 1 ? "chat" : "chats"}, newest first` : "";
  if (!sessions.length) {
    box.innerHTML = `<p class="hint">No chats on this code yet. <a href="/chat.html?graph=${enc}&new=1">Start one</a>.</p>`;
    return;
  }
  box.innerHTML = sessions.map((s) => `
    <div class="chat-row" data-id="${esc(s.id)}">
      <a class="chat-open" href="/chat.html?graph=${enc}&session=${encodeURIComponent(s.id)}">
        <span class="chat-title">${esc(s.first_question || "(no question)")}</span>
        <span class="chat-meta">${esc(when(s.last))} · ${s.questions} ${s.questions === 1 ? "question" : "questions"}</span>
      </a>
      <button type="button" class="icon-btn chat-del" data-delete="${esc(s.id)}" aria-label="Delete this chat" title="Delete this chat">
        <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M5 7h14M10 7V5h4v2m-7 0l1 12h8l1-12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
    </div>`).join("");
}

async function deleteChat(id, button) {
  const row = button.closest(".chat-row");
  const title = row.querySelector(".chat-title").textContent;
  if (!confirm(`Delete this chat?\n\n"${title.slice(0, 120)}"\n\nIts messages, questions and facts are removed from the memory graph. This cannot be undone.`)) return;
  button.disabled = true;
  try {
    await getJSON(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    row.remove();
    const left = document.querySelectorAll(".chat-row").length;
    $("#chat-count").textContent = left ? `${left} ${left === 1 ? "chat" : "chats"}, newest first` : "";
    if (!left) loadChats();
  } catch (err) {
    button.disabled = false;
    notice(`Could not delete the chat: ${esc(err.message)}`, "bad");
  }
}

document.addEventListener("click", (e) => {
  const del = e.target.closest("[data-delete]");
  if (del) deleteChat(del.dataset.delete, del);
});
document.addEventListener("DOMContentLoaded", () => { loadHead(); loadChats(); });
