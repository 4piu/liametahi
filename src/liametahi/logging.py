"""Configured logging with redaction (spec §12).

Account passwords, model API keys, message subjects, and message bodies
must never appear in log output at any level. Two complementary
mechanisms enforce this:

- `register_secret(value)` registers an exact literal (a password or API
  key, read once from the loaded config) to be scrubbed from every
  subsequent formatted log record, wherever it appears as a substring.
- Free-form untrusted message content (subjects, bodies, raw header
  values) has no fixed shape, so it cannot be recognised after the fact.
  Callers in other modules MUST pass such content through the reserved
  `extra` keys below rather than interpolating it into the message
  string themselves; `RedactionFilter` replaces those fields with a
  placeholder before formatting. Baking untrusted text directly into an
  f-string message defeats this — that discipline is on the caller, not
  something this module can retroactively fix.
"""

import contextlib
import logging
from collections.abc import Mapping
from pathlib import Path

REDACTED = "<redacted>"

#: Extra-field names reserved for untrusted, per-message content. A log
#: call that wants to reference a subject, body, or raw header must pass
#: it via `extra={"subject": ...}` etc. so it can be scrubbed; the field
#: is replaced with REDACTED before any handler formats the record.
UNSAFE_EXTRA_KEYS: tuple[str, ...] = (
    "subject",
    "body",
    "excerpt",
    "raw_header",
    "raw_headers",
    "password",
    "api_key",
)

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal secret (account password, model API key) to be
    scrubbed from every subsequent formatted log message."""
    if value:
        _registered_secrets.add(value)


def clear_registered_secrets() -> None:
    """Test hook: reset the registered-secret set between test cases."""
    _registered_secrets.clear()


def _scrub(text: str) -> str:
    for secret in _registered_secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _scrub_value(value: object) -> object:
    return _scrub(value) if isinstance(value, str) else value


class RedactionFilter(logging.Filter):
    """Scrubs registered secrets and reserved untrusted fields from every
    record before it is formatted by any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in UNSAFE_EXTRA_KEYS:
            if hasattr(record, key):
                setattr(record, key, REDACTED)
        record.msg = _scrub(str(record.msg))
        if record.args:
            if isinstance(record.args, Mapping):
                record.args = {k: _scrub_value(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_scrub_value(a) for a in record.args)
        return True


def configure_logging(
    *, level: str = "info", log_file: Path | None = None, logger_name: str = "liametahi"
) -> logging.Logger:
    """Configure the named logger with a redacting stderr handler and, if
    `log_file` is given, a redacting file handler. Sets file mode 0600 on
    a created log file (spec §12)."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(_LEVELS.get(level, logging.INFO))
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redaction_filter = RedactionFilter()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redaction_filter)
    logger.addHandler(stream_handler)

    if log_file is not None:
        log_file = log_file.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction_filter)
        logger.addHandler(file_handler)
        with contextlib.suppress(OSError):
            log_file.chmod(0o600)

    return logger


def get_logger(logger_name: str = "liametahi") -> logging.Logger:
    return logging.getLogger(logger_name)


def enable_verbose(logger_name: str = "liametahi") -> None:
    """Escalate the already-configured logger to `debug` for this
    invocation only (`run --verbose`), without touching its handlers.
    Low-level per-call detail (retry attempts, per-item action results,
    ...) is logged at `debug` precisely so it stays silent unless this
    is called; high-level phase/batch progress is logged at `info` and
    is visible regardless."""
    logging.getLogger(logger_name).setLevel(logging.DEBUG)
