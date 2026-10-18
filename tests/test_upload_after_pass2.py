"""The upload's stages after Pass 2: search indexes, then the global pass under what Pass 2
left of the cost cap, its spend added to the upload's cost. A fake model and encoder on
the global pass's invented graph (no API calls)."""
from __future__ import annotations

import json

from app.api.ingest_routes import _after_pass2
from tests.test_global_pass import KeywordEncoder, fake_transport, graph  # noqa: F401  (fixture)


class FakeJob:
    pdf_hash = "abc123def456ffff"

    def __init__(self):
        self.stages, self.logs, self.events = [], [], []

    def set_stage(self, stage, message):
        self.stages.append(stage)

    def log(self, message):
        self.logs.append(message)

    def emit(self, event, data):
        self.events.append((event, data))


def test_indexes_then_the_global_pass_share_the_upload_cap(graph, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_MAX_COST", "3")
    job, seen = FakeJob(), []
    cost = _after_pass2(job, graph, 1.25, {"done": 10, "total": 10, "flagged": 0}, tmp_path,
                        transport=fake_transport(seen), encoder=KeywordEncoder(), cache_dir=tmp_path / "cache")
    assert job.stages == ["indexes", "global"]
    assert seen, "the global pass asked the (fake) model"
    assert any("of the cap left" in m and "$1.75" in m for m in job.logs)  # the estimate is logged first
    counts = [d for e, d in job.events if e == "counts"][-1]
    assert counts["cost_usd"] == round(1.25 + cost, 4) and counts["max_cost"] == 3.0 and counts["done"] == 10
    report = json.loads((tmp_path / "abc123def456_global.json").read_text(encoding="utf-8"))
    assert {"resolve", "commentary", "inferred", "gives", "checks"} <= set(report["parts"])
    vector = graph.ro_query("CALL db.indexes() YIELD label, types RETURN label, types").result_set
    assert any(label == "Clause" and "VECTOR" in str(types) for label, types in vector)


def test_no_budget_left_means_no_calls_but_the_checks_still_run(graph, tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_MAX_COST", "1")
    job, seen = FakeJob(), []
    cost = _after_pass2(job, graph, 1.0, {}, tmp_path, transport=fake_transport(seen), encoder=KeywordEncoder(),
                        cache_dir=tmp_path / "cache")
    assert cost == 0 and seen == []
    counts = [d for e, d in job.events if e == "counts"][-1]
    assert counts["global_skipped_by_cap"] > 0
    report = json.loads((tmp_path / "abc123def456_global.json").read_text(encoding="utf-8"))
    assert set(report["parts"]["checks"]) == {"dependency_cycle", "missing_reference", "undefined_parameter"}
