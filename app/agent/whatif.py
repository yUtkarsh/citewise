"""What-if answers (SPEC Step 6): when a fact changes, the stored path of the last answer
is re-run with the new facts (the same tools with the same arguments, so the same
matched rows), and a one-line change summary is built in code from the old and the new
results, for example:

    Changed: W 1500 → 2000 kips. V 1069.2 → 1425.6 kips; same path (Equation 3-10).
"""
from __future__ import annotations

from app.agent.compute import fmt
from app.agent.facts import Fact
from app.agent.results import ToolResult, Touched

REPLAYABLE = ("compute", "applicable_clauses", "plan_procedure", "trace")
PATH_LABELS = ("Equation", "Table", "Row")


def _value(v) -> str:
    try:
        return fmt(float(v))
    except (TypeError, ValueError):
        return str(v)


def outcome(name: str, result: ToolResult) -> dict:
    """The part of a tool result a what-if compares."""
    d = result.data if isinstance(result.data, dict) else {}
    if name != "compute":
        return {"summary": result.summary}
    if "result" in d:
        r = d["result"]
        return {"kind": "equation", "equation": d.get("equation"), "id": r["id"], "value": r["value"],
                "unit": r.get("unit"), "computed": True}
    if "values" in d:
        return {"kind": "table", "table": d.get("table"), "row": d.get("row"),
                "values": {v["param"]: v["number"] for v in d["values"] if v.get("param")},
                "interpolated": bool(d.get("interpolation"))}
    return {"kind": "equation" if "equation" in d else "other", "equation": d.get("equation"), "computed": False,
            "missing": [m["id"] for m in d.get("missing", [])], "summary": result.summary}


def path_nodes(results: list[ToolResult]) -> list[str]:
    seen = {}
    for r in results:
        for label, nid in r.touched.nodes:
            if label in PATH_LABELS:
                seen[nid] = None
    return list(seen)


def fact_changes(changes: list[tuple[Fact | None, Fact]]) -> str:
    parts = []
    for old, new in changes:
        unit = f" {new.unit}" if new.unit else ""
        parts.append(f"{new.id} {_value(old.value) if old else 'unset'} → {_value(new.value)}{unit}")
    return ", ".join(parts)


def result_changes(old: list[dict], new: list[dict]) -> list[str]:
    out = []
    for a, b in zip(old, new):
        if b.get("kind") == "equation":
            unit = f" {b['unit']}" if b.get("unit") else ""
            if a.get("computed") and b.get("computed") and abs(a["value"] - b["value"]) > 1e-12 * max(1, abs(a["value"])):
                out.append(f"{b['id']} {fmt(a['value'])} → {fmt(b['value'])}{unit}")
            elif not a.get("computed") and b.get("computed"):
                out.append(f"{b['id']} now {fmt(b['value'])}{unit}")
            elif a.get("computed") and not b.get("computed"):
                out.append(f"{a['id']} no longer computed ({b.get('summary', '')[:80]})")
        elif b.get("kind") == "table":
            if a.get("row") != b.get("row"):
                out.append(f"{b['table']}: {a.get('row')} → {b.get('row')}")
            changed = [f"{p} {_value(a['values'].get(p))} → {_value(v)}" for p, v in b["values"].items()
                       if a.get("values", {}).get(p) != v]
            out += changed[:6]
    return out


def change_line(changes, old_out, new_out, old_path, new_path) -> str:
    results = result_changes(old_out, new_out)
    if old_path == new_path:
        path = f"same path ({', '.join(old_path[:4])})" if old_path else "same path"
    else:
        added = [n for n in new_path if n not in old_path]
        dropped = [n for n in old_path if n not in new_path]
        path = "path changed: " + "; ".join(x for x in (f"now uses {', '.join(added)}" if added else "",
                                                        f"no longer {', '.join(dropped)}" if dropped else "") if x)
    return f"Changed: {fact_changes(changes)}. " + (", ".join(results) if results else "no result changed") + \
        f"; {path}."


def replay(toolbox, last_calls: list[tuple[str, dict, ToolResult]], changes) -> ToolResult | None:
    """Re-run the replayable calls of the last answer with the current facts. None when
    the last answer used no replayable tool."""
    calls = [(n, a, r) for n, a, r in last_calls if n in REPLAYABLE]
    if not calls:
        return None
    new_results = [toolbox._run(n, a) for n, a, _ in calls]
    old_out = [outcome(n, r) for n, _, r in calls]
    new_out = [outcome(n, r) for (n, _, _), r in zip(calls, new_results)]
    line = change_line(changes, old_out, new_out, path_nodes([r for _, _, r in calls]), path_nodes(new_results))
    t = Touched()
    labels = []
    for r in new_results:
        for key in r.touched.nodes:
            t.node(*key)
        for e in r.touched.edges:
            t.edges[e] = None
        labels += r.labels
    data = {"change_line": line, "results_changed": bool(result_changes(old_out, new_out)),
            "rerun": [{"tool": n, "args": a, "summary": r.summary, "data": r.data}
                      for (n, a, _), r in zip(calls, new_results)]}
    return ToolResult(line, data, t, labels,
                      reruns=[(n, a, r) for (n, a, _), r in zip(calls, new_results)])
