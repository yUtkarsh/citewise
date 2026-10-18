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
  return `
    <div class="section-label">Source beside what was extracted${x.gives.length ? ` · gives ${esc(x.gives.join(", "))}` : ""}</div>
    <div class="cmp">
      ${cropColumn(d.source.crops)}
      <div class="cmp-col"><div class="ba-h">Extracted (in the graph)</div>${tableViewHtml(x.view, null, x.view.printed_header, "")}</div>
    </div>`;
}

function cropColumn(crops) {
  return `<div class="cmp-col cmp-crop"><div class="ba-h">PDF crop</div>${(crops || []).map((u) =>
    `<img class="crop" src="${esc(u)}" alt="Table crop" loading="lazy">`).join("") || `<span class="hint">no crop</span>`}</div>`;
}

// ---- table answers, rendered as tables: value columns, then each row's conditions and values ----

const cellKey = (v) => (v ? `${v.value ?? ""}|${v.unit ?? ""}|${v.formula ?? ""}` : "");
const blankKey = (b) => (b || []).map((x) => `${x.col}:${x.interpretation}`).sort().join("|");

function valueColumns(view, other) {
  const cols = new Set();
  for (const v of [view, other]) {
    if (!v) continue;
    for (const r of v.rows) Object.keys(r.values).forEach((c) => cols.add(Number(c)));
    for (const b of v.rows.flatMap((r) => r.blank)) cols.add(Number(b.col));
  }
  if (!cols.size) for (const v of [view, other]) (v ? v.columns : []).forEach((c) => cols.add(Number(c.col)));
  return [...cols].sort((a, b) => a - b);
}

function paramsAt(view, col) {
  return view ? view.columns.filter((c) => Number(c.col) === col) : [];
}

// side: "" (no comparison), "before" or "after"; other: the view on the other side
function tableViewHtml(view, other, printed, side) {
  if (!view) return `<span class="hint">no answer</span>`;
  const mark = side === "before" ? "was" : "chg";
  const cols = valueColumns(view, other);
  const byRow = (v) => new Map((v ? v.rows : []).map((r) => [r.row, r]));
  const mine = byRow(view), theirs = byRow(other);
  const rowIds = [...new Set([...mine.keys(), ...(other ? theirs.keys() : [])])].sort((a, b) => a - b);
  let changed = 0;
  const head = cols.map((c) => {
    const p = paramsAt(view, c), q = paramsAt(other, c);
    const key = (ps) => ps.map((x) => `${x.parameter_id}|${x.units ?? ""}`).sort().join(",");
    const diff = other && key(p) !== key(q);
    if (diff) changed++;
    const label = (printed && printed[String(c)]) || (p[0] && p[0].header_text) || `col ${c}`;
    return `<th class="${diff ? mark : ""}"><span class="th-text">${esc(label)}</span>`
      + `${p.map((x) => `<span class="th-param">${esc(x.parameter_id)}${x.units ? ` (${esc(x.units)})` : ""}</span>`).join("") || `<span class="th-param none">no parameter</span>`}</th>`;
  }).join("");
  const body = rowIds.map((id) => {
    const r = mine.get(id), o = theirs.get(id);
    if (!r) return `<tr class="missing"><td class="rn">${id}</td><td colspan="${cols.length + 1}">not in this answer</td></tr>`;
    const condDiff = other && (!o || (r.conditions || []).join("; ") !== (o.conditions || []).join("; "));
    if (condDiff) changed++;
    const cells = cols.map((c) => {
      const v = r.values[String(c)], w = o ? o.values[String(c)] : undefined;
      const blank = r.blank.filter((b) => Number(b.col) === c);
      const diff = other && (cellKey(v) !== cellKey(w) || blankKey(blank) !== blankKey(o ? o.blank.filter((b) => Number(b.col) === c) : []));
      if (diff) changed++;
      const inner = v ? `${esc(v.value)}${v.unit ? ` <span class="unit">${esc(v.unit)}</span>` : ""}`
        + `${v.formula ? `<span class="formula">${esc(v.formula)}</span>` : ""}${v.source === "image" ? `<span class="src-img" title="read from the image">img</span>` : ""}`
        : blank.length ? `<span class="blank">blank: ${esc(blank.map((b) => b.interpretation).join("; "))}</span>` : "";
      return `<td class="num ${diff ? mark : ""}">${inner}</td>`;
    }).join("");
    return `<tr class="${other && !o ? mark + "-row" : ""}"><td class="rn">${id}</td>`
      + `<td class="cond ${condDiff ? mark : ""}">${esc((r.conditions || []).join("; ")) || `<span class="hint">–</span>`}</td>${cells}</tr>`;
  }).join("");
  const notes = (view.footnotes || []).map((f) => {
    const diff = other && !(other.footnotes || []).some((g) => g.marker === f.marker && g.rule === f.rule);
    if (diff) changed++;
    return `<li class="${diff ? mark : ""}"><b>${esc(f.marker ? f.marker + " · " : "")}${esc(f.kind)}</b>: ${esc(f.rule)}</li>`;
  }).join("");
  const summary = other ? `<p class="hint diff-count">${changed ? `${changed} ${changed === 1 ? "difference" : "differences"} highlighted` : "No differences"}</p>` : "";
  return `${summary}<div class="table-scroll"><table class="tview"><thead><tr><th class="rn">row</th><th>Conditions</th>${head}</tr></thead>`
    + `<tbody>${body}</tbody></table></div>${notes ? `<ul class="plain notes">${notes}</ul>` : ""}`;
}

function jsonToggle(value) {
  return `<details class="json"><summary>Show JSON</summary><pre class="log">${esc(JSON.stringify(value, null, 1))}</pre></details>`;
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

// ---- clause answers, rendered as items: equations, then the extracted lists ----

function clauseItemsHtml(x, other, side) {
  const mark = side === "before" ? "was" : "chg";
  const eqOther = new Map(((other && other.equations) || []).map((e) => [e.id, e]));
  const eqs = (x.equations || []).map((e) => {
    const o = eqOther.get(e.id);
    const diff = other && (!o || o.latex !== e.latex || o.sympy !== e.sympy);
    return `<div class="eq-item ${diff ? mark : ""}"><div class="eq-id">${esc(e.id)}</div><div class="tex">${tex(e.latex)}</div>`
      + `<div class="sympy">SymPy: ${esc(e.sympy || "–")}${e.computes && e.computes.length ? ` · computes ${esc([].concat(e.computes).join(", "))}` : ""}</div></div>`;
  }).join("");
  const list = (name, field, key, render) => {
    const items = x[field];
    if (!items || !items.length) return "";
    const otherKeys = other ? (other[field] || []).map(key) : null;
    return `<details class="issues" open><summary>${name} (${items.length})</summary><ul>${items.map((it) =>
      `<li class="${otherKeys && !otherKeys.includes(key(it)) ? mark : ""}">${render(it)}</li>`).join("")}</ul></details>`;
  };
  return eqs
    + list("Parameters", "parameters", (p) => `${p.id}|${p.description}`, (p) => `${tex(p.latex || p.id, false)} <span class="hint">${esc(p.id)}</span> ${esc(p.description || "")}${p.units ? ` (${esc(p.units)})` : ""}`)
    + list("Conditions", "conditions", (t) => t, esc)
    + list("Requirements", "requirements", (t) => t, esc)
    + list("Exceptions", "exceptions", (t) => t, esc);
}

function compareHtml(r) {
  const before = r.before.extracted;
  const cost = r.cost && r.cost.total_cost_usd != null ? `$${r.cost.total_cost_usd.toFixed(4)}` : "–";
  const res = r.results.map((x) => `<div class="flags-line"><b>${esc(x.job)}</b>: new answer ${esc(x.status)}${x.kept ? ", kept" : ", not kept"}`
    + (x.issues.length ? `<ul class="flags">${x.issues.map((i) => `<li class="sev-${esc(i.severity || "medium")}"><b>${esc(i.reason || i.code)}</b>${i.detail ? `: ${esc(i.detail)}` : ""}</li>`).join("")}</ul>` : "") + `</div>`).join("");
  const afterTitle = r.replaced ? "After: new answer (kept)" : "After: new answer (not kept)";
  const raws = r.results.map((x) => x.answer && x.answer.raw).filter(Boolean);
  const afterJson = jsonToggle(raws.length === 1 ? raws[0] : raws);
  let body;
  if (r.before.label === "Table") {
    const printed = before.view.printed_header;
    body = `<div class="cmp cmp3">
      ${cropColumn(r.before.source.crops)}
      <div class="cmp-col"><div class="ba-h">Before</div>${tableViewHtml(before.view, r.after_view, printed, "before")}${jsonToggle(before.view)}</div>
      <div class="cmp-col ${r.replaced ? "kept" : "dropped"}"><div class="ba-h">${afterTitle}</div>${tableViewHtml(r.after_view, before.view, printed, "after")}${afterJson}</div>
    </div>`;
  } else {
    const after = r.results[0] && r.results[0].answer;
    body = `<div class="cmp">
      <div class="cmp-col"><div class="ba-h">Before</div>${clauseItemsHtml(before, after, "before")}${jsonToggle(before)}</div>
      <div class="cmp-col ${r.replaced ? "kept" : "dropped"}"><div class="ba-h">${afterTitle}</div>${after ? clauseItemsHtml(after, before, "after") : `<span class="hint">no answer</span>`}${afterJson}</div>
    </div>`;
  }
  return `<div class="section-label">Before and after · ${esc(r.model)} · ${cost}</div>
    ${res}${body}
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
