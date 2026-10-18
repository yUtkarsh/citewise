"""Cached, validated Claude calls.

Two paths share the cache: ``call_structured`` (sync, text only; the profile step) and
``call_validated`` (async, text and images, never raises on invalid output; Pass 2).

Every call is cached under ``data/cache/<prompt name>/<key>.json`` where the key is
the hash of (input text, schema version, prompt version). A cache hit never builds a
client. Output is parsed into a Pydantic model; on a validation error the call is
retried once with the error appended, then the error is raised.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

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


# ---- async calls with images (Pass 2) ------------------------------------------------
#
# Pass 2 calls carry images, run many at a time, and must not raise when a result stays
# invalid: it is stored as flagged instead. The cache key is the same hash as above, over
# the call's text plus its image keys.

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # `fallbacks: "default"` (Claude API only)
RETRY_NOTE = ("Your previous answer failed validation. Fix these problems and answer again in full; "
              "leave out anything you cannot support from the input:\n")


@dataclass
class CallSpec:
    model: str
    system: str
    content: list[dict] | Callable[[], list[dict]]  # user content blocks; a callable is built only on a cache miss
    output: type[BaseModel]
    max_tokens: int = 16000
    effort: str | None = "low"
    fallbacks: bool = False


@dataclass
class Reply:
    text: str
    model: str
    stop_reason: str
    usage: dict[str, int]


@dataclass
class CallResult:
    output: BaseModel | None
    raw: str | None
    issues: list[Any]
    attempts: int
    usages: list[dict]  # one per API attempt: model plus token counts
    from_cache: bool = False
    refused: bool = False
    parse_error: str | None = None
    retry_reasons: list[str] = field(default_factory=list)  # what made the first attempt retry


AsyncTransport = Callable[[CallSpec], Awaitable[Reply]]
Checker = Callable[[BaseModel], list]  # returns issues; an issue has .retry and .detail


_client = None


def _async_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.AsyncAnthropic(max_retries=0)  # the Pass 2 runner owns retries and backoff
    return _client


def _usage(message) -> dict[str, int]:
    u = message.usage
    return {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}


async def anthropic_async_transport(spec: CallSpec) -> Reply:
    """One streamed structured-output request at the spec's effort, with server-side
    fallback when the spec asks for it."""
    client = _async_client()
    output_config: dict = {"format": {"type": "json_schema", "schema": strict_schema(spec.output)}}
    if spec.effort:
        output_config["effort"] = spec.effort
    kwargs = dict(model=spec.model, max_tokens=spec.max_tokens,
                  system=[{"type": "text", "text": spec.system, "cache_control": {"type": "ephemeral"}}],
                  messages=[{"role": "user", "content": spec.content}], output_config=output_config)
    if spec.fallbacks:
        stream = client.beta.messages.stream(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
    else:
        stream = client.messages.stream(**kwargs)
    async with stream as s:
        message = await s.get_final_message()
    text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    return Reply(text=text, model=message.model, stop_reason=message.stop_reason or "", usage=_usage(message))


def _record_usage(reply: Reply) -> dict:
    return {"model": reply.model, **reply.usage}


async def call_validated(
    spec: CallSpec,
    prompt: Prompt,
    cache_text: str,
    schema_version: str,
    check: Checker,
    transport: AsyncTransport | None = None,
    cache_dir: Path = CACHE_DIR,
    use_cache: bool = True,
) -> CallResult:
    """Claude's answer for one Pass 2 job: parsed, checked, retried once with the issues
    that ask for a retry, and cached (flagged results too, so a resume never pays twice).
    A cache hit is re-checked; it is retried only if it was accepted on its first attempt
    and now fails a check (the checks changed), since every answer is owed one retry."""
    path = cache_path(cache_key(cache_text, schema_version, prompt), prompt, cache_dir)
    usages, raw, output, issues, error, refused = [], None, None, [], None, False
    first, prior = 1, []  # prior: usages of a cached first attempt (already paid for)
    if use_cache and path.is_file():  # use_cache=False asks again and replaces the cached answer
        record = json.loads(path.read_text(encoding="utf-8"))
        output, issues, error = _parse_and_check(record["raw"], spec.output, check)
        attempts = record.get("attempts", 1)
        unretried = attempts < 2 and not record.get("refused") and (error or any(i.retry for i in issues))
        if not unretried:
            return CallResult(output, record["raw"], issues, attempts, record.get("usages", []),
                              from_cache=True, refused=record.get("refused", False), parse_error=error,
                              retry_reasons=record.get("retry_reasons", []))
        # Accepted on its first attempt under older checks: it is owed its one retry.
        raw, prior, first = record["raw"], list(record.get("usages", [])), 2

    send = transport or anthropic_async_transport
    if callable(spec.content):
        spec = CallSpec(**{**spec.__dict__, "content": await asyncio.to_thread(spec.content)})
    attempt, problems = 0, []
    for attempt in range(first, 3):
        content = spec.content
        if attempt == 2:
            problems = [error] if error else [i.detail for i in issues if i.retry]
            content = spec.content + [{"type": "text", "text": RETRY_NOTE + "\n".join(f"- {p}" for p in problems)}]
        reply = await send(CallSpec(**{**spec.__dict__, "content": content}))
        usages.append(_record_usage(reply))
        if reply.stop_reason == "refusal":
            refused, raw, output, issues, error = True, None, None, [], "request refused"
            break
        raw = reply.text
        if reply.stop_reason == "max_tokens":
            output, issues, error = None, [], "output truncated at max_tokens"
        else:
            output, issues, error = _parse_and_check(raw, spec.output, check)
        if not error and not any(i.retry for i in issues):
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"prompt": prompt.name, "prompt_version": prompt.version, "model": spec.model,
              "schema_version": schema_version, "raw": raw if raw is not None else "", "attempts": attempt,
              "usages": prior + usages, "refused": refused, "retry_reasons": problems}
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return CallResult(output, raw, issues, attempt, usages, refused=refused, parse_error=error,
                      retry_reasons=problems)


def is_cached(cache_text: str, schema_version: str, prompt: Prompt, cache_dir: Path = CACHE_DIR) -> bool:
    return cache_path(cache_key(cache_text, schema_version, prompt), prompt, cache_dir).is_file()


def _parse_and_check(raw: str, output_type: type[BaseModel], check: Checker):
    if not raw:
        return None, [], "empty answer"
    try:
        output = output_type.model_validate_json(raw)
    except ValidationError as exc:
        return None, [], f"the answer does not match the schema: {exc}"
    return output, check(output), None
