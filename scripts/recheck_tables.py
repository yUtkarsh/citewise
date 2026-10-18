"""Re-run the current table checks on the stored table answers. Sends no API calls.

For each table job, the answer in use is the pinned one (``data/jobs/<hash>_pass2_pins``),
or without a pin the cached one, or without either (single-part tables) the rows the
graph holds. Prints the tables whose answer now fails a check
that asks for a retry, grouped by reason, and writes a JSON report.

    python -m scripts.recheck_tables data/fema356.pdf [--reason value_column] [--flag-graph]

``--flag-graph`` also flags, in the graph, every table whose stored rows hold values in a
column no GIVES edge names (answers older than the column check), so answers label them
unreviewed until they are re-extracted.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import sys

import pymupdf

from app.config import ROOT
from app.graph.connection import get_db
from app.graph.schema import graph_name
from app.ingest.pass2.jobs import page_job, row_bands, table_jobs
from app.ingest.pass2.records import from_graph
from app.ingest.pass2.run_pass2 import merge_notation, range_pages
from app.ingest.pass2.runner import JobState, Runner
from app.ingest.pass2.schemas import PASS2_SCHEMA_VERSION, TableExtraction
from app.ingest.pass2 import validate as v
from app.llm import cache_key, cache_path
from app.prompts import load
from app.profile.build import file_hash, load_profile, profile_path

JOBS_DIR = ROOT / "data" / "jobs"
REPORT_DIR = ROOT / "data" / "reports"


async def _no_calls(spec):
    raise RuntimeError("recheck sends no API calls")


def notation_dict(doc, pdf_hash: str, profile, scratch: Path):
    """The notation dictionary from the cached notation pages (it shapes the table jobs' inputs)."""
    runner = Runner(doc=doc, model_for=lambda job: "cached", checks=lambda job: (lambda out: []),
                    write=lambda outcome: None, state=JobState(scratch), transport=_no_calls, log=lambda m: None)
    jobs = [page_job(doc, pdf_hash, p, "notation") for p in range_pages(profile, "notation")]
    outcomes = asyncio.run(runner.run(jobs))
    notation, _ = merge_notation(outcomes, lambda m: None)
    return notation


def stored_raw(job, pins_dir: Path) -> tuple[str | None, str]:
    from app.ingest.pass2.runner import PinStore
    pin = PinStore(pins_dir).get(job.id)
    if pin and pin.get("raw"):
        return pin["raw"], "pin"
    path = cache_path(cache_key(job.cache_text(), PASS2_SCHEMA_VERSION, load(job.prompt)), load(job.prompt))
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8")).get("raw"), "cache"
    return None, "none"


def flag_stray_columns(graph) -> list[str]:
    """Flag tables whose rows hold values in a column that no GIVES edge names."""
    flagged = []
    tables = graph.ro_query("MATCH (t:Table) OPTIONAL MATCH (t)-[e:GIVES]->() "
                            "RETURN t.id, collect(coalesce(e.cols, [e.col]))").result_set
    for tid, col_lists in tables:
        cols = {c for cs in col_lists for c in (cs or []) if c is not None}
        rows = graph.ro_query("MATCH (:Table {id: $id})-[:HAS_ROW]->(r:Row) WHERE r.values IS NOT NULL "
                              "RETURN r.id, r.values", {"id": tid}).result_set
        stray = [rid.rsplit("#", 1)[-1] for rid, values in rows if {x["col"] for x in json.loads(values)} - cols]
        if stray:
            flag = json.dumps(v.Issue("value_column", f"rows {stray[:20]} hold values in columns no header "
                                                      f"parameter names").as_dict(), ensure_ascii=False)
            graph.query("MATCH (t:Table {id: $id}) SET t.status = 'flagged', "
                        "t.flags = [f IN coalesce(t.flags, []) WHERE NOT f CONTAINS 'value_column'] + [$flag]",
                        {"id": tid, "flag": flag})
            flagged.append(tid)
    return flagged


def answer_from_graph(graph, table) -> TableExtraction | None:
    """The answer the graph holds for a table (for jobs whose stored answer is gone)."""
    import json as _json

    from app.ingest.pass2.schemas import BlankCell, CellValue, HeaderParameter, RowOut

    gives = graph.ro_query("MATCH (:Table {id: $id})-[e:GIVES]->(p:Parameter) "
                           "RETURN p.id, coalesce(e.cols, [e.col]), e.header_text, e.units", {"id": table.id}).result_set
    rows = graph.ro_query("MATCH (:Table {id: $id})-[:HAS_ROW]->(r:Row) WHERE r.values IS NOT NULL "
                          "RETURN r.id, r.conditions, r.values, r.blank_cells", {"id": table.id}).result_set
    if not rows:
        return None
    headers = [HeaderParameter(col=c, parameter_id=p, header_text=h or p, units=u)
               for p, cols, h, u in gives for c in cols if c is not None]
    out, blanks = [], []
    for rid, conds, values, blank in rows:
        index = int(rid.rsplit("#r", 1)[1])
        vals = [CellValue(col=x["col"], value=x.get("value") or "", number=x.get("number"), unit=x.get("unit"),
                          formula=x.get("formula")) for x in _json.loads(values or "[]")]
        out.append(RowOut(row=index, conditions=_json.loads(conds or "[]"), values=vals))
        blanks += [BlankCell(**b) for b in _json.loads(blank or "[]")]
    return TableExtraction(header_parameters=headers, rows=out, footnote_rules=[], blank_cells=blanks)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--reason", help="only report this issue code")
    parser.add_argument("--flag-graph", action="store_true", help="flag tables with stray value columns in the graph")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    pdf = args.pdf.resolve()
    pdf_hash = file_hash(pdf)
    doc = pymupdf.open(pdf)
    profile = load_profile(profile_path(pdf_hash))
    name = graph_name(pdf)
    skel = from_graph(get_db().select_graph(name), name)
    notation = notation_dict(doc, pdf_hash, profile, REPORT_DIR / "_recheck_state.jsonl")
    pins_dir = JOBS_DIR / f"{pdf_hash[:12]}_pass2_pins"

    failing, sources, reasons = {}, Counter(), Counter()
    graph = get_db().select_graph(name)
    for table in skel.tables:
        jobs = table_jobs(table, skel, notation)
        for job in jobs:
            raw, source = stored_raw(job, pins_dir)
            out = None
            if raw:
                try:
                    out = TableExtraction.model_validate_json(raw)
                except Exception:
                    out = None
            if out is None and len(jobs) == 1:  # no stored answer: check what the graph holds
                out, source = answer_from_graph(graph, table), "graph"
            sources[source if out is not None else "none"] += 1
            if out is None:
                continue
            issues = [i for i in v.check_table(out, job.table, job.rows, row_bands(doc, job.rows))
                      if i.retry and (not args.reason or i.code == args.reason)]
            if issues:
                failing[job.id] = {"source": source, "issues": [i.as_dict() for i in issues]}
                reasons.update({i.code for i in issues})

    report = {"tables_checked": sum(sources.values()), "answer_sources": dict(sources),
              "failing": len(failing), "failing_by_reason": dict(reasons), "jobs": failing}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"{pdf_hash[:12]}_table_recheck.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"tables checked: {report['tables_checked']} {dict(sources)}; failing: {len(failing)} {dict(reasons)}")
    for job_id, item in failing.items():
        print(f"  {job_id} ({item['source']}): " + "; ".join(i["detail"][:140] for i in item["issues"][:3]))
    print(f"report: {out_path.relative_to(ROOT)}")
    if args.flag_graph:
        flagged = flag_stray_columns(get_db().select_graph(name))
        print(f"flagged in the graph ({len(flagged)}): {', '.join(flagged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
