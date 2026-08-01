"""Typer CLI (spec §9).

All four commands are functional. This module owns argument parsing,
process exit codes, and printing; every decision of substance belongs to
`runner.py` (phase orchestration), `report.py` (rendering), or
`backup.py` (restore). Nothing here reimplements policy.

Config path resolution (spec §9): `--config PATH`, else `$LIAMETAHI_CONFIG`,
else `~/.config/liametahi/config.yaml`. A missing file is exit 2 naming
the path that was tried.

Exit codes (spec §9):
    0  success
    1  runtime or partial failure
    2  bad invocation or configuration
    3  interrupted (SIGINT/SIGTERM)
    4  authentication failure
    5  task already running
"""

import os
import signal
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Annotated

import platformdirs
import typer

from liametahi import backup as backup_mod
from liametahi import report as report_mod
from liametahi import runner, state
from liametahi.config import Config, ConfigError, load_config
from liametahi.logging import configure_logging, enable_verbose, register_secret

EXIT_SUCCESS = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_BAD_CONFIG = 2
EXIT_INTERRUPTED = 3
EXIT_AUTH_FAILURE = 4
EXIT_TASK_RUNNING = 5

#: Same per-platform resolution as `config.py`'s data directory (spec §9):
#: `~/.config/liametahi` on Linux, `~/Library/Application Support/liametahi`
#: on macOS, `%LOCALAPPDATA%\liametahi` on Windows.
DEFAULT_CONFIG_PATH = (
    platformdirs.user_config_path("liametahi", appauthor=False) / "config.yaml"
)


def _raise_keyboard_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


@contextmanager
def _handle_termination() -> Iterator[None]:
    """Make SIGTERM raise the same `KeyboardInterrupt` Python already
    raises for SIGINT (Ctrl+C), so `runner.py` has exactly one signal to
    catch and finish the run gracefully with (spec §10: exit code 3).
    Scoped to `run` alone and restored on exit, since `signal.signal` is
    process-global and this same process handles every CLI invocation in
    a test run.
    """
    previous = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _resolve_config_path(supplied: Path | None) -> Path:
    """`--config PATH`, else `$LIAMETAHI_CONFIG`, else the default
    (spec §9).

    `None` — not a sentinel path — means "the flag was not given". A
    sentinel `Path` does not survive Click's own path conversion, and
    silently falling through to the default would make the CLI read the
    user's real config when a caller believed it had supplied one.
    """
    if supplied is not None:
        return supplied.expanduser()
    from_env = os.environ.get("LIAMETAHI_CONFIG")
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_CONFIG_PATH


def _load(config: Path | None) -> tuple[Config, Path]:
    """Resolve, load, and validate the config, then register its secrets
    with the log redactor before anything can log. Exits 2 on any config
    problem, including a missing file (spec §9)."""
    path = _resolve_config_path(config)
    if not path.is_file():
        typer.echo(f"config error: no config file at {path}", err=True)
        raise typer.Exit(code=EXIT_BAD_CONFIG)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=EXIT_BAD_CONFIG) from exc
    try:
        configure_logging(level=cfg.settings.log_level, log_file=cfg.settings.log_file)
    except OSError as exc:
        # `configure_logging` creates `log_file`'s parent directory; an
        # unwritable location (permission denied, an ancestor that's a
        # file, ...) is a config problem, not a mid-run failure, so it
        # gets the same treatment as any other bad config value rather
        # than a raw traceback.
        typer.echo(f"config error: settings.log_file: {exc}", err=True)
        raise typer.Exit(code=EXIT_BAD_CONFIG) from exc
    for account in cfg.accounts.values():
        register_secret(account.password)
    for model in cfg.models.values():
        register_secret(model.api_key)
    return cfg, path


def _open_state_db(cfg: Config) -> sqlite3.Connection:
    """`state.open_database` creates `settings.state_db`'s parent
    directory; an unwritable location is a config problem, not a
    mid-command failure, so it gets the same treatment as any other bad
    config value rather than a raw traceback."""
    try:
        return state.open_database(cfg.settings.state_db)
    except OSError as exc:
        typer.echo(f"config error: settings.state_db: {exc}", err=True)
        raise typer.Exit(code=EXIT_BAD_CONFIG) from exc


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Liametahi — a local, cron-friendly IMAP mailbox cleanup CLI.",
)
config_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(config_app, name="config")


ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help="Path to the config file (else $LIAMETAHI_CONFIG, else the default)",
    ),
]


@config_app.command("check")
def config_check(
    config: ConfigOption = None,
    connect: Annotated[
        bool,
        typer.Option(
            "--connect",
            help="Also verify IMAP authentication and capabilities",
        ),
    ] = False,
) -> None:
    """Validate the config file, its ownership/permissions, and its
    cross-references, without contacting anything (spec §9)."""
    cfg, path = _load(config)

    typer.echo(f"config OK: {path}")
    typer.echo(f"  accounts: {', '.join(sorted(cfg.accounts))}")
    typer.echo(f"  models:   {', '.join(sorted(cfg.models))}")
    typer.echo(f"  tasks:    {', '.join(sorted(cfg.tasks))}")
    for task_name in sorted(cfg.tasks):
        task = cfg.tasks[task_name]
        rule_ids = ", ".join(rule.id for rule in task.rules)
        typer.echo(
            f"  task {task_name!r}: account={task.account!r} model={task.model!r} "
            f"rules=[{rule_ids}] fetch_headers={list(task.fetch_headers)}"
        )

    if not connect:
        return

    result = runner.connect_check(cfg)
    for account in result.checked_accounts:
        typer.echo(f"  connected: {account}")
    for issue in result.issues:
        typer.echo(f"  {issue.severity}: {issue.account}: {issue.message}", err=True)
    if result.auth_failed:
        raise typer.Exit(code=EXIT_AUTH_FAILURE)
    if not result.ok:
        raise typer.Exit(code=EXIT_RUNTIME_FAILURE)


@app.command("run")
def run(
    task: Annotated[str, typer.Argument(help="Task name from the config file")],
    config: ConfigOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    fail_fast: Annotated[bool, typer.Option("--fail-fast")] = False,
    reevaluate: Annotated[bool, typer.Option("--reevaluate")] = False,
    wait: Annotated[
        float,
        typer.Option(
            "--wait",
            help="Seconds to wait for the task lock; default 0 (do not wait)",
        ),
    ] = 0.0,
    format_: Annotated[str, typer.Option("--format", help="table|json")] = "table",
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help=(
                "Detailed report output, and low-level per-call progress "
                "logging (retries, per-batch/per-item detail) during the run"
            ),
        ),
    ] = False,
) -> None:
    """Evaluate and, unless `--dry-run`, act on a task's mailbox (spec §4).

    High-level phase/batch progress is logged at `info` regardless of
    `--verbose` (scan/evaluate/execute boundaries); `--verbose` escalates
    to `debug` for low-level per-call detail on top of that, and also
    controls how detailed the final report is.

    SIGINT (Ctrl+C) and SIGTERM both interrupt gracefully: the task lock
    is released either way, but a signal caught mid-run also finishes the
    run with a proper stored report and exit code 3, rather than aborting
    silently and leaving the run row looking perpetually in-progress.

    A report is always produced and stored, including for a dry run and
    for a failed run.
    """
    if format_ not in ("table", "json"):
        typer.echo(f"--format must be 'table' or 'json', got {format_!r}", err=True)
        raise typer.Exit(code=EXIT_BAD_CONFIG)

    cfg, path = _load(config)
    if verbose:
        enable_verbose()

    with _handle_termination():
        outcome = runner.run_task(
            config=cfg,
            config_path=path,
            task_name=task,
            dry_run=dry_run,
            fail_fast=fail_fast,
            reevaluate=reevaluate,
            wait_seconds=wait,
            on_run_created=lambda run_id: typer.echo(f"run {run_id} started"),
        )

    # spec §10: a lock-contended run writes one line to stderr and no
    # report at all, so there is nothing to render.
    if outcome.diagnostic is not None:
        typer.echo(outcome.diagnostic, err=True)
    if outcome.report_data is not None:
        typer.echo(_render(outcome.report_data, format_=format_, verbose=verbose))
    if outcome.run_id is not None:
        typer.echo(f"run {outcome.run_id} finished")
    raise typer.Exit(code=outcome.exit_code)


def _render(data: report_mod.ReportData, *, format_: str, verbose: bool) -> str:
    if format_ == "json":
        return report_mod.render_json(data, verbose=verbose)
    return report_mod.render_table(data, verbose=verbose)


@app.command("report")
def report(
    run_id: Annotated[
        str | None, typer.Argument(help="Run ID to display; omit for the newest")
    ] = None,
    list_: Annotated[
        bool, typer.Option("--list", help="List stored runs, newest first")
    ] = False,
    task: Annotated[str | None, typer.Option("--task")] = None,
    config: ConfigOption = None,
    format_: Annotated[str, typer.Option("--format", help="table|json")] = "table",
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Retrieve a stored run report. Never contacts the mailbox or the
    model (spec §9)."""
    if format_ not in ("table", "json"):
        typer.echo(f"--format must be 'table' or 'json', got {format_!r}", err=True)
        raise typer.Exit(code=EXIT_BAD_CONFIG)

    cfg, _ = _load(config)
    conn = _open_state_db(cfg)
    try:
        if list_:
            typer.echo(report_mod.render_run_list(state.list_runs(conn, task=task)))
            return

        target = run_id
        if target is None:
            runs = state.list_runs(conn, task=task)
            if not runs:
                typer.echo("no stored runs", err=True)
                raise typer.Exit(code=EXIT_RUNTIME_FAILURE)
            target = runs[0].run_id

        try:
            data = report_mod.load_report(conn, target)
        except report_mod.ReportNotFoundError as exc:
            typer.echo(f"report error: {exc}", err=True)
            raise typer.Exit(code=EXIT_RUNTIME_FAILURE) from exc
        typer.echo(_render(data, format_=format_, verbose=verbose))
    finally:
        state.close_database(conn)


@app.command("restore")
def restore(
    backup_id: Annotated[str, typer.Argument(help="Backup ID to restore")],
    mailbox: Annotated[str, typer.Option("--mailbox", help="Destination mailbox")],
    account: Annotated[
        str | None,
        typer.Option("--account", help="Account name; required if more than one"),
    ] = None,
    config: ConfigOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Verify a backup's checksum and APPEND it to a mailbox (spec §4.4).

    Restore is best-effort: it cannot recreate provider-specific labels
    or thread identity, and it does not detect that the message is
    already present, so restoring one that was never trashed produces a
    duplicate.
    """
    cfg, _ = _load(config)

    account_name = account
    if account_name is None:
        if len(cfg.accounts) != 1:
            typer.echo(
                "--account is required when the config defines more than one "
                f"account: {sorted(cfg.accounts)}",
                err=True,
            )
            raise typer.Exit(code=EXIT_BAD_CONFIG)
        account_name = next(iter(cfg.accounts))
    if account_name not in cfg.accounts:
        typer.echo(f"unknown account {account_name!r}", err=True)
        raise typer.Exit(code=EXIT_BAD_CONFIG)

    conn = _open_state_db(cfg)
    mailbox_conn = None
    try:
        # A dry run verifies the checksum and reports what it would
        # append; it must not open a connection to do that (spec §4.4).
        adapter = None
        if not dry_run:
            mailbox_conn = runner.default_mailbox_factory(cfg.accounts[account_name])
            adapter = mailbox_conn
        result = backup_mod.restore_backup(
            conn,
            mailbox=adapter,  # type: ignore[arg-type]
            backup_dir=cfg.settings.backup_dir,
            backup_id=backup_id,
            destination_mailbox=mailbox,
            dry_run=dry_run,
        )
    except backup_mod.RestoreError as exc:
        typer.echo(f"restore error: {exc}", err=True)
        raise typer.Exit(code=EXIT_RUNTIME_FAILURE) from exc
    finally:
        if mailbox_conn is not None:
            close = getattr(mailbox_conn, "close", None)
            if callable(close):
                close()
        state.close_database(conn)

    verb = "would restore" if result.dry_run else "restored"
    typer.echo(
        f"{verb} {result.backup_id} -> {result.mailbox} "
        f"({result.byte_count} bytes, sha256 {result.sha256[:12]}…)"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
