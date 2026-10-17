"""Step 2.1: the page map, one deterministic summary line per page.

Claude reads the rendered map to label page ranges (front matter, body, ...), so
each line must be short and must not depend on any one document's conventions.
"""
from __future__ import annotations

from collections import Counter
import re

import pymupdf
from pydantic import BaseModel

from app.ingest.pdf_lines import Line, body_style, is_bold, read_lines

NUMBERED = re.compile(r"^\s*[A-Z]?\d+(?:[.\-–]\d+)*\.?(?:\s|$)")
DOT_LEADER = re.compile(r"(?:\.\s?){6,}")
NUMERIC_CELL = re.compile(r"^[\s\-–+(]*[\d.,]+%?[)\s]*$")
RUNNING_BAND = 0.08  # top and bottom fraction of the page where running elements sit
HEADING_MAX_CHARS = 100


class PageSummary(BaseModel):
    page: int  # 1-based
    line_count: int
    numbered_lines: int
    header: str
    footer: str
    first_heading: str
    heading_styles: list[str]
    body_size: float
    dot_leaders: int
    has_table: bool
    has_figure: bool
    columns: int


def _in_band(line: Line, height: float) -> str | None:
    centre = (line.bbox[1] + line.bbox[3]) / 2
    if centre < height * RUNNING_BAND:
        return "top"
    if centre > height * (1 - RUNNING_BAND):
        return "bottom"
    return None


def _is_heading_like(line: Line, body: tuple[str, float, bool]) -> bool:
    if len(line.text.strip()) > HEADING_MAX_CHARS:
        return False
    bolder = is_bold(line.first_span) and not body[2]
    return line.size > body[1] + 0.4 or bolder


def ruling_counts(page: pymupdf.Page) -> tuple[int, int, int, int]:
    """(horizontal rules, vertical rules, boxes, curves) from the page drawings."""
    h = v = boxes = curves = 0
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            kind = item[0]
            if kind == "l":
                a, b = item[1], item[2]
                if abs(a.y - b.y) < 1 and abs(a.x - b.x) > 20:
                    h += 1
                elif abs(a.x - b.x) < 1 and abs(a.y - b.y) > 10:
                    v += 1
            elif kind == "re":
                r = item[1]
                if r.height < 2 and r.width > 20:
                    h += 1
                elif r.width < 2 and r.height > 10:
                    v += 1
                elif r.width > 20 and r.height > 10:
                    boxes += 1
            elif kind == "c":
                curves += 1
    return h, v, boxes, curves


def numeric_grid(lines: list[Line]) -> bool:
    """At least 3 rows that each hold 3 or more short numeric spans."""
    rows = 0
    for line in lines:
        cells = [s for s in line.spans if NUMERIC_CELL.match(s["text"]) and s["text"].strip()]
        rows += len(cells) >= 3
    return rows >= 3


def column_count(lines: list[Line], width: float) -> int:
    """2 when most lines sit wholly on one side of the page centre and both sides are used."""
    centre = width / 2
    left = sum(1 for ln in lines if ln.bbox[2] < centre)
    right = sum(1 for ln in lines if ln.bbox[0] > centre)
    if not lines or min(left, right) < 3:
        return 1
    return 2 if (left + right) / len(lines) > 0.7 else 1


def summarize_page(page: pymupdf.Page) -> PageSummary:
    lines = read_lines(page)
    height, width = page.rect.height, page.rect.width
    content = [ln for ln in lines if _in_band(ln, height) is None]
    body = body_style(content)
    headings = [ln for ln in content if _is_heading_like(ln, body)]
    styles = Counter(f"{ln.font} {ln.size}{' bold' if ln.bold else ''}" for ln in headings)
    h, v, boxes, curves = ruling_counts(page)
    big_images = [
        i for i in page.get_image_info()
        if (i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]) > 0.02 * width * height
    ]
    return PageSummary(
        page=page.number + 1,
        line_count=len(content),
        numbered_lines=sum(1 for ln in content if NUMBERED.match(ln.text)),
        header=" | ".join(ln.text.strip() for ln in lines if _in_band(ln, height) == "top")[:60],
        footer=" | ".join(ln.text.strip() for ln in lines if _in_band(ln, height) == "bottom")[:60],
        first_heading=headings[0].text.strip()[:70] if headings else "",
        heading_styles=[s for s, _ in styles.most_common(3)],
        body_size=body[1],
        dot_leaders=sum(1 for ln in content if DOT_LEADER.search(ln.text)),
        has_table=(h >= 3 and v >= 2) or numeric_grid(content),
        has_figure=bool(big_images) or curves >= 10 or boxes >= 4,
        columns=column_count(content, width),
    )


def build_page_map(doc: pymupdf.Document) -> list[PageSummary]:
    return [summarize_page(page) for page in doc]


def render_page_map(summaries: list[PageSummary]) -> str:
    """One compact line per page for Claude."""
    out = ["page | lines | numbered | cols | dots | T/F | header | footer | first heading | heading styles"]
    for s in summaries:
        flags = ("T" if s.has_table else "-") + ("F" if s.has_figure else "-")
        out.append(
            f"p{s.page} | {s.line_count} | {s.numbered_lines} | {s.columns} | {s.dot_leaders} | {flags} | "
            f"{s.header} | {s.footer} | {s.first_heading} | {'; '.join(s.heading_styles)}"
        )
    return "\n".join(out)
