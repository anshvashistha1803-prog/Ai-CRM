"""
HAVN Realty ScoringEngine — evaluates leads deterministically and generates
intent score (0-100), health rating (HOT/WARM/COLD/INSUFFICIENT_DATA),
component breakdown, evidence audit trail, and unresolved questions.
"""

from dataclasses import dataclass
from typing import Any

from app.scoring.rules import (
    RuleResult,
    rule_budget_clarity,
    rule_decision_timeline,
    rule_engagement,
    rule_intent_and_commitment,
    rule_location_and_property_type,
    rule_requirement_specificity,
)


@dataclass(frozen=True)
class ScoreOutput:
    intent_score: int
    lead_health: str
    breakdown: dict[str, Any]
    unresolved_questions: list[str]


class ScoringEngine:
    """
    Deterministic scoring engine implementing the HAVN Realty rubric:
      1. Intent and commitment: 25 points
      2. Budget clarity and readiness: 20 points
      3. Decision timeline: 20 points
      4. Requirement specificity: 15 points
      5. Engagement: 10 points
      6. Location and property-type clarity: 10 points
      Total: 100 points
    """

    RULES = [
        rule_intent_and_commitment,
        rule_budget_clarity,
        rule_decision_timeline,
        rule_requirement_specificity,
        rule_engagement,
        rule_location_and_property_type,
    ]

    @classmethod
    def evaluate(cls, lead: Any, answers: dict[str, Any] | None = None) -> ScoreOutput:
        """
        Evaluate a lead against the 6 deterministic HAVN scoring components.
        Returns clamped intent score (0-100), health tier, auditable breakdown,
        and list of unresolved questions.
        """
        raw_score = 0
        rule_breakdowns: dict[str, Any] = {}
        unresolved: list[str] = []

        for rule_fn in cls.RULES:
            res: RuleResult = rule_fn(lead, answers)
            raw_score += res.points
            rule_breakdowns[res.rule_name] = {
                "points": res.points,
                "max_points": res.max_points,
                "reason": res.reason,
                "evidence": res.evidence,
            }

            # Check if component has zero points / unresolved
            if res.points == 0:
                if res.rule_name == "intent_and_commitment":
                    unresolved.append("Purchase/Sale purpose and decision maker not established")
                elif res.rule_name == "budget_clarity":
                    unresolved.append("Budget bracket and flexibility range not specified")
                elif res.rule_name == "decision_timeline":
                    unresolved.append("Transaction timeline not provided")
                elif res.rule_name == "requirement_specificity":
                    unresolved.append("Property specifications (BHK, area, parking) not provided")
                elif res.rule_name == "location_and_property_type":
                    unresolved.append("Preferred location and property type not specified")

        final_score = max(0, min(100, raw_score))

        # Health tier classification
        # HOT: 70–100, WARM: 40–69, COLD: 10–39, UNQUALIFIED / INSUFFICIENT DATA: 0–9
        if final_score >= 70:
            health = "HOT"
        elif final_score >= 40:
            health = "WARM"
        elif final_score >= 10:
            health = "COLD"
        else:
            health = "INSUFFICIENT_DATA"

        explanation = (
            f"Lead scored {final_score}/100 ({health}). "
            f"Evaluated across 6 criteria with {len(unresolved)} unresolved questions."
        )
        if health == "INSUFFICIENT_DATA":
            explanation += " Insufficient data to qualify client readiness; broker review required."

        breakdown_payload = {
            "total_score": final_score,
            "raw_sum": raw_score,
            "health": health,
            "explanation": explanation,
            "components": rule_breakdowns,
            "unresolved_questions": unresolved,
        }
        if answers:
            breakdown_payload["answers"] = answers

        return ScoreOutput(
            intent_score=final_score,
            lead_health=health,
            breakdown=breakdown_payload,
            unresolved_questions=unresolved,
        )
