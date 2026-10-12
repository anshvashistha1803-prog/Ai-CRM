"""
MatchEngine — deterministic matching engine connecting leads to properties.
Pure functions, fully auditable, no network or LLM dependencies.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MatchScoreResult:
    property_id: Any
    score: float
    reasons: dict[str, Any]


class MatchEngine:
    BUDGET_TOLERANCE_PCT = 0.20  # ±20%

    @classmethod
    def evaluate_pair(cls, lead: Any, prop: Any) -> MatchScoreResult:
        """
        Evaluate compatibility between a single lead and property.
        Returns match score (0-100) and structured reason breakdown.
        """
        reasons: dict[str, Any] = {}
        score = 0.0

        # Availability gate
        status = str(getattr(prop, "status", "")).upper()
        if status != "AVAILABLE":
            return MatchScoreResult(
                property_id=getattr(prop, "id", None),
                score=0.0,
                reasons={"available": False, "reason": f"Property status is {status}"},
            )
        reasons["available"] = True

        # ── 1. Budget Compatibility (max 35 pts) ──────────────────────────────
        prop_price = getattr(prop, "price", 0) or 0
        lead_min = getattr(lead, "budget_min", None)
        lead_max = getattr(lead, "budget_max", None)

        if lead_min is not None and lead_max is not None:
            if lead_min <= prop_price <= lead_max:
                score += 35.0
                reasons["budget"] = {"points": 35.0, "match": "exact_range"}
            elif (lead_min * 0.8) <= prop_price <= (lead_max * 1.2):
                score += 20.0
                reasons["budget"] = {"points": 20.0, "match": "within_20pct"}
            elif (lead_min * 0.7) <= prop_price <= (lead_max * 1.3):
                score += 10.0
                reasons["budget"] = {"points": 10.0, "match": "within_30pct"}
            else:
                reasons["budget"] = {"points": 0.0, "match": "outside_budget"}
        elif lead_max is not None:
            if prop_price <= lead_max:
                score += 35.0
                reasons["budget"] = {"points": 35.0, "match": "under_max"}
            elif prop_price <= (lead_max * 1.2):
                score += 20.0
                reasons["budget"] = {"points": 20.0, "match": "within_20pct"}
            else:
                reasons["budget"] = {"points": 0.0, "match": "over_budget"}
        elif lead_min is not None:
            if prop_price >= lead_min:
                score += 25.0
                reasons["budget"] = {"points": 25.0, "match": "above_min"}
            else:
                score += 15.0
                reasons["budget"] = {"points": 15.0, "match": "below_min"}
        else:
            score += 15.0
            reasons["budget"] = {"points": 15.0, "match": "unspecified"}

        # ── 2. Property Type Match (max 25 pts) ──────────────────────────────
        lead_type = getattr(lead, "property_type", None)
        prop_type = getattr(prop, "property_type", None)

        if lead_type and prop_type:
            if lead_type.strip().upper() == prop_type.strip().upper():
                score += 25.0
                reasons["property_type"] = {"points": 25.0, "match": "exact"}
            else:
                reasons["property_type"] = {"points": 0.0, "match": "mismatch"}
        else:
            score += 15.0
            reasons["property_type"] = {"points": 15.0, "match": "unspecified"}

        # ── 3. BHK Match (max 20 pts) ─────────────────────────────────────────
        lead_bhk = getattr(lead, "bhk", None)
        prop_bhk = getattr(prop, "bhk", None)

        if lead_bhk is not None and prop_bhk is not None:
            if lead_bhk == prop_bhk:
                score += 20.0
                reasons["bhk"] = {"points": 20.0, "match": "exact"}
            elif abs(lead_bhk - prop_bhk) == 1:
                score += 10.0
                reasons["bhk"] = {"points": 10.0, "match": "adjacent_bhk"}
            else:
                reasons["bhk"] = {"points": 0.0, "match": "mismatch"}
        else:
            score += 10.0
            reasons["bhk"] = {"points": 10.0, "match": "unspecified"}

        # ── 4. Location Match (max 20 pts) ────────────────────────────────────
        lead_loc = getattr(lead, "preferred_location", None)
        prop_loc = getattr(prop, "location_text", None)

        if lead_loc and prop_loc:
            lead_loc_clean = lead_loc.strip().lower()
            prop_loc_clean = prop_loc.strip().lower()

            # Word tokens match
            lead_tokens = set(lead_loc_clean.replace(",", " ").split())
            prop_tokens = set(prop_loc_clean.replace(",", " ").split())

            if lead_loc_clean in prop_loc_clean or prop_loc_clean in lead_loc_clean:
                score += 20.0
                reasons["location"] = {"points": 20.0, "match": "substring"}
            elif lead_tokens.intersection(prop_tokens):
                score += 15.0
                reasons["location"] = {"points": 15.0, "match": "token_overlap"}
            else:
                reasons["location"] = {"points": 0.0, "match": "no_match"}
        else:
            score += 10.0
            reasons["location"] = {"points": 10.0, "match": "unspecified"}

        final_score = round(min(100.0, max(0.0, score)), 2)
        return MatchScoreResult(
            property_id=getattr(prop, "id", None),
            score=final_score,
            reasons=reasons,
        )

    @classmethod
    def match_lead(
        cls,
        lead: Any,
        catalog: list[Any],
        min_score_threshold: float = 40.0,
        limit: int = 10,
    ) -> list[MatchScoreResult]:
        """
        Rank a catalog of properties against a lead.
        Returns top matches sorted by score descending.
        """
        matches = []
        for prop in catalog:
            result = cls.evaluate_pair(lead, prop)
            if result.score >= min_score_threshold:
                matches.append(result)

        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:limit]
