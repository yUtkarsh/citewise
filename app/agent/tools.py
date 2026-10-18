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

SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉ₐₑₒₓₕₖₗₘₙₚₛₜ", "0123456789aeoxhklmnpst")


def symbol_key(name: str, case: bool = False) -> str:
    """'C_1', 'C₁' -> 'C1' (with case=False: 'c1')."""
    text = (name or "").translate(SUBSCRIPTS)
    return "".join(ch for ch in (text if case else text.lower()) if ch.isalnum())

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
                                          "to use them as equation inputs"},
         "resolve_inputs": {"type": "boolean",
                            "description": "Equation only: compute missing inputs from the equations and tables "
                                           "that supply them (recursively, when the facts leave no choice) and "
                                           "return every intermediate value with its source, or what is still "
                                           "missing and why"}}, ["target"])},
    {"name": "calculate",
     "description": "Only when compute cannot map a table's values itself (for example a value in a column "
                    "chosen by its header, or interpolation between columns): you cite every input and the "
                    "tool checks each citation against the graph, refuses on any mismatch, and does the "
                    "arithmetic. A cell input cites table, row, col, the value as printed and the header text "
                    "that justifies the column; a fact input cites a session fact. interpolate: lower and upper "
                    "are cells whose headers each hold the column's x; x is a cited input; permitted_by cites "
                    "the footnote, note row or clause that permits interpolation. formula: a SymPy expression "
                    "in the input names, with no numbers in it. State the cells you chose in the answer.",
     "input_schema": _obj({
         "operation": {"type": "string", "enum": ["interpolate", "formula"]},
         "inputs": {"type": "array", "items": _obj({
             "name": {"type": "string", "description": "An identifier for the input"},
             "source": {"type": "string", "enum": ["cell", "fact"]},
             "table": {"type": "string"}, "row": {"type": "string", "description": "Row id, e.g. 'Table 1-4#r6'"},
             "col": {"type": "integer"}, "value": {"type": "string", "description": "The cell value as printed"},
             "header": {"type": "string", "description": "The header text that justifies the column"},
             "fact": {"type": "string", "description": "Session fact id"}}, ["name", "source"])},
         "interpolate": _obj({"x": {"type": "string"}, "lower": {"type": "string"}, "upper": {"type": "string"}},
                             ["x", "lower", "upper"]),
         "permitted_by": _obj({"kind": {"type": "string", "enum": ["footnote", "note_row", "clause"]},
                               "id": {"type": "string"}}, ["kind", "id"]),
         "formula": {"type": "string"},
         "result": {"type": "string", "description": "Name of the result, e.g. a parameter id"}},
         ["operation", "inputs"])},
    {"name": "save_session_facts",
     "description": "Store values the user gave, accepted typical values (source 'assumed', only after the "
                    "user accepts them), or project choices (performance level, procedure). Changing an "
                    "existing value re-runs the last answer's computations.",
     "input_schema": _obj({"facts": {"type": "array", "items": _obj({
         "id": {"type": "string", "description": "Parameter id from the graph, or a short snake_case name for "
                                                 "a project choice"},
         "value": {"type": ["number", "string"]},
         "unit": {"type": ["string", "null"]},
         "source": {"type": "string", "enum": list(SOURCES),
                    "description": "matched_row only for a value compute mapped from a matched row; agent_selected "
                                   "for a value you picked (from an unmapped row, or from calculate)"},
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
    mapped: set[tuple[str, float]] = field(default_factory=set)  # (parameter, value) compute mapped from a row

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
            return gt.applicable_clauses(g, a["scope"], self.facts())
        if name == "get_clause":
            return gt.get_clause(g, a["id"])
        if name == "compute":
            return self.compute(a)
        if name == "save_session_facts":
            return self.save_facts(a.get("facts") or [])
        if name == PROCEDURE_TOOL:
            if self.procedure is None:
                return ToolResult("plan_procedure is not available", {}, error=True)
            return self.procedure(g, a["goal"], self.facts(), self.encode)
        if name == "trace":
            return gt.trace(g, a.get("items") or [])
        if name == "calculate":
            from app.agent.calculate import calculate
            return calculate(g, a, self.facts())
        if name == "run_cypher":
            return gt.run_cypher(g, a["query"])
        return ToolResult(f"unknown tool {name!r}", {}, error=True)

    def resolved_store(self):
        """The session facts with ids of merged parameters resolved to their survivors."""
        from app.agent.facts import ResolvedStore
        from app.graph.merges import merged_map
        return ResolvedStore(self.store, merged_map(self.graph))

    def facts(self) -> dict:
        return self.resolved_store().all()

    def compute(self, a: dict) -> ToolResult:
        target = a["target"]
        found = gt.find_node(self.graph, target)
        if found and found[0] in ("Table", "Row"):
            table_id = target if found[0] == "Table" else target.rsplit("#r", 1)[0]
            row_id = a.get("row_id") if found[0] == "Table" else target
            result = compute_table(self.graph, table_id, self.resolved_store(), row_id, a.get("reason"), a.get("columns"),
                                   bool(a.get("save_as_facts")))
            for v in (result.data.get("values") or []) if isinstance(result.data, dict) else []:
                if v.get("param") and v.get("number") is not None:
                    self.mapped.add((v["param"], float(v["number"])))
            return result
        if a.get("resolve_inputs"):
            from app.agent.chain import compute_chained
            return compute_chained(self.graph, target, self.facts(), a.get("solve_for"))
        return compute_equation(self.graph, target, self.facts(), a.get("solve_for"))

    def parameter_id(self, given: str) -> tuple[str, bool]:
        """(id, is_parameter): a parameter id as given, else the one parameter whose id,
        symbol or alias matches it with underscores and subscripts ignored ('C1' -> 'C_1',
        'Sa' -> 'S_a'): matching case first, then any case. An ambiguous or unknown name
        stays as given (a descriptor)."""
        node = gt.find_node(self.graph, given)
        if node and node[0] == "Parameter":
            return node[1].get("id") or given, True  # a merged id resolves to its survivor
        rows = self.graph.ro_query("MATCH (p:Parameter) RETURN p.id, p.symbol, p.aliases").result_set
        for case in (True, False):
            key = symbol_key(given, case)
            found = {pid for pid, sym, aliases in rows
                     if key in {symbol_key(pid, case), symbol_key(sym or "", case)}
                     | {symbol_key(a, case) for a in aliases or []}}
            if found:
                return (found.pop(), True) if len(found) == 1 else (given, False)
        return given, False

    def save_facts(self, items: list[dict]) -> ToolResult:
        changes, saved, labels = [], [], []
        downgraded = []
        for item in items:
            fid, is_param = self.parameter_id(str(item["id"]))
            if item.get("source") == "matched_row":  # only a value compute mapped from a matched row
                try:
                    number = float(item["value"])
                except (TypeError, ValueError):
                    number = None
                if (fid, number) not in self.mapped:
                    item = {**item, "source": "agent_selected"}
                    downgraded.append(fid)
            label = item.get("label") or (str(item["id"]) if fid != str(item["id"]) else None)
            fact = Fact(id=fid, value=item["value"], unit=item.get("unit"),
                        source=item.get("source", "user"), label=label,
                        kind="parameter" if is_param else "descriptor")
            previous = self.store.set(fact)
            saved.append(fact.display())
            if fact.source == "assumed":
                labels.append(Label(fact.id, "assumed", "a typical value the user accepted, not from the code"))
            if fact.source == "agent_selected":
                labels.append(Label(fact.id, "inputs selected by agent", "a value the agent picked, not mapped by compute"))
            if previous is not None and (previous.value, previous.unit) != (fact.value, fact.unit):
                changes.append((previous, fact))
        result = ToolResult(f"saved {len(saved)} facts" + (f", {len(changes)} changed" if changes else "")
                            + (f"; {', '.join(downgraded)} saved as agent_selected (compute did not map "
                               f"{'it' if len(downgraded) == 1 else 'them'})" if downgraded else ""),
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
