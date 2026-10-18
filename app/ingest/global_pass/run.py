"""The global pass in SPEC order, minus embeddings (``app.graph.search_index``): entity
resolution, inferred edges, commentary linking, then the graph-wide checks last, so they
see the merged graph."""
from __future__ import annotations

from dataclasses import dataclass, field
import time

from app.ingest.global_pass.checks import run_checks
from app.ingest.global_pass.findings import write_findings

ORDER = ("resolve", "inferred", "commentary", "checks")


@dataclass
class GlobalOptions:
    parts: list[str] = field(default_factory=lambda: list(ORDER))
    dry_run: bool = False
    max_cost: float = 4.0
    concurrency: int = 10


async def run_global(graph, options: GlobalOptions, transport=None, encoder=None, cache_dir=None, log=print) -> dict:
    report: dict = {"parts": {}, "dry_run": options.dry_run}
    start = time.time()
    for part in [p for p in ORDER if p in options.parts]:
        if part == "checks":
            found = run_checks(graph)
            counts = {k: found.count(k) for k in sorted(found.kinds)}
            if not options.dry_run:
                write_findings(graph, found)
            log(f"checks: {counts}" + (" (dry run: nothing written)" if options.dry_run else " -> review queue"))
            report["parts"]["checks"] = counts
    report["run_s"] = round(time.time() - start, 1)
    return report
