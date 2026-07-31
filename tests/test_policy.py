"""Tests for `liametahi.policy` (spec section 7.4)."""

from liametahi import policy
from liametahi.config import RuleConfig


def _rule(
    rule_id: str,
    actions: list[str],
    *,
    allow_trash_without_backup: bool = False,
) -> RuleConfig:
    return RuleConfig.model_validate(
        {
            "id": rule_id,
            "when": {"older-than": "1d"},
            "actions": actions,
            "allow_trash_without_backup": allow_trash_without_backup,
        }
    )


# --- select_winner / decide: winner-takes-all (spec section 7.4) --------


def test_select_winner_empty_is_none() -> None:
    assert policy.select_winner([]) is None


def test_select_winner_highest_priority_wins() -> None:
    low = policy.MatchedRule("low", priority=10, config_order=0)
    high = policy.MatchedRule("high", priority=100, config_order=1)
    assert policy.select_winner([low, high]) is high


def test_select_winner_ties_broken_by_config_order() -> None:
    first = policy.MatchedRule("first", priority=50, config_order=0)
    second = policy.MatchedRule("second", priority=50, config_order=1)
    assert policy.select_winner([second, first]) is first


def test_decide_no_matches_is_no_match() -> None:
    decision = policy.decide(
        protected=False, matches=[], rules_by_id={}, trash_mailbox="Trash"
    )
    assert decision.status == "no_match"
    assert decision.winning_rule is None
    assert decision.actions == ()


def test_decide_protected_short_circuits_even_with_matches() -> None:
    rule = _rule("r1", ["backup", "trash"])
    matched = policy.MatchedRule("r1", priority=100, config_order=0)
    decision = policy.decide(
        protected=True,
        matches=[matched],
        rules_by_id={"r1": rule},
        trash_mailbox="Trash",
    )
    assert decision.status == "protected"
    assert decision.winning_rule is None
    assert decision.actions == ()


def test_decide_winner_takes_all_reports_shadowed() -> None:
    winner_rule = _rule("winner", ["move_to:Archive"])
    loser_rule = _rule("loser", ["backup", "trash"])
    matches = [
        policy.MatchedRule("loser", priority=10, config_order=1),
        policy.MatchedRule("winner", priority=100, config_order=0),
    ]
    decision = policy.decide(
        protected=False,
        matches=matches,
        rules_by_id={"winner": winner_rule, "loser": loser_rule},
        trash_mailbox="Trash",
    )
    assert decision.status == "matched"
    assert decision.winning_rule == "winner"
    assert decision.shadowed == ("loser",)
    # The loser's actions (backup+trash) never resolve or run: only the
    # winner's full action list is present.
    assert [a.action for a in decision.actions] == ["move_to:Archive"]


# --- resolve_actions ------------------------------------------------------


def test_resolve_actions_backup_is_not_a_remote_mutation() -> None:
    rule = _rule("r", ["backup"])
    (action,) = policy.resolve_actions(rule, trash_mailbox=None)
    assert action.kind == "backup"
    assert action.is_remote_mutation is False
    assert action.requires_prior_backup is False


def test_resolve_actions_trash_requires_prior_backup_by_default() -> None:
    rule = _rule("r", ["backup", "trash"])
    backup_action, trash_action = policy.resolve_actions(rule, trash_mailbox="Trash")
    assert backup_action.kind == "backup"
    assert trash_action.kind == "trash"
    assert trash_action.destination == "Trash"
    assert trash_action.requires_prior_backup is True
    assert trash_action.is_remote_mutation is True


def test_resolve_actions_trash_allow_without_backup() -> None:
    rule = _rule("r", ["trash"], allow_trash_without_backup=True)
    (trash_action,) = policy.resolve_actions(rule, trash_mailbox="Trash")
    assert trash_action.requires_prior_backup is False


def test_resolve_actions_trash_without_trash_mailbox_raises() -> None:
    rule = _rule("r", ["trash"], allow_trash_without_backup=True)
    try:
        policy.resolve_actions(rule, trash_mailbox=None)
    except policy.PolicyConfigError:
        pass
    else:
        raise AssertionError("expected PolicyConfigError")


def test_resolve_actions_move_to() -> None:
    rule = _rule("r", ["move_to:Archive"])
    (action,) = policy.resolve_actions(rule, trash_mailbox=None)
    assert action.kind == "move_to"
    assert action.destination == "Archive"
    assert action.requires_prior_backup is False
    assert action.is_remote_mutation is True


def test_resolve_actions_label() -> None:
    rule = _rule("r", ["label:Important"])
    (action,) = policy.resolve_actions(rule, trash_mailbox=None)
    assert action.kind == "label"
    assert action.destination == "Important"
    assert action.is_remote_mutation is True


# --- property: winner-takes-all is total and deterministic --------------


def test_winner_selection_is_deterministic_across_orderings() -> None:
    matches = [
        policy.MatchedRule("a", priority=5, config_order=2),
        policy.MatchedRule("b", priority=5, config_order=0),
        policy.MatchedRule("c", priority=9, config_order=1),
    ]
    import itertools

    winners = set()
    for perm in itertools.permutations(matches):
        winner = policy.select_winner(list(perm))
        assert winner is not None
        winners.add(winner.rule_id)
    assert winners == {"c"}  # highest priority always wins regardless of input order


def test_shadowed_never_includes_the_winner() -> None:
    rule = _rule("r1", ["move_to:Archive"])
    matches = [policy.MatchedRule("r1", priority=1, config_order=0)]
    decision = policy.decide(
        protected=False,
        matches=matches,
        rules_by_id={"r1": rule},
        trash_mailbox=None,
    )
    assert decision.winning_rule not in decision.shadowed
