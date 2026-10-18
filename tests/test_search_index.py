"""Search indexes (SPEC Step 3c.5) with a fake encoder: no model download, no API."""
from __future__ import annotations

import hashlib

from app.agent.search import find_entry
from app.graph.search_index import build_search_index, clause_text, parameter_text
from tests.agent_graph import code_graph  # noqa: F401  (fixture)


class FakeEncoder:
    """Bag-of-words vectors over a tiny vocabulary: similar words, similar vectors."""
    name = "fake-encoder"
    dimension = 8

    def encode(self, texts):
        out = []
        for text in texts:
            v = [0.0] * self.dimension
            for word in text.lower().split():
                v[int(hashlib.md5(word.strip(".,:()").encode()).hexdigest(), 16) % self.dimension] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


def test_embedded_texts_are_built_in_code_from_the_graph():
    assert clause_text({"id": "1.2", "title": "Base Shear", "text": "x" * 500}).startswith("1.2 Base Shear. xxx")
    assert parameter_text({"id": "W", "symbol": "W", "description": "Weight", "units": "kips",
                           "aliases": ["Wt"]}) == "W: Weight (kips). Also written Wt"


def test_index_build_embeds_once_and_vector_search_finds_nodes(code_graph):
    enc = FakeEncoder()
    first = build_search_index(code_graph, enc, log=lambda m: None)
    assert first["embedded"]["Clause"] == 3 and first["embedded"]["Parameter"] == 10
    assert set(first["vector_created"]) == {"Clause", "Parameter", "Term"}
    again = build_search_index(code_graph, enc, log=lambda m: None)
    assert again["embedded"] == {"Clause": 0, "Parameter": 0, "Term": 0}  # unchanged text is not re-embedded
    hit = find_entry(code_graph, "base shear rules", enc.encode)
    assert any("vector" in c["matched_by"] for c in hit.data["candidates"])
    ft = code_graph.ro_query("CALL db.idx.fulltext.queryNodes('Parameter', 'weight') YIELD node RETURN node.id")
    assert ["W"] in ft.result_set
