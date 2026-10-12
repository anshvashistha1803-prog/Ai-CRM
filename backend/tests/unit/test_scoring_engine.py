"""
Unit tests for HAVN Realty 6-component scoring model in scoring/rules.py and scoring/engine.py:
- Intent and commitment: 25 points
- Budget clarity and readiness: 20 points
- Decision timeline: 20 points
- Requirement specificity: 15 points
- Engagement: 10 points
- Location and property-type clarity: 10 points
- Clamping between 0 and 100
- Health classification (HOT: 70-100, WARM: 40-69, COLD: 10-39, INSUFFICIENT_DATA: 0-9)
- Non-punitive missing answers (no negative penalties)
- Unresolved questions tracking
"""

from types import SimpleNamespace

from app.scoring.engine import ScoringEngine
from app.scoring.rules import (
    rule_budget_clarity,
    rule_decision_timeline,
    rule_engagement,
    rule_intent_and_commitment,
    rule_location_and_property_type,
    rule_requirement_specificity,
)


class TestScoringRules:
    def test_intent_rule_primary_residence_sole_decision_maker(self):
        lead = SimpleNamespace(purpose="PRIMARY RESIDENCE", decision_maker="SOLE", intent="BUY")
        res = rule_intent_and_commitment(lead)
        assert res.points == 25
        assert res.max_points == 25
        assert "Primary residence" in res.reason

    def test_intent_rule_second_home(self):
        lead = SimpleNamespace(purpose="SECOND HOME", decision_maker="FAMILY", intent="BUY")
        res = rule_intent_and_commitment(lead)
        assert res.points >= 20
        assert "Second home" in res.reason

    def test_intent_rule_empty(self):
        lead = SimpleNamespace(purpose=None, decision_maker=None, intent=None)
        res = rule_intent_and_commitment(lead)
        assert res.points == 0
        assert res.max_points == 25

    def test_budget_rule_min_max(self):
        lead = SimpleNamespace(budget_min=10000000, budget_max=15000000, budget_flexibility="FLEXIBLE")
        res = rule_budget_clarity(lead)
        assert res.points == 20
        assert res.max_points == 20

    def test_budget_rule_max_only(self):
        lead = SimpleNamespace(budget_min=None, budget_max=20000000, budget_flexibility=None)
        res = rule_budget_clarity(lead)
        assert res.points == 14

    def test_budget_rule_empty(self):
        lead = SimpleNamespace(budget_min=None, budget_max=None, budget_flexibility=None)
        res = rule_budget_clarity(lead)
        assert res.points == 0

    def test_timeline_immediate(self):
        lead = SimpleNamespace(move_in_timeline="IMMEDIATE", delay_factors="NONE")
        res = rule_decision_timeline(lead)
        assert res.points == 20
        assert res.max_points == 20

    def test_timeline_1_to_3_months(self):
        lead = SimpleNamespace(move_in_timeline="1-3 months", delay_factors=None)
        res = rule_decision_timeline(lead)
        assert res.points == 12

    def test_timeline_empty(self):
        lead = SimpleNamespace(move_in_timeline=None, delay_factors=None)
        res = rule_decision_timeline(lead)
        assert res.points == 0

    def test_requirement_specificity_complete(self):
        answers = {
            "bhk": 4,
            "built_up_area": "4500 sq ft",
            "parking": "3 cars",
            "amenities": "Pool, Clubhouse",
            "users": "Family with 2 kids",
        }
        res = rule_requirement_specificity(None, answers)
        assert res.points == 15
        assert res.max_points == 15

    def test_requirement_specificity_empty(self):
        res = rule_requirement_specificity(SimpleNamespace(bhk=None))
        assert res.points == 0

    def test_engagement_rule_multi_turn(self):
        lead = SimpleNamespace(phone_hash="hash123", raw_message_text="Client: Hi\nClient: Buy\nClient: Villa\nClient: 2 Cr")
        res = rule_engagement(lead, {"bhk": 3, "intent": "BUY", "location": "Dehradun", "budget": "2 Cr", "timeline": "Immediate"})
        assert res.points == 10
        assert res.max_points == 10

    def test_engagement_rule_empty(self):
        lead = SimpleNamespace(phone_hash=None, phone_e164_enc=None, raw_message_text=None)
        res = rule_engagement(lead, {})
        assert res.points == 0

    def test_location_and_property_type_both(self):
        lead = SimpleNamespace(property_type="VILLA", preferred_location="Rajpur Road")
        res = rule_location_and_property_type(lead)
        assert res.points == 10
        assert res.max_points == 10

    def test_location_and_property_type_one_only(self):
        lead = SimpleNamespace(property_type="FLAT", preferred_location=None)
        res = rule_location_and_property_type(lead)
        assert res.points == 5

    def test_location_and_property_type_empty(self):
        lead = SimpleNamespace(property_type=None, preferred_location=None)
        res = rule_location_and_property_type(lead)
        assert res.points == 0


class TestScoringEngine:
    def test_hot_lead_evaluation(self):
        # A fully qualified lead ready to buy immediately
        lead = SimpleNamespace(
            purpose="PRIMARY RESIDENCE",
            decision_maker="SOLE",
            intent="BUY",
            budget_min=15000000,
            budget_max=25000000,
            budget_flexibility="FLEXIBLE",
            move_in_timeline="IMMEDIATE",
            delay_factors="NONE",
            property_type="VILLA",
            bhk=4,
            preferred_location="Rajpur Road",
            phone_hash="hash123",
            raw_message_text="Client: Hello\nClient: Looking for 4 BHK villa\nClient: Rajpur Road\nClient: 2 Cr",
        )
        answers = {
            "built_up_area": "4000 sq ft",
            "parking": "2 cars",
            "amenities": "Garden",
        }
        out = ScoringEngine.evaluate(lead, answers)
        assert out.intent_score >= 70
        assert out.lead_health == "HOT"
        assert len(out.unresolved_questions) == 0
        assert "components" in out.breakdown
        assert out.breakdown["total_score"] == out.intent_score

    def test_warm_lead_evaluation(self):
        lead = SimpleNamespace(
            purpose="INVESTMENT",
            decision_maker="FAMILY",
            intent="BUY",
            budget_min=None,
            budget_max=10000000,
            budget_flexibility=None,
            move_in_timeline="1-3 MONTHS",
            delay_factors=None,
            property_type="FLAT",
            bhk=3,
            preferred_location="Dehradun",
            phone_hash="hash123",
            raw_message_text="Client: Looking to invest in 3 bhk",
        )
        out = ScoringEngine.evaluate(lead)
        assert 40 <= out.intent_score <= 69
        assert out.lead_health == "WARM"

    def test_cold_lead_evaluation(self):
        lead = SimpleNamespace(
            purpose=None,
            decision_maker=None,
            intent="BUY",
            budget_min=None,
            budget_max=None,
            budget_flexibility=None,
            move_in_timeline="> 6 MONTHS",
            delay_factors=None,
            property_type="PLOT",
            bhk=None,
            preferred_location=None,
            phone_hash="hash123",
            raw_message_text="Client: Plot",
        )
        out = ScoringEngine.evaluate(lead)
        assert 10 <= out.intent_score <= 39
        assert out.lead_health == "COLD"

    def test_insufficient_data_lead_evaluation(self):
        # Bare lead with zero criteria
        lead = SimpleNamespace(
            purpose=None,
            decision_maker=None,
            intent=None,
            budget_min=None,
            budget_max=None,
            budget_flexibility=None,
            move_in_timeline=None,
            delay_factors=None,
            property_type=None,
            bhk=None,
            preferred_location=None,
            phone_hash=None,
            phone_e164_enc=None,
            raw_message_text=None,
        )
        out = ScoringEngine.evaluate(lead)
        assert out.intent_score == 0
        assert out.lead_health == "INSUFFICIENT_DATA"
        assert len(out.unresolved_questions) > 0
        assert "Insufficient data" in out.breakdown["explanation"]
