"""Grid revision on a grid-wrong reject (SPEC Step 4), with a fake model (never the API).

The synthetic table prints two rows of values; its Pass 1 grid is wrong (the row label
is merged with the first value), the way a text-line grid can be."""
from __future__ import annotations

import asyncio
import json

import pymupdf
import pytest

from app.graph import connection
from app.ingest.pass2.grid_revision import GridRevision, check_grid, store_grid, text_layer_numbers
from app.ingest.pass2.jobs import table_jobs
from app.ingest.pass2.notation import NotationDict
from app.ingest.pass2.records import RowRec, TableRec, from_graph
from app.ingest.pass2 import validate as v
from app.ingest.pass2.schemas import CellValue
from app.llm import Reply

GRAPH = "code_test_grid"
LINES = [(72, 100, "Class      a      b"), (72, 120, "A      0.8      1.2"), (72, 140, "B      1.0      1.5"),
         (72, 160, "NOTE: interpolation shall be used for intermediate values of 2.5")]
GOOD = {"columns": ["Class", "a", "b"],
        "header_rows": [{"cells": [{"col": 0, "span": 1, "text": "Class"}, {"col": 1, "span": 1, "text": "a"},
                                   {"col": 2, "span": 1, "text": "b"}]}],
        "rows": [{"cells": [{"col": 0, "text": "A"}, {"col": 1, "text": "0.8"}, {"col": 2, "text": "1.2"}]},
                 {"cells": [{"col": 0, "text": "B"}, {"col": 1, "text": "1.0"}, {"col": 2, "text": "1.5"}]}]}
BAD = {**GOOD, "rows": GOOD["rows"][:1]}  # row B (1.0, 1.5) lost


@pytest.fixture
def pdf(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    for x, y, text in LINES:
        page.insert_text((x, y), text, fontsize=10)
    path = tmp_path / "grid.pdf"
    doc.save(path)
    return path


def pass1_table(pdf_path) -> TableRec:
    doc = pymupdf.open(pdf_path)
    boxes = {}
    for b in doc[0].get_text("blocks"):
        boxes[round(b[3])] = (b[0], b[1], b[2], b[3])
    ys = sorted(boxes)
    rows = [RowRec(0, 1, boxes[ys[0]], ["Class a b"], True, "text_lines"),
            RowRec(1, 1, boxes[ys[1]], ["A 0.8", "1.2"], False, "text_lines"),  # wrong: label merged with a value
            RowRec(2, 1, boxes[ys[2]], ["B 1.0", "1.5"], False, "text_lines"),
            RowRec(3, 1, boxes[ys[3]], [LINES[3][2]], False, "text_lines")]
    return TableRec(id="Table 9-1", number="9-1", caption="Table 9-1 Factors", pages=[1], crops=[], header_crop=None,
                    rows=rows, footnotes=[], clause_id=None)


def test_the_text_layer_numbers_a_grid_must_hold(pdf):
    assert text_layer_numbers(pymupdf.open(pdf), pass1_table(pdf)) == ["0.8", "1.2", "1.0", "1.5"]  # not the note's 2.5


def test_a_grid_missing_numbers_or_with_bad_columns_fails():
    required = ["0.8", "1.2", "1.0", "1.5"]
    assert check_grid(GridRevision(**GOOD), required) == []
    issues = check_grid(GridRevision(**BAD), required)
    assert [i.code for i in issues] == ["grid_numbers"] and issues[0].retry and "1.0, 1.5" in issues[0].detail
    shifted = json.loads(json.dumps(GOOD))
    shifted["rows"][0]["cells"][2]["col"] = 3
    assert "grid_structure" in [i.code for i in check_grid(GridRevision(**shifted), required)]


@pytest.fixture
def graph(pdf):
    try:
        connection.ping()
    except Exception:
        pytest.skip("FalkorDB is not running (docker compose up -d)")
    g = connection.get_db().select_graph(GRAPH)
    try:
        g.delete()
    except Exception:
        pass
    g = connection.get_db().select_graph(GRAPH)
    t = pass1_table(pdf)
    g.query("CREATE (:Table {id: $id, number: '9-1', caption: $cap, pages: [1], crops: [], page: 1, status: 'flagged'})",
            {"id": t.id, "cap": t.caption})
    g.query("MATCH (t:Table {id: $id}) UNWIND $rows AS r CREATE (t)-[:HAS_ROW {index: r.index}]->(:Row {id: r.id, "
            "index: r.index, page: 1, bbox: r.bbox, cells: r.cells, is_header: r.h, markers: [], grid_method: 'text_lines'})",
            {"id": t.id, "rows": [{"id": f"{t.id}#r{r.index}", "index": r.index, "bbox": list(r.bbox),
                                   "cells": json.dumps(r.cells), "h": r.is_header} for r in t.rows]})
    yield g
    g.delete()


def test_a_stored_grid_replaces_the_rows_and_the_table_is_extracted_as_image_trust(graph, pdf):
    table = pass1_table(pdf)
    store_grid(graph, table, GridRevision(**GOOD), ["0.8", "1.2", "1.0", "1.5"])
    t = next(x for x in from_graph(graph, GRAPH).tables if x.id == "Table 9-1")
    assert [r.cells for r in t.rows] == [["Class", "a", "b"], ["A", "0.8", "1.2"], ["B", "1.0", "1.5"]]
    assert t.trust == "image" and all(r.grid_method == "image" for r in t.rows) and t.text_numbers == [0.8, 1.2, 1.0, 1.5]
    pass1 = json.loads(graph.ro_query("MATCH (t:Table {id: 'Table 9-1'}) RETURN t.grid_pass1").result_set[0][0])
    assert pass1[1]["cells"] == ["A 0.8", "1.2"]  # the Pass 1 grid is kept for traceability
    grid = json.loads(table_jobs(t, None, NotationDict())[0].text)["grid"]  # the extraction's input is the new grid
    assert grid[1]["cells"] == [{"col": 0, "text": "A"}, {"col": 1, "text": "0.8"}, {"col": 2, "text": "1.2"}]
    # numbers are confirmed against the text layer, not the image grid: 0.9 goes to the second read
    read = CellValue(col=1, value="0.9", number=0.9, unit=None, formula=None)
    assert v.number_issue(read, t.rows[1], t) is not None
    assert v.number_issue(CellValue(col=1, value="0.8", number=0.8, unit=None, formula=None), t.rows[1], t) is None


def fake(*answers):
    sent = []

    async def send(spec):
        sent.append(spec)
        return Reply(text=json.dumps(answers[min(len(sent), len(answers)) - 1]), model="claude-sonnet-5-5",
                     stop_reason="end_turn", usage={"input_tokens": 100, "output_tokens": 50})
    return send, sent


def revise(graph, pdf, tmp_path, monkeypatch, *answers):
    from app.api import review_routes as rr
    monkeypatch.setattr(rr, "_document_hash", lambda g: "hash")
    monkeypatch.setattr(rr, "_pdf_for", lambda name, h: pdf)
    send, sent = fake(*answers)
    result = asyncio.run(rr.revise_grid(graph, GRAPH, "Table 9-1", "the grid is wrong: labels are merged with values",
                                        rr.Paths(jobs_dir=tmp_path, cache_dir=tmp_path), send))
    return result, sent


def test_a_grid_wrong_reject_rebuilds_the_grid(graph, pdf, tmp_path, monkeypatch):
    result, sent = revise(graph, pdf, tmp_path, monkeypatch, GOOD)
    assert result["ok"] and len(sent) == 1 and "the grid is wrong" in sent[0].content[-1]["text"]
    assert result["before"][1]["cells"] == ["A 0.8", "1.2"] and result["after"][1]["cells"] == ["A", "0.8", "1.2"]
    cells = graph.ro_query("MATCH (:Table {id: 'Table 9-1'})-[:HAS_ROW]->(r) RETURN r.cells ORDER BY r.index").result_set
    assert json.loads(cells[1][0]) == ["A", "0.8", "1.2"]


def test_a_bad_grid_is_retried_once_then_refused_and_the_old_grid_stays(graph, pdf, tmp_path, monkeypatch):
    result, sent = revise(graph, pdf, tmp_path, monkeypatch, BAD, BAD)
    assert not result["ok"] and len(sent) == 2 and result["after"] is None
    assert "numbers printed in the table are missing" in sent[1].content[-1]["text"]  # the retry says why
    cells = graph.ro_query("MATCH (:Table {id: 'Table 9-1'})-[:HAS_ROW]->(r) RETURN r.cells ORDER BY r.index").result_set
    assert json.loads(cells[1][0]) == ["A 0.8", "1.2"]


def test_a_grid_wrong_note_is_recognised():
    from app.api.review_routes import grid_is_wrong
    assert grid_is_wrong("The grid is wrong: site class is merged with the first value", False)
    assert grid_is_wrong("columns are shifted by one", False)
    assert grid_is_wrong("anything", True)
    assert not grid_is_wrong("the value in row 3 should be 1.2", False)
