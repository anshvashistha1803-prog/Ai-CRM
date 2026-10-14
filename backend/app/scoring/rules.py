"""
HAVN Realty deterministic scoring rule definitions for real estate leads.

Categories & Weights (Max 100 points):
  1. Intent and commitment: 25 points
  2. Budget clarity and readiness: 20 points
  3. Decision timeline: 20 points
  4. Requirement specificity: 15 points
  5. Engagement: 10 points
  6. Location and property-type clarity: 10 points

Health Tiers:
  - HOT: 70–100
  - WARM: 40–69
  - COLD: 10–39
  - UNQUALIFIED / INSUFFICIENT DATA: 0–9

Rules are pure, deterministic functions with no network or DB access.
Missing answers are treated as 0 (unknown), never penalized with negative deductions.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuleResult:
    rule_name: str
    points: int
    max_points: int
    reason: str
    evidence: str | None = None


def _get_val(lead: Any, answers: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    if answers and key in answers:
        return answers[key]
    if hasattr(lead, key):
        val = getattr(lead, key)
        if val is not None:
            return val
    # Check score_breakdown if answers stored there
    if hasattr(lead, "score_breakdown") and isinstance(lead.score_breakdown, dict):
        stored_answers = lead.score_breakdown.get("answers", {})
        if isinstance(stored_answers, dict) and key in stored_answers:
            return stored_answers[key]
    return default


def rule_intent_and_commitment(lead: Any, answers: dict[str, Any] | None = None) -> RuleResult:
    """
    Component 1: Intent and commitment (Max 25 points).
    Evaluates purchase/sale purpose, decision-maker authority, and end-user vs speculative commitment.
    """
    purpose = str(_get_val(lead, answers, "purpose", "") or "").upper()
    decision_maker = str(_get_val(lead, answers, "decision_maker", "") or "").upper()
    intent = str(_get_val(lead, answers, "intent", "") or "").upper()

    points = 0
    reasons = []

    # Purpose clarity (up to 15 pts)
    if any(k in purpose for k in ["BUY", "SELF_USE", "PRIMARY", "PRIMARY RESIDENCE", "END_USER"]):
        points += 15
        reasons.append("Primary residence / end-user buyer (+15)")
    elif any(k in purpose for k in ["SECOND_HOME", "SECOND HOME", "LEGACY"]):
        points += 14
        reasons.append("Second home / luxury legacy buyer (+14)")
    elif any(k in purpose for k in ["INVEST", "INVESTMENT", "DEVELOPMENT"]):
        points += 12
        reasons.append("Investor / developer with capital (+12)")
    elif any(k in purpose for k in ["SELL", "LISTING", "SALE"]) or intent in ["BUY", "SELL"]:
        points += 8
        reasons.append("Clear transaction intent (+8)")

    # Decision maker authority (up to 10 pts)
    if any(k in decision_maker for k in ["SELF", "SOLE", "ME", "INDIVIDUAL", "I AM"]):
        points += 10
        reasons.append("Sole decision maker (+10)")
    elif any(k in decision_maker for k in ["FAMILY", "SPOUSE", "PARTNER", "JOINT"]):
        points += 7
        reasons.append("Joint family decision makers (+7)")
    elif decision_maker:
        points += 4
        reasons.append("Identified decision structure (+4)")

    clamped = min(25, points)
    reason_str = "; ".join(reasons) if reasons else "Intent/commitment not yet established (0/25)"
    return RuleResult("intent_and_commitment", clamped, 25, reason_str, evidence=purpose or intent)


def rule_budget_clarity(lead: Any, answers: dict[str, Any] | None = None) -> RuleResult:
    """
    Component 2: Budget clarity and readiness (Max 20 points).
    Evaluates concrete numbers, min/max spread, and budget flexibility.
    """
    b_min = _get_val(lead, answers, "budget_min")
    b_max = _get_val(lead, answers, "budget_max")
    budget_flex = str(_get_val(lead, answers, "budget_flexibility", "") or "").upper()
    raw_budget = str(_get_val(lead, answers, "budget", "") or "").strip()

    points = 0
    reasons = []

    try:
        b_min_val = int(b_min) if b_min is not None else None
        b_max_val = int(b_max) if b_max is not None else None
    except (ValueError, TypeError):
        b_min_val, b_max_val = None, None

    if b_min_val is not None and b_max_val is not None and b_max_val >= b_min_val and b_max_val > 0:
        points += 16
        reasons.append(f"Defined budget bracket ({b_min_val:,} - {b_max_val:,}) (+16)")
    elif b_max_val is not None and b_max_val > 0:
        points += 14
        reasons.append(f"Explicit target budget ({b_max_val:,}) (+14)")
    elif raw_budget and any(c.isdigit() for c in raw_budget):
        points += 10
        reasons.append(f"Numeric budget indication '{raw_budget}' (+10)")

    # Flexibility clarity (+4 pts)
    if any(k in budget_flex for k in ["FLEXIBLE", "STRETCH", "YES", "COMFORTABLE", "FIXED"]):
        points += 4
        reasons.append(f"Budget flexibility clarified ({budget_flex}) (+4)")

    clamped = min(20, points)
    reason_str = "; ".join(reasons) if reasons else "Budget not yet specified (0/20)"
    evidence_str = f"min={b_min_val}, max={b_max_val}" if (b_min_val or b_max_val) else raw_budget
    return RuleResult("budget_clarity", clamped, 20, reason_str, evidence=evidence_str or None)


def rule_decision_timeline(lead: Any, answers: dict[str, Any] | None = None) -> RuleResult:
    """
    Component 3: Decision timeline (Max 20 points).
    Evaluates immediacy, transaction urgency, and potential delay factors.
    """
    timeline = str(_get_val(lead, answers, "move_in_timeline", "") or _get_val(lead, answers, "timeline", "") or "").upper()
    delay_factors = str(_get_val(lead, answers, "delay_factors", "") or "").upper()

    points = 0
    reasons = []

    if any(k in timeline for k in ["IMMEDIATE", "< 1 MONTH", "15 DAYS", "READY", "IMMEDIATELY", "RIGHT NOW", "ASAP"]):
        points += 16
        reasons.append("Immediate timeline (<1 month) (+16)")
    elif any(k in timeline for k in ["1-3 MONTHS", "2 MONTHS", "3 MONTHS", "SHORT TERM", "SOON"]):
        points += 12
        reasons.append("Short-term timeline (1-3 months) (+12)")
    elif any(k in timeline for k in ["3-6 MONTHS", "6 MONTHS", "MID TERM"]):
        points += 8
        reasons.append("Mid-term timeline (3-6 months) (+8)")
    elif any(k in timeline for k in ["> 6 MONTHS", "1 YEAR", "EXPLORING", "LONG TERM"]):
        points += 4
        reasons.append("Long-term exploration (>6 months) (+4)")
    elif timeline:
        points += 4
        reasons.append(f"Timeline stated ({timeline}) (+4)")

    # Factor delay clarity (up to 4 pts)
    if "NONE" in delay_factors or "NO" in delay_factors or "READY TO MOVE" in delay_factors:
        points += 4
        reasons.append("No blocking factors (+4)")
    elif delay_factors:
        points += 2
        reasons.append(f"Identified potential factors ({delay_factors[:30]}) (+2)")

    clamped = min(20, points)
    reason_str = "; ".join(reasons) if reasons else "Timeline not specified (0/20)"
    return RuleResult("decision_timeline", clamped, 20, reason_str, evidence=timeline or None)


def rule_requirement_specificity(lead: Any, answers: dict[str, Any] | None = None) -> RuleResult:
    """
    Component 4: Requirement specificity (Max 15 points).
    Evaluates depth of property specifications (BHK, dimensions, area, parking, amenities, users).
    """
    bhk = _get_val(lead, answers, "bhk")
    built_up = _get_val(lead, answers, "built_up_area") or _get_val(lead, answers, "area_sqft")
    parking = _get_val(lead, answers, "parking") or _get_val(lead, answers, "parking_required")
    amenities = _get_val(lead, answers, "amenities")
    users = _get_val(lead, answers, "users") or _get_val(lead, answers, "family_details")
    dimensions = _get_val(lead, answers, "dimensions") or _get_val(lead, answers, "plot_size")

    points = 0
    reasons = []

    if bhk is not None:
        points += 4
        reasons.append(f"{bhk} BHK specified (+4)")
    if built_up or dimensions:
        points += 4
        reasons.append(f"Area/dimensions specified ({built_up or dimensions}) (+4)")
    if parking:
        points += 3
        reasons.append(f"Parking specified ({parking}) (+3)")
    if amenities:
        points += 2
        reasons.append("Key amenities specified (+2)")
    if users:
        points += 2
        reasons.append("Family/occupants detailed (+2)")

    clamped = min(15, points)
    reason_str = "; ".join(reasons) if reasons else "No specific property specs provided (0/15)"
    return RuleResult("requirement_specificity", clamped, 15, reason_str, evidence=str(reasons) if reasons else None)


def rule_engagement(lead: Any, answers: dict[str, Any] | None = None) -> RuleResult:
    """
    Component 5: Engagement (Max 10 points).
    Evaluates responsive dialogue, question completion rate, and verified contact completeness.
    """
    phone = _get_val(lead, answers, "phone_e164_enc") or _get_val(lead, answers, "phone_hash") or _get_val(lead, answers, "phone")
    raw_messages = str(_get_val(lead, answers, "raw_message_text", "") or "")
    ans_count = len(answers) if answers else 0

    points = 0
    reasons = []

    # Valid phone communication channel (+4 pts)
    if phone:
        points += 4
        reasons.append("Verified WhatsApp/phone channel (+4)")

    # Conversational interactivity (+6 pts)
    client_turns = raw_messages.count("Client:") or raw_messages.count("\n")
    if ans_count >= 5 or client_turns >= 4:
        points += 6
        reasons.append("High multi-turn engagement (+6)")
    elif ans_count >= 2 or client_turns >= 2:
        points += 4
        reasons.append("Moderate engagement (+4)")
    elif ans_count >= 1 or client_turns >= 1:
        points += 2
        reasons.append("Initial response received (+2)")

    clamped = min(10, points)
    reason_str = "; ".join(reasons) if reasons else "No engagement observed (0/10)"
    return RuleResult("engagement", clamped, 10, reason_str, evidence=f"turns={client_turns}, answers={ans_count}")


def rule_location_and_property_type(lead: Any, answers: dict[str, Any] | None = None) -> RuleResult:
    """
    Component 6: Location and property-type clarity (Max 10 points).
    Both specified = 10 pts; one specified = 5 pts.
    """
    p_type = _get_val(lead, answers, "property_type")
    loc = _get_val(lead, answers, "preferred_location") or _get_val(lead, answers, "location")

    points = 0
    reasons = []

    if p_type:
        points += 5
        reasons.append(f"Property type specified ({p_type}) (+5)")
    if loc:
        points += 5
        reasons.append(f"Location specified ({loc}) (+5)")

    clamped = min(10, points)
    reason_str = "; ".join(reasons) if reasons else "Location & property type unspecified (0/10)"
    evidence_str = f"type={p_type}, loc={loc}" if (p_type or loc) else None
    return RuleResult("location_and_property_type", clamped, 10, reason_str, evidence=evidence_str)
