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
    "bad_id": "low", "refused": "high", "equation_mismatch": "high", "equation_latex": "low",  # equation_latex: the checker could not verify
    "image_read": "low", "image_read_disagrees": "high", "equation_missing": "medium",
    "value_column": "high", "row_missing": "high", "value_missing": "high", "garbled_text": "high",
    "footnote_in_value": "high",
}
GRID_METHODS = {"docling", "find_tables"}  # grids whose cells are trusted for exact matching
SHORT_EDIT_RAW, SHORT_EDIT_NEW = 2, 3  # character-level edit outside suspect text
MIN_UNCHANGED = 0.8
LETTER_MATCH = 0.6
EQUATION_POINTS = 3
EQUATION_RTOL = 1e-6
LOWER_LIMIT, UPPER_LIMIT = 2, 4  # integer values for symbols used as sum limits
PRIMES = "'′’"
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
    """Parse one side; names that are Python keywords (a parameter called 'lambda') are
    renamed for the parser and restored afterwards."""
    import keyword

    from sympy import Symbol
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations

    renamed = {}

    def hide(m):
        renamed[m.group(0) + "_kw_"] = m.group(0)
        return m.group(0) + "_kw_"
    safe = re.sub(r"\b(" + "|".join(keyword.kwlist) + r")\b", hide, text)
    expr = parse_expr(safe, local_dict={}, global_dict=_sympy_globals(), transformations=standard_transformations)
    return expr.xreplace({Symbol(k): Symbol(v) for k, v in renamed.items()})


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


# ---- LaTeX against SymPy ---------------------------------------------------------------

def norm_primes(text: str) -> str:
    """One prime character: ASCII apostrophe and right single quote become the prime."""
    return re.sub("['\u2019]", "\u2032", text)


def symbol_key(name: str) -> str:
    """Spelling-independent key for a symbol: 'C_{vx}', 'C_vx' and 'Cvx' agree (case
    counts: T and t differ); primes (f'_{cE}, f′cE, id f_prime_cE) become 'prime'."""
    name = re.sub(f"[{PRIMES}]|\\\\prime", "prime", name)
    name = re.sub(r"\\(mathrm|mathit|text|operatorname)", "", name)
    return re.sub(r"[^A-Za-z0-9]", "", name)


DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
UPPER_GREEK = ["Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega"]


def _upper_greek(m) -> str:
    return "Z_{" + m.group(1) + (m.group(2) or m.group(3) or "") + "}"


def latex_for_parsing(latex: str) -> str:
    r"""LaTeX the parser accepts. Parameter LaTeX goes through the same rewrite before
    matching, so names still agree:
    - primes move into the subscript (f'_{cE} -> f_{primecE});
    - digits in longer subscripts are spelled out (V_{C318} -> V_{Cthreeoneeight});
    - upper-case Greek, which the parser lacks, becomes a placeholder (\Delta_w -> Z_{Deltaw});
    - \pi becomes its value, \pm becomes +, square brackets become parentheses;
    - \left/\right and spacing commands are dropped."""
    text = re.sub(r"\\(left|right)\s*\\?([\[\]{}])", lambda m: "(" if m.group(2) in "[{" else ")", latex)
    text = re.sub(r"\\(left|right|,|;|!|quad|qquad)", " ", text)
    text = text.replace("[", "(").replace("]", ")").replace(r"\lvert", "|").replace(r"\rvert", "|")
    text = re.sub(r"\\pm(?![A-Za-z])", "+", text)
    # π as a value, multiplied explicitly after a number or name ("4\pi^2" -> "4 \cdot (3.14…)^2")
    text = re.sub(r"(?<=[0-9A-Za-z)}])\s*\\pi(?![A-Za-z])", r" \\cdot (3.141592653589793)", text)
    text = re.sub(r"\\pi(?![A-Za-z])", "(3.141592653589793)", text)
    text = re.sub(r"\\(" + "|".join(UPPER_GREEK) + r")(?![A-Za-z])(?:_\{([^{}]*)\}|_([A-Za-z0-9]))?",
                  _upper_greek, text)
    text = re.sub(r"\\(mathrm|mathit|text)\{([^}]*)\}", r"\2", text)

    def prime(m):
        sub = m.group(2) or m.group(3) or ""
        return f"{m.group(1)}_{{prime{sub}}}"
    pattern = r"([A-Za-z])(?:[" + PRIMES + r"]|\^\{?\\prime\}?)(?:_\{([^}]*)\}|_([A-Za-z0-9]))?"
    text = re.sub(pattern, prime, text)

    def spell(m):
        sub = m.group(1)
        if len(sub) > 1 and sub.isalnum() and any(c.isdigit() for c in sub):
            sub = "".join(DIGIT_WORDS[int(c)] if c.isdigit() else c for c in sub)
        return "_{" + sub + "}"
    text = re.sub(r"_\{([^{}]*)\}", spell, text)
    # products the parser does not see: after a bare d (it reads "d x" as a differential),
    # and after a closing parenthesis or absolute value ("(a-1)\frac{b}{c}", "|a|(R-1)")
    text = re.sub(r"\|([^|]*)\|", r"(|\1|)", text)
    text = _product_after_roots(text)
    not_operator = r"(?!\\(?:le|leq|ge|geq|lt|gt|cdot|times|right|pm|ne|neq)(?![A-Za-z]))"
    text = re.sub(r"(\)(?:\^\{[^{}]*\}|\^[A-Za-z0-9])?)\s*" + not_operator + r"(?=[A-Za-z0-9\\(])",
                  r"\1 \\cdot ", text)
    return re.sub(r"(?<![A-Za-z\\_{^])d(?![A-Za-z_^{])\s*(?=[A-Za-z0-9\\({])", r"d \\cdot ", text)


def _product_after_roots(text: str) -> str:
    """'\\sqrt{f}A' -> '\\sqrt{f} \\cdot A': the parser needs the product after a root."""
    out, i = [], 0
    for m in re.finditer(r"\\sqrt(?:\[[^\]]*\])?\{", text):
        if m.start() < i:
            continue
        depth, j = 1, m.end()
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        out.append(text[i:j])
        rest = text[j:].lstrip()
        if rest and (rest[0].isalnum() or rest[0] == "(") or (rest[:1] == "\\" and not re.match(
                r"\\(le|leq|ge|geq|cdot|times|right|pm|ne|neq)(?![A-Za-z])", rest)):
            out.append(r" \cdot ")
        i = j
    out.append(text[i:])
    return "".join(out)


RELATION_TOKEN = re.compile(r"\\(?:leq|le|geq|ge|lt|gt|neq|ne)(?![A-Za-z])|<=|>=|=|<|>")


def relation_texts(latex: str) -> list[str]:
    """The single relations a LaTeX chain states, as the SymPy form may write them:
    - 'A = expr' and 'A \\le expr' stay as they are;
    - 'A = expr \\le 3.0' keeps its equation 'A = expr' (the limit is a separate statement);
    - an equality chain 'Q = V = expr' gives 'Q = expr' and 'V = expr': its last expression,
      with any name of the chain on the left.
    Relations inside braces (\\sum_{i=1}) do not count."""
    def depth_at(i: int) -> int:
        return latex[:i].count("{") - latex[:i].count("}")
    tokens = [t for t in RELATION_TOKEN.finditer(latex) if depth_at(t.start()) == 0]
    if len(tokens) < 2:
        return [latex]
    cuts = [0, *[p for t in tokens for p in (t.start(), t.end())], len(latex)]
    segments = [latex[cuts[i]:cuts[i + 1]].strip() for i in range(0, len(cuts), 2)]
    ops = [t.group(0) for t in tokens]
    equals = next((i for i, op in enumerate(ops) if op != "="), len(ops))  # leading '=' operators
    if equals == 0:
        return [f"{segments[0]} {ops[0]} {segments[1]}"]
    last = segments[equals]
    return [f"{segments[i]} = {last}" for i in range(equals)]


def parse_latex_relation(latex: str) -> list[tuple]:
    """(lhs, rhs) readings of the LaTeX: one per relation a chain states, and several when
    the lark parser finds the LaTeX ambiguous (usually equal). Raises ValueError."""
    import warnings

    from sympy.core.relational import Relational
    from sympy.parsing.latex import parse_latex

    relations, errors = [], []
    for text in relation_texts(latex_for_parsing(latex)):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # SymPy deprecation notices from the lark transformer
                expr = parse_latex(text, backend="lark")
        except Exception as exc:  # the parser raises many exception types
            errors.append(str(exc).splitlines()[0][:120])
            continue
        readings = getattr(expr, "children", None) if getattr(expr, "data", None) == "_ambig" else [expr]
        relations += [r for r in readings or [] if isinstance(r, Relational)]
    if not relations:
        raise ValueError(f"the LaTeX does not parse: {errors[0]}" if errors else
                         "the LaTeX is not an equation or inequality")
    return list(dict.fromkeys((r.lhs, r.rhs) for r in relations))



def _index_names(expr) -> set[str]:
    from sympy import Sum
    return {lim[0].name for s in expr.atoms(Sum) for lim in s.limits}


def _limit_values(*exprs) -> dict[str, int]:
    """Symbols used as sum limits get small integers: lower limits 2, upper limits 4."""
    from sympy import Sum
    values: dict[str, int] = {}
    for expr in exprs:
        for s in expr.atoms(Sum):
            for _, lo, hi in s.limits:
                for sym in lo.free_symbols:
                    values.setdefault(sym.name, LOWER_LIMIT)
                for sym in hi.free_symbols:
                    values[sym.name] = UPPER_LIMIT if values.get(sym.name) != LOWER_LIMIT else 3
    return values


def _indexed_by(name: str, index: str) -> bool:
    return index in [p for p in re.split(r"[_{}]+", name) if p]


def evaluate(expr, values: dict[str, float], index: dict[str, int] | None = None) -> float:
    """Numeric value with sums expanded; a symbol subscripted by a sum index varies with it."""
    from sympy import Float, Sum

    index = index or {}
    if isinstance(expr, Sum):
        var, lo, hi = expr.limits[0]
        body = expr.function if len(expr.limits) == 1 else Sum(expr.function, *expr.limits[1:])
        start, stop = int(round(evaluate(lo, values, index))), int(round(evaluate(hi, values, index)))
        return sum(evaluate(body, values, {**index, var.name: k}) for k in range(start, stop + 1))
    if expr.is_Symbol:
        if expr.name in index:
            return float(index[expr.name])
        value = values[expr.name]
        for name, k in index.items():
            if _indexed_by(expr.name, name):
                value *= 1 + 0.37 * k
        return value
    if expr.is_Number:
        return float(expr)
    if not expr.args:
        return float(expr.evalf())
    args = [Float(evaluate(a, values, index)) for a in expr.args]
    return float(expr.func(*args).evalf())


def _rename(expr, key_to_id: dict[str, str], keep: set[str]):
    """Symbols renamed to parameter ids by key; returns (expr, unmapped names)."""
    from sympy import Symbol
    mapping, unmapped = {}, []
    for sym in expr.free_symbols:
        if sym.name in keep:
            continue
        pid = key_to_id.get(symbol_key(sym.name))
        if pid is None:
            unmapped.append(sym.name)
        else:
            mapping[sym] = Symbol(pid)
    return expr.xreplace(mapping), unmapped


def check_equation_latex(eq, symbols: dict[str, str], seed: str) -> Issue | None:
    """The LaTeX-vs-SymPy check. Disagreement ('equation_mismatch': values or left side
    differ) is retried, then flagged high. When the checker cannot verify the equation
    (LaTeX it cannot parse, symbols it cannot map, no point it can evaluate) the result is
    a low-severity note without retry: a limit of the checker, not an error in the answer."""
    issue = _check_equation_latex(eq, symbols, seed)
    if issue is not None and issue.code == "equation_latex":
        issue.retry = False
    return issue


def _check_equation_latex(eq, symbols: dict[str, str], seed: str) -> Issue | None:
    """Parse the LaTeX, map its symbols to parameter ids, and compare it with the SymPy
    form at 3 positive points (sum limits as small integers). ``symbols``: id -> LaTeX
    for the clause's parameters and the notation entries. Where the parser reads the
    LaTeX more than one way, any reading that agrees passes."""
    label = eq.equation_id or eq.computes
    try:
        readings = parse_latex_relation(eq.latex)
        slhs, srhs = parse_relation(eq.sympy)
    except ValueError as exc:
        return Issue("equation_latex", f"equation {label}: {exc}; write standard LaTeX (sums with both limits)")
    key_to_id: dict[str, str] = {}
    for pid, latex in symbols.items():  # in order: the first parameter to claim a key keeps it
        key_to_id.setdefault(symbol_key(pid), pid)
        key_to_id.setdefault(symbol_key(latex_for_parsing(latex or "")), pid)
    first = None
    for llhs, lrhs in readings:
        issue = _compare_reading(llhs, lrhs, slhs, srhs, key_to_id, label, seed)
        if issue is None:
            return None
        first = first or issue
    return first


def _compare_reading(llhs, lrhs, slhs, srhs, key_to_id, label, seed) -> Issue | None:
    import random

    keep = _index_names(lrhs) | _index_names(srhs)
    renamed = [_rename(e, key_to_id, keep) for e in (llhs, lrhs)]
    (llhs, _), (lrhs, _) = renamed
    unmapped = sorted({n for _, u in renamed for n in u})
    if unmapped:
        return Issue("equation_latex", f"equation {label}: LaTeX symbols {', '.join(unmapped)} match no parameter; "
                                       f"give each symbol a parameter whose latex is written the same way")
    if {s.name for s in llhs.free_symbols} != {s.name for s in slhs.free_symbols}:
        return Issue("equation_mismatch", f"equation {label}: the LaTeX left side {llhs} is not the SymPy left side {slhs}")
    names = {s.name for e in (lrhs, srhs, llhs, slhs) for s in e.free_symbols} - keep
    limits = _limit_values(lrhs, srhs)
    rng = random.Random(seed)
    compared = 0
    for _ in range(EQUATION_POINTS):
        values = {n: float(limits[n]) if n in limits else rng.uniform(0.5, 3.0) for n in sorted(names)}
        try:
            a, b = evaluate(lrhs, values), evaluate(srhs, values)
        except Exception:  # a point outside the domain (division by zero, complex result)
            continue
        compared += 1
        if abs(a - b) > EQUATION_RTOL * max(1.0, abs(a), abs(b)):
            point = ", ".join(f"{k}={v:.3g}" for k, v in sorted(values.items()))
            return Issue("equation_mismatch", f"equation {label}: the LaTeX and the SymPy form give different "
                                              f"values ({a:.6g} vs {b:.6g} at {point})")
    if not compared:
        return Issue("equation_latex", f"equation {label}: could not evaluate the LaTeX or SymPy form at any test point")
    return None


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
    # corrections that passed, with where they apply: "clause" (the clause text) or "where" (a where-block line)
    accepted: list[tuple[SymbolCorrection, str]] = field(default_factory=list)


def where_lines(clause: ClauseRec) -> list[str]:
    """Where-block lines as Claude sees them: each item's symbol, definition, and both together."""
    items = [*clause.where, *(w for e in clause.equations for w in e.where)]
    return [t for w in items for t in (f"{w.symbol} {w.definition}", w.symbol, w.definition) if t]


def place_correction(corr: SymbolCorrection, clause: ClauseRec) -> tuple[Issue | None, int, str]:
    """Check a correction against the clause text, else the where-block line holding its raw
    text. Returns (issue, position in the clause text or -1, target)."""
    if corr.raw and corr.raw in clause.text:
        issue, start = check_correction(corr, clause.text, suspect_mask(clause.text, clause.suspect_spans))
        return issue, start, "clause"
    for line in where_lines(clause):
        if corr.raw and corr.raw in line:
            issue, _ = check_correction(corr, line, suspect_mask(line, clause.suspect_spans))
            return issue, -1, "where"
    issue, start = check_correction(corr, clause.text, [])
    if issue and "is not in the clause text" in issue.detail:
        issue.detail = f"raw text {corr.raw[:60]!r} is not in the clause text or its where-block"
    return issue, start, "clause"


def check_clause(result: ClauseExtraction, clause: ClauseRec, notation_ids: set[str],
                 notation_latex: dict[str, str] | None = None) -> ClauseCheck:
    """``notation_latex``: id -> LaTeX of the notation entries, for the LaTeX-vs-SymPy check.
    Symbol corrections may fix the clause text or a where-block line; only clause-text ones
    change the corrected clause text."""
    issues: list[Issue] = []
    placed, accepted = [], []
    for corr in result.symbol_corrections:
        issue, start, target = place_correction(corr, clause)
        if issue:
            issues.append(issue)
            continue
        accepted.append((corr, target))
        if target == "clause":
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
        symbols = {p.id: p.latex for p in result.parameters}  # the clause's own parameters win a shared LaTeX
        for i, latex in {**{i: i for i in notation_ids}, **(notation_latex or {})}.items():
            symbols.setdefault(i, latex)
        if not undefined and (issue := check_equation_latex(eq, symbols, f"{clause.id}:{label}")):
            issues.append(issue)
        if eq.computes not in {s.name for s in lhs.free_symbols}:
            issues.append(Issue("equation_structure", f"equation {label}: computes {eq.computes!r} is not on the "
                                                      f"left-hand side"))
        if eq.equation_id in equations:
            raw = equations[eq.equation_id].raw_text
            if sum(c.isascii() and c.isalpha() for c in raw) >= 3 and letters_match(eq.latex, raw) < LETTER_MATCH:
                issues.append(Issue("equation_letter_mismatch",
                                    f"equation {label}: the LaTeX letters do not match the text layer {raw[:80]!r}"))

    answered = {eq.equation_id for eq in result.equations}
    if missing := [e.id for e in clause.equations if e.id not in answered]:
        issues.append(Issue("equation_missing", f"numbered equations missing from the answer: {', '.join(missing)}; "
                                                f"give each one with its equation_id"))
    known_refs = {norm_text(r.text) for r in clause.references}
    issues += [Issue("reference_unknown", f"reference {r.text[:80]!r} is not in the references list", retry=False)
               for r in result.references if norm_text(r.text) not in known_refs]
    issues += [Issue("unmapped", f"{u.kind}: {u.text[:120]}", retry=False) for u in result.unmapped]
    issues += garbled_text(result)  # control, private-use or replacement characters outside quotes
    return ClauseCheck(issues, corrected if placed else None, accepted)


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
    if row.grid_method == "image":  # a grid read from the image: confirm against the text layer's numbers
        if not any(abs(n - value.number) <= 1e-9 * max(1.0, abs(n)) for n in table.text_numbers):
            return Issue("table_number_mismatch", f"row {row.index}, col {value.col}: {value.number:g} is not "
                                                  f"printed in the table's text layer")
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


def image_cells(result: TableExtraction, table: TableRec, rows: list[RowRec]) -> list[tuple[int, int, float, str]]:
    """(row, col, number, value) of every number the text layer does not confirm: on an
    image-trust table these are taken from the image and go to a second read."""
    by_index = {r.index: r for r in rows}
    out = []
    for o in result.rows:
        row = by_index.get(o.row)
        if row is None or row.is_header:
            continue
        for value in o.values:
            if 0 <= value.col < len(row.cells) and number_issue(value, row, table):
                out.append((o.row, value.col, value.number, value.value))
    return out


def check_second_read(reads, wanted: list[tuple[int, int, float, str]]) -> list[Issue]:
    """Compare an independent second read of image-sourced cells with the first.
    A missing cell is retried; agreement is a low note, disagreement a high flag."""
    got = {(c.row, c.col): c for c in reads.cells}
    missing = [(r, c) for r, c, _, _ in wanted if (r, c) not in got]
    if missing:
        return [Issue("grid_dimension", f"cells not read: {missing[:20]}")]
    agree, issues = 0, []
    for r, c, number, value in wanted:
        second = got[(r, c)].number
        if second is not None and abs(second - number) <= 1e-9 * max(1.0, abs(number)):
            agree += 1
        else:
            issues.append(Issue("image_read_disagrees", f"row {r}, col {c}: first read {value!r} ({number:g}), "
                                                       f"second read {got[(r, c)].value!r}", retry=False))
    if agree:
        issues.append(Issue("image_read", f"{agree} value(s) read from the image, confirmed by a second read",
                            retry=False))
    return issues


def is_body_row(row: RowRec, caption: str, headers: list[str]) -> bool:
    """A non-header row that holds values. Header rows, the caption repeated in the grid
    and heading rows (labels only, folded into the next rows' conditions) are not."""
    if row.is_header or not has_values(row, headers):
        return False
    text, cap = norm_text(" ".join(row.cells)), norm_text(caption)
    return not (cap and (text in cap or cap in text))


def _cond_key(text: str) -> str:
    text = (text or "").lower().replace("≤", "<=").replace("≥", ">=").replace("√", "sqrt").replace("′", "'")
    return re.sub(r"[\s()*·×]+", "", text)


COMPARISON = re.compile(r"[≤≥<>]")
ITEM_NUMBER = re.compile(r"^\s*\d+\.\s*$")  # '1.' numbering a list item


def _not_a_value(text: str, conds: str, header: str = "") -> bool:
    """Label text (a trailing digit is a footnote marker), a comparison, list numbering,
    or text that is part of the row's conditions."""
    return (is_blank(text) or is_label(text, header) or mostly_words(text) or bool(COMPARISON.search(text))
            or bool(ITEM_NUMBER.match(text)) or (bool(_cond_key(text)) and _cond_key(text) in conds))


def _without_cell_marker(word: str, row: RowRec) -> str:
    """A text-layer word with a raised footnote marker glued on ('20007' for '2000' and
    marker 7) loses the marker when the rest is a number printed in the row's cells."""
    cell_numbers = {n for cell in row.cells for n in numbers(cell)}
    for marker in {m for ms in row.cell_markers.values() for m in ms}:
        rest = word.rstrip()[:-len(marker)] if word.rstrip().endswith(marker) else None
        if rest and numbers(rest) and all(n in cell_numbers for n in numbers(rest)):
            return rest
    return word


def grid_numbers_missing(out, row: RowRec, headers: list[str], band: list[str] | None = None,
                         markers: set[str] | frozenset = frozenset()) -> list[str]:
    """Numbers printed in the row that the answer's values for the row leave out: in its
    grid cells, and in ``band`` (the words of the PDF's text layer inside the table's width
    and the row's height, which also catches a column the grid lost). Numbers that appear
    in the row's conditions are not values either."""
    answered = []
    for value in out.values:
        answered += ([value.number] if value.number is not None else []) + numbers(value.value)
    answered += numbers(" ".join(out.conditions))
    conds = _cond_key(" ".join(out.conditions))
    missing = []
    texts = [(f"col {c}", cell, headers[c] if c < len(headers) else "") for c, cell in enumerate(row.cells)]
    texts += [("text", _without_cell_marker(word, row), "") for word in band or []]
    for where, text, header in texts:
        if _not_a_value(text, conds, header) or (where == "text" and text.strip() in markers):
            continue  # a footnote marker printed as its own word is not a value
        for n in numbers(text):
            if not any(abs(n - a) <= 1e-9 * max(1.0, abs(n)) for a in answered):
                missing.append(f"{where}: {text.strip()[:30]}")
                break
    return list(dict.fromkeys(missing))


GARBLED_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ue000-\uf8ff\ufffd\U000f0000-\U0010ffff]"
                          # the same characters written out as JSON escapes ('\u0003' as six characters)
                          r"|\\u00[01][0-9a-fA-F]|\\u007[fF]|\\u[eE][0-9a-fA-F]{3}|\\u[fF][0-8][0-9a-fA-F]{2}"
                          r"|\\u[fF][fF][fF][dD]")
QUOTE_FIELDS = {"source_quote", "raw"}  # quotes of the text layer: garbled text is allowed there
COPY_WINDOW = 5


def _strings(value, path: str = ""):
    """(path, text) of every string in a Pass 2 answer, except quotes of the text layer."""
    if isinstance(value, dict):
        for k, x in value.items():
            if k not in QUOTE_FIELDS:
                yield from _strings(x, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, x in enumerate(value):
            yield from _strings(x, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, value


def _odd(ch: str) -> bool:
    """A character a font-mapping fault produces: control, private use, replacement, or
    a Latin-1 letter (a math font can map '+' to 'þ', parentheses to 'ð' and 'Þ')."""
    return bool(GARBLED_CHAR.match(ch)) or ("\u00c0" <= ch <= "\u00ff" and ch not in "×÷")


def garbled_text(result, spans: list[str] | None = None) -> list[Issue]:
    """A control, private-use or replacement character anywhere in the answer, or text
    copied verbatim from a suspect span (a stretch of it that holds an odd character)."""
    issues = []
    strings = list(_strings(result.model_dump(mode="json")))
    for path, text in strings:
        if m := GARBLED_CHAR.search(text):
            issues.append(Issue("garbled_text", f"{path}: {text[max(0, m.start() - 15):m.end() + 15]!r} holds a control, "
                                                f"private-use or replacement character; transcribe it from the image"))
    for span in spans or []:
        windows = {span[i:i + COPY_WINDOW] for i in range(max(0, len(span) - COPY_WINDOW + 1))
                   if any(_odd(c) for c in span[i:i + COPY_WINDOW])}
        if not windows:
            continue
        for path, text in strings:
            flat = " ".join(text.split())
            if any(w in flat for w in windows):
                issues.append(Issue("garbled_text", f"{path}: copies the grid text of a suspect cell ({span[:40]!r}); "
                                                    f"transcribe it from the image, never copy the grid text"))
                break
    return list({i.detail: i for i in issues}.values())[:12]


def value_columns(result: TableExtraction) -> list[Issue]:
    """Every value must sit in a column that a header parameter names, or nothing says
    what the number is. One issue per row, listing its stray columns."""
    header_cols = sorted({h.col for h in result.header_parameters})
    issues = []
    for out in result.rows:
        stray = sorted({v.col for v in out.values} - set(header_cols))
        if stray:
            issues.append(Issue("value_column", f"row {out.row}: values in columns {stray} have no header parameter "
                                                f"(header parameter columns: {header_cols}); add a header parameter for each "
                                                f"such column (when merged cells shift a quantity to another column in "
                                                f"some rows, list its parameter once per column)"))
    return issues


def footnote_in_value(value, row: RowRec) -> Issue | None:
    """A value that ends with the digits of a footnote marker printed raised on that
    cell (``cell_markers``), where the cell without the marker does not: '3000-100002'
    read from '3000-10,000' with a raised '2'."""
    cell = row.cells[value.col] if 0 <= value.col < len(row.cells) else ""
    flat = lambda s: re.sub(r"[\s,]", "", s or "")  # noqa: E731
    for marker in row.cell_markers.get(value.col, []):
        texts = [flat(value.value)] + ([f"{value.number:g}"] if value.number is not None else [])
        if any(t.endswith(marker) and t[:-len(marker)][-1:].isdigit() and not flat(cell).endswith(t)
               for t in texts if len(t) > len(marker)):
            return Issue("footnote_in_value", f"row {row.index} col {value.col}: {value.value!r} ends with "
                         f"footnote marker {marker!r}, printed raised in the PDF; the value is {cell!r} and the "
                         f"marker refers to footnote {marker}")
    return None


def check_table(result: TableExtraction, table: TableRec, rows: list[RowRec],
                bands: dict[int, list[str]] | None = None) -> list[Issue]:
    """``bands``: row index -> the words of the text layer in that row's band (optional)."""
    issues: list[Issue] = []
    number_rows: set[int] = set()
    by_index = {r.index: r for r in rows}
    width = max((len(r.cells) for r in rows), default=0)
    seen = set()
    for out in result.rows:
        row = by_index.get(out.row)
        if row is None or row.is_header:
            issues.append(Issue("grid_dimension", f"row {out.row} is not a body row of this grid"))
            continue
        seen.add(out.row)
        band = (bands or {}).get(out.row) or []
        for value in out.values:
            if value.col >= len(row.cells) and value.number is not None and \
                    any(abs(n - value.number) <= 1e-9 * max(1.0, abs(n)) for n in numbers(" ".join(band))):
                continue  # a value the grid lost, printed in the row's text band: accepted past the grid
            if not 0 <= value.col < len(row.cells):
                issues.append(Issue("grid_dimension", f"row {out.row} has no column {value.col}"))
                continue
            if issue := footnote_in_value(value, row):
                issues.append(issue)
            if (issue := number_issue(value, row, table)) and table.trust == "text":
                issues.append(issue)  # text-trust tables: the text layer is the reference
                number_rows.add(out.row)
            if value.formula:
                try:
                    parse_side(value.formula) if SAFE_EXPR.match(value.formula) and "__" not in value.formula \
                        else (_ for _ in ()).throw(ValueError("unsupported characters"))
                except Exception as exc:
                    issues.append(Issue("sympy_parse", f"row {out.row}, col {value.col}: formula "
                                                       f"{value.formula[:80]!r}: {exc}"))
    issues += value_columns(result)
    issues += garbled_text(result, table.suspect_spans)
    headers = column_headers(rows)
    if table.trust == "text":  # image-trust tables: the text layer is not the reference
        for out in result.rows:
            row = by_index.get(out.row)
            if row is None or row.is_header or out.row in number_rows:
                continue  # a row with a wrong number is already retried for it
            markers = {m for m, _ in table.footnotes} | set(row.markers)
            band = None if row.grid_method == "image" else (bands or {}).get(out.row)  # no row boxes in an image grid
            if gaps := grid_numbers_missing(out, row, headers, band, markers):
                issues.append(Issue("value_missing", f"row {out.row}: numbers in the grid are missing from the answer "
                                                     f"({'; '.join(gaps[:6])}); report every value in the row"))
    missing = [r.index for r in rows if r.index not in seen and is_body_row(r, table.caption, headers)]
    if missing:
        issues.append(Issue("row_missing", f"body rows of the grid are missing from the answer: {missing[:20]}; "
                                           f"report every row that holds values"))
    past_grid = {x.col for o in result.rows for x in o.values if x.col >= width}  # accepted above
    for hp in result.header_parameters:
        if not 0 <= hp.col < width and hp.col not in past_grid:
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
