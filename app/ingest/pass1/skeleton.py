"""Blocks plus segmentation over every segmented page: shared by profile
validation (Step 2.4) and Pass 1, so the two can never disagree."""
from __future__ import annotations

from dataclasses import dataclass

import pymupdf

from app.ingest.pdf_lines import style_label
from app.ingest.pass1.blocks import DocContext, load_context, page_blocks
from app.ingest.pass1.layout import vocabulary
from app.ingest.pass1.model import Block, TableSegment
from app.ingest.pass1.segment import Segmentation, segment
from app.profile.schema import Profile


@dataclass
class Skeleton:
    ctx: DocContext
    blocks: dict[int, list[Block]]
    seg: Segmentation


def build_skeleton(
    doc: pymupdf.Document,
    profile: Profile,
    tables: dict[int, list[TableSegment]] | None = None,
    pages: list[int] | None = None,
    ctx: DocContext | None = None,
) -> Skeleton:
    pages = pages or profile.segmented_pages()
    ctx = ctx or load_context(doc, profile, pages)
    tables = tables or {}
    blocks = {p: page_blocks(p, ctx, tables.get(p, [])) for p in pages}
    vocab = vocabulary([ln.text for lines in ctx.page_lines.values() for ln in lines])
    seg = segment(blocks, profile, vocab, style_label(*ctx.body))
    return Skeleton(ctx, blocks, seg)
