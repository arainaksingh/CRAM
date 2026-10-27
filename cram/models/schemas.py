import json
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator

EventStatus = Literal["planned", "ongoing", "completed", "cancelled"]
TemporalType = Literal["point", "interval", "relative", "unspecified"]

def get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class MessageCreate(BaseModel):
    message_id: Optional[str] = None
    session_id: str
    role: str = Field(..., description="'user', 'assistant', or 'system'")
    content: str
    timestamp: Optional[str] = None

    model_config = {"extra": "forbid"}

    @field_validator("timestamp", mode="before")
    def set_timestamp(cls, v):
        if not v:
            return get_utc_now_iso()
        return v

class MessageRecord(BaseModel):
    message_id: str
    session_id: str
    role: str
    content: str
    timestamp: str
    turn_index: int = Field(default=1, ge=1)
    is_processed: bool = False
    retry_count: int = 0
    error_message: Optional[str] = None

class CandidateFact(BaseModel):
    """
    Schema enforced on LLM output for structured fact extraction.
    """
    subject_name: str = Field(
        ...,
        description="The entity this fact is about (e.g. 'User', 'Alice', 'Google', 'Brother'). Use 'User' if the speaker speaks about themselves."
    )
    entity_type: str = Field(
        ...,
        description="Category of the entity: 'user', 'person', 'organization', 'project', 'location', or 'other'."
    )
    attribute: str = Field(
        ...,
        description="Dynamic name of the relationship or attribute (e.g. 'residence', 'employer', 'grad_year', 'age', 'hobbies')."
    )
    value: Union[str, int, float, bool, List[str]] = Field(
        ...,
        description="The value of the attribute. Can be a string, number, boolean, or array of strings."
    )
    raw_value: str = Field(
        ...,
        description="Human-readable text representation of the value."
    )
    evidence_quote: str = Field(
        ...,
        description="Verbatim substring quote from the source message supporting this assertion."
    )
    source_message_ids: List[str] = Field(
        default_factory=list,
        description="List of message IDs containing the evidence."
    )
    temporal_expression: Optional[str] = Field(
        default=None,
        description="Original temporal expression mentioned (e.g. 'in 2024', 'last summer', 'now') or null if none."
    )
    event_start: Optional[str] = Field(
        default=None,
        description="Normalized ISO start timestamp/date (e.g. '2024-04-20', '2024-04', '2024') or null."
    )
    event_end: Optional[str] = Field(
        default=None,
        description="Normalized ISO end timestamp/date (e.g. '2024-04-20', '2024-04', '2024') or null."
    )
    event_status: Optional[EventStatus] = Field(
        default=None,
        description="Event status: 'planned', 'ongoing', 'completed', or 'cancelled'."
    )
    temporal_type: Optional[TemporalType] = Field(
        default=None,
        description="Temporal granularity or type: 'point', 'interval', 'relative', or 'unspecified'."
    )
    assertion_intent: Optional[str] = Field(
        default="statement",
        description="'statement', 'correction', 'update', or 'retraction'."
    )
    confirmation_quote: Optional[str] = Field(
        default=None,
        description="Verbatim user confirmation quote if confirming an assistant statement."
    )
    confirmation_message_id: Optional[str] = Field(
        default=None,
        description="Message ID of the user confirmation if confirming an assistant statement."
    )

    @field_validator("event_status", mode="before")
    def normalize_candidate_event_status(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("temporal_type", mode="before")
    def normalize_candidate_temporal_type(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

class ExtractionResult(BaseModel):
    """Container for candidate facts extracted from a batch of messages."""
    facts: List[CandidateFact] = Field(default_factory=list)

class EntityRecord(BaseModel):
    entity_id: str
    name: str
    entity_type: str
    canonical_name: str
    created_at: str

class FactIdentityRecord(BaseModel):
    fact_id: str
    entity_id: str
    attribute: str
    canonical_attribute: str
    current_assertion_id: Optional[str] = None
    status: str = "active"
    created_at: str
    updated_at: str

class AssertionRecord(BaseModel):
    assertion_id: str
    fact_id: Optional[str] = None
    entity_id: str
    attribute: str
    value_json: str
    raw_value: str
    temporal_expression: Optional[str] = None
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    event_status: Optional[EventStatus] = None
    temporal_type: Optional[TemporalType] = None
    assertion_type: str
    supersedes_assertion_id: Optional[str] = None
    session_id: str
    confidence: float = 1.0
    created_at: str

    @field_validator("event_status", mode="before")
    def normalize_assertion_event_status(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("temporal_type", mode="before")
    def normalize_assertion_temporal_type(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    def parsed_value(self) -> Any:
        try:
            return json.loads(self.value_json)
        except Exception:
            return self.raw_value

class EvidenceRecord(BaseModel):
    evidence_id: str
    assertion_id: str
    message_id: str
    evidence_quote: str
    is_exact_match: bool = True
    validation_status: str = "valid"
    validation_notes: Optional[str] = None
    created_at: str

class DecisionRecord(BaseModel):
    decision_id: str
    session_id: str
    candidate_assertion_id: Optional[str] = None
    matched_fact_id: Optional[str] = None
    matched_assertion_id: Optional[str] = None
    decision_type: str
    reason: str
    confidence: float = 1.0
    created_at: str

class ValidationFailureRecord(BaseModel):
    failure_id: str
    session_id: str
    message_id: Optional[str] = None
    candidate_json: str
    failure_reason: str
    created_at: str
