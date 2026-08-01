"""Tests for `liametahi.config` (spec §6, §7.3, §12; contracts §3)."""

import copy
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
    # (spec §6) -- a user must opt in explicitly.
    assert task.protect.unread is False
    assert task.protect.flags == []
    assert task.protect.senders == []
    assert task.max_candidates_per_run == 500
    assert task.max_actions_per_run is None  # unset means no cap (spec §6)


def test_explicit_max_actions_per_run_is_honoured(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["max_actions_per_run"] = 5
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)
    assert cfg.tasks["inbox-cleanup"].max_actions_per_run == 5


def test_max_actions_per_run_must_be_positive(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["max_actions_per_run"] = 0
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


def test_explicit_protect_block_is_honoured(tmp_path: Path) -> None:
    """spec §6: protection is opt-in, but an explicit `protect` block
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
    assert cfg.settings.candidate_retention_days == 90
    assert str(cfg.settings.state_db).endswith("state.sqlite3")
    assert cfg.settings.log_level == "info"


def test_full_spec_example_config_loads(tmp_path: Path) -> None:
    """The full example from spec §6, including all three sample rules."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"] = [
        {
            "id": "old-weekly-digest",
            "priority": 100,
            "when": [{"older-than": "30d"}, {"list-id-contains": "digest"}],
            "actions": ["backup", "trash"],
        },
        {
            "id": "mozmail-notifications",
            "priority": 50,
            "when": [
                {"recipient-match": "*@mozmail.com"},
                {"older-than": "7d"},
                {
                    "llm": (
                        "Automated, low-priority notification with no requested action."
                    )
                },
            ],
            "allow_content_escalation": True,
            "actions": ["move_to:Archive"],
        },
        {
            "id": "stale-updates",
            "priority": 10,
            "when": [
                {"older-than": "14d"},
                {
                    "llm": (
                        "An update or announcement that is safe to discard, "
                        "except bills, account-security notices, or mail that "
                        "asks the recipient to act."
                    )
                },
            ],
            "actions": ["backup", "trash"],
        },
    ]
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)
    task = cfg.tasks["inbox-cleanup"]
    assert len(task.rules) == 3


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


def test_task_unknown_model_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["model"] = "does-not-exist"
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="unknown model"):
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


def test_duplicate_rule_id_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    second = copy.deepcopy(data["tasks"]["inbox-cleanup"]["rules"][0])
    data["tasks"]["inbox-cleanup"]["rules"].append(second)
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="duplicate rule id"):
        load_config(path)


# --- Rule constraints (spec §7.3) --------------------------------------


def test_two_llm_atoms_in_one_rule_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"llm": "first"},
        {"llm": "second"},
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="at most one 'llm' atom"):
        load_config(path)


def test_llm_under_not_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = {
        "not": {"llm": "should not be allowed here"}
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="'llm' may not appear under 'not'"):
        load_config(path)


def test_trash_on_llm_only_rule_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = {"llm": "discard freely"}
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["trash"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="deterministic condition"):
        load_config(path)


def test_trash_with_deterministic_condition_allowed(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"older-than": "30d"},
        {"llm": "safe to discard"},
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["backup", "trash"]
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


def test_trash_without_backup_rejected_unless_opted_out(tmp_path: Path) -> None:
    """spec §7.4: 'trash' silently fails every run without a preceding
    'backup' in the same action list, unless the rule explicitly opts
    out via `allow_trash_without_backup` (e.g. because the account's own
    trash folder is recovery enough). Config load must catch this, not
    let it validate and then fail at every execution."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["trash"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="allow_trash_without_backup"):
        load_config(path)


def test_trash_before_backup_in_action_list_rejected(tmp_path: Path) -> None:
    """Order matters: a 'backup' listed *after* 'trash' never satisfies
    the requirement, since actions run strictly in the written order."""
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["trash", "backup"]
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError, match="allow_trash_without_backup"):
        load_config(path)


def test_trash_without_backup_allowed_with_explicit_opt_out(tmp_path: Path) -> None:
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["trash"]
    data["tasks"]["inbox-cleanup"]["rules"][0]["allow_trash_without_backup"] = True
    path = write_config(tmp_path / "cfg.yaml", data)
    load_config(path)  # should not raise


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


# --- Top-level `when:` shapes (spec §7.2) --------------------------------


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


# --- Nesting depth (spec §7.2) ------------------------------------------


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


# --- Value grammars (contracts §3) --------------------------------------


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


# --- has-attachment (spec §7.1; contracts §3) -----------------------------


def test_has_attachment_true_boolean_accepted() -> None:
    tree = parse_condition_tree({"has-attachment": True})
    assert isinstance(tree, rules.HasAttachment)


@pytest.mark.parametrize("value", [False, "true", 1, "True", None, "yes"])
def test_has_attachment_rejects_anything_but_literal_true(value: object) -> None:
    with pytest.raises(ConfigError):
        parse_condition_tree({"has-attachment": value})


# --- auth-result (spec §7.1; contracts §3) --------------------------------


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


def test_batch_size_out_of_range_rejected(tmp_path: Path) -> None:
    data = make_config_dict()
    data["models"]["local"]["batch_size"] = 30
    path = write_config(tmp_path / "cfg.yaml", data)
    with pytest.raises(ConfigError):
        load_config(path)


# --- Derived fetch-header list (spec §4.1) --------------------------------


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
    assert has_deterministic_atom(parse_condition_tree({"older-than": "1d"}))
    assert not has_deterministic_atom(parse_condition_tree({"llm": "x"}))
    assert has_deterministic_atom(
        parse_condition_tree({"all": [{"llm": "x"}, {"older-than": "1d"}]})
    )


# --- File permission/ownership checks (spec §12; acceptance test 15) -----


def test_acceptance_15_world_readable_config_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    path.chmod(0o644)
    with pytest.raises(ConfigFilePermissionError):
        load_config(path)


def test_group_readable_config_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    path.chmod(0o640)
    with pytest.raises(ConfigFilePermissionError):
        check_file_permissions(path)


def test_owner_only_config_accepted(tmp_path: Path) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    path.chmod(0o600)
    check_file_permissions(path)  # should not raise


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
