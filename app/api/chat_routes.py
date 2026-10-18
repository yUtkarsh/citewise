"""Chat page routes: POST /chat (Server-Sent Events), GET /node, GET /crop, GET /pdf.

The event contract is in web/EVENTS.md. POST /chat streams from the agent's
``app.agent.stream.stream_chat`` when it exists, else replays a built-in mock question.

Standalone run (with the ingest routes and web/): uvicorn app.api.web_app:app --reload
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import ROOT
from app.graph.connection import get_db
from app.graph.schema import NODE_LABELS, graph_name

DATA = ROOT / "data"
CROPS = DATA / "crops"
WEB = ROOT / "web"
RELATED_LIMIT = 60

router = APIRouter()


# ---- POST /chat ------------------------------------------------------------------------------

class Fact(BaseModel):
    param_id: str
    symbol: str | None = None
    latex: str | None = None
    value: int | float | str | None = None
    unit: str | None = None
    source: Literal["user", "matched_row", "assumed", "agent_selected", "from_clause"] = "user"
    note: str | None = None
    node: dict | None = None


class ChatRequest(BaseModel):
    question: str
    session_id: str | None = None
    graph: str | None = None
    facts: list[Fact] | None = None
    rerun: bool = False
    mock: bool = False


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _agent_stream():
    try:
        from app.agent.stream import stream_chat  # Phase 4; see web/EVENTS.md "Agent hook"
    except ImportError:
        return None
    return stream_chat


async def _iterate(gen) -> AsyncIterator[dict]:
    """Accept a sync or async generator of {"event", "data"} dicts."""
    if inspect.isasyncgen(gen):
        async for item in gen:
            yield item
        return
    sentinel = object()
    while (item := await asyncio.to_thread(next, gen, sentinel)) is not sentinel:
        yield item


async def _frames(events: AsyncIterator[dict], request: Request) -> AsyncIterator[str]:
    try:
        async for item in events:
            if await request.is_disconnected():
                return
            yield sse(item["event"], item.get("data", {}))
    except Exception as exc:  # the page shows it and stops
        yield sse("error", {"message": f"{type(exc).__name__}: {exc}"})


@router.post("/chat")
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    stream_chat = None if body.mock else _agent_stream()
    facts = [f.model_dump() for f in body.facts] if body.facts is not None else None
    if stream_chat is None:
        mode, graph = "mock", MOCK_GRAPH
        events = mock_stream(body.question, facts, body.rerun, session_id=body.session_id)
    else:
        mode, graph = "agent", body.graph or os.getenv("CODE_GRAPH")
        events = _iterate(stream_chat(body.question, session_id=body.session_id, graph=graph,
                                      facts=facts, rerun=body.rerun))
    headers = {"X-Chat-Mode": mode, "X-Code-Graph": graph or "", "Cache-Control": "no-cache",
               "X-Accel-Buffering": "no"}
    return StreamingResponse(_frames(events, request), media_type="text/event-stream", headers=headers)


@router.get("/chat/example")
def chat_example() -> dict:
    """The example question shown on the empty chat (the mock replay's question)."""
    return {"question": MOCK_QUESTION, "graph": MOCK_GRAPH}


# ---- GET /node -------------------------------------------------------------------------------

def _graph(name: str | None):
    name = name or os.getenv("CODE_GRAPH")
    if not name:
        raise HTTPException(400, "pass ?graph=<code graph name> or set CODE_GRAPH")
    if not name.startswith("code_"):
        raise HTTPException(400, "only code graphs can be read here")
    return name, get_db().select_graph(name)


def _crop_url(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path).as_posix()
    prefix = "data/crops/"
    return "/crop/" + (p[len(prefix):] if p.startswith(prefix) else p.lstrip("/"))


def _json_list(value) -> list:
    """Lists are stored either natively or as JSON strings; flags as lists of JSON strings."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return value if isinstance(value, list) else [value]


def _flags(props: dict) -> list[dict]:
    out = []
    for f in _json_list(props.get("flags")):
        if isinstance(f, str):
            try:
                f = json.loads(f)
            except ValueError:
                f = {"reason": f}
        out.append(f if isinstance(f, dict) else {"reason": str(f)})
    return out


def _display_name(label: str, props: dict) -> str:
    if label == "Clause":
        return f"{props.get('number') or props.get('id')} {props.get('title') or ''}".strip()
    return str(props.get("symbol") or props.get("caption") or props.get("title") or props.get("id"))


def _rows(graph, query: str, params: dict) -> list:
    return graph.ro_query(query, params, timeout=5000).result_set


@router.get("/node/{label}/{node_id:path}")
def node(label: str, node_id: str, graph: str | None = Query(None)) -> dict:
    if label not in NODE_LABELS:
        raise HTTPException(404, f"unknown label {label!r}")
    name, g = _graph(graph)
    found = _rows(g, f"MATCH (n:{label} {{id: $id}}) RETURN properties(n)", {"id": node_id})
    if not found:
        raise HTTPException(404, f"{label} {node_id!r} not in {name}")
    props = dict(found[0][0])
    related = [{"dir": d, "type": t, "label": (ls or ["?"])[0], "id": p.get("id"), "name": _display_name((ls or ["?"])[0], p)}
               for d, t, ls, p in _rows(g, (
                   f"MATCH (n:{label} {{id: $id}})-[r]-(m) "
                   "RETURN CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END, type(r), labels(m), properties(m) "
                   f"LIMIT {RELATED_LIMIT}"), {"id": node_id})]
    out: dict[str, Any] = {
        "label": label, "id": node_id, "title": _display_name(label, props),
        "page": props.get("page"), "page_end": props.get("page_end"),
        "status": props.get("status"), "review_status": props.get("review_status"),
        "confidence": props.get("confidence"), "extraction_method": props.get("extraction_method"),
        "text": None, "latex": props.get("latex"), "sympy": props.get("sympy"),
        "crops": [], "pdf_url": f"/pdf/{name}#page={props['page']}" if props.get("page") else None,
        "flags": _flags(props), "where": [], "rows": [], "related": related,
    }
    _add_details(g, label, node_id, props, out)
    skip = {"text", "text_corrected", "raw_spans", "regions", "suspect_spans", "cells", "values", "flags"}
    out["props"] = {k: v for k, v in props.items() if k not in skip and k not in out}
    return out


def _add_details(g, label: str, node_id: str, props: dict, out: dict) -> None:
    if label == "Clause":
        out["text"] = props.get("text_corrected") or props.get("text")
        for kind, p in _rows(g, ("MATCH (:Clause {id: $id})-[r:HAS_FLAG|HAS_CORRECTION]->(x) "
                                 "RETURN type(r), properties(x)"), {"id": node_id}):
            if kind == "HAS_FLAG":
                out["flags"].append({"reason": "flagged_line", "severity": "low",
                                     "detail": f"p.{p.get('page')}: {p.get('text')}"})
            else:
                out["flags"].append({"reason": "symbol_correction", "severity": "medium" if p.get("uncertain") else "low",
                                     "detail": f"{p.get('raw')} → {p.get('corrected')}"})
    elif label == "Equation":
        out["text"] = props.get("raw_text") or props.get("source_quote")
        out["crops"] = [u for u in [_crop_url(props.get("crop"))] if u]
        out["where"] = [{"symbol": s, "definition": d, "unit": u} for s, d, u in _rows(
            g, "MATCH (:Equation {id: $id})-[:WHERE]->(w) RETURN w.symbol, w.definition, w.unit ORDER BY w.id",
            {"id": node_id})]
    elif label == "Table":
        out["text"] = props.get("caption")
        out["crops"] = [_crop_url(c) for c in _json_list(props.get("crops")) if c]
        out["rows"] = [{"id": rid, "cells": _json_list(cells)} for rid, cells in _rows(
            g, "MATCH (:Table {id: $id})-[h:HAS_ROW]->(r) RETURN r.id, r.cells ORDER BY h.index", {"id": node_id})]
    elif label == "Row":
        out["text"] = " | ".join(str(c) for c in _json_list(props.get("cells")) if c)
        out["rows"] = [{"id": node_id, "cells": _json_list(props.get("cells")),
                        "conditions": _json_list(props.get("conditions")), "values": _json_list(props.get("values"))}]
        tables = _rows(g, "MATCH (t:Table)-[:HAS_ROW]->(:Row {id: $id}) RETURN t.crops", {"id": node_id})
        out["crops"] = [_crop_url(c) for c in _json_list(tables[0][0] if tables else None) if c]
    elif label == "Figure":
        out["text"] = props.get("caption")
        out["crops"] = [u for u in [_crop_url(props.get("crop"))] if u]
    elif label in ("Parameter", "Term"):
        out["text"] = props.get("description") or props.get("definition") or props.get("source_quote")
    elif label == "WhereItem":
        out["text"] = props.get("definition")
    else:
        out["text"] = props.get("text") or props.get("source_quote")


# ---- GET /crop, GET /pdf ---------------------------------------------------------------------

def _inside(base: Path, rel: str) -> Path:
    path = (base / rel).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise HTTPException(404, "not found")
    return path


@router.get("/crop/{path:path}")
def crop(path: str) -> FileResponse:
    return FileResponse(_inside(CROPS, path), headers={"Cache-Control": "max-age=3600"})


@router.get("/pdf/{graph}")
def pdf(graph: str) -> FileResponse:
    for folder in (DATA / "uploads", DATA, ROOT / "private_fixtures"):
        for path in sorted(folder.glob("*.pdf")):
            if graph_name(path) == graph:
                return FileResponse(path, media_type="application/pdf")
    raise HTTPException(404, f"no stored PDF for {graph}")


# ---- mock replay -----------------------------------------------------------------------------
# A realistic run of the agent on FEMA 356 Equation 3-10 (ids and pages are the real graph's).
# Only the values are mock: the arithmetic below stands in for the agent's `compute` tool.

MOCK_GRAPH = "code_fema356"
MOCK_QUESTION = ("What is the pseudo lateral load V for a 4-story reinforced concrete moment frame by the "
                 "Linear Static Procedure? W = 2,400 kips, S_a = 0.9 g, T = 0.8 s, T_S = 0.6 s, and the "
                 "stability coefficient θ is 0.06 in every story.")
MOCK_FACTS = [
    {"param_id": "W", "symbol": "W", "latex": "W", "value": 2400, "unit": "kips", "source": "user"},
    {"param_id": "S_a", "symbol": "S_a", "latex": "S_a", "value": 0.9, "unit": "g", "source": "user"},
    {"param_id": "T_period", "symbol": "T", "latex": "T", "value": 0.8, "unit": "s", "source": "user"},
    {"param_id": "T_S", "symbol": "T_S", "latex": "T_S", "value": 0.6, "unit": "s", "source": "user"},
    {"param_id": "num_stories", "symbol": "stories", "latex": r"\text{stories}", "value": 4, "unit": None, "source": "user"},
    {"param_id": "theta_i", "symbol": "θ", "latex": r"\theta_i", "value": 0.06, "unit": None, "source": "user"},
]
EQ, CL = "Equation 3-10", "3.3.1.3.1"


def _n(label: str, nid: str, name: str | None = None, page: int | None = None) -> dict:
    return {k: v for k, v in {"label": label, "id": nid, "name": name, "page": page}.items() if v is not None}


def _num(facts: dict, key: str) -> float:
    return float(facts[key]["value"])


def _mock_compute(facts: dict) -> dict:
    """Stand-in for `compute`: the Eq 3-10 where-item rules and the Table 3-1 row match."""
    T, Ts, theta = _num(facts, "T_period"), _num(facts, "T_S"), _num(facts, "theta_i")
    if T >= Ts:
        c1, c1_rule = 1.0, ("Equation 3-10#w4", "1.0 for T ≥ T_S")
    elif T < 0.1:
        c1, c1_rule = 1.5, ("Equation 3-10#w3", "1.5 for T < 0.10 s")
    else:
        c1, c1_rule = 1.5 + (1.0 - 1.5) * (T - 0.1) / (Ts - 0.1), ("Equation 3-10#w4", "linear interpolation between 0.10 s and T_S")
    c3 = 1.0 if theta < 0.1 else 1.0 + 5 * (theta - 0.1) / T
    stories = _num(facts, "num_stories")
    if "C_m" in facts and facts["C_m"]["source"] == "user":
        cm, row = _num(facts, "C_m"), None
    elif T > 1.0:
        cm, row = 1.0, ("Table 3-1#r5", "note 1: T > 1.0 s, so C_m = 1.0")
    elif stories <= 2:
        cm, row = 1.0, ("Table 3-1#r3", "1–2 stories, concrete moment frame column")
    else:
        cm, row = 0.9, ("Table 3-1#r4", "3 or more stories, concrete moment frame column")
    v = c1 * 1.0 * c3 * cm * _num(facts, "S_a") * _num(facts, "W")
    return {"C_1": c1, "c1_rule": c1_rule, "C_2": 1.0, "C_3": c3, "C_m": cm, "row": row, "V": v}


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:.3g}"


def _factor(x: float) -> str:
    s = f"{x:.3g}"
    return s if "." in s else s + ".0"


def _same(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


_last_facts: dict[str | None, list[dict]] = {}  # mock only: previous facts per session, for the change line


async def mock_stream(question: str, facts_in: list[dict] | None, rerun: bool,
                      pace: float = 1.0, session_id: str | None = None) -> AsyncIterator[dict]:
    facts = {f["param_id"]: dict(f) for f in (facts_in or MOCK_FACTS)}
    before = {f["param_id"]: f for f in _last_facts.get(session_id, MOCK_FACTS)}
    prev = _mock_compute(before) if rerun else None
    r = _mock_compute(facts)
    if not (facts.get("C_m") and facts["C_m"]["source"] == "user") and r["row"]:
        facts["C_m"] = {"param_id": "C_m", "symbol": "C_m", "latex": "C_m", "value": r["C_m"], "unit": None,
                        "source": "matched_row", "note": f"Table 3-1, {r['row'][1]}",
                        "node": {"label": "Row", "id": r["row"][0]}}
    user_facts = [f for f in facts.values() if f["param_id"] != "C_m" or f["source"] == "user"]

    async def pause(seconds: float):
        await asyncio.sleep(seconds * pace)

    steps: list[tuple[str, dict, str, list[dict], list[str]]] = [
        ("find_entry", {"query": "pseudo lateral load V linear static procedure"},
         "Best match: parameter V, computed by Equation 3-10 in 3.3.1.3.1 (p.101)",
         [_n("Parameter", "V", "V", 101), _n("Equation", EQ, "Eq. 3-10", 101), _n("Clause", CL, "§3.3.1.3.1", 101)],
         [f"{EQ}|COMPUTES|V", f"{CL}|CONTAINS|{EQ}", f"{CL}|DEFINES|V"]),
        ("save_session_facts", {"facts": [{k: f[k] for k in ("param_id", "value", "unit")} for f in user_facts]},
         f"Saved {len(user_facts)} facts from the question", [], []),
        ("get_dependencies", {"parameter": "V"},
         "Equation 3-10 uses C_1, C_2, C_3, C_m, S_a, W; C_m is given by Table 3-1, C_2 by Table 3-3",
         [_n("Parameter", p, p, 101) for p in ("C_1", "C_2", "C_3", "C_m", "S_a", "W")]
         + [_n("Table", "Table 3-1", "Table 3-1", 102), _n("Table", "Table 3-3", "Table 3-3", 110),
            _n("Equation", "Equation 3-17", "Eq. 3-17", 110)],
         [f"{EQ}|USES|{p}" for p in ("C_1", "C_2", "C_3", "C_m", "S_a", "W")]
         + ["Table 3-1|GIVES|C_m", "Table 3-3|GIVES|C_2", "Equation 3-17|COMPUTES|C_3"]),
        ("get_clause", {"id": CL},
         "3.3.1.3.1 Pseudo Lateral Load, p.101: where-items set C_1, C_2 (1.0 for linear procedures), C_3 and C_m",
         [_n("WhereItem", f"{EQ}#w{i}", n, 101) for i, n in ((3, "C1 < 0.1 s"), (4, "C1 ≥ T_S"), (7, "C2 linear"),
                                                             (8, "C3 θ rule"), (9, "Cm rule"))]
         + [_n("Clause", "3.3.1.2", "§3.3.1.2 Period", 100), _n("Clause", "3.2.5.2", "§3.2.5.2 P-Δ", 96)],
         [f"{EQ}|WHERE|{EQ}#w{i}" for i in (3, 4, 7, 8, 9)]
         + [f"{CL}|REFERENCES|3.3.1.2", f"{CL}|REFERENCES|3.2.5.2", f"{CL}|REFERENCES|Table 3-1"]),
        ("applicable_clauses", {"parameters": ["C_1", "C_3", "C_m"], "facts": ["T_period", "T_S", "theta_i", "num_stories"]},
         f"C_1 = {_factor(r['C_1'])} ({r['c1_rule'][1]}); C_3 = {_factor(r['C_3'])}; "
         + (f"C_m matched row {r['row'][0]} ({r['row'][1]})" if r["row"] else "C_m given by the user"),
         ([_n("Row", r["row"][0], "3-1 row " + r["row"][0][-1], 102)] if r["row"] else [])
         + [_n("Row", "Table 3-1#r5", "3-1 note 1", 102)],
         ([f"Table 3-1|HAS_ROW|{r['row'][0]}"] if r["row"] else []) + ["Table 3-1|HAS_ROW|Table 3-1#r5"]),
        ("compute", {"equation": EQ, "inputs": {"C_1": r["C_1"], "C_2": 1.0, "C_3": r["C_3"], "C_m": r["C_m"],
                                               "S_a": _num(facts, "S_a"), "W": _num(facts, "W")}},
         f"V = {_fmt(r['V'])} kips (SymPy, Equation 3-10)", [_n("Parameter", "V", "V", 101)], [f"{EQ}|COMPUTES|V"]),
        ("trace", {"parameter": "V"}, "Path: 3.3.1.3.1 → Eq. 3-10 → V, with Table 3-1 for C_m", [], []),
    ]

    for i, (name, args, summary, nodes, edges) in enumerate(steps, 1):
        yield {"event": "tool_call", "data": {"id": f"t{i}", "name": name, "args": args}}
        await pause(0.55)
        yield {"event": "touched", "data": {"tool_call_id": f"t{i}", "summary": summary, "nodes": nodes, "edges": edges}}
        if name == "save_session_facts":
            yield {"event": "facts", "data": {"facts": user_facts}}
        if name == "applicable_clauses":
            yield {"event": "facts", "data": {"facts": list(facts.values())}}
        await pause(0.35)

    row_cite = f"[[Row:{r['row'][0]}]]" if r["row"] else "(your value)"
    lines = []
    if prev:
        changed = [f"{f['symbol']} {m['value']:,} → {f['value']:,}{' ' + f['unit'] if f.get('unit') else ''}"
                   for pid, m in before.items() if (f := facts.get(pid)) and f["source"] == "user"
                   and not _same(f["value"], m["value"])]
        effects = [f"V {_fmt(prev['V'])} → {_fmt(r['V'])} kips"] + [
            f"{k} {_factor(prev[k])} → {_factor(r[k])}" for k in ("C_1", "C_3", "C_m") if abs(prev[k] - r[k]) > 1e-9]
        lines.append(f"**Changed:** {', '.join(changed) or 'no inputs'}. {'; '.join(effects)}.\n\n")
    lines += [
        f"**V = {_fmt(r['V'])} kips**, from $V = C_1 C_2 C_3 C_m S_a W$ [[Equation:{EQ}]] in "
        f"§3.3.1.3.1 Pseudo Lateral Load [[Clause:{CL}]], p. 101.\n\n",
        "Factors, each from the graph:\n\n",
        f"- $C_1 = {_factor(r['C_1'])}$: {r['c1_rule'][1]} (T = {_fmt(_num(facts, 'T_period'))} s, "
        f"T_S = {_fmt(_num(facts, 'T_S'))} s) [[WhereItem:{r['c1_rule'][0]}]]\n",
        f"- $C_2 = 1.0$: taken as 1.0 for linear procedures [[WhereItem:{EQ}#w7]]\n",
        f"- $C_3 = {_factor(r['C_3'])}$: θ = {_fmt(_num(facts, 'theta_i'))} "
        + ("is below 0.1 in every story" if _num(facts, "theta_i") < 0.1 else "is 0.1 or more, so 1 + 5(θ − 0.1)/T")
        + f" [[WhereItem:{EQ}#w8]]\n",
        f"- $C_m = {_factor(r['C_m'])}$: matched row of Table 3-1 [[Table:Table 3-1]] {row_cite}"
        + (f", {r['row'][1]}" if r["row"] else "") + "\n",
        f"- $S_a$ = {_fmt(_num(facts, 'S_a'))} g and $W$ = {_fmt(_num(facts, 'W'))} kips: from you\n\n",
        f"Computed by `compute` in SymPy: {_factor(r['C_1'])} × 1.0 × {_factor(r['C_3'])} × {_factor(r['C_m'])} × "
        f"{_fmt(_num(facts, 'S_a'))} × {_fmt(_num(facts, 'W'))} = {_fmt(r['V'])} kips.\n\n",
        "Table 3-1 and Equation 3-10 are **unreviewed**: not yet approved in the review queue. "
        "Table 3-3 also gives C_2, but only for the nonlinear procedure, so it was not used.",
    ]
    for token in re.findall(r"\S+\s*", "".join(lines)):
        yield {"event": "answer_delta", "data": {"text": token}}
        await pause(0.018)

    path_nodes = [_n("Clause", CL), _n("Equation", EQ), _n("Parameter", "V"), _n("Table", "Table 3-1"),
                  *[_n("Parameter", p) for p in ("C_1", "C_2", "C_3", "C_m", "S_a", "W")],
                  _n("WhereItem", r["c1_rule"][0]), _n("WhereItem", f"{EQ}#w7"), _n("WhereItem", f"{EQ}#w8")]
    path_edges = [f"{CL}|CONTAINS|{EQ}", f"{EQ}|COMPUTES|V", "Table 3-1|GIVES|C_m", f"{CL}|REFERENCES|Table 3-1",
                  *[f"{EQ}|USES|{p}" for p in ("C_1", "C_2", "C_3", "C_m", "S_a", "W")],
                  f"{EQ}|WHERE|{r['c1_rule'][0]}", f"{EQ}|WHERE|{EQ}#w7", f"{EQ}|WHERE|{EQ}#w8"]
    if r["row"]:
        path_nodes.append(_n("Row", r["row"][0]))
        path_edges.append(f"Table 3-1|HAS_ROW|{r['row'][0]}")
    _last_facts[session_id] = list(facts.values())
    yield {"event": "facts", "data": {"facts": list(facts.values())}}
    yield {"event": "answer_done", "data": {
        "path": {"nodes": path_nodes, "edges": path_edges},
        "labels": [{"node": _n("Table", "Table 3-1"), "tag": "unreviewed", "note": "not yet approved in the review queue"},
                   {"node": _n("Equation", EQ), "tag": "unreviewed", "note": "not yet approved in the review queue"},
                   {"node": _n("Clause", CL), "tag": "flagged", "note": "one flagged line, one symbol correction"}],
        "citations": [{"label": "Equation", "id": EQ, "text": "Eq. 3-10", "page": 101},
                      {"label": "Clause", "id": CL, "text": "§3.3.1.3.1", "page": 101},
                      {"label": "Table", "id": "Table 3-1", "text": "Table 3-1", "page": 102}]
        + ([{"label": "Row", "id": r["row"][0], "text": f"Table 3-1 row {r['row'][0][-1]}", "page": 102}] if r["row"] else [])
        + [{"label": "WhereItem", "id": wid, "text": f"Eq. 3-10 where {sym}", "page": 101}
           for wid, sym in ((r["c1_rule"][0], "C1"), (f"{EQ}#w7", "C2"), (f"{EQ}#w8", "C3"))],
        "cost": {"usd": 0.0, "input_tokens": 0, "output_tokens": 0, "tool_calls": len(steps), "model": "mock"},
    }}


# ---- static page ---------------------------------------------------------------------------

def mount_web(target: FastAPI) -> None:
    """Serve web/ at "/". Call after every other route is registered."""
    target.mount("/", StaticFiles(directory=WEB, html=True), name="web")
