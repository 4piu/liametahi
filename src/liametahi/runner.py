"""Phase orchestration: the three-phase run of specification §3 / §4.

This is the integration point named in contracts §7: it sequences Unit
2's scan phase (`imap_adapter.scan`), Unit 3's evaluate phase
(`evaluate.evaluate_candidates`), and Unit 4's execute/reconcile phases
(`execute.execute_items` / `execute.reconcile_task`), plus Unit 4's
winner-selection (`policy.decide`). It never reimplements what those
modules already do; it only decides *when* to call them and *what* to do
with SQLite rows that require information only available at the
integration layer (see the "raw SQL" notes below, which follow the same
precedent already set by `backup.py` and `report.py`: a narrowly scoped
query, documented at the call site, when `state.py`'s typed surface does
not (yet) cover something this module needs).

Ordering and the non-negotiable safety properties (spec §3, §4, §10):

1. The advisory task lock is acquired **before any IMAP connection and
   before any SQLite write** -- including the reconcile pass. Reconcile
   (spec §4.0) explicitly assumes "the task lock guarantees any earlier
   run of the *same* task has already exited... before this run could
   acquire the lock" (see `execute.reconcile_task`'s own docstring); that
   guarantee only holds if reconcile runs *after* lock acquisition, so
   that is the order implemented here, even though it reads "reconcile,
   then lock" if the two clauses of the work-unit brief are read as a
   strict sequence rather than as two properties step 1 must satisfy.
   This is documented as a deliberate, conservative reading, not an
   oversight -- see the final report for the reasoning.
2. The scan phase connects, fetches, and disconnects; the evaluate phase
   never touches a mailbox connection at all, so an idle local-model
   batch never holds a socket open.
3. Protected messages (`rules.is_protected`) are filtered before *any*
   candidate reaches a classifier -- protected candidates never enter
   `evaluate.evaluate_candidates`, so they can never trigger an LLM call.
4. A dry run performs no backup write and no remote mutation (enforced
   inside `execute.execute_items`), but a `result_items` row -- and a
   full stored report -- is written regardless.
"""

import json
import re
import sqlite3
import ssl
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.message import Message
from html import unescape
from pathlib import Path
from typing import Literal

from liametahi import backup, evaluate, execute, policy, prompt, report, rules, state
from liametahi.classifier import Classifier, OfferedRule
from liametahi.classifier.anthropic import AnthropicClassifier
from liametahi.classifier.openai_compatible import OpenAICompatibleClassifier
from liametahi.config import (
    AccountConfig,
    Config,
    ModelConfig,
    RuleConfig,
    TaskConfig,
)
from liametahi.domain import Candidate, MessageKey
from liametahi.imap_adapter import ImapMailbox, MailboxAdapter, scan
from liametahi.locks import LockTimeout, task_lock
from liametahi.logging import get_logger, register_secret

logger = get_logger(__name__)

EXIT_SUCCESS = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_BAD_CONFIG = 2
EXIT_AUTH_FAILURE = 4
EXIT_TASK_RUNNING = 5

MailboxFactory = Callable[[AccountConfig], MailboxAdapter]
ClassifierFactory = Callable[[ModelConfig], Classifier]

_REASON_CAP = 200


def default_mailbox_factory(account: AccountConfig) -> MailboxAdapter:
    """The real, `imaplib`-backed adapter (spec §4.1). Tests inject
    `FakeMailbox` via `mailbox_factory` instead.

    Certificate-verifying TLS is the default and the only option for a
    real account: `tls_insecure_skip_verify` is rejected at config load
    for any non-loopback host (spec §12), so by the time a context is
    built here the flag can only mean a local development server.
    """
    ssl_context: ssl.SSLContext | None = None
    if account.tls_insecure_skip_verify:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    return ImapMailbox(
        host=account.host,
        port=account.port,
        username=account.username,
        password=account.password,
        ssl_context=ssl_context,
    )


def default_classifier_factory(model: ModelConfig) -> Classifier:
    """Selects the real adapter for a model's configured provider (spec
    §8). Tests inject `FakeClassifier` via `classifier_factory` instead."""
    if model.provider == "openai_compatible":
        return OpenAICompatibleClassifier(model)
    if model.provider == "anthropic":
        return AnthropicClassifier(model)
    raise ValueError(f"unknown model provider: {model.provider!r}")  # pragma: no cover


def _close_mailbox(mailbox: object) -> None:
    """Close a mailbox connection if it exposes `close()`. `MailboxAdapter`
    (contracts §5.4) is a structural `Protocol` with no `close()` method
    -- `ImapMailbox` has a real socket to release, `FakeMailbox` does
    not -- so this is duck-typed rather than assumed."""
    close = getattr(mailbox, "close", None)
    if callable(close):
        close()


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What `cli.py`'s `run` command needs to print and exit with."""

    run_id: str | None
    exit_code: int
    dry_run: bool
    report_data: report.ReportData | None
    diagnostic: str | None = None  # a stderr-only one-liner (spec §10 exit 5, etc.)


# --- Entry point ------------------------------------------------------


def run_task(
    *,
    config: Config,
    config_path: Path,
    task_name: str,
    dry_run: bool,
    fail_fast: bool,
    reevaluate: bool,
    wait_seconds: float,
    mailbox_factory: MailboxFactory = default_mailbox_factory,
    classifier_factory: ClassifierFactory = default_classifier_factory,
    on_run_created: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> RunOutcome:
    """Run one task end to end (spec §3, §4). See the module docstring
    for the safety-critical ordering this function enforces.

    `on_run_created` is invoked with the new `run_id` immediately after
    the run row is created (before any phase executes), so `cli.py` can
    print it "at the start" of `run` (spec §9) without this module doing
    any `print()`/`typer.echo()` itself (contracts §2's logging rule).
    """
    if task_name not in config.tasks:
        return RunOutcome(
            run_id=None,
            exit_code=EXIT_BAD_CONFIG,
            dry_run=dry_run,
            report_data=None,
            diagnostic=(
                f"unknown task {task_name!r}; configured tasks: {sorted(config.tasks)}"
            ),
        )

    try:
        with task_lock(
            config.settings.task_lock_dir,
            config_path,
            task_name,
            wait_seconds=wait_seconds,
        ):
            return _run_locked(
                config=config,
                task_name=task_name,
                dry_run=dry_run,
                fail_fast=fail_fast,
                reevaluate=reevaluate,
                mailbox_factory=mailbox_factory,
                classifier_factory=classifier_factory,
                on_run_created=on_run_created,
                now=now or datetime.now(UTC),
            )
    except LockTimeout as exc:
        # spec §10: no run report, no DB write, no mailbox/model work.
        return RunOutcome(
            run_id=None,
            exit_code=EXIT_TASK_RUNNING,
            dry_run=dry_run,
            report_data=None,
            diagnostic=str(exc),
        )
    except ValueError as exc:
        # `--wait` given a negative/non-finite value (bad invocation).
        return RunOutcome(
            run_id=None,
            exit_code=EXIT_BAD_CONFIG,
            dry_run=dry_run,
            report_data=None,
            diagnostic=str(exc),
        )


def _run_locked(
    *,
    config: Config,
    task_name: str,
    dry_run: bool,
    fail_fast: bool,
    reevaluate: bool,
    mailbox_factory: MailboxFactory,
    classifier_factory: ClassifierFactory,
    on_run_created: Callable[[str], None] | None,
    now: datetime,
) -> RunOutcome:
    """Everything from here on runs with the task lock held (spec §10)."""
    task = config.tasks[task_name]
    account_cfg = config.accounts[task.account]
    model_cfg = config.models[task.model]
    register_secret(account_cfg.password)
    register_secret(model_cfg.api_key)

    conn = state.open_database(config.settings.state_db)
    try:
        account_id = state.upsert_account(
            conn,
            name=task.account,
            host=account_cfg.host,
            username=account_cfg.username,
        )
        run_id = state.new_run_id()
        state.create_run(
            conn,
            run_id=run_id,
            task=task_name,
            account_id=account_id,
            model_name=task.model,
            provider=model_cfg.provider,
            model_id=model_cfg.model,
            dry_run=dry_run,
            reevaluate=reevaluate,
            fetch_headers=task.fetch_headers,
            config_hash="",
        )
        if on_run_created is not None:
            on_run_created(run_id)

        try:
            outcome = _run_phases(
                conn,
                config=config,
                task=task,
                task_name=task_name,
                account_id=account_id,
                account_cfg=account_cfg,
                model_cfg=model_cfg,
                run_id=run_id,
                dry_run=dry_run,
                fail_fast=fail_fast,
                reevaluate=reevaluate,
                mailbox_factory=mailbox_factory,
                classifier_factory=classifier_factory,
                now=now,
            )
        except _AuthFailure as exc:
            state.finish_run(
                conn,
                run_id=run_id,
                exit_code=EXIT_AUTH_FAILURE,
                candidates_scanned=0,
                llm_calls=0,
            )
            logger.error("run %s: authentication failure: %s", run_id, exc)
            report_data = report.load_report(conn, run_id)
            return RunOutcome(
                run_id=run_id,
                exit_code=EXIT_AUTH_FAILURE,
                dry_run=dry_run,
                report_data=report_data,
                diagnostic=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - recorded outcome, not swallowed
            state.finish_run(
                conn,
                run_id=run_id,
                exit_code=EXIT_RUNTIME_FAILURE,
                candidates_scanned=0,
                llm_calls=0,
            )
            logger.error("run %s: unhandled error: %s", run_id, exc)
            state.append_audit_event(
                conn, run_id=run_id, kind="run_failed", data={"error": str(exc)}
            )
            report_data = report.load_report(conn, run_id)
            return RunOutcome(
                run_id=run_id,
                exit_code=EXIT_RUNTIME_FAILURE,
                dry_run=dry_run,
                report_data=report_data,
                diagnostic=f"run failed: {exc}",
            )

        _prune_old_candidate_content(conn, settings=config.settings, now=now)
        return outcome
    finally:
        state.close_database(conn)


class _AuthFailure(Exception):
    """Raised when connecting/authenticating to IMAP fails (spec §9 exit
    code 4). Kept distinct from every other runtime failure so `_run_locked`
    can map it to a different exit code."""


def _connect(
    mailbox_factory: MailboxFactory, account_cfg: AccountConfig
) -> MailboxAdapter:
    try:
        return mailbox_factory(account_cfg)
    except Exception as exc:
        raise _AuthFailure(str(exc)) from exc


def _run_phases(
    conn: sqlite3.Connection,
    *,
    config: Config,
    task: TaskConfig,
    task_name: str,
    account_id: int,
    account_cfg: AccountConfig,
    model_cfg: ModelConfig,
    run_id: str,
    dry_run: bool,
    fail_fast: bool,
    reevaluate: bool,
    mailbox_factory: MailboxFactory,
    classifier_factory: ClassifierFactory,
    now: datetime,
) -> RunOutcome:
    rules_by_id = {rule.id: rule for rule in task.rules}
    rule_order = {rule.id: index for index, rule in enumerate(task.rules)}

    # --- Phase 1: scan (spec §4.1) + reconcile (spec §4.0) -------------
    logger.info("run %s: connecting to account %s", run_id, task.account)
    scan_mailbox = _connect(mailbox_factory, account_cfg)
    try:
        reconcile_summary = execute.reconcile_task(
            conn, task=task_name, mailbox=scan_mailbox
        )
        if reconcile_summary.rows_closed:
            logger.info(
                "run %s: reconcile closed %d stale action row(s)",
                run_id,
                reconcile_summary.rows_closed,
            )
        logger.info(
            "run %s: scanning %s", run_id, ", ".join(task.source_mailboxes)
        )
        scan_result = scan(
            scan_mailbox,
            conn,
            account_id=account_id,
            source_mailboxes=task.source_mailboxes,
            fetch_headers=task.fetch_headers,
            max_candidates_per_run=task.max_candidates_per_run,
        )
        for mailbox_result in scan_result.mailboxes:
            logger.debug(
                "run %s: %s: %d new, %d re-identified (uidvalidity=%s%s)",
                run_id,
                mailbox_result.mailbox,
                mailbox_result.new_candidates,
                mailbox_result.reidentified_candidates,
                mailbox_result.uidvalidity,
                ", changed" if mailbox_result.uidvalidity_changed else "",
            )
        capped_note = (
            " (stopped at max_candidates_per_run)" if scan_result.stopped_at_cap else ""
        )
        logger.info(
            "run %s: scan complete: %d candidate(s)%s",
            run_id,
            scan_result.candidates_scanned,
            capped_note,
        )
    finally:
        _close_mailbox(scan_mailbox)

    # --- Phase 2: evaluate (spec §4.2) -- no mailbox connection open ---
    live_candidates = _live_candidates(
        conn, account_id=account_id, mailboxes=task.source_mailboxes
    )
    protected_ids: set[int] = set()
    eligible: list[tuple[int, Candidate]] = []
    for candidate_id, candidate in live_candidates:
        if rules.is_protected(
            candidate,
            protected_flags=task.protect.flags,
            protected_senders=task.protect.senders,
            protect_unread=task.protect.unread,
        ):
            protected_ids.add(candidate_id)
            state.insert_result_item(
                conn, run_id=run_id, candidate_id=candidate_id, status="protected"
            )
        else:
            eligible.append((candidate_id, candidate))

    logger.info(
        "run %s: evaluating %d candidate(s) (%d protected, excluded)",
        run_id,
        len(eligible),
        len(protected_ids),
    )
    classifier = classifier_factory(model_cfg)
    evaluate_outcome = evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        model_config=model_cfg,
        model_id=model_cfg.model,
        classifier=classifier,
        candidates=eligible,
        now=now,
        reevaluate=reevaluate,
    )
    logger.info(
        "run %s: evaluate complete: %d LLM call(s)%s",
        run_id,
        evaluate_outcome.llm_calls,
        f" (structured_output={evaluate_outcome.structured_output_level})"
        if evaluate_outcome.structured_output_level
        else "",
    )
    candidates_by_id = dict(eligible)
    results_by_id = {r.candidate_id: r for r in evaluate_outcome.results}

    _run_content_escalation(
        conn,
        run_id=run_id,
        account_id=account_id,
        task=task,
        rules_by_id=rules_by_id,
        model_cfg=model_cfg,
        classifier=classifier,
        account_cfg=account_cfg,
        mailbox_factory=mailbox_factory,
        candidates_by_id=candidates_by_id,
        results_by_id=results_by_id,
    )

    execution_items: list[execute.ExecutionItem] = []
    shadowed_by_candidate: dict[int, tuple[str, ...]] = {}
    for candidate_id, candidate in eligible:
        result = results_by_id[candidate_id]
        if result.status is not None:
            state.insert_result_item(
                conn,
                run_id=run_id,
                candidate_id=candidate_id,
                status=result.status,
                detail=result.error or result.reason,
            )
            continue

        matched = [
            policy.MatchedRule(
                rule_id=m.rule_id,
                priority=rules_by_id[m.rule_id].priority,
                config_order=rule_order[m.rule_id],
            )
            for m in result.matches
        ]
        decision = policy.decide(
            protected=False,
            matches=matched,
            rules_by_id=rules_by_id,
            trash_mailbox=account_cfg.trash_mailbox,
        )
        if decision.status != "matched" or decision.winning_rule is None:
            # Defensive only: `result.matches` non-empty guarantees a
            # winner (spec §7.4's select_winner never returns None for a
            # non-empty input).
            state.insert_result_item(
                conn, run_id=run_id, candidate_id=candidate_id, status="no_match"
            )
            continue

        execution_items.append(
            execute.ExecutionItem(
                candidate_id=candidate_id,
                key=candidate.key,
                fingerprint=candidate.fingerprint,
                message_id=candidate.message_id,
                winning_rule=decision.winning_rule,
                actions=decision.actions,
            )
        )
        if decision.shadowed:
            shadowed_by_candidate[candidate_id] = decision.shadowed

    # --- Phase 3: execute (spec §4.3) -----------------------------------
    logger.info("run %s: executing %d item(s)", run_id, len(execution_items))
    exec_mailbox = _connect(mailbox_factory, account_cfg)
    try:
        execution_items = _drop_unsupported_for_dry_run(
            conn,
            mailbox=exec_mailbox,
            run_id=run_id,
            items=execution_items,
            dry_run=dry_run,
        )
        execute_summary = execute.execute_items(
            conn,
            mailbox=exec_mailbox,
            items=execution_items,
            run_id=run_id,
            backup_dir=config.settings.backup_dir,
            max_actions_per_run=task.max_actions_per_run,
            dry_run=dry_run,
            fail_fast=fail_fast,
        )
    finally:
        _close_mailbox(exec_mailbox)

    status_counts = Counter(outcome.status for outcome in execute_summary.outcomes)
    status_note = ", ".join(
        f"{count} {status}" for status, count in sorted(status_counts.items())
    )
    stopped_note = (
        " [stopped early: --fail-fast]" if execute_summary.stopped_early else ""
    )
    logger.info(
        "run %s: execute complete: %d action(s) performed (%s)%s",
        run_id,
        execute_summary.actions_performed,
        status_note,
        stopped_note,
    )

    for outcome in execute_summary.outcomes:
        shadowed = shadowed_by_candidate.get(outcome.candidate_id)
        if shadowed and outcome.result_id is not None:
            # `execute.ExecutionItem` carries no `shadowed_by` field
            # (contracts §5's `execute.py` signature is fixed), and
            # `result_items` allows only one row per (run_id,
            # candidate_id) -- so the *other* matched-but-not-winning
            # rules for this message cannot get a row of their own. This
            # is the narrowly-scoped, documented raw-SQL update this
            # module's docstring describes: it augments the row
            # `execute.execute_items` already created for the winner
            # with the informational list of rules it shadowed, encoded
            # as JSON text (contracts §4 declares the column `TEXT`,
            # not a fixed single value).
            conn.execute(
                "UPDATE result_items SET shadowed_by = ? WHERE result_id = ?",
                (json.dumps(list(shadowed)), outcome.result_id),
            )

    exit_code = (
        EXIT_RUNTIME_FAILURE
        if any(o.status == "failed" for o in execute_summary.outcomes)
        else EXIT_SUCCESS
    )
    state.finish_run(
        conn,
        run_id=run_id,
        exit_code=exit_code,
        candidates_scanned=scan_result.candidates_scanned,
        llm_calls=evaluate_outcome.llm_calls,
        structured_output_level=evaluate_outcome.structured_output_level,
        input_tokens=evaluate_outcome.input_tokens,
        output_tokens=evaluate_outcome.output_tokens,
    )
    report_data = report.load_report(conn, run_id)
    return RunOutcome(
        run_id=run_id, exit_code=exit_code, dry_run=dry_run, report_data=report_data
    )


# --- Candidate discovery (state.py has no bulk "live candidates" query) --
#
# Precedent: backup.py's `_find_existing_backup`/`find_backups_by_key` and
# report.py's `_account_name`/`_fetch_result_items` already document this
# same gap and the same fallback (a narrowly scoped, read-only raw query
# owned by the calling module, not a new ad-hoc SQL habit). `state.py`
# exposes `get_candidate(key)` and `find_candidates_by_fingerprint`, but
# nothing that lists every still-live candidate for a task's source
# mailboxes -- exactly what the evaluate phase needs every run (spec
# §4.2, §13: deterministic conditions are "re-evaluated free on every
# run", which requires reconsidering candidates beyond just the ones this
# run's scan happened to touch, not only newly-scanned ones).
#
# "Live" here means: the candidate's own `uidvalidity` still matches the
# mailbox's current `uidvalidity` in `mailbox_state` -- i.e. it was not
# superseded by a `UIDVALIDITY` change (spec §4.1, §11). It does *not*
# detect a candidate whose message was already moved out of the mailbox
# by an earlier action while `UIDVALIDITY` stayed the same (there is no
# "resolved" flag on `candidates` in the fixed contracts §4 DDL); such a
# stale row is re-evaluated again here, but this is safe, not just
# tolerated: `execute.py`'s re-verify step (spec §4.3 point 4) always
# re-fetches by key before any mutation and reports `vanished` rather
# than acting, so at worst this produces repeat "vanished"/no-op report
# noise for a long-dead candidate, never a wrong mutation. Flagged in the
# final report as a gap `state.py`/the schema could close.


def _live_candidates(
    conn: sqlite3.Connection, *, account_id: int, mailboxes: Sequence[str]
) -> list[tuple[int, Candidate]]:
    if not mailboxes:
        return []
    placeholders = ",".join("?" for _ in mailboxes)
    rows = conn.execute(
        f"""
        SELECT c.mailbox AS mailbox, c.uidvalidity AS uidvalidity, c.uid AS uid
        FROM candidates c
        JOIN mailbox_state m
          ON m.account_id = c.account_id AND m.mailbox = c.mailbox
        WHERE c.account_id = ? AND c.mailbox IN ({placeholders})
          AND c.uidvalidity = m.uidvalidity
        ORDER BY c.internaldate ASC
        """,
        (account_id, *mailboxes),
    ).fetchall()
    result: list[tuple[int, Candidate]] = []
    for row in rows:
        key = MessageKey(
            account_id, str(row["mailbox"]), row["uidvalidity"], row["uid"]
        )
        found = state.get_candidate(conn, key=key)
        if found is not None:
            result.append(found)
    return result


# --- Dry-run capability preview (spec §7.5) -------------------------------
#
# "Both capability checks are performed and reported during --dry-run, so
# a cron user learns about an unsupported action before a real run."
# `execute.execute_items`'s dry-run branch (Unit 4's file, not this
# unit's to change) records every action as `skipped` without consulting
# capabilities at all, so this module performs the check itself, once,
# only for `--dry-run`, and reports the affected candidates as
# `unsupported` directly rather than handing them to `execute_items`. A
# real run does not need this: `execute.py` already turns an
# `UnsupportedCapability` raised by `mailbox.move`/`add_keyword` into a
# `state="unsupported"` action attempt and an `unsupported` result item.


def _drop_unsupported_for_dry_run(
    conn: sqlite3.Connection,
    *,
    mailbox: MailboxAdapter,
    run_id: str,
    items: Sequence[execute.ExecutionItem],
    dry_run: bool,
) -> list[execute.ExecutionItem]:
    if not dry_run:
        return list(items)
    caps = mailbox.capabilities()
    keyword_ok_by_mailbox: dict[str, bool] = {}
    kept: list[execute.ExecutionItem] = []
    for item in items:
        blocked_action: str | None = None
        for action in item.actions:
            if action.kind in ("trash", "move_to") and "MOVE" not in caps:
                blocked_action = action.action
                break
            if action.kind == "label":
                if item.key.mailbox not in keyword_ok_by_mailbox:
                    status = mailbox.select(item.key.mailbox, readonly=True)
                    keyword_ok_by_mailbox[item.key.mailbox] = (
                        status.accepts_custom_keywords
                    )
                if not keyword_ok_by_mailbox[item.key.mailbox]:
                    blocked_action = action.action
                    break
        if blocked_action is None:
            kept.append(item)
            continue
        state.insert_result_item(
            conn,
            run_id=run_id,
            candidate_id=item.candidate_id,
            status="unsupported",
            winning_rule=item.winning_rule,
            detail=f"'{blocked_action}' requires a capability the server does not "
            "advertise (spec §7.5)",
        )
    return kept


# --- Content escalation (spec §4.2 step 7, §5.1, §5.3) --------------------
#
# `evaluate.py` deliberately has no mailbox access and therefore discards
# (never accepts, never caches) any response whose `needs_content` flag
# is set (see its module docstring); it records `needs_content` on the
# `classifications` row so this module -- which *does* have mailbox
# access -- can find and re-drive exactly those candidates. Honours all
# three switches named in the work-unit brief, in this precedence:
#   1. `model.content_escalation.enabled` -- a hard global gate.
#   2. Per rule `allow_content_escalation` -- only rules that opt in are
#      re-offered; if none of a candidate's originally-offered rules opt
#      in, escalation is unavailable for it (spec §5.3's "if escalation
#      is unavailable for any reason, the item is treated as unknown").
#   3. `model.content_escalation.max_messages_per_run` -- caps how many
#      candidates get an excerpt fetch in this run at all, applied in
#      the same oldest-first order candidates are otherwise processed.


def _run_content_escalation(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    account_id: int,
    task: TaskConfig,
    rules_by_id: Mapping[str, RuleConfig],
    model_cfg: ModelConfig,
    classifier: Classifier,
    account_cfg: AccountConfig,
    mailbox_factory: MailboxFactory,
    candidates_by_id: dict[int, Candidate],
    results_by_id: dict[int, evaluate.CandidateResult],
) -> None:
    if not model_cfg.content_escalation.enabled:
        return

    rows = conn.execute(
        "SELECT candidate_id, offered_rules FROM classifications "
        "WHERE run_id = ? AND needs_content = 1 AND valid = 1",
        (run_id,),
    ).fetchall()
    if not rows:
        return

    max_messages = model_cfg.content_escalation.max_messages_per_run
    plan: list[tuple[int, tuple[str, ...]]] = []
    for row in rows:
        candidate_id = int(row["candidate_id"])
        result = results_by_id.get(candidate_id)
        if result is None or result.matches:
            continue  # already has an accepted (deterministic) match
        offered = tuple(json.loads(row["offered_rules"]))
        allowed = tuple(
            rule_id
            for rule_id in offered
            if getattr(rules_by_id.get(rule_id), "allow_content_escalation", False)
        )
        if not allowed:
            continue
        plan.append((candidate_id, allowed))
        if len(plan) >= max_messages:
            break
    if not plan:
        return

    excerpts = _fetch_excerpts(
        conn,
        mailbox_factory=mailbox_factory,
        account_cfg=account_cfg,
        account_id=account_id,
        candidates_by_id=candidates_by_id,
        candidate_ids=[cid for cid, _ in plan],
    )

    max_chars = model_cfg.content_escalation.max_chars
    for candidate_id, allowed_rules in plan:
        excerpt_text = excerpts.get(candidate_id)
        if excerpt_text is None:
            continue
        candidate = candidates_by_id[candidate_id]
        built = prompt.build_excerpt_payload(
            candidate,
            payload_id="c1",
            offered=allowed_rules,
            excerpt_text=excerpt_text,
            max_chars=max_chars,
        )
        offered_payload = [
            OfferedRule(
                rule_id=rule_id,
                description=_llm_description(rules_by_id[rule_id]),
            )
            for rule_id in allowed_rules
        ]
        try:
            outcome = classifier.classify([built.payload], offered_payload)
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            state.append_audit_event(
                conn,
                run_id=run_id,
                kind="escalation_classify_failed",
                subject=str(candidate_id),
                data={"error": str(exc)},
            )
            continue
        classification = next(
            (c for c in outcome.results if c.payload_id == "c1"), None
        )
        if classification is None:
            continue
        results_by_id[candidate_id] = _resolve_escalation_response(
            conn,
            classification=classification,
            candidate=candidate,
            candidate_id=candidate_id,
            allowed_rules=allowed_rules,
            rules_by_id=rules_by_id,
            run_id=run_id,
            account_id=account_id,
            model_id=model_cfg.model,
            input_hash=built.input_hash,
        )


def _llm_description(rule: RuleConfig) -> str:
    llm_condition = rules.llm_atom(rule.when)
    assert llm_condition is not None
    return llm_condition.description


def _rule_text_hash(rule: RuleConfig) -> str:
    llm_condition = rules.llm_atom(rule.when)
    assert llm_condition is not None
    return prompt.compute_rule_text_hash(llm_condition.description)


def _resolve_escalation_response(
    conn: sqlite3.Connection,
    *,
    classification: object,
    candidate: Candidate,
    candidate_id: int,
    allowed_rules: tuple[str, ...],
    rules_by_id: Mapping[str, RuleConfig],
    run_id: str,
    account_id: int,
    model_id: str,
    input_hash: str,
) -> evaluate.CandidateResult:
    """Validate the excerpt-level response with exactly the same rigour
    as `evaluate._resolve_item` (that function is module-private to
    `evaluate.py` and not part of the cross-unit interface, so its logic
    is reimplemented here rather than reached into): every returned rule
    id must have been offered for *this* candidate and must appear at
    most once (spec §5.2, §5.3). There is no confidence to threshold —
    every rule id that survives vocabulary validation is accepted
    outright. Nothing here ever accepts a rule id outside `allowed_rules`.
    """
    offered_set = set(allowed_rules)
    accepted: list[evaluate.ValidatedMatch] = []
    seen: set[str] = set()
    matches: tuple[str, ...] = tuple(getattr(classification, "matches", ()))
    needs_content = bool(getattr(classification, "needs_content", False))
    raw_for_audit = list(matches)

    if not needs_content:
        for rule_id in matches:
            if rule_id not in offered_set or rule_id in seen:
                continue
            seen.add(rule_id)
            accepted.append(evaluate.ValidatedMatch(rule_id=rule_id))
        for rule_id in offered_set - seen:
            rule_cfg = rules_by_id[rule_id]
            state.record_negative_decision(
                conn,
                account_id=account_id,
                fingerprint=candidate.fingerprint,
                rule_id=rule_id,
                rule_text_hash=_rule_text_hash(rule_cfg),
                input_hash=input_hash,
                model_id=model_id,
            )

    reason = getattr(classification, "reason", None)
    if reason is not None and len(reason) > _REASON_CAP:
        reason = reason[:_REASON_CAP]

    state.insert_classification(
        conn,
        run_id=run_id,
        candidate_id=candidate_id,
        input_level="excerpt",
        input_hash=input_hash,
        offered_rules=allowed_rules,
        matches=raw_for_audit,
        needs_content=needs_content,
        reason=reason,
        valid=True,
        error=None,
        latency_ms=None,
    )

    status = None if accepted else "no_match"
    return evaluate.CandidateResult(
        candidate_id=candidate_id,
        matches=tuple(accepted),
        status=status,
        reason=reason,
        offered_rules=allowed_rules,
        input_hash=input_hash,
        valid=True,
        error=None,
    )


def _fetch_excerpts(
    conn: sqlite3.Connection,
    *,
    mailbox_factory: MailboxFactory,
    account_cfg: AccountConfig,
    account_id: int,
    candidates_by_id: dict[int, Candidate],
    candidate_ids: Sequence[int],
) -> dict[int, str]:
    """Fetch a bounded plain-text excerpt for each candidate (read-only,
    `PEEK`, spec §4.2 step 7). A brief, separate reconnect: the bulk
    evaluate phase holds no mailbox connection (spec §3), but this is a
    small, bounded (`max_messages_per_run`, default 20) exception, and
    the connection is closed again before any classify() calls happen.
    """
    by_mailbox: dict[str, list[int]] = {}
    for candidate_id in candidate_ids:
        mailbox_name = candidates_by_id[candidate_id].key.mailbox
        by_mailbox.setdefault(mailbox_name, []).append(candidate_id)

    excerpts: dict[int, str] = {}
    mailbox = _connect(mailbox_factory, account_cfg)
    try:
        for mailbox_name, ids in by_mailbox.items():
            status = mailbox.select(mailbox_name, readonly=True)
            stored_uidvalidity = state.get_mailbox_uidvalidity(
                conn, account_id=account_id, mailbox=mailbox_name
            )
            if (
                stored_uidvalidity is not None
                and status.uidvalidity != stored_uidvalidity
            ):
                # Reconnect-and-recheck (spec §3, §10): UIDVALIDITY moved
                # under us since the scan phase; escalation is
                # unavailable for this mailbox's candidates this run.
                continue
            for candidate_id in ids:
                candidate = candidates_by_id[candidate_id]
                try:
                    raw = mailbox.fetch_raw(candidate.key.uid)
                except Exception as exc:
                    if backup.is_vanished_error(exc):
                        continue
                    raise
                excerpts[candidate_id] = extract_plain_text_excerpt(raw)
    finally:
        _close_mailbox(mailbox)
    return excerpts


# --- Plain-text excerpt extraction (spec §5.1) -----------------------------
#
# Nothing in the codebase does this yet: `prompt.build_excerpt_payload`'s
# own docstring is explicit that turning a raw message into "cleaned
# plain text (HTML removed, quoted history and signatures stripped)"
# "happens wherever the excerpt is fetched from the mailbox... out of
# this unit's scope" -- i.e. here. The stripping heuristics below (a
# leading `-- ` signature delimiter, an `On ... wrote:` reply-quote
# header, `>`-prefixed quoted lines) are common conventions, not a
# guarantee; the spec does not define exact rules, so this is a
# documented best-effort choice, and `sanitize_text`/the field cap in
# `prompt.build_excerpt_payload` remain the actual safety boundary for
# whatever text this produces.

_MAX_RAW_TEXT_CHARS = 20_000
_SIGNATURE_RE = re.compile(r"(?m)^-- \s*$")
_REPLY_HEADER_RE = re.compile(r"(?mi)^On .{0,120} wrote:\s*$")
_QUOTE_LINE_RE = re.compile(r"(?m)^>.*$")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


def extract_plain_text_excerpt(raw: bytes) -> str:
    """Best-effort: raw RFC 822 bytes -> cleaned plain text (spec §5.1)."""
    message = message_from_bytes(raw)
    text = _first_text_part(message)[:_MAX_RAW_TEXT_CHARS]

    reply_match = _REPLY_HEADER_RE.search(text)
    if reply_match:
        text = text[: reply_match.start()]
    signature_match = _SIGNATURE_RE.search(text)
    if signature_match:
        text = text[: signature_match.start()]
    text = _QUOTE_LINE_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _first_text_part(message: Message) -> str:
    if message.is_multipart():
        plain: str | None = None
        html: str | None = None
        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain" and plain is None:
                plain = _decode_part(part)
            elif content_type == "text/html" and html is None:
                html = _decode_part(part)
        if plain is not None:
            return plain
        if html is not None:
            return _strip_html(html)
        return ""
    content_type = message.get_content_type()
    payload = _decode_part(message)
    return _strip_html(payload) if content_type == "text/html" else payload


def _decode_part(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - malformed MIME, produce no text
        return ""
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError, ValueError:
        return payload.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    html = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", html)
    return unescape(text)


# --- Candidate content retention (spec §6, §11) ---------------------------
#
# No other module calls `state.prune_candidate_content`; it is run-time
# housekeeping with no natural owner among Units 1-4's phase callables,
# so it is invoked once per run here.


def _prune_old_candidate_content(
    conn: sqlite3.Connection, *, settings: object, now: datetime
) -> None:
    retention_days = getattr(settings, "candidate_retention_days", 90)
    cutoff = now - timedelta(days=retention_days)
    before = cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")
    state.prune_candidate_content(conn, before=before)


# --- `config check --connect` (spec §9) ------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectCheckIssue:
    severity: Literal["error"]
    account: str
    message: str


@dataclass(frozen=True, slots=True)
class ConnectCheckResult:
    checked_accounts: tuple[str, ...]
    issues: tuple[ConnectCheckIssue, ...]
    auth_failed: bool

    @property
    def ok(self) -> bool:
        return not self.issues


def connect_check(
    config: Config, *, mailbox_factory: MailboxFactory = default_mailbox_factory
) -> ConnectCheckResult:
    """spec §9: `--connect` "verifies IMAP authentication, mailbox
    existence, and MOVE/keyword capabilities" for every account actually
    used by a task."""
    accounts_used: dict[str, AccountConfig] = {}
    mailboxes_by_account: dict[str, set[str]] = {}
    needs_move: set[str] = set()
    needs_keyword: set[str] = set()
    for task in config.tasks.values():
        account_cfg = config.accounts[task.account]
        accounts_used[task.account] = account_cfg
        mailboxes = mailboxes_by_account.setdefault(task.account, set())
        mailboxes.update(task.source_mailboxes)
        if account_cfg.trash_mailbox:
            mailboxes.add(account_cfg.trash_mailbox)
        for rule in task.rules:
            for action in rule.actions:
                if action == "trash" or action.startswith("move_to:"):
                    needs_move.add(task.account)
                elif action.startswith("label:"):
                    needs_keyword.add(task.account)

    issues: list[ConnectCheckIssue] = []
    auth_failed = False
    for account_name, account_cfg in sorted(accounts_used.items()):
        try:
            mailbox = mailbox_factory(account_cfg)
        except Exception as exc:
            auth_failed = True
            issues.append(ConnectCheckIssue("error", account_name, str(exc)))
            continue
        try:
            if account_name in needs_move:
                caps = mailbox.capabilities()
                if "MOVE" not in caps:
                    issues.append(
                        ConnectCheckIssue(
                            "error",
                            account_name,
                            "server does not advertise MOVE (RFC 6851), required "
                            "by a trash/move_to action",
                        )
                    )
            for mailbox_name in sorted(mailboxes_by_account.get(account_name, ())):
                try:
                    status = mailbox.select(mailbox_name, readonly=True)
                except Exception as exc:
                    issues.append(
                        ConnectCheckIssue(
                            "error",
                            account_name,
                            f"mailbox {mailbox_name!r} could not be selected: {exc}",
                        )
                    )
                    continue
                if account_name in needs_keyword and not status.accepts_custom_keywords:
                    issues.append(
                        ConnectCheckIssue(
                            "error",
                            account_name,
                            f"mailbox {mailbox_name!r} PERMANENTFLAGS lacks \\* "
                            "required by a label: action",
                        )
                    )
        finally:
            _close_mailbox(mailbox)

    return ConnectCheckResult(
        checked_accounts=tuple(sorted(accounts_used)),
        issues=tuple(issues),
        auth_failed=auth_failed,
    )
