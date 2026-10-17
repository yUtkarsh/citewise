"""Step 3a.4: figures paired with their captions (cropped for display only)."""
from __future__ import annotations

import pymupdf

from app.ingest.pdf_lines import Rect, union
from app.ingest.pass1.model import Block, Figure

MIN_AREA = 100.0  # square points; smaller drawings are rules and dots
REACH = 400.0  # max distance between a caption and its graphics
CLUSTER_GAP = 30.0


def graphics(page: pymupdf.Page) -> list[Rect]:
    rects = [tuple(i["bbox"]) for i in page.get_image_info()]
    for drawing in page.get_drawings():
        r = drawing["rect"]
        if r.width * r.height >= MIN_AREA or (r.width > 20 and r.height > 20):
            rects.append((r.x0, r.y0, r.x1, r.y1))
    return [r for r in rects if (r[2] - r[0]) * (r[3] - r[1]) > 0]


def _cluster(rects: list[Rect], start_y: float, upward: bool) -> Rect | None:
    """Graphics contiguous with the caption on one side, nearest first."""
    rects = sorted(rects, key=lambda r: -r[3] if upward else r[1])
    box, edge = None, start_y
    for r in rects:
        gap = (edge - r[3]) if upward else (r[1] - edge)
        if gap > (REACH if box is None else CLUSTER_GAP):
            break
        box = r if box is None else union(box, r)
        edge = box[1] if upward else box[3]
    return box


def find_figure(caption: Block, page: pymupdf.Page) -> Figure:
    """The figure for a caption block: the graphics cluster nearest above or below it,
    overlapping it horizontally. Falls back to the caption box when none is found."""
    cap = caption.bbox
    near = [r for r in graphics(page) if min(r[2], cap[2]) > max(r[0], cap[0])]
    above = _cluster([r for r in near if r[3] <= cap[1] + 2], cap[1], upward=True)
    below = _cluster([r for r in near if r[1] >= cap[3] - 2], cap[3], upward=False)
    options = [b for b in (above, below) if b]
    box = min(options, key=lambda b: min(abs(cap[1] - b[3]), abs(b[1] - cap[3]))) if options else cap
    return Figure(id=f"Figure {caption.number}", number=caption.number, caption=caption.text,
                  page=caption.page, bbox=union(box, cap))
