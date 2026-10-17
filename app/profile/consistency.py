"""Self-consistency checks between a proposed profile and the document it describes.

Each check compares what a profile pattern accepts with what a broad, generic
detector finds in the same text, and fails when the profile covers less than a
relative share of it. No vocabulary from any one document is used: candidate
reference words are learned from the text itself (words that are mostly followed
by a section-style number), plus the words the profile proposes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re

from app.ingest.pass1.layout import reading_order
from app.ingest.pass1.references import reference_pattern
from app.ingest.pass1.segment import clause_id
from app.ingest.pass1.skeleton import Skeleton
from app.profile.schema import Profile

MIN_WHERE_SHARE = 0.90  # share of definition lines in where-blocks the item pattern must accept
MIN_REFERENCE_SHARE = 0.90  # share of reference-like mentions the reference patterns must accept
REFERENCE_WORD_SHARE = 0.5  # a word followed by a section-style number at least this often is a reference word
MAX_ID_MISMATCH_SHARE = 0.01  # share of headings whose id may disagree with the printed number
MAX_UNDECLARED_SHARE = 0.01  # share of headings that may sit at a depth their stream does not declare

# A definition line: a short symbol, "=", then text. Symbols may hold any characters but spaces and "=";
# a first word ending in ":" is a label ("Note: IO = ..."), not a symbol.
DEFINITION = re.compile(r"^[^\s=]{0,19}[^\s=:](?:\s[^\s=]{1,6})?\s*=\s*\S")
# A section-style number: dotted or dashed components (4.2, 12.3.1, 5-6, 3-11a) or a lettered one (A.1).
SECTION_NUMBER = r"(?:[A-Z]\.\d+(?:\.\d+)*|[1-9]\d*(?:[.\-–]\d+)+[a-z]?)(?![\d\-]|\.\d)"
MENTION = re.compile(rf"(?P<word>§|[A-Za-z]{{2,}})\.?\s*\(?(?P<num>{SECTION_NUMBER})")
WORD = re.compile(r"[A-Za-z]{2,}")


@dataclass
class Share:
    matched: int
    total: int

    @property
    def share(self) -> float:
        return self.matched / self.total if self.total else 1.0


def where_lines(skel: Skeleton, profile: Profile) -> list[str]:
    """Definition-shaped lines inside where-blocks: from each intro line, the following
    lines in reading order, skipping indented continuations, until a line that is
    neither."""
    out = []
    intro = profile.where_block.intro_regex
    for page in sorted(skel.ctx.page_lines):
        lines = reading_order(skel.ctx.page_lines[page])
        for i, line in enumerate(lines):
            text = line.text.strip()
            if not re.match(intro, text):
                continue
            rest = re.sub(intro, "", text, count=1).strip(" :")
            if DEFINITION.match(rest):
                out.append(rest)
            item_x = None
            for nxt in lines[i + 1:]:
                t = nxt.text.strip()
                if DEFINITION.match(t):
                    out.append(t)
                    item_x = nxt.bbox[0] if item_x is None else item_x
                elif item_x is not None and nxt.bbox[0] > item_x + 2:
                    continue
                else:
                    break
    return out


def where_share(skel: Skeleton, profile: Profile) -> tuple[Share, list[str]]:
    lines = where_lines(skel, profile)
    rejected = [t for t in lines if not re.match(profile.where_block.item_regex, t)]
    return Share(len(lines) - len(rejected), len(lines)), rejected


def reference_share(text: str, profile: Profile) -> tuple[Share, list[str]]:
    """Mentions = a learned or proposed reference word followed by a number; covered =
    inside a match of the profile's reference patterns or external-code pattern."""
    words = Counter(w.lower() for w in WORD.findall(text))
    generic = list(MENTION.finditer(text))
    followed = Counter(m.group("word").lower() for m in generic)
    learned = {w for w, n in followed.items() if w == "§" or n >= REFERENCE_WORD_SHARE * max(words[w], 1)}
    proposed = {w.lower() for kind in profile.references for w in kind.words}
    mentions = {m.start(): m.group(0) for m in generic if m.group("word").lower() in learned}
    for word in proposed:
        for m in re.finditer(rf"\b{re.escape(word)}\s*\(?\d", text, re.IGNORECASE):
            mentions.setdefault(m.start(), m.group(0))
    spans = [(m.start(), m.end()) for kind in profile.references for m in reference_pattern(kind, profile).finditer(text)]
    spans += [(m.start(), m.end()) for m in re.finditer(profile.external_code_regex, text)]
    uncovered = [s for start, s in sorted(mentions.items()) if not any(a <= start < b for a, b in spans)]
    return Share(len(mentions) - len(uncovered), len(mentions)), uncovered


def id_mismatches(skel: Skeleton, profile: Profile) -> tuple[Share, list[str]]:
    """Clause ids must agree with the numbers printed in the headings: the stream's
    printed prefix stands right before the number, and only a nested stream (quoted
    material) may add a namespace that is not printed."""
    streams = {s.name: s for s in profile.streams}
    total, bad = 0, []
    for blocks in skel.blocks.values():
        for block in blocks:
            if block.kind != "heading":
                continue
            stream = streams[block.stream]
            total += 1
            text = block.lines[0].text.strip()
            match = re.match(stream.number_regex, text)
            printed = text[:match.start("num")] if match else ""
            if not printed.endswith(stream.prefix):
                bad.append(f"p{block.page} {text[:30]!r}: prefix {stream.prefix!r} is not printed before the number")
            elif stream.id_prefix and not stream.nested_in:
                bad.append(f"p{block.page} {text[:30]!r}: id {clause_id(stream, block.number)!r} adds "
                           f"{stream.id_prefix!r}, which is not printed (only nested streams may)")
    return Share(total - len(bad), total), bad


def undeclared_levels(skel: Skeleton, profile: Profile) -> tuple[Share, Counter]:
    """Headings at a depth for which their stream declares no level: the profile is
    incomplete even when another level's style happens to catch them."""
    streams = {s.name: s for s in profile.streams}
    total, missing = 0, Counter()
    for blocks in skel.blocks.values():
        for block in blocks:
            if block.kind != "heading":
                continue
            stream = streams[block.stream]
            total += 1
            depth = len(block.number.split(stream.separator))
            if all(lv.depth != depth for lv in stream.levels):
                missing[(stream.name, depth, block.style)] += 1
    return Share(total - sum(missing.values()), total), missing


def consistency_issues(skel: Skeleton, profile: Profile) -> tuple[list[str], list[str]]:
    """(issues that fail validation, examples to show Claude)."""
    failing, examples = [], []
    where, rejected = where_share(skel, profile)
    if where.share < MIN_WHERE_SHARE:
        failing.append(f"where-items: item_regex accepts {where.matched} of {where.total} definition lines "
                       f"in where-blocks ({where.share:.0%}, needs {MIN_WHERE_SHARE:.0%})")
        examples += [f"  rejected definition line: {t[:60]!r}" for t in rejected[:8]]
    text = "\n".join(c.text for c in skel.seg.clauses)
    refs, uncovered = reference_share(text, profile)
    if refs.share < MIN_REFERENCE_SHARE:
        failing.append(f"references: reference patterns cover {refs.matched} of {refs.total} reference-like "
                       f"mentions ({refs.share:.0%}, needs {MIN_REFERENCE_SHARE:.0%})")
        common = Counter(re.sub(r"\d", "#", u) for u in uncovered).most_common(10)
        examples += [f"  uncovered mention shape: {shape!r} x{n}" for shape, n in common]
    levels, missing = undeclared_levels(skel, profile)
    if levels.total and 1 - levels.share > MAX_UNDECLARED_SHARE:
        failing.append(f"levels: {sum(missing.values())} of {levels.total} headings sit at depths their stream "
                       f"does not declare")
        examples += [f"  stream {s} depth {d} [{style}] x{n}: add a level" for (s, d, style), n in missing.most_common(6)]
    ids, bad = id_mismatches(skel, profile)
    if ids.total and 1 - ids.share > MAX_ID_MISMATCH_SHARE:
        failing.append(f"ids: {ids.total - ids.matched} of {ids.total} heading ids disagree with the printed numbers")
        examples += [f"  {b}" for b in bad[:6]]
    return failing, examples
