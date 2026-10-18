"""Pass 2 output schemas (SPEC Step 3b). Claude's answers are parsed into these, then
checked by ``app.ingest.pass2.validate``. Field descriptions go into the JSON schema
Claude sees, so they double as instructions."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PASS2_SCHEMA_VERSION = "pass2-v1"

def quote():
    return Field(description="Exact words copied from the source text that support this item")


# ---- notation and definitions pages --------------------------------------------

class NotationEntry(BaseModel):
    display: str = Field(description="The symbol as printed, in Unicode (e.g. f′cE)")
    latex: str = Field(description="LaTeX for the symbol (e.g. f'_{cE})")
    id: str = Field(description="Stable identifier: ASCII letters, digits and underscores, starting with a letter "
                                "(e.g. f_prime_cE); also the SymPy symbol name")
    description: str
    units: str | None = Field(description="Units as printed, or null")
    defined_in: list[str] = Field(description="Where the symbol is defined, as printed (e.g. 'Section 2.4.1')")
    aliases: list[str] = Field(description="Other ways the symbol is written in the text layer")
    scope_chapters: list[str] = Field(description="Chapter numbers this meaning applies to; empty when it applies everywhere")
    source_quote: str = quote()


class NotationPage(BaseModel):
    entries: list[NotationEntry]


class TermEntry(BaseModel):
    term: str
    definition: str
    aliases: list[str]
    source_quote: str = quote()


class DefinitionsPage(BaseModel):
    terms: list[TermEntry]


# ---- clauses ----------------------------------------------------------------------

class ParameterOut(BaseModel):
    id: str = Field(description="Notation id when the symbol is in the notation slice, else a new id in the same style")
    symbol: str = Field(description="The symbol as printed")
    latex: str
    description: str
    units: str | None
    source_quote: str = quote()


class EquationOut(BaseModel):
    equation_id: str | None = Field(description="Id of a numbered equation from the input, or null for an inline equation")
    latex: str = Field(description="Read from the equation image")
    sympy: str = Field(description="'lhs = rhs' in SymPy syntax using parameter ids as names and explicit '*' "
                                   "(functions: sqrt, Min, Max, Abs, log, exp, Piecewise; Sum(term, (i, lower, upper)) for a summation)")
    computes: str = Field(description="Parameter id on the left-hand side")
    uses: list[str] = Field(description="Parameter ids on the right-hand side")
    source_quote: str = quote()


class ConditionOut(BaseModel):
    text: str = Field(description="When or where the clause applies")
    source_quote: str = quote()


class RequirementOut(BaseModel):
    modal: Literal["shall", "should", "may", "other"]
    text: str
    source_quote: str = quote()


class ExceptionOut(BaseModel):
    text: str
    source_quote: str = quote()


ReferenceType = Literal["requires", "modifies", "exception_to", "alternative_to", "uses_value_from", "see_also"]


class ReferenceOut(BaseModel):
    text: str = Field(description="The reference text exactly as it appears in the outgoing references list")
    type: ReferenceType


class SymbolCorrection(BaseModel):
    raw: str = Field(description="Exact characters copied from the clause text")
    corrected: str = Field(description="The same span as printed in the image; only misread symbols changed")
    uncertain: bool = Field(description="True when the image does not make the symbol clear")
    note: str


class UnmappedItem(BaseModel):
    kind: str = Field(description="Short name for what this is")
    text: str
    source_quote: str = quote()


class ClauseExtraction(BaseModel):
    parameters: list[ParameterOut]
    equations: list[EquationOut]
    conditions: list[ConditionOut]
    requirements: list[RequirementOut]
    exceptions: list[ExceptionOut]
    references: list[ReferenceOut]
    symbol_corrections: list[SymbolCorrection]
    unmapped: list[UnmappedItem]


# ---- tables -----------------------------------------------------------------------

class HeaderParameter(BaseModel):
    col: int = Field(description="0-based column index in the grid")
    parameter_id: str
    header_text: str
    units: str | None


class CellValue(BaseModel):
    col: int
    value: str = Field(description="The cell text as printed")
    number: float | None = Field(description="The number in the cell, or null when it holds none")
    unit: str | None
    formula: str | None = Field(description="SymPy expression when the cell holds a formula, else null")


class RowOut(BaseModel):
    row: int = Field(description="Row index from the grid")
    conditions: list[str] = Field(description="What this row applies to, in plain words")
    values: list[CellValue]


class FootnoteRule(BaseModel):
    marker: str
    kind: Literal["interpolation", "condition", "override", "note"]
    rule: str
    applies_to_rows: list[int]
    applies_to_cols: list[int]


class BlankCell(BaseModel):
    row: int
    col: int
    interpretation: str = Field(description="What the blank means (e.g. 'not permitted', 'same as row above')")
    applies_to_rows: list[int]


class TableExtraction(BaseModel):
    header_parameters: list[HeaderParameter]
    rows: list[RowOut]
    footnote_rules: list[FootnoteRule]
    blank_cells: list[BlankCell]


# ---- second read of image-sourced table cells -----------------------------------------

class CellRead(BaseModel):
    row: int
    col: int
    value: str = Field(description="The cell as printed in the image")
    number: float | None = Field(description="The number in the cell, or null when it holds none")


class CellReads(BaseModel):
    cells: list[CellRead]
