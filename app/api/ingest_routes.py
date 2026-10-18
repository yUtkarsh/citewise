"""Library, upload and ingestion routes (SPEC Steps 1-3): GET /codes, POST /codes, the
profile/confirm/progress job flow, and GET /codes/{graph}/report.

Each upload is one background job in a thread. It calls the existing pipeline functions
(page map, profile, Pass 1, Pass 2) unchanged and records an append-only event log. The
progress stream replays that log, so a reloaded page picks up where it was.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import pymupdf

from app.config import ROOT
from app.graph.connection import get_db
from app.graph.schema import graph_name

UPLOADS = ROOT / "data" / "uploads"
MAX_PAGES = 1000
MIN_TEXT_CHARS = 20  # a page with less text than this has no usable text layer
SCANNED_SHARE = 0.5  # reject when more than this share of pages has no text layer
SAMPLE_HEADINGS = 10
NO_HEADING_RUN = 5  # report runs of at least this many segmented pages without a heading
LIVE_MAX_DEPTH = 2  # the progress graph shows chapters and sections only (SPEC Step 3a, live view)

router = APIRouter()


def upload_max_cost() -> float:
    try:
        return float(os.getenv("UPLOAD_MAX_COST", "3"))
    except ValueError:
        return 3.0


def sse(event: str, data: Any, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ---- library ---------------------------------------------------------------------------------

def _code_graphs() -> list[str]:
    return sorted(g for g in get_db().list_graphs() if g.startswith("code_"))


def _rows(graph, query: str, params: dict | None = None) -> list:
    return graph.ro_query(query, params or {}, timeout=10000).result_set


def _document(graph) -> dict:
    rows = _rows(graph, "MATCH (d:Document) RETURN properties(d) LIMIT 1")
    return dict(rows[0][0]) if rows else {}


def _label_counts(graph) -> dict[str, dict[str, int]]:
    rows = _rows(graph, "MATCH (n) RETURN labels(n)[0], count(n), "
                        "sum(CASE WHEN n.status = 'flagged' THEN 1 ELSE 0 END), "
                        "sum(CASE WHEN n.status = 'skeleton' THEN 1 ELSE 0 END)")
    return {label: {"count": int(n), "flagged": int(f), "skeleton": int(s)} for label, n, f, s in rows}


def _ingest_status(counts: dict[str, dict[str, int]]) -> dict:
    """Pass 2 moves clauses and tables from 'skeleton' to 'extracted' or 'flagged'."""
    total = sum(counts.get(k, {}).get("count", 0) for k in ("Clause", "Table"))
    waiting = sum(counts.get(k, {}).get("skeleton", 0) for k in ("Clause", "Table"))
    if total == 0:
        return {"state": "empty", "text": "No skeleton yet", "pass2_share": 0.0}
    share = round(1 - waiting / total, 3)
    if waiting == total:
        return {"state": "pass1", "text": "Pass 1 done; Pass 2 not run", "pass2_share": share}
    if waiting:
        return {"state": "partial", "text": f"Pass 2 on {share:.0%} of clauses and tables", "pass2_share": share}
    return {"state": "ready", "text": "Ready", "pass2_share": 1.0}


def code_card(name: str) -> dict:
    graph = get_db().select_graph(name)
    doc = _document(graph)
    counts = _label_counts(graph)
    flagged = sum(v["flagged"] for v in counts.values())
    job = _active_job_for(name)
    return {
        "graph": name, "title": doc.get("title") or name, "edition": doc.get("edition"),
        "jurisdiction": doc.get("jurisdiction"), "designation": doc.get("self_designation"),
        "pages": doc.get("page_count"), "file_hash": doc.get("file_hash"), "coverage": doc.get("coverage"),
        "counts": {"clauses": counts.get("Clause", {}).get("count", 0), "tables": counts.get("Table", {}).get("count", 0),
                   "equations": counts.get("Equation", {}).get("count", 0), "flagged": flagged,
                   "symbol_warnings": counts.get("FlaggedLine", {}).get("count", 0)},
        "status": _ingest_status(counts),
        "job": {"id": job.id, "stage": job.stage} if job else None,
        "has_pdf": _pdf_for(name) is not None,
    }


@router.get("/codes")
def list_codes() -> dict:
    cards = []
    for name in _code_graphs():
        try:
            cards.append(code_card(name))
        except Exception as exc:  # one broken graph must not hide the others
            cards.append({"graph": name, "title": name, "error": str(exc)})
    running = [j.summary() for j in JOBS.values() if j.stage not in ("done", "failed") and not j.graph_exists]
    return {"codes": cards, "jobs": running, "max_cost": upload_max_cost()}


def _pdf_for(name: str) -> Path | None:
    for folder in (UPLOADS, ROOT / "data", ROOT / "private_fixtures"):
        for path in sorted(folder.glob("*.pdf")):
            if graph_name(path) == name:
                return path
    return None


# ---- jobs ------------------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    pdf: Path
    pdf_hash: str
    graph: str
    reingest: bool = False
    stage: str = "queued"  # queued, profiling, profile_ready, pass1, pass2, done, failed
    events: list[tuple[str, dict]] = field(default_factory=list)
    profile_view: dict | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)  # reset on confirm: event times count working time
    profile_seconds: float = 0.0
    cond: threading.Condition = field(default_factory=threading.Condition)
    graph_exists: bool = False  # the graph is on the library already (re-ingest)

    def emit(self, event: str, data: dict) -> None:
        with self.cond:
            self.events.append((event, data))
            self.cond.notify_all()

    def set_stage(self, stage: str, message: str) -> None:
        self.stage = stage
        self.emit("stage", {"stage": stage, "message": message, "t": round(time.time() - self.started, 1)})

    def log(self, message: str) -> None:
        self.emit("log", {"message": str(message)[:400], "t": round(time.time() - self.started, 1)})

    def summary(self) -> dict:
        return {"id": self.id, "graph": self.graph, "stage": self.stage, "file": self.pdf.name,
                "reingest": self.reingest, "error": self.error}


JOBS: dict[str, Job] = {}


def _active_job_for(name: str) -> Job | None:
    return next((j for j in JOBS.values() if j.graph == name and j.stage not in ("done", "failed")), None)


def _existing_graph_for_hash(pdf_hash: str) -> str | None:
    for name in _code_graphs():
        if _document(get_db().select_graph(name)).get("file_hash") == pdf_hash:
            return name
    return None


def _check_pdf(path: Path) -> tuple[int, str | None]:
    """(page count, reason to reject or None)."""
    try:
        doc = pymupdf.open(path)
    except Exception:
        return 0, "This file is not a readable PDF."
    with doc:
        pages = doc.page_count
        if pages == 0:
            return 0, "This PDF has no pages."
        if pages > MAX_PAGES:
            return pages, f"This PDF has {pages} pages; the limit is {MAX_PAGES}."
        empty = sum(1 for page in doc if len(page.get_text("text").strip()) < MIN_TEXT_CHARS)
        if empty / pages > SCANNED_SHARE:
            return pages, (f"{empty} of {pages} pages have no text layer. This looks like a scanned PDF; "
                           "only digital PDFs with a text layer are supported.")
    return pages, None


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._") or "upload"
    return stem[:60]


@router.post("/codes")
async def upload_code(request: Request, filename: str = Query(..., description="original file name")) -> dict:
    """The request body is the PDF itself (Content-Type: application/pdf)."""
    UPLOADS.mkdir(parents=True, exist_ok=True)
    tmp = UPLOADS / f".upload-{uuid.uuid4().hex}.pdf"
    import hashlib
    digest = hashlib.sha256()
    try:
        with open(tmp, "wb") as fh:
            async for chunk in request.stream():
                digest.update(chunk)
                fh.write(chunk)
        pdf_hash = digest.hexdigest()
        if existing := await asyncio.to_thread(_existing_graph_for_hash, pdf_hash):
            return {"status": "exists", "graph": existing, "message": "This PDF is already in the library."}
        if running := next((j for j in JOBS.values() if j.pdf_hash == pdf_hash and j.stage != "failed"), None):
            return {"status": "running", "job": running.summary()}
        pages, reason = await asyncio.to_thread(_check_pdf, tmp)
        if reason:
            raise HTTPException(422, reason)
        stem = _safe_stem(filename)
        target = UPLOADS / f"{stem}.pdf"
        if target.exists() or graph_name(target) in _code_graphs():
            target = UPLOADS / f"{stem}_{pdf_hash[:6]}.pdf"
        shutil.move(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    job = _start_job(target, pdf_hash, reingest=False)
    return {"status": "started", "job": job.summary(), "pages": pages}


@router.post("/codes/{name}/reingest")
def reingest(name: str) -> dict:
    """Run the pipeline again on a code's stored PDF: profile screen first, then a fresh graph."""
    if job := _active_job_for(name):
        return {"status": "running", "job": job.summary()}
    pdf = _pdf_for(name)
    if pdf is None:
        raise HTTPException(404, f"no stored PDF for {name}")
    from app.profile.build import file_hash
    job = _start_job(pdf, file_hash(pdf), reingest=True)
    return {"status": "started", "job": job.summary()}


def _start_job(pdf: Path, pdf_hash: str, reingest: bool) -> Job:
    name = graph_name(pdf)
    job = Job(id=uuid.uuid4().hex[:12], pdf=pdf, pdf_hash=pdf_hash, graph=name, reingest=reingest,
              graph_exists=name in _code_graphs())
    JOBS[job.id] = job
    threading.Thread(target=_profile_stage, args=(job,), daemon=True, name=f"profile-{job.id}").start()
    return job


def _get_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job (jobs live in memory; the server may have restarted)")
    return job


@router.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = _get_job(job_id)
    return {**job.summary(), "profile": job.profile_view, "max_cost": upload_max_cost(),
            "events": len(job.events)}


# ---- stage 1: profile ------------------------------------------------------------------------

def _profile_stage(job: Job) -> None:
    from app.ingest.run import get_profile
    from app.profile.page_map import build_page_map
    from app.profile.validate import validate

    try:
        job.set_stage("profiling", "Reading the page map")
        with pymupdf.open(job.pdf) as doc:
            page_map = build_page_map(doc)
            job.log(f"page map: {doc.page_count} pages")
            job.set_stage("profiling", "Building the document profile (Claude reads the page map and sample pages)")
            args = _Args(reprofile=False, profile=None)
            profile = get_profile(doc, job.pdf_hash, args, page_map, job.log)
            report = validate(doc, profile)
            job.profile_view = profile_view(profile, report)
        job.profile_seconds = round(time.time() - job.started, 1)
        job.set_stage("profile_ready", "Profile ready: check it and confirm to start Pass 1 and Pass 2")
    except Exception as exc:
        _fail(job, exc)


@dataclass
class _Args:
    reprofile: bool
    profile: str | None


def profile_view(profile, report) -> dict:
    """What the profile screen shows: detected metadata, streams, sample headings, checks."""
    clauses = report.skeleton.seg.clauses
    step = max(1, len(clauses) // SAMPLE_HEADINGS)
    samples = [{"number": c.number, "title": c.title, "page": c.page, "stream": c.stream, "depth": c.depth}
               for c in clauses[::step][:SAMPLE_HEADINGS]]
    heading_pages = {c.page for c in clauses}
    runs, run = [], []
    for page in profile.segmented_pages():
        if page in heading_pages:
            if len(run) >= NO_HEADING_RUN:
                runs.append((run[0], run[-1]))
            run = []
        else:
            run.append(page)
    if len(run) >= NO_HEADING_RUN:
        runs.append((run[0], run[-1]))
    v = report.validation
    per_stream = Counter(c.stream for c in clauses)
    return {
        "title": profile.title, "edition": profile.edition, "jurisdiction": profile.jurisdiction,
        "designation": profile.self_designation, "pages": profile.page_count,
        "ranges": [r.model_dump() for r in profile.ranges],
        "streams": [{"name": s.name, "kind": s.kind, "prefix": s.prefix, "examples": s.examples[:6],
                     "levels": [lv.depth for lv in s.levels], "headings": per_stream.get(s.name, 0)}
                    for s in profile.streams],
        "samples": samples,
        "validation": v.model_dump() if v else None,
        "no_heading_runs": runs,
        "table_caption": profile.table_caption.regex, "equation_number": profile.equation_number,
    }


# ---- stage 2: confirm, Pass 1, Pass 2 --------------------------------------------------------

class Confirm(BaseModel):
    title: str
    edition: str
    jurisdiction: str


@router.post("/jobs/{job_id}/confirm")
def confirm(job_id: str, body: Confirm) -> dict:
    job = _get_job(job_id)
    if job.stage != "profile_ready":
        raise HTTPException(409, f"job is {job.stage}, not waiting for confirmation")
    job.stage = "pass1"
    job.started = time.time()
    threading.Thread(target=_pipeline_stage, args=(job, body), daemon=True, name=f"ingest-{job.id}").start()
    return job.summary()


def _pipeline_stage(job: Job, meta: Confirm) -> None:
    from app.graph.writers import GraphRows, build_rows, write
    from app.ingest.run import CROPS_DIR, REPORT_DIR, summary
    from app.ingest.pass1.run_pass1 import run_pass1
    from app.llm import CACHE_DIR
    from app.profile.build import load_profile, profile_path, save_profile
    from app.profile.page_map import build_page_map

    try:
        path = profile_path(job.pdf_hash)
        profile = load_profile(path)
        profile.title, profile.edition, profile.jurisdiction = meta.title, meta.edition, meta.jurisdiction
        save_profile(profile, path)
        job.set_stage("pass1", "Pass 1: reading blocks, tables, equations and references (no API calls)")
        start = time.time()
        with pymupdf.open(job.pdf) as doc:
            page_map = build_page_map(doc)
            result = run_pass1(doc, job.pdf, job.pdf_hash, profile, page_map, CACHE_DIR, CROPS_DIR / job.graph,
                               use_docling=False, log=job.log)
            db = get_db()
            graph = db.select_graph(job.graph)
            if job.graph in _code_graphs():
                job.log(f"graph: deleting the old {job.graph} before writing it again")
                graph.delete()
                graph = db.select_graph(job.graph)
            rows = build_rows(result, profile, job.graph, job.pdf_hash)
            _write_skeleton_live(job, graph, rows, GraphRows, write)
            counts = write(graph, rows)
            job.log(f"graph: wrote {job.graph}")
            report = summary(result, profile, counts, time.time() - start)
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            (REPORT_DIR / f"{job.pdf_hash[:12]}_pass1.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            job.emit("counts", {"pass": 1, "clauses": len(result.clauses), "tables": len(result.tables),
                                "equations": len(result.equations), "figures": len(result.figures),
                                "symbol_warnings": len(result.flagged), "coverage": round(result.coverage, 4)})
            pass1_seconds = round(time.time() - start, 1)
            _run_pass2(job, doc, profile, graph, REPORT_DIR)
        (REPORT_DIR / f"{job.pdf_hash[:12]}_upload.json").write_text(json.dumps({
            "graph": job.graph, "file": job.pdf.name, "profile_seconds": job.profile_seconds,
            "pass1_seconds": pass1_seconds, "max_cost": upload_max_cost(),
            "total_seconds": round(job.profile_seconds + time.time() - job.started, 1),  # without the wait to confirm
            "docling": False, "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2), encoding="utf-8")
        job.set_stage("done", "Ingestion finished")
    except Exception as exc:
        _fail(job, exc)


def _write_skeleton_live(job: Job, graph, rows, GraphRows, write) -> None:
    """Write clause nodes chapter by chapter so the progress graph grows as they land; the
    full write that follows MERGEs everything (these nodes included) again."""
    clauses = sorted(rows.nodes.get("Clause", []), key=lambda c: c.get("order") or 0)
    parents = {e["src"]: e["dst"] for e in rows.edges.get(("Clause", "PART_OF", "Clause"), [])}
    chapter: list[dict] = []

    def flush() -> None:
        if not chapter:
            return
        part = GraphRows()
        for c in chapter:
            part.node("Clause", c)
        write(graph, part)
        shown = [c for c in chapter if (c.get("depth") or 1) <= LIVE_MAX_DEPTH]
        job.emit("graph", {"nodes": [{"id": c["id"], "name": f"{c['number']} {c.get('title') or ''}".strip(),
                                      "depth": c.get("depth"), "kind": c.get("kind"), "page": c.get("page")}
                                     for c in shown],
                           "edges": [f"{c['id']}|PART_OF|{parents[c['id']]}" for c in shown if c["id"] in parents]})
        chapter.clear()

    for c in clauses:
        if (c.get("depth") or 1) == 1 and chapter:
            flush()
        chapter.append(c)
    flush()


def _run_pass2(job: Job, doc, profile, graph, report_dir: Path) -> None:
    from app.ingest.pass2.jobs import KINDS
    from app.ingest.pass2.records import from_graph
    from app.ingest.pass2.run_pass2 import GraphSink, Pass2Options, run_pass2
    from app.ingest.run import JOBS_DIR
    from app.config import get_settings

    max_cost = upload_max_cost()
    job.set_stage("pass2", f"Pass 2: Claude reads notation, definitions, clauses and tables (cost cap ${max_cost:.2f})")
    skel = from_graph(graph, job.graph)
    state_path = JOBS_DIR / f"{job.pdf_hash[:12]}_pass2.jsonl"
    meter = _CostMeter(state_path)
    progress = {"done": 0, "flagged": 0, "total": None}

    def log(message: str) -> None:
        job.log(message)
        if m := re.match(r"clauses and tables: (\d+) calls, (\d+) trivial", message):
            progress["total"] = int(m.group(1)) + int(m.group(2))
        elif m := re.match(r"([.!]) (\S+?):(.+?) (extracted|flagged|needs_manual)\b", message):
            if m.group(2) not in ("clause", "table"):
                return
            progress["done"] += 1
            progress["flagged"] += m.group(1) == "!"
            if m.group(2) == "clause":
                job.emit("graph_status", {"id": m.group(3), "status": m.group(4)})
        else:
            return
        job.emit("counts", {"pass": 2, **progress, "cost_usd": meter.read(), "max_cost": max_cost})

    opts = Pass2Options(jobs=set(KINDS), concurrency=12, max_cost=max_cost,
                        crop_mode=get_settings().clause_crops)
    report = asyncio.run(run_pass2(doc, job.pdf_hash, profile, skel, GraphSink(graph), opts, state_path, log=log))
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{job.pdf_hash[:12]}_pass2.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    total = report.get("costs", {}).get("total_cost_usd", 0.0)
    job.emit("counts", {"pass": 2, **progress, "cost_usd": total, "max_cost": max_cost,
                        "stopped_at_cost_cap": report.get("stopped_at_cost_cap"),
                        "jobs_not_run": report.get("jobs_not_run")})


class _CostMeter:
    """Spend of this run so far, from the Pass 2 job-state file (read-only)."""

    def __init__(self, path: Path):
        self.path, self.start = path, time.time() - 1
        self.offset = path.stat().st_size if path.exists() else 0
        self.total = 0.0

    def read(self) -> float:
        from app.ingest.pass2.runner import cost_of

        if not self.path.exists():
            return round(self.total, 4)
        with open(self.path, encoding="utf-8") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
        complete = chunk[:chunk.rfind("\n") + 1]
        self.offset += len(complete.encode("utf-8"))
        for line in complete.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            for usage in record.get("usages") or []:
                self.total += cost_of(usage) or 0.0
        return round(self.total, 4)


def _fail(job: Job, exc: Exception) -> None:
    job.error = f"{type(exc).__name__}: {exc}"
    job.set_stage("failed", job.error)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, after: int = -1) -> StreamingResponse:
    """Replays the job's events after `after` (or the Last-Event-ID header), then follows them."""
    job = _get_job(job_id)
    start = max(int(request.headers.get("last-event-id", "-1")), after) + 1

    async def frames():
        index = start
        while True:
            while index < len(job.events):
                event, data = job.events[index]
                yield sse(event, data, index)
                index += 1
            if job.stage in ("done", "failed", "profile_ready") and index >= len(job.events):
                yield sse("end", {"stage": job.stage})
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.3)

    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- report ----------------------------------------------------------------------------------

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}


def _flag_reasons(graph) -> tuple[Counter, Counter, dict[str, str]]:
    """(flags by reason, flags by severity, highest severity seen per reason) over every node
    that carries Pass 2 flags."""
    reasons, severities, worst = Counter(), Counter(), {}
    for (flags,) in _rows(graph, "MATCH (n) WHERE n.flags IS NOT NULL AND size(n.flags) > 0 RETURN n.flags"):
        for f in flags if isinstance(flags, list) else [flags]:
            try:
                item = json.loads(f) if isinstance(f, str) else f
            except ValueError:
                item = {"reason": f}
            if isinstance(item, dict):
                reason, severity = item.get("reason") or "?", item.get("severity") or "?"
                reasons[reason] += 1
                severities[severity] += 1
                if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(worst.get(reason), 0):
                    worst[reason] = severity
    return reasons, severities, worst


@router.get("/codes/{name}/report")
def code_report(name: str) -> dict:
    from app.ingest.run import REPORT_DIR

    if name not in _code_graphs():
        raise HTTPException(404, f"no code graph {name}")
    graph = get_db().select_graph(name)
    card = code_card(name)
    counts = _label_counts(graph)
    reasons, severities, worst = _flag_reasons(graph)
    warnings = Counter()
    by_page = Counter()
    for page, kinds in _rows(graph, "MATCH (f:FlaggedLine) RETURN f.page, f.reasons"):
        by_page[page] += 1
        for k in kinds or []:
            warnings[str(k).split(":")[0]] += 1
    key = (card.get("file_hash") or "")[:12]
    pass1 = _read_json(REPORT_DIR / f"{key}_pass1.json") if key else None
    pass2 = _read_json(REPORT_DIR / f"{key}_pass2.json") if key else None
    upload = _read_json(REPORT_DIR / f"{key}_upload.json") if key else None
    return {
        "card": card,
        "nodes": {label: v for label, v in sorted(counts.items(), key=lambda kv: -kv[1]["count"])},
        "flags_by_reason": [{"reason": r, "count": n, "severity": worst.get(r)} for r, n in reasons.most_common()],
        "flags_by_severity": dict(severities),
        "symbol_warnings_by_page": sorted([p, n] for p, n in by_page.items() if p is not None),
        "symbol_warnings_by_reason": dict(warnings.most_common()),
        "coverage": (pass1 or {}).get("coverage", card.get("coverage")),
        "low_coverage_ranges": (pass1 or {}).get("low_coverage_ranges"),
        "profile_validation": (pass1 or {}).get("profile_validation"),
        "pass1": pass1, "pass2": pass2, "upload": upload,
    }
