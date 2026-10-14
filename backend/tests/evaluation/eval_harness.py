"""
Evaluation Harness for HAVN Realty Lead Qualification AI.
Executes synthetic evaluation scenarios against an LLMAdapter,
measures field-level and scenario-level metrics, detects critical errors,
and produces machine-readable summary reports.
"""

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from app.qualification.llm_adapter import (
    LLMAdapter,
    LLMAdapterError,
)
from tests.evaluation.dataset import EVALUATION_DATASET, EvaluationScenario


@dataclass
class ScenarioEvaluationResult:
    scenario_id: str
    category: str
    success: bool
    schema_valid: bool
    intent_match: bool
    budget_match: bool
    location_match: bool
    property_type_match: bool
    bhk_match: bool
    unknowns_preserved: bool
    unsupported_claims: list[str]
    broker_handoff_recognized: bool
    critical_errors: list[str]
    latency_ms: float
    extracted_data: dict[str, Any]
    expected_data: dict[str, Any]
    notes: str = ""


@dataclass
class EvaluationReport:
    adapter_name: str
    model_name: str
    total_scenarios: int
    schema_valid_rate: float
    exact_field_accuracy: float
    intent_accuracy: float
    budget_accuracy: float
    location_accuracy: float
    property_type_accuracy: float
    unknown_preservation_rate: float
    unsupported_extraction_rate: float
    broker_handoff_accuracy: float
    critical_error_count: int
    mean_latency_ms: float
    scenario_results: list[ScenarioEvaluationResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class EvaluationHarness:
    def __init__(self, adapter: LLMAdapter) -> None:
        self.adapter = adapter

    async def evaluate_scenario(self, scenario: EvaluationScenario) -> ScenarioEvaluationResult:
        start_time = time.perf_counter()
        schema_valid = False
        critical_errors: list[str] = []
        unsupported_claims: list[str] = []
        extracted_dict: dict[str, Any] = {}

        # 1. Check broker handoff recognition via conversational tokenizer
        combined_text = " ".join(scenario.messages)
        # Use dummy session for static check
        is_broker_token = any(
            tok in combined_text.lower()
            for tok in [
                "broker", "agent", "human", "call", "call me", "talk to human",
                "talk to agent", "connect me", "broker se baat", "phone karo",
            ]
        )
        broker_match = is_broker_token == scenario.expected_broker_handoff
        if scenario.expected_broker_handoff and not is_broker_token:
            critical_errors.append("Missed direct broker handoff request")

        # 2. Run extraction through adapter
        try:
            extraction, model_name, _ = await self.adapter.extract_qualification(scenario.messages)
            schema_valid = True
            extracted_dict = {
                "purpose": extraction.purpose,
                "property_type": extraction.property_type,
                "bhk": extraction.bhk,
                "budget_min": extraction.budget_min,
                "budget_max": extraction.budget_max,
                "preferred_location": extraction.preferred_location,
                "move_in_timeline": extraction.move_in_timeline,
                "summary": extraction.summary,
            }
        except LLMAdapterError as exc:
            schema_valid = False
            critical_errors.append(f"LLM Adapter error: {exc}")
            extracted_dict = {"error": str(exc)}
            extraction = None
        except Exception as exc:
            schema_valid = False
            critical_errors.append(f"Unexpected extraction failure: {type(exc).__name__} - {exc}")
            extracted_dict = {"error": str(exc)}
            extraction = None

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        if not extraction:
            return ScenarioEvaluationResult(
                scenario_id=scenario.scenario_id,
                category=scenario.category,
                success=False,
                schema_valid=False,
                intent_match=False,
                budget_match=False,
                location_match=False,
                property_type_match=False,
                bhk_match=False,
                unknowns_preserved=False,
                unsupported_claims=["Extraction failed"],
                broker_handoff_recognized=broker_match,
                critical_errors=critical_errors,
                latency_ms=latency_ms,
                extracted_data=extracted_dict,
                expected_data={
                    "intent": scenario.expected_intent,
                    "purpose": scenario.expected_purpose,
                    "bhk": scenario.expected_bhk,
                    "property_type": scenario.expected_property_type,
                    "budget_max": scenario.expected_budget_max,
                    "location": scenario.expected_location,
                },
                notes="Adapter threw exception during extraction",
            )

        # 3. Verify Purpose / Intent
        intent_match = False
        if extraction.purpose == scenario.expected_purpose or scenario.acceptable_alternatives.get("expected_purpose") == extraction.purpose or scenario.expected_intent == "AMBIGUOUS" and extraction.purpose is None:
            intent_match = True

        # Check for critical intent misclassification
        if scenario.expected_intent == "RENT" and extraction.purpose == "BUY":
            critical_errors.append("Misclassified out-of-scope RENT inquiry as BUY")

        # 4. Verify BHK
        bhk_match = extraction.bhk == scenario.expected_bhk
        if scenario.expected_bhk is None and extraction.bhk is not None:
            unsupported_claims.append(f"Invented BHK {extraction.bhk} when unstated")
            critical_errors.append(f"Hallucinated BHK: {extraction.bhk}")

        # 5. Verify Property Type
        prop_match = extraction.property_type == scenario.expected_property_type
        if scenario.expected_property_type is None and extraction.property_type is not None:
            unsupported_claims.append(f"Invented property type {extraction.property_type}")
            critical_errors.append(f"Hallucinated property type: {extraction.property_type}")

        # 6. Verify Location
        loc_match = False
        if scenario.expected_location is None:
            loc_match = extraction.preferred_location is None
            if extraction.preferred_location is not None:
                unsupported_claims.append(f"Invented location {extraction.preferred_location}")
                critical_errors.append(f"Hallucinated location: {extraction.preferred_location}")
        elif extraction.preferred_location:
            loc_match = scenario.expected_location.lower() in extraction.preferred_location.lower()

        # 7. Verify Budget
        budget_match = False
        # Max budget
        max_ok = False
        if scenario.expected_budget_max is None:
            max_ok = extraction.budget_max is None
            if extraction.budget_max is not None:
                unsupported_claims.append(f"Invented budget_max {extraction.budget_max}")
                critical_errors.append(f"Hallucinated budget: {extraction.budget_max}")
        else:
            if extraction.budget_max == scenario.expected_budget_max or scenario.acceptable_alternatives.get("budget_max") == extraction.budget_max or extraction.budget_max and abs(extraction.budget_max - scenario.expected_budget_max) / scenario.expected_budget_max <= 0.15:
                max_ok = True

        budget_match = max_ok

        # 8. Check preservation of expected unknowns
        unknowns_preserved = True
        for unk in scenario.expected_unknown_fields:
            val = extracted_dict.get(unk)
            if val is not None:
                # Check acceptable alternatives
                if unk in scenario.acceptable_alternatives and scenario.acceptable_alternatives[unk] == val:
                    continue
                unknowns_preserved = False
                unsupported_claims.append(f"Field '{unk}' was asserted as {val!r} instead of remaining null")

        # Overall scenario success
        success = (
            schema_valid
            and intent_match
            and budget_match
            and loc_match
            and prop_match
            and bhk_match
            and unknowns_preserved
            and broker_match
            and len(critical_errors) == 0
        )

        return ScenarioEvaluationResult(
            scenario_id=scenario.scenario_id,
            category=scenario.category,
            success=success,
            schema_valid=schema_valid,
            intent_match=intent_match,
            budget_match=budget_match,
            location_match=loc_match,
            property_type_match=prop_match,
            bhk_match=bhk_match,
            unknowns_preserved=unknowns_preserved,
            unsupported_claims=unsupported_claims,
            broker_handoff_recognized=broker_match,
            critical_errors=critical_errors,
            latency_ms=latency_ms,
            extracted_data=extracted_dict,
            expected_data={
                "intent": scenario.expected_intent,
                "purpose": scenario.expected_purpose,
                "property_type": scenario.expected_property_type,
                "bhk": scenario.expected_bhk,
                "budget_max": scenario.expected_budget_max,
                "location": scenario.expected_location,
                "expected_unknowns": scenario.expected_unknown_fields,
            },
            notes=scenario.rationale,
        )

    async def run_suite(self, scenarios: list[EvaluationScenario] | None = None) -> EvaluationReport:
        dataset = scenarios or EVALUATION_DATASET
        results: list[ScenarioEvaluationResult] = []

        for scenario in dataset:
            res = await self.evaluate_scenario(scenario)
            results.append(res)

        total = len(results)
        if total == 0:
            raise ValueError("Evaluation dataset is empty.")

        schema_valid_rate = sum(1 for r in results if r.schema_valid) / total
        intent_accuracy = sum(1 for r in results if r.intent_match) / total
        budget_accuracy = sum(1 for r in results if r.budget_match) / total
        location_accuracy = sum(1 for r in results if r.location_match) / total
        property_type_accuracy = sum(1 for r in results if r.property_type_match) / total
        unknown_preservation = sum(1 for r in results if r.unknowns_preserved) / total
        unsupported_count = sum(1 for r in results if len(r.unsupported_claims) > 0)
        unsupported_rate = unsupported_count / total
        broker_accuracy = sum(1 for r in results if r.broker_handoff_recognized) / total
        critical_count = sum(len(r.critical_errors) for r in results)
        mean_latency = sum(r.latency_ms for r in results) / total

        # Overall exact field accuracy across all 5 key fields (intent, budget, location, type, bhk)
        total_fields = total * 5
        matched_fields = (
            sum(1 for r in results if r.intent_match)
            + sum(1 for r in results if r.budget_match)
            + sum(1 for r in results if r.location_match)
            + sum(1 for r in results if r.property_type_match)
            + sum(1 for r in results if r.bhk_match)
        )
        exact_field_accuracy = matched_fields / total_fields

        adapter_name = type(self.adapter).__name__
        if getattr(self.adapter, "override_result", None):
            model_name = "mock-override"
        elif hasattr(self.adapter, "settings") and getattr(self.adapter.settings, "openai_model", None):
            model_name = self.adapter.settings.openai_model
        else:
            model_name = "mock-v1"

        return EvaluationReport(
            adapter_name=adapter_name,
            model_name=model_name,
            total_scenarios=total,
            schema_valid_rate=schema_valid_rate,
            exact_field_accuracy=exact_field_accuracy,
            intent_accuracy=intent_accuracy,
            budget_accuracy=budget_accuracy,
            location_accuracy=location_accuracy,
            property_type_accuracy=property_type_accuracy,
            unknown_preservation_rate=unknown_preservation,
            unsupported_extraction_rate=unsupported_rate,
            broker_handoff_accuracy=broker_accuracy,
            critical_error_count=critical_count,
            mean_latency_ms=mean_latency,
            scenario_results=results,
        )
