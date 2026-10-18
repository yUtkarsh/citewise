"""Orphan cleanup deletes only extraction leftovers. Skips without FalkorDB."""
import pytest

from app.graph import connection
from scripts.cleanup_orphans import cleanup

GRAPH = "test_cleanup_orphans"


@pytest.fixture
def graph():
    try:
        connection.ping()
    except Exception:
        pytest.skip("FalkorDB is not running (docker compose up -d)")
    g = connection.get_db().select_graph(GRAPH)
    g.query("CREATE (:Parameter {id: 'left_clause', source: 'clause'}), (:Parameter {id: 'left_table', source: 'table'}), "
            "(:Parameter {id: 'dictionary', source: 'notation'}), (:Term {id: 'term:glossary'}), "
            "(:Clause {id: '1'})-[:DEFINES]->(:Parameter {id: 'used', source: 'clause'})")
    yield g
    g.delete()


def test_only_unlinked_extraction_leftovers_are_deleted(graph):
    dry = cleanup(graph, dry_run=True)
    assert dry["leftover_parameters"] == 2 and dry["deleted"] == 0
    assert graph.ro_query("MATCH (p:Parameter) RETURN count(p)").result_set[0][0] == 4
    done = cleanup(graph)
    assert sorted(done["ids"]) == ["left_clause", "left_table"] and done["deleted"] == 2
    kept = {r[0] for r in graph.ro_query("MATCH (n) RETURN n.id").result_set}
    assert kept == {"dictionary", "term:glossary", "1", "used"}
    assert done["orphans_after"] == {"Parameter/notation": 1, "Term": 1}
