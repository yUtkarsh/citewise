"""Cached, validated Claude calls.

Every call is cached under ``data/cache/<prompt name>/<key>.json`` where the key is
the hash of (input text, schema version, prompt version). A cache hit never builds a
client. Output is parsed into a Pydantic model; on a validation error the call is
retried once with the error appended, then the error is raised.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import ROOT
from app.prompts import Prompt

CACHE_DIR = ROOT / "data" / "cache"
MAX_TOKENS = 32000

Model = TypeVar("Model", bound=BaseModel)
# (model, system, user_text, output type) -> raw JSON text from Claude
Transport = Callable[[str, str, str, type[BaseModel]], str]


class ClaudeOutputError(RuntimeError):
    """Claude's output failed validation twice."""


def cache_key(user_text: str, schema_version: str, prompt: Prompt) -> str:
    payload = "\x1f".join([user_text, schema_version, prompt.name, prompt.version])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_path(key: str, prompt: Prompt, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / prompt.name / f"{key}.json"


def anthropic_transport(model: str, system: str, user_text: str, output: type[BaseModel]) -> str:
    """One structured-output request; returns the JSON text of the reply."""
    import anthropic  # imported here so cache hits and tests never need the SDK client

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_text}],
        output_config={"format": {"type": "json_schema", "schema": strict_schema(output)}},
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise ClaudeOutputError(f"request refused: {message.stop_details}")
    if message.stop_reason == "max_tokens":
        raise ClaudeOutputError("output truncated at max_tokens")
    return next(block.text for block in message.content if block.type == "text")


def strict_schema(output: type[BaseModel]) -> dict:
    """JSON schema for structured outputs: every object closed, every field required,
    and the keywords structured outputs do not support removed."""
    schema = output.model_json_schema()
    unsupported = {"default", "title", "minLength", "maxLength", "minimum", "maximum",
                   "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems", "pattern", "format"}

    def walk(node):
        if isinstance(node, dict):
            for key in unsupported & node.keys():
                if not isinstance(node[key], dict) or key == "default":
                    node.pop(key)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


def call_structured(
    model: str,
    prompt: Prompt,
    user_text: str,
    output: type[Model],
    schema_version: str,
    transport: Transport | None = None,
    cache_dir: Path = CACHE_DIR,
    check: Callable[[Model], list[str]] | None = None,
) -> Model:
    """Return Claude's validated answer to ``user_text`` under ``prompt``, cached.

    ``check`` runs deterministic checks that need context the schema lacks; any
    errors it returns are treated like validation errors (one retry)."""
    path = cache_path(cache_key(user_text, schema_version, prompt), prompt, cache_dir)
    if path.is_file():
        return output.model_validate_json(json.loads(path.read_text(encoding="utf-8"))["raw"])

    send = transport or anthropic_transport
    text = user_text
    for attempt in (1, 2):
        raw = send(model, prompt.text, text, output)
        try:
            result = output.model_validate_json(raw)
            errors = check(result) if check else []
        except ValidationError as exc:
            errors = [str(exc)]
        if errors:
            message = "\n".join(errors)
            if attempt == 2:
                raise ClaudeOutputError(f"{prompt.name}: invalid output after retry:\n{message}")
            text = (f"{user_text}\n\nYour previous answer failed validation. Fix these errors "
                    f"and answer again in full:\n{message}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"prompt": prompt.name, "prompt_version": prompt.version, "model": model,
                  "schema_version": schema_version, "raw": raw}
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        return result
    raise AssertionError("unreachable")
