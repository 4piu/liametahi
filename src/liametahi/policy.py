"""Guardrails, winner-takes-all rule selection, and action resolution.

Pure, no I/O -- this module is a primary property-testing target
alongside `rules.py`. It never touches the mailbox, the model, or
SQLite; it only turns "which rules matched this candidate" into "which
single rule wins, and what does its action list concretely mean."

A rule has no `id` and no `priority` any more: a
matching rule's rank is simply its position in `task.rules` -- the
first-listed match wins outright, not merely a tiebreaker under a
separate priority number.

Guardrails enforced here in code, not just by calling convention, because
the specification is explicit that a rule can never override them:

- A `protected` candidate never receives an action, no matter what
  matched. `decide()` takes `protected` as an explicit argument and
  short-circuits before looking at any match, so a caller cannot
  accidentally launder a protected candidate through rule matching.
- `resolve_actions` always sets `requires_prior_backup=False` --
  backup-before-trash has been removed entirely, so there is no more
  `allow_trash_without_backup` config field to read; the field
  is kept on `ResolvedAction` rather than removed so the execute phase's
  check remains in place as a defensive backstop, not because anything
  can set it `True` any more.
- `task:<id>` resolves to a non-remote-mutation
  `ResolvedAction` whose `destination` is the target task id -- never an
  IMAP mutation, so it never contends for the "one remote mutation per
  action list" slot (already enforced at config load).

The run-wide action cap (`max_actions`) is the third guardrail
the specification names, but it is inherently a cross-candidate,
stateful concern (how many messages this run has already mutated), so
it is enforced by the execute phase, not here. It is opt-in: a task with
no configured cap runs uncapped.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from liametahi.config import RuleConfig

ActionKind = Literal["backup", "trash", "move_to", "label", "task"]
DecisionStatus = Literal["protected", "no_match", "matched"]


def rule_label(index: int, total: int) -> str:
    """A rule has no name of its own, so reports
    identify it by a stable positional reference instead -- `index` is
    0-based, `total` is `len(task.rules)`."""
    return f"rule #{index + 1} of {total}"


@dataclass(frozen=True, slots=True)
class MatchedRule:
    """One rule that fully matched a candidate: the deterministic tree
    resolved to TRUE, or every
    still-`UNKNOWN` atom was resolved to TRUE by a validated processor
    answer.
    """

    rule_index: int  # position within task.rules; also the tie-break order
    label: str  # stable rendering for reports


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    """One action from the winning rule's action list, expanded from its
    config token into what the execute phase needs to run it."""

    action: str  # original config token: "backup", "trash", "move_to:X", "label:X"
    kind: ActionKind
    destination: str | None  # mailbox for trash/move_to, keyword for label
    requires_prior_backup: bool  # only ever True for kind == "trash"
    is_remote_mutation: bool  # True for trash / move_to / label


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    status: DecisionStatus
    winning_rule: str | None  # rendered label, not an id
    shadowed: tuple[str, ...]  # other matched rules' labels, shadowed by the winner
    actions: tuple[ResolvedAction, ...]


class PolicyConfigError(Exception):
    """A resolved rule requires configuration that isn't present (for
    example `trash` with no account `trash_mailbox`). Config-load-time
    validation should make this unreachable in
    practice; this exists as a defensive backstop, not a normal
    control-flow path."""


def select_winner(matches: Sequence[MatchedRule]) -> MatchedRule | None:
    """Matching rules are ordered by plain config order -- the
    first-listed match wins outright, with no separate priority number.
    Returns None if `matches` is empty."""
    if not matches:
        return None
    return min(matches, key=lambda m: m.rule_index)


def resolve_actions(
    rule: RuleConfig, *, trash_mailbox: str | None
) -> tuple[ResolvedAction, ...]:
    """Expand a rule's validated action-token list
    into concrete `ResolvedAction`s. `config.py` has already validated
    every token at load time (unknown actions, malformed
    `move_to:`/`label:`/`task:`, more than one remote mutation), so any
    token this function can't recognise indicates a `config.py`
    validation gap, not bad user input -- it is a defensive
    `AssertionError`, matching the `_assert_never` pattern already used
    in `rules.py`.
    """
    resolved: list[ResolvedAction] = []
    for action in rule.actions:
        if action == "backup":
            resolved.append(
                ResolvedAction(
                    action=action,
                    kind="backup",
                    destination=None,
                    requires_prior_backup=False,
                    is_remote_mutation=False,
                )
            )
        elif action == "trash":
            if not trash_mailbox:
                raise PolicyConfigError(
                    "a rule has a 'trash' action but no account "
                    "trash_mailbox is configured; config.py's cross-reference "
                    "check should have rejected this at load time"
                )
            resolved.append(
                ResolvedAction(
                    action=action,
                    kind="trash",
                    destination=trash_mailbox,
                    # Backup-before-trash is no
                    # longer mandatory -- there is no config field left
                    # that could ever make this True.
                    requires_prior_backup=False,
                    is_remote_mutation=True,
                )
            )
        elif action.startswith("move_to:"):
            resolved.append(
                ResolvedAction(
                    action=action,
                    kind="move_to",
                    destination=action.removeprefix("move_to:"),
                    requires_prior_backup=False,
                    is_remote_mutation=True,
                )
            )
        elif action.startswith("label:"):
            resolved.append(
                ResolvedAction(
                    action=action,
                    kind="label",
                    destination=action.removeprefix("label:"),
                    requires_prior_backup=False,
                    is_remote_mutation=True,
                )
            )
        elif action.startswith("task:"):
            resolved.append(
                ResolvedAction(
                    action=action,
                    kind="task",
                    destination=action.removeprefix("task:"),
                    requires_prior_backup=False,
                    is_remote_mutation=False,
                )
            )
        else:  # pragma: no cover - config.py rejects this at load time
            raise AssertionError(f"unreachable action token: {action!r}")
    return tuple(resolved)


def decide(
    *,
    protected: bool,
    matches: Sequence[MatchedRule],
    rules_by_index: Sequence[RuleConfig],
    trash_mailbox: str | None,
) -> PolicyDecision:
    """Resolve the winning rule (if any) into permitted actions.

    A protected candidate always resolves to `status="protected"` with
    no actions, regardless of `matches` -- this is the guardrail that a
    rule can never override protected senders, flags, or unread state.
    Callers are expected to never call `decide()`
    for a protected candidate's rule matches in the first place
    (protection is checked before any rule reaches evaluation); this is
    a second, independent check so the
    guarantee does not rest solely on call-site discipline.
    """
    if protected:
        return PolicyDecision(
            status="protected",
            winning_rule=None,
            shadowed=(),
            actions=(),
        )
    winner = select_winner(matches)
    if winner is None:
        return PolicyDecision(
            status="no_match",
            winning_rule=None,
            shadowed=(),
            actions=(),
        )
    shadowed = tuple(m.label for m in matches if m.rule_index != winner.rule_index)
    rule = rules_by_index[winner.rule_index]
    actions = resolve_actions(rule, trash_mailbox=trash_mailbox)
    return PolicyDecision(
        status="matched",
        winning_rule=winner.label,
        shadowed=shadowed,
        actions=actions,
    )
