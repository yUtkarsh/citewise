"""Steps 2.2-2.4: Claude labels ranges and proposes rules; code validates them on
every body page; one revision on failure; then proceed with a warning."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf

from app.config import ROOT, require_model
from app.ingest.pass1.layout import assign_columns, reading_order
from app.ingest.pdf_lines import read_lines
from app.llm import CACHE_DIR, Transport, call_structured
from app.prompts import load
from app.profile.page_map import PageSummary, render_page_map
from app.profile.schema import PROFILE_SCHEMA_VERSION, Profile, ProfileRules, RangeLabels
from app.profile.validate import Report, validate

PROFILE_DIR = ROOT / "data" / "profiles"
MAX_TITLE_CHARS = 3000


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def profile_path(pdf_hash: str) -> Path:
    return PROFILE_DIR / f"{pdf_hash[:12]}.json"


def styled_page(page: pymupdf.Page) -> str:
    """A page as styled lines (x,y [font size bold] text) in reading order."""
    lines = read_lines(page)
    assign_columns(lines, page.rect.width)
    rows = [f"{ln.bbox[0]:.0f},{ln.bbox[1]:.0f} [{ln.style}] {ln.text.strip()}" for ln in reading_order(lines)]
    return "\n".join(rows)


def rules_input(doc: pymupdf.Document, labels: RangeLabels) -> str:
    parts = ["# Title pages"]
    for p in labels.title_pages:
        parts.append(f"## page {p}\n{doc[p - 1].get_text()[:MAX_TITLE_CHARS]}")
    parts.append("# Sample pages")
    for p in labels.sample_pages:
        parts.append(f"## page {p}\n{styled_page(doc[p - 1])}")
    return "\n\n".join(parts)


def examples_present(sample_text: str):
    def check(rules: ProfileRules) -> list[str]:
        return [f"stream {s.name}: example {ex!r} does not occur in the sample pages"
                for s in rules.streams for ex in s.examples if ex not in sample_text]
    return check


def to_profile(rules: ProfileRules, labels: RangeLabels, page_count: int) -> Profile:
    return Profile(**rules.model_dump(), page_count=page_count, ranges=labels.ranges)


def build_profile(
    doc: pymupdf.Document,
    page_map: list[PageSummary],
    transport: Transport | None = None,
    cache_dir: Path = CACHE_DIR,
    log=print,
) -> tuple[Profile, Report]:
    model = require_model("model_profile")
    labels = call_structured(
        model, load("profile_ranges"), render_page_map(page_map), RangeLabels, PROFILE_SCHEMA_VERSION,
        transport=transport, cache_dir=cache_dir, check=lambda r: r.errors_for(doc.page_count),
    )
    log(f"profile: ranges {[(r.start, r.end, r.kind) for r in labels.ranges]}; samples {labels.sample_pages}")
    sample_text = rules_input(doc, labels)
    rules = call_structured(
        model, load("profile_rules"), sample_text, ProfileRules, PROFILE_SCHEMA_VERSION,
        transport=transport, cache_dir=cache_dir, check=examples_present(sample_text),
    )
    profile = to_profile(rules, labels, doc.page_count)
    report = validate(doc, profile)
    log(f"profile: validation {report.validation.status}, coverage {report.validation.coverage:.1%}, "
        f"{report.validation.heading_count} headings")
    profile.body_style = report.skeleton.ctx.body
    if report.validation.status == "pass":
        profile.validation = report.validation
        return profile, report

    revise_input = (f"# Current rules\n{rules.model_dump_json(indent=1)}\n\n"
                    f"# Validation result\n{report.failures}")
    revised_rules = call_structured(
        model, load("profile_revise"), revise_input, ProfileRules, PROFILE_SCHEMA_VERSION,
        transport=transport, cache_dir=cache_dir,
    )
    revised = to_profile(revised_rules, labels, doc.page_count)
    revised.body_style = profile.body_style
    revised_report = validate(doc, revised)
    log(f"profile: revised validation {revised_report.validation.status}, "
        f"coverage {revised_report.validation.coverage:.1%}")
    best, best_report = (revised, revised_report) if _better(revised_report, report) else (profile, report)
    best_report.validation.revised = best is revised
    best.validation = best_report.validation
    return best, best_report


def _better(a: Report, b: Report) -> bool:
    va, vb = a.validation, b.validation
    return (va.status == "pass", va.coverage) > (vb.status == "pass", vb.coverage)


def save_profile(profile: Profile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")


def load_profile(path: Path) -> Profile:
    return Profile.model_validate(json.loads(path.read_text(encoding="utf-8")))


