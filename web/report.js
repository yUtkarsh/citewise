// Ingestion report: counts, coverage, flags by reason, symbol warnings by page, cost and time.
"use strict";

const graph = new URLSearchParams(location.search).get("graph");
const NODE_ORDER = ["Clause", "Table", "Row", "Equation", "WhereItem", "Figure", "Parameter", "Term", "Condition",
                    "Requirement", "Exception", "Footnote", "ExternalCode", "FlaggedLine", "SymbolCorrection"];

async function load() {
  const root = $("#report");
  if (!graph) { root.innerHTML = `<p class="src-error">No graph in the address.</p>`; return; }
  let r;
  try { r = await getJSON(`/codes/${encodeURIComponent(graph)}/report`); }
  catch (err) { root.innerHTML = `<a class="back" href="/">← Library</a><p class="src-error">${esc(err.message)}</p>`; return; }
  document.title = `Report · ${r.card.designation || r.card.title}`.slice(0, 60);
  root.innerHTML = [head(r), summary(r), nodes(r), flags(r), warnings(r), runs(r)].join("");
}

function head(r) {
  const c = r.card;
  return `<a class="back" href="/">← Library</a>
    <div class="page-head">
      <div>
        <h1>${esc(c.title)}</h1>
        <p class="lede">${[c.edition, c.jurisdiction].filter(Boolean).map(esc).join(" · ")} · <span class="graph-id">${esc(c.graph)}</span></p>
      </div>
      <div class="card-actions">
        <span class="status st-${esc(c.status.state)}">${esc(c.status.text)}</span>
        <a class="btn primary" href="chat.html?graph=${encodeURIComponent(c.graph)}">Open chat</a>
      </div>
    </div>`;
}

function summary(r) {
  const c = r.card, n = c.counts, p2 = r.pass2 || {}, p1 = r.pass1 || {};
  const cost = p2.costs ? p2.costs.total_cost_usd : null;
  const seconds = (p1.timings_s || {}).total;
  const v = r.profile_validation || {};
  return `<div class="panel">
    <dl class="stats big">
      <div><dt>Pages</dt><dd>${fmtInt(c.pages)}</dd></div>
      <div><dt>Clauses</dt><dd>${fmtInt(n.clauses)}</dd></div>
      <div><dt>Tables</dt><dd>${fmtInt(n.tables)}</dd></div>
      <div><dt>Equations</dt><dd>${fmtInt(n.equations)}</dd></div>
      <div><dt>Coverage</dt><dd class="${r.coverage < 0.9 ? "warnv" : ""}">${fmtPct(r.coverage)}</dd></div>
      <div><dt>Flagged items</dt><dd class="${n.flagged ? "warnv" : ""}">${fmtInt(n.flagged)}</dd></div>
      <div><dt>Symbol warnings</dt><dd class="${n.symbol_warnings ? "warnv" : ""}">${fmtInt(n.symbol_warnings)}</dd></div>
      <div><dt>Last Pass 2 cost</dt><dd>${fmtUsd(cost)}</dd></div>
    </dl>
    <p class="hint">Profile validation ${esc(v.status || "–")}${v.revised ? " (after one revision)" : ""}, ${fmtInt(v.heading_count)} headings.
      ${(r.low_coverage_ranges || []).length ? `Low coverage on pages ${r.low_coverage_ranges.map(([a, b]) => a === b ? a : `${a}–${b}`).join(", ")}.` : "No low-coverage page ranges."}
      Pass 1 took ${fmtSecs(seconds)}.</p>
  </div>`;
}

function nodes(r) {
  const entries = Object.entries(r.nodes).sort(([a], [b]) => (NODE_ORDER.indexOf(a) + 99) % 99 - (NODE_ORDER.indexOf(b) + 99) % 99);
  return `<div class="panel"><h2 class="panel-h">Graph contents</h2>
    <table class="data"><thead><tr><th>Node</th><th class="num">Count</th><th class="num">Extracted by Pass 2</th><th class="num">Flagged</th><th class="num">Skeleton only</th></tr></thead><tbody>
    ${entries.map(([label, v]) => `<tr><td>${esc(label)}</td><td class="num">${fmtInt(v.count)}</td>
      <td class="num">${["Clause", "Table", "Equation"].includes(label) ? fmtInt(v.count - v.skeleton - v.flagged) : ""}</td>
      <td class="num ${v.flagged ? "warnv" : ""}">${v.flagged ? fmtInt(v.flagged) : ""}</td>
      <td class="num">${v.skeleton ? fmtInt(v.skeleton) : ""}</td></tr>`).join("")}
    </tbody></table></div>`;
}

function bars(entries, sevOf) {
  const max = Math.max(1, ...entries.map(([, n]) => n));
  return `<div class="hbars">${entries.map(([k, n]) => {
    const sev = sevOf ? sevOf(k) : null;
    return `<div class="hbar"><span class="hk">${esc(k)}${sev ? ` <span class="sev sev-${sev}">${sev}</span>` : ""}</span>`
      + `<span class="ht"><i style="width:${(n / max) * 100}%"></i></span><span class="hn">${fmtInt(n)}</span></div>`;
  }).join("")}</div>`;
}

function flags(r) {
  const entries = r.flags_by_reason.map((f) => [f.reason, f.count]);
  const worst = Object.fromEntries(r.flags_by_reason.map((f) => [f.reason, f.severity]));
  const sev = r.flags_by_severity || {};
  return `<div class="panel"><h2 class="panel-h">Flags by reason</h2>
    ${entries.length ? bars(entries, (k) => worst[k]) : `<p class="hint">No flags.</p>`}
    <p class="hint">By severity: ${["high", "medium", "low"].map((s) => `${s} ${fmtInt(sev[s] || 0)}`).join(" · ")}. Each flag is one item for the review queue.</p></div>`;
}

function warnings(r) {
  const pages = r.symbol_warnings_by_page || [];
  if (!pages.length) return `<div class="panel"><h2 class="panel-h">Symbol warnings</h2><p class="hint">None.</p></div>`;
  const max = Math.max(...pages.map(([, n]) => n));
  const total = r.card.pages || pages[pages.length - 1][0];
  return `<div class="panel"><h2 class="panel-h">Symbol warnings by page</h2>
    <div class="strip" role="img" aria-label="Symbol warnings per page">
      ${pages.map(([p, n]) => `<i style="left:${((p - 1) / total) * 100}%;height:${20 + (n / max) * 80}%" title="page ${p}: ${n}"></i>`).join("")}
    </div>
    <div class="strip-axis"><span>p.1</span><span>p.${fmtInt(total)}</span></div>
    ${bars(Object.entries(r.symbol_warnings_by_reason))}
    <details class="issues"><summary>${pages.length} pages with warnings</summary>
      <p class="mono small">${pages.map(([p, n]) => `p.${p}: ${n}`).join(" · ")}</p></details></div>`;
}

function runs(r) {
  const p2 = r.pass2, p1 = r.pass1, up = r.upload;
  const rows = [];
  if (p1) {
    rows.push(["Pass 1", fmtSecs((p1.timings_s || {}).total), "$0.00 (no API calls)",
      `${fmtInt(p1.references_resolved)} references resolved, ${fmtInt(p1.references_unresolved)} unresolved; ${fmtInt(p1.external_codes)} external codes`]);
  }
  if (p2) {
    const c = p2.costs || {};
    const by = Object.entries(c.by_job_type || {}).map(([k, v]) =>
      `${k}: ${fmtInt(v.jobs)} jobs, ${fmtInt(v.api_calls || 0)} calls, ${fmtInt(v.cache_hits || 0)} cached, ${fmtUsd(v.cost_usd || 0)}`).join("<br>");
    rows.push(["Last Pass 2 run", fmtSecs(p2.seconds), fmtUsd(c.total_cost_usd),
      `${by}${p2.stopped_at_cost_cap ? `<br><strong class="warnv">Stopped at the cost cap (${fmtUsd(p2.max_cost_usd)}); ${fmtInt(p2.jobs_not_run)} jobs not run.</strong>` : ""}`]);
  }
  if (up) rows.push(["Upload", fmtSecs(up.total_seconds), `cap ${fmtUsd(up.max_cost)}`, `${esc(up.file)}: profile ${fmtSecs(up.profile_seconds)}, Pass 1 ${fmtSecs(up.pass1_seconds)}, Docling ${up.docling ? "on" : "off"}; finished ${esc(up.finished)}. Time excludes the wait on the profile screen.`]);
  return `<div class="panel"><h2 class="panel-h">Runs, cost and time</h2>
    ${rows.length ? `<table class="data"><thead><tr><th>Run</th><th>Time</th><th>Cost</th><th>Detail</th></tr></thead><tbody>
      ${rows.map((x) => `<tr><td>${x[0]}</td><td class="mono">${x[1]}</td><td class="mono">${x[2]}</td><td class="small">${x[3]}</td></tr>`).join("")}</tbody></table>`
      : `<p class="hint">No run reports saved for this file.</p>`}
    <p class="hint">The Pass 2 report covers the last run only; a run limited to some pages or tables reports just those.</p></div>`;
}

document.addEventListener("DOMContentLoaded", load);
