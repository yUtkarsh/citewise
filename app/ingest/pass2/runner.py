"""Run Pass 2 jobs: real-time calls in parallel, backoff on rate limits, every result
checked and handed to the writer as soon as it is done, job state appended to a JSONL
file for resuming, and a token and cost log per job type (SPEC Step 3b, Delivery)."""
from __future__ import annotations

import asyncio
import base64
from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import random
import re
import threading
import time
from typing import Callable

import pymupdf

from app.config import ROOT
from app.ingest.pass2.jobs import Job
from app.ingest.pass2.schemas import PASS2_SCHEMA_VERSION
from app.ingest.pass2.validate import Issue
from app.llm import (CACHE_DIR, AsyncTransport, CallResult, CallSpec, Reply, _parse_and_check,
                     anthropic_async_transport, call_validated, is_cached)
from app.prompts import Prompt, load

PRICING = json.loads((ROOT / "app" / "pricing.json").read_text(encoding="utf-8"))
MAX_ATTEMPTS = 6  # per API request, on rate limits, overload, server and connection errors
BASE_DELAY, MAX_DELAY = 2.0, 60.0
DEFAULT_CONCURRENCY = 12
_RENDER_LOCK = threading.Lock()  # PyMuPDF documents are not thread-safe


@dataclass
class Outcome:
    job: Job
    status: str  # extracted / flagged
    output: object | None
    issues: list[Issue]
    call: CallResult | None  # None for jobs that needed no call


# ---- backoff ----------------------------------------------------------------------------

class Gate:
    """Shared pause: a rate limit on one worker holds every worker until it passes."""

    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep):
        self.resume_at = 0.0
        self.clock, self.sleep = clock, sleep

    async def wait(self) -> None:
        while (delay := self.resume_at - self.clock()) > 0:
            await self.sleep(delay)

    def pause(self, seconds: float) -> None:
        self.resume_at = max(self.resume_at, self.clock() + seconds)


def _retry_after(exc) -> float | None:
    response = getattr(exc, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _retryable(exc) -> bool:
    import anthropic

    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


def with_backoff(transport: AsyncTransport, gate: Gate, log=print, retryable=_retryable,
                 jitter=random.random) -> AsyncTransport:
    """Wrap a transport: on 429, 5xx (529 overloaded included) and connection errors,
    pause every worker for retry-after (or exponential backoff with jitter) and retry."""

    async def send(spec: CallSpec) -> Reply:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await gate.wait()
            try:
                return await transport(spec)
            except Exception as exc:
                if not retryable(exc) or attempt == MAX_ATTEMPTS:
                    raise
                delay = min(MAX_DELAY, BASE_DELAY * 2 ** (attempt - 1)) + jitter()
                delay = max(delay, _retry_after(exc) or 0.0)
                log(f"api: {type(exc).__name__}; retrying in {delay:.0f}s (attempt {attempt}/{MAX_ATTEMPTS})")
                gate.pause(delay)
        raise AssertionError("unreachable")

    return send


# ---- job state ---------------------------------------------------------------------------

class JobState:
    """Append-only JSONL: one line per finished job. The last line for a job wins."""

    def __init__(self, path: Path):
        self.path = path
        self.done: dict[str, dict] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self.done[entry["job"]] = entry

    def record(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.done[entry["job"]] = entry


# ---- pinned answers -------------------------------------------------------------------------

class PinStore:
    """The stored answer of each job, one JSON file per job. A stored answer that passes
    its checks, or that a reviewer approved, is kept: the job is not asked again. A new
    answer replaces a stored one only if the stored one is flagged and the new one passes.
    (The cache is keyed by the inputs; pins are keyed by the job, so a change elsewhere,
    such as a new notation slice, never silently replaces a good answer.)"""

    def __init__(self, folder: Path):
        self.folder = folder

    def _path(self, job_id: str) -> Path:
        return self.folder / (re.sub(r"[^A-Za-z0-9._-]+", "_", job_id) + ".json")

    def get(self, job_id: str) -> dict | None:
        path = self._path(job_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def put(self, job_id: str, record: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._path(job_id).write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")

    def approve(self, job_id: str, approved: bool = True) -> None:
        """For the review queue: an approved answer is never re-extracted."""
        record = self.get(job_id)
        if record is None:
            raise KeyError(job_id)
        self.put(job_id, {**record, "approved": approved})


def passes(job: Job, outcome: Outcome) -> bool:
    """Good enough to replace a stored answer: extracted, and for a strict job (one run
    with a reviewer hint) every equation verified by the LaTeX check. A table answer
    only needs every check that asks for a retry to pass (numbers, columns, rows):
    blank-cell readings always go to review, so they never stop a replacement."""
    if outcome.output is None:
        return False
    if job.kind == "table":
        return not any(i.retry for i in outcome.issues)
    if outcome.status != "extracted":
        return False
    return not (job.strict and any(i.code == "equation_latex" for i in outcome.issues))


# ---- cost log ------------------------------------------------------------------------------

def cost_of(usage: dict) -> float | None:
    price = PRICING.get(usage.get("model", ""))
    if not price:
        return None
    return (usage.get("input_tokens", 0) * price["input"] + usage.get("output_tokens", 0) * price["output"]
            + usage.get("cache_read_input_tokens", 0) * price["cache_read"]
            + usage.get("cache_creation_input_tokens", 0) * price["cache_write"]) / 1e6


@dataclass
class CostLog:
    kinds: dict[str, dict] = field(default_factory=lambda: defaultdict(lambda: defaultdict(float)))
    unknown_models: set[str] = field(default_factory=set)

    def add(self, outcome: Outcome) -> None:
        k = self.kinds[outcome.job.kind]
        k["jobs"] += 1
        k[outcome.status] += 1
        call = outcome.call
        if call is None:
            k["no_call"] += 1
            return
        if call.from_cache:
            k["cache_hits"] += 1
            return
        k["api_calls"] += len(call.usages)
        k["retries"] += max(0, call.attempts - 1)
        k["refused"] += call.refused
        for usage in call.usages:
            for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                k[name] += usage.get(name, 0)
            cost = cost_of(usage)
            if cost is None:
                self.unknown_models.add(usage.get("model", "?"))
            else:
                k["cost_usd"] += cost

    def summary(self) -> dict:
        kinds = {kind: {n: round(v, 4) if n == "cost_usd" else int(v) for n, v in values.items()}
                 for kind, values in self.kinds.items()}
        total = round(sum(v.get("cost_usd", 0.0) for v in self.kinds.values()), 4)
        return {"by_job_type": kinds, "total_cost_usd": total,
                "models_without_price": sorted(self.unknown_models)}


# ---- running -------------------------------------------------------------------------------

def save_images(job: Job, doc: pymupdf.Document, out_dir: Path) -> list[Path]:
    """Write the exact PNGs a job sends to ``out_dir/<job id>/`` (debugging; nothing is sent)."""
    folder = out_dir / re.sub(r"[^A-Za-z0-9._-]+", "_", job.id)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, ref in enumerate(job.images, 1):
        with _RENDER_LOCK:
            png = ref.png(doc)
        path = folder / f"{n:02d}_{re.sub(r'[^A-Za-z0-9._-]+', '_', ref.label)}.png"
        path.write_bytes(png)
        paths.append(path)
    return paths


def content_blocks(job: Job, doc: pymupdf.Document, save_dir: Path | None = None) -> list[dict]:
    """Each image follows a one-line label; the job text comes last. With ``save_dir`` the
    PNGs sent are also written there."""
    if save_dir is not None:
        save_images(job, doc, save_dir)
    blocks = []
    for ref in job.images:
        blocks.append({"type": "text", "text": f"[image: {ref.label}]"})
        with _RENDER_LOCK:
            png = ref.png(doc)
        data = base64.standard_b64encode(png).decode("ascii")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
    blocks.append({"type": "text", "text": job.text})
    return blocks


def outcome_of(job: Job, call: CallResult) -> Outcome:
    issues = list(call.issues)
    if call.refused:
        issues = [Issue("refused", "Claude declined the request", retry=False)]
    elif call.output is None:
        issues = [Issue("invalid_output", call.parse_error or "no valid answer", retry=False)]
    return Outcome(job, status_of(issues), call.output, issues, call)


def status_of(issues: list[Issue]) -> str:
    """Flagged when an issue can change a result (medium or high severity) or a retry did
    not fix it. Low-severity notes (unmapped items, references Claude named that Pass 1
    did not find) stay in the flags for the review queue without flagging the result."""
    return "flagged" if any(i.retry or i.severity in ("medium", "high") for i in issues) else "extracted"


@dataclass
class Runner:
    doc: pymupdf.Document
    model_for: Callable[[Job], str]
    checks: Callable[[Job], Callable]  # job -> checker for its output
    write: Callable[[Outcome], None]  # sync; called once per finished job, one at a time
    state: JobState
    costs: CostLog = field(default_factory=CostLog)
    transport: AsyncTransport | None = None
    concurrency: int = DEFAULT_CONCURRENCY
    effort: str = "low"
    fallbacks_for: Callable[[str], bool] = lambda model: False
    cache_dir: Path = CACHE_DIR
    log: Callable[[str], None] = print
    gate: Gate = field(default_factory=Gate)
    prompts: dict[str, Prompt] = field(default_factory=dict)
    max_cost: float | None = None  # USD; no new call starts once spend plus calls in flight would pass it
    estimate_cost: Callable[[Job, str], float] = lambda job, model: 0.0
    reserved: float = 0.0  # estimated cost of calls in flight
    stopped: bool = False  # the cost cap was reached
    not_run: list[str] = field(default_factory=list)
    use_cache: bool = True
    save_images_dir: Path | None = None  # write the images of every call sent (debugging)
    pins: PinStore | None = None  # stored answers, kept unless flagged and beaten by a passing one

    def pinned_outcome(self, job: Job, pin: dict) -> Outcome:
        output, issues, error = _parse_and_check(pin.get("raw") or "", job.output, self.checks(job))
        call = CallResult(output, pin.get("raw"), issues, pin.get("attempts", 1), [], from_cache=True,
                          refused=pin.get("refused", False), parse_error=error)
        return outcome_of(job, call)

    def pin(self, job: Job, outcome: Outcome) -> None:
        call = outcome.call
        self.pins.put(job.id, {"job": job.id, "raw": call.raw if call else None, "status": outcome.status,
                               "attempts": call.attempts if call else 0, "refused": bool(call and call.refused),
                               "prompt": f"{job.prompt}:{self.prompt(job.prompt).version}", "approved": False,
                               "issues": [i.as_dict() for i in outcome.issues][:20], "time": round(time.time(), 1)})

    def spent(self) -> float:
        return sum(v.get("cost_usd", 0.0) for v in self.costs.kinds.values())

    def admit(self, job: Job, model: str) -> float | None:
        """The cost reserved for this job, or None when the cap leaves no room for it.
        Cached jobs are free and always run."""
        if self.max_cost is None or (self.use_cache and is_cached(job.cache_text(), PASS2_SCHEMA_VERSION,
                                                                  self.prompt(job.prompt), self.cache_dir)):
            return 0.0
        estimate = self.estimate_cost(job, model)
        if self.stopped or self.spent() + self.reserved + estimate > self.max_cost:
            if not self.stopped:
                self.log(f"cost cap: ${self.spent():.2f} spent, ${self.reserved:.2f} in flight; the next call "
                         f"(about ${estimate:.2f}) would pass ${self.max_cost:.2f}; starting no new calls")
            self.stopped = True
            return None
        self.reserved += estimate
        return estimate

    def prompt(self, name: str) -> Prompt:
        if name not in self.prompts:
            self.prompts[name] = load(name)
        return self.prompts[name]

    async def run(self, jobs: list[Job]) -> list[Outcome]:
        send = with_backoff(self.transport or anthropic_async_transport, self.gate, self.log)
        semaphore = asyncio.Semaphore(self.concurrency)
        write_lock = asyncio.Lock()
        outcomes: list[Outcome] = []

        async def one(job: Job) -> None:
            stored = self.pins.get(job.id) if self.pins is not None else None
            kept = self.pinned_outcome(job, stored) if stored is not None else None
            if kept is not None and (stored.get("approved") or kept.status == "extracted"):
                await self.finish(kept, write_lock)  # a passing or approved answer is never asked again
                outcomes.append(kept)
                return
            async with semaphore:
                model = self.model_for(job)
                reserved = self.admit(job, model)
                if reserved is None:
                    self.not_run.append(job.id)
                    return
                spec = CallSpec(model=model, system=self.prompt(job.prompt).text,
                                content=lambda: content_blocks(job, self.doc, self.save_images_dir),
                                output=job.output,
                                max_tokens=job.max_tokens, effort=self.effort, fallbacks=self.fallbacks_for(model))
                try:
                    call = await call_validated(spec, self.prompt(job.prompt), job.cache_text(),
                                                PASS2_SCHEMA_VERSION, self.checks(job), send, self.cache_dir,
                                                self.use_cache)
                except Exception as exc:  # a request that failed after every backoff attempt
                    self.log(f"{job.id}: request failed: {type(exc).__name__}: {exc}")
                    self.costs.kinds[job.kind]["failed"] += 1
                    self.reserved -= reserved
                    return
            outcome = outcome_of(job, call)
            if self.pins is not None and outcome.call is not None:
                if kept is None or passes(job, outcome):
                    self.pin(job, outcome)
                else:  # the stored answer is flagged and so is the new one: keep the stored one
                    self.log(f"{job.id}: the new answer does not pass "
                             f"({', '.join(sorted({i.code for i in outcome.issues})) or 'no output'}); kept the stored one")
                    outcome = Outcome(job, kept.status, kept.output, kept.issues, call)  # the call's cost still counts
            await self.finish(outcome, write_lock)
            self.reserved -= reserved  # released only once its real cost is in the log
            outcomes.append(outcome)

        await asyncio.gather(*(one(job) for job in jobs))
        return outcomes

    async def finish(self, outcome: Outcome, write_lock: asyncio.Lock) -> None:
        async with write_lock:
            await asyncio.to_thread(self.write, outcome)
        self.costs.add(outcome)
        call = outcome.call
        self.state.record({
            "job": outcome.job.id, "kind": outcome.job.kind, "status": outcome.status,
            "issues": [i.as_dict() for i in outcome.issues][:20],
            "attempts": call.attempts if call else 0, "cached": bool(call and call.from_cache),
            "retry_reasons": [r[:300] for r in call.retry_reasons][:20] if call else [],
            "usages": call.usages if call and not call.from_cache else [], "time": round(time.time(), 1),
        })
        mark = "!" if outcome.status == "flagged" else "."
        self.log(f"{mark} {outcome.job.id} {outcome.status}"
                 + (f" ({', '.join(sorted({i.code for i in outcome.issues}))})" if outcome.issues else ""))

    async def skip(self, job: Job, write_lock: asyncio.Lock | None = None) -> Outcome:
        """A job that needs no call (a trivial clause): extracted with no facts."""
        outcome = Outcome(job, "extracted", None, [], None)
        await self.finish(outcome, write_lock or asyncio.Lock())
        return outcome
