"""``find_entry``: exact match on ids and symbols, then full-text search, then vector
search (local embeddings), merged by reciprocal rank fusion."""
from __future__ import annotations

import re
from typing import Callable

from app.agent.results import ToolResult, Touched

RRF_K = 60
EXACT_BONUS = 1.0  # an exact id or symbol match outranks any search hit
STOPWORDS = {"the", "and", "for", "with", "what", "which", "that", "this", "from", "are", "how", "does", "into",
             "of", "a", "an", "to", "in", "on", "is", "be", "by", "or", "at", "as", "it", "my", "i"}
FULLTEXT_LABELS = ("Clause", "Parameter", "Term", "Table")
VECTOR_LABELS = ("Clause", "Parameter", "Term")
SUMMARY_FIELDS = {"Clause": ("title", "page"), "Parameter": ("symbol", "description", "units"),
                  "Term": ("term", "definition"), "Table": ("caption", "page"), "Equation": ("latex", "page"),
                  "Figure": ("caption", "page")}


def ft_query(text: str) -> str:
    """Words joined with '|' (any word), RediSearch syntax characters removed."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", text) if w.lower() not in STOPWORDS and len(w) > 1]
    return "|".join(dict.fromkeys(words))


def _brief(label: str, props: dict) -> dict:
    out = {"label": label, "id": props.get("id")}
    for f in SUMMARY_FIELDS.get(label, ()):
        value = props.get(f)
        if value is not None:
            out[f] = value[:160] if isinstance(value, str) else value
    if props.get("status") == "flagged":
        out["status"] = "flagged"
    return out


def exact_matches(graph, text: str) -> list[tuple[str, dict]]:
    q = text.strip()
    found = []
    for label in ("Clause", "Equation", "Table", "Figure", "Parameter", "Term"):
        rows = graph.ro_query(f"MATCH (n:{label}) WHERE n.id = $q OR n.number = $q RETURN n LIMIT 3",
                              {"q": q}).result_set
        found += [(label, r[0].properties) for r in rows]
    rows = graph.ro_query("MATCH (p:Parameter) WHERE p.symbol = $q OR $q IN coalesce(p.aliases, []) "
                          "OR toLower(p.id) = toLower($q) RETURN p LIMIT 5", {"q": q}).result_set
    found += [("Parameter", r[0].properties) for r in rows]
    rows = graph.ro_query("MATCH (t:Term) WHERE toLower(t.term) = toLower($q) RETURN t LIMIT 3", {"q": q}).result_set
    found += [("Term", r[0].properties) for r in rows]
    rows = graph.ro_query("MATCH (c:Clause) WHERE toLower(c.title) = toLower($q) RETURN c ORDER BY c.page LIMIT 3",
                          {"q": q}).result_set
    found += [("Clause", r[0].properties) for r in rows]
    # "Section 3.3.1.3", "Equation (3-10)", "Table 6-7": the number inside the text
    for number in re.findall(r"\b[A-Z]?\d+(?:[.-]\d+)+\b", q):
        for label in ("Clause", "Equation", "Table", "Figure"):
            rows = graph.ro_query(f"MATCH (n:{label}) WHERE n.id = $n OR n.number = $n OR n.id ENDS WITH (' ' + $n) "
                                  f"RETURN n LIMIT 2", {"n": number}).result_set
            found += [(label, r[0].properties) for r in rows]
    return found


def fulltext(graph, text: str, k: int) -> dict[str, list[tuple[str, dict]]]:
    query = ft_query(text)
    out = {}
    if not query:
        return out
    for label in FULLTEXT_LABELS:
        try:
            rows = graph.ro_query(f"CALL db.idx.fulltext.queryNodes('{label}', $q) YIELD node, score "
                                  f"RETURN node ORDER BY score DESC LIMIT {k}", {"q": query}).result_set
        except Exception:  # no full-text index on this label in this graph
            continue
        out[label] = [(label, r[0].properties) for r in rows]
    return out


def vector(graph, vec: list[float], k: int) -> dict[str, list[tuple[str, dict]]]:
    out = {}
    for label in VECTOR_LABELS:
        try:
            rows = graph.ro_query(f"CALL db.idx.vector.queryNodes('{label}', 'embedding', {k}, vecf32($v)) "
                                  f"YIELD node, score RETURN node ORDER BY score ASC", {"v": vec}).result_set
        except Exception:  # no vector index on this label in this graph
            continue
        out[label] = [(label, r[0].properties) for r in rows]
    return out


def find_entry(graph, query: str, encode: Callable[[list[str]], list[list[float]]] | None = None,
               k: int = 8, labels: list[str] | None = None) -> ToolResult:
    scores: dict[tuple[str, str], float] = {}
    props: dict[tuple[str, str], dict] = {}
    how: dict[tuple[str, str], list[str]] = {}

    def add(ranked: list[tuple[str, dict]], method: str, bonus: float = 0.0) -> None:
        for rank, (label, p) in enumerate(ranked):
            key = (label, p.get("id"))
            scores[key] = scores.get(key, 0.0) + bonus + 1.0 / (RRF_K + rank + 1)
            props[key] = p
            how.setdefault(key, []).append(method)

    add(exact_matches(graph, query), "exact", EXACT_BONUS)
    for ranked in fulltext(graph, query, k).values():
        add(ranked, "fulltext")
    if encode is not None:
        for ranked in vector(graph, encode([query])[0], k).values():
            add(ranked, "vector")
    keys = [key for key in sorted(scores, key=lambda x: -scores[x]) if not labels or key[0] in labels][:k]
    t = Touched()
    out = []
    for key in keys:
        t.node(*key)
        out.append({**_brief(key[0], props[key]), "matched_by": sorted(set(how[key]))})
    summary = f"{len(out)} candidates: " + ", ".join(f"{c['label']} {c['id']}" for c in out[:6])
    return ToolResult(summary, {"query": query, "candidates": out}, t)
