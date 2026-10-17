"""Step 3a.1: one ordered list of blocks per page, every pattern from the profile."""
from __future__ import annotations

from dataclasses import dataclass, field
import re

import pymupdf

from app.ingest import text_checks as tc
from app.ingest.pdf_lines import Line, Rect, body_style, is_bold, read_lines, union
from app.ingest.pass1 import equations as eqs
from app.ingest.pass1.layout import assign_columns, is_running, reading_order, repeated_band_lines
from app.ingest.pass1.model import Block, TableSegment
from app.profile.schema import Profile, Stream

CONTINUATION_GAP = 1.6  # line pitch, in font sizes, that still continues a heading or caption
FOOTNOTE_SIZE_MARGIN = 0.5  # footnotes are set smaller than the document body text


@dataclass
class DocContext:
    """Document-wide facts the per-page classification needs."""

    profile: Profile
    body: tuple[str, float, bool]
    repeated: set[str]
    page_lines: dict[int, list[Line]] = field(default_factory=dict)  # running elements removed
    sizes: dict[int, tuple[float, float]] = field(default_factory=dict)  # page -> (width, height)


def load_context(doc: pymupdf.Document, profile: Profile, pages: list[int]) -> DocContext:
    raw = {p: read_lines(doc[p - 1]) for p in pages}
    sizes = {p: (doc[p - 1].rect.width, doc[p - 1].rect.height) for p in pages}
    repeated = repeated_band_lines(raw, {p: h for p, (_, h) in sizes.items()})
    cleaned = {
        p: [ln for ln in lines if not is_running(ln, sizes[p][1], profile, repeated)]
        for p, lines in raw.items()
    }
    for p, lines in cleaned.items():
        assign_columns(lines, sizes[p][0])
    body = profile.body_style or body_style([ln for lines in cleaned.values() for ln in lines])
    return DocContext(profile=profile, body=body, repeated=repeated, page_lines=cleaned, sizes=sizes)


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
        if heading := match_heading(line, profile):
            stream, num, title = heading
            group = [line]
            while i + 1 < len(lines) and _continues(group[-1], lines[i + 1]) and not match_heading(lines[i + 1], profile):
                i += 1
                group.append(lines[i])
                title = f"{title} {lines[i].text.strip()}".strip()
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
