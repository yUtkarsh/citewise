"""Embedding similarity for candidate finding: the local vectors ``search_index`` stored
on Clause and Parameter nodes, and the same local model for texts that have none
(tables). Vectors are normalized, so a dot product is the cosine similarity."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Vectors:
    ids: list[str]
    matrix: np.ndarray  # one normalized row per id

    def top(self, vector, k: int, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        if not self.ids:
            return []
        scores = self.matrix @ _norm(np.asarray(vector, dtype=np.float32))
        order = np.argsort(-scores)
        out = []
        for i in order:
            if allowed is None or self.ids[i] in allowed:
                out.append((self.ids[i], float(scores[i])))
                if len(out) == k:
                    break
        return out

    def vector(self, node_id: str):
        try:
            return self.matrix[self.ids.index(node_id)]
        except ValueError:
            return None


def _norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n == 0, 1, n)


def load_vectors(graph, label: str, where: str = "") -> Vectors:
    rows = graph.ro_query(f"MATCH (n:{label}) WHERE n.embedding IS NOT NULL {where} "
                          "RETURN n.id, n.embedding").result_set
    if not rows:
        return Vectors([], np.zeros((0, 1), dtype=np.float32))
    return Vectors([r[0] for r in rows], _norm(np.asarray([list(r[1]) for r in rows], dtype=np.float32)))


def encode(encoder, texts: list[str]) -> list[np.ndarray]:
    if not texts:
        return []
    return [_norm(np.asarray(v, dtype=np.float32)) for v in encoder.encode(texts)]
