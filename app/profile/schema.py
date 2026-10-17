"""The document profile: the rules Claude proposes for one document (SPEC Step 2).

Everything document-specific (heading numbers, caption and footnote formats,
running headers, reference words) lives here, never in the extraction code.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

PROFILE_SCHEMA_VERSION = "profile-v2"

RangeKind = Literal[
    "front_matter", "toc", "notation", "definitions", "body", "commentary", "appendix",
    "references", "index", "other",
]
SEGMENTED_KINDS = {"body", "commentary", "appendix"}  # pages Pass 1 segments into clauses


def check_regex(pattern: str, groups: tuple[str, ...] = ()) -> str:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"regex {pattern!r} does not compile: {exc}") from exc
    missing = [g for g in groups if g not in compiled.groupindex]
    if missing:
        raise ValueError(f"regex {pattern!r} lacks named group(s) {missing}")
    return pattern


# ---- call 1: range labelling ------------------------------------------------

class PageRange(BaseModel):
    start: int
    end: int
    kind: RangeKind

    @model_validator(mode="after")
    def ordered(self) -> "PageRange":
        if self.start < 1 or self.end < self.start:
            raise ValueError(f"bad page range {self.start}-{self.end}")
        return self


class RangeLabels(BaseModel):
    ranges: list[PageRange]
    sample_pages: list[int]
    title_pages: list[int]
    notes: str

    @model_validator(mode="after")
    def consistent(self) -> "RangeLabels":
        spans = sorted((r.start, r.end) for r in self.ranges)
        for (_, end), (start, _) in zip(spans, spans[1:]):
            if start <= end:
                raise ValueError(f"page ranges overlap at page {start}")
        if not 4 <= len(self.sample_pages) <= 12:
            raise ValueError("pick 4 to 12 sample pages")
        return self

    def errors_for(self, page_count: int) -> list[str]:
        """Checks that need the page count."""
        errors = []
        if any(r.end > page_count for r in self.ranges):
            errors.append(f"a range ends after the last page ({page_count})")
        segmented = {p for r in self.ranges if r.kind in SEGMENTED_KINDS for p in range(r.start, r.end + 1)}
        if not segmented:
            errors.append("no body pages labelled")
        outside = [p for p in self.sample_pages if p not in segmented]
        if outside:
            errors.append(f"sample pages {outside} are not body/commentary/appendix pages")
        if any(not 1 <= p <= page_count for p in self.title_pages):
            errors.append("title page out of range")
        return errors


# ---- call 2: rule proposal ---------------------------------------------------

class StyleSpec(BaseModel):
    font_regex: str  # matched with re.search against the PDF font name
    size_min: float
    size_max: float
    bold: bool | None  # None: either

    @field_validator("font_regex")
    @classmethod
    def check_pattern(cls, v):
        return check_regex(v)

    def matches(self, font: str, size: float, bold: bool) -> bool:
        if not re.search(self.font_regex, font):
            return False
        if not self.size_min - 0.05 <= size <= self.size_max + 0.05:
            return False
        return self.bold is None or self.bold == bold


class Level(BaseModel):
    depth: int  # number of components in the heading number, 1 = chapter
    style: StyleSpec


class Stream(BaseModel):
    name: str
    kind: Literal["provision", "commentary"]
    number_regex: str  # matched at the start of a line; group "num" is the number without prefix
    prefix: str  # literal prefix in front of num ("" for provisions)
    separator: str  # between number components, e.g. "."
    commentary_of: str | None  # for commentary: name of the provision stream
    levels: list[Level]
    id_prefix: str = ""  # namespace for clause ids when numbers repeat across streams, e.g. a quoted standard
    nested_in: str | None = None  # stream whose current clause contains this stream's clauses
    examples: list[str]  # heading numbers exactly as printed on the sample pages

    @field_validator("number_regex")
    @classmethod
    def check_pattern(cls, v):
        return check_regex(v, ("num",))

    @model_validator(mode="after")
    def examples_match(self) -> "Stream":
        if not self.levels:
            raise ValueError(f"stream {self.name}: no heading levels")
        for example in self.examples:
            if not re.match(self.number_regex, example):
                raise ValueError(f"stream {self.name}: example {example!r} does not match number_regex")
        return self


class CaptionFormat(BaseModel):
    regex: str  # matched at the start of a line; group "num"
    continued_regex: str | None  # searched in the caption text of continuation pages
    style: StyleSpec | None  # style of the caption's first span; None = any non-body style

    @field_validator("regex")
    @classmethod
    def check_pattern(cls, v):
        return check_regex(v, ("num",))
    @field_validator("continued_regex")
    @classmethod
    def check_cont(cls, v):
        return v if v is None else check_regex(v)


class FootnoteFormat(BaseModel):
    line_regex: str  # matched at the start of a footnote line; group "marker"
    marker_superscript: bool  # footnote lines may instead start with a raised marker
    marker_regex: str = r"\d{1,2}"  # shape of one marker, e.g. a digit or a lowercase letter

    @field_validator("line_regex")
    @classmethod
    def check_pattern(cls, v):
        return check_regex(v, ("marker",))

    @field_validator("marker_regex")
    @classmethod
    def check_marker(cls, v):
        return check_regex(v)


class WhereBlock(BaseModel):
    intro_regex: str  # the line that opens a where-block
    item_regex: str  # one item; groups "symbol" and "definition"

    @field_validator("intro_regex")
    @classmethod
    def check_intro(cls, v):
        return check_regex(v)
    @field_validator("item_regex")
    @classmethod
    def check_item(cls, v):
        return check_regex(v, ("symbol", "definition"))


class RunningElement(BaseModel):
    regex: str  # searched in the line text
    zone: Literal["top", "bottom", "any"]

    @field_validator("regex")
    @classmethod
    def check_pattern(cls, v):
        return check_regex(v)


class ReferenceKind(BaseModel):
    kind: Literal["clause", "table", "figure", "equation"]
    words: list[str]  # words that introduce a reference, singular and plural
    number_regex: str  # one cited number, no anchors; may include the commentary prefix

    @field_validator("number_regex")
    @classmethod
    def check_pattern(cls, v):
        return check_regex(v)


class ProfileRules(BaseModel):
    """What Claude returns from the sample pages."""

    title: str
    edition: str
    jurisdiction: str
    self_designation: str  # how the document names itself, e.g. its report number
    streams: list[Stream]
    table_caption: CaptionFormat
    figure_caption: CaptionFormat
    equation_number: str  # full text of a stand-alone equation number; group "num"
    footnote: FootnoteFormat
    where_block: WhereBlock
    running_elements: list[RunningElement]
    references: list[ReferenceKind]
    list_connectors: list[str]
    range_connectors: list[str]
    external_code_regex: str  # group "code"
    unmapped_fonts: list[str] = []  # font-name regexes whose glyphs come out as wrong characters

    @field_validator("equation_number")
    @classmethod
    def check_eq(cls, v):
        return check_regex(v, ("num",))
    @field_validator("external_code_regex")
    @classmethod
    def check_ext(cls, v):
        return check_regex(v, ("code",))

    @field_validator("unmapped_fonts")
    @classmethod
    def check_fonts(cls, v):
        return [check_regex(p) for p in v]

    @model_validator(mode="after")
    def streams_consistent(self) -> "ProfileRules":
        names = {s.name for s in self.streams}
        for s in self.streams:
            if s.nested_in is not None and (s.nested_in not in names or s.nested_in == s.name):
                raise ValueError(f"stream {s.name}: nested_in {s.nested_in!r} is not another stream")
        ids = [(s.id_prefix, s.prefix) for s in self.streams]
        if len(set(ids)) != len(ids):
            raise ValueError("two streams would produce the same clause ids; give one an id_prefix")
        if not any(s.kind == "provision" for s in self.streams):
            raise ValueError("at least one provision stream is required")
        for s in self.streams:
            if s.commentary_of is not None and s.commentary_of not in names:
                raise ValueError(f"stream {s.name}: commentary_of {s.commentary_of!r} is not a stream")
        return self


# ---- the saved profile -------------------------------------------------------

class Validation(BaseModel):
    status: Literal["pass", "warning"]
    coverage: float
    heading_count: int
    order_violations: int
    hierarchy_violations: int
    low_coverage_ranges: list[tuple[int, int]]
    revised: bool
    issues: list[str]
    checks: dict[str, float] = {}  # self-consistency shares (where_items, references, ids)


class Profile(ProfileRules):
    schema_version: str = PROFILE_SCHEMA_VERSION
    page_count: int = 0
    ranges: list[PageRange] = []
    body_style: tuple[str, float, bool] | None = None  # measured over all body pages, not proposed by Claude
    validation: Validation | None = None

    def segmented_pages(self) -> list[int]:
        pages = {p for r in self.ranges if r.kind in SEGMENTED_KINDS for p in range(r.start, r.end + 1)}
        return sorted(pages)

    def stream(self, name: str) -> Stream:
        return next(s for s in self.streams if s.name == name)
