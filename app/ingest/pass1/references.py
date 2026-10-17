"""Step 3a.7: references. PDF link annotations first, then regex built from the
profile's reference words and number formats, expanding lists and ranges."""
from __future__ import annotations

from dataclasses import dataclass
import re

import pymupdf

from app.ingest.pdf_lines import Line
from app.ingest.pass1.model import Block, Reference
from app.profile.schema import Profile, ReferenceKind

LINK_REACH = 40.0  # a link target this close above a heading points at that heading


@dataclass
class Targets:
    """Known node ids per kind, in document order (ranges expand over this order)."""

    clause: list[str]
    table: list[str]
    figure: list[str]
    equation: list[str]
    clause_depth: dict[str, int]

    def ordered(self, kind: str) -> list[str]:
        return getattr(self, kind)


def node_id(kind: str, number: str) -> str:
    number = number.strip().strip("()[]").strip()
    return {"table": f"Table {number}", "figure": f"Figure {number}", "equation": f"Equation {number}"}.get(kind, number)


def _connector_pattern(words: list[str]) -> str:
    parts = []
    for w in sorted(words, key=len, reverse=True):
        parts.append(re.escape(w) if not w[:1].isalnum() else rf"\b{re.escape(w)}\b")
    return "|".join(parts) if parts else r"(?!)"


def reference_pattern(kind: ReferenceKind, profile: Profile) -> re.Pattern:
    words = "|".join(rf"\b{re.escape(w)}\b" for w in sorted(kind.words, key=len, reverse=True))
    conn = _connector_pattern(profile.list_connectors + profile.range_connectors)
    num = f"(?:{kind.number_regex})"
    return re.compile(rf"(?:{words})\s+(?P<nums>{num}(?:\s*(?:{conn})\s*{num})*)")


def expand(kind: ReferenceKind, nums_text: str, profile: Profile, targets: Targets) -> list[tuple[str, str]]:
    """(id, cited text) for each number in a cited list; ranges expand to the known
    ids between their ends (same depth for clauses)."""
    num_re = re.compile(kind.number_regex)
    matches = list(num_re.finditer(nums_text))
    ranges = {c.strip().lower() for c in profile.range_connectors}
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        out.append((node_id(kind.kind, m.group(0)), m.group(0)))
        if i + 1 < len(matches):
            between = nums_text[m.end():matches[i + 1].start()].strip().lower()
            if between in ranges:
                out.extend(_span(kind.kind, out[-1][0], node_id(kind.kind, matches[i + 1].group(0)), targets))
    return list(dict.fromkeys(out))


def _span(kind: str, start: str, end: str, targets: Targets) -> list[tuple[str, str]]:
    ordered = targets.ordered(kind)
    if start not in ordered or end not in ordered:
        return []
    a, b = ordered.index(start), ordered.index(end)
    inner = ordered[a + 1:b]
    if kind == "clause":
        inner = [i for i in inner if targets.clause_depth.get(i) == targets.clause_depth.get(start)]
    return [(i, f"{start}..{end}") for i in inner]


def regex_references(source_id: str, text: str, page: int, profile: Profile, targets: Targets) -> list[Reference]:
    known = {k: set(targets.ordered(k)) for k in ("clause", "table", "figure", "equation")}
    refs = []
    for kind in profile.references:
        for match in reference_pattern(kind, profile).finditer(text):
            for target, cited in expand(kind, match.group("nums"), profile, targets):
                if target == source_id:
                    continue
                resolved = target if target in known[kind.kind] else None
                refs.append(Reference(source_id, match.group(0) if cited in match.group(0) else cited,
                                      kind.kind, resolved, "regex", page))
                if resolved is None:
                    refs[-1].text = f"{match.group(0)} -> {target}"
    return refs


def external_codes(source_id: str, text: str, page: int, profile: Profile) -> list[Reference]:
    own = re.sub(r"\s+", "", profile.self_designation).lower()
    refs = []
    for match in re.finditer(profile.external_code_regex, text):
        code = re.sub(r"\s+", " ", match.group("code")).strip()
        if re.sub(r"\s+", "", code).lower() == own:
            continue
        refs.append(Reference(source_id, match.group(0), "external", code, "regex", page))
    return refs


def _owner_at(blocks: list[Block], owner: dict[int, str | None], x: float, y: float) -> str | None:
    """Clause owning the block at (x, y): the block containing the point, else the
    nearest block above it in the same column."""
    inside = [b for b in blocks if b.bbox[0] - 2 <= x <= b.bbox[2] + 2 and b.bbox[1] - 2 <= y <= b.bbox[3] + 2]
    if inside:
        return owner.get(id(inside[0]))
    above = [b for b in blocks if b.bbox[3] <= y + 2 and b.bbox[0] - 2 <= x <= b.bbox[2] + 2]
    return owner.get(id(max(above, key=lambda b: b.bbox[3]))) if above else None


def _target_at(blocks: list[Block], owner: dict[int, str | None], x: float, y: float) -> tuple[str, str | None]:
    """(kind, id) of what a link points at: a heading or caption just below the point,
    a table or equation containing it, or the clause there."""
    for b in sorted(blocks, key=lambda b: b.bbox[1]):
        if b.bbox[1] >= y - 2 and b.bbox[1] - y <= LINK_REACH and b.kind in ("heading", "caption"):
            if b.kind == "heading":
                return "clause", owner.get(id(b))
            return b.style, node_id(b.style, b.number)
    for b in blocks:
        if b.kind in ("table", "equation") and b.bbox[0] <= x <= b.bbox[2] and b.bbox[1] <= y <= b.bbox[3]:
            obj = b.obj
            return b.kind, getattr(obj, "table_id", None) or getattr(obj, "id", None)
    return "clause", _owner_at(blocks, owner, x, y)


def _text_under(lines: list[Line], rect) -> str:
    """Text of the spans a link rectangle covers (from lines already extracted)."""
    out = []
    for line in lines:
        for span in line.spans:
            b = span["bbox"]
            overlap = min(b[2], rect.x1) - max(b[0], rect.x0)
            if overlap > 0.5 * (b[2] - b[0]) and min(b[3], rect.y1) - max(b[1], rect.y0) > 0:
                out.append(span["text"].strip())
    return " ".join(t for t in out if t)


def link_references(doc: pymupdf.Document, pages: list[int], blocks: dict[int, list[Block]],
                    owner: dict[int, str | None], page_lines: dict[int, list[Line]]) -> list[Reference]:
    refs = []
    for page in pages:
        pg = doc[page - 1]
        for link in pg.get_links():
            if link.get("kind") != pymupdf.LINK_GOTO or link.get("page", -1) < 0:
                continue
            target_page = link["page"] + 1
            if target_page not in blocks:
                continue
            rect = link["from"]
            source = _owner_at(blocks[page], owner, (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
            to = link.get("to") or pymupdf.Point(0, 0)
            kind, target = _target_at(blocks[target_page], owner, to.x, to.y)
            if source and target and source != target:
                text = _text_under(page_lines.get(page, []), rect) or f"link to p{target_page}"
                refs.append(Reference(source, text, kind, target, "link", page))
    return refs


def dedupe(refs: list[Reference]) -> list[Reference]:
    """One edge per (source, kind, target); links win over regex hits."""
    seen: dict[tuple, Reference] = {}
    for ref in sorted(refs, key=lambda r: r.method != "link"):
        key = (ref.source_id, ref.kind, ref.target_id or ref.text)
        seen.setdefault(key, ref)
    return list(seen.values())
