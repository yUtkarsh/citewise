"""Pass 2 validation (SPEC Step 3b): document-independent checks on hand-made results.
Never calls the API."""
import pymupdf
import pytest

from app.ingest.pass2 import validate as v
from app.ingest.pass2.records import ClauseRec, EquationRec, ReferenceRec, RowRec, TableRec
from app.ingest.pass2.schemas import (BlankCell, CellValue, ClauseExtraction, EquationOut, FootnoteRule,
                                      HeaderParameter, NotationEntry, NotationPage, ParameterOut, RowOut,
                                      SymbolCorrection, TableExtraction)


def clause(text, suspect=(), equations=(), references=()):
    return ClauseRec(id="1.2", number="1.2", title="Strength", kind="provision", stream="p", depth=2, page=1,
                     page_end=1, parent_id=None, text=text, regions=[(1, (0, 0, 100, 100))],
                     suspect_spans=list(suspect), equations=list(equations), references=list(references))


def extraction(**kw):
    base = dict(parameters=[], equations=[], conditions=[], requirements=[], exceptions=[], references=[],
                symbol_corrections=[], unmapped=[])
    return ClauseExtraction(**(base | kw))


def corr(raw, corrected, uncertain=False):
    return SymbolCorrection(raw=raw, corrected=corrected, uncertain=uncertain, note="")


def codes(issues):
    return sorted(i.code for i in issues)


# ---- source quotes -----------------------------------------------------------------------

def test_quotes_must_come_from_the_source_ignoring_spacing_ligatures_and_case():
    c = clause("The design shear  force shall not exceed the eﬀective capacity.")
    ok = extraction(parameters=[ParameterOut(id="V_d", symbol="Vd", latex="V_d", description="shear", units=None,
                                             source_quote="design shear force shall NOT exceed the effective")])
    bad = extraction(parameters=[ParameterOut(id="V_d", symbol="Vd", latex="V_d", description="shear", units=None,
                                              source_quote="shear force must be doubled")])
    assert not v.check_clause(ok, c, set()).issues
    assert codes(v.check_clause(bad, c, set()).issues) == ["source_quote"]


def test_quote_with_ellipsis_needs_every_piece():
    src = v.norm_text("alpha beta gamma delta")
    assert v.quote_found("alpha ... delta", src)
    assert not v.quote_found("alpha ... omega", src)


# ---- equations ---------------------------------------------------------------------------

EQ = EquationRec(id="Equation 3-1", number="3-1", page=1, bbox=(0, 0, 1, 1), raw_text="V d = 0.5 b w d f c",
                 crop=None, clause_id="1.2")


def equation(sympy, computes="V_d", uses=("b_w", "d", "f_c"), latex=r"V_d = 0.5\, b_w d \sqrt{f_c}"):
    return EquationOut(equation_id="Equation 3-1", latex=latex, sympy=sympy, computes=computes, uses=list(uses),
                       source_quote="V d = 0.5")


def params(*ids):
    return [ParameterOut(id=i, symbol=i, latex=i, description="", units=None, source_quote="V d") for i in ids]


def test_equation_parses_and_symbols_are_defined_locally_or_in_notation():
    c = clause("V d = 0.5 b w d f c", equations=[EQ])
    r = extraction(parameters=params("V_d", "b_w"), equations=[equation("V_d = 0.5*b_w*d*sqrt(f_c)")])
    assert not v.check_clause(r, c, notation_ids={"d", "f_c"}).issues


def test_summation_index_needs_no_parameter():
    c = clause("V d = sum of w i h i", equations=[EQ])
    r = extraction(parameters=params("V_d", "w_i", "h_i", "n"),
                   equations=[equation("V_d = Sum(w_i*h_i, (i, 1, n))", uses=("w_i", "h_i", "n"),
                                       latex=r"V_d = \sum_{i=1}^{n} w_i h_i")])
    assert "undefined_symbol" not in codes(v.check_clause(r, c, set()).issues)
    r_bad = extraction(parameters=params("V_d", "w_i", "h_i"),
                       equations=[equation("V_d = Sum(w_i*h_i, (i, 1, n))", uses=("w_i", "h_i"))])
    assert "undefined_symbol" in codes(v.check_clause(r_bad, c, set()).issues)  # the bound n is still free


def test_unparseable_equation_is_a_sympy_failure():
    c = clause("V d = 0.5 b w d f c", equations=[EQ])
    r = extraction(parameters=params("V_d", "b_w", "d", "f_c"), equations=[equation("V_d = 0.5 b_w (d")])
    assert codes(v.check_clause(r, c, set()).issues) == ["sympy_parse"]


def test_code_injection_is_refused_by_the_parser():
    with pytest.raises(ValueError):
        v.parse_relation("x = __import__('os').system('echo hi')")


def test_undefined_symbol_is_reported():
    c = clause("V d = 0.5 b w d f c", equations=[EQ])
    r = extraction(parameters=params("V_d", "b_w"), equations=[equation("V_d = 0.5*b_w*d*sqrt(f_c)")])
    issues = v.check_clause(r, c, notation_ids={"d"}).issues
    assert codes(issues) == ["undefined_symbol"] and "f_c" in issues[0].detail


def test_latex_letters_must_roughly_match_the_text_layer():
    c = clause("V d = 0.5 b w d f c", equations=[EQ])
    r = extraction(parameters=params("V_d", "b_w", "d", "f_c"),
                   equations=[equation("V_d = 0.5*b_w*d*sqrt(f_c)", latex=r"M_{yz} = q_k \cdot h_g")])
    assert "equation_letter_mismatch" in codes(v.check_clause(r, c, set()).issues)


# ---- symbol corrections ------------------------------------------------------------------

def test_character_level_corrections_are_accepted_anywhere():
    c = clause("use p of fc0 for the strength")
    r = extraction(symbol_corrections=[corr("p of", "√ of"), corr("fc0", "f′c")])
    check = v.check_clause(r, c, set())
    assert not check.issues
    assert check.corrected_text == "use √ of f′c for the strength"


def test_rewording_is_rejected():
    c = clause("the strength shall be reduced")
    r = extraction(symbol_corrections=[corr("shall be reduced", "must be lowered")])
    assert codes(v.check_clause(r, c, set()).issues) == ["symbol_correction"]


@pytest.mark.parametrize("raw, corrected", [
    ("the psuedo lateral load", "the pseudo lateral load"),  # spelling
    ("displacement modifier η", "displacement multiplier η"),  # wording, as small letter edits
    ("J =Coefficient", "J = Coefficient"),  # spacing
    ("strength of 3000 psi", "strength of 3600 psi"),  # a digit for a digit
])
def test_letter_digit_and_spacing_edits_are_not_symbol_fixes(raw, corrected):
    c = clause(f"see {raw} here")
    assert codes(v.check_clause(extraction(symbol_corrections=[corr(raw, corrected)]), c, set()).issues) ==         ["symbol_correction"]


@pytest.mark.parametrize("raw, corrected", [
    ("1 + 5 (θ -0.1)/", "1 + 5 (θ − 0.1)/"),  # minus sign
    ("a = 0.5", "α = 0.5"),  # Greek letter read as Latin
])
def test_symbol_edits_are_accepted(raw, corrected):
    c = clause(f"see {raw} here")
    check = v.check_clause(extraction(symbol_corrections=[corr(raw, corrected)]), c, set())
    assert not check.issues and check.accepted


def test_many_small_edits_that_change_most_of_a_span_are_rejected():
    c = clause("abcdefghij")
    r = extraction(symbol_corrections=[corr("abcdefghij", "aXcXeXgXiX")])
    assert codes(v.check_clause(r, c, set()).issues) == ["symbol_correction"]


def test_suspect_characters_may_be_replaced_in_runs_of_any_length():
    radical = "p" + "ﬃ" * 7
    c = clause(f"shall not exceed 6 {radical} fc for the member", suspect=["p", "ﬃ" * 7])
    r = extraction(symbol_corrections=[corr(f"6 {radical} fc", "6 √fc")])
    check = v.check_clause(r, c, set())
    assert not check.issues
    assert "6 √fc for" in check.corrected_text


def test_the_same_long_run_outside_suspect_text_is_rejected():
    c = clause("shall not exceed 6 pqrstuvw fc for the member")
    r = extraction(symbol_corrections=[corr("6 pqrstuvw fc", "6 √fc")])
    assert codes(v.check_clause(r, c, set()).issues) == ["symbol_correction"]


def test_raw_text_must_be_in_the_clause():
    c = clause("plain text")
    r = extraction(symbol_corrections=[corr("not here", "not hère")])
    assert codes(v.check_clause(r, c, set()).issues) == ["symbol_correction"]


def test_uncertain_correction_goes_to_review_without_retry():
    c = clause("use fc0 here")
    issues = v.check_clause(extraction(symbol_corrections=[corr("fc0", "f′c", uncertain=True)]), c, set()).issues
    assert codes(issues) == ["uncertain_symbol_correction"]
    assert not v.needs_retry(issues) and issues[0].severity == "medium"


def test_asce_radical_pattern_from_the_private_page(private_fixture):
    """The licensed ASCE page draws a radical as 'p' plus a run of ffi ligatures in an
    unmapped font; replacing that whole run with '√' is a symbol fix."""
    from app.ingest.pass1.skeleton import build_skeleton
    from app.ingest.pass2.records import ClauseRec as Rec
    from app.profile.build import file_hash, load_profile, profile_path

    pdf = private_fixture("asce41_sample.pdf")
    path = profile_path(file_hash(pdf))
    if not path.is_file():
        pytest.skip("no saved profile for the ASCE excerpt")
    profile = load_profile(path)
    skel = build_skeleton(pymupdf.open(pdf), profile)
    hits = [c for c in skel.seg.clauses if any("ﬃﬃ" in s for s in c.suspect_spans)]
    if not hits:
        pytest.skip("no radical drawn as ligatures in this excerpt's clause text")
    c = hits[0]
    start = c.text.index("ﬃﬃ") - 1
    end = start + 1
    while end < len(c.text) and c.text[end] == "ﬃ":
        end += 1
    raw = c.text[max(0, start - 2):end + 3]
    corrected = raw.replace(c.text[start:end], "√")
    rec = Rec(id=c.id, number=c.number, title=c.title, kind=c.kind, stream=c.stream, depth=c.depth, page=c.page,
              page_end=c.page_end, parent_id=c.parent_id, text=c.text, regions=c.regions,
              suspect_spans=c.suspect_spans)
    check = v.check_clause(extraction(symbol_corrections=[corr(raw, corrected)]), rec, set())
    assert not check.issues, check.issues
    assert "√" in check.corrected_text


# ---- references and unmapped ----------------------------------------------------------------

def test_unknown_reference_and_unmapped_items_flag_without_retry():
    from app.ingest.pass2.schemas import ReferenceOut, UnmappedItem
    c = clause("See Section 2.4 for details.", references=[ReferenceRec("Section 2.4", "2.4", "clause")])
    r = extraction(references=[ReferenceOut(text="Section 2.4", type="see_also"),
                               ReferenceOut(text="Section 9.9", type="requires")],
                   unmapped=[UnmappedItem(kind="note", text="x", source_quote="for details")])
    issues = v.check_clause(r, c, set()).issues
    assert codes(issues) == ["reference_unknown", "unmapped"] and not v.needs_retry(issues)


# ---- tables ---------------------------------------------------------------------------------

def table(method, rows):
    recs = [RowRec(index=i, page=1, bbox=(0, 100 + 12 * i, 300, 110 + 12 * i), cells=cells, is_header=i == 0,
                   grid_method=method) for i, cells in enumerate(rows)]
    return TableRec(id="Table 1", number="1", caption="Values", pages=[1], crops=[], header_crop=None, rows=recs,
                    footnotes=[("a", "Interpolate.")], clause_id=None)


GRID = [["Type", "m-factor", "Limit"], ["Flexible", "1.5", "2,000"], ["Stiff", "", "-0.25"]]


def result(rows, blanks=(), rules=(), headers=()):
    return TableExtraction(header_parameters=list(headers), rows=rows, footnote_rules=list(rules),
                           blank_cells=list(blanks))


def value(col, number, text=None):
    return CellValue(col=col, value=text or str(number), number=number, unit=None, formula=None)


@pytest.mark.parametrize("method", ["docling", "find_tables"])
def test_grid_numbers_match_cells_exactly(method):
    t = table(method, GRID)
    ok = result([RowOut(row=1, conditions=["flexible"], values=[value(1, 1.5), value(2, 2000)]),
                 RowOut(row=2, conditions=["stiff"], values=[value(2, -0.25)])])
    assert not v.check_table(ok, t, t.rows)
    wrong_cell = result([RowOut(row=1, conditions=[], values=[value(1, 2000)]),
                         RowOut(row=2, conditions=[], values=[value(2, -0.25)])])
    assert codes(v.check_table(wrong_cell, t, t.rows)) == ["table_number_mismatch"]


def test_text_line_grid_numbers_need_only_appear_in_the_row_band():
    t = table("text_lines", [["Type m-factor"], ["Flexible 1.5", "2,000"], ["Stiff -0.25"]])
    # 2000 is reported in the wrong column, but it is printed on that row: accepted
    ok = result([RowOut(row=1, conditions=[], values=[value(0, 2000), value(0, 1.5)]),
                 RowOut(row=2, conditions=[], values=[value(0, -0.25)])])
    assert not v.check_table(ok, t, t.rows)
    other_row = result([RowOut(row=1, conditions=[], values=[value(0, 1.5)]),
                        RowOut(row=2, conditions=[], values=[value(0, 7.5)])])
    assert codes(v.check_table(other_row, t, t.rows)) == ["table_number_mismatch"]


def test_header_cells_are_exempt_and_header_rows_are_not_body_rows():
    t = table("docling", GRID)
    r = result([RowOut(row=1, conditions=[], values=[value(1, 1.5), value(2, 2000)]),
                RowOut(row=2, conditions=[], values=[value(2, -0.25)])],
               headers=[HeaderParameter(col=1, parameter_id="m_factor", header_text="m-factor 99", units=None)])
    assert not v.check_table(r, t, t.rows)
    as_body = result([RowOut(row=0, conditions=[], values=[]), *r.rows])
    assert codes(v.check_table(as_body, t, t.rows)) == ["grid_dimension"]


def test_heading_rows_with_footnote_digits_need_no_values():
    t = table("docling", [["Type", "m-factor", "Limit"], ["ii. Beams controlled by shear1"],
                          ["Columns—flexure 2, 7"], ["Flexible", "1.5", "2,000"]])
    r = result([RowOut(row=3, conditions=[], values=[value(1, 1.5), value(2, 2000)])])
    assert not v.check_table(r, t, t.rows)  # rows 1 and 2 are labels with marker digits, not values
    missing = result([RowOut(row=1, conditions=[], values=[])])
    assert "grid_dimension" in codes(v.check_table(missing, t, t.rows))  # row 3 has values


def test_wordy_cell_under_a_numeric_header_is_a_value():
    t = table("docling", [["Ratio", "0.2", "0.5"], ["Flexible", "1.5 typ", "about 2 kips"]])
    assert v.has_values(t.rows[1], v.column_headers(t.rows))
    r = result([])
    assert "grid_dimension" in codes(v.check_table(r, t, t.rows))
    assert v.is_label("Fully Restrained Moment Connections13", "Component")
    assert not v.is_label("9θ y", "LS") and not v.is_label("about 2 kips", "0.5")


def test_missing_rows_and_columns_are_grid_errors():
    t = table("docling", GRID)
    r = result([RowOut(row=1, conditions=[], values=[value(5, 1.5)])])
    assert codes(v.check_table(r, t, t.rows)) == ["grid_dimension", "grid_dimension"]


def test_blank_cells_are_flagged_and_must_be_blank():
    t = table("docling", GRID)
    rows = [RowOut(row=1, conditions=[], values=[value(1, 1.5), value(2, 2000)]),
            RowOut(row=2, conditions=[], values=[value(2, -0.25)])]
    blank = BlankCell(row=2, col=1, interpretation="not permitted", applies_to_rows=[2])
    issues = v.check_table(result(rows, blanks=[blank]), t, t.rows)
    assert codes(issues) == ["blank_cell"] and issues[0].severity == "high" and not v.needs_retry(issues)
    not_blank = BlankCell(row=1, col=1, interpretation="?", applies_to_rows=[])
    assert codes(v.check_table(result(rows, blanks=[not_blank]), t, t.rows)) == ["grid_dimension"]


def test_footnote_markers_must_be_markers_pass1_found_or_retry():
    t = table("docling", GRID)
    t.rows[1].markers = ["b"]  # a marker Pass 1 saw inside a row counts too
    rows = [RowOut(row=1, conditions=[], values=[value(1, 1.5)]), RowOut(row=2, conditions=[], values=[])]
    rule = FootnoteRule(marker="line c", kind="note", rule="x", applies_to_rows=[1], applies_to_cols=[])
    issues = v.check_table(result(rows, rules=[rule]), t, t.rows)
    assert codes(issues) == ["footnote_unknown"] and v.needs_retry(issues)
    for marker in ("a", "b"):
        ok = FootnoteRule(marker=marker, kind="note", rule="x", applies_to_rows=[1], applies_to_cols=[])
        assert not v.check_table(result(rows, rules=[ok]), t, t.rows)


# ---- notation pages ---------------------------------------------------------------------------

def test_notation_ids_are_identifiers_and_unique():
    entry = dict(display="f′c", latex="f'_c", description="strength", units="psi", defined_in=[], aliases=[],
                 scope_chapters=[], source_quote="compressive strength")
    page = NotationPage(entries=[NotationEntry(id="f_prime_c", **entry), NotationEntry(id="f prime", **entry),
                                 NotationEntry(id="f_prime_c", **entry)])
    issues = v.check_notation(page, "f c ' = compressive strength of concrete")
    assert codes(issues) == ["bad_id", "bad_id"]
