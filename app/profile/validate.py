"""Step 2.4: apply a profile to every body page and check it deterministically.

Checks: heading order within each stream, hierarchy (every parent appears first),
coverage (share of body prose assigned to a clause whose heading is near), caption
style, unmapped fonts, and self-consistency (where-items, reference patterns and ids
against what generic detectors find in the same text; see consistency.py).
The failing spots are rendered as text for Claude's single revision.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import re

import pymupdf

from app.ingest.pdf_lines import base_font_name, is_bold
from app.ingest.pass1.layout import reading_order
from app.ingest.pass1.segment import Segmentation, clause_id, low_coverage_ranges, number_key, parent_number
from app.ingest.pass1.skeleton import Skeleton, build_skeleton
from app.profile.consistency import (consistency_issues, id_mismatches, reference_share, undeclared_levels,
                                     where_share)
from app.profile.schema import Profile, Validation

MIN_COVERAGE = 0.90
VIOLATION_SHARE = 0.01  # tolerated share of headings with order or hierarchy problems
MIN_VIOLATIONS = 2
MAX_ISSUES = 40
SAMPLE_LINES = 6


@dataclass
class Report:
    validation: Validation
    failures: str  # failing spots for the revise prompt ("" when it passes)
    skeleton: Skeleton


def order_issues(seg: Segmentation, profile: Profile) -> list[str]:
    """Numbers never decrease within a stream; a nested stream restarts under each
    clause that quotes it."""
    streams = {s.name: s for s in profile.streams}
    last: dict[tuple, tuple[tuple, str]] = {}
    issues = []
    for clause in seg.clauses:
        if "~" in clause.id:
            continue
        scope = (clause.stream, clause.container_id if streams[clause.stream].nested_in else None)
        key = number_key(clause.number, streams[clause.stream].separator)
        if scope in last and key < last[scope][0]:
            issues.append(f"order: p{clause.page} {clause.id} ({clause.title[:40]!r}) "
                          f"comes after {last[scope][1]}")
        last[scope] = (key, clause.id)
    return issues


def hierarchy_issues(seg: Segmentation, profile: Profile) -> list[str]:
    """Every provision heading's direct parent number appears earlier in its stream.
    (Commentary is sparse by nature, so it is checked by its link instead.)"""
    streams = {s.name: s for s in profile.streams}
    seen: set[str] = set()
    issues = []
    for clause in seg.clauses:
        stream = streams[clause.stream]
        parent = parent_number(clause.number, stream.separator)
        if stream.nested_in:  # quoted sections may start at any depth, but need a quoting clause
            if clause.container_id is None:
                issues.append(f"hierarchy: p{clause.page} {clause.id} ({clause.title[:40]!r}) "
                              f"appears before any {stream.nested_in} clause that could quote it")
        elif stream.kind == "provision" and parent and clause_id(stream, parent) not in seen:
            issues.append(f"hierarchy: p{clause.page} {clause.id} ({clause.title[:40]!r}) "
                          f"has no earlier parent {clause_id(stream, parent)}")
        seen.add(clause.id)
    return issues


def font_issues(skel: Skeleton, profile: Profile) -> list[str]:
    """Every unmapped-font pattern must name a font that occurs in the document."""
    fonts = {base_font_name(s["font"]) for lines in skel.ctx.page_lines.values() for ln in lines for s in ln.spans}
    return [f"fonts: unmapped_fonts pattern {pattern!r} matches no font in the document"
            for pattern in profile.unmapped_fonts if not any(re.search(pattern, f) for f in fonts)]


def style_census(skel: Skeleton, profile: Profile) -> list[str]:
    """Heading styles per stream and depth, with counts and one example each, so a
    style that only one odd heading uses stands out."""
    by_level: dict[tuple, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for blocks in skel.blocks.values():
        for b in blocks:
            if b.kind == "heading":
                depth = len(b.number.split(next(s.separator for s in profile.streams if s.name == b.stream)))
                by_level[(b.stream, depth)][b.style].append(f"p{b.page} {b.number} {(b.title or '')[:40]!r}")
    out = []
    for (stream, depth), styles in sorted(by_level.items()):
        for style, examples in sorted(styles.items(), key=lambda kv: -len(kv[1])):
            out.append(f"  {stream} depth {depth}: [{style}] x{len(examples)}, e.g. {examples[0]}")
    return out


def commentary_issues(seg: Segmentation) -> list[str]:
    ids = {c.id for c in seg.clauses}
    return [f"commentary: p{c.page} {c.id} has no provision {c.commentary_on}"
            for c in seg.clauses if c.commentary_on and c.commentary_on not in ids]


def caption_issues(skel: Skeleton, profile: Profile) -> tuple[int, list[str]]:
    """Two caption checks, both document-independent:
    - caption-shaped lines (regex match, not body style) that the caption style rejects;
    - caption-styled lines that start with the same word as accepted captions but that
      the regex rejects (for example a prefixed number shape the regex does not allow)."""
    body = skel.ctx.body
    lines = [(page, ln) for page in sorted(skel.ctx.page_lines) for ln in skel.ctx.page_lines[page]
             if (ln.font, ln.size, ln.bold) != body]
    candidates, issues = 0, []
    for kind, fmt in (("table", profile.table_caption), ("figure", profile.figure_caption)):
        lead_words = set()
        for page, line in lines:
            first = line.first_span
            styled = fmt.style is None or fmt.style.matches(first["font"], first["size"], is_bold(first))
            if re.match(fmt.regex, line.text.strip()):
                candidates += 1
                if styled:
                    lead_words.add(line.text.split()[0])
                else:
                    issues.append(f"caption: p{page} {line.text.strip()[:50]!r} [{line.style}] "
                                  f"does not match the {kind} caption style")
        for page, line in lines:
            first = line.first_span
            words = line.text.split()
            if (fmt.style is not None and fmt.style.matches(first["font"], first["size"], is_bold(first))
                    and words and words[0] in lead_words and not re.match(fmt.regex, line.text.strip())):
                candidates += 1
                issues.append(f"caption: p{page} {line.text.strip()[:50]!r} looks like a {kind} caption "
                              f"but the {kind} caption regex rejects it")
    return candidates, issues


def styled_lines(skel: Skeleton, page: int, limit: int | None = None) -> list[str]:
    lines = reading_order(skel.ctx.page_lines.get(page, []))
    out = [f"  {ln.bbox[0]:.0f},{ln.bbox[1]:.0f} [{ln.style}] {ln.text.strip()[:90]}" for ln in lines]
    return out[:limit] if limit else out


def validate(doc: pymupdf.Document, profile: Profile, skeleton: Skeleton | None = None) -> Report:
    skel = skeleton or build_skeleton(doc, profile)
    seg = skel.seg
    order = order_issues(seg, profile)
    hierarchy = hierarchy_issues(seg, profile)
    unmatched = commentary_issues(seg)
    caption_count, captions = caption_issues(skel, profile)
    fonts = font_issues(skel, profile)
    consistency, examples = consistency_issues(skel, profile)
    low = low_coverage_ranges(seg)
    heads = len(seg.clauses)
    tolerated = max(MIN_VIOLATIONS, VIOLATION_SHARE * heads)
    captions_ok = len(captions) <= max(MIN_VIOLATIONS, VIOLATION_SHARE * caption_count)
    passed = (heads > 0 and seg.coverage >= MIN_COVERAGE and captions_ok and not fonts and not consistency
              and len(order) <= tolerated and len(hierarchy) <= tolerated)
    issues = (consistency + examples + fonts + order + hierarchy + captions[:15] + seg.duplicates
              + unmatched)[:MAX_ISSUES]
    validation = Validation(
        status="pass" if passed else "warning", coverage=round(seg.coverage, 4), heading_count=heads,
        order_violations=len(order), hierarchy_violations=len(hierarchy),
        low_coverage_ranges=low, revised=False, issues=issues, checks=check_shares(skel, profile),
    )
    failures = "" if passed else render_failures(skel, validation, low, profile)
    return Report(validation, failures, skel)


def check_shares(skel: Skeleton, profile: Profile) -> dict[str, float]:
    text = "\n".join(c.text for c in skel.seg.clauses)
    return {"where_items": round(where_share(skel, profile)[0].share, 4),
            "references": round(reference_share(text, profile)[0].share, 4),
            "ids": round(id_mismatches(skel, profile)[0].share, 4),
            "levels": round(undeclared_levels(skel, profile)[0].share, 4)}


def render_failures(skel: Skeleton, validation: Validation, low: list[tuple[int, int]], profile: Profile) -> str:
    out = [f"coverage {validation.coverage:.1%} (needs {MIN_COVERAGE:.0%}); headings found: "
           f"{validation.heading_count}; order violations: {validation.order_violations}; "
           f"hierarchy violations: {validation.hierarchy_violations}"]
    per_stream = defaultdict(int)
    for clause in skel.seg.clauses:
        per_stream[clause.stream] += 1
    out.append("headings per stream: " + ", ".join(f"{k}={v}" for k, v in per_stream.items()))
    out.append("\nHeading styles found, per stream and depth:")
    out += style_census(skel, profile)
    if validation.issues:
        out.append("\nIssues:")
        out += [f"- {issue}" for issue in validation.issues]
    if low:
        out.append("\nPage ranges where body text is not assigned to a nearby clause "
                   "(probably missed headings). Styled lines from the first page of each range "
                   "(x,y [font size bold] text):")
        for start, end in low[:12]:
            out.append(f"pages {start}-{end}, lines from p{start}:")
            out += styled_lines(skel, start, SAMPLE_LINES * 4)
    return "\n".join(out)
