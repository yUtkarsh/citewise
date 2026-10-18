"""The agent's tools: JSON schemas Claude sees, and the dispatcher that runs them as
plain Python functions over one code graph and one fact store."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Callable

from app.agent import graph_tools as gt
from app.agent.compute import compute_equation, compute_table
from app.agent.facts import SOURCES, Fact, FactStore
from app.agent.results import Label, ToolResult
from app.agent.search import find_entry

PROCEDURE_TOOL = "plan_procedure"


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


ID = {"type": "string", "description": "A node id exactly as a tool returned it"}

TOOLS: list[dict] = [
    {"name": "find_entry",
     "description": "Find graph nodes for a phrase, symbol or number: exact id/symbol match, full-text and "
                    "vector search. Start here to get the ids the other tools need.",
     "input_schema": _obj({"query": {"type": "string"},
                           "labels": {"type": "array", "items": {"type": "string", "enum": [
                               "Clause", "Parameter", "Equation", "Table", "Term", "Figure"]},
                                      "description": "Only these node kinds (optional)"}}, ["query"])},
    {"name": "get_dependencies",
     "description": "Walk the graph from a node. upstream: what a parameter, equation, table or clause depends "
                    "on (equations that compute it, their inputs, tables that give it, clauses it requires). "
                    "downstream: what uses it (equations, the clauses that contain or cite them) - use this "
                    "for 'what is affected if X changes'.",
     "input_schema": _obj({"id": ID, "direction": {"type": "string", "enum": ["upstream", "downstream"]},
                           "depth": {"type": "integer", "description": "1 to 4, default 3"}}, ["id", "direction"])},
    {"name": "applicable_clauses",
     "description": "A clause and its sub-clauses, each with its conditions checked against the session facts "
                    "(applies / does not apply / undetermined).",
     "input_schema": _obj({"scope": {"type": "string", "description": "Clause id"}}, ["scope"])},
    {"name": "get_clause",
     "description": "A clause's text, page, references (typed; 'pdf link' for hyperlinks in the PDF), "
                    "equations with crops, tables, conditions, requirements, exceptions and commentary. "
                    "For a table id: its value columns, condition columns, each row's conditions, footnotes "
                    "and governing clause, but not its values (those come from compute).",
     "input_schema": _obj({"id": {"type": "string", "description": "Clause or table id"}}, ["id"])},
    {"name": "compute",
     "description": "The only way to get a number. Equation target: evaluates its SymPy form with the session "
                    "facts (save inputs first); reports missing inputs; refuses equations flagged high. Table "
                    "target: checks each row's conditions against the facts and returns the matching row's "
                    "values (pass row_id and a reason when the match is a description), interpolating between "
                    "rows when a footnote allows it.",
     "input_schema": _obj({
         "target": {"type": "string", "description": "Equation id or number, a parameter id, or a table id"},
         "solve_for": {"type": "string", "description": "Equation only: the symbol to solve for, if not the "
                                                        "left-hand side"},
         "row_id": {"type": "string", "description": "Table only: the row you matched"},
         "reason": {"type": "string", "description": "Table only: why that row matches the user's description"},
         "columns": {"type": "array", "items": {"type": "string"},
                     "description": "Table only: return only these header parameter ids"},
         "save_as_facts": {"type": "boolean",
                           "description": "Table only: store the row's values as facts (source matched_row) "
                                          "to use them as equation inputs"}}, ["target"])},
    {"name": "save_session_facts",
     "description": "Store values the user gave, accepted typical values (source 'assumed', only after the "
                    "user accepts them), or project choices (performance level, procedure). Changing an "
                    "existing value re-runs the last answer's computations.",
     "input_schema": _obj({"facts": {"type": "array", "items": _obj({
         "id": {"type": "string", "description": "Parameter id from the graph, or a short snake_case name for "
                                                 "a project choice"},
         "value": {"type": ["number", "string"]},
         "unit": {"type": ["string", "null"]},
         "source": {"type": "string", "enum": list(SOURCES)},
         "label": {"type": ["string", "null"],
                   "description": "How the code writes the quantity, when it is a condition's expression"}},
         ["id", "value", "source"])}}, ["facts"])},
    {"name": PROCEDURE_TOOL,
     "description": "For 'what is the procedure / what steps' questions: walks back from the goal and returns "
                    "ordered steps (dependencies first), each with its clause, table or equation ids, pages, "
                    "required inputs, which inputs the session facts already give, and the decisions that "
                    "change the path. Allows up to 20 tool calls for the question.",
     "input_schema": _obj({"goal": {"type": "string", "description": "A node id (best) or a phrase"}}, ["goal"])},
    {"name": "trace",
     "description": "Citations (clause, table, row, equation and page) and labels for the nodes your answer "
                    "relies on. Call it once before answering, with every node you cite.",
     "input_schema": _obj({"items": {"type": "array", "items": _obj({"id": ID}, ["id"])}}, ["items"])},
    {"name": "run_cypher",
     "description": "A read-only Cypher query on the code graph, for questions the other tools miss. At most 200 "
                    "rows; times out after a few seconds. Node labels: Clause, Equation, Parameter, Table, Row, "
                    "Footnote, Term, Figure, Condition, Requirement, Exception. Edges: PART_OF, REFERENCES "
                    "(type), CONTAINS, COMPUTES, USES, GIVES, HAS_ROW, HAS_FOOTNOTE, DEFINES, DEFINED_IN, "
                    "HAS_CONDITION, HAS_REQUIREMENT, HAS_EXCEPTION, COMMENTARY_ON, USES_TERM.",
     "input_schema": _obj({"query": {"type": "string"}}, ["query"])},
]
TOOL_NAMES = [t["name"] for t in TOOLS]


@dataclass
class Toolbox:
    graph: object
    store: FactStore
    encode: Callable[[list[str]], list[list[float]]] | None = None
    on_facts_changed: Callable[[list[tuple[Fact | None, Fact]]], ToolResult | None] | None = None
    procedure: Callable[..., ToolResult] | None = None
    calls: list[tuple[str, dict, ToolResult]] = field(default_factory=list)

    def run(self, name: str, args: dict) -> ToolResult:
        try:
            result = self._run(name, args or {})
        except Exception as exc:  # a tool bug must not end the conversation; Claude sees the error
            result = ToolResult(f"{name} failed: {type(exc).__name__}: {str(exc)[:200]}", {"args": args}, error=True)
        self.calls.append((name, args, result))
        return result

    def _run(self, name: str, a: dict) -> ToolResult:
        g = self.graph
        if name == "find_entry":
            return find_entry(g, a["query"], self.encode, labels=a.get("labels"))
        if name == "get_dependencies":
            depth = max(1, min(4, int(a.get("depth") or 3)))
            return gt.get_dependencies(g, a["id"], a.get("direction", "upstream"), depth)
        if name == "applicable_clauses":
            return gt.applicable_clauses(g, a["scope"], self.store.all())
        if name == "get_clause":
            return gt.get_clause(g, a["id"])
        if name == "compute":
            return self.compute(a)
        if name == "save_session_facts":
            return self.save_facts(a.get("facts") or [])
        if name == PROCEDURE_TOOL:
            if self.procedure is None:
                return ToolResult("plan_procedure is not available", {}, error=True)
            return self.procedure(g, a["goal"], self.store.all(), self.encode)
        if name == "trace":
            return gt.trace(g, a.get("items") or [])
        if name == "run_cypher":
            return gt.run_cypher(g, a["query"])
        return ToolResult(f"unknown tool {name!r}", {}, error=True)

    def compute(self, a: dict) -> ToolResult:
        target = a["target"]
        found = gt.find_node(self.graph, target)
        if found and found[0] == "Table":
            return compute_table(self.graph, target, self.store, a.get("row_id"), a.get("reason"),
                                 a.get("columns"), bool(a.get("save_as_facts")))
        if found and found[0] == "Row":
            table_id = target.rsplit("#r", 1)[0]
            return compute_table(self.graph, table_id, self.store, target, a.get("reason"), a.get("columns"),
                                 bool(a.get("save_as_facts")))
        return compute_equation(self.graph, target, self.store.all(), a.get("solve_for"))

    def save_facts(self, items: list[dict]) -> ToolResult:
        changes, saved, labels = [], [], []
        for item in items:
            node = gt.find_node(self.graph, str(item["id"]))
            fact = Fact(id=str(item["id"]), value=item["value"], unit=item.get("unit"),
                        source=item.get("source", "user"), label=item.get("label"),
                        kind="parameter" if node and node[0] == "Parameter" else "descriptor")
            previous = self.store.set(fact)
            saved.append(fact.display())
            if fact.source == "assumed":
                labels.append(Label(fact.id, "assumed", "a typical value the user accepted, not from the code"))
            if previous is not None and (previous.value, previous.unit) != (fact.value, fact.unit):
                changes.append((previous, fact))
        result = ToolResult(f"saved {len(saved)} facts" + (f", {len(changes)} changed" if changes else ""),
                            {"saved": saved, "changed": [f"{p.id}: {p.value} -> {f.value}" for p, f in changes],
                             "facts": [f.display() for f in self.store.all().values()]}, labels=labels)
        if changes and self.on_facts_changed is not None:
            replay = self.on_facts_changed(changes)
            if replay is not None:
                result.data["replay"] = replay.data
                result.summary += "; " + replay.summary
                result.touched = replay.touched
                result.labels += replay.labels
        return result


def args_text(args: dict) -> str:
    return json.dumps(args, ensure_ascii=False)[:160]
