"""Step 3a.9: graph writes are idempotent (UNWIND ... MERGE). Skips without FalkorDB."""
import pymupdf
import pytest

from app.graph import connection
from app.graph.schema import NODE_LABELS, graph_name
from app.graph.writers import build_rows, write
from app.ingest.pass1.run_pass1 import run_pass1
from app.profile.page_map import build_page_map
from tests.conftest import FIXTURES

TEST_GRAPH = "test_pass1_writer"


@pytest.fixture
def graph():
    try:
        connection.ping()
    except Exception:
        pytest.skip("FalkorDB is not running (docker compose up -d)")
    g = connection.get_db().select_graph(TEST_GRAPH)
    yield g
    try:
        g.delete()
    except Exception:
        pass


def counts(g) -> tuple[int, int]:
    nodes = g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
    edges = g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    return nodes, edges


def test_rewriting_creates_no_duplicates(graph, fema_profile, tmp_path):
    doc = pymupdf.open(FIXTURES / "commentary.pdf")
    result = run_pass1(doc, FIXTURES / "commentary.pdf", "test", fema_profile, build_page_map(doc),
                       tmp_path / "cache", tmp_path / "crops", use_docling=False, pages=[1], log=lambda *_: None)
    rows = build_rows(result, fema_profile, TEST_GRAPH, "hash")
    write(graph, rows)
    first = counts(graph)
    write(graph, rows)
    assert counts(graph) == first
    assert first[0] == sum(len(v) for v in rows.nodes.values())

    clause = graph.ro_query("MATCH (c:Clause) RETURN c LIMIT 1").result_set[0][0].properties
    for key in ("page", "bbox", "source_quote", "confidence", "extraction_method", "review_status", "schema_version"):
        assert key in clause
    linked = graph.ro_query("MATCH (:Clause)-[r:COMMENTARY_ON]->(:Clause) RETURN count(r)").result_set[0][0]
    assert linked > 0


def test_graph_name_is_one_per_document(tmp_path):
    assert graph_name(tmp_path / "FEMA 356.pdf") == "code_fema_356"
    assert "Clause" in NODE_LABELS
