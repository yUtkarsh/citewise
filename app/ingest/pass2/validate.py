"""Deterministic checks on every Pass 2 result (SPEC Step 3b, Validation per result).

Each check returns ``Issue``s. An issue with ``retry`` makes the call retry once with
the issues in the prompt; after that the result is stored as flagged. Issues without
``retry`` (blank-cell readings, uncertain symbol corrections, unmapped items) only flag
the result for review. Severity follows SPEC Step 4.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import difflib
import re
import unicodedata

from app.ingest import text_checks as tc
from app.ingest.pass1.layout import normalize
from app.ingest.pass2.records import ClauseRec, RowRec, TableRec
from app.ingest.pass2.schemas import (ClauseExtraction, DefinitionsPage, NotationPage, SymbolCorrection,
                                      TableExtraction)

SEVERITY = {
    "table_number_mismatch": "high", "equation_letter_mismatch": "high", "sympy_parse": "high",
    "undefined_symbol": "high", "blank_cell": "high", "grid_dimension": "high", "invalid_output": "high",
    "uncertain_symbol_correction": "medium", "symbol_correction": "medium", "equation_structure": "medium",
    "source_quote": "low", "unmapped": "low", "reference_unknown": "low", "footnote_unknown": "low",
    "bad_id": "low", "refused": "high",
}
GRID_METHODS = {"docling", "find_tables"}  # grids whose cells are trusted for exact matching
SHORT_EDIT_RAW, SHORT_EDIT_NEW = 2, 3  # character-level edit outside suspect text
MIN_UNCHANGED = 0.8
LETTER_MATCH = 0.6
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SAFE_EXPR = re.compile(r"^[A-Za-z0-9_+\-*/().,=<>\s]*$")
RELATION = re.compile(r"<=|>=|==|=|<|>")
NUMBER_RE = re.compile(r"(?<![\d.])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|(?<![\d.])[-−]?\.\d+")
QUOTE_SPLIT = re.compile(r"\.\.\.|…")


@dataclass
class Issue:
    code: str
    detail: str
    retry: bool = True

    @property
    def severity(self) -> str:
        return SEVERITY.get(self.code, "low")

    def as_dict(self) -> dict:
        return {"reason": self.code, "severity": self.severity, "detail": self.detail}


def needs_retry(issues: list[Issue]) -> bool:
    return any(i.retry for i in issues)


# ---- text helpers -------------------------------------------------------------------

def norm_text(text: str) -> str:
    """Form used to compare quotes with sources: compatibility forms, ligatures, primes,
    dashes and quotes folded; whitespace removed (the text layer spaces unreliably); lower case."""
    text = unicodedata.normalize("NFKC", normalize(text))
    for src, dst in {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "‐": "-", "‑": "-"}.items():
        text = text.replace(src, dst)
    return re.sub(r"\s+", "", text).lower()


def quote_found(quote: str, source_norm: str) -> bool:
    pieces = [norm_text(p) for p in QUOTE_SPLIT.split(quote)]
    pieces = [p for p in pieces if p]
    return bool(pieces) and all(p in source_norm for p in pieces)


def check_quotes(items: list[tuple[str, str]], source: str) -> list[Issue]:
    """``items`` are (what, quote) pairs; every quote must appear in ``source``."""
    source_norm = norm_text(source)
    return [Issue("source_quote", f"{what}: quote not found in the source text: {quote[:120]!r}")
            for what, quote in items if not quote_found(quote, source_norm)]


def numbers(text: str) -> list[float]:
    out = []
    for token in NUMBER_RE.findall(text or ""):
        try:
            out.append(float(token.replace("−", "-").replace(",", "")))
        except ValueError:
            continue
    return out


# ---- SymPy -----------------------------------------------------------------------------

def _sympy_globals() -> dict:
    import sympy
    names = ["sqrt", "Min", "Max", "Abs", "log", "exp", "Piecewise", "Sum", "Integer", "Float", "Rational",
             "Symbol", "pi", "And", "Or"]
    return {name: getattr(sympy, name) for name in names}


def parse_side(text: str):
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations

    return parse_expr(text, local_dict={}, global_dict=_sympy_globals(), transformations=standard_transformations)


def parse_formula(text: str):
    """A SymPy expression (no relation); raises ValueError."""
    if not SAFE_EXPR.match(text) or "__" in text or RELATION.search(text):
        raise ValueError("only names, numbers, + - * / ** ( ) and commas are allowed")
    try:
        return parse_side(text)
    except Exception as exc:  # SymPy raises many exception types on bad input
        raise ValueError(f"SymPy could not parse it: {exc}") from exc


def parse_relation(text: str):
    """(lhs, rhs) SymPy expressions of 'lhs = rhs' (or an inequality); raises ValueError."""
    if not SAFE_EXPR.match(text) or "__" in text:
        raise ValueError("only names, numbers, + - * / ** ( ) , and one relation are allowed")
    parts = RELATION.split(text)
    if len(parts) != 2:
        raise ValueError("expected exactly one '=' (or one inequality)")
    try:
        return parse_side(parts[0]), parse_side(parts[1])
    except Exception as exc:  # SymPy raises many exception types on bad input
        raise ValueError(f"SymPy could not parse it: {exc}") from exc


def latex_letters(latex: str) -> Counter:
    stripped = re.sub(r"\\[A-Za-z]+", " ", latex)
    return Counter(c for c in stripped if c.isascii() and c.isalpha())


def letters_match(latex: str, raw: str) -> float:
    """Share of the LaTeX's ASCII letters also found in the text layer."""
    ours, theirs = latex_letters(latex), Counter(c for c in raw if c.isascii() and c.isalpha())
    total = sum(ours.values())
    return 1.0 if not total else sum(min(n, theirs[c]) for c, n in ours.items()) / total


# ---- symbol corrections ----------------------------------------------------------------

def suspect_mask(text: str, suspect_spans: list[str]) -> list[bool]:
    """True at characters that came from flagged spans or unmapped fonts (by occurrence
    of those spans' text) and at characters the warning rules flag on their own."""
    mask = [tc.char_class(c) != "other" for c in text]
    for span in suspect_spans:
        if not span:
            continue
        start = text.find(span)
        while start != -1:
            for i in range(start, start + len(span)):
                mask[i] = True
            start = text.find(span, start + 1)
    return mask


def involves_symbol(changed: str) -> bool:
    """True if a changed run touches a symbol: a character that is not an ASCII letter,
    digit or whitespace ('p' -> '√', '0' -> '′', '-' -> '−', 'a' -> 'α'). Letter-for-letter
    and spacing edits are spelling or layout, not misread symbols."""
    return any(not (c.isascii() and (c.isalnum() or c.isspace())) for c in changed)


def check_correction(corr: SymbolCorrection, text: str, mask: list[bool]) -> tuple[Issue | None, int]:
    """Accept a correction that only replaces misread symbols. Suspect characters may be
    replaced in runs of any length; anywhere else each change is at most 2 characters
    replaced by at most 3, no replacement swaps letters or digits for letters or digits,
    the correction changes at least one symbol, and at least 80% of the other
    characters stay unchanged (a moved prime, 'fc0' -> 'f′c', deletes a digit and
    inserts the symbol elsewhere).
    Returns (issue or None, position of raw in text)."""
    if not corr.raw:
        return Issue("symbol_correction", "a correction has an empty raw text"), -1
    start = text.find(corr.raw)
    if start == -1:
        return Issue("symbol_correction", f"raw text {corr.raw[:60]!r} is not in the clause text"), -1
    if corr.raw == corr.corrected:
        return Issue("symbol_correction", f"correction of {corr.raw[:60]!r} changes nothing"), start
    plain_total = sum(1 for i, c in enumerate(corr.raw) if not mask[start + i] and not c.isspace())
    plain_changed = 0
    ops = difflib.SequenceMatcher(None, corr.raw, corr.corrected, autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op == "equal":
            continue
        seg = [(i, corr.raw[i]) for i in range(i1, i2) if not corr.raw[i].isspace()]
        suspect = bool(seg) and all(mask[start + i] for i, _ in seg)
        if suspect:
            continue
        if i2 - i1 > SHORT_EDIT_RAW or j2 - j1 > SHORT_EDIT_NEW:
            return Issue("symbol_correction",
                         f"{corr.raw[i1:i2]!r} -> {corr.corrected[j1:j2]!r} is more than a symbol fix; "
                         f"only misread symbols may change"), start
        if op == "replace" and not involves_symbol(corr.raw[i1:i2] + corr.corrected[j1:j2]):
            return Issue("symbol_correction",
                         f"{corr.raw[i1:i2]!r} -> {corr.corrected[j1:j2]!r} swaps letters or digits; "
                         f"spelling, wording and numbers must stay as in the text layer"), start
        plain_changed += sum(1 for i, _ in seg if not mask[start + i])
    changed = "".join(corr.raw[i1:i2] + corr.corrected[j1:j2] for op, i1, i2, j1, j2 in ops if op != "equal")
    if not involves_symbol(changed) and not any(mask[start + i] for i in range(len(corr.raw))):
        return Issue("symbol_correction", f"correction of {corr.raw[:60]!r} changes no symbol; spelling, wording "
                                          f"and spacing must stay as in the text layer"), start
    if plain_changed > max(SHORT_EDIT_RAW, (1 - MIN_UNCHANGED) * plain_total):
        return Issue("symbol_correction", f"correction of {corr.raw[:60]!r} changes too much of the text layer"), start
    return None, start


def apply_corrections(text: str, placed: list[tuple[int, SymbolCorrection]]) -> tuple[str, list[Issue]]:
    """Clause text with the accepted corrections applied; overlapping ones are refused."""
    issues, out, last = [], text, None
    for start, corr in sorted(placed, key=lambda p: p[0], reverse=True):
        end = start + len(corr.raw)
        if last is not None and end > last:
            issues.append(Issue("symbol_correction", f"corrections overlap at {corr.raw[:40]!r}"))
            continue
        out = out[:start] + corr.corrected + out[end:]
        last = start
    return out, issues


# ---- clauses -----------------------------------------------------------------------------

@dataclass
class ClauseCheck:
    issues: list[Issue]
    corrected_text: str | None
    accepted: list[SymbolCorrection] = field(default_factory=list)  # corrections that passed


def check_clause(result: ClauseExtraction, clause: ClauseRec, notation_ids: set[str]) -> ClauseCheck:
    issues: list[Issue] = []
    mask = suspect_mask(clause.text, clause.suspect_spans)
    placed = []
    for corr in result.symbol_corrections:
        issue, start = check_correction(corr, clause.text, mask)
        if issue:
            issues.append(issue)
            continue
        placed.append((start, corr))
        if corr.uncertain:
            issues.append(Issue("uncertain_symbol_correction", f"{corr.raw[:60]!r} -> {corr.corrected[:60]!r}: "
                                                               f"{corr.note[:120]}", retry=False))
    corrected, overlap = apply_corrections(clause.text, placed)
    issues += overlap

    source = "\n".join([clause.text, corrected, *(f"{w.symbol} {w.definition} {w.unit or ''}" for w in clause.where),
                        *(e.raw_text for e in clause.equations),
                        *(f"{w.symbol} {w.definition} {w.unit or ''}" for e in clause.equations for w in e.where),
                        *(f.text for f in clause.flagged), clause.title])
    quotes = [(f"parameter {p.id}", p.source_quote) for p in result.parameters]
    quotes += [(f"equation {e.equation_id or e.computes}", e.source_quote) for e in result.equations]
    quotes += [("condition", c.source_quote) for c in result.conditions]
    quotes += [("requirement", r.source_quote) for r in result.requirements]
    quotes += [("exception", x.source_quote) for x in result.exceptions]
    quotes += [(f"unmapped {u.kind}", u.source_quote) for u in result.unmapped]
    issues += check_quotes(quotes, source)

    param_ids = {p.id for p in result.parameters}
    issues += [Issue("bad_id", f"parameter id {p.id!r} is not an ASCII identifier") for p in result.parameters
               if not ID_RE.match(p.id)]
    known = param_ids | notation_ids
    equations = {e.id: e for e in clause.equations}
    for eq in result.equations:
        label = eq.equation_id or eq.computes
        if eq.equation_id and eq.equation_id not in equations:
            issues.append(Issue("equation_structure", f"equation id {eq.equation_id!r} is not one of the inputs"))
        try:
            lhs, rhs = parse_relation(eq.sympy)
        except ValueError as exc:
            issues.append(Issue("sympy_parse", f"equation {label}: {eq.sympy[:120]!r}: {exc}"))
            continue
        names = {s.name for s in lhs.free_symbols | rhs.free_symbols}
        if undefined := sorted(names - known):
            issues.append(Issue("undefined_symbol", f"equation {label}: {', '.join(undefined)} not defined in the "
                                                    f"clause's parameters or the notation dictionary"))
        if eq.computes not in {s.name for s in lhs.free_symbols}:
            issues.append(Issue("equation_structure", f"equation {label}: computes {eq.computes!r} is not on the "
                                                      f"left-hand side"))
        if eq.equation_id in equations:
            raw = equations[eq.equation_id].raw_text
            if sum(c.isascii() and c.isalpha() for c in raw) >= 3 and letters_match(eq.latex, raw) < LETTER_MATCH:
                issues.append(Issue("equation_letter_mismatch",
                                    f"equation {label}: the LaTeX letters do not match the text layer {raw[:80]!r}"))

    known_refs = {norm_text(r.text) for r in clause.references}
    issues += [Issue("reference_unknown", f"reference {r.text[:80]!r} is not in the references list", retry=False)
               for r in result.references if norm_text(r.text) not in known_refs]
    issues += [Issue("unmapped", f"{u.kind}: {u.text[:120]}", retry=False) for u in result.unmapped]
    return ClauseCheck(issues, corrected if placed else None, [c for _, c in placed])


# ---- tables ----------------------------------------------------------------------------

def is_blank(cell: str) -> bool:
    return not any(c.isalnum() for c in cell or "")


def mostly_words(text: str) -> bool:
    """At least three letters and more letters than digits ('Connections13', not '9θ y')."""
    letters = sum(c.isalpha() for c in text)
    return letters >= 3 and letters > sum(c.isdigit() for c in text)


def column_headers(rows: list[RowRec]) -> list[str]:
    """Header text of each column: the header rows' cells joined."""
    width = max((len(r.cells) for r in rows), default=0)
    return [" ".join(r.cells[c] for r in rows if r.is_header and c < len(r.cells)).strip() for c in range(width)]


def is_label(cell: str, header: str) -> bool:
    """A cell that names its row: mostly words, in a column whose header is not a number.
    Digits in a label are footnote markers or part of a name, not values."""
    numeric_header = bool(header) and not mostly_words(header) and bool(numbers(header))
    return mostly_words(cell) and not numeric_header


def has_values(row: RowRec, headers: list[str]) -> bool:
    """True if a cell other than a row label holds a number: the row must be answered."""
    return any(numbers(cell) and not is_label(cell, headers[c] if c < len(headers) else "")
               for c, cell in enumerate(row.cells))


def band_numbers(table: TableRec, row: RowRec) -> list[float]:
    """Numbers printed in the table's text within the row's vertical band on its page."""
    y0, y1 = row.bbox[1], row.bbox[3]
    tol = max(2.0, 0.5 * (y1 - y0))
    out = []
    for other in table.rows:
        if other.page == row.page and other.bbox[1] <= y1 + tol and other.bbox[3] >= y0 - tol:
            out += numbers(" ".join(other.cells))
    return out


def number_issue(value, row: RowRec, table: TableRec) -> Issue | None:
    if value.number is None:
        return None
    if row.grid_method in GRID_METHODS:
        cell = row.cells[value.col]
        if not any(abs(n - value.number) < 1e-9 for n in numbers(cell)):
            return Issue("table_number_mismatch", f"row {row.index}, col {value.col}: {value.number:g} is not in "
                                                  f"the cell {cell[:60]!r}")
        return None
    if not any(abs(n - value.number) < 1e-9 for n in band_numbers(table, row)):
        return Issue("table_number_mismatch", f"row {row.index}, col {value.col}: {value.number:g} is not printed in "
                                              f"the table's text on that row")
    return None


def check_table(result: TableExtraction, table: TableRec, rows: list[RowRec]) -> list[Issue]:
    issues: list[Issue] = []
    by_index = {r.index: r for r in rows}
    width = max((len(r.cells) for r in rows), default=0)
    seen = set()
    for out in result.rows:
        row = by_index.get(out.row)
        if row is None or row.is_header:
            issues.append(Issue("grid_dimension", f"row {out.row} is not a body row of this grid"))
            continue
        seen.add(out.row)
        for value in out.values:
            if not 0 <= value.col < len(row.cells):
                issues.append(Issue("grid_dimension", f"row {out.row} has no column {value.col}"))
                continue
            if issue := number_issue(value, row, table):
                issues.append(issue)
            if value.formula:
                try:
                    parse_side(value.formula) if SAFE_EXPR.match(value.formula) and "__" not in value.formula \
                        else (_ for _ in ()).throw(ValueError("unsupported characters"))
                except Exception as exc:
                    issues.append(Issue("sympy_parse", f"row {out.row}, col {value.col}: formula "
                                                       f"{value.formula[:80]!r}: {exc}"))
    headers = column_headers(rows)
    missing = [r.index for r in rows if not r.is_header and r.index not in seen and has_values(r, headers)]
    if missing:
        issues.append(Issue("grid_dimension", f"rows with numbers are missing from the answer: {missing[:20]}"))
    for hp in result.header_parameters:
        if not 0 <= hp.col < width:
            issues.append(Issue("grid_dimension", f"header parameter {hp.parameter_id}: no column {hp.col}"))
        if not ID_RE.match(hp.parameter_id):
            issues.append(Issue("bad_id", f"parameter id {hp.parameter_id!r} is not an ASCII identifier"))
    markers = {m for m, _ in table.footnotes} | {m for r in table.rows for m in r.markers}
    all_rows = {r.index for r in table.rows}
    for rule in result.footnote_rules:
        if rule.marker not in markers:
            known = ", ".join(sorted(markers)) or "none"
            issues.append(Issue("footnote_unknown", f"footnote marker {rule.marker!r} is not a marker of this table "
                                                    f"(markers: {known})"))
        if bad := [i for i in rule.applies_to_rows if i not in all_rows]:
            issues.append(Issue("grid_dimension", f"footnote {rule.marker!r} applies to unknown rows {bad[:10]}"))
    for blank in result.blank_cells:
        row = by_index.get(blank.row)
        if row is None or not 0 <= blank.col < len(row.cells):
            issues.append(Issue("grid_dimension", f"blank cell ({blank.row}, {blank.col}) is not in the grid"))
        elif not is_blank(row.cells[blank.col]):
            issues.append(Issue("grid_dimension", f"cell ({blank.row}, {blank.col}) is not blank: "
                                                  f"{row.cells[blank.col][:40]!r}"))
        else:
            issues.append(Issue("blank_cell", f"({blank.row}, {blank.col}): {blank.interpretation[:120]}", retry=False))
    return issues


# ---- notation and definitions pages ------------------------------------------------------

def check_notation(result: NotationPage, page_text: str) -> list[Issue]:
    issues = check_quotes([(f"symbol {e.display}", e.source_quote) for e in result.entries], page_text)
    issues += [Issue("bad_id", f"notation id {e.id!r} is not an ASCII identifier") for e in result.entries
               if not ID_RE.match(e.id)]
    counts = Counter(e.id for e in result.entries)
    issues += [Issue("bad_id", f"notation id {i!r} is used for {n} entries; give each meaning its own id")
               for i, n in counts.items() if n > 1]
    return issues


def check_definitions(result: DefinitionsPage, page_text: str) -> list[Issue]:
    return check_quotes([(f"term {t.term}", t.source_quote) for t in result.terms], page_text)
