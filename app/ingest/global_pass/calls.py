"""Claude calls of the global pass: one small text-only call per item, cached by hash of
(input text, schema version, prompt version), validated with Pydantic and a
deterministic check (one retry), run concurrently under one cost cap for the whole run.
``estimate`` prices the calls a run would make without making any."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from app.ingest.global_pass.findings import GLOBAL_SCHEMA_VERSION
from app.ingest.pass2.runner import PRICING, cost_of
from app.ingest.pass2.validate import Issue
from app.llm import CACHE_DIR, CallResult, CallSpec, anthropic_async_transport, call_validated, is_cached
from app.prompts import load

CHARS_PER_TOKEN = 3.5
OUTPUT_TOKENS = 250  # a choice, a confidence and a one-line reason


@dataclass
class Ask:
    key: str  # the item this call decides
    prompt: str  # prompt name, e.g. global_commentary
    text: str  # the user message
    output: type[BaseModel]
    check: Callable[[BaseModel], list[Issue]]
    model: str
    max_tokens: int = 2000


@dataclass
class Caller:
    transport: object = None
    cache_dir: Path | None = None
    max_cost: float = 4.0
    concurrency: int = 10
    log: Callable[[str], None] = print
    spent: float = 0.0
    calls: int = 0
    cached: int = 0
    skipped: int = 0
    by_prompt: dict[str, float] = field(default_factory=dict)

    def estimated_cost(self, ask: Ask) -> float:
        prompt = load(ask.prompt)
        tokens_in = (len(prompt.text) + len(ask.text)) / CHARS_PER_TOKEN
        return cost_of({"model": ask.model, "input_tokens": int(tokens_in), "output_tokens": OUTPUT_TOKENS}) or 0.0

    def is_cached(self, ask: Ask) -> bool:
        return is_cached(ask.text, GLOBAL_SCHEMA_VERSION, load(ask.prompt), self.cache_dir or CACHE_DIR)

    def estimate(self, asks: list[Ask]) -> dict:
        todo = [a for a in asks if not self.is_cached(a)]
        return {"calls": len(asks), "cached": len(asks) - len(todo),
                "models": sorted({a.model for a in todo}),
                "estimated_usd": round(sum(self.estimated_cost(a) for a in todo), 4),
                "priced": all(a.model in PRICING for a in todo)}

    async def run(self, asks: list[Ask]) -> dict[str, CallResult]:
        gate = asyncio.Semaphore(self.concurrency)
        out: dict[str, CallResult] = {}

        async def one(ask: Ask) -> None:
            async with gate:
                cached = self.is_cached(ask)
                if not cached and self.spent + self.estimated_cost(ask) > self.max_cost:
                    self.skipped += 1
                    return
                prompt = load(ask.prompt)
                spec = CallSpec(model=ask.model, system=prompt.text, content=[{"type": "text", "text": ask.text}],
                                output=ask.output, max_tokens=ask.max_tokens, effort="low")
                result = await call_validated(spec, prompt, ask.text, GLOBAL_SCHEMA_VERSION, ask.check,
                                              self.transport or anthropic_async_transport,
                                              self.cache_dir or CACHE_DIR)
                cost = 0.0 if result.from_cache else sum(cost_of(u) or 0.0 for u in result.usages)
                self.spent += cost
                self.by_prompt[ask.prompt] = self.by_prompt.get(ask.prompt, 0.0) + cost
                self.calls += 0 if result.from_cache else 1
                self.cached += 1 if result.from_cache else 0
                out[ask.key] = result

        await asyncio.gather(*(one(a) for a in asks))
        if self.skipped:
            self.log(f"  cost cap ${self.max_cost:.2f} reached: {self.skipped} call(s) not made")
        return out

    def summary(self) -> dict:
        return {"calls": self.calls, "cached": self.cached, "skipped_by_cap": self.skipped,
                "cost_usd": round(self.spent, 4), "by_prompt": {k: round(v, 4) for k, v in self.by_prompt.items()}}


def accepted(result: CallResult | None):
    """The validated output, or None when the call failed, was refused or stays invalid."""
    if result is None or result.output is None or any(i.retry for i in result.issues):
        return None
    return result.output


def bad(detail: str) -> Issue:
    return Issue("invalid_output", detail)
