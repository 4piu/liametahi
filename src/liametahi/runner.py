"""Phase orchestration: the three-phase run of specification.

This is the integration point that sequences Unit
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

Ordering and the non-negotiable safety properties:

1. The advisory task lock is acquired **before any IMAP connection and
   before any SQLite write** -- including the reconcile pass. Reconcile
   explicitly assumes "the task lock guarantees any earlier
   run of the *same* task has already exited... before this run could
   acquire the lock" (see `execute.reconcile_task`'s own docstring); that
   guarantee only holds if reconcile runs *after* lock acquisition, so
   that is the order implemented here -- a deliberate, conservative
   reading chosen specifically to preserve that guarantee, not an
   oversight.
2. The scan phase connects, fetches, and disconnects; the evaluate phase
   never touches a mailbox connection at all, so an idle local-model
   batch never holds a socket open.
3. Protected messages (`rules.is_protected`) are filtered before *any*
   candidate reaches a classifier -- protected candidates never enter
   `evaluate.evaluate_candidates`, so they can never trigger a model call.
4. A dry run performs no backup write and no remote mutation (enforced
   inside `execute.execute_items`), but a `result_items` row -- and a
   full stored report -- is written regardless.
"""

import json
import re
import sqlite3
import ssl
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import Message
from html import unescape
from pathlib import Path
from typing import Literal

from liametahi import backup, evaluate, execute, policy, prompt, report, rules, state
from liametahi.classifier import Classifier
from liametahi.classifier.anthropic import AnthropicClassifier
from liametahi.classifier.jev import JevClassifier
from liametahi.classifier.openai_compatible import OpenAICompatibleClassifier
from liametahi.config import (
    AccountConfig,
    Config,
    ModelConfig,
    TaskConfig,
)
from liametahi.domain import Candidate, MessageKey
from liametahi.imap_adapter import ImapMailbox, MailboxAdapter, scan
from liametahi.locks import LockTimeout, task_lock
from liametahi.logging import get_logger, register_secret
from liametahi.progress import NullProgress, Progress

logger = get_logger(__name__)

EXIT_SUCCESS = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_BAD_CONFIG = 2
EXIT_INTERRUPTED = 3
EXIT_AUTH_FAILURE = 4
EXIT_TASK_RUNNING = 5

MailboxFactory = Callable[[AccountConfig], MailboxAdapter]
ClassifierFactory = Callable[[ModelConfig], Classifier]

_REASON_CAP = 200


def default_mailbox_factory(account: AccountConfig) -> MailboxAdapter:
    """The real, `imaplib`-backed adapter. Tests inject
    `FakeMailbox` via `mailbox_factory` instead.

    Certificate-verifying TLS is the default and the only option for a
    real account: `tls_insecure_skip_verify` is rejected at config load
    for any non-loopback host, so by the time a context is
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
    """Selects the real adapter for a model's configured provider. Tests
    inject `FakeClassifier` via `classifier_factory` instead."""
    if model.provider == "openai_compatible":
        return OpenAICompatibleClassifier(model)
    if model.provider == "anthropic":
        return AnthropicClassifier(model)
    if model.provider == "jev":
        return JevClassifier(model)
    raise ValueError(f"unknown model provider: {model.provider!r}")  # pragma: no cover


def _close_mailbox(mailbox: object) -> None:
    """Close a mailbox connection if it exposes `close()`. `MailboxAdapter`
    is a structural `Protocol` with no `close()` method
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
    diagnostic: str | None = None  # a stderr-only one-liner (exit 5, etc.)


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
    progress: Progress | None = None,
) -> RunOutcome:
    """Run one task end to end. See the module docstring
    for the safety-critical ordering this function enforces.

    `on_run_created` is invoked with the new `run_id` immediately after
    the run row is created (before any phase executes), so `cli.py` can
    print it "at the start" of `run` without this module doing
    any `print()`/`typer.echo()` itself (logging goes through the logger).
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
                progress=progress or NullProgress(),
            )
    except LockTimeout as exc:
        # No run report, no DB write, no mailbox/model work.
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
    except OSError as exc:
        # `task_lock` creates `settings.task_lock_dir` (and opens the
        # lock file inside it); an unwritable location is a config
        # problem, not a mid-run failure -- and, same as `LockTimeout`,
        # nothing here ever got as far as opening the state database, so
        # there is no run report to produce.
        return RunOutcome(
            run_id=None,
            exit_code=EXIT_BAD_CONFIG,
            dry_run=dry_run,
            report_data=None,
            diagnostic=f"settings.task_lock_dir: {exc}",
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
    progress: Progress,
) -> RunOutcome:
    """Everything from here on runs with the task lock held."""
    task = config.tasks[task_name]
    account_cfg = config.accounts[task.account]
    register_secret(account_cfg.password)
    # A task no longer names one `model:` -- each
    # of its rules' referenced processors carries its own. Register every
    # configured model's api_key up front rather than trying to figure
    # out in advance which ones this particular task will actually touch;
    # registering an unused one is harmless.
    for model_cfg in config.models.values():
        register_secret(model_cfg.api_key)
    model_name_summary, provider_summary, model_id_summary = _summarize_task_models(
        task, config
    )

    try:
        conn = state.open_database(config.settings.state_db)
    except OSError as exc:
        # `open_database` creates `settings.state_db`'s parent directory;
        # an unwritable location is a config problem. Nothing has been
        # opened yet, so there is no `conn` to close and no run row to
        # produce a report for.
        return RunOutcome(
            run_id=None,
            exit_code=EXIT_BAD_CONFIG,
            dry_run=dry_run,
            report_data=None,
            diagnostic=f"settings.state_db: {exc}",
        )
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
            model_name=model_name_summary,
            provider=provider_summary,
            model_id=model_id_summary,
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
                run_id=run_id,
                dry_run=dry_run,
                fail_fast=fail_fast,
                reevaluate=reevaluate,
                mailbox_factory=mailbox_factory,
                classifier_factory=classifier_factory,
                now=now,
                progress=progress,
            )
        except _AuthFailure as exc:
            state.finish_run(
                conn,
                run_id=run_id,
                exit_code=EXIT_AUTH_FAILURE,
                candidates_scanned=0,
                model_calls=0,
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
        except KeyboardInterrupt:
            # SIGINT is turned into this by Python itself; `cli.py`
            # additionally turns SIGTERM into it so both
            # signals reach exactly one handler. Caught here rather than
            # left to propagate so the run gets a proper terminal report
            # instead of the silent, unreported abort a bare
            # `KeyboardInterrupt` would otherwise leave behind -- the
            # `with task_lock(...)` block in `run_task` still releases
            # the lock normally either way, since this returns rather
            # than re-raising.
            state.finish_run(
                conn,
                run_id=run_id,
                exit_code=EXIT_INTERRUPTED,
                candidates_scanned=0,
                model_calls=0,
            )
            logger.warning("run %s: interrupted (SIGINT/SIGTERM)", run_id)
            state.append_audit_event(conn, run_id=run_id, kind="run_interrupted")
            report_data = report.load_report(conn, run_id)
            return RunOutcome(
                run_id=run_id,
                exit_code=EXIT_INTERRUPTED,
                dry_run=dry_run,
                report_data=report_data,
                diagnostic="run interrupted (SIGINT/SIGTERM)",
            )
        except Exception as exc:  # noqa: BLE001 - recorded outcome, not swallowed
            state.finish_run(
                conn,
                run_id=run_id,
                exit_code=EXIT_RUNTIME_FAILURE,
                candidates_scanned=0,
                model_calls=0,
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

        return outcome
    finally:
        state.close_database(conn)


class _AuthFailure(Exception):
    """Raised when connecting/authenticating to IMAP fails (exit
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
    run_id: str,
    dry_run: bool,
    fail_fast: bool,
    reevaluate: bool,
    mailbox_factory: MailboxFactory,
    classifier_factory: ClassifierFactory,
    now: datetime,
    progress: Progress,
) -> RunOutcome:
    # --- Phase 1: scan + reconcile ---------------------------------------
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
        logger.info("run %s: scanning %s", run_id, ", ".join(task.source_mailboxes))
        scan_result = scan(
            scan_mailbox,
            conn,
            account_id=account_id,
            source_mailboxes=task.source_mailboxes,
            fetch_headers=task.fetch_headers,
            max_new_mails=task.max_new_mails,
            progress=progress,
        )
        for mailbox_result in scan_result.mailboxes:
            logger.debug(
                "run %s: %s: %d new, %d re-identified, %d flags refreshed "
                "(uidvalidity=%s%s)",
                run_id,
                mailbox_result.mailbox,
                mailbox_result.new_candidates,
                mailbox_result.reidentified_candidates,
                mailbox_result.flags_refreshed,
                mailbox_result.uidvalidity,
                ", changed" if mailbox_result.uidvalidity_changed else "",
            )
        capped_note = (
            " (stopped at max_new_mails)" if scan_result.stopped_at_cap else ""
        )
        logger.info(
            "run %s: scan complete: %d mail(s)%s",
            run_id,
            scan_result.candidates_scanned,
            capped_note,
        )
    finally:
        _close_mailbox(scan_mailbox)

    # --- Phase 2: evaluate -- no mailbox connection open ------------------
    # This task's pool is the union of its own
    # mailbox scan and whatever another task's rule routed here via
    # `task:<id>`, this run or a previous one.
    live_candidates = _merge_routed_candidates(
        conn,
        account_id=account_id,
        task_name=task_name,
        scanned=_live_candidates(
            conn, account_id=account_id, mailboxes=task.source_mailboxes
        ),
    )
    protected_ids: set[int] = set()
    eligible: list[tuple[int, Candidate]] = []
    # Pure local bookkeeping (no mailbox connection is open here at all),
    # so the `protected` rows go in one transaction (see
    # `state.transaction`) rather than one fsync each.
    with state.transaction(conn):
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
        "run %s: evaluating %d mail(s) (%d protected, excluded)",
        run_id,
        len(eligible),
        len(protected_ids),
    )
    # A processor's resolved `fields` selection is a static per-processor
    # choice, not a dynamic escalation -- if a still-undecided rule needs
    # a body-field-requiring processor answered, fetch the body text now,
    # before asking anything, rather than after an unsure round-trip.
    excerpt_candidate_ids = _candidates_needing_excerpt(task, config, eligible, now)
    excerpts: dict[int, str] = {}
    html_bodies: dict[int, str] = {}
    if excerpt_candidate_ids:
        logger.info(
            "run %s: fetching %d body field(s) for excerpt/html processor(s)",
            run_id,
            len(excerpt_candidate_ids),
        )
        excerpts, html_bodies = _fetch_body_texts(
            conn,
            mailbox_factory=mailbox_factory,
            account_cfg=account_cfg,
            account_id=account_id,
            candidates_by_id=dict(eligible),
            candidate_ids=excerpt_candidate_ids,
        )
    evaluate_outcome = evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier_factory=classifier_factory,
        candidates=eligible,
        now=now,
        reevaluate=reevaluate,
        excerpts=excerpts,
        html_bodies=html_bodies,
        progress=progress,
    )
    logger.info(
        "run %s: evaluate complete: %d model call(s)%s",
        run_id,
        evaluate_outcome.model_calls,
        f" (structured_output={evaluate_outcome.structured_output_level})"
        if evaluate_outcome.structured_output_level
        else "",
    )
    results_by_id = {r.candidate_id: r for r in evaluate_outcome.results}

    # The decision cache is keyed on `fingerprint`, which is stable
    # across a move -- so a message the user restores from Trash back to
    # a source mailbox re-scans under a new UID/mailbox, hits a cached
    # positive decision, and would otherwise be re-trashed with no model
    # call and no signal to the user. One batched query, over every
    # candidate that has a winning match this run, finds any whose
    # fingerprint already has a completed `trash`/`move_to:*` from an
    # earlier run; those are reported `restored` instead of executed.
    # This is deliberately unconditional -- it does not matter whether
    # the match this run came from the cache or a fresh classification,
    # and it is intentionally skip-the-whole-item rather than "only skip
    # if the winning action would itself be destructive": a message with
    # this history looks like a deliberate user restore, so the
    # conservative choice is to leave it alone entirely rather than
    # second-guess which of its matched rules are safe to still apply.
    # `--reevaluate` does not override this: it governs the decision
    # cache, a different concern. There is currently no way to
    # deliberately re-trash a restored message.
    candidates_pending_decision = [
        (candidate_id, candidate)
        for candidate_id, candidate in eligible
        if results_by_id[candidate_id].status is None
    ]
    restored_fingerprints = state.fingerprints_with_completed_destructive_action(
        conn,
        account_id=account_id,
        fingerprints=[c.fingerprint for _, c in candidates_pending_decision],
        exclude_run_id=run_id,
    )

    execution_items: list[execute.ExecutionItem] = []
    shadowed_by_candidate: dict[int, tuple[str, ...]] = {}
    # Everything in this loop is local: `policy.decide` is pure and no
    # mailbox connection is open between the scan and execute phases,
    # so the whole batch of no-op/skipped result items commits once
    # instead of fsyncing per row (see `state.transaction`).
    with state.transaction(conn):
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

            if candidate.fingerprint in restored_fingerprints:
                state.insert_result_item(
                    conn,
                    run_id=run_id,
                    candidate_id=candidate_id,
                    status="restored",
                    detail=(
                        "fingerprint already has a completed trash/move_to from "
                        "a previous run; not re-acting on what looks like a "
                        "user restore"
                    ),
                )
                # Retire it as well as skipping it. Without this the decision
                # is re-derived on every future run: the candidate stays live,
                # is re-evaluated, and reports `restored` again forever --
                # exactly the never-retires waste this retirement logic
                # exists to end, just reached by a different path, since
                # skipping here means `execute._reverify` never runs and so
                # can never retire it as `vanished` either. One decision to
                # leave a message alone is final; there is no reason to keep
                # re-making it.
                state.retire_candidate(
                    conn, candidate_id=candidate_id, reason="prior_trash"
                )
                continue

            matched = [
                policy.MatchedRule(
                    rule_index=m.rule_index,
                    label=policy.rule_label(m.rule_index, len(task.rules)),
                )
                for m in result.matches
            ]
            decision = policy.decide(
                protected=False,
                matches=matched,
                rules_by_index=task.rules,
                trash_mailbox=account_cfg.trash_mailbox,
            )
            if decision.status != "matched" or decision.winning_rule is None:
                # Defensive only: `result.matches` non-empty guarantees a
                # winner (`select_winner` never returns None for a
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

    # --- Phase 3: execute --------------------------------------------------
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
            max_actions=task.max_actions,
            dry_run=dry_run,
            fail_fast=fail_fast,
            protected_flags=task.protect.flags,
            protect_unread=task.protect.unread,
            progress=progress,
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
            # (`execute.py`'s signature is fixed), and
            # `result_items` allows only one row per (run_id,
            # candidate_id) -- so the *other* matched-but-not-winning
            # rules for this message cannot get a row of their own. This
            # is the narrowly-scoped, documented raw-SQL update this
            # module's docstring describes: it augments the row
            # `execute.execute_items` already created for the winner
            # with the informational list of rules it shadowed, encoded
            # as JSON text (the column is declared `TEXT`,
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
        model_calls=evaluate_outcome.model_calls,
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
# mailboxes -- exactly what the evaluate phase needs every run
# (deterministic conditions are "re-evaluated free on every
# run", which requires reconsidering candidates beyond just the ones this
# run's scan happened to touch, not only newly-scanned ones).
#
# "Live" here means: the candidate's own `uidvalidity` still matches the
# mailbox's current `uidvalidity` in `mailbox_state` -- i.e. it was not
# superseded by a `UIDVALIDITY` change -- **and** the
# row is not retired (migration 0003: `state.
# retire_candidate` stamps `retired_at` once a `trash`/`move_to:*` action
# actually completes, or `execute._reverify` reports the message gone
# from the server). Excluding retired rows is what makes
# `max_new_mails`'s "no cap" default bound the evaluate set at
# all: without it every successfully-trashed or confirmed-vanished
# message came back forever, re-matching from the decision cache at
# zero model cost but still burning a claim + re-verify round trip every
# run. It does *not* detect a candidate whose
# message was already moved out of the mailbox by an earlier action
# while `UIDVALIDITY` stayed the same and that action's completion was
# never recorded (e.g. a crash the reconcile pass has not yet visited);
# such a stale row is re-evaluated again here, but this is safe, not
# just tolerated: `execute.py`'s re-verify step
# always re-fetches by key before any mutation and reports `vanished`
# rather than acting, so at worst this produces repeat "vanished"/no-op
# report noise for a long-dead candidate, never a wrong mutation.


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
          AND c.retired_at IS NULL
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


# --- Dry-run capability preview -------------------------------------------
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
            "advertise",
        )
    return kept


# --- Task-model summary ----------------------------------------------------
#
# A task no longer names one `model:` -- each of its rules' referenced
# processors carries its own (config.py's `ProcessorConfig.model`).
# `runs.model_name`/`provider`/`model_id` are still single-value, NOT
# NULL columns (unaltered by this migration), so this
# collapses whatever models a task's rules actually reference into one
# display-only summary per column: the single value if there is exactly
# one, else a sorted comma-joined list. This is a deliberate choice
# filling a gap the config schema doesn't address on its own, not a
# spec/contracts requirement.


def _summarize_task_models(task: TaskConfig, config: Config) -> tuple[str, str, str]:
    processor_names: set[str] = set()
    for rule in task.rules:
        processor_names |= rules.processor_names(rule.when)
    model_names = sorted(
        {
            config.processors[name].model
            for name in processor_names
            if name in config.processors
        }
    )
    if not model_names:
        return "(none)", "(none)", "(none)"
    if len(model_names) == 1:
        model_cfg = config.models[model_names[0]]
        return model_names[0], model_cfg.provider, model_cfg.model
    providers = sorted({config.models[name].provider for name in model_names})
    model_ids = sorted({config.models[name].model for name in model_names})
    return ",".join(model_names), ",".join(providers), ",".join(model_ids)


# --- Task routing -----------------------------------------------------------
#
# `task:<id>` is a local-only action: a candidate whose matched rule
# includes it becomes part of the target task's candidate pool from that
# point on, without the target needing to independently rediscover it via
# `source_mailboxes`. `state.routed_fingerprints`/
# `find_live_candidates_by_fingerprint` do the actual lookup; this module
# only merges the result into the task's own mailbox-scan pool, restoring
# oldest-first order across the merge.


def _merge_routed_candidates(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    task_name: str,
    scanned: list[tuple[int, Candidate]],
) -> list[tuple[int, Candidate]]:
    merged: dict[int, Candidate] = dict(scanned)
    for fingerprint in state.routed_fingerprints(
        conn, account_id=account_id, target_task=task_name
    ):
        for candidate_id, candidate in state.find_live_candidates_by_fingerprint(
            conn, account_id=account_id, fingerprint=fingerprint
        ):
            merged.setdefault(candidate_id, candidate)
    return sorted(merged.items(), key=lambda pair: pair[1].internaldate)


# --- Body-excerpt prefetch --------------------------------------------------
#
# A processor's resolved `fields` selection is a static per-processor
# choice, not a dynamic escalation ("there is no dynamically-triggered
# second pass"). `evaluate.py` has no mailbox access at all, so this
# module determines up front -- via the same pure, no-cache first pass
# `evaluate.processors_needed_for_candidate` exposes -- which eligible
# candidates reference an `excerpt`/`html`-selecting processor from a
# still-undecided rule, fetches both a bounded plain-text excerpt and the
# raw HTML body for exactly those, and hands the text to
# `evaluate.evaluate_candidates(..., excerpts=..., html_bodies=...)`. A
# candidate absent from the returned mappings (fetch failed, message
# vanished, `UIDVALIDITY` moved) simply leaves that processor unresolved
# for this round, the same as a processor that was never asked at all.


def _candidates_needing_excerpt(
    task: TaskConfig,
    config: Config,
    eligible: Sequence[tuple[int, Candidate]],
    now: datetime,
) -> list[int]:
    body_processors = {
        name
        for name, cfg in config.processors.items()
        if set(cfg.resolved_fields) & prompt.BODY_FIELDS
    }
    if not body_processors:
        return []
    result: list[int] = []
    for candidate_id, candidate in eligible:
        needed = evaluate.processors_needed_for_candidate(task.rules, candidate, now)
        if needed & body_processors:
            result.append(candidate_id)
    return result


def _fetch_body_texts(
    conn: sqlite3.Connection,
    *,
    mailbox_factory: MailboxFactory,
    account_cfg: AccountConfig,
    account_id: int,
    candidates_by_id: dict[int, Candidate],
    candidate_ids: Sequence[int],
) -> tuple[dict[int, str], dict[int, str]]:
    """Fetch a bounded plain-text excerpt and the raw HTML body for each
    candidate (read-only, `PEEK`) -- one raw-message fetch per candidate
    serves both, since which of `excerpt`/`html` any given processor
    actually asked for is a payload-construction concern, not a fetch
    one. A brief, separate reconnect: the bulk evaluate phase holds no
    mailbox connection, but this is a small, bounded
    (`max_messages_per_run`, default 20) exception, and the connection is
    closed again before any classify() calls happen.
    """
    by_mailbox: dict[str, list[int]] = {}
    for candidate_id in candidate_ids:
        mailbox_name = candidates_by_id[candidate_id].key.mailbox
        by_mailbox.setdefault(mailbox_name, []).append(candidate_id)

    excerpts: dict[int, str] = {}
    html_bodies: dict[int, str] = {}
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
                # Reconnect-and-recheck: UIDVALIDITY moved
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
                excerpt, html_text = extract_body_texts(raw)
                excerpts[candidate_id] = excerpt
                html_bodies[candidate_id] = html_text
    finally:
        _close_mailbox(mailbox)
    return excerpts, html_bodies


# --- Plain-text excerpt / raw HTML extraction -------------------------------
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
    """Best-effort: raw RFC 822 bytes -> cleaned plain text."""
    return extract_body_texts(raw)[0]


def extract_raw_html(raw: bytes) -> str:
    """Best-effort: raw RFC 822 bytes -> the raw (unstripped) `text/html`
    part, or `""` if the message has none. This is the `html` catalog
    field's source -- deliberately *not* run through `_strip_html`/the
    excerpt cleanup heuristics, since a user selecting `html` wants the
    actual markup (tracking pixels, disguised link targets), not
    de-tagged text."""
    return extract_body_texts(raw)[1]


def extract_body_texts(raw: bytes) -> tuple[str, str]:
    """Parse `raw` once and return `(cleaned_plain_excerpt, raw_html)`:
    the same cleaned, capped-source plain-text excerpt
    `extract_plain_text_excerpt` has always produced (prefer `text/plain`,
    else strip `text/html`), plus the raw, unstripped `text/html` part if
    present, else `""`. One parse serves both `excerpt` and `html`
    selection -- which of the two any given processor actually asked for
    is a payload-construction concern, not a fetch/parse one.
    """
    message = message_from_bytes(raw)
    plain, html = _extract_body_parts(message)
    excerpt_source = plain if plain is not None else _strip_html(html or "")
    text = excerpt_source[:_MAX_RAW_TEXT_CHARS]

    reply_match = _REPLY_HEADER_RE.search(text)
    if reply_match:
        text = text[: reply_match.start()]
    signature_match = _SIGNATURE_RE.search(text)
    if signature_match:
        text = text[: signature_match.start()]
    text = _QUOTE_LINE_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    raw_html = (html or "")[:_MAX_RAW_TEXT_CHARS]
    return text.strip(), raw_html


def _extract_body_parts(message: Message) -> tuple[str | None, str | None]:
    """The first `text/plain` and first `text/html` part found, decoded
    and charset-normalised but otherwise unprocessed -- `None` for
    whichever is absent. Both are returned (rather than picking one, as
    the old `_first_text_part` did) since `excerpt` and `html` are now
    independently selectable fields that may both be requested."""
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
        return plain, html
    content_type = message.get_content_type()
    payload = _decode_part(message)
    if content_type == "text/html":
        return None, payload
    return payload, None


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


# --- `config check --connect` -----------------------------------------------


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
    """`--connect` "verifies IMAP authentication, mailbox
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
