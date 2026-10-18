"""Global pass, inferred GIVES edges with a fake model: a table column linked to the
parameter equations use, never across units; table readers ignore the inferred edge.
A small invented graph (no text from any real code)."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.graph import connection
from app.ingest.global_pass.run import GlobalOptions, run_global
from app.llm import Reply

GRAPH = "code_test_global_gives"


class Encoder:
    name, dimension = "stub", 3

    def encode(self, texts):
        return [[1.0, 0.0, 0.0] if "strength" in t.lower() else [0.0, 1.0, 0.0] for t in texts]


@pytest.fixture
def graph():
    g = connection.get_db().select_graph(GRAPH)
    try:
        g.delete()
    except Exception:
        pass
    g = connection.get_db().select_graph(GRAPH)
    g.query("CREATE (:Clause {id: '2.1', title: 'Strength', stream: 'provision', page: 5})")
    g.query("CREATE (:Table {id: 'Table 2-1', caption: 'Table 2-1 Default strength by era', page: 6})")
    g.query("UNWIND $rows AS r CREATE (p:Parameter) SET p = r, p.embedding = vecf32(r.v) REMOVE p.v", {"rows": [
        {"id": "f_c", "symbol": "f_c", "description": "Compressive strength", "units": "psi", "source": "notation",
         "v": [1, 0, 0]},
        {"id": "L_e", "symbol": "L_e", "description": "Effective strength length", "units": "in.", "source": "notation",
         "v": [1, 0, 0]},
        {"id": "fc_walls", "symbol": "fc_walls", "units": "psi", "source": "table", "v": [0, 1, 0]},
        {"id": "era", "symbol": "era", "source": "table", "v": [0, 1, 0]}]})
    for pid, col in (("era", 0), ("fc_walls", 1)):
        g.query("MATCH (t:Table {id: 'Table 2-1'}), (p:Parameter {id: $p}) "
                "CREATE (t)-[:GIVES {col: $c, cols: [$c], header_text: $p, units: p.units}]->(p)", {"p": pid, "c": col})
    g.query("MATCH (t:Table {id: 'Table 2-1'}) CREATE (t)-[:HAS_ROW]->(:Row {id: 'Table 2-1#r1', index: 1, "
            "cells: '[\"Old\", \"2000\"]', conditions: '[\"Old\"]', values: $v, is_header: false})",
            {"v": json.dumps([{"col": 1, "value": "2000", "number": 2000.0, "unit": "psi", "formula": None}])})
    g.query("MATCH (c:Clause {id: '2.1'}) CREATE (c)-[:CONTAINS]->(e:Equation {id: 'Equation 2-1', sympy: 'P = f_c*L_e'})")
    for pid in ("f_c", "L_e"):
        g.query("MATCH (e:Equation {id: 'Equation 2-1'}), (p:Parameter {id: $p}) CREATE (e)-[:USES]->(p)", {"p": pid})
    yield g
    g.delete()


def run(graph, tmp_path, answer, seen=None, **kw):
    seen = [] if seen is None else seen

    async def send(spec):
        seen.append(spec.content[0]["text"])
        return Reply(json.dumps(answer), "fake-model", "end_turn", {"input_tokens": 10, "output_tokens": 5})
    return asyncio.run(run_global(graph, GlobalOptions(parts=["gives"], **kw), transport=send, encoder=Encoder(),
                                  cache_dir=tmp_path, log=lambda m: None))


def test_a_column_is_linked_to_the_parameter_its_values_supply(graph, tmp_path):
    from app.agent.compute import table_headers
    from app.agent.graph_tools import get_dependencies

    seen = []
    answer = {"links": [{"header_id": "fc_walls", "parameter_id": "f_c", "confidence": 0.9,
                         "reason": "default compressive strengths"}]}
    report = run(graph, tmp_path, answer, seen)
    walls = seen[0].split("Column header fc_walls")[1]
    assert "id: f_c" in walls and "id: L_e" not in walls  # in. vs psi: never a candidate for a psi column
    assert report["parts"]["gives"]["edges"] == 1
    edge = graph.ro_query("MATCH (:Table {id: 'Table 2-1'})-[e:GIVES {method: 'inferred'}]->(p) "
                          "RETURN p.id, e.via, e.col, e.units").result_set
    assert edge == [["f_c", "fc_walls", 1, "psi"]]
    assert [h.id for h in table_headers(graph, "Table 2-1")] == ["era", "fc_walls"]  # columns unchanged
    deps = get_dependencies(graph, "f_c", "upstream", 1)
    assert any(x.label == "inferred" for x in deps.labels) and "Table 2-1" in [n["id"] for n in deps.data["nodes"]]


def test_low_confidence_links_go_to_review(graph, tmp_path):
    answer = {"links": [{"header_id": "fc_walls", "parameter_id": "f_c", "confidence": 0.5, "reason": "maybe"}]}
    run(graph, tmp_path, answer)
    assert graph.ro_query("MATCH ()-[e:GIVES {method: 'inferred'}]->() RETURN count(e)").result_set == [[0]]
    proposal = json.loads(graph.ro_query("MATCH (f:Finding {id: 'inferred_gives:Table 2-1'}) RETURN f.proposal"
                                         ).result_set[0][0])
    assert proposal["edges"][0]["props"]["via"] == "fc_walls"
