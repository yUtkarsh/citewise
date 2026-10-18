"""Per-table source trust (SPEC Step 3b): clean text layer, suspect or missing text layer
with a second independent read, and a clean text layer that Claude disagrees with.
Never calls the API."""
import asyncio
import json

import pytest

from app.ingest.pass2 import validate as v
from app.ingest.pass2.records import RowRec, Skeleton2, TableRec, table_trust
from app.ingest.pass2.run_pass2 import Pass2Options, run_pass2
from app.ingest.pass2.schemas import CellRead, CellReads, CellValue, HeaderParameter, RowOut, TableExtraction
from app.llm import Reply

GRID = [["Type", "m"], ["Flexible", "1.5"], ["Stiff", "2.0"]]


def rows(cells=GRID, method="docling"):
    return [RowRec(i, 1, (100, 100 + 12 * i, 300, 110 + 12 * i), c, i == 0, method) for i, c in enumerate(cells)]


def table(trust="text", cells=GRID):
    return TableRec(id="Table 1", number="1", caption="Values", pages=[1], crops=[], header_crop=None,
                    rows=rows(cells), footnotes=[], clause_id=None, trust=trust)


def answer(*numbers):
    return TableExtraction(header_parameters=[HeaderParameter(col=1, parameter_id="m", header_text="m", units=None)],
                           footnote_rules=[], blank_cells=[], rows=[
        RowOut(row=i + 1, conditions=[], values=[CellValue(col=1, value=str(n), number=n, unit=None, formula=None)])
        for i, n in enumerate(numbers)])


# ---- deciding trust ------------------------------------------------------------------------

def test_clean_region_with_text_is_text_trust():
    assert table_trust(rows(), []) == ("text", "")
    elsewhere = [(1, (100, 500, 300, 510), ["private_use"]), (2, (100, 110, 300, 120), ["unmapped_font"])]
    assert table_trust(rows(), elsewhere)[0] == "text"  # flags outside the table's region and pages


def test_flagged_lines_in_the_region_make_it_image_trust():
    trust, reason = table_trust(rows(), [(1, (120, 112, 200, 122), ["unmapped_font", "private_use"])])
    assert trust == "image" and "unmapped_font" in reason


def test_missing_text_layer_makes_it_image_trust():
    assert table_trust(rows([["Type", "m"], ["", ""], ["", ""]]), [])[0] == "image"


# ---- the three cases ---------------------------------------------------------------------------

def test_text_clean_numbers_must_match_the_text_layer():
    t = table("text")
    assert not v.check_table(answer(1.5, 2.0), t, t.rows)
    issues = v.check_table(answer(1.5, 2.5), t, t.rows)  # Claude disagrees with a clean text layer
    assert [i.code for i in issues] == ["table_number_mismatch"] and v.needs_retry(issues)


def test_text_suspect_numbers_come_from_the_image_and_go_to_a_second_read():
    t = table("image")
    result = answer(1.5, 2.5)
    assert not v.check_table(result, t, t.rows)  # accepted from the image, no retry
    assert v.image_cells(result, t, t.rows) == [(2, 1, 2.5, "2.5")]  # only the unconfirmed cell


def test_second_read_agreement_is_a_note_and_disagreement_a_high_flag():
    wanted = [(2, 1, 2.5, "2.5")]
    agree = v.check_second_read(CellReads(cells=[CellRead(row=2, col=1, value="2.5", number=2.5)]), wanted)
    assert [(i.code, i.severity, i.retry) for i in agree] == [("image_read", "low", False)]
    differ = v.check_second_read(CellReads(cells=[CellRead(row=2, col=1, value="2.8", number=2.8)]), wanted)
    assert [(i.code, i.severity) for i in differ] == [("image_read_disagrees", "high")]
    missing = v.check_second_read(CellReads(cells=[]), wanted)
    assert v.needs_retry(missing)


# ---- end to end ------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def models(monkeypatch):
    from app import config
    monkeypatch.setenv("MODEL_TABLES", "claude-sonnet-5-5")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


class Sink:
    def __init__(self):
        self.results = []

    def notation(self, *a): ...

    def terms(self, *a): ...

    def result(self, outcome, ids):
        self.results.append(outcome)


def run(t, second_number, fema_profile, tmp_path):
    seen = []

    async def send(spec):
        seen.append(spec)
        if spec.output is TableExtraction:
            out = answer(1.5, 2.5)
        else:
            cells = json.loads(spec.content[-1]["text"])["cells"]
            out = CellReads(cells=[CellRead(row=c["row"], col=c["col"], value=str(second_number), number=second_number)
                                   for c in cells])
        return Reply(text=out.model_dump_json(), model="claude-sonnet-5-5", stop_reason="end_turn",
                     usage={"input_tokens": 100, "output_tokens": 50})

    profile = fema_profile.model_copy(update={"ranges": []})
    sink = Sink()
    report = asyncio.run(run_pass2(None, "hash", profile, Skeleton2("d", [], [t]), sink,
                                   Pass2Options(jobs={"tables"}), tmp_path / "state.jsonl", transport=send,
                                   cache_dir=tmp_path / "cache", log=lambda *_: None))
    return report, sink, seen


def test_image_trust_table_gets_one_second_read_that_agrees(fema_profile, tmp_path):
    report, sink, seen = run(table("image"), 2.5, fema_profile, tmp_path)
    assert [s.output.__name__ for s in seen] == ["TableExtraction", "CellReads"]
    second = json.loads(seen[1].content[-1]["text"])
    assert [(c["row"], c["col"]) for c in second["cells"]] == [(2, 1)]
    assert "2.5" not in seen[1].content[-1]["text"]  # the first read's value is not shown to the second
    kinds = {o.job.kind: o for o in sink.results}
    assert kinds["table"].status == "extracted" and kinds["table_cells"].status == "extracted"
    assert [i.code for i in kinds["table_cells"].issues] == ["image_read"]


def test_image_trust_disagreement_flags_the_table_high(fema_profile, tmp_path):
    _, sink, _ = run(table("image"), 2.8, fema_profile, tmp_path)
    second = next(o for o in sink.results if o.job.kind == "table_cells")
    assert second.status == "flagged" and second.issues[0].severity == "high"


def test_text_trust_table_never_gets_a_second_read(fema_profile, tmp_path):
    _, sink, seen = run(table("text"), 2.5, fema_profile, tmp_path)
    assert all(s.output is TableExtraction for s in seen)
    assert next(o for o in sink.results if o.job.kind == "table").status == "flagged"  # 2.5 is not in the text
