"""Official `anthropic` SDK adapter (spec §8.2).

This needs its own adapter because the Anthropic Messages API is not
Chat-Completions-shaped:

- the system prompt is the top-level `system` parameter, not a
  `messages` entry;
- `max_tokens` is required;
- the response is a `content[]` list of typed blocks, not
  `choices[0].message.content`;
- structured output is `output_config.format` with a JSON Schema;
- and — the trap for any shared request builder — `temperature` is
  **rejected with a 400** on current models, so this adapter must never
  send it, unlike `openai_compatible` which may.

Like `openai_compatible.py`, this adapter performs exactly one model
call per `classify()` invocation and does no semantic validation of the
response; that happens once, in `liametahi.evaluate` (see the
`liametahi.classifier` and `liametahi.prompt` module docstrings).
"""

import json
import time
from collections.abc import Sequence

import anthropic

from liametahi.classifier import CandidatePayload, ClassifyOutcome, OfferedRule
from liametahi.config import ModelConfig
from liametahi.logging import get_logger
from liametahi.prompt import (
    RESPONSE_JSON_SCHEMA,
    SYSTEM_PROMPT,
    build_request_payload,
    parse_classification_response,
)

logger = get_logger(__name__)

_MAX_TOKENS = 4096


class TransportError(Exception):
    """The Anthropic API call failed after the SDK's own retry budget
    (and, for `structured_output=auto`, after falling back to an
    unstructured request) was exhausted."""


class AnthropicClassifier:
    """`Classifier` implementation for the official `anthropic` SDK."""

    def __init__(
        self, config: ModelConfig, *, client: anthropic.Anthropic | None = None
    ) -> None:
        if config.provider != "anthropic":
            raise ValueError(
                "AnthropicClassifier requires provider='anthropic', "
                f"got {config.provider!r}"
            )
        self._config = config
        if client is not None:
            self._client = client
        else:
            self._client = anthropic.Anthropic(
                api_key=config.api_key,
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
                default_headers=dict(config.extra_headers) or None,
            )

    def classify(
        self, candidates: Sequence[CandidatePayload], rules: Sequence[OfferedRule]
    ) -> ClassifyOutcome:
        requested_ids = [c.payload_id for c in candidates]
        user_content = json.dumps(build_request_payload(candidates, rules))

        configured = self._config.structured_output
        want_schema = configured in ("auto", "json_schema")
        # The ladder (try schema, degrade on rejection) is specifically
        # what `structured_output: auto` means (spec §8.1). A *fixed*
        # `json_schema` is an explicit user request for validated output;
        # silently downgrading it on rejection would defeat the point of
        # pinning it, so a fixed level that gets rejected is a hard
        # failure, exactly like `openai_compatible`'s `_levels_to_try()`
        # for a non-auto configuration.
        allow_fallback = configured == "auto"

        start = time.monotonic()
        level = "json_schema" if want_schema else "none"
        try:
            message = self._create(user_content, with_schema=want_schema)
        except anthropic.APIStatusError as exc:
            if not want_schema or not allow_fallback:
                raise TransportError(str(exc)) from exc
            # The provider rejected structured output for this model;
            # degrade to an unstructured request (spec §8.1's ladder,
            # applied to the one structured level Anthropic offers).
            logger.debug("classify: structured_output=json_schema rejected: %s", exc)
            try:
                message = self._create(user_content, with_schema=False)
                level = "none"
            except anthropic.APIStatusError as exc2:
                raise TransportError(str(exc2)) from exc2
        except anthropic.APIConnectionError as exc:
            raise TransportError(str(exc)) from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        content = _extract_text(message)
        input_tokens, output_tokens = _extract_usage(message)
        parsed = parse_classification_response(content, requested_ids)
        logger.debug(
            "classify: structured_output=%s accepted in %dms "
            "(input_tokens=%s output_tokens=%s)",
            level,
            latency_ms,
            input_tokens,
            output_tokens,
        )
        return ClassifyOutcome(
            results=parsed.results,
            invalid=parsed.invalid,
            missing=parsed.missing,
            structured_output_level=level,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

    def _create(
        self, user_content: str, *, with_schema: bool
    ) -> anthropic.types.Message:
        """One Messages request.

        Deliberately no `temperature`: current Anthropic models reject it
        with a 400 (spec §8.2). Sampling parameters are per-adapter and
        never live in the shared request builder. `system` is a top-level
        parameter here, not a message role.
        """
        messages: list[anthropic.types.MessageParam] = [
            {"role": "user", "content": user_content}
        ]
        if not with_schema:
            return self._client.messages.create(
                model=self._config.model,
                max_tokens=_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
        return self._client.messages.create(
            model=self._config.model,
            max_tokens=_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_config={
                "format": {"type": "json_schema", "schema": RESPONSE_JSON_SCHEMA}
            },
        )


def _extract_text(message: anthropic.types.Message) -> str:
    parts = [block.text for block in message.content if block.type == "text"]
    return "".join(parts) if parts else "{}"


def _extract_usage(message: anthropic.types.Message) -> tuple[int | None, int | None]:
    usage = message.usage
    if usage is None:
        return None, None
    return usage.input_tokens, usage.output_tokens
