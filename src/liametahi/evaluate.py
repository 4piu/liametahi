"""The evaluate phase: three-valued rule evaluation against the decision
cache, batched processor classification, response validation, and
split-and-retry failure handling.

This module is the callable `runner.py` sequences for phase 2.
`evaluate_candidates()` consumes already-scanned candidates plus a task's
configuration and produces, per candidate, either a set of validated
matched rules (deterministic and/or processor-backed, ready for the
policy engine to pick a winner) or a terminal no-op status drawn from the
fixed vocabulary (`no_match`/`cached_no_match`/`invalid_response`/
`no_model_response`).

This generalises the old "the one `llm` atom, ask once,
finalize" loop into: collect the distinct **processor names** referenced
by any still-`Tri.UNKNOWN` rule (`rules.processor_names`), resolve as many
as possible from the decision cache, group whatever remains by the model
each references, ask each group once per candidate that still needs it
(deduplicated by *processor*, not by rule -- two rules sharing one named
processor invoke it once), merge every answer into one per-candidate
`processor_values` map, and re-run `rules.evaluate(...,
processor_values=...)` to finalize. A processor whose resolved `fields`
selection includes `excerpt`/`html` needs body text this module has no
mailbox access to fetch; `runner.py` fetches it up front (a static,
always-needed fetch now, not a dynamic "escalation" -- there is
deliberately no `when:`-gated processor) and passes the text in via
`excerpts`/`html_bodies`. A candidate whose only unresolved processor
needs a body field that was not available this round (fetch failed, or
`runner.py` never called for it) simply stays unresolved for whatever
rules reference that processor -- the exact same "no answer this round"
shape a cache miss followed by no ask at all would produce.

Safety-critical property enforced here, not in any adapter: a
`Classification` returned by a `Classifier` is untrusted until every
field has been checked against what was actually offered for that exact
candidate and processor -- including that an answer's `value` is one of
the processor's declared criteria (or, for `noul`, a probability in
`[0, 1]`) (never let an adapter response widen what an action may do).
See `_validate_answer` below.
"""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from liametahi import prompt, rules, state
from liametahi.classifier import (
    CandidatePayload,
    Classification,
    Classifier,
    ClassifyOutcome,
    OfferedProcessor,
)
from liametahi.config import (
    Config,
    ModelConfig,
    ProcessorConfig,
    RuleConfig,
    TaskConfig,
)
from liametahi.domain import Candidate
from liametahi.logging import get_logger
from liametahi.progress import NullProgress, Progress
from liametahi.rules import ProcessorAnswer

logger = get_logger(__name__)

#: Cap on a model-reported `reason`, written to the audit table only and
#: never read by policy.
_REASON_CAP = 200

ClassifierFactory = Callable[[ModelConfig], Classifier]


@dataclass(frozen=True, slots=True)
class ValidatedMatch:
    """One rule (identified by its position in `task.rules`) that matched
    a candidate: the tree resolved to TRUE against the accumulated
    `processor_values`."""

    rule_index: int


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """The evaluate phase's verdict for one candidate.

    `matches` non-empty means "hand this to the policy engine"; `status`
    is set exactly when `matches` is empty, explaining why (one of the
    no-op statuses in the fixed vocabulary).
    """

    candidate_id: int
    matches: tuple[ValidatedMatch, ...]
    status: str | None
    reason: str | None
    valid: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class EvaluateOutcome:
    results: tuple[CandidateResult, ...]
    model_calls: int
    structured_output_level: str | None
    input_tokens: int | None
    output_tokens: int | None


# --- Internal working state ------------------------------------------


@dataclass(slots=True)
class _CandidateState:
    candidate_id: int
    candidate: Candidate
    processor_values: dict[str, ProcessorAnswer] = field(default_factory=dict)
    used_cache: bool = False
    # A classify() call that raised, or a response item that failed
    # structural validation, for a job this candidate was part of:
    # distinct from simply not having a match, and from
    # `saw_missing` below (a well-formed response that just never
    # mentioned this candidate).
    saw_invalid: bool = False
    saw_missing: bool = False
    last_error: str | None = None
    last_reason: str | None = None
    finalized: CandidateResult | None = None


def _offered_processor(name: str, cfg: ProcessorConfig) -> OfferedProcessor:
    return OfferedProcessor(
        name=name,
        type=cfg.type,
        instructions=cfg.instructions,
        criteria=cfg.criteria,
        fields=cfg.resolved_fields,
    )


def _evaluate_rules(
    task_rules: Sequence[RuleConfig],
    candidate: Candidate,
    now: datetime,
    processor_values: Mapping[str, ProcessorAnswer],
) -> tuple[list[ValidatedMatch], set[str]]:
    """One pass over every rule with the processor answers resolved so
    far. Pure and cheap, so the caller simply re-runs it whenever
    `processor_values` grows rather than tracking incremental state by
    hand."""
    matches: list[ValidatedMatch] = []
    needed: set[str] = set()
    for index, rule in enumerate(task_rules):
        tri = rules.evaluate(
            rule.when, candidate, now=now, processor_values=processor_values
        )
        if tri is rules.Tri.TRUE:
            matches.append(ValidatedMatch(index))
        elif tri is rules.Tri.UNKNOWN:
            needed |= rules.processor_names(rule.when)
    return matches, needed


def processors_needed_for_candidate(
    task_rules: Sequence[RuleConfig], candidate: Candidate, now: datetime
) -> frozenset[str]:
    """Every processor name a still-`UNKNOWN` rule references for this
    candidate, with no prior processor knowledge -- exactly pass 1 of
    `evaluate_candidates`, exposed so `runner.py` can decide which
    candidates need a body field fetched *before* evaluation starts at
    all (a processor's resolved `fields` selection is a static per-
    processor choice, not a dynamically-triggered second pass)."""
    _, needed = _evaluate_rules(task_rules, candidate, now, {})
    return frozenset(needed)


def _finalize(state_item: _CandidateState, matches: list[ValidatedMatch]) -> None:
    """Priority mirrors the old per-rule `_mark_invalid`/`_mark_missing`
    semantics, generalised to processors: a match
    always reaches policy regardless of an unrelated processor's failure
    elsewhere, but `valid`/`error` on the row still records that failure
    for audit; a candidate with no match is `invalid_response` if any
    job it needed raised or failed structural validation,
    `no_model_response` if a well-formed response simply never mentioned
    it, `cached_no_match` if every remaining processor resolved from
    cache, else a confident `no_match`.
    """
    invalid = state_item.saw_invalid or state_item.saw_missing
    if matches:
        status = None
    elif state_item.saw_invalid:
        status = "invalid_response"
    elif state_item.saw_missing:
        status = "no_model_response"
    elif state_item.used_cache:
        status = "cached_no_match"
    else:
        status = "no_match"
    state_item.finalized = CandidateResult(
        candidate_id=state_item.candidate_id,
        matches=tuple(matches),
        status=status,
        reason=state_item.last_reason,
        valid=not invalid,
        error=state_item.last_error if invalid else None,
    )


def _processor_input_hash(
    candidate: Candidate,
    cfg: ProcessorConfig,
    model_cfg: ModelConfig,
    excerpt_text: str | None,
    html_text: str | None,
) -> str | None:
    """The cache/request input hash for one processor against one
    candidate, computed from *this processor's own* resolved fields only
    -- independent of whatever else happened to be unioned into a shared
    batch payload, so the cache reflects exactly what this processor's
    identity says it depends on. `None` means "cannot be resolved this
    round" -- a processor whose fields need a body field with no text
    available yet."""
    fields = cfg.resolved_fields
    needs_body = bool(set(fields) & prompt.BODY_FIELDS)
    if needs_body:
        if ("excerpt" in fields and excerpt_text is None) or (
            "html" in fields and html_text is None
        ):
            return None
        return prompt.build_excerpt_payload(
            candidate,
            payload_id="c0",
            fields=fields,
            excerpt_text=excerpt_text,
            html_text=html_text,
            max_chars=model_cfg.body_excerpt.max_chars,
        ).input_hash
    return prompt.build_candidate_payload(
        candidate, payload_id="c0", fields=fields
    ).input_hash


# --- Entry point --------------------------------------------------------


def evaluate_candidates(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    run_id: str,
    task: TaskConfig,
    config: Config,
    classifier_factory: ClassifierFactory,
    candidates: Sequence[tuple[int, Candidate]],
    now: datetime,
    reevaluate: bool,
    excerpts: Mapping[int, str] | None = None,
    html_bodies: Mapping[int, str] | None = None,
    progress: Progress | None = None,
) -> EvaluateOutcome:
    """Run the evaluate phase over already-scanned, already
    protected-filtered candidates.

    `excerpts`/`html_bodies` map a candidate id to already-fetched text
    for any candidate that needs one of this task's `excerpt`/`html`
    field-selecting processors answered -- `runner.py`'s job, since this
    module has no mailbox access. Absent from a map means "no such text
    available this round"; a processor that needs it simply stays
    unresolved for that candidate.
    """
    reporter = progress or NullProgress()
    excerpts = excerpts or {}
    html_bodies = html_bodies or {}
    states: dict[int, _CandidateState] = {
        candidate_id: _CandidateState(candidate_id=candidate_id, candidate=candidate)
        for candidate_id, candidate in candidates
    }

    # --- Pass 1: deterministic-only, no processor calls at all ----------
    pending: list[int] = []
    for candidate_id, item in states.items():
        matches, needed = _evaluate_rules(task.rules, item.candidate, now, {})
        if not needed:
            _finalize(item, matches)
        else:
            pending.append(candidate_id)

    if not pending:
        return _assemble_outcome(states, candidates, _BatchStats(model_calls=0))

    # --- Pass 2: decision cache -----------------------------------------
    still_pending: list[int] = []
    for candidate_id in pending:
        item = states[candidate_id]
        _, needed = _evaluate_rules(
            task.rules, item.candidate, now, item.processor_values
        )
        ask_now = _consult_cache(
            conn,
            config=config,
            account_id=account_id,
            reevaluate=reevaluate,
            item=item,
            needed=needed,
            excerpts=excerpts,
            html_bodies=html_bodies,
        )
        matches, needed_after = _evaluate_rules(
            task.rules, item.candidate, now, item.processor_values
        )
        if not needed_after or not ask_now:
            _finalize(item, matches)
        else:
            still_pending.append(candidate_id)

    if not still_pending:
        return _assemble_outcome(states, candidates, _BatchStats(model_calls=0))

    # --- Pass 3: batch the rest by (model, exact processor set needed) --
    to_ask: dict[int, set[str]] = {}
    for candidate_id in still_pending:
        item = states[candidate_id]
        _, needed = _evaluate_rules(
            task.rules, item.candidate, now, item.processor_values
        )
        if needed:
            to_ask[candidate_id] = needed

    stats = _BatchStats(model_calls=0)
    if to_ask:
        reporter.start("classifying", total=len(to_ask))
        try:
            stats = _classify_all(
                conn,
                config=config,
                classifier_factory=classifier_factory,
                run_id=run_id,
                account_id=account_id,
                states=states,
                to_ask=to_ask,
                excerpts=excerpts,
                html_bodies=html_bodies,
                reporter=reporter,
            )
        finally:
            reporter.stop()

    # --- Finalize everything left ---------------------------------------
    for candidate_id in still_pending:
        item = states[candidate_id]
        if item.finalized is None:
            matches, _ = _evaluate_rules(
                task.rules, item.candidate, now, item.processor_values
            )
            _finalize(item, matches)

    return _assemble_outcome(states, candidates, stats)


def _assemble_outcome(
    states: Mapping[int, _CandidateState],
    candidates: Sequence[tuple[int, Candidate]],
    stats: _BatchStats,
) -> EvaluateOutcome:
    results = []
    for candidate_id, _ in candidates:
        finalized = states[candidate_id].finalized
        assert finalized is not None
        results.append(finalized)
    return EvaluateOutcome(
        results=tuple(results),
        model_calls=stats.model_calls,
        structured_output_level=stats.structured_output_level,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
    )


def _consult_cache(
    conn: sqlite3.Connection,
    *,
    config: Config,
    account_id: int,
    reevaluate: bool,
    item: _CandidateState,
    needed: set[str],
    excerpts: Mapping[int, str],
    html_bodies: Mapping[int, str],
) -> set[str]:
    """Resolve as many of `needed` as possible from the decision cache,
    writing hits into `item.processor_values`. Returns the
    subset that still needs a live ask this round (excludes anything
    resolved from cache and anything that cannot be asked this round at
    all, e.g. a body-needing processor with no excerpt available)."""
    ask_now: set[str] = set()
    for name in needed:
        cfg = config.processors[name]
        model_cfg = config.models[cfg.model]
        excerpt_text = excerpts.get(item.candidate_id)
        html_text = html_bodies.get(item.candidate_id)
        input_hash = _processor_input_hash(
            item.candidate, cfg, model_cfg, excerpt_text, html_text
        )
        if input_hash is None:
            continue
        offered = _offered_processor(name, cfg)
        processor_hash = prompt.compute_processor_hash(offered)
        if not reevaluate:
            cached = state.get_cached_processor_decision(
                conn,
                account_id=account_id,
                fingerprint=item.candidate.fingerprint,
                processor_name=name,
                processor_hash=processor_hash,
                input_hash=input_hash,
                model_id=model_cfg.model,
                prompt_version=prompt.PROMPT_VERSION,
            )
            if cached is not None:
                item.processor_values[name] = cached
                item.used_cache = True
                continue
        ask_now.add(name)
    return ask_now


# --- Batching and classification ----------------------------------------


@dataclass(frozen=True, slots=True)
class _BatchStats:
    model_calls: int
    structured_output_level: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _sum_optional(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def _combine_stats(left: _BatchStats, right: _BatchStats) -> _BatchStats:
    return _BatchStats(
        model_calls=left.model_calls + right.model_calls,
        structured_output_level=right.structured_output_level
        or left.structured_output_level,
        input_tokens=_sum_optional(left.input_tokens, right.input_tokens),
        output_tokens=_sum_optional(left.output_tokens, right.output_tokens),
    )


def _group_by_model(
    to_ask: Mapping[int, set[str]], config: Config
) -> dict[str, dict[frozenset[str], list[int]]]:
    """model name -> exact processor-name-set needed -> candidate ids
    needing exactly that set from that model. A candidate whose needed
    processors span two models appears once in each model's grouping,
    with only that model's share of its needed names -- two independent
    asks."""
    grouped: dict[str, dict[frozenset[str], list[int]]] = {}
    for candidate_id, names in to_ask.items():
        by_model: dict[str, set[str]] = {}
        for name in names:
            model_name = config.processors[name].model
            by_model.setdefault(model_name, set()).add(name)
        for model_name, name_set in by_model.items():
            per_key = grouped.setdefault(model_name, {})
            per_key.setdefault(frozenset(name_set), []).append(candidate_id)
    return grouped


@dataclass(frozen=True, slots=True)
class _Job:
    candidate_ids: tuple[int, ...]
    processors: tuple[OfferedProcessor, ...]
    #: The union of every offered processor's own resolved fields --
    #: every candidate in this job shares one payload (batch-level, like
    #: `processors` itself), so the payload must satisfy whichever
    #: processor asked for the most, not just one of them.
    fields: tuple[str, ...]
    needs_body: bool


def _classify_all(
    conn: sqlite3.Connection,
    *,
    config: Config,
    classifier_factory: ClassifierFactory,
    run_id: str,
    account_id: int,
    states: dict[int, _CandidateState],
    to_ask: Mapping[int, set[str]],
    excerpts: Mapping[int, str],
    html_bodies: Mapping[int, str],
    reporter: Progress,
) -> _BatchStats:
    """Classify every still-needed candidate, model by model.

    Requests for the same model may overlap up to
    `model_cfg.max_concurrent_requests` (the real throughput lever for a
    first run over a large mailbox); the network half of each batch
    (`_classify_network`) touches no shared state at all, so it is safe
    to run on a worker thread, while every database write
    (`_record_attempt`) always happens back on this thread, in job
    submission order rather than completion order -- `executor.map`
    already guarantees that ordering, which is what keeps the database,
    the audit trail, and the report identical no matter the concurrency
    setting or how a provider happened to schedule its responses.
    """
    classifiers: dict[str, Classifier] = {}
    grouped = _group_by_model(to_ask, config)
    stats = _BatchStats(model_calls=0)

    for model_name, by_names in grouped.items():
        model_cfg = config.models[model_name]
        classifier = classifiers.setdefault(model_name, classifier_factory(model_cfg))
        jobs: list[_Job] = []
        for name_set, candidate_ids in by_names.items():
            processors = tuple(
                _offered_processor(name, config.processors[name])
                for name in sorted(name_set)
            )
            job_fields = tuple(sorted({f for p in processors for f in p.fields}))
            needs_body = bool(set(job_fields) & prompt.BODY_FIELDS)
            batch_size = model_cfg.mails_per_request
            for start in range(0, len(candidate_ids), batch_size):
                chunk = tuple(candidate_ids[start : start + batch_size])
                jobs.append(_Job(chunk, processors, job_fields, needs_body))
        if not jobs:
            continue

        def network(
            job: _Job,
            *,
            classifier: Classifier = classifier,
            model_cfg: ModelConfig = model_cfg,
        ) -> list[_NetworkAttempt]:
            return _classify_network(
                classifier,
                states,
                job.candidate_ids,
                job.processors,
                fields=job.fields,
                max_chars=model_cfg.body_excerpt.max_chars,
                excerpts=excerpts,
                html_bodies=html_bodies,
                run_id=run_id,
            )

        def record(job: _Job, attempts: list[_NetworkAttempt]) -> None:
            nonlocal stats
            for attempt in attempts:
                stats = _combine_stats(
                    stats,
                    _record_attempt(
                        conn,
                        attempt,
                        config=config,
                        states=states,
                        run_id=run_id,
                        account_id=account_id,
                        excerpts=excerpts,
                        html_bodies=html_bodies,
                    ),
                )
            reporter.advance(len(job.candidate_ids))

        concurrency = min(model_cfg.max_concurrent_requests, len(jobs))
        if concurrency <= 1:
            for job in jobs:
                record(job, network(job))
            continue

        executor = ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="lia-classify"
        )
        try:
            results = executor.map(network, jobs)
            for job, attempts in zip(jobs, results, strict=True):
                record(job, attempts)
        except BaseException:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
    return stats


def _build_payloads(
    states: dict[int, _CandidateState],
    candidate_ids: Sequence[int],
    *,
    fields: Sequence[str],
    needs_body: bool,
    max_chars: int | None,
    excerpts: Mapping[int, str],
    html_bodies: Mapping[int, str],
) -> tuple[list[CandidatePayload], dict[str, int], dict[str, str], str]:
    payloads: list[CandidatePayload] = []
    by_payload_id: dict[str, int] = {}
    input_hash_by_payload_id: dict[str, str] = {}
    input_level = "excerpt" if needs_body else "metadata"
    for index, candidate_id in enumerate(candidate_ids, start=1):
        payload_id = f"c{index}"
        candidate = states[candidate_id].candidate
        excerpt_text = excerpts.get(candidate_id)
        html_text = html_bodies.get(candidate_id)
        if needs_body and (excerpt_text is not None or html_text is not None):
            built = prompt.build_excerpt_payload(
                candidate,
                payload_id=payload_id,
                fields=fields,
                excerpt_text=excerpt_text,
                html_text=html_text,
                max_chars=max_chars,
            )
        else:
            non_body_fields = [f for f in fields if f not in prompt.BODY_FIELDS]
            built = prompt.build_candidate_payload(
                candidate, payload_id=payload_id, fields=non_body_fields
            )
        payloads.append(built.payload)
        by_payload_id[payload_id] = candidate_id
        input_hash_by_payload_id[payload_id] = built.input_hash
    return payloads, by_payload_id, input_hash_by_payload_id, input_level


@dataclass(frozen=True, slots=True)
class _NetworkAttempt:
    """The network half of classifying one batch, and nothing else --
    touches no database and no shared mutable state, which is what makes
    it safe to run for several batches at once (see `_classify_all`).
    `outcome is None` means the call failed or came back wholly invalid;
    a wholly-invalid attempt has already been split-and-retried once by
    the time this is returned, so the caller never
    retries it again."""

    candidate_ids: tuple[int, ...]
    by_payload_id: dict[str, int]
    input_hash_by_payload_id: dict[str, str]
    input_level: str
    processors: tuple[OfferedProcessor, ...]
    outcome: ClassifyOutcome | None
    failure_reason: str = "classify() raised an exception"


def _classify_network(
    classifier: Classifier,
    states: dict[int, _CandidateState],
    candidate_ids: Sequence[int],
    processors: Sequence[OfferedProcessor],
    *,
    fields: Sequence[str],
    max_chars: int | None,
    excerpts: Mapping[int, str],
    html_bodies: Mapping[int, str],
    run_id: str,
    allow_retry: bool = True,
) -> list[_NetworkAttempt]:
    needs_body = bool(set(fields) & prompt.BODY_FIELDS)
    payloads, by_payload_id, input_hash_by_payload_id, input_level = _build_payloads(
        states,
        candidate_ids,
        fields=fields,
        needs_body=needs_body,
        max_chars=max_chars,
        excerpts=excerpts,
        html_bodies=html_bodies,
    )

    failure_reason = "unparseable or wholly invalid model response"
    try:
        outcome = classifier.classify(payloads, processors)
    except Exception as exc:  # noqa: BLE001 - explicit recorded outcome below
        failure_reason = f"classifier raised {type(exc).__name__}: {exc}"
        logger.debug("run %s: %s", run_id, failure_reason)
        outcome = None

    wholly_invalid = outcome is None or len(outcome.results) == 0
    if wholly_invalid and allow_retry and len(candidate_ids) > 1:
        midpoint = len(candidate_ids) // 2
        attempts: list[_NetworkAttempt] = []
        for half in (candidate_ids[:midpoint], candidate_ids[midpoint:]):
            attempts.extend(
                _classify_network(
                    classifier,
                    states,
                    half,
                    processors,
                    fields=fields,
                    max_chars=max_chars,
                    excerpts=excerpts,
                    html_bodies=html_bodies,
                    run_id=run_id,
                    allow_retry=False,
                )
            )
        return attempts

    return [
        _NetworkAttempt(
            candidate_ids=tuple(candidate_ids),
            by_payload_id=by_payload_id,
            input_hash_by_payload_id=input_hash_by_payload_id,
            input_level=input_level,
            processors=tuple(processors),
            outcome=None if wholly_invalid else outcome,
            failure_reason=failure_reason,
        )
    ]


def _record_attempt(
    conn: sqlite3.Connection,
    attempt: _NetworkAttempt,
    *,
    config: Config,
    states: dict[int, _CandidateState],
    run_id: str,
    account_id: int,
    excerpts: Mapping[int, str],
    html_bodies: Mapping[int, str],
) -> _BatchStats:
    """The bookkeeping half: record what `_classify_network` came back
    with. Pure local work -- one transaction per batch rather than one
    fsync per row (see `state.transaction`) -- and always on the thread
    that owns `conn`, never inside a worker."""
    by_payload_id = attempt.by_payload_id
    input_hash_by_payload_id = attempt.input_hash_by_payload_id
    input_level = attempt.input_level
    processors = attempt.processors
    outcome = attempt.outcome
    model_cfg = config.models[config.processors[processors[0].name].model]

    with state.transaction(conn):
        if outcome is None:
            for payload_id, candidate_id in by_payload_id.items():
                item = states[candidate_id]
                item.saw_invalid = True
                item.last_error = attempt.failure_reason
                state.insert_classification(
                    conn,
                    run_id=run_id,
                    candidate_id=candidate_id,
                    input_level=input_level,
                    input_hash=input_hash_by_payload_id[payload_id],
                    offered_processors=[p.name for p in processors],
                    processor_answers={},
                    reason=None,
                    valid=False,
                    error=attempt.failure_reason,
                    latency_ms=None,
                )
            return _BatchStats(model_calls=1)

        seen_payload_ids: set[str] = set()
        for classification in outcome.results:
            payload_id = classification.payload_id
            resolved_id = by_payload_id.get(payload_id)
            if resolved_id is None:
                state.append_audit_event(
                    conn,
                    run_id=run_id,
                    kind="classifier_unknown_candidate_id",
                    data={"payload_id": payload_id},
                )
                continue
            if payload_id in seen_payload_ids:
                state.append_audit_event(
                    conn,
                    run_id=run_id,
                    kind="classifier_duplicate_candidate_id",
                    subject=str(resolved_id),
                    data={"payload_id": payload_id},
                )
                continue
            seen_payload_ids.add(payload_id)
            _resolve_item(
                conn,
                classification,
                candidate_id=resolved_id,
                item=states[resolved_id],
                config=config,
                processors=processors,
                model_cfg=model_cfg,
                run_id=run_id,
                account_id=account_id,
                excerpts=excerpts,
                html_bodies=html_bodies,
                input_level=input_level,
                input_hash=input_hash_by_payload_id[payload_id],
            )

        for payload_id in outcome.invalid:
            if payload_id in seen_payload_ids:
                continue
            resolved_id = by_payload_id.get(payload_id)
            if resolved_id is None:
                continue
            item = states[resolved_id]
            item.saw_invalid = True
            item.last_error = "response item failed structural validation"
            state.insert_classification(
                conn,
                run_id=run_id,
                candidate_id=resolved_id,
                input_level=input_level,
                input_hash=input_hash_by_payload_id[payload_id],
                offered_processors=[p.name for p in processors],
                processor_answers={},
                reason=None,
                valid=False,
                error="response item failed structural validation",
                latency_ms=outcome.latency_ms,
            )

        for payload_id in outcome.missing:
            if payload_id in seen_payload_ids:
                continue
            resolved_id = by_payload_id.get(payload_id)
            if resolved_id is None:
                continue
            item = states[resolved_id]
            item.saw_missing = True
            item.last_error = "absent from model response"

        # Defence in depth: a well-behaved `Classifier` always partitions
        # every requested payload id across `results`/`invalid`/`missing`
        # (that invariant is what `prompt.parse_classification_response`
        # guarantees), but `Classifier` is an adapter-implemented
        # Protocol, not a sealed type, and its response is untrusted. If
        # some future or third-party adapter ever violates that
        # invariant, every candidate in this batch must still end up
        # with *some* record of this attempt rather than silently
        # falling through to "no processor ever answered" -- treat an
        # untouched payload id exactly like `missing`.
        for payload_id, candidate_id in by_payload_id.items():
            if payload_id in seen_payload_ids or payload_id in outcome.invalid:
                continue
            if payload_id in outcome.missing:
                continue
            states[candidate_id].saw_missing = True
            states[candidate_id].last_error = "absent from model response"

    return _BatchStats(
        model_calls=1,
        structured_output_level=outcome.structured_output_level,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
    )


# --- Response validation (the untrusted-adapter-response boundary) --------


def _validate_answer(cfg: ProcessorConfig, answer: ProcessorAnswer) -> bool:
    """An answer is untrusted until its `value` is checked against the
    processor's own declared vocabulary -- exactly the boundary
    `classifier/__init__.py`'s module docstring requires: never let an
    adapter response widen what an action may do.

    `confidence` gets the same treatment: it is a calibrated probability,
    so anything outside `[0, 1]` is not a value
    jev or a well-formed chat schema would ever produce -- a malformed
    or hostile `confidence` (e.g. `999.0`) must not be allowed to make a
    `processor: "name.confidence >= 0.85"` condition spuriously TRUE for
    every candidate."""
    if answer.confidence is not None and not 0.0 <= answer.confidence <= 1.0:
        return False
    if cfg.type == "noul":
        return (
            isinstance(answer.value, int | float)
            and not isinstance(answer.value, bool)
            and 0.0 <= answer.value <= 1.0
        )
    if cfg.type == "choice":
        return (
            isinstance(answer.value, str)
            and isinstance(cfg.criteria, dict)
            and answer.value in cfg.criteria
        )
    # score
    return (
        isinstance(answer.value, str)
        and isinstance(cfg.criteria, list)
        and answer.value in cfg.criteria
    )


def _resolve_item(
    conn: sqlite3.Connection,
    classification: Classification,
    *,
    candidate_id: int,
    item: _CandidateState,
    config: Config,
    processors: Sequence[OfferedProcessor],
    model_cfg: ModelConfig,
    run_id: str,
    account_id: int,
    excerpts: Mapping[int, str],
    html_bodies: Mapping[int, str],
    input_level: str,
    input_hash: str,
) -> None:
    offered_by_name = {p.name: p for p in processors}
    accepted_count = 0
    for name, raw_answer in classification.answers.items():
        offered = offered_by_name.get(name)
        if offered is None:
            # A processor name this candidate was never offered in this
            # call -- exactly the shape of a hostile response trying to
            # claim an answer to something it was not asked. Drop it.
            state.append_audit_event(
                conn,
                run_id=run_id,
                kind="classifier_unoffered_processor",
                subject=str(candidate_id),
                data={"processor": name},
            )
            continue
        cfg = config.processors[name]
        if not _validate_answer(cfg, raw_answer):
            state.append_audit_event(
                conn,
                run_id=run_id,
                kind="classifier_invalid_answer_value",
                subject=str(candidate_id),
                data={"processor": name, "value": str(raw_answer.value)},
            )
            continue
        item.processor_values[name] = raw_answer
        accepted_count += 1

        model_for_processor = config.models[cfg.model]
        excerpt_text = excerpts.get(candidate_id)
        html_text = html_bodies.get(candidate_id)
        cache_input_hash = _processor_input_hash(
            item.candidate, cfg, model_for_processor, excerpt_text, html_text
        )
        if cache_input_hash is not None:
            processor_hash = prompt.compute_processor_hash(offered)
            state.record_processor_decision(
                conn,
                account_id=account_id,
                fingerprint=item.candidate.fingerprint,
                processor_name=name,
                processor_hash=processor_hash,
                input_hash=cache_input_hash,
                model_id=model_for_processor.model,
                prompt_version=prompt.PROMPT_VERSION,
                answer=raw_answer,
            )

    reason = classification.reason
    if reason is not None and len(reason) > _REASON_CAP:
        reason = reason[:_REASON_CAP]
    if reason is not None:
        item.last_reason = reason

    state.insert_classification(
        conn,
        run_id=run_id,
        candidate_id=candidate_id,
        input_level=input_level,
        input_hash=input_hash,
        offered_processors=[p.name for p in processors],
        processor_answers=classification.answers,
        reason=reason,
        valid=True,
        error=None,
        latency_ms=None,
    )
    _ = accepted_count  # (kept for potential future audit/telemetry use)
