"""Images for Pass 2 calls.

Clause regions and flagged lines are rendered on the fly from the PDF and never saved
(SPEC Step 3a.5). Equation and table crops are stored files from Pass 1. Each image has
a short key for the cache: (pdf hash, page, box, dpi) for rendered regions, the file's
hash for stored crops, so a cache key never depends on PNG bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

import pymupdf

from app.config import ROOT
from app.ingest.pdf_lines import Rect

REGION_DPI = 150  # a full column stays under the 1568 px long edge Claude reads at
LINE_DPI = 200
REGION_PADDING = 4.0
LINE_PADDING = 2.0
CLAUSE_CROP_MODES = ("always", "flagged")


@dataclass(frozen=True)
class ImageRef:
    label: str  # shown to Claude before the image
    key: str  # stable cache key
    kind: str  # rendered / file
    page: int = 0
    bbox: Rect | None = None
    dpi: int = 0
    padding: float = 0.0
    path: str | None = None

    def png(self, doc: pymupdf.Document | None) -> bytes:
        if self.kind == "file":
            return (ROOT / self.path).read_bytes() if not Path(self.path).is_absolute() else Path(self.path).read_bytes()
        return render(doc, self.page, self.bbox, self.dpi, self.padding)


def render(doc: pymupdf.Document, page: int, bbox: Rect | None, dpi: int, padding: float) -> bytes:
    """PNG bytes of a region of ``page`` (1-based), or of the whole page when bbox is None."""
    pg = doc[page - 1]
    clip = pg.rect if bbox is None else (pymupdf.Rect(bbox[0] - padding, bbox[1] - padding,
                                                      bbox[2] + padding, bbox[3] + padding) & pg.rect)
    return pg.get_pixmap(dpi=dpi, clip=clip).tobytes("png")


def region(pdf_hash: str, page: int, bbox: Rect | None, label: str, dpi: int = REGION_DPI,
           padding: float = REGION_PADDING) -> ImageRef:
    box = None if bbox is None else tuple(round(float(v), 1) for v in bbox)
    return ImageRef(label=label, key=f"{pdf_hash[:16]}:p{page}:{box}:{dpi}", kind="rendered", page=page, bbox=box,
                    dpi=dpi, padding=padding)


def stored(path: str, label: str) -> ImageRef | None:
    full = Path(path) if Path(path).is_absolute() else ROOT / path
    if not full.is_file():
        return None
    return ImageRef(label=label, key="file:" + hashlib.sha256(full.read_bytes()).hexdigest()[:24], kind="file",
                    path=path)


def clause_images(clause, pdf_hash: str, mode: str) -> list[ImageRef]:
    """Crops of the clause for its Pass 2 call: the whole region, one per page and column
    (mode 'always'), or only the lines flagged in Pass 1 (mode 'flagged')."""
    if mode not in CLAUSE_CROP_MODES:
        raise ValueError(f"CLAUSE_CROPS must be one of {CLAUSE_CROP_MODES}, not {mode!r}")
    if mode == "always":
        return [region(pdf_hash, page, box, f"clause region, page {page}") for page, box in clause.regions]
    return [region(pdf_hash, f.page, f.bbox, f"flagged line {f.id}", dpi=LINE_DPI, padding=LINE_PADDING)
            for f in clause.flagged]
