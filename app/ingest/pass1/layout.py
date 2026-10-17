"""Step 3a.2: strip running elements, order lines by column, clean body text."""
from __future__ import annotations

from collections import Counter
import re

from app.ingest.pdf_lines import Line
from app.ingest.text_checks import is_unmapped_font
from app.profile.schema import Profile

RUNNING_BAND = 0.08  # top and bottom fraction of the page height
REPEAT_SHARE = 0.3  # a band line repeated on this share of pages is a running element
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
SYMBOLS = {"−": "-", "′": "'", "″": "''", "‴": "'''", "­": ""}
WORD = re.compile(r"[A-Za-z]+")
HYPHEN_END = re.compile(r"([A-Za-z]+)-$")
WORD_START = re.compile(r"^([a-z]+)")


def zone(line: Line, page_height: float) -> str | None:
    centre = (line.bbox[1] + line.bbox[3]) / 2
    if centre < page_height * RUNNING_BAND:
        return "top"
    if centre > page_height * (1 - RUNNING_BAND):
        return "bottom"
    return None


def _shape(text: str) -> str:
    """Line text with digits folded, so page numbers repeat across pages."""
    return re.sub(r"\d+", "#", text.strip().lower())


def repeated_band_lines(pages: dict[int, list[Line]], heights: dict[int, float]) -> set[str]:
    """Shapes of band lines that repeat on many pages: a profile-independent safety net."""
    counts = Counter()
    for page, lines in pages.items():
        counts.update({_shape(ln.text) for ln in lines if zone(ln, heights[page])})
    threshold = max(3, REPEAT_SHARE * len(pages))
    return {shape for shape, n in counts.items() if n >= threshold}


def is_running(line: Line, page_height: float, profile: Profile, repeated: set[str]) -> bool:
    where = zone(line, page_height)
    text = line.text.strip()
    for element in profile.running_elements:
        if element.zone in (where, "any") and re.search(element.regex, text):
            return True
    return where is not None and _shape(text) in repeated


def assign_columns(lines: list[Line], page_width: float) -> None:
    """col 0 = left (or single column), 1 = right, -1 = full width across the centre."""
    centre = page_width / 2
    left = sum(1 for ln in lines if ln.bbox[2] < centre)
    right = sum(1 for ln in lines if ln.bbox[0] > centre)
    two_columns = min(left, right) >= 3
    for line in lines:
        if not two_columns:
            line.col = 0
        elif line.bbox[2] <= centre + 2:
            line.col = 0
        elif line.bbox[0] >= centre - 2:
            line.col = 1
        else:
            line.col = -1


def reading_order(items: list, key_bbox=lambda x: x.bbox, key_col=lambda x: x.col) -> list:
    """Full-width items cut the page into bands; within a band the left column
    comes before the right, each top to bottom."""
    items = sorted(items, key=lambda x: (key_bbox(x)[1], key_bbox(x)[0]))
    ordered, band = [], []

    def flush():
        band.sort(key=lambda x: (key_col(x), key_bbox(x)[1], key_bbox(x)[0]))
        ordered.extend(band)
        band.clear()

    for item in items:
        if key_col(item) == -1:
            flush()
            ordered.append(item)
        else:
            band.append(item)
    flush()
    return ordered


def normalize(text: str) -> str:
    """Ligatures, minus signs and primes in body text."""
    for src, dst in {**LIGATURES, **SYMBOLS}.items():
        text = text.replace(src, dst)
    return text


def normalize_line(line: Line, unmapped_fonts: list[str]) -> str:
    """Normalized line text, except spans from unmapped fonts: their characters are
    not what is printed (a radical drawn as ligatures), so they stay raw and flagged."""
    def clean(span: dict, text: str) -> str:
        return text if is_unmapped_font(span["font"], unmapped_fonts) else normalize(text)

    return line.joined(clean)


def vocabulary(texts: list[str]) -> set[str]:
    return {w.lower() for t in texts for w in WORD.findall(t)}


def join_lines(lines: list[str], vocab: set[str]) -> str:
    """Join wrapped lines. A trailing hyphen is dropped only when the joined word
    occurs unhyphenated elsewhere in the document; otherwise it is a real compound."""
    out = ""
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        if out.endswith("-") and (head := HYPHEN_END.search(out)) and (tail := WORD_START.match(text)):
            joined = (head.group(1) + tail.group(1)).lower()
            out = out[:-1] + text if joined in vocab else out + text
        else:
            out = f"{out} {text}" if out else text
    return out
