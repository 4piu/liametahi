"""The evaluate phase (spec §4.2, steps 2-6): three-valued rule
evaluation against the LLM decision cache, batched LLM classification,
response validation, and split-and-retry failure handling.

This module is the callable Unit 5's `runner.py` will sequence for
phase 2 — deliberately not named `runner.py` itself, which is Unit 5's
file. `evaluate_candidates()` consumes already-scanned candidates plus a
task's configuration and produces, per candidate, either a set of
validated matched rules (deterministic and/or LLM, ready for the policy
engine to pick a winner — Unit 4's job, not this module's) or a terminal
no-op status drawn from the spec §9 vocabulary
(`no_match`/`cached_no_match`/`invalid_response`/`no_llm_response`).

The model's output is yes/no/unsure, not a score (spec §5.3): a rule id
appearing in a `Classification`'s `matches` is accepted outright once it
passes vocabulary validation (offered for this candidate, not a
duplicate); there is no confidence threshold anywhere in this module. An
item with `needs_content: true` is unsure and nothing about it is
accepted or cached (see `_resolve_item`).

Scope boundaries, deliberately:

- Deterministic protected-message exclusion (spec §4.2 step 1) is
  `rules.is_protected`, already provided by Unit 1; this module assumes
  its caller has already filtered those out, so a protected message is
  never even passed in here and can never reach a model.
- Winner selection and action resolution (spec §4.2 step 8) are Unit 4's
  `policy.py`. This module only ever narrows "which rules could this
  message plausibly match", never picks a single winner.
- Content escalation (spec §4.2 step 7) needs a mailbox re-fetch, which
  is Unit 2's territory and unavailable to this module in isolation.
  Per spec §5.3 ("if escalation is unavailable for any reason, the item
  is treated as unknown and produces no action"), a candidate whose only
  surviving classification requested `needs_content` is left with no
  accepted match; Unit 5 may re-invoke this module with an excerpt-level
  payload once it has fetched one, but that orchestration lives outside
  this unit.

Safety-critical property enforced here, not in either adapter: a
`Classification` returned by a `Classifier` is untrusted until every
single field has been checked against what was actually offered for
that exact candidate. See `_resolve_item` below.
"""

import sqlite3
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from liametahi import prompt, rules, state
from liametahi.classifier import (
    CandidatePayload,
    Classification,
    Classifier,
    ClassifyOutcome,
    OfferedRule,
)
from liametahi.config import ModelConfig, RuleConfig, TaskConfig
from liametahi.domain import Candidate
from liametahi.logging import get_logger
from liametahi.progress import NullProgress, Progress

logger = get_logger(__name__)

#: Cap on a model-reported `reason`, written to the audit table only and
#: never read by policy (spec §5.3).
_REASON_CAP = 200


@dataclass(frozen=True, slots=True)
class ValidatedMatch:
    """One rule that matched a candidate, having survived every
    validation check in `_resolve_item` — either the deterministic tree
    resolved to TRUE (spec §7.2), or the model confidently reported this
    rule id in `matches` and the id was offered for this candidate and
    not a duplicate. There is no confidence score to carry (spec §5.3):
    the model's output is yes/no/unsure, not a threshold-worthy number.
    """

    rule_id: str


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """The evaluate phase's verdict for one candidate.

    `matches` non-empty means "hand this to the policy engine"; `status`
    is set exactly when `matches` is empty, explaining why (one of the
    no-op statuses in spec §9). `valid`/`error` mirror the
    `classifications` table columns (contracts §4) for audit.
    """

    candidate_id: int
    matches: tuple[ValidatedMatch, ...]
    status: str | None
    reason: str | None
    offered_rules: tuple[str, ...]
    input_hash: str | None
    valid: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class EvaluateOutcome:
    results: tuple[CandidateResult, ...]
    llm_calls: int
    structured_output_level: str | None
    input_tokens: int | None
    output_tokens: int | None


# --- Internal working state ------------------------------------------


@dataclass(frozen=True, slots=True)
class _BatchItem:
    candidate_id: int
    candidate: Candidate
    resolved_matches: tuple[ValidatedMatch, ...]
    cache_hit: bool
    remaining_rule_ids: tuple[str, ...]
    input_hash: str


@dataclass(frozen=True, slots=True)
class _BatchAttempt:
    """One completed model call and the items it covered -- the whole
    result of the network half of a batch, carrying everything the
    bookkeeping half needs so the two can happen on different threads.
    `outcome is None` means the call failed or came back wholly invalid,
    with `failure_reason` explaining which."""

    items: tuple[_BatchItem, ...]
    item_by_payload_id: dict[str, _BatchItem]
    outcome: ClassifyOutcome | None
    failure_reason: str


@dataclass(frozen=True, slots=True)
class _BatchStats:
    llm_calls: int
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
        llm_calls=left.llm_calls + right.llm_calls,
        structured_output_level=right.structured_output_level
        or left.structured_output_level,
        input_tokens=_sum_optional(left.input_tokens, right.input_tokens),
        output_tokens=_sum_optional(left.output_tokens, right.output_tokens),
    )


def _finalize_no_llm(
    candidate_id: int,
    resolved_matches: tuple[ValidatedMatch, ...],
    *,
    cache_hit: bool,
) -> CandidateResult:
    """A candidate whose rule set was fully resolved by deterministic
    evaluation plus (optionally) the LLM decision cache, with no model
    call this run (spec §4.2 step 4)."""
    if resolved_matches:
        status = None
    elif cache_hit:
        status = "cached_no_match"
    else:
        status = "no_match"
    return CandidateResult(
        candidate_id=candidate_id,
        matches=resolved_matches,
        status=status,
        reason=None,
        offered_rules=(),
        input_hash=None,
        valid=True,
        error=None,
    )


# --- Entry point --------------------------------------------------------


def evaluate_candidates(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    run_id: str,
    task: TaskConfig,
    model_config: ModelConfig,
    model_id: str,
    classifier: Classifier,
    candidates: Sequence[tuple[int, Candidate]],
    now: datetime,
    reevaluate: bool,
    progress: Progress | None = None,
) -> EvaluateOutcome:
    """Run spec §4.2 steps 2-6 over already-scanned, already
    protected-filtered candidates.

    `candidates` pairs each candidate with its `candidates.candidate_id`
    primary key (contracts §4), needed for `result_items`/
    `classifications` foreign keys and for the LLM decision cache's
    `fingerprint` (read off the `Candidate` itself).
    """
    reporter = progress or NullProgress()
    rules_by_id: dict[str, RuleConfig] = {rule.id: rule for rule in task.rules}
    per_candidate: dict[int, CandidateResult] = {}
    llm_items: list[_BatchItem] = []

    for candidate_id, candidate in candidates:
        resolved_matches: list[ValidatedMatch] = []
        unknown_rule_ids: list[str] = []
        for rule in task.rules:
            tri = rules.evaluate(rule.when, candidate, now=now)
            if tri is rules.Tri.TRUE:
                resolved_matches.append(ValidatedMatch(rule.id))
            elif tri is rules.Tri.UNKNOWN:
                unknown_rule_ids.append(rule.id)

        if not unknown_rule_ids:
            per_candidate[candidate_id] = _finalize_no_llm(
                candidate_id, tuple(resolved_matches), cache_hit=False
            )
            continue

        built = prompt.build_candidate_payload(
            candidate, payload_id="c0", offered=tuple(unknown_rule_ids)
        )
        cache_hit = False
        remaining: list[str] = []
        for rule_id in unknown_rule_ids:
            rule_text_hash = _rule_text_hash(rules_by_id[rule_id])
            if not reevaluate:
                cached = state.get_cached_decision(
                    conn,
                    account_id=account_id,
                    fingerprint=candidate.fingerprint,
                    rule_id=rule_id,
                    rule_text_hash=rule_text_hash,
                    input_hash=built.input_hash,
                    model_id=model_id,
                    prompt_version=prompt.PROMPT_VERSION,
                )
                if cached is not None:
                    cache_hit = True
                    if cached["matched"]:
                        # A cached "yes": skip straight to policy/execution
                        # without asking again. This is what lets a message
                        # whose remote mutation failed last run (wrong
                        # trash_mailbox, an unadvertised capability, ...)
                        # retry that mutation on the next run instead of
                        # being reclassified from scratch every time.
                        resolved_matches.append(ValidatedMatch(rule_id))
                    continue
            remaining.append(rule_id)

        if not remaining:
            per_candidate[candidate_id] = _finalize_no_llm(
                candidate_id, tuple(resolved_matches), cache_hit=cache_hit
            )
            continue

        llm_items.append(
            _BatchItem(
                candidate_id=candidate_id,
                candidate=candidate,
                resolved_matches=tuple(resolved_matches),
                cache_hit=cache_hit,
                remaining_rule_ids=tuple(remaining),
                input_hash=built.input_hash,
            )
        )

    stats = _BatchStats(llm_calls=0)
    if llm_items:
        batches = _group_into_batches(
            llm_items, batch_size=model_config.mails_per_request
        )
        total_batches = len(batches)
        concurrency = min(model_config.max_concurrent_requests, total_batches)
        logger.info(
            "run %s: %d mail(s) need an LLM call, in %d batch(es)%s",
            run_id,
            len(llm_items),
            total_batches,
            "" if concurrency == 1 else f", {concurrency} at a time",
        )
        reporter.start("classifying", total=len(llm_items))
        try:
            stats = _classify_all(
                conn,
                batches,
                rules_by_id=rules_by_id,
                classifier=classifier,
                run_id=run_id,
                account_id=account_id,
                model_id=model_id,
                per_candidate=per_candidate,
                concurrency=concurrency,
                reporter=reporter,
            )
        finally:
            reporter.stop()

    results = tuple(per_candidate[candidate_id] for candidate_id, _ in candidates)
    return EvaluateOutcome(
        results=results,
        llm_calls=stats.llm_calls,
        structured_output_level=stats.structured_output_level,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
    )


def _rule_text_hash(rule: RuleConfig) -> str:
    llm_condition = rules.llm_atom(rule.when)
    assert llm_condition is not None, (
        f"rule {rule.id!r} reached the LLM path without an 'llm' atom; "
        "config validation guarantees every UNKNOWN rule has exactly one"
    )
    return prompt.compute_rule_text_hash(llm_condition.description)


# --- Batching (spec §5.4) -----------------------------------------------


def _group_into_batches(
    items: Sequence[_BatchItem], *, batch_size: int
) -> list[list[_BatchItem]]:
    """Group candidates by identical remaining-rule-set "where
    practical" (spec §5.4), then chunk each group to `batch_size`.
    Candidates are processed in their given (oldest-first, spec §4.1)
    order; grouping only reorders within that stability by rule-set key,
    it never changes which candidates are considered."""
    by_rule_set: dict[tuple[str, ...], list[_BatchItem]] = {}
    order: list[tuple[str, ...]] = []
    for item in items:
        key = item.remaining_rule_ids
        if key not in by_rule_set:
            by_rule_set[key] = []
            order.append(key)
        by_rule_set[key].append(item)

    batches: list[list[_BatchItem]] = []
    for key in order:
        group = by_rule_set[key]
        for start in range(0, len(group), batch_size):
            batches.append(group[start : start + batch_size])
    return batches


# --- Classification + split-and-retry (spec §5.4) ------------------------


def _build_batch_request(
    items: Sequence[_BatchItem], rules_by_id: dict[str, RuleConfig]
) -> tuple[list[CandidatePayload], dict[str, _BatchItem], list[OfferedRule]]:
    payloads: list[CandidatePayload] = []
    item_by_payload_id: dict[str, _BatchItem] = {}
    all_rule_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        payload_id = f"c{index}"
        built = prompt.build_candidate_payload(
            item.candidate, payload_id=payload_id, offered=item.remaining_rule_ids
        )
        payloads.append(built.payload)
        item_by_payload_id[payload_id] = item
        all_rule_ids.update(item.remaining_rule_ids)
    offered_rules = [
        OfferedRule(rule_id=rule_id, description=_llm_description(rules_by_id[rule_id]))
        for rule_id in sorted(all_rule_ids)
    ]
    return payloads, item_by_payload_id, offered_rules


def _llm_description(rule: RuleConfig) -> str:
    llm_condition = rules.llm_atom(rule.when)
    assert llm_condition is not None
    return llm_condition.description


def _classify_all(
    conn: sqlite3.Connection,
    batches: Sequence[Sequence[_BatchItem]],
    *,
    rules_by_id: dict[str, RuleConfig],
    classifier: Classifier,
    run_id: str,
    account_id: int,
    model_id: str,
    per_candidate: dict[int, CandidateResult],
    concurrency: int,
    reporter: Progress,
) -> _BatchStats:
    """Classify every batch and record the results.

    The model calls may overlap (`concurrency > 1`); the recording never
    does. Every write goes through `conn` on this thread, in batch order
    -- not completion order -- so the database, the audit trail, and the
    report come out identical no matter what the concurrency is set to
    or how the provider happened to schedule the requests. The only
    thing `max_concurrent_requests` is allowed to change is how long the
    phase takes.

    `sqlite3` connections are not safe to share across threads, and
    `state.transaction` is explicit that a transaction must never be
    held open across network I/O; keeping every write on this thread is
    what satisfies both at once.
    """
    total = len(batches)

    def label(index: int) -> str:
        return f"{index}/{total}"

    def network(index: int, batch: Sequence[_BatchItem]) -> list[_BatchAttempt]:
        logger.info(
            "run %s: classifying batch %d/%d (%d mails)",
            run_id,
            index,
            total,
            len(batch),
        )
        return _call_classifier(
            batch,
            rules_by_id=rules_by_id,
            classifier=classifier,
            run_id=run_id,
            batch_label=label(index),
        )

    stats = _BatchStats(llm_calls=0)

    def record(batch: Sequence[_BatchItem], attempts: list[_BatchAttempt]) -> None:
        nonlocal stats
        for attempt in attempts:
            stats = _combine_stats(
                stats,
                _apply_attempt(
                    conn,
                    attempt,
                    rules_by_id=rules_by_id,
                    run_id=run_id,
                    account_id=account_id,
                    model_id=model_id,
                    per_candidate=per_candidate,
                ),
            )
        reporter.advance(len(batch))

    if concurrency <= 1:
        for index, batch in enumerate(batches, start=1):
            record(batch, network(index, batch))
        return stats

    # `executor.map` yields in submission order, which is what keeps the
    # recording deterministic. Interrupting mid-phase still has to wait
    # for the requests already in flight to come back -- there is no way
    # to abandon a socket a worker thread is blocked on -- but the ones
    # merely queued are dropped rather than started.
    executor = ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix="lia-classify"
    )
    try:
        results = executor.map(network, range(1, total + 1), batches)
        for batch, attempts in zip(batches, results, strict=True):
            record(batch, attempts)
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return stats


def _call_classifier(
    items: Sequence[_BatchItem],
    *,
    rules_by_id: dict[str, RuleConfig],
    classifier: Classifier,
    run_id: str,
    allow_retry: bool = True,
    batch_label: str = "1/1",
) -> list[_BatchAttempt]:
    """The network half of classifying one batch, and nothing else.

    Touches no database and no shared mutable state, which is what makes
    it safe to run for several batches at once (see
    `_classify_concurrently`). Everything that has to be *written* comes
    back in the returned attempts for the caller to apply.

    If the response is unparseable or wholly invalid (spec §5.4 point 2),
    the batch is split in half and each half retried exactly once
    (`allow_retry=False` on the recursive call prevents a second split),
    which is why this returns a list rather than a single attempt.
    """
    payloads, item_by_payload_id, offered_rules = _build_batch_request(
        items, rules_by_id
    )

    outcome: ClassifyOutcome | None = None
    try:
        completed = classifier.classify(payloads, offered_rules)
    except Exception as exc:  # noqa: BLE001 - explicit recorded outcome below
        wholly_invalid = True
        failure_reason = f"classifier raised {type(exc).__name__}: {exc}"
        logger.debug(
            "run %s: batch %s: classify() raised %s",
            run_id,
            batch_label,
            failure_reason,
        )
    else:
        outcome = completed
        wholly_invalid = len(completed.results) == 0
        failure_reason = "unparseable or wholly invalid model response"
        logger.debug(
            "run %s: batch %s: structured_output=%s latency_ms=%s "
            "input_tokens=%s output_tokens=%s valid=%d invalid=%d missing=%d",
            run_id,
            batch_label,
            outcome.structured_output_level,
            outcome.latency_ms,
            outcome.input_tokens,
            outcome.output_tokens,
            len(outcome.results),
            len(outcome.invalid),
            len(outcome.missing),
        )

    if wholly_invalid and allow_retry and len(items) > 1:
        logger.debug(
            "run %s: batch %s wholly invalid (%s); splitting %d item(s) and retrying",
            run_id,
            batch_label,
            failure_reason,
            len(items),
        )
        midpoint = len(items) // 2
        halves = []
        for half_index, half in enumerate(
            (items[:midpoint], items[midpoint:]), start=1
        ):
            halves.extend(
                _call_classifier(
                    half,
                    rules_by_id=rules_by_id,
                    classifier=classifier,
                    run_id=run_id,
                    allow_retry=False,
                    batch_label=f"{batch_label} (retry {half_index}/2)",
                )
            )
        return halves

    return [
        _BatchAttempt(
            items=tuple(items),
            item_by_payload_id=item_by_payload_id,
            outcome=None if wholly_invalid else outcome,
            failure_reason=failure_reason,
        )
    ]


def _apply_attempt(
    conn: sqlite3.Connection,
    attempt: _BatchAttempt,
    *,
    rules_by_id: dict[str, RuleConfig],
    run_id: str,
    account_id: int,
    model_id: str,
    per_candidate: dict[int, CandidateResult],
) -> _BatchStats:
    """The bookkeeping half: record what `_call_classifier` came back
    with. Pure local work -- one transaction per batch rather than one
    fsync per row (see `state.transaction`) -- and always on the thread
    that owns `conn`, never inside a worker.

    Items the model answered invalidly are `invalid_response`; a
    well-formed response missing some ids finalises those as
    `no_llm_response`."""
    items = attempt.items
    outcome = attempt.outcome
    item_by_payload_id = attempt.item_by_payload_id

    if outcome is None:
        failure_reason = attempt.failure_reason
        with state.transaction(conn):
            for item in items:
                _mark_invalid(item, per_candidate, error=failure_reason)
                state.insert_classification(
                    conn,
                    run_id=run_id,
                    candidate_id=item.candidate_id,
                    input_level="metadata",
                    input_hash=item.input_hash,
                    offered_rules=item.remaining_rule_ids,
                    matches=(),
                    needs_content=False,
                    reason=None,
                    valid=False,
                    error=failure_reason,
                    latency_ms=None,
                )
        return _BatchStats(llm_calls=1)

    with state.transaction(conn):
        _apply_outcome(
            conn,
            outcome,
            item_by_payload_id,
            rules_by_id=rules_by_id,
            run_id=run_id,
            account_id=account_id,
            model_id=model_id,
            per_candidate=per_candidate,
        )
    # Defence in depth: a well-behaved `Classifier` always partitions
    # every requested payload id across `results`/`invalid`/`missing`
    # (that invariant is what `prompt.parse_classification_response`
    # guarantees), but `Classifier` is an adapter-implemented Protocol,
    # not a sealed type, and its response is untrusted. If some future
    # or third-party adapter ever violates that invariant, a candidate
    # must still get a terminal result rather than silently vanishing
    # from the run (and crashing `evaluate_candidates` with a KeyError
    # when the final result tuple is assembled) -- treat it exactly like
    # `missing`.
    for item in items:
        if item.candidate_id not in per_candidate:
            _mark_missing(item, per_candidate)
    return _BatchStats(
        llm_calls=1,
        structured_output_level=outcome.structured_output_level,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
    )


def _mark_invalid(
    item: _BatchItem, per_candidate: dict[int, CandidateResult], *, error: str
) -> None:
    if item.resolved_matches:
        per_candidate[item.candidate_id] = CandidateResult(
            candidate_id=item.candidate_id,
            matches=item.resolved_matches,
            status=None,
            reason=None,
            offered_rules=item.remaining_rule_ids,
            input_hash=item.input_hash,
            valid=False,
            error=error,
        )
    else:
        per_candidate[item.candidate_id] = CandidateResult(
            candidate_id=item.candidate_id,
            matches=(),
            status="invalid_response",
            reason=None,
            offered_rules=item.remaining_rule_ids,
            input_hash=item.input_hash,
            valid=False,
            error=error,
        )


def _mark_missing(item: _BatchItem, per_candidate: dict[int, CandidateResult]) -> None:
    if item.resolved_matches:
        per_candidate[item.candidate_id] = CandidateResult(
            candidate_id=item.candidate_id,
            matches=item.resolved_matches,
            status=None,
            reason=None,
            offered_rules=item.remaining_rule_ids,
            input_hash=item.input_hash,
            valid=False,
            error="absent from model response",
        )
    else:
        per_candidate[item.candidate_id] = CandidateResult(
            candidate_id=item.candidate_id,
            matches=(),
            status="no_llm_response",
            reason=None,
            offered_rules=item.remaining_rule_ids,
            input_hash=item.input_hash,
            valid=False,
            error="absent from model response",
        )


# --- Response validation (spec §5.2, §5.3) -------------------------------
#
# This is the safety boundary contracts §5.3 requires to exist exactly
# once, in the caller, so it cannot be skipped per adapter. A
# `Classification` arriving here is untrusted in every field: its
# `payload_id` may not be one we sent, and its matches may name rules
# never offered for that candidate or repeat a rule. None of that may
# ever reach the policy engine; anything that fails is silently dropped,
# never escalated into a wider action than what was actually offered.


def _apply_outcome(
    conn: sqlite3.Connection,
    outcome: ClassifyOutcome,
    item_by_payload_id: dict[str, _BatchItem],
    *,
    rules_by_id: dict[str, RuleConfig],
    run_id: str,
    account_id: int,
    model_id: str,
    per_candidate: dict[int, CandidateResult],
) -> None:
    seen_payload_ids: set[str] = set()

    for classification in outcome.results:
        payload_id = classification.payload_id
        item = item_by_payload_id.get(payload_id)
        if item is None:
            # Names a candidate id never in this batch (spec §5.2: "an ID
            # not present in the submitted batch is dropped"). There is
            # no real candidate row to attach a status to; audit and move
            # on — it cannot affect any real candidate's outcome.
            state.append_audit_event(
                conn,
                run_id=run_id,
                kind="classifier_unknown_candidate_id",
                data={"payload_id": payload_id},
            )
            continue
        if payload_id in seen_payload_ids:
            # A candidate id must be present exactly once (spec §5.3);
            # a repeat makes the whole item ambiguous.
            _mark_invalid(
                item, per_candidate, error="candidate id repeated in response"
            )
            continue
        seen_payload_ids.add(payload_id)
        _resolve_item(
            conn,
            classification,
            item,
            rules_by_id=rules_by_id,
            run_id=run_id,
            account_id=account_id,
            model_id=model_id,
            per_candidate=per_candidate,
        )

    for payload_id in outcome.invalid:
        item = item_by_payload_id.get(payload_id)
        if item is not None and payload_id not in seen_payload_ids:
            _mark_invalid(
                item, per_candidate, error="response item failed structural validation"
            )
            state.insert_classification(
                conn,
                run_id=run_id,
                candidate_id=item.candidate_id,
                input_level="metadata",
                input_hash=item.input_hash,
                offered_rules=item.remaining_rule_ids,
                matches=(),
                needs_content=False,
                reason=None,
                valid=False,
                error="response item failed structural validation",
                latency_ms=None,
            )

    for payload_id in outcome.missing:
        item = item_by_payload_id.get(payload_id)
        if item is not None and payload_id not in seen_payload_ids:
            _mark_missing(item, per_candidate)


def _resolve_item(
    conn: sqlite3.Connection,
    classification: Classification,
    item: _BatchItem,
    *,
    rules_by_id: dict[str, RuleConfig],
    run_id: str,
    account_id: int,
    model_id: str,
    per_candidate: dict[int, CandidateResult],
) -> None:
    """Validate one candidate's `Classification` against exactly the
    vocabulary it was offered, then decide its final status. This is
    where a hostile response (an unoffered rule id, a duplicate) is
    neutralised: none of those checks are optional, and every rejected
    match is dropped rather than merely logged, so it can never influence
    the policy engine. There is no confidence to threshold (spec §5.3):
    every rule id that survives vocabulary validation is accepted
    outright.
    """
    accepted: list[ValidatedMatch] = list(item.resolved_matches)
    offered_set = set(item.remaining_rule_ids)
    seen_rule_ids: set[str] = set()
    raw_matches_for_audit: list[str] = list(classification.matches)

    if classification.needs_content:
        # Escalation requested (spec §5.3). This module has no mailbox
        # access to fetch an excerpt — that orchestration is Unit 5's
        # (spec §4.2 step 7) — so, per "if escalation is unavailable for
        # any reason, the item is treated as unknown and produces no
        # action", the *entire* response for this candidate is
        # discarded: no match is accepted from it, regardless of what
        # `matches` also contained, and — critically — nothing is cached
        # either way, because the model did not decline or confirm any
        # of these rules, it deferred. Caching either answer here would
        # permanently suppress a future re-ask once escalation is
        # actually available.
        state.append_audit_event(
            conn,
            run_id=run_id,
            kind="classifier_needs_content_unavailable",
            subject=str(item.candidate_id),
            data={"offered_rules": sorted(offered_set)},
        )
    else:
        for rule_id in classification.matches:
            if rule_id not in offered_set:
                # A rule id this candidate was never offered — exactly
                # the shape of a prompt-injection attempt trying to
                # claim an arbitrary rule. Drop it; it never reaches
                # policy.
                state.append_audit_event(
                    conn,
                    run_id=run_id,
                    kind="classifier_unoffered_rule",
                    subject=str(item.candidate_id),
                    data={"rule_id": rule_id},
                )
                continue
            if rule_id in seen_rule_ids:
                state.append_audit_event(
                    conn,
                    run_id=run_id,
                    kind="classifier_duplicate_rule",
                    subject=str(item.candidate_id),
                    data={"rule_id": rule_id},
                )
                continue

            seen_rule_ids.add(rule_id)
            accepted.append(ValidatedMatch(rule_id=rule_id))

        # Every offered rule got a confident yes or no this run (a rule
        # that requested escalation instead never reaches this branch at
        # all — see the `needs_content` arm above) — cache it either way
        # so a re-run does not re-ask (spec §13). Caching the "yes" too is
        # what lets a message whose remote mutation failed last run
        # (wrong trash_mailbox, an unadvertised capability, ...) retry
        # that mutation on the next run instead of being reclassified
        # from scratch every time.
        for rule_id in offered_set:
            rule_cfg = rules_by_id[rule_id]
            rule_text_hash = _rule_text_hash(rule_cfg)
            state.record_decision(
                conn,
                account_id=account_id,
                fingerprint=item.candidate.fingerprint,
                rule_id=rule_id,
                rule_text_hash=rule_text_hash,
                input_hash=item.input_hash,
                model_id=model_id,
                prompt_version=prompt.PROMPT_VERSION,
                matched=rule_id in seen_rule_ids,
            )

    reason = classification.reason
    if reason is not None and len(reason) > _REASON_CAP:
        reason = reason[:_REASON_CAP]

    if accepted:
        status = None
    elif item.cache_hit:
        status = "cached_no_match"
    else:
        status = "no_match"

    state.insert_classification(
        conn,
        run_id=run_id,
        candidate_id=item.candidate_id,
        input_level="metadata",
        input_hash=item.input_hash,
        offered_rules=item.remaining_rule_ids,
        matches=raw_matches_for_audit,
        needs_content=classification.needs_content,
        reason=reason,
        valid=True,
        error=None,
        latency_ms=None,
    )

    per_candidate[item.candidate_id] = CandidateResult(
        candidate_id=item.candidate_id,
        matches=tuple(accepted),
        status=status,
        reason=reason,
        offered_rules=item.remaining_rule_ids,
        input_hash=item.input_hash,
        valid=True,
        error=None,
    )
