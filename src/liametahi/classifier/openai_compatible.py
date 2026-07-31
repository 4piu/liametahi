"""Chat-Completions-shaped adapter (spec §8.1).

Covers Ollama, llama.cpp server, LM Studio, vLLM, OpenAI, and any other
gateway that speaks `POST /chat/completions`. OpenRouter needs no special
handling here: it is OpenAI-compatible, so pointing `base_url` at it and
setting `model` to a namespaced `vendor/model` slug is purely
configuration (spec §8.1).

This adapter performs exactly one model call per `classify()` invocation
(spec §5.4's split-and-retry batching lives in `liametahi.evaluate`, one
level up) and does no semantic validation of what comes back — see
`liametahi.classifier` and `liametahi.prompt` module docstrings for why
that boundary is drawn where it is.
"""

import json
import time
from collections.abc import Sequence

import httpx

from liametahi.classifier import CandidatePayload, ClassifyOutcome, OfferedRule
from liametahi.config import ModelConfig
from liametahi.prompt import (
    RESPONSE_JSON_SCHEMA,
    SYSTEM_PROMPT,
    build_request_payload,
    parse_classification_response,
)

#: A JSON response is small; this bounds runaway generation without
#: needing a dedicated config knob (the spec does not define one).
_MAX_TOKENS = 4096

_STRUCTURED_OUTPUT_LADDER: tuple[str, ...] = ("json_schema", "json_object", "none")


class TransportError(Exception):
    """The HTTP call failed (transport error, or every structured-output
    level was rejected) after exhausting `max_retries`."""


class OpenAICompatibleClassifier:
    """`Classifier` implementation for Chat-Completions-shaped APIs."""

    def __init__(
        self, config: ModelConfig, *, client: httpx.Client | None = None
    ) -> None:
        if config.provider != "openai_compatible":
            raise ValueError(
                "OpenAICompatibleClassifier requires provider='openai_compatible', "
                f"got {config.provider!r}"
            )
        if not config.base_url:
            raise ValueError("openai_compatible requires 'base_url'")
        self._config = config
        headers = dict(config.extra_headers)
        if config.api_key:
            headers.setdefault("Authorization", f"Bearer {config.api_key}")
        self._client = client or httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers=headers,
        )

    def classify(
        self, candidates: Sequence[CandidatePayload], rules: Sequence[OfferedRule]
    ) -> ClassifyOutcome:
        requested_ids = [c.payload_id for c in candidates]
        user_content = json.dumps(build_request_payload(candidates, rules))
        last_error: Exception | None = None

        for level in self._levels_to_try():
            body: dict[str, object] = {
                "model": self._config.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": _MAX_TOKENS,
                "temperature": 0,
            }
            if level == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "classification_response",
                        "schema": RESPONSE_JSON_SCHEMA,
                        "strict": True,
                    },
                }
            elif level == "json_object":
                body["response_format"] = {"type": "json_object"}

            start = time.monotonic()
            try:
                response = self._post_with_retry(body)
            except TransportError as exc:
                last_error = exc
                continue
            latency_ms = int((time.monotonic() - start) * 1000)

            if response.status_code >= 400:
                # The provider rejected this structured-output level (or the
                # request in general); degrade to the next level down.
                last_error = TransportError(
                    f"HTTP {response.status_code} at structured_output={level}: "
                    f"{response.text[:200]}"
                )
                continue

            data = response.json()
            content = _extract_content(data)
            input_tokens, output_tokens = _extract_usage(data)
            parsed = parse_classification_response(content, requested_ids)
            return ClassifyOutcome(
                results=parsed.results,
                invalid=parsed.invalid,
                missing=parsed.missing,
                structured_output_level=level,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
            )

        raise TransportError(
            f"openai_compatible classify() failed at every structured-output "
            f"level: {last_error}"
        ) from last_error

    def _levels_to_try(self) -> tuple[str, ...]:
        configured = self._config.structured_output
        if configured == "auto":
            return _STRUCTURED_OUTPUT_LADDER
        return (configured,)

    def _post_with_retry(self, body: dict[str, object]) -> httpx.Response:
        """Retry a transport-level failure (connection reset, timeout) up
        to `max_retries` times (spec §6: "transport errors only, never a
        rejected response" — an HTTP error response is returned to the
        caller above, not retried here)."""
        attempts = self._config.max_retries + 1
        last_exc: Exception | None = None
        for _ in range(attempts):
            try:
                return self._client.post("/chat/completions", json=body)
            except httpx.TransportError as exc:
                last_exc = exc
                continue
        assert last_exc is not None
        raise TransportError(str(last_exc)) from last_exc


def _extract_content(data: object) -> str:
    if not isinstance(data, dict):
        return "{}"
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "{}"
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        return "{}"
    content = message.get("content")
    return content if isinstance(content, str) else "{}"


def _extract_usage(data: object) -> tuple[int | None, int | None]:
    if not isinstance(data, dict):
        return None, None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None, None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    input_tokens = prompt_tokens if isinstance(prompt_tokens, int) else None
    output_tokens = completion_tokens if isinstance(completion_tokens, int) else None
    return input_tokens, output_tokens
