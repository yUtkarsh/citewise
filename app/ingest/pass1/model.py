"""Pass 1 data objects. Every object that becomes a graph node keeps its page and bbox."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.ingest.pdf_lines import Line, Rect


@dataclass
class Block:
    kind: str  # heading, text, caption, footnote, equation, where, table, figure
    page: int
    bbox: Rect
    col: int
    text: str
    style: str = ""
    lines: list[Line] = field(default_factory=list)
    number: str | None = None  # heading / caption / equation number
    stream: str | None = None  # heading stream
    title: str | None = None  # heading title
    marker: str | None = None  # footnote marker
    obj: object | None = None  # the Table / Equation / Figure this block stands for


@dataclass
class Clause:
    id: str
    number: str
    stream: str
    kind: str  # provision / commentary
    title: str
    depth: int
    page: int
    bbox: Rect
    parent_id: str | None = None
    commentary_on: str | None = None
    container_id: str | None = None  # for a nested stream: the clause that quotes it
    page_end: int = 0
    text_parts: list[str] = field(default_factory=list)
    text: str = ""
    order: int = 0


@dataclass
class TableSegment:
    page: int
    bbox: Rect
    rows: list[list[str]]
    row_bboxes: list[Rect]
    header_rows: int
    col_x: list[float]
    method: str  # docling / find_tables
    caption: str | None = None
    caption_num: str | None = None
    continued: bool = False
    crop: str | None = None
    table_id: str | None = None  # set when segments are merged into tables
    caption_bbox: Rect | None = None


@dataclass
class Row:
    index: int
    page: int
    bbox: Rect
    cells: list[str]
    is_header: bool
    markers: list[str] = field(default_factory=list)


@dataclass
class Footnote:
    marker: str
    text: str
    page: int
    bbox: Rect


@dataclass
class Table:
    id: str
    number: str | None
    caption: str
    segments: list[TableSegment]
    rows: list[Row] = field(default_factory=list)
    footnotes: list[Footnote] = field(default_factory=list)
    merge_conflict: bool = False
    header_crop: str | None = None
    clause_id: str | None = None

    @property
    def pages(self) -> list[int]:
        return [s.page for s in self.segments]


@dataclass
class WhereItem:
    symbol: str
    definition: str
    unit: str | None
    page: int
    bbox: Rect
    owner_id: str | None = None  # the equation it follows, else its clause


@dataclass
class Equation:
    id: str
    number: str
    page: int
    bbox: Rect
    raw_text: str
    raw_spans: list[dict]
    crop: str | None = None
    clause_id: str | None = None
    where: list[WhereItem] = field(default_factory=list)


@dataclass
class Figure:
    id: str
    number: str
    caption: str
    page: int
    bbox: Rect
    crop: str | None = None
    clause_id: str | None = None


@dataclass
class Reference:
    source_id: str
    text: str
    kind: str  # clause / table / figure / equation / external
    target_id: str | None
    method: str  # link / regex
    page: int


@dataclass
class FlaggedLine:
    id: str
    page: int
    bbox: Rect
    text: str
    reasons: list[str]
    crop: str | None = None
    clause_id: str | None = None
