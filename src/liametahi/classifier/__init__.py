"""Classifier protocol and payload/response models (contracts Sec 5.3).

This is the fixed cross-unit interface between the evaluate phase (this
unit) and the two provider adapters (`openai_compatible.py`,
`anthropic.py`, also this unit). It was originally reproduced verbatim
inside `tests/fakes/fake_classifier.py` because this module did not yet
exist when Unit 1 landed; per contracts Sec 5.4 that fake now imports these
definitions from here instead of declaring local copies.

**Validation against the offered rule and candidate vocabulary happens in
the caller (see `liametahi.evaluate`), never in an adapter.** An adapter's
`classify()` only has to do transport, structured-output negotiation, and
JSON parsing; it is free to hand back a `Classification` that names a rule
or candidate id it was never offered; the model is untrusted and the
architecture assumes every adapter response is hostile until validated.

There is no model-reported confidence score anywhere in this contract
(spec §5.3): the model's output is yes/no/unsure (`matches` /
absent-from-`matches` / `needs_content`), never a number to threshold.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OfferedRule:
    """One rule offered to the model: its id and the free-form `llm`
    condition description a user wrote in the config."""

    rule_id: str
    description: str


@dataclass(frozen=True, slots=True)
class CandidatePayload:
    """One candidate as sent to the model: a batch-local id, its already
    capped-and-sanitised metadata fields (spec Sec 5.1, Sec 5.2), and the rule
    ids offered specifically for it."""

    payload_id: str  # batch-local, "c1".."cN"
    fields: Mapping[str, object]  # already capped and sanitised by prompt.py
    offered: tuple[str, ...]  # rule ids offered for THIS candidate


@dataclass(frozen=True, slots=True)
class Classification:
    """One raw, unvalidated per-candidate model response item.

    The model's output is yes/no/unsure, not a score (spec §5.3): `matches`
    lists the rule ids the model confidently says yes to; an offered rule
    id absent from `matches` is a confident no; `needs_content: true`
    marks the *whole item* unsure. Not yet validated against anything —
    `matches` may name a rule id that was never offered for this
    candidate, or repeat one.
    """

    payload_id: str
    matches: tuple[str, ...]
    needs_content: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class ClassifyOutcome:
    """The full result of one `classify()` call: parsed items, the
    payload ids that failed structural parsing, the payload ids simply
    absent from the response, and per-call metadata for the audit trail
    (spec Sec 8.4)."""

    results: tuple[Classification, ...]  # validated items only
    invalid: tuple[str, ...]  # payload_ids that failed validation
    missing: tuple[str, ...]  # payload_ids absent from the response
    structured_output_level: str  # json_schema|json_object|none
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


class Classifier(Protocol):
    def classify(
        self,
        candidates: Sequence[CandidatePayload],
        rules: Sequence[OfferedRule],
    ) -> ClassifyOutcome: ...
