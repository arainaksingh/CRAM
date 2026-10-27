"""
Deterministic Temporal Normalizer for CRAM.

Normalizes raw temporal expressions and reference timestamps into structured
temporal representations (event_start, event_end, event_status, temporal_type).

Guiding Principles:
1. Message timestamps serve only as anchors for interpreting relative language,
   never automatically as event time.
2. 'Now' or 'currently' signifies ongoing status, never proof of a precise start date.
3. Completion or cancellation is never inferred from date expressions alone.
4. Month-only (e.g. 'April 2024') and year-only (e.g. 'in 2024') expressions preserve
   their true granularity (YYYY-MM and YYYY) without pretending an exact day was specified.
5. Vague expressions (e.g. 'last summer') and ambiguous dates (e.g. '04/05/2024') preserve
   the raw expression with no fabricated dates.
6. Relative expressions ('yesterday', 'today', 'tomorrow') resolve only when a valid
   reference timestamp is available.
"""

from datetime import date, datetime, timedelta
import re
from typing import Any, Literal, Optional, Tuple, Union
from pydantic import BaseModel, Field

EventStatus = Literal["planned", "ongoing", "completed", "cancelled"]
TemporalType = Literal["point", "interval", "relative", "unspecified"]

MONTH_NAMES = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

# Explicit relative expressions with relative day offsets
RELATIVE_OFFSETS = {
    "today": 0,
    "yesterday": -1,
    "tomorrow": 1,
    "the day before yesterday": -2,
    "day before yesterday": -2,
    "the day after tomorrow": 2,
    "day after tomorrow": 2,
}


class NormalizedTemporal(BaseModel):
    """Structured representation of normalized temporal information."""

    raw_expression: Optional[str] = None
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    event_status: Optional[EventStatus] = None
    temporal_type: Optional[TemporalType] = None


def parse_reference_date(reference_time: Optional[Union[str, datetime, date]]) -> Optional[date]:
    """Extracts a valid calendar date from a reference timestamp or string."""
    if reference_time is None:
        return None
    if isinstance(reference_time, datetime):
        return reference_time.date()
    if isinstance(reference_time, date):
        return reference_time
    if isinstance(reference_time, str):
        ref_str = reference_time.strip()
        if not ref_str:
            return None
        # Handle ISO timestamp strings, e.g. '2026-10-25T14:30:00Z'
        clean_str = ref_str.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(clean_str)
            return dt.date()
        except Exception:
            pass
        # Fallback: check YYYY-MM-DD
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", ref_str)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                return None
    return None


def _detect_event_status(text: str) -> Optional[EventStatus]:
    """
    Detects explicit event status cues. Never infers status from date values alone.
    """
    # Cancelled cues
    if re.search(r"\b(cancelled|canceled|called off|revoked|aborted)\b", text):
        return "cancelled"
    # Planned cues
    if re.search(r"\b(planned|scheduled|tentative|upcoming)\b", text):
        return "planned"
    # Ongoing cues
    if re.search(r"\b(ongoing|currently|presently|in progress|at present|as of now|right now)\b", text):
        return "ongoing"
    if re.search(r"\bnow\b", text):
        return "ongoing"
    # Completed cues
    if re.search(r"\b(completed|finished|concluded|graduated)\b", text):
        return "completed"

    return None


def _clean_prepositions(text: str) -> str:
    """Strips common leading/trailing temporal prepositions and articles."""
    cleaned = re.sub(
        r"^(on|in|during|at|for|around|as of|since|until|by|from)\s+",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    ).strip()
    return cleaned


def _parse_explicit_iso_date(s: str) -> Optional[Tuple[str, str, TemporalType]]:
    """
    Parses explicit ISO dates (YYYY-MM-DD) or intervals (from YYYY-MM-DD to YYYY-MM-DD).
    Returns (event_start, event_end, temporal_type) or None.
    """
    # Check for two ISO dates (interval)
    iso_pattern = r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\d)"
    matches = list(re.finditer(iso_pattern, s))

    if len(matches) == 2:
        m1, m2 = matches[0], matches[1]
        try:
            d1 = date(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)))
            d2 = date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
            iso1 = d1.isoformat()
            iso2 = d2.isoformat()
            if iso1 <= iso2:
                return (iso1, iso2, "interval")
            return (iso2, iso1, "interval")
        except ValueError:
            return None

    if len(matches) == 1:
        m = matches[0]
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            iso = d.isoformat()
            # If prepended by 'since', interval from start to open-ended
            if re.search(r"\bsince\b", s, re.IGNORECASE):
                return (iso, None, "interval")  # type: ignore[return-value]
            # If prepended by 'until' or 'before', open-ended to end
            if re.search(r"\b(until|before)\b", s, re.IGNORECASE):
                return (None, iso, "interval")  # type: ignore[return-value]
            return (iso, iso, "point")
        except ValueError:
            return None

    return None


def _parse_unambiguous_full_date(s: str) -> Optional[Tuple[str, str, TemporalType]]:
    """
    Parses unambiguous full dates with month names (e.g. '20 April 2024' or 'April 20, 2024').
    Returns (event_start, event_end, temporal_type) or None.
    """
    # Pattern 1: Day Month Year (e.g. '20 April 2024', '20th of April, 2024')
    p1 = r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)[,\s]+(\d{4})\b"
    # Pattern 2: Month Day Year (e.g. 'April 20, 2024', 'April 20th 2024')
    p2 = r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,\s]+(\d{4})\b"

    found_dates = []

    # Check for pattern 1 matches
    for m in re.finditer(p1, s, re.IGNORECASE):
        day_str, month_str, year_str = m.group(1), m.group(2).lower(), m.group(3)
        if month_str in MONTH_NAMES:
            try:
                d = date(int(year_str), MONTH_NAMES[month_str], int(day_str))
                found_dates.append(d)
            except ValueError:
                pass

    # If no pattern 1, check pattern 2
    if not found_dates:
        for m in re.finditer(p2, s, re.IGNORECASE):
            month_str, day_str, year_str = m.group(1).lower(), m.group(2), m.group(3)
            if month_str in MONTH_NAMES:
                try:
                    d = date(int(year_str), MONTH_NAMES[month_str], int(day_str))
                    found_dates.append(d)
                except ValueError:
                    pass

    if len(found_dates) == 2:
        iso1, iso2 = found_dates[0].isoformat(), found_dates[1].isoformat()
        if iso1 <= iso2:
            return (iso1, iso2, "interval")
        return (iso2, iso1, "interval")

    if len(found_dates) == 1:
        iso = found_dates[0].isoformat()
        if re.search(r"\bsince\b", s, re.IGNORECASE):
            return (iso, None, "interval")  # type: ignore[return-value]
        if re.search(r"\b(until|before)\b", s, re.IGNORECASE):
            return (None, iso, "interval")  # type: ignore[return-value]
        return (iso, iso, "point")

    return None


def _parse_month_only(s: str) -> Optional[Tuple[str, str, TemporalType]]:
    """
    Parses month-only expressions (e.g. 'April 2024', 'in 2024-04').
    Represented with month precision (YYYY-MM) without pretending an exact day was specified.
    """
    # 1. ISO month: YYYY-MM without trailing day
    iso_month_matches = list(re.finditer(r"(?<!\d)(\d{4})-(\d{2})(?!\d)", s))
    if iso_month_matches:
        valid_months = []
        for m in iso_month_matches:
            year, month = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12:
                valid_months.append(f"{year:04d}-{month:02d}")
        if len(valid_months) == 2:
            return (min(valid_months), max(valid_months), "interval")
        if len(valid_months) == 1:
            m_iso = valid_months[0]
            if re.search(r"\bsince\b", s, re.IGNORECASE):
                return (m_iso, None, "interval")  # type: ignore[return-value]
            return (m_iso, m_iso, "interval")

    # 2. Textual month: 'Month Year' or 'Month of Year'
    text_month_pattern = r"\b([a-z]+)[,\s]+(\d{4})\b"
    valid_text_months = []
    for m in re.finditer(text_month_pattern, s, re.IGNORECASE):
        month_name, year_str = m.group(1).lower(), m.group(2)
        if month_name in MONTH_NAMES:
            month_num = MONTH_NAMES[month_name]
            valid_text_months.append(f"{int(year_str):04d}-{month_num:02d}")

    if len(valid_text_months) == 2:
        return (min(valid_text_months), max(valid_text_months), "interval")
    if len(valid_text_months) == 1:
        m_iso = valid_text_months[0]
        if re.search(r"\bsince\b", s, re.IGNORECASE):
            return (m_iso, None, "interval")  # type: ignore[return-value]
        return (m_iso, m_iso, "interval")

    return None


def _parse_year_only(s: str) -> Optional[Tuple[str, str, TemporalType]]:
    """
    Parses year-only expressions (e.g. '2024', 'in 2024', 'from 2020 to 2024').
    Represented with year precision (YYYY) without pretending an exact month or day was specified.
    """
    # Match standalone 4-digit years between 1000 and 9999
    year_matches = [
        int(m.group(1))
        for m in re.finditer(r"(?<!\d)(\d{4})(?!\d)", s)
        if 1000 <= int(m.group(1)) <= 9999
    ]

    if len(year_matches) == 2:
        y1, y2 = year_matches[0], year_matches[1]
        return (str(min(y1, y2)), str(max(y1, y2)), "interval")

    if len(year_matches) == 1:
        y = str(year_matches[0])
        if re.search(r"\bsince\b", s, re.IGNORECASE):
            return (y, None, "interval")  # type: ignore[return-value]
        return (y, y, "interval")

    return None


def normalize_temporal(
    expression: Optional[str],
    reference_time: Optional[Union[str, datetime, date]] = None,
) -> NormalizedTemporal:
    """
    Normalizes a raw temporal expression into structured temporal attributes.

    Parameters:
        expression: The raw temporal expression extracted from conversational dialogue.
        reference_time: Source message timestamp or conversation turn reference time.

    Returns:
        NormalizedTemporal containing event_start, event_end, event_status, temporal_type.
    """
    if expression is None:
        return NormalizedTemporal()

    raw = expression.strip()
    if not raw:
        return NormalizedTemporal(raw_expression=raw)

    text_lower = raw.lower()

    # Step 1: Detect explicit event status cues (never inferred from date values alone)
    detected_status = _detect_event_status(text_lower)

    # Step 2: Parse reference date for relative language anchoring
    ref_date = parse_reference_date(reference_time)

    # Step 3: Check documented relative expressions (yesterday, today, tomorrow)
    cleaned_for_rel = _clean_prepositions(text_lower)
    for rel_expr, offset in RELATIVE_OFFSETS.items():
        if cleaned_for_rel == rel_expr or text_lower == rel_expr:
            if ref_date is not None:
                target_date = ref_date + timedelta(days=offset)
                iso = target_date.isoformat()
                return NormalizedTemporal(
                    raw_expression=raw,
                    event_start=iso,
                    event_end=iso,
                    event_status=detected_status,
                    temporal_type="relative",
                )
            else:
                # Reference time missing or invalid: preserve raw relative expression without inventing dates
                return NormalizedTemporal(
                    raw_expression=raw,
                    event_start=None,
                    event_end=None,
                    event_status=detected_status,
                    temporal_type="relative",
                )

    # Step 4: Standalone 'now', 'currently', 'presently'
    if cleaned_for_rel in ("now", "currently", "presently", "at present", "as of now"):
        return NormalizedTemporal(
            raw_expression=raw,
            event_start=None,
            event_end=None,
            event_status=detected_status or "ongoing",
            temporal_type="unspecified",
        )

    # Step 5: Check ambiguous date formats (e.g. MM/DD/YYYY vs DD/MM/YYYY)
    # Do not invent dates for ambiguous slash formats
    if re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", raw):
        return NormalizedTemporal(
            raw_expression=raw,
            event_start=None,
            event_end=None,
            event_status=detected_status,
            temporal_type="unspecified",
        )

    # Step 6: Explicit ISO full dates (YYYY-MM-DD)
    iso_res = _parse_explicit_iso_date(raw)
    if iso_res is not None:
        start_val, end_val, t_type = iso_res
        return NormalizedTemporal(
            raw_expression=raw,
            event_start=start_val,
            event_end=end_val,
            event_status=detected_status,
            temporal_type=t_type,
        )

    # Step 7: Unambiguous full dates with textual month (e.g. '20 April 2024')
    full_res = _parse_unambiguous_full_date(raw)
    if full_res is not None:
        start_val, end_val, t_type = full_res
        return NormalizedTemporal(
            raw_expression=raw,
            event_start=start_val,
            event_end=end_val,
            event_status=detected_status,
            temporal_type=t_type,
        )

    # Step 8: Month-only precision (e.g. 'April 2024', '2024-04')
    month_res = _parse_month_only(raw)
    if month_res is not None:
        start_val, end_val, t_type = month_res
        return NormalizedTemporal(
            raw_expression=raw,
            event_start=start_val,
            event_end=end_val,
            event_status=detected_status,
            temporal_type=t_type,
        )

    # Step 9: Year-only precision (e.g. 'in 2024')
    year_res = _parse_year_only(raw)
    if year_res is not None:
        start_val, end_val, t_type = year_res
        return NormalizedTemporal(
            raw_expression=raw,
            event_start=start_val,
            event_end=end_val,
            event_status=detected_status,
            temporal_type=t_type,
        )

    # Step 10: Unknown, vague (e.g. 'last summer'), or unsupported expressions
    return NormalizedTemporal(
        raw_expression=raw,
        event_start=None,
        event_end=None,
        event_status=detected_status,
        temporal_type="unspecified",
    )
