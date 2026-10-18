// Review queue: flagged items by severity then page; approve, or reject with a note
// (re-runs the job on MODEL_REVIEW and shows before and after).
"use strict";

const graph = new URLSearchParams(location.search).get("graph");
const PAGE = 60;
// Low-severity items are notes (they affect explanations, not numbers): hidden unless "Show notes" is on.
const isOpen = (i) => !["approved", "needs_manual"].includes(i.review_status);
const isNote = (i) => i.severity === "low";
const TABS = [
  ["open", "To review", (i) => isOpen(i) && (state.notes || !isNote(i))],
  ["high", "High", (i) => isOpen(i) && i.severity === "high"],
  ["medium", "Medium", (i) => isOpen(i) && i.severity === "medium"],
  ["needs_manual", "Needs manual", (i) => i.review_status === "needs_manual"],
  ["approved", "Approved", (i) => i.review_status === "approved"],
];
const state = { items: [], tab: "open", shown: PAGE, current: null, busy: false,
                notes: storage(() => localStorage.getItem("review_notes") === "1", false) };

function tex(latex, display = true) {
  if (!latex) return "";
  if (!window.katex) return `<code>${esc(latex)}</code>`;
  try { return katex.renderToString(latex, { throwOnError: false, displayMode: display }); }
  catch { return `<code>${esc(latex)}</code>`; }
}
const key = (i) => `${i.label}|${i.id}`;

async function load(select) {
  if (!graph) { $("#queue").innerHTML = `<p class="src-error">No graph in the address.</p>`; return; }
  let data;
  try { data = await getJSON(`/review/${encodeURIComponent(graph)}`); }
  catch (err) { $("#queue").innerHTML = `<p class="src-error">${esc(err.message)}</p>`; return; }
  state.items = data.items;
  $("#doc-line").innerHTML = `${esc(data.designation || data.title)} · <span class="graph-id">${esc(graph)}</span>`;
  $("#head-links").innerHTML = `<a class="btn" href="report.html?graph=${encodeURIComponent(graph)}">Report</a>`
    + `<a class="btn primary" href="chat.html?graph=${encodeURIComponent(graph)}">Open chat</a>`;
  renderTabs();
  renderQueue();
  const want = select || location.hash.slice(1);
  if (want) { const [label, ...rest] = decodeURIComponent(want).split("|"); openItem(label, rest.join("|")); }
}

function renderTabs() {
  $("#tabs").innerHTML = TABS.map(([id, name, test]) => {
    const n = state.items.filter(test).length;
    return `<button type="button" role="tab" class="tab ${state.tab === id ? "on" : ""}" data-tab="${id}" aria-selected="${state.tab === id}">${name} <span>${fmtInt(n)}</span></button>`;
  }).join("") + `<label class="notes-toggle"><input type="checkbox" id="notes-toggle" ${state.notes ? "checked" : ""}>`
    + ` Show notes <span>${fmtInt(state.items.filter((i) => isOpen(i) && isNote(i)).length)}</span></label>`;
}

function renderQueue() {
  const test = TABS.find(([id]) => id === state.tab)[2];
  const list = state.items.filter(test);
  const box = $("#queue");
  if (!list.length) { box.innerHTML = `<p class="hint">Nothing here.</p>`; return; }
  box.innerHTML = list.slice(0, state.shown).map((i) => `
    <button type="button" class="q-row ${state.current === key(i) ? "on" : ""}" data-key="${esc(key(i))}">
      <span class="sev sev-${esc(i.severity)}">${esc(i.severity)}</span>
      <span class="q-main"><span class="q-title">${esc(i.title)}</span>
        <span class="q-reasons">${i.reasons.map(esc).join(" · ")}</span></span>
      <span class="q-page">p.${esc(i.page ?? "–")}${i.review_status !== "unreviewed" ? `<br><span class="q-rs rs-${esc(i.review_status)}">${esc(i.review_status.replace("_", " "))}</span>` : ""}</span>
    </button>`).join("")
    + (list.length > state.shown ? `<button type="button" class="btn ghost more" id="more">Show ${Math.min(PAGE, list.length - state.shown)} more of ${fmtInt(list.length - state.shown)}</button>` : "");
}

async function openItem(label, id) {
  state.current = `${label}|${id}`;
  history.replaceState(null, "", `#${encodeURIComponent(state.current)}`);
  renderQueue();
  const box = $("#item");
  box.innerHTML = `<div class="skeleton"></div><div class="skeleton" style="width:60%"></div>`;
  try {
    const d = await getJSON(`/review/${encodeURIComponent(graph)}/item/${encodeURIComponent(label)}/${encodeURIComponent(id)}`);
    if (state.current !== `${label}|${id}`) return;
    box.innerHTML = itemHtml(d);
    wireActions(d);
  } catch (err) { box.innerHTML = `<p class="src-error">${esc(err.message)}</p>`; }
}

function itemHtml(d) {
  const sev = d.flags.reduce((s, f) => ({ high: 3, medium: 2, low: 1 }[f.severity] > ({ high: 3, medium: 2, low: 1 }[s] || 0) ? f.severity : s), "low");
  return `
    <div class="item-head">
      <div><span class="kind">${esc(d.label)}</span><h2>${esc(d.title)}</h2></div>
      <div class="pills">
        <span class="sev sev-${sev}">${sev}</span>
        <span class="pill st-${esc(d.status)}">${esc(d.status)}</span>
        <span class="pill st-${esc(d.review_status || "unreviewed")}">${esc(d.review_status || "unreviewed")}</span>
        ${d.rejections ? `<span class="pill">rejected ${d.rejections}×</span>` : ""}
        ${d.pdf_url ? `<a class="pdf-link" href="${esc(d.pdf_url)}" target="_blank" rel="noopener">p. ${esc(d.page)}${d.page_end && d.page_end !== d.page ? "–" + esc(d.page_end) : ""} in the PDF ↗</a>` : ""}
      </div>
    </div>
    <div class="section-label">Why it is flagged</div>
    <ul class="flags">${d.flags.map((f) => `<li class="sev-${esc(f.severity || "medium")}"><b>${esc(f.reason)}</b> · ${esc(f.severity || "")}${f.detail ? `<br>${esc(f.detail)}` : ""}</li>`).join("")}</ul>
    ${d.review_note ? `<p class="hint">Last reviewer note: ${esc(d.review_note)}</p>` : ""}
    ${d.label === "Clause" ? clauseHtml(d) : tableHtml(d)}
    <div class="actions" id="actions">
      <div class="section-label">Decision</div>
      <textarea id="note" rows="2" placeholder="Note: what is wrong (required to reject, optional to approve)"></textarea>
      <div class="card-actions">
        <button type="button" class="btn primary" id="approve">Approve</button>
        <button type="button" class="btn" id="reject">Reject and re-run with the note</button>
        <span class="hint" id="action-msg"></span>
      </div>
    </div>
    <div id="compare"></div>`;
}

function clauseHtml(d) {
  const x = d.extracted;
  const eqs = x.equations.map((e) => `
    <div class="eq-compare">
      <figure><figcaption>PDF crop · ${esc(e.number ? "Eq. " + e.number : e.id)}</figcaption>
        ${e.crop ? `<img class="crop" src="${esc(e.crop)}" alt="Crop of ${esc(e.id)}">` : `<span class="hint">inline equation, no crop</span>`}</figure>
      <figure><figcaption>Extracted LaTeX</figcaption><div class="tex">${e.latex ? tex(e.latex) : `<span class="hint">none</span>`}</div></figure>
    </div>
    <div class="sympy">SymPy: ${esc(e.sympy || "–")} · computes ${esc((e.computes || []).join(", ") || "–")} · uses ${esc((e.uses || []).join(", ") || "–")}</div>`).join("");
  return `
    <div class="section-label">Source: clause text</div>
    <p class="src-text clamp">${esc(d.source.text || "")}</p>
    ${eqs ? `<div class="section-label">Equations: crop beside the extracted LaTeX</div>${eqs}` : ""}
    <div class="section-label">Extracted</div>
    ${extractedLists(x)}`;
}

function extractedLists(x) {
  const list = (name, items) => items && items.length ? `<details class="issues" ${items.length <= 6 ? "open" : ""}><summary>${name} (${items.length})</summary><ul>${items.map((t) => `<li>${t}</li>`).join("")}</ul></details>` : "";
  return list("Parameters", (x.parameters || []).map((p) => `${tex(p.latex || p.id, false)} <span class="hint">${esc(p.id)}</span> ${esc(p.description || "")}${p.units ? ` (${esc(p.units)})` : ""}`))
    + list("Conditions", (x.conditions || []).map(esc))
    + list("Requirements", (x.requirements || []).map(esc))
    + list("Exceptions", (x.exceptions || []).map(esc))
    + list("Symbol corrections", (x.corrections || []).map((c) => `${esc(c.raw)} → ${esc(c.corrected)}${c.uncertain ? " (uncertain)" : ""}`));
}

function tableHtml(d) {
  const x = d.extracted;
  const rows = x.rows.map((r) => `<tr class="${r.status === "flagged" ? "hit" : ""}">${(r.cells || []).map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("");
  return `
    <div class="section-label">Source: table crop</div>
    <div class="crops">${d.source.crops.map((u) => `<img class="crop" src="${esc(u)}" alt="Table crop" loading="lazy">`).join("") || `<span class="hint">no crop</span>`}</div>
    <div class="section-label">Parsed rows${x.gives.length ? ` · gives ${esc(x.gives.join(", "))}` : ""}</div>
    <div class="table-scroll"><table class="rows">${rows}</table></div>
    ${x.footnotes.length ? `<div class="section-label">Footnote rules</div><ul class="plain">${x.footnotes.map((f) => `<li><b>${esc(f.kind || "note")}</b>: ${esc(f.rule || f.text)}</li>`).join("")}</ul>` : ""}`;
}

function wireActions(d) {
  const msg = $("#action-msg");
  const busy = (on, text) => {
    state.busy = on;
    $("#approve").disabled = on; $("#reject").disabled = on;
    msg.innerHTML = text ? (on ? `<span class="spinner"></span> ${text}` : text) : "";
  };
  $("#approve").onclick = async () => {
    busy(true, "Approving…");
    try {
      const r = await getJSON(`${itemUrl(d)}/approve`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: $("#note").value.trim() || null }) });
      busy(false, `Approved${r.pins_approved.length ? `; pinned answer locked (${r.pins_approved.length})` : ""}. ${r.note || ""}`);
      updateItem(d, "approved");
    } catch (err) { busy(false, `<span class="src-error">${esc(err.message)}</span>`); }
  };
  $("#reject").onclick = async () => {
    const note = $("#note").value.trim();
    if (!note) { msg.innerHTML = `<span class="src-error">Write a note first: it is sent to Claude with the original inputs.</span>`; $("#note").focus(); return; }
    busy(true, "Re-running this job with your note on the review model…");
    try {
      const r = await getJSON(`${itemUrl(d)}/reject`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }) });
      busy(false, r.replaced ? "The new answer passes its checks and replaced the old one. It is back in the queue for approval."
        : `The new answer did not pass, so the stored one stays. Status: ${esc(r.review_status.replace("_", " "))}.`);
      $("#compare").innerHTML = compareHtml(r);
      updateItem(d, r.review_status);
    } catch (err) { busy(false, `<span class="src-error">${esc(err.message)}</span>`); }
  };
}

function itemUrl(d) {
  return `/review/${encodeURIComponent(graph)}/item/${encodeURIComponent(d.label)}/${encodeURIComponent(d.id)}`;
}

function updateItem(d, status) {
  const it = state.items.find((i) => i.label === d.label && i.id === d.id);
  if (it) it.review_status = status;
  renderTabs();
  renderQueue();
}

function answerHtml(a) {
  if (!a) return `<span class="hint">no answer</span>`;
  if (a.raw) return `<pre class="log">${esc(JSON.stringify(a.raw, null, 1).slice(0, 4000))}</pre>`;
  return (a.equations || []).map((e) => `<div class="tex">${tex(e.latex)}</div><div class="sympy">SymPy: ${esc(e.sympy)}</div>`).join("")
    + extractedLists(a);
}

function compareHtml(r) {
  const before = r.before.extracted;
  const cost = r.cost && r.cost.total_cost_usd != null ? `$${r.cost.total_cost_usd.toFixed(4)}` : "–";
  const res = r.results.map((x) => `<div class="flags-line"><b>${esc(x.job)}</b>: new answer ${esc(x.status)}${x.kept ? ", kept" : ", not kept"}`
    + (x.issues.length ? `<ul class="flags">${x.issues.map((i) => `<li class="sev-${esc(i.severity || "medium")}"><b>${esc(i.reason || i.code)}</b>${i.detail ? `: ${esc(i.detail)}` : ""}</li>`).join("")}</ul>` : "") + `</div>`).join("");
  const beforeHtml = r.before.label === "Clause"
    ? before.equations.map((e) => `<div class="tex">${tex(e.latex)}</div><div class="sympy">SymPy: ${esc(e.sympy)}</div>`).join("") + extractedLists(before)
    : `<p class="hint">${before.rows.length} rows</p>`;
  return `<div class="section-label">Before and after · ${esc(r.model)} · ${cost}</div>
    ${res}
    <div class="ba">
      <div class="ba-col"><div class="ba-h">Before</div>${beforeHtml}</div>
      <div class="ba-col ${r.replaced ? "kept" : "dropped"}"><div class="ba-h">After: ${r.replaced ? "new answer (kept)" : "new answer (not kept)"}</div>${r.results.map((x) => answerHtml(x.answer)).join("")}</div>
    </div>
    <details class="issues"><summary>Run log</summary><pre class="log">${esc(r.log.join("\n"))}</pre></details>`;
}

document.addEventListener("change", (e) => {
  if (e.target.id !== "notes-toggle") return;
  state.notes = e.target.checked;
  storage(() => localStorage.setItem("review_notes", state.notes ? "1" : "0"));
  state.shown = PAGE;
  renderTabs();
  renderQueue();
});
document.addEventListener("click", (e) => {
  const tab = e.target.closest("[data-tab]");
  if (tab) { state.tab = tab.dataset.tab; state.shown = PAGE; renderTabs(); renderQueue(); return; }
  const row = e.target.closest(".q-row");
  if (row && !state.busy) { const [label, ...rest] = row.dataset.key.split("|"); openItem(label, rest.join("|")); return; }
  if (e.target.id === "more") { state.shown += PAGE; renderQueue(); }
});
document.addEventListener("DOMContentLoaded", () => load());
