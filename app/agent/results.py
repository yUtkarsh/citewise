"""What every agent tool returns: a one-line summary, the data, the graph nodes and
edges it touched (Phase 5 highlights them), and the answer labels its items carry.

Node refs are ``{"label", "id"}``; edge ids are ``"<src id>|<TYPE>|<dst id>"``, stable
across runs (FalkorDB's internal ids are not).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

UNREVIEWED, INFERRED, ASSUMED = "unreviewed", "inferred", "assumed"
DROP_KEYS = {"embedding", "embed_hash", "embed_model", "raw_spans", "regions", "suspect_spans", "alias_text",
             "review_status", "schema_version", "extraction_schema"}
TEXT_CHARS = 1500  # long text properties are cut in tool results


@dataclass
class Touched:
    nodes: dict[tuple[str, str], None] = field(default_factory=dict)  # ordered set of (label, id)
    edges: dict[str, None] = field(default_factory=dict)

    def node(self, label: str, node_id: str) -> None:
        if node_id is not None:
            self.nodes[(label, str(node_id))] = None

    def edge(self, src: str, kind: str, dst: str) -> None:
        self.edges[f"{src}|{kind}|{dst}"] = None

    def as_dict(self) -> dict:
        return {"nodes": [{"label": label, "id": i} for label, i in self.nodes],
                "edges": list(self.edges)}


@dataclass
class Label:
    target: str  # node id, edge id, or fact id
    label: str  # unreviewed / inferred / assumed
    reason: str

    def as_dict(self) -> dict:
        return {"target": self.target, "label": self.label, "reason": self.reason}


@dataclass
class ToolResult:
    summary: str
    data: dict | list
    touched: Touched = field(default_factory=Touched)
    labels: list[Label] = field(default_factory=list)
    error: bool = False
    reruns: list = field(default_factory=list)  # what-if only: the (tool, args, result) calls it re-ran

    def for_model(self) -> str:
        """The tool_result text Claude sees."""
        payload = {"summary": self.summary, "data": self.data}
        if self.labels:
            payload["labels"] = [x.as_dict() for x in self.labels]
        return json.dumps(payload, ensure_ascii=False, default=str)


def node_label(props: dict, label: str) -> Label | None:
    """'unreviewed' for a flagged node that no reviewer approved (SPEC Step 4)."""
    if props.get("status") == "flagged" and props.get("review_status") != "approved":
        return Label(f"{props.get('id')}", UNREVIEWED, f"{label} flagged: " + flag_reasons(props.get("flags")))
    return None


def edge_label(src: str, kind: str, dst: str, props: dict) -> Label | None:
    method = str(props.get("method") or props.get("extraction_method") or "")
    if method == "inferred" or props.get("inferred"):
        return Label(f"{src}|{kind}|{dst}", INFERRED, "edge inferred by the global pass, not stated by the code")
    return None


def flag_reasons(flags) -> str:
    reasons = []
    for f in flags or []:
        try:
            item = json.loads(f) if isinstance(f, str) else f
            reasons.append(f"{item.get('reason')} ({item.get('severity')})")
        except (ValueError, AttributeError):
            reasons.append(str(f)[:60])
    return ", ".join(dict.fromkeys(reasons)) or "flagged"


def clean(props: dict) -> dict:
    """Node properties fit for a tool result: no vectors or layout internals, long text cut.
    No review status: nothing is reviewed yet, so it would read as a label on every node;
    a node is either ``flagged: true`` (its labels say why) or carries no status."""
    out = {}
    for key, value in props.items():
        if key == "status":
            if value == "flagged":
                out["flagged"] = True
            continue
        if key in DROP_KEYS or value is None:
            continue
        if isinstance(value, str) and len(value) > TEXT_CHARS:
            value = value[:TEXT_CHARS] + " …"
        out[key] = value
    return out
