import json
import logging
from typing import Dict, List, Optional
from pydantic import BaseModel
from cram.db.repository import Repository
from cram.extraction.extractor import FactExtractor
from cram.models.schemas import CandidateFact
from cram.pipeline.evidence_validator import EvidenceValidator
from cram.pipeline.resolver import FactResolver, canonicalize_attribute
from cram.temporal.normalizer import normalize_temporal
from cram.temporal.validator import validate_temporal_bounds

logger = logging.getLogger(__name__)

class ProcessingSummary(BaseModel):
    session_id: str
    messages_processed: int
    assertions_created: int
    decisions_count: Dict[str, int]
    validation_failures_count: int
    status: str

class SessionProcessor:
    def __init__(
        self,
        repository: Repository,
        extractor: Optional[FactExtractor] = None,
        validator: Optional[EvidenceValidator] = None,
        resolver: Optional[FactResolver] = None,
    ):
        self.repo = repository
        self.extractor = extractor or FactExtractor()
        self.validator = validator or EvidenceValidator()
        self.resolver = resolver or FactResolver()

    def process_session(self, session_id: str) -> ProcessingSummary:
        """
        Processes unprocessed messages in the specified session:
        1. Gathers unprocessed messages and dialogue context.
        2. Extracts candidate assertions via Gemini structured output.
        3. Validates evidence grounding and message IDs.
        4. Cross-references all existing facts across sessions.
        5. Persists assertions, evidence, and audit decisions in SQLite.
        6. Safely marks messages as processed.
        """
        session = self.repo.get_session(session_id)
        if not session:
            raise ValueError(f"Session '{session_id}' does not exist.")

        self.repo.update_session_status(session_id, "processing")

        all_messages = self.repo.get_session_messages(session_id)
        unprocessed_messages = [m for m in all_messages if not m.is_processed]

        if not unprocessed_messages:
            self.repo.update_session_status(session_id, "completed")
            return ProcessingSummary(
                session_id=session_id,
                messages_processed=0,
                assertions_created=0,
                decisions_count={},
                validation_failures_count=0,
                status="completed",
            )

        messages_by_id = {m.message_id: m for m in all_messages}
        unprocessed_ids = [m.message_id for m in unprocessed_messages]

        # Call extraction with full session context
        try:
            candidate_facts: List[CandidateFact] = self.extractor.extract_facts(all_messages)
        except Exception as e:
            logger.error(f"Extraction failed for session {session_id}: {e}")
            for m_id in unprocessed_ids:
                self.repo.increment_message_retry(m_id, str(e))
            self.repo.update_session_status(session_id, "error")
            raise

        decisions_count: Dict[str, int] = {
            "NEW": 0,
            "DUPLICATE": 0,
            "UPDATE": 0,
            "CORRECTION": 0,
            "UNRESOLVED": 0,
        }
        assertions_created = 0
        failures_count = 0

        # Load all existing facts & active assertions across all sessions
        existing_facts = self.repo.list_fact_identities()
        active_assertions_by_fact_id = {}
        for f in existing_facts:
            if f.current_assertion_id:
                ass = self.repo.get_assertion(f.current_assertion_id)
                if ass:
                    active_assertions_by_fact_id[f.fact_id] = ass

        # Process each candidate fact
        for candidate in candidate_facts:
            # 1. Evidence Validation
            validation_result = self.validator.validate_fact_evidence(candidate, messages_by_id)

            if not validation_result.is_valid:
                failures_count += 1
                first_msg_id = candidate.source_message_ids[0] if candidate.source_message_ids else None
                self.repo.insert_validation_failure(
                    session_id=session_id,
                    message_id=first_msg_id,
                    candidate_json=candidate.model_dump_json(),
                    failure_reason=validation_result.failure_reason or "Unknown validation error",
                )
                logger.warning(
                    f"Rejected ungrounded claim: {candidate.subject_name} | {candidate.attribute} -> "
                    f"{validation_result.failure_reason}"
                )
                continue

            # Check if this candidate fact originated solely from already-processed messages
            # (to prevent duplicate insertions on re-processing)
            candidate_is_from_new = any(
                item.message_id in unprocessed_ids for item in validation_result.evidence_items
            )
            if not candidate_is_from_new:
                # This fact belongs entirely to earlier messages that were already processed
                continue

            # 2. Derive or normalize structured temporal fields
            ev_start = candidate.event_start
            ev_end = candidate.event_end
            ev_status = candidate.event_status
            temp_type = candidate.temporal_type

            if (ev_start is None and ev_end is None and temp_type is None) and candidate.temporal_expression:
                ref_ts = None
                for ev_item in validation_result.evidence_items:
                    ref_msg = messages_by_id.get(ev_item.message_id)
                    if ref_msg and ref_msg.timestamp:
                        ref_ts = ref_msg.timestamp
                        break
                if not ref_ts and candidate.source_message_ids:
                    for sm_id in candidate.source_message_ids:
                        ref_msg = messages_by_id.get(sm_id)
                        if ref_msg and ref_msg.timestamp:
                            ref_ts = ref_msg.timestamp
                            break
                norm = normalize_temporal(candidate.temporal_expression, reference_time=ref_ts)
                ev_start = norm.event_start
                ev_end = norm.event_end
                ev_status = ev_status or norm.event_status
                temp_type = norm.temporal_type

            # 3. Temporal Bounds Validation
            temp_val_res = validate_temporal_bounds(
                event_start=ev_start,
                event_end=ev_end,
                temporal_type=temp_type,
            )
            if not temp_val_res.is_valid:
                failures_count += 1
                first_msg_id = candidate.source_message_ids[0] if candidate.source_message_ids else None
                self.repo.insert_validation_failure(
                    session_id=session_id,
                    message_id=first_msg_id,
                    candidate_json=candidate.model_dump_json(),
                    failure_reason=temp_val_res.failure_reason or "Invalid temporal bounds",
                )
                logger.warning(
                    f"Rejected temporally invalid claim: {candidate.subject_name} | {candidate.attribute} -> "
                    f"{temp_val_res.failure_reason}"
                )
                continue

            # Populate validated temporal attributes onto candidate
            candidate.event_start = ev_start
            candidate.event_end = ev_end
            candidate.event_status = ev_status
            candidate.temporal_type = temp_type

            # 4. Entity Resolution
            entity = self.repo.get_or_create_entity(
                name=candidate.subject_name,
                entity_type=candidate.entity_type,
            )

            # 5. Temporal-Aware Decision & Comparison
            decision = self.resolver.resolve_candidate(
                candidate=candidate,
                entity=entity,
                existing_facts=existing_facts,
                active_assertions_by_fact_id=active_assertions_by_fact_id,
            )

            canonical_attr = canonicalize_attribute(candidate.attribute)
            val_json = json.dumps(candidate.value)

            # 6. Atomic Persistence based on Decision
            fact_id = decision.matched_fact_id
            if not fact_id:
                # Create new fact identity
                new_fact = self.repo.create_fact_identity(
                    entity_id=entity.entity_id,
                    attribute=candidate.attribute,
                    canonical_attribute=canonical_attr,
                )
                fact_id = new_fact.fact_id
                existing_facts.append(new_fact)

            # Insert assertion
            supersedes_id = decision.matched_assertion_id if decision.decision_type in ("UPDATE", "CORRECTION") else None
            assertion = self.repo.insert_assertion(
                entity_id=entity.entity_id,
                attribute=candidate.attribute,
                value_json=val_json,
                raw_value=candidate.raw_value,
                session_id=session_id,
                assertion_type=decision.decision_type,
                fact_id=fact_id,
                temporal_expression=candidate.temporal_expression,
                event_start=candidate.event_start,
                event_end=candidate.event_end,
                event_status=candidate.event_status,
                temporal_type=candidate.temporal_type,
                supersedes_assertion_id=supersedes_id,
                confidence=decision.confidence,
            )
            assertions_created += 1
            decisions_count[decision.decision_type] = decisions_count.get(decision.decision_type, 0) + 1

            # Update active fact pointer if appropriate
            if decision.decision_type in ("NEW", "UPDATE", "CORRECTION"):
                self.repo.update_fact_identity_active_assertion(
                    fact_id=fact_id,
                    current_assertion_id=assertion.assertion_id,
                    status="active",
                )
                active_assertions_by_fact_id[fact_id] = assertion
            # For DUPLICATE, UNRESOLVED, or HISTORICAL: we do NOT mutate current_assertion_id

            # Insert evidence links
            for ev in validation_result.evidence_items:
                self.repo.insert_evidence(
                    assertion_id=assertion.assertion_id,
                    message_id=ev.message_id,
                    evidence_quote=ev.quote,
                    is_exact_match=ev.is_exact_match,
                    validation_status=ev.status,
                    validation_notes=ev.notes,
                )

            # Log decision audit record
            self.repo.insert_decision(
                session_id=session_id,
                decision_type=decision.decision_type,
                reason=decision.reason,
                candidate_assertion_id=assertion.assertion_id,
                matched_fact_id=fact_id,
                matched_assertion_id=decision.matched_assertion_id,
                confidence=decision.confidence,
            )

        # Safely mark all newly processed messages
        self.repo.mark_messages_processed(unprocessed_ids)
        self.repo.update_session_status(session_id, "completed")

        return ProcessingSummary(
            session_id=session_id,
            messages_processed=len(unprocessed_ids),
            assertions_created=assertions_created,
            decisions_count=decisions_count,
            validation_failures_count=failures_count,
            status="completed",
        )
