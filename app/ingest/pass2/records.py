"""What Pass 2 reads: the skeleton, as plain records.

Loaded from FalkorDB (``from_graph``) for real runs, or straight from a Pass 1 result
(``from_pass1``) for tests and fixture runs. Both give the same records.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

from app.ingest.pdf_lines import Rect
from app.ingest.pass1.run_pass1 import Pass1Result


@dataclass
class WhereRec:
    symbol: str
    definition: str
    unit: str | None


@dataclass
class EquationRec:
    id: str
    number: str
    page: int
    bbox: Rect
    raw_text: str
    crop: str | None
    clause_id: str | None
    where: list[WhereRec] = field(default_factory=list)


@dataclass
class ReferenceRec:
    text: str
    target_id: str
    kind: str
    target_title: str = ""


@dataclass
class FlagRec:
    id: str
    page: int
    bbox: Rect
    text: str
    reasons: list[str]


@dataclass
class ClauseRec:
    id: str
    number: str
    title: str
    kind: str
    stream: str
    depth: int
    page: int
    page_end: int
    parent_id: str | None
    text: str
    regions: list[tuple[int, Rect]]
    suspect_spans: list[str]
    order: int = 0
    where: list[WhereRec] = field(default_factory=list)
    equations: list[EquationRec] = field(default_factory=list)
    references: list[ReferenceRec] = field(default_factory=list)
    flagged: list[FlagRec] = field(default_factory=list)
    status: str = "skeleton"


@dataclass
class RowRec:
    index: int
    page: int
    bbox: Rect
    cells: list[str]
    is_header: bool
    grid_method: str
    markers: list[str] = field(default_factory=list)


@dataclass
class TableRec:
    id: str
    number: str | None
    caption: str
    pages: list[int]
    crops: list[str]
    header_crop: str | None
    rows: list[RowRec]
    footnotes: list[tuple[str, str]]  # (marker, text)
    clause_id: str | None
    referenced_by: list[str] = field(default_factory=list)
    status: str = "skeleton"
    trust: str = "text"  # text: numbers must match the text layer; image: read from the image, read twice
    trust_reason: str = ""
    suspect_spans: list[str] = field(default_factory=list)  # flagged text-layer lines inside the table


GARBLED = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ue000-\uf8ff\ufffd\U000f0000-\U0010ffff]")


def _ws(text: str) -> str:
    return " ".join((text or "").split())


def table_suspect_spans(rows: list[RowRec], flagged: list[tuple[int, Rect, list[str], str]]) -> list[str]:
    """The text of every flagged line (symbol warning, unmapped font, control character)
    whose centre lies in the table's region on its page."""
    regions: dict[int, Rect] = {}
    for r in rows:
        b = regions.get(r.page)
        regions[r.page] = r.bbox if b is None else (min(b[0], r.bbox[0]), min(b[1], r.bbox[1]),
                                                   max(b[2], r.bbox[2]), max(b[3], r.bbox[3]))
    out = []
    for page, box, _reasons, text in flagged:
        region = regions.get(page)
        if region is None or not _ws(text):
            continue
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if region[0] - 2 <= cx <= region[2] + 2 and region[1] - 2 <= cy <= region[3] + 2:
            out.append(_ws(text))
    return list(dict.fromkeys(out))


def is_suspect_cell(text: str, spans: list[str]) -> bool:
    """A grid cell whose text the text layer cannot be trusted for: it holds a control,
    private-use or replacement character, or it is (part of) a flagged line."""
    if GARBLED.search(text or ""):
        return True
    cell = _ws(text)
    if not cell:
        return False
    return any(cell == s or s in cell or (len(cell) >= 3 and cell in s) for s in spans)


def table_trust(rows: list[RowRec], flagged: list[tuple[int, Rect, list[str]]]) -> tuple[str, str]:
    """Per-table source trust (SPEC Step 3b): 'image' when a flagged line (symbol warning or
    unmapped font) lies in the table's region on any of its pages, or when the grid's body
    has no text at all; else 'text'. ``flagged``: (page, bbox, reasons) of every flagged line."""
    body = [r for r in rows if not r.is_header]
    if body and not any(c.strip() for r in body for c in r.cells):
        return "image", "no text layer in the table body"
    regions: dict[int, Rect] = {}
    for r in rows:
        b = regions.get(r.page)
        regions[r.page] = r.bbox if b is None else (min(b[0], r.bbox[0]), min(b[1], r.bbox[1]),
                                                   max(b[2], r.bbox[2]), max(b[3], r.bbox[3]))
    hits = []
    for page, box, reasons in flagged:
        region = regions.get(page)
        if region is None:
            continue
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if region[0] - 2 <= cx <= region[2] + 2 and region[1] - 2 <= cy <= region[3] + 2:
            hits += [r.split(":")[0] for r in reasons]
    if hits:
        return "image", "flagged lines in the table: " + ", ".join(sorted(set(hits)))
    return "text", ""


@dataclass
class Skeleton2:
    doc_id: str
    clauses: list[ClauseRec]
    tables: list[TableRec]

    def clause(self, cid: str) -> ClauseRec | None:
        return self._by_id().get(cid)

    def _by_id(self) -> dict[str, ClauseRec]:
        if not hasattr(self, "_index"):
            self._index = {c.id: c for c in self.clauses}
        return self._index

    def chapter(self, clause: ClauseRec) -> str:
        """Top-level number of the clause's root ancestor (commentary maps to its provision's chapter)."""
        by_id = self._by_id()
        node = clause
        while node.parent_id and node.parent_id in by_id:
            node = by_id[node.parent_id]
        return node.number.split(".")[0]

    def breadcrumb(self, clause: ClauseRec) -> list[str]:
        by_id, out, node = self._by_id(), [], clause
        while node.parent_id and node.parent_id in by_id:
            node = by_id[node.parent_id]
            out.append(f"{node.number} {node.title}".strip())
        return list(reversed(out))


def _box(b) -> Rect:
    return tuple(float(v) for v in b)


def from_pass1(result: Pass1Result, doc_id: str) -> Skeleton2:
    clauses = {
        c.id: ClauseRec(id=c.id, number=c.number, title=c.title, kind=c.kind, stream=c.stream, depth=c.depth,
                        page=c.page, page_end=c.page_end, parent_id=c.parent_id, text=c.text,
                        regions=[(p, _box(b)) for p, b in c.regions], suspect_spans=list(c.suspect_spans),
                        order=c.order)
        for c in result.clauses
    }
    titles = {c.id: c.title for c in result.clauses}
    titles |= {t.id: t.caption for t in result.tables} | {f.id: f.caption for f in result.figures}
    titles |= {e.id: e.number for e in result.equations}
    for e in result.equations:
        rec = EquationRec(id=e.id, number=e.number, page=e.page, bbox=_box(e.bbox), raw_text=e.raw_text,
                          crop=e.crop, clause_id=e.clause_id,
                          where=[WhereRec(w.symbol, w.definition, w.unit) for w in e.where])
        if e.clause_id in clauses:
            clauses[e.clause_id].equations.append(rec)
    for w in result.where_items:
        if w.owner_id in clauses:
            clauses[w.owner_id].where.append(WhereRec(w.symbol, w.definition, w.unit))
    referenced_by: dict[str, list[str]] = {}
    for r in result.references:
        if r.source_id in clauses and r.target_id and r.kind != "external":
            clauses[r.source_id].references.append(
                ReferenceRec(r.text, r.target_id, r.kind, titles.get(r.target_id, "")))
            if r.kind == "table":
                referenced_by.setdefault(r.target_id, []).append(r.source_id)
    for f in result.flagged:
        if f.clause_id in clauses:
            clauses[f.clause_id].flagged.append(FlagRec(f.id, f.page, _box(f.bbox), f.text, list(f.reasons)))
    tables = []
    flagged = [(f.page, _box(f.bbox), list(f.reasons)) for f in result.flagged]
    flagged_text = [(f.page, _box(f.bbox), list(f.reasons), f.text) for f in result.flagged]
    for t in result.tables:
        method_on = {s.page: s.method for s in t.segments}
        rows = [RowRec(r.index, r.page, _box(r.bbox), list(r.cells), r.is_header,
                       method_on.get(r.page, t.segments[0].method), list(r.markers)) for r in t.rows]
        trust, reason = table_trust(rows, flagged)
        tables.append(TableRec(id=t.id, number=t.number, caption=t.caption, pages=t.pages,
                               crops=[s.crop for s in t.segments if s.crop], header_crop=t.header_crop, rows=rows,
                               footnotes=[(f.marker, f.text) for f in t.footnotes], clause_id=t.clause_id,
                               referenced_by=list(dict.fromkeys(referenced_by.get(t.id, []))),
                               trust=trust, trust_reason=reason,
                               suspect_spans=table_suspect_spans(rows, flagged_text)))
    return Skeleton2(doc_id=doc_id, clauses=sorted(clauses.values(), key=lambda c: c.order), tables=tables)


# ---- FalkorDB ---------------------------------------------------------------------

def _rows(graph, query: str, params: dict | None = None) -> list[list]:
    return graph.ro_query(query, params or {}).result_set


def from_graph(graph, doc_id: str) -> Skeleton2:
    """Read the Pass 1 skeleton (and any Pass 2 status) back from the document's graph."""
    clauses: dict[str, ClauseRec] = {}
    q = ("MATCH (c:Clause) OPTIONAL MATCH (c)-[:PART_OF]->(p:Clause) "
         "RETURN c.id, c.number, c.title, c.kind, c.stream, c.depth, c.page, c.page_end, p.id, c.text, "
         "c.regions, c.suspect_spans, c.order, c.status")
    for (cid, num, title, kind, stream, depth, page, page_end, parent, text, regions, suspect, order,
         status) in _rows(graph, q):
        clauses[cid] = ClauseRec(
            id=cid, number=num, title=title or "", kind=kind, stream=stream, depth=depth, page=page,
            page_end=page_end or page, parent_id=parent, text=text or "",
            regions=[(r[0], tuple(r[1:])) for r in json.loads(regions or "[]")],
            suspect_spans=json.loads(suspect or "[]"), order=order or 0, status=status or "skeleton")
    if clauses and all(not c.regions for c in clauses.values()):
        raise RuntimeError("the skeleton has no clause regions: re-run Pass 1 (python -m app.ingest.run <pdf> --pass1)")

    # Pass 1 equations only: inline equations are Pass 2 results, not skeleton
    q = ("MATCH (c:Clause)-[:CONTAINS]->(e:Equation) WHERE e.inline IS NULL OR e.inline = false "
         "OPTIONAL MATCH (e)-[:WHERE]->(w:WhereItem) "
         "RETURN c.id, e.id, e.number, e.page, e.bbox, e.raw_text, e.crop, "
         "collect([w.symbol, w.definition, w.unit])")
    for cid, eid, num, page, bbox, raw, crop, where in _rows(graph, q):
        items = [WhereRec(*w) for w in where if w[0] is not None]
        clauses[cid].equations.append(EquationRec(eid, num, page, _box(bbox), raw or "", crop, cid, items))
    q = "MATCH (c:Clause)-[:WHERE]->(w:WhereItem) RETURN c.id, w.symbol, w.definition, w.unit ORDER BY w.id"
    for cid, sym, definition, unit in _rows(graph, q):
        clauses[cid].where.append(WhereRec(sym, definition, unit))
    q = ("MATCH (c:Clause)-[r:REFERENCES]->(t) WHERE NOT t:ExternalCode "
         "RETURN c.id, r.text, t.id, labels(t)[0], coalesce(t.title, t.caption, t.number, '')")
    referenced_by: dict[str, list[str]] = {}
    for cid, text, tid, label, title in _rows(graph, q):
        clauses[cid].references.append(ReferenceRec(text, tid, label.lower(), title))
        if label == "Table":
            referenced_by.setdefault(tid, []).append(cid)
    q = "MATCH (c:Clause)-[:HAS_FLAG]->(f:FlaggedLine) RETURN c.id, f.id, f.page, f.bbox, f.text, f.reasons"
    for cid, fid, page, bbox, text, reasons in _rows(graph, q):
        clauses[cid].flagged.append(FlagRec(fid, page, _box(bbox), text, list(reasons or [])))

    tables = []
    flagged_text = [(page, _box(bbox), list(reasons or []), text or "") for page, bbox, reasons, text in _rows(
        graph, "MATCH (f:FlaggedLine) RETURN f.page, f.bbox, f.reasons, f.text")]
    flagged = [f[:3] for f in flagged_text]
    q = ("MATCH (t:Table) OPTIONAL MATCH (c:Clause)-[:CONTAINS]->(t) "
         "RETURN t.id, t.number, t.caption, t.pages, t.crops, t.header_crop, t.grid_method, c.id, t.status")
    for tid, num, caption, pages, crops, header_crop, methods, cid, status in _rows(graph, q):
        rows = [RowRec(index, page, _box(bbox), json.loads(cells), is_header, method or (methods or [""])[0],
                       list(markers or []))
                for index, page, bbox, cells, is_header, method, markers in _rows(
                    graph, "MATCH (:Table {id: $id})-[:HAS_ROW]->(r:Row) RETURN r.index, r.page, r.bbox, r.cells, "
                           "r.is_header, r.grid_method, r.markers ORDER BY r.index", {"id": tid})]
        notes = [(m, text) for m, text in _rows(
            graph, "MATCH (:Table {id: $id})-[:HAS_FOOTNOTE]->(f:Footnote) RETURN f.marker, f.text ORDER BY f.id",
            {"id": tid})]
        trust, reason = table_trust(rows, flagged)
        tables.append(TableRec(id=tid, number=num, caption=caption or "", pages=list(pages or []),
                               crops=[c for c in (crops or []) if c], header_crop=header_crop, rows=rows,
                               footnotes=notes, clause_id=cid,
                               referenced_by=list(dict.fromkeys(referenced_by.get(tid, []))),
                               status=status or "skeleton", trust=trust, trust_reason=reason,
                               suspect_spans=table_suspect_spans(rows, flagged_text)))
    tables.sort(key=lambda t: (t.pages[0] if t.pages else 0, t.id))
    return Skeleton2(doc_id=doc_id, clauses=sorted(clauses.values(), key=lambda c: c.order), tables=tables)
