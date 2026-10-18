"""Review queue (SPEC Step 4): flagged Pass 2 results of one code, approve, and reject
with a note.

A queue item is one Pass 2 job: a Clause or a Table node that carries flags. Approve sets
``review_status: approved`` on the node (and the equations or rows its job wrote) and
approves the job's pinned answer, so it is never extracted again. Reject re-runs that one
job on MODEL_REVIEW with the reviewer note appended the way ``--hint`` appends it, and
keeps the new answer only if it passes the pipeline's own ``passes()`` check. This module
drives the existing Pass 2 pieces; it changes none of them.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import pymupdf

from app.config import ROOT
from app.graph.connection import get_db
from app.graph.schema import graph_name

router = APIRouter()

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
HINT_PREFIX = "\n\nReviewer note (from a human checking an earlier answer): "  # as run_pass2 adds --hint
MAX_REJECTIONS = 2  # SPEC Step 4: after two rejections an item is needs_manual
REVIEW_MAX_COST = 1.0  # USD, one reject


@dataclass
class Paths:
    """Where pins, job state and the call cache live; tests point these at a scratch folder."""
    jobs_dir: Path = field(default_factory=lambda: ROOT / "data" / "jobs")
    cache_dir: Path | None = None  # None: the pipeline's CACHE_DIR

    def pins(self, pdf_hash: str):
        from app.ingest.pass2.runner import PinStore
        return PinStore(self.jobs_dir / f"{pdf_hash[:12]}_pass2_pins")

    def state(self, pdf_hash: str) -> Path:
        return self.jobs_dir / f"{pdf_hash[:12]}_pass2.jsonl"


def _code_graph(name: str):
    if not name.startswith("code_") or name not in get_db().list_graphs():
        raise HTTPException(404, f"no code graph {name}")
    return get_db().select_graph(name)


def _rows(graph, query: str, params: dict | None = None) -> list:
    return graph.ro_query(query, params or {}, timeout=10000).result_set


def _flags(raw) -> list[dict]:
    out = []
    for f in raw if isinstance(raw, list) else ([raw] if raw else []):
        try:
            item = json.loads(f) if isinstance(f, str) else f
        except ValueError:
            item = {"reason": str(f)}
        out.append(item if isinstance(item, dict) else {"reason": str(item)})
    return out


def _severity(flags: list[dict]) -> str:
    return max((f.get("severity") or "low" for f in flags), key=lambda s: SEVERITY_RANK.get(s, 0), default="low")


def _crop_url(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path).as_posix()
    return "/crop/" + (p[len("data/crops/"):] if p.startswith("data/crops/") else p.lstrip("/"))


def _json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


# ---- queue -----------------------------------------------------------------------------------

def queue(graph) -> list[dict]:
    rows = _rows(graph, (
        "MATCH (n) WHERE (n:Clause OR n:Table) AND ((n.flags IS NOT NULL AND size(n.flags) > 0) "
        "OR n.review_rejections > 0) "
        "RETURN labels(n)[0], n.id, n.number, n.title, n.caption, n.page, n.status, n.review_status, n.flags, "
        "n.review_rejections, n.review_note, n.review_severity"))
    items = []
    for label, nid, number, title, caption, page, status, review, flags, rejections, note, was in rows:
        parsed = _flags(flags)
        if not parsed and was:  # re-extracted after a rejection: waits for approval at its old severity
            parsed = [{"reason": "re-extracted after rejection", "severity": was}]
        items.append({
            "job": f"{label.lower()}:{nid}", "label": label, "id": nid,
            "title": (f"{number} {title or ''}".strip() if label == "Clause" else caption) or nid,
            "page": page, "status": status, "review_status": review or "unreviewed",
            "severity": _severity(parsed), "reasons": sorted({f.get("reason") or "?" for f in parsed}),
            "flags": len(parsed), "rejections": rejections or 0, "note": note,
        })
    items.sort(key=lambda i: (-SEVERITY_RANK.get(i["severity"], 0), i["page"] or 0, i["id"]))
    return items


@router.get("/review/{name}")
def review_queue(name: str) -> dict:
    graph = _code_graph(name)
    items = queue(graph)
    doc = _rows(graph, "MATCH (d:Document) RETURN d.title, d.self_designation LIMIT 1")
    return {"graph": name, "title": doc[0][0] if doc else name, "designation": doc[0][1] if doc else None,
            "items": items}


# ---- one item ----------------------------------------------------------------------------------

def item_detail(graph, name: str, label: str, nid: str) -> dict:
    if label not in ("Clause", "Table"):
        raise HTTPException(404, "review items are clauses and tables")
    found = _rows(graph, f"MATCH (n:{label} {{id: $id}}) RETURN properties(n)", {"id": nid})
    if not found:
        raise HTTPException(404, f"{label} {nid!r} not found")
    p = dict(found[0][0])
    out = {"label": label, "id": nid, "job": f"{label.lower()}:{nid}", "page": p.get("page"),
           "page_end": p.get("page_end"), "status": p.get("status"), "review_status": p.get("review_status"),
           "review_note": p.get("review_note"), "rejections": p.get("review_rejections") or 0,
           "flags": _flags(p.get("flags")) or ([{"reason": "re-extracted after rejection", "severity": p["review_severity"],
                                                 "detail": "the new answer passes its checks; approve it or reject again"}]
                                               if p.get("review_severity") else []),
           "pdf_url": f"/pdf/{name}#page={p['page']}" if p.get("page") else None}
    if label == "Clause":
        out["title"] = f"{p.get('number')} {p.get('title') or ''}".strip()
        out["source"] = {"text": p.get("text_corrected") or p.get("text"), "raw_text": p.get("text")
                         if p.get("text_corrected") else None}
        out["extracted"] = clause_extracted(graph, nid)
    else:
        out["title"] = p.get("caption") or nid
        out["source"] = {"crops": [_crop_url(c) for c in _json(p.get("crops"), []) if c]}
        out["extracted"] = table_extracted(graph, nid)
    return out


def clause_extracted(graph, cid: str) -> dict:
    eqs = [{"id": i, "number": num, "latex": latex, "sympy": sympy, "crop": _crop_url(crop), "status": st,
            "raw_text": raw}
           for i, num, latex, sympy, crop, st, raw in _rows(graph, (
               "MATCH (:Clause {id: $id})-[:CONTAINS]->(e:Equation) "
               "RETURN e.id, e.number, e.latex, e.sympy, e.crop, e.status, e.raw_text ORDER BY e.id"), {"id": cid})]
    for e in eqs:
        e["computes"] = [r[0] for r in _rows(graph, "MATCH (:Equation {id: $id})-[:COMPUTES]->(p) RETURN p.id",
                                             {"id": e["id"]})]
        e["uses"] = [r[0] for r in _rows(graph, "MATCH (:Equation {id: $id})-[:USES]->(p) RETURN p.id ORDER BY p.id",
                                         {"id": e["id"]})]
    params = [{"id": i, "latex": latex, "description": d, "units": u} for i, latex, d, u in _rows(graph, (
        "MATCH (:Clause {id: $id})-[:DEFINES]->(p:Parameter) RETURN p.id, p.latex, p.description, p.units "
        "ORDER BY p.id"), {"id": cid})]
    parts = {kind: [t for (t,) in _rows(graph, f"MATCH (:Clause {{id: $id}})-[:{edge}]->(x) RETURN x.text ORDER BY x.id",
                                         {"id": cid})]
             for kind, edge in (("conditions", "HAS_CONDITION"), ("requirements", "HAS_REQUIREMENT"),
                                ("exceptions", "HAS_EXCEPTION"))}
    corrections = [{"raw": r, "corrected": c, "uncertain": u} for r, c, u in _rows(graph, (
        "MATCH (:Clause {id: $id})-[:HAS_CORRECTION]->(s) RETURN s.raw, s.corrected, s.uncertain"), {"id": cid})]
    return {"equations": eqs, "parameters": params, **parts, "corrections": corrections}


def table_extracted(graph, tid: str) -> dict:
    rows = [{"id": i, "cells": _json(cells, []), "values": _json(values, []), "conditions": _json(conds, []),
             "status": st}
            for i, cells, values, conds, st in _rows(graph, (
                "MATCH (:Table {id: $id})-[h:HAS_ROW]->(r:Row) RETURN r.id, r.cells, r.values, r.conditions, r.status "
                "ORDER BY h.index"), {"id": tid})]
    params = [r[0] for r in _rows(graph, "MATCH (:Table {id: $id})-[:GIVES]->(p) RETURN p.id ORDER BY p.id", {"id": tid})]
    notes = [{"text": t, "rule": rule, "kind": kind} for t, rule, kind in _rows(graph, (
        "MATCH (:Table {id: $id})-[:HAS_FOOTNOTE]->(f) RETURN f.text, f.rule, f.rule_kind ORDER BY f.id"), {"id": tid})]
    return {"rows": rows, "gives": params, "footnotes": notes}


@router.get("/review/{name}/item/{label}/{nid:path}")
def review_item(name: str, label: str, nid: str) -> dict:
    return item_detail(_code_graph(name), name, label, nid)


# ---- approve -----------------------------------------------------------------------------------

class Decision(BaseModel):
    note: str | None = None


def _pin_ids(pins, job: str) -> list[str]:
    """The pinned job ids of one item: a long table is split into parts."""
    candidates = [job] + ([f"{job}#part{n}" for n in range(1, 21)] if job.startswith("table:") else [])
    return [j for j in candidates if pins.get(j) is not None]


def _document_hash(graph) -> str:
    rows = _rows(graph, "MATCH (d:Document) RETURN d.file_hash LIMIT 1")
    if not rows or not rows[0][0]:
        raise HTTPException(409, "this graph has no Document node with a file hash")
    return rows[0][0]


def approve(graph, label: str, nid: str, note: str | None, paths: Paths) -> dict:
    item = item_detail(graph, "", label, nid)
    pins = paths.pins(_document_hash(graph))
    pinned = _pin_ids(pins, item["job"])
    for job_id in pinned:
        pins.approve(job_id)
    child = "MATCH (:Clause {id: $id})-[:CONTAINS]->(c:Equation)" if label == "Clause" \
        else "MATCH (:Table {id: $id})-[:HAS_ROW]->(c:Row)"
    params = {"id": nid, "t": time.time(), "note": note}
    graph.query(f"MATCH (n:{label} {{id: $id}}) SET n.review_status = 'approved', n.review_time = $t, "
                "n.review_note = $note", params)
    graph.query(f"{child} SET c.review_status = 'approved', c.review_time = $t", params)
    return {"approved": True, "pins_approved": pinned,
            "note": None if pinned else "no pinned answer for this job (a trivial clause or a run before pins); "
                                        "the graph is marked approved"}


@router.post("/review/{name}/item/{label}/{nid:path}/approve")
def approve_route(name: str, label: str, nid: str, body: Decision) -> dict:
    graph = _code_graph(name)
    return approve(graph, label, nid, body.note, Paths()) | {"item": item_detail(graph, name, label, nid)}


# ---- reject: re-run one job with the note --------------------------------------------------------

def _pdf_for(name: str, pdf_hash: str) -> Path:
    from app.profile.build import file_hash

    folders = (ROOT / "data" / "uploads", ROOT / "data", ROOT / "private_fixtures")
    candidates = [p for folder in folders for p in sorted(folder.glob("*.pdf"))]
    for path in candidates:
        if graph_name(path) == name and file_hash(path) == pdf_hash:
            return path
    for path in candidates:
        if file_hash(path) == pdf_hash:
            return path
    raise HTTPException(409, "the PDF this graph was built from is not stored locally")


def _summary(output, job) -> dict | None:
    """What a Pass 2 answer says, in the shape the review page shows."""
    if output is None:
        return None
    if job.kind == "clause":
        return {"equations": [{"id": e.equation_id, "latex": e.latex, "sympy": e.sympy, "computes": e.computes,
                               "uses": list(e.uses)} for e in output.equations],
                "parameters": [{"id": p.id, "latex": p.latex, "description": p.description, "units": p.units}
                               for p in output.parameters],
                "conditions": [c.text for c in output.conditions],
                "requirements": [r.text for r in output.requirements],
                "exceptions": [x.text for x in output.exceptions]}
    return {"raw": output.model_dump(mode="json")}


async def rerun_with_note(graph, name: str, label: str, nid: str, note: str, paths: Paths,
                          transport=None, log=None) -> dict:
    """Re-run one clause or table job on MODEL_REVIEW with the reviewer note. The new answer
    replaces the stored one (graph and pin) only if it passes; else the stored one stays."""
    from app.config import get_settings, require_model
    from app.ingest.pass2.jobs import clause_job, page_job, table_jobs
    from app.ingest.pass2.notation import NotationDict
    from app.ingest.pass2.records import from_graph
    from app.ingest.pass2.run_pass2 import GraphSink, checker_for, estimated_cost, merge_notation, range_pages
    from app.ingest.pass2.runner import JobState, Runner, passes
    from app.llm import CACHE_DIR
    from app.profile.build import load_profile, profile_path

    lines: list[str] = []
    say = (lambda m: (lines.append(str(m)), log and log(m)))  # noqa: E731
    pdf_hash = _document_hash(graph)
    doc = pymupdf.open(_pdf_for(name, pdf_hash))
    profile = load_profile(profile_path(pdf_hash))
    skel = from_graph(graph, name)
    pins = paths.pins(pdf_hash)
    model = require_model("model_review")
    settings = get_settings()
    cache_dir = paths.cache_dir or CACHE_DIR

    # notation from the pinned answers (no calls): the clause checks need the symbol dictionary
    notation = NotationDict()
    runner = Runner(doc=doc, model_for=lambda job: model, checks=lambda job: checker_for(job, notation),
                    write=lambda outcome: None, state=JobState(paths.state(pdf_hash)), transport=transport,
                    concurrency=2, fallbacks_for=lambda m: settings.server_fallback and "haiku" not in m,
                    cache_dir=cache_dir, log=say, max_cost=REVIEW_MAX_COST, estimate_cost=estimated_cost,
                    use_cache=False, pins=None)
    pinned_notation = []
    for page in range_pages(profile, "notation"):
        job = page_job(doc, pdf_hash, page, "notation")
        if (pin := pins.get(job.id)) is not None:
            pinned_notation.append(runner.pinned_outcome(job, pin))
    if pinned_notation:
        notation, _ = merge_notation(pinned_notation, say)
    else:
        say("no pinned notation answers: the clause checks run without the symbol dictionary")

    if label == "Clause":
        clause = skel.clause(nid)
        if clause is None:
            raise HTTPException(404, f"clause {nid} is not in the skeleton")
        jobs = [clause_job(clause, skel, notation, pdf_hash, settings.clause_crops)]
    else:
        table = next((t for t in skel.tables if t.id == nid), None)
        if table is None:
            raise HTTPException(404, f"table {nid} is not in the skeleton")
        jobs = table_jobs(table, skel, notation)
    for job in jobs:
        job.text += HINT_PREFIX + note
        job.strict = True

    before = {job.id: pins.get(job.id) for job in jobs}
    say(f"re-running {', '.join(j.id for j in jobs)} on {model} with the reviewer note")
    outcomes = await runner.run(jobs)
    sink = GraphSink(graph)
    results = []
    for outcome in outcomes:
        job, stored = outcome.job, before.get(outcome.job.id)
        keep = passes(job, outcome)
        if keep:
            sink.result(outcome, notation.ids())
            runner.pins = pins
            runner.pin(job, outcome)
            runner.pins = None
        say(f"{job.id}: new answer {outcome.status}"
            + (f" ({', '.join(sorted({i.code for i in outcome.issues}))})" if outcome.issues else "")
            + ("; kept it" if keep else "; it does not pass, so the stored answer stays"))
        results.append({"job": job.id, "status": outcome.status, "kept": keep,
                        "issues": [i.as_dict() for i in outcome.issues][:20],
                        "answer": _summary(outcome.output, job),
                        "stored_status": stored.get("status") if stored else None})
    if len(outcomes) < len(jobs):
        say("not every job ran (cost cap or a failed request)")
    return {"results": results, "replaced": bool(results) and all(r["kept"] for r in results),
            "cost": runner.costs.summary(), "model": model, "log": lines}


async def reject(graph, name: str, label: str, nid: str, note: str, paths: Paths, transport=None) -> dict:
    if not note or not note.strip():
        raise HTTPException(422, "a rejection needs a note saying what is wrong")
    before = item_detail(graph, name, label, nid)
    run = await rerun_with_note(graph, name, label, nid, note.strip(), paths, transport)
    rejections = (before.get("rejections") or 0) + 1
    status = "unreviewed" if run["replaced"] else ("needs_manual" if rejections >= MAX_REJECTIONS else "rejected")
    graph.query(f"MATCH (n:{label} {{id: $id}}) SET n.review_status = $s, n.review_rejections = $r, "
                "n.review_note = $note, n.review_time = $t, n.review_severity = $sev",
                {"id": nid, "s": status, "r": rejections, "note": note.strip(), "t": time.time(),
                 "sev": _severity(before["flags"]) if before["flags"] else None})
    after = item_detail(graph, name, label, nid)
    return {"before": before, "after": after, "review_status": status, **run}


@router.post("/review/{name}/item/{label}/{nid:path}/reject")
async def reject_route(name: str, label: str, nid: str, body: Decision) -> dict:
    graph = _code_graph(name)
    return await reject(graph, name, label, nid, body.note or "", Paths())
