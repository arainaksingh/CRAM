import json
import re
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel
from cram.models.schemas import (
    AssertionRecord,
    CandidateFact,
    EntityRecord,
    FactIdentityRecord,
)
from cram.temporal.validator import compare_temporal_bounds

class ResolutionResult(BaseModel):
    decision_type: str  # 'NEW', 'DUPLICATE', 'UPDATE', 'CORRECTION', 'UNRESOLVED', 'HISTORICAL'
    matched_fact_id: Optional[str] = None
    matched_assertion_id: Optional[str] = None
    reason: str
    confidence: float = 1.0

# Attribute normalization / alias dictionary to detect semantic equivalence
ATTRIBUTE_FAMILIES = {
    "residence": ["residence", "live in", "lives in", "reside in", "resides in", "living in", "home_city", "current_city", "city"],
    "workplace": ["workplace", "work in", "works in", "work location", "office location", "works at office in"],
    "employer": ["employer", "works at", "works for", "company", "employed by", "organization"],
    "age": ["age", "years old"],
    "education": ["education", "degree", "studied at", "alma mater", "graduated from", "university", "college"],
}

CORRECTION_CUES = [
    "earlier statement was wrong",
    "was wrong",
    "i was mistaken",
    "mistake",
    "correction",
    "correcting",
    "actually i meant",
    "not delhi, not",
    "retract",
]

UPDATE_CUES = [
    "moved to",
    "relocated to",
    "now live",
    "i live here now",
    "currently living",
    "changed to",
    "switched to",
    "new job",
    "promoted to",
]

HISTORICAL_CUES = [
    "used to live",
    "used to work",
    "used to be",
    "used to",
    "previously lived",
    "previously worked",
    "previously",
    "in the past",
    "formerly",
    "back in",
    "earlier in",
]

AMBIGUOUS_CUES = [
    "spend time in",
    "spending time",
    "travel to",
    "visit",
    "sometimes",
    "part-time",
    "frequently stay in",
]

VALID_STATUS_TRANSITIONS: Dict[str, set[str]] = {
    "planned": {"ongoing", "completed", "cancelled"},
    "ongoing": {"completed", "cancelled"},
}

def canonicalize_attribute(attr: str) -> str:
    cleaned = re.sub(r"[_\s]+", " ", attr.strip().lower())
    for family, aliases in ATTRIBUTE_FAMILIES.items():
        if cleaned == family or cleaned in aliases:
            return family
    return cleaned.replace(" ", "_")

def normalize_value(val: Any) -> str:
    if isinstance(val, (int, float, bool)):
        return str(val).lower()
    if isinstance(val, str):
        return re.sub(r"\s+", " ", val.strip().lower())
    if isinstance(val, (list, dict)):
        return json.dumps(val, sort_keys=True)
    return str(val).strip().lower()

class FactResolver:
    def resolve_candidate(
        self,
        candidate: CandidateFact,
        entity: EntityRecord,
        existing_facts: List[FactIdentityRecord],
        active_assertions_by_fact_id: Dict[str, AssertionRecord],
    ) -> ResolutionResult:
        """
        Compares candidate assertion against existing facts and active assertions
        across all sessions to categorize into:
        NEW, DUPLICATE, UPDATE, CORRECTION, or UNRESOLVED.
        """
        cand_canonical_attr = canonicalize_attribute(candidate.attribute)
        cand_val_norm = normalize_value(candidate.value)
        cand_raw_norm = normalize_value(candidate.raw_value)
        quote_lower = (candidate.evidence_quote or "").lower()
        intent = (candidate.assertion_intent or "statement").lower()

        # Find matching fact identity for this entity + canonical attribute
        matched_fact: Optional[FactIdentityRecord] = None
        for fact in existing_facts:
            if fact.entity_id == entity.entity_id and fact.canonical_attribute == cand_canonical_attr:
                matched_fact = fact
                break

        # If no fact exists for this entity and attribute family -> NEW
        if not matched_fact:
            return ResolutionResult(
                decision_type="NEW",
                matched_fact_id=None,
                matched_assertion_id=None,
                reason=f"No prior fact recorded for entity '{entity.name}' with attribute '{cand_canonical_attr}'.",
                confidence=1.0,
            )

        # Retrieve active assertion for this fact
        active_assertion = active_assertions_by_fact_id.get(matched_fact.fact_id)
        if not active_assertion:
            return ResolutionResult(
                decision_type="NEW",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=None,
                reason=f"Fact identity exists for '{cand_canonical_attr}' but has no prior active assertion.",
                confidence=1.0,
            )

        active_val_norm = normalize_value(active_assertion.parsed_value())
        active_raw_norm = normalize_value(active_assertion.raw_value)

        # 1. Check for Explicit Correction
        is_correction = intent == "correction" or any(cue in quote_lower for cue in CORRECTION_CUES)
        if is_correction:
            return ResolutionResult(
                decision_type="CORRECTION",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=(
                    f"Explicit correction detected for '{cand_canonical_attr}'. "
                    f"Prior claim '{active_assertion.raw_value}' corrected to '{candidate.raw_value}'."
                ),
                confidence=0.95,
            )

        # 2. Check for Equivalent / Duplicate Fact & Status Transitions
        is_val_equal = (cand_val_norm == active_val_norm) or (cand_raw_norm == active_raw_norm)

        # Status transition checks
        cand_status = candidate.event_status
        active_status = active_assertion.event_status

        # Detect contradictory status transitions (e.g. completed -> cancelled without correction)
        if active_status == "completed" and cand_status == "cancelled":
            return ResolutionResult(
                decision_type="UNRESOLVED",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=(
                    f"Contradictory event status for '{cand_canonical_attr}': active event is recorded as 'completed', "
                    f"but candidate claims 'cancelled' without explicit correction."
                ),
                confidence=0.7,
            )
        if active_status == "cancelled" and cand_status in ("ongoing", "completed"):
            return ResolutionResult(
                decision_type="UNRESOLVED",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=(
                    f"Contradictory event status for '{cand_canonical_attr}': active event was 'cancelled', "
                    f"but candidate claims '{cand_status}' without explicit correction."
                ),
                confidence=0.7,
            )

        has_status_transition = False
        if active_status and cand_status and cand_status != active_status:
            if cand_status in VALID_STATUS_TRANSITIONS.get(active_status, set()):
                has_status_transition = True
        elif not active_status and cand_status == "cancelled":
            has_status_transition = True

        if is_val_equal and not has_status_transition:
            return ResolutionResult(
                decision_type="DUPLICATE",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=(
                    f"Semantic duplicate detected for '{cand_canonical_attr}'. "
                    f"Existing active value '{active_assertion.raw_value}' matches candidate '{candidate.raw_value}'."
                ),
                confidence=1.0,
            )

        # If values match but status transitioned (e.g. trip planned -> cancelled)
        if is_val_equal and has_status_transition:
            reason = (
                f"Event status transition detected for '{cand_canonical_attr}': "
                f"'{active_status}' -> '{cand_status}'."
            )
            if cand_status == "cancelled":
                reason += " Cancellation recorded without inferring event occurrence."
            return ResolutionResult(
                decision_type="UPDATE",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=reason,
                confidence=0.95,
            )

        # 3. Check Ambiguous cues (e.g. 'spend time in', 'visit')
        is_ambiguous = any(cue in quote_lower for cue in AMBIGUOUS_CUES)
        if is_ambiguous:
            return ResolutionResult(
                decision_type="UNRESOLVED",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=(
                    f"Ambiguous statement for '{cand_canonical_attr}'. "
                    f"Candidate claim '{candidate.raw_value}' indicates non-exclusive or temporary presence "
                    f"without replacing established active value '{active_assertion.raw_value}'."
                ),
                confidence=0.8,
            )

        # 4. Status transition on different value or explicit cancellation
        if has_status_transition:
            reason = (
                f"Status transition for '{cand_canonical_attr}': '{active_status}' -> '{cand_status}'."
            )
            if cand_status == "cancelled":
                reason += " Cancellation recorded without inferring event occurrence."
            return ResolutionResult(
                decision_type="UPDATE",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=reason,
                confidence=0.9,
            )

        # 5. Check for Historical Language in Evidence Quote
        is_historical_cue = any(cue in quote_lower for cue in HISTORICAL_CUES)

        # 6. Temporal Comparison between Candidate and Active Assertion
        comp = compare_temporal_bounds(
            candidate.event_start,
            candidate.event_end,
            active_assertion.event_start,
            active_assertion.event_end,
        )

        # 6A. Candidate is strictly in the past relative to active assertion
        if comp.relation == "strictly_before" or (is_historical_cue and comp.relation != "strictly_after"):
            return ResolutionResult(
                decision_type="HISTORICAL",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=None,
                reason=(
                    f"Historical statement detected for '{cand_canonical_attr}': candidate event '{candidate.event_start or candidate.raw_value}' "
                    f"occurred prior to established active state '{active_assertion.raw_value}' "
                    f"('{active_assertion.event_start or 'current'}'). "
                    f"Preserving historical assertion without superseding active state."
                ),
                confidence=0.95,
            )

        # 6B. Candidate is strictly newer than active assertion (Temporal Progression)
        if comp.relation == "strictly_after":
            return ResolutionResult(
                decision_type="UPDATE",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=(
                    f"Temporal progression detected for '{cand_canonical_attr}': candidate event "
                    f"('{candidate.event_start}') occurred after active assertion ('{active_assertion.event_start}'). "
                    f"Superseding prior state."
                ),
                confidence=0.95,
            )

        # 6C. Contradictory claims at the exact same point in time
        if comp.relation == "same":
            return ResolutionResult(
                decision_type="UNRESOLVED",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=(
                    f"Contradictory temporal claim for '{cand_canonical_attr}': conflicting values "
                    f"('{candidate.raw_value}' vs active '{active_assertion.raw_value}') asserted for the same time "
                    f"'{candidate.event_start}' without explicit correction."
                ),
                confidence=0.7,
            )

        # 6D. Ambiguous or partial overlap
        if comp.relation in ("during", "contains", "ambiguous_overlap"):
            is_explicit_update = intent == "update" or any(cue in quote_lower for cue in UPDATE_CUES)
            if not is_explicit_update:
                return ResolutionResult(
                    decision_type="UNRESOLVED",
                    matched_fact_id=matched_fact.fact_id,
                    matched_assertion_id=active_assertion.assertion_id,
                    reason=(
                        f"Ambiguous temporal ordering for '{cand_canonical_attr}': candidate event ('{candidate.event_start}') "
                        f"and active assertion ('{active_assertion.event_start}') overlap ({comp.relation}). "
                        f"Insufficient evidence to establish chronological order."
                    ),
                    confidence=0.75,
                )

        # 7. Unspecified bounds fallback
        is_explicit_update = intent == "update" or any(cue in quote_lower for cue in UPDATE_CUES)
        is_numerical = False
        try:
            float(active_val_norm)
            float(cand_val_norm)
            is_numerical = True
        except ValueError:
            is_numerical = False

        if is_explicit_update or is_numerical:
            reason_detail = (
                f"Numerical value change from '{active_assertion.raw_value}' to '{candidate.raw_value}'"
                if is_numerical
                else "Temporal or state transition cue detected in evidence"
            )
            return ResolutionResult(
                decision_type="UPDATE",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=f"Update detected for '{cand_canonical_attr}': {reason_detail}.",
                confidence=0.9,
            )

        if candidate.temporal_expression and candidate.temporal_expression.lower() in ["now", "currently", "recent"]:
            return ResolutionResult(
                decision_type="UPDATE",
                matched_fact_id=matched_fact.fact_id,
                matched_assertion_id=active_assertion.assertion_id,
                reason=(
                    f"Update detected for '{cand_canonical_attr}' based on temporal cue "
                    f"'{candidate.temporal_expression}'."
                ),
                confidence=0.85,
            )

        # Default fallback for conflicting values with no clear transition or correction
        return ResolutionResult(
            decision_type="UPDATE",
            matched_fact_id=matched_fact.fact_id,
            matched_assertion_id=active_assertion.assertion_id,
            reason=(
                f"Different value asserted for '{cand_canonical_attr}' "
                f"('{candidate.raw_value}' vs active '{active_assertion.raw_value}'). "
                f"Preserving both assertions with lineage link."
            ),
            confidence=0.75,
        )
