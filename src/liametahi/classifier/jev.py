"""Jev (`provider: jev`) adapter (jev-provider-plan §1, §5, §10, §11).

Jev is not a chat model: a decision model reached through
`POST /v1/systemone`, answering one of three fixed, structured question
shapes (`noul`/`choice`/`score`) with a resolved `value` plus a calibrated
`confidence`, never generated text. There is no prompt to compile and no
JSON-schema negotiation ladder (`prompt.build_response_schema` is chat-only,
per its own docstring) — a processor's `type`/`criteria`/`options`/`levels`
map onto a `noul`/`choice`/`score` request directly, and the response's
`value`/`confidence` are passed straight through into a `ProcessorAnswer`
with no folding or thresholding of any kind (jev-provider-plan §5).

`mails_per_request` must be `1` for `provider: jev` (enforced at config
load, `config.ModelConfig._validate_provider_requirements`): jev answers
one candidate per HTTP call, not a batch. When a candidate needs more than
one jev-backed processor answered, `classify()` makes one HTTP call per
processor for that candidate (still "one call per candidate" for any
single processor, per jev-provider-plan §5) and merges the results into
one `Classification` per candidate. A processor call that never succeeds
is simply absent from that candidate's `answers` map (per
`classifier.Classification`'s own contract: "a name absent from the map
means not answered this round") rather than voiding the whole candidate —
a candidate with at least one successful answer is still reported in
`results`; a candidate with zero successful answers goes to `invalid`.

**Retry policy is a deliberate deviation from `openai_compatible.py`'s
"transport errors only, never a rejected response" policy** (spec §6):
jev is a single-purpose decision API behind a rate limiter, not a chat
completion request whose rejection typically means the request itself was
malformed. A `429`/`529` here means "the service is temporarily out of
capacity, try again shortly" — exactly the shape `max_retries` already
exists to absorb for transport errors — so this adapter retries those two
status codes up to `max_retries` times with the same backoff-free retry
loop shape `openai_compatible.py` uses for transport errors. Every other
4xx/5xx (`401` unauthorized, `422` malformed request, ...) fails
immediately with no retry: retrying a `401`/`422` can never succeed and
only wastes the retry budget that would otherwise be available for a
genuine `429`/`529`.

The wire shape below (`POST` body: `model`/`type`/`criteria`-or-`options`-
or-`levels`/`input`; response body: `{"value": ..., "confidence": ...}`)
is this project's own design choice, since jev-provider-plan does not pin
one — documented as an explicit assumption in the final report.
"""

import time
from collections.abc import Sequence

import httpx

from liametahi.classifier import (
    CandidatePayload,
    Classification,
    ClassifyOutcome,
    OfferedProcessor,
)
from liametahi.config import ModelConfig
from liametahi.logging import get_logger
from liametahi.rules import ProcessorAnswer

logger = get_logger(__name__)

#: Status codes worth retrying (jev-provider-plan's deliberate deviation,
#: see module docstring): temporary capacity problems, not bad requests.
_RETRYABLE_STATUS_CODES = frozenset({429, 529})


class TransportError(Exception):
    """A jev HTTP call failed after exhausting `max_retries` (for a
    retryable status) or failed immediately on a non-retryable one."""


class JevClassifier:
    """`Classifier` implementation for `provider: jev`."""

    def __init__(self, config: ModelConfig, *, client: httpx.Client | None = None):
        if config.provider != "jev":
            raise ValueError(
                f"JevClassifier requires provider='jev', got {config.provider!r}"
            )
        if not config.base_url:
            raise ValueError("jev requires 'base_url'")
        if config.mails_per_request != 1:
            raise ValueError("jev requires mails_per_request == 1")
        self._config = config
        self._endpoint_url = config.base_url
        headers = dict(config.extra_headers)
        headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = client or httpx.Client(
            timeout=config.timeout_seconds, headers=headers
        )

    def classify(
        self,
        candidates: Sequence[CandidatePayload],
        processors: Sequence[OfferedProcessor],
    ) -> ClassifyOutcome:
        results: list[Classification] = []
        invalid: list[str] = []
        total_latency_ms = 0
        call_count = 0

        for candidate in candidates:
            answers: dict[str, ProcessorAnswer] = {}
            for processor in processors:
                call_count += 1
                start = time.monotonic()
                try:
                    answer = self._call_one(candidate, processor)
                except TransportError as exc:
                    logger.debug(
                        "jev classify: candidate=%s processor=%s failed: %s",
                        candidate.payload_id,
                        processor.name,
                        exc,
                    )
                    continue
                finally:
                    total_latency_ms += int((time.monotonic() - start) * 1000)
                answers[processor.name] = answer
            if answers:
                results.append(
                    Classification(
                        payload_id=candidate.payload_id, answers=answers, reason=None
                    )
                )
            else:
                invalid.append(candidate.payload_id)

        return ClassifyOutcome(
            results=tuple(results),
            invalid=tuple(invalid),
            missing=(),
            structured_output_level="json_schema",
            input_tokens=None,
            output_tokens=None,
            latency_ms=total_latency_ms,
        )

    def _call_one(
        self, candidate: CandidatePayload, processor: OfferedProcessor
    ) -> ProcessorAnswer:
        body: dict[str, object] = {
            "model": self._config.model,
            "type": processor.type,
            "input": dict(candidate.fields),
        }
        if processor.type == "noul":
            body["criteria"] = dict(processor.criteria or {})
        elif processor.type == "choice":
            body["options"] = dict(processor.options or {})
        else:
            body["levels"] = list(processor.levels or ())

        response = self._post_with_retry(body)
        data = response.json()
        if not isinstance(data, dict) or "value" not in data:
            raise TransportError(
                f"malformed jev response for processor {processor.name!r}: "
                f"{response.text[:200]!r}"
            )
        value = data["value"]
        if not isinstance(value, bool | float | int | str):
            raise TransportError(
                f"jev response 'value' has an unsupported type for "
                f"processor {processor.name!r}: {value!r}"
            )
        if isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        confidence_raw = data.get("confidence")
        if confidence_raw is not None and not isinstance(confidence_raw, int | float):
            raise TransportError(
                f"jev response 'confidence' is not numeric for processor "
                f"{processor.name!r}: {confidence_raw!r}"
            )
        confidence = float(confidence_raw) if confidence_raw is not None else None
        return ProcessorAnswer(value=value, confidence=confidence)

    def _post_with_retry(self, body: dict[str, object]) -> httpx.Response:
        """POST once, retrying only `429`/`529` up to `max_retries` times
        (module docstring's deliberate deviation from
        `openai_compatible.py`'s transport-errors-only policy). A
        transport-level exception (connection reset, timeout) is also
        retried, matching every other adapter's behaviour for those.
        Every other status code -- including every other 4xx/5xx -- fails
        immediately, no retry."""
        attempts = self._config.max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.post(self._endpoint_url, json=body)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.debug(
                    "jev: transport attempt %d/%d failed: %s", attempt, attempts, exc
                )
                continue
            if response.status_code < 400:
                return response
            if response.status_code not in _RETRYABLE_STATUS_CODES:
                raise TransportError(
                    f"HTTP {response.status_code} (non-retryable): "
                    f"{response.text[:200]}"
                )
            last_exc = TransportError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
            logger.debug(
                "jev: retryable HTTP %d on attempt %d/%d",
                response.status_code,
                attempt,
                attempts,
            )
        assert last_exc is not None
        raise TransportError(str(last_exc)) from last_exc
