"""Pass 2 jobs: one Claude call each, with the inputs SPEC Step 3b lists."""
from __future__ import annotations

from dataclasses import dataclass, field
import json

import pymupdf
from pydantic import BaseModel

from app.ingest.pass2 import images as img
from app.ingest.pass2.notation import NotationDict
from app.ingest.pass2.records import ClauseRec, RowRec, Skeleton2, TableRec
from app.ingest.pass2.schemas import ClauseExtraction, DefinitionsPage, NotationPage, TableExtraction

TRIVIAL_CHARS = 40  # a clause with less text, no equations and no flags needs no call
TABLE_SPLIT_ROWS = 80  # larger tables are sent one page segment at a time
CONTEXT_CHARS = 4000  # referencing clause text sent with a table
PAGE_DPI = 150

KINDS = ("notation", "definitions", "clauses", "tables")


@dataclass
class Job:
    id: str
    kind: str  # notation / definitions / clause / table
    model_setting: str  # Settings field holding the model id
    prompt: str
    output: type[BaseModel]
    text: str
    images: list[img.ImageRef] = field(default_factory=list)
    max_tokens: int = 16000
    clause: ClauseRec | None = None
    table: TableRec | None = None
    rows: list[RowRec] | None = None  # the rows this job covers (a table part)
    page: int = 0
    page_text: str = ""

    def cache_text(self) -> str:
        """Everything that defines the answer: the text and the image keys."""
        return self.text + "\x1e" + "\x1e".join(f"{i.label}={i.key}" for i in self.images)


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1)


# ---- notation and definitions pages --------------------------------------------

def page_job(doc: pymupdf.Document, pdf_hash: str, page: int, kind: str) -> Job:
    text = doc[page - 1].get_text("text")
    output = NotationPage if kind == "notation" else DefinitionsPage
    return Job(id=f"{kind}:p{page}", kind=kind, model_setting="model_notation", prompt=f"pass2_{kind}",
               output=output, text=_dump({"page": page, "text_layer": text}),
               images=[img.region(pdf_hash, page, None, f"page {page}", dpi=PAGE_DPI, padding=0)],
               max_tokens=32000, page=page, page_text=text)


# ---- clauses --------------------------------------------------------------------

def is_trivial(clause: ClauseRec) -> bool:
    return len(clause.text) < TRIVIAL_CHARS and not clause.equations and not clause.flagged


def clause_symbols(clause: ClauseRec) -> set[str]:
    symbols = {w.symbol.strip() for w in clause.where}
    symbols |= {w.symbol.strip() for e in clause.equations for w in e.where}
    for e in clause.equations:
        symbols |= set(e.raw_text.split())
    return symbols


def clause_job(clause: ClauseRec, skel: Skeleton2, notation: NotationDict, pdf_hash: str, crop_mode: str) -> Job:
    text_for_slice = " ".join([clause.text, *(e.raw_text for e in clause.equations),
                               *(f"{w.symbol} {w.definition}" for w in clause.where)])
    entries = notation.slice(text_for_slice, skel.chapter(clause), clause_symbols(clause))
    equation_images = []
    for e in clause.equations:
        ref = img.stored(e.crop, f"equation {e.id}") if e.crop else None
        equation_images.append(ref or img.region(pdf_hash, e.page, e.bbox, f"equation {e.id}", dpi=200, padding=3))
    payload = {
        "breadcrumb": skel.breadcrumb(clause),
        "clause": {"id": clause.id, "number": clause.number, "title": clause.title, "kind": clause.kind,
                   "text": clause.text},
        "where_block": [{"symbol": w.symbol, "definition": w.definition, "unit": w.unit} for w in clause.where],
        "notation": [{"id": e.id, "symbol": e.display, "aliases": e.aliases, "description": e.description,
                      "units": e.units} for e in entries],
        "references": [{"text": r.text, "target": r.target_id, "target_title": r.target_title}
                       for r in clause.references],
        "equations": [{"id": e.id, "number": e.number, "text_layer": e.raw_text,
                       "where": [{"symbol": w.symbol, "definition": w.definition, "unit": w.unit} for w in e.where]}
                      for e in clause.equations],
        "suspect_text": clause.suspect_spans,
        "flagged_lines": [{"id": f.id, "text": f.text, "reasons": f.reasons} for f in clause.flagged],
    }
    return Job(id=f"clause:{clause.id}", kind="clause", model_setting="model_clauses", prompt="pass2_clause",
               output=ClauseExtraction, text=_dump(payload),
               images=equation_images + img.clause_images(clause, pdf_hash, crop_mode), clause=clause)


# ---- tables ---------------------------------------------------------------------

def table_parts(table: TableRec) -> list[list[RowRec]]:
    """All rows in one part, or (large tables) one part per page with the header rows repeated."""
    if len(table.rows) <= TABLE_SPLIT_ROWS or len(set(r.page for r in table.rows)) < 2:
        return [table.rows]
    headers = [r for r in table.rows if r.is_header]
    parts = []
    for page in sorted({r.page for r in table.rows}):
        body = [r for r in table.rows if r.page == page and not r.is_header]
        if body:
            parts.append(headers + [r for r in body if r not in headers])
    return parts


def table_jobs(table: TableRec, skel: Skeleton2, notation: NotationDict) -> list[Job]:
    context_id = (table.referenced_by or [table.clause_id] or [None])[0]
    context = skel.clause(context_id) if context_id else None
    parts = table_parts(table)
    jobs = []
    for n, rows in enumerate(parts):
        cells_text = " ".join(c for r in rows for c in r.cells)
        notes_text = " ".join(t for _, t in table.footnotes)
        entries = notation.slice(f"{table.caption} {cells_text} {notes_text}", None,
                                 {c.strip() for r in rows for c in r.cells})
        pages = sorted({r.page for r in rows})
        payload = {
            "table": {"id": table.id, "number": table.number, "caption": table.caption},
            "part": f"{n + 1} of {len(parts)}" if len(parts) > 1 else None,
            "grid": [{"row": r.index, "header": r.is_header, "cells": r.cells} for r in rows],
            "footnotes": [{"marker": m, "text": t} for m, t in table.footnotes],
            "notation": [{"id": e.id, "symbol": e.display, "aliases": e.aliases, "description": e.description,
                          "units": e.units} for e in entries],
            "referring_clause": ({"id": context.id, "title": context.title, "text": context.text[:CONTEXT_CHARS]}
                                 if context else None),
        }
        crops = [c for i, c in enumerate(table.crops) if len(parts) == 1 or table.pages[i] in pages]
        images = [ref for i, path in enumerate(crops) if (ref := img.stored(path, f"table crop {i + 1}"))]
        if table.header_crop and (ref := img.stored(table.header_crop, "header rows")):
            images.append(ref)
        job_id = f"table:{table.id}" + (f"#part{n + 1}" if len(parts) > 1 else "")
        jobs.append(Job(id=job_id, kind="table", model_setting="model_tables", prompt="pass2_table",
                        output=TableExtraction, text=_dump(payload), images=images, max_tokens=32000,
                        table=table, rows=rows))
    return jobs
