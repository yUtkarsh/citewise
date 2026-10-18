"""Steps 3a.1, 3a.4 and 3a.6: table grids, captions, footnotes and multi-page merges.

Docling builds the grid on flagged pages. Its result is checked deterministically:
the numbers printed inside the table area must appear in its cells; when Docling
finds nothing or fails that check, PyMuPDF ``find_tables()`` is used instead, with
the vertical pieces it returns stitched back into one segment.
"""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import json
from pathlib import Path
import re

import pymupdf

from app.ingest import text_checks as tc
from app.ingest.pdf_lines import Line, Rect, is_bold, union
from app.ingest.pass1.model import Block, Footnote, Row, Table, TableSegment
from app.profile.schema import Profile

NUMBER = re.compile(r"\d+(?:\.\d+)?")
DOCLING_MIN_AGREEMENT = 0.9  # share of printed numbers Docling's cells must contain
CAPTION_REACH = 80.0  # max gap between a caption and the table below it
COLUMN_TOLERANCE = 3.0  # column x-positions that match across pages
STITCH_GAP = 40.0  # find_tables pieces closer than this are one table
ROW_BASELINE = 3.0  # lines of a borderless table within this many points form one row
CACHE_VERSION = "tables-v1"


# ---- grids -------------------------------------------------------------------

def numbers_in(lines: list[Line], box: Rect) -> Counter:
    tokens = Counter()
    for line in lines:
        cx, cy = (line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            tokens.update(NUMBER.findall(line.text))
    return tokens


def agreement(segment: TableSegment, lines: list[Line]) -> float:
    printed = numbers_in(lines, segment.bbox)
    if not printed:
        return 1.0
    cells = Counter(t for row in segment.rows for cell in dict.fromkeys(row) for t in NUMBER.findall(cell))
    found = sum(min(n, cells[t]) for t, n in printed.items())
    return found / sum(printed.values())


def _segment_from_rows(page: int, rows: list[list[str]], row_boxes: list[Rect], header_rows: int,
                       col_x: list[float], method: str) -> TableSegment:
    box = row_boxes[0]
    for b in row_boxes[1:]:
        box = union(box, b)
    return TableSegment(page=page, bbox=box, rows=rows, row_bboxes=row_boxes, header_rows=header_rows,
                        col_x=col_x, method=method)


def bold_row(page_lines: list[Line], box: Rect) -> bool:
    inside = [ln for ln in page_lines if _centre_in(ln.bbox, box)]
    return bool(inside) and all(is_bold(ln.first_span) for ln in inside)


def _centre_in(b: Rect, box: Rect, margin: float = 1.0) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return box[0] - margin <= cx <= box[2] + margin and box[1] - margin <= cy <= box[3] + margin


def find_tables_segments(page: pymupdf.Page, page_lines: list[Line]) -> list[TableSegment]:
    """PyMuPDF tables, with vertically stacked pieces of one table stitched together and
    the lines between pieces (row-group labels) kept as one-cell rows."""
    pieces = sorted(page.find_tables().tables, key=lambda t: t.bbox[1])
    groups: list[list] = []
    for piece in pieces:
        if groups:
            last = groups[-1][-1]
            gap = piece.bbox[1] - last.bbox[3]
            overlap = min(piece.bbox[2], last.bbox[2]) - max(piece.bbox[0], last.bbox[0])
            if gap < STITCH_GAP and overlap > 0.8 * (last.bbox[2] - last.bbox[0]):
                groups[-1].append(piece)
                continue
        groups.append([piece])
    segments = []
    for group in groups:
        rows, boxes = [], []
        for i, piece in enumerate(group):
            if i:
                between = (group[i - 1].bbox[0], group[i - 1].bbox[3], piece.bbox[2], piece.bbox[1])
                for line in page_lines:
                    if _centre_in(line.bbox, between, 0):
                        rows.append([line.text.strip()])
                        boxes.append(line.bbox)
            for row, cells in zip(piece.rows, piece.extract()):
                rows.append([(c or "").replace("\n", " ").strip() for c in cells])
                boxes.append(tuple(row.bbox))
        if not rows:
            continue
        header = 0
        while header < len(rows) - 1 and bold_row(page_lines, boxes[header]):
            header += 1
        first = group[0]
        col_x = sorted({round(c[0], 1) for c in first.cells if c})
        segments.append(_segment_from_rows(page.number + 1, rows, boxes, header, col_x, "find_tables"))
    return segments


@lru_cache(maxsize=1)
def docling_converter():
    """One Docling converter per process; OCR is off because the PDF has a text layer."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(do_ocr=False)
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def docling_segments(pdf: Path, pages: list[int], heights: dict[int, float]) -> dict[int, list[TableSegment]]:
    """Run Docling on the given pages (contiguous runs share one conversion)."""
    converter = docling_converter()
    out: dict[int, list[TableSegment]] = {p: [] for p in pages}
    for start, end in _runs(pages):
        document = converter.convert(str(pdf), page_range=(start, end)).document
        for table in document.tables:
            if not table.prov:
                continue
            page = table.prov[0].page_no
            if page in out:
                seg = _from_docling(table, page, heights[page])
                if seg:
                    out[page].append(seg)
    return out


def _from_docling(table, page: int, height: float) -> TableSegment | None:
    grid = table.data.grid
    if not grid:
        return None
    rows = [[cell.text.strip() for cell in row] for row in grid]
    boxes, header, col_x = [], 0, {}
    for r, row in enumerate(grid):
        cells = [c for c in row if c.bbox is not None]
        if not cells:
            boxes.append(boxes[-1] if boxes else (0, 0, 0, 0))
            continue
        tl = [c.bbox.to_top_left_origin(page_height=height) for c in cells]
        boxes.append((min(b.l for b in tl), min(b.t for b in tl), max(b.r for b in tl), max(b.b for b in tl)))
        if all(c.column_header for c in row if c.text.strip()) and header == r:
            header = r + 1
        for c, b in zip(cells, tl):
            if c.col_span == 1:
                col_x[c.start_col_offset_idx] = min(b.l, col_x.get(c.start_col_offset_idx, b.l))
    header = min(header, max(len(rows) - 1, 0))
    return _segment_from_rows(page, rows, boxes, header, [round(col_x[k], 1) for k in sorted(col_x)], "docling")


def _runs(pages: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for p in sorted(pages):
        if runs and p == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], p)
        else:
            runs.append((p, p))
    return runs


def _seg_to_json(s: TableSegment) -> dict:
    return {"page": s.page, "bbox": s.bbox, "rows": s.rows, "row_bboxes": s.row_bboxes,
            "header_rows": s.header_rows, "col_x": s.col_x, "method": s.method}


def _seg_from_json(d: dict) -> TableSegment:
    return TableSegment(page=d["page"], bbox=tuple(d["bbox"]), rows=d["rows"],
                        row_bboxes=[tuple(b) for b in d["row_bboxes"]], header_rows=d["header_rows"],
                        col_x=d["col_x"], method=d["method"])


def ruling_columns(page: pymupdf.Page, box: Rect) -> list[float]:
    """x-positions of the vertical rules inside a table box, so every segment's columns
    are measured the same way whichever method built its grid."""
    xs = []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]
                x, y0, y1, vertical = a.x, min(a.y, b.y), max(a.y, b.y), abs(a.x - b.x) < 1
            elif item[0] == "re":
                r = item[1]
                x, y0, y1, vertical = (r.x0 + r.x1) / 2, r.y0, r.y1, r.width < 2
            else:
                continue
            if vertical and y1 - y0 > 8 and box[0] - 2 <= x <= box[2] + 2 and y0 < box[3] and y1 > box[1]:
                xs.append(round(x, 1))
    cols: list[float] = []
    for x in sorted(xs):
        if not cols or x - cols[-1] > COLUMN_TOLERANCE:
            cols.append(x)
    return cols


def extract_segments(
    doc: pymupdf.Document, pdf: Path, pdf_hash: str, pages: list[int], page_lines: dict[int, list[Line]],
    use_docling: bool, cache_dir: Path, log=print,
) -> dict[int, list[TableSegment]]:
    """Table segments per page: Docling (cached per page) when it agrees with the
    printed numbers, otherwise find_tables()."""
    heights = {p: doc[p - 1].rect.height for p in pages}
    docling: dict[int, list[TableSegment]] = {}
    if use_docling:
        cache = cache_dir / "docling"
        cache.mkdir(parents=True, exist_ok=True)
        path_of = {p: cache / f"{pdf_hash[:12]}_{CACHE_VERSION}_p{p}.json" for p in pages}
        todo = [p for p in pages if not path_of[p].is_file()]
        if todo:
            log(f"tables: Docling on {len(todo)} page(s), cached afterwards")
            for p, segs in docling_segments(pdf, todo, heights).items():
                path_of[p].write_text(json.dumps([_seg_to_json(s) for s in segs]), encoding="utf-8")
        for p in pages:
            docling[p] = [_seg_from_json(d) for d in json.loads(path_of[p].read_text(encoding="utf-8"))]
    out = {}
    for p in pages:
        lines = page_lines.get(p, [])
        segs = docling.get(p, [])
        if not segs or any(agreement(s, lines) < DOCLING_MIN_AGREEMENT for s in segs):
            segs = find_tables_segments(doc[p - 1], lines)
        for seg in segs:
            rules = ruling_columns(doc[p - 1], seg.bbox)
            if len(rules) >= 3:
                seg.col_x = rules
        out[p] = segs
    return out


# ---- captions, merging, rows, footnotes ------------------------------------------

def caption_groups(lines: list[Line], profile: Profile, body: tuple[str, float, bool]) -> list[tuple[str, str, Rect]]:
    """(number, full text, bbox) of every table caption on a page, wrapped lines joined."""
    from app.ingest.pass1.blocks import caption_match

    out = []
    for line in lines:
        hit = caption_match(line, profile, body)
        if not hit or hit[0] != "table":
            continue
        group = [line]
        while nxt := _caption_continuation(group[-1], lines):
            if caption_match(nxt, profile, body):
                break
            group.append(nxt)
        box = group[0].bbox
        for ln in group[1:]:
            box = union(box, ln.bbox)
        out.append((hit[1], " ".join(ln.text.strip() for ln in group), box))
    return out


def text_segment(lines: list[Line], caption: tuple[str, str, Rect], profile: Profile,
                 body: tuple[str, float, bool], page: int) -> TableSegment | None:
    """A table with no ruled grid (borderless): the lines below its caption that overlap
    it horizontally, until body prose, a heading or another caption. Lines on one
    baseline form a row; each line is a cell."""
    from app.ingest.pass1.blocks import caption_match, match_heading

    num, text, cap = caption
    below = sorted((ln for ln in lines if ln.bbox[1] >= cap[3] - 1 and ln.bbox[0] < cap[2] and ln.bbox[2] > cap[0]),
                   key=lambda ln: ln.bbox[1])
    members: list[Line] = []
    for line in below:
        prose = (line.font, line.size, line.bold) == body
        if prose or match_heading(line, profile) or caption_match(line, profile, body):
            break
        members.append(line)
    rows: list[list[Line]] = []
    for line in sorted(members, key=lambda ln: ln.baseline):
        if rows and abs(rows[-1][0].baseline - line.baseline) <= ROW_BASELINE:
            rows[-1].append(line)
        else:
            rows.append([line])
    if not rows:
        return None
    cells = [[ln.text.strip() for ln in sorted(row, key=lambda ln: ln.bbox[0])] for row in rows]
    boxes = []
    for row in rows:
        box = row[0].bbox
        for ln in row[1:]:
            box = union(box, ln.bbox)
        boxes.append(box)
    seg = _segment_from_rows(page, cells, boxes, 0, [], "text_lines")
    seg.caption, seg.caption_num, seg.caption_bbox = text, num, cap
    return seg


def add_borderless(segments: dict[int, list[TableSegment]], page_lines: dict[int, list[Line]], profile: Profile,
                   body: tuple[str, float, bool], pages: list[int]) -> None:
    """Captions with no grid attached get a text-line segment."""
    for page in pages:
        lines = page_lines.get(page, [])
        used = {s.caption_bbox for s in segments.get(page, []) if s.caption_bbox}
        for caption in caption_groups(lines, profile, body):
            if caption[2] in used:
                continue
            seg = text_segment(lines, caption, profile, body, page)
            if seg:
                segments.setdefault(page, []).append(seg)


def trim_notes(segments: dict[int, list[TableSegment]], page_lines: dict[int, list[Line]], is_note,
               marker_regex: str = tc.DEFAULT_MARKER) -> None:
    """Table notes printed inside the grid box (below the last row that has several
    cells) are not grid rows: cut the box and its rows above the first note, so the
    notes become footnotes of the table. Notes are recognised only when the table or
    its caption references at least one of their markers; a cell that merely starts
    with a number is left alone."""
    for page, segs in segments.items():
        lines = page_lines.get(page, [])
        for seg in segs:
            inside = sorted((ln for ln in lines if _centre_in(ln.bbox, seg.bbox)), key=lambda ln: ln.bbox[1])
            multi = [ln for ln in inside if sum(1 for o in inside if o is not ln and abs(o.baseline - ln.baseline) <= ROW_BASELINE) > 0]
            last_row = max((ln.bbox[3] for ln in multi), default=seg.bbox[1])
            notes = [(ln, m) for ln in inside if ln.bbox[1] >= last_row - 1 and (m := is_note(ln))]
            if not notes:
                continue
            scope = inside + [ln for ln in lines if seg.caption_bbox and _centre_in(ln.bbox, seg.caption_bbox)]
            referenced = {m for ln in scope for m, _ in tc.superscripts(ln, marker_regex)}
            if not any(m in referenced for _, m in notes):
                continue
            cut = notes[0][0].bbox[1] - 0.5
            keep = [i for i, b in enumerate(seg.row_bboxes) if (b[1] + b[3]) / 2 < cut]
            seg.rows = [seg.rows[i] for i in keep]
            seg.row_bboxes = [seg.row_bboxes[i] for i in keep]
            seg.header_rows = min(seg.header_rows, len(seg.rows))
            seg.bbox = (seg.bbox[0], seg.bbox[1], seg.bbox[2], cut)


def _caption_continuation(prev: Line, lines: list[Line]) -> Line | None:
    """The wrapped next line of a caption: same style, directly below, starting
    within the previous line's horizontal extent."""
    last = prev.spans[-1]
    for line in lines:
        first = line.first_span
        gap = line.baseline - prev.baseline
        if (0 < gap <= 1.6 * first["size"] and first["font"] == last["font"]
                and abs(first["size"] - last["size"]) < 0.2 and prev.bbox[0] - 2 <= line.bbox[0] <= prev.bbox[2]):
            return line
    return None


def attach_captions(segments: dict[int, list[TableSegment]], page_lines: dict[int, list[Line]], profile: Profile,
                    body: tuple[str, float, bool]) -> None:
    """Pair each segment with the nearest table caption above it, and keep the caption
    out of the grid box (Docling's box sometimes includes it)."""
    cont = profile.table_caption.continued_regex
    for page, segs in segments.items():
        captions = caption_groups(page_lines.get(page, []), profile, body)
        for seg in segs:
            above = [c for c in captions if c[2][1] <= seg.bbox[1] + CAPTION_REACH
                     and c[2][1] <= seg.bbox[3] and seg.bbox[1] - c[2][3] < CAPTION_REACH
                     and min(c[2][2], seg.bbox[2]) > max(c[2][0], seg.bbox[0])
                     and c[2][3] <= seg.bbox[1] + 0.5 * (seg.bbox[3] - seg.bbox[1])]
            if not above:
                continue
            num, text, box = min(above, key=lambda c: abs(seg.bbox[1] - c[2][3]))
            seg.caption, seg.caption_num, seg.caption_bbox = text, num, box
            seg.continued = bool(cont and re.search(cont, text))
            if box[3] > seg.bbox[1]:  # caption inside the grid box: drop rows above its bottom
                keep = [i for i, b in enumerate(seg.row_bboxes) if (b[1] + b[3]) / 2 > box[3]]
                seg.rows = [seg.rows[i] for i in keep]
                seg.row_bboxes = [seg.row_bboxes[i] for i in keep]
                seg.header_rows = min(seg.header_rows, len(seg.rows))
                seg.bbox = (seg.bbox[0], box[3] + 0.5, seg.bbox[2], seg.bbox[3])


def _columns_match(a: TableSegment, b: TableSegment) -> bool:
    if len(a.col_x) != len(b.col_x) or not a.col_x:
        return False
    # relative to the table's left edge: even and odd pages often shift the whole table
    return all(abs((x - a.col_x[0]) - (y - b.col_x[0])) <= COLUMN_TOLERANCE for x, y in zip(a.col_x, b.col_x))


def merge_segments(ordered: list[TableSegment]) -> tuple[list[Table], list[TableSegment]]:
    """Group segments into tables. A segment continues the previous table when its
    caption names the same table (continued or not), or when it has no caption, starts
    the next page and matches the column count and positions. When the caption and the
    column layout disagree, it merges by caption and the table is flagged. Uncaptioned
    grids that continue nothing (flowcharts, boxed text) are returned separately."""
    tables: list[Table] = []
    skipped: list[TableSegment] = []
    for seg in ordered:
        prev = tables[-1] if tables else None
        last = prev.segments[-1] if prev else None
        same_number = prev is not None and seg.caption_num is not None and seg.caption_num == prev.number
        next_page = last is not None and seg.page == last.page + 1
        layout = last is not None and _columns_match(last, seg)
        if same_number:
            prev.segments.append(seg)
            if not layout or not (seg.continued or seg.page == last.page):
                prev.merge_conflict = True
            continue
        if seg.caption_num is None and layout and (
            (next_page and _first_on_page(seg, ordered)) or (last is not None and seg.page == last.page)
        ):
            prev.segments.append(seg)  # an uncaptioned piece continuing the table
            continue
        if seg.caption_num is None:
            skipped.append(seg)  # a grid with no caption that continues nothing: not a table
            continue
        tables.append(Table(id=f"Table {seg.caption_num}", number=seg.caption_num, caption=seg.caption or "",
                            segments=[seg]))
    _dedupe_ids(tables)
    return tables, skipped


def _first_on_page(seg: TableSegment, ordered: list[TableSegment]) -> bool:
    return min((s.bbox[1] for s in ordered if s.page == seg.page), default=seg.bbox[1]) == seg.bbox[1]


def _dedupe_ids(tables: list[Table]) -> None:
    seen = Counter()
    for t in tables:
        seen[t.id] += 1
        if seen[t.id] > 1:
            t.id = f"{t.id}~{seen[t.id]}"


def _norm(row: list[str]) -> str:
    return re.sub(r"\s+", " ", " | ".join(dict.fromkeys(c for c in row if c))).strip().lower()


def build_rows(table: Table, page_lines: dict[int, list[Line]], marker_regex: str = tc.DEFAULT_MARKER) -> None:
    """Rows in page order; header rows repeated on continuation pages are dropped.
    Row markers are superscripts inside the row (footnote references). A marker raised
    on a number in a body cell is stripped from the cell and kept in ``cell_markers``."""
    first = table.segments[0]
    headers = {_norm(r) for r in first.rows[:first.header_rows]}
    index = 0
    for n, seg in enumerate(table.segments):
        for i, (cells, box) in enumerate(zip(seg.rows, seg.row_bboxes)):
            is_header = i < seg.header_rows
            if n and (is_header or _norm(cells) in headers):
                continue  # repeated header on a continuation page
            inside = [line for line in page_lines.get(seg.page, []) if _centre_in(line.bbox, box)]
            markers = [m for line in inside for m, _ in tc.superscripts(line, marker_regex)]
            cells, cell_markers = (cells, {}) if is_header else tc.strip_value_markers(
                cells, [x for line in inside for x in tc.value_superscripts(line, marker_regex)])
            markers += [m for ms in cell_markers.values() for m in ms]
            table.rows.append(Row(index=index, page=seg.page, bbox=box, cells=cells, is_header=is_header,
                                  markers=sorted(set(markers)), cell_markers=cell_markers))
            index += 1


def attach_footnotes(tables: list[Table], blocks: dict[int, list[Block]]) -> set[int]:
    """A footnote block belongs to the nearest table segment above it on its page that
    overlaps it horizontally (two tables on a page each keep their own notes).
    Returns the ids of the blocks used."""
    by_page: dict[int, list[tuple[TableSegment, Table]]] = {}
    for table in tables:
        for seg in table.segments:
            by_page.setdefault(seg.page, []).append((seg, table))
    used = set()
    for page, segs in by_page.items():
        for block in blocks.get(page, []):
            if block.kind != "footnote":
                continue
            above = [(seg, table) for seg, table in segs
                     if block.bbox[1] >= seg.bbox[3] - 2
                     and min(block.bbox[2], seg.bbox[2]) > max(block.bbox[0], seg.bbox[0])]
            if not above:
                continue
            _, table = max(above, key=lambda st: st[0].bbox[3])
            table.footnotes.append(Footnote(block.marker, block.text, block.page, block.bbox))
            used.add(id(block))
    return used


def candidate_pages(page_map, lines: dict[int, list[Line]], profile: Profile, pages: list[int]) -> list[int]:
    """Pages the page map flagged for tables, plus pages with a table caption."""
    flagged = {s.page for s in page_map if s.has_table}
    captioned = {p for p in pages for ln in lines.get(p, []) if re.match(profile.table_caption.regex, ln.text.strip())}
    return sorted((flagged | captioned) & set(pages))
