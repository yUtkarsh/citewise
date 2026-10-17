"""Copy single FEMA 356 pages into fixtures/ with insert_pdf (no re-rendering,
so fonts and the text layer stay exactly as in the source).

Usage: python scripts/make_fixtures.py [path/to/fema356.pdf]
"""
from pathlib import Path
import sys

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "fema356.pdf"
OUT_DIR = ROOT / "fixtures"

# fixture name -> 1-based page number in FEMA 356
FIXTURES = {
    "notation.pdf": 467,           # Symbols-1
    "definitions.pdf": 485,        # Definitions-1
    "equations.pdf": 103,          # 3-15, Eqs (3-10)-(3-13), where-blocks, stacked fraction
    "equations_brackets.pdf": 93,  # 3-5, large brackets from Symbol-font PUA glyphs
    "table.pdf": 221,              # 6-21, Table 6-7 modeling parameters
    "table_multi_1.pdf": 186,      # 5-40, Table 5-6 first page
    "table_multi_2.pdf": 187,      # 5-41, Table 5-6 (continued)
    "commentary.pdf": 205,         # 6-5, provisions with C-prefixed commentary
    "text.pdf": 231,               # 6-31, two-column text with cross-references
}


def copy_page(src: pymupdf.Document, page_no: int, dest: Path) -> None:
    out = pymupdf.open()
    out.insert_pdf(src, from_page=page_no - 1, to_page=page_no - 1)
    out.save(dest, garbage=3, deflate=True)
    out.close()


def main(argv: list[str]) -> int:
    source = Path(argv[0]) if argv else SOURCE
    OUT_DIR.mkdir(exist_ok=True)
    with pymupdf.open(source) as src:
        for name, page_no in FIXTURES.items():
            copy_page(src, page_no, OUT_DIR / name)
            print(f"{name:24} <- page {page_no}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
