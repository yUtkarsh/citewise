"""Pass 1 (SPEC Step 3a): the deterministic skeleton of one document."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import time

import pymupdf

from app.ingest.pass1 import references as refs
from app.ingest.pass1 import tables as tbl
from app.ingest.pass1.blocks import is_footnote, load_context
from app.ingest.pass1.crops import crop, safe_name
from app.ingest.pass1.figures import find_figure
from app.ingest.pass1.model import Clause, Equation, Figure, FlaggedLine, Reference, Table, WhereItem
from app.ingest.pass1.segment import low_coverage_ranges
from app.ingest.pass1.skeleton import Skeleton, build_skeleton
from app.ingest.pass1.warnings import flag_lines
from app.profile.page_map import PageSummary
from app.profile.schema import Profile


@dataclass
class Pass1Result:
    clauses: list[Clause]
    tables: list[Table]
    equations: list[Equation]
    where_items: list[WhereItem]
    figures: list[Figure]
    references: list[Reference]
    flagged: list[FlaggedLine]
    coverage: float
    low_coverage: list[tuple[int, int]]
    timings: dict[str, float] = field(default_factory=dict)
    table_methods: Counter = field(default_factory=Counter)
    skipped_grids: int = 0
    links_unresolved: int = 0


def run_pass1(
    doc: pymupdf.Document, pdf: Path, pdf_hash: str, profile: Profile, page_map: list[PageSummary],
    cache_dir: Path, crops_dir: Path, use_docling: bool = True, pages: list[int] | None = None, log=print,
) -> Pass1Result:
    timings: dict[str, float] = {}
    clock = time.time()

    def lap(name: str) -> None:
        nonlocal clock
        timings[name] = round(time.time() - clock, 1)
        clock = time.time()

    pages = pages or profile.segmented_pages()
    ctx = load_context(doc, profile, pages)
    lap("lines")

    table_pages = tbl.candidate_pages(page_map, ctx.page_lines, profile, pages)
    segments = tbl.extract_segments(doc, pdf, pdf_hash, table_pages, ctx.page_lines, use_docling, cache_dir, log)
    tbl.attach_captions(segments, ctx.page_lines, profile, ctx.body)
    tbl.add_borderless(segments, ctx.page_lines, profile, ctx.body, pages)
    tbl.trim_notes(segments, ctx.page_lines, lambda line: is_footnote(line, ctx), profile.footnote.marker_regex)
    ordered = [s for p in pages for s in sorted(segments.get(p, []), key=lambda s: s.bbox[1])]
    tables, skipped = tbl.merge_segments(ordered)
    kept = {id(s) for t in tables for s in t.segments}
    segments = {p: [s for s in segs if id(s) in kept] for p, segs in segments.items()}
    lap("tables")

    skel = build_skeleton(doc, profile, tables=segments, pages=pages, ctx=ctx)
    lap("segment")

    for table in tables:
        for seg in table.segments:
            seg.table_id = table.id
        tbl.build_rows(table, ctx.page_lines, profile.footnote.marker_regex)
    tbl.attach_footnotes(tables, skel.blocks)
    _assign_owners(skel, tables)
    equations, where_items = _equations(skel)
    figures = _figures(doc, skel)
    lap("objects")

    flagged = flag_lines(ctx.page_lines, tables, profile)
    _crop_all(doc, crops_dir, tables, equations, figures)
    _flag_owners(skel, flagged)
    lap("crops")

    references, links_unresolved = _references(doc, pages, skel, profile, tables, equations, figures)
    lap("references")

    return Pass1Result(
        clauses=skel.seg.clauses, tables=tables, equations=equations, where_items=where_items, figures=figures,
        references=references, flagged=flagged, coverage=skel.seg.coverage,
        low_coverage=low_coverage_ranges(skel.seg), timings=timings,
        table_methods=Counter(s.method for t in tables for s in t.segments), skipped_grids=len(skipped),
        links_unresolved=links_unresolved,
    )


def _assign_owners(skel: Skeleton, tables: list[Table]) -> None:
    owner_of_seg = {}
    for blocks in skel.blocks.values():
        for block in blocks:
            if block.kind == "table":
                owner_of_seg[id(block.obj)] = skel.seg.owner.get(id(block))
    for table in tables:
        table.clause_id = owner_of_seg.get(id(table.segments[0]))


def _equations(skel: Skeleton) -> tuple[list[Equation], list[WhereItem]]:
    """Equations with their clause; a where-block belongs to the equation just before
    it in the same clause, else to the clause."""
    equations, items = [], []
    seen: Counter = Counter()
    last_eq: Equation | None = None
    for page in sorted(skel.blocks):
        for block in skel.blocks[page]:
            owner = skel.seg.owner.get(id(block))
            if block.kind == "heading":
                last_eq = None
            elif block.kind == "equation":
                eq: Equation = block.obj
                seen[eq.id] += 1
                if seen[eq.id] > 1:
                    eq.id = f"{eq.id}~{seen[eq.id]}"
                eq.clause_id = owner
                equations.append(eq)
                last_eq = eq
            elif block.kind == "where":
                target = last_eq.id if last_eq and last_eq.clause_id == owner else owner
                for item in block.obj:
                    item.owner_id = target
                    items.append(item)
                    if last_eq and target == last_eq.id:
                        last_eq.where.append(item)
    return equations, items


def _figures(doc: pymupdf.Document, skel: Skeleton) -> list[Figure]:
    figures, seen = [], Counter()
    for page in sorted(skel.blocks):
        for block in skel.blocks[page]:
            if block.kind == "caption" and block.style == "figure":
                fig = find_figure(block, doc[page - 1])
                seen[fig.id] += 1
                if seen[fig.id] > 1:
                    fig.id = f"{fig.id}~{seen[fig.id]}"
                fig.clause_id = skel.seg.owner.get(id(block))
                figures.append(fig)
    return figures


def _crop_all(doc, crops_dir: Path, tables, equations, figures) -> None:
    for eq in equations:
        eq.crop = crop(doc, eq.page, eq.bbox, crops_dir / "equations" / f"{safe_name(eq.id)}_p{eq.page}.png")
    for table in tables:
        for n, seg in enumerate(table.segments):
            seg.crop = crop(doc, seg.page, seg.bbox, crops_dir / "tables" / f"{safe_name(table.id)}_{n}_p{seg.page}.png")
        first = table.segments[0]
        if first.header_rows and first.row_bboxes:
            box = first.row_bboxes[0]
            for b in first.row_bboxes[1:first.header_rows]:
                box = (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
            table.header_crop = crop(doc, first.page, box,
                                     crops_dir / "tables" / f"{safe_name(table.id)}_header_p{first.page}.png")
    for fig in figures:
        fig.crop = crop(doc, fig.page, fig.bbox, crops_dir / "figures" / f"{safe_name(fig.id)}_p{fig.page}.png")
    # Flagged lines and clause regions are never stored: Pass 2 renders them on the fly.


def _flag_owners(skel: Skeleton, flagged: list[FlaggedLine]) -> None:
    for line in flagged:
        cx, cy = (line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2
        blocks = skel.blocks.get(line.page, [])
        inside = [b for b in blocks if b.bbox[0] - 2 <= cx <= b.bbox[2] + 2 and b.bbox[1] - 2 <= cy <= b.bbox[3] + 2]
        above = [b for b in blocks if b.bbox[3] <= cy]
        block = inside[0] if inside else (max(above, key=lambda b: b.bbox[3]) if above else None)
        line.clause_id = skel.seg.owner.get(id(block)) if block else None


def _references(doc, pages, skel: Skeleton, profile: Profile, tables, equations, figures) -> tuple[list[Reference], int]:
    clauses = skel.seg.clauses
    streams = {s.name: s for s in profile.streams}
    targets = refs.Targets(
        clause=[c.id for c in clauses], table=[t.id for t in tables], figure=[f.id for f in figures],
        equation=[e.id for e in equations], clause_depth={c.id: c.depth for c in clauses},
        clause_namespace={c.id: streams[c.stream].id_prefix for c in clauses},
    )
    found, links_unresolved = refs.link_references(doc, pages, skel.blocks, skel.seg.owner, skel.ctx.page_lines)
    for clause in clauses:
        found += refs.regex_references(clause.id, clause.text, clause.page, profile, targets)
        found += refs.external_codes(clause.id, clause.text, clause.page, profile)
    return refs.dedupe(found), links_unresolved
