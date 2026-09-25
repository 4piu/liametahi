"""Classifier protocol and payload/response models.

This is the fixed cross-unit interface between the evaluate phase (this
unit) and the provider adapters (`openai.py`, `anthropic.py`,
`systemone.py`, also this unit). It was originally reproduced verbatim inside
`tests/fakes/fake_classifier.py` because this module did not yet exist
when Unit 1 landed; per the explicit instruction that fake now imports
these definitions from here instead of declaring local copies.

**Validation against the offered processor and candidate vocabulary
happens in the caller (see `liametahi.evaluate`), never in an adapter.**
An adapter's `classify()` only has to do transport, structured-output
negotiation, and JSON parsing; it is free to hand back a `Classification`
that names a processor or candidate id it was never offered, or an
answer value outside a processor's declared criteria; the model is
untrusted and the architecture assumes every adapter response is hostile
until validated -- an adapter must never let a model response widen what
an action may do.

This retires the old single-`llm`-atom,
yes/no/unsure vocabulary (`OfferedRule`/`Classification.matches`/
`needs_content`) in favour of a per-processor answer map: each candidate
may be asked about several independently-named processors in one batch,
and each processor answers with a resolved `value` (a probability in
`[0, 1]` for `noul`, a choice string for `choice`, a level string for
`score`) plus an optional `confidence` (a systemone model always
populates it for `choice`/`score`, never for `noul`; a chat processor
only if its own declared schema asked for it -- which it never does for
`noul`, since a `noul` processor's `.confidence` is always `None`
regardless of backend).
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
    enough of its declared shape (`type`, its required `instructions`,
    plus its optional/required `criteria`) for an adapter to compile a
    request/schema from (`systemone.py` maps these straight onto its
    `noul`/`choice`/`score` request shape -- the protocol's own schema,
    not a project invention; `prompt.py` compiles the same fields into a chat
    prompt + JSON schema for the other two providers). `criteria`'s shape
    depends on `type`: a `{"true": ..., "false": ...}` mapping (optional)
    for `noul`, an `{option: description}` mapping (required) for
    `choice`, or an ordered sequence of level names (required) for
    `score`."""

    name: str
    type: str  # "noul" | "choice" | "score"
    instructions: str
    criteria: Mapping[str, str] | Sequence[str] | None = None
    #: This processor's resolved `fields:` selection (`config.
    #: ProcessorConfig.resolved_fields`) -- the model-visible metadata
    #: catalog names it depends on, used to compute its cache/processor
    #: identity hash and to decide whether it needs a body fetch. Not
    #: itself sent to the model; only `prompt.py`'s payload builder reads
    #: it, same as `include_body` was never read by an adapter either.
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidatePayload:
    """One candidate as sent to the model: a batch-local id and its
    already capped-and-sanitised metadata fields."""

    payload_id: str  # batch-local, "c1".."cN"
    fields: Mapping[str, object]  # already capped and sanitised by prompt.py


@dataclass(frozen=True, slots=True)
class Classification:
    """One raw, unvalidated per-candidate model response item.

    `answers` maps a processor name to its raw, not-yet-validated
    `ProcessorAnswer` -- only for the processors this response actually
    resolved for this candidate this round; a processor name absent from
    the map means "not answered this round" and the caller must treat it
    the same as invalid/needing retry, never as silently unknown-forever.
    `value` may name an option/level outside what was offered, or the
    wrong type for the processor's declared `type` -- none of that has
    been validated yet.
    """

    payload_id: str
    answers: Mapping[str, ProcessorAnswer]
    reason: str | None


@dataclass(frozen=True, slots=True)
class ClassifyOutcome:
    """The full result of one `classify()` call: parsed items, the
    payload ids that failed structural parsing, the payload ids simply
    absent from the response, and per-call metadata for the audit trail."""

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
