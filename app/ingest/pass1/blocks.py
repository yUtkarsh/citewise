"""Step 3a.1: one ordered list of blocks per page, every pattern from the profile."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import re

import pymupdf

from app.ingest import text_checks as tc
from app.ingest.pdf_lines import (Line, Rect, base_font_name, body_style, is_bold, outside_text_area, read_lines,
                                  style_key, union)
from app.ingest.pass1 import equations as eqs
from app.ingest.pass1.layout import assign_columns, is_running, reading_order, repeated_band_lines
from app.ingest.pass1.model import Block, TableSegment
from app.profile.schema import Profile, Stream

CONTINUATION_GAP = 1.6  # line pitch, in font sizes, that still continues a heading or caption
FOOTNOTE_SIZE_MARGIN = 0.5  # footnotes are set smaller than the document body text
PROSE_SHARE = 0.05  # a regular-weight style with this share of all characters is a prose style


@dataclass
class DocContext:
    """Document-wide facts the per-page classification needs."""

    profile: Profile
    body: tuple[str, float, bool]
    repeated: set[str]
    page_lines: dict[int, list[Line]] = field(default_factory=dict)  # running elements removed
    sizes: dict[int, tuple[float, float]] = field(default_factory=dict)  # page -> (width, height)
    prose_styles: set[tuple[str, float, bool]] = field(default_factory=set)  # regular-weight text styles


def load_context(doc: pymupdf.Document, profile: Profile, pages: list[int]) -> DocContext:
    raw = {p: read_lines(doc[p - 1]) for p in pages}
    sizes = {p: (doc[p - 1].rect.width, doc[p - 1].rect.height) for p in pages}
    repeated = repeated_band_lines(raw, {p: h for p, (_, h) in sizes.items()})
    cleaned = {
        p: [ln for ln in lines
            if not is_running(ln, sizes[p][1], profile, repeated) and not outside_text_area(ln, lines)]
        for p, lines in raw.items()
    }
    for p, lines in cleaned.items():
        assign_columns(lines, sizes[p][0])
    all_lines = [ln for lines in cleaned.values() for ln in lines]
    body = profile.body_style or body_style(all_lines)
    return DocContext(profile=profile, body=body, repeated=repeated, page_lines=cleaned, sizes=sizes,
                      prose_styles=prose_styles(all_lines) | {body})


def prose_styles(lines: list[Line]) -> set[tuple[str, float, bool]]:
    """Regular-weight styles that carry a real share of the text (a document may set
    its own provisions upright and quoted material in italic, for example)."""
    counts: Counter = Counter()
    for line in lines:
        for span in line.spans:
            counts[style_key(span)] += len(span["text"].strip())
    total = sum(counts.values()) or 1
    return {style for style, n in counts.items() if not style[2] and n / total >= PROSE_SHARE}


def match_heading(line: Line, profile: Profile) -> tuple[Stream, str, str] | None:
    """(stream, number, title) when the line starts with a stream number in that level's style."""
    text = line.text.strip()
    first = line.first_span
    for stream in profile.streams:
        match = re.match(stream.number_regex, text)
        if not match:
            continue
        num = match.group("num")
        depth = len(num.split(stream.separator))
        level = next((lv for lv in stream.levels if lv.depth == depth), None)
        if level and level.style.matches(first["font"], first["size"], is_bold(first)):
            return stream, num, text[match.end():].strip()
    return None


def split_run_in(line: Line, prose: set[tuple[str, float, bool]]) -> tuple[Line, Line | None]:
    """A run-in heading ("7.2.1 Title. Body text ...") ends where the heading's typeface
    ends and a prose style begins; returns (heading part, body part or None)."""
    spans = line.spans
    first = line.first_span
    start = spans.index(first)

    k = start
    while k < len(spans) and (same_face(spans[k], first) or not spans[k]["text"].strip()):
        k += 1
    rest = [s for s in spans[k:]]
    if not rest or not any(s["text"].strip() for s in rest):
        return line, None
    lead = next(s for s in rest if s["text"].strip())
    if (lead["font"], round(lead["size"], 1), is_bold(lead)) not in prose:
        return line, None  # a title with a symbol or a change of type, not a run-in body
    head = Line(line.page, _span_box(spans[:k]), spans[:k], col=line.col)
    body_part = Line(line.page, _span_box(rest), rest, col=line.col)
    return head, body_part


def same_face(span: dict, ref: dict) -> bool:
    """Same typeface (font without subset or encoding tags), weight and size."""
    return (base_font_name(span["font"]) == base_font_name(ref["font"]) and is_bold(span) == is_bold(ref)
            and abs(span["size"] - ref["size"]) < 0.3)


def _span_box(spans: list[dict]) -> Rect:
    box = tuple(spans[0]["bbox"])
    for span in spans[1:]:
        box = union(box, tuple(span["bbox"]))
    return box


def continues_citation(prev: Line | None, line: Line, profile: Profile) -> bool:
    """True when ``line`` starts with a number only because a citation wrapped: the line
    just above ends with a reference word or connector ("... Sections 12.1 through")."""
    if prev is None or prev.col != line.col:
        return False
    gap = line.baseline - prev.baseline
    if not 0 < gap <= CONTINUATION_GAP * line.size:
        return False
    words = [w for kind in profile.references for w in kind.words]
    words += profile.list_connectors + profile.range_connectors
    tail = prev.text.rstrip()
    for word in sorted(set(words), key=len, reverse=True):
        if word[:1].isalnum():
            if re.search(rf"\b{re.escape(word)}$", tail, re.IGNORECASE):
                return True
        elif word and tail.endswith(word):
            return True
    return False


def _continues(prev: Line, line: Line) -> bool:
    """``line`` directly below ``prev`` in the same column and style (a wrapped title)."""
    last, first = prev.spans[-1], line.first_span
    same_style = last["font"] == first["font"] and abs(last["size"] - first["size"]) < 0.2
    gap = line.baseline - prev.baseline
    return line.col == prev.col and same_style and 0 < gap <= CONTINUATION_GAP * first["size"]


def _title_below(heading: Line, line: Line, ctx: DocContext) -> bool:
    """A number-only heading line (a chapter "1.") whose title is the next line in a non-body style."""
    if match_heading(line, ctx.profile) or line.col != heading.col:
        return False
    not_body = (line.font, line.size, line.bold) != ctx.body
    return not_body and 0 < line.baseline - heading.baseline <= 4 * line.size


def caption_match(line: Line, profile: Profile, body: tuple[str, float, bool]) -> tuple[str, str] | None:
    """('table' | 'figure', number) when the line starts a caption. Prose that merely
    begins with "Table 5-6 lists ..." is excluded by the caption style (or, without
    one in the profile, by not being in the body style)."""
    text = line.text.strip()
    first = line.first_span
    for kind, fmt in (("table", profile.table_caption), ("figure", profile.figure_caption)):
        if not (match := re.match(fmt.regex, text)):
            continue
        if fmt.style is not None:
            styled = fmt.style.matches(first["font"], first["size"], is_bold(first))
        else:
            styled = (line.font, line.size, line.bold) != body
        if styled:
            return kind, match.group("num")
    return None


def is_footnote(line: Line, ctx: DocContext) -> str | None:
    if line.size > ctx.body[1] - FOOTNOTE_SIZE_MARGIN:
        return None
    return tc.footnote_marker(line, ctx.body[1] - FOOTNOTE_SIZE_MARGIN, ctx.profile.footnote)


def _inside(line: Line, box: Rect, margin: float = 2.0) -> bool:
    cx, cy = (line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2
    return box[0] - margin <= cx <= box[2] + margin and box[1] - margin <= cy <= box[3] + margin


def bbox_col(bbox: Rect, page_width: float) -> int:
    centre = page_width / 2
    if bbox[2] <= centre + 2:
        return 0
    if bbox[0] >= centre - 2:
        return 1
    return -1


def page_blocks(page: int, ctx: DocContext, tables: list[TableSegment] = ()) -> list[Block]:
    """Ordered blocks for one page. Lines inside a table segment become one table block."""
    profile = ctx.profile
    width = ctx.sizes[page][0]
    lines = list(ctx.page_lines.get(page, []))
    blocks: list[Block] = []

    for seg in tables:
        blocks.append(Block("table", page, seg.bbox, bbox_col(seg.bbox, width), seg.caption or "", obj=seg))
        lines = [ln for ln in lines if not _inside(ln, seg.bbox)]

    for eq, members in eqs.find_equations(lines, profile, ctx.body):
        member_ids = {id(m) for m in members}
        lines = [ln for ln in lines if id(ln) not in member_ids]
        blocks.append(Block("equation", page, eq.bbox, members[0].col, eq.raw_text, number=eq.number, obj=eq))

    lines = reading_order(lines)
    i = 0
    while i < len(lines):
        line = lines[i]
        text = line.text.strip()
        style = line.style
        heading = match_heading(line, profile)
        if heading and continues_citation(lines[i - 1] if i else None, line, profile):
            heading = None
        if heading:
            stream, num, title = heading
            head, run_in = split_run_in(line, ctx.prose_styles)
            if run_in is not None:  # run-in heading: the rest of the line is body text
                match = re.match(stream.number_regex, head.text.strip())
                title = head.text.strip()[match.end():].strip() if match else title
                blocks.append(_block("heading", [head], style, number=num, stream=stream.name, title=title))
                blocks.append(_block("text", [run_in], run_in.style))
                i += 1
                continue
            group, tail = [line], None
            while (i + 1 < len(lines) and _continues(group[-1], lines[i + 1])
                   and same_face(lines[i + 1].first_span, line.first_span)
                   and not match_heading(lines[i + 1], profile)):
                i += 1
                part, tail = split_run_in(lines[i], ctx.prose_styles)  # a wrapped title may end in a run-in body
                group.append(part)
                title = f"{title} {part.text.strip()}".strip()
                if tail is not None:
                    break
            if tail is not None:
                blocks.append(_block("heading", group, style, number=num, stream=stream.name, title=title))
                blocks.append(_block("text", [tail], tail.style))
                i += 1
                continue
            if not title and i + 1 < len(lines) and _title_below(line, lines[i + 1], ctx):
                i += 1
                group.append(lines[i])
                title = lines[i].text.strip()
            blocks.append(_block("heading", group, style, number=num, stream=stream.name, title=title))
        elif caption := caption_match(line, profile, ctx.body):
            kind, num = caption
            group = [line]
            while (i + 1 < len(lines) and _continues(group[-1], lines[i + 1])
                   and not caption_match(lines[i + 1], profile, ctx.body)):
                i += 1
                group.append(lines[i])
            blocks.append(_block("caption", group, kind, number=num))
        elif marker := is_footnote(line, ctx):
            group = [line]
            while (i + 1 < len(lines) and _continues(group[-1], lines[i + 1])
                   and not is_footnote(lines[i + 1], ctx)):
                i += 1
                group.append(lines[i])
            blocks.append(_block("footnote", group, style, marker=marker))
        elif re.match(profile.where_block.intro_regex, text):
            items, end = eqs.parse_where(lines, i, profile)
            group = lines[i:end]
            blocks.append(_block("where", group, style, obj=items))
            i = end - 1
        else:
            blocks.append(_block("text", [line], style))
        i += 1
    return reading_order(blocks)


def _block(kind: str, group: list[Line], style: str, **fields) -> Block:
    box = group[0].bbox
    for line in group[1:]:
        box = union(box, line.bbox)
    text = " ".join(ln.text.strip() for ln in group)
    return Block(kind, group[0].page, box, group[0].col, text, style=style, lines=group, **fields)
