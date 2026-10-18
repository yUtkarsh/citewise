// Ingest page: the profile screen (confirm), then the progress screen fed by the job's events.
"use strict";

const jobId = new URLSearchParams(location.search).get("job");
const STAGES = ["profiling", "pass1", "pass2", "done"];
const LOG_LINES = 200;
const state = { job: null, cy: null, graph: null, counts1: null, counts2: null, layoutTimer: null, logs: [] };

function show(id) {
  for (const s of ["profiling", "profile", "progress", "failed"]) $("#" + s).hidden = s !== id;
}

function setBadge(stage) {
  const badge = $("#stage-badge");
  const text = { queued: "Queued", profiling: "Profiling", profile_ready: "Check the profile", pass1: "Pass 1",
                 pass2: "Pass 2", done: "Done", failed: "Failed" }[stage] || stage;
  badge.textContent = text;
  badge.className = `status st-${stage === "done" ? "ready" : stage === "failed" ? "failed" : stage === "profile_ready" ? "partial" : "running"}`;
}

async function load() {
  if (!jobId) { fail("No job id in the address."); return; }
  try { state.job = await getJSON(`/jobs/${encodeURIComponent(jobId)}`); }
  catch (err) { fail(err.message); return; }
  const j = state.job;
  state.graph = j.graph;
  $("#file-line").innerHTML = `${esc(j.file)} → <span class="graph-id">${esc(j.graph)}</span>`;
  $("#cap").textContent = fmtUsd(j.max_cost);
  $("#cost-cap").textContent = `cap ${fmtUsd(j.max_cost)}`;
  setBadge(j.stage);
  if (j.stage === "failed") return fail(j.error);
  if (j.stage === "profile_ready") return showProfile(j.profile);
  if (j.stage === "queued" || j.stage === "profiling") {
    show("profiling");
    follow(-1, profilingEvent, () => load());
    return;
  }
  startProgress(-1);
}

function fail(message) {
  setBadge("failed");
  show("failed");
  $("#failed").innerHTML = `<strong>Ingestion stopped.</strong><br>${esc(message || "Unknown error")}`
    + `<p><a class="btn" href="/">Back to the library</a></p>`;
}

function follow(after, onEvent, onEnd) {
  return streamEvents(`/jobs/${encodeURIComponent(jobId)}/events?after=${after}`, onEvent, onEnd);
}

function profilingEvent(name, data) {
  if (name === "stage") { $("#profiling-msg").textContent = data.message; setBadge(data.stage); }
  if (name === "stage" && data.stage === "failed") fail(data.message);
  if (name === "log") appendLog($("#profiling-log"), data);
}

// ---- profile screen ----------------------------------------------------------------------------

function showProfile(p) {
  show("profile");
  $("#heading").textContent = "Check the profile";
  const form = $("#confirm-form");
  form.title.value = p.title || "";
  form.edition.value = p.edition || "";
  form.jurisdiction.value = p.jurisdiction || "";
  $("#reingest-note").hidden = !state.job.reingest;

  const v = p.validation || {};
  const checks = Object.entries(v.checks || {}).map(([k, x]) => `<span class="pill">${esc(k)} ${fmtPct(x)}</span>`).join("");
  const issues = (v.issues || []).slice(0, 12);
  const runs = p.no_heading_runs || [];
  $("#validation").innerHTML = `
    <div class="val-head"><span class="status st-${v.status === "pass" ? "ready" : "partial"}">${esc(v.status || "not validated")}</span>
      ${v.revised ? `<span class="pill">revised once by Claude</span>` : ""}</div>
    <dl class="stats compact">
      <div><dt>Coverage</dt><dd class="${v.coverage < 0.9 ? "warnv" : ""}">${fmtPct(v.coverage)}</dd></div>
      <div><dt>Headings</dt><dd>${fmtInt(v.heading_count)}</dd></div>
      <div><dt>Order issues</dt><dd class="${v.order_violations ? "warnv" : ""}">${fmtInt(v.order_violations)}</dd></div>
      <div><dt>Hierarchy issues</dt><dd class="${v.hierarchy_violations ? "warnv" : ""}">${fmtInt(v.hierarchy_violations)}</dd></div>
    </dl>
    ${checks ? `<div class="pills">${checks}</div>` : ""}
    ${(v.low_coverage_ranges || []).length ? `<p class="warn-line">Low coverage on pages ${v.low_coverage_ranges.map(([a, b]) => a === b ? a : `${a}–${b}`).join(", ")}</p>` : ""}
    ${runs.length ? `<p class="warn-line">No headings on pages ${runs.map(([a, b]) => `${a}–${b}`).join(", ")}</p>` : ""}
    ${issues.length ? `<details class="issues"><summary>${issues.length}${(v.issues || []).length > 12 ? "+" : ""} warnings</summary><ul>${issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></details>` : `<p class="hint">No warnings.</p>`}`;

  $("#streams").innerHTML = (p.streams || []).map((s) => `
    <div class="stream">
      <div><strong>${esc(s.name)}</strong> <span class="pill">${esc(s.kind)}</span> <span class="hint">${fmtInt(s.headings)} headings · levels ${esc(s.levels.join(", "))}</span></div>
      <div class="examples">${s.examples.map((e) => `<code>${esc(s.prefix || "")}${esc(e.replace(s.prefix || "", ""))}</code>`).join(" ")}</div>
    </div>`).join("")
    + `<div class="stream"><span class="hint">Table captions</span> <code>${esc(p.table_caption)}</code></div>`
    + `<div class="stream"><span class="hint">Equation numbers</span> <code>${esc(p.equation_number)}</code></div>`;

  $("#samples").innerHTML = `<thead><tr><th>Number</th><th>Title</th><th>Stream</th><th>Page</th></tr></thead><tbody>`
    + (p.samples || []).map((s) => `<tr><td class="mono" style="padding-left:${8 + (s.depth - 1) * 12}px">${esc(s.number)}</td>`
      + `<td>${esc(s.title)}</td><td>${esc(s.stream)}</td><td class="mono">${esc(s.page)}</td></tr>`).join("") + "</tbody>";

  const total = p.pages || 1;
  $("#ranges").innerHTML = `<div class="range-bar">${(p.ranges || []).map((r) =>
    `<span class="rk-${esc(r.kind)}" style="flex:${r.end - r.start + 1}" title="${esc(r.kind)}: pages ${r.start}–${r.end}"></span>`).join("")}</div>`
    + `<div class="range-legend">${(p.ranges || []).map((r) => `<span><i class="rk-${esc(r.kind)}"></i>${esc(r.kind.replace("_", " "))} ${r.start}–${r.end}</span>`).join("")}</div>`
    + `<p class="hint">${fmtInt(total)} pages</p>`;

  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#confirm-btn");
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      await getJSON(`/jobs/${encodeURIComponent(jobId)}/confirm`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: form.title.value, edition: form.edition.value, jurisdiction: form.jurisdiction.value }),
      });
      startProgress(streamEvents.lastId ?? -1);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Confirm and start Pass 1 and Pass 2";
      alert(err.message);
    }
  };
}

// ---- progress screen ---------------------------------------------------------------------------

function startProgress(after) {
  show("progress");
  $("#heading").textContent = "Building the graph";
  initGraph();
  renderCounters();
  follow(after, progressEvent, (end) => { if (end.stage === "done") finished(); });
}

function progressEvent(name, data) {
  switch (name) {
    case "stage":
      setBadge(data.stage);
      markStage(data.stage);
      appendLog($("#log"), { t: data.t, message: `— ${data.message}` });
      if (data.stage === "failed") fail(data.message);
      if (data.stage === "done") finished();
      break;
    case "log": appendLog($("#log"), data); break;
    case "counts":
      if (data.pass === 1) state.counts1 = data; else state.counts2 = data;
      renderCounters();
      break;
    case "graph": addSkeleton(data); break;
    case "graph_status": markClause(data); break;
  }
}

function markStage(stage) {
  const at = STAGES.indexOf(stage);
  document.querySelectorAll("#stages li").forEach((li) => {
    const i = STAGES.indexOf(li.dataset.stage);
    li.className = i < at || stage === "done" ? "done" : i === at ? "active" : "";
  });
}

function renderCounters() {
  const c1 = state.counts1 || {}, c2 = state.counts2 || {};
  const items = [["Clauses", c1.clauses], ["Tables", c1.tables], ["Equations", c1.equations], ["Figures", c1.figures],
                 ["Symbol warnings", c1.symbol_warnings], ["Coverage", c1.coverage != null ? fmtPct(c1.coverage) : null],
                 ["Pass 2 flagged", c2.flagged]];
  $("#counters").innerHTML = items.map(([k, v]) => `<div><dt>${k}</dt><dd>${v == null ? "–" : typeof v === "number" ? fmtInt(v) : esc(v)}</dd></div>`).join("");
  $("#counters").className = "stats";
  if (c2.cost_usd != null) {
    $("#cost").textContent = `$${c2.cost_usd.toFixed(3)}`;
    $("#cost-bar").style.width = `${Math.min(100, (c2.cost_usd / (c2.max_cost || 1)) * 100)}%`;
  }
  if (c2.total) {
    $("#jobs-done").textContent = `${fmtInt(c2.done)} / ${fmtInt(c2.total)}`;
    $("#jobs-bar").style.width = `${Math.min(100, (c2.done / c2.total) * 100)}%`;
  }
}

function appendLog(pre, data) {
  const line = `${data.t != null ? String(Math.round(data.t)).padStart(4) + "s " : ""}${data.message}`;
  state.logs.push(line);
  pre.textContent = (pre.textContent ? pre.textContent + "\n" : "") + line;
  const lines = pre.textContent.split("\n");
  if (lines.length > LOG_LINES) pre.textContent = lines.slice(-LOG_LINES).join("\n");
  pre.scrollTop = pre.scrollHeight;
}

function finished() {
  if (!$("#done-box").hidden) return;
  markStage("done");
  const c2 = state.counts2 || {};
  const box = $("#done-box");
  box.hidden = false;
  box.className = "notice ok";
  box.innerHTML = `<strong>Ingestion finished.</strong> ${c2.stopped_at_cost_cap ? `Stopped at the cost cap with ${fmtInt(c2.jobs_not_run)} jobs not run; re-ingest to resume them. ` : ""}`
    + `<div class="card-actions"><a class="btn primary" href="report.html?graph=${encodeURIComponent(state.graph)}">View report</a>`
    + `<a class="btn" href="chat.html?graph=${encodeURIComponent(state.graph)}">Open chat</a></div>`;
}

// ---- live skeleton graph -----------------------------------------------------------------------

function graphStyle() {
  return [
    { selector: "node", style: {
      "background-color": cssVar("--n-clause"), "width": 9, "height": 9, "label": "",
      "font-family": cssVar("--font"), "font-size": 9, "color": cssVar("--ink-2"), "opacity": 0.9,
    } },
    { selector: "node[depth = 1]", style: {
      "width": 18, "height": 18, "label": "data(name)", "background-color": cssVar("--accent"),
      "text-valign": "bottom", "text-margin-y": 3, "text-wrap": "ellipsis", "text-max-width": 120,
      "text-background-color": cssVar("--bg"), "text-background-opacity": 0.8,
    } },
    { selector: "node[kind = 'commentary']", style: { "background-color": cssVar("--n-where") } },
    { selector: "node.extracted", style: { "background-color": cssVar("--n-parameter") } },
    { selector: "node.flagged", style: { "background-color": cssVar("--bad") } },
    { selector: "edge", style: { "width": 1, "line-color": cssVar("--edge"), "opacity": 0.5, "curve-style": "haystack" } },
  ];
}

function initGraph() {
  if (state.cy) return;
  state.cy = cytoscape({ container: $("#cy"), style: graphStyle(), minZoom: 0.05, maxZoom: 3, wheelSensitivity: 0.25 });
  state.cy.on("mouseover", "node", (e) => e.target.style("label", e.target.data("name")));
  state.cy.on("mouseout", "node[depth != 1]", (e) => e.target.removeStyle("label"));
  document.addEventListener("themechange", () => state.cy.style(graphStyle()));
}

function addSkeleton(data) {
  const cy = state.cy;
  cy.batch(() => {
    for (const n of data.nodes || []) {
      if (!cy.getElementById(n.id).length) cy.add({ group: "nodes", data: { ...n } });
    }
    for (const e of data.edges || []) {
      const [src, , dst] = e.split("|");
      if (cy.getElementById(src).length && cy.getElementById(dst).length && !cy.getElementById(e).length) {
        cy.add({ group: "edges", data: { id: e, source: src, target: dst } });
      }
    }
  });
  $("#graph-count").textContent = `${fmtInt(cy.nodes().length)} chapters and sections`;
  clearTimeout(state.layoutTimer);
  state.layoutTimer = setTimeout(() => {
    cy.layout({ name: "cose", animate: false, randomize: false, fit: true, padding: 20, nodeRepulsion: () => 4000,
                idealEdgeLength: () => 30, numIter: 600 }).run();
  }, 250);
}

function markClause(data) {
  const n = state.cy && state.cy.getElementById(data.id);
  if (n && n.length) n.removeClass("extracted flagged").addClass(data.status === "flagged" ? "flagged" : "extracted");
}

document.addEventListener("DOMContentLoaded", load);
