"""Classifier protocol and payload/response models (contracts Sec 5.3;
jev-provider-plan §5, §10).

This is the fixed cross-unit interface between the evaluate phase (this
unit) and the provider adapters (`openai_compatible.py`, `anthropic.py`,
`jev.py`, also this unit). It was originally reproduced verbatim inside
`tests/fakes/fake_classifier.py` because this module did not yet exist
when Unit 1 landed; per contracts Sec 5.4 that fake now imports these
definitions from here instead of declaring local copies.

**Validation against the offered processor and candidate vocabulary
happens in the caller (see `liametahi.evaluate`), never in an adapter.**
An adapter's `classify()` only has to do transport, structured-output
negotiation, and JSON parsing; it is free to hand back a `Classification`
that names a processor or candidate id it was never offered, or an
answer value outside a processor's declared options/levels; the model is
untrusted and the architecture assumes every adapter response is hostile
until validated -- an adapter must never let a model response widen what
an action may do.

The jev-provider-plan redesign retires the old single-`llm`-atom,
yes/no/unsure vocabulary (`OfferedRule`/`Classification.matches`/
`needs_content`) in favour of a per-processor answer map: each candidate
may be asked about several independently-named processors in one batch,
and each processor answers with a resolved `value` (bool/choice-string/
score-level-string) plus an optional `confidence` (jev always populates
it; a chat processor only if its own declared schema asked for it).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from liametahi.rules import ProcessorAnswer

__all__ = [
    "CandidatePayload",
    "Classification",
    "Classifier",
    "ClassifyOutcome",
    "OfferedProcessor",
    "ProcessorAnswer",
]


@dataclass(frozen=True, slots=True)
class OfferedProcessor:
    """One processor offered to the model for a batch: its name and
    enough of its declared shape (`type` plus `criteria`/`options`/
    `levels`) for an adapter to compile a request/schema from (`jev.py`
    maps these straight onto its `noul`/`choice`/`score` request shape;
    `prompt.py` compiles the same fields into a chat prompt + JSON
    schema for the other two providers)."""

    name: str
    type: str  # "noul" | "choice" | "score"
    criteria: Mapping[str, str] | None = None
    options: Mapping[str, str] | None = None
    levels: tuple[str, ...] | None = None
    include_body: bool = False


@dataclass(frozen=True, slots=True)
class CandidatePayload:
    """One candidate as sent to the model: a batch-local id and its
    already capped-and-sanitised metadata fields (spec Sec 5.1, Sec
    5.2)."""

    payload_id: str  # batch-local, "c1".."cN"
    fields: Mapping[str, object]  # already capped and sanitised by prompt.py


@dataclass(frozen=True, slots=True)
class Classification:
    """One raw, unvalidated per-candidate model response item.

    `answers` maps a processor name to its raw, not-yet-validated
    `ProcessorAnswer` -- only for the processors this response actually
    resolved for this candidate this round; a processor name absent from
    the map means "not answered this round" and the caller must treat it
    the same as invalid/needing retry, never as silently unknown-forever
    (jev-provider-plan's `classifier/__init__.py` note). `value` may name
    an option/level outside what was offered, or the wrong type for the
    processor's declared `type` -- none of that has been validated yet.
    """

    payload_id: str
    answers: Mapping[str, ProcessorAnswer]
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
        processors: Sequence[OfferedProcessor],
    ) -> ClassifyOutcome: ...
