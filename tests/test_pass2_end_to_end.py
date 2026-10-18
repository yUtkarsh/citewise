"""Pass 2 end to end on fixture pages with a fake Claude: notation, definitions, clauses
and tables in both CLAUSE_CROPS modes, results handed to the sink as they finish, and
(when FalkorDB runs) idempotent graph writes. Never calls the API."""
import asyncio
import json

import pymupdf
import pytest

from app.ingest.pass1.run_pass1 import run_pass1
from app.ingest.pass2 import validate as v
from app.ingest.pass2.records import from_pass1
from app.ingest.pass2.run_pass2 import Pass2Options, run_pass2
from app.ingest.pass2.schemas import ClauseExtraction, DefinitionsPage, NotationPage, TableExtraction
from app.llm import Reply
from app.profile.page_map import build_page_map
from app.profile.schema import PageRange

BODY = ["text.pdf", "equations.pdf", "equations_brackets.pdf", "table.pdf"]


def payload(spec) -> dict:
    return json.loads(spec.content[-1]["text"])


def first_line(text: str) -> str:
    return next(line.strip() for line in text.splitlines() if len(line.strip()) > 8)[:40]


def fake_claude(seen: list):
    """Answers each prompt with a minimal result that passes validation."""

    async def send(spec):
        seen.append(spec)
        p = payload(spec)
        if spec.output is NotationPage:
            q = first_line(p["text_layer"])
            out = {"entries": [{"display": "Vd", "latex": "V_d", "id": "V_d", "description": q, "units": None,
                                "defined_in": [], "aliases": ["V d"], "scope_chapters": [], "source_quote": q}]}
        elif spec.output is DefinitionsPage:
            q = first_line(p["text_layer"])
            out = {"terms": [{"term": "Primary Component", "definition": q, "aliases": [], "source_quote": q}]}
        elif spec.output is ClauseExtraction:
            text = p["clause"]["text"]
            out = {"parameters": [], "equations": [], "conditions": [], "requirements": [], "exceptions": [],
                   "references": [], "symbol_corrections": [], "unmapped": []}
            if len(text) > 20:
                out["conditions"] = [{"text": "applies", "source_quote": text[:20]}]
        else:
            rows = []
            for g in p["grid"]:
                if g["header"]:
                    continue
                values = [{"col": i, "value": cell, "number": v.numbers(cell)[0], "unit": None, "formula": None}
                          for i, cell in enumerate(g["cells"]) if v.numbers(cell)]
                rows.append({"row": g["row"], "conditions": [g["cells"][0][:30] if g["cells"] else ""],
                             "values": values})
            out = {"header_parameters": [], "rows": rows, "footnote_rules": [], "blank_cells": []}
        return Reply(text=json.dumps(out), model="claude-haiku-5-5", stop_reason="end_turn",
                     usage={"input_tokens": 500, "output_tokens": 100})

    return send


class RecordingSink:
    def __init__(self):
        self.notation_entries, self.terms_written, self.results = [], [], []

    def notation(self, entries, defined_in):
        self.notation_entries += entries

    def terms(self, terms, links):
        self.terms_written += terms

    def result(self, outcome, notation_ids):
        self.results.append(outcome)


@pytest.fixture
def fixture_doc(combined_pdf, fema_profile, tmp_path):
    path = combined_pdf(*BODY, "notation.pdf", "definitions.pdf")
    doc = pymupdf.open(path)
    n = len(BODY)
    profile = fema_profile.model_copy(update={"ranges": [
        PageRange(start=1, end=n, kind="body"), PageRange(start=n + 1, end=n + 1, kind="notation"),
        PageRange(start=n + 2, end=n + 2, kind="definitions")]})
    result = run_pass1(doc, path, "fixturehash", profile, build_page_map(doc), tmp_path / "cache1",
                       tmp_path / "crops", use_docling=False, pages=list(range(1, n + 1)), log=lambda *_: None)
    return doc, profile, result


@pytest.fixture(autouse=True)
def models(monkeypatch):
    from app import config
    for name in ("MODEL_NOTATION", "MODEL_CLAUSES", "MODEL_TABLES"):
        monkeypatch.setenv(name, "claude-haiku-5-5")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def run(doc, profile, result, tmp_path, mode, sink=None, seen=None, **opts):
    seen = [] if seen is None else seen
    sink = sink or RecordingSink()
    report = asyncio.run(run_pass2(doc, "fixturehash", profile, from_pass1(result, "fixtures"), sink,
                                   Pass2Options(crop_mode=mode, **opts), tmp_path / f"state_{mode}.jsonl",
                                   transport=fake_claude(seen), cache_dir=tmp_path / "cache2", log=lambda *_: None))
    return report, sink, seen


@pytest.mark.parametrize("mode", ["always", "flagged"])
def test_fixture_pages_pass_validation_in_both_crop_modes(fixture_doc, tmp_path, mode):
    doc, profile, result = fixture_doc
    report, sink, seen = run(doc, profile, result, tmp_path, mode)
    assert sink.notation_entries and sink.terms_written
    kinds = {o.job.kind for o in sink.results}
    assert {"clause", "table"} <= kinds
    flagged = [(o.job.id, [i.as_dict() for i in o.issues]) for o in sink.results if o.status == "flagged"]
    assert not flagged
    clause_calls = [s for s in seen if s.output is ClauseExtraction]
    images = [b for s in clause_calls for b in s.content if b["type"] == "image"]
    labels = [b["text"] for s in clause_calls for b in s.content if b["type"] == "text" and b["text"].startswith("[image")]
    if mode == "always":
        assert any("clause region" in t for t in labels)
    else:
        assert not any("clause region" in t for t in labels)
        assert any("flagged line" in t for t in labels)  # the bracket page has flagged lines
    assert images
    assert report["costs"]["by_job_type"]["clause"]["api_calls"] == len(clause_calls)


def test_results_are_handed_over_one_by_one_and_resume_skips_them(fixture_doc, tmp_path):
    doc, profile, result = fixture_doc
    _, sink, seen = run(doc, profile, result, tmp_path, "always")
    calls = len(seen)
    report, sink2, seen2 = run(doc, profile, result, tmp_path, "always")
    # skeleton status is still 'skeleton' in records built from Pass 1, so jobs re-run, all from cache
    assert len(seen2) == 0
    assert report["costs"]["by_job_type"]["clause"].get("cache_hits", 0) > 0
    assert calls > 0


def test_dry_run_sends_nothing(fixture_doc, tmp_path):
    doc, profile, result = fixture_doc
    report, sink, seen = run(doc, profile, result, tmp_path, "always", dry_run=True)
    assert not seen and not sink.results
    assert report["estimate"]["clause"]["jobs"] > 0


# ---- FalkorDB --------------------------------------------------------------------------------

@pytest.fixture
def graph():
    from app.graph import connection
    try:
        connection.ping()
    except Exception:
        pytest.skip("FalkorDB is not running (docker compose up -d)")
    g = connection.get_db().select_graph("test_pass2_writer")
    yield g
    try:
        g.delete()
    except Exception:
        pass


def counts(g):
    return (g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
            g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0])


def test_graph_writes_are_idempotent_and_read_back(fixture_doc, tmp_path, graph):
    from app.graph.writers import build_rows, write
    from app.ingest.pass2.records import from_graph
    from app.ingest.pass2.run_pass2 import GraphSink

    doc, profile, result = fixture_doc
    write(graph, build_rows(result, profile, "fixtures", "fixturehash"))
    skel = from_graph(graph, "fixtures")
    assert {c.id for c in skel.clauses} == {c.id for c in result.clauses}
    assert all(c.regions for c in skel.clauses)
    sink = GraphSink(graph)
    asyncio.run(run_pass2(doc, "fixturehash", profile, skel, sink, Pass2Options(), tmp_path / "s.jsonl",
                          transport=fake_claude([]), cache_dir=tmp_path / "c", log=lambda *_: None))
    first = counts(graph)
    statuses = {s for (s,) in graph.ro_query("MATCH (c:Clause) RETURN DISTINCT c.status").result_set}
    assert statuses <= {"extracted", "flagged"}
    asyncio.run(run_pass2(doc, "fixturehash", profile, skel, sink, Pass2Options(redo=True), tmp_path / "s.jsonl",
                          transport=fake_claude([]), cache_dir=tmp_path / "c", log=lambda *_: None))
    assert counts(graph) == first
    assert graph.ro_query("MATCH (p:Parameter {id: 'V_d'}) RETURN count(p)").result_set[0][0] == 1
    assert graph.ro_query("MATCH (:Clause)-[:HAS_CONDITION]->(c:Condition) RETURN count(c)").result_set[0][0] > 0


def test_re_extraction_removes_stale_children(fixture_doc, graph):
    from app.graph.pass2_writers import write_clause
    from app.graph.writers import build_rows, write
    from app.ingest.pass2.jobs import Job
    from app.ingest.pass2.runner import Outcome

    doc, profile, result = fixture_doc
    write(graph, build_rows(result, profile, "fixtures", "fixturehash"))
    clause = next(c for c in from_pass1(result, "fixtures").clauses if len(c.text) > 40)
    job = Job(id=f"clause:{clause.id}", kind="clause", model_setting="model_clauses", prompt="pass2_clause",
              output=ClauseExtraction, text="", clause=clause)

    def outcome(n):
        conditions = [{"text": f"c{i}", "source_quote": clause.text[:10]} for i in range(n)]
        out = ClauseExtraction.model_validate({"parameters": [], "equations": [], "conditions": conditions,
                                               "requirements": [], "exceptions": [], "references": [],
                                               "symbol_corrections": [], "unmapped": []})
        return Outcome(job, "extracted", out, [], None)

    def conditions():
        return graph.ro_query("MATCH (:Clause {id: $id})-[:HAS_CONDITION]->(c) RETURN count(c)",
                              {"id": clause.id}).result_set[0][0]

    write_clause(graph, outcome(3), set())
    assert conditions() == 3
    write_clause(graph, outcome(1), set())
    assert conditions() == 1
    assert graph.ro_query("MATCH (c:Condition) RETURN count(c)").result_set[0][0] == 1
