"""Pydantic v2 configuration models and load-time validation.

Implements specification §6 (configuration reference), §7.3 (rule
constraints), §12 (config file is a secret — ownership/permission check),
and the derived fetch-header list of §4.1. Value grammars follow
implementation-contracts.md §3.

Two exception families surface from this module:

- `pydantic.ValidationError` for structural/type problems (wrong type,
  missing required key, unknown key, out-of-range scalar). `load_config`
  catches these and re-raises as `ConfigError` so every caller only needs
  to catch one type.
- `ConfigError` (and its `ConfigFilePermissionError` subclass) for
  semantic/business-rule problems: rule constraints, cross-references,
  value-grammar parsing. These are raised directly from field/model
  validators. Pydantic-core only intercepts `ValueError`, `TypeError`,
  `AssertionError`, and `PydanticCustomError` from validator callables;
  since `ConfigError` is none of those, it propagates unchanged out of
  `model_validate`, giving one precise, single-cause message instead of a
  pydantic issue list. This is deliberate, not an oversight.
"""

import hashlib
import os
import re
import stat
from datetime import timedelta
from pathlib import Path
from typing import Literal, Self

import platformdirs
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from liametahi import rules
from liametahi.rules import ConditionTree

# --- Errors ------------------------------------------------------------


class ConfigError(Exception):
    """A config file failed load-time validation. Maps to exit code 2."""


class ConfigFilePermissionError(ConfigError):
    """The config file is not exclusively owned/readable by the invoking
    user (spec §12). Maps to exit code 2."""


# --- Value grammars (contracts §3) --------------------------------------

_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_SIZE_RE = re.compile(r"^(\d+)([kKmMgG]?)[bB]?$")
# spec §7.3: "no spaces, no `(){%*"\]`"
_LABEL_FORBIDDEN_RE = re.compile(r'[\s(){%*"\\\]]')

_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_SIZE_UNIT_MULTIPLIER = {
    "": 1,
    "k": 1024,
    "K": 1024,
    "m": 1024**2,
    "M": 1024**2,
    "g": 1024**3,
    "G": 1024**3,
}


def _parse_duration(value: str) -> timedelta:
    match = _DURATION_RE.match(value)
    if not match:
        raise ConfigError(
            f"invalid duration {value!r}: expected a single integer and unit "
            "s/m/h/d/w, e.g. '30d' or '12h' (no compound forms, no floats)"
        )
    amount = int(match.group(1))
    unit_seconds = _DURATION_UNIT_SECONDS[match.group(2)]
    return timedelta(seconds=amount * unit_seconds)


def _parse_size(value: str) -> int:
    match = _SIZE_RE.match(value)
    if not match:
        raise ConfigError(
            f"invalid size {value!r}: expected an integer with an optional "
            "k/K/m/M/g/G unit (base 1024) and optional trailing 'b', e.g. "
            "'500k', '2M', or '1048576'"
        )
    amount = int(match.group(1))
    multiplier = _SIZE_UNIT_MULTIPLIER[match.group(2)]
    return amount * multiplier


#: Hosts for which `tls_insecure_skip_verify` is permitted (spec §12).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

BASE_FETCH_HEADERS: tuple[str, ...] = (
    "FROM",
    "TO",
    "CC",
    "SUBJECT",
    "DATE",
    "MESSAGE-ID",
    "LIST-ID",
    "LIST-UNSUBSCRIBE",
    "DELIVERED-TO",
    "X-ORIGINAL-TO",
)

#: `appauthor=False` avoids inventing a per-platform "author" namespace
#: (Windows otherwise nests under one); this is a single personal tool, not
#: a vendor's product line. On Linux this resolves to the same
#: `~/.local/share/liametahi` this project has always used; on macOS to
#: `~/Library/Application Support/liametahi`; on Windows to
#: `%LOCALAPPDATA%\liametahi`.
_DEFAULT_STATE_DIR = platformdirs.user_data_path("liametahi", appauthor=False)
DEFAULT_STATE_DB = _DEFAULT_STATE_DIR / "state.sqlite3"
DEFAULT_BACKUP_DIR = _DEFAULT_STATE_DIR / "backups"
DEFAULT_LOCK_DIR = _DEFAULT_STATE_DIR / "locks"


# --- Condition-tree parsing (spec §7.1, §7.2, §7.3) ---------------------

_COMPOSITION_KEYS = frozenset({"all", "any", "not"})
_MAX_NESTING_DEPTH = 3


def _require_str(key: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"condition {key!r} requires a non-empty string value")
    return value


# A value that both starts and ends with `/` (with only letters after the
# final `/`) is a regex literal, e.g. `/.+@gmail\.com/i`; anything else is
# the condition's plain glob/substring form. `.*` is greedy, so a `/`
# inside the pattern itself (escaped or not) does not end the match early.
_MATCH_LITERAL_RE = re.compile(r"^/(.*)/([a-zA-Z]*)$", re.DOTALL)
_REGEX_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
#: "global" has no meaning for a single boolean match (there is nothing to
#: iterate) — matching is already "search anywhere in the string" without
#: it — so `g` is accepted and silently ignored rather than rejected, for
#: familiarity with JS/PCRE regex-literal syntax.
_REGEX_NOOP_FLAGS = frozenset({"g"})


def _parse_match_pattern(key: str, value: object) -> rules.MatchPattern:
    """A `sender-match`/`recipient-match`/`subject-contains`/`list-id-contains`
    value (spec §7.1): either a plain glob/substring (the default, matched
    case-insensitively), or a `/pattern/flags` regex literal, compiled
    once here so a malformed pattern is a config-load error, never a
    runtime one.

    Regex mode is case-*sensitive* by default — the opposite of this
    condition's own glob/substring default — unless the `i` flag is
    given. That is a deliberate asymmetry, not an inconsistency: someone
    reaching for regex is opting into precise control, and typing `i` is
    cheap; silently forcing case-insensitivity on a hand-written pattern
    would be more surprising than not.

    These patterns run against attacker-controlled input (sender/subject
    of inbound mail this tool does not control); Python's `re` has no
    built-in backtracking timeout, so a pattern with nested quantifiers
    (e.g. `(a+)+b`) can hang a run against an adversarial value. Prefer
    simple, anchored patterns.
    """
    text = _require_str(key, value)
    literal_match = _MATCH_LITERAL_RE.match(text)
    if literal_match is None:
        return rules.LiteralPattern(text=text.lower())

    pattern_text, flag_chars = literal_match.groups()
    compiled_flags = 0
    for ch in flag_chars:
        if ch in _REGEX_NOOP_FLAGS:
            continue
        mapped = _REGEX_FLAG_MAP.get(ch)
        if mapped is None:
            raise ConfigError(
                f"condition {key!r} has an invalid regex flag {ch!r} in "
                f"{text!r}; supported flags are i (case-insensitive), "
                "m (multiline), s (dotall), g (accepted, no effect)"
            )
        compiled_flags |= mapped
    try:
        compiled = re.compile(pattern_text, compiled_flags)
    except re.error as exc:
        raise ConfigError(
            f"condition {key!r} has an invalid regex {pattern_text!r} "
            f"(from {text!r}): {exc}"
        ) from exc
    return rules.RegexPattern(regex=compiled)


def _parse_atom(key: str, value: object) -> rules.Atom:
    if key == "llm":
        text = _require_str(key, value)
        return rules.LlmCondition(description=text)
    if key == "older-than":
        return rules.OlderThan(duration=_parse_duration(_require_str(key, value)))
    if key == "newer-than":
        return rules.NewerThan(duration=_parse_duration(_require_str(key, value)))
    if key == "sender-match":
        return rules.SenderMatch(pattern=_parse_match_pattern(key, value))
    if key == "recipient-match":
        return rules.RecipientMatch(pattern=_parse_match_pattern(key, value))
    if key == "subject-contains":
        return rules.SubjectContains(pattern=_parse_match_pattern(key, value))
    if key == "list-id-contains":
        return rules.ListIdContains(pattern=_parse_match_pattern(key, value))
    if key == "has-header":
        return rules.HasHeader(name=_require_str(key, value).lower())
    if key == "has-flag":
        return rules.HasFlag(flag=_require_str(key, value))
    if key == "in-mailbox":
        return rules.InMailbox(mailbox=_require_str(key, value))
    if key == "larger-than":
        return rules.LargerThan(size_bytes=_parse_size(_require_str(key, value)))
    raise ConfigError(
        f"unknown condition {key!r}; expected one of all/any/not or an atom "
        "(older-than, newer-than, sender-match, recipient-match, "
        "subject-contains, list-id-contains, has-header, has-flag, in-mailbox, "
        "larger-than, llm)"
    )


def parse_condition_tree(raw: object, *, depth: int = 0) -> ConditionTree:
    """Parse one raw YAML condition node into a `rules.ConditionTree`.

    Enforces: each node is a single-key mapping, `all`/`any` values are
    non-empty lists, `not` takes a single nested node, and nesting of
    `all`/`any`/`not` does not exceed depth 3 (spec §7.2). Grammar
    parsing for duration/size atoms happens here (contracts §3).

    `llm`-count and `llm`-under-`not` constraints (spec §7.3) are *not*
    checked here — they need whole-tree context and are validated once
    the full tree exists, by `_validate_llm_placement`.
    """
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ConfigError(
            "each condition node must be a mapping with exactly one key "
            f"(all/any/not or a single atom); got {raw!r}"
        )
    ((key, value),) = raw.items()
    if key in _COMPOSITION_KEYS:
        if depth >= _MAX_NESTING_DEPTH:
            raise ConfigError(
                f"condition nesting exceeds the maximum depth of "
                f"{_MAX_NESTING_DEPTH} at {key!r}"
            )
        if key == "not":
            child = parse_condition_tree(value, depth=depth + 1)
            return rules.NotNode(child)
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{key!r} requires a non-empty list of conditions")
        children = tuple(parse_condition_tree(item, depth=depth + 1) for item in value)
        return rules.AllNode(children) if key == "all" else rules.AnyNode(children)
    return _parse_atom(key, value)


def _validate_llm_placement(tree: ConditionTree, *, rule_id: str) -> None:
    """Enforce spec §7.3: at most one `llm` atom per rule, never under `not`."""
    count = 0

    def walk(node: ConditionTree, in_not: bool) -> None:
        nonlocal count
        if isinstance(node, rules.AllNode | rules.AnyNode):
            for child in node.children:
                walk(child, in_not)
        elif isinstance(node, rules.NotNode):
            walk(node.child, True)
        elif isinstance(node, rules.LlmCondition):
            count += 1
            if in_not:
                raise ConfigError(
                    f"rule {rule_id!r}: 'llm' may not appear under 'not' "
                    "(spec §7.3) — express negation in the description text"
                )

    walk(tree, False)
    if count > 1:
        raise ConfigError(
            f"rule {rule_id!r}: at most one 'llm' atom is allowed per rule (spec §7.3)"
        )


def has_deterministic_atom(tree: ConditionTree) -> bool:
    """True if the tree contains at least one non-`llm` atom (spec §5.2)."""
    if isinstance(tree, rules.AllNode | rules.AnyNode):
        return any(has_deterministic_atom(child) for child in tree.children)
    if isinstance(tree, rules.NotNode):
        return has_deterministic_atom(tree.child)
    return not isinstance(tree, rules.LlmCondition)


def collect_header_names(tree: ConditionTree) -> frozenset[str]:
    """Collect every header name referenced by a `has-header` atom."""
    if isinstance(tree, rules.AllNode | rules.AnyNode):
        names: set[str] = set()
        for child in tree.children:
            names |= collect_header_names(child)
        return frozenset(names)
    if isinstance(tree, rules.NotNode):
        return collect_header_names(tree.child)
    if isinstance(tree, rules.HasHeader):
        return frozenset({tree.name})
    return frozenset()


def _validate_actions(
    actions: list[str],
    when: ConditionTree,
    *,
    rule_id: str,
    allow_trash_without_backup: bool,
) -> None:
    """Enforce spec §7.3/§7.4/§7.5 action-list constraints."""
    remote_mutations = 0
    backup_index: int | None = None
    trash_index: int | None = None
    for index, action in enumerate(actions):
        if action == "backup":
            if backup_index is None:
                backup_index = index
            continue
        if action == "trash":
            remote_mutations += 1
            if trash_index is None:
                trash_index = index
            continue
        if action.startswith("move_to:"):
            target = action.removeprefix("move_to:")
            if not target:
                raise ConfigError(
                    f"rule {rule_id!r}: 'move_to:' requires a mailbox name"
                )
            remote_mutations += 1
            continue
        if action.startswith("label:"):
            keyword = action.removeprefix("label:")
            if not keyword:
                raise ConfigError(f"rule {rule_id!r}: 'label:' requires a keyword")
            if _LABEL_FORBIDDEN_RE.search(keyword):
                raise ConfigError(
                    f"rule {rule_id!r}: label keyword {keyword!r} is not a "
                    'valid IMAP atom (no spaces or ( ) { % * " \\ ]) (spec §7.3)'
                )
            remote_mutations += 1
            continue
        raise ConfigError(
            f"rule {rule_id!r}: unknown action {action!r}; expected one of "
            "'backup', 'trash', 'move_to:<mailbox>', 'label:<keyword>' "
            "(spec §7.4)"
        )
    if remote_mutations > 1:
        raise ConfigError(
            f"rule {rule_id!r}: at most one remote mutation "
            "(trash / move_to / label) is allowed per rule's action list "
            "(spec §7.3)"
        )
    if "trash" in actions and not has_deterministic_atom(when):
        raise ConfigError(
            f"rule {rule_id!r}: a rule whose actions include 'trash' must "
            "contain at least one deterministic condition (spec §5.2)"
        )
    if (
        trash_index is not None
        and not allow_trash_without_backup
        and (backup_index is None or backup_index > trash_index)
    ):
        raise ConfigError(
            f"rule {rule_id!r}: 'trash' requires a preceding 'backup' in the "
            "same action list (spec §7.4) — at runtime this rule's 'trash' "
            "action would abort every time with no backup to satisfy it, "
            "silently doing nothing on every match. If the account's own "
            "trash folder is recovery enough and no local copy is wanted, "
            "set allow_trash_without_backup: true on this rule instead of "
            "omitting backup."
        )


# --- Config models --------------------------------------------------------


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_db: Path = Field(default_factory=lambda: DEFAULT_STATE_DB)
    backup_dir: Path = Field(default_factory=lambda: DEFAULT_BACKUP_DIR)
    task_lock_dir: Path = Field(default_factory=lambda: DEFAULT_LOCK_DIR)
    log_file: Path | None = None
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    candidate_retention_days: int = Field(default=90, gt=0)

    @field_validator(
        "state_db", "backup_dir", "task_lock_dir", "log_file", mode="before"
    )
    @classmethod
    def _expand_user(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["imap"] = "imap"
    host: str = Field(min_length=1)
    port: int = Field(default=993, gt=0, le=65535)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    trash_mailbox: str | None = None
    tls_insecure_skip_verify: bool = False

    @model_validator(mode="after")
    def _insecure_tls_is_loopback_only(self) -> AccountConfig:
        """Certificate-verifying TLS is mandatory (spec §12).

        The single exception is a local development server with a
        self-signed certificate (`tools/dev_imap.py`), so this flag is
        refused outright for any non-loopback host: a typo or a
        copy-pasted config can never silently disable verification
        against a real account. Same guard `capture_corpus.py` applies.
        """
        if self.tls_insecure_skip_verify and self.host not in LOOPBACK_HOSTS:
            raise ValueError(
                "tls_insecure_skip_verify is permitted only for a loopback "
                f"host ({', '.join(sorted(LOOPBACK_HOSTS))}); refusing to "
                f"disable certificate verification for host {self.host!r}"
            )
        return self


class ContentEscalationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    format: Literal["plain_text_excerpt"] = "plain_text_excerpt"
    max_chars: int = Field(default=2000, gt=0)
    max_messages_per_run: int = Field(default=20, gt=0)


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai_compatible", "anthropic"]
    base_url: str | None = None
    model: str = Field(min_length=1)
    api_key: str | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    structured_output: Literal["auto", "json_schema", "json_object", "none"] = "auto"
    batch_size: int = Field(default=10, ge=1, le=25)
    timeout_seconds: int = Field(default=45, gt=0)
    max_retries: int = Field(default=2, ge=0)
    content_escalation: ContentEscalationConfig = Field(
        default_factory=ContentEscalationConfig
    )

    @model_validator(mode="after")
    def _validate_provider_requirements(self) -> Self:
        if self.provider == "openai_compatible" and not self.base_url:
            raise ConfigError(
                "models: 'base_url' is required for provider 'openai_compatible'"
            )
        if self.provider == "anthropic" and not self.api_key:
            raise ConfigError("models: 'api_key' is required for provider 'anthropic'")
        return self


class ProtectConfig(BaseModel):
    """No implicit protection (spec §6): a task with no `protect` block, or
    one that omits a field, protects nothing on that axis. Every exclusion
    a user relies on must be visible in their own config, not inherited
    from a library default."""

    model_config = ConfigDict(extra="forbid")

    flags: list[str] = Field(default_factory=list)
    senders: list[str] = Field(default_factory=list)
    unread: bool = False


class RuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: str = Field(min_length=1)
    when: ConditionTree
    actions: list[str] = Field(min_length=1)
    priority: int = 0
    allow_content_escalation: bool = False
    allow_trash_without_backup: bool = False

    @field_validator("when", mode="before")
    @classmethod
    def _parse_when(cls, value: object) -> ConditionTree:
        return parse_condition_tree(value)

    @model_validator(mode="after")
    def _validate_rule(self) -> Self:
        _validate_llm_placement(self.when, rule_id=self.id)
        _validate_actions(
            self.actions,
            self.when,
            rule_id=self.id,
            allow_trash_without_backup=self.allow_trash_without_backup,
        )
        return self


class TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1)
    model: str = Field(min_length=1)
    source_mailboxes: list[str] = Field(default_factory=lambda: ["INBOX"])
    protect: ProtectConfig = Field(default_factory=ProtectConfig)
    max_candidates_per_run: int = Field(default=500, gt=0)
    max_actions_per_run: int | None = Field(default=None, gt=0)
    rules: list[RuleConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_rule_ids(self) -> Self:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ConfigError(
                    f"duplicate rule id {rule.id!r} in task (rule ids must be "
                    "unique within a task)"
                )
            seen.add(rule.id)
        return self

    @property
    def fetch_headers(self) -> tuple[str, ...]:
        """The derived fetch-header list (spec §4.1): the base set plus
        every header named by a `has-header` condition anywhere in this
        task's rules, computed once from the loaded config."""
        extra = {
            name.upper()
            for rule in self.rules
            for name in collect_header_names(rule.when)
        }
        return tuple(sorted(set(BASE_FETCH_HEADERS) | extra))


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    settings: Settings = Field(default_factory=Settings)
    accounts: dict[str, AccountConfig] = Field(min_length=1)
    models: dict[str, ModelConfig] = Field(min_length=1)
    tasks: dict[str, TaskConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _cross_reference(self) -> Self:
        trash_capable_accounts: set[str] = set()
        for task_name, task in self.tasks.items():
            if task.account not in self.accounts:
                raise ConfigError(
                    f"task {task_name!r} references unknown account {task.account!r}"
                )
            if task.model not in self.models:
                raise ConfigError(
                    f"task {task_name!r} references unknown model {task.model!r}"
                )
            for rule in task.rules:
                if "trash" in rule.actions:
                    trash_capable_accounts.add(task.account)
        for account_name in trash_capable_accounts:
            if self.accounts[account_name].trash_mailbox is None:
                raise ConfigError(
                    f"account {account_name!r} is used by a task with a "
                    "'trash' action but has no 'trash_mailbox' configured "
                    "(spec §6)"
                )
        return self


# --- File loading (spec §12) ----------------------------------------------

# Any bit here on a config file (which contains literal credentials, per
# spec §12) means it is readable, writable, or executable by someone other
# than its owner. The spec's minimum bar is "group- or world-readable"; we
# reject the broader set deliberately, as the more conservative check.
_UNSAFE_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO


def check_file_permissions(path: Path) -> None:
    """Enforce spec §12: the config file must be owned by the invoking
    user and inaccessible to anyone else. Raises `ConfigFilePermissionError`
    otherwise (the caller maps this to exit code 2)."""
    st = path.stat()
    invoking_uid = os.getuid()
    if st.st_uid != invoking_uid:
        raise ConfigFilePermissionError(
            f"config file {path} is owned by uid {st.st_uid}, not the "
            f"invoking user (uid {invoking_uid}); refusing to read a "
            "config file containing credentials that another user placed "
            "or could modify (spec §12)"
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & _UNSAFE_MODE_BITS:
        raise ConfigFilePermissionError(
            f"config file {path} has mode {oct(mode)}, which grants group "
            "or other access; it contains literal credentials (spec §12) — "
            f"run 'chmod 600 {path}'"
        )


def compute_config_hash(path: Path) -> str:
    """sha256 of the raw config file bytes, recorded on each run
    (`runs.config_hash`) so a report can show which config version
    produced it. The spec does not define this precisely; hashing the
    file bytes as loaded is the least ambiguous choice."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_config(path: Path) -> Config:
    """Load, permission-check, parse, and validate the config file.

    Raises `ConfigError` (including `ConfigFilePermissionError`) on any
    failure; callers map this to exit code 2 (spec §9).
    """
    resolved = path.expanduser()
    if not resolved.is_file():
        raise ConfigError(f"config file not found: {resolved}")
    check_file_permissions(resolved)
    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read config file {resolved}: {exc}") from exc
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config file must contain a YAML mapping at the top level")
    try:
        return Config.model_validate(raw)
    except ConfigError:
        raise
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration:\n{exc}") from exc
