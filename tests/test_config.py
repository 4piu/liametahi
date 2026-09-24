"""Tests for `liametahi.config`."""

import copy
from collections.abc import Mapping
from pathlib import Path

import pytest

from liametahi import rules
from liametahi.config import (
    ConfigError,
    ConfigFilePermissionError,
    check_file_permissions,
    collect_header_names,
    compute_config_hash,
    has_deterministic_atom,
    load_config,
    parse_condition_tree,
    parse_when,
)
from tests.conftest import make_config_dict, write_config

# --- Happy path -------------------------------------------------------


def test_valid_config_loads(config_path: Path) -> None:
    cfg = load_config(config_path)
    assert cfg.version == 1
    assert "personal" in cfg.accounts
    assert "local" in cfg.models
    assert "inbox-cleanup" in cfg.tasks
    task = cfg.tasks["inbox-cleanup"]
    assert task.source_mailboxes == ["INBOX"]
    # No `protect` block in BASE_CONFIG: nothing is protected by default
    # -- a user must opt in explicitly.
    assert task.protect.unread is False
    assert task.protect.flags == []
    assert task.protect.senders == []
    assert task.max_new_mails is None  # unset means no cap
    assert task.max_actions is None  # unset means no cap


def test_explicit_max_actions_is_honoured(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["max_actions"] = 5
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)
    assert cfg.tasks["inbox-cleanup"].max_actions == 5


def test_max_actions_must_be_positive(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["max_actions"] = 0
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_explicit_protect_block_is_honoured(tmp_path: Path) -> None:
    """Protection is opt-in, but an explicit `protect` block
    still takes effect exactly as configured."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["protect"] = {
        "flags": ["\\Flagged"],
        "senders": ["boss@example.com"],
        "unread": True,
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)
    task = cfg.tasks["inbox-cleanup"]
    assert task.protect.flags == ["\\Flagged"]
    assert task.protect.senders == ["boss@example.com"]
    assert task.protect.unread is True


def test_settings_defaults_applied(config_path: Path) -> None:
    cfg = load_config(config_path)
    assert str(cfg.settings.state_db).endswith("state.sqlite3")
    assert cfg.settings.log_level == "info"


def test_full_jev_provider_plan_example_config_loads(tmp_path: Path) -> None:
    """The two-task, two-processor worked example (trimmed to what this
    config already has: account/model names, trash_mailbox)."""
    data = make_config_dict()
    data["models"]["local"]["provider"] = "openai_compatible"
    data["processors"] = {
        "spam-category": {
            "model": "local",
            "type": "choice",
            "instructions": "What kind of mail is this?",
            "criteria": {
                "spam": "unsolicited bulk mail",
                "personal": "legitimate mail",
            },
        },
        "spam-review": {
            "model": "local",
            "type": "noul",
            "fields": ["subject", "excerpt"],
            "instructions": "Is this spam?",
            "criteria": {"true": "this is spam", "false": "this is legitimate"},
        },
    }
    data["tasks"]["inbox-cleanup"]["rules"] = [
        {
            "when": [
                {"processor": "spam-category.value == spam"},
            ],
            "actions": ["task:inbox-review"],
        },
        {
            "when": [{"older-than": "30d"}, {"list-id-contains": "digest"}],
            "actions": ["backup", "trash"],
        },
    ]
    data["tasks"]["inbox-review"] = {
        "account": "personal",
        "rules": [
            {
                # A rule that trashes needs at least one non-processor
                # atom -- a `processor:` atom alone never counts as
                # deterministic. `in-mailbox` supplies it here.
                "when": [
                    {"processor": "spam-review.value >= 0.9"},
                    {"in-mailbox": "INBOX"},
                ],
                "actions": ["backup", "trash"],
            },
        ],
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)
    task = cfg.tasks["inbox-cleanup"]
    assert len(task.rules) == 2
    assert cfg.tasks["inbox-review"].source_mailboxes == []


# --- Structural validation (unknown keys, missing sections) ----------------


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["bogus"] = True
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_account_key_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["accounts"]["personal"]["bogus"] = True
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_rule_key_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["bogus"] = True
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_version_must_be_1(tmp_path: Path) -> None:
    data = make_config_dict()
    data["version"] = 2
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("section", ["accounts", "models", "tasks"])
def test_empty_required_section_rejected(tmp_path: Path, section: str) -> None:
    data = make_config_dict()
    data[section] = {}
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_required_section_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    del data["accounts"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


# --- Cross references --------------------------------------------------


def test_task_unknown_account_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["account"] = "does-not-exist"
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="unknown account"):
        load_config(path)


def test_processor_unknown_model_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["processors"] = {
        "spam-category": {
            "model": "does-not-exist",
            "type": "choice",
            "instructions": "q",
            "criteria": {"spam": "d"},
        }
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"processor": "spam-category.value == spam"}
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="unknown model"):
        load_config(path)


def test_rule_references_undeclared_processor_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"processor": "not-declared.value == spam"}
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="unknown processor"):
        load_config(path)


def test_trash_without_trash_mailbox_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    del data["accounts"]["personal"]["trash_mailbox"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="trash_mailbox"):
        load_config(path)


def test_trash_mailbox_not_required_when_unused(tmp_path: Path) -> None:
    data = make_config_dict()
    del data["accounts"]["personal"]["trash_mailbox"]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["backup"]
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)  # should not raise
    assert cfg.accounts["personal"].trash_mailbox is None


def test_rule_id_is_rejected_as_an_unknown_key(tmp_path: Path) -> None:
    """A rule has no `id` any more -- nothing
    references a rule by name, so the field is simply gone, and
    `extra="forbid"` rejects it like any other unknown key."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["id"] = "some-id"
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_rule_priority_is_rejected_as_an_unknown_key(tmp_path: Path) -> None:
    """A matching rule's rank is simply its
    position in `rules:` -- there is no separate `priority:` field any
    more."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["priority"] = 100
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_rules_are_simply_allowed_now(tmp_path: Path) -> None:
    """With no `id` to collide on, two rules with identical `when`/
    `actions` are unremarkable -- the first-listed one always wins."""
    data = make_config_dict()
    second = copy.deepcopy(data["tasks"]["inbox-cleanup"]["rules"][0])
    data["tasks"]["inbox-cleanup"]["rules"].append(second)
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)  # should not raise
    assert len(cfg.tasks["inbox-cleanup"].rules) == 2


# --- Rule constraints ---------------------------------------------------


def test_multiple_processor_atoms_in_one_rule_allowed(tmp_path: Path) -> None:
    """No per-rule count cap on `processor:` atoms
    -- the old `llm:` atom's "at most one" restriction is gone."""
    data = make_config_dict()
    data["processors"] = {
        "a": {
            "model": "local",
            "type": "choice",
            "instructions": "q",
            "criteria": {"x": "d"},
        },
        "b": {
            "model": "local",
            "type": "choice",
            "instructions": "q",
            "criteria": {"y": "d"},
        },
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"processor": "a.value == x"},
        {"processor": "b.value == y"},
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_processor_atom_under_not_is_allowed(tmp_path: Path) -> None:
    """Unlike the old `llm:` atom, `not` around a
    `processor:` atom is fine -- the payload no longer carries an
    un-negatable free-form description."""
    data = make_config_dict()
    data["processors"] = {
        "a": {
            "model": "local",
            "type": "choice",
            "instructions": "q",
            "criteria": {"x": "d"},
        },
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = {
        "not": {"processor": "a.value == x"}
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_trash_on_processor_only_rule_rejected(tmp_path: Path) -> None:
    """The deterministic-atom requirement for `trash` is
    unchanged by the redesign -- a `processor:` atom never counts as
    deterministic regardless of processor type or backend."""
    data = make_config_dict()
    data["processors"] = {
        "a": {
            "model": "local",
            "type": "choice",
            "instructions": "q",
            "criteria": {"x": "d"},
        },
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = {"processor": "a.value == x"}
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["trash"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="deterministic condition"):
        load_config(path)


def test_trash_with_deterministic_condition_allowed(tmp_path: Path) -> None:
    data = make_config_dict()
    data["processors"] = {
        "a": {
            "model": "local",
            "type": "choice",
            "instructions": "q",
            "criteria": {"x": "d"},
        },
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"older-than": "30d"},
        {"processor": "a.value == x"},
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["backup", "trash"]
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_trash_without_backup_is_now_valid(tmp_path: Path) -> None:
    """Backup-before-trash is no longer required
    -- 'trash' with no preceding 'backup' must load cleanly."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["trash"]
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_allow_trash_without_backup_is_an_unknown_field_error(tmp_path: Path) -> None:
    """The flag itself is gone, not merely
    deprecated -- setting it is a config error, not a silent no-op."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["trash"]
    data["tasks"]["inbox-cleanup"]["rules"][0]["allow_trash_without_backup"] = True
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_allow_body_excerpt_is_an_unknown_field_error(tmp_path: Path) -> None:
    """Body-excerpt opt-in moved to
    `ProcessorConfig.fields` (the `excerpt`/`html` catalog entries); the
    old per-rule flag is gone."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["allow_body_excerpt"] = True
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_more_than_one_remote_mutation_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["trash", "label:foo"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="at most one remote mutation"):
        load_config(path)


def test_two_move_to_actions_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = [
        "move_to:Archive",
        "move_to:Other",
    ]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="at most one remote mutation"):
        load_config(path)


@pytest.mark.parametrize(
    "keyword",
    [
        "has space",
        'quote"mark',
        "back\\slash",
        "brack]et",
        "pct%ent",
        "star*x",
        "paren(x)",
        "brace{x}",
    ],
)
def test_label_keyword_invalid_atom_rejected(tmp_path: Path, keyword: str) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = [f"label:{keyword}"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="valid IMAP atom|unknown action"):
        load_config(path)


def test_label_keyword_valid_atom_accepted(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["label:work-important"]
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_unknown_action_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["delete_forever"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="unknown action"):
        load_config(path)


def test_move_to_requires_mailbox(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="move_to"):
        load_config(path)


# --- Top-level `when:` shapes -----------------------------------------


def test_when_as_a_list_is_implicit_all() -> None:
    tree = parse_when([{"older-than": "30d"}, {"sender-match": "*@bar.com"}])
    assert isinstance(tree, rules.AllNode)
    assert len(tree.children) == 2
    assert isinstance(tree.children[0], rules.OlderThan)
    assert isinstance(tree.children[1], rules.SenderMatch)


def test_when_as_a_single_atom_still_works() -> None:
    """A rule that only ever needed one condition loses nothing -- no
    list wrapper required."""
    tree = parse_when({"older-than": "30d"})
    assert isinstance(tree, rules.OlderThan)


def test_when_as_a_single_any_still_works() -> None:
    """A rule that's fundamentally an OR at the top stays a bare `any:`,
    not `when: [{any: [...]}]`."""
    tree = parse_when({"any": [{"older-than": "30d"}, {"larger-than": "1M"}]})
    assert isinstance(tree, rules.AnyNode)


def test_when_top_level_all_is_rejected() -> None:
    """The old top-level `all:` wrapper is gone -- it was purely
    redundant with the list form, and having two spellings for the same
    thing was the specific awkwardness this replaces."""
    with pytest.raises(ConfigError, match="no longer accepted"):
        parse_when({"all": [{"older-than": "30d"}, {"larger-than": "1M"}]})


def test_when_empty_list_rejected() -> None:
    with pytest.raises(ConfigError):
        parse_when([])


def test_when_nested_all_inside_any_still_works() -> None:
    """`all:` remains valid as a *nested* keyword -- only the redundant
    top-level wrapper was removed."""
    tree = parse_when(
        [
            {
                "any": [
                    {"all": [{"older-than": "30d"}, {"larger-than": "1M"}]},
                    {"sender-match": "*@bar.com"},
                ]
            }
        ]
    )
    assert isinstance(tree, rules.AllNode)
    assert len(tree.children) == 1
    inner = tree.children[0]
    assert isinstance(inner, rules.AnyNode)
    assert isinstance(inner.children[0], rules.AllNode)


# --- Nesting depth -------------------------------------------------------


def test_nesting_depth_3_allowed() -> None:
    raw = {"all": [{"any": [{"not": {"older-than": "1d"}}]}]}
    tree = parse_condition_tree(raw)
    assert tree is not None


def test_nesting_depth_4_rejected() -> None:
    raw = {"all": [{"any": [{"not": {"all": [{"older-than": "1d"}]}}]}]}
    with pytest.raises(ConfigError, match="nesting"):
        parse_condition_tree(raw)


def test_condition_node_must_have_exactly_one_key() -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"older-than": "1d", "newer-than": "2d"})
    with pytest.raises(ConfigError):
        parse_condition_tree({})


def test_all_requires_nonempty_list() -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"all": []})


def test_unknown_condition_key_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown condition"):
        parse_condition_tree({"bogus-condition": "x"})


# --- Value grammars --------------------------------------------------------


@pytest.mark.parametrize("value", ["30d", "12h", "2w", "1s", "999m"])
def test_valid_durations(value: str) -> None:
    tree = parse_condition_tree({"older-than": value})
    assert isinstance(tree, rules.OlderThan)


@pytest.mark.parametrize("value", ["1d12h", "1.5d", "d", "30", "-1d", "30x"])
def test_invalid_durations_rejected(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"older-than": value})


@pytest.mark.parametrize(
    "value,expected_bytes",
    [
        ("500k", 500 * 1024),
        ("2M", 2 * 1024 * 1024),
        ("1048576", 1048576),
        ("1g", 1024**3),
    ],
)
def test_valid_sizes(value: str, expected_bytes: int) -> None:
    tree = parse_condition_tree({"larger-than": value})
    assert isinstance(tree, rules.LargerThan)
    assert tree.size_bytes == expected_bytes


@pytest.mark.parametrize("value", ["", "abc", "-5k", "5.5k", "5X"])
def test_invalid_sizes_rejected(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"larger-than": value})


@pytest.mark.parametrize(
    "value,op,count",
    [
        (">10", ">", 10),
        (">=5", ">=", 5),
        ("<3", "<", 3),
        ("<=3", "<=", 3),
        ("==1", "==", 1),
        ("!=0", "!=", 0),
    ],
)
def test_valid_recipient_count_comparisons(value: str, op: str, count: int) -> None:
    tree = parse_condition_tree({"recipient-count": value})
    assert isinstance(tree, rules.RecipientCount)
    assert tree.op == op
    assert tree.value == count


@pytest.mark.parametrize(
    "value", ["", "10", "=10", ">", ">-5", "> 10", ">10.5", "=>10"]
)
def test_invalid_recipient_count_comparisons_rejected(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"recipient-count": value})


# --- has-attachment ----------------------------------------------------


def test_has_attachment_true_boolean_accepted() -> None:
    tree = parse_condition_tree({"has-attachment": True})
    assert isinstance(tree, rules.HasAttachment)


@pytest.mark.parametrize("value", [False, "true", 1, "True", None, "yes"])
def test_has_attachment_rejects_anything_but_literal_true(value: object) -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"has-attachment": value})


# --- auth-result -------------------------------------------------------


@pytest.mark.parametrize(
    "value,mechanism,result",
    [
        ("spf=pass", "spf", "pass"),
        ("dkim=fail", "dkim", "fail"),
        ("dmarc=softfail", "dmarc", "softfail"),
        ("SPF=PASS", "SPF", "PASS"),
    ],
)
def test_auth_result_valid_values_accepted(
    value: str, mechanism: str, result: str
) -> None:
    tree = parse_condition_tree({"auth-result": value})
    assert isinstance(tree, rules.AuthResult)
    assert tree.regex.search(f"host; {mechanism}={result}")


@pytest.mark.parametrize(
    "value",
    ["foo=pass", "spfpass", "spf=", "=pass", "spf==pass", "spf=pass=extra", ""],
)
def test_auth_result_invalid_values_rejected(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"auth-result": value})


def test_auth_result_fetch_headers_include_authentication_results(
    tmp_path: Path,
) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"auth-result": "spf=fail"},
        {"older-than": "1d"},
    ]
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)
    assert "AUTHENTICATION-RESULTS" in cfg.tasks["inbox-cleanup"].fetch_headers


def test_auth_result_absent_fetch_headers_excludes_authentication_results(
    tmp_path: Path,
) -> None:
    cfg = load_config(write_config(tmp_path / "cfg.yaml", make_config_dict()))
    assert "AUTHENTICATION-RESULTS" not in cfg.tasks["inbox-cleanup"].fetch_headers


def test_sender_match_plain_value_is_a_literal_pattern() -> None:
    tree = parse_condition_tree({"sender-match": "*@example.org"})
    assert isinstance(tree, rules.SenderMatch)
    assert isinstance(tree.pattern, rules.LiteralPattern)
    assert tree.pattern.text == "*@example.org"


def test_sender_match_regex_literal_is_compiled() -> None:
    tree = parse_condition_tree({"sender-match": r"/.+@gmail\.com/i"})
    assert isinstance(tree, rules.SenderMatch)
    assert isinstance(tree.pattern, rules.RegexPattern)
    assert tree.pattern.regex.match("someone@gmail.com")
    assert tree.pattern.regex.match("SOMEONE@GMAIL.COM")  # `i` flag honoured


def test_regex_literal_without_flags_is_case_sensitive() -> None:
    tree = parse_condition_tree({"subject-contains": "/urgent/"})
    assert isinstance(tree, rules.SubjectContains)
    assert isinstance(tree.pattern, rules.RegexPattern)
    assert tree.pattern.regex.search("Urgent: reply needed") is None
    assert tree.pattern.regex.search("this is urgent")


def test_regex_literal_g_flag_is_a_documented_no_op() -> None:
    tree = parse_condition_tree({"subject-contains": "/urgent/g"})
    assert isinstance(tree, rules.SubjectContains)
    assert isinstance(tree.pattern, rules.RegexPattern)
    assert tree.pattern.regex.search("this is urgent")


def test_regex_literal_invalid_pattern_rejected() -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"sender-match": "/(unclosed/"})


def test_regex_literal_unknown_flag_rejected() -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"sender-match": "/foo/x"})


def test_value_that_is_just_a_slash_stays_literal() -> None:
    """A single `/` cannot supply both delimiters, so it is not
    misparsed as an (invalid) empty regex."""
    tree = parse_condition_tree({"subject-contains": "/"})
    assert isinstance(tree, rules.SubjectContains)
    assert isinstance(tree.pattern, rules.LiteralPattern)
    assert tree.pattern.text == "/"


# --- Model provider requirements ----------------------------------------


def test_openai_compatible_requires_base_url(tmp_path: Path) -> None:
    data = make_config_dict()
    del data["models"]["local"]["base_url"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="base_url"):
        load_config(path)


def test_anthropic_requires_api_key(tmp_path: Path) -> None:
    data = make_config_dict()
    data["models"]["cloud"] = {"provider": "anthropic", "model": "claude-haiku-4-5"}
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="api_key"):
        load_config(path)


def test_anthropic_with_api_key_ok(tmp_path: Path) -> None:
    data = make_config_dict()
    data["models"]["cloud"] = {
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "api_key": "sk-ant-xxxx",
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_mails_per_request_has_no_upper_bound(tmp_path: Path) -> None:
    """A large batch measurably degrades small local models, but that is
    guidance for the README, not grounds for rejecting the config: an
    implicit ceiling the user never wrote is exactly the kind of hidden
    limit these keys were reworked to remove. Only <1 is refused."""
    data = make_config_dict()
    data["models"]["local"]["mails_per_request"] = 200
    load_config(write_config(tmp_path / "cfg.yaml", data))  # must not raise

    data["models"]["local"]["mails_per_request"] = 0
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path / "cfg2.yaml", data))


# --- Derived fetch-header list ----------------------------------------


def test_fetch_headers_includes_base_set(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path / "cfg.yaml", make_config_dict()))
    task = cfg.tasks["inbox-cleanup"]
    for header in (
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
    ):
        assert header in task.fetch_headers


def test_fetch_headers_includes_has_header_names(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"has-header": "X-Spam-Flag"},
        {"older-than": "1d"},
    ]
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)
    assert "X-SPAM-FLAG" in cfg.tasks["inbox-cleanup"].fetch_headers


def test_collect_header_names_walks_full_tree() -> None:
    tree = parse_condition_tree(
        {"any": [{"has-header": "a"}, {"not": {"has-header": "b"}}]}
    )
    assert collect_header_names(tree) == frozenset({"a", "b"})


def test_has_deterministic_atom() -> None:
    processor_atom = {"processor": "spam.value == true"}
    assert has_deterministic_atom(parse_condition_tree({"older-than": "1d"}))
    assert not has_deterministic_atom(parse_condition_tree(processor_atom))
    assert has_deterministic_atom(
        parse_condition_tree({"all": [processor_atom, {"older-than": "1d"}]})
    )
    assert not has_deterministic_atom(parse_condition_tree({"none": [processor_atom]}))


# --- File permission/ownership checks (acceptance test 15) ---------------


def test_acceptance_15_world_readable_config_warns_but_loads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    path.chmod(0o644)
    load_config(path)  # should not raise
    stderr = capsys.readouterr().err
    assert "warning" in stderr
    assert str(path) in stderr


def test_group_readable_config_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    path.chmod(0o640)
    check_file_permissions(path)  # should not raise
    assert "warning" in capsys.readouterr().err


def test_owner_only_config_accepted(tmp_path: Path) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    path.chmod(0o600)
    check_file_permissions(path)  # should not raise


def test_config_owned_by_another_user_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership stays a hard failure even though loose mode bits (above)
    now only warn: a file owned by someone else may already have been
    tampered with."""
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    path.chmod(0o600)
    monkeypatch.setattr("os.getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ConfigFilePermissionError):
        check_file_permissions(path)


def test_missing_config_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_invalid_yaml_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("version: 1\n  bad: [indent\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ConfigError):
        load_config(path)


def test_config_hash_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    path1 = write_config(tmp_path / "a.yaml", make_config_dict())
    path2 = write_config(tmp_path / "b.yaml", make_config_dict())
    other = make_config_dict()
    other["settings"] = {"log_level": "debug"}
    path3 = write_config(tmp_path / "c.yaml", other)
    assert compute_config_hash(path1) == compute_config_hash(path2)
    assert compute_config_hash(path1) != compute_config_hash(path3)


# --- `none:` composition keyword --------------------------------------------


def test_none_composition_parses_to_none_node() -> None:
    tree = parse_condition_tree(
        {"none": [{"older-than": "90d"}, {"larger-than": "1M"}]}
    )
    assert isinstance(tree, rules.NoneNode)
    assert len(tree.children) == 2


def test_none_requires_nonempty_list() -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"none": []})


def test_none_counts_toward_nesting_depth_same_as_any() -> None:
    raw = {"all": [{"any": [{"none": [{"older-than": "1d"}]}]}]}
    parse_condition_tree(raw)  # depth 3, must not raise
    too_deep = {"all": [{"any": [{"none": [{"not": {"older-than": "1d"}}]}]}]}
    with pytest.raises(ConfigError, match="nesting"):
        parse_condition_tree(too_deep)


# --- `processor:` atom grammar -----------------------------------------------


def test_processor_condition_equality_string_comparand() -> None:
    tree = parse_condition_tree({"processor": "spam-category.value == spam"})
    assert isinstance(tree, rules.ProcessorCondition)
    assert tree.name == "spam-category"
    assert tree.field == "value"
    assert tree.op == "=="
    assert tree.value == "spam"


def test_processor_condition_numeric_confidence_comparand() -> None:
    tree = parse_condition_tree({"processor": "urgency.confidence >= 0.85"})
    assert isinstance(tree, rules.ProcessorCondition)
    assert tree.field == "confidence"
    assert tree.op == ">="
    assert tree.value == 0.85


def test_processor_condition_boolean_comparand() -> None:
    tree = parse_condition_tree({"processor": "vibe-check.value == true"})
    assert isinstance(tree, rules.ProcessorCondition)
    assert tree.value is True


@pytest.mark.parametrize(
    "text",
    [
        "missing-dot-field == spam",  # no ".field"
        "spam-category.value",  # no "op value"
        "spam-category.value spam",  # no operator at all
    ],
)
def test_processor_condition_malformed_shape_rejected(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"processor": text})


def test_processor_condition_invalid_field_rejected() -> None:
    with pytest.raises(ConfigError, match="'value' or 'confidence'"):
        parse_condition_tree({"processor": "spam-category.bogus == spam"})


def test_processor_condition_non_numeric_operator_rejected() -> None:
    with pytest.raises(ConfigError, match="numeric comparand"):
        parse_condition_tree({"processor": "spam-category.value >= spam"})


def test_processor_condition_boolean_with_inequality_operator_rejected() -> None:
    with pytest.raises(ConfigError, match="numeric comparand"):
        parse_condition_tree({"processor": "vibe-check.value < true"})


# --- `_parse_comparison`'s widened decimal grammar --------------------------


@pytest.mark.parametrize("value", [">=0.85", ">0.5", "<=0.99", "==0.5", "!=0.1"])
def test_processor_condition_decimal_comparisons_accepted(value: str) -> None:
    tree = parse_condition_tree({"processor": f"urgency.confidence {value}"})
    assert isinstance(tree, rules.ProcessorCondition)
    assert isinstance(tree.value, float)


@pytest.mark.parametrize(
    "value,op,count",
    [(">10", ">", 10), (">=5", ">=", 5)],
)
def test_recipient_count_integer_forms_still_work_after_widening(
    value: str, op: str, count: int
) -> None:
    """The regex widened to accept decimals must not change existing
    integer-only behaviour for `recipient-count`."""
    tree = parse_condition_tree({"recipient-count": value})
    assert isinstance(tree, rules.RecipientCount)
    assert tree.op == op
    assert tree.value == count
    assert isinstance(tree.value, int)


def test_recipient_count_rejects_a_decimal_comparand() -> None:
    with pytest.raises(ConfigError, match="integer"):
        parse_condition_tree({"recipient-count": ">1.5"})


# --- `ProcessorConfig` validation --------------------------------------------


def _processors_config(
    tmp_path: Path,
    processors: Mapping[str, object],
    when: list[object] | dict[str, object],
) -> Path:
    data = make_config_dict()
    data["processors"] = processors
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = when
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    return write_config(tmp_path / "cfg.yaml", data)


def test_noul_processor_instructions_only_is_valid(tmp_path: Path) -> None:
    """`criteria` is a genuinely optional refinement for `noul`, not an
    alternate encoding of `instructions` -- giving only `instructions`
    loads cleanly with no synthesized `criteria`, exactly like jev's own
    documented `is_human_escalation` example."""
    processors = {
        "vibe-check": {
            "model": "local",
            "type": "noul",
            "instructions": "Is this a routine newsletter?",
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value >= 0.5"}]
    )
    cfg = load_config(path)
    assert cfg.processors["vibe-check"].criteria is None


def test_noul_processor_explicit_criteria_requires_both_keys(tmp_path: Path) -> None:
    processors = {
        "vibe-check": {
            "model": "local",
            "type": "noul",
            "instructions": "x?",
            "criteria": {"true": "x"},
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value >= 0.5"}]
    )
    with pytest.raises(ConfigError, match="exactly the keys"):
        load_config(path)


def test_noul_processor_instructions_and_criteria_both_set_is_valid(
    tmp_path: Path,
) -> None:
    """`criteria` is given *together with* `instructions` when present,
    not as an alternate encoding of it -- both may be set at once."""
    processors = {
        "vibe-check": {
            "model": "local",
            "type": "noul",
            "instructions": "x?",
            "criteria": {"true": "a", "false": "b"},
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value >= 0.5"}]
    )
    cfg = load_config(path)
    assert cfg.processors["vibe-check"].criteria == {"true": "a", "false": "b"}


def test_processor_instructions_required_for_every_type(tmp_path: Path) -> None:
    processors = {
        "spam-category": {
            "model": "local",
            "type": "choice",
            "criteria": {"spam": "d"},
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "spam-category.value == spam"}]
    )
    with pytest.raises(ConfigError, match="instructions"):
        load_config(path)


# --- `ProcessorConfig.fields` -----------------------------------------------


def test_processor_with_no_fields_gets_the_default_profile(tmp_path: Path) -> None:
    from liametahi import prompt

    processors = {
        "vibe-check": {
            "model": "local",
            "type": "noul",
            "instructions": "x?",
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value >= 0.5"}]
    )
    cfg = load_config(path)
    assert cfg.processors["vibe-check"].fields is None
    assert (
        cfg.processors["vibe-check"].resolved_fields == prompt.DEFAULT_PROCESSOR_FIELDS
    )


def test_processor_fields_unknown_catalog_name_rejected(tmp_path: Path) -> None:
    """An unrecognised name is a `ConfigError` at load time, not a
    silent no-op."""
    processors = {
        "vibe-check": {
            "model": "local",
            "type": "noul",
            "instructions": "x?",
            "fields": ["subject", "not-a-real-field"],
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value >= 0.5"}]
    )
    with pytest.raises(ConfigError, match="not-a-real-field"):
        load_config(path)


def test_processor_fields_is_a_full_replace_not_a_merge(tmp_path: Path) -> None:
    """Setting `fields:` at all means exactly the listed fields and
    nothing else -- no partial-override syntax that extends the default
    profile, matching the project's existing no-partial-merge convention
    for `criteria`/`options`."""
    processors = {
        "vibe-check": {
            "model": "local",
            "type": "noul",
            "instructions": "x?",
            "fields": ["subject"],
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value >= 0.5"}]
    )
    cfg = load_config(path)
    assert cfg.processors["vibe-check"].resolved_fields == ("subject",)


def test_processor_fields_empty_list_is_not_the_default(tmp_path: Path) -> None:
    """An explicit empty list is a real, if degenerate, selection -- it
    must not be silently treated the same as omitting `fields:` entirely."""
    processors = {
        "vibe-check": {
            "model": "local",
            "type": "noul",
            "instructions": "x?",
            "fields": [],
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value >= 0.5"}]
    )
    cfg = load_config(path)
    assert cfg.processors["vibe-check"].fields == []
    assert cfg.processors["vibe-check"].resolved_fields == ()


def test_choice_processor_requires_nonempty_criteria(tmp_path: Path) -> None:
    processors = {
        "spam-category": {"model": "local", "type": "choice", "instructions": "q"}
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "spam-category.value == spam"}]
    )
    with pytest.raises(ConfigError, match="criteria"):
        load_config(path)


def test_choice_processor_rejects_list_shaped_criteria(tmp_path: Path) -> None:
    """`criteria`'s expected shape depends on `type` -- a `choice`
    processor requires the `{option: description}` mapping shape, not
    `score`'s ordered list shape."""
    processors = {
        "spam-category": {
            "model": "local",
            "type": "choice",
            "instructions": "q",
            "criteria": ["low", "high"],
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "spam-category.value == spam"}]
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_score_processor_requires_criteria_between_2_and_10(tmp_path: Path) -> None:
    processors = {
        "urgency": {
            "model": "local",
            "type": "score",
            "instructions": "q",
            "criteria": ["only-one"],
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "urgency.value == only-one"}]
    )
    with pytest.raises(ConfigError, match="criteria"):
        load_config(path)


def test_score_processor_more_than_10_criteria_rejected(tmp_path: Path) -> None:
    processors = {
        "urgency": {
            "model": "local",
            "type": "score",
            "instructions": "q",
            "criteria": [str(i) for i in range(11)],
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "urgency.value == 0"}]
    )
    with pytest.raises(ConfigError, match="criteria"):
        load_config(path)


def test_choice_processor_option_count_over_255_rejected(tmp_path: Path) -> None:
    processors = {
        "spam-category": {
            "model": "local",
            "type": "choice",
            "instructions": "q",
            "criteria": {f"o{i}": "d" for i in range(256)},
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "spam-category.value == o1"}]
    )
    with pytest.raises(ConfigError, match="255"):
        load_config(path)


# --- Cross-reference: option/level vocabulary -------------------------


def test_choice_processor_atom_value_must_be_a_declared_option(tmp_path: Path) -> None:
    processors = {
        "spam-category": {
            "model": "local",
            "type": "choice",
            "instructions": "q",
            "criteria": {"spam": "d", "personal": "d"},
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "spam-category.value == bogus"}]
    )
    with pytest.raises(ConfigError, match="not declared"):
        load_config(path)


def test_score_processor_atom_value_must_be_a_declared_level(tmp_path: Path) -> None:
    processors = {
        "urgency": {
            "model": "local",
            "type": "score",
            "instructions": "q",
            "criteria": ["low", "high"],
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "urgency.value == medium"}]
    )
    with pytest.raises(ConfigError, match="not declared"):
        load_config(path)


def test_choice_processor_atom_value_case_sensitive_match(tmp_path: Path) -> None:
    processors = {
        "spam-category": {
            "model": "local",
            "type": "choice",
            "instructions": "q",
            "criteria": {"spam": "d"},
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "spam-category.value == Spam"}]
    )
    with pytest.raises(ConfigError, match="not declared"):
        load_config(path)


def test_noul_processor_atom_value_string_comparand_rejected(tmp_path: Path) -> None:
    processors = {
        "vibe-check": {"model": "local", "type": "noul", "instructions": "x?"},
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value == maybe"}]
    )
    with pytest.raises(ConfigError, match="probability"):
        load_config(path)


def test_noul_processor_atom_value_boolean_comparand_rejected(tmp_path: Path) -> None:
    """The old `.value == true` style must not silently misbehave --
    `.value` is a probability now, so a bare boolean comparand fails
    config validation with a message explaining why."""
    processors = {
        "vibe-check": {"model": "local", "type": "noul", "instructions": "x?"},
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value == true"}]
    )
    with pytest.raises(ConfigError, match="probability"):
        load_config(path)


def test_noul_processor_atom_value_numeric_comparand_needs_no_vocabulary_check(
    tmp_path: Path,
) -> None:
    """A numeric comparand against a `noul` processor's `.value` (any
    operator) is accepted with no vocabulary check -- there is no closed
    vocabulary for a probability."""
    processors = {
        "vibe-check": {"model": "local", "type": "noul", "instructions": "x?"},
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.value >= 0.9"}]
    )
    load_config(path)  # should not raise


def test_confidence_field_against_jev_processor_not_checked_against_vocabulary(
    tmp_path: Path,
) -> None:
    """Only `.value` equality/inequality is checked against a declared
    vocabulary; `.confidence` on a `provider: jev` processor is a plain
    float with nothing to validate against (and jev always populates
    it for `choice`/`score`)."""
    data = make_config_dict()
    data["models"]["jev-primary"] = {
        "provider": "jev",
        "base_url": "https://jev.example.com/v1/systemone",
        "model": "jev-latest",
        "api_key": "secret",
        "mails_per_request": 1,
    }
    data["processors"] = {
        "urgency": {
            "model": "jev-primary",
            "type": "score",
            "instructions": "q",
            "criteria": ["low", "high"],
        }
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"processor": "urgency.confidence >= 0.5"}
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_confidence_field_against_chat_backed_choice_score_processor_accepted(
    tmp_path: Path,
) -> None:
    """'field: confidence' resolves for a `choice`/`score` processor on
    any backend now, not just `provider: jev`: a chat-backed processor's
    compiled schema asks for `value_probability`/`runner_up_probability`
    and derives a margin-based `.confidence` from them
    (`prompt.py`'s `_derive_margin_confidence`), so this is no longer a
    load-time rejection."""
    processors = {
        "urgency": {
            "model": "local",
            "type": "score",
            "instructions": "q",
            "criteria": ["low", "high"],
        }
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "urgency.confidence >= 0.5"}]
    )
    load_config(path)  # should not raise


def test_confidence_field_against_noul_processor_rejected_on_any_backend(
    tmp_path: Path,
) -> None:
    """A `.confidence` atom against a `noul` processor is rejected at
    config-load time regardless of provider -- `noul` never populates a
    separate confidence, on jev or on a chat-compiled schema."""
    data = make_config_dict()
    data["models"]["jev-primary"] = {
        "provider": "jev",
        "base_url": "https://jev.example.com/v1/systemone",
        "model": "jev-latest",
        "api_key": "secret",
        "mails_per_request": 1,
    }
    data["processors"] = {
        "vibe-check": {
            "model": "jev-primary",
            "type": "noul",
            "instructions": "q",
        }
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"processor": "vibe-check.confidence >= 0.5"}
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="confidence"):
        load_config(path)


def test_confidence_field_against_chat_backed_noul_processor_rejected(
    tmp_path: Path,
) -> None:
    """The same rejection applies to a `noul` processor answered by a
    chat-compiled provider (`openai_compatible`/`anthropic`) -- not just
    `jev` -- since the "jev-only" carve-out in
    `_validate_processor_atom` is for `choice`/`score` only, never for
    `noul` on any backend."""
    processors = {
        "vibe-check": {"model": "local", "type": "noul", "instructions": "q"},
    }
    path = _processors_config(
        tmp_path, processors, [{"processor": "vibe-check.confidence >= 0.5"}]
    )
    with pytest.raises(ConfigError, match="confidence"):
        load_config(path)


# --- `task:<id>` routing cross-references -----------------------------


def test_task_routing_to_unknown_task_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["task:does-not-exist"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="unknown task"):
        load_config(path)


def test_task_routing_cycle_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["task:second"]
    data["tasks"]["second"] = {
        "account": "personal",
        "rules": [{"when": {"older-than": "1d"}, "actions": ["task:inbox-cleanup"]}],
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="cycle"):
        load_config(path)


def test_task_routing_self_loop_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["task:inbox-cleanup"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="cycle"):
        load_config(path)


def test_task_routing_dag_without_cycle_is_valid(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["task:second"]
    data["tasks"]["second"] = {
        "account": "personal",
        "rules": [{"when": {"older-than": "1d"}, "actions": ["backup", "trash"]}],
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_task_with_no_source_and_no_routing_target_is_unreachable(
    tmp_path: Path,
) -> None:
    data = make_config_dict()
    data["tasks"]["orphan"] = {
        "account": "personal",
        "rules": [{"when": {"older-than": "1d"}, "actions": ["backup", "trash"]}],
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="no candidate source"):
        load_config(path)


def test_task_that_is_only_a_routing_target_is_reachable(tmp_path: Path) -> None:
    """`source_mailboxes` is optional -- a task
    that exists purely as a `task:<id>` target, with no mailbox scan of
    its own, is a valid, reachable task."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["task:downstream"]
    data["tasks"]["downstream"] = {
        "account": "personal",
        "rules": [{"when": {"older-than": "1d"}, "actions": ["backup", "trash"]}],
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)
    assert cfg.tasks["downstream"].source_mailboxes == []


def test_task_action_does_not_count_as_a_remote_mutation(tmp_path: Path) -> None:
    """`task:<id>` composes freely with a real
    remote mutation in the same action list."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = [
        "backup",
        "trash",
        "task:second",
    ]
    data["tasks"]["second"] = {
        "account": "personal",
        "rules": [{"when": {"older-than": "1d"}, "actions": ["backup", "trash"]}],
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_task_empty_target_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["task:"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="task"):
        load_config(path)


# --- `jev` provider requirements ---------------------------------------------


def test_jev_provider_requires_base_url_and_api_key(tmp_path: Path) -> None:
    data = make_config_dict()
    data["models"]["jev-primary"] = {"provider": "jev", "model": "jev-latest"}
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="base_url"):
        load_config(path)


def test_jev_provider_requires_mails_per_request_one(tmp_path: Path) -> None:
    data = make_config_dict()
    data["models"]["jev-primary"] = {
        "provider": "jev",
        "model": "jev-latest",
        "base_url": "https://api.example.com/v1/systemone",
        "api_key": "k",
        "mails_per_request": 5,
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="mails_per_request"):
        load_config(path)


def test_jev_provider_valid_config_loads(tmp_path: Path) -> None:
    data = make_config_dict()
    data["models"]["jev-primary"] = {
        "provider": "jev",
        "model": "jev-latest",
        "base_url": "https://api.example.com/v1/systemone",
        "api_key": "k",
        "mails_per_request": 1,
    }
    data["processors"] = {
        "spam-category": {
            "model": "jev-primary",
            "type": "choice",
            "instructions": "q",
            "criteria": {"spam": "d", "personal": "d"},
        }
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"processor": "spam-category.value == spam"}
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)
    assert cfg.processors["spam-category"].model == "jev-primary"
