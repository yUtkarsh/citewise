"""Command-line chat with one code graph (SPEC Build order, Phase 4).

    python -m app.agent.chat --code fema356 [--max-cost 3.00] [--script questions.txt] [--dry]

Prints each tool call, the node and edge ids it touched, the answer, and the cost of
each question. Facts and answer traces go to the memory graph (``memory``).
Commands: /facts, /set id=value [unit] (edits a fact and re-answers the last question,
like the facts panel), /new (a new session: no history, no facts), /cost, /quit.
A script file holds one question or command per line ('#' starts a comment).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading

from app.agent.facts import Fact, facts_block
from app.agent.loop import Agent, ToolCall, Turn
from app.agent.tools import Toolbox
from app.graph.connection import get_db

RULE = "-" * 78


class LazyEncoder:
    """Loads the local embedding model in the background (it takes a while to import)."""

    def __init__(self):
        self._encoder = None
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._load, daemon=True)
        self._thread.start()

    def _load(self):
        try:
            from app.graph.search_index import default_encoder
            self._encoder = default_encoder()
        except Exception as exc:  # no model cached: find_entry falls back to exact and full-text search
            self._error = exc

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self._thread.join()
        if self._encoder is None:
            raise RuntimeError(f"embedding model unavailable: {self._error}")
        return self._encoder.encode(texts)


def print_call(call: ToolCall) -> None:
    touched = call.result.touched.as_dict()
    nodes = [n["id"] for n in touched["nodes"]]
    print(f"  -> {call.name}({call.line().split('(', 1)[1].rsplit(') -> ', 1)[0]})")
    print(f"     <- {call.result.summary}")
    print(f"     touched nodes ({len(nodes)}): {', '.join(nodes)}")
    if touched["edges"]:
        print(f"     touched edges ({len(touched['edges'])}): {', '.join(touched['edges'])}")
    for label in call.result.labels:
        print(f"     label: {label.target} = {label.label} ({label.reason[:100]})")
    sys.stdout.flush()


def print_turn(turn: Turn, spent: float) -> None:
    print(RULE)
    print(turn.answer)
    print(RULE)
    for w in turn.warnings:
        print(f"warning: {w}")
    if turn.labels_added:
        print(f"checker added {len(turn.labels_added)} missing label(s)")
    totals = {k: sum(u.get(k, 0) for u in turn.usages) for k in
              ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}
    print(f"answer path: {', '.join(n['id'] for n in turn.path())}")
    print(f"calls: {len(turn.usages)} model, {len(turn.calls)} tools | tokens: in {totals['input_tokens']}, "
          f"cache read {totals['cache_read_input_tokens']}, cache write {totals['cache_creation_input_tokens']}, "
          f"out {totals['output_tokens']} | cost ${turn.cost:.4f} (session ${spent:.4f})")
    if turn.stopped:
        print(f"stopped: {turn.stopped}")
    sys.stdout.flush()


def parse_set(arg: str) -> Fact:
    name, _, rest = arg.partition("=")
    parts = rest.strip().split(maxsplit=1)
    value: float | str = parts[0]
    try:
        value = float(parts[0])
    except ValueError:
        pass
    return Fact(name.strip(), value, parts[1] if len(parts) > 1 else None, "user")


def build_agent(code: str, max_cost: float | None) -> Agent:
    from app.agent.procedure import plan_procedure
    from app.memory.graph import MemoryGraph

    db = get_db()
    graph = db.select_graph(f"code_{code}")
    docs = graph.ro_query("MATCH (d:Document) RETURN d.title, d.edition LIMIT 1").result_set
    document = " / ".join(x for x in docs[0] if x) if docs else code
    memory = MemoryGraph(db, f"code_{code}")
    toolbox = Toolbox(graph, memory, LazyEncoder(), procedure=plan_procedure)
    return Agent(toolbox, max_cost=max_cost, on_tool=print_call, document=document, memory=memory)


def handle(agent: Agent, line: str) -> bool:
    """Run one question or command; False ends the session."""
    line = line.strip()
    if not line or line.startswith("#"):
        return True
    if line in ("/quit", "/exit"):
        return False
    if line == "/facts":
        print(facts_block(agent.toolbox.store))
    elif line == "/cost":
        print(f"session cost ${agent.spent:.4f}" + (f" of ${agent.max_cost:.2f}" if agent.max_cost else ""))
    elif line == "/new":
        from app.memory.graph import MemoryGraph

        memory = MemoryGraph(get_db(), agent.memory.code) if agent.memory else None
        agent.new_session(store=memory, memory=memory)
        print(f"new session{' ' + memory.session if memory else ''}: no history, no facts")
    elif line.startswith("/set "):
        result = agent.toolbox.save_facts([parse_set(line[5:]).__dict__ | {"source": "user"}])
        print(result.summary)
        if "replay" in result.data and agent.last_replay is not None:
            print(">>> (fact edited) re-answering the last question it affects")
            print_turn(agent.answer_replay(agent.last_replay), agent.spent)
    else:
        print(f"\n>>> {line}")
        print_turn(agent.ask(line), agent.spent)
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Chat with a code graph from the command line")
    parser.add_argument("--code", required=True, help="code name, e.g. fema356 (graph code_fema356)")
    parser.add_argument("--max-cost", type=float, default=3.0, help="stop calling the model past this (USD)")
    parser.add_argument("--script", type=Path, help="run the questions in this file, one per line")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    agent = build_agent(args.code, args.max_cost)
    print(f"{args.code}: model {agent.model}, effort {agent.effort}, cost cap ${args.max_cost:.2f}")
    if args.script:
        for line in args.script.read_text(encoding="utf-8").splitlines():
            if not handle(agent, line):
                break
        return 0
    while True:
        try:
            line = input("\nyou> ")
        except (EOFError, KeyboardInterrupt):
            return 0
        if not handle(agent, line):
            return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
