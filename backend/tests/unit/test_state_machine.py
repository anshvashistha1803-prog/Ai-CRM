"""
Unit tests for the lead state machine.
These are pure logic tests — no DB, no HTTP, no fixtures needed.
"""

import pytest

from app.core.dependencies import UserRole
from app.core.exceptions import InvalidTransitionError
from app.leads.state_machine import (
    TERMINAL_STAGES,
    LeadStage,
    get_allowed_transitions,
    validate_transition,
)


class TestValidTransitions:
    """Forward flow transitions that must succeed."""

    def test_new_to_qualifying_agent(self):
        validate_transition(LeadStage.NEW, LeadStage.QUALIFYING, UserRole.AGENT)

    def test_qualifying_to_qualified_agent(self):
        validate_transition(LeadStage.QUALIFYING, LeadStage.QUALIFIED, UserRole.AGENT)

    def test_qualified_to_site_visit_agent(self):
        validate_transition(LeadStage.QUALIFIED, LeadStage.SITE_VISIT_SCHEDULED, UserRole.AGENT)

    def test_site_visit_to_negotiation_agent(self):
        validate_transition(LeadStage.SITE_VISIT_SCHEDULED, LeadStage.NEGOTIATION, UserRole.AGENT)

    def test_negotiation_to_won_agent(self):
        validate_transition(LeadStage.NEGOTIATION, LeadStage.WON, UserRole.AGENT)

    def test_negotiation_to_lost_agent(self):
        validate_transition(LeadStage.NEGOTIATION, LeadStage.LOST, UserRole.AGENT)

    def test_do_not_contact_from_any_stage_agent(self):
        for stage in [
            LeadStage.NEW, LeadStage.QUALIFYING, LeadStage.QUALIFIED,
            LeadStage.SITE_VISIT_SCHEDULED, LeadStage.NEGOTIATION, LeadStage.STALLED,
        ]:
            validate_transition(stage, LeadStage.DO_NOT_CONTACT, UserRole.AGENT)

    def test_same_stage_noop(self):
        # No-op transition (stage unchanged) is always allowed
        validate_transition(LeadStage.QUALIFIED, LeadStage.QUALIFIED, UserRole.AGENT)


class TestInvalidTransitions:
    """Transitions that must raise InvalidTransitionError."""

    def test_skip_qualifying_stage(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition(LeadStage.NEW, LeadStage.QUALIFIED, UserRole.AGENT)

    def test_skip_to_won_directly(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition(LeadStage.NEW, LeadStage.WON, UserRole.AGENT)

    def test_backwards_without_manager(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition(
                LeadStage.SITE_VISIT_SCHEDULED,
                LeadStage.QUALIFIED,
                UserRole.AGENT,  # Agent cannot go backward
            )

    def test_manager_can_go_backwards(self):
        # Should NOT raise — manager is allowed
        validate_transition(
            LeadStage.SITE_VISIT_SCHEDULED,
            LeadStage.QUALIFIED,
            UserRole.MANAGER,
        )

    def test_disqualify_requires_manager(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition(LeadStage.QUALIFYING, LeadStage.DISQUALIFIED, UserRole.AGENT)

    def test_manager_can_disqualify(self):
        validate_transition(LeadStage.QUALIFYING, LeadStage.DISQUALIFIED, UserRole.MANAGER)

    def test_stalled_is_system_only(self):
        with pytest.raises(InvalidTransitionError):
            validate_transition(
                LeadStage.QUALIFYING,
                LeadStage.STALLED,
                UserRole.MANAGER,
                is_system=False,
            )

    def test_system_can_mark_stalled(self):
        validate_transition(
            LeadStage.QUALIFYING,
            LeadStage.STALLED,
            UserRole.AGENT,
            is_system=True,  # system-triggered
        )

    def test_won_is_terminal(self):
        """No transitions FROM WON for agents or managers."""
        for to_stage in LeadStage:
            if to_stage == LeadStage.WON:
                continue
            with pytest.raises(InvalidTransitionError):
                validate_transition(LeadStage.WON, to_stage, UserRole.MANAGER)


class TestTerminalStages:
    def test_terminal_set_is_correct(self):
        assert LeadStage.WON in TERMINAL_STAGES
        assert LeadStage.DO_NOT_CONTACT in TERMINAL_STAGES
        assert LeadStage.DISQUALIFIED in TERMINAL_STAGES
        assert LeadStage.LOST not in TERMINAL_STAGES  # LOST is recoverable


class TestGetAllowedTransitions:
    def test_agent_from_new(self):
        allowed = get_allowed_transitions(LeadStage.NEW, UserRole.AGENT)
        assert LeadStage.QUALIFYING in allowed
        assert LeadStage.DO_NOT_CONTACT in allowed
        assert LeadStage.STALLED not in allowed  # system only

    def test_manager_gets_more_transitions(self):
        agent_allowed = get_allowed_transitions(LeadStage.QUALIFYING, UserRole.AGENT)
        manager_allowed = get_allowed_transitions(LeadStage.QUALIFYING, UserRole.MANAGER)
        # Manager can do everything agent can, plus more
        assert set(agent_allowed).issubset(set(manager_allowed))
        assert LeadStage.DISQUALIFIED in manager_allowed
        assert LeadStage.DISQUALIFIED not in agent_allowed
