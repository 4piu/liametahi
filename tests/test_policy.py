"""Tests for `liametahi.policy`."""

from liametahi import policy
from liametahi.config import RuleConfig


def _rule(actions: list[str]) -> RuleConfig:
    return RuleConfig.model_validate({"when": {"older-than": "1d"}, "actions": actions})


# --- select_winner / decide: winner-takes-all by config order -------------


def test_select_winner_empty_is_none() -> None:
    assert policy.select_winner([]) is None


def test_select_winner_first_listed_wins() -> None:
    """A matching rule's rank is simply its
    position in `task.rules` -- there is no separate priority number any
    more."""
    later = policy.MatchedRule(rule_index=1, label="rule #2 of 2")
    earlier = policy.MatchedRule(rule_index=0, label="rule #1 of 2")
    assert policy.select_winner([later, earlier]) is earlier


def test_select_winner_order_of_input_list_does_not_matter() -> None:
    first = policy.MatchedRule(rule_index=0, label="rule #1 of 2")
    second = policy.MatchedRule(rule_index=1, label="rule #2 of 2")
    assert policy.select_winner([second, first]) is first
    assert policy.select_winner([first, second]) is first


def test_decide_no_matches_is_no_match() -> None:
    decision = policy.decide(
        protected=False, matches=[], rules_by_index=[], trash_mailbox="Trash"
    )
    assert decision.status == "no_match"
    assert decision.winning_rule is None
    assert decision.actions == ()


def test_decide_protected_short_circuits_even_with_matches() -> None:
    rule = _rule(["backup", "trash"])
    matched = policy.MatchedRule(rule_index=0, label="rule #1 of 1")
    decision = policy.decide(
        protected=True,
        matches=[matched],
        rules_by_index=[rule],
        trash_mailbox="Trash",
    )
    assert decision.status == "protected"
    assert decision.winning_rule is None
    assert decision.actions == ()


def test_decide_winner_takes_all_reports_shadowed() -> None:
    winner_rule = _rule(["move_to:Archive"])
    loser_rule = _rule(["backup", "trash"])
    rules_by_index = [winner_rule, loser_rule]
    matches = [
        policy.MatchedRule(rule_index=1, label="rule #2 of 2"),
        policy.MatchedRule(rule_index=0, label="rule #1 of 2"),
    ]
    decision = policy.decide(
        protected=False,
        matches=matches,
        rules_by_index=rules_by_index,
        trash_mailbox="Trash",
    )
    assert decision.status == "matched"
    assert decision.winning_rule == "rule #1 of 2"
    assert decision.shadowed == ("rule #2 of 2",)
    # The loser's actions (backup+trash) never resolve or run: only the
    # winner's full action list is present.
    assert [a.action for a in decision.actions] == ["move_to:Archive"]


# --- resolve_actions --------------------------------------------------------


def test_resolve_actions_backup_is_not_a_remote_mutation() -> None:
    rule = _rule(["backup"])
    (action,) = policy.resolve_actions(rule, trash_mailbox=None)
    assert action.kind == "backup"
    assert action.is_remote_mutation is False
    assert action.requires_prior_backup is False


def test_resolve_actions_trash_never_requires_prior_backup() -> None:
    """Backup-before-trash is no longer mandatory
    -- `requires_prior_backup` is always False now, with or without a
    preceding `backup` action."""
    rule = _rule(["backup", "trash"])
    backup_action, trash_action = policy.resolve_actions(rule, trash_mailbox="Trash")
    assert backup_action.kind == "backup"
    assert trash_action.kind == "trash"
    assert trash_action.destination == "Trash"
    assert trash_action.requires_prior_backup is False
    assert trash_action.is_remote_mutation is True

    bare_rule = _rule(["trash"])
    (bare_trash_action,) = policy.resolve_actions(bare_rule, trash_mailbox="Trash")
    assert bare_trash_action.requires_prior_backup is False


def test_resolve_actions_trash_without_trash_mailbox_raises() -> None:
    rule = _rule(["trash"])
    try:
        policy.resolve_actions(rule, trash_mailbox=None)
    except policy.PolicyConfigError:
        pass
    else:
        raise AssertionError("expected PolicyConfigError")


def test_resolve_actions_move_to() -> None:
    rule = _rule(["move_to:Archive"])
    (action,) = policy.resolve_actions(rule, trash_mailbox=None)
    assert action.kind == "move_to"
    assert action.destination == "Archive"
    assert action.requires_prior_backup is False
    assert action.is_remote_mutation is True


def test_resolve_actions_label() -> None:
    rule = _rule(["label:Important"])
    (action,) = policy.resolve_actions(rule, trash_mailbox=None)
    assert action.kind == "label"
    assert action.destination == "Important"
    assert action.is_remote_mutation is True


def test_resolve_actions_task_is_not_a_remote_mutation() -> None:
    """`task:<id>` is local-only bookkeeping,
    never an IMAP mutation."""
    rule = _rule(["task:inbox-review"])
    (action,) = policy.resolve_actions(rule, trash_mailbox=None)
    assert action.kind == "task"
    assert action.destination == "inbox-review"
    assert action.is_remote_mutation is False
    assert action.requires_prior_backup is False


# --- property: winner-takes-all is total and deterministic --------------


def test_winner_selection_is_deterministic_across_orderings() -> None:
    matches = [
        policy.MatchedRule(rule_index=2, label="rule #3 of 3"),
        policy.MatchedRule(rule_index=0, label="rule #1 of 3"),
        policy.MatchedRule(rule_index=1, label="rule #2 of 3"),
    ]
    import itertools

    winners = set()
    for perm in itertools.permutations(matches):
        winner = policy.select_winner(list(perm))
        assert winner is not None
        winners.add(winner.rule_index)
    assert winners == {0}  # first-listed rule always wins regardless of input order


def test_shadowed_never_includes_the_winner() -> None:
    rule = _rule(["move_to:Archive"])
    matches = [policy.MatchedRule(rule_index=0, label="rule #1 of 1")]
    decision = policy.decide(
        protected=False,
        matches=matches,
        rules_by_index=[rule],
        trash_mailbox=None,
    )
    assert decision.winning_rule not in decision.shadowed


def test_rule_label_is_one_indexed() -> None:
    assert policy.rule_label(0, 3) == "rule #1 of 3"
    assert policy.rule_label(2, 3) == "rule #3 of 3"
