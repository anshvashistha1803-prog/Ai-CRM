"""
Unit tests for matching/engine.py — MatchEngine.
Tests cover 10+ scenarios: budget tolerances, property type, BHK,
location matching, availability gating, and multi-property ranking.
"""

import uuid
from types import SimpleNamespace

from app.matching.engine import MatchEngine


class TestMatchEngine:
    def test_scenario_1_perfect_match(self):
        lead = SimpleNamespace(
            budget_min=8000000,
            budget_max=12000000,
            property_type="APARTMENT",
            bhk=3,
            preferred_location="Whitefield, Bengaluru",
        )
        prop = SimpleNamespace(
            id=uuid.uuid4(),
            price=10000000,
            property_type="APARTMENT",
            bhk=3,
            location_text="Whitefield, Bengaluru",
            status="AVAILABLE",
        )
        res = MatchEngine.evaluate_pair(lead, prop)
        assert res.score == 100.0
        assert res.reasons["available"] is True
        assert res.reasons["budget"]["match"] == "exact_range"
        assert res.reasons["property_type"]["match"] == "exact"
        assert res.reasons["bhk"]["match"] == "exact"
        assert res.reasons["location"]["match"] == "substring"

    def test_scenario_2_unavailable_property_returns_zero(self):
        lead = SimpleNamespace(
            budget_min=8000000,
            budget_max=12000000,
            property_type="APARTMENT",
            bhk=3,
            preferred_location="Whitefield",
        )
        prop = SimpleNamespace(
            id=uuid.uuid4(),
            price=10000000,
            property_type="APARTMENT",
            bhk=3,
            location_text="Whitefield",
            status="SOLD",
        )
        res = MatchEngine.evaluate_pair(lead, prop)
        assert res.score == 0.0
        assert res.reasons["available"] is False

    def test_scenario_3_budget_within_20_percent_tolerance(self):
        lead = SimpleNamespace(
            budget_min=8000000,
            budget_max=10000000,
            property_type="APARTMENT",
            bhk=3,
            preferred_location="Whitefield",
        )
        # 11,500,000 is 15% above max (within 20%)
        prop = SimpleNamespace(
            id=uuid.uuid4(),
            price=11500000,
            property_type="APARTMENT",
            bhk=3,
            location_text="Whitefield",
            status="AVAILABLE",
        )
        res = MatchEngine.evaluate_pair(lead, prop)
        assert res.reasons["budget"]["match"] == "within_20pct"
        assert res.reasons["budget"]["points"] == 20.0

    def test_scenario_4_budget_outside_30_percent_tolerance(self):
        lead = SimpleNamespace(
            budget_min=8000000,
            budget_max=10000000,
            property_type="APARTMENT",
            bhk=3,
            preferred_location="Whitefield",
        )
        # 20,000,000 is double the max budget
        prop = SimpleNamespace(
            id=uuid.uuid4(),
            price=20000000,
            property_type="APARTMENT",
            bhk=3,
            location_text="Whitefield",
            status="AVAILABLE",
        )
        res = MatchEngine.evaluate_pair(lead, prop)
        assert res.reasons["budget"]["match"] == "outside_budget"
        assert res.reasons["budget"]["points"] == 0.0

    def test_scenario_5_property_type_mismatch(self):
        lead = SimpleNamespace(
            budget_min=10000000,
            budget_max=15000000,
            property_type="VILLA",
            bhk=4,
            preferred_location="Sarjapur",
        )
        prop = SimpleNamespace(
            id=uuid.uuid4(),
            price=12000000,
            property_type="APARTMENT",
            bhk=4,
            location_text="Sarjapur",
            status="AVAILABLE",
        )
        res = MatchEngine.evaluate_pair(lead, prop)
        assert res.reasons["property_type"]["match"] == "mismatch"
        assert res.reasons["property_type"]["points"] == 0.0

    def test_scenario_6_adjacent_bhk_partial_points(self):
        # 3 BHK requested, 2 BHK property
        lead = SimpleNamespace(
            budget_min=None,
            budget_max=10000000,
            property_type="APARTMENT",
            bhk=3,
            preferred_location=None,
        )
        prop = SimpleNamespace(
            id=uuid.uuid4(),
            price=8000000,
            property_type="APARTMENT",
            bhk=2,
            location_text="Koramangala",
            status="AVAILABLE",
        )
        res = MatchEngine.evaluate_pair(lead, prop)
        assert res.reasons["bhk"]["match"] == "adjacent_bhk"
        assert res.reasons["bhk"]["points"] == 10.0

    def test_scenario_7_distant_bhk_mismatch(self):
        # 4 BHK requested, 1 BHK property
        lead = SimpleNamespace(
            budget_min=None,
            budget_max=10000000,
            property_type="APARTMENT",
            bhk=4,
            preferred_location=None,
        )
        prop = SimpleNamespace(
            id=uuid.uuid4(),
            price=5000000,
            property_type="APARTMENT",
            bhk=1,
            location_text="Koramangala",
            status="AVAILABLE",
        )
        res = MatchEngine.evaluate_pair(lead, prop)
        assert res.reasons["bhk"]["match"] == "mismatch"
        assert res.reasons["bhk"]["points"] == 0.0

    def test_scenario_8_location_token_overlap(self):
        lead = SimpleNamespace(
            budget_min=None,
            budget_max=None,
            property_type=None,
            bhk=None,
            preferred_location="Indiranagar 100ft Road",
        )
        prop = SimpleNamespace(
            id=uuid.uuid4(),
            price=15000000,
            property_type="APARTMENT",
            bhk=3,
            location_text="Indiranagar, Bangalore",
            status="AVAILABLE",
        )
        res = MatchEngine.evaluate_pair(lead, prop)
        assert res.reasons["location"]["match"] in ["substring", "token_overlap"]
        assert res.reasons["location"]["points"] >= 15.0

    def test_scenario_9_unspecified_lead_criteria_receives_neutral_points(self):
        lead = SimpleNamespace(
            budget_min=None,
            budget_max=None,
            property_type=None,
            bhk=None,
            preferred_location=None,
        )
        prop = SimpleNamespace(
            id=uuid.uuid4(),
            price=15000000,
            property_type="COMMERCIAL",
            bhk=None,
            location_text="Outer Ring Road",
            status="AVAILABLE",
        )
        res = MatchEngine.evaluate_pair(lead, prop)
        # Should receive neutral baseline scores
        assert res.score > 0
        assert res.reasons["budget"]["match"] == "unspecified"
        assert res.reasons["property_type"]["match"] == "unspecified"

    def test_scenario_10_multi_property_ranking(self):
        lead = SimpleNamespace(
            budget_min=5000000,
            budget_max=7000000,
            property_type="APARTMENT",
            bhk=2,
            preferred_location="Electronic City",
        )

        p1_exact = SimpleNamespace(
            id="p1",
            price=6000000,
            property_type="APARTMENT",
            bhk=2,
            location_text="Electronic City Phase 1",
            status="AVAILABLE",
        )
        p2_partial = SimpleNamespace(
            id="p2",
            price=6500000,
            property_type="APARTMENT",
            bhk=3,  # adjacent
            location_text="Electronic City Phase 2",
            status="AVAILABLE",
        )
        p3_mismatch = SimpleNamespace(
            id="p3",
            price=25000000,  # way over budget
            property_type="VILLA",
            bhk=5,
            location_text="North Bangalore",
            status="AVAILABLE",
        )
        p4_sold = SimpleNamespace(
            id="p4",
            price=6000000,
            property_type="APARTMENT",
            bhk=2,
            location_text="Electronic City",
            status="SOLD",  # unavailable
        )

        catalog = [p3_mismatch, p4_sold, p2_partial, p1_exact]
        ranked = MatchEngine.match_lead(lead, catalog, min_score_threshold=40.0)

        assert len(ranked) == 2
        assert ranked[0].property_id == "p1"
        assert ranked[1].property_id == "p2"
        assert ranked[0].score > ranked[1].score
