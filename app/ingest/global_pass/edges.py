"""Inferred edges: connections the code implies but never states (SPEC Step 3c.2-3).

Each one carries ``method: inferred``, a confidence, a one-line reason and the model that
proposed it; the agent labels them "inferred" (``app.agent.results.edge_label``). They
are MERGEd with ``method`` in the pattern, so an inferred edge never overwrites a stated
edge between the same nodes (a PDF link, say). A re-run first removes the inferred edges
of its kind that no reviewer approved, so changed answers leave nothing stale.
"""
from __future__ import annotations

from collections import defaultdict
import time

INFERRED = "inferred"
SOURCE = "global_pass"


def inferred_edge(src: tuple[str, str], kind: str, dst: tuple[str, str], confidence: float, reason: str,
                  model: str, **props) -> dict:
    return {"src_label": src[0], "src": src[1], "kind": kind, "dst_label": dst[0], "dst": dst[1],
            "props": {"method": INFERRED, "source": SOURCE, "confidence": round(float(confidence), 3),
                      "reason": reason, "model": model, "review_status": "unreviewed", "time": time.time(),
                      **props}}


def write_edges(graph, edges: list[dict]) -> int:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for e in edges:
        groups[(e["src_label"], e["kind"], e["dst_label"])].append({"src": e["src"], "dst": e["dst"],
                                                                   "props": e["props"]})
    for (src, kind, dst), rows in groups.items():
        graph.query(f"UNWIND $rows AS r MATCH (a:{src} {{id: r.src}}) MATCH (b:{dst} {{id: r.dst}}) "
                    f"MERGE (a)-[e:{kind} {{method: '{INFERRED}'}}]->(b) SET e += r.props", {"rows": rows})
    return len(edges)


def clear_inferred(graph, kind: str, src_label: str, dst_label: str) -> int:
    """Remove this kind's inferred global-pass edges that no reviewer approved."""
    result = graph.query(f"MATCH (:{src_label})-[e:{kind}]->(:{dst_label}) WHERE e.method = '{INFERRED}' "
                         f"AND e.source = '{SOURCE}' AND coalesce(e.review_status, '') <> 'approved' DELETE e")
    return result.relationships_deleted
