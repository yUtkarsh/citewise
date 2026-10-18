"""Pass 2 (SPEC Step 3b): notation pages, definitions, clauses and tables, in that order.

Notation always runs before clauses and tables (their notation slices come from it);
its calls are cached, so re-runs cost nothing. Clauses and tables share one worker pool.
Results go to a ``Sink`` as soon as each one is checked: ``GraphSink`` writes FalkorDB,
tests use a recording sink.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Protocol

import pymupdf

from app.config import get_settings, require_model
from app.ingest.pass1 import references as refs
from app.ingest.pass2 import images as img
from app.ingest.pass2.jobs import KINDS, Job, clause_job, is_trivial, page_job, table_jobs
from app.ingest.pass2.notation import NotationDict, link_terms
from app.ingest.pass2.records import ClauseRec, Skeleton2
from app.ingest.pass2.runner import CostLog, JobState, Outcome, Runner, cost_of
from app.ingest.pass2.schemas import PASS2_SCHEMA_VERSION, NotationEntry, TermEntry
from app.ingest.pass2 import validate as v
from app.llm import CACHE_DIR, AsyncTransport, cache_key, cache_path
from app.prompts import load
from app.profile.schema import Profile

OUTPUT_GUESS = {"notation": 5000, "definitions": 3000, "clause": 700, "table": 3000}  # tokens, for --dry-run
IMAGE_TOKEN_CAP = 1600  # Claude scales images to about 1.15 megapixels


@dataclass
class Pass2Options:
    jobs: set[str] = field(default_factory=lambda: set(KINDS))
    pages: set[int] | None = None
    chapters: set[str] | None = None
    limit: int | None = None
    concurrency: int = 12
    redo: bool = False
    dry_run: bool = False
    crop_mode: str = "always"
    max_cost: float | None = None  # USD


class Sink(Protocol):
    def notation(self, entries: list[tuple[NotationEntry, int, str]], defined_in: dict[str, list[str]]) -> None: ...
    def terms(self, terms: list[tuple[TermEntry, int, str]], links: dict[str, list[str]]) -> None: ...
    def result(self, outcome: Outcome, notation_ids: set[str]) -> None: ...


class GraphSink:
    def __init__(self, graph):
        from app.graph.writers import ensure_indexes

        self.graph = graph
        ensure_indexes(graph)

    def notation(self, entries, defined_in):
        from app.graph.pass2_writers import write_notation
        write_notation(self.graph, entries, defined_in)

    def terms(self, terms, links):
        from app.graph.pass2_writers import write_terms
        write_terms(self.graph, terms, links)

    def result(self, outcome, notation_ids):
        from app.graph.pass2_writers import write_clause, write_table
        if outcome.job.kind == "clause":
            write_clause(self.graph, outcome, notation_ids)
        elif outcome.job.kind == "table":
            write_table(self.graph, outcome)


# ---- selection ---------------------------------------------------------------------------

def _pages_of(clause: ClauseRec) -> set[int]:
    return set(range(clause.page, clause.page_end + 1))


def select_clauses(skel: Skeleton2, opts: Pass2Options) -> list[ClauseRec]:
    out = [c for c in skel.clauses
           if (opts.pages is None or _pages_of(c) & opts.pages)
           and (opts.chapters is None or skel.chapter(c) in opts.chapters)]
    return out[:opts.limit] if opts.limit else out


def select_tables(skel: Skeleton2, opts: Pass2Options):
    out = []
    for t in skel.tables:
        owner = skel.clause(t.clause_id) if t.clause_id else None
        if opts.pages is not None and not set(t.pages) & opts.pages:
            continue
        if opts.chapters is not None and not (owner and skel.chapter(owner) in opts.chapters):
            continue
        out.append(t)
    return out[:opts.limit] if opts.limit else out


def range_pages(profile: Profile, kind: str) -> list[int]:
    return [p for r in profile.ranges if r.kind == kind for p in range(r.start, r.end + 1)]


# ---- checks ------------------------------------------------------------------------------

def checker_for(job: Job, notation: NotationDict):
    if job.kind == "notation":
        return lambda out: v.check_notation(out, job.page_text)
    if job.kind == "definitions":
        return lambda out: v.check_definitions(out, job.page_text)
    if job.kind == "clause":
        ids = notation.ids()
        return lambda out: v.check_clause(out, job.clause, ids).issues
    return lambda out: v.check_table(out, job.table, job.rows)


# ---- notation and definitions merging ------------------------------------------------------

def merge_notation(outcomes: list[Outcome], log) -> tuple[NotationDict, list[tuple[NotationEntry, int, str]]]:
    """One entry per id. The same id on two pages with a different meaning keeps the
    first and renames the later one (suffix _p<page>)."""
    notation, entries, seen = NotationDict(), [], {}
    for o in sorted(outcomes, key=lambda o: o.job.page):
        if o.output is None:
            continue
        kept = []
        for e in o.output.entries:
            if e.id in seen and seen[e.id] != v.norm_text(e.description):
                renamed = f"{e.id}_p{o.job.page}"
                log(f"notation: id {e.id} has two meanings; page {o.job.page} entry renamed {renamed}")
                e = e.model_copy(update={"id": renamed})
            elif e.id in seen:
                continue
            seen[e.id] = v.norm_text(e.description)
            kept.append(e)
            entries.append((e, o.job.page, o.status))
        notation.add(o.job.page, kept)
    return notation, entries


def resolve_defined_in(entries, skel: Skeleton2, profile: Profile) -> dict[str, list[str]]:
    clause_ids = [c.id for c in skel.clauses]
    streams = {s.name: s for s in profile.streams}
    targets = refs.Targets(clause=clause_ids, table=[t.id for t in skel.tables], figure=[],
                           equation=[e.id for c in skel.clauses for e in c.equations],
                           clause_depth={c.id: c.depth for c in skel.clauses},
                           clause_namespace={c.id: streams[c.stream].id_prefix for c in skel.clauses
                                             if c.stream in streams})
    out = {}
    for e, page, _ in entries:
        found = [r.target_id for text in e.defined_in
                 for r in refs.regex_references(e.id, text, page, profile, targets) if r.kind == "clause"]
        if found:
            out[e.id] = list(dict.fromkeys(found))
    return out


# ---- dry run -------------------------------------------------------------------------------

def image_tokens(ref: img.ImageRef) -> int:
    if ref.kind == "file":
        try:
            pix = pymupdf.Pixmap(str(img.ROOT / ref.path))
            w, h = pix.width, pix.height
        except Exception:
            return IMAGE_TOKEN_CAP
    elif ref.bbox is None:
        w, h = 8.5 * ref.dpi, 11 * ref.dpi
    else:
        scale = ref.dpi / 72
        w, h = (ref.bbox[2] - ref.bbox[0] + 2 * ref.padding) * scale, (ref.bbox[3] - ref.bbox[1] + 2 * ref.padding) * scale
    return min(IMAGE_TOKEN_CAP, int(w * h / 750))


def estimated_usage(job: Job, model: str) -> dict:
    """Rough tokens for one attempt: text at about 3 characters a token, images by area."""
    prompt = load(job.prompt)
    return {"model": model, "input_tokens": (len(job.text) + len(prompt.text)) // 3
            + sum(image_tokens(i) for i in job.images), "output_tokens": OUTPUT_GUESS[job.kind]}


def estimated_cost(job: Job, model: str) -> float:
    return cost_of(estimated_usage(job, model)) or 0.0


def estimate(jobs: list[Job], model_for, cache_dir: Path) -> dict:
    rows: dict[str, Counter] = {}
    dollars: Counter = Counter()
    for job in jobs:
        prompt = load(job.prompt)
        cached = cache_path(cache_key(job.cache_text(), PASS2_SCHEMA_VERSION, prompt), prompt, cache_dir).is_file()
        r = rows.setdefault(job.kind, Counter())
        r["jobs"] += 1
        if cached:
            r["cached"] += 1
            continue
        usage = estimated_usage(job, model_for(job))
        r["images"] += len(job.images)
        r["input_tokens"] += usage["input_tokens"]
        r["output_tokens"] += usage["output_tokens"]
        dollars[job.kind] += cost_of(usage) or 0.0
    out = {kind: {**dict(r), "cost_usd": round(dollars[kind], 3)} for kind, r in rows.items()}
    out["total_cost_usd"] = round(sum(dollars.values()), 3)
    return out


# ---- the pass --------------------------------------------------------------------------------

async def run_pass2(doc: pymupdf.Document, pdf_hash: str, profile: Profile, skel: Skeleton2, sink: Sink,
                    opts: Pass2Options, state_path: Path, transport: AsyncTransport | None = None,
                    cache_dir: Path = CACHE_DIR, log=print) -> dict:
    start = time.time()
    settings = get_settings()
    models = {}

    def model_for(job: Job) -> str:
        if job.model_setting not in models:
            models[job.model_setting] = require_model(job.model_setting)
        return models[job.model_setting]

    notation = NotationDict()
    state = JobState(state_path)
    costs = CostLog()
    current = {"notation_ids": set()}

    def write(outcome: Outcome) -> None:
        sink.result(outcome, current["notation_ids"])

    runner = Runner(doc=doc, model_for=model_for, checks=lambda job: checker_for(job, notation), write=write,
                    state=state, costs=costs, transport=transport, concurrency=opts.concurrency,
                    fallbacks_for=lambda model: settings.server_fallback and "haiku" not in model,
                    cache_dir=cache_dir, log=log, max_cost=opts.max_cost, estimate_cost=estimated_cost)
    # Claude Haiku 5.5 has no server-side fallback (Anthropic docs, Refusals and fallback).

    report: dict = {"crop_mode": opts.crop_mode, "schema": PASS2_SCHEMA_VERSION}
    done: list[Outcome] = []
    need_notation = bool(opts.jobs & {"notation", "clauses", "tables"})
    page_jobs = {kind: [page_job(doc, pdf_hash, p, kind) for p in range_pages(profile, kind)]
                 for kind in ("notation", "definitions")}
    if opts.dry_run:
        planned = (page_jobs["notation"] if need_notation else []) + \
                  (page_jobs["definitions"] if "definitions" in opts.jobs else [])
        planned += [clause_job(c, skel, notation, pdf_hash, opts.crop_mode)
                    for c in select_clauses(skel, opts) if not is_trivial(c)] if "clauses" in opts.jobs else []
        planned += [j for t in select_tables(skel, opts) for j in table_jobs(t, skel, notation)] \
            if "tables" in opts.jobs else []
        report["estimate"] = estimate(planned, model_for, cache_dir)
        report["note"] = "notation slices are empty in a dry run before the notation pages are cached"
        return report

    if need_notation and page_jobs["notation"]:
        log(f"notation: {len(page_jobs['notation'])} pages")
        outcomes = await runner.run(page_jobs["notation"])
        done += outcomes
        notation, entries = merge_notation(outcomes, log)
        current["notation_ids"] = notation.ids()
        sink.notation(entries, resolve_defined_in(entries, skel, profile))
        report["notation_entries"] = len(entries)

    if "definitions" in opts.jobs and page_jobs["definitions"]:
        log(f"definitions: {len(page_jobs['definitions'])} pages")
        outcomes = await runner.run(page_jobs["definitions"])
        done += outcomes
        terms, seen = [], set()
        for o in sorted(outcomes, key=lambda o: o.job.page):
            for t in (o.output.terms if o.output else []):
                if t.term.lower() not in seen:
                    seen.add(t.term.lower())
                    terms.append((t, o.job.page, o.status))
        links = link_terms([t for t, _, _ in terms], [(c.id, c.text) for c in skel.clauses], log)
        sink.terms(terms, links)
        report["terms"] = len(terms)
        report["term_links"] = sum(len(x) for x in links.values())

    jobs: list[Job] = []
    trivial: list[Job] = []
    skipped = 0

    def finished(job_id: str, status: str) -> bool:
        entry = state.done.get(job_id)
        return not opts.redo and entry is not None and status != "skeleton"

    if "clauses" in opts.jobs:
        for c in select_clauses(skel, opts):
            if finished(f"clause:{c.id}", c.status):
                skipped += 1
                continue
            job = clause_job(c, skel, notation, pdf_hash, opts.crop_mode)
            (trivial if is_trivial(c) else jobs).append(job)
    if "tables" in opts.jobs:
        for t in select_tables(skel, opts):
            for job in table_jobs(t, skel, notation):
                if finished(job.id, t.status):
                    skipped += 1
                else:
                    jobs.append(job)
    log(f"clauses and tables: {len(jobs)} calls, {len(trivial)} trivial clauses, {skipped} already done")
    lock = asyncio.Lock()
    for job in trivial:
        done.append(await runner.skip(job, lock))
    done += await runner.run(jobs)

    flagged = Counter(i.code for o in done for i in o.issues)
    report |= {
        "jobs": dict(Counter(o.job.kind for o in done)),
        "status": dict(Counter(f"{o.job.kind}:{o.status}" for o in done)),
        "trivial_clauses": len(trivial), "skipped_done": skipped,
        "issues_by_reason": dict(flagged.most_common()),
        "costs": costs.summary(),
        "max_cost_usd": opts.max_cost,
        "stopped_at_cost_cap": runner.stopped,
        "jobs_not_run": len(runner.not_run),
        "seconds": round(time.time() - start, 1),
    }
    if runner.stopped:
        log(f"stopped at the cost cap: {len(runner.not_run)} jobs not run; re-run to resume them")
    return report
