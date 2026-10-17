"""Font and text-layer check for building-code PDFs.

For each PDF in fixtures/ (plus data/fema356.pdf if present), report fonts used
(information only), warning spans (private-use, control or U+FFFD characters),
other non-ASCII characters with Unicode names, and superscript digits after
letters split into footnote markers and unexplained superscripts.
Warning rules live in app/ingest/text_checks.py.

Usage: python scripts/font_check.py [extra.pdf ...]
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import sys

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ingest import text_checks as tc  # noqa: E402

DEFAULT_TARGETS = [ROOT / "fixtures", ROOT / "data" / "fema356.pdf"]
MAX_SAMPLES = 12
WARNING_CLASSES = ("private_use", "control", "replacement")


@dataclass
class FontInfo:
    name: str
    font_type: str
    encoding: str
    embedded: bool
    pages: set[int] = field(default_factory=set)


@dataclass
class CharStat:
    count: int = 0
    fonts: Counter = field(default_factory=Counter)
    pages: set[int] = field(default_factory=set)


@dataclass
class Superscript:
    page: int
    marker: str
    context: str


@dataclass
class FileReport:
    path: Path
    page_count: int = 0
    pages_without_text: list[int] = field(default_factory=list)
    fonts: dict[str, FontInfo] = field(default_factory=dict)
    non_ascii: dict[str, CharStat] = field(default_factory=lambda: defaultdict(CharStat))
    warning_spans: Counter = field(default_factory=Counter)  # font -> spans with bad chars
    warning_pages: set[int] = field(default_factory=set)
    footnote_markers: list[Superscript] = field(default_factory=list)
    unexplained: list[Superscript] = field(default_factory=list)

    def by_class(self, cls: str) -> dict[str, CharStat]:
        return {c: s for c, s in self.non_ascii.items() if tc.char_class(c) == cls}

    def occurrences(self, cls: str) -> int:
        return sum(s.count for s in self.by_class(cls).values())


def collect_fonts(page: pymupdf.Page, report: FileReport) -> None:
    for xref, ext, font_type, basefont, _name, encoding, *_ in page.get_fonts(full=True):
        key = basefont or f"xref{xref}"
        info = report.fonts.setdefault(
            key, FontInfo(key, font_type, encoding or "-", embedded=ext != "n/a")
        )
        info.pages.add(page.number + 1)


def page_lines(page: pymupdf.Page) -> list[dict]:
    raw = page.get_text("rawdict")
    return [line for block in raw["blocks"] for line in block.get("lines", [])]


def record_chars(span: dict, page_no: int, report: FileReport) -> None:
    for c in span["chars"]:
        ch = c["c"]
        if ord(ch) > 127 or tc.is_control(ch):
            stat = report.non_ascii[ch]
            stat.count += 1
            stat.fonts[span["font"]] += 1
            stat.pages.add(page_no)
    if tc.bad_chars(span):
        report.warning_spans[span["font"]] += 1
        report.warning_pages.add(page_no)


def scan_page(page: pymupdf.Page, report: FileReport) -> None:
    collect_fonts(page, report)
    page_no = page.number + 1
    lines = page_lines(page)
    footnotes = tc.footnote_markers(lines)
    had_text = False
    for line in lines:
        spans = line["spans"]
        for i, span in enumerate(spans):
            if tc.span_text(span).strip():
                had_text = True
            record_chars(span, page_no, report)
            marker = tc.superscript_marker(spans[i - 1], span) if i > 0 else None
            if marker:
                context = tc.span_text(spans[i - 1])[-20:] + "^" + marker
                explained = tc.is_footnote_reference(marker, span, footnotes)
                target = report.footnote_markers if explained else report.unexplained
                target.append(Superscript(page_no, marker, context))
    if not had_text:
        report.pages_without_text.append(page_no)


def scan_pdf(path: Path) -> FileReport:
    report = FileReport(path)
    with pymupdf.open(path) as doc:
        report.page_count = doc.page_count
        for page in doc:
            scan_page(page, report)
    return report


def fmt_pages(pages, limit: int = 8) -> str:
    ordered = sorted(pages)
    shown = ", ".join(map(str, ordered[:limit]))
    return shown + (f" (+{len(ordered) - limit} more)" if len(ordered) > limit else "")


def fmt_char_line(ch: str, stat: CharStat) -> str:
    fonts = ", ".join(f for f, _ in stat.fonts.most_common(2))
    return (
        f"    U+{ord(ch):04X} {ch!r:8} {tc.char_name(ch):38} x{stat.count:<6} "
        f"fonts: {fonts}  pages: {fmt_pages(stat.pages, 4)}"
    )


def print_chars(title: str, chars: dict[str, CharStat]) -> None:
    total = sum(s.count for s in chars.values())
    print(f"\n  {title}: {len(chars)} distinct, {total} total")
    for ch, stat in sorted(chars.items(), key=lambda kv: -kv[1].count):
        print(fmt_char_line(ch, stat))


def print_report(report: FileReport) -> None:
    print("=" * 100)
    print(f"{report.path.name}  ({report.page_count} pages)")
    print("=" * 100)
    if report.pages_without_text:
        print(f"  Pages with no text layer: {fmt_pages(report.pages_without_text)}")

    print(f"\n  Fonts ({len(report.fonts)}, information only):")
    for f in sorted(report.fonts.values(), key=lambda f: f.name):
        emb = "embedded" if f.embedded else "not embedded"
        print(f"    {f.name:45} {f.font_type:10} {f.encoding:18} {emb:13} pages: {fmt_pages(f.pages, 4)}")

    spans = sum(report.warning_spans.values())
    print(f"\n  WARNING spans (private-use, control or U+FFFD): {spans} spans on {len(report.warning_pages)} pages")
    if spans:
        print(f"    by font: {dict(report.warning_spans.most_common())}")
        print(f"    pages: {fmt_pages(report.warning_pages, 20)}")
    print_chars("  Private-use characters", report.by_class("private_use"))
    print_chars("  Control characters", report.by_class("control"))
    print_chars("  Replacement characters (U+FFFD)", report.by_class("replacement"))
    print_chars("Other non-ASCII characters (information only)", report.by_class("other"))

    print(f"\n  Superscript digits after letters: {len(report.footnote_markers)} footnote markers, "
          f"{len(report.unexplained)} UNEXPLAINED")
    for s in report.unexplained[:MAX_SAMPLES]:
        print(f"    p{s.page}: {s.context}")
    if len(report.unexplained) > MAX_SAMPLES:
        print(f"    ... pages: {fmt_pages({s.page for s in report.unexplained}, 20)}")
    print()


def print_summary(reports: list[FileReport]) -> None:
    print("SUMMARY")
    print(f"  {'file':26} {'pages':>5} {'fonts':>5} {'warn-spans':>10} {'PUA':>5} {'ctrl':>5} "
          f"{'U+FFFD':>6} {'footnote':>8} {'unexpl-sup':>10} {'no-text':>7}")
    for r in reports:
        print(
            f"  {r.path.name[:26]:26} {r.page_count:>5} {len(r.fonts):>5} "
            f"{sum(r.warning_spans.values()):>10} {r.occurrences('private_use'):>5} "
            f"{r.occurrences('control'):>5} {r.occurrences('replacement'):>6} "
            f"{len(r.footnote_markers):>8} {len(r.unexplained):>10} {len(r.pages_without_text):>7}"
        )
    print("  (PUA/ctrl/U+FFFD = character occurrences; footnote = superscripts matched to a footnote on the page)")


def find_pdfs(targets: list[Path]) -> list[Path]:
    pdfs: dict[Path, None] = {}  # ordered, de-duplicated
    for t in targets:
        if t.is_dir():
            pdfs.update(dict.fromkeys(p.resolve() for p in sorted(t.glob("*.pdf"))))
        elif t.is_file() and t.suffix.lower() == ".pdf":
            pdfs[t.resolve()] = None
    return list(pdfs)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    pdfs = find_pdfs(DEFAULT_TARGETS + [Path(a) for a in argv])
    if not pdfs:
        print("No PDFs found in fixtures/ or data/fema356.pdf.")
        return 1
    reports = [scan_pdf(p) for p in pdfs]
    for r in reports:
        print_report(r)
    print_summary(reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
