"""Tests for `liametahi.logging` (spec §12: redaction)."""

import logging
from collections.abc import Generator
from pathlib import Path

import pytest

from liametahi.logging import (
    clear_registered_secrets,
    configure_logging,
    get_logger,
    register_secret,
)


@pytest.fixture(autouse=True)
def _reset_secrets() -> Generator[None]:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_registered_password_is_redacted_from_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = configure_logging(level="debug", logger_name="test.redact.password")
    register_secret("abcd efgh ijkl mnop")
    logger.info("connecting with password %s", "abcd efgh ijkl mnop")
    captured = capsys.readouterr()
    assert "abcd efgh ijkl mnop" not in captured.err
    assert "<redacted>" in captured.err


def test_registered_api_key_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    logger = configure_logging(level="debug", logger_name="test.redact.apikey")
    register_secret("sk-ant-super-secret-key")
    logger.error("request failed: sk-ant-super-secret-key was rejected")
    captured = capsys.readouterr()
    assert "sk-ant-super-secret-key" not in captured.err
    assert "<redacted>" in captured.err


def test_unregistered_secret_embedded_directly_is_not_caught(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Documents the module's stated limitation: text baked directly into
    a message string that was never registered cannot be recognised."""
    logger = configure_logging(level="debug", logger_name="test.redact.unregistered")
    logger.info("never registered: hunter2")
    captured = capsys.readouterr()
    assert "hunter2" in captured.err  # demonstrates the discipline requirement


def test_unsafe_extra_field_subject_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = configure_logging(level="debug", logger_name="test.redact.subject")
    handler = logger.handlers[0]
    handler.setFormatter(logging.Formatter("%(message)s subject=%(subject)s"))
    logger.info(
        "processing candidate", extra={"subject": "URGENT: click this hostile link"}
    )
    captured = capsys.readouterr()
    assert "URGENT: click this hostile link" not in captured.err
    assert "<redacted>" in captured.err


def test_default_level_never_leaks_registered_credential_or_subject(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = configure_logging(level="info", logger_name="test.redact.default")
    handler = logger.handlers[0]
    handler.setFormatter(logging.Formatter("%(message)s subject=%(subject)s"))
    register_secret("s3cr3t-password")

    logger.info(
        "evaluating candidate with account password %s",
        "s3cr3t-password",
        extra={"subject": "Your invoice is overdue"},
    )
    logger.debug("this should not even appear at info level: %s", "s3cr3t-password")

    captured = capsys.readouterr()
    assert "s3cr3t-password" not in captured.err
    assert "Your invoice is overdue" not in captured.err
    assert "this should not even appear" not in captured.err  # below configured level


def test_configure_logging_writes_log_file_with_mode_0600(tmp_path: Path) -> None:
    import stat

    log_file = tmp_path / "liametahi.log"
    configure_logging(level="info", log_file=log_file, logger_name="test.redact.file")
    logger = get_logger("test.redact.file")
    logger.info("hello")
    for handler in logger.handlers:
        handler.flush()
    assert log_file.exists()
    mode = stat.S_IMODE(log_file.stat().st_mode)
    assert mode == 0o600


def test_log_file_also_redacts(tmp_path: Path) -> None:
    log_file = tmp_path / "liametahi.log"
    logger = configure_logging(
        level="info", log_file=log_file, logger_name="test.redact.file2"
    )
    register_secret("file-secret-value")
    logger.info("token: %s", "file-secret-value")
    for handler in logger.handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    assert "file-secret-value" not in content
    assert "<redacted>" in content
