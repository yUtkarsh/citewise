"""Step 3a.4: equations from right-aligned numbers, and inline where-blocks.

An equation number is a span matching the profile's ``equation_number`` that sits
at the right edge of its column, alone or after a clear gap. The equation box is
the number line plus the non-prose lines of that column just above and below it,
which covers stacked fractions drawn as separate spans. Lines between two
equation-number lines are split at the widest vertical gap between them, so a box
never runs into the numerator (or exponent) of the next equation.
"""
from __future__ import annotations

from collections import Counter
import re

from app.ingest.pdf_lines import Line, Rect, style_key, union
from app.ingest.pass1.model import Equation, WhereItem
from app.profile.schema import Profile

RIGHT_EDGE_TOLERANCE = 24.0  # points from the column's right edge
REACH_ABOVE = 72.0  # max distance above the number baseline that still belongs to the equation
REACH_BELOW = 40.0
PROSE_WIDTH = 0.6  # prose lines fill at least this share of the column
PROSE_WORD = re.compile(r"(?<![\w(])[a-z]{3,}[,.:;]?(?=\s|$)")
PROSE_WORDS = 3
UNIT = re.compile(r"\(([A-Za-z][^()]{0,11})\)\.?\s*$")  # "(kips)", "(in.)"; not "(3-10)"


def column_bounds(lines: list[Line], col: int) -> tuple[float, float]:
    xs = [ln for ln in lines if ln.col == col] or lines
    return min(ln.bbox[0] for ln in xs), max(ln.bbox[2] for ln in xs)


def is_prose(line: Line, body: tuple[str, float, bool], col_width: float) -> bool:
    """A line mostly in the body style that is wide, reads as words, or introduces
    something with a colon (the short last line of a paragraph)."""
    if dominant_style(line) != body or "=" in line.text:
        return False
    text = line.text.strip()
    wide = (line.bbox[2] - line.bbox[0]) >= PROSE_WIDTH * col_width
    return wide or text.endswith(":") or len(PROSE_WORD.findall(text)) >= PROSE_WORDS


def dominant_style(line: Line) -> tuple[str, float, bool]:
    counts = Counter()
    for span in line.spans:
        counts[style_key(span)] += len(span["text"].strip())
    return counts.most_common(1)[0][0]


def number_span(line: Line, profile: Profile, col_right: float) -> tuple[str, dict] | None:
    """(number, span) if this line ends in a stand-alone equation number at the column edge."""
    spans = [s for s in line.spans if s["text"].strip()]
    if not spans:
        return None
    last = spans[-1]
    match = re.fullmatch(profile.equation_number, last["text"].strip())
    if not match or last["bbox"][2] < col_right - RIGHT_EDGE_TOLERANCE:
        return None
    if len(spans) > 1:
        gap = last["bbox"][0] - spans[-2]["bbox"][2]
        if gap < last["size"]:  # a reference like "Equation (3-11)" inside a prose line
            return None
    return match.group("num"), last


def find_equations(
    lines: list[Line], profile: Profile, body: tuple[str, float, bool]
) -> list[tuple[Equation, list[Line]]]:
    """Equations on one page, each with the lines its box consumes."""
    found = []
    taken: set[int] = set()
    for line in lines:
        left, right = column_bounds(lines, line.col)
        hit = number_span(line, profile, right)
        if not hit or id(line) in taken:
            continue
        num, span = hit
        col_lines = sorted((ln for ln in lines if ln.col == line.col), key=lambda ln: ln.bbox[1])
        number_lines = [ln for ln in col_lines if number_span(ln, profile, right)]
        members = [line] + _neighbours(col_lines, line, body, right - left, profile, right, number_lines)
        members = [m for m in members if id(m) not in taken]
        taken.update(id(m) for m in members)
        box = members[0].bbox
        for m in members[1:]:
            box = union(box, m.bbox)
        spans = [s for m in members for s in m.spans]
        raw = " ".join(m.text.strip() for m in sorted(members, key=lambda m: (m.bbox[1], m.bbox[0])))
        eq = Equation(id=f"Equation {num}", number=num, page=line.page, bbox=box, raw_text=raw,
                      raw_spans=[{"text": s["text"], "font": s["font"], "size": round(s["size"], 2),
                                  "bbox": [round(v, 1) for v in s["bbox"]]} for s in spans])
        found.append((eq, members))
    return found


def split_point(upper: Line, lower: Line, col_lines: list[Line]) -> float:
    """y where the lines between two equation-number lines divide: the top of the line
    after the widest vertical gap in the run upper, ..., lower."""
    run = [ln for ln in col_lines if ln.bbox[1] >= upper.bbox[3] - 1 and ln.bbox[3] <= lower.bbox[1] + 1
           and ln is not upper and ln is not lower]  # lines strictly between the two number rows
    seq = [upper, *sorted(run, key=lambda ln: ln.bbox[1]), lower]
    gaps = [(seq[k + 1].bbox[1] - seq[k].bbox[3], k) for k in range(len(seq) - 1)]
    _, k = max(gaps)
    return seq[k + 1].bbox[1]


def own_band(line: Line, col_lines: list[Line], number_lines: list[Line]) -> tuple[float, float]:
    """(top, bottom) limits of the lines that can belong to this number line's equation."""
    above = [n for n in number_lines if n.bbox[1] < line.bbox[1] and n is not line]
    below = [n for n in number_lines if n.bbox[1] > line.bbox[1] and n is not line]
    top = split_point(max(above, key=lambda n: n.bbox[1]), line, col_lines) if above else float("-inf")
    bottom = split_point(line, min(below, key=lambda n: n.bbox[1]), col_lines) if below else float("inf")
    return top, bottom


def _neighbours(col_lines, line, body, col_width, profile, col_right, number_lines=()) -> list[Line]:
    """Non-prose lines just above and below the number line, stopping at prose,
    another equation number, the split with a neighbouring equation, or the reach limits."""
    idx = col_lines.index(line)
    top, bottom = own_band(line, col_lines, list(number_lines))
    out = []
    for step, reach in ((-1, REACH_ABOVE), (1, REACH_BELOW)):
        i = idx + step
        while 0 <= i < len(col_lines):
            other = col_lines[i]
            distance = (line.baseline - other.bbox[3]) if step < 0 else (other.bbox[1] - line.baseline)
            if distance > reach or is_prose(other, body, col_width) or number_span(other, profile, col_right):
                break
            beside = other.bbox[1] < line.bbox[3] and other.bbox[3] > line.bbox[1]  # on the number's own row
            if not beside and (other.bbox[1] < top - 0.5 or other.bbox[1] >= bottom - 0.5):
                break  # past the split with the previous or next equation number
            if re.match(profile.where_block.intro_regex, other.text.strip()):
                break
            out.append(other)
            i += step
    return out


def parse_where(lines: list[Line], start: int, profile: Profile) -> tuple[list[WhereItem], int]:
    """Items of the where-block whose intro line is ``lines[start]``; returns the
    items and the index after the block. Items may continue on indented lines."""
    items: list[WhereItem] = []
    intro = lines[start]
    rest = intro.text.strip()
    rest = re.sub(profile.where_block.intro_regex, "", rest, count=1).strip()
    pending = [(rest, intro)] if rest else []
    i = start + 1
    item_x = None
    while i < len(lines):
        line = lines[i]
        text = line.text.strip()
        if re.match(profile.where_block.item_regex, text):
            pending.append((text, line))
            item_x = line.bbox[0] if item_x is None else item_x
        elif pending and item_x is not None and line.bbox[0] > item_x + 2:
            prev_text, prev_line = pending[-1]
            pending[-1] = (f"{prev_text} {text}", prev_line)
        else:
            break
        i += 1
    for text, line in pending:
        match = re.match(profile.where_block.item_regex, text)
        if not match:
            continue
        definition = match.group("definition").strip()
        unit_match = UNIT.search(definition)
        items.append(WhereItem(symbol=match.group("symbol").strip(), definition=definition,
                               unit=unit_match.group(1).strip() if unit_match else None,
                               page=line.page, bbox=line.bbox))
    return items, i


def pad(bbox: Rect, margin: float = 3.0) -> Rect:
    return bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin
