"""
Temporal Validator for CRAM.

Provides:
1. Validation of structured date bounds and detection of inverted intervals
   (where event_start is strictly after event_end).
2. Proper handling of partial date precision (YYYY, YYYY-MM, YYYY-MM-DD)
   without pretending an exact day or timestamp was specified.
3. Preservation of unknown/ambiguous temporal expressions.
4. Strict, multi-granularity temporal comparison between assertions
   (strictly_before, strictly_after, same, during, contains, ambiguous_overlap, unspecified).
"""

import calendar
from datetime import date, datetime
import re
from typing import Literal, NamedTuple, Optional, Tuple, Union
from pydantic import BaseModel

TemporalRelation = Literal[
    "strictly_before",
    "strictly_after",
    "same",
    "during",
    "contains",
    "ambiguous_overlap",
    "unspecified",
]


class TemporalComparison(NamedTuple):
    """Result of comparing two temporal bounds."""

    relation: TemporalRelation
    description: str


class TemporalValidationResult(BaseModel):
    """Result of temporal bounds validation."""

    is_valid: bool
    failure_reason: Optional[str] = None


def parse_iso_date_to_bounds(val: Optional[str]) -> Optional[Tuple[date, date]]:
    """
    Maps an ISO date string to calendar date bounds [earliest_date, latest_date]:
    - 'YYYY' (len 4) -> [YYYY-01-01, YYYY-12-31]
    - 'YYYY-MM' (len 7) -> [YYYY-MM-01, YYYY-MM-last_day]
    - 'YYYY-MM-DD' (len 10) -> [YYYY-MM-DD, YYYY-MM-DD]
    - 'YYYY-MM-DDTHH:MM:SS...' -> [YYYY-MM-DD, YYYY-MM-DD]

    Returns None if val is None, empty, or an invalid calendar date.
    """
    if not val:
        return None
    cleaned = val.strip()
    if not cleaned:
        return None

    # Check for YYYY-MM-DD (or full ISO timestamp)
    m_day = re.match(r"^(\d{4})-(\d{2})-(\d{2})", cleaned)
    if m_day:
        y, m, d = int(m_day.group(1)), int(m_day.group(2)), int(m_day.group(3))
        try:
            day_obj = date(y, m, d)
            return (day_obj, day_obj)
        except ValueError:
            return None

    # Check for YYYY-MM
    m_month = re.match(r"^(\d{4})-(\d{2})$", cleaned)
    if m_month:
        y, m = int(m_month.group(1)), int(m_month.group(2))
        if 1 <= m <= 12:
            try:
                last_day = calendar.monthrange(y, m)[1]
                return (date(y, m, 1), date(y, m, last_day))
            except ValueError:
                return None
        return None

    # Check for YYYY
    m_year = re.match(r"^(\d{4})$", cleaned)
    if m_year:
        y = int(m_year.group(1))
        if 1000 <= y <= 9999:
            try:
                return (date(y, 1, 1), date(y, 12, 31))
            except ValueError:
                return None
        return None

    return None


def validate_temporal_bounds(
    event_start: Optional[str],
    event_end: Optional[str],
    temporal_type: Optional[str] = None,
) -> TemporalValidationResult:
    """
    Validates temporal bounds:
    - If both start and end are provided, verifies that start does not occur after end.
    - Validates that partial date expressions represent real calendar years/months/days.
    - Preserves unknown or unspecified bounds without inventing dates.
    """
    # If neither is provided, bounds are unknown/unspecified -> valid
    if not event_start and not event_end:
        return TemporalValidationResult(is_valid=True)

    bounds_start: Optional[Tuple[date, date]] = None
    bounds_end: Optional[Tuple[date, date]] = None

    if event_start:
        bounds_start = parse_iso_date_to_bounds(event_start)
        # Check if event_start looks like a date attempt but failed calendar validation (e.g. 2024-02-30)
        if bounds_start is None and re.match(r"^\d{4}(-\d{2})?(-\d{2})?", event_start.strip()):
            return TemporalValidationResult(
                is_valid=False,
                failure_reason=f"Invalid calendar date in event_start: '{event_start}'.",
            )

    if event_end:
        bounds_end = parse_iso_date_to_bounds(event_end)
        # Check if event_end looks like a date attempt but failed calendar validation
        if bounds_end is None and re.match(r"^\d{4}(-\d{2})?(-\d{2})?", event_end.strip()):
            return TemporalValidationResult(
                is_valid=False,
                failure_reason=f"Invalid calendar date in event_end: '{event_end}'.",
            )

    # If both bounds are present and valid, verify chronological order
    if bounds_start and bounds_end:
        earliest_start, latest_start = bounds_start
        earliest_end, latest_end = bounds_end

        # Inverted interval: start is strictly after end
        if earliest_start > latest_end:
            return TemporalValidationResult(
                is_valid=False,
                failure_reason=(
                    f"Temporal interval start '{event_start}' occurs after interval end '{event_end}'."
                ),
            )

    return TemporalValidationResult(is_valid=True)


def compare_temporal_bounds(
    start_a: Optional[str],
    end_a: Optional[str],
    start_b: Optional[str],
    end_b: Optional[str],
) -> TemporalComparison:
    """
    Compares two temporal entities A and B across different granularities.

    Returns:
        TemporalComparison(relation, description) where relation is one of:
        - 'strictly_before': All possible times of A are strictly before all possible times of B.
        - 'strictly_after': All possible times of A are strictly after all possible times of B.
        - 'same': Both A and B specify the exact same point or interval bounds.
        - 'during': A is fully contained within B.
        - 'contains': B is fully contained within A.
        - 'ambiguous_overlap': A and B overlap partially with uncertain chronological precedence.
        - 'unspecified': Date bounds are missing on A or B.
    """
    bounds_a_start = parse_iso_date_to_bounds(start_a)
    bounds_b_start = parse_iso_date_to_bounds(start_b)

    if not bounds_a_start or not bounds_b_start:
        return TemporalComparison(
            relation="unspecified",
            description="Insufficient structured date bounds to compare ordering",
        )

    bounds_a_end = parse_iso_date_to_bounds(end_a) if end_a else bounds_a_start
    bounds_b_end = parse_iso_date_to_bounds(end_b) if end_b else bounds_b_start

    a_min = bounds_a_start[0]
    a_max = (bounds_a_end or bounds_a_start)[1]
    b_min = bounds_b_start[0]
    b_max = (bounds_b_end or bounds_b_start)[1]

    # 1. Strictly Before: A fully ends before B begins
    if a_max < b_min:
        return TemporalComparison(
            relation="strictly_before",
            description=f"Event A ({start_a}) concluded before Event B ({start_b}) started",
        )

    # 2. Strictly After: A begins after B fully ends
    if a_min > b_max:
        return TemporalComparison(
            relation="strictly_after",
            description=f"Event A ({start_a}) started after Event B ({start_b}) concluded",
        )

    # 3. Exact Same Bounds
    if a_min == b_min and a_max == b_max:
        return TemporalComparison(
            relation="same",
            description=f"Event A ({start_a}) and Event B ({start_b}) have identical temporal bounds",
        )

    # 4. A is during B (A is contained inside B)
    if b_min <= a_min and a_max <= b_max:
        return TemporalComparison(
            relation="during",
            description=f"Event A ({start_a}) falls within the duration of Event B ({start_b})",
        )

    # 5. A contains B (B is contained inside A)
    if a_min <= b_min and b_max <= a_max:
        return TemporalComparison(
            relation="contains",
            description=f"Event A ({start_a}) spans across Event B ({start_b})",
        )

    # 6. Ambiguous / Partial Overlap
    return TemporalComparison(
        relation="ambiguous_overlap",
        description=f"Event A ({start_a}) and Event B ({start_b}) overlap with ambiguous precedence",
    )
