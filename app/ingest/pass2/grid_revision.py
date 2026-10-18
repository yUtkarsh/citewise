"""Grid revision (SPEC Step 4, reject): when a reviewer says a table's grid is wrong, one
MODEL_REVIEW call reads the grid's structure from the table image (column names in order,
header rows with spanning cells, each body row's cells as {col, text}), the result is
checked against the text layer, and it replaces the Pass 1 grid. The table is then
extracted again against the new grid as an image-trust table.

Check: every plain number in the table's text layer (outside flagged lines, note lines
and footnote markers) must appear in some cell of the new grid; every cell's column and
every header span must fit the columns; a row may not use a column twice.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field
import pymupdf

from app.ingest.pass2 import images as img
from app.ingest.pass2.jobs import PLAIN_NUMBER, Job
from app.ingest.pass2.records import RowRec, TableRec
from app.ingest.pass2.validate import Issue, mostly_words, numbers

GRID_SCHEMA_VERSION = "grid-v1"
NUMBER_TOL = 1e-9


class HeaderCell(BaseModel):
    col: int = Field(description="0-based index of the first column the cell covers")
    span: int = Field(description="How many columns the cell covers (1 for a single column)")
    text: str


class HeaderRow(BaseModel):
    cells: list[HeaderCell]


class BodyCell(BaseModel):
    col: int = Field(description="0-based column index")
    text: str = Field(description="The cell as printed in the image")


class BodyRow(BaseModel):
    cells: list[BodyCell]


class GridRevision(BaseModel):
    columns: list[str] = Field(description="A short name for each column, left to right")
    header_rows: list[HeaderRow]
    rows: list[BodyRow] = Field(description="Body rows top to bottom; notes below the table are not rows")


# ---- the text layer's numbers ---------------------------------------------------------------------

def _region(rows: list[RowRec], page: int):
    boxes = [r.bbox for r in rows if r.page == page]
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def is_note_row(row: RowRec) -> bool:
    """A single wide cell of words: a caption, note or footnote line, not a table row."""
    cells = [c for c in row.cells if c.strip()]
    return len(cells) == 1 and mostly_words(cells[0]) and len(cells[0]) > 25


def text_layer_numbers(doc: pymupdf.Document, table: TableRec) -> list[str]:
    """Plain numbers printed in the table's region, outside flagged lines, note lines and
    footnote markers: the numbers any grid of this table must hold."""
    markers = {m for m, _ in table.footnotes} | {m for r in table.rows for m in r.markers}
    suspect_words = {w for s in table.suspect_spans for w in s.split()}
    out = []
    for page in sorted({r.page for r in table.rows}):
        x0, y0, x1, y1 = _region(table.rows, page)
        notes = [r.bbox for r in table.rows if r.page == page and is_note_row(r)]
        for w in doc[page - 1].get_text("words"):
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            if not (x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1) or not PLAIN_NUMBER.match(w[4]):
                continue
            if w[4] in markers or w[4] in suspect_words or any(b[1] - 1 <= cy <= b[3] + 1 for b in notes):
                continue
            out.append(w[4])
    return out


# ---- the call -------------------------------------------------------------------------------------

def grid_job(table: TableRec, note: str) -> Job:
    payload = {
        "table": {"id": table.id, "caption": table.caption},
        "reviewer_note": note,
        "pass1_grid_for_reference_only": [{"row": r.index, "header": r.is_header, "cells": [c for c in r.cells if c.strip()]}
                                          for r in table.rows],
    }
    images = [ref for i, path in enumerate(table.crops) if (ref := img.stored(path, f"table crop {i + 1}"))]
    if table.header_crop and (ref := img.stored(table.header_crop, "header rows")):
        images.append(ref)
    return Job(id=f"grid:{table.id}", kind="grid", model_setting="model_review", prompt="pass2_grid",
               output=GridRevision, text=json.dumps(payload, ensure_ascii=False, indent=1), images=images,
               max_tokens=32000, table=table, rows=table.rows)


def check_grid(rev: GridRevision, required: list[str]) -> list[Issue]:
    issues = []
    width = len(rev.columns)
    if width == 0 or not rev.rows:
        return [Issue("grid_structure", "the grid needs at least one column and one body row")]
    for i, h in enumerate(rev.header_rows):
        for c in h.cells:
            if c.col < 0 or c.span < 1 or c.col + c.span > width:
                issues.append(Issue("grid_structure", f"header row {i}: cell {c.text[:30]!r} at col {c.col} "
                                                      f"span {c.span} does not fit {width} columns"))
    for i, r in enumerate(rev.rows):
        cols = [c.col for c in r.cells]
        if any(c < 0 or c >= width for c in cols):
            issues.append(Issue("grid_structure", f"row {i}: columns {cols} do not fit {width} columns"))
        if len(cols) != len(set(cols)):
            issues.append(Issue("grid_structure", f"row {i}: a column is used twice ({cols})"))
    printed = [n for r in rev.header_rows for c in r.cells for n in numbers(c.text)] + \
              [n for r in rev.rows for c in r.cells for n in numbers(c.text)]
    missing = [w for w in required if not any(abs(n - x) <= NUMBER_TOL * max(1.0, abs(x))
                                              for x in numbers(w) for n in printed)]
    if missing:
        issues.append(Issue("grid_numbers", f"numbers printed in the table are missing from the grid: "
                                            f"{', '.join(dict.fromkeys(missing))}; every value cell must be a cell"))
    return issues


def rows_from_revision(rev: GridRevision, table: TableRec) -> list[RowRec]:
    """The revised grid as skeleton rows (one page box for all: the image has no row boxes)."""
    page = table.pages[0] if table.pages else table.rows[0].page
    box = _region(table.rows, page) if table.rows else (0, 0, 0, 0)
    width = len(rev.columns)
    out = []
    for h in rev.header_rows:
        cells = [""] * width
        for c in h.cells:
            cells[c.col] = c.text
        out.append(RowRec(len(out), page, box, cells, True, "image"))
    for r in rev.rows:
        cells = [""] * width
        for c in r.cells:
            cells[c.col] = c.text
        out.append(RowRec(len(out), page, box, cells, False, "image"))
    return out


def store_grid(graph, table: TableRec, rev: GridRevision, text_numbers: list[str]) -> list[RowRec]:
    """Replace the table's rows with the revised grid (``grid_method: image``); the Pass 1
    rows are kept on the table as ``grid_pass1`` (once: the first grid is the one traced)."""
    pass1 = [{"index": r.index, "page": r.page, "bbox": list(r.bbox), "cells": r.cells, "is_header": r.is_header,
              "grid_method": r.grid_method} for r in table.rows]
    rows = rows_from_revision(rev, table)
    graph.query("MATCH (t:Table {id: $id}) SET t.grid_pass1 = coalesce(t.grid_pass1, $pass1), t.grid = $grid, "
                "t.grid_source = 'image', t.text_numbers = $numbers, t.grid_method = ['image']",
                {"id": table.id, "pass1": json.dumps(pass1, ensure_ascii=False),
                 "grid": rev.model_dump_json(), "numbers": [float(numbers(n)[0]) for n in text_numbers if numbers(n)]})
    graph.query("MATCH (:Table {id: $id})-[:HAS_ROW]->(r:Row) DETACH DELETE r", {"id": table.id})
    graph.query("MATCH (:Table {id: $id})-[:HAS_CORRECTION]->(c) DETACH DELETE c", {"id": table.id})
    graph.query("MATCH (t:Table {id: $id}) UNWIND $rows AS r CREATE (t)-[:HAS_ROW {index: r.index}]->(:Row {"
                "id: r.id, index: r.index, page: r.page, bbox: r.bbox, cells: r.cells, is_header: r.is_header, "
                "markers: [], grid_method: 'image', status: 'skeleton', review_status: 'unreviewed', "
                "extraction_method: 'claude:grid', source: 'image'})",
                {"id": table.id, "rows": [{"id": f"{table.id}#r{r.index}", "index": r.index, "page": r.page,
                                           "bbox": list(r.bbox), "cells": json.dumps(r.cells, ensure_ascii=False),
                                           "is_header": r.is_header} for r in rows]})
    return rows


def snapshot_table(graph, table_id: str) -> dict:
    """The table's properties, rows (with their values) and corrections, so a grid
    replacement can be undone when the table's re-extraction is not kept."""
    q = lambda cy: graph.ro_query(cy, {"id": table_id}).result_set  # noqa: E731
    return {"table": dict(q("MATCH (t:Table {id: $id}) RETURN properties(t)")[0][0]),
            "rows": [(dict(p), i) for p, i in q("MATCH (:Table {id: $id})-[h:HAS_ROW]->(r:Row) RETURN properties(r), h.index")],
            "row_footnotes": q("MATCH (:Table {id: $id})-[:HAS_ROW]->(r:Row)-[:FOOTNOTE]->(f) RETURN r.id, f.id"),
            "corrections": [dict(p) for (p,) in q("MATCH (:Table {id: $id})-[:HAS_CORRECTION]->(c) RETURN properties(c)")]}


def restore_table(graph, table_id: str, snap: dict) -> None:
    """Put back what ``snapshot_table`` saved: rows, values, corrections, table properties."""
    graph.query("MATCH (:Table {id: $id})-[:HAS_ROW]->(r:Row) DETACH DELETE r", {"id": table_id})
    graph.query("MATCH (:Table {id: $id})-[:HAS_CORRECTION]->(c) DETACH DELETE c", {"id": table_id})
    graph.query("MATCH (t:Table {id: $id}) SET t = $props", {"id": table_id, "props": snap["table"]})
    graph.query("MATCH (t:Table {id: $id}) UNWIND $rows AS r CREATE (t)-[:HAS_ROW {index: r.i}]->(x:Row) SET x = r.p",
                {"id": table_id, "rows": [{"p": p, "i": i} for p, i in snap["rows"]]})
    graph.query("UNWIND $pairs AS pr MATCH (r:Row {id: pr[0]}), (f:Footnote {id: pr[1]}) CREATE (r)-[:FOOTNOTE]->(f)",
                {"pairs": [list(x) for x in snap["row_footnotes"]]})
    graph.query("MATCH (t:Table {id: $id}) UNWIND $cs AS c CREATE (t)-[:HAS_CORRECTION]->(x:SymbolCorrection) SET x = c",
                {"id": table_id, "cs": snap["corrections"]})


def grid_view(rows: list[dict] | list[RowRec]) -> list[dict]:
    """Rows as {header, cells} for the review page's before-and-after tables."""
    out = []
    for r in rows:
        cells = r.cells if isinstance(r, RowRec) else r["cells"]
        header = r.is_header if isinstance(r, RowRec) else r["is_header"]
        out.append({"header": bool(header), "cells": list(cells)})
    return out
