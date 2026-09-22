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
import sys
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Literal, Self, cast

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
from liametahi.rules import ConditionTree, ProcessorCondition

# --- Errors ------------------------------------------------------------


class ConfigError(Exception):
    """A config file failed load-time validation. Maps to exit code 2."""


class ConfigFilePermissionError(ConfigError):
    """The config file is not exclusively owned/readable by the invoking
    user (spec §12). Maps to exit code 2."""


# --- Value grammars (contracts §3) --------------------------------------

_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_SIZE_RE = re.compile(r"^(\d+)([kKmMgG]?)[bB]?$")
# Two-character operators must precede their one-character prefixes in the
# alternation (contracts §3): regex alternation picks the first alternative
# that matches at a position, not the longest, so ">" before ">=" would
# swallow the ">" and leave a stray "=" that fails the trailing \d+ anchor.
# Widened (jev-provider-plan §10) to also accept a decimal amount, so
# `>=0.85` parses the same way `>=2` already does -- `recipient-count`'s
# existing integer forms are unaffected, since `\d+` on its own still
# matches with no decimal point captured.
_COMPARISON_RE = re.compile(r"^(==|!=|>=|<=|>|<)(\d+(?:\.\d+)?)$")
# jev-provider-plan §3: `processor: "name.field op value"`, one fixed
# shape, no optional parts. `field` is checked against the closed
# {"value", "confidence"} set after matching, not in the regex itself, so
# an invalid field name gets a clear message instead of a generic
# "condition doesn't match" one.
_PROCESSOR_CONDITION_RE = re.compile(
    r"^([A-Za-z_][\w-]*)\.([A-Za-z_][\w-]*)\s*(==|!=|>=|<=|>|<)\s*(.+)$"
)
# contracts §3: mechanism is a closed set, result word is deliberately open
# (pass/fail/softfail/neutral/none/temperror/permerror and provider-specific
# extensions all appear in real Authentication-Results headers).
_AUTH_RESULT_RE = re.compile(r"^(spf|dkim|dmarc)\s*=\s*([A-Za-z_]+)$", re.IGNORECASE)
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


def _parse_comparison(key: str, value: str) -> tuple[rules.ComparisonOp, int | float]:
    match = _COMPARISON_RE.match(value)
    if not match:
        raise ConfigError(
            f"condition {key!r} has an invalid comparison {value!r}: expected "
            "one operator from ==, !=, >=, <=, >, < immediately followed by a "
            "non-negative integer or decimal, e.g. '>10', '<=3', or '>=0.85'"
        )
    # _COMPARISON_RE's first group is one of exactly these six alternatives.
    op = cast(rules.ComparisonOp, match.group(1))
    number_text = match.group(2)
    if "." in number_text:
        return op, float(number_text)
    return op, int(number_text)


def _parse_processor_condition(key: str, value: object) -> ProcessorCondition:
    """`processor: "name.field op value"` (jev-provider-plan §3): one
    fixed shape, no optional parts. `field` is exactly `value` or
    `confidence`; the comparand is `true`/`false` (lowercase, exact) ->
    bool, else a float if it parses as one, else a plain string (a
    declared choice/level option name). A bool or string comparand only
    ever supports `==`/`!=` -- `>`/`>=`/`<`/`<=` against a non-numeric
    comparand is rejected here, at config load, not lazily at evaluation
    time. Whether the referenced processor exists, and whether a string
    comparand actually names one of its declared options/levels, needs
    the whole config and is validated by `Config._cross_reference`
    instead.
    """
    text = _require_str(key, value)
    match = _PROCESSOR_CONDITION_RE.match(text)
    if not match:
        raise ConfigError(
            f"condition {key!r} has an invalid value {text!r}: expected "
            "'name.field op value', e.g. 'spam-category.value == spam' or "
            "'urgency.confidence >= 0.85'"
        )
    name, field, op_text, comparand_text = match.groups()
    if field not in ("value", "confidence"):
        raise ConfigError(
            f"condition {key!r}: field {field!r} in {text!r} must be "
            "'value' or 'confidence' -- no other field exists"
        )
    op = cast(rules.ComparisonOp, op_text)
    comparand_text = comparand_text.strip()
    comparand: bool | float | str
    if comparand_text == "true":
        comparand = True
    elif comparand_text == "false":
        comparand = False
    else:
        try:
            comparand = float(comparand_text)
        except ValueError:
            comparand = comparand_text
    if not isinstance(comparand, float) and op not in ("==", "!="):
        raise ConfigError(
            f"condition {key!r}: operator {op_text!r} in {text!r} is only "
            "valid against a numeric comparand; a boolean or string "
            "comparand only supports == or !="
        )
    return ProcessorCondition(
        name=name,
        field=cast(Literal["value", "confidence"], field),
        op=op,
        value=comparand,
    )


def _parse_auth_result(key: str, value: object) -> rules.AuthResult:
    """`auth-result: mechanism=result` (spec §7.1; contracts §3). The
    mechanism is one of `spf`/`dkim`/`dmarc` (case-insensitive); the
    result word is open vocabulary. Compiled once here into a single
    precomputed `re.Pattern`, never re-built per candidate at eval time."""
    text = _require_str(key, value)
    match = _AUTH_RESULT_RE.match(text)
    if not match:
        raise ConfigError(
            f"condition {key!r} has an invalid value {text!r}: expected "
            "'mechanism=result' where mechanism is one of spf, dkim, dmarc, "
            "e.g. 'spf=fail'"
        )
    mechanism, result = match.group(1), match.group(2)
    pattern = re.compile(
        rf"\b{re.escape(mechanism)}\s*=\s*{re.escape(result)}\b", re.IGNORECASE
    )
    return rules.AuthResult(regex=pattern)


def _parse_has_attachment(key: str, value: object) -> rules.HasAttachment:
    """`has-attachment: true` (spec §7.1): only the literal YAML boolean
    `True` is accepted -- anything else, including `False` or the string
    `"true"`, is a config error. Negate with `not: {has-attachment: true}`
    rather than inventing a second spelling for the same fact."""
    if value is not True:
        raise ConfigError(
            f"condition {key!r} requires the literal YAML boolean 'true' "
            f"(got {value!r}); negate with 'not: {{has-attachment: true}}' "
            "instead of a second spelling for the same fact"
        )
    return rules.HasAttachment()


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

_COMPOSITION_KEYS = frozenset({"all", "any", "none", "not"})
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
    if key == "processor":
        return _parse_processor_condition(key, value)
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
    if key == "recipient-count":
        op, count = _parse_comparison(key, _require_str(key, value))
        if not isinstance(count, int):
            raise ConfigError(
                f"condition {key!r} requires an integer comparand, got "
                f"{value!r} (recipient-count is a count, not a decimal)"
            )
        return rules.RecipientCount(op=op, value=count)
    if key == "has-attachment":
        return _parse_has_attachment(key, value)
    if key == "auth-result":
        return _parse_auth_result(key, value)
    raise ConfigError(
        f"unknown condition {key!r}; expected one of all/any/none/not or an "
        "atom (older-than, newer-than, sender-match, recipient-match, "
        "subject-contains, list-id-contains, has-header, has-flag, in-mailbox, "
        "larger-than, recipient-count, has-attachment, auth-result, processor)"
    )


def parse_condition_tree(raw: object, *, depth: int = 0) -> ConditionTree:
    """Parse one raw YAML condition node into a `rules.ConditionTree`.

    Enforces: each node is a single-key mapping, `all`/`any`/`none`
    values are non-empty lists, `not` takes a single nested node, and
    nesting of `all`/`any`/`none`/`not` does not exceed depth 3 (spec
    §7.2; jev-provider-plan §4). Grammar parsing for duration/size/
    processor atoms happens here (contracts §3; jev-provider-plan §3).
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
        if key == "all":
            return rules.AllNode(children)
        if key == "any":
            return rules.AnyNode(children)
        return rules.NoneNode(children)
    return _parse_atom(key, value)


def parse_when(raw: object) -> ConditionTree:
    """Parse a rule's top-level `when:` value (spec §7.2).

    Two shapes are accepted:

    - **A YAML list** of condition nodes, implicitly ANDed together —
      `when: [{older-than: 30d}, {sender-match: foo@bar.com}]`. This
      replaces the old top-level `all:` wrapper for the common
      match-all-of-these case; a list item is parsed at `depth=1`, the
      same nesting cost the `all:` wrapper it replaces used to charge.
    - **A single condition node** — one atom, or an `any`/`not` — exactly
      as `parse_condition_tree` already handles, unchanged. A rule that
      only ever needed one condition (`when: {older-than: 30d}`) or is
      fundamentally an OR/negation at the top (`when: {any: [...]}`)
      loses nothing and gains no new wrapping to write.

    `all:` is no longer valid as this value's own top-level key — it was
    purely redundant with the list form (`when: {all: [A, B]}` and
    `when: [A, B]` meant the same thing) and having two spellings for the
    same case was the specific awkwardness this replaces. `all:` remains
    a valid *nested* keyword, e.g. inside an `any:`'s list, to group
    several conditions as one alternative.
    """
    if isinstance(raw, list):
        if not raw:
            raise ConfigError(
                "'when' as a list must be non-empty — a rule needs at "
                "least one condition"
            )
        children = tuple(parse_condition_tree(item, depth=1) for item in raw)
        return rules.AllNode(children)
    if isinstance(raw, dict) and len(raw) == 1 and next(iter(raw)) == "all":
        raise ConfigError(
            "'when: {all: [...]}' is no longer accepted — write the list "
            "directly instead: 'when: [...]' (the same conditions, "
            "without the redundant wrapper)"
        )
    return parse_condition_tree(raw, depth=0)


def has_deterministic_atom(tree: ConditionTree) -> bool:
    """True if the tree contains at least one non-`processor` atom (spec
    §5.2). A `processor:` atom never counts as deterministic regardless
    of processor type, backend, or which field is being compared
    (jev-provider-plan §3's safety invariant) -- a `trash` rule still
    needs at least one atom besides it."""
    if isinstance(tree, rules.AllNode | rules.AnyNode | rules.NoneNode):
        return any(has_deterministic_atom(child) for child in tree.children)
    if isinstance(tree, rules.NotNode):
        return has_deterministic_atom(tree.child)
    return not isinstance(tree, ProcessorCondition)


def collect_header_names(tree: ConditionTree) -> frozenset[str]:
    """Collect every header name referenced by a `has-header` atom, plus
    `authentication-results` whenever an `auth-result` atom appears --
    this is what makes `TaskConfig.fetch_headers` actually fetch
    `Authentication-Results` for a task that uses `auth-result` anywhere
    in its rules, without fetching it unconditionally for every task."""
    if isinstance(tree, rules.AllNode | rules.AnyNode | rules.NoneNode):
        names: set[str] = set()
        for child in tree.children:
            names |= collect_header_names(child)
        return frozenset(names)
    if isinstance(tree, rules.NotNode):
        return collect_header_names(tree.child)
    if isinstance(tree, rules.HasHeader):
        return frozenset({tree.name})
    if isinstance(tree, rules.AuthResult):
        return frozenset({"authentication-results"})
    return frozenset()


def _collect_processor_atoms(tree: ConditionTree) -> tuple[ProcessorCondition, ...]:
    """Every `ProcessorCondition` atom anywhere in `tree`, for
    `Config._cross_reference`'s per-atom checks (referenced processor
    exists, `.value` comparand names a declared option/level). Mirrors
    `collect_header_names`'s traversal shape; `rules.processor_names()`
    is the public, name-only equivalent `evaluate.py` uses at runtime."""
    if isinstance(tree, rules.AllNode | rules.AnyNode | rules.NoneNode):
        atoms: list[ProcessorCondition] = []
        for child in tree.children:
            atoms.extend(_collect_processor_atoms(child))
        return tuple(atoms)
    if isinstance(tree, rules.NotNode):
        return _collect_processor_atoms(tree.child)
    if isinstance(tree, ProcessorCondition):
        return (tree,)
    return ()


def _validate_actions(
    actions: list[str],
    when: ConditionTree,
    *,
    rule_label: str,
) -> None:
    """Enforce spec §7.3/§7.4/§7.5 action-list constraints.

    Backup-before-trash (formerly enforced here) is removed
    (jev-provider-plan §8): `trash` no longer requires a preceding
    `backup` in the same action list, and `allow_trash_without_backup`
    no longer exists as a config field at all -- `RuleConfig`'s
    `extra="forbid"` now rejects it outright as an unknown key. What is
    unchanged: at most one remote mutation per action list, and `trash`
    still requires at least one deterministic condition (spec §5.2) --
    the more important of the two, since it stops a model's (or
    processor's) verdict alone from being sufficient to delete
    something. `task:<id>` is a new, local-only action (jev-provider-plan
    §7): never an IMAP mutation, so it never counts toward
    `remote_mutations` and composes freely with everything else.
    """
    remote_mutations = 0
    for action in actions:
        if action == "backup":
            continue
        if action == "trash":
            remote_mutations += 1
            continue
        if action.startswith("move_to:"):
            target = action.removeprefix("move_to:")
            if not target:
                raise ConfigError(f"{rule_label}: 'move_to:' requires a mailbox name")
            remote_mutations += 1
            continue
        if action.startswith("label:"):
            keyword = action.removeprefix("label:")
            if not keyword:
                raise ConfigError(f"{rule_label}: 'label:' requires a keyword")
            if _LABEL_FORBIDDEN_RE.search(keyword):
                raise ConfigError(
                    f"{rule_label}: label keyword {keyword!r} is not a "
                    'valid IMAP atom (no spaces or ( ) { % * " \\ ]) (spec §7.3)'
                )
            remote_mutations += 1
            continue
        if action.startswith("task:"):
            target = action.removeprefix("task:")
            if not target:
                raise ConfigError(f"{rule_label}: 'task:' requires a task id")
            continue
        raise ConfigError(
            f"{rule_label}: unknown action {action!r}; expected one of "
            "'backup', 'trash', 'move_to:<mailbox>', 'label:<keyword>', "
            "'task:<id>' (spec §7.4; jev-provider-plan §7)"
        )
    if remote_mutations > 1:
        raise ConfigError(
            f"{rule_label}: at most one remote mutation "
            "(trash / move_to / label) is allowed per rule's action list "
            "(spec §7.3)"
        )
    if "trash" in actions and not has_deterministic_atom(when):
        raise ConfigError(
            f"{rule_label}: a rule whose actions include 'trash' must "
            "contain at least one deterministic condition (spec §5.2)"
        )


# --- Config models --------------------------------------------------------


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_db: Path = Field(default_factory=lambda: DEFAULT_STATE_DB)
    backup_dir: Path = Field(default_factory=lambda: DEFAULT_BACKUP_DIR)
    task_lock_dir: Path = Field(default_factory=lambda: DEFAULT_LOCK_DIR)
    log_file: Path | None = None
    log_level: Literal["debug", "info", "warning", "error"] = "info"

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


class BodyExcerptConfig(BaseModel):
    """No separate on/off switch here (spec §5.1; jev-provider-plan §2):
    a processor's own `include_body` is already the opt-in, and a second
    gate at the model level would only mean two places to enable the same
    thing before it does anything, with no clear story for what one
    enabled and the other disabled means. This holds the shape/budget
    settings (`format`, `max_chars`) that apply once a processor has
    already opted in."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["plain_text_excerpt"] = "plain_text_excerpt"
    # Opt-in: unset means no truncation. An implicit default here would
    # silently shorten an excerpt with nothing in the config to point at.
    max_chars: int | None = Field(default=None, gt=0)


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai_compatible", "anthropic", "jev"]
    base_url: str | None = None
    model: str = Field(min_length=1)
    api_key: str | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    structured_output: Literal["auto", "json_schema", "json_object", "none"] = "auto"
    # Not a limit but a batching factor: a request needs *some* size, so
    # unlike the `max_*` keys this keeps a default. No upper bound is
    # enforced -- large batches measurably degrade small local models, but
    # that is guidance for the README, not something to reject outright.
    # jev-provider-plan §11: a `jev` model must set this to exactly 1 --
    # jev is "one HTTP call per candidate", not a batched chat request.
    mails_per_request: int = Field(default=10, ge=1)
    # How many of those requests may be in flight at once. Defaults to 1
    # -- fully serial, the behaviour every existing config already has --
    # because the right value depends on a provider's rate limits, which
    # this program cannot discover. Raising it is the single largest win
    # available on a first run over a large mailbox, where classification
    # is thousands of independent requests whose latency is nearly all
    # waiting.
    max_concurrent_requests: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(default=45, gt=0)
    max_retries: int = Field(default=2, ge=0)
    body_excerpt: BodyExcerptConfig = Field(default_factory=BodyExcerptConfig)

    @model_validator(mode="after")
    def _validate_provider_requirements(self) -> Self:
        if self.provider == "openai_compatible" and not self.base_url:
            raise ConfigError(
                "models: 'base_url' is required for provider 'openai_compatible'"
            )
        if self.provider == "anthropic" and not self.api_key:
            raise ConfigError("models: 'api_key' is required for provider 'anthropic'")
        if self.provider == "jev":
            # jev-provider-plan §1, §11: a real HTTP endpoint and a
            # bearer credential, exactly like a hosted chat provider.
            if not self.base_url:
                raise ConfigError("models: 'base_url' is required for provider 'jev'")
            if not self.api_key:
                raise ConfigError("models: 'api_key' is required for provider 'jev'")
            if self.mails_per_request != 1:
                raise ConfigError(
                    "models: 'mails_per_request' must be 1 for provider 'jev' "
                    "(jev-provider-plan §11): jev answers one candidate per "
                    "HTTP call, never a batch. Use 'max_concurrent_requests' "
                    "to raise real throughput instead."
                )
        return self


class ProcessorConfig(BaseModel):
    """One named question in Jev's structured vocabulary
    (jev-provider-plan §2, §9): `type` selects which of `criteria`
    (`noul`), `options` (`choice`), or `levels` (`score`) is meaningful;
    the other two must be left unset. `model:` alone determines how the
    question is compiled/sent (`prompt.py` for a chat provider, straight
    through for `jev`) -- there is no separate `backend:`/`kind:` field,
    since `models.<name>.provider` already says this.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    type: Literal["noul", "choice", "score"]
    question: str | None = None
    criteria: dict[str, str] | None = None
    options: dict[str, str] | None = None
    levels: list[str] | None = None
    include_body: bool = False

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.type == "noul":
            if self.question is not None:
                if self.criteria is not None:
                    raise ConfigError(
                        "processors: 'question' is shorthand for a 'noul' "
                        "processor's 'criteria' -- set one or the other, "
                        "not both"
                    )
                if self.options is not None or self.levels is not None:
                    raise ConfigError(
                        "processors: a 'noul' processor must not set "
                        "'options' or 'levels'"
                    )
                # jev-provider-plan §2: the plain-string shorthand implies
                # `criteria: {true: question}` -- deliberately not a
                # synthesized 'false' entry; the shorthand and the
                # explicit two-key form are two different valid shapes,
                # not one normalised into the other.
                self.criteria = {"true": self.question}
                return self
            if self.options is not None or self.levels is not None:
                raise ConfigError(
                    "processors: a 'noul' processor must not set 'options' or 'levels'"
                )
            if self.criteria is None:
                raise ConfigError(
                    "processors: a 'noul' processor requires 'criteria' "
                    "({'true': ..., 'false': ...}) or a plain 'question'"
                )
            if set(self.criteria) != {"true", "false"}:
                raise ConfigError(
                    "processors: a 'noul' processor's explicit 'criteria' "
                    "must have exactly the keys 'true' and 'false'"
                )
        elif self.type == "choice":
            if self.criteria is not None or self.levels is not None:
                raise ConfigError(
                    "processors: a 'choice' processor must not set "
                    "'criteria' or 'levels'"
                )
            if not self.options:
                raise ConfigError(
                    "processors: a 'choice' processor requires a "
                    "non-empty 'options' map"
                )
            if len(self.options) > 255:
                raise ConfigError(
                    "processors: 'options' may have at most 255 entries "
                    "(jev's own limit)"
                )
        else:  # score
            if self.criteria is not None or self.options is not None:
                raise ConfigError(
                    "processors: a 'score' processor must not set "
                    "'criteria' or 'options'"
                )
            if self.levels is None or not 2 <= len(self.levels) <= 10:
                raise ConfigError(
                    "processors: a 'score' processor requires 'levels' "
                    "with between 2 and 10 entries"
                )
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
    """A rule has no `id` and no `priority` (jev-provider-plan §9): nothing
    else in the config ever references a rule by name (see the plan's
    referenced-from table), and a matching rule's rank is simply its
    position in `rules:` -- first-listed wins (spec §7.4, reinterpreted:
    "config order" is now the *only* ordering key, not a tiebreaker under
    `priority`)."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    when: ConditionTree
    actions: list[str] = Field(min_length=1)

    @field_validator("when", mode="before")
    @classmethod
    def _parse_when(cls, value: object) -> ConditionTree:
        return parse_when(value)


class TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1)
    # Optional (jev-provider-plan §7): a task can scan its own mailbox,
    # exist purely as a `task:<id>` routing target, or both. Defaults to
    # an *empty* list, not `["INBOX"]` -- a routing-only task (no
    # `source_mailboxes` at all) must not silently also scan INBOX, which
    # is the whole point of §7's worked example being able to omit the
    # field on a task that exists only to receive routed candidates.
    source_mailboxes: list[str] = Field(default_factory=list)
    protect: ProtectConfig = Field(default_factory=ProtectConfig)
    max_new_mails: int | None = Field(default=None, gt=0)
    max_actions: int | None = Field(default=None, gt=0)
    rules: list[RuleConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_rules(self) -> Self:
        """Per-rule action-list validation (spec §7.3/§7.4/§7.5), done
        here rather than on `RuleConfig` itself so the error message can
        name a rule by its position (jev-provider-plan §9) -- a rule has
        no name of its own to report."""
        total = len(self.rules)
        for index, rule in enumerate(self.rules):
            _validate_actions(
                rule.actions,
                rule.when,
                rule_label=f"task rule #{index + 1} of {total}",
            )
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


def _validate_processor_atom(
    atom: ProcessorCondition, *, processors: Mapping[str, ProcessorConfig]
) -> None:
    """jev-provider-plan §9's per-atom cross-reference rule, once the
    atom's processor name is already known to exist: an equality/
    inequality comparison against `.value` on a `choice`/`score`
    processor must name one of its declared options/levels
    (case-sensitive exact match). `.confidence` and non-equality
    operators against `.value` (numeric `score` comparisons) need no
    such check -- there is no closed vocabulary to validate against."""
    processor = processors[atom.name]
    if atom.field != "value" or atom.op not in ("==", "!="):
        return
    if not isinstance(atom.value, str):
        return
    if processor.type == "choice":
        assert processor.options is not None
        if atom.value not in processor.options:
            raise ConfigError(
                f"processor {atom.name!r}: 'processor: \"{atom.name}.value "
                f"{atom.op} {atom.value}\"' names an option that is not "
                f"declared; options are {sorted(processor.options)}"
            )
    elif processor.type == "score":
        assert processor.levels is not None
        if atom.value not in processor.levels:
            raise ConfigError(
                f"processor {atom.name!r}: 'processor: \"{atom.name}.value "
                f"{atom.op} {atom.value}\"' names a level that is not "
                f"declared; levels are {processor.levels}"
            )
    elif processor.type == "noul" and atom.value not in ("true", "false"):
        # A `noul` processor's `.value` is a plain bool at runtime (jev
        # answers noul/true-false directly); a string comparand here can
        # never match anything a `noul` answer actually produces.
        raise ConfigError(
            f"processor {atom.name!r} is 'noul' (a boolean answer); "
            f"'processor: \"{atom.name}.value {atom.op} {atom.value}\"' "
            "should compare against the boolean true/false instead of a "
            "quoted string"
        )


def _check_routing_acyclic(edges: Mapping[str, frozenset[str]]) -> None:
    """DAG check for `task:<id>` routing edges (jev-provider-plan §7):
    plain DFS, the same style as `_MAX_NESTING_DEPTH`'s guard -- a
    routing cycle would ping-pong candidates between tasks forever across
    cron ticks and belongs at `config check` time, not discovered at 3am.
    Unknown targets are skipped here; they are already reported by the
    caller's separate existence check."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(edges, WHITE)

    def visit(node: str, path: tuple[str, ...]) -> None:
        color[node] = GRAY
        for neighbor in sorted(edges.get(node, frozenset())):
            if neighbor not in color:
                continue
            if color[neighbor] == GRAY:
                cycle = " -> ".join([*path, neighbor])
                raise ConfigError(
                    f"'task:' routing forms a cycle: {cycle} (jev-provider-plan §7)"
                )
            if color[neighbor] == WHITE:
                visit(neighbor, (*path, neighbor))
        color[node] = BLACK

    for node in sorted(edges):
        if color[node] == WHITE:
            visit(node, (node,))


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    settings: Settings = Field(default_factory=Settings)
    accounts: dict[str, AccountConfig] = Field(min_length=1)
    models: dict[str, ModelConfig] = Field(min_length=1)
    processors: dict[str, ProcessorConfig] = Field(default_factory=dict)
    tasks: dict[str, TaskConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _cross_reference(self) -> Self:
        trash_capable_accounts: set[str] = set()
        referenced_processors: set[str] = set()
        routing_edges: dict[str, set[str]] = {name: set() for name in self.tasks}
        routed_targets: set[str] = set()

        for task_name, task in self.tasks.items():
            if task.account not in self.accounts:
                raise ConfigError(
                    f"task {task_name!r} references unknown account {task.account!r}"
                )
            for rule in task.rules:
                if "trash" in rule.actions:
                    trash_capable_accounts.add(task.account)
                for atom in _collect_processor_atoms(rule.when):
                    referenced_processors.add(atom.name)
                for action in rule.actions:
                    if action.startswith("task:"):
                        target = action.removeprefix("task:")
                        routing_edges[task_name].add(target)
                        routed_targets.add(target)

        for account_name in trash_capable_accounts:
            if self.accounts[account_name].trash_mailbox is None:
                raise ConfigError(
                    f"account {account_name!r} is used by a task with a "
                    "'trash' action but has no 'trash_mailbox' configured "
                    "(spec §6)"
                )

        for name in sorted(referenced_processors):
            if name not in self.processors:
                raise ConfigError(
                    f"a rule references unknown processor {name!r}; "
                    "declare it under 'processors:'"
                )
        for name, processor in self.processors.items():
            if processor.model not in self.models:
                raise ConfigError(
                    f"processors.{name!r} references unknown model {processor.model!r}"
                )

        for task in self.tasks.values():
            for rule in task.rules:
                for atom in _collect_processor_atoms(rule.when):
                    if atom.name in self.processors:
                        _validate_processor_atom(atom, processors=self.processors)

        for target in sorted(routed_targets):
            if target not in self.tasks:
                raise ConfigError(
                    f"a 'task:{target}' action references unknown task {target!r}"
                )
        _check_routing_acyclic(
            {name: frozenset(targets) for name, targets in routing_edges.items()}
        )

        for task_name, task in self.tasks.items():
            if not task.source_mailboxes and task_name not in routed_targets:
                raise ConfigError(
                    f"task {task_name!r} has no candidate source: it has no "
                    "'source_mailboxes' and is never the target of a "
                    "'task:<id>' action anywhere in the config "
                    "(jev-provider-plan §7) -- it can never run"
                )
        return self


# --- File loading (spec §12) ----------------------------------------------

# Any bit here on a config file (which contains literal credentials, per
# spec §12) means it is readable, writable, or executable by someone other
# than its owner. The spec's minimum bar is "group- or world-readable"; we
# warn on the broader set deliberately, as the more conservative check.
_UNSAFE_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO


def check_file_permissions(path: Path) -> None:
    """Enforce spec §12's ownership requirement and warn about loose mode
    bits on the config file.

    Ownership is a hard failure (`ConfigFilePermissionError`, exit code 2):
    a config file owned by someone else may already have been read or
    tampered with by that other user, so refusing to load it is the only
    safe choice.

    Group- or world-readable mode bits are, deliberately, only a printed
    warning: the file still loads. Logging isn't configured yet at this
    point in `load_config`, so this prints directly to stderr rather than
    going through `liametahi.logging` — a narrow, intentional exception to
    this module otherwise only raising and leaving printing to the CLI
    layer.
    """
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
        print(
            f"warning: config file {path} has mode {oct(mode)}, which "
            "grants group or other access; it contains literal credentials "
            f"(spec §12) — run 'chmod 600 {path}'",
            file=sys.stderr,
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
