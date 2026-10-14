"""
Unit tests for the Phase 8 AI qualification evaluation harness and dataset.
Verifies dataset integrity, metric calculation, unknown field preservation,
and runs the baseline evaluation suite against MockLLMAdapter without external network calls.
"""

import pytest

from app.qualification.llm_adapter import MockLLMAdapter
from tests.evaluation.dataset import EVALUATION_DATASET
from tests.evaluation.eval_harness import EvaluationHarness


class TestEvaluationDatasetIntegrity:
    def test_dataset_size_and_categories(self):
        assert len(EVALUATION_DATASET) == 28
        scenario_ids = [s.scenario_id for s in EVALUATION_DATASET]
        assert len(scenario_ids) == len(set(scenario_ids)), "Duplicate scenario IDs detected"

        categories = {s.category for s in EVALUATION_DATASET}
        expected_categories = {
            "A_STANDARD_BUY",
            "B_HINGLISH_SHORTHAND",
            "C_MISSING_AND_SKIPPED",
            "D_CORRECTIONS_AND_AMBIGUITY",
            "E_INTENT_AND_SCOPE",
            "F_ADVERSARIAL_AND_UNSUPPORTED",
        }
        assert categories == expected_categories

    def test_scenarios_have_required_fields(self):
        for s in EVALUATION_DATASET:
            assert len(s.messages) > 0
            assert s.expected_intent in {"BUY", "SELL", "INVEST", "RENT", "AMBIGUOUS"}
            assert isinstance(s.expected_unknown_fields, list)
            assert isinstance(s.expected_broker_handoff, bool)
            assert len(s.rationale) > 10


class TestMockEvaluationHarnessExecution:
    @pytest.mark.asyncio
    async def test_baseline_mock_evaluation_run(self):
        """
        Runs the full 28-scenario evaluation against the baseline MockLLMAdapter.
        Verifies that harness calculates valid rates, tracks critical errors,
        and produces a complete report without external network calls.
        """
        adapter = MockLLMAdapter()
        harness = EvaluationHarness(adapter)

        report = await harness.run_suite(EVALUATION_DATASET)

        assert report.total_scenarios == 28
        assert report.adapter_name == "MockLLMAdapter"
        assert report.schema_valid_rate == 1.0  # Mock adapter always returns schema-valid objects
        assert 0.0 <= report.exact_field_accuracy <= 1.0
        assert 0.0 <= report.intent_accuracy <= 1.0
        assert 0.0 <= report.budget_accuracy <= 1.0
        assert 0.0 <= report.unknown_preservation_rate <= 1.0
        assert report.broker_handoff_accuracy == 1.0  # Tokenizer accurately spots human requests

        # Verify scenario-level results
        assert len(report.scenario_results) == 28

        # Standard buy scenario BUY-01 matches mock heuristics
        buy_01 = next(r for r in report.scenario_results if r.scenario_id == "BUY-01")
        assert buy_01.schema_valid is True
        assert buy_01.intent_match is True
        assert buy_01.property_type_match is True
        assert buy_01.bhk_match is True
        assert buy_01.location_match is True

        # Broker request INT-05 recognized
        int_05 = next(r for r in report.scenario_results if r.scenario_id == "INT-05")
        assert int_05.broker_handoff_recognized is True

    @pytest.mark.asyncio
    async def test_evaluation_detects_unsupported_claims(self):
        """Verifies that when an unmentioned field is populated, the harness flags unsupported claims."""
        from app.qualification.llm_adapter import QualificationExtraction

        # Create a mock that hallucinates a 5 BHK and a 50 Cr budget on every input
        hallucinating_extraction = QualificationExtraction(
            bhk=5,
            property_type="VILLA",
            budget_max=500_000_000,
            preferred_location="Whitefield",
            purpose="BUY",
        )
        fake_adapter = MockLLMAdapter(override_result=hallucinating_extraction)
        harness = EvaluationHarness(fake_adapter)

        # Run on SKIP-04 (where BHK, budget, and property type are all unstated)
        skip_04 = next(s for s in EVALUATION_DATASET if s.scenario_id == "SKIP-04")
        result = await harness.evaluate_scenario(skip_04)

        assert result.success is False
        assert len(result.unsupported_claims) > 0
        assert result.unknowns_preserved is False
        assert any("bhk" in claim.lower() for claim in result.unsupported_claims)
