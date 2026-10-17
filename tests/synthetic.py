"""Small generated PDFs and a generic profile, for document-independent tests."""
from __future__ import annotations

from pathlib import Path

import pymupdf

from app.profile.schema import Profile, ProfileRules

FONTS = {"bold": "hebo", "body": "tiro", "sans": "helv", "boldit": "hebi"}
PROSE = "This requirement applies to every structure covered by this part of the code"


def make_pdf(path: Path, pages: list[list[tuple]]) -> Path:
    """pages: per page, (text, x, y, kind, size) with kind in FONTS."""
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page(width=612, height=792)
        for text, x, y, kind, size in items:
            page.insert_text((x, y), text, fontname=FONTS[kind], fontsize=size)
    doc.save(path)
    return path


def heading(num: str, title: str, y: float, x: float = 54, size: float = 11) -> list[tuple]:
    return [(num, x, y, "bold", size), (title, x + 6 * len(num) + 14, y, "bold", size)]


def prose(y: float, lines: int = 3, x: float = 54) -> list[tuple]:
    return [(PROSE, x, y + 13 * i, "body", 10) for i in range(lines)]


def rules(**overrides) -> dict:
    data = {
        "title": "Synthetic Code", "edition": "2026", "jurisdiction": "", "self_designation": "SC 1",
        "streams": [{
            "name": "provision", "kind": "provision", "number_regex": r"^(?P<num>\d+(?:\.\d+)*)\.?(?:\s+|$)",
            "prefix": "", "separator": ".", "commentary_of": None, "examples": [],
            "levels": [{"depth": d, "style": {"font_regex": "^Helvetica-Bold$", "size_min": 10.5,
                                               "size_max": 11.5, "bold": True}} for d in (1, 2, 3)],
        }],
        "table_caption": {"regex": r"^Table\s+(?P<num>\d+-\d+)", "continued_regex": r"\(continued\)", "style": None},
        "figure_caption": {"regex": r"^Figure\s+(?P<num>\d+-\d+)", "continued_regex": None, "style": None},
        "equation_number": r"\((?P<num>\d+-\d+)\)",
        "footnote": {"line_regex": r"^(?P<marker>\d+)\.\s", "marker_superscript": True},
        "where_block": {"intro_regex": r"^where\b", "item_regex": r"^(?P<symbol>\S+)\s*=\s*(?P<definition>.+)"},
        "running_elements": [],
        "references": [
            {"kind": "clause", "words": ["Section", "Sections"], "number_regex": r"\d+(?:\.\d+)*"},
            {"kind": "table", "words": ["Table", "Tables"], "number_regex": r"\d+-\d+"},
            {"kind": "figure", "words": ["Figure"], "number_regex": r"\d+-\d+"},
            {"kind": "equation", "words": ["Equation"], "number_regex": r"\d+-\d+"},
        ],
        "list_connectors": [",", "and"], "range_connectors": ["through"],
        "external_code_regex": r"\b(?P<code>(?:ACI|ASTM)\s?\d+(?:-\d+)?)",
    }
    data.update(overrides)
    return data


def profile(page_count: int, **overrides) -> Profile:
    rule_set = ProfileRules.model_validate(rules(**overrides))
    return Profile(**rule_set.model_dump(), page_count=page_count,
                   ranges=[{"start": 1, "end": page_count, "kind": "body"}])
