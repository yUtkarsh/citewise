"""Graph schema for code graphs: one FalkorDB graph per document.

Every node carries the provenance fields: page, bbox, source_quote, confidence,
extraction_method, review_status, schema_version. Pass 1 nodes have status "skeleton".
"""
from __future__ import annotations

from pathlib import Path
import re

GRAPH_SCHEMA_VERSION = "graph-v1"
QUOTE_CHARS = 300

NODE_LABELS = ("Document", "Clause", "Table", "Row", "Footnote", "Equation", "WhereItem",
               "Figure", "ExternalCode", "FlaggedLine",
               # Pass 2
               "Parameter", "Term", "Condition", "Requirement", "Exception", "SymbolCorrection")

INDEXES = [
    "CREATE INDEX FOR (n:{label}) ON (n.id)".format(label=label) for label in NODE_LABELS
] + [
    "CREATE INDEX FOR (p:Parameter) ON (p.symbol)",
    "CREATE INDEX FOR (c:Clause) ON (c.page)",
]
FULLTEXT = "CALL db.idx.fulltext.createNodeIndex('Clause', 'text', 'title')"


def graph_name(pdf: Path) -> str:
    """'code_' + a slug of the file name, e.g. data/fema356.pdf -> code_fema356."""
    return "code_" + re.sub(r"[^a-z0-9]+", "_", pdf.stem.lower()).strip("_")


def provenance(page: int | None, bbox, quote: str, method: str, confidence: float = 1.0) -> dict:
    return {
        "page": page,
        "bbox": [round(float(v), 1) for v in bbox] if bbox else None,
        "source_quote": (quote or "")[:QUOTE_CHARS],
        "confidence": confidence,
        "extraction_method": method,
        "review_status": "unreviewed",
        "schema_version": GRAPH_SCHEMA_VERSION,
        "status": "skeleton",
    }
