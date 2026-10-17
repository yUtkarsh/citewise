"""Cached, validated Claude calls without the API: a fake transport stands in."""
import json

import pytest
from pydantic import BaseModel

from app import llm
from app.prompts import Prompt, load


class Answer(BaseModel):
    value: int


PROMPT = Prompt(name="test_prompt", version="v1", text="system")


def transport_returning(*replies):
    calls = []

    def send(model, system, user_text, output):
        calls.append(user_text)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    return send, calls


def test_cache_key_depends_on_input_schema_and_prompt_version():
    base = llm.cache_key("text", "s1", PROMPT)
    assert base == llm.cache_key("text", "s1", PROMPT)
    assert base != llm.cache_key("text2", "s1", PROMPT)
    assert base != llm.cache_key("text", "s2", PROMPT)
    assert base != llm.cache_key("text", "s1", Prompt("test_prompt", "v2", "system"))


def test_second_call_is_served_from_cache(tmp_path):
    send, calls = transport_returning('{"value": 3}')
    first = llm.call_structured("m", PROMPT, "q", Answer, "s1", transport=send, cache_dir=tmp_path)
    second = llm.call_structured("m", PROMPT, "q", Answer, "s1", transport=send, cache_dir=tmp_path)
    assert first.value == second.value == 3
    assert len(calls) == 1
    cached = json.loads(next((tmp_path / "test_prompt").glob("*.json")).read_text(encoding="utf-8"))
    assert cached["prompt_version"] == "v1"


def test_invalid_output_is_retried_once_with_the_error(tmp_path):
    send, calls = transport_returning('{"value": "x"}', '{"value": 5}')
    assert llm.call_structured("m", PROMPT, "q", Answer, "s1", transport=send, cache_dir=tmp_path).value == 5
    assert len(calls) == 2 and "failed validation" in calls[1]


def test_still_invalid_after_retry_raises_and_caches_nothing(tmp_path):
    send, calls = transport_returning('{"value": "x"}')
    with pytest.raises(llm.ClaudeOutputError):
        llm.call_structured("m", PROMPT, "q", Answer, "s1", transport=send, cache_dir=tmp_path)
    assert len(calls) == 2
    assert not (tmp_path / "test_prompt").exists()


def test_deterministic_check_triggers_the_retry(tmp_path):
    send, calls = transport_returning('{"value": 1}', '{"value": 2}')
    check = lambda a: [] if a.value == 2 else ["value must be 2"]  # noqa: E731
    result = llm.call_structured("m", PROMPT, "q", Answer, "s1", transport=send, cache_dir=tmp_path, check=check)
    assert result.value == 2 and "value must be 2" in calls[1]


def test_strict_schema_closes_objects_and_requires_all_fields():
    class Inner(BaseModel):
        a: int = 1

    class Outer(BaseModel):
        inner: Inner
        note: str | None = None

    schema = llm.strict_schema(Outer)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"inner", "note"}
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False and "default" not in inner["properties"]["a"]


def test_profile_prompts_are_versioned():
    for name in ("profile_ranges", "profile_rules", "profile_revise"):
        prompt = load(name)
        assert prompt.version.startswith("v") and prompt.text.strip()
