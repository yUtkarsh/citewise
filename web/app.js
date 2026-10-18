// CiteWise chat page. Event contract: web/EVENTS.md.
"use strict";

const $ = (sel) => document.querySelector(sel);
const LABEL_VARS = {
  Clause: "--n-clause", Equation: "--n-equation", Parameter: "--n-parameter", Table: "--n-table",
  Row: "--n-row", Footnote: "--n-row", WhereItem: "--n-where", Term: "--n-term",
};
const LEGEND = ["Clause", "Equation", "Parameter", "Table", "Row", "WhereItem"];
const LEGEND_NAMES = { WhereItem: "Where-item" };

const state = {
  sessionId: null,
  graph: new URLSearchParams(location.search).get("graph"),
  forceMock: new URLSearchParams(location.search).has("mock"),
  busy: false,
  lastQuestion: null,
  facts: [],
  baseFacts: {},       // param_id -> value before the user's edits, to mark changed ones
  nodes: new Map(),    // bare id -> {label, id, name, page, cyId}
  citations: new Map(),
  cy: null,
};

// ---- small helpers -----------------------------------------------------------------------------

function storage(fn, fallback) { try { return fn(); } catch { return fallback; } }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function labelColor(label) { return cssVar(LABEL_VARS[label] || "--n-other"); }
function tex(latex, display = false) {
  if (!window.katex) return esc(latex);
  try { return katex.renderToString(latex, { throwOnError: false, displayMode: display }); }
  catch { return esc(latex); }
}
function cyId(label, id) { return `${label}::${id}`; }
function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}

function shortName(label, id) {
  const s = String(id);
  if (label === "Clause") return "§" + s;
  if (label === "Equation") return s.replace(/^Equation\s+/, "Eq. ");
  if (s.includes("#")) {
    const [owner, part] = s.split("#");
    return `${owner.replace(/^Equation\s+/, "Eq. ").replace(/^Table\s+/, "T")} ${part}`;
  }
  return s;
}

// ---- theme -------------------------------------------------------------------------------------

function initTheme() {
  const saved = storage(() => localStorage.getItem("theme"), null);
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const dark = currentTheme() === "dark";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    storage(() => localStorage.setItem("theme", dark ? "light" : "dark"));
    restyleGraph();
    renderLegend();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { restyleGraph(); renderLegend(); });
}
function currentTheme() {
  const t = document.documentElement.dataset.theme;
  if (t) return t;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// ---- graph -------------------------------------------------------------------------------------

function graphStyle() {
  const ink = cssVar("--ink"), ink2 = cssVar("--ink-2"), edge = cssVar("--edge"), path = cssVar("--path");
  const bg = cssVar("--bg"), font = cssVar("--font");
  return [
    { selector: "node", style: {
      "background-color": (n) => labelColor(n.data("label")),
      "label": "data(name)", "font-family": font, "font-size": 10, "color": ink2,
      "text-valign": "bottom", "text-margin-y": 4, "text-wrap": "ellipsis", "text-max-width": 110,
      "width": 16, "height": 16, "opacity": 0.38, "border-width": 0, "border-color": path,
      "text-background-color": bg, "text-background-opacity": 0.75, "text-background-padding": 1,
      "transition-property": "opacity, width, height, border-width", "transition-duration": "250ms",
    } },
    { selector: "node[label = 'Clause']", style: { "shape": "round-rectangle", "width": 20, "height": 14 } },
    { selector: "node[label = 'Equation']", style: { "shape": "diamond", "width": 19, "height": 19 } },
    { selector: "node[label = 'Table']", style: { "shape": "rectangle", "width": 17, "height": 13 } },
    { selector: "node[label = 'Row'], node[label = 'Footnote']", style: { "shape": "rectangle", "width": 13, "height": 7 } },
    { selector: "node[label = 'WhereItem']", style: { "width": 11, "height": 11 } },
    { selector: "node.fresh", style: { "opacity": 0.75 } },
    { selector: "node.path", style: {
      "opacity": 1, "border-width": 2.5, "color": ink, "font-weight": 600, "font-size": 11,
      "width": 22, "height": 22, "z-index": 10,
    } },
    { selector: "node.path[label = 'Clause']", style: { "width": 26, "height": 18 } },
    { selector: "node.path[label = 'Row']", style: { "width": 18, "height": 10 } },
    { selector: "node.unreviewed, node.flagged", style: { "border-style": "dashed", "border-width": 2, "border-color": cssVar("--warn") } },
    { selector: "node.path.unreviewed, node.path.flagged", style: { "border-color": path, "border-style": "dashed", "border-width": 3 } },
    { selector: "node.faded", style: { "opacity": 0.22 } },
    { selector: "node:selected, node.focus", style: {
      "overlay-color": path, "overlay-opacity": 0.14, "overlay-padding": 7, "opacity": 1,
    } },
    { selector: "edge", style: {
      "width": 1, "line-color": edge, "target-arrow-color": edge, "target-arrow-shape": "triangle",
      "arrow-scale": 0.7, "curve-style": "bezier", "opacity": 0.35,
      "label": "", "font-family": font, "font-size": 8, "color": ink2,
      "text-rotation": "autorotate", "text-background-color": bg, "text-background-opacity": 0.85,
      "transition-property": "opacity, width, line-color", "transition-duration": "250ms",
    } },
    { selector: "edge.fresh", style: { "opacity": 0.7 } },
    { selector: "edge.faded", style: { "opacity": 0.15 } },
    { selector: "edge.path", style: {
      "width": 3, "line-color": path, "target-arrow-color": path, "opacity": 1, "label": "data(type)", "z-index": 9,
    } },
    { selector: "edge.inferred", style: { "line-style": "dashed" } },
    { selector: "edge:selected", style: { "label": "data(type)", "opacity": 1 } },
  ];
}

function initGraph() {
  state.cy = cytoscape({
    container: $("#cy"), elements: [], style: graphStyle(),
    minZoom: 0.2, maxZoom: 3, wheelSensitivity: 0.25, boxSelectionEnabled: false,
  });
  state.cy.on("tap", "node", (e) => openSource(e.target.data("label"), e.target.data("nodeId")));
  state.cy.on("mouseover", "edge", (e) => e.target.select());
  state.cy.on("mouseout", "edge", (e) => e.target.unselect());
  renderLegend();
}

function restyleGraph() { if (state.cy) state.cy.style(graphStyle()); }

function renderLegend() {
  $("#legend").innerHTML = LEGEND.map((l) => `<span><i style="background:${labelColor(l)}"></i>${LEGEND_NAMES[l] || l}</span>`).join("")
    + `<span><i class="faint-key"></i>explored</span><span><i class="path-key"></i>answer path</span>`;
}

function resetGraph() {
  state.cy.elements().remove();
  state.nodes.clear();
  state.citations.clear();
  $("#graph-empty").hidden = false;
  $("#graph-status").textContent = "";
}

function addNode(n) {
  if (!n || !n.label || n.id == null) return null;
  const key = String(n.id);
  const known = state.nodes.get(key);
  if (known && known.label === n.label) {
    if (n.page && !known.page) known.page = n.page;
    return known;
  }
  const info = { label: n.label, id: key, name: n.name || shortName(n.label, key), page: n.page, cyId: cyId(n.label, key) };
  if (!state.cy.getElementById(info.cyId).length) {
    const pos = seedPosition();
    state.cy.add({ group: "nodes", data: { id: info.cyId, label: info.label, nodeId: key, name: info.name }, position: pos, classes: "fresh" });
  }
  if (!known) state.nodes.set(key, info);
  return info;
}

function seedPosition() {
  const ext = state.cy.extent();
  const cx = (ext.x1 + ext.x2) / 2, cy = (ext.y1 + ext.y2) / 2;
  return { x: cx + (Math.random() - 0.5) * 80, y: cy + (Math.random() - 0.5) * 80 };
}

function parseEdge(s) {
  const parts = String(s).split("|");
  if (parts.length !== 3) return null;
  return { src: parts[0], type: parts[1], dst: parts[2] };
}

function addEdge(s) {
  const e = parseEdge(s);
  if (!e) return null;
  const a = state.nodes.get(e.src), b = state.nodes.get(e.dst);
  if (!a || !b) { console.warn("edge end not seen yet:", s); return null; }
  const id = `${a.cyId}|${e.type}|${b.cyId}`;
  if (!state.cy.getElementById(id).length) {
    state.cy.add({ group: "edges", data: { id, source: a.cyId, target: b.cyId, type: e.type }, classes: "fresh" });
  }
  return state.cy.getElementById(id);
}

function cyNode(label, id) { return state.cy.getElementById(cyId(label, id)); }

let layoutTimer = null;
function relayout() {
  clearTimeout(layoutTimer);
  layoutTimer = setTimeout(() => {
    const n = state.cy.nodes().length;
    $("#graph-empty").hidden = n > 0;
    if (!n) return;
    state.cy.layout({
      name: "cose", animate: true, animationDuration: 450, randomize: false, fit: true, padding: 40,
      nodeRepulsion: () => 9000, idealEdgeLength: () => 70, edgeElasticity: () => 80, gravity: 0.35,
      numIter: 900, nodeDimensionsIncludeLabels: true,
    }).run();
  }, 60);
}

function onTouched(data) {
  state.cy.elements(".fresh").removeClass("fresh");
  (data.nodes || []).forEach(addNode);
  const added = (data.edges || []).map(addEdge).filter(Boolean);
  (data.nodes || []).forEach((n) => cyNode(n.label, n.id).addClass("fresh"));
  added.forEach((e) => e.addClass("fresh"));
  const c = state.cy;
  $("#graph-status").textContent = `${c.nodes().length} nodes · ${c.edges().length} edges touched`;
  relayout();
}

function onAnswerPath(data) {
  const path = data.path || {};
  (path.nodes || []).forEach(addNode);
  const edges = (path.edges || []).map(addEdge).filter(Boolean);
  state.cy.elements().removeClass("fresh");
  (path.nodes || []).forEach((n) => cyNode(n.label, n.id).addClass("path"));
  edges.forEach((e) => { e.addClass("path"); e.source().addClass("path"); e.target().addClass("path"); });
  state.cy.elements().not(".path").addClass("faded");
  for (const item of data.labels || []) {
    if (item.node) cyNode(item.node.label, item.node.id).addClass(item.tag);
    if (item.edge) { const e = parseEdge(item.edge); const a = e && state.nodes.get(e.src), b = e && state.nodes.get(e.dst);
      if (a && b) state.cy.getElementById(`${a.cyId}|${e.type}|${b.cyId}`).addClass(item.tag); }
  }
  const c = state.cy;
  $("#graph-status").textContent = `${c.nodes(".path").length} of ${c.nodes().length} nodes on the answer path`;
  relayout();
}

function focusNode(label, id) {
  const n = cyNode(label, id);
  state.cy.nodes(".focus").removeClass("focus");
  if (!n.length) return;
  n.addClass("focus");
  // centre the node in the part of the graph the source panel does not cover
  const cy = state.cy, panel = $("#source");
  const covered = panel.hidden || cy.width() < 700 ? 0 : panel.offsetWidth + 10;
  const zoom = Math.max(cy.zoom(), 1.1), pos = n.position();
  cy.animate({ zoom, pan: { x: (cy.width() - covered) / 2 - pos.x * zoom, y: cy.height() / 2 - pos.y * zoom } }, { duration: 300 });
}

// ---- answer rendering --------------------------------------------------------------------------

const INLINE = /(\[\[[^\]]+\]\]|\$[^$\n]+\$|\*\*[^*]+\*\*|`[^`]+`)/g;

function renderInline(text) {
  return text.split(INLINE).map((tok, i) => {
    if (i % 2 === 0) return esc(tok);
    if (tok.startsWith("[[")) return chipHtml(tok.slice(2, -2));
    if (tok.startsWith("$")) return tex(tok.slice(1, -1));
    if (tok.startsWith("**")) return `<strong>${renderInline(tok.slice(2, -2))}</strong>`;
    return `<code>${esc(tok.slice(1, -1))}</code>`;
  }).join("");
}

function chipHtml(ref) {
  const cut = ref.indexOf(":");
  if (cut < 1) return esc(`[[${ref}]]`);
  const label = ref.slice(0, cut).trim(), id = ref.slice(cut + 1).trim();
  const cite = state.citations.get(`${label}::${id}`);
  const node = state.nodes.get(id);
  const text = cite?.text || node?.name || shortName(label, id);
  const page = cite?.page ?? node?.page;
  return `<button type="button" class="chip" style="--dot:${labelColor(label)}" data-label="${esc(label)}" data-id="${esc(id)}"`
    + ` title="${esc(label)} ${esc(id)}">${esc(text)}${page ? ` <span class="pg">p.${esc(page)}</span>` : ""}</button>`;
}

function renderMarkdown(text, streaming) {
  let src = text;
  if (streaming) src = src.replace(/\[\[[^\]]*$/, "");  // hide a citation marker still arriving
  const blocks = src.split(/\n{2,}/);
  return blocks.map((block) => {
    const lines = block.split("\n").filter((l) => l.trim() !== "");
    if (!lines.length) return "";
    if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
      return "<ul>" + lines.map((l) => `<li>${renderInline(l.replace(/^\s*[-*]\s+/, ""))}</li>`).join("") + "</ul>";
    }
    return `<p>${lines.map(renderInline).join("<br>")}</p>`;
  }).join("");
}

// ---- chat --------------------------------------------------------------------------------------

function scrollChat() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }

function addUserMessage(text, rerun) {
  $("#empty").hidden = true;
  const m = el("div", "msg user");
  if (rerun) m.append(el("span", "rerun-tag", "Re-run with edited facts"));
  m.append(document.createTextNode(text));
  $("#messages").append(m);
  scrollChat();
}

function newAssistantMessage() {
  const m = el("div", "msg assistant");
  const steps = el("ol", "steps");
  const answer = el("div", "answer");
  const meta = el("div", "meta");
  m.append(steps, answer, meta);
  $("#messages").append(m);
  return { m, steps, answer, meta, text: "", stepEls: new Map() };
}

function argsPreview(args) {
  const s = JSON.stringify(args ?? {});
  return s.length > 110 ? s.slice(0, 107) + "…" : s;
}

function setBusy(busy) {
  state.busy = busy;
  $("#send").disabled = busy;
  document.querySelectorAll(".fact input").forEach((i) => { i.disabled = busy; });
}

async function ask(question, opts = {}) {
  if (state.busy || !question.trim()) return;
  setBusy(true);
  state.lastQuestion = question;
  addUserMessage(question, opts.rerun);
  resetGraph();
  closeSource();
  const msg = newAssistantMessage();
  const body = { question, session_id: state.sessionId, graph: state.graph, rerun: !!opts.rerun, mock: state.forceMock };
  if (opts.facts) body.facts = opts.facts;
  try {
    const res = await fetch("/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok || !res.body) throw new Error(`POST /chat failed: ${res.status} ${await res.text()}`);
    const mode = res.headers.get("X-Chat-Mode");
    if (mode) { const b = $("#mode"); b.hidden = false; b.textContent = mode; b.className = "mode " + mode; }
    if (!state.graph && res.headers.get("X-Code-Graph")) state.graph = res.headers.get("X-Code-Graph");
    $("#graph-name").textContent = state.graph || "";
    await readSSE(res.body, (event, data) => handleEvent(msg, event, data));
  } catch (err) {
    msg.answer.append(el("div", "error", esc(err.message || String(err))));
  } finally {
    msg.answer.classList.remove("streaming");
    msg.stepEls.forEach((s) => { s.classList.remove("running"); s.classList.add("done"); });
    setBusy(false);
    scrollChat();
  }
}

async function readSSE(stream, onEvent) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      let event = "message";
      const data = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (!data.length) continue;
      let parsed;
      try { parsed = JSON.parse(data.join("\n")); } catch { parsed = { text: data.join("\n") }; }
      onEvent(event, parsed);
    }
  }
}

function handleEvent(msg, event, data) {
  switch (event) {
    case "tool_call": {
      msg.stepEls.forEach((s) => { s.classList.remove("running"); s.classList.add("done"); });
      const li = el("li", "step running",
        `<span class="tool">${esc(data.name)}</span> <span class="args">${esc(argsPreview(data.args))}</span>`);
      msg.steps.append(li);
      msg.stepEls.set(data.id ?? data.name, li);
      break;
    }
    case "touched": {
      onTouched(data);
      const li = msg.stepEls.get(data.tool_call_id) || [...msg.stepEls.values()].pop();
      if (li) {
        li.classList.remove("running"); li.classList.add("done");
        if (data.summary) li.append(el("span", "summary", esc(data.summary)));
      }
      break;
    }
    case "answer_delta":
      msg.text += data.text ?? "";
      msg.answer.classList.add("streaming");
      msg.answer.innerHTML = renderMarkdown(msg.text, true);
      break;
    case "answer_done": {
      for (const c of data.citations || []) state.citations.set(`${c.label}::${c.id}`, c);
      onAnswerPath(data);
      msg.answer.classList.remove("streaming");
      msg.answer.innerHTML = renderMarkdown(msg.text, false);
      for (const w of data.warnings || []) msg.answer.append(el("div", "warning", `Check: ${esc(w)}`));
      msg.meta.textContent = costLine(data.cost);
      break;
    }
    case "facts":
      setFacts(data.facts || []);
      break;
    case "error":
      msg.answer.append(el("div", "error", esc(data.message || "Something went wrong")));
      if (data.code === "session_graph") newChat();  // the next question starts a new chat on this graph
      break;
    default:
      console.debug("unhandled event", event, data);
  }
  scrollChat();
}

function costLine(cost) {
  if (!cost) return "";
  const parts = [];
  if (cost.tool_calls != null) parts.push(`${cost.tool_calls} tool calls`);
  if (cost.input_tokens) parts.push(`${(cost.input_tokens / 1000).toFixed(1)}k in / ${(cost.output_tokens / 1000).toFixed(1)}k out`);
  if (cost.usd) parts.push(`$${cost.usd.toFixed(4)}`);
  if (cost.model) parts.push(cost.model);
  return parts.join(" · ");
}

// ---- facts panel -------------------------------------------------------------------------------

const SOURCE_TEXT = { user: "you", assumed: "assumed", matched_row: "row", from_clause: "clause",
  agent_selected: "agent" };
const SOURCE_TITLE = { user: "from you", assumed: "an assumed typical value you accepted", matched_row: "matched table row",
  from_clause: "stated in a clause", agent_selected: "selected by the agent" };
const FACT_GROUPS = [
  ["you", "From you"], ["code", "From the code"], ["agent", "Selected by agent"], ["intermediate", "Intermediate values"],
];
const paramInfo = new Map();  // `${graph}|${id}` -> {inGraph, latex, symbol} from the code graph

// Facts from a restored session carry no LaTeX: ask the code graph whether each id is a
// parameter and what its LaTeX and symbol are (cached per graph).
async function lookupParams(facts) {
  const todo = facts.filter((f) => !f.latex && !paramInfo.has(`${state.graph}|${f.param_id}`));
  if (!todo.length || !state.graph) return false;
  await Promise.all(todo.map(async (f) => {
    const key = `${state.graph}|${f.param_id}`;
    try {
      const res = await fetch(`/node/Parameter/${encodeURIComponent(f.param_id)}?graph=${encodeURIComponent(state.graph)}`);
      if (!res.ok) { paramInfo.set(key, { inGraph: false }); return; }
      const d = await res.json();
      paramInfo.set(key, { inGraph: true, latex: d.latex || null, symbol: (d.props && d.props.symbol) || d.title || null });
    } catch { paramInfo.set(key, { inGraph: false }); }
  }));
  return true;
}

function factInfo(f) {
  const info = paramInfo.get(`${state.graph}|${f.param_id}`) || {};
  return { latex: f.latex || info.latex || null, inGraph: Boolean(f.latex) || info.inGraph, symbol: info.symbol || f.symbol };
}

function factGroup(f, info) {
  if (f.source === "user" || f.source === "assumed") return "you";
  if (f.source === "matched_row" || f.source === "from_clause") return "code";
  // a unitless agent value whose id is no parameter of the code: a helper saved for one step (e.g. interpolation bounds)
  if (info.inGraph === false && !f.unit) return "intermediate";
  return "agent";
}

function factName(f, info) {
  if (info.latex) return `<span class="fact-tex">${tex(info.latex)}</span>`;
  const sym = info.symbol && info.symbol !== f.param_id ? info.symbol : f.param_id;
  return `<span class="fact-plain">${esc(String(sym).replace(/_/g, " "))}</span>`;
}

function sigDigits(v) {
  const s = Math.abs(v).toString();
  if (s.includes("e")) return 99;
  return s.replace(".", "").replace(/^0+/, "").length;
}

// At most 4 significant digits (1.14982 -> 1.150, 0.599747 -> 0.5997); exact values stay as they are.
function fmtFact(v) {
  if (typeof v !== "number" || !Number.isFinite(v)) return String(v ?? "");
  if (Number.isInteger(v) && Math.abs(v) < 1e4) return String(v);
  if (sigDigits(v) <= 4) return String(v);
  const s = v.toPrecision(4);
  return s.includes("e") ? Number(s).toLocaleString("en-US") : s;
}

function setFacts(facts) {
  state.facts = facts.map((f) => ({ ...f }));
  for (const f of state.facts) if (!(f.param_id in state.baseFacts)) state.baseFacts[f.param_id] = f.value;
  renderFacts();
  lookupParams(state.facts).then((fetched) => { if (fetched) renderFacts(); });
}

function factRow(f, info) {
  const row = el("div", "fact");
  if (state.baseFacts[f.param_id] !== undefined && String(state.baseFacts[f.param_id]) !== String(f.value)) row.classList.add("changed");
  const full = `${f.value ?? ""}${f.unit ? " " + f.unit : ""}`;
  row.title = `${f.param_id} = ${full}${f.note ? ` · ${f.note}` : ""}`;
  row.append(el("span", "fact-name", factName(f, info)));
  const val = el("span", "fact-val");
  const input = el("input");
  const shown = fmtFact(f.value);
  const fit = (text) => { input.style.width = `${Math.max(3, String(text).length) + 1.5}ch`; };
  input.value = shown;
  fit(shown);
  input.title = full;
  input.disabled = state.busy;
  input.setAttribute("aria-label", `Value of ${f.symbol || f.param_id}`);
  input.addEventListener("focus", () => { input.value = f.value ?? ""; fit(input.value); input.select(); });
  input.addEventListener("blur", () => { if (String(input.value).trim() === String(f.value ?? "")) { input.value = shown; fit(shown); } });
  input.addEventListener("input", () => fit(input.value));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") input.blur();
    if (e.key === "Escape") { input.value = f.value ?? ""; input.blur(); }
  });
  input.addEventListener("change", () => editFact(f.param_id, input.value));
  val.append(input);
  if (f.unit) val.append(el("span", "unit", esc(f.unit)));
  row.append(val);
  const tag = el(f.node ? "button" : "span", `tag ${f.source}`, esc(SOURCE_TEXT[f.source] || f.source));
  tag.title = SOURCE_TITLE[f.source] || f.source;
  if (f.node) {
    tag.type = "button";
    tag.addEventListener("click", () => { focusNode(f.node.label, f.node.id); openSource(f.node.label, f.node.id); });
  }
  row.append(tag);
  return row;
}

function renderFacts() {
  const list = $("#facts-list");
  list.innerHTML = "";
  const n = state.facts.length;
  $("#facts-count").textContent = n ? `(${n})` : "";
  $("#facts-hint").textContent = n
    ? "Edit a value and press Enter to re-run the last question."
    : "Facts from your question appear here. Edit a value to re-run.";
  const groups = Object.fromEntries(FACT_GROUPS.map(([id]) => [id, []]));
  for (const f of state.facts) {
    const info = factInfo(f);
    groups[factGroup(f, info)].push([f, info]);
  }
  for (const [id, name] of FACT_GROUPS) {
    const items = groups[id];
    if (!items.length) continue;
    const box = el(id === "intermediate" ? "details" : "div", `fact-group g-${id}`);
    const head = el(id === "intermediate" ? "summary" : "div", "fact-group-h", `${name} <span>${items.length}</span>`);
    box.append(head);
    for (const [f, info] of items) box.append(factRow(f, info));
    list.append(box);
  }
}

function initFactsToggle() {
  const panel = $("#facts"), btn = $("#facts-toggle");
  const apply = (collapsed) => {
    panel.classList.toggle("collapsed", collapsed);
    btn.setAttribute("aria-expanded", String(!collapsed));
  };
  apply(storage(() => localStorage.getItem("facts_collapsed") === "1", false));
  btn.addEventListener("click", () => {
    const collapsed = !panel.classList.contains("collapsed");
    apply(collapsed);
    storage(() => localStorage.setItem("facts_collapsed", collapsed ? "1" : "0"));
  });
}

function editFact(paramId, raw) {
  if (state.busy || !state.lastQuestion) return;
  const text = raw.trim();
  const num = Number(text.replace(/,/g, ""));
  const value = text !== "" && Number.isFinite(num) ? num : text;
  const facts = state.facts.map((f) => (f.param_id === paramId ? { ...f, value, source: "user", node: null, note: "edited in the facts panel" } : f));
  const old = state.facts.find((f) => f.param_id === paramId);
  if (old && String(old.value) === String(value)) return;
  state.facts = facts;
  renderFacts();
  ask(state.lastQuestion, { facts, rerun: true });
}

// ---- source panel ------------------------------------------------------------------------------

let sourceSeq = 0;

function closeSource() { $("#source").hidden = true; state.cy?.nodes(".focus").removeClass("focus"); }

async function openSource(label, id) {
  const seq = ++sourceSeq;
  const panel = $("#source"), body = $("#src-body");
  panel.hidden = false;
  $("#src-kind").innerHTML = `<i style="background:${labelColor(label)}"></i>${esc(label)}`;
  body.innerHTML = `<h2>${esc(state.nodes.get(id)?.name || id)}</h2><div class="skeleton"></div><div class="skeleton" style="width:70%"></div>`;
  focusNode(label, id);
  try {
    const q = state.graph ? `?graph=${encodeURIComponent(state.graph)}` : "";
    const res = await fetch(`/node/${encodeURIComponent(label)}/${encodeURIComponent(id)}${q}`);
    const data = await res.json();
    if (seq !== sourceSeq) return;
    if (!res.ok) throw new Error(data.detail || res.statusText);
    body.innerHTML = sourceHtml(data);
  } catch (err) {
    if (seq === sourceSeq) body.innerHTML = `<h2>${esc(id)}</h2><p class="src-error">Could not load this node: ${esc(err.message)}</p>`;
  }
}

function sourceHtml(d) {
  const out = [`<h2>${esc(d.title || d.id)}</h2>`];
  const pills = [];
  if (d.page) pills.push(`<span class="pill">p. ${esc(d.page)}${d.page_end && d.page_end !== d.page ? "–" + esc(d.page_end) : ""}</span>`);
  if (d.status) pills.push(`<span class="pill st-${esc(d.status)}">${esc(d.status)}</span>`);
  if (d.review_status) pills.push(`<span class="pill st-${esc(d.review_status)}">${esc(d.review_status)}</span>`);
  if (d.confidence != null) pills.push(`<span class="pill">conf ${esc(d.confidence)}</span>`);
  if (d.extraction_method) pills.push(`<span class="pill">${esc(d.extraction_method)}</span>`);
  if (d.pdf_url) pills.push(`<a class="pdf-link" href="${esc(d.pdf_url)}" target="_blank" rel="noopener">Open PDF at page ${esc(d.page)} ↗</a>`);
  out.push(`<div class="pills">${pills.join("")}</div>`);

  if (d.label === "Equation" && (d.crops.length || d.latex)) {
    out.push(`<div class="section-label">Crop vs. extracted LaTeX</div><div class="eq-compare">`
      + `<figure><figcaption>PDF crop</figcaption>${d.crops.map((u) => `<img class="crop" src="${esc(u)}" alt="Crop of ${esc(d.id)}">`).join("") || "<span>no crop</span>"}</figure>`
      + `<figure><figcaption>LaTeX (KaTeX)</figcaption><div class="tex">${d.latex ? tex(d.latex, true) : "not extracted"}</div></figure></div>`);
    if (d.sympy) out.push(`<div class="sympy">SymPy: ${esc(d.sympy)}</div>`);
  } else if (d.latex && d.label === "Parameter") {
    out.push(`<div class="tex" style="font-size:18px;margin:4px 0 8px">${tex(d.latex, false)}</div>`);
  }

  if (d.text && !(d.label === "Equation" && d.latex)) {
    out.push(`<div class="section-label">${d.label === "Clause" ? "Clause text" : "Text"}</div><p class="src-text">${esc(d.text)}</p>`);
  } else if (d.text) {
    out.push(`<div class="section-label">Text layer</div><p class="src-text">${esc(d.text)}</p>`);
  }

  if (d.flags && d.flags.length) {
    out.push(`<div class="section-label">Flags</div><ul class="flags">${d.flags.map((f) =>
      `<li class="sev-${esc(f.severity || "medium")}"><b>${esc(f.reason || "flag")}</b>${f.severity ? ` · ${esc(f.severity)}` : ""}${f.detail ? `<br>${esc(f.detail)}` : ""}</li>`).join("")}</ul>`);
  }

  if (d.where && d.where.length) {
    out.push(`<div class="section-label">Where</div><dl class="where">${d.where.map((w) =>
      `<dt>${esc(w.symbol)}</dt><dd>${esc(w.definition)}${w.unit ? ` (${esc(w.unit)})` : ""}</dd>`).join("")}</dl>`);
  }

  if (d.label !== "Equation" && d.crops && d.crops.length) {
    out.push(`<div class="section-label">${d.label === "Row" ? "Table crop" : "Crop"}</div><div class="crops">${d.crops.map((u) =>
      `<img class="crop" src="${esc(u)}" alt="Crop of ${esc(d.id)}" loading="lazy">`).join("")}</div>`);
  }

  if (d.rows && d.rows.length) {
    out.push(`<div class="section-label">${d.label === "Row" ? "Row" : "Rows"}</div><table class="rows">${d.rows.map((r) =>
      `<tr class="${state.nodes.has(r.id) && d.label === "Table" ? "hit" : ""}">${(r.cells || []).map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</table>`);
    const conds = d.rows[0].conditions;
    if (d.label === "Row" && conds && conds.length) out.push(`<p class="src-text" style="margin-top:6px">Conditions: ${esc(conds.join("; "))}</p>`);
  }

  if (d.related && d.related.length) {
    const rel = d.related.filter((r) => !["NEXT"].includes(r.type)).slice(0, 40);
    out.push(`<div class="section-label">Connected (${d.related.length})</div><div class="related">${rel.map((r) =>
      `<button type="button" class="chip" style="--dot:${labelColor(r.label)}" data-label="${esc(r.label)}" data-id="${esc(r.id)}">`
      + `<span class="rtype">${r.dir === "out" ? "→" : "←"} ${esc(r.type)}</span> ${esc(r.name || r.id)}</button>`).join("")}</div>`);
  }
  return out.join("");
}

// ---- wiring ------------------------------------------------------------------------------------

// One chat session per code graph: a session is tied to the graph it started on.
function sessionKey() { return `session_id:${state.graph || ""}`; }
function makeId() { return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2); }
function initSession() {
  state.sessionId = storage(() => sessionStorage.getItem(sessionKey()), null) || makeId();
  storage(() => sessionStorage.setItem(sessionKey(), state.sessionId));
}
function newChat() {
  state.sessionId = makeId();
  storage(() => sessionStorage.setItem(sessionKey(), state.sessionId));
}

async function initExample() {
  const btn = $("#example");
  try {
    const res = await fetch("/chat/example");
    const data = await res.json();
    btn.textContent = data.question;
  } catch { btn.hidden = true; }
  btn.addEventListener("click", () => ask(btn.textContent));
}

async function restoreSession(id) {
  let data;
  try {
    const res = await fetch(`/sessions/${encodeURIComponent(id)}`);
    data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
  } catch (err) {
    $("#empty").hidden = true;
    $("#messages").append(el("div", "error", `Could not reopen this chat: ${esc(err.message)}`));
    return;
  }
  state.sessionId = data.id;
  if (data.graph) state.graph = data.graph;
  storage(() => sessionStorage.setItem(sessionKey(), data.id));
  if (data.graph) { state.graph = data.graph; $("#graph-name").textContent = data.graph; }
  if (!data.messages.length) return;
  $("#empty").hidden = true;
  for (const m of data.messages) {
    if (m.role === "user") addUserMessage(m.text);
    else { const msg = newAssistantMessage(); msg.answer.innerHTML = renderMarkdown(m.text || "", false); }
  }
  state.lastQuestion = data.last_question;
  state.baseFacts = {};
  setFacts(data.facts || []);
  const note = el("div", "meta", "Reopened from the memory graph. The graph view fills in on the next question.");
  $("#messages").append(note);
  scrollChat();
}

// Back to the code page this chat belongs to, named by the code's designation.
async function setBackLink() {
  const link = $("#back-code");
  if (!state.graph || !link) return;
  link.href = `/code/${encodeURIComponent(state.graph)}`;
  link.hidden = false;
  try {
    const c = await (await fetch(`/codes/${encodeURIComponent(state.graph)}`)).json();
    link.textContent = `← ${c.designation || c.edition || c.title || state.graph}`;
  } catch { link.textContent = `← ${state.graph}`; }
}

function init() {
  initSession();
  const params = new URLSearchParams(location.search);
  const reopen = params.get("session");
  if (reopen) restoreSession(reopen).then(setBackLink);
  else {
    if (params.has("new")) newChat();  // from the code page's New chat: never reuse the tab's last session
    setBackLink();
  }
  initTheme();
  initGraph();
  initFactsToggle();
  initExample();
  if (state.graph) $("#graph-name").textContent = state.graph;
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#question");
    const text = q.value;
    if (!text.trim() || state.busy) return;
    q.value = "";
    state.baseFacts = {};
    ask(text);
  });
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  document.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip[data-id]");
    if (chip) openSource(chip.dataset.label, chip.dataset.id);
  });
  $("#src-close").addEventListener("click", closeSource);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#source").hidden) closeSource(); });
}

document.addEventListener("DOMContentLoaded", init);
