"""A deterministic font inventory for the rule-proposal prompt.

PDF metadata does not reliably say which fonts are symbol fonts whose glyphs come
out as the wrong characters (a radical as "p" plus ligatures, a prime as "0"). The
inventory shows Claude what each font actually emits, so the profile can list them.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import unicodedata

import pymupdf

from app.ingest.pdf_lines import base_font_name, raw_lines

TOP_CHARS = 12
CONTEXTS = 2
CONTEXT_CHARS = 10


def show(ch: str) -> str:
    """A character as it can be read in a prompt (control and private-use as U+XXXX)."""
    if ord(ch) < 0x20 or unicodedata.category(ch) in {"Co", "Cc", "Cf", "Cn"}:
        return f"U+{ord(ch):04X}"
    return ch


def font_inventory(doc: pymupdf.Document, pages: list[int]) -> str:
    chars: dict[str, Counter] = defaultdict(Counter)
    spans: Counter = Counter()
    raised: Counter = Counter()
    contexts: dict[str, list[str]] = defaultdict(list)
    for page in pages:
        for line in raw_lines(doc[page - 1]):
            texts = [s["text"] for s in line.spans]
            for i, span in enumerate(line.spans):
                if not span["text"].strip():
                    continue
                font = base_font_name(span["font"])
                spans[font] += 1
                chars[font].update(c for c in span["text"] if not c.isspace())
                if i and span["origin"][1] < line.spans[i - 1]["origin"][1] - 0.5:
                    raised[font] += 1
                if len(contexts[font]) < CONTEXTS:
                    before = "".join(texts[:i])[-CONTEXT_CHARS:]
                    after = "".join(texts[i + 1:])[:CONTEXT_CHARS]
                    contexts[font].append(
                        "".join(map(show, before)) + "[" + "".join(map(show, span["text"][:CONTEXT_CHARS])) + "]"
                        + "".join(map(show, after)))
    rows = ["font | spans | raised share | most common characters (count) | contexts, font's text in []"]
    for font, n in spans.most_common():
        top = " ".join(f"{show(c)}({k})" for c, k in chars[font].most_common(TOP_CHARS))
        rows.append(f"{font} | {n} | {raised[font] / n:.0%} | {top} | {' ; '.join(contexts[font])}")
    return "\n".join(rows)
