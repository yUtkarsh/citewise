"""Search indexes for ``find_entry`` (SPEC Step 3c.5): full-text indexes on clauses,
parameters, terms and tables, and local sentence-transformers embeddings in FalkorDB
vector indexes. Embedding runs offline: the model is loaded from the local cache with
the Hugging Face hub switched off, and no API is called.

    python -m app.graph.search_index --code fema356 [--force]

Each embedded node stores ``embedding`` (vecf32), ``embed_model`` and ``embed_hash`` (a
hash of the embedded text and model), so a re-run only embeds nodes whose text changed.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from typing import Callable, Protocol

from app.config import get_settings

BATCH = 256
SUMMARY_CHARS = 300  # clause text embedded after the title (a summary built in code, not by Claude)

FULLTEXT_INDEXES = {  # label -> string properties
    "Clause": ("text", "title"),
    "Parameter": ("symbol", "description", "alias_text"),
    "Term": ("term", "definition"),
    "Table": ("caption",),
}
VECTOR_LABELS = ("Clause", "Parameter", "Term")


class Encoder(Protocol):
    name: str
    dimension: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class LocalEncoder:
    """A sentence-transformers model loaded from the local cache only."""

    def __init__(self, model_name: str):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer  # slow import; only when embedding

        self.name = model_name
        self.model = SentenceTransformer(model_name, device="cpu")
        dim = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dimension = dim()

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]


_encoder: Encoder | None = None


def default_encoder() -> Encoder:
    global _encoder
    if _encoder is None:
        _encoder = LocalEncoder(get_settings().embed_model)
    return _encoder


# ---- texts to embed --------------------------------------------------------------------

def clause_text(row: dict) -> str:
    return f"{row['id']} {row.get('title') or ''}. {(row.get('text') or '')[:SUMMARY_CHARS]}".strip()


def parameter_text(row: dict) -> str:
    aliases = ", ".join(a for a in (row.get("aliases") or []) if a)
    units = f" ({row['units']})" if row.get("units") else ""
    return f"{row.get('symbol') or row['id']}: {row.get('description') or ''}{units}" + \
        (f". Also written {aliases}" if aliases else "")


def term_text(row: dict) -> str:
    return f"{row.get('term') or row['id']}: {row.get('definition') or ''}"


TEXTS: dict[str, tuple[str, Callable[[dict], str]]] = {
    "Clause": ("n.id AS id, n.title AS title, n.text AS text", clause_text),
    "Parameter": ("n.id AS id, n.symbol AS symbol, n.description AS description, n.units AS units, "
                  "n.aliases AS aliases", parameter_text),
    "Term": ("n.id AS id, n.term AS term, n.definition AS definition", term_text),
}


def text_hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}\x1f{text}".encode("utf-8")).hexdigest()[:16]


# ---- index creation ------------------------------------------------------------------------

def _existing_indexes(graph) -> dict[str, dict[str, list[str]]]:
    """label -> {property: [index types]}."""
    out: dict[str, dict[str, list[str]]] = {}
    for row in graph.ro_query("CALL db.indexes()").result_set:
        label, types = row[0], row[2]
        out[label] = {prop: list(kinds) for prop, kinds in dict(types).items()}
    return out


def create_fulltext_indexes(graph) -> list[str]:
    """Full-text indexes on every FULLTEXT_INDEXES field not yet indexed. Parameters get an
    ``alias_text`` string first (a full-text index cannot index a list)."""
    graph.query("MATCH (p:Parameter) SET p.alias_text = reduce(s = '', a IN coalesce(p.aliases, []) | s + ' ' + a)")
    existing = _existing_indexes(graph)
    created = []
    for label, props in FULLTEXT_INDEXES.items():
        have = existing.get(label, {})
        missing = [p for p in props if "FULLTEXT" not in have.get(p, [])]
        if missing:
            args = ", ".join(f"'{x}'" for x in (label, *missing))
            graph.query(f"CALL db.idx.fulltext.createNodeIndex({args})")
            created.append(f"{label}({', '.join(missing)})")
    return created


def create_vector_indexes(graph, dimension: int) -> list[str]:
    existing = _existing_indexes(graph)
    created = []
    for label in VECTOR_LABELS:
        if "VECTOR" not in existing.get(label, {}).get("embedding", []):
            graph.query(f"CREATE VECTOR INDEX FOR (n:{label}) ON (n.embedding) "
                        f"OPTIONS {{dimension: {dimension}, similarityFunction: 'cosine'}}")
            created.append(label)
    return created


# ---- embedding ---------------------------------------------------------------------------

def embed_label(graph, label: str, encoder: Encoder, force: bool = False, log=print) -> int:
    fields, to_text = TEXTS[label]
    rows = graph.ro_query(f"MATCH (n:{label}) RETURN {fields}, n.embed_hash AS h").result_set
    names = [f.split(" AS ")[1].strip() for f in fields.split(", ")] + ["h"]
    todo = []
    for values in rows:
        row = dict(zip(names, values))
        text = to_text(row)
        h = text_hash(text, encoder.name)
        if force or row["h"] != h:
            todo.append((row["id"], text, h))
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        vectors = encoder.encode([t for _, t, _ in batch])
        graph.query(f"UNWIND $rows AS r MATCH (n:{label} {{id: r.id}}) "
                    "SET n.embedding = vecf32(r.v), n.embed_hash = r.h, n.embed_model = $model",
                    {"rows": [{"id": i, "v": v, "h": h} for (i, _, h), v in zip(batch, vectors)],
                     "model": encoder.name})
    log(f"  {label}: {len(todo)} embedded, {len(rows) - len(todo)} unchanged")
    return len(todo)


def build_search_index(graph, encoder: Encoder | None = None, force: bool = False, log=print) -> dict:
    start = time.time()
    created_ft = create_fulltext_indexes(graph)
    load_start = time.time()
    encoder = encoder or default_encoder()
    load_s = time.time() - load_start
    embed_start = time.time()
    counts = {label: embed_label(graph, label, encoder, force, log) for label in VECTOR_LABELS}
    embed_s = time.time() - embed_start
    created_vec = create_vector_indexes(graph, encoder.dimension)
    return {"model": encoder.name, "dimension": encoder.dimension, "fulltext_created": created_ft,
            "vector_created": created_vec, "embedded": counts, "model_load_s": round(load_s, 1),
            "embed_s": round(embed_s, 1), "total_s": round(time.time() - start, 1)}


def main(argv: list[str]) -> int:
    from app.graph.connection import get_db

    parser = argparse.ArgumentParser(description="Build the find_entry search indexes for one code graph")
    parser.add_argument("--code", required=True, help="code name, e.g. fema356 (graph code_fema356)")
    parser.add_argument("--force", action="store_true", help="re-embed every node")
    args = parser.parse_args(argv)
    graph = get_db().select_graph(f"code_{args.code}")
    report = build_search_index(graph, force=args.force)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
