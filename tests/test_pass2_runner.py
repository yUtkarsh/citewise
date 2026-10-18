"""Pass 2 runner with fake transports: retry, flagging, cache, backoff, concurrency,
resume, live writes and the cost log. Never calls the API."""
import asyncio
import json

import pytest
from pydantic import BaseModel

from app.ingest.pass2 import runner as rn
from app.ingest.pass2.jobs import Job
from app.ingest.pass2.validate import Issue
from app.llm import Reply
from app.prompts import Prompt


class Answer(BaseModel):
    value: int


def job(n=0, kind="clause"):
    return Job(id=f"{kind}:{n}", kind=kind, model_setting="model_clauses", prompt="test_prompt", output=Answer,
               text=f"question {n}")


def check(out: Answer):
    return [] if out.value > 0 else [Issue("source_quote", "value must be positive")]


def fake(*replies, usage=None, delay=0.0, track=None):
    calls = []

    async def send(spec):
        calls.append(spec)
        if track is not None:
            track["now"] += 1
            track["peak"] = max(track["peak"], track["now"])
        await asyncio.sleep(delay)
        if track is not None:
            track["now"] -= 1
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        text, stop = reply if isinstance(reply, tuple) else (reply, "end_turn")
        return Reply(text=text, model="claude-haiku-5-5", stop_reason=stop,
                     usage=usage or {"input_tokens": 1000, "output_tokens": 200})

    return send, calls


def make_runner(tmp_path, send, written=None, **kw):
    written = written if written is not None else []
    return rn.Runner(doc=None, model_for=lambda j: "claude-haiku-5-5", checks=lambda j: check,
                     write=written.append, state=rn.JobState(tmp_path / "state.jsonl"), transport=send,
                     cache_dir=tmp_path / "cache", log=lambda *_: None,
                     prompts={"test_prompt": Prompt("test_prompt", "v1", "system")}, **kw), written


def run(r, jobs):
    return asyncio.run(r.run(jobs))


def test_valid_answer_is_extracted_and_written(tmp_path):
    send, calls = fake('{"value": 3}')
    r, written = make_runner(tmp_path, send)
    [o] = run(r, [job()])
    assert o.status == "extracted" and o.output.value == 3 and len(calls) == 1
    assert written == [o]


def test_one_retry_with_the_issue_in_the_prompt(tmp_path):
    send, calls = fake('{"value": 0}', '{"value": 5}')
    r, _ = make_runner(tmp_path, send)
    [o] = run(r, [job()])
    assert o.status == "extracted" and o.call.attempts == 2
    assert "value must be positive" in calls[1].content[-1]["text"]


def test_still_invalid_after_retry_is_stored_flagged(tmp_path):
    send, calls = fake('{"value": 0}')
    r, written = make_runner(tmp_path, send)
    [o] = run(r, [job()])
    assert o.status == "flagged" and len(calls) == 2 and written == [o]
    assert [i.code for i in o.issues] == ["source_quote"]


def test_schema_failure_twice_is_flagged_invalid_output(tmp_path):
    send, _ = fake('{"value": "many"}')
    r, _ = make_runner(tmp_path, send)
    [o] = run(r, [job()])
    assert o.status == "flagged" and o.output is None and o.issues[0].code == "invalid_output"


def test_refusal_is_flagged_without_retry(tmp_path):
    send, calls = fake(("", "refusal"))
    r, _ = make_runner(tmp_path, send)
    [o] = run(r, [job()])
    assert o.status == "flagged" and o.issues[0].code == "refused" and len(calls) == 1


def test_second_run_is_served_from_cache_even_when_flagged(tmp_path):
    send, calls = fake('{"value": 0}')
    r, _ = make_runner(tmp_path, send)
    run(r, [job()])
    r2, _ = make_runner(tmp_path, send)
    [o] = run(r2, [job()])
    assert o.call.from_cache and o.status == "flagged" and len(calls) == 2  # no new calls


def test_images_are_built_only_on_a_cache_miss(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(rn, "content_blocks", lambda j, doc, save=None: built.append(j.id) or [{"type": "text", "text": j.text}])
    send, _ = fake('{"value": 1}')
    r, _ = make_runner(tmp_path, send)
    run(r, [job()])
    run(r, [job()])
    assert built == ["clause:0"]


def test_concurrency_is_capped(tmp_path):
    track = {"now": 0, "peak": 0}
    send, _ = fake('{"value": 1}', delay=0.01, track=track)
    r, written = make_runner(tmp_path, send, concurrency=3)
    run(r, [job(n) for n in range(10)])
    assert track["peak"] == 3 and len(written) == 10


def test_job_state_records_each_finished_job(tmp_path):
    send, _ = fake('{"value": 1}')
    r, _ = make_runner(tmp_path, send)
    run(r, [job(1), job(2)])
    lines = [json.loads(x) for x in (tmp_path / "state.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {x["job"] for x in lines} == {"clause:1", "clause:2"}
    assert set(rn.JobState(tmp_path / "state.jsonl").done) == {"clause:1", "clause:2"}


def test_cost_log_per_job_type(tmp_path):
    send, _ = fake('{"value": 0}', '{"value": 1}', usage={"input_tokens": 1_000_000, "output_tokens": 100_000})
    r, _ = make_runner(tmp_path, send)
    run(r, [job(1)])
    s = r.costs.summary()["by_job_type"]["clause"]
    assert s["api_calls"] == 2 and s["retries"] == 1 and s["input_tokens"] == 2_000_000
    assert s["cost_usd"] == pytest.approx(2 * (0.1 + 0.05))


def test_unknown_model_price_is_reported_not_guessed():
    log = rn.CostLog()
    assert rn.cost_of({"model": "some-new-model", "input_tokens": 10}) is None
    o = rn.Outcome(job(), "extracted", None, [], type("C", (), {"from_cache": False, "usages": [
        {"model": "some-new-model", "input_tokens": 5}], "attempts": 1, "refused": False})())
    log.add(o)
    assert log.summary()["models_without_price"] == ["some-new-model"]


# ---- backoff ------------------------------------------------------------------------------------

class FakeRateLimit(Exception):
    def __init__(self, retry_after):
        self.response = type("R", (), {"headers": {"retry-after": str(retry_after)}})()


def test_rate_limit_pauses_every_worker_for_retry_after():
    clock = {"t": 0.0}
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    gate = rn.Gate(clock=lambda: clock["t"], sleep=sleep)
    send, calls = fake(FakeRateLimit(30), '{"value": 1}')
    wrapped = rn.with_backoff(send, gate, log=lambda *_: None, retryable=lambda e: isinstance(e, FakeRateLimit),
                              jitter=lambda: 0.0)
    reply = asyncio.run(wrapped(object()))
    assert reply.text == '{"value": 1}' and len(calls) == 2
    assert slept == [30.0]  # retry-after beats the 2 s first backoff
    assert gate.resume_at == 30.0


def test_backoff_gives_up_after_max_attempts():
    clock = {"t": 0.0}

    async def sleep(seconds):
        clock["t"] += seconds

    gate = rn.Gate(clock=lambda: clock["t"], sleep=sleep)
    send, calls = fake(FakeRateLimit(0))
    wrapped = rn.with_backoff(send, gate, log=lambda *_: None, retryable=lambda e: isinstance(e, FakeRateLimit),
                              jitter=lambda: 0.0)
    with pytest.raises(FakeRateLimit):
        asyncio.run(wrapped(object()))
    assert len(calls) == rn.MAX_ATTEMPTS


def test_non_retryable_errors_raise_at_once():
    gate = rn.Gate()
    send, calls = fake(ValueError("400"))
    wrapped = rn.with_backoff(send, gate, log=lambda *_: None, retryable=lambda e: False)
    with pytest.raises(ValueError):
        asyncio.run(wrapped(object()))
    assert len(calls) == 1


def test_a_failed_request_is_logged_and_left_for_resume(tmp_path):
    send, _ = fake(ValueError("400 bad request"))
    r, written = make_runner(tmp_path, send)
    assert run(r, [job()]) == [] and written == []
    assert "clause:0" not in rn.JobState(tmp_path / "state.jsonl").done


def test_low_severity_notes_do_not_flag_the_result():
    low = Issue("unmapped", "a note", retry=False)
    assert rn.status_of([low, Issue("reference_unknown", "x", retry=False)]) == "extracted"
    assert rn.status_of([low, Issue("blank_cell", "x", retry=False)]) == "flagged"
    assert rn.status_of([Issue("uncertain_symbol_correction", "x", retry=False)]) == "flagged"
    assert rn.status_of([Issue("source_quote", "still wrong after the retry")]) == "flagged"
    assert rn.status_of([]) == "extracted"


def test_cost_cap_stops_new_calls_and_reports_the_rest(tmp_path):
    send, calls = fake('{"value": 1}', usage={"input_tokens": 1_000_000, "output_tokens": 0})  # $0.10 a call
    logs = []
    r, written = make_runner(tmp_path, send, concurrency=1, max_cost=0.25,
                             estimate_cost=lambda job, model: 0.10)
    r.log = logs.append
    run(r, [job(n) for n in range(5)])
    assert len(calls) == 2 and len(written) == 2  # a third call would pass $0.25
    assert r.stopped and len(r.not_run) == 3 and any("cost cap" in x for x in logs)
    assert r.spent() == pytest.approx(0.20)


def test_cached_jobs_run_past_the_cost_cap(tmp_path):
    send, calls = fake('{"value": 1}', usage={"input_tokens": 1_000_000, "output_tokens": 0})
    r, _ = make_runner(tmp_path, send, concurrency=1)
    run(r, [job(n) for n in range(3)])
    r2, written = make_runner(tmp_path, send, concurrency=1, max_cost=0.0, estimate_cost=lambda j, m: 1.0)
    run(r2, [job(n) for n in range(3)])
    assert len(written) == 3 and not r2.stopped and len(calls) == 3


def test_cached_first_attempt_failing_a_newer_check_gets_its_one_retry(tmp_path):
    send, calls = fake('{"value": 0}', '{"value": 4}')
    lenient, _ = make_runner(tmp_path, send)
    lenient.checks = lambda j: (lambda out: [])  # older checks accept value 0
    run(lenient, [job()])
    r, _ = make_runner(tmp_path, send)  # current checks reject it
    [o] = run(r, [job()])
    assert len(calls) == 2 and o.status == "extracted" and o.output.value == 4 and o.call.attempts == 2
    assert len(o.call.usages) == 1  # only the new attempt is billed to this run
    r2, _ = make_runner(tmp_path, send)
    run(r2, [job()])
    assert len(calls) == 2  # now cached with both attempts


def test_no_cache_asks_again_and_replaces_the_answer(tmp_path):
    send, calls = fake('{"value": 1}', '{"value": 7}')
    r, _ = make_runner(tmp_path, send)
    run(r, [job()])
    again, _ = make_runner(tmp_path, send, use_cache=False)
    [o] = run(again, [job()])
    assert len(calls) == 2 and o.output.value == 7 and not o.call.from_cache
    r3, _ = make_runner(tmp_path, send)
    [o3] = run(r3, [job()])
    assert o3.call.from_cache and o3.output.value == 7


def test_saved_images_are_the_bytes_sent(tmp_path, fema_profile):
    import base64
    import pymupdf
    from app.ingest.pass2 import images as img
    from tests.conftest import FIXTURES

    doc = pymupdf.open(FIXTURES / "text.pdf")
    j = job()
    j.images = [img.region("hash", 1, (50, 50, 300, 200), "clause region, page 1")]
    blocks = rn.content_blocks(j, doc, tmp_path / "debug")
    [saved] = list((tmp_path / "debug").rglob("*.png"))
    sent = next(b for b in blocks if b["type"] == "image")["source"]["data"]
    assert base64.standard_b64decode(sent) == saved.read_bytes()
