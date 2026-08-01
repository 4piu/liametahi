"""Guardrails, winner-takes-all rule selection, and action resolution
(spec section 7.4; contracts section 5 work-unit table).

Pure, no I/O -- this module is a primary property-testing target
alongside `rules.py`. It never touches the mailbox, the model, or
SQLite; it only turns "which rules matched this candidate" into "which
single rule wins, and what does its action list concretely mean."

Two guardrails are enforced here in code, not just by calling
convention, because the specification is explicit that a rule can never
override them:

- A `protected` candidate never receives an action, no matter what
  matched. `decide()` takes `protected` as an explicit argument and
  short-circuits before looking at any match, so a caller cannot
  accidentally launder a protected candidate through rule matching.
- `trash` never becomes a bare mutation: `resolve_actions` always
  records whether an action requires a prior successful backup
  (`ResolvedAction.requires_prior_backup`), computed from the rule's own
  `allow_trash_without_backup`. The actual backup-before-mutate
  enforcement is a runtime concern (it depends on whether a backup
  action actually succeeded) and lives in the execute phase, but the
  *requirement* is decided here, once, from configuration -- the execute
  phase never re-derives it from a rule.

The run-wide action cap (`max_actions`) is the third guardrail
the specification names, but it is inherently a cross-candidate,
stateful concern (how many messages this run has already mutated), so
it is enforced by the execute phase, not here. It is opt-in: a task with
no configured cap runs uncapped (spec §6).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from liametahi.config import RuleConfig

ActionKind = Literal["backup", "trash", "move_to", "label"]
DecisionStatus = Literal["protected", "no_match", "matched"]


@dataclass(frozen=True, slots=True)
class MatchedRule:
    """One rule that fully matched a candidate (spec section 4.2 step 8's
    input): the deterministic tree resolved to TRUE, or an `llm` atom
    was resolved to TRUE by a validated classification (spec §5.3: the
    model's output is yes/no/unsure, not a score, so there is no
    confidence to carry here).
    """

    rule_id: str
    priority: int
    config_order: int  # index within task.rules; smaller = earlier = wins ties


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
    winning_rule: str | None
    shadowed: tuple[str, ...]  # other matched rule ids, all shadowed by the winner
    actions: tuple[ResolvedAction, ...]


class PolicyConfigError(Exception):
    """A resolved rule requires configuration that isn't present (for
    example `trash` with no account `trash_mailbox`). Config-load-time
    validation (spec section 6) should make this unreachable in
    practice; this exists as a defensive backstop, not a normal
    control-flow path."""


def select_winner(matches: Sequence[MatchedRule]) -> MatchedRule | None:
    """spec section 7.4: matching rules are ordered by `(priority
    descending, config order)`; the first is the winner. Returns None
    if `matches` is empty."""
    if not matches:
        return None
    return min(matches, key=lambda m: (-m.priority, m.config_order))


def resolve_actions(
    rule: RuleConfig, *, trash_mailbox: str | None
) -> tuple[ResolvedAction, ...]:
    """Expand a rule's validated action-token list (spec section 7.4)
    into concrete `ResolvedAction`s. `config.py` has already validated
    every token at load time (unknown actions, malformed
    `move_to:`/`label:`, more than one remote mutation), so any token
    this function can't recognise indicates a `config.py` validation
    gap, not bad user input -- it is a defensive `AssertionError`,
    matching the `_assert_never` pattern already used in `rules.py`.
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
                    f"rule {rule.id!r} has a 'trash' action but no account "
                    "trash_mailbox is configured; config.py's cross-reference "
                    "check (spec section 6) should have rejected this at load time"
                )
            resolved.append(
                ResolvedAction(
                    action=action,
                    kind="trash",
                    destination=trash_mailbox,
                    requires_prior_backup=not rule.allow_trash_without_backup,
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
        else:  # pragma: no cover - config.py rejects this at load time
            raise AssertionError(f"unreachable action token: {action!r}")
    return tuple(resolved)


def decide(
    *,
    protected: bool,
    matches: Sequence[MatchedRule],
    rules_by_id: Mapping[str, RuleConfig],
    trash_mailbox: str | None,
) -> PolicyDecision:
    """spec section 4.2 step 8 / section 7.4: resolve the winning rule
    (if any) into permitted actions.

    A protected candidate always resolves to `status="protected"` with
    no actions, regardless of `matches` -- this is the guardrail that a
    rule can never override protected senders, flags, or unread state
    (spec section 7.4). Callers are expected to never call `decide()`
    for a protected candidate's rule matches in the first place
    (protection is checked before any rule reaches evaluation, spec
    section 4.2 step 1); this is a second, independent check so the
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
    shadowed = tuple(m.rule_id for m in matches if m.rule_id != winner.rule_id)
    rule = rules_by_id[winner.rule_id]
    actions = resolve_actions(rule, trash_mailbox=trash_mailbox)
    return PolicyDecision(
        status="matched",
        winning_rule=winner.rule_id,
        shadowed=shadowed,
        actions=actions,
    )
