"""Step 3a.5: render regions at 200 DPI and return their repo-relative paths."""
from __future__ import annotations

from pathlib import Path
import re

import pymupdf

from app.config import ROOT
from app.ingest.pdf_lines import Rect

DPI = 200
PADDING = 3.0


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80]


def crop(doc: pymupdf.Document, page: int, bbox: Rect, path: Path, padding: float = PADDING) -> str:
    """Save the region of ``page`` (1-based) at 200 DPI; returns the path relative to the repo."""
    pg = doc[page - 1]
    rect = pymupdf.Rect(bbox[0] - padding, bbox[1] - padding, bbox[2] + padding, bbox[3] + padding) & pg.rect
    path.parent.mkdir(parents=True, exist_ok=True)
    pg.get_pixmap(dpi=DPI, clip=rect).save(path)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()
