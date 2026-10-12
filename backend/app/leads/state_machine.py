"""
Lead stage state machine — explicit, backend-enforced transitions.

RULES:
  1. All valid transitions are listed here. Anything not listed is FORBIDDEN.
  2. validate_transition() is called by LeadService before every stage change.
  3. Frontend cannot bypass this — the API only accepts stage changes through
     the transition endpoint, never via a raw PATCH on the stage field.
  4. Some transitions require a minimum role (MANAGER+).
"""

from enum import StrEnum
from typing import NamedTuple

from app.core.dependencies import UserRole
from app.core.exceptions import InvalidTransitionError


class LeadStage(StrEnum):
    NEW = "NEW"
    QUALIFYING = "QUALIFYING"
    QUALIFIED = "QUALIFIED"
    SITE_VISIT_SCHEDULED = "SITE_VISIT_SCHEDULED"
    NEGOTIATION = "NEGOTIATION"
    WON = "WON"
    LOST = "LOST"
    STALLED = "STALLED"
    DISQUALIFIED = "DISQUALIFIED"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"


class AutomationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    HUMAN_TAKEOVER = "HUMAN_TAKEOVER"
    OPTED_OUT = "OPTED_OUT"


class LeadHealth(StrEnum):
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"
    UNKNOWN = "UNKNOWN"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class TransitionRule(NamedTuple):
    to_stage: LeadStage
    min_role: UserRole = UserRole.AGENT  # minimum role required
    system_only: bool = False            # True = only automation/system can trigger


# ── Allowed transitions ───────────────────────────────────────────────────────
# Format: (from_stage, to_stage) -> TransitionRule
LEAD_TRANSITIONS: dict[tuple[LeadStage, LeadStage], TransitionRule] = {
    # Normal forward flow
    (LeadStage.NEW, LeadStage.QUALIFYING): TransitionRule(LeadStage.QUALIFYING),
    (LeadStage.QUALIFYING, LeadStage.QUALIFIED): TransitionRule(LeadStage.QUALIFIED),
    (LeadStage.QUALIFIED, LeadStage.SITE_VISIT_SCHEDULED): TransitionRule(LeadStage.SITE_VISIT_SCHEDULED),
    (LeadStage.SITE_VISIT_SCHEDULED, LeadStage.NEGOTIATION): TransitionRule(LeadStage.NEGOTIATION),
    (LeadStage.NEGOTIATION, LeadStage.WON): TransitionRule(LeadStage.WON),
    (LeadStage.NEGOTIATION, LeadStage.LOST): TransitionRule(LeadStage.LOST),

    # Stalled detection (system-triggered from any active stage)
    (LeadStage.NEW, LeadStage.STALLED): TransitionRule(LeadStage.STALLED, system_only=True),
    (LeadStage.QUALIFYING, LeadStage.STALLED): TransitionRule(LeadStage.STALLED, system_only=True),
    (LeadStage.QUALIFIED, LeadStage.STALLED): TransitionRule(LeadStage.STALLED, system_only=True),
    (LeadStage.SITE_VISIT_SCHEDULED, LeadStage.STALLED): TransitionRule(LeadStage.STALLED, system_only=True),
    (LeadStage.NEGOTIATION, LeadStage.STALLED): TransitionRule(LeadStage.STALLED, system_only=True),

    # Recovery from stalled (MANAGER+)
    (LeadStage.STALLED, LeadStage.QUALIFYING): TransitionRule(LeadStage.QUALIFYING, min_role=UserRole.MANAGER),
    (LeadStage.STALLED, LeadStage.QUALIFIED): TransitionRule(LeadStage.QUALIFIED, min_role=UserRole.MANAGER),

    # Disqualification (MANAGER+, from any non-terminal stage)
    (LeadStage.NEW, LeadStage.DISQUALIFIED): TransitionRule(LeadStage.DISQUALIFIED, min_role=UserRole.MANAGER),
    (LeadStage.QUALIFYING, LeadStage.DISQUALIFIED): TransitionRule(LeadStage.DISQUALIFIED, min_role=UserRole.MANAGER),
    (LeadStage.QUALIFIED, LeadStage.DISQUALIFIED): TransitionRule(LeadStage.DISQUALIFIED, min_role=UserRole.MANAGER),
    (LeadStage.STALLED, LeadStage.DISQUALIFIED): TransitionRule(LeadStage.DISQUALIFIED, min_role=UserRole.MANAGER),

    # Opt-out / do-not-contact (any stage → system or agent)
    (LeadStage.NEW, LeadStage.DO_NOT_CONTACT): TransitionRule(LeadStage.DO_NOT_CONTACT),
    (LeadStage.QUALIFYING, LeadStage.DO_NOT_CONTACT): TransitionRule(LeadStage.DO_NOT_CONTACT),
    (LeadStage.QUALIFIED, LeadStage.DO_NOT_CONTACT): TransitionRule(LeadStage.DO_NOT_CONTACT),
    (LeadStage.SITE_VISIT_SCHEDULED, LeadStage.DO_NOT_CONTACT): TransitionRule(LeadStage.DO_NOT_CONTACT),
    (LeadStage.NEGOTIATION, LeadStage.DO_NOT_CONTACT): TransitionRule(LeadStage.DO_NOT_CONTACT),
    (LeadStage.STALLED, LeadStage.DO_NOT_CONTACT): TransitionRule(LeadStage.DO_NOT_CONTACT),

    # MANAGER backward corrections (e.g., visit cancelled → back to QUALIFIED)
    (LeadStage.SITE_VISIT_SCHEDULED, LeadStage.QUALIFIED): TransitionRule(LeadStage.QUALIFIED, min_role=UserRole.MANAGER),
    (LeadStage.NEGOTIATION, LeadStage.QUALIFIED): TransitionRule(LeadStage.QUALIFIED, min_role=UserRole.MANAGER),
    (LeadStage.NEGOTIATION, LeadStage.SITE_VISIT_SCHEDULED): TransitionRule(LeadStage.SITE_VISIT_SCHEDULED, min_role=UserRole.MANAGER),

    # Lost → re-qualifying (MANAGER+, e.g., lead came back)
    (LeadStage.LOST, LeadStage.QUALIFYING): TransitionRule(LeadStage.QUALIFYING, min_role=UserRole.MANAGER),
}

# Stages from which no further transitions are possible (except by SUPER_ADMIN)
TERMINAL_STAGES: frozenset[LeadStage] = frozenset({
    LeadStage.WON,
    LeadStage.DO_NOT_CONTACT,
    LeadStage.DISQUALIFIED,
})


def validate_transition(
    from_stage: LeadStage,
    to_stage: LeadStage,
    actor_role: UserRole,
    is_system: bool = False,
) -> None:
    """
    Validate a requested stage transition.
    Raises InvalidTransitionError if the transition is not allowed.
    Call this before persisting any stage change.

    Args:
        from_stage: Current lead stage.
        to_stage: Requested new stage.
        actor_role: Role of the user requesting the change.
        is_system: True if triggered by automation (skips role check for system_only transitions).
    """
    if from_stage == to_stage:
        return  # No-op is always allowed

    rule = LEAD_TRANSITIONS.get((from_stage, to_stage))

    if rule is None:
        raise InvalidTransitionError(
            f"Transition from {from_stage!r} to {to_stage!r} is not permitted.",
            detail=f"Allowed transitions from {from_stage}: "
                   f"{[t for (f, t) in LEAD_TRANSITIONS if f == from_stage]}",
        )

    if rule.system_only and not is_system:
        raise InvalidTransitionError(
            f"Transition to {to_stage!r} can only be triggered by the system.",
        )

    # Check role unless it's a system action
    if not is_system:
        role_levels = ["AGENT", "MANAGER", "AGENCY_ADMIN", "SUPER_ADMIN"]
        actor_level = role_levels.index(actor_role) if actor_role in role_levels else -1
        required_level = role_levels.index(rule.min_role) if rule.min_role in role_levels else 0
        if actor_level < required_level:
            raise InvalidTransitionError(
                f"Transition from {from_stage!r} to {to_stage!r} requires "
                f"{rule.min_role} role or higher."
            )


def get_allowed_transitions(stage: LeadStage, actor_role: UserRole) -> list[LeadStage]:
    """Return all stages the given role can transition to from current stage."""
    role_levels = ["AGENT", "MANAGER", "AGENCY_ADMIN", "SUPER_ADMIN"]
    actor_level = role_levels.index(actor_role) if actor_role in role_levels else -1

    allowed = []
    for (from_s, to_s), rule in LEAD_TRANSITIONS.items():
        if from_s != stage:
            continue
        if rule.system_only:
            continue
        required_level = role_levels.index(rule.min_role) if rule.min_role in role_levels else 0
        if actor_level >= required_level:
            allowed.append(to_s)
    return allowed
