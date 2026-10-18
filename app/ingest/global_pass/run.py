"""The global pass in SPEC order, minus embeddings (``app.graph.search_index``): entity
resolution, commentary linking, inferred edges, then the graph-wide checks last, so they
see the merged and linked graph. All Claude calls share one cost cap; a dry run prices
them (cached calls are free) and writes nothing."""
from __future__ import annotations

from dataclasses import dataclass, field
import time

from app.config import require_model
from app.ingest.global_pass import commentary, gives, inferred, resolve
from app.ingest.global_pass.calls import Caller
from app.ingest.global_pass.checks import run_checks
from app.ingest.global_pass.edges import clear_inferred, write_edges
from app.ingest.global_pass.findings import FindingSet, write_findings
from app.ingest.global_pass.similarity import load_vectors

ORDER = ("resolve", "commentary", "inferred", "gives", "checks")


@dataclass
class GlobalOptions:
    parts: list[str] = field(default_factory=lambda: list(ORDER))
    dry_run: bool = False
    max_cost: float = 4.0
    concurrency: int = 10


def _findings(kind: str, items) -> FindingSet:
    found = FindingSet({kind})
    for f in items:
        found.add(f)
    return found


def still_unlinked_commentary(graph) -> int:
    return graph.ro_query("MATCH (c:Clause {stream: 'commentary'}) WHERE NOT (c)-[:COMMENTARY_ON]->() "
                          "RETURN count(c)").result_set[0][0]


async def run_global(graph, options: GlobalOptions, transport=None, encoder=None, cache_dir=None, log=print) -> dict:
    caller = Caller(transport, cache_dir, options.max_cost, options.concurrency, log)
    dry = options.dry_run
    report: dict = {"parts": {}, "dry_run": dry, "max_cost": options.max_cost}
    start = time.time()

    def get_encoder():
        nonlocal encoder
        if encoder is None:
            from app.graph.search_index import default_encoder
            encoder = default_encoder()
        return encoder

    for part in [p for p in ORDER if p in options.parts]:
        if part == "resolve":
            model = require_model("model_global")
            plan = resolve.plan(graph, model)
            out = {"clusters": len(plan.clusters), "exact_clusters": len(plan.exact), "adjudicated": len(plan.asks)}
            if dry:
                out |= caller.estimate(plan.asks)
            else:
                results = await caller.run(plan.asks)
                merges, findings = resolve.apply(graph, plan, results, model)
                counts = write_findings(graph, _findings("merge", findings))
                if any(m.get("merged") for m in merges):
                    from app.graph.search_index import embed_label
                    embed_label(graph, "Parameter", get_encoder(), log=log)
                out |= {"merged": sum(1 for m in merges if m.get("merged")),
                        "merges": [{k: m.get(k) for k in ("survivor", "removed", "method", "confidence", "reason",
                                                          "merged", "why")} for m in merges],
                        "to_review": counts["merge"]}
            log(f"resolve: {out if dry else {k: v for k, v in out.items() if k != 'merges'}}")
            report["parts"]["resolve"] = out

        elif part == "commentary":
            model = require_model("model_clauses")
            clauses = commentary.load_clauses(graph)
            asks, cands = commentary.build_asks(graph, clauses, load_vectors(graph, "Clause"), model)
            out = {"unmatched": len(cands), "with_candidates": len(asks)}
            if dry:
                out |= caller.estimate(asks)
            else:
                results = await caller.run(asks)
                edges, findings = commentary.decide(results, cands, clauses, model)
                cleared = clear_inferred(graph, "COMMENTARY_ON", "Clause", "Clause")
                write_edges(graph, edges)
                counts = write_findings(graph, _findings("commentary_link", findings))
                out |= {"linked": len(edges), "to_review": counts["commentary_link"],
                        "with_proposal": sum(1 for f in findings if f.proposal),
                        "still_unlinked": still_unlinked_commentary(graph), "replaced_edges": cleared}
            log(f"commentary: {out}")
            report["parts"]["commentary"] = out

        elif part == "inferred":
            model = require_model("model_global")
            clauses = commentary.load_clauses(graph)
            asks, plan = inferred.build_asks(graph, clauses, load_vectors(graph, "Clause"), get_encoder(), model)
            out = {"tables": sum(1 for k in plan if k.startswith("Table:")),
                   "parameters": sum(1 for k in plan if k.startswith("Parameter:")), "calls": len(asks)}
            if dry:
                out |= caller.estimate(asks)
            else:
                results = await caller.run(asks)
                edges, findings = inferred.decide(results, plan, model)
                cleared = sum(clear_inferred(graph, "REFERENCES", "Clause", dst) for dst in ("Table", "Parameter"))
                write_edges(graph, edges)
                counts = write_findings(graph, _findings("inferred_edge", findings))
                out |= {"edges": len(edges), "items_linked": len({(e["dst_label"], e["dst"]) for e in edges}),
                        "to_review": counts["inferred_edge"], "replaced_edges": cleared,
                        "stored": [f"{e['src']} -{e['props']['type']}-> {e['dst']} ({e['props']['confidence']})"
                                   for e in edges]}
            log(f"inferred: {out if dry else {k: v for k, v in out.items() if k != 'stored'}}")
            report["parts"]["inferred"] = out

        elif part == "gives":
            model = require_model("model_global")
            plan = gives.plan(graph, get_encoder(), model)
            out = {"tables": len(plan.items), "columns": sum(len(i.columns) for i in plan.items),
                   "columns_with_candidates": sum(1 for i in plan.items for c in i.columns if c.candidates),
                   "calls": len(plan.asks)}
            if dry:
                out |= caller.estimate(plan.asks)
            else:
                results = await caller.run(plan.asks)
                edges, findings = gives.decide(plan, results, model)
                cleared = clear_inferred(graph, "GIVES", "Table", "Parameter")
                write_edges(graph, edges)
                counts = write_findings(graph, _findings("inferred_gives", findings))
                out |= {"edges": len(edges), "to_review": counts["inferred_gives"], "replaced_edges": cleared,
                        "stored": [f"{e['src']} [{e['props']['via']}] -GIVES-> {e['dst']} ({e['props']['confidence']})"
                                   for e in edges]}
            log(f"gives: {out if dry else {k: v for k, v in out.items() if k != 'stored'}}")
            report["parts"]["gives"] = out

        elif part == "checks":
            found = run_checks(graph)
            counts = {k: found.count(k) for k in sorted(found.kinds)}
            if not dry:
                write_findings(graph, found)
            log(f"checks: {counts}" + (" (dry run: nothing written)" if dry else " -> review queue"))
            report["parts"]["checks"] = counts

    if dry:
        est = [p for p in report["parts"].values() if "estimated_usd" in p]
        report["estimated_usd"] = round(sum(p["estimated_usd"] for p in est), 4)
        log(f"estimated cost of uncached calls: ${report['estimated_usd']:.2f} (cap ${options.max_cost:.2f})")
    report["calls"] = caller.summary()
    report["run_s"] = round(time.time() - start, 1)
    if not dry:
        log(f"calls: {report['calls']}")
    return report
