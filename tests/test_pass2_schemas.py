"""Pass 2 output schemas work as strict structured-output schemas."""
import pytest

from app.ingest.pass2.schemas import ClauseExtraction, DefinitionsPage, NotationPage, TableExtraction
from app.llm import strict_schema

UNSUPPORTED = {"default", "title", "minLength", "maxLength", "minimum", "maximum", "pattern", "format"}


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


@pytest.mark.parametrize("model", [NotationPage, DefinitionsPage, ClauseExtraction, TableExtraction])
def test_every_object_is_closed_and_fully_required(model):
    schema = strict_schema(model)
    for node in walk(schema):
        if node.get("type") == "object" and "properties" in node:
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        assert not UNSUPPORTED & {k for k, val in node.items() if not isinstance(val, dict)}
