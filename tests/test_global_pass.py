"""Global pass, parts 2-4 with a fake model: commentary linking, inferred edges and entity
resolution with adjudication. A small invented graph (no text from any real code);
embeddings are set by hand and the encoder is a keyword stub."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.graph import connection
from app.graph.merges import merged_map, rename_symbols, resolve_id, units_conflict
from app.ingest.global_pass.run import GlobalOptions, run_global
from app.ingest.global_pass.schemas import CommentaryChoice, InferredLinks, MergeDecision
from app.llm import Reply

GRAPH = "code_test_global_pass"


class KeywordEncoder:
    name, dimension = "keyword-stub", 4

    def encode(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] if "concrete" in t.lower() else [0.0, 1.0, 0.0, 0.0] for t in texts]


def _clause(cid, number, title, text, stream, page, vec):
    return {"id": cid, "number": number, "title": title, "text": text, "stream": stream, "page": page,
            "unresolved_refs": [], "v": vec}


@pytest.fixture
def graph():
    g = connection.get_db().select_graph(GRAPH)
    try:
        g.delete()
    except Exception:
        pass
    g = connection.get_db().select_graph(GRAPH)
    clauses = [
        _clause("2", "2", "Materials", "General material rules.", "provision", 10, [0.5, 0.5, 0, 0]),
        _clause("2.1", "2.1", "Concrete Strength", "Use the default compressive strength of concrete.", "provision",
                11, [1, 0.1, 0, 0]),
        _clause("2.2", "2.2", "Steel", "Steel rules.", "provision", 12, [0, 0, 1, 0]),
        _clause("3", "3", "Walls", "Wall rules.", "provision", 20, [0, 0, 0, 1]),
        _clause("3.1", "3.1", "Wall Strength", "Wall strength from concrete properties.", "provision", 21,
                [0.9, 0, 0.1, 0]),
        _clause("C2.1.5", "2.1.5", "Historic Concrete", "Older concrete was weaker.", "commentary", 40, [1, 0, 0, 0]),
        _clause("C9.9", "9.9", "Odd Note", "A note on steel.", "commentary", 41, [0, 0, 0.9, 0.1]),
    ]
    g.query("UNWIND $rows AS r CREATE (c:Clause) SET c = r, c.embedding = vecf32(r.v) REMOVE c.v", {"rows": clauses})
    g.query("MATCH (c:Clause) WHERE c.id CONTAINS '.' AND NOT c.id STARTS WITH 'C' "
            "MATCH (p:Clause {id: split(c.id, '.')[0]}) CREATE (c)-[:PART_OF]->(p)")
    g.query("CREATE (:Table {id: 'Table 2-1', caption: 'Table 2-1 Default concrete strength by year', page: 40})")
    g.query("MATCH (c:Clause {id: 'C2.1.5'}), (t:Table {id: 'Table 2-1'}) CREATE (c)-[:CONTAINS]->(t)")
    params = [
        {"id": "f_c", "symbol": "f_c", "description": "Concrete strength, see Section 3.1", "source": "notation",
         "v": [0, 0, 0, 1]},
        {"id": "E_d", "symbol": "E_d", "description": "Total energy dissipated in one cycle of response",
         "units": "in-kips", "source": "notation", "v": [0, 0, 1, 0]},
        {"id": "Sum_E_d", "symbol": "ΣE_d", "description": "Total energy dissipated in one cycle of response.",
         "units": "in-kips", "source": "notation", "v": [0, 0, 1, 0.01]},
        {"id": "V_a", "symbol": "V_a", "description": "Shear on the wall", "source": "clause", "v": [1, 0, 0, 0]},
        {"id": "V_b", "symbol": "V_b", "description": "Wall shear", "source": "clause", "v": [0.99, 0.05, 0, 0]},
        {"id": "K_1", "symbol": "K_1", "description": "Stiffness", "units": "kips", "source": "clause",
         "v": [0, 1, 0, 0]},
        {"id": "K_2", "symbol": "K_2", "description": "Stiffness", "units": "in.", "source": "clause",
         "v": [0, 1, 0, 0]},
        {"id": "P_x", "symbol": "P_x", "description": "Load in x", "source": "clause", "v": [0.6, 0.6, 0.5, 0]},
        {"id": "P_y", "symbol": "P_y", "description": "Load in y", "source": "clause", "v": [0.6, 0.6, 0.48, 0]},
    ]
    g.query("UNWIND $rows AS r CREATE (p:Parameter) SET p = r, p.embedding = vecf32(r.v) REMOVE p.v", {"rows": params})
    g.query("MATCH (c:Clause {id: '3.1'}) CREATE (c)-[:CONTAINS]->(:Equation {id: 'Equation 3-1', number: '3-1', "
            "sympy: 'M = V_b*2', page: 21})")
    g.query("MATCH (e:Equation {id: 'Equation 3-1'}), (v:Parameter {id: 'V_b'}) CREATE (e)-[:USES]->(v)")
    for pid in ("V_a", "V_b", "K_1", "K_2", "P_x", "P_y"):
        g.query("MATCH (c:Clause {id: '3.1'}), (p:Parameter {id: $id}) CREATE (c)-[:DEFINES]->(p)", {"id": pid})
    g.query("MATCH (c:Clause {id: '2.1'}), (p:Parameter {id: 'E_d'}) CREATE (c)-[:DEFINES]->(p)")
    yield g
    g.delete()


ANSWERS = {
    CommentaryChoice: lambda text: {"choice": "2.1", "confidence": 0.9, "reason": "explains concrete strength"}
    if "C2.1.5" in text else {"choice": "2.2", "confidence": 0.4, "reason": "both mention steel"},
    InferredLinks: lambda text: {"links": [
        {"clause_id": "3.1", "edge_type": "uses_value_from", "confidence": 0.85, "reason": "uses concrete strength"},
        {"clause_id": "2.1", "edge_type": "uses_value_from", "confidence": 0.5, "reason": "maybe"}]}
    if "Table 2-1" in text else {"links": []},
    MergeDecision: lambda text: {"decision": "merge", "survivor": "V_a", "merge_ids": ["V_b"], "confidence": 0.9,
                                 "reason": "same wall shear"}
    if "V_a" in text else {"decision": "keep_separate", "survivor": "", "merge_ids": [], "confidence": 0.5,
                           "reason": "different directions"},
}


def fake_transport(seen: list):
    async def send(spec):
        text = spec.content[0]["text"]
        seen.append((spec.output.__name__, text))
        body = ANSWERS[spec.output](text)
        return Reply(text=json.dumps(body), model="fake-model", stop_reason="end_turn",
                     usage={"input_tokens": 100, "output_tokens": 20})
    return send


def run(graph, tmp_path, seen=None, **options):
    seen = [] if seen is None else seen
    return asyncio.run(run_global(graph, GlobalOptions(**options), transport=fake_transport(seen),
                                  encoder=KeywordEncoder(), cache_dir=tmp_path, log=lambda m: None))


def rows(graph, cypher, params=None):
    return graph.ro_query(cypher, params or {}).result_set


def test_dry_run_prices_the_calls_and_writes_nothing(graph, tmp_path):
    seen = []
    report = run(graph, tmp_path, seen, dry_run=True)
    assert seen == []
    parts = report["parts"]
    assert parts["commentary"]["calls"] == 2 and parts["inferred"]["calls"] == 3  # Table 2-1, f_c, Sum_E_d
    assert parts["resolve"]["exact_clusters"] == 1 and parts["resolve"]["calls"] == 2
    assert report["estimated_usd"] > 0
    assert rows(graph, "MATCH ()-[e {method: 'inferred'}]->() RETURN count(e)") == [[0]]
    assert rows(graph, "MATCH (n) WHERE n:Finding OR n:MergedId RETURN count(n)") == [[0]]


def test_commentary_is_linked_by_a_confident_pick_and_the_rest_goes_to_review(graph, tmp_path):
    seen = []
    report = run(graph, tmp_path, seen, parts=["commentary"])
    prompt = next(t for kind, t in seen if kind == "CommentaryChoice" and "C2.1.5" in t)
    assert "id: 2.1 | why: parent by number" in prompt  # the nearest provision up the number comes first
    edge = rows(graph, "MATCH (:Clause {id: 'C2.1.5'})-[e:COMMENTARY_ON]->(p) RETURN p.id, e.method, e.confidence, "
                       "e.reason")
    assert edge == [["2.1", "inferred", 0.9, "explains concrete strength"]]
    finding = rows(graph, "MATCH (f:Finding {id: 'commentary_link:C9.9'}) RETURN f.severity, f.proposal")
    assert finding[0][0] == "medium" and json.loads(finding[0][1])["edges"][0]["dst"] == "2.2"
    assert report["parts"]["commentary"]["still_unlinked"] == 1


def test_unlinked_items_get_inferred_edges_the_agent_labels(graph, tmp_path):
    from app.agent.graph_tools import get_dependencies

    seen = []
    run(graph, tmp_path, seen, parts=["inferred"])
    param_prompt = next(t for kind, t in seen if kind == "InferredLinks" and "f_c" in t)
    assert "id: 3.1 | why: named in its text" in param_prompt
    stored = rows(graph, "MATCH (c:Clause)-[e:REFERENCES]->(:Table {id: 'Table 2-1'}) RETURN c.id, e.type, e.method")
    assert stored == [["3.1", "uses_value_from", "inferred"]]
    weak = json.loads(rows(graph, "MATCH (f:Finding {id: 'inferred_edge:Table:Table 2-1'}) RETURN f.proposal")[0][0])
    assert [e["src"] for e in weak["edges"]] == ["2.1"]
    assert rows(graph, "MATCH (f:Finding {id: 'inferred_edge:Parameter:f_c'}) RETURN f.proposal") == [[None]]
    deps = get_dependencies(graph, "3.1", "upstream", 1)
    assert any(x.label == "inferred" and x.target == "3.1|REFERENCES|Table 2-1" for x in deps.labels)


def test_duplicates_are_merged_and_old_ids_still_resolve(graph, tmp_path):
    from app.agent.facts import Fact, MemoryFacts, ResolvedStore
    from app.agent.graph_tools import find_node

    report = run(graph, tmp_path, parts=["resolve"])
    merged = merged_map(graph)
    assert merged == {"Sum_E_d": "E_d", "V_b": "V_a"}  # exact tier, then Claude's confident merge
    assert {m["method"] for m in report["parts"]["resolve"]["merges"]} == {"exact_description", "claude_adjudicated"}
    assert rows(graph, "MATCH (:Equation {id: 'Equation 3-1'})-[:USES]->(p) RETURN p.id") == [["V_a"]]
    assert rows(graph, "MATCH (e:Equation {id: 'Equation 3-1'}) RETURN e.sympy") == [["M = V_a*2"]]
    assert "V_b" in rows(graph, "MATCH (p:Parameter {id: 'V_a'}) RETURN p.aliases")[0][0]
    assert find_node(graph, "V_b")[1]["id"] == "V_a" and resolve_id(graph, "V_b") == "V_a"
    store = MemoryFacts()
    store.set(Fact("V_b", 12.0))
    assert ResolvedStore(store, merged).all()["V_a"].value == 12.0
    # never paired: different units; the low-confidence keep-separate goes to review
    assert rows(graph, "MATCH (p:Parameter) WHERE p.id IN ['K_1', 'K_2'] RETURN count(p)") == [[2]]
    assert rows(graph, "MATCH (f:Finding {kind: 'merge'}) RETURN f.subjects") == [[["P_x", "P_y"]]]


def test_a_rerun_from_cache_changes_nothing(graph, tmp_path):
    parts = ["commentary", "inferred", "checks"]  # resolve changes the graph it reads, by design
    run(graph, tmp_path, parts=parts)
    def count():
        return rows(graph, "MATCH ()-[e]->() RETURN count(e)")[0][0], rows(graph, "MATCH (n) RETURN count(n)")[0][0]

    before = count()
    seen = []
    report = run(graph, tmp_path, seen, parts=parts)
    assert seen == [] and report["calls"]["calls"] == 0  # every answer came from the cache
    assert count() == before


def test_a_merge_proposal_is_applied_only_when_approved(graph, tmp_path):
    from app.api.review_routes import Paths, approve

    low = dict(ANSWERS)
    ANSWERS[MergeDecision] = lambda text: {"decision": "merge", "survivor": "P_x", "merge_ids": ["P_y"],
                                           "confidence": 0.6, "reason": "maybe the same load"} \
        if "P_x" in text else low[MergeDecision](text)
    try:
        run(graph, tmp_path, parts=["resolve"])
    finally:
        ANSWERS.update(low)
    assert "P_y" not in merged_map(graph)
    assert approve(graph, "Finding", "merge:P_x|P_y", None, Paths())["applied"]
    assert merged_map(graph)["P_y"] == "P_x"


def test_merge_guards_and_symbol_renames():
    assert units_conflict("kips", "in.") and not units_conflict("in-kips", "In-Kips") and not units_conflict(None, "k")
    assert rename_symbols("f_c*A + f_ce", {"f_c": "f_prime_c"}) == "f_prime_c*A + f_ce"


def test_the_exact_tier_never_merges_ids_that_differ_only_by_case():
    from app.ingest.global_pass.resolve import Param, is_exact

    meaning = "Expected plastic moment capacity of the member"
    params = {i: Param(i, i, [], meaning, None, src, [], 1) for i, src in
              (("M_PCE", "notation"), ("M_pCE", "clause"), ("Sum_E", "notation"), ("Sigma_E", "notation"))}
    assert not is_exact(["M_PCE", "M_pCE"], params)  # goes to Claude with meanings, units and scope
    assert is_exact(["Sigma_E", "Sum_E"], params)
    params["c_1"] = Param("c_1", "c1", [], meaning, None, "notation", [], 1)
    params["C_1"] = Param("C_1", "C1", [], meaning, None, "notation", [], 1)
    assert not is_exact(["C_1", "c_1"], params)
